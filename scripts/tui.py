"""mecode 的 TUI（textual）。CLI(chat.py) 留作调试；这里是给人用的界面。

架构：agent.run_turn 是同步生成器，跑在 textual 的 worker 线程里，每个事件
post_message 给 UI 线程更新 widget——阻塞活（模型流/工具执行）在后台，界面始终跟手。
这正是“agent 只产出事件、显示完全分离”的红利：TUI 不过是换一个事件消费者。

显示就一个滚动区 #log（VerticalScroll，挂真 widget）：流式正文直接挂一个活 Static 进去逐片更新
（纯文本，避免半截 markdown），和上面的历史/工具调用连续不断层；本轮某段落定（要调工具 / 本轮结束）
时把它换成 Markdown 渲染、思考收成一行折叠的 “💭 思考”。不另设 #live 区（那样会和历史断层、留空隙）。
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

# 全局 mecode 命令会从任意工作目录启动；config 在导入时就读环境变量，而 load_dotenv() 默认只从
# 当前目录找 .env。这里用绝对路径先加载项目自带的 .env，保证从哪启动都连得上后端（当前目录另有
# .env 时，config 自己的 load_dotenv(override=True) 会再覆盖，支持按项目改后端）。
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=True)
except ImportError:
    pass

import rich.cells as _rich_cells                       # noqa: E402
from rich.cells import cell_len                         # noqa: E402
from rich.style import Style                            # noqa: E402
from rich.text import Text                              # noqa: E402
from textual import events, on, work                    # noqa: E402
from textual.app import App, ComposeResult              # noqa: E402
from textual.binding import Binding                     # noqa: E402
from textual.color import Color                          # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.geometry import Size                       # noqa: E402
from textual.message import Message                     # noqa: E402
from textual.screen import ModalScreen                  # noqa: E402
from textual.scroll_view import ScrollView             # noqa: E402
from textual.strip import Strip                         # noqa: E402
from textual.widgets import Input, ListItem, ListView, Markdown, Static, TextArea  # noqa: E402

from mecode.agent import Agent                           # noqa: E402
from mecode.compact import estimate_tokens               # noqa: E402
from mecode.config import (                              # noqa: E402
    _CONTEXT_CAP_DEFAULT, agent_config, current_agent_config, current_backend,
    delete_saved, env_backend, load_user_config, migrate_context_cap, save_user_config,
    saved_configs, update_settings,
)
from mecode.events import (                              # noqa: E402
    Notice, PlanProposed, ReasoningDelta, TextDelta, ToolResult, ToolStarted, Usage,
)
from mecode.mcp import (                                 # noqa: E402
    connect_servers, cycle_server_timeout, default_config_paths, load_mcp_config,
    server_states, set_server_enabled,
)
from mecode.memory import build_memory_prompt, memory_tools   # noqa: E402
from mecode.mode import CYCLE, DEFAULT_MODE, MODES, apply_mode, next_mode   # noqa: E402
from mecode.permission import PermissionPolicy, pattern_for   # noqa: E402
from mecode.provider import Provider, ProviderError      # noqa: E402
from mecode.registry import PROVIDERS, context_window_for   # noqa: E402
from mecode.session import SessionStore                  # noqa: E402
from mecode.skills import discover_skills, set_enabled, skill_reminder   # noqa: E402
from mecode.system_prompt import build_system_prompt     # noqa: E402
from mecode.tools import default_registry, make_ask_user_tool   # noqa: E402


def _is_error_result(result: str) -> bool:
    # 只看“是不是错误信息”，不在内容里乱搜——否则 read_file 读到含 "错误：" / "(exit code:" 的文件
    # （比如 tools.py 自己）会被误判。真错误信息在开头；bash 非零退出码在末行。
    head = result.lstrip()
    if head.startswith("错误：") or (head.startswith("工具 ") and "执行出错：" in head[:80]):
        return True
    return result.rstrip().rsplit("\n", 1)[-1].startswith("(exit code:")


def _clip(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# Emoji 的 VS16（例如 ⚠️ = U+26A0 U+FE0F）在不同终端里可能实际推进 1 或 2 格。
# Rich 按 Unicode 规范默认算 2；若终端实际只推进 1，同行右侧的滚动条/分割线就会整体左漂一格。
# 保留字面 Emoji（不能删 VS16，否则会退成黑白文本符号），只在启动探测出“终端按 1 格画”时
# 改 Rich 的计宽规则。Textual 各模块引用的是 rich.cells.cached_cell_len；清同一个缓存即可全局生效。
_RICH_CELL_LEN = _rich_cells._cell_len
_vs16_cell_width = 2


def _set_vs16_cell_width(width: int) -> None:
    """让 Rich/Textual 对 VS16 emoji 的计宽与当前终端一致；不修改实际显示字符。"""
    global _vs16_cell_width
    if width not in (1, 2):
        raise ValueError("VS16 emoji width must be 1 or 2")
    _vs16_cell_width = width
    if width == 1:
        # 终端忽略 VS16 的“窄变宽”语义时，Rich 也按去掉 VS16 后的基字符宽度计算。
        # 宽基字符（🚀 等）本来就是 2 格，不受影响；⚠️/❤️ 这类则从 2 改为 1。
        _rich_cells._cell_len = lambda text, unicode_version: _RICH_CELL_LEN(
            text.replace("\ufe0f", ""), unicode_version
        )
    else:
        _rich_cells._cell_len = _RICH_CELL_LEN
    _rich_cells.cached_cell_len.cache_clear()


# textual 7.4.0 的 Markdown：无语言代码块用 highlight() 做“通用高亮”，会把树形字符 │├└─ 误判成 error
# token → 渲成红字+暗红底（用户看到“树形加了红色背景”）。这里让【无语言】代码块渲纯文本、绕开误判；
# 有语言的（```python 等）照常高亮。try/except 兜底：textual 内部结构变了就跳过、顶多恢复成红树不崩。
try:
    from textual.content import Content as _Content            # noqa: E402
    from textual.widgets._markdown import MarkdownFence as _MDFence   # noqa: E402
    _orig_fence_highlight = _MDFence.highlight.__func__

    def _fence_highlight_plain_when_unlabeled(cls, code, language):
        return _Content(code) if not language else _orig_fence_highlight(cls, code, language)

    _MDFence.highlight = classmethod(_fence_highlight_plain_when_unlabeled)
except Exception:
    pass


def _md(text: str) -> Markdown:
    """markdown 文本 → textual 原生 Markdown widget（主题色/代码高亮，渲染更贴 TUI）。
    它异步多遍渲染、高度逐帧增长，但 #log 是滚动锚定的——只要 watch_scroll_y 正确调 super
    （基类在那里更新滚动条 + 触发重画 + 维护 anchor），内容长高时视图会被锚在底部，不会出问题。"""
    return Markdown(text)


def _fmt_time(ts) -> str:
    return time.strftime("%Y年%m月%d日 %H:%M", time.localtime(ts)) if ts else "未知时间"


def _summarize_tool(name: str, args: dict) -> str:
    """把工具参数压成一行摘要：bash 显命令、read 显文件+行段、写/改显路径、grep/glob 显模式。"""
    if not isinstance(args, dict):
        return ""
    if name == "bash":
        return args.get("command", "")
    if name == "read_file":
        p, off, lim = args.get("path", ""), args.get("offset"), args.get("limit")
        return f"{p} [{off or 0}:{(off or 0) + (lim or 0)}]" if (off or lim) else p
    if name in ("write_file", "edit_file"):
        return args.get("path", "")
    if name == "grep":
        p = args.get("path")
        return f'"{args.get("pattern", "")}"' + (f" in {p}" if p else "")
    if name == "glob":
        p = args.get("path")
        # 也显示搜索路径：权限判定的是 path（见 permission._subject），只显示 pattern 会让人误以为 gate 在 pattern 上。
        return args.get("pattern", "") + (f" in {p}" if p else "")
    if name == "subagent":                       # 子 agent：标题显示短标签 description（无则退回 prompt；截断由外层 _clip 处理）
        return (args.get("description") or args.get("prompt", "")).replace("\n", " ")
    if name == "ask_user":                       # 提问：标题显示首题（多题标条数）
        # 渲染发生在工具校验之前（ToolStarted 先于 execute），模型吐畸形形状时这里必须自防——
        # 异常会落在 UI 线程的消息 handler 里，直接崩整个 app。畸形就落回下方通用 key=value 展示。
        qs = args.get("questions")
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            first = str(qs[0].get("question") or "").replace("\n", " ")
            return (f"{len(qs)} 题：" if len(qs) > 1 else "") + first
    return " ".join(f"{k}={str(v).replace(chr(10), ' ')}" for k, v in args.items())


def _tool_input_text(name: str, args: dict) -> str | None:
    """工具块展开后【输入框】的内容——每个工具都能点开看完整参数（标题行只有 64 字符截断摘要，
    edit 的 old/new、grep 的长正则在那里根本看不全）。按工具定制最可读的形式：
    - bash → $ 完整命令；subagent → 完整 prompt；run_workflow → 铺开的阶段结构
    - edit_file/write_file → None【不给输入框】：结果框本就用参数渲染完整 diff，再给一遍是重复
    - 其余（read/grep/glob/web/mcp 等）→ 逐参数一行 'key: value'（多行值缩进原样保留）"""
    if not isinstance(args, dict):
        return None
    if name == "bash":
        return f"$ {args.get('command', '')}"
    if name == "subagent":
        return f"任务: {args.get('prompt', '')}"
    if name == "run_workflow":
        return _workflow_stages_text(args)
    if name == "ask_user":
        return _ask_questions_text(args)
    if name in ("edit_file", "write_file"):
        return None
    if not args:
        return None                     # 无参工具（exit_plan 等）：没内容就不给空框
    lines = []
    for k, v in args.items():
        s = str(v)
        if "\n" in s:                   # 多行值（长正则/内容）：换行缩进原样铺，别压成一行
            body = "\n".join("    " + ln for ln in s.splitlines())
            lines.append(f"{k}:\n{body}")
        else:
            lines.append(f"{k}: {s}")
    return "\n".join(lines)


def _workflow_stages_text(args: dict) -> str:
    """run_workflow 的输入框内容：把 stages 结构铺成人读的多行（id/依赖/描述 + 完整 prompt），
    比收起行的截断 JSON 有用得多——审批/回看时能看清模型编排了什么。"""
    stages = args.get("stages")
    if not isinstance(stages, list):
        return str(args)
    lines = [f"workflow · {len(stages)} 个阶段"]
    for s in stages:
        if not isinstance(s, dict):
            continue
        dep = f"（依赖 {', '.join(s.get('after') or [])}）" if s.get("after") else "（并行起点）"
        lines.append(f"\n[{s.get('id', '?')}] {dep} {s.get('description', '')}")
        lines.append(f"  {s.get('prompt', '')}")
    return "\n".join(lines)


def _ask_questions_text(args: dict) -> str:
    """ask_user 输入框内容：把每题的问题与选项铺成人读的多行（回看时能看清模型问了什么）。"""
    qs = args.get("questions")
    if not isinstance(qs, list):
        return str(args)
    lines = []
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            continue
        tag = "（多选）" if q.get("multi_select") else ""
        lines.append(f"{i}. {str(q.get('question') or '')}{tag}")
        opts = q.get("options")
        for o in opts if isinstance(opts, list) else []:   # 同为校验前渲染，标量 options 不能炸
            if isinstance(o, dict):
                d = o.get("description") or ""
                lines.append(f"   - {o.get('label', '')}" + (f"：{d}" if d else ""))
    return "\n".join(lines)


def _result_preview(content: str) -> str:
    """历史工具结果的一行预览（resume 重渲用）：一般取首行；用户拒绝的结果只取"错误：用户拒绝执行工具 X"
    那一句——后面给模型看的"不要重试…"长指令留在 transcript 里（发给模型的内容不变），只是不铺到界面上。"""
    first = (content or "").splitlines()[0] if content else ""
    if first.startswith("错误：用户拒绝执行工具"):
        return first.split("。", 1)[0]
    return first


def _read_range(result: str) -> str:
    """从 read_file 结果（每行 "行号\\t内容"）里取实际读到的首行—末行号，如 "1-37 行"。"""
    nums = [int(h) for line in result.splitlines()
            if (h := line.split("\t", 1)[0].strip()).isdigit()]
    return f"{nums[0]}-{nums[-1]} 行" if nums else ""


# 同类工具批折叠：把【连续同类】的工具调用折成一行摘要。分组键（class）如下；非折叠类返回 None，
# 让它维持现状逐行显示、不进任何批（memory/后台工具少且一次性，折叠没意义反而丢信息）。
_TOOL_CLASS = {
    "read_file": "explore", "grep": "explore", "glob": "explore",
    "bash": "bash",
    "write_file": "edit", "edit_file": "edit",
    "subagent": "subagent",
    "task_create": "task", "task_update": "task", "task_get": "task", "task_list": "task",
}


def _tool_class(name: str) -> str | None:
    """工具名 → 折叠分组键（explore/bash/edit/subagent/task）；非折叠类返回 None。"""
    return _TOOL_CLASS.get(name)


def _batch_summary(cls: str, calls: list[tuple[str, dict]]) -> tuple[str, str]:
    """算一个批的折叠行文案：返回 (图标, 明细文本)。calls 是该批 (工具名, args) 列表（按调用序）。
    纯逻辑（不碰 widget）便于单测；args 非 dict 的按空 dict 处理。图标黑白，收起态用。"""
    calls = [(n, a if isinstance(a, dict) else {}) for n, a in calls]
    if cls == "explore":
        # F=去重后 read_file 的 path 数；D=去重后 grep/glob 的搜索路径数（grep/glob 无 path 视为 "."）
        files = {a.get("path", "") for n, a in calls if n == "read_file"}
        files.discard("")
        dirs = {a.get("path") or "." for n, a in calls if n in ("grep", "glob")}
        f, d = len(files), len(dirs)
        if f == 1 and d == 0:                       # 只探了一个文件 → 直接显示文件名
            return "⌕", "探索 · " + _clip(Path(next(iter(files))).name, 64)
        segs = []
        if f:
            segs.append(f"{f} 个文件")
        if d:
            segs.append(f"{d} 个文件夹")
        return "⌕", "探索 · " + ", ".join(segs)
    if cls == "bash":
        n = len(calls)
        if n == 1:                                  # 单条 → 直接显示那条命令（超长截断，别撑成多行）
            return "$", "命令 · " + _clip(calls[0][1].get("command", ""), 64)
        return "$", f"命令 · {n} 条"
    if cls == "edit":
        paths = {a.get("path", "") for _, a in calls}
        paths.discard("")
        m, n = len(paths), len(calls)
        if m == 1:
            txt = "编辑 · " + _clip(Path(next(iter(paths))).name, 64)
            return "✎", txt + (f" · {n} 处" if n > 1 else "")
        txt = f"编辑 · {m} 个文件"
        return "✎", txt + (f" · {n} 处" if n > m else "")
    if cls == "subagent":
        n = len(calls)
        if n == 1:                                  # 单个 → 显示其 description（无则退回 prompt；超长截断）
            desc = _clip(calls[0][1].get("description") or calls[0][1].get("prompt", ""), 64)
            return "◇", "启用子 agent · " + desc
        return "◇", f"启用子 agent · {n} 个"
    if cls == "task":
        return "≡", f"更新任务清单 · {len(calls)} 次"
    return "▶", ""      # 不应到这（非折叠类不入批）；兜底


class AgentEvent(Message):
    """把 agent 的一个事件从 worker 线程投递给 UI 线程（ev=None 表示本轮结束）。"""
    def __init__(self, ev) -> None:
        self.ev = ev
        super().__init__()


class AskPermission(Message):
    """worker 线程请求 UI 弹审批弹窗（工具要执行 ask 类动作）。UI 答复经 threading.Event 回传 worker。
    is_sub=是子 agent 在问（标题用）；can_stop=这条分支可被单独停 → 多给一项"停止此后台任务"。"""
    def __init__(self, tool: str, args: dict, is_sub: bool = False, can_stop: bool = False,
                 seq: int = 0) -> None:
        self.tool, self.args, self.is_sub, self.can_stop = tool, args, is_sub, can_stop
        self.seq = seq            # 请求序号：迟到的答复据此丢弃（见 App._on_ask_permission.done）
        super().__init__()


class DismissPermission(Message):
    """worker 被打断、不再等这张审批窗了 → 请 UI 收掉它。
    不收的话它留在屏上成"幽灵窗"：用户对着它按的那一下会被【下一次】审批当成答复（含"总是允许"落盘）。"""
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__()


class AskQuestion(Message):
    """worker 线程请求 UI 弹提问弹窗（ask_user 工具）。答复经 threading.Event 回传 worker。
    seq=请求序号，用途同 AskPermission（迟到答复丢弃 / 收窗认这张窗自己的号）。"""
    def __init__(self, questions: list[dict], seq: int = 0) -> None:
        self.questions, self.seq = questions, seq
        super().__init__()


class DismissQuestion(Message):
    """worker 被打断、不再等这张提问窗了 → 请 UI 收掉它（同 DismissPermission）。"""
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__()


class BgComplete(Message):
    """后台任务【自然完成】时，守护线程经 on_complete 投给 UI 的唤醒信号（空闲则起一轮自动接续）。
    带上 task：自动接续不用它，但超时要据 status 闪一下提示。"""
    def __init__(self, task) -> None:
        self.task = task
        super().__init__()


class TasksChanged(Message):
    """任务清单变更（create/update/delete）时，由 TaskManager.on_change 投给 UI → 刷侧栏 TodoPanel。
    on_change 可能跑在 worker 线程（工具执行），post_message 线程安全；不带数据，刷新时直接读 summaries()。"""


def _truncate(s: str, width: int) -> str:
    """按显示宽度截断、尾部加 …（工具名超出侧栏宽度时用，避免撑宽面板）。"""
    if width <= 1 or cell_len(s) <= width:
        return s
    out = ""
    for ch in s:
        if cell_len(out) + cell_len(ch) + 1 > width:   # +1 给省略号留位
            break
        out += ch
    return out + "…"


class ToolsPanel(Static):
    """本地工具列表：一行一个、默认只显示 4 个；多出的点击展开/收起（溢出由 #sidebar 滚动）。"""
    ALLOW_SELECT = False   # 它是“按钮”，关掉文字选择，否则点击会被当成选中手势而高亮
    COLLAPSED = 4          # 收起时显示几个

    def __init__(self, tools: list[str], **kw) -> None:
        super().__init__(**kw)
        self._tools = tools
        self._expanded = False

    def on_mount(self) -> None:
        if len(self._tools) > self.COLLAPSED:   # 可展开/收起时才标记为可点击（用于 hover 高亮）
            self.add_class("-clickable")
        self._render_tools()

    def on_click(self) -> None:
        if len(self._tools) > self.COLLAPSED:   # 仅在有隐藏项时才需要切换
            self._expanded = not self._expanded
            self._render_tools()

    def set_tools(self, tools: list[str]) -> None:   # MCP 后台连好后刷新（本地工具）
        self._tools = tools
        self.set_class(len(tools) > self.COLLAPSED, "-clickable")
        self._render_tools()

    def _render_tools(self) -> None:       # 不能叫 _render：会覆盖 textual Widget 的内部同名方法
        avail = getattr(self.app, "_panel_text_width", 25) - 2   # 文本可用宽（已扣滚动条）- 左缩进 2
        shown = self._tools if self._expanded else self._tools[:self.COLLAPSED]
        hidden = len(self._tools) - len(shown)

        t = Text()
        t.append(f"工具 ({len(self._tools)})", style="bold")
        if hidden:
            t.append("  ▶ 查看更多", style="yellow")
        elif self._expanded and len(self._tools) > self.COLLAPSED:
            t.append("  ▼ 收起", style="yellow")
        t.append("\n")
        for n in shown:                                     # 一行一个，长名截断
            t.append("  " + _truncate(n, avail) + "\n", style="dim")
        self.update(t)


class ServerPanel(Static):
    """一个 MCP server 的可展开行：收起=“name · N 工具 ▶”，展开=▼ + 缩进列出工具名（去掉 server 前缀）。
    溢出由 #sidebar 滚动。"""
    ALLOW_SELECT = False

    def __init__(self, name: str, tools: list[str], **kw) -> None:
        super().__init__(**kw)
        self._name = name
        self._tools = tools             # 原始工具名（不带 '<server>__' 前缀）
        self._expanded = False

    def on_mount(self) -> None:
        self.add_class("-clickable")
        self._render_server()

    def on_click(self) -> None:
        self._expanded = not self._expanded
        self._render_server()

    def _render_server(self) -> None:
        avail = getattr(self.app, "_panel_text_width", 25) - 4   # 文本可用宽（已扣滚动条）- 缩进 4
        t = Text()
        t.append(f"  {self._name} · {len(self._tools)} 工具", style="green")
        t.append(f"  {'▼' if self._expanded else '▶'}\n", style="yellow")
        if self._expanded:
            for n in self._tools:
                t.append("    " + _truncate(n, avail) + "\n", style="dim")
        self.update(t)


class SkillPanel(Static):
    """侧栏技能区：显示【本次会话加载】的技能（= 会话创建时进 system prompt 索引的那批，启停改动
    新会话才生效，故本面板在会话内不变）。交互同 ToolsPanel + MCP 区：默认只显示 4 个、超出点整块展开/收起；
    "管理"两字是独立链接（点它开 /skill，不触发展开）。【一个技能都没启用时也常驻】：留个"管理"入口方便点开启用。"""
    ALLOW_SELECT = False
    COLLAPSED = 4          # 收起时显示几个（同 ToolsPanel）
    # "管理" 链接样式：同 MCP 区（黄色、无下划线、meta @click 走 App.action_open_skills）
    _MANAGE = Style(color="yellow", underline=False, meta={"@click": "app.open_skills"})

    def __init__(self, names: list[str], **kw) -> None:
        super().__init__(**kw)
        self._names = names
        self._expanded = False

    def on_mount(self) -> None:
        self._render_skills()

    def on_click(self, event) -> None:
        # 点在"管理"链接上 → 交给它的 @click action（开 /skill），不切换展开；点别处才折叠/展开。
        if getattr(event.style, "meta", None) and event.style.meta.get("@click"):
            return
        if len(self._names) > self.COLLAPSED:
            self._expanded = not self._expanded
            self._render_skills()

    def set_skills(self, names: list[str]) -> None:   # resume 重建会话后刷新
        self._names = names
        self._render_skills()

    def _render_skills(self) -> None:
        avail = getattr(self.app, "_panel_text_width", 25) - 2
        n = len(self._names)
        self.set_class(n > self.COLLAPSED, "-clickable")     # 有隐藏项才整块可点（hover 高亮）
        shown = self._names if self._expanded else self._names[:self.COLLAPSED]
        hidden = n - len(shown)

        t = Text()
        t.append(f"技能 ({n})" if n else "技能", style="bold")
        t.append("  管理", style=self._MANAGE)               # 独立可点：开 /skill 管理
        if hidden:
            t.append("  ▶ 查看更多", style="yellow")
        elif self._expanded and n > self.COLLAPSED:
            t.append("  ▼ 收起", style="yellow")
        t.append("\n")
        if n:
            for nm in shown:
                t.append("  " + _truncate(nm, avail) + "\n", style="dim")
        else:                                                # 无启用技能：留"未启用"占位，管理入口仍在
            t.append("  未启用", style="dim")
        self.update(t)


class TodoPanel(Static):
    """侧栏任务清单：读 agent._tasks 渲染当前任务（☐pending / ▶in_progress / ✓completed），随任务变更刷新。
    空清单时整块隐藏、不占位。非交互、只展示进度——in_progress 显示 active_form（窄栏只放一个，故用进行时
    而非祈使句标题，这正是 active_form 存在的理由）；被依赖挡住的标 🔒。完整描述/依赖明细走 AI 的 task_get。
    【全部完成后可点击收起】：完成态留着有信息量但不该永占侧栏——收起记住当时的清单签名，
    清单再变（新任务/状态变）自动重新出现。"""
    ALLOW_SELECT = False
    _ICON = {"pending": "☐", "in_progress": "▶", "completed": "✓"}
    _STYLE = {"pending": "", "in_progress": "bold yellow", "completed": "dim"}

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._dismissed_sig = None          # 点击收起时的清单签名；签名变了自动解除收起

    @staticmethod
    def _sig(rows: list) -> tuple:
        return tuple((r["id"], r["status"]) for r in rows)

    def on_click(self) -> None:
        rows = self.app.agent._tasks.summaries()
        if rows and all(r["status"] == "completed" for r in rows):   # 只有全完成才可收（跑一半别误触消失）
            self._dismissed_sig = self._sig(rows)
            self.display = False

    def refresh_tasks(self) -> None:        # _refresh_todos 推送刷新 / 定宽变化时调
        rows = self.app.agent._tasks.summaries()
        sig = self._sig(rows)
        if self._dismissed_sig is not None and sig != self._dismissed_sig:
            self._dismissed_sig = None      # 清单变了（新任务/状态变）→ 解除收起、重新出现
        self.display = bool(rows) and sig != self._dismissed_sig
        if not self.display:
            return
        avail = getattr(self.app, "_panel_text_width", 25) - 2   # 文本可用宽（已扣滚动条）- 左缩进 2
        done = sum(1 for r in rows if r["status"] == "completed")
        all_done = done == len(rows)
        self.set_class(all_done, "-clickable")                    # 全完成 → hover 高亮提示可收
        t = Text()
        t.append(f"任务清单 ({done}/{len(rows)})", style="bold")
        if all_done:
            t.append("  点击收起", style="yellow")
        t.append("\n")
        for r in rows:
            label = (r["active_form"] if r["status"] == "in_progress" and r["active_form"]
                     else r["subject"])
            line = f"{self._ICON.get(r['status'], '☐')} #{r['id']} {label}"
            # if r["open_blockers"]:          # 依赖功能已停用（见 tasks.py 模块注释"依赖"段）
            #     line += " 🔒"               # 还有未完成的依赖挡着
            t.append("  " + _truncate(line, avail) + "\n", style=self._STYLE.get(r["status"], ""))
        self.update(t)


class WorkflowPanel(Static):
    """侧栏 workflow 分支树：run_workflow 执行期间轮询 agent._workflow 画各阶段状态
    （○等待 / ▶运行中 / ✓完成 / ✗失败，缩进体现依赖层级）。空闲（无 stages）整块隐藏。
    随 App 现有的 0.5s 定时器刷新（_sync_bgtasks 顺带调），不另起 timer。
    【跑完后可点击收起】（同 TodoPanel）：完成树留着能看各阶段终态，但不该永占侧栏——
    收起记住这批 stages 的身份，新 workflow 覆盖 stages 后自动重新出现。"""
    ALLOW_SELECT = False
    _ICON = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}
    _STYLE = {"pending": "dim", "running": "bold yellow", "done": "green", "failed": "red"}

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._dismissed = None              # 收起时的 stages 列表身份（id()）；stages 被新 workflow 整表替换后自动解除

    def on_click(self) -> None:
        run = self.app.agent._workflow
        if run.stages and not run.active:   # 只有跑完才可收（运行中别误触消失）
            self._dismissed = id(run.stages)
            self.display = False

    def refresh_workflow(self) -> None:
        run = self.app.agent._workflow
        if self._dismissed is not None and id(run.stages) != self._dismissed:
            self._dismissed = None          # 新 workflow（stages 整表替换）→ 解除收起
        show = (bool(run.stages) and id(run.stages) != self._dismissed
                and (run.active or any(s.status != "pending" for s in run.stages)))
        self.display = show
        if not show:
            return
        avail = getattr(self.app, "_panel_text_width", 25) - 2
        done = sum(1 for s in run.stages if s.status in ("done", "failed"))
        finished = not run.active
        self.set_class(finished, "-clickable")                    # 跑完 → hover 高亮提示可收
        # 依赖深度做缩进：无依赖=0 层，依赖的最大深度+1（图不深，简单递推够用）
        depth: dict = {}
        for s in run.stages:                       # stages 本身近似拓扑序（模型按依赖顺序写）
            depth[s.id] = 1 + max((depth.get(d, 0) for d in s.after), default=-1)
        t = Text()
        t.append(f"Workflow ({done}/{len(run.stages)})", style="bold")
        if finished:
            t.append("  点击收起", style="yellow")
        t.append("\n")
        for s in run.stages:
            pad = "  " * (1 + depth.get(s.id, 0))
            label = s.description or s.id
            t.append(pad + _truncate(f"{self._ICON[s.status]} {label}", avail) + "\n",
                     style=self._STYLE[s.status])
        self.update(t)


class Splitter(Static):
    """#main 与 #sidebar 之间的可拖动竖条：按住左右拖即改侧栏宽度。
    终端无法改鼠标指针形状（没法显示"拖动"光标），故用 hover 高亮 + 拖动实时变宽作反馈。"""
    ALLOW_SELECT = False

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._drag = False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._drag = True
        self.capture_mouse()             # 拖动期间所有鼠标事件都进来，移出竖条也不丢
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._drag:
            return
        # 拖动中按键本应仍按着（button≠0）。若 button=0 说明 mouse_up 丢了（松键在窗外/被吞）——
        # 不收尾就会一直 capture 住、鼠标全卡死。被 capture 时所有移动都路由到这里，故下一动即自愈。
        if not event.button:
            self._end_drag()
            return
        self.app._apply_panel_width(self.app.size.width - event.screen_x)   # 侧栏在右：宽 = 总宽 - 鼠标列
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._end_drag()
        event.stop()

    def _end_drag(self) -> None:
        self._drag = False
        if self.app.mouse_captured is self:   # 只在确实持有时释放，幂等、不误放别人的 capture
            self.release_mouse()


def _fmt_elapsed(s: float) -> str:
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60}s"


class _BgStop(Static):
    """后台任务行右侧的“✕停止”按钮：点击杀该任务。event.stop() 防止冒泡到 BgTaskRow（那会触发展开）。"""
    ALLOW_SELECT = False

    def __init__(self, task, **kw) -> None:
        super().__init__("✕ 停止", **kw)
        self._bgtask = task

    def on_mount(self) -> None:
        self.add_class("-clickable")

    def on_click(self, event) -> None:
        event.stop()
        # note=True：用户手动停 → 留一条便条，AI 下次跑时知道是被你停的（不自动起轮）。下个 _sync 摘掉本块。
        tid = self._bgtask.id
        if self.app.agent._bg.kill(tid, note=True) is None:
            return                                   # 已经结束了（正好抢在这一下之前）→ 别报假回执
        self.app.notice_killed(tid)


class BgTaskRow(Vertical):
    """输入框上方“运行中后台任务”的一块：头行（▶/▼ 命令〔按实际宽度截断、省略号不再过早〕 ＋ 已 Ns ＋
    红色 ✕停止 按钮）＋ 可展开输出区（展开时第二行先给【完整命令】，再给最近 8 行输出，0.5s 跟刷；
    全文靠 AI 的 check-in）。点头行 → 展开/收起；点 ✕停止 → 杀任务。"""
    ALLOW_SELECT = False

    def __init__(self, task, **kw) -> None:
        super().__init__(**kw)
        self._bgtask = task                           # 不叫 _task：撞 textual Widget 内部的 _task(消息泵 asyncio Task)
        self._expanded = False
        self._label = Static(classes="bg-label")      # ▶ + 命令（width 1fr，按实际可用宽截断）
        self._meta = Static(classes="bg-meta")        # · 已 Ns（右侧定宽、始终可见，不被长命令挤掉）
        self._out = Static(classes="bg-out")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bg-headrow"):
            yield self._label
            yield self._meta
            yield _BgStop(self._bgtask, classes="bg-stop")
        yield self._out

    def on_mount(self) -> None:
        self._label.add_class("-clickable")
        self._meta.add_class("-clickable")
        self._out.display = False
        self.refresh_row()

    def refresh_row(self) -> None:                    # _sync_bgtasks 每 0.5s 调：刷已运行时长 +（展开时）刷输出
        is_sub = getattr(self._bgtask, "is_subagent", False)
        prefix = "subagent: " if is_sub else ""       # 后台子 agent 和 bash 混在一起时能区分
        # 子 agent 收起行给短标签 description（无则退回 prompt=command）；bash 给命令。展开才给完整 prompt。
        label_text = (getattr(self._bgtask, "description", "") or self._bgtask.command) if is_sub else self._bgtask.command
        avail = self._label.size.width or 60          # 按【实际可用宽】截断（布局后有效；首帧回退 60）→ 不再固定 50 过早省略
        head = Text()
        head.append(f"{'▼' if self._expanded else '▶'} ", style="yellow")
        head.append(prefix + _truncate(label_text, max(8, avail - 2 - len(prefix))),
                    style="green" if is_sub else "cyan")   # 子 agent 用绿（运行中/非报错）区分 bash 的青；品红太像报错红
        self._label.update(head)
        self._meta.update(Text(f"· 已 {_fmt_elapsed(self._bgtask.elapsed())}", style="dim"))
        if self._expanded:
            snap = self.app.agent._bg.check_output(self._bgtask.id) or ""
            tail = "\n".join(snap.splitlines()[-8:]) or "（暂无输出）"
            body = Text()
            label = "任务: " if is_sub else "$ "       # 第二行起：子 agent=任务 / bash=命令
            body.append(f"{label}{self._bgtask.command}\n", style="cyan")
            body.append(tail, style="dim")
            self._out.update(body)

    def on_click(self) -> None:                       # 点头行 → 展开/收起；✕停止 已 event.stop() 不会到这
        self._expanded = not self._expanded
        self._out.display = self._expanded
        self.refresh_row()


class _PlanSwitch(Static):
    """计划审批条上的 ⇆：点击循环切"批准后以哪个模式执行"（普通/自动/YOLO）。"""
    ALLOW_SELECT = False          # 连点不触发双击选中整行
    def on_click(self) -> None:
        self.app._cycle_plan_target()


class _PlanApprove(Static):
    """计划审批条上的 [批准执行]：点击 → 切到目标执行模式、按计划开跑。"""
    ALLOW_SELECT = False
    def on_click(self) -> None:
        self.app._approve_plan()


class _PlanView(Static):
    """计划审批条上的"查看计划"：点击 → 弹出计划详情（Markdown，Esc/✕ 关）。计划不铺进对话，按需看。"""
    ALLOW_SELECT = False
    def on_click(self) -> None:
        self.app._view_plan()


class PlanBar(Horizontal):
    """输入框上方的"计划待批准"条：批准后以【目标执行模式】执行；⇆ 切目标；查看计划 看详情；[批准执行] 批准。
    要改计划：直接在输入框打修改意见发出去（发出即起新一轮、这条随之消失）。不用模态弹窗→不抢阅读焦点。"""
    ALLOW_SELECT = False

    def compose(self) -> ComposeResult:
        yield Static("📋 计划待批准 · 批准后以", classes="plan-lead")
        yield _PlanSwitch(id="plan-switch")               # 【模式】⇆ 整块：可点循环切目标 + 整块 hover，refresh_target 刷内容
        yield Static(" 模式执行 · ", classes="plan-lead")
        yield _PlanView("查看计划", id="plan-view")        # 点开弹窗看计划详情
        yield Static(classes="plan-gap")                  # width 1fr 撑开 → 批准按钮顶到右缘
        yield _PlanApprove("批准执行", id="plan-approve")

    def on_mount(self) -> None:
        self.refresh_target()

    def refresh_target(self) -> None:
        m = self.app._plan_target or DEFAULT_MODE
        color = MODE_COLORS.get(m, "cyan")
        self.query_one("#plan-switch", _PlanSwitch).update(
            Text.assemble((f"【{MODES[m].label}】", f"bold {color}"), ("⇆", "dim")))   # 模式彩色 + 紧贴的暗色 ⇆，整块可点


class _PlanClose(Static):
    """查看计划弹窗右上角的 ✕：点击关闭弹窗。"""
    ALLOW_SELECT = False
    def on_click(self) -> None:
        self.screen.dismiss()


class _PlanModalApprove(Static):
    """查看计划弹窗里的 [批准执行]：dismiss("approve") → App 回调走批准流程（切模式 + 执行）。"""
    ALLOW_SELECT = False
    def on_click(self) -> None:
        self.screen.dismiss("approve")


class PlanViewModal(ModalScreen):
    """查看计划弹窗：Markdown 渲染计划正文；右上角 [批准执行] 直接批准、✕/Esc 关闭。计划不铺进对话，点"查看计划"才弹这个。"""
    BINDINGS = [Binding("escape", "close", "关闭")]
    CSS = """
    PlanViewModal { align: center middle; background: $background 70%; }
    #plan-box       { width: 88%; height: 82%; padding: 1 2; border: round $accent; background: $surface; }
    #plan-box-head  { height: 1; margin-bottom: 1; }   /* 与正文/滚动条隔一行 → 按钮不贴着进度条 */
    #plan-box-title { width: 1fr; }
    #plan-modal-approve      { width: auto; padding: 0 2; margin-right: 4; background: $success; color: $text; text-style: bold; }   /* 右留大间距，和 ✕ 拉开、防误点 */
    #plan-modal-approve:hover{ background: $success-lighten-1; }
    #plan-view-x    { width: auto; padding: 0 1; color: $text-muted; text-style: bold; }   /* 干净的关闭图标：暗色 ✕、hover 才变红，无常驻色块 */
    #plan-view-x:hover { color: $text; background: $error; }
    #plan-box-body  { height: 1fr; }
    """

    def __init__(self, plan: str) -> None:
        super().__init__()
        self._plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-box"):
            with Horizontal(id="plan-box-head"):
                yield Static(Text.assemble(("📋 计划", "bold yellow"), ("   (按 Esc 关闭)", "dim")),
                             id="plan-box-title")
                yield _PlanModalApprove("批准执行", id="plan-modal-approve")
                yield _PlanClose("✕", id="plan-view-x")
            with VerticalScroll(id="plan-box-body"):
                yield Markdown(self._plan or "（计划为空）")

    def action_close(self) -> None:
        self.dismiss()


class ToolBox(VerticalScroll):
    """工具结果方框：内部可滚动。框内还有可滚内容（在那个方向上没到头）时滚框、吞掉滚轮，不带动
    外层对话；一旦框内滚到头、或内容根本没溢出，就放行让滚轮冒泡到 #log 去滚对话。
    （别无条件 stop——否则展开的大框/没溢出的框会把整个对话的滚动卡死：鼠标悬其上怎么滚都不动。）
    框内不响应点击收起：这片是给滚动/选中内容用的，收起走上面的标题。"""
    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.allow_vertical_scroll and self.scroll_y < self.max_scroll_y:
            super()._on_mouse_scroll_down(event)
            event.stop()                     # 框内还能往下 → 滚框、不冒泡

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.allow_vertical_scroll and self.scroll_y > 0:
            super()._on_mouse_scroll_up(event)
            event.stop()                     # 框内还能往上 → 滚框、不冒泡


class LinesView(ScrollView):
    """大输出【虚拟化】视图：只渲染当前可见的那十几行（render_line 按需调），创建时【不】渲染全部，
    展开上万行也不卡（O(可见) 而非 O(全部)；实测 5 万行一次性渲染 ~1.6s，虚拟化后只渲屏上 ~14 行）。
    带行号、dim、可纵横滚。用于后台完成块的输出（普通工具结果已被 agent 截到 6000 字，不需要它）。"""
    def __init__(self, text: str, **kw) -> None:
        super().__init__(**kw)
        self._lines = text.split("\n")                  # 只切分（廉价），不建 Text/strip
        w = max((len(ln) for ln in self._lines), default=0) + 6   # 行号栏 ~6 列；用 len 估宽（够快）
        self.virtual_size = Size(w, len(self._lines))

    def on_mount(self) -> None:
        self.styles.height = min(len(self._lines), 14)  # 视口最多 14 行，更多内部纵向滚动

    def render_line(self, y: int) -> Strip:
        sx, sy = self.scroll_offset
        idx = y + sy                                    # 屏上第 y 行 = 内容第 (滚动偏移 + y) 行
        if idx < 0 or idx >= len(self._lines):
            return Strip.blank(self.size.width)
        line = Text(f"{idx + 1:>4}  {self._lines[idx]}", style="dim", no_wrap=True, end="")
        segs = list(line.render(self.app.console))
        return Strip(segs).crop(sx, sx + self.size.width)   # 横向按滚动偏移裁到视口宽


class HistoryLog(VerticalScroll):
    """对话历史容器（#log）。续会话只渲倒数一批、落到底；滚到顶再加载更早一批。
    watch_scroll_y 监听滚动位置，刚从下方滑到顶（old>0 → new≤0）就发 LoadMore 给 App，
    App 决定是否真有更早历史要加载（无则空操作）。
    可聚焦（Tab 从输入框切过来）：聚焦时 Ctrl+↑/↓ 跳上/下一条【真·用户消息】、↑/↓ 逐行滚；
    打字/Esc/Enter 回输入框（打字把那个字带过去，无缝）。

    【滚动接管】user_scrolled：流式输出会不断贴底（_mount/_flush_stream 的 scroll_end），用户想回看
    时会被硬拽回底部。滚轮/按键【向上】滚 → 置位接管，App 侧所有自动贴底跳过；用户自己滚回底部
    （watch_scroll_y 检测到贴底）→ 交还，恢复跟随输出。"""
    can_focus = True

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.user_scrolled = False      # True=用户在回看历史，暂停自动贴底

    class LoadMore(Message):
        pass

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.user_scrolled = True       # 滚轮向上=明确想回看 → 接管（悬停即可滚，不需要焦点）
        super()._on_mouse_scroll_up(event)

    def on_focus(self) -> None:      # 对话区获/失焦（含点击换焦点）→ 让 App 刷新边框/提示
        self.app.call_after_refresh(self.app._on_focus_change)

    def on_blur(self) -> None:
        self.app.call_after_refresh(self.app._on_focus_change)

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "pageup", "home", "ctrl+up"):   # 键盘向上滚/跳 → 同滚轮，接管自动贴底
            self.user_scrolled = True
        if key == "ctrl+up":
            event.stop()
            self.app._jump_user_msg(-1)
        elif key == "ctrl+down":
            event.stop()
            self.app._jump_user_msg(1)
        elif key == "shift+tab":                         # Shift+Tab → 循环切运行模式
            event.stop()
            self.app.action_cycle_mode()
        elif key in ("tab", "enter"):                    # Tab/Enter → 回输入框（提示由 on_descendant_focus 刷）
            event.stop()
            self.app.query_one("#input", InputArea).focus()
        elif event.is_printable and event.character:     # 打字 → 回输入框并把这个字带过去
            event.stop()
            inp = self.app.query_one("#input", InputArea)
            inp.focus()
            inp.insert(event.character)
        # 其余（↑/↓/PageUp/Down/Home/End 等）不 stop → 冒泡到 VerticalScroll 的滚动绑定

    def watch_scroll_y(self, old: float, new: float) -> None:
        # 必须先调 super：基类在这里更新滚动条位置 + 触发滚动重画(_refresh_scroll)。
        # 之前漏了 → 滚动条永远停在顶、内容不重画(滚轮失灵/画面撕裂)，长 md 回复尤其明显。
        super().watch_scroll_y(old, new)
        if self.user_scrolled and new >= self.max_scroll_y - 1:   # 用户自己滚回了底部 → 交还，恢复跟随
            self.user_scrolled = False
        if hasattr(self.app, "_sync_pinned_header"):    # 滚动 → 用户消息可能进/出视口，更新悬浮头
            self.app._sync_pinned_header()
        if old > 0 and new <= 0:
            self.post_message(self.LoadMore())


class Thinking(Static):
    """思考块：标题+正文是同一个 widget，整块统一高亮/点击（不像 Collapsible 分成标题、正文两块，
    hover 会裂成两段）。标题带字数和思考耗时。点击展开/收起，但拖动选中文字不触发。"""
    def __init__(self, body: str, secs: float, **kw) -> None:
        super().__init__(**kw)
        self._body = body
        self._secs = secs
        self._expanded = False
        self._down_offset = None     # 记 MouseDown 位置，用来区分点击与拖动选中

    def on_mount(self) -> None:
        self._render_thinking()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._down_offset = event.screen_offset

    async def _on_click(self, event: events.Click) -> None:
        # 切换逻辑直接放在框架 _on_click 里：① 不调框架的多击选词/选整行（点击不选文字，拖选仍可）；
        # ② 按下到松开位置变了=拖动选中 → 不切换；③ 单击 → 展开/收起。
        if self._down_offset is not None and event.screen_offset != self._down_offset:
            return
        self._expanded = not self._expanded
        self._render_thinking()

    def _render_thinking(self) -> None:
        t = Text()
        arrow = "▼" if self._expanded else "▶"
        # 标题用蓝色（不用红/品红——那是报错色），跟白色回复、青色工具都区分开
        t.append(f"{arrow} 💭 思考 ({len(self._body)} 字) · {self._secs:.0f}s", style="blue")
        if self._expanded:                       # 正文整体缩进 2 格，挂在标题下面
            t.append("\n")
            t.append("\n".join("  " + ln for ln in self._body.splitlines()), style="dim")
        self.update(t)


class _ToolHead(Static):
    """工具块标题行：单击收起所在 ToolBlock、可拖选复制；但禁掉内置的双击选词/三击选整行
    （框架在 chain≥2 时 text_select_all，点击不该选文字）。无 hover 样式 → 悬停冒泡到 ToolBlock。"""
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._down = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._down = event.screen_offset

    async def _on_click(self, event: events.Click) -> None:
        # 切换逻辑放框架 _on_click 里：不调多击选词/选整行；拖动选中=不收起，单击=收起/展开。
        if self._down is not None and event.screen_offset != self._down:
            return
        node = self.parent
        while node is not None and not isinstance(node, ToolBlock):
            node = node.parent
        if node is not None:
            node.toggle()


class ToolBlock(Vertical):
    """工具块：标题行(可点收起) + 结果框(可滚/可选)。整块是同一个 hover 目标——标题/框内任意处
    悬停都高亮整块（标题与框都没 hover 样式，冒泡到这里）；而内框有自己的底色，会盖住高亮 → 高亮
    自然避开内框。点标题收起/展开，点内框只滚动/选中。"""
    def __init__(self, name: str, summary: str, boxes: list, background: bool = False,
                 args: dict | None = None, **kw) -> None:
        super().__init__(**kw)
        self._name = name
        self._summary = summary
        self._args = args if isinstance(args, dict) else {}   # 批折叠算摘要时回读参数（path/command 等）
        self._background = background            # 后台 bash：标题标“· 后台”，和前台 bash 区分
        self._err = None                         # None=进行中 / False=成功 / True=失败
        self._interrupted = False                # 被用户 Ctrl+C 中断：整行变黄、标“已中断”
        self._expanded = False
        # 直接持有子控件引用：真实流里 ToolStarted/ToolResult 背靠背到达，finish 会在 compose 跑完
        # 前就被调用，靠 query_one 会 NoMatches；用引用 + Static.update（挂载前也能用）最稳。
        self._head = _ToolHead(classes="tool-head")
        self._body = Vertical(*boxes, classes="tool-body")

    def compose(self) -> ComposeResult:
        yield self._head
        yield self._body

    def on_mount(self) -> None:
        self._refresh()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh()

    def finish(self, summary: str, err: bool, expand: bool | None = None,
               interrupted: bool = False) -> None:
        self._summary = summary
        self._err = err
        self._interrupted = interrupted
        # 默认：出错自动展开、正常折叠；expand 显式覆盖（如用户拒绝=已知一行，不展开成大框）
        self._expanded = err if expand is None else expand
        self._refresh()

    def _refresh(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        t = Text()
        # 中断=整行黄；正常标题青（跟白色回复、品红思考区分）；✓/✗ 各自绿/红
        style = "yellow" if self._interrupted else "cyan"
        t.append(f"{arrow} 🔧 {self._name}  {self._summary}", style=style)
        if self._background:
            t.append("  · 后台", style="yellow")     # 后台 bash：立即返回、转后台跑（完成会另起一行通知）
        if self._interrupted:
            t.append(" ⊘ 已中断", style="yellow")
        elif self._err is not None:
            t.append(" ✗" if self._err else " ✓", style="red" if self._err else "green")
        self._head.update(t)
        self._body.display = self._expanded


class BgDoneBlock(ToolBlock):
    """后台任务【完成】通知块：点标题展开/收起，看完整命令 + 完整输出（跟 bash 工具块点击后一样）。
    继承 ToolBlock 复用展开机制（_ToolHead 单击向上找 ToolBlock → toggle()，子类也命中 isinstance）；
    只重写标题文案为“⚙ bash · 命令 · 后台任务完成 (exit N)”（超时则红色“超时被终止”）。"""
    def __init__(self, command: str, exit_code, timeout: bool, boxes: list,
                 is_subagent: bool = False, description: str = "", **kw) -> None:
        # 收起标题：子 agent 用短 description（无则退回 command），bash 用命令；都截 50。展开框才给完整 prompt。
        super().__init__("subagent" if is_subagent else "bash",
                         _truncate((description or command) if is_subagent else command, 50), boxes, **kw)
        self._command = command
        self._exit_code = exit_code
        self._timeout = timeout
        self._is_subagent = is_subagent
        self._description = description

    def _refresh(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        cmd = _truncate(self._command, 50)
        if self._is_subagent:                            # 后台子 agent：短标签 + subagent 名，文案区分于 bash
            label = _truncate(self._description or self._command, 50)
            t = Text(f"{arrow} ◇ subagent · {label} · 后台完成", style="green")   # 图标与批折叠一致（subagent=◇）
        elif self._timeout:
            t = Text(f"{arrow} $ bash · {cmd} · 后台任务超时被终止", style="red")   # 红色本身即警示，图标统一用 $
        else:
            code = f" (exit {self._exit_code})" if self._exit_code is not None else ""
            t = Text(f"{arrow} $ bash · {cmd} · 后台任务完成{code}", style="green")   # bash=$，和批折叠一致
        self._head.update(t)
        self._body.display = self._expanded


class _Batch:
    """当前正在累积的【同类工具批】的状态：cls=分组键，blocks=该批的 ToolBlock（供算摘要/读中断），
    widgets=该批从头到尾挂进 #log 的所有 widget（工具块 + 夹在其间被 commit 的思考/正文），
    finalize 时把它们一起折进一行摘要。"""
    def __init__(self, cls: str) -> None:
        self.cls = cls
        self.blocks: list[ToolBlock] = []
        self.widgets: list = []


class BatchSummary(Static):
    """同类工具批折叠成的一行摘要（就地隐藏兄弟节点方案）：挂在该批第一个 widget 之前，
    持有这批 widget 的引用；点击切换它们的 display（展开=全显+▼，收起=全隐+▶）。
    黑白风格（青色/黄色）；若该批有被中断的黄块则整行标黄。参照 ServerPanel/BgTaskRow 的
    ALLOW_SELECT=False + add_class('-clickable') + on_click 写法（不撞基类/事件）。"""
    ALLOW_SELECT = False

    def __init__(self, icon: str, detail: str, widgets: list, interrupted: bool, **kw) -> None:
        super().__init__(**kw)
        self._icon = icon
        self._detail = detail
        self._widgets = widgets           # 该批被折叠隐藏的 widget（挂载的才隐；未挂/已移除的已在 finalize 时剔除）
        self._interrupted = interrupted
        self._expanded = False

    def on_mount(self) -> None:
        self.add_class("-clickable")
        self._render_summary()

    def on_click(self) -> None:
        self._expanded = not self._expanded
        for w in self._widgets:
            w.display = self._expanded    # 展开=还原全部；收起=全隐
        self._render_summary()

    def _render_summary(self) -> None:
        arrow = "▼" if self._expanded else "▶"
        style = "yellow" if self._interrupted else "cyan"
        self.update(Text(f"{arrow} {self._icon}  {self._detail}", style=style))   # 图标与文字之间留 2 空格


class InputArea(TextArea):
    """多行输入框：Enter 发送 / Shift+Enter（或 Alt/Ctrl+Enter）换行 / 首末行 ↑↓ 召回历史；
    高度随内容自适应。多行粘贴走 textual 的 Paste 事件，原样插入、不会误触发发送。"""
    ALLOW_SELECT = True
    # 覆盖 TextArea 自带的 ctrl+a（原本是“跳到行首”）为全选，方便一键清空（全选后删/输入即替换）。
    # 只覆盖 ctrl+a；home 仍走父类的“跳到行首”。
    BINDINGS = [Binding("ctrl+a", "select_all", "全选", show=False)]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._history: list[str] = []
        self._hist_idx: int | None = None   # None=不在浏览历史；否则为历史索引
        self._draft = ""                     # 进入历史前暂存的当前草稿

    def on_mount(self) -> None:
        self._autosize()

    def push_history(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_idx = None

    def _autosize(self) -> None:
        # 高度随内容自适应：可视行数 + 2（TextArea 自带 tall 边框占上下各 1 行）。
        # 内容行夹在 [1, 12]，总高 [3, 14]。
        h = max(self.wrapped_document.height, self.document.line_count, 1)
        self.styles.height = min(h, 12) + 2

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._hist_idx is None:           # 第一次上翻：暂存草稿，跳到最后一条
            self._draft = self.text
            self._hist_idx = len(self._history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        else:
            return
        self.load_text(self._history[self._hist_idx])
        self.move_cursor(self.document.end)
        self._autosize()

    def _history_next(self) -> None:
        if self._hist_idx is None:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self.load_text(self._history[self._hist_idx])
        else:                                # 翻回最新：恢复草稿、退出历史浏览
            self._hist_idx = None
            self.load_text(self._draft)
        self.move_cursor(self.document.end)
        self._autosize()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "tab":               # Tab → 切到对话区（放 read_only 之前：跑动中也能切过去浏览）
            event.stop()
            event.prevent_default()
            self.app.action_toggle_focus()
            return
        if event.key == "shift+tab":         # Shift+Tab → 循环切运行模式（跑动中也可切，下个 gate/请求生效）
            event.stop()
            event.prevent_default()
            self.app.action_cycle_mode()
            return
        if self.read_only:                   # 本轮在跑时只读：吞掉编辑/历史键（Ctrl+C 是 app 优先绑定，不受影响）
            return
        key = event.key
        if key == "enter":                   # 回车 = 发送
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if key in ("shift+enter", "ctrl+j", "alt+enter", "ctrl+enter"):   # 换行（看终端支持哪个）
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        # 历史召回挪到 Ctrl+↑/↓（普通 ↑/↓ 留给移动光标——多行输入里常只想上/下移一行）。
        if key == "ctrl+up" and self._history:
            event.stop()
            event.prevent_default()
            self._history_prev()
            return
        if key == "ctrl+down" and self._hist_idx is not None:
            event.stop()
            event.prevent_default()
            self._history_next()
            return
        await super()._on_key(event)


class SessionPickerScreen(ModalScreen):
    """续会话选择器（/rs）：列最近若干会话，↑↓ 选 · 0-9 跳选 · Enter 加载 · Esc 取消。
    dismiss 返回选中的会话头 dict（取消则 None），由 App 的回调拿去 resume。"""
    BINDINGS = [Binding("escape", "cancel", "取消")]
    CSS = """
    SessionPickerScreen { align: center middle; background: $background 70%; }
    #picker { width: 76; height: auto; max-height: 80%; padding: 1 2;
              border: round $accent; background: $surface; }
    #picker-hint { color: $text-muted; padding-bottom: 1; }
    #picker-list { height: auto; max-height: 18; }
    #picker-list > ListItem { padding: 0 1; }
    """

    def __init__(self, sessions: list[dict], current_id: str | None = None) -> None:
        super().__init__()
        self._sessions = sessions
        self._current_id = current_id           # 当前会话 id：列表里它那条标注"(当前)"

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("续哪个会话？  ↑↓ 选 · 0-9 跳 · Enter 加载 · Esc 取消", id="picker-hint")
            yield ListView(id="picker-list")

    def on_mount(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        for i, m in enumerate(self._sessions):
            spans = [(f"{i}  ", "bold cyan"), (m.get("title") or "(无标题)", "bold")]
            if m.get("session_id") == self._current_id:
                spans.append(("  (当前)", "green"))
            spans.append(("\n     " + _fmt_time(m.get("updated_at")), "dim"))
            lv.append(ListItem(Static(Text.assemble(*spans))))
        lv.index = 0
        lv.focus()

    def on_key(self, event: events.Key) -> None:
        # 数字键 0-9：把高亮跳到第 N 个（不直接加载，仍需 Enter）——上下箭头/Enter 交给 ListView 原生处理
        if event.key.isdigit():
            d = int(event.key)
            if d < len(self._sessions):
                self.query_one("#picker-list", ListView).index = d
            event.stop()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.query_one("#picker-list", ListView).index or 0
        self.dismiss(self._sessions[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)


class PermissionModal(ModalScreen):
    """工具审批弹窗：↑↓ 选 · Enter 确认 · Esc 拒绝。dismiss 返回 once/always/deny。"""
    BINDINGS = [Binding("escape", "deny", "拒绝")]
    CSS = """
    PermissionModal { align: center middle; background: $background 70%; }
    #perm { width: 72; height: auto; padding: 1 2; border: round $warning; background: $surface; }
    #perm-sum { color: $text-muted; padding: 0 0 1 0; }
    #perm-list { height: auto; }
    #perm-list > ListItem { padding: 0 1; }
    """

    def __init__(self, tool: str, args: dict, is_sub: bool = False, root: str | None = None,
                 can_stop: bool = False) -> None:
        super().__init__()
        self._tool, self._args = tool, args
        self._is_sub = is_sub                  # 只影响标题（让用户知道在批哪一层）
        # 文案是"停止此【后台任务】"而不是"停止此 agent 分支"：workflow 各阶段【共用】那个后台任务的
        # 打断标志（workflow_tools 自己 new 一个 Event 交给 bg.start_fn），停一个 = 停整个 workflow，
        # 说成"分支"会让人以为只停这一个阶段。而后台子 agent 本身就是一个后台任务 → 两种情形下
        # "停止此后台任务"都说的是实话。（"一次打断即全部打断"对 workflow 是合理设计，不是缺陷。）
        # 能被【单独】停掉的（后台/workflow 子 agent）才给这一项：拒这一步之外
        # 还能停掉整条分支，否则三个选项全是"让它继续跑"，跑偏时只能一次次拒、它一次次换法子再问。
        # 前台子 agent【不给】——它与主 agent 共享打断标志，那一项会连主 agent 和并发的兄弟一起停。
        self._can_stop = can_stop
        # 该次调用能不能生成有意义的授权规则（畸形参数、含元字符的 bash 等生成不了）。
        # 生成不了就【不给"总是允许"这一项】——记不住的事别承诺，此前那一项点下去要么无效、
        # 要么记下一条比本次调用更宽的规则。root 用来判"文件夹是否大到不该整片授权"。
        self._pattern = pattern_for(tool, args, root)
        # 【选项与返回值出自同一份清单】：(返回值, 显示文案)。此前两处各写各的顺序——compose 里
        # "停止"排在"拒绝"之前、_choices 里排在最后 → 点"停止此后台任务"只拒了这一步（任务照跑）、
        # 点"拒绝"反而把整个后台任务停了，两个选项的行为互换。同一份清单就不可能再错位。
        self._options: list[tuple[str, Text | str]] = [("once", "允许一次")]
        if self._pattern:
            self._options.append(("always", f"总是允许 {self._pattern}（记住，不再问）"))
        self._options.append(("deny", "拒绝"))
        if can_stop:
            self._options.append(("stop", Text("停止此后台任务（拒绝并终止它）", style="bold red")))

    @property
    def _choices(self) -> list[str]:
        """选项的返回值序列（与屏上列表项一一对应，由 _options 派生）。"""
        return [c for c, _ in self._options]

    def compose(self) -> ComposeResult:
        title = "⚠ 子 agent 请求执行工具？  " if self._is_sub else "⚠ 允许执行工具？  "
        with Vertical(id="perm"):
            yield Static(Text.assemble((title, "bold yellow"), (self._tool, "bold")))
            yield Static(Text(_summarize_tool(self._tool, self._args), style="dim"), id="perm-sum")
            yield ListView(*(ListItem(Static(label)) for _, label in self._options), id="perm-list")

    def on_mount(self) -> None:
        lv = self.query_one("#perm-list", ListView)
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.query_one("#perm-list", ListView).index or 0
        self.dismiss(self._choices[idx] if idx < len(self._choices) else "deny")

    def action_deny(self) -> None:
        self.dismiss("deny")


class AskOtherInput(TextArea):
    """提问弹窗"其他"的输入行：用主输入框（InputArea）同款 TextArea 而非 Input——真机实测
    中文输入法对 Input 会把预编辑串错位到屏幕左上角、拼音字母漏进框里；TextArea 是本项目里
    唯一经长期中文输入验证的部件。placeholder 原生支持（框内灰字提示）。Enter 提交；
    Esc 不拦（默认 tab_behavior="focus" 不吃 escape），冒泡给弹窗做收起。"""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        await super()._on_key(event)

    def on_show(self) -> None:
        """display=False→True 与 focus() 同一拍执行时，focus 时刻算的输入法锚点用的是隐藏期的
        空 region → (0,0)，输入法预编辑窗被钉到屏幕左上角、还会退化成生字母直发。可见后等一帧
        布局完成再重钉锚点、再排一帧把终端真光标写进框内，预编辑串就回到输入位。"""
        def pin() -> None:
            if self.has_focus:
                self.app.cursor_position = self.cursor_screen_offset
                self.refresh()
        self.call_after_refresh(pin)


class _AskNavBtn(Static):
    """提问弹窗右下角导航钮（上一题 / 下一题·完成）。点击转发弹窗对应 action（首题置灰等在 action 里自拦）。"""
    def __init__(self, role: str, id: str) -> None:
        super().__init__("", id=id, classes="ask-btn")
        self._role = role

    def on_click(self) -> None:
        if self._role == "prev":
            self.screen.action_prev_q()
        else:
            self.screen.action_next_q()


class QuestionModal(ModalScreen):
    """ask_user 提问弹窗：逐题作答，右下角「上一题 / 下一题·完成」可来回导航改答（首题隐藏上一题）。
    单选 Enter 选中后选中行消失-出现闪烁两次（约 1 秒）自动跳下一题（期间可换选/导航，重新计时）；多选 Enter
    勾/去勾，「下一题/完成」即提交本题；"其他"换出输入框自由作答。Esc 立即收卷：已答带回、未答进 skipped。
    dismiss 返回 {"answers": {问题: 答案}, "skipped": [未答问题]}；答案=label / "其他：文本" / 多选 label 列表。"""
    BINDINGS = [Binding("escape", "skip", "跳过"),
                Binding("left", "prev_q", "上一题", show=False),
                Binding("right", "next_q", "下一题", show=False)]
    CSS = """
    QuestionModal { align: center middle; background: $background 70%; }
    #ask { width: 76; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #ask-head { color: $text-muted; }
    #ask-q { padding: 0 0 1 0; }
    #ask-list { height: auto; max-height: 18; }
    #ask-list > ListItem { padding: 0 1; }
    #ask-other { height: 3; }
    #ask-btns { height: 1; margin: 1 0 0 0; }
    #ask-next { dock: right; }
    .ask-btn { padding: 0 2; width: auto; background: $panel; }
    .ask-btn:hover { background: $accent 50%; }
    #ask-hint { color: $text-muted; padding: 1 0 0 0; }
    """

    def __init__(self, questions: list[dict]) -> None:
        super().__init__()
        self._qs = questions
        self._idx = 0                     # 当前第几题
        # 每题独立作答状态（「上一题」回看改答要能还原）：chosen=单选选中项下标（-1="其他"，None=未答）；
        # checked=多选勾选集；other=该题"其他"的自由输入文本
        self._state = [{"chosen": None, "checked": set(), "other": None} for _ in questions]
        self._adv_timer = None            # 单选选中后"闪烁反馈→自动跳下一题"的定时器
        self._flash_hide = False          # 闪烁相位（True=选中行处于"消失"拍）
        self._done = False                # 已收卷（dismiss 后迟到的事件全部忽略）

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Static("", id="ask-head")
            yield Static("", id="ask-q")
            yield ListView(id="ask-list")
            inp = AskOtherInput(placeholder="输入自定义回答，Enter 确认", id="ask-other")
            inp.cursor_blink = False       # 光标闪烁每 0.5s 重绘一帧，同样会顶走输入法预编辑串
            inp.display = False
            yield inp
            with Horizontal(id="ask-btns"):
                yield _AskNavBtn("prev", id="ask-prev")        # 常规流靠左
                yield _AskNavBtn("next", id="ask-next")        # dock: right 钉右缘
            yield Static("", id="ask-hint")

    def on_mount(self) -> None:
        self._show()

    def _cur(self) -> dict:
        return self._qs[self._idx]

    def _st(self) -> dict:
        return self._state[self._idx]

    def _show(self, keep_index: int = 0) -> None:
        """渲染当前题：题头 + 问题 + 选项列表（含作答态标记）+ 导航钮 + 操作提示。
        keep_index=重建后光标落哪。"""
        q = self._cur()
        head = f"❓ 提问 第 {self._idx + 1}/{len(self._qs)} 题"
        if q.get("header"):
            head += f" · {q['header']}"
        self.query_one("#ask-head", Static).update(Text(head, style="bold"))
        self.query_one("#ask-q", Static).update(Text(q["question"], style="bold"))
        inp = self.query_one("#ask-other", AskOtherInput)   # 换题统一收起输入框：用户可 Tab/鼠标绕开
        inp.display = False                                 # 输入框直接答题推进，不复位会把残留文本错记
        inp.load_text("")                                   # 成新题的"其他"答案
        lv = self.query_one("#ask-list", ListView)
        lv.clear()
        for i in range(len(q["options"]) + 1):        # 选项行 + 末尾"其他"行
            lv.append(ListItem(Static(self._row_text(i))))
        last = self._idx == len(self._qs) - 1
        prev = self.query_one("#ask-prev", _AskNavBtn)
        prev.display = self._idx > 0                # 首题没有"上一题"：整个隐藏而非置灰
        prev.update(Text("‹ 上一题", style="bold"))
        self.query_one("#ask-next", _AskNavBtn).update(
            Text("完成 ✔" if last else "下一题 ›", style="bold #33d17a" if last else "bold"))
        self.query_one("#ask-hint", Static).update(Text(self._hint_text()))
        lv.index = keep_index
        lv.focus()

    def _row_text(self, i: int) -> Text:
        """选项列表第 i 行的内容（i==len(options) 为末尾"其他"行），含单选闪烁"消失"拍的整行留白
        （行数不变，不跳版）。独立成方法：闪烁节拍只原地 update 选中行的 Static（见 _refresh_row），
        不整表重建——clear/append 是异步清+挂，0.2s 节拍下两次重建交错会让列表瞬间全空，
        看起来像所有选项一起闪。"""
        q, st = self._cur(), self._st()
        multi = q["multi_select"]
        opts = q["options"]
        t = Text()
        if i >= len(opts):                            # "其他"行
            picked = (not multi and st["chosen"] == -1)
            if picked and self._flash_hide:
                t.append(" ")
                return t
            if multi:
                t.append("[x] " if st["other"] is not None else "[ ] ",
                         style="bold #33d17a" if st["other"] is not None else "dim")
            else:
                t.append("● " if picked else "  ", style="bold #33d17a")
            t.append("其他（自由输入）" if st["other"] is None else f"其他：{st['other']}",
                     style="bold #33d17a" if picked else "bold")
            return t
        o = opts[i]
        picked = (not multi and st["chosen"] == i)
        if picked and self._flash_hide:
            t.append(" " + ("\n " if o["description"] else ""))
            return t
        if multi:
            t.append("[x] " if i in st["checked"] else "[ ] ",
                     style="bold #33d17a" if i in st["checked"] else "dim")
        else:
            t.append("● " if picked else "  ", style="bold #33d17a")
        t.append(o["label"], style="bold #33d17a" if picked else "bold")
        if o["description"]:
            t.append("\n" + ("    " if multi else "  ") + o["description"], style="dim")
        return t

    def _refresh_row(self, row: int) -> None:
        """闪烁节拍：原地更新第 row 行内容（不重建列表，见 _row_text 的注释）。"""
        lv = self.query_one("#ask-list", ListView)
        if row < len(lv.children):
            lv.children[row].query_one(Static).update(self._row_text(row))

    def _hint_text(self) -> str:
        last = self._idx == len(self._qs) - 1
        if self._cur()["multi_select"]:
            return f"Enter 勾选/取消 · → 或点「{'完成' if last else '下一题'}」确认提交 · Esc 跳过提问"
        if last:
            return "↑↓ 选 · Enter 选中 · → 或点「完成」提交 · Esc 跳过提问"
        return "↑↓ 选 · Enter 确认 · ←→ 上/下一题 · Esc 跳过提问"

    # ---- 作答状态 ----

    def _answer_of(self, i: int):
        """第 i 题当前作答（None=未答）：单选=label 或 "其他：文本"；多选=label 列表。"""
        q, st = self._qs[i], self._state[i]
        if q["multi_select"]:
            labels = [q["options"][j]["label"] for j in sorted(st["checked"])]
            if st["other"] is not None:
                labels.append(f"其他：{st['other']}")
            return labels or None
        if st["chosen"] is None:
            return None
        return f"其他：{st['other']}" if st["chosen"] == -1 else q["options"][st["chosen"]]["label"]

    def _finish(self) -> None:
        """收卷：逐题已答进 answers、未答进 skipped（Esc 提前收卷时可能非连续）。"""
        if self._done:
            return
        self._done = True
        self._cancel_timer()
        answers, skipped = {}, []
        for i, q in enumerate(self._qs):
            a = self._answer_of(i)
            if a is None:
                skipped.append(q["question"])
            else:
                answers[q["question"]] = a
        self.dismiss({"answers": answers, "skipped": skipped})

    # ---- 导航 ----

    def _cancel_timer(self) -> None:
        if self._adv_timer is not None:
            self._adv_timer.stop()
            self._adv_timer = None
        self._flash_hide = False

    def _advance(self) -> None:
        self._cancel_timer()
        if self._idx >= len(self._qs) - 1:
            self._finish()
        else:
            self._idx += 1
            self._show()

    def _arm_auto_advance(self) -> None:
        """单选选中后的视觉反馈：选中行消失-出现闪烁两次（每拍 0.2s，共约 1 秒）再自动推进——
        闪烁期间可换选/导航，随时取消重来。"""
        self._cancel_timer()
        armed = self._idx
        chosen = self._st()["chosen"]
        row = len(self._cur()["options"]) if chosen == -1 else chosen
        seq = [True, False, True, False]          # True=消失拍：隐-现-隐-现 = 闪两次，第 5 拍推进

        def tick() -> None:
            if self._done or self._idx != armed:
                return
            if seq:
                self._flash_hide = seq.pop(0)
                self._refresh_row(row)            # 只碰选中行，别整表重建
            else:
                self._advance()

        self._adv_timer = self.set_interval(0.2, tick, repeat=5)

    def action_prev_q(self) -> None:
        if self._done or self._idx == 0:
            return
        self._cancel_timer()
        self._idx -= 1
        self._show()

    def action_next_q(self) -> None:
        """下一题/完成：当前题有作答才放行——多选的"提交本题"合并在这（勾了即算答）。"""
        if self._done:
            return
        self._cancel_timer()
        if self._answer_of(self._idx) is None:
            self.query_one("#ask-hint", Static).update(
                Text("请先作答（或 Esc 跳过提问）", style="bold yellow"))
            return
        self._advance()

    # ---- 事件 ----

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._done:                  # 收卷后队列里迟到的 Selected 全部忽略
            return
        q, st = self._cur(), self._st()
        idx = self.query_one("#ask-list", ListView).index or 0
        n_opt = len(q["options"])
        if not q["multi_select"]:
            if idx < n_opt:                         # 单选：标记选中 → 停留 1 秒自动跳转（换选重计时）
                st["chosen"], st["other"] = idx, None
                self._show(keep_index=idx)
                if self._idx < len(self._qs) - 1:   # 末题不自动收卷：亮选中标，等显式「完成」
                    self._arm_auto_advance()
            else:                                   # "其他" → 换出输入框；进"其他"即放弃已选项，
                self._cancel_timer()                # 避免"● 旧选项 + 输入框"两个作答态并存的歧义
                st["chosen"], st["other"] = None, None
                self._show(keep_index=idx)          # 重渲去掉旧选中标记
                self._show_input()
            return
        if idx < n_opt:                             # 多选：Enter 在选项上是勾/去勾
            st["checked"] ^= {idx}
            self._show(keep_index=idx)
        else:                                       # 多选的"其他"：录过再 Enter 是去掉，没录过是去输入
            if st["other"] is not None:
                st["other"] = None
                self._show(keep_index=idx)
            else:
                self._show_input()

    def _show_input(self) -> None:
        inp = self.query_one("#ask-other", AskOtherInput)
        inp.load_text("")
        inp.display = True
        self.query_one("#ask-hint", Static).update(Text("Enter 确认 · Esc 收起返回选项"))
        inp.focus()

    @on(AskOtherInput.Changed)
    def _on_other_changed(self, event) -> None:
        """多选的"其他"随输入实时勾选：框里有字即勾（文本同步进勾选行），删空即去勾——不用等 Enter。"""
        if self._done or not self._cur()["multi_select"]:
            return
        inp = self.query_one("#ask-other", AskOtherInput)
        if not inp.display:             # _show 复位输入框时的 load_text 也会发 Changed，忽略
            return
        self._st()["other"] = inp.text.strip() or None
        self._refresh_row(len(self._cur()["options"]))

    @on(AskOtherInput.Submitted)
    def _on_other_submitted(self, event: AskOtherInput.Submitted) -> None:
        if self._done:                  # 收卷后迟到的提交忽略
            return
        st = self._st()
        if self._cur()["multi_select"]:             # 多选：文本已随输入实时同步（Changed），Enter 只收起回列表
            self.query_one("#ask-other", AskOtherInput).display = False
            self._show(keep_index=len(self._cur()["options"]))
            return
        text = event.value.strip()
        if not text:
            return
        self.query_one("#ask-other", AskOtherInput).display = False
        if self._idx < len(self._qs) - 1:           # 单选：显式 Enter 确认，无需停留，直接推进
            st["chosen"], st["other"] = -1, text
            self._advance()
        else:                                       # 单选末题：只落答显示，等显式「完成」收卷
            st["chosen"], st["other"] = -1, text
            self._show(keep_index=len(self._cur()["options"]))

    def action_skip(self) -> None:
        """Esc：输入框开着 → 收起回列表；否则立即收卷（已答带回、未答进 skipped）。"""
        if self._done:
            return
        inp = self.query_one("#ask-other", AskOtherInput)
        if inp.display:
            inp.display = False
            self.query_one("#ask-hint", Static).update(Text(self._hint_text()))
            self.query_one("#ask-list", ListView).focus()
            return
        self._finish()


# 模式色（单一色源，渲染细节归 TUI、不进 mode.py）：状态栏按钮底色 + 输入框边框 + 弹窗圆点都用它。
# auto 用亮绿（贴近侧栏上下文数字的绿），别用 Textual "green"(#008000) 那种深绿。
MODE_COLORS = {"normal": "cyan", "auto": "#33d17a", "plan": "yellow", "yolo": "red"}


def _mode_chip_css() -> str:
    """由 MODE_COLORS 生成模式按钮每模式的底色 + hover 变暗色（向黑压深 45%，变化明显）。
    走 CSS `:hover` 而非手动 on_enter/on_leave：引擎管 hover、必覆盖整块、切模式即时重算，不留手动改内联背景的坑。"""
    out = []
    for k, c in MODE_COLORS.items():
        hover = Color.parse(c).blend(Color(0, 0, 0), 0.45).hex
        out.append(f"#mode-chip.-m-{k} {{ background: {c}; }}")
        out.append(f"#mode-chip.-m-{k}:hover {{ background: {hover}; }}")
    return "\n".join(out)


class ModeChip(Static):
    """状态栏模式按钮：底色/hover 由 CSS 类 `-m-<mode>` 控制（见 MODE_COLORS 与 _mode_chip_css）；点击打开模式弹窗。"""
    def on_click(self) -> None:
        self.app.action_open_modes()


class _PickOpt(Static):
    """思考区一个可点选项（思考 开/关、深度 high/max）。选中=亮绿带框、否则暗、思考关时深度置灰；
    点击回调所在弹窗的 _pick(group, value)，不关弹窗（思考是正交轴，切它不切模式）。"""
    def __init__(self, text: str, value, group: str, selected: bool, enabled: bool = True) -> None:
        super().__init__(classes="pick-opt")
        self._text = text
        self.value = value
        self.group = group
        self._selected = selected
        self._enabled = enabled
        self._paint()

    def _paint(self) -> None:
        if not self._enabled:
            self.update(Text(f" {self._text} ", style="dim"))
        elif self._selected:
            self.update(Text(f"[{self._text}]", style="bold #33d17a"))
        else:
            self.update(Text(f" {self._text} ", style="white"))

    def set_selected(self, sel: bool) -> None:
        self._selected = sel
        self._paint()

    def set_enabled(self, en: bool) -> None:
        self._enabled = en
        self._paint()

    def on_click(self) -> None:
        if self._enabled:
            self.screen._pick(self.group, self.value)


class _ModeClose(Static):
    """弹窗右上角关闭 ✕：点击 = 取消（同 Esc）。"""
    def on_click(self) -> None:
        self.screen.action_cancel()


class ModeModal(ModalScreen):
    """模式选择弹窗：↑↓/数字 选 · Enter 应用 · Esc 取消 · 点击直接选。dismiss 返回模式 key（取消则 None）。
    每行 = 彩色圆点 + 模式名 + 一句简介，当前模式打勾。颜色由 App 注入（渲染细节不进 mode.py）。
    底部按当前模型的思考档案能力，显"思考 开/关""深度 high/max"（点选即生效、不关弹窗；不支持的不显）。"""
    BINDINGS = [Binding("escape", "cancel", "取消")]
    CSS = """
    ModeModal { align: center middle; background: $background 70%; }
    #modes { width: 66; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #modes-head { height: auto; padding-bottom: 1; }
    #modes-hint { width: 1fr; color: $text-muted; }
    #modes-close { width: auto; padding: 0 1; color: $text-muted; }
    #modes-close:hover { background: #7a1f1f; color: white; }   /* hover：暗红方块底 + 白叉 */
    #modes-list { height: auto; }
    #modes-list > ListItem { padding: 0 1; }
    #think-sep { color: $text-muted; padding-top: 1; }
    .think-row { height: 1; }
    .think-label { width: auto; color: $text-muted; }
    .pick-opt { width: auto; padding: 0 1; }
    .pick-opt:hover { background: white 12%; }
    """

    def __init__(self, current: str, colors: dict, profile, thinking_on: bool,
                 effort: str, on_thinking) -> None:
        super().__init__()
        self._current = current
        self._colors = colors
        self._profile = profile               # 当前模型的思考能力档案（决定显哪些控件）
        self._thinking_on = thinking_on
        self._effort = effort or profile.default_effort
        self._on_thinking = on_thinking       # 回调 App：(thinking_on, effort) → 设 provider + 持久化
        self._think_opts: list[_PickOpt] = []
        self._depth_opts: list[_PickOpt] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modes"):
            with Horizontal(id="modes-head"):
                yield Static(Text.assemble(("模式", "bold"), ("   Shift+Tab 循环切换", "dim"),
                                           ("   (按 Esc 关闭)", "dim")), id="modes-hint")
                yield _ModeClose("✕", id="modes-close")
            yield ListView(id="modes-list")
            p = self._profile
            if p.toggleable or p.effort_tiers:
                yield Static(Text("─" * 44, style="dim"), id="think-sep")
            if p.toggleable:
                with Horizontal(classes="think-row"):
                    yield Static("思考  ", classes="think-label")
                    on_o = _PickOpt("开", True, "think", self._thinking_on)
                    off_o = _PickOpt("关", False, "think", not self._thinking_on)
                    self._think_opts = [on_o, off_o]
                    yield on_o
                    yield off_o
            if p.effort_tiers:
                with Horizontal(classes="think-row"):
                    yield Static("深度  ", classes="think-label")
                    for tier in p.effort_tiers:
                        o = _PickOpt(tier, tier, "depth", tier == self._effort,
                                     enabled=self._thinking_on)
                        self._depth_opts.append(o)
                        yield o

    def on_mount(self) -> None:
        lv = self.query_one("#modes-list", ListView)
        for i, key in enumerate(CYCLE):
            m = MODES[key]
            color = self._colors.get(key, "white")
            spans = [(f"{i + 1} ", "dim"), ("● ", color), (m.label, f"bold {color}")]
            if key == self._current:
                spans.append(("  ✓ 当前", "green"))
            spans.append((f"\n     {m.desc}", "dim"))
            lv.append(ListItem(Static(Text.assemble(*spans))))
        lv.index = CYCLE.index(self._current) if self._current in CYCLE else 0
        lv.focus()

    def on_key(self, event: events.Key) -> None:
        # 数字键 1-N：高亮跳到第 N 个（仍需 Enter 应用）；↑↓/Enter 交给 ListView 原生
        if event.key.isdigit():
            d = int(event.key) - 1
            if 0 <= d < len(CYCLE):
                self.query_one("#modes-list", ListView).index = d
            event.stop()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.query_one("#modes-list", ListView).index or 0
        self.dismiss(CYCLE[idx])

    def _pick(self, group: str, value) -> None:
        """点了思考区选项：更新高亮 + 回调 App 生效（不关弹窗）。"""
        if group == "think":
            self._thinking_on = bool(value)
            for o in self._think_opts:
                o.set_selected(o.value == value)
            for o in self._depth_opts:                # 思考关 → 深度置灰不可点；开 → 恢复
                o.set_enabled(self._thinking_on)
        else:                                          # depth
            if not self._thinking_on:
                return
            self._effort = value
            for o in self._depth_opts:
                o.set_selected(o.value == value)
        self._on_thinking(self._thinking_on, self._effort)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _fmt_window(n: int) -> str:
    """上下文窗口 token 数 → 人读：1_000_000→1M、256_000→256K、204_800→200K、0→空。"""
    if not n:
        return ""
    if n >= 1_000_000:
        return (f"{n / 1_000_000:g}M")
    return f"{round(n / 1000)}K"


_INVALID = object()   # ConfigScreen._parse_adv 的"非法输入"哨兵（区别于合法的 0=用默认）


class _CfgClick(Static):
    """ConfigScreen 里的可点元素（tab / 确定按钮）：点击 → 调所在 screen 的方法名。"""
    def __init__(self, label: str, action: str, *, id: str | None = None, classes: str = "") -> None:
        super().__init__(label, id=id, classes=classes)
        self._action = action

    def on_click(self) -> None:
        getattr(self.screen, self._action)()


class _SavedLbl(Static):
    """切换页列表行的标签：点击【只把高亮移过来】（event.stop() 拦住冒泡——否则 ListItem 会让
    ListView 发 Selected，被当成"确定"直接切换关窗）。确定只走 Enter / 确定并保存按钮。"""
    def __init__(self, text, idx: int) -> None:
        super().__init__(text, classes="cfg-saved-lbl")
        self._idx = idx

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.screen.query_one("#cfg-saved-list", ListView).index = self._idx


class _SavedDel(Static):
    """切换页列表行右侧的 ✕：删这条已保存配置。event.stop() 防冒泡成"选中该行"。
    二次确认：第一次点 ✕ 变成红底"确认删除？"（同时把别行已弹出的确认收回去），再点一次才真删。"""
    def __init__(self, entry: dict) -> None:
        super().__init__("✕", classes="cfg-del")
        self._entry = entry
        self._armed = False

    def disarm(self) -> None:
        self._armed = False
        self.update("✕")
        self.remove_class("-armed")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if not self._armed:
            for other in self.screen.query(_SavedDel):   # 同时只允许一行处于待确认态
                other.disarm()
            self._armed = True
            self.update("确认删除？")
            self.add_class("-armed")
        else:
            self.screen._delete_saved(self._entry)


class ConfigScreen(ModalScreen):
    """`/config` 模型管理：后端单一真相源 = ~/.mecode/config.json（保存后重启 mecode 生效）。
    两级菜单：新增模型（一键选 provider·model→填 key / 手动全填）/ 切换模型（config 里已存的列表）。
    .env 只作检测源：配了模型且 config 没有时，切换页给"点此加载"行 → 跳手动页预填导入。
    已存 ≥2 个模型时默认落切换页（大概率是来切的），否则落新增页。"""
    BINDINGS = [Binding("escape", "cancel", "取消")]
    CSS = """
    ConfigScreen { align: center middle; background: $background 85%; }
    #cfg { width: 80; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #cfg-head { height: 1; margin-bottom: 1; }
    #cfg-title { width: 1fr; text-style: bold; }
    #cfg-x { width: auto; padding: 0 1; color: $text-muted; }
    #cfg-x:hover { background: #7a1f1f; color: white; }   /* 同 ModeModal：hover 暗红方块底 + 白叉 */
    /* .env 导入行：检测到 .env 配了模型且 config 里没有时显示；整行可点 → 跳手动页预填 */
    #cfg-envimport { height: auto; margin-top: 1; padding: 0 1; background: $warning 20%; color: $warning; display: none; }
    #cfg-envimport:hover { background: $warning 40%; }
    /* 两级菜单：顶层【新增/切换】胶囊 tab；新增下再分【一键选择/手动填写】子 tab。
       都是 1 行高胶囊（与标题同尺度）；未选淡底标可点、选中亮橙底黑字。
       子 tab 行在"切换"页用 visibility 隐藏（占位不塌）→ 切顶层菜单弹窗高度不变。 */
    #cfg-tabs { height: 1; margin-bottom: 1; }
    .cfg-tab { width: auto; padding: 0 3; margin-right: 2; color: $text; background: white 8%; }
    .cfg-tab.-on { color: black; background: $accent; text-style: bold; }
    .cfg-tab:hover { background: white 25%; }
    #cfg-subtabs { height: 1; margin-bottom: 1; }
    .cfg-subtab { width: auto; padding: 0 2; margin-right: 2; color: $text-muted; background: white 8%; }
    .cfg-subtab.-on { color: $text; background: $primary 40%; text-style: bold; }
    .cfg-subtab:hover { background: white 25%; }
    /* 两个 tab 面板共用这块【定高】区：切 tab 不跳。高度取各态最高的一态(手动页+思维链选项≈18)；
       preset 页把模型列表放高、正好填满同样高度 → 既不裁手动页选项、preset 页也没有空隙。 */
    /* 按内容自适应。曾写死 18 行（按最高的"一键页"定的），于是切换页只有一个列表（5 条≈8 行）时
       白占 10 行，底部按钮被推到老远。代价：切页时弹窗高度会变（各页内容 8~15 行不等）——
       比一大片空白可接受。*/
    #cfg-body { height: auto; }
    #cfg-preset { height: auto; }
    #cfg-saved { height: auto; }
    #cfg-saved-list { height: auto; max-height: 14; border: round $panel; }
    #cfg-saved-empty { height: auto; margin-top: 1; }
    .cfg-saved-row { height: 1; }
    .cfg-saved-lbl { width: 1fr; }
    .cfg-del { width: auto; padding: 0 1; color: $text-muted; }
    .cfg-del:hover { background: #7a1f1f; color: white; }
    .cfg-del.-armed { background: $error; color: black; text-style: bold; }
    .cfg-del.-armed:hover { background: $error-lighten-1; }
    #cfg-models { height: auto; max-height: 11; border: round $panel; }
    #cfg-keyhint { color: $text-muted; height: auto; }
    #cfg-adv { height: auto; }   /* 不留 margin：第一项是标签，紧贴上一块即可 */
    .cfg-row { height: auto; }
    .cfg-row Input { width: 1fr; }
    .cfg-inline-save { width: auto; padding: 0 2; margin-top: 1; margin-left: 2; display: none;
                       background: $success; color: black; text-style: bold; border: solid $success; }
    .cfg-inline-save:hover { background: $success-lighten-1; border: solid $success-lighten-1; }
    ConfigScreen Input { margin-top: 1; }
    #cfg-msg { color: $text-error; height: auto; }
    #cfg-actions { height: auto; margin-top: 1; }
    #cfg-spacer { width: 1fr; }
    .cfg-cancel { width: auto; padding: 0 3; background: $error; color: black; text-style: bold; border: solid $error; }
    .cfg-cancel:hover { background: $error-lighten-1; border: solid $error-lighten-1; }
    #cfg-test { width: auto; padding: 0 3; margin-right: 2; color: black; background: $primary; border: solid $primary; text-style: bold; display: none; }
    #cfg-test:hover { background: $primary-lighten-1; border: solid $primary-lighten-1; }
    #cfg-ok { width: auto; padding: 0 3; background: $success; color: black; text-style: bold; border: solid $success; }
    #cfg-ok:hover { background: $success-lighten-1; border: solid $success-lighten-1; }
    .cfg-lbl { color: $text-muted; }
    #cfg-think { height: auto; margin-top: 1; display: none; }
    #cfg-think-hint { color: $text-muted; }
    #cfg-think-opts { height: auto; margin-top: 1; }
    .cfg-opt { width: auto; padding: 0 1; margin-right: 1; color: $text; border: solid $border-blurred; }
    .cfg-opt.-on { background: $accent; color: black; text-style: bold; border: solid $accent; }
    .cfg-opt:hover { background: white 15%; }
    #cfg-think-auto { height: auto; margin-top: 1; display: none; }
    .cfg-auto-msg { color: $success; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tab = "preset"
        self._models = [(p, m) for p in PROVIDERS for m in p.models]   # 扁平 (provider, model)
        # manual 模式下未知模型的思维链保留方式。默认跟 registry.INERT 一致（工具调用回合保留）；
        # 显式关掉存哨兵 "none" 而不是 ""——空串同时也是"从没设过"，两者混一起的话，改了默认之后
        # 用户点的"不保留"会落回默认，等于开关点了没反应（见 provider.__init__）。
        self._keep_reasoning = "tool_calls"
        self._cap_target: tuple[str, str] | None = None   # 上限框此刻指向哪个后端（None=当前后端）
        self._cap_base = ""                                # 该后端存着的上限（保存钮的比对基准）
        self._add_sub = "preset"    # 新增页当前停留的子页（一键/手动）；切回"新增"时恢复
        migrate_context_cap()                 # 老配置：顶层的全局上限抄进各条（幂等），否则切一次就没了
        self._saved = self._collect_saved()   # 已配置过的后端（只读 config.json）
        self._env: dict = {}        # .env 检测到且 config 没有的后端（on_mount 填；"点此加载"用）

    @staticmethod
    def _collect_saved() -> list[dict]:
        """切换页列表【只以 saved 列表为源】（每次保存都 upsert 进去，当前的必在）；顶层当前后端只用来
        给对应条目标注 [当前]、不作为独立来源——否则删掉当前那条后，它会从顶层字段被重新捞回列表，
        表现成"删不掉"。删当前条目：列表里消失，但顶层字段保留、当前连接不受影响。
        .env 不进列表——单独一行"点此加载"导入。"""
        cfg = load_user_config()
        cur = (cfg.get("base_url"), cfg.get("model"))
        return [{**e, "_source": "当前" if (e.get("base_url"), e.get("model")) == cur else ""}
                for e in saved_configs() if e.get("model")]

    def compose(self) -> ComposeResult:
        with Vertical(id="cfg"):
            with Horizontal(id="cfg-head"):
                yield Static("模型管理", id="cfg-title")
                yield _CfgClick("✕", "action_cancel", id="cfg-x")
            with Horizontal(id="cfg-tabs"):
                yield _CfgClick("新增模型", "_show_add", classes="cfg-tab -on")
                yield _CfgClick("切换模型", "_show_saved", classes="cfg-tab")
            with Horizontal(id="cfg-subtabs"):
                yield _CfgClick("一键选择", "_show_preset", classes="cfg-subtab -on")
                yield _CfgClick("手动填写", "_show_manual", classes="cfg-subtab")
            with Vertical(id="cfg-body"):
                with Vertical(id="cfg-saved"):
                    yield Static("点选 / Enter 一个已配置过的模型，立即切换（✕ 删除该条）：", classes="cfg-lbl")
                    yield ListView(id="cfg-saved-list")
                    yield Static("", id="cfg-saved-empty", classes="cfg-lbl")
                    yield _CfgClick("", "_import_env", id="cfg-envimport")
                with Vertical(id="cfg-preset"):
                    yield Static("选模型（↑↓ / 点选）· 名后为最高支持上下文：", classes="cfg-lbl")
                    yield ListView(id="cfg-models")
                    yield Static("", id="cfg-keyhint")
                    yield Input(placeholder="粘贴 API Key", password=True, id="cfg-key")
                with Vertical(id="cfg-manual"):
                    yield Input(placeholder="base_url，如 https://api.deepseek.com", id="cfg-base")
                    yield Input(placeholder="model，如 deepseek-v4-flash", id="cfg-model")
                    yield Input(placeholder="API Key", password=True, id="cfg-mkey")
                    with Vertical(id="cfg-think"):
                        yield Static("该模型不在内置列表，请选择思维链在上下文中的保留方式：", id="cfg-think-hint", classes="cfg-lbl")
                        # 默认选中"工具调用时保留"：多数模型现在默认就在思考，思考链也收得到，
                        # 剥掉等于每轮让它丢失自己上一步的推理（同 registry.INERT 的默认）。
                        with Horizontal(id="cfg-think-opts"):
                            yield _CfgClick("不保留", "_set_keep_none", classes="cfg-opt")
                            yield _CfgClick("全部保留", "_set_keep_all", classes="cfg-opt")
                            yield _CfgClick("工具调用时保留", "_set_keep_tool", classes="cfg-opt -on")
                    with Vertical(id="cfg-think-auto"):
                        yield Static("", id="cfg-think-auto-text", classes="cfg-auto-msg")
            with Vertical(id="cfg-adv"):
                # 两项各配一个【行内保存】按钮：改了值才出现，点它只落这一项、不切后端、不关窗。
                # 否则想改个阈值也得走"确定并保存"，那条路是按"新增/切换模型"设计的（会热切后端）。
                yield Static("该模型的有效上下文上限（token，越低越省/压得越勤；不填则默认 128000）：",
                             classes="cfg-lbl")
                with Horizontal(classes="cfg-row"):
                    yield Input(placeholder=str(_CONTEXT_CAP_DEFAULT), id="cfg-cap")
                    yield _CfgClick("保存", "_save_cap", id="cfg-cap-save", classes="cfg-inline-save")
                yield Static("压缩触发阈值（占上限几成就压，0~1；不填则默认 0.7）：", classes="cfg-lbl")
                with Horizontal(classes="cfg-row"):
                    yield Input(placeholder="0.7", id="cfg-thresh")
                    yield _CfgClick("保存", "_save_thresh", id="cfg-thresh-save",
                                    classes="cfg-inline-save")
            yield Static("", id="cfg-msg")
            with Horizontal(id="cfg-actions"):
                yield _CfgClick("取消", "action_cancel", classes="cfg-cancel")
                yield Static("", id="cfg-spacer")
                yield _CfgClick("测试连接", "_test_backend", id="cfg-test")
                yield _CfgClick("确定并保存", "_confirm", id="cfg-ok")

    def on_mount(self) -> None:
        lv = self.query_one("#cfg-models", ListView)
        for p, m in self._models:
            win = _fmt_window(m.context_window)
            spans = [(f"{p.name} · ", "dim"), (m.id, "bold cyan")]
            if win:
                spans.append((f"   最高 {win}", "dim"))
            lv.append(ListItem(Static(Text.assemble(*spans))))
        lv.index = 0
        self._fill_saved_list()
        self.query_one("#cfg-manual").display = False
        self.query_one("#cfg-saved").display = False
        self._update_keyhint(0)
        # .env 检测：配了模型且 config 里还没有这条 → 切换页给一行"点此加载"（点击跳手动页预填）
        env = env_backend()
        if env and (env["base_url"], env["model"]) not in {
                (e.get("base_url"), e.get("model")) for e in self._saved}:
            self._env = env
            imp = self.query_one("#cfg-envimport", Static)
            imp.update(Text.assemble(("⚡ 检测到 .env 中有 ", ""), (env["model"], "bold"),
                                     (" 模型，", ""), ("点此加载", "bold underline cyan")))
            imp.display = True
        if len(self._saved) >= 2 or self._env:   # 存了俩以上 / .env 有待导入模型 → 直接落切换页
            self._set_tab("saved")
        cfg = load_user_config()                             # 高级项：已存过就回填当前值，否则留 placeholder
        if cfg.get("compact_threshold"):
            self.query_one("#cfg-thresh", Input).value = str(cfg["compact_threshold"])
        # 上限框指向当前后端（切到切换页时会改指高亮那一条）；同时置好保存钮的基准
        self._load_cap_box()

    def _fill_saved_list(self) -> None:
        """填/重填切换页列表（只读 config.json）。每行 = 模型名+窗口（撑满）+ 右侧 ✕ 删除。"""
        slv = self.query_one("#cfg-saved-list", ListView)
        slv.clear()
        for i, e in enumerate(self._saved):
            win = _fmt_window(context_window_for(e["model"]))
            spans = [(e["model"], "bold cyan")]
            if e.get("_source"):
                spans.insert(0, (f"[{e['_source']}] ", "yellow"))
            if win:
                spans.append((f"   最高 {win}", "dim"))
            row = Horizontal(_SavedLbl(Text.assemble(*spans), i),
                             _SavedDel(e), classes="cfg-saved-row")
            slv.append(ListItem(row))
        empty = self.query_one("#cfg-saved-empty", Static)
        if self._saved:
            slv.index = 0
            empty.update("")
        else:
            empty.update("（还没有已保存的模型；先在「新增模型」页配置一个）")
        # 空文案时整个收掉：内容高度是 0，但 margin-top 照占一行，白留一条缝
        empty.display = not self._saved

    def _delete_saved(self, entry: dict) -> None:
        """删一条已保存配置（config.json saved 列表），当场重填列表。"""
        delete_saved(entry.get("base_url", ""), entry.get("model", ""))
        self._saved = self._collect_saved()
        self._fill_saved_list()

    def _import_env(self) -> None:
        """.env 导入行：跳手动填写页，预填 .env 里有的字段（base_url/model/api_key），缺的用户自己补。
        走正常"确定并保存"落 config.json → 从此统一以 config 为准。"""
        self._set_tab("manual")
        for field, wid in (("base_url", "#cfg-base"), ("model", "#cfg-model"), ("api_key", "#cfg-mkey")):
            if self._env.get(field):
                self.query_one(wid, Input).value = self._env[field]

    def _show_add(self) -> None:
        self._set_tab(self._add_sub)         # 回到新增页上次停留的子页

    def _show_saved(self) -> None:
        self._set_tab("saved")

    def _show_preset(self) -> None:
        self._set_tab("preset")

    def _show_manual(self) -> None:
        self._set_tab("manual")

    def _set_tab(self, tab: str) -> None:
        """两级菜单：顶层 新增(preset/manual 二选一) / 切换(saved)。子 tab 行只在新增页可见
        （visibility 隐藏占位不塌，切顶层高度不跳）。"""
        self._tab = tab
        if tab in ("preset", "manual"):
            self._add_sub = tab
        for name in ("preset", "manual", "saved"):
            self.query_one(f"#cfg-{name}").display = tab == name
        tabs = list(self.query(".cfg-tab"))
        tabs[0].set_class(tab != "saved", "-on")
        tabs[1].set_class(tab == "saved", "-on")
        subs = list(self.query(".cfg-subtab"))
        subs[0].set_class(tab == "preset", "-on")
        subs[1].set_class(tab == "manual", "-on")
        self.query_one("#cfg-subtabs").visible = tab != "saved"
        self._refresh_test_visible()      # 每页"信息齐没齐"标准不同，切页重判

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        if event.list_view.id == "cfg-models":                       # keyhint 只跟一键页的模型列表
            self._update_keyhint(idx)
        elif event.list_view.id == "cfg-saved-list" and idx < len(self._saved):
            self._load_cap_box(self._saved[idx])   # 上限框跟着高亮那一条走（保存也写给它）

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """切换页列表【Enter / 点选】即确定切换（选中就是意图，别再点一次按钮）。"""
        if event.list_view.id == "cfg-saved-list":
            self._confirm()

    def _update_keyhint(self, idx: int) -> None:
        p = self._models[idx][0]
        self.query_one("#cfg-keyhint", Static).update(
            Text.assemble((f"② {p.key_help}\n   ", "dim"), (p.key_url, "cyan underline")))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cfg-model":
            self._check_model(event.value.strip())
        if event.input.id in ("cfg-cap", "cfg-thresh"):
            self._refresh_inline_save()
        self._refresh_test_visible()

    def _load_cap_box(self, entry: dict | None = None) -> None:
        """把上限框指向某个后端并填上它的值。entry=None → 当前后端（顶层那份）。
        **框显示谁的，保存就写给谁** —— 两者必须同源，否则会出现"看着是 A 的值、存到了 B 头上"
        （真出过：框跟着高亮走、保存却写当前后端，一比对基准就错位，没改也冒保存钮）。"""
        self._cap_target = (entry["base_url"], entry["model"]) if entry else None
        val = str((entry or load_user_config()).get("context_cap") or "")
        self._cap_base = val
        self.query_one("#cfg-cap", Input).value = val
        self._refresh_inline_save()

    def _refresh_inline_save(self) -> None:
        """值和基准不一样才显示保存钮——没改就别摆个按钮在那儿让人以为没保存。"""
        th_s = str(load_user_config().get("compact_threshold") or "")
        self.query_one("#cfg-cap-save").display = \
            self.query_one("#cfg-cap", Input).value.strip() != self._cap_base
        self.query_one("#cfg-thresh-save").display = \
            self.query_one("#cfg-thresh", Input).value.strip() != th_s

    def _save_cap(self) -> None:
        self._save_adv(cap_only=True)

    def _save_thresh(self) -> None:
        self._save_adv(cap_only=False)

    def _save_adv(self, *, cap_only: bool) -> None:
        """行内保存：只落这一项，不动后端三元组、不切后端、不关窗。
        上限跟【当前后端条目】走（见 config.update_settings）；阈值是全局偏好。"""
        cap, thresh = self._parse_adv()
        if _INVALID in (cap, thresh):
            return    # _parse_adv 已报错（两项一起校验：另一项填错了也该先让用户看见）
        if cap_only:
            update_settings(context_cap=cap, for_backend=self._cap_target)
            self._cap_base = str(cap or "")
            self._saved = self._collect_saved()      # 条目里的值变了，列表数据源跟着刷
        else:
            update_settings(compact_threshold=thresh)
        self._refresh_inline_save()
        app = self.app
        if hasattr(app, "_apply_settings_change"):
            app._apply_settings_change()          # 让正在跑的 agent 立刻用上新值，不必重启/切后端
        self.query_one("#cfg-msg", Static).update(
            Text("✓ 已保存" + ("上下文上限" if cap_only else "压缩阈值"), style="green"))

    def _refresh_test_visible(self) -> None:
        """"测试连接"按钮只在当前页信息齐了才显示：一键=填了 key；手动=三项全填；切换=有可选条目。"""
        if self._tab == "preset":
            ready = bool(self.query_one("#cfg-key", Input).value.strip())
        elif self._tab == "manual":
            ready = all(self.query_one(w, Input).value.strip()
                        for w in ("#cfg-base", "#cfg-model", "#cfg-mkey"))
        else:
            ready = bool(self._saved)
        self.query_one("#cfg-test").display = ready

    def _check_model(self, model: str) -> None:
        """检测手动填写的 model 是否在已知列表，决定显不显思维链选项。"""
        from mecode.registry import _BY_MODEL
        think = self.query_one("#cfg-think")
        think_auto = self.query_one("#cfg-think-auto")
        auto_text = self.query_one("#cfg-think-auto-text", Static)
        if not model:
            think.display = False
            think_auto.display = False
            return
        if model in _BY_MODEL:
            profile = _BY_MODEL[model]
            think.display = False
            think_auto.display = True
            tiers = f"，深度档：{profile.effort_tiers}" if profile.effort_tiers else ""
            toggle = "可开关" if profile.toggleable else "强制开"
            auto_text.update(f"已自动识别：{model} · 思考 {toggle}{tiers} · 思维链保留：{profile.keep_reasoning or '不保留'}")
            self._keep_reasoning = profile.keep_reasoning
        else:
            think.display = True
            think_auto.display = False

    def _set_keep_none(self) -> None:
        self._keep_reasoning = "none"     # 哨兵：显式关掉（"" 会被当成"没设过"、吃默认）
        self._update_keep_opt(0)

    def _set_keep_all(self) -> None:
        self._keep_reasoning = "all"
        self._update_keep_opt(1)

    def _set_keep_tool(self) -> None:
        self._keep_reasoning = "tool_calls"
        self._update_keep_opt(2)

    def _update_keep_opt(self, idx: int) -> None:
        opts = list(self.query(".cfg-opt"))
        for i, opt in enumerate(opts):
            opt.set_class(i == idx, "-on")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()          # 输入框里回车 = 确定

    def _err(self, msg: str) -> None:
        self.query_one("#cfg-msg", Static).update(Text(msg, style="red"))

    def _confirm(self) -> None:
        got = self._resolve_backend()
        if got is None:
            return    # _resolve_backend 已报错
        base, model, key = got
        cap, thresh = self._parse_adv()
        if _INVALID in (cap, thresh):
            return    # _parse_adv 已报错
        if self._tab == "saved":
            e = self._saved[self.query_one("#cfg-saved-list", ListView).index]
            keep = e.get("keep_reasoning", "")
            # 切换页用【那条自己的上限】，不看输入框——框里装的是"当前后端"的值（配行内保存钮用），
            # 拿它去存另一个后端就是把当前后端的设置抄到别人头上。
            # 没单独设过就是没设过（0 = 回默认）。老配置"上限只在顶层"的情况由 migrate_context_cap
            # 在开窗时一次性抄进各条，不在这里沿用当前值——那会把一个值传染给切过的每一条。
            cap = int(e.get("context_cap") or 0)
        elif self._tab == "preset":
            keep = self._models[self.query_one("#cfg-models", ListView).index][1].profile.keep_reasoning
        else:
            keep = self._keep_reasoning
        from_add = self._tab in ("preset", "manual")
        save_user_config(base, model, key, keep_reasoning=keep,
                         context_cap=cap, compact_threshold=thresh)
        if from_add:
            # 新增页保存：不关窗——跳切换页，让用户看到刚添加的模型已进列表（并已成为当前）。
            # 热切直接通知 App（不走 dismiss 回调），窗留着用户自己关/继续操作。
            self._saved = self._collect_saved()
            self._fill_saved_list()
            self._set_tab("saved")
            self.query_one("#cfg-msg", Static).update(Text(f"✓ 已保存并切换到 {model}", style="green"))
            app = self.app
            if hasattr(app, "_handle_config_saved"):
                app._handle_config_saved(model)
        else:
            self.dismiss(model)      # 切换页确定：关窗，App 回调里热切（跑轮中则排到轮后）

    def _resolve_backend(self) -> tuple[str, str, str] | None:
        """按当前 tab 取"要用的后端三元组"（不落盘）；不完整则报错返回 None。_confirm/_test_backend 共用。"""
        if self._tab == "saved":
            idx = self.query_one("#cfg-saved-list", ListView).index
            if idx is None or not self._saved:
                self._err("没有可用的已保存后端")
                return None
            e = self._saved[idx]
            return e["base_url"], e["model"], e.get("api_key", "")
        if self._tab == "preset":
            idx = self.query_one("#cfg-models", ListView).index
            if idx is None:
                self._err("请先选一个模型")
                return None
            p, m = self._models[idx]
            key = self.query_one("#cfg-key", Input).value.strip()
            if not key:
                self._err("请粘贴 API Key")
                return None
            return p.base_url, m.id, key
        base = self.query_one("#cfg-base", Input).value.strip()
        model = self.query_one("#cfg-model", Input).value.strip()
        key = self.query_one("#cfg-mkey", Input).value.strip()
        if not (base and model and key):
            self._err("base_url / model / API Key 三项都要填")
            return None
        return base, model, key

    def _test_backend(self) -> None:
        """测试连接：拿当前选中/填写的后端发一次 1 token 补全（后台线程，不卡 UI）。
        保存前就能发现 key 错/网络不通/模型名不存在。"""
        got = self._resolve_backend()
        if got is None:
            return
        base, model, key = got
        msg = self.query_one("#cfg-msg", Static)
        msg.update(Text("… 正在连接测试", style="yellow"))

        def probe() -> None:
            import httpx
            try:
                r = httpx.post(f"{base}/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"model": model, "max_tokens": 1,
                                     "messages": [{"role": "user", "content": "hi"}]},
                               timeout=15)
                if r.status_code < 400:
                    out, style = f"✓ 连接成功（{model}）", "green"
                else:
                    detail = ""
                    try:
                        detail = (r.json().get("error", {}) or {}).get("message", "")[:80]
                    except Exception:
                        pass
                    out, style = f"✗ HTTP {r.status_code} {detail}", "red"
            except Exception as exc:
                out, style = f"✗ 连接失败：{type(exc).__name__} {str(exc)[:80]}", "red"
            self.app.call_from_thread(msg.update, Text(out, style=style))

        threading.Thread(target=probe, daemon=True).start()

    def _parse_adv(self):
        """解析高级项（上下文上限 / 压缩阈值）：空=不写(用默认)，返回 0；非法则报错返回 _INVALID。"""
        cap_s = self.query_one("#cfg-cap", Input).value.strip()
        thresh_s = self.query_one("#cfg-thresh", Input).value.strip()
        cap = 0
        if cap_s:
            if not cap_s.isdigit() or int(cap_s) < 1000:
                self._err("上下文上限要是 ≥1000 的整数")
                return _INVALID, _INVALID
            cap = int(cap_s)
        thresh = 0.0
        if thresh_s:
            try:
                thresh = float(thresh_s)
            except ValueError:
                self._err("压缩阈值要是 0~1 之间的小数")
                return _INVALID, _INVALID
            if not 0.0 < thresh < 1.0:
                self._err("压缩阈值要在 0~1 之间（不含端点）")
                return _INVALID, _INVALID
        return cap, thresh

    def action_cancel(self) -> None:
        self.dismiss(None)


class _SkillLbl(Static):
    """技能面板列表行的标签：点击只移高亮（event.stop() 拦冒泡，防 ListItem 触发 Selected 直接使用）。
    使用只走 Enter / [使用] 按钮——和 ConfigScreen 切换页同一交互约定。"""
    def __init__(self, text, idx: int) -> None:
        super().__init__(text, classes="sk-lbl")
        self._idx = idx

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.screen.query_one("#sk-list", ListView).index = self._idx


class _SkillToggle(Static):
    """技能行右侧的状态 chip（已启用/已停用），点击切换（写状态文件，新会话生效）。
    event.stop() 防冒泡成选中使用。"""
    def __init__(self, idx: int, enabled: bool) -> None:
        super().__init__("已启用" if enabled else "已停用",
                         classes="sk-toggle" + ("" if enabled else " -off"))
        self._idx = idx

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.screen._toggle(self._idx)


class SkillScreen(ModalScreen):
    """/skill 面板：列出所有已发现的技能（项目/用户/内置）+ 启停状态。
    - ↑↓/点击选行，Enter / [使用] = 把该技能全文以 <system-reminder> 注入并起一轮（一次性，无"使用中"态）
    - 行右侧 [停用]/[启用] = 写状态文件，【新会话生效】（当前会话 system prompt 不动，不击穿缓存）
    dismiss 返回选中的 Skill（取消/仅改启停则 None）。"""
    BINDINGS = [Binding("escape", "cancel", "取消")]
    CSS = """
    SkillScreen { align: center middle; background: $background 70%; }
    #sk { width: 80; height: auto; max-height: 85%; padding: 1 2; border: round $accent; background: $surface; }
    #sk-head { height: 1; margin-bottom: 1; }
    #sk-title { width: 1fr; text-style: bold; }
    #sk-x { width: auto; padding: 0 1; color: $text-muted; }
    #sk-x:hover { background: #7a1f1f; color: white; }
    #sk-list { height: auto; max-height: 16; border: round $panel; }
    .sk-row { height: auto; }
    .sk-lbl { width: 1fr; }
    /* 状态 chip：已启用=绿字提示在用、已停用=灰字；点击即切换（hover 提亮示意可点） */
    .sk-toggle { width: auto; padding: 0 1; color: $success; background: white 8%; }
    .sk-toggle.-off { color: $text-muted; }
    .sk-toggle:hover { background: white 25%; }
    #sk-msg { height: auto; margin-top: 1; color: $warning; }
    #sk-empty { height: auto; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._skills = discover_skills(Path.cwd())

    def compose(self) -> ComposeResult:
        with Vertical(id="sk"):
            with Horizontal(id="sk-head"):
                yield Static("技能  ↑↓ 选 · Enter 使用 · Esc 关闭", id="sk-title")
                yield _CfgClick("✕", "action_cancel", id="sk-x")
            yield ListView(id="sk-list")
            yield Static("", id="sk-empty")
            yield Static("", id="sk-msg")

    def on_mount(self) -> None:
        self._fill()

    def _fill(self) -> None:
        lv = self.query_one("#sk-list", ListView)
        lv.clear()
        for i, s in enumerate(self._skills):
            # 启用=正常配色；停用=整行置灰（状态另有右侧 chip 显示，行内不再重复文字）
            if s.enabled:
                spans = [(s.name, "bold cyan"), (f"  [{s.source}]", "yellow"),
                         ("\n  " + _clip(s.description, 64), "dim")]
            else:
                spans = [(s.name, "bold dim"), (f"  [{s.source}]", "dim"),
                         ("\n  " + _clip(s.description, 64), "dim")]
            row = Horizontal(_SkillLbl(Text.assemble(*spans), i),
                             _SkillToggle(i, s.enabled), classes="sk-row")
            lv.append(ListItem(row))
        if self._skills:
            lv.index = 0
            lv.focus()
        else:
            self.query_one("#sk-empty", Static).update(
                "（还没有技能。放一个 <项目>/.mecode/skills/<名>/SKILL.md 或 ~/.mecode/skills/<名>/SKILL.md，"
                "frontmatter 带 name + description，正文写完整流程。）")

    def _toggle(self, idx: int) -> None:
        s = self._skills[idx]
        set_enabled(s.name, not s.enabled)
        self._skills = discover_skills(Path.cwd())     # 重读（带新状态）
        self._fill()
        word = "已停用" if s.enabled else "已启用"
        self.query_one("#sk-msg", Static).update(
            f"{word}「{s.name}」——索引变更【新会话生效】（当前会话模型看到的技能列表不变）")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or not self._skills:
            return
        s = self._skills[idx]
        if not s.enabled:
            self.query_one("#sk-msg", Static).update(f"「{s.name}」已停用——先点右侧 [启用] 再使用")
            return
        self.dismiss(s)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _McpToggle(Static):
    """MCP 行右侧的状态 chip（已启用/已停用），点击切换（写状态文件，新会话生效）。"""
    def __init__(self, name: str, enabled: bool) -> None:
        super().__init__("已启用" if enabled else "已停用",
                         classes="mcp-toggle" + ("" if enabled else " -off"))
        self._name = name

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.screen._toggle(self._name)


class _McpTimeout(Static):
    """MCP 行的握手超时 chip（超时 Ns），点击循环预设 15/30/60/120（写状态文件，新会话生效）。
    重型 server（下 Chromium、首次 uvx 下包）连不上多半是默认 15s 不够 → 点大一点。"""
    def __init__(self, name: str, seconds: int) -> None:
        super().__init__(f"超时 {seconds}s", classes="mcp-timeout")
        self._name = name

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.screen._cycle_timeout(self._name)


class MCPScreen(ModalScreen):
    """MCP 管理面板：列配置里的【全部】server（含没连上的）+ 本会话连接状态 + 启停 chip。
    启停同 skill 规矩：写 ~/.mecode/mcp_state.json 禁用名单，【新会话生效】（server 是活进程，
    不做运行时热开关——停用=下次启动不连；当前会话已连的照常可用）。纯管理，无"选中使用"概念。"""
    BINDINGS = [Binding("escape", "cancel", "关闭")]
    CSS = """
    MCPScreen { align: center middle; background: $background 70%; }
    #mcp { width: 80; height: auto; max-height: 85%; padding: 1 2; border: round $accent; background: $surface; }
    #mcp-head { height: 1; margin-bottom: 1; }
    #mcp-title { width: 1fr; text-style: bold; }
    #mcp-x { width: auto; padding: 0 1; color: $text-muted; }
    #mcp-x:hover { background: #7a1f1f; color: white; }
    #mcp-list { height: auto; max-height: 16; }
    .mcp-row { height: auto; margin-bottom: 1; }
    .mcp-lbl { width: 1fr; }
    .mcp-toggle { width: auto; padding: 0 1; color: $success; background: white 8%; }
    .mcp-toggle.-off { color: $text-muted; }
    .mcp-toggle:hover { background: white 25%; }
    .mcp-timeout { width: auto; padding: 0 1; margin-right: 1; color: $text-muted; background: white 8%; }
    .mcp-timeout:hover { background: white 25%; }
    #mcp-msg { height: auto; margin-top: 1; color: $warning; }
    #mcp-empty { height: auto; color: $text-muted; }
    """

    def __init__(self, servers: dict, connected: set[str], errors: dict | None = None) -> None:
        super().__init__()
        self._servers = servers          # 配置里的全部 server（load_mcp_config 的结果）
        self._connected = connected      # 本会话已连上的名字
        self._errors = errors or {}      # 没连上的 server → 具体失败原因（超时/命令找不到/server 报错）

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp"):
            with Horizontal(id="mcp-head"):
                yield Static("MCP server 管理  (Esc 关闭)", id="mcp-title")
                yield _CfgClick("✕", "action_cancel", id="mcp-x")
            yield Vertical(id="mcp-list")
            yield Static("", id="mcp-empty")
            yield Static("", id="mcp-msg")

    def on_mount(self) -> None:
        self._fill()

    def _fill(self) -> None:
        box = self.query_one("#mcp-list", Vertical)
        box.remove_children()
        states = server_states(self._servers)
        for st in states:
            # 本会话状态：已连=绿点 / 启用但没连上=黄（起失败或还在连）/ 停用=灰
            if st["name"] in self._connected:
                dot, dstyle = "●", "green"
                note = "本会话已连接"
            elif st["enabled"]:
                # 启用但没连上：有具体失败原因就显原因（红），否则还在连/未捕获原因（黄）
                reason = self._errors.get(st["name"])
                dot, dstyle = "●", ("red" if reason else "yellow")
                note = f"连接失败：{reason}" if reason else "本会话未连接（连接中或启动失败）"
            else:
                dot, dstyle = "○", "dim"
                note = "已停用（启动时跳过）"
            name_style = "bold cyan" if st["enabled"] else "bold dim"
            spans = [(f"{dot} ", dstyle), (st["name"], name_style),
                     ("\n   " + _clip(st["command"], 56), "dim"),
                     ("\n   " + note, "dim")]
            row = Horizontal(Static(Text.assemble(*spans), classes="mcp-lbl"),
                             _McpTimeout(st["name"], st["timeout"]),
                             _McpToggle(st["name"], st["enabled"]), classes="mcp-row")
            box.mount(row)
        self.query_one("#mcp-empty", Static).update(
            "" if states else
            "（没有配置任何 MCP server。配置写 <项目>/.mcp.json 或 ~/.mecode/mcp.json，"
            "格式见 mcp-install 技能；也可以直接把 MCP 链接发给模型让它装。）")

    def _toggle(self, name: str) -> None:
        st = {s["name"]: s for s in server_states(self._servers)}[name]
        set_server_enabled(name, not st["enabled"])
        self._fill()
        word = "已停用" if st["enabled"] else "已启用"
        extra = "（本会话已连的照常可用，下次启动不再连接）" if st["enabled"] and name in self._connected \
            else "——重启 mecode 后生效"
        self.query_one("#mcp-msg", Static).update(f"{word}「{name}」{extra}")

    def _cycle_timeout(self, name: str) -> None:
        new = cycle_server_timeout(name)                 # 切下一档预设并落盘
        self._fill()
        self.query_one("#mcp-msg", Static).update(
            f"「{name}」握手超时改为 {new}s——重启 mecode 后生效")

    def action_cancel(self) -> None:
        self.dismiss(None)


class MecodeApp(App):
    CSS = """
    #status  { height: 1; background: $panel; }   /* 整行底色恒定，不随 hover 变 */
    #mode-chip { width: auto; padding: 0 1; text-style: bold; color: black; }   /* 模式按钮：彩底黑字；底色/hover 由 -m-<mode> 类控（见 CSS 末尾拼入的 _mode_chip_css）。只有它 hover、只有它可点 */
    #status-info { width: 1fr; color: $text-muted; padding: 0 1; }
    #body    { height: 1fr; }
    #main    { width: 1fr; }
    #current-msg { display: none; height: auto; max-height: 3; padding: 0 1; background: $boost; }
    #log     { height: 1fr; padding: 0 1; margin: 1 0; }   /* 上下各留 1 行 → 滚动条不贴顶/底边界、上下等距 */
    #sidebar { width: 28; height: 1fr; padding: 0 1; }
    /* 信息区里的 @click 链接（MCP 管理/未配置引导）：textual 会给带 @click 的 span 叠加 link-style
       （默认下划线，盖过 Style 里的 underline=False）→ 在 widget 级把 link 样式改成无下划线。
       颜色用 $warning：rich 直写的 "yellow" 是 ANSI 黄，SkillPanel 的 style="yellow" 经 textual
       主题解析成的是橙（两个"yellow"不是一个色）——统一走主题色和技能区一致 */
    #sb-info { link-style: none; link-color: $warning; link-background: transparent; }
    #sb-info { link-style-hover: bold; link-color-hover: $warning; link-background-hover: transparent; }
    #sb-todos { display: none; margin-top: 1; }   /* 任务清单：空则隐藏（refresh_tasks 切 display），与上方信息区留一行 */
    #sb-workflow { display: none; margin-top: 1; }   /* workflow 分支树：空闲隐藏（refresh_workflow 切 display） */
    /* 任务清单/workflow 树【完成后可点击收起】：-clickable 时 hover 高亮提示可点（同 ToolsPanel 约定） */
    TodoPanel.-clickable:hover     { background: $block-hover-background; }
    WorkflowPanel.-clickable:hover { background: $block-hover-background; }
    #sb-skills { margin-top: 1; }   /* 技能区与上方（server 行/信息区）留一行；始终常驻（含无启用技能时） */
    #sb-skills.-clickable:hover { background: $block-hover-background; }   /* >4 可折叠时整块 hover 高亮（同 ToolsPanel） */
    /* 技能区"管理"@click 链接：同 #sb-info——去掉 textual 给链接叠的默认下划线、统一走 $warning 主题色 */
    #sb-skills { link-style: none; link-color: $warning; link-background: transparent; }
    #sb-skills { link-style-hover: bold; link-color-hover: $warning; link-background-hover: transparent; }
    #sb-tools { margin-top: 1; }   /* 技能区与“工具”区之间留一行 */
    /* #main 与 #sidebar 之间的可拖动分割线（竖条），hover 高亮提示可拖 */
    #splitter        { width: 1; height: 1fr; background: $panel; }
    #splitter:hover  { background: $primary; }
    #work    { height: 1; padding: 0 3 0 1; }   /* 右留 3：让快捷键提示缩到 #log 滚动条左侧、和对话内容对齐 */
    /* 边框用 round（细单线+圆角），不要默认 tall 那种左右实心粗块；聚焦变亮色 */
    #input        { height: auto; max-height: 14; border: round $border-blurred; }
    #input:focus  { border: round $border; }
    /* 工具块：整块一个 hover 高亮目标（标题/框内悬停都触发，标题与框都无 hover 样式 → 冒泡到这）。
       内框有自己的底色，会盖住高亮 → 高亮自然避开内框。 */
    ToolBlock         { height: auto; margin-bottom: 1; }   /* 工具之间留 1 行间距 */
    ToolBlock:hover   { background: $block-hover-background; }
    .tool-head        { width: 1fr; padding: 0 0 0 1; }   /* 标题行铺满整行、左缩进 1（收起触发器） */
    /* 内框四周留 1 格空隙（含下方）：这圈空隙属于 ToolBlock，会随 hover 高亮，内框自己有底色不被染
       → 高亮延伸到内框下方/四周、但避开内框本体 */
    .tool-body        { height: auto; padding: 0 1 1 1; }
    /* 内框：height auto 随内容收缩（VerticalScroll 默认 1fr 会撑满留空白），超 14 行才内部滚动；
       有自己的底色，让整块高亮时它不被染（高亮避开内框） */
    .toolbox { height: auto; max-height: 14; border: round $panel; background: $surface-darken-1; padding: 0 1; }
    /* 思考块（整块一个 widget）和工具面板（可展开/收起）：hover 整块高亮，让点击范围可见 */
    Thinking                         { padding: 0 1; margin-bottom: 1; }   /* 与下一块留 1 行间距 */
    Thinking:hover                   { background: $block-hover-background; }
    ToolsPanel.-clickable:hover      { background: $block-hover-background; }
    ServerPanel.-clickable:hover     { background: $block-hover-background; }
    BatchSummary                     { margin-bottom: 1; }   /* 折叠行与下一块留 1 行间距 */
    BatchSummary.-clickable:hover    { background: $block-hover-background; }   /* 折叠行 hover 高亮，提示可点开 */
    .user-msg                        { margin-bottom: 1; }   /* 用户消息与下一块留 1 行：关掉思考、没有思考块时也不与回复紧贴 */
    /* 运行中后台任务：输入框上方一行一个；空则隐藏（display 在 _sync_bgtasks 里按有无内容切） */
    #bgtasks                  { height: auto; max-height: 12; display: none; }
    BgTaskRow                 { height: auto; }
    .bg-headrow               { height: 1; padding: 0 1; }
    .bg-label                 { width: 1fr; }
    .bg-meta                  { width: auto; padding: 0 1; }   /* 已 Ns，定宽始终可见 */
    /* ✕停止：红底 chip + 内边距，一眼看出是按钮；hover 变实心红 + 加粗 */
    .bg-stop                  { width: auto; padding: 0 1; background: $error 30%; color: $text; }
    .bg-stop:hover            { background: $error; text-style: bold; }
    .bg-label:hover, .bg-meta:hover { background: $block-hover-background; }
    .bg-out                   { height: auto; max-height: 10; padding: 0 1 0 3; color: $text-muted; }
    /* "计划待批准"条：一行；⇆ 和 批准执行 是带背景+hover 的按钮，目标模式是彩色文字 */
    #planbar          { height: auto; display: none; padding: 0 3 0 1; }   /* 右留 3：批准执行按钮缩到 #log 滚动条左侧 */
    PlanBar           { height: 1; }
    .plan-lead        { width: auto; color: $text; text-style: bold; }   /* 亮+粗，别和灰色快捷键提示混 */
    .plan-gap         { width: 1fr; }    /* 撑开：把批准按钮顶到右缘 */
    #plan-switch      { width: auto; padding: 0 1 0 0; }                 /* 【模式】⇆ 整块：可点切换；左无内边距（贴紧"批准后以"，别留大空隙）*/
    #plan-switch:hover{ background: white 20%; }                         /* 整块 hover：明显的灰白高亮，提示可点 */
    #plan-view        { width: auto; color: $accent; text-style: underline; }  /* "查看计划" 链接样式 */
    #plan-view:hover  { color: $text; text-style: underline bold; }
    #plan-approve     { width: auto; padding: 0 1; background: $success; color: $text; text-style: bold; }
    #plan-approve:hover { background: $success-lighten-1; }
    """ + _mode_chip_css()      # 每模式底色 + hover 提亮色，由 MODE_COLORS 生成拼入（单一色源）
    # Ctrl+Q 专管退出。Ctrl+C 只做“中断或复制”，必须 priority=True：textual 默认聚焦的输入框
    # 把 ctrl+c 绑成复制、Screen 又绑了 screen.copy_text，优先级绑定能在“转发给聚焦 widget 之前”
    # 的优先 pass 里先触发，抢过它们——代价是内置复制被一并盖掉，所以复制要在 action 里自己补回。
    ENABLE_COMMAND_PALETTE = False     # 不用命令面板 → 把它默认占的 ctrl+p 让给"开关右侧面板"
    BINDINGS = [
        ("ctrl+q", "quit_safely", "退出"),
        Binding("ctrl+c", "interrupt_or_copy", "打断/复制", priority=True),
        Binding("ctrl+p", "toggle_sidebar", "面板", priority=True),
    ]
    # Tab 切焦点【不】走 app 绑定：TextArea 会把 Tab 当插入制表符，priority 抢占不一定稳 → 各 widget
    # 在自己的按键处理里显式转 action_toggle_focus（InputArea._on_key / HistoryLog.on_key），确定。

    HISTORY_BATCH = 20             # 续会话时每批渲染多少条历史（滚到顶再加载下一批）
    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    IDLE_HINT = "Ctrl+Enter 换行 · Ctrl+C 打断 · Ctrl+Q 退出 · Ctrl+↑↓ 历史 · Shift+Tab 模式 · /help 全部命令 · Ctrl+P 面板"
    CONV_HINT = "对话区 · Ctrl+↑↓ 跳用户消息 · ↑↓ 滚动 · Tab/打字 回输入框 · Ctrl+Q 退出"

    # 本地斜杠命令表【单一真相】：_on_submit 据此分发、/help 据此列清单——新增无参命令只改这里。
    # (别名元组, 说明, 无参 handler 方法名)。/rl /rs 带参数走 _match_resume，单列在 _show_help 里。
    SLASH_COMMANDS = (
        (("/help", "/?"), "列出所有可用斜杠命令", "_show_help"),
        (("/system", "/sys", "/prompt"), "查看当前发给模型的 system prompt（含本模式每轮注入的提示）", "_show_system_prompt"),
        (("/tool", "/tools"), "列出当前上下文里模型能调的所有工具（含 MCP）", "_show_tools"),
        (("/config", "/model"), "配置/切换模型后端（保存写 config.json，支持热切）", "_open_config"),
        (("/skill", "/skills"), "技能面板：选中使用 / 启停管理", "_open_skills"),
        (("/mcp",), "MCP server 管理面板：启停、握手超时、失败原因", "_open_mcp"),
    )

    def __init__(self, agent: Agent | None = None) -> None:
        super().__init__()
        # 审批跨线程桥：worker 线程跑 run_turn，遇 ask 类工具 → post AskPermission + 阻塞等本 Event；
        # UI 线程弹窗、用户选完把结果填进 _perm_result 再 set，唤醒 worker。
        self._perm_event = threading.Event()
        self._perm_result = "deny"
        # 审批是【一次一个】：前台子 agent 并发跑（各自一个 Agent、共用这一个回调），没有这把锁的话
        # 两个线程会同时 clear/post，用户答一次把两边一起唤醒、读到同一个结果（串台）。加锁后排队弹。
        self._perm_lock = threading.Lock()
        self._perm_seq = 0              # 审批请求序号：被放弃那张窗的迟到答复据此丢弃，不串给下一个等待者
        self._perm_screen_seq = None    # 【屏上那张窗】的序号。收窗要认它，不能认全局计数器——
        # worker A 被打断发了 Dismiss(1) 后就放开锁，worker B 立刻把计数器推到 2 再弹新窗；
        # UI 这时才处理 Dismiss(1)，拿 1 跟 2 比对不上 → 窗 1 收不掉，变成幽灵留在屏上。
        self._perm_waiting = False      # 有 worker 正等审批窗？轮末清理据此避免误杀（见 ev is None 分支）
        self._ask_seq = 0               # 同款序号，给 ask_user 那张窗（迟到答复不串给下一次提问）
        self._ask_screen_seq = None
        self._ask_waiting = False
        # 提问跨线程桥（ask_user 工具）：同款机制——worker post AskQuestion + 阻塞等 Event，UI 弹窗回填。
        self._ask_event = threading.Event()
        self._ask_result: dict | None = None
        self._mcp_clients = []          # 已连 MCP 客户端（退出时 stop）
        self._mcp_errors = {}           # 没连上的 server → 失败原因（侧栏标红计数、/mcp 面板显详情）
        self._mcp_status = "未接入"
        self._mcp_servers = {}          # 配置里的 server（on_mount 后台连）
        self._server_panels = []        # 每个已连 server 的可展开行（连好后挂进侧栏，重连前清掉）
        self._bg_rows = {}              # 运行中后台任务的显示行：task_id -> BgTaskRow（_sync_bgtasks 维护）
        self._panel_text_width = 25     # 侧栏文本可用宽（已扣内边距+滚动条）；定宽/拖动时更新，工具名按它截断
        self._mcp_cwd = "."
        self._tool_reg = None           # 共享工具表（本地 + MCP）；resume 复用，不重连 MCP
        self._mode = DEFAULT_MODE        # 当前运行模式（Shift+Tab 循环）；新会话默认，resume 时从 session.json 恢复
        self._last_exec_mode = DEFAULT_MODE   # 最近的非 plan 执行模式（plan 批准后回退到它；Batch B 用）
        self._memory_prompt = ""         # 记忆段（拼 system 时复用，免重读磁盘）
        # 本次会话加载的技能名（= 会话创建时进 system prompt 索引的启用技能；启停改动新会话生效，
        # 故会话内固定）。侧栏 SkillPanel 显示，resume 重建会话时重算刷新。
        self._session_skills = [s.name for s in discover_skills(Path.cwd()) if s.enabled]
        if agent is not None:
            self.agent = agent
            self._tool_reg = agent.tools
        else:
            store = SessionStore(root=agent_config.session_root)
            registry = default_registry()       # 本地工具 + 记忆工具；MCP 启动后【后台】连进来
            for t in memory_tools(store.memory_dir):
                registry.register(t)
            self._tool_reg = registry
            self._mcp_cwd = str(store.cwd)
            self._mcp_servers = load_mcp_config(default_config_paths(store.cwd))
            self._mcp_status = "连接中…" if self._mcp_servers else "未接入"
            self._memory_prompt = build_memory_prompt(store.memory_dir)
            self.agent = Agent(
                Provider(current_backend()), registry,          # 按此刻 config.json（导入时快照可能已旧）
                system_prompt=self._system_text(session_dir=store.dir),
                config=current_agent_config(),
                store=store,
                policy=apply_mode(PermissionPolicy.from_persisted(
                    store.load_permissions(), project_root=store.cwd), self._mode),
                ask_permission=self._ask_permission)
        # ask_user 只在 TUI 注册：无头/网关端没有可作答的界面。重复 register 是幂等覆盖，resume 共用同表无碍。
        self._tool_reg.register(make_ask_user_tool(self._ask_user))
        self._reasoning = ""            # 本轮累积的思考
        # 思考耗时 = reasoning 末片时刻 - 本段思考开始时刻。不能用“首片到末片”：有的模型(kimi)
        # 思考完一次性吐出 reasoning，首末片几乎同时到 → 跨度≈0。要从“开始等模型”算起才对。
        self._think_start = 0.0         # 本段思考开始（提交时 / 工具出结果后）
        self._reasoning_end = 0.0       # reasoning 末片到达时刻
        self._answer = ""               # 本轮累积的正文
        self._user_msg_widget = None    # 本轮用户消息在 #log 里那个 widget（用于判它是否还可见）
        self._cur_user_text = None      # 本轮用户消息的 Text（现仅 Ctrl+↑↓ 跳转/构造气泡用；悬浮头改为按视口动态找锚点）
        self._stream_widget = None      # 流式正文的活 Static（挂在 #log 里，逐片更新；commit 时换成 markdown）
        self._live_dirty = False        # 流式区有新内容待刷（攒着由 spinner tick 批量刷，别每片都刷）
        # 本轮是否在跑（决定 Ctrl+C/Ctrl+Q 的语义）。注意不能叫 _running——会撞 textual App 基类
        # 内部的同名“app 是否运行”标志（app 一启动它就是 True），那样所有 guard 会永远早退。
        self._turn_busy = False
        self._work_timer = None         # spinner 定时器
        self._spin_i = 0
        self._work_activity = ""
        self._phase_start = 0.0         # 当前阶段起点（活动标签一变就重置 → spinner 计时按阶段归零）
        # 在跑工具的折叠块：tool_call_id → (块, 内容容器, summary, args)。ToolStarted 建、ToolResult 按 id 填。
        # 用字典（非单指针）：并发子 agent 时多个工具同时在跑、结果乱序回来，靠 id 各自配回、不串位。
        self._tools_by_id: dict = {}
        self._batch: _Batch | None = None   # 当前正在累积的同类工具批（None=没有开着的批）；见 _finalize_batch
        self._interrupting = False       # 用户按了 Ctrl+C、正在收尾中断：spinner 锁“正在停止”、工具块结果标黄“已中断”
        self._history: list[dict] = []   # 续会话的全量历史消息（顺读 transcript）；只渲窗口、按需加载
        self._history_shown = 0          # 已渲染了倒数多少条（滚到顶加载更早一批时增长）
        self._plan_target: str | None = None   # "计划待批准"时，批准后以哪个执行模式跑（⇆ 可切；默认 _last_exec_mode）
        self._pending_plan = ""                 # 当前待批准的计划正文（"查看计划"弹窗展示用；计划不铺进对话）
        self._pending_switch = False            # /config 保存时本轮还在跑 → 收尾时再热切后端
        self._emoji_probe_pending = False       # 启动时测 VS16 emoji 在当前终端实际占 1/2 格
        self._emoji_probe_timer = None

    # ---- 键位 action ----

    def action_toggle_sidebar(self) -> None:
        show = not self.query_one("#sidebar").display   # 收起 → #main(1fr) 自动占满整宽
        self.query_one("#sidebar").display = show
        self.query_one("#splitter").display = show      # 分割线随侧栏一起显隐

    def action_quit_safely(self) -> None:
        # 跑动中先静默置位打断标志，让 worker 线程里的 run_turn 尽快退出循环、收尾合法上下文，
        # 再退出 app（否则线程没法强杀，textual 退出时还得空等那轮跑完）。空闲则直接退。
        self.agent._bg.kill_all()         # 杀掉所有后台任务，防 mecode 退出后子进程残留泄漏
        if self._turn_busy:
            self.agent._interrupt.set()
            self.agent._kill_running_procs()   # 退出前【同步】杀干净（含在跑前台子 agent 的 bash），避免残留
        self.exit()

    def action_interrupt_or_copy(self) -> None:
        # 【复制优先】：有选区（输入框内选中 / 对话区拖选）→ Ctrl+C 就是复制，哪怕模型正在跑——
        # 跑动中翻看输出顺手复制一段是常事，此时打断是灾难（用户只想复制）。
        # 没有任何选区时：跑动中 → 打断本轮；空闲 → 什么都不做（退出走 Ctrl+Q）。
        focused = self.focused
        text = focused.selected_text if isinstance(focused, TextArea) and focused.selected_text \
            else self.screen.get_selected_text()
        if text:
            self._copy_text(text)
            self.screen.clear_selection()     # 复制完清选区：下一次 Ctrl+C 恢复"打断"语义，也给了"已复制"的视觉反馈
            return
        if self._turn_busy:
            self._begin_interrupt()

    def _copy_text(self, text: str) -> None:
        """复制到系统剪贴板。textual 默认走 OSC52 转义序列（把 base64 塞给终端）——Windows 上不少终端
        对大载荷处理极慢（按下复制卡几秒）或压根不支持。Windows 直接调 clip.exe（系统自带、瞬时），
        其余平台仍走 OSC52。
        编码坑：clip.exe 靠【UTF-16 BOM (FF FE)】识别宽字符——裸 utf-16-le 没有 BOM 会被它当
        ANSI 字节流收进剪贴板，粘贴出来全是乱码（每个汉字裂成两个错码）。必须带 BOM。"""
        if os.name == "nt":
            try:
                subprocess.run(["clip"], input=b"\xff\xfe" + text.encode("utf-16-le"),
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=3, check=True)
                self._clipboard = text          # 同步 textual 的内部剪贴板（粘贴回退用）
                return
            except Exception:
                pass                            # clip 不可用：回落 OSC52
        self.copy_to_clipboard(text)

    def _begin_interrupt(self) -> None:
        """Ctrl+C 打断（跑动中）：spinner 锁“正在停止”给反馈；杀进程在后台线程做（不卡 UI）。
        工具块【不】乐观翻黄——各自等真结果回来（被杀→communicate 返回）时才翻黄“已中断”，
        避免“块已显示已中断、spinner 却还在正在停止”的自相矛盾。"""
        self._interrupting = True
        self._set_activity("正在停止")
        self.agent.request_interrupt()        # 置打断标志 + 后台线程树杀进程（不卡 UI）

    def action_toggle_focus(self) -> None:
        # Tab：输入框 ↔ 对话区 切焦点。对话区聚焦时 Ctrl+↑/↓ 跳用户消息、↑/↓ 滚动。
        # 用 has_focus 判（比 self.focused is log 稳）：在输入框→去对话区；在别处（对话区/焦点飘走）→ 回输入框。
        inp = self.query_one("#input", InputArea)
        log = self.query_one("#log", HistoryLog)
        (log if inp.has_focus else inp).focus()
        self.call_after_refresh(self._on_focus_change)   # focus() 异步生效 → 等刷新后再按新焦点刷边框/提示

    # ---- 运行模式（Shift+Tab 循环 normal/auto/plan/yolo） ----

    def _system_text(self, session_dir=None) -> str:
        """拼完整 system（与模式无关）+ 记忆段。构造/resume 共用。模式行为不进 system——见 _set_mode。
        session_dir=该会话的存储目录（环境块告诉模型日志/外置输出在哪）。"""
        sys = build_system_prompt(session_dir=session_dir)
        return f"{sys}\n\n{self._memory_prompt}" if self._memory_prompt else sys

    def action_cycle_mode(self) -> None:
        """Shift+Tab：循环切到下一个模式。"""
        self._set_mode(next_mode(self._mode))

    def action_open_modes(self) -> None:
        """点击状态栏：弹出模式选择弹窗，列出各模式+简介，选中即应用；底部按模型能力显思考开关/深度。"""
        def done(key: str | None) -> None:
            if key:
                self._set_mode(key)
        prov = self.agent.provider
        self.push_screen(
            ModeModal(self._mode, MODE_COLORS, prov.profile, prov.thinking_on,
                      prov.effort, self._set_thinking),
            done)

    def _set_thinking(self, thinking_on: bool, effort: str) -> None:
        """思考开关/深度改变（弹窗回调）：设 provider 运行态 + 持久化到 session.json（下轮拼包即生效）。"""
        prov = self.agent.provider
        prov.thinking_on = thinking_on
        prov.effort = effort
        if self.agent.store is not None:
            self.agent.store.write_header(thinking_on=thinking_on, effort=effort)

    def _set_mode(self, key: str) -> None:
        """切到指定模式：重建 policy（叠加覆盖）+ 设本模式的每轮提示词 + 持久化 + 刷视觉。
        【不碰 system prompt】——模式提示词改由 agent 每轮以 <system-reminder> 注入（保前缀缓存）。
        不打断当前轮：policy 下个 gate 生效、提示词下一轮注入生效。"""
        if key not in MODES:
            return
        self._mode = key
        if key != "plan":
            self._last_exec_mode = key              # 记住最近的执行模式（plan 批准后回退到它）
        self.agent.mode = key                       # write_header 每轮据此把模式落 session.json → resume 恢复
        store = self.agent.store
        plan_path = store.plan_path.as_posix() if store is not None else None
        if store is not None:                       # 注入式 agent（测试）可能无 store：跳过 policy 重建
            policy = apply_mode(
                PermissionPolicy.from_persisted(store.load_permissions(), project_root=store.cwd), key)
            if key == "plan":
                policy.plan_path = plan_path        # 计划模式：放行对计划文件的 write/edit（只读之下的例外）
            self.agent.policy = policy
        # 模式提示词：plan/yolo 有段 → 每轮注入（normal/auto 空 → 静默）；plan 再把【计划文件路径】拼进去，模型才知道写哪
        reminder = MODES[key].prompt
        if key == "plan" and plan_path:
            reminder += f"\n计划文件（把计划写在这里、用 write_file/edit_file 迭代它）：{plan_path}"
        self.agent.mode_reminder = reminder
        # 子 agent 版模式段：造子 agent 时拼进它的 system prompt（不含计划文件那句——子 agent 不写计划）
        self.agent.subagent_reminder = MODES[key].sub_prompt
        self._refresh_mode_indicator()

    def _refresh_mode_indicator(self) -> None:
        """刷新模式视觉：状态栏模式按钮（切 -m-<mode> 类换底色/hover）+ 输入框焦点边框，都同步成当前模式色。"""
        color = MODE_COLORS.get(self._mode, "cyan")
        chip = self.query_one("#mode-chip", ModeChip)
        chip.remove_class(*(f"-m-{k}" for k in MODE_COLORS))
        chip.add_class(f"-m-{self._mode}")
        chip.update(f" {MODES[self._mode].label} ")
        self._refresh_input_border()          # 边框随模式变；亮/暗由当前焦点决定（_refresh_input_border）

    # ---- 计划模式：内联"计划待批准"条（PlanProposed 触发；无模态、先读后批） ----

    def _show_plan_bar(self) -> None:
        self._plan_target = self._last_exec_mode          # 默认目标 = 最近执行模式（⇆ 可改）
        holder = self.query_one("#planbar", Vertical)
        holder.remove_children()
        holder.mount(PlanBar())
        holder.display = True

    def _hide_plan_bar(self) -> None:
        holder = self.query_one("#planbar", Vertical)
        holder.remove_children()
        holder.display = False

    def _cycle_plan_target(self) -> None:
        """⇆：循环切"批准后以哪个模式执行"——只在三个执行模式里转，不含 plan。"""
        exec_modes = [k for k in CYCLE if k != "plan"]
        cur = self._plan_target if self._plan_target in exec_modes else exec_modes[0]
        self._plan_target = exec_modes[(exec_modes.index(cur) + 1) % len(exec_modes)]
        for bar in self.query(PlanBar):
            bar.refresh_target()

    def _view_plan(self) -> None:
        """"查看计划"：弹出计划详情弹窗（Markdown）。窗内点 [批准执行] → 关窗后走批准流程；✕/Esc 只关。"""
        if not self._pending_plan:
            return
        def done(result: str | None) -> None:
            if result == "approve":
                self._approve_plan()
        self.push_screen(PlanViewModal(self._pending_plan), done)

    def _approve_plan(self) -> None:
        """[批准执行]：切到目标执行模式（写权限开）→ 起一轮按计划执行。
        批准消息带上①计划文件路径（执行期模型知道去哪回看）②任务清单引导（多步计划拆 task 跟踪）。"""
        target = self._plan_target or self._last_exec_mode
        store = self.agent.store
        plan_path = store.plan_path.as_posix() if store is not None else None
        self._hide_plan_bar()
        self._set_mode(target)
        if self.agent.policy is not None and plan_path:
            self.agent.policy.plan_path = plan_path   # 执行期仍放行读/写计划文件（read_file 回看、必要时更新计划）
        # 执行提示（task 引导 + 计划路径）作为隐藏 <system-reminder> 发给模型，不整段显示成用户消息
        guidance = "（执行提示）若是多步任务，先用 task_create 把计划拆成任务清单跟踪进度，再逐步实施。"
        if plan_path:
            guidance += f"计划文件在 {plan_path}，需要时可 read_file 回看。"
        self.agent._pending_reminder = guidance
        self._begin_turn("我已批准上述计划，现在按计划开始执行。")   # 用户气泡只显示这句简短的

    def _jump_user_msg(self, direction: int) -> None:
        """对话区聚焦时 Ctrl+↑/↓：滚到上一条(-1)/下一条(+1)【真·用户消息】，置于视口顶。
        真·用户消息 = 标了 .user-msg 的气泡（_reminder/后台事件/压缩提示都不带这个类，天然跳过）。"""
        log = self.query_one("#log", HistoryLog)
        msgs = list(self.query("#log .user-msg"))      # DOM 序 = 从上到下
        if not msgs:
            return
        vp_y = log.scrollable_content_region.y         # 视口顶的屏幕 y
        if direction < 0:                               # 上一条：在视口顶【之上】的、最靠下那条
            above = [w for w in msgs if w.region.y < vp_y - 1]
            target = above[-1] if above else msgs[0]
        else:                                           # 下一条：在视口顶【之下】的、最靠上那条
            below = [w for w in msgs if w.region.y > vp_y + 1]
            target = below[0] if below else msgs[-1]
        log.scroll_to_widget(target, top=True, animate=False)

    # ---- 布局 ----

    def _live_backend(self):
        """当前实际生效的后端 = agent 里 provider 的（热切后即时反映；模块级 backend 是导入时快照）。"""
        return self.agent.provider.backend

    def _status_text(self) -> str:
        be = self._live_backend()
        return f"mecode · {be.base_url or '未配置后端'} · {Path.cwd().as_posix()}"

    def compose(self) -> ComposeResult:
        tools = [t["function"]["name"] for t in self.agent.tools.schemas()]
        with Horizontal(id="status"):            # 模式按钮 + 后端/目录信息；只有按钮可点/可 hover
            yield ModeChip(id="mode-chip")       # 彩色模式按钮（on_mount 填色/文案）
            yield Static(self._status_text(), id="status-info")
        with Horizontal(id="body"):
            with Vertical(id="main"):                 # 左聊天列：对话 + 底部栏（工作提示/后台任务/输入）都在这列内，
                yield Static(id="current-msg")        # 故后台任务的 ✕停止、输入框都对齐到本列右缘，不顶到终端最右
                yield HistoryLog(id="log")            # 对话+流式都在这一个滚动区（流式正文直接挂进来，连续不断层）
                yield VerticalScroll(id="bgtasks")    # 运行中的后台任务：一块一个（空则隐藏，可滚）
                yield Vertical(id="planbar")          # "计划待批准"条（PlanProposed 时挂 PlanBar+show，批准/改时清+hide）
                yield Static(id="work")               # spinner / 空闲时显示键位提示：紧贴输入框上方，故 bgtasks/planbar 都在它之上
                yield InputArea(id="input", soft_wrap=True)   # TextArea 自带整圈边框（四边一致、聚焦变蓝）
            yield Splitter(id="splitter")             # 可拖动分割线：拖它改侧栏宽度
            with VerticalScroll(id="sidebar"):        # 右栏满高、可滚：MCP/工具展开溢出时滚轮可滚
                yield Static(id="sb-info")            # 模型/上下文/MCP 标题（随对话刷新）
                yield TodoPanel(id="sb-todos")        # 任务清单（空则隐藏）；在信息区之下、server/工具之上
                yield WorkflowPanel(id="sb-workflow")  # workflow 分支树（空闲隐藏；0.5s 定时器顺带刷）
                yield SkillPanel(self._session_skills, id="sb-skills")  # 本会话加载的技能；点击开 /skill 管理
                yield ToolsPanel(tools, id="sb-tools")  # server 行在 MCP 连好后挂到技能区之前

    def on_mount(self) -> None:
        self._fit_sidebar_width()
        self._update_sidebar()
        self._refresh_mode_indicator()      # 状态栏模式 chip + 输入框边框（默认 normal=蓝）
        self._show_idle_hint()
        self._wire_bg()
        # 常驻刷新后台任务行（含空闲时；更新“已 Ns”、增删行）。存句柄：ask_user 弹窗期间要暂停
        # ——它每 0.5s 的重绘帧会把输入法预编辑串顶走（与 spinner 同理）。
        self._bg_sync_timer = self.set_interval(0.5, self._sync_bgtasks)
        self.query_one("#input", InputArea).focus()
        # 等首帧画完再探测，避免和终端进入 application mode 的初始化序列交叉。
        # 探测只改计宽、不改字符，所以彩色 emoji 原样保留。
        self.call_after_refresh(self._probe_emoji_width)
        if self._mcp_servers:              # 有配置 → 后台连 MCP（不卡启动）
            self._connect_mcp()
        # 首次运行（config.json 无 model；.env 配了也不算——统一以 config 为准）：不强弹引导，
        # 侧栏模型区显示"未选择模型 · 输入 /config 配置"（见 _update_sidebar），用户 /config 自己来。

    def _probe_emoji_width(self) -> None:
        """用 CPR 实测 ⚠️ 的光标推进量；Windows/VS Code/VTE 等主流终端都支持。

        可用 MECODE_TUI_VS16_WIDTH=1/2 强制覆盖（终端不回 CPR 或特殊 SSH 链路时兜底）。
        探测序列先保存光标、在左上角输出 emoji 并查询位置、随后立即恢复光标；收到回报后全屏重画，
        不把探针留在界面上。
        """
        forced = os.getenv("MECODE_TUI_VS16_WIDTH", "").strip()
        if forced in ("1", "2"):
            _set_vs16_cell_width(int(forced))
            self.screen.refresh(repaint=True, layout=True)
            return
        if not self.console.is_terminal:
            return
        try:
            self._emoji_probe_pending = True
            # ESC 7/8 保存/恢复光标。尾随 ASCII 哨兵强制终端先完成 emoji cluster，再用
            # “CPR 零基 x - 1 个哨兵格”得到 emoji 的实际宽度。
            self._driver.write("\x1b7\x1b[1;1H⚠️X\x1b[6n\x1b8")
            self._driver.flush()
            self._emoji_probe_timer = self.set_timer(0.4, self._finish_emoji_probe)
        except Exception:
            self._emoji_probe_pending = False   # 特殊/无头 driver：保留 Unicode 标准的 2 格规则
            self.screen.refresh(repaint=True, layout=True)

    @on(events.CursorPosition)
    def _on_emoji_cursor_position(self, event: events.CursorPosition) -> None:
        if not self._emoji_probe_pending or event.y != 0 or event.x not in (2, 3):
            return
        self._emoji_probe_pending = False
        if self._emoji_probe_timer is not None:
            self._emoji_probe_timer.stop()
            self._emoji_probe_timer = None
        _set_vs16_cell_width(event.x - 1)
        event.stop()
        self.screen.refresh(repaint=True, layout=True)

    def _finish_emoji_probe(self) -> None:
        """终端没回 CPR 时停止等待，并重画被探针短暂覆盖的左上角。"""
        self._emoji_probe_pending = False
        self._emoji_probe_timer = None
        self.screen.refresh(repaint=True, layout=True)

    def _wire_bg(self) -> None:
        """把后台任务 / 任务清单的变更回流到 UI（均随 resume 重建 Agent，故 on_mount 和 _resume_into 都调）：
        - 后台任务【自然完成】：守护线程 on_complete → post_message(BgComplete) → 空闲起接续轮。
        - 任务清单变更：worker 线程 on_change → post_message(TasksChanged) → 刷侧栏 TodoPanel。
        两个回调都可能跑在非 UI 线程，post_message 线程安全。末尾立即渲染一次清单（含 resume 还原已存清单）。"""
        self.agent._bg.on_complete = lambda task: self.post_message(BgComplete(task))
        self.agent._tasks.on_change = lambda: self.post_message(TasksChanged())
        self._refresh_todos()

    def _fit_sidebar_width(self) -> None:
        """按内容（模型名/上下文/标题行）自动定面板宽度 + 留 1 格余量。
        工具名【不】参与撑宽——MCP 工具名很长（everything__get-structured-content），让它撑会暴宽到上限；
        改为窄面板 + 长名截断（见 _truncate）。"""
        cfg = self.agent.config
        n_tools = len(self.agent.tools.schemas())
        content = max(
            2 + cell_len(self._live_backend().model),                          # 模型名（带 2 缩进）
            cell_len(f"上下文 估  ({cfg.compact_threshold * 100:.0f}%自动压缩)"),  # 上下文标题行（按带"估"的最宽形态量）
            cell_len(f"  {cfg.context_limit} / {cfg.context_limit}  100%"),    # 上下文数值（最宽）
            cell_len(f"工具 ({n_tools})  ▶ 查看更多"),                          # 工具标题行
        )
        # 内容 + 左右内边距2 + 滚动条1 + 1 余量；上限 40
        self._apply_panel_width(min(content + 4, 40))

    def _apply_panel_width(self, w: int) -> None:
        """设侧栏宽度（定宽计算 / 拖动分割线都走这）。算出文本可用宽（扣内边距2+滚动条1），
        重渲各面板按新宽截断、刷新信息区（模型名也按新宽截断，免得拖窄了折行）。"""
        w = max(24, min(int(w), 80))
        self.query_one("#sidebar").styles.width = w
        self._panel_text_width = w - 3
        for p in self.query(ToolsPanel):
            p._render_tools()
        for p in self.query(ServerPanel):
            p._render_server()
        for p in self.query(SkillPanel):          # 技能名按新宽重截断
            p._render_skills()
        for p in self.query(TodoPanel):           # 任务清单按新宽重截断（拖窄了别折行）
            p.refresh_tasks()
        self._update_sidebar()

    @on(TextArea.Changed)
    def _on_input_changed(self) -> None:
        self.query_one("#input", InputArea)._autosize()   # 内容变 → 重算输入框高度

    # ---- 提交一轮 ----

    @on(InputArea.Submitted)
    def _on_submit(self, event: InputArea.Submitted) -> None:
        user = event.value.strip()
        if not user:
            return
        inp = self.query_one("#input", InputArea)
        if self._turn_busy:                             # 本轮还在跑：入队，下一轮请求前带上（即时转向），不打断本轮
            inp.push_history(user)
            inp.load_text("")
            inp._autosize()
            self.agent.queue_user(user)                 # agent 的 _inject_bg_events 下轮顶部会 drain 进上下文
            self._commit_live()                         # 先落定当前流式，气泡不插进流中间
            self._mount(Static(Text.assemble(("> ", "bold cyan"), user), classes="user-msg"), batch=False)   # 用户气泡：独立可见、不折叠、可被 Ctrl+↑↓ 跳转命中
            return                                       # 兜底（消息发在本轮末尾 drain 之后）→ 收尾 _on_event(None) 里 _start_bg_turn 处理
        for aliases, _desc, method in self.SLASH_COMMANDS:   # 本地命令（表驱动，见 SLASH_COMMANDS）：纯本地、不走模型
            if user in aliases:
                inp.push_history(user)
                inp.load_text("")
                inp._autosize()
                getattr(self, method)()
                return
        which = self._match_resume(user)                # /rl·/resume latest → "latest"；/rs·/resume session → "session"
        if which is not None:                           # 续会话命令：不进 run_turn
            inp.push_history(user)
            inp.load_text("")
            inp._autosize()
            self._do_resume(which)
            return
        if not self._live_backend().model:              # 没配后端：别放进 run_turn 撞原始连接报错，给指引
            self._mount(Static(Text("还没有配置模型后端——输入 /config（或点侧栏【点击此处】）先配置一个。",
                                    style="yellow")), batch=False)
            return
        inp.push_history(user)
        self._begin_turn(user)

    def action_open_config(self) -> None:
        """侧栏"[点击此处]"链接（rich meta @click=app.open_config）→ 开配置弹窗。"""
        self._open_config()

    def action_open_mcp(self) -> None:
        """侧栏 MCP 标题旁"管理"链接（rich meta @click=app.open_mcp）→ 开 MCP 管理面板。"""
        self._open_mcp()

    def action_open_skills(self) -> None:
        """侧栏技能区"管理"链接（rich meta @click=app.open_skills）→ 开 /skill 管理面板。"""
        self._open_skills()

    def _open_mcp(self) -> None:
        """/mcp：管理面板列配置里全部 server（含没连上的）+ 启停（新会话生效）+ 失败原因。"""
        connected = {c.name for c in self._mcp_clients}
        self.push_screen(MCPScreen(self._mcp_servers, connected, self._mcp_errors))

    def _open_config(self) -> None:
        """/config：弹配置界面选/换后端，保存写 ~/.mecode/config.json 并【热切】——
        换上按最新 config 现造的 Provider/AgentConfig（agent.switch_backend 会按新档案清洗历史思考字段），
        不用重启。本轮还在跑则记为待切，收尾时再切（中途换后端会把半截流搞乱）。
        新增页保存【不关窗】（跳切换页展示列表），热切由 ConfigScreen 调 _handle_config_saved；
        切换页确定才走 dismiss 回调。两条路都汇到 _handle_config_saved。"""
        self.push_screen(ConfigScreen(), lambda model: model and self._handle_config_saved(model))

    def _open_skills(self) -> None:
        """/skill：技能面板。选中一个启用的技能 → 全文以一次性 <system-reminder> 注入 + 起一轮
        （复用 agent._pending_reminder，同"批准计划"注入约定；一次性进历史、不重发、无"使用中"态）。
        跑轮中打开只能看/改启停，选中使用会提示等本轮结束。"""
        def done(skill) -> None:
            if skill is None:
                return
            if self._turn_busy:
                self._mount(Static(Text(f"（本轮还在跑，技能「{skill.name}」未注入——等结束后再 /skill 使用）",
                                        style="yellow")), batch=False)
                return
            if not self._live_backend().model:
                self._mount(Static(Text("还没有配置模型后端——先 /config 配置一个。", style="yellow")),
                            batch=False)
                return
            self.agent._pending_reminder = skill_reminder(skill)
            self._mount(Static(Text(f"（技能「{skill.name}」已注入本轮）", style="green")), batch=False)
            self._begin_turn(f"请按技能「{skill.name}」的流程执行。")
        self.push_screen(SkillScreen(), done)

    def _handle_config_saved(self, model: str) -> None:
        """config.json 落了新后端：空闲立即热切；本轮在跑则记为待切、收尾时切。"""
        if self._turn_busy:
            self._pending_switch = True
            self._mount(Static(Text(f"（已保存 {model}；本轮结束后自动切换）", style="yellow")),
                        batch=False)
        else:
            self._apply_backend_switch()

    def _apply_settings_change(self) -> None:
        """只改了上限/阈值（行内保存）：让【正在跑的】agent 立刻用上新值。
        不走 _apply_backend_switch——那条会重建 Provider 并把思考态重置回档案默认，
        而这里后端根本没换，重置是无谓的副作用。"""
        self.agent.config = current_agent_config()
        self.query_one("#status-info", Static).update(self._status_text())

    def _apply_backend_switch(self) -> None:
        """按此刻的 config.json 热切后端：新 Provider（带新模型思考档案）+ 新 AgentConfig（上限/阈值随新模型），
        思考态回新档案默认并落盘，刷状态栏/侧栏。"""
        self._pending_switch = False
        be = current_backend()
        prov = Provider(be)
        self.agent.switch_backend(prov, current_agent_config())
        if self.agent.store is not None:      # 思考态回新档案默认并落盘（resume 恢复用）
            self.agent.store.write_header(thinking_on=prov.thinking_on, effort=prov.effort)
        self.query_one("#status-info", Static).update(self._status_text())
        self._fit_sidebar_width()
        self._update_sidebar()
        self._mount(Static(Text(f"（已切换到 {be.model}，即刻生效）", style="green")), batch=False)

    def _show_system_prompt(self) -> None:
        """/system：把当前发给模型的 system prompt（messages[0]）+ 本模式每轮注入的 <system-reminder>
        原样显示，供开发时查看（模型被训练成不吐 system prompt，问它不可靠）。纯本地、不调模型。"""
        msgs = self.agent.messages
        sysmsg = msgs[0]["content"] if msgs and msgs[0].get("role") == "system" else "（当前没有 system 消息）"
        body = Text()
        body.append(f"当前 system prompt（{len(sysmsg)} 字）\n", style="bold yellow")
        body.append(sysmsg)
        if self.agent.mode_reminder:                    # A 重构后模式提示词不在 system、改每轮注入 → 单列出来
            body.append(f"\n\n本模式（{self._mode}）每轮注入的 <system-reminder>：\n", style="bold yellow")
            body.append(self.agent.mode_reminder, style="dim")
        self._mount(Static(body), batch=False)

    def _show_tools(self) -> None:
        """/tool：列出当前上下文里模型能调的所有工具（agent.tools.schemas()，含 MCP 连进来的），
        名字 + 参数（必填/可选?）+ 描述，原样查看。纯本地、不调模型。"""
        schemas = self.agent.tools.schemas()
        body = Text()
        body.append(f"当前上下文的工具（{len(schemas)} 个）\n", style="bold yellow")
        for s in schemas:
            fn = s.get("function", {})
            params = fn.get("parameters", {}) or {}
            props = params.get("properties", {}) or {}
            required = set(params.get("required", []) or [])
            sig = ", ".join(p + ("" if p in required else "?") for p in props)   # 可选参数带 ?
            body.append(f"\n● {fn.get('name', '?')}", style="bold cyan")
            body.append(f"({sig})\n", style="cyan")
            body.append(f"  {fn.get('description', '')}\n", style="dim")
        self._mount(Static(body), batch=False)

    def _show_help(self) -> None:
        """/help：列出全部本地斜杠命令（SLASH_COMMANDS 表 + 带参数的续会话命令）+ 快捷键。
        纯本地、不调模型。"""
        rows = [("、".join(aliases), desc) for aliases, desc, _m in self.SLASH_COMMANDS]
        rows += [("/rl、/resume latest", "续上最近一个历史会话"),
                 ("/rs、/resume session", "弹选择器挑一个历史会话续上")]
        body = Text()
        body.append(f"可用斜杠命令（{len(rows)} 组，均为本地命令、不走模型）\n", style="bold yellow")
        for cmd, desc in rows:
            body.append(f"\n● {cmd}\n", style="bold cyan")
            body.append(f"  {desc}\n", style="dim")
        body.append("\n快捷键：Ctrl+Enter 换行 · Ctrl+C 打断 · Ctrl+Q 退出 · Ctrl+↑↓ 输入历史 · "
                    "Shift+Tab 切模式 · Ctrl+P 侧栏面板；对话区 Ctrl+↑↓ 跳用户消息\n", style="dim")
        self._mount(Static(body), batch=False)

    def _begin_turn(self, user: str) -> None:
        """起一轮：清输入、挂用户气泡、起 spinner、后台线程跑 run_turn。
        _on_submit（用户输入）和 _approve_plan（批准计划的合成消息）共用。"""
        self._hide_plan_bar()                           # 新一轮开始 → 收掉待批准条（批准/改计划都在此清）
        inp = self.query_one("#input", InputArea)
        inp.load_text("")
        inp._autosize()
        self._turn_busy = True
        self.query_one("#log", HistoryLog).user_scrolled = False   # 发新消息=要看新回复 → 恢复跟随
        self._cur_user_text = Text.assemble(("> ", "bold cyan"), user)
        self._user_msg_widget = Static(self._cur_user_text, classes="user-msg")   # 真用户消息：Ctrl+↑↓ 跳转命中
        self._mount(self._user_msg_widget, batch=False)   # 用户消息：进 #log 保历史顺序、不折叠；悬浮头由 _sync 按可见性决定显隐
        # 几何要等布局刷新才算得出（刚 mount 时 region 高度=0 会误判"不可见"→悬浮头错误显示，
        # 且整轮再无人 sync 会一直挂着）→ 推迟到刷新后判，此时 scroll_end 也已落定。
        self.call_after_refresh(self._sync_pinned_header)
        self._reasoning = self._answer = ""
        # 这轮若上下文已过压缩阈值，run_turn 会【先】做一次"总结整段历史"的压缩（阻塞、可达数十秒）。
        # 用和 agent 完全相同的条件预判（含 usage 缺失时的本地估算兜底，estimate_tokens 同一函数）
        # → spinner 先显示"压缩中"，等压缩完的 Notice 到了再切"思考中"
        # （纯 TUI 侧，不动 agent；条件一致 → 永远和 agent 实际是否压缩同步）。
        cfg = self.agent.config
        tokens = self.agent.context_tokens or estimate_tokens(
            self.agent.messages, self.agent.tools.schemas())
        will_compact = tokens > cfg.compact_threshold * cfg.context_limit
        self._start_work("压缩中" if will_compact else "思考中")
        self._run_agent(user)

    @work(thread=True, exclusive=True)
    def _run_agent(self, user: str) -> None:
        # 在后台线程跑同步生成器；每个事件投递给 UI。出错也兜住，finally 必发结束标记放开输入。
        try:
            for ev in self.agent.run_turn(user):
                self.post_message(AgentEvent(ev))
        except ProviderError as e:            # 后端错误已是人话（key 无效/限流/连不上…），直接显示
            self.post_message(AgentEvent(Notice(str(e))))
        except Exception as e:
            self.post_message(AgentEvent(Notice(f"出错：{type(e).__name__}: {e}")))
        finally:
            self.post_message(AgentEvent(None))

    @on(TasksChanged)
    def _on_tasks_changed(self, msg: TasksChanged) -> None:
        self._refresh_todos()             # 任务清单变更（create/update/delete）→ 刷侧栏

    def _refresh_todos(self) -> None:
        """刷侧栏任务清单（TasksChanged 推送 / resume / 定宽变化时调）。面板未挂载（极早期）则跳过。"""
        try:
            self.query_one("#sb-todos", TodoPanel).refresh_tasks()
        except Exception:
            pass

    @on(BgComplete)
    def _on_bg_complete(self, msg: BgComplete) -> None:
        # 自然完成（done/timeout，用户杀的不会进这）→ 往对话末尾挂一条持久通知行（在 AI 自动接续那一轮之前），
        # 让"哪个后台任务结束了"在历史里有迹可循。完成行的 #bgtasks 那块由 _sync 随 running() 摘掉。
        task = msg.task
        timeout = task.status != "done"
        is_sub = getattr(task, "is_subagent", False)
        # 头行框（子 agent=任务；bash=命令）+ 输出框（虚拟化 LinesView：只渲可见行，上万行展开也不卡）。
        head = f"任务: {task.command}" if is_sub else f"$ {task.command}"
        # 子 agent 总结是短散文 → 用 Static 稳定渲染（LinesView 是给 bash 可能上万行日志的虚拟化视图，短内容会渲成空）。
        result_box = (ToolBox(Static(Text(task.output or "（无总结）", style="dim")), classes="toolbox")
                      if is_sub else
                      LinesView(task.output or "（无输出）", classes="toolbox"))
        boxes = [ToolBox(Static(Text(head, style="cyan")), classes="toolbox"), result_box]
        self._mount(BgDoneBlock(task.command, task.exit_code, timeout, boxes, is_subagent=is_sub,
                                description=getattr(task, "description", "")), batch=False)   # 后台完成通知：独立可见、不折进无关的工具批
        self._start_bg_turn()             # 后台任务自然完成 → 空闲就起一轮自动接续
        if timeout:                       # 超时再闪一条 2s 红字（异常结束，给个即时醒目提示）
            self._flash_bgtask(f"⚠ 后台任务 #{task.id} 超时被终止：{_truncate(task.command, 50)}")

    def _sync_bgtasks(self) -> None:
        """把“运行中后台任务”的显示行和 bg.running() 对齐（0.5s 常驻调）：增新行、删已结束、刷已运行时长。
        完成/被杀的任务会从 running() 消失 → 对应行被摘掉（“完成即关”）。"""
        container = self.query_one("#bgtasks", VerticalScroll)
        running = {t.id: t for t in self.agent._bg.running()}
        for tid in list(self._bg_rows):                   # 删：不再运行的行
            if tid not in running:
                self._bg_rows.pop(tid).remove()
        for tid, task in running.items():                 # 增/刷：运行中的行
            if tid not in self._bg_rows:
                row = BgTaskRow(task)
                self._bg_rows[tid] = row
                container.mount(row)
            else:
                self._bg_rows[tid].refresh_row()
        container.display = bool(container.children)      # 有行（或超时闪）才显示，空则隐藏
        self.query_one("#sb-workflow", WorkflowPanel).refresh_workflow()   # 顺带刷 workflow 分支树（同一 0.5s 节拍）
        self._start_bg_turn()                             # 顺带轮询：到点的 check-in（时间驱动、无回调）空闲就起轮

    def _flash_bgtask(self, text: str) -> None:
        """超时提示：在后台任务区闪一条红字，2 秒后自动消失（done 不闪，靠 AI 自动回应通知）。"""
        container = self.query_one("#bgtasks", VerticalScroll)
        w = Static(Text(text, style="red"))
        container.mount(w)
        container.display = True
        self.set_timer(2.0, lambda: (w.remove(), self._sync_bgtasks()))

    def _start_bg_turn(self) -> None:
        """有【后台完成】或【排队的用户消息】待处理且当前空闲 → 起一轮“无用户输入”的接续。
        正忙则不起（正在跑的那轮 _turn_loop 会顺手 drain 掉这些）。_turn_busy 充当单一互斥。
        排队用户消息一般在本轮内就被 drain（即时转向）；仅当它发在本轮末尾 drain 之后、没赶上时走这里兜底。"""
        if self._turn_busy or not (self.agent._bg.has_pending() or self.agent.has_pending_user()):
            return
        self._turn_busy = True
        self._start_work("思考中" if self.agent.has_pending_user() else "处理后台结果")
        self._run_bg_agent()

    @work(thread=True, exclusive=True)
    def _run_bg_agent(self) -> None:
        # 同 _run_agent，但跑 run_bg_turn（无用户输入，靠 drain 出的完成通知驱动）。
        try:
            for ev in self.agent.run_bg_turn():
                self.post_message(AgentEvent(ev))
        except ProviderError as e:
            self.post_message(AgentEvent(Notice(str(e))))
        except Exception as e:
            self.post_message(AgentEvent(Notice(f"出错：{type(e).__name__}: {e}")))
        finally:
            self.post_message(AgentEvent(None))

    @work(thread=True)
    def _connect_mcp(self) -> None:
        # 后台线程连 MCP（握手慢、不能卡 UI）。连好把工具注册进【同一张共享表】——agent 下一轮的
        # schemas() 就看得到（已快照遍历，并发安全）；再回 UI 线程刷状态/工具列表/宽度。
        clients, tools, errors = connect_servers(self._mcp_servers, cwd=self._mcp_cwd)
        for t in tools:
            self._tool_reg.register(t)

        def done() -> None:
            self._mcp_clients = clients
            # errors 每条形如 "name: 错误类型: 详情" → 拆成 {name: 原因}（供侧栏计数 + /mcp 面板显详情）
            self._mcp_errors = dict(e.partition(": ")[::2] for e in errors)
            self._mcp_status = "未接入" if not clients else f"已接入 {len(clients)} 个"
            sidebar = self.query_one("#sidebar", VerticalScroll)
            sb_tools = self.query_one("#sb-tools", ToolsPanel)
            sb_skills = self.query_one("#sb-skills", SkillPanel)
            for p in self._server_panels:           # 清掉上次的 server 行（重连/幂等）
                p.remove()
            self._server_panels = []
            mcp_full = set()
            for c in clients:                        # 每个 server 一行可展开，挂在技能区之前（MCP→技能→工具）
                names = [t["name"] for t in c.tools]
                mcp_full.update(f"{c.name}__{n}" for n in names)
                panel = ServerPanel(c.name, names)
                sidebar.mount(panel, before=sb_skills)
                self._server_panels.append(panel)
            # “工具”区只放本地工具（MCP 工具已归到各自 server 行下，不再混在这里）
            local = [s["function"]["name"] for s in self.agent.tools.schemas()
                     if s["function"]["name"] not in mcp_full]
            sb_tools.set_tools(local)
            self._fit_sidebar_width()
            self._update_sidebar()
        self.call_from_thread(done)

    # ---- 工具审批（agent 注入的 ask_permission，跑在 worker 线程） ----

    def notice_killed(self, tid: int | None) -> None:
        """停掉后台任务后的回执（✕停止按钮 / 审批弹窗的"停止"两处共用）。
        没有它的话，任务块消失和"它自己跑完了"看起来一模一样，无从确认停止生效没有。
        走 Notice（batch=False，不被折进工具批）；不起轮——便条等下次因别的原因跑时捎带注入。
        post_message 是线程安全的，故 worker 线程里也能调。"""
        self.post_message(AgentEvent(Notice(
            f"已停止后台任务 #{tid}" if tid is not None else "已停止该后台任务")))

    def _ask_permission(self, tool: str, args: dict, ctx=None) -> str:
        """worker 线程调用：请 UI 弹审批弹窗，阻塞等结果。
        ctx（agent.AskContext）= 谁在问：盯【它自己的】打断标志退出——后台子 agent 用的是
        BackgroundManager 给的独立标志，只盯主 agent 的话 kill_bgtask 解不开卡在弹窗上的线程。
        并发调用（前台子 agent 批）由 _perm_lock 排队：一次只弹一个，结果不串台。
        选"停止此后台任务" → 置它的标志（=终止该子 agent）并按拒绝返回。"""
        ev = getattr(ctx, "interrupt", None) or self.agent._interrupt
        is_sub = bool(getattr(ctx, "is_sub", False))
        can_stop = bool(getattr(ctx, "can_stop", False))
        # 排队等锁也要能被打断：`with self._perm_lock` 是无限阻塞的——一头扎进去就再也看不见打断标志，
        # 前面那个"已在中断中"的检查只挡得住【进来之前】就被停的。于是 Ctrl+C / kill_bgtask 在
        # 别人的弹窗被答掉之前完全不生效（用户以为没反应，其实是这条线程卡在锁上）。改成带超时轮询。
        while not self._perm_lock.acquire(timeout=0.05):
            if ev.is_set():
                return "deny"
        if ev.is_set():                           # 拿到锁的瞬间又被停了 → 别弹窗，直接放开
            self._perm_lock.release()
            return "deny"
        try:
            self._perm_event.clear()
            self._perm_seq += 1
            seq = self._perm_seq
            self._perm_waiting = True             # 告诉轮末清理：这张窗有人等着，别收
            self.post_message(AskPermission(tool, args, is_sub, can_stop, seq))
            while not self._perm_event.wait(0.05):
                if ev.is_set():                   # 被 Ctrl+C / kill_bgtask 停掉 → 当拒绝，放开线程
                    self.post_message(DismissPermission(seq))   # 顺手收窗，别留幽灵窗在屏上
                    return "deny"
            if self._perm_result == "stop":
                ev.set()                          # 终止这条 agent 分支（它在下一个检查点停）
                # 认 id 靠"它的打断标志就是这个 ev"（workflow 各阶段共用一个 → 认到那个后台任务，正确）
                self.notice_killed(next((t.id for t in self.agent._bg.running()
                                         if t.interrupt is ev), None))
                return "deny"
            return self._perm_result
        finally:
            self._perm_waiting = False
            self._perm_lock.release()

    @on(AskPermission)
    def _on_ask_permission(self, msg: AskPermission) -> None:
        def done(choice: str | None) -> None:       # 弹窗结果回填 → 唤醒 worker
            if self._perm_screen_seq == msg.seq:
                self._perm_screen_seq = None        # 这张窗谢幕了，屏上不再是它
            if msg.seq != self._perm_seq:           # 迟到的答复：这张窗早被放弃（打断/收尾）→ 丢弃，
                return                              # 否则会被【下一次】审批当成答复（含"总是允许"落盘）
            self._perm_result = choice or "deny"
            self._perm_event.set()
        root = self.agent.policy.root if self.agent.policy is not None else None
        self._perm_screen_seq = msg.seq            # 记住【屏上这张】是谁，收窗时认它（见 _on_dismiss_permission）
        self.push_screen(PermissionModal(msg.tool, msg.args, msg.is_sub, root, msg.can_stop), done)

    def _clear_orphan_modal(self) -> bool:
        """轮收尾清【孤儿】弹窗：打断会把审批/提问窗留在屏上（worker 已按拒绝/跳过返回、没人再等它），
        不清的话后台自动接续的下一轮会压着旧窗弹新窗，用户答旧窗的结果被错配给新调用。

        但【有人正等的窗不能动】：后台子 agent / workflow 阶段跑在自己的线程里，与主 agent 这一轮
        各自独立——主 agent 收尾时它们可能刚弹窗等你答，无条件 dismiss 会让用户【没答】就被判成
        "用户拒绝"，还写进它的工具结果上报给主 agent。返回是否真的清了（供测试断言）。"""
        if self._perm_waiting or self._ask_waiting:
            return False
        if isinstance(self.screen, (QuestionModal, PermissionModal)):
            self.screen.dismiss(None)
            return True
        return False

    @on(DismissPermission)
    def _on_dismiss_permission(self, msg: DismissPermission) -> None:
        """收掉一张没人再等的审批窗（worker 被打断时请求）。
        认【这张窗自己的序号】，不是全局计数器：worker A 发完收窗请求就放开锁，worker B 会立刻把
        计数器推到下一个再弹新窗，等 UI 处理到这条时拿旧序号跟新计数器比必然对不上 → 窗收不掉、
        成了压在新窗底下的幽灵。"""
        if msg.seq == self._perm_screen_seq and isinstance(self.screen, PermissionModal):
            self.screen.dismiss(None)

    # ---- 提问（ask_user 工具注入的回调，跑在 worker 线程） ----

    def _ask_user(self, questions: list[dict]) -> dict | None:
        """worker 线程调用：请 UI 弹提问弹窗，阻塞等用户作答。打断中（Ctrl+C）返回 None（按跳过处理）。"""
        self._ask_event.clear()
        self._ask_seq += 1                        # 同审批那套：被放弃那张窗的迟到答复据此丢弃
        seq = self._ask_seq
        self._ask_waiting = True                  # 同审批：告诉轮末清理这张窗有人等着，别收
        try:
            self.post_message(AskQuestion(questions, seq))
            while not self._ask_event.wait(0.05):
                if self.agent._interrupt.is_set():
                    self.post_message(DismissQuestion(seq))   # 顺手收窗，别留幽灵窗在屏上
                    return None
            return self._ask_result
        finally:
            self._ask_waiting = False

    @on(AskQuestion)
    def _on_ask_question(self, msg: AskQuestion) -> None:
        # 弹窗期间暂停 spinner 帧：10Hz 刷状态栏=终端每 0.1s 重画一帧，每帧都会把输入法的
        # 预编辑串（拼音预览）顶走/抹掉——表现为拼音在框内与弹窗边缘持续闪烁。主输入框打字时
        # 轮次空闲无 spinner，故从无此症。状态栏改静态文案，收窗恢复计时动画。
        if self._work_timer is not None:
            self._work_timer.pause()
            self.query_one("#work", Static).update(
                Text("❓ 等待你在弹窗中作答…   Ctrl+C 打断", style="yellow"))
        self._bg_sync_timer.pause()

        def done(res: dict | None) -> None:         # 弹窗结果回填 → 唤醒 worker
            if self._work_timer is not None:
                self._work_timer.resume()
            self._bg_sync_timer.resume()
            if self._ask_screen_seq == msg.seq:
                self._ask_screen_seq = None         # 这张窗谢幕了
            if msg.seq != self._ask_seq:            # 迟到的答复：这张窗早被放弃（打断）→ 丢弃，
                return                              # 否则问 A 的答案会被记到【下一次提问】头上
            self._ask_result = res
            self._ask_event.set()
        self._ask_screen_seq = msg.seq
        self.push_screen(QuestionModal(msg.questions), done)

    @on(DismissQuestion)
    def _on_dismiss_question(self, msg: DismissQuestion) -> None:
        """收掉一张没人再等的提问窗（同 _on_dismiss_permission，认这张窗自己的序号）。"""
        if msg.seq == self._ask_screen_seq and isinstance(self.screen, QuestionModal):
            self.screen.dismiss(None)

    # ---- 续会话（/rl·/resume latest 续最近；/rs·/resume session 开选择器） ----

    @staticmethod
    def _match_resume(s: str) -> str | None:
        s = s.strip()
        if s in ("/rl", "/resume latest"):
            return "latest"
        if s in ("/rs", "/resume session"):
            return "session"
        return None

    def _do_resume(self, which: str) -> None:
        sessions = SessionStore.list_sessions(root=agent_config.session_root)
        if not sessions:
            self._mount(Static(Text("（本项目还没有历史会话）", style="dim")))
            return
        cur = self.agent.store.session_id if self.agent.store else None
        if which == "latest":                       # 续最近——排除当前会话（重载当前没意义）
            others = [m for m in sessions if m.get("session_id") != cur]
            if not others:
                self._mount(Static(Text("（除当前会话外没有其他历史会话）", style="dim")))
                return
            self._resume_into(others[0])
        else:                                       # 弹选择器（含当前、标"(当前)"），选中后回调 resume
            self.push_screen(SessionPickerScreen(sessions[:10], current_id=cur),
                             self._on_session_picked)

    def _on_session_picked(self, meta: dict | None) -> None:
        if meta:                                    # None = 用户按 Esc 取消
            self._resume_into(meta)

    def _resume_into(self, meta: dict) -> None:
        """换上目标会话：重建 Agent（新鲜 system + 工作记忆），清空对话视图、窗口化重渲历史。
        两种读法分工：Agent 用 load_messages（fold 出的工作记忆，发给 API）；视图用
        read_transcript_messages（顺读全量人类历史，给人看）——正是"两个真相"的体现。
        视图不一次性全渲（长会话上千条会卡死 textual），只渲倒数一批、落到底，滚到顶再加载更早。"""
        store = SessionStore(root=agent_config.session_root, session_id=meta["session_id"])
        restored = meta.get("mode", DEFAULT_MODE)         # 恢复上次保留的模式（会话头里存的）
        if restored not in MODES:
            restored = DEFAULT_MODE
        self._memory_prompt = build_memory_prompt(store.memory_dir)
        self.agent = Agent(Provider(current_backend()), self._tool_reg or default_registry(),
                           system_prompt=self._system_text(session_dir=store.dir),   # 与模式无关（模式提示词每轮注入）
                           config=current_agent_config(),
                           store=store, resume_messages=store.load_messages() or None,
                           policy=PermissionPolicy.from_persisted(
                               store.load_permissions(), project_root=store.cwd),
                           ask_permission=self._ask_permission)
        self._wire_bg()                                  # 新 agent 的 bg 要重新接 on_complete
        # 重建时 system prompt 重拼 → 技能索引按此刻启停重算，侧栏技能区跟着刷
        self._session_skills = [s.name for s in discover_skills(Path.cwd()) if s.enabled]
        self.query_one("#sb-skills", SkillPanel).set_skills(self._session_skills)
        self.agent.context_tokens = meta.get("context_tokens", 0) or 0   # 恢复上下文用量 → 侧栏立即显示、不用等首条消息
        prov = self.agent.provider                       # 恢复思考运行态（会话头里存的）→ 设回新 provider
        prov.thinking_on = meta.get("thinking_on", True)
        prov.effort = meta.get("effort") or prov.profile.default_effort
        self._set_mode(restored)                         # 恢复模式：应用 policy 覆盖 + mode_reminder + 刷 chip/边框
        self.query_one("#log", VerticalScroll).remove_children()
        self._cur_user_text = self._user_msg_widget = None
        self._sync_pinned_header()                       # 隐藏悬浮头
        self._reasoning = self._answer = ""
        self._stream_widget = None
        self._tools_by_id = {}
        self._batch = None                               # 复位当前批（换会话不残留上一会话未 finalize 的批）
        self._history = store.read_transcript_messages()
        self._history_shown = 0
        self._render_initial_history()
        more = "，滚到顶加载更早" if len(self._history) > self.HISTORY_BATCH else ""
        self._mount(Static(Text(f"↻ 已续会话「{meta.get('title', '')}」"
                                f"（共 {len(self._history)} 条{more}）", style="cyan dim")))
        self._resume_pending_plan()                   # 续接的会话若停在"已 exit_plan 未批准" → 重渲计划 + 挂回待批准条
        self._update_sidebar()
        self.query_one("#input", InputArea).focus()   # 续会话后焦点回输入框（可预期，别落在对话区）

    def _resume_pending_plan(self) -> None:
        """resume 后：先清掉旧会话残留的待批准条；若续接的会话最后停在"已 exit_plan、还没批准"，
        读回 plan.md 重渲计划 + 重新挂出待批准条（这样 /rl 回来能接着测/批准）。"""
        self._hide_plan_bar()                         # 先清旧残留（下面按需恢复）
        msgs = self.agent.messages
        last_user = -1                                # 最后一条【真·用户消息】（排除 <system-reminder> 注入）
        for i, m in enumerate(msgs):
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("<system-reminder>"):
                last_user = i
        pending = any(                                # 它之后若有 assistant 调过 exit_plan → 计划待批准（用户还没回应）
            tc.get("function", {}).get("name") == "exit_plan"
            for m in msgs[last_user + 1:] if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or []))
        store = self.agent.store
        if not pending or store is None or not store.plan_path.is_file():
            return
        plan = store.plan_path.read_text(encoding="utf-8", errors="replace")
        if plan.strip():
            self._pending_plan = plan            # 存起来供"查看计划"弹窗；不铺进对话
            self._show_plan_bar()

    def _history_widgets(self, messages: list[dict]) -> list:
        """把一批历史消息简化成 widget 列表（不挂载）。简化版：用户行/助手 Markdown/工具一行摘要——
        思考(reasoning)从不入存档故无法重现，交互式工具块也不还原（结果取首行预览），重在看清脉络。

        工具调用与结果【配对】：一条 assistant 可能并行多个 tool_call、结果分散在其后的多条 tool 消息里。
        先按 tool_call_id 建 结果映射，再把每条结果【贴到它对应的调用正下方】、头+结果合成一个 widget——
        否则会"🔧 一堆、↳ 一堆"分两块（resume 时尤其明显）。"""
        results = {m.get("tool_call_id"): _result_preview(m.get("content"))
                   for m in messages if m.get("role") == "tool" and m.get("content")}
        out = []
        for m in messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                if content == "[Request interrupted by user]":
                    out.append(Static(Text("（已打断）", style="dim")))
                elif content.startswith("<system-reminder>"):
                    # 系统注入（后台事件/压缩提示/技能注入/模式提示）：不是用户说的话——
                    # 不带 > 前缀、剥掉标签壳、整体置灰、只留首行+字数（全文本就是给模型的，人看脉络即可）
                    body = content.removeprefix("<system-reminder>").removesuffix("</system-reminder>").strip()
                    first = body.splitlines()[0] if body else ""
                    label = _clip(first, 72) + (f"（系统注入 · {len(body)} 字）" if len(body) > len(first) else "（系统注入）")
                    out.append(Static(Text(f"⚙ {label}", style="dim")))
                else:                                            # 真·用户消息 → 标 .user-msg，Ctrl+↑↓ 跳转命中
                    out.append(Static(Text.assemble(("> ", "bold cyan"), content), classes="user-msg"))
            elif role == "assistant":
                if content.strip():
                    out.append(_md(content.strip()))
                for tc in m.get("tool_calls", []):
                    t = Text(f"🔧 {tc.get('function', {}).get('name', '')}", style="cyan")
                    head = results.get(tc.get("id"))     # 该调用的结果首行
                    if head:
                        t.append(f"\n  ↳ {_clip(head, 80)}", style="dim")   # 贴到本工具正下方
                    out.append(Static(t))
            # role == "tool"：结果已在上面贴到各自工具下 → 这里不再单独铺一行
            elif role == "system":                       # 中途系统提示（空响应兜底等）
                out.append(Static(Text(content, style="dim")))
        return out

    def _render_initial_history(self) -> None:
        """续会话首屏：先渲倒数 HISTORY_BATCH 条，再落底 + 保证可滚（高终端下补渲直到溢出，见 _ensure_scrollable）。"""
        log = self.query_one("#log", VerticalScroll)
        total = len(self._history)
        batch = self._history[max(0, total - self.HISTORY_BATCH):total]
        for w in self._history_widgets(batch):
            log.mount(w)
        self._history_shown = len(batch)
        self.call_after_refresh(self._ensure_scrollable)

    def _prepend_earlier(self) -> bool:
        """把更早一批历史渲染并挂到 #log 最前。返回是否真加了（已到最早/空批 → False）。"""
        total = len(self._history)
        if self._history_shown >= total:
            return False
        end = total - self._history_shown
        start = max(0, end - self.HISTORY_BATCH)
        widgets = self._history_widgets(self._history[start:end])
        self._history_shown += end - start
        if not widgets:
            return False
        log = self.query_one("#log", VerticalScroll)
        anchor = log.children[0] if log.children else None
        if anchor is not None:
            log.mount(*widgets, before=anchor)
        else:
            for w in widgets:
                log.mount(w)
        return True

    def _ensure_scrollable(self, tries: int = 0) -> None:
        """续会话落底 + 保证可滚。高终端下末 20 条填不满视口 → 不溢出 → 没滚动条、也无法滚到顶 LoadMore
        （死锁）。故若没溢出且还有更早历史，就补渲更早批、逐帧重排重查，直到能滚或渲完（tries 上限兜底）。
        每帧 refresh(layout=True) 也顺带覆盖 markdown 异步渲高：据最终高度判是否溢出、显示滚动条。"""
        log = self.query_one("#log", VerticalScroll)
        if tries < 12 and log.max_scroll_y <= 0 and self._prepend_earlier():
            self.call_after_refresh(self._ensure_scrollable, tries + 1)
        else:
            log.refresh(layout=True)                      # 据最终高度重排，滚动条按真实溢出显示
            self.call_after_refresh(log.scroll_end, animate=False)

    @on(HistoryLog.LoadMore)
    def _on_load_more_history(self) -> None:
        """滚到顶 → 把更早的一批挂到最前，并锚定原来的顶部 widget，视位不跳、可继续往上滑。"""
        log = self.query_one("#log", VerticalScroll)
        anchor = log.children[0] if log.children else None   # 加载前最顶的 widget
        if self._prepend_earlier() and anchor is not None:
            self.call_after_refresh(log.scroll_to_widget, anchor, animate=False, top=True)

    # ---- 消费 agent 事件 ----

    @on(AgentEvent)
    def _on_event(self, msg: AgentEvent) -> None:
        ev = msg.ev
        if ev is None:                                # 本轮结束 → 落定流式区、停 spinner、回焦输入框
            # 【顺序要紧】先 finalize 批（把批置空），再 commit 最终答案：这样最终答案经 _mount 时
            # _batch 已 None → 挂进 #log 不进任何批 → 保持可见，绝不被折叠。
            self._turn_busy = False
            self._interrupting = False                # 中断收尾完成，复位（下轮 _set_activity 恢复正常）
            self._clear_orphan_modal()
            self._finalize_batch()
            self._commit_live()
            self.query_one("#input", InputArea).focus()   # 先回焦输入框
            self._stop_work()                         # 再停 spinner + 显提示（此时已空闲、焦点=输入框 → IDLE_HINT）
            self._update_sidebar()
            if self._pending_switch:                  # 本轮跑时用户 /config 存了新后端 → 现在安全，热切
                self._apply_backend_switch()
            self._start_bg_turn()                     # 收尾时若有后台完成待处理 → 接着自动起一轮
            return
        match ev:
            case ReasoningDelta(text=t):
                self._reasoning += t
                self._reasoning_end = time.monotonic()  # 末片时刻随片更新
                self._set_activity("思考中")
                self._live_dirty = True                 # 攒着，由 _tick 批量刷（见下）
            case TextDelta(text=t):
                self._answer += t
                self._set_activity("回复中")
                self._live_dirty = True
            case Usage():                             # 模型返回精确 token → 实时刷新上下文面板
                self._update_sidebar()
            case PlanProposed(plan=plan):             # 计划模式提交计划：不铺计划正文（AI 写完会有总结）→ 只存计划 + 弹"计划待批准"条
                self._finalize_batch()                # 收掉写计划那批 → AI 的总结独立可见
                self._commit_live()
                self._pending_plan = plan             # 存起来，供条上"查看计划"弹窗展示（write_file 那块也照常显示）
                self._show_plan_bar()
            case ToolStarted(name="exit_plan"):       # exit_plan 只是"呈交"信号 → 不画工具块（计划另有查看入口）
                pass
            case ToolResult(name="exit_plan"):
                pass
            case ToolStarted(name=name, arguments=args, id=tid):
                self._commit_live()                   # 工具前先把已成形的思考/正文落进历史，保持顺序
                #（此刻 commit 出的思考/正文若挂进了旧批，紧接着的批切换会把它一并折进去，可接受）
                # 按 class 起/续/切批：非折叠类先 finalize 当前批、随后单独挂（不进批）；折叠类同类续、异类切。
                cls = _tool_class(name)
                if cls is None:
                    self._finalize_batch()            # 非折叠类：先收掉当前批，它本身单独逐行、之后 _batch 保持 None
                elif self._batch is None or self._batch.cls != cls:
                    self._finalize_batch()            # 换了类（或没有开着的批）→ 收掉旧批、开新批
                    self._batch = _Batch(cls)
                summary = _clip(_summarize_tool(name, args), 64)
                is_bg = name == "bash" and isinstance(args, dict) and bool(args.get("background"))
                self._set_activity("启动后台任务" if is_bg else f"运行 {name}")
                # 每个工具块：输入框（完整参数，_tool_input_text 按工具定制；edit/write 例外——结果框
                # 本就是参数渲染的完整 diff）+ 结果框。点开都是定高可滚方框。
                boxes = []
                input_text = _tool_input_text(name, args)
                if input_text:
                    boxes.append(ToolBox(Static(Text(input_text, style="cyan")), classes="toolbox"))
                result_content = Static("运行中…")    # 持有内容 Static 的引用，结果到了直接 update
                boxes.append(ToolBox(result_content, classes="toolbox"))
                tr = ToolBlock(name, summary, boxes, background=is_bg, args=args)
                # 按 tool_call_id 存块（并发子 agent 时多个工具同时在跑，结果按完成序乱序回来，靠 id 配回各自的块）
                self._tools_by_id[tid] = (tr, result_content, summary, args)
                if self._batch is not None:           # 折叠类：块进当前批（供 finalize 时算摘要 + 折叠隐藏）
                    self._batch.blocks.append(tr)
                self._mount(tr)                       # _mount 会把它追加进 _batch.widgets（若有开着的批）
            case ToolResult(name=name, result=result, id=tid):
                err = _is_error_result(result)
                entry = self._tools_by_id.pop(tid, None)   # 按 id 取回它自己的块（不再是"当前工具"单指针 → 并发不串位）
                if entry is not None:
                    tr, content, summary, a = entry
                    a = a if isinstance(a, dict) else {}
                    if name == "read_file" and not err:   # 标题补上实际读到的行段
                        rng = _read_range(result)
                        if rng:
                            summary = _clip(f"{a.get('path', '')}  {rng}", 64)
                    elif name == "edit_file" and not err:  # 标题补上改了多少行（+新 -旧，与 box 一致）
                        new_n = len(a.get("new_string", "").splitlines())
                        old_n = len(a.get("old_string", "").splitlines())
                        summary = _clip(f"{a.get('path', '')}  +{new_n} -{old_n}", 64)
                    elif name == "write_file" and not err:  # 标题补上写了多少行（+N，与展开的全绿 + 内容一致）
                        n = len(a.get("content", "").splitlines())
                        summary = _clip(f"{a.get('path', '')}  +{n} 行", 64)
                    denied = result.startswith("错误：用户拒绝")   # 拒绝=已知一行，标 ✗ 但不展开成大框
                    # 中断收尾期间到达的结果 → 整行标黄“已中断”（和乐观翻黄一致）；否则正常 ✓/✗
                    tr.finish(summary, err, expand=False if denied else None,
                              interrupted=self._interrupting)
                    content.update(self._render_tool_body(name, a, result, err))
                # 所有在跑工具都出结果了，才切回“思考中”并归零思考计时——并发批（多个 subagent）要等【最后一个】：
                # 不能第一个结果回来就变“思考中”（其余还在跑）。中断期 _set_activity 会被锁在“正在停止”。
                if not self._tools_by_id:
                    self._set_activity("思考中")
                    self._think_start = time.monotonic()
            case Notice(text=text):
                if "已压缩" in text:                  # 压缩结束 → spinner 从"压缩中"切回"思考中"（计时归零）
                    self._set_activity("思考中")
                # 出错/已打断/已压缩/最大循环等一次性重要提示：独立可见，别折进当时开着的工具批而被藏起来。
                self._mount(Static(Text(f"（{text}）", style="yellow")), batch=False)

    @staticmethod
    def _render_tool_body(name: str, args: dict, result: str, err: bool) -> Text:
        """方框内容：edit/write 从参数渲染 diff（红减绿增），read 原样（已自带行号），
        其余补一个行号栏。这样 diff 不进模型上下文，纯由 TUI 从参数算。"""
        t = Text()
        if err:
            t.append(result.rstrip(), style="red")
            return t
        if name == "edit_file" and isinstance(args, dict):
            for i, ln in enumerate(args.get("old_string", "").splitlines(), 1):
                t.append(f"{i:>4} - {ln}\n", style="red")
            for i, ln in enumerate(args.get("new_string", "").splitlines(), 1):
                t.append(f"{i:>4} + {ln}\n", style="green")
            return t
        if name == "write_file" and isinstance(args, dict):
            for i, ln in enumerate(args.get("content", "").splitlines(), 1):
                t.append(f"{i:>4} + {ln}\n", style="green")
            return t
        if name == "read_file":                       # 结果已是 “行号\t内容”，原样展示
            t.append(result.rstrip(), style="dim")
            return t
        for i, ln in enumerate(result.rstrip().splitlines(), 1):   # bash/grep/glob/其他：补行号栏
            t.append(f"{i:>4}  {ln}\n", style="dim")
        return t

    def _mount(self, widget, batch: bool = True) -> None:
        # 不 await：在消息处理里 await 挂载会和消息泵重入死锁。mount() 直接调度即可，
        # 多次调用按顺序入队、顺序挂载；滚动放到布局刷新后做，保证落在真正的底部。
        log = self.query_one("#log", HistoryLog)
        log.mount(widget)
        # batch=True（折叠内容：工具块 + 夹其间的思考/正文）且有开着的批 → 记进批，finalize 时一起折叠。
        # batch=False（独立消息：用户消息 / Notice 出错·打断·压缩 / 后台完成通知 / 临时流式 Static）→ 不入批、绝不被折叠隐藏。
        if batch and self._batch is not None:
            self._batch.widgets.append(widget)
        if not log.user_scrolled:                                # 用户在回看历史时不硬拽回底（滚回底部自动恢复跟随）
            self.call_after_refresh(log.scroll_end, animate=False)   # 内容同步渲染（_md），高度当场定，一次即可

    def _finalize_batch(self) -> None:
        """把当前批折成一行摘要（方案A：该批从头到尾的所有 widget——工具块 + 夹其间的思考/正文——
        一起折进去，展开可全部还原）。就地隐藏兄弟节点：造 BatchSummary 挂在该批第一个（仍挂载的）
        widget 之前，再把该批所有已挂载 widget display=False。之后 _batch 复位为 None。"""
        batch = self._batch
        self._batch = None
        if batch is None or not batch.blocks:
            return
        # 只保留仍挂载的 widget：_flush_stream 会先挂临时流式 Static、_commit_live 又 remove 换成 markdown，
        # 那个临时 Static 早已 remove/未挂载 → 跳过（否则隐藏/挂 before 会作用在无效节点上）。
        widgets = [w for w in batch.widgets if w.is_mounted]
        if not widgets:
            return
        calls = [(b._name, b._args) for b in batch.blocks]
        icon, detail = _batch_summary(batch.cls, calls)
        interrupted = any(getattr(b, "_interrupted", False) for b in batch.blocks)   # 批内有中断黄块 → 折叠行标黄
        summary = BatchSummary(icon, detail, widgets, interrupted)
        log = self.query_one("#log", VerticalScroll)
        log.mount(summary, before=widgets[0])         # 摘要行挂在该批第一个 widget 之前（就地占位）
        for w in widgets:                             # 折叠：该批全部隐藏，展开时 BatchSummary.on_click 还原
            w.display = False

    def _pinned_anchor(self) -> Static | None:
        """悬浮头的锚点 = 当前视口内容【所属】的那条用户消息：
        视口内已有任意用户消息可见 → None（不显示，别重复）；
        否则取视口【上方最近】的一条（DOM 序扫描，最后一条在顶上方的就是它）——
        翻到历史某段回复中间时显示的就是那段对应的问题，而不是永远最新那条。"""
        log = self.query_one("#log", VerticalScroll)
        top = log.scroll_offset.y
        bottom = top + log.scrollable_content_region.height
        anchor = None
        for w in self.query("#log .user-msg"):
            if not w.is_mounted or not w.display:
                continue
            vr = w.virtual_region              # 虚拟（内容）坐标：不随滚动变，和 scroll_offset 同空间
            if vr.height <= 0:
                continue
            if vr.y < bottom and vr.y + vr.height > top:
                return None                    # 有可见的用户消息 → 不需要悬浮头
            if vr.y + vr.height <= top:
                anchor = w                     # 完全在视口上方：越靠后越近，循环完即"上方最近"
        return anchor

    def _sync_pinned_header(self) -> None:
        # 视口里一条用户消息都看不见时（长回复/工具输出中段）才悬浮置顶：内容 = 视口上方最近那条
        # （= 当前看的这段对话对应的问题）；有任何用户消息可见则不显示。
        cm = self.query_one("#current-msg", Static)
        anchor = self._pinned_anchor()
        if anchor is not None:
            cm.update(anchor.render())
            cm.display = True
        else:
            cm.display = False

    def _flush_stream(self) -> None:
        # 把累积的流式正文刷进 #log（直接在对话流里，连续不断层；思考期间无正文则只有 spinner）。
        # 正文首片到达时：先把此前的思考收成一行 Thinking（保证"思考"在"正文"之前），再挂一个流式
        # Static。之后每次更新它 + #log 贴底（它是 #log 自己的子控件，长高时锚定自然跟随）。
        # 流式期间用纯文本（不渲染 markdown，避免半截代码块）；轮末/工具前 commit 时再换成 markdown。
        if not self._answer:
            return
        if self._stream_widget is None:
            self._commit_reasoning()
            self._stream_widget = Static()
            self._mount(self._stream_widget, batch=False)   # 临时流式 Static（commit 时被 remove 换 markdown）→ 不入批，避免异步剪枝时序问题
        self._stream_widget.update(Text(self._answer))
        log = self.query_one("#log", HistoryLog)
        if not log.user_scrolled:                           # 回看历史时流式不贴底
            log.scroll_end(animate=False)

    def _commit_reasoning(self) -> None:
        # 思考收成一行折叠的 “💭 思考 (N 字)”；幂等（提交后清空 _reasoning）。
        if self._reasoning.strip():
            secs = max(0.0, self._reasoning_end - self._think_start)
            self._mount(Thinking(self._reasoning.strip(), secs))
        self._reasoning = ""

    def _commit_live(self) -> None:
        # 段落落定（工具前 / 本轮结束）：思考落定；正文换成 markdown 渲染。
        # 两种到达方式都兜住：① 经过流式刷新（有 _stream_widget）→ 原地替换它；
        # ② deltas 太快、tick 还没刷就 commit（_stream_widget 仍 None）→ 直接挂。
        self._commit_reasoning()
        if self._answer.strip():
            log = self.query_one("#log", VerticalScroll)
            md = _md(self._answer.strip())
            if self._stream_widget is not None:
                log.mount(md, after=self._stream_widget)
            else:
                log.mount(md)
            # 有开着的批 → 这段正文夹在批中间，记进批一起折叠（临时流式 Static 已 remove、finalize 会跳过它）。
            if self._batch is not None:
                self._batch.widgets.append(md)
            if not self.query_one("#log", HistoryLog).user_scrolled:   # 回看历史时落定也不贴底
                self.call_after_refresh(log.scroll_end, animate=False)
        if self._stream_widget is not None:
            self._stream_widget.remove()
        self._stream_widget = None
        self._answer = ""
        self.call_after_refresh(self._sync_pinned_header)   # 落定后按可见性更新悬浮头

    # ---- 右侧信息面板 ----

    def _update_sidebar(self) -> None:
        """刷新右侧面板的信息区：模型 / 上下文用量(占比着色，标题行标注自动压缩点) / MCP。"""
        cfg = self.agent.config
        used, limit = self.agent.context_tokens, cfg.context_limit
        estimated = used == 0                       # 没有精确 usage（后端不回/刚压缩完/会话刚开）→ 本地估算兜底
        if estimated:
            used = estimate_tokens(self.agent.messages, self.agent.tools.schemas())
        ratio = used / limit if limit else 0.0
        # 着色按“离压缩触发点多近”：超阈值=红（下轮就压），接近=黄，其余=绿
        ctx_style = "red" if ratio >= cfg.compact_threshold \
            else "yellow" if ratio >= cfg.compact_threshold * 0.85 else "green"

        t = Text()
        t.append("模型", style="bold")
        t.append("  管理", style=Style(color="yellow", underline=False,   # 同 MCP/技能区"管理"样式；点开 /config
                                     meta={"@click": "app.open_config"}))
        t.append("\n")
        live_model = self._live_backend().model      # agent 里实时后端（热切后即时反映）
        if live_model:
            t.append(f"  {_truncate(live_model, self._panel_text_width - 2)}\n\n", style="cyan")
        else:   # 没配模型（.env 有也不算）：引导去 /config；[点击此处] 直接开配置弹窗
            t.append("  未选择模型", style="yellow")
            t.append("[点击此处]", style=Style(color="yellow", bold=True, underline=True,
                                           meta={"@click": "app.open_config"}))
            t.append("\n  或输入 /config 配置\n\n", style="yellow")
        t.append("上下文", style="bold")
        if estimated:
            t.append(" 估", style="dim")            # 数字是本地估算（CJK≈1字/token），非后端回的精确 usage
        t.append(f"  ({cfg.compact_threshold * 100:.0f}%自动压缩)\n", style="dim")
        t.append(f"  {used} / {limit}  {ratio * 100:.0f}%\n\n", style=ctx_style)
        t.append("MCP", style="bold")
        t.append("  管理", style=Style(color="yellow", underline=False,   # 同技能区的"管理"样式；关掉链接默认下划线
                                     meta={"@click": "app.open_mcp"}))
        if self._mcp_errors:                        # 有 server 没连上 → 标红计数（具体原因点"管理"进 /mcp 看）
            t.append(f"  失败 {len(self._mcp_errors)}", style="red")
        if not self._mcp_clients:                   # 未连/连接中：补一行状态。已连的不加尾随换行——
            t.append(f"\n  {self._mcp_status}", style="dim")   # Static 末尾的 \n 会渲成空行，和下方 server 行隔开一大截
        self.query_one("#sb-info", Static).update(t)

    # ---- 底部 spinner / 提示 ----

    def _show_idle_hint(self) -> None:
        inp = self.query_one("#input", InputArea)
        hint = self.IDLE_HINT if inp.has_focus else self.CONV_HINT   # 焦点在对话区 → 换成对话区提示
        self.query_one("#work", Static).update(Text(hint, style="dim"))

    def _refresh_input_border(self) -> None:
        """输入框边框：聚焦=模式色亮边，失焦=模式色压暗——作焦点指示（Tab 到对话区时边框会暗下去）。"""
        inp = self.query_one("#input", InputArea)
        color = MODE_COLORS.get(self._mode, "cyan")
        inp.styles.border = ("round", color if inp.has_focus
                             else Color.parse(color).blend(Color(0, 0, 0), 0.55).hex)

    def _on_focus_change(self) -> None:
        """输入框 ↔ 对话区 焦点变了：刷新边框 + 提示（跑动中 #work 显 spinner，别覆盖）。"""
        self._refresh_input_border()
        if not self._turn_busy:
            self._show_idle_hint()

    def _set_activity(self, label: str) -> None:
        # 中断收尾期间锁定“正在停止”，别被还在陆续到达的残余事件（工具结果/思考片）改回“思考中”。
        if self._interrupting and label != "正在停止":
            return
        # 活动标签变了才重置阶段起点 → spinner 计时随阶段（思考中/运行X/回复中）归零，
        # 不再一路累加整轮时间。标签没变（如连续的思考片）不重置。
        if label != self._work_activity:
            self._work_activity = label
            self._phase_start = time.monotonic()

    def _start_work(self, activity: str) -> None:
        self._work_activity = activity
        self._phase_start = time.monotonic()
        self._think_start = self._phase_start    # 提交即开始等模型思考
        self._spin_i = 0
        if self._work_timer is None:
            self._work_timer = self.set_interval(0.1, self._tick)
        self._tick()

    def _tick(self) -> None:
        if self._live_dirty:                 # 把累积的流式文本批量刷进 #log（≤10/s），
            self._flush_stream()             # 而不是每来一片都重建整段文本——否则主线程被刷新
            self._live_dirty = False         # 拖垮，spinner 定时器被饿死、计时卡住
        self._spin_i = (self._spin_i + 1) % len(self.SPIN)
        el = time.monotonic() - self._phase_start
        # 思考阶段额外显示当前已生成的思考字数（_reasoning 随片增长，commit/工具后归零）
        extra = f" · {len(self._reasoning)} 字" if self._work_activity == "思考中" else ""
        self.query_one("#work", Static).update(Text(
            f"{self.SPIN[self._spin_i]} {self._work_activity} · {el:.0f}s{extra}   Ctrl+C 打断",
            style="yellow"))

    def _stop_work(self) -> None:
        if self._work_timer is not None:
            self._work_timer.stop()
            self._work_timer = None
        self._show_idle_hint()

    def stop_mcp(self) -> None:
        for c in self._mcp_clients:
            try:
                c.stop()
            except Exception:
                pass
        self._mcp_clients = []


if __name__ == "__main__":
    app = MecodeApp()
    try:
        app.run()
    finally:
        app.stop_mcp()             # 退出时关掉 MCP 子进程，别留僵尸
