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
    # 提问工具：它的动作就是"弹界面问用户"本身，用户在弹窗里作答即完成把关 → 再过审批闸是双重打扰。
    "ask_user": {"allow": ["*"]},
    # 派生子 agent：放行的是【派生这个动作】本身——它只是开一个干净上下文，不碰文件不跑命令。
    # 真正的风险在子 agent 内部的动作，那些现在【继承主 agent 的 policy 逐个过闸】（见 subagent.py 头），
    # 不再是"派活即信任"的全放行——所以这里放行 subagent 不构成绕过口。
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


def _strip_wildcards(path: str) -> str:
    """路径里带 * 或 ? 时，截到【第一个含通配的段之前】——取它真正落在哪个目录。

    模型偶尔把通配符写进 path（`grep path="C:/桌面/*"`）。不截的话这个串会一路带进判定与 spec：
    `_spec_match` 见到 * 就走 fnmatch，而 fnmatch 的 * 跨 `/` → 那条 spec 等于"这个目录下的一切"，
    弹窗上却显示成 `C:/桌面/*`（读起来像只批了一层）。护栏②即便判出"圈住了项目根"要收窄，收窄的
    目标也还是这个带 * 的串 —— 收了个寂寞。源头截掉，判定与 spec 都基于真实落点，护栏也就有的可收。

    【只截 * 和 ?，不截 [】：`data[1]`、`Season [2020]` 是合法目录名，按 [ 截会把正常路径切错；
    它们由 _esc_glob 在拼 `/**` 时转义处理（精确形态走逐字符相等，本就无需转义）。
    两个工具在执行侧也不接受带通配的 path（tools 里 `base.is_dir()` 直接判假），故按目录前缀理解是保守且正确的。"""
    if "*" not in path and "?" not in path:
        return path
    out: list[str] = []
    for seg in path.split("/"):
        if "*" in seg or "?" in seg:
            break
        out.append(seg)
    return "/".join(out)


def _subject(tool: str, args: dict) -> str:
    """取该工具用于匹配的串（路径类统一正斜杠，便于和规则比对）。"""
    if not isinstance(args, dict):
        return ""
    if tool == "bash":
        cmd = args.get("command", "")
        # strip：前导空白对 shell 无意义，但会让 `  ls` 匹配不上 `ls:*` 前缀 spec——判定与"总是允许"
        # 记下的规则就此对不上（点了不再问、下次还问）。归一化后两边同源。
        return cmd.strip() if isinstance(cmd, str) else ""
    if tool not in _PATH_TOOLS:
        return ""
    # 路径类：path 不是字符串（模型偶尔给数字/数组/对象）→ 当空，别让权限闸抛异常。
    # 闸在 tools.execute 的 catch-all【外面】：抛出去会中断整轮、留下无结果的孤儿 tool_call。
    path = args.get("path")
    if not isinstance(path, (str, type(None))):
        return ""
    path = _strip_wildcards((path or "").replace("\\", "/"))
    if tool in ("read_file", "write_file", "edit_file"):
        return path
    if tool == "grep":                            # grep 的 pattern 是内容正则、搜索被 path 框住 → 只看 path
        return path or os.getcwd().replace("\\", "/")
    if tool == "glob":
        # 只取 path：pattern 能不能爬出根，由独立的一道闸判（_glob_escapes）。
        # 曾经把"含 .. 的 pattern"合并进 subject 来兜——那是想【静态推断 pattern 的落点】，两轮审查
        # 证明这条路走不通（花括号可以把 .. 藏进 token、只看第一个含 .. 的分支可被换序绕过、
        # `**/../*` 的落点更是静态算不出）。subject 回归干净的 path，让 spec 与显示都不带通配符。
        return path or "."
    return ""


def _glob_escapes(args: dict, root: str) -> bool:
    """glob 的 pattern 会不会把搜索落点带出 path：**展开后任一支含 `..` 段即算逃出**。

    实测（隔离目录跑 pathlib）：`../*`、`{..,x}/**/*`、`**/../*`、`*/../../*`、`src/**/../../*`
    全都真的枚举到了 path 外，而 `..` 是唯一出路——绝对 pattern 被 pathlib 直接拒（
    NotImplementedError），`~` 不展开（当普通目录名）。故不再去静态推算落点（那类推算正是此前
    被绕过的地方：花括号能把 .. 藏进段里、`**/..` 在字符串层会自我抵消），一律禁掉 `..`。
    tools._glob 侧同样报错，两边同源。

    逐支检查【所有】展开分支：`{nonexistent/..,../../..}/**/*` 把自我抵消的一支放在前面，
    只看第一支就会被整个绕过（实测 decide 曾直接 allow）。展开只做一层，与 _expand_braces 一致。"""
    from .tools import _expand_braces      # 局部 import：避免 permission ↔ tools 顶层循环
    # 类型消毒在【这里也要做】：本函数被 _gen_specs 与 decide 直接喂【原始 args】，绕过了 _subject
    # 那道闸。而权限闸在 tools.execute 的 catch-all【外面】：抛异常会中断整轮留下孤儿 tool_call，
    # 走 pattern_for 那条更是落在 UI 线程里带 traceback 掀掉整个 TUI。
    if not isinstance(args, dict):
        return True                        # 连 args 都不是 dict → 保守判逃出（落 ask）
    pat = args.get("pattern")
    pat = pat.replace("\\", "/") if isinstance(pat, str) else ""
    return any(".." in p.split("/") for p in (_expand_braces(pat) if "{" in pat else [pat]))


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


def _allow_subjects(tool: str, args: dict) -> list[str]:
    """allow 侧比对用的 subject 形态列表——**单一真相源**：decide() 与 specs_for() 的自检都走这里。

    路径类工具：
    ① _abs = realpath 真实落点（解掉 .. 与软链/junction/subst）——匹配"项目根内放行"的绝对规则，
       也让模型给的相对路径/'.'/'src' 能命中它。这一条是权威形态。
    ② 归一化的【相对】形态——仅为匹配用户自己写的相对 spec（如 src/**）而补；subject 本身是绝对
       路径时【不补】：对它而言相对形态就是那个未解析的原始串，留着等于再开一次绕过口子
       （`<根>/../../etc/x`、`<根>/软链/x` 都以根开头，会被 `<根>/**` 匹配上而放行）。
    其余工具：就是它自己的 subject。

    抽成一处的理由：spec 生成与判定必须用同一套形态，否则"记住的"和"检查的"会漂移——本模块
    已经因为这类漂移栽过好几次（V1 的原始串 vs 落点、spec_for 的未解析串 vs decide 的落点）。"""
    subject = _subject(tool, args)
    if tool not in _PATH_TOOLS:
        return [subject]
    out: list[str] = []
    if a := _abs(subject):
        out.append(a)
    n = _norm(subject)
    if n and not os.path.isabs(n) and n not in out:
        out.append(n)
    return out


def _same_path(a: str, b: str) -> bool:
    """两路径是否指向同一文件（归一化盘符大小写、正反斜杠、相对→绝对）。计划文件写例外用。"""
    if not a or not b:
        return False
    try:                       # realpath 与 _abs 同源：软链/junction 下两个写法指向同一文件也算同一个
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


# 路径类工具：subject 是路径/搜索目录，可能是相对（模型常给 '.'/'src'/'src/x.py'）。
_PATH_TOOLS = ("read_file", "write_file", "edit_file", "grep", "glob")
_FILE_TOOLS = ("read_file", "write_file", "edit_file")   # 只有它们受护栏②约束，理由见 _guard


def _abs(subject: str) -> str:
    """把路径类 subject 解析成【真实落点】的绝对路径、统一正斜杠；空/异常返回空串。
    用途：和"项目根内放行"的【绝对】规则比对——模型给相对路径时，相对形态匹配不上绝对根规则，
    补一个绝对形态一并比对才能命中（相对语义本就相对 cwd、与工具实际执行一致）。

    用 realpath 而非 abspath：abspath 是纯字符串运算，不问文件系统"这段路是不是替身"——项目内
    一个指向根外的软链/junction（node_modules 里很常见）或 subst 虚拟盘符，abspath 算出来仍在根内
    而 open() 会跟着替身走到根外，又是"检查的 ≠ 执行的"。realpath 逐段解开替身，判的是真实落点。
    对不存在的路径（write_file 新建）能解多少解多少、结果同 abspath；代价是每次多几个系统调用
    （~0.3ms，相对一次工具调用可忽略）。注意：root 侧必须同步 realpath（见 from_persisted），
    否则项目根本身位于软链下时两边不同源，正常路径会全部误落 ask。"""
    if not subject:
        return ""
    try:
        return os.path.realpath(subject).replace("\\", "/")
    except (OSError, ValueError):
        return ""


def _norm(subject: str) -> str:
    """把路径类 subject 里的 . / .. 在字符串层解掉（不转绝对，保住相对形态）；空/异常返回原串。

    为什么必须解：allow 侧的根规则是 `<根>/**`，而 fnmatch 的 `*` 会跨 `/`（不同于 shell glob）
    ——原始串 `<根>/../../etc/passwd` 以根开头，就被 `<根>/**` 匹配上而放行，可 open() 时 OS 会
    真的执行那两个 ..、落到根外。即"检查的字符串 ≠ 执行的路径"。先归一化，落点在字符串层就摆正
    （`<根>/../x` → `<根>` 的上级/x，不再匹配 `<根>/**`），而 `src/x.py` 归一化后不变、用户的
    相对 spec 照常命中。与 bash 侧同源：那边 _path_escapes_root 也是先算落点再判根内。"""
    if not subject:
        return ""
    try:
        return os.path.normpath(subject).replace("\\", "/")
    except (OSError, ValueError):
        return subject


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


def _probe(tok: str) -> str | None:
    """路径 token → 它的【落点】；None = 静态算不出，调用方一律保守处理。

    算不出的三种（都能把 .. 藏进去、展开后爬出根）：含 $ 变量、含 {} 花括号、通配里还带 ..。
    含通配无 .. 则取通配前的目录前缀（`*.txt`→cwd、`/var/log/*`→/var/log）：那是能触及的最上层目录。"""
    if "$" in tok or "{" in tok or "}" in tok:
        return None
    if any(c in tok for c in "*?["):
        if ".." in tok.split("/"):
            return None
        tok = _glob_base(tok) or "."
    return os.path.expanduser(tok) or "."


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


# 接受 diff 选项（含写文件的 --output）的默认放行 git 子命令：三者都能 `--output=<file>` 写任意文件。
_GIT_DIFF_SUBCMDS = frozenset({"diff", "log", "show"})


def _bash_file_probes(command: str) -> tuple[list[str], list[str]] | None:
    """单条（无管道）bash 命令会碰到的文件【落点】：(要读的, 要写的)。
    None = 静态算不出，调用方保守拦；([], []) = 不碰文件。

    只解析【默认放行、却能碰任意文件】的两类命令，其余返回 ([], [])（不在 allow 名单里，走正常审批）：
    ① _READ_CMDS（cat/head/tail/grep/wc/cut）：位置参数一律当文件——grep 不猜 pattern（贴附形式
       `-e.`/`-iefoo` 会让"跳过一个位置参数当 pattern"误跳掉真文件，已证实的绕过口子）；代价是
       pattern 像路径时会被当文件判。另抽出贴附的读文件 flag 值（`--x=值`、grep `-f<值>`）。
       `--files0-from` 从清单/stdin 取一批文件名再逐个打开，界定不了 → None。
    ② `git diff/log/show`：读——两个操作数至少一个在工作树外时会隐式进入 no-index 模式 dump 任意
       文件，故【像路径的】操作数当读落点（纯 ref/pathspec 如 HEAD、origin/main 不动）；
       写——`--output=<file>` 把 diff 写进任意文件，不经 shell、一个元字符都没有，是 `git diff > f`
       绕开元字符闸的写法，故当写落点。分开写的 `--output <file>` 目标在下一个 token。

    返回落点而不是直接判在不在根内：判据交给 read_file / write_file 两道闸（见 _file_gate_ok），
    "同一个文件能不能读"只有一套答案。"""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None                                  # 引号不配对等 → 解析不了，保守
    if not tokens:
        return [], []
    cmd = tokens[0]
    is_git = tokens[:1] == ["git"] and tokens[1:2] in ([s] for s in _GIT_DIFF_SUBCMDS)
    if cmd not in _READ_CMDS and not is_git:
        # shlex 的命令名可能与 bash 分叉：`cat$''`/`cat$x` 里 shlex 把 $ 粘进命令名 token（'cat$'）
        # → 脱离 _READ_CMDS 使闸短路，而 bash 折叠成 cat 去读文件。用与 allow 判定同源的 _spec_match
        # 重判原始命令：仍匹配某受管控命令的 spec 就说明名字被污染 → 后续 token 也不可信 → None。
        stripped = command.strip()
        managed = [f"{rc}:*" for rc in _READ_CMDS] + [f"git {sc}:*" for sc in _GIT_DIFF_SUBCMDS]
        return None if any(_spec_match(sp, stripped) for sp in managed) else ([], [])

    reads: list[str] = []
    if not is_git:
        for tok in tokens[1:]:
            if tok.startswith("-"):                   # flag：贴附的读文件值另抽出，其余跳过
                if tok == "--files0-from" or tok.startswith("--files0-from="):
                    return None                       # 间接读一批文件，界定不了
                f = _flag_file_value(cmd, tok)
                if f is not None:
                    if (p := _probe(f)) is None:
                        return None
                    reads.append(p)
                continue
            if (p := _probe(tok)) is None:            # 位置参数 = 文件
                return None
            reads.append(p)
        return reads, []

    writes: list[str] = []
    want_output = False                               # 上一个 token 是分开写的 --output
    for tok in tokens[2:]:
        if want_output:
            want_output = False
            if (p := _probe(tok)) is None:
                return None
            writes.append(p)
            continue
        if tok.startswith("-"):
            if tok == "--output":
                want_output = True
            elif tok.startswith("--output="):
                if (p := _probe(tok.split("=", 1)[1])) is None:
                    return None
                writes.append(p or ".")
            continue
        # 像路径：以 . 开头 / 含路径分隔或盘符 / 含 $（没有 git ref 用 $）/ 花括号里带 , 或 ..
        # （会展开成路径，如 {..,.}）。reflog ref `HEAD@{2}` 含 {} 但无 ,/.. → 不误伤。
        looks_path = (tok[:1] == "." or any(c in tok for c in "/~\\:$")
                      or ("{" in tok and ("," in tok or ".." in tok)))
        if looks_path:
            if (p := _probe(tok)) is None:
                return None
            reads.append(p)
    return (None if want_output else (reads, writes))  # --output 悬在末尾 → 目标不明，保守


_GLOB_META = "*?["                            # fnmatch 里有特殊含义的字符（] 不在类内时是字面量）


def _esc_glob(s: str) -> str:
    """把路径里的 fnmatch 元字符转义成单字符类，让 spec 只按【字面】匹配。
    `data[1]`、`Season [2020]`、`log[old]` 这类是合法目录名，不转义会被当字符类：既匹配不回自己
    （pattern 里的 `[1]` 要求单个字符 '1'，而路径里是字面 '[1]'），又会误授权到 `data1` 这种从没批过的路径。"""
    return "".join(f"[{c}]" if c in _GLOB_META else c for c in s)


def _exact_cmd_spec(cmd: str) -> str:
    """整条命令的【精确】spec：只匹配它自己。含 * ? 才转义成单字符类（否则 _spec_match 走 fnmatch，
    `rm *.tmp` 会当模式匹配到 `rm 别的.tmp`）；不含时保持原样走逐字符相等——无谓地转义 [ 会让
    `cat x[1]` 变成 `cat x[[]1]` 而永远匹配不回自己（_guard 不变量①栽过这个坑）。"""
    return _esc_glob(cmd) if ("*" in cmd or "?" in cmd) else cmd


def _cmd_spec(cmd: str) -> str:
    """命令 → "总是允许"要记的 spec。
    **首词后紧跟非选项词** → 发两词前缀（`git diff:*`）；**否则** → 整条命令精确（不带 `:*`）。

    只记首词太宽：`git diff HEAD~1` 记成 `git:*` 等于把 `git push --force`、`reset --hard` 一并放行；
    `python 脚本.py` 记成 `python:*` 更是换来 `python -c "任意代码"`。

    为什么中间夹了选项就不发前缀：`-` 前缀分不出"选项的值"和"子命令"。`python -m pytest -q` 取到第一个
    非选项词是 `pytest`（恰好是想要的），但 `python -X utf8 script.py` 取到的是 `utf8`（`-X` 的值）
    → 发出 `python -X utf8:*` → `python -X utf8 -c "任意代码"` 照样命中，和 `python:*` 是同一个洞；
    `git -c k=v diff`、`docker -H x ps`、`npm --prefix ./app run build` 同理。断在哪判不了，就不断
    ——整条记下来，一个字都不许变。代价：带选项的命令"总是允许"退化成"记住这一条"（`python -m pytest -q`
    批过后换 `-v` 要再批）。单词命令（`make`）也走精确：后面追加参数是另一个动作，不该顺带授权。"""
    parts = cmd.split()
    if len(parts) >= 2 and not parts[1].startswith("-"):
        return f"{parts[0]} {parts[1]}:*"      # 命令名 + 子命令：前缀授权（与默认名单的 git log:* 同形）
    return _exact_cmd_spec(cmd)


def _root_like(folder: str, root: str | None) -> bool:
    """folder 是不是"大到不该被一次点击授权出去"的容器：项目根本身 / 盘符根 / 文件系统根 / UNC 共享根。
    这类要退回【精确到该文件】——否则在项目根下建一个 notes.md 点一次"总是允许"，就等于把整个项目
    （含 .mecode/permissions.json 与 .git/）的写权限永久发出去，normal 模式一键变成 auto 模式。
    注：folder 是项目根【祖先】的情形不在这里判——那由 _guard 的范围护栏统一兜住（它比逐条列举可靠）。"""
    if not folder:
        return True                               # POSIX 根（'/x.txt' rsplit 后是空串）
    if root and os.path.normcase(folder) == os.path.normcase(root):
        return True                               # 项目根
    if folder.startswith("//"):                   # UNC：\\server 或 \\server\share 都是"整台机/整个共享"
        return len([s for s in folder[2:].split("/") if s]) <= 2
    return len(folder) <= 2 and folder.endswith(":")   # 盘符根 C:


def specs_for(tool: str, args: dict, root: str | None = None) -> list[str]:
    """对外入口：生成候选 spec（_gen_specs）后【一律过护栏】（_guard）。
    分两步的理由见 _guard——不变量由护栏统一强制，各分支写错也兜得住。"""
    return _guard(_gen_specs(tool, args, root), tool, args, root)


def _gen_specs(tool: str, args: dict, root: str | None = None) -> list[str]:
    """"总是允许"要记住的 spec 列表。**空列表 = 这次调用【不可授权】**（弹窗不该给"总是允许"这一项）。

    三条不变量（每条都对应一个实测过的坑）：
    ① **能匹配回自己**：生成的 spec 必须命中本次调用在 decide() 里用的那个 subject 形态，否则就是
       "点了不再问、下次还问"。故 spec 从 _abs 落点算（与 decide 同源）；grep/glob 的 subject 就是
       那个目录本身，只给 `<dir>/**` 匹配不回它（fnmatch 下还差一个分隔符）→ 必须同时记 `<dir>`
       ——这正是根规则写成 [root, root/**] 两条的原因。
    ② **不得比"被批准的那个对象"更宽**：算不出有意义的范围时【不生成】（返回 []），绝不退化成 '*'
       或整盘。'*' 只属于本来就没有细分维度的工具（subagent/web_*/MCP 等，粒度就是工具本身）。
    ③ **只按字面匹配**：路径里的 * ? [ 要转义（见 _esc_glob）。

    root = 项目根（allow_always 传 self.root）：用来判"文件夹是不是大到不该整片授权"（见 _root_like）。
    """
    if tool == "bash":
        cmd = _subject(tool, args)
        if not cmd.strip():
            # 畸形调用：参数被截断时 provider 会给 {'__raw__':..,'__error__':..}（无 command 键），
            # 缺键/空串/纯空白都走到这。此前它退化成 '*' → 一次点击永久无条件放行【全部】bash。
            return []
        segs = _pipeline_segments(cmd)
        if segs is not None:                      # 纯管道：逐段各记一条，以后同类管道每段都命中
            heads: list[str] = []
            for s in segs:
                if not s.split():
                    return []
                # 【必须与单条命令同源】：这里曾经只取首词，于是 `git log … | jq .` 记成 git:*，
                # 一次点击把 git push --force / reset --hard 全放行。
                # 同一件事换成管道形态就漏——孪生路径漏改的典型。
                if (sp := _cmd_spec(s)) not in heads:
                    heads.append(sp)
            return heads
        if _has_shell_meta(cmd):
            # 含元字符的命令，decide 压根不看 allow 表（直接落 default）→ 记下的 spec 永远不生效，
            # 却是一条比本次调用更宽的命令头授权（`echo hi > x` 记成 `echo:*`）。两头都错 → 不可授权。
            return []
        return [_cmd_spec(cmd)]
    if tool in _PATH_TOOLS:
        raw = _subject(tool, args)
        base = raw
        if tool == "glob" and root and _glob_escapes(args, root):
            # pattern 会把落点带出根：spec 是按 path 生成的，而危险在 pattern 里——记下的规则既
            # 不能如实表达"批准了列根外"，弹窗上显示的也只是那个根内的 path（不披露逃逸）。
            # 表达不了就不承诺：不给"总是允许"，用户仍可"允许一次"。同 bash 含元字符那一档。
            return []
        path = _abs(base) or _norm(base)          # 与 decide 同源：先算落点（解 .. 与软链）
        if not path:
            return []                             # 空 path / 非 dict args → 不可授权（此前记下空串死条目）
        # 【只在要拼 /** 的 spec 上转义】：那条走 fnmatch，路径里的 * ? [ 不转义会被当模式；
        # 而【精确 spec】走的是逐字符相等，转义反而比不上（`x[1].txt` 转成 `x[[]1].txt` 就永远不自匹配）。
        if tool in ("grep", "glob"):              # 搜索路径本身就是目录 → 目录本身 + 其下，两条
            d = path.rstrip("/") or path
            return [d, f"{_esc_glob(d)}/**"]
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        if _root_like(folder, root):              # 项目根 / 盘符根 / 文件系统根 → 退回精确到该文件
            return [path]
        return [f"{_esc_glob(folder)}/**"]        # 文件 → 所在文件夹及其下
    # 其余工具（subagent/web_*/task_*/MCP 工具等）subject 恒为空：参数是自然语言 prompt / URL /
    # 阶段结构，没有可用来收窄范围的维度 →"总是允许"只可能是"以后这个工具都放行"，粒度就是工具本身。
    return ["*"]


def _guard(specs: list[str], tool: str, args: dict, root: str | None) -> list[str]:
    """spec 生成后的【机制护栏】：两条不变量在这里被强制成立，而不是靠各分支自己写对。

    这两道是本模块最重要的防线——两轮对抗审查里，spec 生成的坑全部是"某个分支忘了考虑某形态"，
    而分支只会越加越多。把不变量做成【生成后统一校验】，新分支写错也会被当场兜住。

    ① 自匹配：生成的 spec 必须命中本次调用（用 _allow_subjects，与 decide 同源）。破了 → 退回
       精确路径形态。此前 _esc_glob 把 `<根>/x[1].txt` 转义成 `<根>/x[[]1].txt`，串里没有 * ?
       → 落 _spec_match 的【精确相等】分支 → 与原串逐字符比必然不等，用户点了"不再问"却每次还问。
    ② 范围不外溢：spec 不得把【项目根】整个圈进去（除非被批对象本身就在根内——那是正常的
       "根内某文件夹及其下"）。此前 _root_like 只认 folder==root、不认 root 的【祖先】，对项目
       外一层的文件点一次"总是允许"，发出去的 spec 反过来把整个项目连同 .mecode/permissions.json
       与 .git/ 一起放行。
       **只管 read/write/edit**：它们的 spec 是"文件所在文件夹 + /**"，比批准的那一个文件宽。
       grep/glob 不受——subject 就是那个目录、操作又天然递归，批准一次即已读遍整棵树，收掉
       `<目录>/**` 挡不住任何读取，只制造"批过还问"。
    """
    if not specs:
        return specs
    subjects = _allow_subjects(tool, args)
    exact = subjects[0] if subjects else ""

    segs = _pipeline_segments(exact) if tool == "bash" else None

    def ok(spec_list: list[str]) -> bool:
        # 必须与 decide 同源：管道是【逐段】比对的，拿整条命令去自检等于检查了另一件事
        # （此前靠"前缀 spec 恰好也是整条命令的前缀"蒙混过关，spec 一变精确就把好 spec 收成了死规则）
        if segs is not None:
            return all(_any(spec_list, s) for s in segs)
        return any(_any(spec_list, s) for s in subjects)

    if tool in _FILE_TOOLS and root and exact:          # ② 范围：spec 不得圈住项目根（grep/glob 见文档）
        inside = _in_root(exact, root)                  # 被批对象本来就在根内 → 圈到根内某层是正常的
        if not inside and any(_spec_match(sp, root) or _spec_match(sp, root + "/x") for sp in specs):
            specs = [exact]
    if not ok(specs):                                   # ① 自匹配：破了就退回【精确形态】
        # 路径类 → 精确路径；bash → 整条命令（_exact_cmd_spec 顺带转义 * ?，否则 `rm *.tmp` 会
        # 落 fnmatch 分支当模式用）。无细分维度的工具 spec 是 '*'，恒自匹配，走不到这。
        # 管道没有可退的精确形态——整条命令的 spec 在 decide 的逐段比对里用不上，记了也是死规则 → 不授权。
        if segs is not None:
            return []
        specs = [_exact_cmd_spec(exact) if tool == "bash" else exact] if exact else []
    return specs


def _bash_path_grants(command: str, root: str) -> list[tuple[str, str]] | None:
    """单条 bash 命令点"总是允许"时要【连带】写下的落点授权：[(read_file|write_file, 绝对路径)]。
    None = 落点算不出 → 这条命令不可授权（闸永远拦它，别承诺）。

    非连带不可：闸按落点问 read_file/write_file，而 bash 侧记的是命令头前缀 `cat /etc/hosts:*`
    ——它表达不了"这个文件可以读"，只记前缀的话闸下次照样在落点上拦住 = 点了不再问、下次还问。
    粒度精确到落点（不是 read_file"总是允许"给的文件夹级）：用户批的就是这条命令碰的这几个文件。
    根内的读不记（默认本就放行）；写一律记（write_file 在根内也要审批）。"""
    probes = _bash_file_probes(command)
    if probes is None:
        return None
    reads, writes = probes
    out: list[tuple[str, str]] = []
    for p in reads:
        if (a := _abs(p)) and not _in_root(a, root):   # 根内读默认放行，无需授权
            out.append(("read_file", a))
    for p in writes:
        if a := _abs(p):
            out.append(("write_file", a))
    return out


def grants_for(tool: str, args: dict, root: str | None = None) -> list[tuple[str, str]]:
    """该次调用点"总是允许"要写下的【全部】规则：[(工具, spec)]。空列表 = 不可授权。
    多数工具就是 [(自己, spec)]；bash 额外带上落点授权（见 _bash_path_grants）——闸按落点判，
    授权就得按落点记，两侧同源。"""
    specs = specs_for(tool, args, root)
    if not specs:
        return []
    out = [(tool, s) for s in specs]
    if tool == "bash" and root:
        cmd = _subject(tool, args)
        for seg in (_pipeline_segments(cmd) or [cmd]):
            extra = _bash_path_grants(seg, root)
            if extra is None:
                return []                              # 有一段的落点算不出 → 整条不可授权
            out.extend(g for g in extra if g not in out)
    return out


def spec_for(tool: str, args: dict, root: str | None = None) -> str:
    """单条主 spec（显示/兼容用；多条见 specs_for）。不可授权时返回空串。"""
    specs = specs_for(tool, args, root)
    return specs[-1] if specs else ""


def pattern_for(tool: str, args: dict, root: str | None = None) -> str:
    """显示用的可读形式（同 Claude 的 Tool(spec)）：如 bash(rm:*)、bash(cat:*, jq:*)、write_file(C:/a/b/**)。
    不可授权（grants_for 为空）时返回空串——调用方据此【不显示"总是允许"这一项】，别承诺记不住的事。
    bash 连带的落点授权也列出来（`bash(cat X:*) + read_file(X)`）：多记了什么就写在弹窗上。"""
    grants = grants_for(tool, args, root)
    if not grants:
        return ""
    by_tool: dict[str, list[str]] = {}
    for t, spec in grants:
        by_tool.setdefault(t, []).append(spec)
    return " + ".join(f"{t}({', '.join(v)})" for t, v in by_tool.items())


def _clone(rules: dict) -> dict:
    """深拷贝规则（{tool:{decision:[spec]}}），免得运行时 allow_always 改到模块级 DEFAULT_RULES。"""
    return {t: {d: list(specs) for d, specs in groups.items()} for t, groups in rules.items()}


class PermissionPolicy:
    def __init__(self, rules: dict | None = None, default: str = DEFAULT_DECISION,
                 root: str | None = None, plan_path: str | None = None) -> None:
        self.rules = _clone(DEFAULT_RULES if rules is None else rules)
        self.default = default
        self.root = root          # 项目根（posix）；模式覆盖里的 @root 占位展开用（auto 放行项目内编辑）。None=无项目上下文
        self.plan_path = plan_path   # 计划文件路径；非 None（=计划模式）时，唯独放行对它的 write/edit（只读之下的写例外）
        # 注：曾另存一份"用户显式加的 spec"（_user_allow），供读命令根闸开一个"用户授权过这条命令
        # 就整道闸让路"的出口。那是个提权洞（见 _file_gate_ok），随闸改按落点判已删——授权状态只有
        # self.rules 一处。

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
            # realpath 与 _abs 同源（见其文档）：两边必须都解替身，否则项目根本身位于软链/subst 下时
            # root 与 subject 不同源，根内的正常路径会全部误落 ask。
            root = os.path.realpath(str(project_root)).replace("\\", "/")
            # 去掉尾部斜杠：项目根是盘符根时 realpath 给的是 "C:/"，于是根规则拼成 "C://**"（畸形）、
            # _in_root 拿 root+"/" = "C://" 去比也永远不中 → 整道闸失灵，根内路径全落 ask。
            # 普通根不带尾斜杠，这行对它们是空操作。POSIX 根 "/" → "" ：_in_root 的 startswith("/")
            # 与规则 "/**" 都仍然成立。
            root = root.rstrip("/") if root != "/" else ""
            for t in ("read_file", "grep", "glob"):
                merged.setdefault(t, {})["allow"] = [root, f"{root}/**"]   # 根本身 + 根下
        # skill 文件（system prompt 只给索引、模型场景命中时才 read_file 读全文）常在项目根外
        # （内置在安装目录、用户级在 ~/.mecode）——读它们无副作用，默认放行，别为读个流程说明弹授权。
        allow = merged.setdefault("read_file", {}).setdefault("allow", [])
        for d in (Path(__file__).resolve().parent / "skills_builtin",
                  Path("~/.mecode/skills").expanduser().resolve()):   # resolve=realpath，与 _abs 同源
            p = d.as_posix()
            allow.extend([p, f"{p}/**"])
        for tool, groups in (saved or {}).items():
            if not isinstance(groups, dict):       # 跳过旧格式/坏数据（如早期扁平表），别让它搞崩启动
                continue
            dst = merged.setdefault(tool, {})
            for decision, specs in groups.items():
                if not isinstance(specs, list):
                    continue
                cur = dst.setdefault(decision, [])
                cur.extend(s for s in specs if s not in cur)
        return cls(rules=merged, root=root)

    def decide(self, tool: str, args: dict) -> str:
        """返回 allow / deny / ask。先取该工具的规则组，再 deny > allow > default(ask)。"""
        subject = _subject(tool, args)
        # 计划文件例外：唯独放行对【计划文件本身】的 read/write/edit（模型用它迭代计划、执行期回看）。
        # 放在 deny 之前 → 覆盖 plan 覆盖层对 write/edit 的 deny "*"，也免去项目外读它被问；其余写/改照旧。
        if self.plan_path and tool in ("read_file", "write_file", "edit_file") \
                and _same_path(subject, self.plan_path):
            return ALLOW
        r = self.rules.get(tool, {})
        # 路径类工具的 allow 比对形态：
        # ① _abs = realpath 真实落点（解掉 .. 与软链/junction/subst）——匹配"项目根内放行"的绝对规则，
        #    也让模型给的相对路径/'.'/'src' 能命中它。这一条是权威形态。
        # ② 归一化的【相对】形态——仅为匹配用户自己写的相对 spec（如 src/**）而补。
        # 【关键】allow 侧【不】拿未解析的绝对原始串比对，两个已证实的绕过都出在它身上：
        #    `<根>/../../etc/x` 与 `<根>/软链/x` 都以根开头，会被 `<根>/**` 匹配上而放行（fnmatch 的
        #    * 跨 /），但 OS 执行时会落到根外——"检查的字符串 ≠ 执行的路径"。
        subjects = _allow_subjects(tool, args)      # 单一真相源（spec 生成的自检走同一份）
        # deny 侧另外保留【未解析的原始串】：多一个形态在 deny 是多一次被拦的机会（更安全），
        # 在 allow 是多一条放行路（更宽松）——同一件事在两侧语义相反。
        deny_subjects = subjects if subject in subjects else [subject, *subjects]
        if any(_any(r.get(DENY, []), s) for s in deny_subjects):
            return DENY
        # bash 含 shell 副作用元字符 → 不让 allow 前缀直接放行（deny 已先判过）。
        # 唯一例外：纯管道且【每一段命令都在 allow 名单里】→ 放行（cat|grep|wc 这类只读命令互接，安全、免问）。
        if tool == "bash" and _has_shell_meta(subject):
            segs = _pipeline_segments(subject)
            if segs is not None and all(_any(r.get(ALLOW, []), s) for s in segs):
                # 每段的读命令（cat/grep 等）路径参数也要过落点闸（同下方单命令那道）
                if self.root and not all(self._file_gate_ok(s) for s in segs):
                    return self.default
                return ALLOW
            return self.default
        if any(_any(r.get(ALLOW, []), s) for s in subjects):
            # 读/写文件的命令（cat/head/tail/grep/wc/cut、git diff --output）即便命中放行，
            # 其文件落点仍要各自过 read_file / write_file 那道闸（见 _file_gate_ok）。仅在有项目根时生效。
            if tool == "bash" and self.root and not self._file_gate_ok(subject):
                return self.default
            # glob 的 path 即便在根内，pattern 仍能把搜索落点带出去（../* 、**/../* 、{..,x}/**/*）
            # → 补一道 pattern 闸。
            # 【没有"用户显式授权即跳过"的出口】：_glob_escapes 判的是"落点在根内【或 path 自己底下】"，
            # 所以用户批准过的根外目录配正常 pattern 本就不触发这道闸（不变量①自然成立），出口是多余的；
            # 而留着它反倒会把【逃逸 pattern】一并放行——那类调用 specs_for 明确判为"不可授权"，
            # 等于从另一扇门把它授权了出去（批准 glob(C:/某目录) 换来 ../../* 整片列举）。
            if tool == "glob" and self.root and _glob_escapes(args, self.root):
                return self.default
            return ALLOW
        return self.default

    def _file_gate_ok(self, command: str) -> bool:
        """单条 bash 命令过"文件落点闸"：把它要读/写的文件解析成绝对落点，逐个回头问
        read_file / write_file 那两道闸——读按读的规矩判、写按写的规矩判。

        按落点问、而不是看命令像不像被授权过：闸管的是"哪几个文件"，而 bash 的 allow spec 是命令头
        前缀（`cat:*`），粒度对不上。此前末行是"命中任一 bash allow spec 即整道闸让路"，于是尾巴上
        追加的路径搭便车：批过 `cat README.md` 换来 `cat README.md ../../secret`；批过只读的
        `git diff` 换来 `git diff --output C:/任意`（读权限就地提权成任意文件写）。
        改成按落点问后，"同一个文件能不能读/写"只有一套答案，不再有第二套判据可绕。"""
        probes = _bash_file_probes(command)
        if probes is None:
            return False                      # 落点静态算不出（$ 变量 / 花括号 / 通配带 ..）→ 保守拦
        reads, writes = probes
        return (all(self.decide("read_file", {"path": p}) == ALLOW for p in reads)
                and all(self.decide("write_file", {"path": p}) == ALLOW for p in writes))

    def add(self, tool: str, decision: str, spec: str) -> None:
        lst = self.rules.setdefault(tool, {}).setdefault(decision, [])
        if spec not in lst:
            lst.append(spec)

    def allow_always(self, tool: str, args: dict) -> list[tuple[str, str]]:
        """记住该次调用对应的"总是允许"规则（本进程即时生效），返回 [(工具, spec)]——管道每段一条头，
        bash 还会连带落点授权，故规则可能落在【别的工具名下】，持久化要按返回的那个工具名落盘。
        **返回空列表 = 这次调用不可授权**（畸形参数 / 含元字符的 bash / 落点算不出的命令等），此时
        什么都不记：宁可下次再问，也不留一条记不住或比批准范围更宽的规则。"""
        grants = grants_for(tool, args, self.root)   # 带上项目根：判"文件夹是否大到不该整片授权"
        for t, spec in grants:
            self.add(t, ALLOW, spec)
        return grants
