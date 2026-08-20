"""mecode 桌面端服务 —— 把 agent 的事件流和交互契约暴露成本地 HTTP 接口，供 Web 前端渲染。

    python scripts/deskserve.py                    # 随机端口，打印就绪行
    python scripts/deskserve.py --port 8200 --open # 固定端口并自动开浏览器

和 scripts/serve.py 的区别（为什么不复用它）：
  serve.py 是【OpenAI 兼容网关】，为多 agent 编排（辩论项目）而写——把整个工具循环折叠成
  一次 /v1/chat/completions 回复、每请求无状态、默认 yolo 放行。桌面端要的正好相反：
  - 要【展开】的事件流（每个 delta、每次工具调用实时显示），不是折叠成一条回复
  - 要【持续会话】（切模式、resume），不是每请求新建 agent 用完即弃
  - 要【交互式审批】，而 OpenAI 的响应格式里没有"服务端向客户端发起请求"这个概念
  两者并存、各服务各的调用方；serve.py 一行不动。

协议：SSE 下行 + POST 上行（不用 WebSocket）
  零新依赖——stdlib 的 ThreadingHTTPServer 就够，和 serve.py 同一套选型；开发期用浏览器
  直接开，不必先有桌面壳。真要换成子进程 stdio JSON-RPC，下面的消息体可以原样搬。

    GET  /api/events        服务端→前端：agent 事件、审批/提问、侧栏状态、心跳
    GET  /api/state         快照：模式/模型/上下文用量/思考档/工具/技能
    GET  /api/sessions      本项目的会话列表
    GET  /api/history       当前会话历史（分页，带 tool_calls）
    GET  /api/tasks         任务清单        GET /api/task?id=      单条详情
    GET  /api/bg/output     后台任务的中间输出（按需拉）
    GET  /api/tool_output   外置的大工具输出（分页读）
    POST /api/send          用户输入（忙时排队）      POST /api/interrupt  打断
    POST /api/compact       手动压缩一次上下文
    POST /api/reply         回答审批/提问            POST /api/mode       切模式
    POST /api/resume        切会话                   POST /api/session/new 开新会话
    POST /api/session/rename 会话改名                POST /api/session/fork  复制一份并切过去
    POST /api/session/archive 归档/取消归档（只是从清单收起来，不删任何东西）
    GET  /api/workspaces    所有工作区及各自的会话      GET  /api/browse   列子目录（选工作区用）
    POST /api/workspace     换工作区（会 os.chdir，见 set_workspace）
    POST /api/workspace/pick 开【系统自带】文件夹对话框选工作区（阻塞到用户点完）
    POST /api/bg/kill       终止后台任务             POST /api/task       改/删任务
    POST /api/plan/approve  批准计划、切回执行模式    GET  /api/plan       待批准的计划正文
    GET  /api/config        模型目录/已存后端/思考档/上限阈值（api_key 一律不下发）
    POST /api/config/save   新增或覆盖一个后端并切过去   POST /api/config/switch 切到已存的
    POST /api/config/delete 删一条已存后端              POST /api/config/test   连通性探活
    POST /api/prefs         改上下文上限 / 压缩阈值      POST /api/thinking      思考开关与深度
    GET  /api/skills        技能清单                    POST /api/skills/toggle 启停
    GET  /api/system        当前 system prompt + 模式提示（对应 TUI 的 /system）
    POST /api/skills/run    把某技能全文注入并起一轮
    GET  /api/mcp           MCP server 清单与连接状态    POST /api/mcp/toggle   启停
    POST /api/mcp/timeout   改握手超时                  POST /api/mcp/save     新增/改一个 server
    POST /api/mcp/delete    删一个 server               POST /api/mcp/reload   按当前配置重连
    GET  /                  静态文件（desktop/ 目录）

信任闸（别删）：
  服务监听本地端口、且桌面端必然跑在 auto/yolo 档——等于本机任何进程都能驱动 agent 跑任意
  bash。浏览器的同源策略也拦不住：跨域 POST 属于简单请求，不触发预检就能发出去，一个恶意
  网页就能远程操纵你的 agent。所以三道闸缺一不可：
  ① 启动时生成随机 token，注入进 index.html（同源策略保证别的站点读不到、也猜不出）；
     所有 /api/* 和首页都要带它。SSE 走查询串——EventSource 设不了自定义请求头。
     【首页必须一起拦】：token 就注入在它的 HTML 里，放行等于把 token 白送。
  ② 校验 Host 头必须是回环地址，挡 DNS rebinding（把域名解析到 127.0.0.1 绕过同源）。
  ③ 校验 Origin 头（存在时）必须是自己，挡跨站请求。
  只绑 127.0.0.1 本身【不够】——同机的其它程序照样连得上。

阻塞往返 —— 本文件最容易写错的地方：
  ask_permission 是【agent 工作线程上的同步阻塞调用】，必须走一趟前端再拿着答复回来。
  规则照搬 TUI 那套已经踩平的桥（scripts/tui.py 的审批循环）：
  ① 阻塞只发生在工作线程，HTTP 线程永不阻塞；
  ② 每个阻塞点写成"带超时的轮询等待 + 每轮查一次打断标志"（Event.wait(0.05)），不能无限
     wait——否则 agent 被打断时这条线程永远醒不过来，整个进程卡死；
  ③ 打断时按 deny 返回，并【主动下发 dismiss】收掉前端那张窗。不收窗的话前端会留一张幽灵窗，
     用户下一次点击被错配给下一个请求——最严重的是把"总是允许"落到另一个工具名下；
  ④ 答复按 id 路由，陈旧/未知 id 直接丢弃；
  ⑤ 前端断开（关标签页/刷新）不打断当轮——刷新一下就杀掉正在跑的任务不合理。但"没有任何
     前端连着"时不能无限等审批，超过 NO_CLIENT_DENY_SECS 判 deny 收场，免得工作线程永久挂起。
     重连时把还没答复的请求【重放】给新连上的前端，否则刷新后那张窗就永远出不来了。

跨进程要额外注意的两类字段（同进程的 TUI 不用管，这里必须管）：
  - 单调时钟：BackgroundTask.started_at 是 time.monotonic() 的值，跨进程没有意义。
    推给前端要换成墙钟（time.time() - elapsed()）。
  - 每秒都在变的量（elapsed、next_checkin_at）：绝不能进状态快照的比对键，否则每个 tick
    都判成"变了"、每 0.5 秒推一帧。改成推起点，由前端本地算。
"""
from __future__ import annotations

import argparse
import itertools
import json
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mecode.agent import Agent                                            # noqa: E402
from mecode.bootstrap import build_agent                                  # noqa: E402
from mecode.config import (                                               # noqa: E402
    USER_CONFIG_PATH, agent_config, current_agent_config, current_backend,
    delete_saved, effective_window, env_backend, load_user_config, save_user_config,
    saved_configs, update_settings,
)
from mecode.events import (                                               # noqa: E402
    Done, Notice, PlanProposed, ReasoningDelta, TextDelta,
    ToolCall, ToolResult, ToolStarted, Usage,
)
from mecode.mcp import (                                                  # noqa: E402
    TIMEOUT_PRESETS, connect_servers, default_config_paths, load_mcp_config,
    server_states, set_server_enabled, set_server_timeout,
)
from mecode.memory import build_memory_prompt, memory_tools               # noqa: E402
from mecode.mode import DEFAULT_MODE, MODES, apply_mode                   # noqa: E402
from mecode.permission import (                                           # noqa: E402
    ALWAYS, DENY, ONCE, PermissionPolicy, pattern_for,
)
from mecode.provider import Provider, ProviderError                       # noqa: E402
from mecode.session import SessionStore                                   # noqa: E402
from mecode.registry import PROVIDERS, context_window_for                 # noqa: E402
from mecode.skills import discover_skills, set_enabled, skill_reminder     # noqa: E402
from mecode.system_prompt import build_system_prompt                      # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "desktop"
POLL = 0.05                  # 阻塞点的轮询粒度（秒）——同时决定打断的响应延迟
TICK = 0.5                   # 状态轮询间隔：后台任务/workflow 内核没有变更回调，只能定时比对
HEARTBEAT = 15.0             # SSE 心跳间隔：既保活也用来探活（写失败=前端没了）
NO_CLIENT_DENY_SECS = 60.0   # 无前端连着时，等审批最多等这么久就判 deny（防工作线程永久挂起）
HISTORY_PAGE = 40            # 历史每页条数：一次全量返回，长会话能到上千条、前端 DOM 直接卡死
BLOB_PAGE = 200_000          # 外置输出每次最多读这么多字符（全文上限 50 万）
TOKEN = secrets.token_urlsafe(24)   # 每次启动一换：进程重启即失效，泄漏也只影响这一次运行
_ids = itertools.count(1)

# 内核外置大工具输出时留下的标记（agent._offload → session.offload_tool_output）
_OFFLOAD = re.compile(r"存盘：(.+?)（用 read_file 读取）")

# 全量替换语义的事件：同一帧里只保留最后一条（天然幂等，早的那条必然被晚的覆盖）
_REPLACE = ("bg", "workflow", "tasks")

# MCP server 名会成为工具名前缀（<name>__tool），非标识符字符会让模型没法正确调用
_MCP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


# 子进程里开系统文件夹对话框，把结果打到 stdout。取消 → 空行。
_PICK_SRC = """
import sys, tkinter, tkinter.filedialog as fd
root = tkinter.Tk()
root.withdraw()                       # 只要对话框，不要那个空的主窗口
root.attributes('-topmost', True)     # 否则会开在浏览器【后面】，用户以为没反应
try:
    p = fd.askdirectory(title='选择工作区', mustexist=True, initialdir=sys.argv[1] or None)
finally:
    root.destroy()
sys.stdout.write(p or '')
"""


def mask_key(key: str) -> str:
    """api_key 的可辨认摘要。页面上要能认出"哪条是哪条"，但【绝不下发原文】——
    key 没有任何理由离开本机进程，切换已存后端时前端只送 (base_url, model)。"""
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 14 else key[:3] + "…"


# ---------------------------------------------------------------- 事件序列化

def encode(ev) -> dict | None:
    """agent 事件 → 可 JSON 的消息。未知类型返回 None（丢弃，不让前端猜）。"""
    if isinstance(ev, TextDelta):
        return {"type": "text", "text": ev.text}
    if isinstance(ev, ReasoningDelta):
        return {"type": "reasoning", "text": ev.text}
    if isinstance(ev, ToolStarted):
        return {"type": "tool_start", "id": ev.id, "name": ev.name, "args": ev.arguments}
    if isinstance(ev, ToolResult):
        msg = {"type": "tool_result", "id": ev.id, "name": ev.name, "result": ev.result}
        hit = _OFFLOAD.search(ev.result or "")
        if hit:            # 结果被外置了 → 带上路径，前端才能给"看全文"
            msg["full_path"] = hit.group(1)
        return msg
    if isinstance(ev, Notice):
        return {"type": "notice", "text": ev.text}
    if isinstance(ev, PlanProposed):
        return {"type": "plan", "plan": ev.plan, "path": ev.path}
    if isinstance(ev, Usage):
        return {"type": "usage", "prompt": ev.prompt_tokens,
                "completion": ev.completion_tokens, "total": ev.total_tokens,
                "reasoning": getattr(ev, "reasoning_tokens", 0)}
    if isinstance(ev, Done):
        return {"type": "done", "reason": ev.reason}
    if isinstance(ev, ToolCall):
        return None          # 工具调用由 ToolStarted 呈现，这条是给 agent 内部用的
    return None



# ---------------------------------------------------------------- SSE 广播

class Bus:
    """事件总线：工作线程 emit，每个 SSE 连接一个队列各自消费。

    多连接（刷新页面/开第二个窗口）都收同一份事件。队列不设上限——丢事件等于丢正文，
    宁可占内存；真正的流量控制在写端做（见 drain）。
    """

    def __init__(self) -> None:
        self._qs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._qs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._qs:
                self._qs.remove(q)

    def clients(self) -> int:
        with self._lock:
            return len(self._qs)

    def emit(self, msg: dict) -> None:
        with self._lock:
            qs = list(self._qs)
        for q in qs:
            q.put(msg)


def drain(q: queue.Queue, first: dict) -> list[dict]:
    """把队列里【当前已到】的消息一次取空，压掉两类冗余。

    ① 相邻的同类文本片合并：一轮下来 TextDelta 可能上千条，一条一个 SSE 消息纯属浪费
       （JSON 包头比正文还长）。只合并 text/reasoning，工具事件合并会丢配对关系。
    ② 全量替换类（bg/workflow/tasks）只留最后一条：它们的语义就是"当前全貌"，
       前端连着渲三次中间态毫无意义，还会让侧栏抖。
    """
    out: list[dict] = [first]
    while True:
        try:
            m = q.get_nowait()
        except queue.Empty:
            break
        prev = out[-1]
        if m["type"] in ("text", "reasoning") and prev["type"] == m["type"]:
            prev["text"] += m["text"]
        else:
            out.append(m)
    seen_last: dict[str, int] = {}
    for i, m in enumerate(out):
        if m["type"] in _REPLACE:
            seen_last[m["type"]] = i
    return [m for i, m in enumerate(out)
            if m["type"] not in _REPLACE or seen_last[m["type"]] == i]


# ---------------------------------------------------------------- 阻塞往返

class Pending:
    """待答复的请求表（审批 / 提问）。工作线程挂在这里等，HTTP 线程从这里放行。

    并发子 agent 会同时挂多个，所以按 id 路由、不能用单槽。
    """

    def __init__(self) -> None:
        self._recs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open(self, payload: dict) -> tuple[str, dict]:
        rid = f"r{next(_ids)}"
        rec = {"event": threading.Event(), "answer": None, "payload": {**payload, "id": rid}}
        with self._lock:
            self._recs[rid] = rec
        return rid, rec

    def close(self, rid: str) -> None:
        with self._lock:
            self._recs.pop(rid, None)

    def answer(self, rid: str, value) -> bool:
        """前端给了答复。未知/陈旧 id 返回 False（丢弃，不报错——重复点击是常事）。"""
        with self._lock:
            rec = self._recs.get(rid)
        if rec is None:
            return False
        rec["answer"] = value
        rec["event"].set()
        return True

    def outstanding(self) -> list[dict]:
        """还没答复的请求（新前端连上时重放，否则刷新后那张窗再也不会出现）。"""
        with self._lock:
            return [r["payload"] for r in self._recs.values() if not r["event"].is_set()]

    def cancel_all(self) -> list[str]:
        """全部按"没答"结算（换会话时用）。返回被取消的 id，供下发 dismiss。"""
        with self._lock:
            ids = list(self._recs)
            for rec in self._recs.values():
                rec["answer"] = None
                rec["event"].set()
        return ids


class Desk:
    """一个桌面会话：持有 agent、跑轮的工作线程、事件总线、待答复表。"""

    def __init__(self, cwd: Path, mode: str, mcp: bool, **build_kwargs) -> None:
        """build_kwargs 透传 build_agent（同 serve.py 的口子）：测试注入假 provider 用，
        免得跑测试还要连真后端。"""
        self.bus = Bus()
        self.pending = Pending()
        self.busy = False
        self.mode = mode
        self.cwd = cwd
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._bg_start: dict[int, float] = {}   # 任务 id → 首次算出的墙钟起点（见 _snapshot）
        self._last_exec_mode = mode if mode != "plan" else DEFAULT_MODE   # 批准计划后回退到它
        self._stop = threading.Event()
        self._pending_switch = False            # 本轮跑完要不要热切后端（切模型时忙就推迟）
        # MCP 自己连，不走 build_agent 的 mcp=True：
        #   ① setup_mcp 把连接错误吞掉了，桌面端要在面板上显示"哪个 server 没起来、为什么"；
        #   ② 同步连最慢要 15 秒/个，服务得等它连完才能应答——改成后台连，页面秒开。
        self.agent = build_agent(cwd=cwd, mode=mode, mcp=False,
                                 ask_permission=self._ask_permission, **build_kwargs)
        self.agent.tools.register(_make_ask_user(self))
        # 【留住这张工具表】：切会话时要复用同一张，不能重新组装——否则每次 resume 都把
        # MCP server 重连一遍（子进程重启、几秒卡顿），还会丢掉 ask_user 和记忆工具。
        self.registry = self.agent.tools
        self.mcp_clients: list = []
        self.mcp_errors: list[str] = []
        self._mcp_tools: list[str] = []        # 已注册的 MCP 工具名，重连时按它摘干净
        self._wire()
        threading.Thread(target=self._poll, daemon=True).start()
        if mcp:
            threading.Thread(target=self._connect_mcp, daemon=True).start()

    # ---- 内核回调 ----

    def _wire(self) -> None:
        """把内核的变更回调接到总线上。【resume 换了 agent 之后必须重挂】——漏了不会报错，
        只会静默哑掉：后台任务跑完永远没通知、任务清单改了侧栏永远不刷新。"""
        self.agent._bg.on_complete = self._on_bg_done
        self.agent._tasks.on_change = self.push_tasks

    def _on_bg_done(self, t) -> None:
        """后台任务自然完成（killed 的静默、不回调）。跑在守护线程上，Bus 自身线程安全。"""
        self.bus.emit({
            "type": "bg_done", "id": t.id, "command": t.command[:200],
            "description": t.description, "is_subagent": t.is_subagent,
            "status": t.status, "exit_code": t.exit_code,
            "output": (t.output or "")[:4000],
        })
        # 空闲就自动起一轮把结果交给 agent——只推事件给前端而不告诉 agent，它会一直等一个
        # 永远不来的结果（TUI 在 BgComplete 里做的就是这件事）。
        self.maybe_bg_turn()

    def push_tasks(self) -> None:
        """把当前任务清单全量推一次。

        除了 on_change 回调，【启动和 resume 之后必须主动调一次】：TaskManager 在构造时就把
        tasks.json 读进来了，那次装载早于回调挂接，历史清单永远不会自己冒出来。
        """
        self.bus.emit({"type": "tasks", "tasks": self.agent._tasks.summaries()})

    # ---- 服务端 → 前端的阻塞请求（审批 / 提问）----

    def _wait(self, rid: str, rec: dict, interrupt: threading.Event | None, on_cancel):
        """所有阻塞往返的共同等待循环。返回 rec["answer"]，被取消则返回 on_cancel()。

        三条硬要求（缺一条就有 bug，见模块头）：带超时轮询而非无限 wait、每轮查打断标志、
        取消时主动下发 dismiss 收窗。
        """
        idle = 0.0
        try:
            while not rec["event"].wait(POLL):
                if interrupt is not None and interrupt.is_set():
                    self.bus.emit({"type": "dismiss", "id": rid})
                    return on_cancel()
                # 没有任何前端连着时不能无限等：没人能答，工作线程会永久挂起
                idle = idle + POLL if self.bus.clients() == 0 else 0.0
                if idle >= NO_CLIENT_DENY_SECS:
                    self.bus.emit({"type": "dismiss", "id": rid})
                    return on_cancel()
            if rec["answer"] is None:          # cancel_all 结算的（换会话）
                self.bus.emit({"type": "dismiss", "id": rid})
                return on_cancel()
            return rec["answer"]
        finally:
            self.pending.close(rid)

    def _ask_permission(self, name: str, args: dict, ctx) -> str:
        """注入 agent 的审批回调。跑在【工作线程】上，同步阻塞直到前端回答或被打断。"""
        # pattern = "总是允许"这一下【到底会记住什么】的可读形式（bash(git:*) 这种）。
        # 空串 = 这次调用生成不了有意义的规则（含元字符的 bash、畸形参数、会逃出根的 glob），
        # 前端据此【不显示那个按钮】——记不住的事别承诺。
        try:
            pattern = pattern_for(name, args, str(self.cwd))
        except Exception:            # noqa: BLE001 —— 只是显示用，算不出来就当不可授权，别把审批搞崩
            pattern = ""
        rid, rec = self.pending.open({
            "type": "ask_permission", "tool": name, "args": args,
            "pattern": pattern,
            "is_sub": getattr(ctx, "is_sub", False),
            "can_stop": getattr(ctx, "can_stop", False),
        })
        self.bus.emit(rec["payload"])
        answer = self._wait(rid, rec, getattr(ctx, "interrupt", None), lambda: DENY)
        return answer if answer in (ONCE, ALWAYS, DENY) else DENY

    def ask_user(self, questions: list[dict]) -> dict | None:
        """注入 ask_user 工具的提问回调。同上，返回 None = 用户跳过/被打断。"""
        rid, rec = self.pending.open({"type": "ask_user", "questions": questions})
        self.bus.emit(rec["payload"])
        answer = self._wait(rid, rec, self.agent._interrupt, lambda: None)
        return answer if isinstance(answer, dict) else None

    # ---- 跑一轮 ----

    def send(self, text: str) -> str:
        """起一轮，或在忙时排队。返回 "started" / "queued"。

        忙时【排队而不是拒绝】：内核的 _turn_loop 每轮开头会 drain 待注入的用户消息，
        所以中途发的话能当场转向；赶不上的由收尾兜底起一轮。直接回 409 会让用户以为
        自己白打了一段字。
        """
        with self._lock:
            if self.busy:
                self.agent.queue_user(text)
                self.bus.emit({"type": "queued", "text": text})
                return "queued"
            self.busy = True
        self.bus.emit({"type": "turn_start", "kind": "user", "text": text})
        threading.Thread(target=self._pump, args=(text,), daemon=True).start()
        return "started"

    def _pump(self, text: str | None) -> None:
        """事件泵。四条必须逐字照抄 TUI：try 泵 / 后端错转人话 Notice / 其它异常也转 Notice /
        finally 无条件发轮终止哨兵——少了最后一条，一次崩溃就让前端永远卡在"忙"。

        text=None 走 run_bg_turn（无用户输入的接续轮）。【不能拿空串走 run_turn】——那会往
        transcript 里记一条空的 user 消息，历史里凭空多出一条谁也没说过的话。
        """
        try:
            stream = self.agent.run_turn(text) if text is not None else self.agent.run_bg_turn()
            for ev in stream:
                msg = encode(ev)
                if msg is not None:
                    self.bus.emit(msg)
        except ProviderError as e:
            self.bus.emit({"type": "notice", "text": str(e)})
        except Exception as e:                       # noqa: BLE001 —— 泵不能因任何异常静默死掉
            self.bus.emit({"type": "notice", "text": f"出错：{type(e).__name__}: {e}"})
        finally:
            with self._lock:
                self.busy = False
            self.bus.emit({"type": "turn_end", "context_tokens": self.agent.context_tokens})
            # 待切的后端在这里兑现，且【必须早于 maybe_bg_turn】——晚一步就又忙起来了，
            # 待切标志会一直挂着，用户只看到"已保存"却永远没换过模型。
            if self._pending_switch:
                self._do_switch()
            self.maybe_bg_turn()

    def maybe_bg_turn(self) -> None:
        """空闲且有【后台完成】或【没赶上 drain 的排队消息】→ 起一轮无用户输入的接续。

        两种来源：
        ① 后台任务跑完，agent 需要被告知结果才能接着干（TUI 的 _start_bg_turn 同款）；
        ② 用户消息发在本轮 drain 之后没赶上——绝大多数情况下它在本轮内就被注入了（即时转向），
           这里只是兜底。
        正忙则不起：正在跑的那轮会顺手 drain 掉这些，busy 充当单一互斥。
        """
        ag = self.agent
        if not (ag._bg.has_pending() or ag.has_pending_user()):
            return
        with self._lock:
            if self.busy:
                return
            self.busy = True
        self.bus.emit({"type": "turn_start", "kind": "bg"})
        threading.Thread(target=self._pump, args=(None,), daemon=True).start()

    def push_state(self) -> None:
        """状态变了主动推一帧。轮询快照（_snapshot）只盯后台任务那几项，
        切模型/改阈值/重连 MCP 这些它一概看不见，改完必须自己推。"""
        self.bus.emit(dict(self.state(), type="state"))

    # ---- MCP ----

    def _connect_mcp(self, announce: bool = False) -> None:
        """连 MCP server 并把工具注册进 registry。启动时在后台线程跑。

        启动期【只加不减】，所以不与正在跑的轮抢：工具表是发请求那一刻快照给模型的，
        中途多出几个工具，最坏是这一轮用不上它们。摘工具就不一样了（见 mcp_reload）。
        """
        servers = load_mcp_config(default_config_paths(str(self.cwd)))
        clients, tools, errors = connect_servers(servers, cwd=str(self.cwd)) if servers else ([], [], [])
        for t in tools:
            self.registry.register(t)
        self.mcp_clients, self.mcp_errors = clients, errors
        self._mcp_tools = [t.name for t in tools]
        if errors:
            self.bus.emit({"type": "notice", "text": "MCP 未连上：" + "；".join(errors[:3])})
        elif announce:
            # 一个都没配也要回话——手动点了「重连」却毫无反应，只会让人以为按钮坏了
            self.bus.emit({"type": "notice",
                           "text": f"MCP 已重连：{len(clients)} 个 server / {len(tools)} 个工具"
                                   if clients else "MCP 已重连：当前没有配置任何 server"})
        self.push_state()

    def _mcp_origins(self) -> dict:
        """每个 server 是在哪个文件里定义的（name → "global" / "project"）。

        删除必须按【它自己所在的文件】来：一律按全局删的话，项目级 .mcp.json 里的那条
        永远删不掉，还会回一句"这个文件里没有 xxx"，看着像数据坏了。
        同名时以项目级为准——和 load_mcp_config 的合并顺序（后面的覆盖前面的）保持一致。
        """
        paths = default_config_paths(str(self.cwd))
        origin: dict = {}
        for scope, pp in (("global", paths[0]), ("project", paths[1])):
            for name in load_mcp_config([pp]):
                origin[name] = scope
        return origin

    def _mcp_path(self, scope: str) -> Path:
        """全局 = ~/.mecode/mcp.json；项目 = <工作区>/.mcp.json（可提交、可分享）。"""
        paths = default_config_paths(str(self.cwd))
        return Path(paths[1]) if scope == "project" else Path(paths[0])

    def mcp_view(self) -> dict:
        servers = load_mcp_config(default_config_paths(str(self.cwd)))
        live = {c.name: len(c.tools) for c in self.mcp_clients}
        paths = default_config_paths(str(self.cwd))
        origin = self._mcp_origins()
        return {
            "servers": [dict(st, connected=st["name"] in live, tools=live.get(st["name"], 0),
                             scope=origin.get(st["name"], "global"))
                        for st in server_states(servers)],
            "errors": self.mcp_errors,
            "presets": list(TIMEOUT_PRESETS),
            "paths": [{"scope": sc, "path": Path(pp).as_posix(), "exists": Path(pp).is_file()}
                      for sc, pp in (("global", paths[0]), ("project", paths[1]))],
        }

    def mcp_save(self, name: str, command: str, args: list, env: dict,
                 cwd: str, scope: str) -> dict:
        """写一条 server 进配置文件（读-改-写，保住同文件里的其它 server）。"""
        name = (name or "").strip()
        if not _MCP_NAME.fullmatch(name):
            return {"error": "名称只能用字母/数字/下划线/连字符——它会成为工具名前缀"}
        if not (command or "").strip():
            return {"error": "命令不能为空"}
        path = self._mcp_path(scope)
        data: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        spec: dict = {"command": command.strip()}
        if args:
            spec["args"] = [str(a) for a in args]
        if env:
            spec["env"] = {str(k): str(v) for k, v in env.items()}
        if cwd:
            spec["cwd"] = cwd
        data.setdefault("mcpServers", {})[name] = spec
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "path": path.as_posix()}

    def mcp_delete(self, name: str, scope: str) -> dict:
        path = self._mcp_path(scope)
        if not path.is_file():
            return {"error": "配置文件不存在"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "配置文件读不动"}
        if not isinstance(data, dict) or name not in data.get("mcpServers", {}):
            return {"error": f"这个文件里没有 {name}"}
        data["mcpServers"].pop(name)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True}

    def mcp_reload(self) -> dict:
        """按当前配置重连。忙时拒绝——这条会【摘掉】旧工具，而工具表是发请求那一刻
        快照给模型的：一轮跑到一半摘走工具，模型下一次调用只会拿到"没有这个工具"。"""
        with self._lock:
            if self.busy:
                return {"error": "当前还有一轮在跑，等结束再重连"}
        for c in self.mcp_clients:
            try:
                c.stop()
            except Exception:                        # noqa: BLE001 —— 子进程已死等各种，重连不该被它挡住
                pass
        for name in self._mcp_tools:
            self.registry.unregister(name)
        self.mcp_clients, self.mcp_errors, self._mcp_tools = [], [], []
        self._connect_mcp(announce=True)
        return {"ok": True, "servers": len(self.mcp_clients), "errors": self.mcp_errors}

    # ---- 技能 ----

    def system_view(self) -> dict:
        """当前【真的发给模型】的 system prompt + 本模式每轮注入的 reminder（TUI 的 /system）。

        为什么要有这个入口：模型被训练成不吐自己的 system prompt，问它拿到的是编的。
        要看只能从这边读 messages[0]。纯本地、不调模型。
        """
        msgs = self.agent.messages
        head = msgs[0].get("content", "") if msgs and msgs[0].get("role") == "system" else ""
        return {"system": head, "reminder": self.agent.mode_reminder,
                "mode": self.mode, "chars": len(head)}

    def skills_view(self) -> dict:
        return {"skills": [{"name": s.name, "description": s.description, "source": s.source,
                            "path": s.path.as_posix(), "enabled": s.enabled}
                           for s in discover_skills(self.cwd)]}

    def skill_toggle(self, name: str, enabled: bool) -> dict:
        """启停写的是禁用名单。技能索引是建会话时拼进 system prompt 的，故【新会话生效】。"""
        set_enabled(name, bool(enabled))
        self.push_state()
        return {"ok": True, "note": "新会话生效"}

    def skill_run(self, name: str) -> dict:
        """把技能全文以一次性 <system-reminder> 注入并起一轮（同 TUI 的 /skill 面板）。

        忙时【拒绝】而不是排队：_pending_reminder 是下一轮开头才注入的，而排队的用户消息
        会被当前这一轮 drain 走——两者会落到不同轮，模型收到"请按技能执行"时手上还没有技能正文。
        TUI 也是直接拒绝并提示等本轮结束。
        """
        if self.busy:
            return {"error": f"本轮还在跑，技能「{name}」未注入——等结束后再用"}
        if not current_backend().model:
            return {"error": "还没配置模型后端——先去设置里配一个"}
        for s in discover_skills(self.cwd):
            if s.name != name:
                continue
            if not s.enabled:
                return {"error": f"技能「{name}」当前是停用状态"}
            self.agent._pending_reminder = skill_reminder(s)
            self.send(f"请按技能「{name}」的流程执行。")
            return {"ok": True}
        return {"error": f"没有名为 {name} 的技能"}

    # ---- 模型后端 ----

    def config_view(self) -> dict:
        """模型页要的全部数据。api_key 只给掩码，原文不出进程（见 mask_key）。"""
        cur = current_backend()
        prof = self.agent.provider.profile
        raw = load_user_config()
        cfg = self.agent.config
        return {
            "providers": [{
                "name": p.name, "base_url": p.base_url,
                "key_help": p.key_help, "key_url": p.key_url,
                "models": [{"id": m.id, "window": m.context_window,
                            "thinks": m.profile.supports_thinking,
                            "toggleable": m.profile.toggleable,
                            "efforts": list(m.profile.effort_tiers)} for m in p.models],
            } for p in PROVIDERS],
            "saved": [{
                "base_url": e.get("base_url", ""), "model": e.get("model", ""),
                "key_hint": mask_key(e.get("api_key", "")),
                "context_cap": int(e.get("context_cap", 0) or 0),
                "window": context_window_for(e.get("model", "")),
                "current": (e.get("base_url"), e.get("model")) == (cur.base_url, cur.model),
            } for e in saved_configs()],
            "current": {"base_url": cur.base_url, "model": cur.model},
            "prefs": {
                "context_cap": int(raw.get("context_cap", 0) or 0),
                "compact_threshold": float(raw.get("compact_threshold", 0) or 0),
                "context_limit": getattr(cfg, "context_limit", 0),
                "threshold_now": getattr(cfg, "compact_threshold", 0),
                # 设置页拿它现算"上限 × 阈值 = 触发点"。必须由服务端给：
                # 真实上限是 min(窗口, CAP)，而"窗口"对注册表不认识的模型走兜底值，
                # 前端从 saved[].window 去猜的话，本地模型那一栏会猜成 0、算出偏大的触发点。
                "window": effective_window(cur.model),
            },
            "thinking": {
                "on": getattr(self.agent.provider, "thinking_on", False),
                "effort": getattr(self.agent.provider, "effort", ""),
                "supports": prof.supports_thinking, "toggleable": prof.toggleable,
                "efforts": list(prof.effort_tiers),
            },
            "env": env_backend(),                 # .env 里检测到的后端，给"点此导入"
            "pending_switch": self._pending_switch,
            "config_path": USER_CONFIG_PATH.as_posix(),
        }

    def save_backend(self, base_url: str, model: str, api_key: str,
                     context_cap: int = 0) -> dict:
        """新增/覆盖一条后端并切过去。三项缺一不可——空后端只会在发消息时才炸。"""
        base_url, model, api_key = base_url.strip().rstrip("/"), model.strip(), api_key.strip()
        if not (base_url and model and api_key):
            return {"error": "base_url / 模型名 / api_key 三项都要填"}
        if not base_url.startswith(("http://", "https://")):
            return {"error": "base_url 要以 http:// 或 https:// 开头"}
        cap = int(context_cap or 0)
        if cap and cap < 1000:
            return {"error": "上下文上限要么留空，要么 ≥1000"}
        if not cap:
            # 新增表单里没有"上限"这一项（它在设置页单独一行改）。重存一条已有后端时
            # 直接传 0 会把它原来的 context_cap 抹掉——没填不等于要清空。
            for e in saved_configs():
                if (e.get("base_url"), e.get("model")) == (base_url, model):
                    cap = int(e.get("context_cap", 0) or 0)
                    break
        save_user_config(base_url, model, api_key, context_cap=cap)
        return self._apply_backend()

    def switch_backend_to(self, base_url: str, model: str) -> dict:
        """切到一条已保存的后端。key 从 config.json 现取——它从来没到过前端，也不用回传。"""
        for e in saved_configs():
            if (e.get("base_url"), e.get("model")) == (base_url, model):
                save_user_config(e.get("base_url", ""), e.get("model", ""), e.get("api_key", ""),
                                 keep_reasoning=e.get("keep_reasoning", ""),
                                 context_cap=int(e.get("context_cap", 0) or 0))
                return self._apply_backend()
        return {"error": "没有这条已保存的后端"}

    def delete_backend(self, base_url: str, model: str) -> dict:
        """从已存列表删一条。删的若是当前后端，当前连接不受影响（顶层字段还在），
        只是下次它不再出现在列表里——照搬 config.delete_saved 的语义。"""
        delete_saved(base_url, model)
        return {"ok": True}

    def _apply_backend(self) -> dict:
        """按此刻的 config.json 热切。忙时【推迟到本轮结束】而不是拒绝：
        中途换 Provider 会把正在跑的那条流搞乱（历史里的思考字段按新档案过滤，半截流对不上）。"""
        with self._lock:
            if self.busy:
                self._pending_switch = True
                return {"ok": True, "deferred": True, "model": current_backend().model}
        self._do_switch()
        return {"ok": True, "deferred": False, "model": current_backend().model}

    def _do_switch(self) -> None:
        """真正换 Provider。照抄 tui.py:_apply_backend_switch，顺序别动。"""
        self._pending_switch = False
        be = current_backend()
        prov = Provider(be)
        self.agent.switch_backend(prov, current_agent_config())
        if self.agent.store is not None:      # 思考态回新档案默认并落盘（resume 恢复用）
            self.agent.store.write_header(thinking_on=prov.thinking_on, effort=prov.effort)
        self.bus.emit({"type": "notice", "text": f"已切换到 {be.model}，即刻生效"})
        self.push_state()

    def update_prefs(self, context_cap, compact_threshold) -> dict:
        """只改上限/阈值。【不重建 Provider】——后端根本没换，重建只会顺手把思考开关和
        深度档重置回档案默认，是纯副作用（tui.py:_apply_settings_change 同款）。"""
        if context_cap is not None:
            try:
                context_cap = int(context_cap)
            except (TypeError, ValueError):
                return {"error": "上下文上限要是整数"}
            if context_cap and context_cap < 1000:
                return {"error": "上下文上限要么留空（用默认），要么 ≥1000"}
        if compact_threshold is not None:
            try:
                compact_threshold = float(compact_threshold)
            except (TypeError, ValueError):
                return {"error": "压缩阈值要是小数"}
            if compact_threshold and not 0.3 <= compact_threshold <= 0.95:
                return {"error": "压缩阈值要在 0.3~0.95 之间（太低压不停，太高来不及压）"}
        update_settings(context_cap=context_cap, compact_threshold=compact_threshold)
        self.agent.config = current_agent_config()
        self.push_state()
        cfg = self.agent.config
        return {"ok": True, "context_limit": cfg.context_limit,
                "compact_at": int(cfg.context_limit * cfg.compact_threshold)}

    def test_backend(self, base_url: str, model: str, api_key: str) -> dict:
        """发一次 1 token 补全探活：保存之前就能发现 key 错 / 模型名不存在 / 网络不通。

        key 留空 = 用已保存那条的。页面上从来只显示掩码，用户没法把原 key 敲回来，
        不这么兜底的话"测已存的后端"就永远测不了。
        跑在这条 HTTP 请求自己的线程上（ThreadingHTTPServer 一请求一线程），
        阻塞的是它自己，不碰 agent 工作线程。
        """
        base_url = base_url.rstrip("/")
        if not api_key:
            for e in saved_configs():
                if (e.get("base_url"), e.get("model")) == (base_url, model):
                    api_key = e.get("api_key", "")
                    break
        if not (base_url and model and api_key):
            return {"ok": False, "msg": "三项信息不全，没法测"}
        import httpx
        try:
            r = httpx.post(f"{base_url}/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 1,
                                 "messages": [{"role": "user", "content": "hi"}]},
                           timeout=15)
        except Exception as e:                     # noqa: BLE001 —— 探活的全部失败形态都要转人话
            return {"ok": False, "msg": f"连接失败：{type(e).__name__} {str(e)[:100]}"}
        if r.status_code < 400:
            return {"ok": True, "msg": f"连接成功（{model}）"}
        detail = ""
        try:
            detail = ((r.json().get("error") or {}).get("message") or "")[:100]
        except Exception:                          # noqa: BLE001 —— 错误体不是 JSON 也很常见
            pass
        return {"ok": False, "msg": f"HTTP {r.status_code} {detail}".strip()}

    def set_thinking(self, on, effort: str) -> dict:
        """思考开关 / 深度档。运行态存在 Provider 上，同时落进会话头供 resume 恢复。"""
        prov = self.agent.provider
        prof = prov.profile
        if on is not None:
            if not prof.supports_thinking:
                return {"error": "当前模型没有可控的思考开关"}
            if not prof.toggleable:
                return {"error": "这个模型的思考是强制开的，关不掉"}
            prov.thinking_on = bool(on)
        if effort:
            if effort not in prof.effort_tiers:
                return {"error": f"这个模型没有 {effort} 档"}
            prov.effort = effort
        if self.agent.store is not None:
            self.agent.store.write_header(thinking_on=prov.thinking_on, effort=prov.effort)
        self.push_state()
        return {"ok": True, "on": prov.thinking_on, "effort": prov.effort}

    def compact_now(self) -> bool:
        """手动压缩一次（进度环点击触发）。忙时拒绝——压缩要换掉 messages，跟正在跑的轮抢。

        实现上把阈值临时调成 0 再跑内核的 _maybe_compact，而不是自己去调 compact()：
        "压缩后那几件事（换 messages、重置 token 计数、重注任务清单）"内核特意收在一处，
        绕过它另写一遍，迟早漏掉其中一件。
        """
        import dataclasses
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        # 前端只对 kind=="user" 渲气泡，compact 这一档不渲——不自己发一条的话，
        # 对话区在整个压缩期间是空的，只有左上角在转，看不出到底在不在干活。
        self.bus.emit({"type": "turn_start", "kind": "compact"})
        self.bus.emit({"type": "notice", "text": "正在压缩上下文…（可随时点停止）"})
        # 上一次打断留下的标志必须先清掉，否则摘要请求刚开跑就被判为"已打断"，
        # 表现成点了压缩什么也没发生。（和 Agent._pre_turn 开轮清标志同一个道理。）
        self.agent._interrupt.clear()
        said = False
        try:
            saved = self.agent.config
            self.agent.config = dataclasses.replace(saved, compact_threshold=0.0)
            try:
                for ev in self.agent._maybe_compact():
                    m = encode(ev)
                    if m is not None:
                        said = True
                        self.bus.emit(m)
            finally:
                self.agent.config = saved
        except Exception as e:                      # noqa: BLE001
            said = True
            self.bus.emit({"type": "notice", "text": f"压缩失败：{type(e).__name__}: {e}"})
        else:
            # _maybe_compact 一个事件都没产出 = 没压成，而它只在"被打断"时才自己解释。
            # 剩下的情况得由这里说清楚，不然用户看到的是"点了没反应"。
            if not said:
                self.bus.emit({"type": "notice",
                               "text": "没有可压缩的内容（历史还太短，或这次摘要没生成出来）"})
        finally:
            with self._lock:
                self.busy = False
            self.bus.emit({"type": "turn_end", "context_tokens": self.agent.context_tokens})
        return True

    def interrupt(self) -> None:
        self.agent.request_interrupt()

    def set_mode(self, mode: str) -> bool:
        """切模式 = 重建权限策略（同 TUI）。模式提示词每轮注入，不进 system prompt。

        【计划模式在这里是支持的】——build_agent 拒绝以 plan 启动，那条限制是给无头场景的
        （"计划需要人审批，无头没人批"）；桌面端有界面、有人可以批，所以只是不能【以它启动】，
        运行时切过去完全成立。
        """
        if mode not in MODES:
            return False
        st = self.agent.store
        plan_path = st.plan_path.as_posix() if st is not None else None
        if mode != "plan":
            self._last_exec_mode = mode        # 记住最近的执行模式：批准计划后回退到它
        if st is not None:
            policy = apply_mode(
                PermissionPolicy.from_persisted(st.load_permissions(), project_root=st.cwd), mode)
            if mode == "plan" and plan_path:
                policy.plan_path = plan_path   # 计划模式只读，但对计划文件本身放行（唯一的例外）
            self.agent.policy = policy
        self.agent.mode = mode
        # 计划模式要把【计划文件路径】拼进每轮提示，否则模型不知道该往哪写
        reminder = MODES[mode].prompt
        if mode == "plan" and plan_path:
            reminder += f"\n计划文件（把计划写在这里、用 write_file/edit_file 迭代它）：{plan_path}"
        self.agent.mode_reminder = reminder
        self.agent.subagent_reminder = MODES[mode].sub_prompt
        self.mode = mode
        self.bus.emit({"type": "state", **self.state()})   # 别的标签页的模式条也要跟着变
        return True

    def approve_plan(self, target: str = "") -> bool:
        """批准计划：切回执行模式 → 起一轮按计划执行。步骤照抄 tui._approve_plan。

        两条容易漏的：
        ① 执行期仍要放行计划文件（模型要 read_file 回看、必要时更新它）；
        ② 执行提示走【一次性 reminder】而不是用户消息——它是给模型的操作指引，
           显示成用户说的话会让历史看起来像用户自己打了一段莫名其妙的提示。
        """
        if self.busy:
            return False
        target = target if target in MODES and target != "plan" else self._last_exec_mode
        st = self.agent.store
        plan_path = st.plan_path.as_posix() if st is not None else None
        self.set_mode(target)
        if self.agent.policy is not None and plan_path:
            self.agent.policy.plan_path = plan_path
        guidance = "（执行提示）若是多步任务，先用 task_create 把计划拆成任务清单跟踪进度，再逐步实施。"
        if plan_path:
            guidance += f"计划文件在 {plan_path}，需要时可 read_file 回看。"
        self.agent._pending_reminder = guidance
        self.send("我已批准上述计划，现在按计划开始执行。")
        return True

    def pending_plan(self) -> str:
        """会话是否停在"已提交计划、还没批准"。resume 回来要能接着批。

        判据：最后一条【真·用户消息】之后，有 assistant 调过 exit_plan（同 tui._resume_pending_plan）。
        """
        st = self.agent.store
        if st is None or not st.plan_path.is_file():
            return ""
        msgs = self.agent.messages
        last_user = -1
        for i, m in enumerate(msgs):
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("<system-reminder>"):
                last_user = i
        pending = any(tc.get("function", {}).get("name") == "exit_plan"
                      for m in msgs[last_user + 1:] if m.get("role") == "assistant"
                      for tc in (m.get("tool_calls") or []))
        return st.plan_path.read_text(encoding="utf-8", errors="replace") if pending else ""

    def state(self) -> dict:
        b = current_backend()
        prov = self.agent.provider
        st = self.agent.store
        cfg = self.agent.config
        return {
            "busy": self.busy,
            "mode": self.mode,
            "modes": list(MODES),                   # 含 plan：桌面端有人可批，支持它
            "model": b.model,
            "thinking_on": getattr(prov, "thinking_on", False),
            "effort": getattr(prov, "effort", ""),
            "context_tokens": self.agent.context_tokens,
            "context_limit": getattr(cfg, "context_limit", 0),
            # 压缩触发点 = 上限 × 阈值。前端的进度环量的是"离压缩还有多远"，
            # 不是"离上限还有多远"——上限那个数用户其实碰不到，压缩先发生。
            "compact_at": int(getattr(cfg, "context_limit", 0)
                              * getattr(cfg, "compact_threshold", 0)),
            "cwd": str(st.cwd) if st else "",
            "session": st.session_id if st else "",
            "tools": sorted(s["function"]["name"] for s in self.agent.tools.schemas()),
            "skills": [{"name": s.name, "description": s.description}
                       for s in discover_skills(self.cwd)],
            "tasks_count": len(self.agent._tasks.summaries()),
            "bg_count": len(self.agent._bg.running()),
            "pending_plan": bool(self.pending_plan()),
            "pending_switch": self._pending_switch,
            "mcp": len(self.mcp_clients),
            "mcp_errors": len(self.mcp_errors),
        }

    # ---- 侧栏状态：内核没有变更回调，只能定时比对快照 ----

    def _snapshot(self) -> dict:
        """可用于比对的状态快照。

        两类字段【必须排除】，否则每个 tick 都判成"变了"、每 0.5 秒推一帧：
          elapsed()        —— 每次调用都不同
          next_checkin_at  —— agent 每次 due_checkins 都会重排
        起点 started_at 是 monotonic 值，跨进程无意义，换算成墙钟推给前端本地计时。
        """
        now_wall, bg, seen = time.time(), [], set()
        for t in self.agent._bg.running():
            seen.add(t.id)
            # 【墙钟必须缓存，不能每次现算】：now - elapsed() 里两个时钟有微秒级漂移，
            # 每次算出来都差一点点，快照就每个 tick 都判成"变了"——等于每 0.5 秒推一帧，
            # 正是排除 elapsed 想避免的那件事换了个更隐蔽的形式。按任务 id 记住首次算的值。
            if t.id not in self._bg_start:
                self._bg_start[t.id] = now_wall - t.elapsed()
            bg.append({"id": t.id, "command": t.command[:160], "status": t.status,
                       "started_at_wall": self._bg_start[t.id],
                       "is_subagent": t.is_subagent, "description": t.description})
        for tid in [k for k in self._bg_start if k not in seen]:
            del self._bg_start[tid]           # 任务没了就别攒着
        wf = self.agent._workflow
        return {
            "bg": bg,
            "workflow": {
                # run_id 用阶段 id 拼签名，【不能用 id(run.stages)】——那是对象内存地址，
                # 跨进程毫无意义，而且同一个列表对象被原地改写时地址还不变。
                "run_id": "|".join(s.id for s in wf.stages),
                "active": wf.active,
                "stages": [{"id": st.id, "status": st.status,
                            "description": st.description or st.id,
                            "after": st.after, "result": (st.result or "")[:400]}
                           for st in wf.stages],
            },
        }

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(TICK)
            try:
                snap = self._snapshot()
            except Exception:                  # noqa: BLE001 —— 轮询线程不能因一次读失败而死
                continue
            for key in ("bg", "workflow"):
                if snap[key] != self._snap.get(key):
                    self.bus.emit({"type": key, key: snap[key], "now": time.time()})
            self._snap = snap

    # ---- 会话 ----

    def sessions(self, archived: bool = False) -> dict:
        """本项目下的会话，最近的在前。默认只给【未归档】的。

        cwd 必须传 self.cwd：SessionStore 用 project_slug(cwd) 定位，传错会返回空表且不报错。
        归档数一并返回：没有它，前端就没法决定要不要显示"已归档"入口。
        """
        cur = self.agent.store.session_id if self.agent.store else ""
        out, n_arch = [], 0
        for m in SessionStore.list_sessions(root=agent_config.session_root, cwd=self.cwd):
            is_arch = bool(m.get("archived"))
            n_arch += is_arch
            if is_arch != archived:
                continue
            sid = m.get("session_id")
            out.append({"id": sid, "title": m.get("title", ""),
                        "updated_at": m.get("updated_at", 0), "mode": m.get("mode", ""),
                        "tokens": m.get("context_tokens", 0), "current": sid == cur,
                        "archived": is_arch, "forked_from": m.get("forked_from", "")})
        return {"sessions": out, "archived_count": n_arch}

    # ---- 工作区 ----

    def workspaces(self, archived: bool = False) -> dict:
        """所有工作区 + 各自的会话（当前这个排最前）。侧栏按文件夹分组用。

        archived=True 给【已归档】那一份。归档视图也要分组——不分的话，
        几个项目的归档会话混在一张平表里，标题又常常长得一样，根本分不出哪条是哪个项目的。
        archived_count 不随视图变，恒为"这个工作区有几条归档"：前端要拿它决定显不显示入口。
        """
        cur_sid = self.agent.store.session_id if self.agent.store else ""
        cur_cwd = str(self.cwd)
        out = []
        for pj in SessionStore.list_projects(root=agent_config.session_root):
            n_arch = sum(1 for m in pj["sessions"] if m.get("archived"))
            sess = [{"id": m.get("session_id"), "title": m.get("title", ""),
                     "updated_at": m.get("updated_at", 0), "mode": m.get("mode", ""),
                     "tokens": m.get("context_tokens", 0),
                     "archived": bool(m.get("archived")),
                     "current": m.get("session_id") == cur_sid and pj["cwd"] == cur_cwd}
                    for m in pj["sessions"] if bool(m.get("archived")) == archived]
            is_cur = pj["cwd"] == cur_cwd
            if archived and not sess:
                continue          # 归档视图里空组没有意义，直接不列
            out.append({"cwd": pj["cwd"], "name": pj["name"] or pj["slug"],
                        "current": is_cur, "archived_count": n_arch,
                        "sessions": sess})
        # 当前工作区置顶；它可能还没有任何会话（刚切过去），补一个空组，
        # 不然侧栏上看不到"我在哪"。
        if not archived and not any(w["current"] for w in out):
            out.insert(0, {"cwd": cur_cwd, "name": Path(cur_cwd).name, "current": True,
                           "archived_count": 0, "sessions": []})
        out.sort(key=lambda w: not w["current"])
        return {"workspaces": out}

    def set_workspace(self, path: str) -> dict:
        """换工作区（换项目根）。忙时拒绝——要换 Agent 实例，同 resume。

        【会 os.chdir】：工具按【进程工作目录】解析相对路径（bash 子进程直接继承，
        read_file 走 Path(path).resolve()）。不 chdir 的话换了工作区，模型说"读 README.md"
        读的还是旧目录的那个——而且不会报错，只是内容对不上，最难查的那种。

        连带要换的四样：会话存储位置(slug)、权限根、记忆沙箱、项目级 .mcp.json。
        """
        if self.busy:
            return {"error": "当前还有一轮在跑，等结束再换工作区"}
        if not (path or "").strip():
            # Path("").resolve() 会得到【当前目录】，不拦的话漏传字段会静默变成空操作，
            # 界面上看着成功、实际什么也没发生
            return {"error": "没有给路径"}
        try:
            target = Path(path).expanduser().resolve()
        except (OSError, ValueError):
            return {"error": "路径不合法"}
        if not target.is_dir():
            return {"error": f"不是一个文件夹：{target.as_posix()}"}
        if target == self.cwd:
            return {"ok": True, "cwd": target.as_posix(), "unchanged": True}

        os.chdir(target)
        self.cwd = target
        # 记忆工具是【绑定到某个记忆目录】的闭包，换项目必须重新注册；
        # 同名 register 直接覆盖，不必先摘。
        store = SessionStore(root=agent_config.session_root, cwd=target)
        for t in memory_tools(store.memory_dir):
            self.registry.register(t)
        self._swap_agent(store, None)
        # MCP 的 ${cwd} 占位符和项目级 .mcp.json 都跟工作区走 → 重连
        for c in self.mcp_clients:
            try:
                c.stop()
            except Exception:                     # noqa: BLE001 —— 换工作区不该被一个死进程挡住
                pass
        for tname in self._mcp_tools:
            self.registry.unregister(tname)
        self.mcp_clients, self.mcp_errors, self._mcp_tools = [], [], []
        threading.Thread(target=self._connect_mcp, daemon=True).start()

        self.bus.emit({"type": "resumed", "session": store.session_id, "title": ""})
        self.bus.emit({"type": "sessions_changed"})
        self.push_state()
        return {"ok": True, "cwd": target.as_posix(), "session": store.session_id}

    def pick_workspace(self) -> dict:
        """开系统自带的文件夹对话框，选完直接切过去。

        这条请求【会一直阻塞到用户点完】——它跑在自己那条 HTTP 线程上
        （ThreadingHTTPServer 一请求一线程），不碰 agent 工作线程。给 5 分钟上限，
        免得用户把对话框晾在那儿、这条线程永远收不回来。
        """
        import subprocess
        extra = {}
        if sys.platform == "win32":     # 不弹一个黑窗口出来
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run([sys.executable, "-c", _PICK_SRC, str(self.cwd)],
                               capture_output=True, text=True, timeout=300, **extra)
        except subprocess.TimeoutExpired:
            return {"error": "文件夹对话框超过 5 分钟没有结果，已放弃"}
        except OSError as e:
            return {"error": f"打不开系统对话框：{e}"}
        if r.returncode != 0:
            # 没有图形界面（纯 SSH / 容器）时 Tk 起不来，让前端回落到页面内的选择器
            return {"error": "系统对话框不可用", "fallback": True,
                    "detail": (r.stderr or "").strip()[:200]}
        picked = (r.stdout or "").strip()
        if not picked:
            return {"cancelled": True}
        return self.set_workspace(picked)

    def browse(self, path: str) -> dict:
        """列一个目录下的子目录（工作区选择器用）。只列目录名，不列文件。

        这不是新增的暴露面：拿得到 token 就等于能驱动 agent 跑 bash，文件系统本来就全开着。
        隐藏目录跳过——.git/.venv 这些列出来只会把选择器塞满。
        """
        try:
            base = Path(path).expanduser().resolve() if path else Path.home()
        except (OSError, ValueError):
            return {"error": "路径不合法"}
        if not base.is_dir():
            return {"error": "不是一个文件夹"}
        dirs = []
        try:
            for d in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if d.name.startswith(".") or not d.is_dir():
                    continue
                dirs.append({"name": d.name, "path": d.as_posix()})
        except PermissionError:
            return {"error": "没有权限读这个文件夹"}
        parent = base.parent
        return {"path": base.as_posix(), "name": base.name or base.as_posix(),
                "parent": parent.as_posix() if parent != base else "",
                "dirs": dirs[:400]}

    def _store_for(self, session_id: str):
        """按 id 造一个指向那个会话的 store（不切过去）。

        cwd 必须传：漏了就落回 Path.cwd()，store.dir 指向一个不存在的目录——不报错，
        只是改名/归档写进了别的地方，界面上看着像"点了没反应"。
        """
        ids = {m.get("session_id") for m in
               SessionStore.list_sessions(root=agent_config.session_root, cwd=self.cwd)}
        if session_id not in ids:
            return None
        return SessionStore(root=agent_config.session_root, cwd=self.cwd, session_id=session_id)

    def rename_session(self, session_id: str, title: str) -> dict:
        st = self._store_for(session_id)
        if st is None:
            return {"error": "没有这个会话"}
        try:
            name = st.set_title(title)
        except ValueError as e:
            return {"error": str(e)}
        self.bus.emit({"type": "sessions_changed"})
        return {"ok": True, "title": name}

    def archive_session(self, session_id: str, archived: bool) -> dict:
        """归档 = 从清单里收起来，transcript 一个字不动，随时能翻回来。

        不允许归档【当前正在用的】那个：归档后它会从清单消失，而你还在里面聊，
        界面上就成了"标题栏有个会话、列表里却找不到它"。让用户先切走，语义最不含糊。
        """
        cur = self.agent.store.session_id if self.agent.store else ""
        if archived and session_id == cur:
            return {"error": "这是当前正在用的会话——先切到别的会话再归档"}
        st = self._store_for(session_id)
        if st is None:
            return {"error": "没有这个会话"}
        st.set_archived(archived)
        self.bus.emit({"type": "sessions_changed"})
        return {"ok": True}

    def fork_session(self, session_id: str) -> dict:
        """复制一份会话并【切过去】。忙时拒绝——切会话要换 Agent 实例（同 resume）。"""
        if self.busy:
            return {"error": "当前还有一轮在跑，等结束再分叉"}
        st = self._store_for(session_id)
        if st is None:
            return {"error": "没有这个会话"}
        new = st.fork()
        if not self.resume(new.session_id):
            return {"error": "分叉成功但切不过去，手动点一下新会话"}
        return {"ok": True, "session": new.session_id}

    def history(self, before: int = 0, limit: int = HISTORY_PAGE) -> dict:
        """当前会话的人类可读历史，【从尾往前】分页。

        和 load_messages 分工不同：那个 fold 到最后一个压缩点、发给模型；这个顺读全量给人看。
        同一份 transcript 的两种读法——会话存储的核心设计。

        assistant 行要把 tool_calls 一起带出来：少了它，前端没法把工具结果贴回对应的调用
        下方，只能渲成"一堆调用后面跟一堆结果"。
        """
        st = self.agent.store
        if st is None:
            return {"items": [], "total": 0, "next_before": 0}
        msgs = st.read_transcript_messages()
        total = len(msgs)
        # 轮数 / 工具次数【从 transcript 现数】，不另存一份计数器。
        # 前端那两个数原来是页面内的局部变量，刷新就归零、resume 更是从头数起。
        # transcript 里本来就逐条记着谁说了什么、调了哪些工具——单一真相，数它就行，
        # 另存计数器只会多一处能和事实对不上的状态。
        # 轮 = 真正的用户发言；注入的 <system-reminder>（模式提示/任务快照/后台接续）不算。
        turns = sum(1 for m in msgs if m.get("role") == "user"
                    and not str(m.get("content") or "").startswith("<system-reminder>"))
        ncalls = sum(len(m.get("tool_calls") or []) for m in msgs)
        end = max(0, total - before)
        start = max(0, end - limit)
        items = []
        for i, m in enumerate(msgs[start:end], start=start):
            role = m.get("role")
            text = m.get("content") or ""
            if role == "user":
                items.append({"seq": i, "role": "user", "text": text,
                              # 注入的提醒也留下、只打标记：直接丢会让"用户说了什么"和
                              # 模型看到的对不上，排查问题时找不到北
                              "reminder": str(text).startswith("<system-reminder>")})
            elif role == "assistant":
                calls = [{"id": tc.get("id"),
                          "name": (tc.get("function") or {}).get("name"),
                          "args": (tc.get("function") or {}).get("arguments", "")}
                         for tc in (m.get("tool_calls") or [])]
                if text.strip() or calls:
                    items.append({"seq": i, "role": "assistant", "text": text,
                                  "tool_calls": calls})
            elif role == "tool":
                items.append({"seq": i, "role": "tool", "text": str(text)[:2000],
                              "tool_call_id": m.get("tool_call_id")})
        return {"items": items, "total": total, "turns": turns, "tool_calls": ncalls,
                "next_before": before + (end - start) if start > 0 else 0}

    def _swap_agent(self, store: SessionStore, meta: dict | None) -> None:
        """换掉当前 agent（resume 和开新会话共用）。步骤【有严格顺序】，漏一步是静默失效。

        顺序理由见每行；照抄 tui.py:_resume_into，别按自己的理解重排。
        """
        # ① 摘掉旧 agent 的回调再换人：旧会话的后台任务跑完时回调还指着总线，会把 bg_done
        #    推进新会话的事件流——它并不在新 agent 的 running() 里，前端就出现幽灵任务。
        old = self.agent
        old._bg.on_complete = None
        old._tasks.on_change = None
        old._bg.kill_all()                     # 换会话了还留着旧任务跑，到进程退出都收不回
        for rid in self.pending.cancel_all():  # 旧会话挂着的审批窗要收掉
            self.bus.emit({"type": "dismiss", "id": rid})

        # ② 先算记忆段再拼 system：顺序反了新 agent 的 system 里就没有记忆索引，
        #    它不知道 MEMORY.md 里有什么、也就不会去 recall——表现为"续了会话但忘了你是谁"。
        memory = build_memory_prompt(store.memory_dir)
        system = build_system_prompt(session_dir=store.dir)
        if memory:
            system = f"{system}\n\n{memory}"

        meta = meta or {}
        restored = meta.get("mode") or self.mode
        if restored not in MODES or restored == "plan":
            restored = DEFAULT_MODE

        # ③ 建新 Agent。三条硬约束：
        #    registry 必须复用（新建会丢 MCP 工具/ask_user/记忆工具，还重连 MCP 子进程）；
        #    resume_messages 必须用 load_messages（fold 到最后一个压缩点），用顺读全量会把
        #      压缩前的历史全塞回上下文，第一次请求就撞窗口；
        #    config 必须重取（否则上下文上限/压缩阈值停在旧模型的窗口，压缩点算错）。
        self.agent = Agent(
            Provider(current_backend()), self.registry,
            system_prompt=system,
            config=current_agent_config(),
            store=store,
            resume_messages=store.load_messages() or None,
            policy=PermissionPolicy.from_persisted(store.load_permissions(),
                                                   project_root=store.cwd),
            ask_permission=self._ask_permission)
        self._wire()                                       # 换了 agent → 回调必须重挂
        self.agent.context_tokens = meta.get("context_tokens", 0) or 0
        prov = self.agent.provider                         # 恢复思考运行态（会话头里存的）
        prov.thinking_on = meta.get("thinking_on", True)
        prov.effort = meta.get("effort") or prov.profile.default_effort
        self.set_mode(restored)                            # 最后恢复模式（会重建 policy）
        self._snap = {}                                    # 快照作废，下一 tick 全量重推
        self.push_tasks()                                  # 装载早于回调挂接，必须补推

    def resume(self, session_id: str) -> bool:
        """切到另一个会话。忙时拒绝——换 Agent 实例时不能有一轮在跑，否则旧 run_turn
        还在往旧 store 写。"""
        if self.busy:
            return False
        metas = {m.get("session_id"): m
                 for m in SessionStore.list_sessions(root=agent_config.session_root,
                                                     cwd=self.cwd)}
        meta = metas.get(session_id)
        if meta is None:
            return False
        # cwd 必须和 list_sessions 同源：漏传就落回 Path.cwd()，--cwd 与进程工作目录不同时
        # store.dir 指向不存在的目录——不报错，只是历史读成空、新消息还写去错地方。
        store = SessionStore(root=agent_config.session_root, cwd=self.cwd,
                             session_id=session_id)
        self._swap_agent(store, meta)
        self.bus.emit({"type": "resumed", "session": session_id,
                       "title": meta.get("title", "")})
        plan = self.pending_plan()          # 续的会话若停在"已提交计划未批准"，把待批条挂回来
        if plan:
            self.bus.emit({"type": "plan", "plan": plan,
                           "path": store.plan_path.as_posix()})
        # history 不进这一帧：会话可能上千条，一帧塞爆 SSE 也塞爆 DOM。前端自己去拉。
        return True

    def new_session(self) -> str | None:
        if self.busy:
            return None
        store = SessionStore(root=agent_config.session_root, cwd=self.cwd)   # 新 uuid
        self._swap_agent(store, None)
        self.bus.emit({"type": "resumed", "session": store.session_id, "title": ""})
        return store.session_id

    # ---- 按需拉取（不进轮询帧）----

    def bg_output(self, tid: int) -> dict:
        """某个在跑的后台任务的当前输出。每次都真读一次磁盘日志尾部，所以【只在前端展开
        那一行时按需拉】，绝不能塞进 0.5 秒一次的轮询帧里。"""
        out = self.agent._bg.check_output(tid)
        return {"id": tid, "running": out is not None, "output": out or ""}

    def tool_output(self, path: str, offset: int, limit: int) -> dict:
        """读回被外置的大工具输出，分页。

        全文上限 50 万字符，一次整读会让这条 SSE/HTTP 响应占住几百毫秒。
        【路径必须限死在本会话的 tool_outputs 目录内】，否则这就是个任意文件读取接口。
        用 is_relative_to 而不是字符串前缀比较：startswith 会把同级的 tool_outputs2、
        tool_outputs_bak 也判成"在目录内"。
        """
        st = self.agent.store
        if st is None or not path:
            return {"error": "缺少 path"}
        target = Path(path).resolve()
        root = st.tool_outputs_dir.resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return {"error": "只能读本会话外置的工具输出"}
        text = target.read_text(encoding="utf-8", errors="replace")
        limit = max(1, min(limit or BLOB_PAGE, BLOB_PAGE))
        chunk = text[offset:offset + limit]
        return {"path": target.as_posix(), "total": len(text), "offset": offset,
                "chunk": chunk, "eof": offset + len(chunk) >= len(text)}

    def close(self) -> None:
        """优雅停机：先停轮询，再杀后台任务和前台进程，最后关 MCP 子进程。
        顺序不能乱——长驻进程比 TUI 更容易积攒僵尸。"""
        self._stop.set()
        try:
            self.agent._bg.kill_all()
            self.agent._kill_running_procs()
        except Exception:                        # noqa: BLE001 —— 停机路径不能再抛
            pass
        # MCP 客户端挂在 Desk 上（是本类自己连的，不是 build_agent 连的）。
        # 这里曾经读 agent.mcp_clients —— 改成自己连之后那个列表恒为空，子进程就全漏了。
        for c in self.mcp_clients:
            try:
                c.stop()
            except Exception:                    # noqa: BLE001
                pass


def _make_ask_user(desk: Desk):
    from mecode.tools import make_ask_user_tool
    return make_ask_user_tool(desk.ask_user)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    desk: Desk = None            # type: ignore[assignment]  —— main() 里注入

    # ---- 工具 ----

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _q(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _qi(self, key: str, default: int = 0) -> int:
        try:
            return int(self._q().get(key, [default])[0])
        except (TypeError, ValueError):
            return default

    # ---- 信任闸（见模块头）----

    def _origin_ok(self) -> bool:
        """Host 必须是回环（挡 DNS rebinding），Origin 存在时必须是自己（挡跨站）。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            hostpart = origin.split("//", 1)[-1].rsplit(":", 1)[0].strip("[]")
            if hostpart not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _token_ok(self) -> bool:
        """请求头优先；SSE 只能走查询串——EventSource 设不了自定义头。
        用 compare_digest 而不是 ==：字符串比较会提前短路，理论上能被计时侧信道逐字节猜出来。"""
        got = self.headers.get("X-Mecode-Token") or ""
        if not got:
            got = (self._q().get("token") or [""])[0]
        return secrets.compare_digest(got, TOKEN)

    def _guard(self) -> bool:
        if not self._origin_ok():
            self._json(403, {"error": "非本机来源"})
            return False
        if not self._token_ok():
            self._json(403, {"error": "token 无效——请从服务打印的地址打开页面"})
            return False
        return True

    # ---- 路由 ----

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # 首页也要过闸：token 就注入在它的 HTML 里，不拦的话本机任意进程 curl 一下
        # 就把 token 读走了，整道闸等于没有。其余静态资源（css/js）不含机密，放行。
        if (path.startswith("/api/") or path in ("/", "", "/index.html")) and not self._guard():
            return
        d = self.desk
        if path == "/api/events":
            return self._events()
        if path == "/api/state":
            return self._json(200, d.state())
        if path == "/api/sessions":
            return self._json(200, d.sessions(archived=self._qi("archived") == 1))
        if path == "/api/workspaces":
            return self._json(200, d.workspaces(archived=self._qi("archived") == 1))
        if path == "/api/browse":
            r = d.browse(unquote((self._q().get("path") or [""])[0]))
            return self._json(400 if r.get("error") else 200, r)
        if path == "/api/history":
            return self._json(200, d.history(self._qi("before"),
                                             self._qi("limit", HISTORY_PAGE)))
        if path == "/api/tasks":
            return self._json(200, {"tasks": d.agent._tasks.summaries()})
        if path == "/api/plan":
            return self._json(200, {"plan": d.pending_plan()})
        if path == "/api/task":
            t = d.agent._tasks.get((self._q().get("id") or [""])[0])
            if t is None:
                return self._json(404, {"error": "没有这个任务"})
            # get() 给的是活对象，worker 线程会原地改它 → 必须拷一份再序列化
            import dataclasses
            return self._json(200, dataclasses.asdict(t))
        if path == "/api/config":
            return self._json(200, d.config_view())
        if path == "/api/skills":
            return self._json(200, d.skills_view())
        if path == "/api/system":
            return self._json(200, d.system_view())
        if path == "/api/mcp":
            return self._json(200, d.mcp_view())
        if path == "/api/bg/output":
            return self._json(200, d.bg_output(self._qi("id", -1)))
        if path == "/api/tool_output":
            r = d.tool_output(unquote((self._q().get("path") or [""])[0]),
                              self._qi("offset"), self._qi("limit", BLOB_PAGE))
            return self._json(403 if r.get("error") else 200, r)
        return self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._guard():
            return
        body, d = self._body(), self.desk
        if path == "/api/send":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text 不能为空"})
            how = d.send(text)
            return self._json(202 if how == "queued" else 200,
                              {"ok": True, "queued": how == "queued"})
        if path == "/api/reply":
            ok = d.pending.answer(str(body.get("id") or ""), body.get("answer"))
            return self._json(200, {"ok": ok})      # 陈旧 id 不算错，前端重复点击是常事
        if path == "/api/compact":
            ok = d.compact_now()
            return self._json(200 if ok else 409,
                              {"ok": ok} if ok else {"error": "当前还有一轮在跑"})
        if path.startswith("/api/config/") or path in (
                "/api/prefs", "/api/thinking", "/api/skills/toggle", "/api/skills/run",
                "/api/mcp/toggle", "/api/mcp/timeout", "/api/mcp/save",
                "/api/mcp/delete", "/api/mcp/reload"):
            return self._settings(path, body, d)
        if path == "/api/interrupt":
            d.interrupt()
            return self._json(200, {"ok": True})
        if path == "/api/mode":
            ok = d.set_mode(str(body.get("mode") or ""))
            return self._json(200 if ok else 400, {"ok": ok, "mode": d.mode})
        if path == "/api/resume":
            ok = d.resume(str(body.get("id") or ""))
            return self._json(200 if ok else 409,
                              {"ok": ok} if ok else {"error": "会话不存在，或当前还有一轮在跑"})
        if path == "/api/plan/approve":
            ok = d.approve_plan(str(body.get("mode") or ""))
            return self._json(200 if ok else 409,
                              {"ok": ok} if ok else {"error": "当前还有一轮在跑"})
        if path == "/api/workspace":
            r = d.set_workspace(str(body.get("cwd") or ""))
            return self._json(409 if r.get("error") else 200, r)
        if path == "/api/workspace/pick":
            r = d.pick_workspace()
            return self._json(409 if r.get("error") else 200, r)
        if path == "/api/session/rename":
            r = d.rename_session(str(body.get("id") or ""), str(body.get("title") or ""))
            return self._json(400 if r.get("error") else 200, r)
        if path == "/api/session/archive":
            r = d.archive_session(str(body.get("id") or ""), bool(body.get("archived", True)))
            return self._json(400 if r.get("error") else 200, r)
        if path == "/api/session/fork":
            r = d.fork_session(str(body.get("id") or ""))
            return self._json(409 if r.get("error") else 200, r)
        if path == "/api/session/new":
            sid = d.new_session()
            return self._json(200 if sid else 409,
                              {"ok": bool(sid), "session": sid} if sid
                              else {"error": "当前还有一轮在跑"})
        if path == "/api/bg/kill":
            try:
                # note=True：另记一条"已被用户停止"便条给 agent 下次跑时注入。漏了它，
                # AI 上下文里那条"#N 运行中"永远悬着，还会白白去 check 一个幽灵任务。
                snap = d.agent._bg.kill(int(body.get("id")), note=True)
            except (TypeError, ValueError):
                return self._json(400, {"error": "id 不合法"})
            # kill 返回【终止前的输出快照】；任务不存在才返回 None。子 agent 任务正常成功
            # 也返回空串，所以判成败只能看 is not None，不能看真假值。
            return self._json(200, {"ok": snap is not None, "output": snap or ""})
        if path == "/api/task":
            tid = str(body.get("id") or "")
            if body.get("op") == "delete":
                return self._json(200, {"ok": d.agent._tasks.delete(tid)})
            fields = {k: body[k] for k in
                      ("status", "subject", "description", "active_form", "owner", "metadata")
                      if k in body}
            task, warns = d.agent._tasks.update(tid, **fields)
            return self._json(200 if task else 404,
                              {"ok": task is not None, "warnings": warns})
        return self._json(404, {"error": "no such endpoint"})

    def _settings(self, path: str, body: dict, d: Desk) -> None:
        """设置面板那一组接口。单拎出来是因为 do_POST 已经很长了，而这组的共同点很整齐：
        都返回 {ok} 或 {error}，都不涉及事件流，错误一律 400（唯独 reload 忙时是 409）。"""
        s = lambda k: str(body.get(k) or "").strip()      # noqa: E731

        if path == "/api/config/save":
            r = d.save_backend(s("base_url"), s("model"), s("api_key"), body.get("context_cap") or 0)
        elif path == "/api/config/switch":
            r = d.switch_backend_to(s("base_url"), s("model"))
        elif path == "/api/config/delete":
            r = d.delete_backend(s("base_url"), s("model"))
        elif path == "/api/config/test":
            return self._json(200, d.test_backend(s("base_url"), s("model"), s("api_key")))
        elif path == "/api/prefs":
            r = d.update_prefs(body.get("context_cap"), body.get("compact_threshold"))
        elif path == "/api/thinking":
            r = d.set_thinking(body.get("on"), s("effort"))
        elif path == "/api/skills/toggle":
            r = d.skill_toggle(s("name"), bool(body.get("enabled")))
        elif path == "/api/skills/run":
            r = d.skill_run(s("name"))
        elif path == "/api/mcp/toggle":
            set_server_enabled(s("name"), bool(body.get("enabled")))
            r = {"ok": True, "note": "重连或重启后生效"}
        elif path == "/api/mcp/timeout":
            try:
                secs = int(body.get("seconds") or 0)
            except (TypeError, ValueError):
                secs = 0
            if not 1 <= secs <= 600:
                return self._json(400, {"error": "握手超时要在 1~600 秒之间"})
            set_server_timeout(s("name"), secs)
            r = {"ok": True, "note": "重连或重启后生效"}
        elif path == "/api/mcp/save":
            args = body.get("args")
            r = d.mcp_save(s("name"), s("command"), args if isinstance(args, list) else [],
                           body.get("env") if isinstance(body.get("env"), dict) else {},
                           s("cwd"), s("scope"))
        elif path == "/api/mcp/delete":
            r = d.mcp_delete(s("name"), s("scope"))
        else:                                              # /api/mcp/reload
            r = d.mcp_reload()
            return self._json(409 if r.get("error") else 200, r)
        return self._json(400 if r.get("error") else 200, r)

    # ---- SSE ----

    def _events(self) -> None:
        q = self.desk.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")     # 万一前面有反代，别缓冲住流
        self.end_headers()
        try:
            self._sse({"type": "state", **self.desk.state()})
            # 侧栏三块也补一发首帧：轮询只在【变化时】推，不补的话新连上的前端要一直空着
            # 等到下一次真变化才有内容；任务清单更是永远等不到（见 push_tasks）。
            snap = self.desk._snapshot()
            self._sse({"type": "bg", "bg": snap["bg"], "now": time.time()})
            self._sse({"type": "workflow", "workflow": snap["workflow"]})
            self._sse({"type": "tasks", "tasks": self.desk.agent._tasks.summaries()})
            # 还没答复的请求重放：刷新页面后那张审批窗必须回来，否则工作线程还在等、
            # 用户却看不到任何可点的东西。
            for p in self.desk.pending.outstanding():
                self._sse(p)
            last = time.monotonic()
            while True:
                try:
                    first = q.get(timeout=POLL)
                except queue.Empty:
                    if time.monotonic() - last >= HEARTBEAT:
                        self._sse({"type": "ping"})     # 写失败=前端没了→抛异常，走 finally
                        last = time.monotonic()
                    continue
                for m in drain(q, first):
                    self._sse(m)
                last = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass          # 前端断开：不打断当轮（刷新不该杀任务），只收掉这条连接
        finally:
            self.desk.bus.unsubscribe(q)

    def _sse(self, msg: dict) -> None:
        self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---- 静态文件 ----

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        # 目录穿越闸：解析后必须仍在 WEB_DIR 之下（..%2f 之类在这里被挡住）
        if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
            return self._json(404, {"error": "not found"})
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.name == "index.html":
            # token 注入页面：同源策略保证别的站点读不到它，因此"能拿到 token"≈"是我们自己的页面"。
            # 放在 <head> 最前面，前端脚本一起跑就能读到。
            boot = f'<script>window.__MECODE__={{token:"{TOKEN}"}}</script>'.encode("utf-8")
            data = data.replace(b"<head>", b"<head>" + boot, 1)
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or ctype.endswith("javascript") else ctype)
        # 文件是每次请求现读的，但浏览器会按启发式规则自己缓存 .js/.css——
        # 改完代码刷新看不到新版本，只能 Ctrl+Shift+R。本机服务不值得为缓存省这点带宽。
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):      # 精简访问日志：一行一请求
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="mecode 桌面端服务")
    ap.add_argument("--port", type=int, default=0, help="0=让系统分配空闲端口（默认）")
    ap.add_argument("--host", default="127.0.0.1", help="只监听回环，别改成 0.0.0.0")
    ap.add_argument("--cwd", default=".", help="工作区目录（决定会话归属、权限根）")
    ap.add_argument("--mode", default="auto", choices=[m for m in MODES if m != "plan"])
    ap.add_argument("--no-mcp", action="store_true")
    ap.add_argument("--open", action="store_true", help="就绪后自动打开浏览器")
    a = ap.parse_args(argv)

    Handler.desk = Desk(Path(a.cwd).resolve(), a.mode, mcp=not a.no_mcp)
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.daemon_threads = True                  # 停机时不等 SSE 长连接自己断
    url = f"http://{a.host}:{httpd.server_address[1]}"
    # 就绪行带 token：桌面壳解析它来开窗，手动开浏览器也直接粘这个整串地址。
    print(f"deskserve: {url}/?token={TOKEN}", flush=True)
    if a.open:
        webbrowser.open(f"{url}/?token={TOKEN}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止", flush=True)
    finally:
        httpd.server_close()
        Handler.desk.close()


if __name__ == "__main__":
    main()
