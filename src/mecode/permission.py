"""工具权限：每个工具调用执行前过一道闸——allow / deny / ask。

为什么要它：agent 现在能无门槛跑 bash/write_file/edit_file。危险动作要先经用户同意，这是 harness
的安全脊椎。

【按工具名分组的参数细分】规则结构 = {工具名: {"allow":[spec...], "deny":[...]}}。
先按工具名取它自己那组规则，再 deny→allow 查 spec——比 Claude Code 把所有 `Tool(spec)` 串平铺在
一个表里更清晰：不用解析串、匹配只扫该工具的规则、同一工具的规则聚在一起。
  subject = 每个工具的可匹配串：bash→命令、read/write/edit→路径、grep/glob→搜索路径。
  spec 语法：
    *            任意
    head:*       命令/串前缀（head 后须词边界，免得 ls 误匹配 lsof）—— 如 ls:*、git log:*
    含 * 的 glob  fnmatch 路径 —— 如 src/**
    其余          精确相等 —— 如 git status
  优先级：deny > allow > 默认(ask)。不在 allow/deny 的自动落 default=ask，故无需单独的 ask 档。

默认：只读类 + 安全只读命令放行，其余（写文件 / rm / 未列命令）落到 default=ask。

分工（保持可测、不耦合 UI）：PermissionPolicy 纯逻辑判 allow/deny/ask；判 ask 时由 agent 调注入的
ask 回调问用户（once/always/deny）；"always" → 为该次调用生成一条 spec 记进该工具的 allow。
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from pathlib import Path

ALLOW, DENY, ASK = "allow", "deny", "ask"
ONCE, ALWAYS = "once", "always"          # ask 回调的返回值（+ DENY）

# 默认规则：只放静态的、与项目无关的——安全只读 bash 命令。
# 路径类工具(read/write/edit/grep/glob)的放行依赖项目根，由 from_persisted(project_root=) 注入
# 成"只放行根内"；没有项目上下文时它们落 default=ask（保守，不一刀切放行任意路径）。
DEFAULT_RULES: dict[str, dict[str, list[str]]] = {
    "save_memory": {"allow": ["*"]},      # 记忆工具是沙箱的（只动项目级 memory_dir）→ 安全放行，不打扰
    "recall_memory": {"allow": ["*"]},
    # 任务清单工具：纯内部状态读写（清单存会话级 tasks.json，不碰文件系统/不跑命令）→ 无副作用，放行不打扰。
    "task_create": {"allow": ["*"]},
    "task_update": {"allow": ["*"]},
    "task_get": {"allow": ["*"]},
    "task_list": {"allow": ["*"]},
    # 后台任务工具：check/wait 纯读输出/调查看节奏，无副作用；kill 停的是 agent【自己起的】后台任务
    # （非用户数据，CC 的 TaskStop 也不弹问）→ 三者皆放行。
    "check_bgtask": {"allow": ["*"]},
    "wait_bgtask": {"allow": ["*"]},
    "kill_bgtask": {"allow": ["*"]},
    # 派生子 agent：主 agent 既决定派活即信任（选项 a，子 agent 内部也 policy=None 全放）；且前台并发批会
    # 一次派多个，若逐个弹审批会 N 连问。故放行 subagent 本身（子 agent 内部动作的风险由"信任委派"承担）。
    "subagent": {"allow": ["*"]},
    # run_workflow 不进默认放行（落 default=ask）：一次 workflow = 一批子 agent + 数分钟执行 + 真金 token，
    # 分量比单个 subagent 重得多，起跑前该让用户看一眼结构（几个阶段、干什么）再放行。
    # 提交计划：计划模式里唯一的"动作"，只是把计划交给用户审阅（不改任何东西）→ 放行不打扰。
    "exit_plan": {"allow": ["*"]},
    # 联网工具：只读操作（搜索=发关键词、抓页=GET），和 grep/glob 同级 → 放行（用户拍板）。
    "web_search": {"allow": ["*"]},
    "web_fetch": {"allow": ["*"]},
    "bash": {"allow": [
        "ls:*", "cat:*", "head:*", "tail:*", "pwd:*", "echo:*",
        "grep:*", "wc:*", "which:*",
        "cut:*", "tr:*",                      # 常出现在管道里；cut 会读文件（受下方根闸），tr 只读 stdin
        "git status:*", "git log:*", "git diff:*", "git branch:*", "git show:*",
    ]},
    # 注：不放 find:* —— find 的 -exec/-execdir 能跑任意命令（且 `+` 结尾不含元字符、检测不到）。
    # 注：cat/head/tail/grep/wc/cut 虽默认放行，但有项目根时其读取的文件还要过一道【根内闸】
    #     （见 _read_cmd_paths_in_root）——否则 `cat ~/.ssh/id_rsa` 能绕过 read_file 的根限制读根外文件。
}
DEFAULT_DECISION = ASK                    # 该工具的三档都没命中 → 问（保守）

# shell 副作用元字符：重定向 > < 、链接 ; & && || 、命令替换 $() ` ` 、换行（多条命令）。
# 含这些的 bash 命令，allow 前缀不生效（落到问）——否则 `echo x > 重要文件`、`ls; rm -rf`、`$(rm x)`
# 这类能借"被放行的前缀"绕过闸去写文件/跑危险命令。
_SHELL_META = re.compile(r"[<>|;&\n`]|\$\(")


def _has_shell_meta(command: str) -> bool:
    return bool(_SHELL_META.search(command))


# 副作用元字符里只有【纯管道 |】能安全放宽：它只把上一段 stdout 接到下一段 stdin、本身不写不跑新命令；
# 安全前提是管道【每一段的命令都在 allow 名单里】——否则 `... | sh`、`... | tee 文件`、`... | xargs rm`
# 就借放行的头绕过闸去写文件/跑危险命令。其余元字符（> < ; & ` $( 换行）及逻辑或 ||：一律不放宽、照旧问。
_META_EXCEPT_PIPE = re.compile(r"[<>;&\n`]|\$\(")


def _pipeline_segments(command: str) -> list[str] | None:
    """command 是【纯管道】(仅用单个 | 连接、无其他副作用元字符、也不是 ||)时，返回去空白的各段命令；
    否则 None（不能按管道放宽 → 走原逻辑=问）。"""
    if "|" not in command or "||" in command:      # 没管道 / 是逻辑或(||含|但不是管道) → 不放宽
        return None
    if _META_EXCEPT_PIPE.search(command):          # 除 | 外还有别的副作用元字符 → 不放宽
        return None
    segs = [s.strip() for s in command.split("|")]
    if any(not s for s in segs):                   # 空段（| 开头/结尾/连续 ||已排除）→ 异常，不放宽
        return None
    return segs


def _subject(tool: str, args: dict) -> str:
    """取该工具用于匹配的串（路径类统一正斜杠，便于和规则比对）。"""
    if not isinstance(args, dict):
        return ""
    if tool == "bash":
        return args.get("command", "")
    if tool in ("read_file", "write_file", "edit_file"):
        return (args.get("path", "") or "").replace("\\", "/")
    if tool == "grep":                            # grep 的 pattern 是内容正则、搜索被 path 框住 → 只看 path
        return (args.get("path") or os.getcwd()).replace("\\", "/")
    if tool == "glob":
        # glob 的 pattern 是【路径通配】，含 .. 时能逃出 path（glob(path=".", pattern="../../*") 会列到根外）。
        # 正常 pattern（无 ..）不改变可达根 → 主体仍取 path，保持 spec/显示干净；含 .. → 把 path+pattern 合并，
        # 让"根内放行"检查看到真实落点（.. 由 _abs 的 abspath 归一化解掉），否则 path 在根内就被绕过。
        base = args.get("path") or "."
        pat = args.get("pattern") or ""
        if ".." in pat.replace("\\", "/").split("/"):
            return os.path.join(base, pat).replace("\\", "/")
        return base.replace("\\", "/")
    return ""


def _spec_match(spec: str, subject: str) -> bool:
    if spec in ("*", "**"):
        return True
    if spec.endswith(":*"):                       # 命令/串前缀（head 后须是词边界，免得 ls 误匹配 lsof）
        head = spec[:-2]
        if subject == head:
            return True
        if subject.startswith(head):
            nxt = subject[len(head):len(head) + 1]    # head 后一个字符
            return bool(nxt) and not (nxt.isalnum() or nxt == "_")   # 空格/:/- 等都算边界
        return False
    if "*" in spec or "?" in spec:                # 路径 glob
        return fnmatch.fnmatch(subject, spec)
    return subject == spec                         # 精确


def _any(specs: list[str], subject: str) -> bool:
    return any(_spec_match(s, subject) for s in specs)


def _same_path(a: str, b: str) -> bool:
    """两路径是否指向同一文件（归一化盘符大小写、正反斜杠、相对→绝对）。计划文件写例外用。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (OSError, ValueError):
        return False


# 路径类工具：subject 是路径/搜索目录，可能是相对（模型常给 '.'/'src'/'src/x.py'）。
_PATH_TOOLS = ("read_file", "write_file", "edit_file", "grep", "glob")


def _abs(subject: str) -> str:
    """把路径类 subject 按 cwd 解析成绝对、统一正斜杠；空/异常返回空串。
    用途：和"项目根内放行"的【绝对】规则比对——模型给相对路径时，相对形态匹配不上绝对根规则，
    补一个绝对形态一并比对才能命中（相对语义本就相对 cwd、与工具实际执行一致）。"""
    if not subject:
        return ""
    try:
        return os.path.abspath(subject).replace("\\", "/")
    except (OSError, ValueError):
        return ""


# 默认放行、但会读【任意文件内容】的 bash 命令：命中放行后要额外核查它读取的文件落在项目根内。
# _spec_match 对 `cat:*` 只看命令头、不看后面的路径 → 不补这道闸，`cat ~/.ssh/id_rsa` 就能绕过
# read_file 的根限制。这里把 read_file 的根闸补给这几个命令（ls 只列文件名、风险低，不在内）。
# cut 也在内：`cut -c1- FILE` 能像 cat 一样 dump 整个文件（默认放行的 tr 只读 stdin、不吃文件参数，无此问题）。
# 这道闸只拦【默认】放行：用户显式 always-allow 过某读命令（permissions.json 记下 cat:*）即视为
# 已授权 → 之后放行它的全部路径（含根外），由 _read_gate_ok 据用户 spec 放宽。
_READ_CMDS = frozenset({"cat", "head", "tail", "grep", "wc", "cut"})
# grep 短选项里【吃一个参数】的：e(pattern) f(文件) m(数) A/B/C(数) d/D(动作)。捆绑短 flag 里第一个
# 吃参数的选项会把 token 剩余部分当它的值（getopt 语义）；其中只有 f 的值是【文件】、要过根闸。
_GREP_ARG_OPTS = "efmABCdD"


def _in_root(path_abs: str, root: str) -> bool:
    """绝对路径 path_abs 是否在项目根 root 内（根本身或其子孙）。空路径 → False。"""
    return bool(path_abs) and (path_abs == root or path_abs.startswith(root + "/"))


def _glob_base(tok: str) -> str:
    """含通配符的路径 → 取通配前的目录部分（判落点用）：*.txt→''（=cwd）、/var/log/*→/var/log、../*→..。"""
    base: list[str] = []
    for seg in tok.split("/"):
        if any(c in seg for c in "*?["):
            break
        base.append(seg)
    return "/".join(base)


def _path_escapes_root(tok: str, root: str) -> bool:
    """路径 token 是否可能落到项目根之外（保守：判不准就算逃出）。
    含 $（变量）或 {}（花括号展开）、或通配里还带 ..（`*/../x` 展开后能爬出根）一律保守判逃出——
    这些 shell 展开静态判不了落点，且都能把 .. 藏进去爬出根（`cat {.,..}/x` 会展开出 ../x）。"""
    if "$" in tok or "{" in tok or "}" in tok:
        return True                                  # 变量 / 花括号展开：shell 展开到未知落点
    if any(c in tok for c in "*?["):
        if ".." in tok.split("/"):
            return True                              # 通配 + .. → 展开后落点不可控
        probe = _glob_base(tok) or "."               # 否则判通配前的目录前缀（*.txt→cwd、/var/*→/var）
    else:
        probe = tok
    p = _abs(os.path.expanduser(probe))              # 先展开 ~，再按 cwd 解析成绝对
    return not _in_root(p, root)


def _flag_file_value(cmd: str, tok: str) -> str | None:
    """flag token 里【贴附的、可能是文件路径】的值——须过根闸。返回该值；无则 None。
    ①任意长选项 `--x=VALUE`：值一律核查。这样 grep 的 --file=、wc 的 --files0-from= 等"读文件长选项"
      都被覆盖（值路径贴在 token 里、否则会被当纯 flag 跳过而绕过根闸），且对将来新增的读文件长选项自动生效。
      代价：--regexp=/api/ 这类"值像根外路径的非文件长选项"会被误拦——罕见、安全侧、可 always-allow。
    ②grep 短选项捆绑里的 -f<file>（getopt：第一个吃参数的选项若是 f，其后即文件路径）。
    分开写的值（--file <路径> / -f <路径>）不在此列——它是独立位置参数、由主循环当文件核查。"""
    if tok.startswith("--") and "=" in tok:
        return tok.split("=", 1)[1] or None
    if cmd == "grep" and not tok.startswith("--"):    # grep 短选项捆绑：找第一个吃参数的选项字符
        for i in range(1, len(tok)):
            c = tok[i]
            if c == "f":
                return tok[i + 1:] or None            # f 之后即文件值（为空=分开写，走位置参数）
            if c in _GREP_ARG_OPTS:                   # e/m/A/B/C/d/D 吃的是非文件参数 → 其后不是文件
                return None
    return None


def _read_cmd_paths_in_root(command: str, root: str) -> bool:
    """command 是单条（无管道）bash 命令。首词若是受管控读命令（_READ_CMDS），核查它读取的每个文件都在 root 内。
    非这些命令 → True（不额外管）；解析失败 → False（保守）。
    做法：位置参数一律当文件核查——grep 不再猜测 pattern（贴附形式 -e./-iefoo 会让"跳过一个位置参数当
    pattern"误跳掉真文件，是已证实的绕过口子）；代价是 pattern 若长得像根外路径会被误拦（罕见、可 always-allow）。
    另抽出贴附的读文件 flag 值核查（长 --x=值 / grep 短 -f<值>）；分开写的值由位置参数覆盖。"""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False                                 # 引号不配对等 → 保守（当作解析不了）
    if not tokens:
        return True
    cmd = tokens[0]
    if cmd not in _READ_CMDS:
        # shlex 命令名可能与 bash 分叉：cat$'' / cat$"" / cat$x 里 shlex 不解析 $''(ANSI-C)/$""(locale)/
        # $var，把 $ 粘进命令名 token（'cat$'）→ 脱离 _READ_CMDS 使根闸短路，而 bash 会折叠成 cat 读文件。
        # 用与 allow 判定同源的 _spec_match 重判原始命令：仍匹配某读命令 spec（cat:* 等，$ 算词边界）就
        # 说明它其实是读命令、只是名字被污染 → shlex 后续 token 也不可信 → 保守判逃出（ask）。否则真不是读命令。
        stripped = command.strip()
        return not any(_spec_match(f"{rc}:*", stripped) for rc in _READ_CMDS)
    for tok in tokens[1:]:
        if tok.startswith("-"):                       # flag：跳过；贴附的读文件 flag 值（--x=/grep -f）另抽出核查
            # wc --files0-from：从(stdin/清单文件)取一批文件名再逐个打开——静态无法界定这批文件的落点
            #（值指向 stdin `-` 或根内清单时能间接读根外文件）→ 一律问。是六个读命令里唯一的"间接读一批文件"选项。
            if tok == "--files0-from" or tok.startswith("--files0-from="):
                return False
            f = _flag_file_value(cmd, tok)
            if f is not None and _path_escapes_root(f, root):
                return False
            continue
        if _path_escapes_root(tok, root):             # 位置参数 = 文件
            return False
    return True


def _git_diff_paths_in_root(command: str, root: str) -> bool:
    """`git diff` 给两个文件系统路径、且至少一个在工作树外时，会【隐式进入 no-index 模式（无需 --no-index
    旗标）】diff/dump 任意文件内容（含根外）。git 在默认放行的 `git diff:*` 里、又非 _READ_CMDS 读命令 →
    走不到上面那道闸。这里补查 `git diff` 的【像路径的】操作数都在根内：含 / ~ \\ : 或以 . 开头的当路径核查；
    纯 ref/pathspec（HEAD、main、HEAD@{2}、origin/main 等，无路径分隔符）照放，普通 git diff path 也天然根内。"""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if tokens[:2] != ["git", "diff"]:                 # 只管 `git diff …`（allow 也只经 git diff:* 命中）
        return True
    for tok in tokens[2:]:
        if tok.startswith("-"):                       # flag 跳过
            continue
        # 像路径（会经 _path_escapes_root 核查）：以 . 开头 / 含路径分隔或盘符 / 含 $ 变量展开（无 git ref 用 $）
        # / 花括号里带 , 或 ..（花括号展开成路径，如 {..,.}）。reflog ref HEAD@{2} 含 {} 但无 ,/.. → 不误伤。
        looks_path = (tok[:1] == "." or any(c in tok for c in "/~\\:$")
                      or ("{" in tok and ("," in tok or ".." in tok)))
        if looks_path and _path_escapes_root(tok, root):
            return False                              # 像路径且逃出根 → 拦（纯 ref/pathspec 不动）
    return True


def spec_for(tool: str, args: dict) -> str:
    """"总是允许"时为 (tool, args) 生成要记住的 spec：
    bash → 命令首词前缀（rm:*）；读/写/改 → 文件【所在文件夹】（C:/a/b/*，不是整工具、也不是只此一文件）；
    其余 → '*'。"""
    if tool == "bash":
        parts = _subject(tool, args).split()
        return f"{parts[0]}:*" if parts else "*"
    if tool in ("read_file", "write_file", "edit_file"):
        path = _subject(tool, args)               # 文件 → 所在文件夹
        if "/" in path:
            folder = path.rsplit("/", 1)[0]
            if folder:
                return f"{folder}/*"
        return path or "*"                        # 无所在文件夹（裸文件名/根级）→ 精确到该文件，别放整工具
    if tool in ("grep", "glob"):                  # 搜索路径本身就是目录 → 授权它及其下
        path = _subject(tool, args).rstrip("/")
        if path:
            return f"{path}/*"
    return "*"


def specs_for(tool: str, args: dict) -> list[str]:
    """"总是允许"要记住的 spec 列表。bash 纯管道 → 每一段的命令头各记一条（head:*），这样以后同类管道
    每段都命中 allow、不再问；其余（非管道 bash / 路径类）→ 单条（见 spec_for）。"""
    if tool == "bash":
        segs = _pipeline_segments(_subject(tool, args))
        if segs is not None:                      # 纯管道：逐段取首词头
            heads: list[str] = []
            for s in segs:
                parts = s.split()
                sp = f"{parts[0]}:*" if parts else "*"
                if sp not in heads:
                    heads.append(sp)
            if heads:
                return heads
    return [spec_for(tool, args)]


def pattern_for(tool: str, args: dict) -> str:
    """显示用的可读形式（同 Claude 的 Tool(spec)）：如 bash(rm:*)、bash(cat:*, jq:*)、write_file(*)。"""
    return f"{tool}({', '.join(specs_for(tool, args))})"


def _clone(rules: dict) -> dict:
    """深拷贝规则（{tool:{decision:[spec]}}），免得运行时 allow_always 改到模块级 DEFAULT_RULES。"""
    return {t: {d: list(specs) for d, specs in groups.items()} for t, groups in rules.items()}


class PermissionPolicy:
    def __init__(self, rules: dict | None = None, default: str = DEFAULT_DECISION,
                 root: str | None = None, plan_path: str | None = None,
                 user_allow: dict[str, list[str]] | None = None) -> None:
        self.rules = _clone(DEFAULT_RULES if rules is None else rules)
        self.default = default
        self.root = root          # 项目根（posix）；模式覆盖里的 @root 占位展开用（auto 放行项目内编辑）。None=无项目上下文
        self.plan_path = plan_path   # 计划文件路径；非 None（=计划模式）时，唯独放行对它的 write/edit（只读之下的写例外）
        # 用户【显式加】的 allow spec（来自 permissions.json / 运行时 allow_always），和默认 spec 分开记：
        # 读命令根闸只拦【默认】放行；用户一旦显式 always-allow 过某读命令（记下 cat:*），就放行它的全部路径。
        self._user_allow = {t: list(v) for t, v in (user_allow or {}).items()}

    @classmethod
    def from_persisted(cls, saved: dict | None,
                       project_root: str | None = None) -> "PermissionPolicy":
        """默认规则 + 项目级持久化里的用户规则（permissions.json 只存用户加的，按工具名合并）。

        给了 project_root：把【只读类】read/grep/glob 默认收紧成【只放行项目根内】（读/搜无副作用，根内直接放、
        根外要问）。写/改（write/edit）【不】在此列——默认（normal 模式）也要逐次问；auto 模式再放行【项目内】编辑
        （见 mode.apply_mode 的 @root 展开）。选"总是允许"会精确到那个文件夹。不给 project_root 则无项目上下文兜底。"""
        merged = _clone(DEFAULT_RULES)
        root = None
        if project_root:
            root = os.path.abspath(str(project_root)).replace("\\", "/")
            for t in ("read_file", "grep", "glob"):
                merged.setdefault(t, {})["allow"] = [root, f"{root}/**"]   # 根本身 + 根下
        # skill 文件（system prompt 只给索引、模型场景命中时才 read_file 读全文）常在项目根外
        # （内置在安装目录、用户级在 ~/.mecode）——读它们无副作用，默认放行，别为读个流程说明弹授权。
        allow = merged.setdefault("read_file", {}).setdefault("allow", [])
        for d in (Path(__file__).resolve().parent / "skills_builtin",
                  Path("~/.mecode/skills").expanduser()):
            p = d.as_posix()
            allow.extend([p, f"{p}/**"])
        user_allow: dict[str, list[str]] = {}
        for tool, groups in (saved or {}).items():
            if not isinstance(groups, dict):       # 跳过旧格式/坏数据（如早期扁平表），别让它搞崩启动
                continue
            dst = merged.setdefault(tool, {})
            for decision, specs in groups.items():
                if not isinstance(specs, list):
                    continue
                cur = dst.setdefault(decision, [])
                cur.extend(s for s in specs if s not in cur)
                if decision == ALLOW:              # 记成"用户显式加的"——读命令根闸对这些 spec 放行全部
                    ua = user_allow.setdefault(tool, [])
                    ua.extend(s for s in specs if s not in ua)
        return cls(rules=merged, root=root, user_allow=user_allow)

    def decide(self, tool: str, args: dict) -> str:
        """返回 allow / deny / ask。先取该工具的规则组，再 deny > allow > default(ask)。"""
        subject = _subject(tool, args)
        # 计划文件例外：唯独放行对【计划文件本身】的 read/write/edit（模型用它迭代计划、执行期回看）。
        # 放在 deny 之前 → 覆盖 plan 覆盖层对 write/edit 的 deny "*"，也免去项目外读它被问；其余写/改照旧。
        if self.plan_path and tool in ("read_file", "write_file", "edit_file") \
                and _same_path(subject, self.plan_path):
            return ALLOW
        r = self.rules.get(tool, {})
        # 路径类工具：相对 subject 补一个按 cwd 解析的绝对形态，两种形态任一命中即算——
        # 既让"项目根内放行"的绝对规则能命中模型给的相对路径/'.'/'src'（此前误落 ask），
        # 又保留对用户相对 spec（如 src/**）的匹配。bash 的 subject 是命令，不参与。
        subjects = [subject]
        if tool in _PATH_TOOLS:
            a = _abs(subject)
            if a and a != subject:
                subjects.append(a)
        if any(_any(r.get(DENY, []), s) for s in subjects):
            return DENY
        # bash 含 shell 副作用元字符 → 不让 allow 前缀直接放行（deny 已先判过）。
        # 唯一例外：纯管道且【每一段命令都在 allow 名单里】→ 放行（cat|grep|wc 这类只读命令互接，安全、免问）。
        if tool == "bash" and _has_shell_meta(subject):
            segs = _pipeline_segments(subject)
            if segs is not None and all(_any(r.get(ALLOW, []), s) for s in segs):
                # 每段的读命令（cat/grep 等）路径参数也要过根闸（同下方单命令的根闸）
                if self.root and not all(self._read_gate_ok(s) for s in segs):
                    return self.default
                return ALLOW
            return self.default
        if any(_any(r.get(ALLOW, []), s) for s in subjects):
            # 读文件内容的命令（cat/head/tail/grep/wc）即便命中放行，路径参数逃出项目根 → 落 ask
            # （补 read_file 的根闸，防 `cat ~/.ssh/id_rsa` 绕过）。仅在有项目根时生效。
            if tool == "bash" and self.root and not self._read_gate_ok(subject):
                return self.default
            return ALLOW
        return self.default

    def _read_gate_ok(self, command: str) -> bool:
        """单条 bash 命令过"读命令根闸"：非读命令 / 路径都在项目根内 → True；
        路径逃出根时，看命令是否命中【用户显式加的】allow spec——命中即放行全部
        （用户已授权该读命令，根闸只拦默认放行；见 _READ_CMDS 注释）。"""
        if _read_cmd_paths_in_root(command, self.root) and _git_diff_paths_in_root(command, self.root):
            return True
        return _any(self._user_allow.get("bash", []), command)

    def add(self, tool: str, decision: str, spec: str) -> None:
        lst = self.rules.setdefault(tool, {}).setdefault(decision, [])
        if spec not in lst:
            lst.append(spec)

    def allow_always(self, tool: str, args: dict) -> list[str]:
        """记住该次调用对应的"总是允许"spec（本进程即时生效），返回 spec 列表——管道会有多条（每段一条头）；
        持久化由调用方经 store 逐条落盘。"""
        specs = specs_for(tool, args)
        ua = self._user_allow.setdefault(tool, [])
        for spec in specs:
            self.add(tool, ALLOW, spec)
            if spec not in ua:                  # 也记进用户 spec：读命令 always-allow 后根闸对它放行全部
                ua.append(spec)
        return specs
