"""工具系统：定义工具 + 提供给模型的 schema + 按名字执行。

两件事：
1. schemas() → 把工具描述成 OpenAI 格式，发给模型（模型靠这个决定调谁、填什么参数）
2. execute(name, args) → 模型要调某工具时，真正去跑对应的 Python 函数，返回字符串结果
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

MAX_WRITE_BYTES = 10 * 1024 * 1024   # 写文件上限 10MB，防手滑写出超大文件
MAX_READ_BYTES = 10 * 1024 * 1024    # 读文件上限 10MB
DEFAULT_READ_LINES = 200             # 不指定 limit 时一次最多返回的行数
READ_FILE_MAX_OUTPUT_CHARS = 12000   # read_file 结果截断上限：够装 ~200 行（≈3000 token），不被全局 4000 砍半
BASH_MAX_OUTPUT_CHARS = 6000         # bash 结果截断上限（≈1500 token）；keep_tail 保证结尾报错/状态留住
DEFAULT_BASH_TIMEOUT = 30            # bash 默认超时秒数
GREP_IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                    ".pytest_cache", ".idea", ".vscode", ".mecode"}   # grep 跳过的噪音目录
GREP_MAX_RESULTS = 200               # grep 默认最多返回多少条
GREP_MAX_OUTPUT_CHARS = 8000         # grep 结果截断上限
GLOB_MAX_RESULTS = 100               # glob 默认最多返回多少个文件
GLOB_MAX_OUTPUT_CHARS = 6000         # glob 结果截断上限


def truncate_output(text: str, max_chars: int, keep_tail: int) -> str:
    """工具结果【入场截断】：超长就留头+尾、中间挖掉，并标注省略量。

    为什么留头也留尾：不同工具关心的位置不同——read_file 看开头、
    bash/跑测试看结尾（报错和最终状态在末尾），只留头会丢掉关键信息。
    为什么按字符不按 token：省去 tokenizer 依赖；中文按“字”算也比 byte/4 公平。
    标记必须显眼，否则模型会误以为“内容到此为止”，并告诉它如何取更多。
    """
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - keep_tail]   # 前 3200
    tail = text[-keep_tail:]               # 后 800
    # 对齐到行边界，别显示半行：头尾都丢掉贴着中间空洞的那半行。
    # 兜底：找不到换行符 = 超长单行，退回硬切（保证结果始终有界，不会被一行撑爆）。
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]                   # 丢掉被切断的尾半行
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]               # 丢掉被切断的首半行
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n"
        f"…[输出过长，已省略中间约 {omitted} 字；如需完整内容请缩小范围重新调用]…\n"
        f"{tail}"
    )


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON Schema，描述参数
    handler: Callable[[dict], str]        # 真正执行的函数：吃参数 dict，吐字符串
    max_output_chars: int | None = None   # 该工具结果的截断上限（字符）；None=用 agent 全局默认
    takes_slot: bool = False              # True=handler 还吃 (slot, bg)：ProcSlot 起进程登记/可打断杀 + BackgroundManager 转后台（仅 bash）
    read_only: bool = False               # True=纯读不改任何状态（文件/网络/内存快照）→ agent 把相邻的只读调用并发执行


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        """给模型看的工具清单（OpenAI 函数调用格式）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in list(self._tools.values())   # 快照遍历：后台线程（MCP 连好）注册时并发安全
        ]

    def is_read_only(self, name: str) -> bool:
        """该工具是否声明为只读（Tool.read_only）。agent 执行层据此把连续的只读调用并发跑；
        未注册的名字返回 False（走串行，错误照常由 execute 喂回）。"""
        tool = self._tools.get(name)
        return tool is not None and tool.read_only

    def output_limit(self, name: str, default: int) -> int:
        """该工具结果的截断上限：工具自带 max_output_chars 就用它，否则用 default。"""
        tool = self._tools.get(name)
        if tool is not None and tool.max_output_chars is not None:
            return tool.max_output_chars
        return default

    def execute(self, name: str, arguments: dict, slot: "ProcSlot | None" = None,
                bg: "BackgroundManager | None" = None) -> str:
        """按名字执行工具。出错也返回字符串（喂回给模型，让它自己纠错）。
        slot：前台进程槽；bg：后台任务管理器（都由 agent 注入）。二者一起给声明了 takes_slot 的工具
        （bash）：slot 让前台进程可被当场杀，bg 让 background:true 把命令转后台跑。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：没有名为 {name} 的工具"
        try:
            if tool.takes_slot:
                return tool.handler(arguments, slot, bg)   # type: ignore[call-arg]
            return tool.handler(arguments)
        except Exception as e:  # 工具崩了不让整个 agent 崩，把错误喂回模型
            return f"工具 {name} 执行出错：{type(e).__name__}: {e}"


# ---- 辅助函数（供下面的工具执行函数调用）----

def _is_binary(data: bytes) -> bool:
    # 头 8KB 出现 NUL 字节就当二进制
    return b"\x00" in data[:8192]


def _diff_summary(before: str, after: str) -> str:
    # 用 difflib 算增删行数，给模型一个“改了什么”的反馈
    added = removed = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return f"+{added} -{removed} 行"


def _detect_shell() -> tuple[list[str], str]:
    # 初始化时探测可用 shell，返回 (argv 前缀, 给模型的语法提示)；前缀 + [command] 即完整命令。
    # 优先级：POSIX shell(含 Windows 的 git-bash) > PowerShell > cmd。
    # 不能"按命令自动切 cmd/PowerShell"——从命令串没法可靠判断语法，只能选定一个并告知模型。
    # 例子只用运行类命令：读文件/搜内容/找文件有专用工具(read_file/grep/glob)，不在 bash 里 cat/grep/find。
    if os.name != "nt":
        return ["/bin/sh", "-c"], "Unix/bash 命令（mkdir、rm、git、python 等）"
    # which 命中的 bash 可能不是 git-bash：
    #  - C:\Windows\System32\bash.exe = WSL 启动器：把 cwd 映射到 /mnt/c、git 跨文件系统边界报错，
    #    没装发行版时还会空输出退 1。PowerShell 启动的进程里 System32 在 PATH 更靠前，极易命中它。
    #  - WindowsApps\bash.exe = 应用执行别名占位（WSL 未装时打开商店）。
    # 这两类都不是合适的 POSIX shell，排除掉；which 没给出合适的就兜底查 git-bash 常见安装位置。
    bash = shutil.which("bash") or shutil.which("sh")
    if bash and ("\\system32\\" in bash.lower() or "windowsapps" in bash.lower()):
        bash = None
    if not bash:
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.exists(p):
                bash = p
                break
    if bash:                                      # git-bash：-l 加载 profile 配好 PATH，原生处理 Windows 路径
        return [bash, "-lc"], ("Unix/bash 命令（即便系统是 Windows，bash 也是 POSIX shell，"
                               "一律用 Unix 语法 mkdir/rm/git/python 等，不要用 dir/del 这类 "
                               "cmd 或 PowerShell 命令）")
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh:
        # -ExecutionPolicy Bypass：内联 -Command 本就不受策略限制，加上它兜底脚本场景
        return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"], \
            "PowerShell 命令（New-Item、Remove-Item、运行 python/git 等），不要用 Unix 命令"
    return ["cmd.exe", "/C"], "Windows cmd 命令（mkdir、del、运行 python/git 等），不要用 Unix 命令"


def _bash_command(command: str) -> list[str]:
    prefix, _ = _detect_shell()
    return prefix + [command]


# Windows 上"命令找不到"的约定退出码：9009（cmd 的"不是内部或外部命令"就是它；商店的应用执行
# 别名在对应应用未安装时也返回它）。POSIX 退出码只有 8 位，git-bash 会把它截断：9009 & 0xFF = 49，
# 所以同一件事在 bash 下显示为 49、在 PowerShell/cmd 下显示为 9009。
_WIN_NOT_FOUND_CODES = (9009, 49)


def _exit_code_note(code: int) -> str:
    """把 Windows 上含义明确、但数字本身完全看不出名堂的退出码翻成人话。
    只在命令【无任何 stdout/stderr】时补——那种情况模型手里只剩一个裸数字，无从判断。

    刻意【只翻译、不建议】：说清这个码通常意味着什么就停，下一步是换命令名、装依赖、
    还是查 PATH，交给模型自己判断。harness 的职责是把事实讲准，替它选下一步会限制它的思路。

    措辞用"通常表示"而非断言：任何程序都可以自行 exit(49)，实测 `python -c "sys.exit(49)"`
    的签名（退出码 49 + 空输出）与商店别名占位完全相同，无法区分，所以不能把话说死。"""
    if os.name != "nt" or code not in _WIN_NOT_FOUND_CODES:
        return ""
    # 只给含义，不给词源：49 是 9009 截断这件事写在上面的常量注释里（给读源码的人），
    # 对模型不构成任何行动信息；也不展开"名字不存在 or 程序没装"——那是同义反复 + 越俎代庖。
    return f"（退出码 {code} 在 Windows 上通常表示「命令找不到 / 不是内部或外部命令」。）"


def _bash_description() -> str:
    # 执行和描述共用 _detect_shell，二者永远一致：选哪个 shell，就让模型用哪种语法。
    _, hint = _detect_shell()
    return ("在 shell 里执行一条命令，返回 stdout、stderr 和非零退出码。"
            f"默认超时 30 秒，可用 timeout（秒）调整。当前请用 {hint}。")


def _kill_tree(proc: subprocess.Popen) -> None:
    # 杀子进程及其所有后代，防孙进程（后台 daemon）残留。
    # Windows: taskkill /T 杀整棵树；Unix: 子进程自成进程组，整组 SIGKILL。
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


class ProcSlot:
    """前台子进程槽 + 打断信号。让"正在跑的 bash"能被【另一个线程】当场杀掉:
    bash 起进程时 register(proc)、结束时 clear();打断(Ctrl+C)/退出(Ctrl+Q)时由 UI 线程
    调 kill() 直接杀掉它 → worker 的 communicate 立即返回。这样无需轮询、零 CPU、瞬时响应。
    stop_requested() 读同一个打断 Event(agent 的 _interrupt),供 bash"起进程前"先查一道。"""

    def __init__(self, interrupt: threading.Event) -> None:
        self._interrupt = interrupt
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()      # register/clear(worker线程) 与 kill(UI线程) 跨线程共访

    def stop_requested(self) -> bool:
        return self._interrupt.is_set()

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc

    def clear(self) -> None:
        with self._lock:
            self._proc = None

    def kill(self) -> None:
        """杀掉当前登记的前台进程(若有)。打断/退出时调,跨线程安全。"""
        with self._lock:
            if self._proc is not None:
                _kill_tree(self._proc)


def _iter_files(base: Path, glob: str | None) -> Iterator[Path]:
    # 递归遍历 base 下的文件，跳过噪音目录；有 glob 就按文件名过滤。
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in GREP_IGNORE_DIRS]   # 原地裁剪 → os.walk 不进这些目录
        for name in files:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            yield Path(root) / name


def _expand_braces(pattern: str) -> list[str]:
    # 展开一组花括号：*.{py,md} → ["*.py", "*.md"]（一层，pathlib.glob 不支持花括号）。
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[: m.start()], pattern[m.end():]
    return [pre + opt + post for opt in m.group(1).split(",")]


# ---- 工具执行函数 ----

def _read_file(args: dict) -> str:
    # 行窗口读：默认从 offset 行起返回 limit 行，每行带 cat -n 行号前缀（行号仅显示用，
    # 不属于文件真实内容）。大文件不一次性灌进上下文，模型用 offset/limit 翻页。
    path = Path(args["path"])
    if not path.is_file():
        return f"错误：文件不存在 {path}"
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        return f"错误：文件过大（{size} 字节 > 上限 {MAX_READ_BYTES} 字节）{path}"
    raw = path.read_bytes()
    if _is_binary(raw):
        return f"错误：{path} 像是二进制文件，无法按文本读取"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    start = min(int(args.get("offset", 0)), total)
    end = min(start + int(args.get("limit", DEFAULT_READ_LINES)), total)
    if start >= end:
        return f"# {path}（共 {total} 行）\n（此范围无内容）"
    header = f"# {path}（共 {total} 行，显示第 {start + 1}-{end} 行）"
    if end < total:
        header += f"；还有更多，用 offset={end} 继续读"
    body = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(lines[start:end]))
    return f"{header}\n{body}"


def _write_file(args: dict) -> str:
    # 整文件覆盖写：自动建父目录、限大小、区分 create/update，
    # 返回 diff 摘要（增删行数）让模型“看到”自己改了什么。
    path = Path(args["path"])
    content = args.get("content", "")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"错误：内容超过 {MAX_WRITE_BYTES // (1024 * 1024)}MB 上限，拒绝写入 {path}"
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)   # 父目录不存在就建（含多级）
    path.write_text(content, encoding="utf-8")
    n = len(content.splitlines())
    if original is None:
        return f"已创建 {path}（{n} 行）"
    return f"已更新 {path}（{_diff_summary(original, content)}，现共 {n} 行）"


# ---------- edit_file 的替换器级联 ----------
# 模型给的 old_string 常"差一点"（行尾空格丢了/缩进层级记错/tab vs 空格/把 \n 写成字面反斜杠n），
# 精确匹配一票否决会让它反复重试甚至退化成 write_file 整文件重写。参考 Claude Code / opencode 的
# replacer cascade：按【严格 → 宽松】依次尝试多个匹配器，每个匹配器产出"文件里实际存在的候选块"，
# 【替换的是候选块原文】（不是模型给的字符串）→ 文件原有缩进/空白天然保留。
# 安全底线：宽松匹配下候选必须在文件中【唯一】（歧义跳过/最终报"不唯一"）；候选块比 old_string
# 大得不成比例的拒绝（宽锚点圈中一大段 = 危险）。

def _iter_line_blocks(text: str, n: int):
    """产出 (起始行号, 连续 n 行拼成的原文块)。级联里多个匹配器共用。"""
    lines = text.split("\n")
    for i in range(len(lines) - n + 1):
        yield i, "\n".join(lines[i:i + n])


def _find_line_trimmed(text: str, old: str):
    """级联②行修剪：逐行 strip 后比对（容忍行尾空格/缩进差异），产出原文块。"""
    old_lines = [ln.strip() for ln in old.split("\n")]
    if old_lines and old_lines[-1] == "":
        old_lines.pop()
    if not old_lines:
        return
    for _, block in _iter_line_blocks(text, len(old_lines)):
        if [ln.strip() for ln in block.split("\n")] == old_lines:
            yield block


def _find_block_anchor(text: str, old: str):
    """级联③块锚点：首尾行（trim）当锚，块大小容差 25%，中间行平均相似度 ≥0.65 才算命中——
    长块中间记错一两个字也能中。≥3 行才启用（短块锚点没意义）。"""
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines.pop()
    if len(old_lines) < 3:
        return
    first, last = old_lines[0].strip(), old_lines[-1].strip()
    size = len(old_lines)
    delta = max(1, size // 4)
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() != first:
            continue
        for j in range(i + 2, min(i + size + delta + 1, len(lines))):
            if lines[j].strip() != last:
                continue
            mid_n = min(size, j - i + 1) - 2
            if mid_n > 0:
                sim = sum(
                    difflib.SequenceMatcher(None, lines[i + k].strip(), old_lines[k].strip()).ratio()
                    for k in range(1, mid_n + 1)) / mid_n
            else:
                sim = 1.0
            if sim >= 0.65:
                yield "\n".join(lines[i:j + 1])
            break                              # 锚定第一个匹配的尾行即可


def _find_ws_normalized(text: str, old: str):
    """级联④空白归一：连续空白折成一个再比（容忍 tab/空格混用、多余空格）。"""
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    old_n = norm(old)
    n = len(old.split("\n"))
    for _, block in _iter_line_blocks(text, n):
        if norm(block) == old_n:
            yield block


def _find_indent_flexible(text: str, old: str):
    """级联⑤缩进弹性：整块去掉公共缩进后比对（模型把嵌套层级记浅/记深了）。"""
    def dedent(s: str) -> str:
        lines = s.split("\n")
        nonempty = [ln for ln in lines if ln.strip()]
        if not nonempty:
            return s
        m = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
        return "\n".join(ln[m:] if ln.strip() else ln for ln in lines)
    old_d = dedent(old)
    n = len(old.split("\n"))
    for _, block in _iter_line_blocks(text, n):
        if dedent(block) == old_d:
            yield block


def _find_escape_normalized(text: str, old: str):
    """级联⑥转义归一：模型把换行/制表写成字面 \\n \\t（弱模型高发）→ 还原成真字符后再找。"""
    unescaped = re.sub(r"\\(n|t|r)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}[m.group(1)], old)
    if unescaped != old and unescaped in text:
        yield unescaped


# 顺序原则：越严格、越能保留代码物理结构（换行/相对缩进/行内内容）的匹配器越靠前——
# 行修剪/转义归一/缩进弹性 的行内内容都是【精确】的（只容忍空白或转义形态），空白归一连换行都
# 折叠（物理结构全丢），块锚点更是允许中间行【真实内容差异】（相似度≥0.65 即可）→ 垫底。
# （opencode 原版把 BlockAnchor 排第二，按此原则站不住，故不照抄。）
_EDIT_CASCADE = (
    ("行修剪", _find_line_trimmed),
    ("转义归一", _find_escape_normalized),
    ("缩进弹性", _find_indent_flexible),
    ("空白归一", _find_ws_normalized),
    ("块锚点", _find_block_anchor),
)


def _disproportionate(candidate: str, old: str) -> bool:
    """候选块比 old_string 大得不成比例 → 拒绝（宽锚点圈中一大段，替换会吞掉不该动的代码）。"""
    c_lines, o_lines = candidate.count("\n") + 1, old.count("\n") + 1
    if c_lines >= max(o_lines + 3, o_lines * 2):
        return True
    return o_lines > 1 and len(candidate.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def _closest_snippet(text: str, old: str) -> str:
    """找不到时给"最相似片段"提示：模型照着它修正 old_string，下一轮一次成。"""
    n = len(old.split("\n"))
    best, best_r = "", 0.0
    for _, block in _iter_line_blocks(text, n):
        r = difflib.SequenceMatcher(None, block, old).ratio()
        if r > best_r:
            best, best_r = block, r
    if best_r < 0.5:
        return ""
    return f"\n文件中最相似的片段（相似度 {best_r:.0%}，可对照修正 old_string）：\n{best}"


def _edit_file(args: dict) -> str:
    # 局部替换 old_string→new_string。精确匹配优先；失败走替换器级联（见上）。
    # 校验：文件须存在、old 非空、old≠new；默认要求唯一（防改错地方），replace_all=true 全替
    # （全替只走精确匹配——宽松匹配的"全部"边界不可控）。
    path = Path(args["path"])
    old, new = args["old_string"], args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    if not path.is_file():
        return f"错误：文件不存在 {path}"
    if old == "":
        return "错误：old_string 不能为空（新建文件请用 write_file）"
    if old == new:
        return "错误：old_string 与 new_string 相同，无需修改"
    text = path.read_text(encoding="utf-8")

    count = text.count(old)
    if count > 0:                            # 级联①精确：命中就走原有逻辑
        if count > 1 and not replace_all:
            return (f"错误：old_string 在 {path} 中出现 {count} 次、不唯一。"
                    f"请补更多上下文使其唯一，或设 replace_all=true 全部替换")
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(new_text, encoding="utf-8")
        return f"已编辑 {path}（替换 {count if replace_all else 1} 处，{_diff_summary(text, new_text)}）"

    if replace_all:                          # 全替不走宽松级联
        return f"错误：在 {path} 中找不到 old_string（replace_all 只支持精确匹配）"

    for level, finder in _EDIT_CASCADE:
        # 按级收集【去重】候选。歧义两种形态都要拦：①同一块文本出现多次（count>1）；
        # ②该级产出多个【不同】的候选块（各自唯一也不行——不知道模型想改哪处，静默改第一处=改错地方）。
        cands = list(dict.fromkeys(cand for cand in finder(text, old) if cand in text))
        if not cands:
            continue
        if len(cands) > 1 or text.count(cands[0]) != 1:
            return (f"错误：old_string 在 {path} 中有多处近似匹配（{level}级），无法确定改哪处。"
                    f"请补更多上下文使其唯一")
        cand = cands[0]
        if _disproportionate(cand, old):
            return ("错误：宽松匹配圈中的片段远大于 old_string，为安全拒绝替换。"
                    "请 read_file 重读后提供完整精确的 old_string")
        new_text = text.replace(cand, new, 1)
        path.write_text(new_text, encoding="utf-8")
        return (f"已编辑 {path}（替换 1 处，经{level}匹配——old_string 与原文有细微差异已容忍，"
                f"{_diff_summary(text, new_text)}）")
    return (f"错误：在 {path} 中找不到 old_string（需与原文一致，含缩进/换行）。"
            f"{_closest_snippet(text, old)}")


def _bash(args: dict, slot: "ProcSlot | None" = None,
          bg: "BackgroundManager | None" = None) -> str:
    # 起子进程把命令交给 shell 执行，抓 stdout/stderr/退出码。输出按字节抓再 utf-8 容错解码，
    # 免得 Windows 下编码不是 utf-8 时崩。超时则杀掉整棵进程树（含孙进程）并报错。
    # slot：前台进程槽。起进程前先查打断标志（窗口①：已打断就别起）；起后登记进 slot，使打断
    # (Ctrl+C)/退出(Ctrl+Q)能由 UI 线程当场 kill 它 → 这里的 communicate 立即返回，无需轮询。
    # bg：后台任务管理器。background:true 时把命令交给它、立即返回（不阻塞本轮），见下。
    command = args.get("command", "").strip()
    if not command:
        return "错误：command 不能为空"
    # 后台模式：长/不定时命令（dev server、watch、长构建测试）转后台，立即返回 #id、不阻塞。
    # 默认不限时（dev server 等要长跑）；模型可传 timeout 做有界后台任务。停止用 kill_bgtask(id)。
    if args.get("background"):
        if bg is None:                     # 子 agent 内（bg=None）：禁后台，显式让它前台跑（否则起的后台任务无人 reap）
            return "错误：当前环境（子 agent 内）不支持后台 bash。请去掉 background 参数、前台运行此命令并等它完成。"
        tid = bg.start(command, timeout=args.get("timeout"))   # 不传 timeout 则 None=不限时
        return (f"已在后台启动 · 任务 #{tid} · 运行中"
                f"（完成或卡住会通知你；需要时用 kill_bgtask({tid}) 终止）")
    if slot is not None and slot.stop_requested():     # 窗口①：起进程之前就已被打断 → 不起
        return "（已被用户打断，未执行命令）"
    timeout = int(args.get("timeout", DEFAULT_BASH_TIMEOUT))
    # 子进程脱离调用方控制台：stdin=DEVNULL（不让 python 等借继承的控制台 stdin 去改 Windows 控制台
    # 输入模式，那会搞坏 TUI 致其冻死）；Windows 再加 CREATE_NO_WINDOW 给独立隐藏控制台。
    # Unix 下 start_new_session 让子进程自成进程组，超时能整组杀；Windows 用 taskkill /T。
    # 输出走 PIPE/communicate 捕获，不受这些影响。
    extra = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        extra["start_new_session"] = True
    proc = subprocess.Popen(
        _bash_command(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **extra,
    )
    if slot is not None:
        slot.register(proc)            # 登记前台进程：外部（UI 线程）可据此直接 kill
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)   # 回收输出和进程，避免僵尸/管道挂起
        except subprocess.TimeoutExpired:
            pass
        return f"错误：命令超时（>{timeout} 秒）已被终止：{command}"
    except KeyboardInterrupt:           # CLI(chat.py) 的 Ctrl+C 直接打进 communicate → 杀进程再上抛
        _kill_tree(proc)
        raise
    finally:
        if slot is not None:
            slot.clear()                # 无论正常/超时/打断，结束都从槽里摘掉
    if slot is not None and slot.stop_requested():     # 窗口②：被外部 kill（TUI 打断）→ communicate 正常返回
        # 保留【中断前】已抓到的部分输出（out_b/err_b 就在手边），附一句打断说明——模型能看到"停之前干到哪、
        # 报没报错"，比只回一句"已终止"更有判断依据。
        out = out_b.decode("utf-8", errors="replace").rstrip()
        err = err_b.decode("utf-8", errors="replace").rstrip()
        parts = [p for p in (out, (f"[stderr]\n{err}" if err else "")) if p]
        parts.append("（命令被用户打断，已终止；以上为中断前的输出）")
        return "\n".join(parts)
    out = out_b.decode("utf-8", errors="replace").rstrip()
    err = err_b.decode("utf-8", errors="replace").rstrip()
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if proc.returncode != 0:
        parts.append(f"(exit code: {proc.returncode})")
        if not out and not err:            # 无任何输出、只剩裸退出码 → 能翻译的就翻成人话
            note = _exit_code_note(proc.returncode)
            if note:
                parts.append(note)
    return "\n".join(parts) if parts else "（命令执行成功，无输出）"


def _grep(args: dict) -> str:
    # 内容正则搜索：递归走文件、跳二进制和噪音目录，按 output_mode 给结果。
    pattern = args.get("pattern", "")
    if not pattern:
        return "错误：pattern 不能为空"
    try:
        regex = re.compile(pattern, re.IGNORECASE if args.get("case_insensitive") else 0)
    except re.error as e:
        return f"错误：正则不合法：{e}"
    base = Path(args.get("path", "."))
    if not base.exists():
        return f"错误：路径不存在 {base}"
    mode = args.get("output_mode", "files_with_matches")
    limit = int(args.get("head_limit", GREP_MAX_RESULTS))
    files = [base] if base.is_file() else _iter_files(base, args.get("glob"))

    results: list[str] = []
    truncated = False
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        if _is_binary(raw):
            continue
        hits = [(i, ln) for i, ln in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1)
                if regex.search(ln)]
        if not hits:
            continue
        if mode == "content":
            results += [f"{f}:{i}: {ln.rstrip()}" for i, ln in hits]
        elif mode == "count":
            results.append(f"{f}: {len(hits)} 处")
        else:                       # files_with_matches
            results.append(str(f))
        if len(results) >= limit:
            truncated = True
            results = results[:limit]
            break

    if not results:
        return f"没有匹配 /{pattern}/ 的内容（在 {base} 下）"
    label = {"content": "行匹配", "count": "个文件"}.get(mode, "个文件含匹配")
    suffix = "（已截断，缩小范围或加 glob 过滤）" if truncated else ""
    return f"匹配 /{pattern}/：{len(results)} {label}{suffix}\n" + "\n".join(results)


def _glob(args: dict) -> str:
    # 按路径模式找文件（支持 ** 递归、{a,b} 花括号）。跳噪音目录，按修改时间倒序，只返文件。
    pattern = args.get("pattern", "")
    if not pattern:
        return "错误：pattern 不能为空"
    base = Path(args.get("path", "."))
    if not base.is_dir():
        return f"错误：目录不存在 {base}"
    limit = int(args.get("head_limit", GLOB_MAX_RESULTS))
    seen: set[Path] = set()
    matches: list[Path] = []
    try:
        for pat in _expand_braces(pattern):
            for p in base.glob(pat):
                if not p.is_file() or p in seen or set(p.parts) & GREP_IGNORE_DIRS:
                    continue
                seen.add(p)
                matches.append(p)
    except ValueError as e:
        return f"错误：模式不合法：{e}"
    if not matches:
        return f"没有匹配 {pattern} 的文件（在 {base} 下）"
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)   # 最近改的在前
    truncated = len(matches) > limit
    suffix = "（已截断）" if truncated else ""
    return (f"匹配 {pattern}：{len(matches)} 个文件{suffix}\n"
            + "\n".join(str(p) for p in matches[:limit]))


def _exit_plan(args: dict) -> str:
    # 正常路径由 Agent 拦截 exit_plan（落盘计划、发 PlanProposed、结束本轮），不会走到这个 handler。
    # 仅作兜底：无 Agent 拦截时（理论上不会）返回一句，避免"没有该工具"。
    return "计划已提交，等待用户审阅。"


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        name="read_file",
        description="读取本地文本文件。默认从第 offset 行起返回 limit（默认 200）行，每行带行号前缀；文件更长时会提示用 offset 继续读。二进制文件和超 10MB 的文件会被拒绝。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（用正斜杠 /，如 C:/a/b.txt 或 src/x.py）"},
                "offset": {"type": "integer", "description": "从第几行开始读，0 表示第一行（默认 0）"},
                "limit": {"type": "integer", "description": "最多返回多少行（默认 200）"},
            },
            "required": ["path"],
        },
        handler=_read_file,
        max_output_chars=READ_FILE_MAX_OUTPUT_CHARS,
        read_only=True,
    ))
    reg.register(Tool(
        name="write_file",
        description="把内容写入文件（整文件覆盖；文件不存在则创建，自动建父目录）。新建文件或整体重写时用它；只改局部请用 edit_file。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（用正斜杠 /，如 C:/a/b.txt 或 src/x.py）"},
                "content": {"type": "string", "description": "要写入的完整文件内容"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ))
    reg.register(Tool(
        name="edit_file",
        description="在已存在的文件里把 old_string 替换成 new_string。old_string 必须与原文完全一致（含缩进/换行）且在文件中唯一，否则报错；要替换多处可设 replace_all=true。新建或整体重写请用 write_file。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（用正斜杠 /，如 C:/a/b.txt 或 src/x.py）"},
                "old_string": {"type": "string", "description": "要被替换的原文片段，需与文件中完全一致（含缩进、换行）。禁止带行号前缀——行号不属于文件内容。"},
                "new_string": {"type": "string", "description": "替换成的新内容"},
                "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false：要求 old_string 唯一、只替一处）"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_edit_file,
    ))
    reg.register(Tool(
        name="bash",
        description=_bash_description(),   # 按当前系统实际可用的 shell 动态生成
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer",
                            "description": "超时秒数（前台默认 30；后台默认不限时，传了则后台也按此限时）"},
                "background": {"type": "boolean",
                               "description": "长/不定时命令（dev server、watch、长构建/测试）设 true → "
                                              "转后台跑、立即返回任务编号、不阻塞；完成或卡住会通知你，"
                                              "可用 kill_bgtask(编号) 终止。短命令别用，直接前台跑。"},
            },
            "required": ["command"],
        },
        handler=_bash,
        max_output_chars=BASH_MAX_OUTPUT_CHARS,
        takes_slot=True,   # bash 起子进程 → 要 ProcSlot(前台可杀) + BackgroundManager(转后台)，execute 会注入
    ))
    reg.register(Tool(
        name="grep",
        description="按正则在文件内容里搜索（递归，自动跳过 .git/node_modules 等目录和二进制文件）。output_mode：files_with_matches（默认，列出含匹配的文件）/ content（带行号的匹配行）/ count（每个文件的匹配数）。可用 glob 过滤文件名、case_insensitive 忽略大小写、head_limit 限制条数。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索的根目录或文件（不传此参数则默认当前目录；用正斜杠 /）"},
                "output_mode": {"type": "string", "description": "files_with_matches（默认）/ content / count"},
                "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
                "case_insensitive": {"type": "boolean", "description": "忽略大小写"},
                "head_limit": {"type": "integer", "description": "最多返回多少条（默认 200）"},
            },
            "required": ["pattern"],
        },
        handler=_grep,
        max_output_chars=GREP_MAX_OUTPUT_CHARS,
        read_only=True,
    ))
    reg.register(Tool(
        name="glob",
        description="按路径模式查找文件名（不是搜内容——搜内容用 grep）。支持 ** 递归和 {a,b} 花括号，如 **/*.py、src/**/test_*.py、*.{py,md}。自动跳过 .git/node_modules 等，结果按修改时间倒序（最近改的在前）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "路径/文件名的 glob 模式，如 **/*.py"},
                "path": {"type": "string", "description": "搜索的根目录（不传此参数则默认当前目录；用正斜杠 /）"},
                "head_limit": {"type": "integer", "description": "最多返回多少个文件（默认 100）"},
            },
            "required": ["pattern"],
        },
        handler=_glob,
        max_output_chars=GLOB_MAX_OUTPUT_CHARS,
        read_only=True,
    ))
    reg.register(Tool(
        name="exit_plan",
        description="【仅计划模式】把你写好的计划文件提交给用户审阅（无参数——先把完整计划 write_file 到计划文件、"
                    "再调本工具）。调用后本轮结束、等用户批准或给修改意见；在此之前别动手改项目代码/跑命令。",
        parameters={"type": "object", "properties": {}},
        handler=_exit_plan,
    ))
    from .web import web_tools     # 局部 import：web.py 依赖本模块的 Tool，避免顶层循环
    for t in web_tools():          # web_search / web_fetch（只读联网，权限默认放行）
        reg.register(t)
    return reg
