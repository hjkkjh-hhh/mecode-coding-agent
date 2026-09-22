"""子 agent：派生一个【干净上下文】的 Agent 跑一个子任务、回一段总结。做成【工具】（CC 同款）。

灵魂 = 上下文隔离：子 agent 读文件/改代码/跑命令/搜索的一大堆【中间步骤不进主 agent 上下文】，
主 agent 只拿回【一段总结】。这是"用委派做上下文管理"——把整段脏活外包，主上下文不被子任务撑爆。

v1（同步·推式）：任务写在 prompt 里 → 造干净 Agent 跑到出最终文本 → 返回当工具结果。顺序、阻塞
（一轮里多个 subagent 会顺序跑，不并行——并行留到 v3 复用 bg）。
工具集 = core（read/write/edit/bash/grep/glob），【不含 subagent/bg/task】（防无限递归、保持精简）。
权限 = 【继承主 agent 当前策略】（含模式覆盖）：心智模型是"子 agent 的权限 = 你当前模式的权限"，
  换个说法逃不掉。此前是 policy=None 全放行，理由写的是"主 agent 既决定派活即信任"——但主 agent
  本身就是被这道闸管的对象，用被审查者的判断豁免审查是循环论证；实测 normal 模式下 `rm -rf` 要审批、
  包成 subagent 就不用，是整个权限系统的绕过口（计划模式早已因此把 subagent 一并 deny，等于自认）。
  ask 回调也继承（前台后台都能问）：曾试过"后台不给回调、ask 一律自动拒"，实测代价太大——normal/auto
  模式下 workflow 的每个阶段与后台子 agent 的写动作、非白名单命令会被【静默】拒掉，阶段还标成 ✓ 成功，
  等于 workflow 只在 YOLO 下能干活。改为都能问；后台任务弹审批窗时，窗上多一项"停止此后台任务"
  （置它自己的打断标志 → 拒这一步 + 停整条分支），审批循环也盯【提问方自己的】标志，故 kill_bgtask
  能把卡在弹窗上的后台子 agent 解开（见 agent.AskContext）。无头（主 agent 的 ask_permission 本就是
  None）时自然继承 None → 自动拒绝，文案说明是"没有可审批的用户"而非谎称"用户拒绝"。
  注：子 agent 里选"总是允许"只在本进程生效（它 store=None、不落 permissions.json），主 agent 侧照常持久化。
打断 = 与主 agent 共享同一个 interrupt Event：Ctrl+C 能连带停子 agent（在其下一个检查点，v1 不即时杀其 bash）。
"""
from __future__ import annotations

import threading

from .tools import BACKGROUND_TASK_GUIDANCE, Tool, default_registry

SUBAGENT_BATCH_ORDER = (
    "【批次顺序】同一条回复的 tool_calls 中，所有前台 subagent 必须连续放在末尾；"
    "普通工具及后台任务启动（含 background=true 的 subagent/bash）必须在它们之前。"
    "顺序违规时整批拒绝，所有工具都不会执行。"
    "依赖前台子 agent 结果的操作，等结果返回后在下一轮调用。"
)

SUBAGENT_PROMPT = """你是一个【子 agent】，由主 agent 派来完成一个相对独立的子任务。
- 专注完成派给你的这一个任务：自主读文件 / 改代码 / 跑命令 / 搜索，别问用户——没有用户能回答你。
- 你【不能】起后台类型的任务，（bash 的 background 用不了、也不能再派子 agent）：要跑命令就前台跑、等它完成再继续。
- 完成后用【一段简洁的总结】作为最终回复：说清你做了什么、结果如何、关键结论或产物路径。
  这段总结会【原样返回给主 agent】、是它唯一能看到的东西，所以要自足、别复述中间过程。
- 做不到或遇到硬阻碍，也如实在总结里讲明，别假装完成。"""


_NO_SUMMARY = "（子 agent 未产出总结）"
_INTERRUPTED = "（子 agent 被用户中断，未完成）"


def _last_assistant_text(agent) -> str:
    """子 agent 跑完后取【最后一条有正文的 assistant 消息】= 最终总结。"""
    for m in reversed(agent.messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return m["content"].strip()
    return _NO_SUMMARY


class SubagentRunner:
    """派生子 agent 跑子任务、回总结。持有造子 agent 需要的依赖（provider / config / 前台共享打断标志）。
    两种入口：run（前台，共享主 agent 打断→Ctrl+C 连带停）/ run_for_bg（后台，自带独立打断、暴露 proc_slot 供 kill）。"""

    def __init__(self, provider, config, interrupt: threading.Event | None = None,
                 owner=None) -> None:
        self.provider = provider          # 和主 agent 同一个 provider（同后端）
        self.config = config
        self.interrupt = interrupt        # 前台：与主 agent 共享打断标志（Ctrl+C 连带停）
        # 派它的主 agent：造子 agent 时【实时】读它的 policy/ask_permission（不存快照——切模式会整个
        # 换掉 agent.policy，快照会变陈旧）。None（直接构造 runner 的老用法/测试）→ policy=None 全放行。
        self.owner = owner
        # 当前在跑的【前台】子 agent 的 proc_slot（run 期间登记、跑完注销）。共享打断标志只让子 agent 在
        # 【检查点】停，但它若正卡在长 bash 的 communicate 里收不到——故 Ctrl+C 时主 agent 还要 kill_active
        # 把这些 bash 当场树杀，让 communicate 立即返回、子 agent 到检查点即停（否则并发批会卡到最慢的 bash 跑完）。
        self._active_slots: set = set()
        self._slots_lock = threading.Lock()

    def _make(self, interrupt: threading.Event | None, *, own_branch: bool = False):
        """造一个子 agent：权限与审批回调都继承主 agent（见模块头）。前台后台一视同仁——
        区别只在打断标志：前台与主 agent【共享】（Ctrl+C 连带停，但也意味着停它=停整轮），
        后台/workflow 自带独立的。own_branch 就是这个区别，审批弹窗据它决定给不给
        "停止此后台任务"（对前台给了是骗人的，见 agent.AskContext.can_stop）。"""
        from .agent import Agent          # 延迟导入：避免与 agent.py 循环
        reg = default_registry()          # 干净的 core 工具集（read/write/edit/bash/grep/glob）
        # exit_plan 是"把计划交给用户审阅"——子 agent 没有用户可提交，它的交付物就是那段总结。
        # 留着它，模型一调就会把自己那一轮提前结束且不出总结（计划模式放开子 agent 后正好踩得到）。
        reg.unregister("exit_plan")
        # 模式段（子 agent 版）：主 agent 那段是写给"要产出计划文件、调 exit_plan"的角色看的，照搬有害；
        # 这里注入的是同一模式下【给子 agent 的】说法（如计划模式=只读，别反复重试被拒的动作）。
        prompt = SUBAGENT_PROMPT + (getattr(self.owner, "subagent_reminder", "") or "")
        return Agent(
            self.provider, reg,
            system_prompt=prompt,
            config=self.config,
            store=None,                   # 不落子会话盘（只回总结；主 transcript 记 spawn 调用+总结）
            # 权限与审批回调都继承主 agent（见模块头）：换个说法（包成子 agent）不该绕过闸。
            # 【共用同一个 policy 对象，不 fork】：用户点"总是允许"批的是【这个操作】本身，与"是哪个
            # agent 在问"无关 —— 它就该全局生效。曾经改成 fork（各拿副本）以防"授权外溢到父"，
            # 但那让同一个授权在每个阶段/每个兄弟子 agent 里都要重问一遍，且与弹窗承诺的
            # "记住，不再问"相悖。真正该配套的是【让它落盘】（见下面 persist_permission）。
            policy=getattr(self.owner, "policy", None),
            ask_permission=getattr(self.owner, "ask_permission", None),
            # 子 agent 自己 store=None → 借主 agent 的落盘出口，让它里面点的"总是允许"也进
            # permissions.json（跨会话、且切模式重建 policy 后依然在）。
            persist_permission=getattr(self.owner, "_persist_perm", None),
            subagent=True,                # 不注册 subagent/bg/task 工具（防递归、保持精简）
            interrupt=interrupt,
            own_branch=own_branch,
        )

    def run(self, prompt: str) -> str:
        """前台：造干净子 agent 跑到完成、回总结。共享主 agent 打断（Ctrl+C 连带停）。
        run 期间把它的 proc_slot 登记进 _active_slots，使 Ctrl+C 能连带杀它正跑的 bash（见 kill_active）。"""
        sub = self._make(self.interrupt)
        with self._slots_lock:
            self._active_slots.add(sub._proc_slot)
        try:
            list(sub.run_turn(prompt))
        finally:
            with self._slots_lock:
                self._active_slots.discard(sub._proc_slot)
        summary = _last_assistant_text(sub)
        if summary == _NO_SUMMARY and self.interrupt is not None and self.interrupt.is_set():
            return _INTERRUPTED       # 被中断在跑 bash 时，还没走到出总结那步 → 讲明是中断、非它自己哑了
        return summary

    def kill_active(self) -> None:
        """树杀所有在跑的前台子 agent 正登记的 bash（Ctrl+C 时由主 agent 调）→ 它们的 communicate 立即返回、
        各自在检查点看到共享打断标志后停下 → 并发批不再卡到最慢子 agent 的 bash 跑完。"""
        with self._slots_lock:
            slots = list(self._active_slots)
        for s in slots:
            s.kill()

    def run_for_bg(self, prompt: str, interrupt: threading.Event, on_slot) -> str:
        """后台：用【自带的独立打断标志】造子 agent（Ctrl+C 不碰它，只 kill 停），把它的 ProcSlot 经 on_slot
        交给管理器（供 kill 杀它正跑的前台 bash 及后代）；跑到完成、回总结。
        它需审批时照常弹窗（窗上带"停止此后台任务"）；kill 置的就是这里的 interrupt，
        审批循环盯着它 → 卡在弹窗上也能被 kill 解开。"""
        sub = self._make(interrupt, own_branch=True)   # 自带独立打断标志 → 可被单独停
        on_slot(sub._proc_slot)           # 交出 proc_slot：kill 时置打断 + 杀这条 bash（树杀→含孙进程）
        list(sub.run_turn(prompt))
        return _last_assistant_text(sub)


def subagent_tools(runner: SubagentRunner) -> list[Tool]:
    """AI 可调的子 agent 工具（闭包绑定 runner，和 background_tools/task_tools 一个套路）。takes_slot=True：
    execute 会注入 (slot, bg)，后台档用 bg.start_subagent 起。"""
    def _spawn(args: dict, slot=None, bg=None) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "错误：prompt 不能为空（描述要子 agent 完成的子任务）"
        desc = (args.get("description") or "").strip()     # 短标签：显示用，不进子 agent 上下文
        if args.get("background") and bg is not None:      # 后台：立即返回 id、不阻塞；完成通知你
            tid = bg.start_subagent(prompt, runner, description=desc)
            return (f"已在后台派生子 agent · 任务 #{tid} · 运行中"
                    f"（完成会通知你；需要时用 kill_bgtask({tid}) 停）")
        return runner.run(prompt)         # 前台：跑到完成、回总结（并发批里被调，slot/bg 均 None）

    return [Tool(
        name="subagent",
        description="派生一个【子 agent】去完成一个相对独立的子任务：它有自己干净的上下文，自主读改跑搜，"
                    "完成后回一段总结给你——它的【中间过程不进你的上下文】，你只拿回结论，省上下文。"
                    "适合把'要读一大堆文件 / 试错很多步'的脏活外包出去（探索性调研、独立模块的写+测、"
                    "批量查找等）。任务要一次讲清（做什么、在哪、怎样算完成），它无法回问你；"
                    "它跑不动或失败也会在总结里说明。它自己【不能】再派子 agent。\n"
                    "【前台（默认）vs 后台】：要它的结果才能往下走（调研/多角度/验证）→ 用前台，"
                    "同一条消息里发【多个】subagent 调用会【并发】跑、等齐所有结果（三个各 30s 的，30s 跑完不是 90s）；"
                    "设 background=true 转后台，立即返回编号、完成再通知你（可 kill_bgtask 停）。\n"
                    f"{BACKGROUND_TASK_GUIDANCE}\n"
                    f"{SUBAGENT_BATCH_ORDER}\n"
                    "【什么时候【不】该派】：目标已经明确就直接自己做——知道路径就 read_file、"
                    "找某个符号就 grep；一两步能做完的事别派子 agent，那比自己做还慢。\n"
                    "子 agent 改过代码的，你要自己核一遍实际改动再向用户汇报，别只转述它的总结。\n"
                    "别把【理解】外包：不要写“根据你的发现把这个 bug 修好”这类指令——"
                    "那是把判断推给了子 agent，该你自己做的分析要自己做。",
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "用 3-5 个词概括这个子任务（做标签显示，让你和用户一眼看清这个子 agent 在干嘛，"
                                               "如“探索认证模块”“审查并发安全”）。不发给子 agent、纯显示用。"},
                "prompt": {"type": "string",
                           "description": "交给子 agent 的子任务描述，越具体越好（目标 / 范围 / 验收标准）"},
                "background": {"type": "boolean",
                               "description": "true=转后台跑、立即返回编号，仅用于独立且结果不急用的任务；"
                                              "默认 false=前台，多个前台调用会并发、等齐所有结果"},
            },
            "required": ["description", "prompt"],
        },
        handler=_spawn,
        takes_slot=True,
    )]
