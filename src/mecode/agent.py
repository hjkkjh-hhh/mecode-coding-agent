"""Agent 主循环 —— 整个 harness 的心脏。

核心逻辑：
  加用户输入 → 循环{ 调模型 → 有工具就执行喂回 → 无工具就结束 }

在裸循环之上加了三道加固，应对常见失败模式：
  - 防跑飞：限制单轮最大循环次数，避免模型无限调工具
  - 空响应重试：模型返回空（无文字无工具）时有限次重试，而非静默结束
  - 调工具后未回复的兜底：注入提示让模型基于工具结果继续，而非直接结束

run_turn 把整轮过程作为【事件流】产出，自己不负责显示——
颜色、换行、打印由调用方消费事件来做，实现“编排”与“显示”的分离。
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Iterator

from .background import BackgroundManager, background_tools
from .compact import _reminder, compact, estimate_tokens
from .workflow import WorkflowRun, workflow_tools
from .subagent import SubagentRunner, subagent_tools
from .tasks import TaskManager, task_tools
from .config import AgentConfig, agent_config
from .events import (
    Done, Event, Notice, PlanProposed, ReasoningDelta, TextDelta, ToolCall, ToolResult,
    ToolStarted, Usage,
)
from .permission import ALLOW, ALWAYS, ASK, DENY, PermissionPolicy
from .provider import Provider
from .session import SessionStore
from .tools import ProcSlot, ToolRegistry, truncate_output

MAX_EMPTY_RETRIES = 2        # 空响应最多重试几次再放弃
MAX_PARALLEL_READS = 8       # 连续只读工具组的并发上限（一批几十个 grep/glob 不至于起线程风暴）

# 拒绝执行的统一文案（明确叫停，否则模型常换命令/工具反复重试、刷屏空转）
_DENY_TOOL = ("错误：用户拒绝执行工具 {name}。不要重试或换工具绕过该操作；"
              "直接简短告诉用户你想做什么、为何需要，然后停下等用户指示。")
# 无人可审批（无头模式：ask_permission=None）时【不能】说"用户拒绝"——用户根本没被问过，那是假信息，
# 模型会照此向上汇报。如实讲明是环境所限，并让它把没做成的事写清楚。
_DENY_NO_APPROVER = ("错误：工具 {name} 需要用户授权，但当前环境没有可审批的用户，已自动拒绝。"
                     "不要重试或换工具绕过；跳过该动作继续能做的部分，并在最终回复里写明"
                     "哪些步骤因未获授权而没有执行。")
_DENY_SUBAGENT = "错误：用户拒绝派生子 agent。直接告诉用户你想做什么、为何需要，然后停下。"


class AskContext:
    """交给审批回调的上下文：让 UI 知道【是谁在问】。

    interrupt = 提问方 agent 【正在用的】那个打断标志。审批循环盯它 → 该 agent 被停掉时（Ctrl+C 或
      kill_bgtask），卡在弹窗上的线程能立刻解开。此前循环写死盯主 agent 的标志，而后台子 agent 用的是
      BackgroundManager 给的另一个 Event → kill_bgtask 解不开它。
    is_sub = 提问方是不是子 agent（弹窗标题用，让用户知道自己在批哪一层）。
    can_stop = 这个 agent 是不是【跑在一个可单独停掉的后台任务里】——决定弹窗要不要给"停止此后台任务"。
      注意它不等于 is_sub：**前台子 agent 与主 agent 共享同一个 interrupt**（SubagentRunner 把
      主 agent 的 _interrupt 直接传给它），置它等于按 Ctrl+C 停掉整轮、连并发的兄弟子 agent 一起停 ——
      那一项对前台是骗人的。只有后台/workflow 子 agent 自带独立 Event，停它才真的只停这一条。"""
    __slots__ = ("interrupt", "is_sub", "can_stop")

    def __init__(self, interrupt: threading.Event, is_sub: bool, can_stop: bool = False) -> None:
        self.interrupt = interrupt
        self.is_sub = is_sub
        self.can_stop = can_stop


class _TurnInterrupted(Exception):
    """协作式打断：request_interrupt() 置位标志后，循环检查到就抛它，
    和 Ctrl-C(KeyboardInterrupt) 走同一个打断处理出口。"""


class Agent:
    def __init__(self, provider: Provider, tools: ToolRegistry,
                 system_prompt: str | None = None,
                 config: AgentConfig = agent_config,
                 store: SessionStore | None = None,
                 resume_messages: list[dict] | None = None,
                 policy: PermissionPolicy | None = None,
                 ask_permission: Callable[[str, dict], str] | None = None,
                 subagent: bool = False,
                 interrupt: threading.Event | None = None,
                 own_branch: bool = False,
                 persist_permission: "Callable[[str, str, str], None] | None" = None) -> None:
        self.provider = provider
        self.tools = tools
        # 运行行为配置（防跑飞/上下文上限/压缩阈值/保留轮数）；frozen dataclass，作默认值共享也安全
        self.config = config
        # 会话存储（注入；None=不落盘，旧行为/测试用）。单一真相：管 transcript.jsonl + 工具外置
        self.store = store
        # "总是允许"的落盘出口（窄回调，同 ask_permission 的注入套路）。默认写自己的 store；
        # 子 agent 的 store 是 None（它不落子会话盘），由 SubagentRunner 把【主 agent 的】这个回调
        # 传给它 —— 否则子 agent 里点的"总是允许"进不了 permissions.json：弹窗承诺"记住，不再问"，
        # 实际却是切一次模式（policy 从盘上重建）就凭空消失。
        self._persist_perm: Callable[[str, str, str], None] = (
            persist_permission or (store.add_permission if store is not None
                                   else (lambda tool, decision, spec: None)))
        # 工具权限（注入；None=不设闸、全放，旧行为/测试用）。policy 判 allow/deny/ask；
        # ask 时调 ask_permission 问用户（消费端注入 once/always/deny）。
        self.policy = policy
        self.ask_permission = ask_permission
        # 当前运行模式（key）：write_header 每轮据此把模式落进 session.json（resume 恢复）。消费端(TUI)切模式时设。
        self.mode = "normal"
        # 当前模式的每轮提示词（plan/yolo 有，normal/auto 空）。由消费端（TUI）按模式设；_pre_turn 每轮把它
        # 作为 user+<system-reminder> 注入到最新消息处——【不进 system prompt】，保持前缀稳定、不击穿 prompt 缓存。
        self.mode_reminder = ""
        # 同模式下【给子 agent 的】说法（Mode.sub_prompt）：SubagentRunner 造子 agent 时拼进它的
        # system prompt。与 mode_reminder 分开存——主 agent 那段是写给"要产出计划文件、调 exit_plan"
        # 的角色看的，照搬给子 agent 有害（见 mode.PLAN_SUB_PROMPT 的注释）。
        self.subagent_reminder = ""
        self._pending_reminder = ""   # 一次性 <system-reminder>（消费端设，如批准计划时的执行提示）：下一轮开头注入一次即清，不显示成用户消息
        self._plan_pending = False    # 本轮是否已 exit_plan 提交计划 → 执行完工具就结束本轮、等用户审阅
        # 自带翻页的"read 类"工具名：其超长输出不外置（模型本就能用 offset/limit 翻，外置反绕圈）
        self._read_tool_names = {t.strip() for t in config.rescue_read_tools.split(",") if t.strip()}
        self.context_tokens = 0          # 上次请求返回的精确 prompt_tokens（上下文大小）
        self._pending_interrupt_marker = False   # 下轮是否需单独插一条 [Request interrupted by user]
        # 协作式打断标志（TUI 在另一线程置位，本线程轮询）。可【注入共享】：子 agent 复用主 agent 的同一个，
        # 这样 Ctrl+C 能连带停子 agent（在其下一个检查点）。不注入则自建一个（主 agent / 测试）。
        self._interrupt = interrupt if interrupt is not None else threading.Event()
        self._proc_slot = ProcSlot(self._interrupt)   # 前台子进程槽：打断/退出时据此当场杀掉正在跑的 bash
        # 运行时用户发的消息队列：本轮还在跑时用户按 Enter 发的消息入队，下一轮向模型发请求【前】drain 进
        # 上下文（即时转向，和 bg 事件同一注入点）。跨线程（UI 线程入队、worker 线程 drain），故加锁。
        self._pending_user: list[str] = []
        self._user_lock = threading.Lock()
        # 后台任务管理器：bash background:true 转后台、kill_bgtask 停。输出快照按工具结果同口径上限尾部截断。
        # 任务清单管理器：复杂任务的规划/跟踪。两个 manager 【始终创建】（_inject_bg_events/压缩 reminder 会用到），
        # 但它们的【工具】和 subagent 工具只在【非子 agent】时注册——子 agent 不给这些：防无限递归、且保持精简。
        self._bg = BackgroundManager(output_cap=config.tool_result_max_chars)
        self._tasks = TaskManager(store=store)        # 会话级旁路文件 tasks.json，压缩压不掉、resume 还原侧栏
        # 子 agent 不能起后台任务：既没有 bg 工具（check/kill/wait）来管，且它跑完就回总结、随即消失——
        # 若放它起后台 bash，那条 daemon 线程+子进程在子 agent 结束/被 kill 时无人 reap，会僵尸泄漏。
        # 故子 agent 执行 bash 时传 bg=None（见 _exec_tool），background:true 会被 _bash 显式拒绝。
        self._is_subagent = subagent
        # 这条 agent 分支能不能被【单独】停掉 = 它是否持有自己独立的打断标志。
        # 前台子 agent 与主 agent 共享标志（停它=停整轮）→ False；后台/workflow 子 agent 自带 → True。
        # 只用于审批弹窗要不要给"停止此后台任务"（见 AskContext.can_stop）。
        self._own_branch = own_branch
        self._subagent = None
        self._workflow = WorkflowRun()                # workflow 执行状态（TUI 轮询画分支树；空闲时 stages 为空）
        if not subagent:
            for t in background_tools(self._bg):      # kill_bgtask / check_bgtask / wait_bgtask
                tools.register(t)
            for t in task_tools(self._tasks):         # task_create / update / get / list
                tools.register(t)
            # owner=self：子 agent 造出来时实时读主 agent 的 policy/ask_permission（权限继承，见 subagent.py 头）
            self._subagent = SubagentRunner(provider, config, interrupt=self._interrupt, owner=self)
            for t in subagent_tools(self._subagent):  # subagent（派生子 agent）
                tools.register(t)
            offload = store.offload_tool_output if store is not None else None
            for t in workflow_tools(self._subagent, self._workflow,   # run_workflow（确定性编排子 agent，后台跑）
                                    offload=offload):
                tools.register(t)
        self.messages: list[dict] = []
        if resume_messages is not None:
            # resume：恢复上次的对话上下文，并换上【当前启动】的新鲜 system——旧 system 是运行时配置、
            # 可能过时（日期/cwd/指令都变了），丢弃 load_messages 打头那条（若有），换成现在这版。
            body = (resume_messages[1:] if resume_messages
                    and resume_messages[0].get("role") == "system" else list(resume_messages))
            if system_prompt:
                self.messages.append({"role": "system", "content": system_prompt})
            # 后台任务不跨会话（活进程+内存态，上次退出即全部终止、本次不恢复）。旧历史里若起过后台任务、
            # 可能还写着"#N 运行中"——在系统提示词之后框定一句，免得 AI 去 check 早已不在的幻影任务。
            # 只在确实起过后台任务时注（没用过则不加这条无关提示）。用 user+reminder（同后台事件/压缩的注入约定）。
            if any("已在后台启动" in str(m.get("content", "")) for m in body):
                self.messages.append({"role": "user", "content": _reminder(
                    "续接旧会话：上次起的后台任务已随退出全部结束，当前没有运行中的后台任务"
                    "（下方历史里若提到某后台任务在运行，那是上次的、现已停止运行，不必去 check）。")})
            self.messages.extend(body)
            self._heal_orphans()   # 上次若被硬杀（关终端/崩溃）留下无结果的 tool_call → 补占位，免下次请求 400
            # 跨模型 resume 不用洗历史思考：发送时 provider._filter_reasoning 按当前档案过滤（存全、发时过滤）
        elif system_prompt:
            # 新会话：system 只进 RAM、不写 transcript。它是"运行时配置"（每次启动由 build_system_prompt
            # 重生，含当前日期/cwd/环境），不是对话历史；压缩需要时仍会随 image 进 marker，不丢。
            # 副作用（正合意）：构造时不碰磁盘 → 文件夹推迟到首条用户消息才建（懒创建）。
            self.messages.append({"role": "system", "content": system_prompt})

    def _record(self, msg: dict) -> None:
        """把一条消息记进 live messages[]，同时（有 store 时）追加到 transcript.jsonl。
        压缩对 self.messages 的【重写】不走这里——那是工作记忆的重组、不是新历史，只补一条 marker。"""
        self.messages.append(msg)
        if self.store is not None:
            self.store.append_transcript(msg)

    def update_system_prompt(self, text: str) -> None:
        """替换 system 消息（messages[0]）为新的 system prompt。模式切换时用：重建带新模式段的 prompt 换上。
        system 本就只在 RAM、不写 transcript（运行时配置），换它不动对话历史。"""
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = text
        else:
            self.messages.insert(0, {"role": "system", "content": text})

    def switch_backend(self, provider: Provider, config: AgentConfig | None = None) -> None:
        """热切后端（/config 保存后不重启即生效）：换 provider（连带新模型的思考档案），
        可选换 config（context_limit/compact_threshold 随新模型窗口重定）。
        历史【不用清洗】：思考统一存 reasoning_content、发送时由 provider._filter_reasoning 按
        新档案过滤（"存全、发时过滤"）——切走再切回，CoT 无损。"""
        self.provider = provider
        if config is not None:
            self.config = config
        if self._subagent is not None:
            self._subagent.provider = provider   # 子 agent 跟主 agent 同后端
            if config is not None:
                self._subagent.config = config

    def queue_user(self, msg: str) -> None:
        """运行时（本轮还在跑）用户按 Enter 发的消息入队：下一轮请求前 drain 进上下文（即时转向）。线程安全。"""
        with self._user_lock:
            self._pending_user.append(msg)

    def has_pending_user(self) -> bool:
        """有没有排队待注入的用户消息（TUI 收尾兜底：若消息发在本轮 drain 之后，起一轮处理它）。"""
        with self._user_lock:
            return bool(self._pending_user)

    def _drain_pending_user(self) -> list[str]:
        with self._user_lock:
            msgs, self._pending_user = self._pending_user, []
        return msgs

    def request_interrupt(self) -> None:
        """请求打断当前轮（线程安全）。TUI 的 UI 线程按 Ctrl+C/Ctrl+Q 时调用——置位标志（worker
        在流式循环/工具间轮询到就停），并【当场杀掉】正在跑的前台 bash 进程：worker 卡在 communicate
        里不会轮询标志，必须从外部杀进程才能让它立即返回（→ 走软打断、补好孤儿）。CLI 走 Ctrl-C。"""
        self._interrupt.set()            # 立即置位（瞬时）：流式/检查点即刻感知，UI 不被阻塞
        # 杀进程（taskkill /T 每条 ~秒级，多个子 agent 更甚）放【后台线程】——绝不能在 UI 线程里同步跑，
        # 否则 UI 冻结数秒。置位已足够让循环停下；bash 被杀只是让卡在 communicate 里的那条早点返回。
        threading.Thread(target=self._kill_running_procs, daemon=True).start()

    def _kill_running_procs(self) -> None:
        """树杀主 agent 正跑的前台 bash + 所有在跑前台子 agent 的 bash。退出时可【同步】调（确保子进程反应）。"""
        self._proc_slot.kill()
        if self._subagent is not None:
            self._subagent.kill_active()

    def run_turn(self, user_input: str) -> Iterator[Event]:
        """跑一整轮，把过程作为事件流产出（不负责显示）。
        产出的事件：ReasoningDelta/TextDelta（透传模型输出）、
        ToolStarted/ToolResult（工具执行）、Notice（加固提示）。"""
        yield from self._pre_turn()                  # 清打断标志 +（满了就）压缩 + 补上一轮打断标记
        self._record({"role": "user", "content": user_input})
        if self.store is not None:
            # 会话头：首轮设标题、每轮刷新 updated_at + 落当前模式（resume 恢复），供 /rl /rs 列会话挑选
            self.store.write_header(title=user_input, mode=self.mode)
        yield from self._run_loop_guarded()
        self._persist_context_size()                 # 轮末：最终上下文 token 数落会话头 → resume 立即显示

    def ask(self, user_input: str) -> str:
        """headless 编程入口（见 bootstrap.build_agent）：跑完一整轮（内部工具循环全部走完），
        返回【最后一条有正文的 assistant 消息】——与 subagent 取总结同一语义。
        中途"边干边说"的片段都在 messages 里；要过程细节（思考/工具事件）请自己消费 run_turn。"""
        for _ in self.run_turn(user_input):
            pass
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                return m["content"].strip()
        return ""

    def run_bg_turn(self) -> Iterator[Event]:
        """无用户输入、由消费端（TUI）驱动的一轮，靠 _turn_loop 顶部 drain 出的事件当输入：
        ① 后台任务完成/check-in；② 运行时排队的用户消息发在本轮 drain 之后、没赶上（收尾兜底）。
        两者都没有则空操作（不起空轮）。"""
        if not (self._bg.has_pending() or self.has_pending_user()):
            return
        yield from self._pre_turn()
        yield from self._run_loop_guarded()
        self._persist_context_size()

    def _persist_context_size(self) -> None:
        """把本轮最终上下文 token 数（context_tokens，来自末次请求的 prompt_tokens）落进会话头。
        resume 重建 agent 时 context_tokens=0，靠这个让 /rl 后侧栏立即显示用量、不用等首条消息。"""
        if self.store is not None:
            self.store.write_header(context_tokens=self.context_tokens)

    def _pre_turn(self) -> Iterator[Event]:
        """每轮开头的公共准备：清打断标志、（满了就）压缩、补上一轮遗留的打断标记。run_turn / run_bg_turn 共用。"""
        # 每轮开头清掉上一轮残留的打断标志。
        # 【已知取舍，勿当新 bug 挖】共用同一个 Event 的从属 agent（前台子 agent、workflow 阶段）
        # 开轮时也会执行这句，等于替用户撤销刚下的打断 → 存在一个"按 Ctrl+C 却没停下"的窗口。
        # 实测量过：窗口 ≈ 派发那 5ms，整轮 307ms 里占 2%，跑起来之后按一律有效（三个 sleep 子 agent
        # 真机实测全中断）。后果只是"少停一次得再按一下"，而修它要给 Agent 加字段、动打断主链路——
        # 收益微小、风险不小，故【有意不修】。真要修的方向：只有自己 new 了这个 Event 的 agent 才清
        # （主 agent 清=用户发了新消息，语义正确；从属 agent 不清=无权代表用户撤销）。
        self._interrupt.clear()
        # 加固④：上下文压缩 —— 在追加本轮内容【之前】压一次。优先用上次请求返回的精确 prompt_tokens
        # 判断（token 只有请求后才知道，故用上次的值）；为 0 时（后端不回 usage / 刚压缩完）用本地
        # 字符粗估兜底——否则不回 usage 的后端永不触发压缩，一路涨到撑爆窗口。
        # 旧历史交给摘要承载，本轮新内容随后原样追加。
        tokens = self.context_tokens or estimate_tokens(self.messages, self.tools.schemas())
        if tokens > self.config.compact_threshold * self.config.context_limit:
            compacted = compact(
                self._summarize, self.messages,
                read_tools=tuple(
                    t.strip() for t in self.config.rescue_read_tools.split(",") if t.strip()
                ),
                rescue_count=self.config.rescue_read_count,
                transcript_pointer=(self.store.transcript_path.as_posix()
                                    if self.store is not None else None),
                on_compacted=self._on_compacted,
            )
            if compacted is not None:
                self.messages = compacted
                yield Notice(f"上下文 {'' if self.context_tokens else '约'}{tokens} tokens 接近上限，已压缩旧历史")
                self.context_tokens = 0   # 压缩后重置，等下次请求测得新值
                # 任务清单是带外状态（存 tasks.json、不在被压的历史里）。压缩可能把 task_* 调用折叠掉，
                # 这里把当前清单快照重新注入一条，确保模型压缩后仍看得见自己的清单（侧栏始终在、不受影响）。
                snap = self._tasks.render_reminder()
                if snap:
                    self._record({"role": "user", "content": _reminder(snap)})
        # 上一轮的打断若没在上下文留下标记（流式被打断、或工具批已全完成后才被打断）→ 单独插一条
        # [Request interrupted by user]，让模型知道上一步被打断；工具执行中途被打断时，补齐的 tool
        # 结果里已带该标记，就不再重复插。
        if self._pending_interrupt_marker:
            self._record({"role": "user", "content": "[Request interrupted by user]"})
            self._pending_interrupt_marker = False
        self._inject_mode_reminder()     # 模式行为提示词（plan/yolo）：每轮注入到压缩之后、本轮内容之前（最新消息处）
        if self._pending_reminder:       # 一次性 reminder（如批准计划的执行提示）：注入一次即清，不显示成用户消息
            self._record({"role": "user", "content": _reminder(self._pending_reminder)})
            self._pending_reminder = ""

    def _inject_mode_reminder(self) -> None:
        """当前模式若有行为提示词（plan/yolo），每轮开头注入一条 user+<system-reminder>——贴在最新消息处、
        不进 system prompt（前缀稳→不击穿缓存）。normal/auto 的 mode_reminder 为空 → 不注。切模式即时生效（下一轮）。"""
        if self.mode_reminder:
            self._record({"role": "user", "content": _reminder(self.mode_reminder)})

    def _run_loop_guarded(self) -> Iterator[Event]:
        """跑 _turn_loop，包在 try 里支持 Ctrl-C/打断：不回滚——已完成的工具调用+结果是有用上下文，
        保留；只停在下一次模型调用前，并补齐缺失的 tool 结果（避免孤儿 → 400）。"""
        try:
            yield from self._turn_loop()
        except (KeyboardInterrupt, _TurnInterrupted):   # Ctrl-C(CLI) 或 request_interrupt()(TUI)
            filled = self._fill_interrupted_tool_results()
            self._pending_interrupt_marker = not filled   # 补过 → 标记已在 tool 结果里，无需再插
            yield Notice("已打断；已完成的工具结果保留，未继续后续动作")
        except GeneratorExit:               # 被 close() 关闭（打断恰在 render 处）：补齐但不能 yield
            filled = self._fill_interrupted_tool_results()
            self._pending_interrupt_marker = not filled
            raise
        # 单一真相：每条消息由 _record 即时 append 到 transcript（含打断补齐、压缩 marker），
        # 无需批量刷盘；resume 时 fold transcript 即还原工作记忆。故这里不再存快照。

    def _inject_bg_events(self) -> None:
        """drain 后台事件、以 user + <system-reminder> 注入上下文（调模型前清）：
        ① 完成（done/timeout）；② 到点的 check-in（任务还在跑、附当前部分输出）。
        都【不改】历史里那条“运行中”结果，而是注入新消息——身份用 user+reminder：兼容所有后端，
        且以 user 收尾还能稳稳触发模型回应（自动接续）。超长输出复用工具结果的截断。"""
        for task in self._bg.drain_completions():
            self._record({"role": "user", "content": _reminder(self._bg_done_text(task))})
        for task, snap in self._bg.due_checkins():
            self._record({"role": "user", "content": _reminder(self._bg_checkin_text(task, snap))})
        for tid, cmd, out in self._bg.drain_killed():   # 用户停止按钮杀的：留便条让模型知道（不自动起轮）
            body = f"[后台任务 #{tid} 已被用户停止] 命令: {cmd}"
            if out and out.strip():                     # 附终止前输出（和 AI kill_bgtask、前台 bash 中断对齐）
                body += f"\n（以下为停止前的输出）\n{out}"
            self._record({"role": "user", "content": _reminder(body)})
        for msg in self._drain_pending_user():        # 运行时用户发的消息：作为真·user 消息注入（即时转向）
            self._record({"role": "user", "content": msg})

    def _clip_offload(self, name: str, full: str, limit: int, tail_mode: bool) -> str:
        """超长输出外置（已确认 len(full)>limit 且有 store）：只内联一端，把【read_file 才会读到、不和内联
        重复】的那部分存盘 + 指针。
        tail_mode（bash/日志）：内联【尾部】最近输出，存【全文】——read_file 从头读即更早内容、和内联尾部
            不重复；后台多次 check-in 也不必反复截尾、文件不乱。
        否则（grep/glob 等列表）：内联【头部】，只存【头部之后】的部分——否则 read_file 头 200 行会
            重看一遍已内联的头部，纯属重复。内联 + 存盘 = 全文，且头部截断只做一次。"""
        if tail_mode:
            path = self.store.offload_tool_output(name, full)
            return f"[完整输出已存盘：{path}（用 read_file 读取）]\n{full[-limit:]}"
        path = self.store.offload_tool_output(name, full[limit:])
        return f"{full[:limit]}\n[后续输出已存盘：{path}（用 read_file 读取）]"

    def _bg_clip(self, text: str) -> str:
        # 后台【check-in】快照已在 BackgroundManager._snapshot 里【尾部截断】到 output_cap（日志要最近的尾部，
        # 不走工具结果那种头+尾截断），这里只补空兜底。完成时的外置走 _bg_done_text → _clip_offload。
        return text if text else "（无输出）"

    def _bg_done_text(self, task) -> str:
        # 后台=日志=尾内联：超长就内联尾部 + 全文外置（read_file 看更早），否则直接给全文。
        limit = self.config.tool_result_max_chars
        if self.store is not None and len(task.output) > limit:
            body = self._clip_offload(f"bgtask-{task.id}", task.output, limit, tail_mode=True)
        else:
            body = self._bg_clip(task.output)
        if getattr(task, "is_subagent", False):          # 后台子 agent：讲清是子 agent 完成、给总结
            label = getattr(task, "description", "") or task.command   # 短标签当"任务"（完整 prompt 在 leader 自己的 tool_call 里）
            return f"[后台子 agent #{task.id} 完成]\n任务: {label}\n总结:\n{body}"
        status = "完成" if task.status == "done" else "超时被终止"
        head = f"[后台任务 #{task.id} {status}"
        if task.exit_code is not None:
            head += f" · exit {task.exit_code}"
        head += "]"
        return f"{head}\n命令: {task.command}\n输出:\n{body}"

    def _bg_checkin_text(self, task, snap: str) -> str:
        return (f"[后台任务 #{task.id} 还在运行 · 已 {int(task.elapsed())}s]\n"
                f"命令: {task.command}\n输出至今:\n{self._bg_clip(snap)}\n"
                f"（任务还在跑：继续等可用 wait_bgtask({task.id}, 秒) 推迟下次查看；"
                f"卡住可用 kill_bgtask({task.id}) 停掉。）")

    def _fill_interrupted_tool_results(self) -> bool:
        """打断后保持上下文合法：给最后一条带 tool_calls 的 assistant 补齐【缺失】的 tool 结果
        （标记 [Request interrupted by user]），避免孤儿 tool_calls 导致下次请求 400。
        返回是否补过——补过即上下文已带打断标记，下轮无需再单独插。"""
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "user":
                return False                 # 流式被打断：无 assistant tool_calls → 无孤儿、无标记
            if m.get("role") == "assistant" and m.get("tool_calls"):
                answered = {t.get("tool_call_id") for t in self.messages[i + 1:]
                            if t.get("role") == "tool"}
                filled = False
                for tc in m["tool_calls"]:
                    if tc["id"] not in answered:
                        self._record({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "[Request interrupted by user]",
                        })
                        filled = True
                return filled
        return False

    def _heal_orphans(self) -> None:
        """resume 时一次性自愈孤儿 tool_call：上次若被【硬杀】(关终端/崩溃/断电)，会留下 assistant
        有 tool_call、却没写对应 tool 结果的"孤儿"，下次请求必 400。这里全量扫 self.messages（不是
        _fill_interrupted_tool_results 那种"从尾扫到第一条 user 就停"——孤儿后面常还跟着 user 消息），
        【按 tool_call_id 配对】(不搜内容)，给缺结果的就地补一条占位。只改内存镜像、不写盘：下次
        resume 再 fold transcript 时会再补一遍，幂等。"运行中"那种【有结果】的后台任务不在补的范围内。"""
        out: list[dict] = []
        i, n = 0, len(self.messages)
        while i < n:
            m = self.messages[i]
            out.append(m)
            if m.get("role") == "assistant" and m.get("tool_calls"):
                answered = set()
                j = i + 1
                while j < n and self.messages[j].get("role") == "tool":   # 紧随其后的 tool 结果
                    out.append(self.messages[j])
                    answered.add(self.messages[j].get("tool_call_id"))
                    j += 1
                for tc in m["tool_calls"]:                                 # 缺结果的就地补占位
                    if tc.get("id") not in answered:
                        out.append({"role": "tool", "tool_call_id": tc.get("id"),
                                    "content": "[Request interrupted by user]"})
                i = j
                continue
            i += 1
        self.messages = out

    def _turn_loop(self) -> Iterator[Event]:
        """单轮的“模型 ↔ 工具”循环（被 run_turn 包在 try 里以支持 Ctrl-C 打断回滚）。"""
        iterations = 0            # 防跑飞：循环计数
        empty_retries = 0         # 空响应重试计数
        executed_tools = False    # 本轮是否执行过工具（决定空响应时的提示策略）
        self._plan_pending = False   # 本轮复位：exit_plan 会置 True → 执行完工具即结束本轮

        while True:
            # 加固①：防跑飞 —— 模型反复调工具时强制止损
            iterations += 1
            if iterations > self.config.max_iterations:
                yield Notice(f"已达最大循环 {self.config.max_iterations} 次，强制停止")
                # 也要往【对话】里留一条：Notice 只给 UI 看，不进 messages。不留的话历史停在
                # "调了一堆工具拿到结果然后没下文"，模型下一轮读不出自己是被强制停的——要么当
                # 已完成、要么从头重做（有写操作就是重复写）。其余中断出口（打断/后台完成/压缩）
                # 都记了，唯独这个漏了。
                self._record({"role": "user", "content": _reminder(
                    f"本轮已达最大工具调用次数（{self.config.max_iterations}）被强制停止，"
                    f"任务可能只做了一半。不要假设已完成——先向用户说明做到哪一步、还剩什么，"
                    f"等用户指示。")})
                return

            self._inject_bg_events()        # 每次调模型【前】清后台事件：完成 + 到点的 check-in

            text = ""                       # 本次模型输出的可见正文
            reasoning = ""                  # 本次模型的思考正文（统一 reasoning_content 载体，无条件落盘）
            tool_calls: list[ToolCall] = []  # 本次模型要调的工具

            # ① 调模型，消费 Provider 事件：思考/正文透传给上层，工具调用先收集
            try:
                for ev in self.provider.stream(self.messages, self.tools.schemas(),
                                               should_stop=self._interrupt.is_set):
                    match ev:
                        case ReasoningDelta(text=rt):
                            reasoning += rt      # 累积思考（回传 reasoning_content 用）
                            yield ev
                        case TextDelta(text=t):
                            text += t            # 累积正文（用于写回对话历史）
                            yield ev
                        case ToolCall():
                            tool_calls.append(ev)
                        case Usage(prompt_tokens=pt):
                            self.context_tokens = pt   # 记下这次请求的精确上下文大小
                            yield ev                   # 透传：TUI 状态栏可实时显示 token
            except (KeyboardInterrupt, GeneratorExit, _TurnInterrupted):
                raise                            # 打断有自己的出口（下方 except），别在这里插手
            except Exception:
                # 流中途断（网络重置/服务端掐断）：已产出的正文【已经显示给用户了】，但记录在循环之后，
                # 异常一抛就跳过 → 用户说"继续"时模型不知道自己说过那半句，会从头再说一遍。
                # 先补记再抛：报错行为不变，只是别把已发生的事丢了。（重试不做——流式重来会把已产出的
                # token 重复一遍，provider 那边是刻意不重试的。）
                # 【只在正文非空时补】：只出了思考的话，补的是一条 content="" 的空消息——发送时
                # reasoning 会按档案被剥掉（多数模型收到的是光秃秃的空 assistant），而它还会顶掉压缩
                # 里"硬保留最后一轮 assistant 原文"的位置（_last_content 撞到空串就 return None），
                # 等于拿一条没内容的把有内容的挤掉；也与下方"空响应不记录空回合"的既有决定相悖。
                # tool_calls 传 []：provider 读完整个流才 flush ToolCall，半截调用根本到不了这里，
                # 此时 tool_calls 本就是空的。
                if text.strip():
                    self._record(self._assistant_msg(text, [], reasoning=reasoning))
                raise

            if self._interrupt.is_set():     # 流式被打断（provider 已提前停）→ 走统一打断处理
                raise _TurnInterrupted

            # 加固②：空响应 —— 既没文字也没工具调用，有限次重试（不记录空回合）
            if not text.strip() and not tool_calls:
                empty_retries += 1
                if empty_retries > MAX_EMPTY_RETRIES:
                    yield Notice("模型反复返回空响应，可能上下文太长——建议精简或重开会话")
                    return
                if executed_tools:
                    # 调过工具却没回复：注入提示让模型基于工具结果继续，而非直接结束。
                    # 用 user + <system-reminder>（mecode 注入带外上下文的统一约定，见 compact 的 _reminder）：
                    # 比裸 system 更兼容（user 角色所有后端都收）、且以 user 收尾更稳触发模型回应。
                    self._record({
                        "role": "user",
                        "content": _reminder("请根据工具调用结果继续思考下一步，或者直接回复用户最终结果。"),
                    })
                continue

            empty_retries = 0   # 有真实输出 → 重置空响应计数

            # ② 把这轮的 assistant 消息记进对话（带上它要调的工具）。
            #    思考【无条件存】（"存全、发时过滤"）：transcript 是完整事实，切模型/来回切 CoT 不丢；
            #    该不该带给当前模型（keep_reasoning 作用域）由 provider._filter_reasoning 在发送时决定。
            self._record(self._assistant_msg(text, tool_calls, reasoning=reasoning))

            # ③ 没有工具调用 → 模型给了最终答案 → 本轮结束
            if not tool_calls:
                return

            # ④ 有工具 → 执行、把过程产出、结果喂回对话。两类【并发】：前台 subagent 批（一批 N 个同时、
            #    等齐所有结果；wall-clock=最慢那个、不是求和——顺序阻塞就退化成 leader 自己干）；
            #    连续的只读工具组（read_only=True，见下方分组循环）。其余工具顺序跑。
            executed_tools = True
            # 前台 subagent（无 background）→ 并发批；后台 subagent（background:true）归 others → _exec_tool 拿 bg
            # 起（start_subagent，立即返回 id、不阻塞），和 bash background 一个套路。
            def _fg_sub(tc):
                return tc.name == "subagent" and not (
                    isinstance(tc.arguments, dict) and tc.arguments.get("background"))
            subs = [tc for tc in tool_calls if _fg_sub(tc)]
            others = [tc for tc in tool_calls if not _fg_sub(tc)]
            # others 按模型给出的顺序走：【连续的只读工具】攒成一组并发执行（等齐再继续），
            # 非只读工具是屏障——先冲掉手头攒的只读组、再串行执行它。这样读写交错时语义同纯串行
            # （若无屏障，[read A, edit A, read A] 重排后第二个 read 会读到改前内容）。
            reads: list[ToolCall] = []
            for tc in others:
                if self._interrupt.is_set():     # 工具批执行间被打断 → 已完成的结果保留，停下
                    raise _TurnInterrupted
                if self.tools.is_read_only(tc.name):
                    reads.append(tc)
                    continue
                if reads:
                    yield from self._exec_reads_batch(reads)
                    reads = []
                    if self._interrupt.is_set():
                        raise _TurnInterrupted
                yield from self._exec_tool(tc)
            if reads:
                yield from self._exec_reads_batch(reads)
                if self._interrupt.is_set():
                    raise _TurnInterrupted
            if subs:
                yield from self._exec_subagents_parallel(subs)
                if self._interrupt.is_set():     # 并发批期间被打断 → 结果已保留，停下
                    raise _TurnInterrupted
            if self._plan_pending:               # exit_plan 已提交计划 → 本轮到此为止，等用户批准/给修改意见
                return
            # ⑤ while 回到顶部 → 模型这轮能看到工具结果了

    def _process_result(self, name: str, full: str) -> str:
        """工具结果【入场截断/外置】（顺序路径与 subagent 并发路径共用）：超长 + 有 store + 非 read 类
        → 只内联一端、另一端存盘；否则头尾截断。从源头不让超长输出顶爆上下文。"""
        limit = self.tools.output_limit(name, self.config.tool_result_max_chars)
        if self.store is not None and len(full) > limit and name not in self._read_tool_names:
            return self._clip_offload(name, full, limit, tail_mode=(name == "bash"))
        return truncate_output(full, limit, self.config.tool_result_keep_tail)

    def _exec_tool(self, tc: ToolCall) -> Iterator[Event]:
        """执行单个工具：过闸 → ToolStarted → execute → 结果处理 → ToolResult → 记录。"""
        allowed, no_approver = self._gate(tc)   # 权限闸：可能弹审批（阻塞）——放在 ToolStarted 前
        yield ToolStarted(tc.name, tc.arguments, tc.id)
        if not allowed:                      # 被拒：明确叫停（文案见 _DENY_TOOL / _DENY_NO_APPROVER）
            result = (_DENY_NO_APPROVER if no_approver else _DENY_TOOL).format(name=tc.name)
            self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
            yield ToolResult(tc.name, result, tc.id)
            return
        if tc.name == "exit_plan":           # 控制类工具：Agent 亲自处理（落盘计划 + 发事件 + 结束本轮），不走注册表
            yield from self._exec_exit_plan(tc)
            return
        # 子 agent 传 bg=None：禁掉后台 bash（bg.start 永不被调）→ 无僵尸泄漏；前台 bash 仍走 _proc_slot 可被杀
        bg = None if self._is_subagent else self._bg
        full = self.tools.execute(tc.name, tc.arguments, slot=self._proc_slot, bg=bg)
        result = self._process_result(tc.name, full)
        self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
        yield ToolResult(tc.name, result, tc.id)

    def _exec_exit_plan(self, tc: ToolCall) -> Iterator[Event]:
        """exit_plan（无参）：读【计划文件】呈交。仅计划模式生效——发 PlanProposed 让上层渲染+弹审批条，
        并置 _plan_pending 让本轮到此结束（等用户批准/改）。计划文件是模型自己 write/edit 写的（见 PLAN_PROMPT），
        本工具只负责呈交，故计划正文不进工具参数（修订用 edit_file 增量、历史无冗余全量）。"""
        if self.mode != "plan":
            result = "当前不是计划模式，无需提交计划；请直接进行你要做的操作。"
            self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
            yield ToolResult(tc.name, result, tc.id)
            return
        plan, path = "", None
        if self.store is not None and self.store.plan_path.is_file():
            plan = self.store.plan_path.read_text(encoding="utf-8", errors="replace")
            path = self.store.plan_path.as_posix()
        if not plan.strip():                 # 还没写计划文件 → 不结束本轮，提示先写
            result = "还没有计划内容——请先用 write_file 把完整计划写进计划文件，写好再调 exit_plan 提交。"
            self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
            yield ToolResult(tc.name, result, tc.id)
            return
        self._plan_pending = True
        yield PlanProposed(plan, path)
        result = "计划已提交给用户审阅。现在停下，等用户批准或给修改意见——别自行开始动手。"
        self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
        yield ToolResult(tc.name, result, tc.id)

    def _exec_subagents_parallel(self, subs: list[ToolCall]) -> Iterator[Event]:
        """前台 subagent 批【并发】执行（见 _exec_parallel_batch）。不设并发上限：
        每个子 agent 的瓶颈在它自己的模型调用，线程本身近乎空闲（共享打断标志，Ctrl+C 可连带停）。"""
        yield from self._exec_parallel_batch(subs, _DENY_SUBAGENT)

    def _exec_reads_batch(self, reads: list[ToolCall]) -> Iterator[Event]:
        """连续只读工具组：单个走普通串行路径（免线程池开销，最常见情形），
        多个并发执行、封顶 MAX_PARALLEL_READS。只读工具 takes_slot=False，不需要 slot/bg。"""
        if len(reads) == 1:
            yield from self._exec_tool(reads[0])
        else:
            yield from self._exec_parallel_batch(reads, _DENY_TOOL, cap=MAX_PARALLEL_READS)

    def _exec_parallel_batch(self, tcs: list[ToolCall], deny_msg: str,
                             cap: int | None = None) -> Iterator[Event]:
        """一批工具【并发】执行：同时起、按完成顺序 yield 结果，等齐所有（wall-clock=最慢那个）。
        前台 subagent 批和连续只读工具组共用此路径。先各自过闸 + 冒 ToolStarted（审批弹窗是
        单通道，必须串行问；UI 随即同时显示 N 个在跑），再把放行的丢进线程池并发；单个崩了
        把错误当结果喂回、不拖垮整批。结果按【完成序】而非调用序记录——协议按 tool_call_id 配对。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        runnable: list[ToolCall] = []
        for tc in tcs:
            allowed, no_approver = self._gate(tc)
            yield ToolStarted(tc.name, tc.arguments, tc.id)
            if not allowed:
                result = (_DENY_NO_APPROVER if no_approver else deny_msg).format(name=tc.name)
                self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
                yield ToolResult(tc.name, result, tc.id)
            else:
                runnable.append(tc)
        if not runnable:
            return
        workers = len(runnable) if cap is None else min(len(runnable), cap)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut2tc = {ex.submit(self.tools.execute, tc.name, tc.arguments): tc for tc in runnable}
            for fut in as_completed(fut2tc):
                tc = fut2tc[fut]
                try:
                    full = fut.result()
                except Exception as e:      # 单个崩了不拖垮整批：把错误当结果喂回
                    full = f"工具 {tc.name} 执行出错：{type(e).__name__}: {e}"
                result = self._process_result(tc.name, full)
                self._record({"role": "tool", "tool_call_id": tc.id, "content": result})
                yield ToolResult(tc.name, result, tc.id)

    def _on_compacted(self, image: list[dict]) -> None:
        """压缩完成回调（compact 注入）：把 compaction marker 写进 transcript。
        marker 只带压缩后镜像——resume 时 fold 取回工作记忆；摘要已嵌在镜像里，不另存。无 store 则空操作。"""
        if self.store is not None:
            self.store.mark_compaction(image=image)

    def _gate(self, tc: ToolCall) -> tuple[bool, bool]:
        """工具执行前的权限闸。返回 (放行?, 是否因"没人可审批"而拒)。policy=None 则全放（旧行为/测试）。
        判成 ask 时调注入的 ask_permission 问用户（once/always/deny）；always 记进策略本进程即时生效。

        第二个返回值用来选拒绝文案：显式 deny 规则 / 用户点了拒绝 → "用户拒绝"；而"判成 ask 但没有
        可审批的用户"（无头）是系统自动拒的，说成"用户拒绝"是假信息，模型会照此向上汇报。"""
        if self.policy is None:
            return True, False
        decision = self.policy.decide(tc.name, tc.arguments)
        if decision == ASK:
            if self.ask_permission is None:
                return False, True        # 需要问却没人能问（无头）→ 保守拒绝（文案见 _DENY_NO_APPROVER）
            # 带上"谁在问"：审批循环要盯【本 agent】的打断标志、弹窗要据此决定给不给"停止此分支"
            choice = self.ask_permission(tc.name, tc.arguments,
                                         AskContext(self._interrupt, self._is_subagent,
                                                    self._own_branch))
            if choice == ALWAYS:
                # 生成规则（管道可多条；bash 还会连带 read_file/write_file 的落点授权）+ 本进程即时生效。
                # 故落盘要用【每条规则自己的工具名】，不能一律记在 tc.name 名下。
                for gtool, spec in self.policy.allow_always(tc.name, tc.arguments):
                    self._persist_perm(gtool, ALLOW, spec)     # 逐条落项目级 permissions.json（跨会话）
                return True, False
            return choice != DENY, False  # once → 放行；deny → 拒绝（确实是用户拒的）
        return decision == ALLOW, False

    def _summarize(self, summary_messages: list[dict]) -> str:
        """用 provider 跑一次（不带工具），把流式文本累积成摘要。
        compact() 接收这个函数，从而自身不碰网络、保持纯逻辑、好测。"""
        parts = []
        for ev in self.provider.stream(summary_messages, tools=None):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
        return "".join(parts)

    @staticmethod
    def _assistant_msg(text: str, tool_calls: list[ToolCall], reasoning: str = "") -> dict:
        """把 ToolCall 事件还原成 OpenAI 格式的 assistant 消息。
        关键：发回去时 arguments 必须是【字符串】(json.dumps)，不是 dict。
        思考统一存 reasoning_content 纯文本（四家通用载体，MiniMax 也收）；空 → 不带思考字段。"""
        msg: dict = {"role": "assistant", "content": text or ""}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        return msg
