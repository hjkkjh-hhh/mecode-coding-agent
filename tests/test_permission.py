"""工具权限：按 tool(spec) 模式做参数细分的策略 + agent 执行前过闸。

固化的不变量：
- 默认：只读类/安全只读命令 allow，其余（写/改/未列 bash）落 default=ask
- 参数细分：bash(ls:*) 放行、bash(rm …) 仍 ask；spec 支持前缀/精确/glob
- 优先级 deny > ask > allow > default（窄规则压广规则）
- allow_always 生成 pattern（bash 记首词、其余记整工具）；agent 闸 deny/once/always
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mecode.agent import Agent
from mecode.events import Done, TextDelta, ToolCall, Usage
from mecode.permission import ALLOW as ALLOW_
from mecode.permission import PermissionPolicy, pattern_for, spec_for, specs_for
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


def _root() -> str:
    """测试用的项目根：走【与实现同源】的归一化（realpath + 正斜杠）。

    不能裸用 os.getcwd()：realpath 会把盘符大小写归一（Git Bash `cd /c/...`、部分终端下 getcwd
    给的是小写 c:/…，realpath 归一成 C:/…），断言两边大小写不同就直接变红——测试挂在启动方式上，
    而不是挂在被测行为上。
    """
    return os.path.realpath(os.getcwd()).replace("\\", "/")


def _land(path: str) -> str:
    """路径的真实落点（与实现的 _abs 同源）：断言 allow_always 连带写下的落点授权时用。"""
    return os.path.realpath(path).replace("\\", "/")


def _fake_agent():
    """审批桥测试用的假 agent。字段要和真 agent 对得上（policy / _interrupt / _bg）——少一个就会在
    断言之前先 AttributeError，测出来的是"假对象没搭全"而不是被测行为。三处共用，别再各写一份。"""
    import threading
    return type("A", (), {"policy": None, "_interrupt": threading.Event(),
                          "_bg": type("BG", (), {"running": lambda s: []})()})()

# ---------- 纯策略：参数细分 ----------

def test_默认_bash安全命令放行_危险问_无项目则路径工具皆问():
    p = PermissionPolicy()                                              # 裸 policy，无 project_root
    assert p.decide("bash", {"command": "ls -la"}) == "allow"          # bash(ls:*)
    assert p.decide("bash", {"command": "git status --short"}) == "allow"
    assert p.decide("bash", {"command": "rm -rf x"}) == "ask"          # 未列 → default ask（不一刀切放行）
    # 路径类工具的放行依赖项目根（from_persisted 注入）；无项目上下文 → 一律问，不再默认放行任意路径
    for tool in ("read_file", "write_file", "edit_file", "grep", "glob"):
        assert p.decide(tool, {"path": "/x", "pattern": "y"}) == "ask"


def test_默认_任务清单与后台工具放行():
    p = PermissionPolicy()                                              # 裸 policy，无 project_root
    for tool in ("task_create", "task_update", "task_get", "task_list"):
        assert p.decide(tool, {"subject": "x"}) == "allow"             # 纯内部状态、无副作用 → 不打扰
    for tool in ("check_bgtask", "wait_bgtask", "kill_bgtask"):
        assert p.decide(tool, {"id": 1}) == "allow"                    # 看输出/调节奏/停自己起的后台任务
    assert p.decide("subagent", {"prompt": "x"}) == "allow"            # 派生子 agent：信任委派（选项 a）


def test_spec_前缀_精确_glob():
    p = PermissionPolicy(rules={"bash": {"allow": ["npm run test:*", "git push"]},
                                "write_file": {"allow": ["src/**"]}}, default="ask")
    assert p.decide("bash", {"command": "npm run test"}) == "allow"     # 前缀：head 本身
    assert p.decide("bash", {"command": "npm run test:watch"}) == "allow"
    assert p.decide("bash", {"command": "npm run build"}) == "ask"      # 不匹配
    assert p.decide("bash", {"command": "git push"}) == "allow"         # 精确
    assert p.decide("bash", {"command": "git push --force"}) == "ask"   # 精确不含变体
    assert p.decide("write_file", {"path": "src/a/b.py"}) == "allow"    # glob
    assert p.decide("write_file", {"path": "tests/x.py"}) == "ask"


def test_bash含shell元字符_不被前缀放行_落到问():
    p = PermissionPolicy()                                              # 默认 ls:* 放行
    assert p.decide("bash", {"command": "ls -la"}) == "allow"          # 简单命令照常放行
    assert p.decide("bash", {"command": "echo x > 重要文件"}) == "ask"  # 重定向写文件 → 不放行
    assert p.decide("bash", {"command": "ls; rm -rf x"}) == "ask"      # 链接危险命令
    assert p.decide("bash", {"command": "cat a | sh"}) == "ask"        # 管道
    assert p.decide("bash", {"command": "echo $(rm x)"}) == "ask"      # 命令替换
    assert p.decide("bash", {"command": "find . -exec rm {} ;"}) == "ask"  # ; 被抓


def test_含元字符仍可被deny命中():
    p = PermissionPolicy(rules={"bash": {"deny": ["sudo:*"]}}, default="ask")
    assert p.decide("bash", {"command": "sudo rm > x"}) == "deny"       # deny 在元字符守卫之前判


def test_优先级_deny压allow_其余落default():
    p = PermissionPolicy(rules={"bash": {"allow": ["ls:*"], "deny": ["sudo:*"]}}, default="ask")
    assert p.decide("bash", {"command": "ls"}) == "allow"               # 在 allow
    assert p.decide("bash", {"command": "rm x"}) == "ask"               # 不在 allow/deny → default
    assert p.decide("bash", {"command": "sudo x"}) == "deny"            # 在 deny
    # 同一串既在 allow 又在 deny → deny 赢
    p2 = PermissionPolicy(rules={"bash": {"allow": ["*"], "deny": ["sudo:*"]}}, default="ask")
    assert p2.decide("bash", {"command": "sudo x"}) == "deny"


def test_spec_for与pattern_for():
    assert spec_for("bash", {"command": "rm -rf x"}) == "rm -rf x"      # 夹了选项 → 整条精确
    assert pattern_for("bash", {"command": "rm -rf x"}) == "bash(rm -rf x)"     # 显示用
    assert spec_for("bash", {"command": "git diff HEAD~1"}) == "git diff:*"     # 紧跟子命令 → 前缀
    # 记法用 **（不是 *）：fnmatch 里两者翻译出的正则完全相同（* 本就跨 /），纯记法变更、零行为变化；
    # 但与本模块根规则 <根>/** 统一，也让弹窗上的串如实说出"含子目录"（用户按 shell glob 直觉会读成只有一层）。
    assert spec_for("write_file", {"path": "C:/a/b/x.txt"}) == "C:/a/b/**"   # 文件→所在文件夹及其下
    assert spec_for("edit_file", {"path": "C:/a/b/x.txt"}) == "C:/a/b/**"
    assert pattern_for("write_file", {"path": "C:/a/b/x.txt"}) == "write_file(C:/a/b/**)"
    # 裸文件名先按 cwd 解析成落点（与 decide 的比对形态同源），故拿到的是它【真实所在文件夹】及其下
    cwd = _root()
    assert spec_for("read_file", {"path": "x.txt"}) == cwd + "/**"
    # 但传了项目根时（allow_always 就是这么调的），根下的文件退回精确授权——否则一次点击 = 整个项目
    assert spec_for("read_file", {"path": "x.txt"}, root=cwd) == cwd + "/x.txt"


def test_默认只放行只读类项目根内_写改都要问():
    p = PermissionPolicy.from_persisted({}, project_root="C:/proj")
    assert p.root == "C:/proj"                                              # 项目根存下来（@root 展开用）
    # 只读类（读/搜）：根内直接放、根外问（无副作用）
    assert p.decide("read_file", {"path": "C:/proj/src/x.py"}) == "allow"
    assert p.decide("read_file", {"path": "C:/other/x.py"}) == "ask"
    # 写/改：默认（normal）不自动放行——【根内也要问】（由 auto 模式再放行项目内编辑）
    for tool in ("write_file", "edit_file"):
        assert p.decide(tool, {"path": "C:/proj/src/x.py"}) == "ask"
        assert p.decide(tool, {"path": "C:/other/x.py"}) == "ask"
    # grep/glob 按【搜索路径 path】限制（不是搜索 pattern）
    assert p.decide("grep", {"pattern": "foo", "path": "C:/proj/src"}) == "allow"
    assert p.decide("grep", {"pattern": "foo", "path": "C:/proj"}) == "allow"    # 根本身
    assert p.decide("grep", {"pattern": "foo", "path": "C:/other"}) == "ask"     # 根外 → 问
    assert p.decide("glob", {"pattern": "**/*.py", "path": "C:/other"}) == "ask"
    # 在根外文件夹选"总是允许"后，该文件夹放行（精确到文件夹）
    p.allow_always("write_file", {"path": "C:/other/data/a.txt"})
    assert p.decide("write_file", {"path": "C:/other/data/b.txt"}) == "allow"
    assert p.decide("write_file", {"path": "C:/other/elsewhere/c.txt"}) == "ask"


def test_相对路径与点号也认作根内放行():
    # 模型常给相对搜索路径/'.'/'src'（如 glob(pattern="src/**/*", path=".")）——subject 是相对的，
    # 而根内放行规则是绝对路径。补按 cwd 解析的绝对形态比对后，这些应放行（此前误落 ask）。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("glob", {"pattern": "src/**/*", "path": "."}) == "allow"     # 就是弹窗那个 case
    assert p.decide("grep", {"pattern": "foo", "path": "src"}) == "allow"        # 相对子目录
    assert p.decide("read_file", {"path": "src/mecode/agent.py"}) == "allow"     # 相对文件
    assert p.decide("glob", {"pattern": "**/*"}) == "allow"                      # path 省略=当前目录
    assert p.decide("read_file", {"path": "../secret.txt"}) == "ask"            # 相对跳出根 → 仍问


def test_glob的pattern里双点一律拒():
    """闸判的是 path，而 pattern 是路径通配、能带 .. 爬出 path。`..` 是唯一出路（绝对 pattern 被
    pathlib 直接拒、~ 不展开）→ 一律禁，不再去静态推算落点（推算正是此前被绕过的地方）。"""
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("glob", {"pattern": "src/**/*", "path": "."}) == "allow"     # 正常根内
    assert p.decide("glob", {"pattern": "../../*", "path": "."}) == "ask"        # 逃出根
    assert p.decide("glob", {"pattern": "../*", "path": "src"}) == "ask"         # 落点虽回到根内，也禁
    # 花括号能把 .. 藏在 token 内部：按 / 切开没有裸 `..` 段，字符串层检测漏判，而执行侧
    # tools._expand_braces 展开后真的沿 .. 爬出根（实测能枚举出三万多个根外文件）→ 按展开后的判。
    for pat in ("{..,x}/**/*", "{x,..}/**/*", "{../..,x}/**/*"):
        assert p.decide("glob", {"pattern": pat}) == "ask", pat
    assert p.decide("glob", {"pattern": "*.{py,md}"}) == "allow"                 # 正常花括号不受影响


def test_路径里的双点不能绕过根内限制():
    # 洞：allow 侧曾拿【未归一化的原始串】比对，而 fnmatch 的 * 跨 / → `<根>/../../x` 以根开头就被
    # `<根>/**` 匹配上放行，但 OS 执行那两个 .. 会落到根外（检查的字符串 ≠ 执行的路径）。
    # 归一化后落点在字符串层就摆正，不再匹配根规则。bash 侧本就先算落点再判根内，两侧现已同源。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    for tool, args in (("read_file", {"path": root + "/../../etc/passwd"}),
                       ("read_file", {"path": root + "/src/../../../etc/passwd"}),
                       ("read_file", {"path": root + "/../secret.txt"}),
                       ("grep", {"pattern": "x", "path": root + "/../../etc"}),
                       ("glob", {"pattern": "../../*", "path": root})):
        assert p.decide(tool, args) == "ask", (tool, args)
    # 反斜杠形态（Windows 下模型常给）同样不能绕
    assert p.decide("read_file", {"path": root.replace("/", "\\") + r"\..\..\etc\passwd"}) == "ask"
    # 正常根内路径不受影响（绝对/相对/点号/带 . 与 .. 但仍落在根内）
    assert p.decide("read_file", {"path": root + "/src/mecode/agent.py"}) == "allow"
    assert p.decide("read_file", {"path": root + "/src/./mecode/../mecode/agent.py"}) == "allow"
    assert p.decide("read_file", {"path": "src/mecode/agent.py"}) == "allow"
    assert p.decide("glob", {"pattern": "**/*", "path": "."}) == "allow"


def test_auto模式下写改也不能借双点写到根外():
    # write/edit 在 auto 模式由 @root 展开成 `<根>/**` 放行项目内编辑 → 同一个洞会让它写到根外。
    from mecode.mode import apply_mode
    root = _root()
    p = apply_mode(PermissionPolicy.from_persisted({}, project_root=root), "auto")
    assert p.decide("write_file", {"path": root + "/x.py", "content": "x"}) == "allow"   # 项目内照常自动
    assert p.decide("write_file", {"path": root + "/../../.bashrc", "content": "x"}) == "ask"
    assert p.decide("edit_file", {"path": root + "/../../.bashrc"}) == "ask"


def test_git的output选项不能借白名单写文件():
    # 洞：git diff/log/show 在默认放行名单里（"只读"），但它们都接受 --output=<file> 把 diff 写进任意
    # 文件（内容为空时把目标清成 0 字节）。shell 重定向 `git diff > f` 被元字符闸拦下，--output 不经
    # shell 故绕开了它。根内也拦：写项目内文件本就该同 write_file 一样过审批。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    for cmd in ("git diff --output=/tmp/x",
                "git diff --output=./inside.txt",       # 根内写也要问（与 write_file 一致）
                "git diff --output /tmp/x",
                "git log --output=/tmp/x",              # log/show 同样接受 diff 选项
                "git show --output=/tmp/x",
                "git log -1 --output=./inside.txt"):
        assert p.decide("bash", {"command": cmd}) == "ask", cmd
    # 正常用法不受影响
    assert p.decide("bash", {"command": "git diff"}) == "allow"
    assert p.decide("bash", {"command": "git diff HEAD~1 --stat"}) == "allow"
    assert p.decide("bash", {"command": "git log --oneline -5"}) == "allow"
    assert p.decide("bash", {"command": "git show HEAD:README.md"}) == "allow"


def _make_link(link: Path, target: Path) -> bool:
    """在 link 处建一个指向 target 的目录链接。Windows 用 junction（不需要管理员权限，
    symlink 要开发者模式），其余平台用 symlink。建不成返回 False（用例跳过）。"""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True)
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return False
    return link.exists()


def test_软链指向根外不能绕过根内限制(tmp_path):
    # 洞：abspath 是纯字符串运算，不问文件系统"这段路是不是替身"——项目内一个指向根外的软链/junction
    # （node_modules 里很常见），abspath 算出来仍在根内而 open() 会跟着替身走到根外。改用 realpath
    # 判真实落点；root 侧同步 realpath，两边同源。
    proj, outside = tmp_path / "proj", tmp_path / "outside"
    (proj / "src").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    if not _make_link(proj / "node_modules", outside):
        pytest.skip("本环境建不了目录链接")
    p = PermissionPolicy.from_persisted({}, project_root=str(proj))
    out_via_link = (proj / "node_modules" / "secret.txt").as_posix()
    assert p.decide("read_file", {"path": out_via_link}) == "ask"        # 经软链落到根外 → 问
    assert p.decide("grep", {"pattern": "x", "path": (proj / "node_modules").as_posix()}) == "ask"
    assert p.decide("read_file", {"path": (proj / "src").as_posix()}) == "allow"   # 根内照常放行


def test_软链指向根内照常放行不打扰(tmp_path):
    # realpath 只看落点：软链指向【项目内】时无害，不该因为"它是软链"就打扰用户
    # （pnpm 的 node_modules 全靠软链搭起来，一律问会让项目没法用）。
    proj = tmp_path / "proj"
    (proj / "real").mkdir(parents=True)
    (proj / "real" / "a.txt").write_text("x", encoding="utf-8")
    if not _make_link(proj / "alias", proj / "real"):
        pytest.skip("本环境建不了目录链接")
    p = PermissionPolicy.from_persisted({}, project_root=str(proj))
    assert p.decide("read_file", {"path": (proj / "alias" / "a.txt").as_posix()}) == "allow"


def test_项目根本身位于软链下时根内路径照常放行(tmp_path):
    # root 与 subject 必须同源解替身：只改一边的话，项目根位于软链下时根内的正常路径会全部误落 ask。
    real = tmp_path / "real_proj"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link_proj"
    if not _make_link(link, real):
        pytest.skip("本环境建不了目录链接")
    p = PermissionPolicy.from_persisted({}, project_root=str(link))      # 用软链路径当项目根
    assert p.decide("read_file", {"path": (link / "src" / "x.py").as_posix()}) == "allow"
    assert p.decide("read_file", {"path": (real / "src" / "x.py").as_posix()}) == "allow"  # 真实路径也认
    assert p.decide("read_file", {"path": (tmp_path / "外面.txt").as_posix()}) == "ask"


def _fake_provider():
    """最简 provider：不产工具调用、只回一句话（造 Agent 用，不发网络）。"""
    class P:
        def stream(self, messages, tools=None, should_stop=None):
            yield TextDelta("ok")
            yield Done(reason="stop")
    return P()


def test_主agent真的把自己接给了runner():
    # 关键接线：Agent.__init__ 里 SubagentRunner(..., owner=self)。少了它，子 agent 又变回全放行，
    # 而只测 runner 本身的用例是发现不了的（审查指出：删掉 owner=self 全套测试仍绿）。
    root = _root()
    policy = PermissionPolicy.from_persisted({}, project_root=root)
    a = Agent(_fake_provider(), ToolRegistry(), policy=policy,
              ask_permission=lambda n, ar, ctx=None: "deny")
    assert a._subagent is not None
    assert a._subagent.owner is a                     # ← 就是这条接线
    sub = a._subagent._make(None)
    assert sub.policy is a.policy                     # 共用同一对象："总是允许"批的是操作本身，全局生效
    assert sub.ask_permission is a.ask_permission     # 审批回调也继承（前台后台一视同仁）
    assert sub._is_subagent is True                   # 标记为子 agent → 弹窗才会给"停止此分支"


def test_子agent继承主agent权限_换个说法逃不掉():
    # 洞：子 agent 此前 policy=None 全放行 → normal 模式下 `rm -rf` 要审批，包成 subagent 就不用，
    # 是整个权限系统的绕过口。现在继承主 agent 当前 policy（含模式覆盖）：权限 = 你当前模式的权限。
    from mecode.config import agent_config
    from mecode.subagent import SubagentRunner
    root = _root()
    policy = PermissionPolicy.from_persisted({}, project_root=root)
    called = []
    owner = type("O", (), {})()
    owner.policy = policy
    owner.ask_permission = lambda n, ar, ctx=None: called.append(n) or "deny"

    r = SubagentRunner(object(), agent_config, owner=owner)
    sub = r._make(None)
    assert sub.policy is policy                       # 继承同一个 policy 对象
    assert sub.ask_permission is owner.ask_permission  # 也继承审批回调（非空 → 断言不是空转）
    assert sub.policy.decide("bash", {"command": "rm -rf C:/x"}) == "ask"   # 主 agent 拒的，这里也拒
    # 子 agent 没有 exit_plan：它没有用户可提交计划，留着一调就提前结束自己那一轮且不出总结
    assert "exit_plan" not in [s["function"]["name"] for s in sub.tools.schemas()]

    # 无 owner（直接构造 runner 的老用法/测试）→ 保持 policy=None 全放行，不改既有行为
    bare = SubagentRunner(object(), agent_config)._make(None)
    assert bare.policy is None and bare.ask_permission is None


def test_停止此分支只给能被单独停的分支():
    """走【真实入口】run()/run_for_bg()，不是直接调 _make——接线错了才是真事故
    （直接调 _make 的写法：把 run_for_bg 里的 own_branch=True 删掉，测试照样绿）。

    前台子 agent 与主 agent【共享】同一个 interrupt（SubagentRunner 把主 agent 的 _interrupt 传给它）
    → 置它等于按 Ctrl+C 停整轮、连并发的兄弟子 agent 一起，"停止此后台任务"对它是骗人的。
    只有后台/workflow 子 agent 自带独立 Event，停它才真的只停这一条。"""
    import threading

    from mecode.config import agent_config
    from mecode.subagent import SubagentRunner
    seen = []
    owner = type("O", (), {})()
    owner.policy = PermissionPolicy()                      # rm 落 ask → 触发审批回调
    owner.ask_permission = lambda n, ar, ctx=None: seen.append(ctx) or "deny"
    main_ev = threading.Event()
    # 每条路各给一个新 provider：_OneToolProvider 是有状态的（只在第一次 stream 发工具调用），
    # 复用同一个会让第二条路根本走不到权限闸，测试就成了假通过。
    def _runner(ev):
        return SubagentRunner(_OneToolProvider("bash", {"command": "rm -rf x"}),
                              agent_config, interrupt=ev, owner=owner)

    _runner(main_ev).run("干活")                            # ← 前台真实入口
    assert seen[-1].is_sub is True
    assert seen[-1].can_stop is False                      # 前台不给"停止此分支"（会连主 agent 一起停）
    assert seen[-1].interrupt is main_ev                   # 与主 agent 共享的那个标志

    bg_ev = threading.Event()
    _runner(main_ev).run_for_bg("干活", bg_ev, lambda slot: None)   # ← 后台真实入口
    assert seen[-1].can_stop is True                       # 后台自带独立标志 → 给
    assert seen[-1].interrupt is bg_ev                     # 循环盯它 → kill_bgtask 能解开卡在弹窗上的它
    assert seen[-1].interrupt is not main_ev


def test_审批弹窗的选项文案与返回值一一对应():
    """判据必须是 can_stop（能否单独停），不是 is_sub —— 前台子 agent 也是 is_sub，但给它这一项
    会连主 agent 和并发兄弟一起停。

    更要紧的是【文案与返回值的绑定】：曾经 compose 里"停止"排在"拒绝"之前、_choices 里排在最后，
    于是点"停止此后台任务"只拒了这一步（任务照跑）、点"拒绝"反而把整个后台任务停了 —— 两个选项的
    行为互换，而只断言 _choices 顺序的测试完全看不出来（它没碰屏上那份文案）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import tui
    root = _root()
    args = {"path": root + "/src/a.py", "content": "x"}

    def 绑定(**kw):
        m = tui.PermissionModal("write_file", args, root=root, **kw)
        return [(c, str(label)) for c, label in m._options]

    前台 = 绑定(is_sub=True, can_stop=False)
    后台 = 绑定(is_sub=True, can_stop=True)
    assert "stop" not in [c for c, _ in 前台]              # 前台子 agent：不给
    assert "stop" in [c for c, _ in 后台]                  # 后台子 agent：给
    # 每个返回值都配着说它自己那件事的文案（换序/错位会当场露馅）
    for choice, label in 后台:
        if choice == "stop":
            assert "停止" in label and "终止" in label
        elif choice == "deny":
            assert label == "拒绝"
        elif choice == "always":
            assert label.startswith("总是允许") and "不再问" in label
        else:
            assert label == "允许一次"
    assert [c for c, _ in 后台] == ["once", "always", "deny", "stop"]
    # 不可授权的调用不给"总是允许"，选项列表随之收缩（下标映射不能错位）
    bad = 绑定(can_stop=False) and tui.PermissionModal("bash", {"__error__": "invalid JSON"}, root=root)
    assert [c for c, _ in bad._options] == ["once", "deny"] and bad._pattern == ""


class _EvSetDuringWait:
    """入口那次 is_set() 返回 False（放行进等待循环），之后返回 True（模拟等待中被 Ctrl+C/kill 停掉）。"""
    def __init__(self):
        self.n = 0

    def is_set(self):
        self.n += 1
        return self.n > 1

    def set(self):
        pass


def test_弹窗竞态_迟到答复不串台_早退收窗_有人等的不被收():
    """审批弹窗的竞态，都源于"被放弃的窗还留在屏上 / 它的答复还算数"。

    ① 迟到答复：worker 被打断后放弃了那张窗，用户随后点它 → 结果必须【丢弃】，不能被下一次审批
       当成答复（否则可能把"总是允许"落到另一个工具头上、还跨会话落盘）。
    ② 早退收窗：worker 因中断早退时主动请 UI 收掉那张窗，别留成幽灵窗。
    ③ 收窗要认【那张窗自己的序号】，不是全局计数器 —— 这正是竞态所在：worker A 发完收窗请求就
       放开锁，worker B 立刻把计数器推到下一个再弹新窗，UI 这时才处理 A 的收窗请求。
    """
    import threading

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import tui

    App = type("App", (tui.MecodeApp,), {"screen": None})    # screen 是只读属性 → 子类降级成普通属性
    app = App.__new__(App)
    app._perm_seq = 0
    app._perm_screen_seq = None
    app._perm_result = "deny"
    app._perm_waiting = False
    app._perm_lock = threading.Lock()
    app._perm_event = threading.Event()
    posted = []
    app.post_message = posted.append
    app.agent = _fake_agent()

    # ② 等待中被停 → 早退，并发出收窗请求
    ctx = type("C", (), {"interrupt": _EvSetDuringWait(), "is_sub": True, "can_stop": True})()
    assert app._ask_permission("bash", {"command": "rm x"}, ctx) == "deny"
    assert app._perm_waiting is False                                  # finally 收干净
    assert [type(m).__name__ for m in posted] == ["AskPermission", "DismissPermission"]
    ask1, dismiss1 = posted

    # 屏上弹出窗 1
    captured = {}
    app.push_screen = lambda screen, cb: captured.setdefault("done", cb)
    app._on_ask_permission(ask1)
    assert app._perm_screen_seq == ask1.seq

    # ③ 竞态：worker B 抢到锁、把全局计数器推到下一个（此时 UI 还没处理 A 的收窗请求）
    app._perm_seq = ask1.seq + 1
    dismissed = []
    app.screen = type("S", (tui.PermissionModal,),
                      {"dismiss": lambda self, r: dismissed.append(r)})("bash", {"command": "rm x"})
    app._on_dismiss_permission(dismiss1)
    assert dismissed == [None]          # 认那张窗自己的序号 → 照样收得掉（认全局计数器就收不掉）

    # ① 那张被放弃的窗，用户此刻才点 → 必须丢弃（此时已进到下一次请求）
    app._perm_event.clear()
    captured["done"]("always")
    assert not app._perm_event.is_set()                                # 没唤醒下一个等待者
    assert app._perm_result == "deny"                                  # 也没把 always 写进去

    # 收窗只收序号对得上的：屏上换成别人的窗之后，旧收窗请求不该动它
    app._perm_screen_seq = ask1.seq + 1
    dismissed.clear()
    app._on_dismiss_permission(dismiss1)
    assert dismissed == []


def test_子agent权限随模式实时变_不用陈旧快照():
    # 切模式时 TUI 会整个换掉 agent.policy（tui.py: self.agent.policy = policy）——runner 存快照会陈旧，
    # 故它实时读 owner.policy。
    from mecode.config import agent_config
    from mecode.mode import apply_mode
    from mecode.subagent import SubagentRunner
    root = _root()
    owner = type("O", (), {})()
    owner.policy = PermissionPolicy.from_persisted({}, project_root=root)
    owner.ask_permission = None
    r = SubagentRunner(object(), agent_config, owner=owner)
    assert r._make(None).policy.decide(
        "write_file", {"path": root + "/x.py", "content": "a"}) == "ask"     # normal：写要问
    owner.policy = apply_mode(PermissionPolicy.from_persisted({}, project_root=root), "auto")
    assert r._make(None).policy.decide(
        "write_file", {"path": root + "/x.py", "content": "a"}) == "allow"   # auto：项目内自动


def test_子agent的模式段随模式注入且不是主agent那份():
    # 主 agent 的计划模式段是写给"产出计划文件、调 exit_plan"的角色看的，照搬给子 agent 有害
    # （它没有用户、也没有 exit_plan）→ 用 Mode.sub_prompt 这份专门的说法。
    from mecode.config import agent_config
    from mecode.mode import MODES
    from mecode.subagent import SUBAGENT_PROMPT, SubagentRunner
    owner = type("O", (), {})()
    owner.policy = None
    owner.ask_permission = None
    owner.subagent_reminder = MODES["plan"].sub_prompt
    sub = SubagentRunner(object(), agent_config, owner=owner)._make(None)
    sys_msg = sub.messages[0]
    assert sys_msg["role"] == "system"
    assert sys_msg["content"].startswith(SUBAGENT_PROMPT)
    assert "只读" in sys_msg["content"]                      # 注入了计划模式的子 agent 版说法
    assert "exit_plan" not in sys_msg["content"]             # 不是主 agent 那份
    assert MODES["normal"].sub_prompt == ""                  # normal/auto 无段 → 不注入


def test_无人可审批时的拒绝文案不谎称用户拒绝():
    # 无头（ask_permission=None）时是【系统】自动拒的；说成"用户拒绝"是假信息，模型会照此向上汇报。
    from mecode.agent import _DENY_NO_APPROVER, _DENY_TOOL
    root = _root()
    policy = PermissionPolicy.from_persisted({}, project_root=root)
    call = ToolCall(id="1", name="write_file", arguments={"path": root + "/x.py", "content": "a"})
    headless = Agent(_fake_provider(), ToolRegistry(), policy=policy, ask_permission=None)
    assert headless._gate(call) == (False, True)              # 拒了，且原因是"没人可审批"
    asked = Agent(_fake_provider(), ToolRegistry(), policy=policy,
                  ask_permission=lambda n, ar, ctx=None: "deny")
    assert asked._gate(call) == (False, False)                # 确实是用户点的拒绝
    assert "没有可审批的用户" in _DENY_NO_APPROVER and "用户拒绝" in _DENY_TOOL


def _grant_and_recheck(tool, args, root):
    """模拟"总是允许"的完整闭环：生成 spec → 记住 → 原样再调一次，返回第二次的判定。
    不变量①：必须变成 allow。否则就是"点了不再问、下次还问"。"""
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always(tool, args)
    return p.decide(tool, args)


def test_不变量1_总是允许后同一调用必须不再问():
    # 逐一覆盖此前实测会"点了还问"的形态：grep/glob 的目录本身（只记 <dir>/** 匹配不回自己）、
    # glob 的 .. pattern（通配符原样进 spec）、含 [ ] 的目录名（被 fnmatch 当字符类）、
    # 含 .. 与经软链的路径（spec 未解析）。
    root = _root()
    cases = [
        ("read_file", {"path": root + "/src/mecode/agent.py"}),
        ("read_file", {"path": root + "/../../secret.txt"}),      # 含 ..
        ("write_file", {"path": "C:/other/data/a.txt", "content": "x"}),
        ("write_file", {"path": "C:/tmp/data[1]/a.txt", "content": "x"}),   # 目录名含 [ ]
        ("grep", {"pattern": "TODO", "path": "C:/other/logs"}),   # 目录本身
        ("glob", {"pattern": "**/*.py", "path": "C:/other"}),
        ("glob", {"pattern": "**/*.py", "path": "src"}),
        ("bash", {"command": "rm -rf x"}),
        ("bash", {"command": "cat a.txt | wc -l"}),               # 纯管道：逐段记头
    ]
    for tool, args in cases:
        assert _grant_and_recheck(tool, args, root) == "allow", (tool, args, specs_for(tool, args, root))


def test_不变量2_算不出范围时不授权而非退化成全放行():
    root = _root()
    # bash 畸形调用：参数被流式截断时 provider 给的是 {'__raw__','__error__'}（没有 command 键）。
    # 此前退化成 '*' → 一次点击永久无条件放行【全部】bash，还废掉读命令根闸。
    for args in ({"__raw__": '{"command":"npm ru', "__error__": "invalid JSON"},
                 {"command": ""}, {"command": "   "}, {}):
        assert specs_for("bash", args, root) == []
    # 含 shell 元字符的 bash：decide 压根不看 allow 表 → 记了不生效，却比本次调用更宽 → 不可授权
    assert specs_for("bash", {"command": "echo hi > out.txt"}, root) == []
    # pattern 把落点带出根的 glob：spec 按 path 生成，表达不了"批准列根外"，弹窗也不披露 → 不可授权
    for pat in ("../../*", "{..,x}/**/*", "**/../*", "../*"):
        assert specs_for("glob", {"pattern": pat, "path": "."}, root) == [], pat
    assert specs_for("glob", {"pattern": "**/*.py", "path": "src"}, root) != []   # 无 .. → 可授权
    # 空 path / 非 dict args：此前记下一条空串死条目，还污染 permissions.json
    assert specs_for("read_file", {"path": ""}, root) == []
    assert specs_for("grep", "不是dict", root) == []
    # 不可授权 → pattern_for 返回空串，调用方据此不显示"总是允许"这一项
    assert pattern_for("bash", {"command": "echo hi > out.txt"}, root) == ""
    # 真的没授权出去：点了"总是允许"也不留规则
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.allow_always("bash", {"__error__": "invalid JSON"}) == []
    assert p.decide("bash", {"command": "rm -rf C:/x"}) == "ask"     # 没被那次点击放行


def test_不变量2_项目根与盘符根下的文件退回精确授权():
    # 在项目根下建个 notes.md 点一次"总是允许"，若记成 <根>/** 就等于把整个项目（含 .mecode/
    # permissions.json 与 .git/）的写权限永久发出去，normal 一键变 auto。根级同理（整盘）。
    root = _root()
    assert specs_for("write_file", {"path": "notes.md"}, root) == [root + "/notes.md"]
    assert specs_for("write_file", {"path": root + "/notes.md"}, root) == [root + "/notes.md"]
    assert specs_for("read_file", {"path": "C:/x.txt"}, root) == ["C:/x.txt"]       # 盘符根
    # 子目录里的文件仍按"所在文件夹及其下"授权（正常粒度不变）
    assert specs_for("write_file", {"path": root + "/src/a.py"}, root) == [root + "/src/**"]
    # 授权范围确实没溢出到项目根
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always("write_file", {"path": "notes.md"})
    assert p.decide("write_file", {"path": root + "/.mecode/permissions.json"}) == "ask"
    assert p.decide("write_file", {"path": root + "/其他.md"}) == "ask"


def test_不变量3_路径里的通配符字面量被转义():
    # data[1] 是合法目录名；不转义会被 fnmatch 当字符类 → 既匹配不回自己，又误授权 data1
    root = _root()
    spec = specs_for("write_file", {"path": "C:/tmp/data[1]/a.txt"}, root)[0]
    assert spec == "C:/tmp/data[[]1]/**"
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always("write_file", {"path": "C:/tmp/data[1]/a.txt"})
    assert p.decide("write_file", {"path": "C:/tmp/data[1]/b.txt"}) == "allow"   # 批过的目录
    assert p.decide("write_file", {"path": "C:/tmp/data1/z.txt"}) == "ask"       # 从没批过


def test_总是允许_记住的spec与判定同源且不退化成全放行():
    # 三个洞：① spec 从原始串算 → 含 .. / 经软链的路径会生成永远匹配不上的 spec（点了"不再问"却每次还问）
    #        ② 路径类工具算不出文件夹时掉进 return "*" → 畸形调用换来无条件放行整个工具
    #        ③ 记法 `/*` 让用户以为只批了一层（fnmatch 的 * 本就跨 /）→ 改用 `/**`，零行为变化、如实说
    root = _root()
    # ① 同源：spec 用落点算，记住后同一路径不再问
    p = PermissionPolicy.from_persisted({}, project_root=root)
    outside = root + "/../../secret.txt"
    assert p.decide("read_file", {"path": outside}) == "ask"
    p.allow_always("read_file", {"path": outside})
    assert p.decide("read_file", {"path": outside}) == "allow"       # 此前因 spec 带 .. 而永远匹配不上
    assert ".." not in spec_for("read_file", {"path": outside})
    # ② 畸形/退化路径不再换来 '*'
    assert spec_for("read_file", {"path": ""}) != "*"
    assert spec_for("grep", {"path": "/"}) != "*"
    # ③ 记法用 **（与根规则 <根>/** 统一）
    assert spec_for("write_file", {"path": "C:/a/b/x.txt"}) == "C:/a/b/**"
    assert pattern_for("write_file", {"path": "C:/a/b/x.txt"}) == "write_file(C:/a/b/**)"
    # 无路径维度的工具仍是 '*'（粒度就是工具本身）
    assert spec_for("subagent", {"prompt": "x"}) == "*"


def test_allow_always_即时生效():
    p = PermissionPolicy()
    assert p.decide("bash", {"command": "rm x"}) == "ask"
    grants = p.allow_always("bash", {"command": "rm x"})
    # 第二个词不是选项时连它一起记：只记首词的话，批准 `git diff` 会连 `git push --force` 一起放行
    assert grants == [("bash", "rm x:*")]      # 返回 (工具, spec) 对——bash 可连带落点授权，见 grants_for
    assert p.decide("bash", {"command": "rm x -f"}) == "allow"          # 同前缀的变体：放行
    assert p.decide("bash", {"command": "rm y"}) == "ask"               # 换个目标：仍问（粒度=前两个词）
    # 第二个词是选项时它不是子命令 → 仍只记首词（那类命令本就是首词粒度）
    p2 = PermissionPolicy()
    # 前导选项要跳过再取第一个非选项词：只看"第二词是不是选项"的话，`python -m pytest` 会退回
    # python:*（=任意代码执行）、`git --no-pager diff` 退回 git:*（含 push --force）。
    # 首词后紧跟的是选项 → 断在哪判不了 → 整条精确（不发 :* 前缀），见 _cmd_spec
    assert p2.allow_always("bash", {"command": "rm -rf x"}) == [("bash", "rm -rf x")]


def test_纯管道_每段都放行则免问_否则问():
    p = PermissionPolicy()
    assert p.decide("bash", {"command": "cat x | grep foo | wc -l"}) == "allow"   # 只读命令互接
    assert p.decide("bash", {"command": "cat x | cut -f1 | tr a b"}) == "allow"   # cut/tr 新加进名单
    assert p.decide("bash", {"command": "cat x | sh"}) == "ask"                   # 末段 sh 未放行
    assert p.decide("bash", {"command": "ls | rm -rf x"}) == "ask"                # 末段 rm 未放行
    assert p.decide("bash", {"command": "ls || rm -rf x"}) == "ask"               # 逻辑或不是管道，不放宽
    assert p.decide("bash", {"command": "cat x | grep y > out"}) == "ask"         # 管道+重定向 → 有别的元字符
    assert p.decide("bash", {"command": "git log --oneline | cat"}) == "allow"    # 两词头 git log:* 也对得上


def test_管道版总是允许_每段头都记进allow():
    p = PermissionPolicy()
    assert p.decide("bash", {"command": "cat x | jq ."}) == "ask"                 # jq 未放行 → 问
    grants = p.allow_always("bash", {"command": "cat x | jq ."})
    # 管道逐段也走 _cmd_head（两词粒度）：此前逐段只记首词，`cat x | jq .` 记成 cat:* →
    # 一次点击把 `cat ~/.ssh/id_rsa` 也放行了（那时 cat:* 进 _user_allow 就整体解除了读命令根闸）。
    # 这条断言曾经把那个洞钉死（写的就是被铲掉的宽形态），是"测试看着有牙、锁错东西"的活标本。
    assert grants == [("bash", "cat x:*"), ("bash", "jq .:*")]                        # 两段头都记
    assert pattern_for("bash", {"command": "cat x | jq ."}) == "bash(cat x:*, jq .:*)"  # 显示多条
    assert p.decide("bash", {"command": "cat x | jq . -r"}) == "allow"            # 同前缀的同类管道免问
    # 换掉【不在默认名单里】的那一段就仍要问（cat 本就默认放行，换它的文件看不出粒度）
    assert p.decide("bash", {"command": "cat x | jq 别的表达式"}) == "ask"


def test_bash读命令路径参数受项目根闸约束():
    # 洞：read_file 被限制在项目根内，但 bash cat/head/tail 默认放行、只看命令头不看路径 →
    # `cat ~/.ssh/id_rsa` 能绕过根闸读根外文件。命中放行后补一道路径根闸堵上。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    # 根内：照常放行，不打扰
    assert p.decide("bash", {"command": "cat src/mecode/agent.py"}) == "allow"
    assert p.decide("bash", {"command": "head -n 20 README.md"}) == "allow"          # -n 20 是 flag/值,文件根内
    assert p.decide("bash", {"command": "cat pyproject.toml setup.cfg"}) == "allow"  # 多文件都根内
    assert p.decide("bash", {"command": "cat ./x.txt"}) == "allow"
    # 根外：被堵 → 问
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "ask"
    assert p.decide("bash", {"command": "cat ~/.ssh/id_rsa"}) == "ask"               # ~ 展开到 home（根外）
    assert p.decide("bash", {"command": "cat ../secret.txt"}) == "ask"              # .. 逃出根
    assert p.decide("bash", {"command": "tail -f /var/log/syslog"}) == "ask"
    assert p.decide("bash", {"command": "cat $HOME/.ssh/id_rsa"}) == "ask"          # 含变量 → 保守问


def test_bash_grep路径闸_pattern不误判为路径():
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "grep -rn TODO src"}) == "allow"            # pattern TODO 不当路径,src 根内
    assert p.decide("bash", {"command": "grep 'a.*b' src/x.py"}) == "allow"         # 正则 pattern 含 * 也不误拦（被跳过）
    assert p.decide("bash", {"command": "grep root /etc/passwd"}) == "ask"          # 文件根外 → 问
    assert p.decide("bash", {"command": "grep --regexp=root /etc/passwd"}) == "ask" # pattern 藏 flag 里,位置参数是文件 → 不漏判


def test_bash读命令管道每段都受根闸():
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat src/a.py | grep foo | wc -l"}) == "allow"   # 全根内
    assert p.decide("bash", {"command": "cat /etc/passwd | grep root"}) == "ask"         # 首段读根外 → 问


def test_bash读命令_无项目根时不额外限制():
    # 无项目上下文（root=None）→ 根闸不激活，保持原放行（和 read/grep/glob 的根闸同样只在有项目根时生效）
    p = PermissionPolicy()
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "allow"
    assert p.decide("bash", {"command": "cat x | grep y | wc -l"}) == "allow"


def test_bash_cut也受根闸():
    # 红队发现：cut 默认放行且能 `cut -c1- FILE` dump 整个文件，之前漏在 _READ_CMDS 外 → 补进来
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cut -f1 src/mecode/agent.py"}) == "allow"      # 根内文件
    assert p.decide("bash", {"command": "cut -d: -f1 README.md"}) == "allow"            # -d: 是分隔符不是路径
    assert p.decide("bash", {"command": "cut -c1- ../secret"}) == "ask"                 # 根外 → 拦
    assert p.decide("bash", {"command": "cut -f1 ~/.gitconfig"}) == "ask"
    assert p.decide("bash", {"command": "cut -c1- ../a | tr a b"}) == "ask"             # 管道段里也拦


def test_bash_grep贴附flag形式不绕过根闸():
    # 红队发现：贴附形式让"跳过 pattern"误判/让 -f 文件路径藏进 flag token 逃过核查
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    # 贴附 pattern（-e<pat>、捆绑 -ie<pat>）：真文件是位置参数、必须被核查
    assert p.decide("bash", {"command": "grep -e. ../secret"}) == "ask"                 # -e. 贴附模式,../secret 才是文件
    assert p.decide("bash", {"command": "grep -iefoo ../secret"}) == "ask"              # 捆绑贴附
    # 贴附 -f/--file 模式文件：路径嵌在 flag token 里,要抽出来核查
    assert p.decide("bash", {"command": "grep -f../secret README.md"}) == "ask"         # -f<path>
    assert p.decide("bash", {"command": "grep --file=../secret README.md"}) == "ask"    # --file=<path>
    assert p.decide("bash", {"command": "grep -rf../secret README.md"}) == "ask"        # 捆绑 -rf<path>
    # 对照：根内正常用法照放
    assert p.decide("bash", {"command": "grep -rn TODO src"}) == "allow"
    assert p.decide("bash", {"command": "grep -f patterns.txt src/x"}) == "allow"       # -f 分开写、均根内


def test_bash_读文件长选项贴附值受根闸():
    # 三轮红队发现：wc --files0-from=FILE 会读 FILE；贴附 = 形式的值藏在 flag token 里绕过了根闸。
    # 泛化成"任意 --x=值 都核查"，对将来新增读文件长选项也自动生效。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "wc --files0-from=../../.gitignore"}) == "ask"  # 贴附根外文件
    assert p.decide("bash", {"command": "wc -l README.md"}) == "allow"                  # 普通根内用法不受影响
    assert p.decide("bash", {"command": "grep --color=always TODO src/x"}) == "allow"   # 非文件长选项值不误伤
    # --files0-from 是"间接读一批文件"：值可为 stdin(-)/根内清单,清单内容能命名根外文件 → 一律问
    assert p.decide("bash", {"command": "wc --files0-from=list.txt"}) == "ask"          # 根内清单也拦（内容不可控）
    assert p.decide("bash", {"command": "wc --files0-from=-"}) == "ask"                 # 值=stdin
    assert p.decide("bash", {"command": "echo x | wc --files0-from=-"}) == "ask"        # 管道喂清单
    assert p.decide("bash", {"command": "wc --files0-from list.txt"}) == "ask"          # 分开写也拦


def test_bash_glob带双点不能爬出根():
    # 红队发现：_glob_base 只看通配前缀,`*/../../../x` 的 .. 在通配之后、爬出了根却没被发现
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat */../../../.gitconfig"}) == "ask"          # 通配后接 .. 爬出根
    assert p.decide("bash", {"command": "cat src/*/../../../x"}) == "ask"
    assert p.decide("bash", {"command": "cat src/*.py"}) == "allow"                     # 正常根内 glob 不受影响


def test_bash_花括号展开不能藏双点爬出根():
    # 二轮红队发现：`cat {.,..}/x` 被 bash 展开成 ./x 和 ../x，.. 藏在花括号里躲过"裸 .. 段"检查
    # 且 abspath 把 {.,..} 当字面目录名 → 误判根内。含 {}（无法静态定落点）一律保守拦。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat {.,..}/server_config.md"}) == "ask"
    assert p.decide("bash", {"command": "cut -c1-30 {.,..}/x"}) == "ask"
    assert p.decide("bash", {"command": "cat {.,..}/x | head -1"}) == "ask"             # 管道段内也拦
    assert p.decide("bash", {"command": "grep -f{.,..}/pat README.md"}) == "ask"        # 藏进 -f 贴附路径也拦


def test_bash_命令名shell污染不能短路根闸():
    # 五轮红队：cat$'' 被 shlex 切成 'cat$'（不在 _READ_CMDS）→ 根闸短路放行，但 bash 折叠成 cat 读文件。
    # 用与 allow 同源的 _spec_match 重判原始命令：匹配读命令 spec 就保守拦。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat$'' ../secret"}) == "ask"        # ANSI-C 空引用粘命令名
    assert p.decide("bash", {"command": 'cat$"" ../secret'}) == "ask"        # locale 空引用
    assert p.decide("bash", {"command": "cat$x ../secret"}) == "ask"         # 变量粘命令名
    assert p.decide("bash", {"command": "head$'' ../secret"}) == "ask"
    assert p.decide("bash", {"command": "cat$'' ../a | grep y"}) == "ask"    # 管道段内
    # 对照：非读命令带 $ 不受影响（照旧放行）；干净读命令正常
    assert p.decide("bash", {"command": "echo $HOME"}) == "allow"
    assert p.decide("bash", {"command": "cat src/mecode/agent.py"}) == "allow"


def test_bash_git_diff读根外文件受拦():
    # 六/七轮红队：git diff A B（显式 --no-index 或【隐式】给两路径含根外）会 dump 任意文件内容，
    # git diff:* 默认放行、又非读命令 → 漏。核查 git diff 的像路径操作数都在根内。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    # 显式 --no-index
    assert p.decide("bash", {"command": "git diff --no-index /dev/null ../secret"}) == "ask"
    assert p.decide("bash", {"command": "git diff --no-index a ~/.gitconfig"}) == "ask"
    # 隐式 no-index（不带 --no-index，给两路径、一个在根外 → git 自动进 no-index）
    assert p.decide("bash", {"command": "git diff /dev/null ../secret"}) == "ask"
    assert p.decide("bash", {"command": "git diff ~/.gitconfig /dev/null"}) == "ask"
    assert p.decide("bash", {"command": "git diff ../a README.md"}) == "ask"
    assert p.decide("bash", {"command": "git diff /dev/null ../a | cat"}) == "ask"              # 管道段内
    # 对照：普通 git diff / ref / 根内路径照放
    assert p.decide("bash", {"command": "git diff"}) == "allow"
    assert p.decide("bash", {"command": "git diff HEAD~3"}) == "allow"                          # ref 不是路径
    assert p.decide("bash", {"command": "git diff HEAD@{2}"}) == "allow"                        # reflog ref 含 {} 也不误伤
    assert p.decide("bash", {"command": "git diff @{-1}"}) == "allow"                           # reflog ref
    assert p.decide("bash", {"command": "git diff origin/main"}) == "allow"                     # 带 / 的 ref 归一后仍根内
    # shell 展开出根外路径（$VAR / 花括号）也要拦——path-like 预筛须与 _path_escapes_root 对称
    assert p.decide("bash", {"command": "git diff $HOME ."}) == "ask"                           # $VAR 展开到根外
    assert p.decide("bash", {"command": "git diff --no-index $SECRET ."}) == "ask"
    assert p.decide("bash", {"command": "git diff {..,.}"}) == "ask"                            # 花括号走私 ..
    assert p.decide("bash", {"command": "git diff ${HOME}/x ."}) == "ask"
    assert p.decide("bash", {"command": "git diff src/a.py"}) == "allow"
    assert p.decide("bash", {"command": "git diff --no-index src/a.py src/b.py"}) == "allow"    # 两文件均根内


def test_bash命令头前缀不能豁免落点闸():
    """闸管的是"哪几个文件"，而 bash 的 allow spec 是命令头前缀——粒度对不上。此前出口是"命中任一
    bash allow spec 即整道闸让路"，尾巴上追加的路径就搭便车：批过 `cat README.md` 换来
    `cat README.md ../../secret`；批过只读的 `git diff` 换来 `git diff --output <任意路径>`。"""
    root = _root()
    p = PermissionPolicy.from_persisted({"bash": {"allow": ["cat:*"]}}, project_root=root)
    assert p.decide("bash", {"command": "cat README.md"}) == "allow"         # 根内照常
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "ask"         # 手写的 cat:* 表达不了根外路径
    assert p.decide("bash", {"command": "cat ~/.ssh/id_rsa"}) == "ask"
    assert p.decide("bash", {"command": "cat /a | grep x"}) == "ask"         # 管道段同理

    p2 = PermissionPolicy.from_persisted(None, project_root=root)
    p2.allow_always("bash", {"command": "cat README.md"})                    # 批准一条根内读
    assert p2.decide("bash", {"command": "cat README.md"}) == "allow"
    assert p2.decide("bash", {"command": "cat README.md ../../secret"}) == "ask"   # 追加的根外路径不搭车

    p3 = PermissionPolicy.from_persisted(None, project_root=root)
    p3.allow_always("bash", {"command": "git diff"})                         # 批准一条只读 git
    assert p3.decide("bash", {"command": "git diff"}) == "allow"
    for c in ("git diff --output C:/x/pwned.txt", "git diff --output=C:/x/pwned.txt",
              "git log --output C:/x/pwned.txt", "git diff --output patch.txt"):
        assert p3.decide("bash", {"command": c}) == "ask", c                 # --output 是写 → 过 write_file 那道闸


def test_bash读命令_按落点授权后同一文件换命令换工具都免问():
    """闸按落点判，授权就按落点记（allow_always 连带写下 read_file 那条路径）——否则点了"总是允许"
    下次闸仍在落点上拦住 = 点了还问。同源之后：同一文件换命令/换工具都免问，换个文件仍要批。"""
    root = _root()
    p = PermissionPolicy.from_persisted(None, project_root=root)
    assert p.decide("bash", {"command": "cat /etc/hosts"}) == "ask"
    assert p.allow_always("bash", {"command": "cat /etc/hosts"}) == [
        ("bash", "cat /etc/hosts:*"), ("read_file", _land("/etc/hosts"))]
    assert p.decide("bash", {"command": "cat /etc/hosts"}) == "allow"
    assert p.decide("bash", {"command": "head /etc/hosts"}) == "allow"     # 换个读命令、同一文件
    assert p.decide("read_file", {"path": "/etc/hosts"}) == "allow"        # 换个工具、同一文件
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "ask"       # 换个文件：仍要批
    # 根内的读【不】连带记：默认本就放行，记了只是往 permissions.json 堆无用条目，
    # 还等于凭一次 bash 批准发出一条 read_file 的路径规则（比用户批的那次调用更宽）
    assert p.allow_always("bash", {"command": "cat README.md"}) == [("bash", "cat README.md:*")]

    # 反向也成立：read_file 批过的目录（"总是允许"给的是文件夹级），cat/grep 一并生效
    p2 = PermissionPolicy.from_persisted(None, project_root=root)
    p2.allow_always("read_file", {"path": "C:/日志/a.log"})
    assert p2.decide("bash", {"command": "cat C:/日志/b.log"}) == "allow"
    assert p2.decide("bash", {"command": "cat C:/别处/b.log"}) == "ask"


def test_read_file的deny规则同样管住bash读命令():
    """落点闸直接问 read_file → deny 侧一并继承。此前闸只判"在不在根内"，deny 规则对 cat 不生效
    （read_file 读不到的文件，cat 读得到）。"""
    root = _root()
    p = PermissionPolicy.from_persisted({"read_file": {"deny": ["**/.env"]}}, project_root=root)
    assert p.decide("read_file", {"path": ".env"}) == "deny"
    assert p.decide("bash", {"command": "cat .env"}) == "ask"      # 落点被 deny → 闸不放行 → 落 default


def test_bash读命令_运行时always后即时对根外放行():
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat /etc/hosts"}) == "ask"          # 默认根闸拦
    p.allow_always("bash", {"command": "cat /etc/hosts"})                    # 用户选"总是允许" → 记 cat:*
    # 授权粒度是"这条命令的前两个词"，不是整个 cat：换个文件仍要问（不给超过批准范围的权限）
    assert p.decide("bash", {"command": "cat /etc/hosts -n"}) == "allow"     # 同一文件的变体：放行
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "ask"         # 换个根外文件：仍问


def test_from_persisted_容旧格式坏数据不崩():
    # 早期扁平格式 {"allow":[...]}：值是 list 不是 dict → 跳过、不崩（旧规则被忽略，需重新批准）
    p = PermissionPolicy.from_persisted({"allow": ["bash(rm:*)"], "always_allow": ["bash"]},
                                        project_root="C:/proj")
    assert p.decide("bash", {"command": "ls"}) == "allow"   # 默认仍在
    assert p.decide("bash", {"command": "rm x"}) == "ask"   # 旧规则没生效，但没崩
    # 新格式里混入坏 specs（非 list）也跳过
    p2 = PermissionPolicy.from_persisted({"bash": {"allow": ["rm:*"], "deny": "notalist"}})
    assert p2.decide("bash", {"command": "rm x"}) == "allow"


def test_from_persisted_合默认与用户规则():
    p = PermissionPolicy.from_persisted({"bash": {"allow": ["rm:*"], "deny": ["sudo:*"]}})
    assert p.decide("bash", {"command": "ls"}) == "allow"               # 默认仍在
    assert p.decide("bash", {"command": "rm x"}) == "allow"             # 用户加的
    assert p.decide("bash", {"command": "sudo x"}) == "deny"


# ---------- agent 闸集成 ----------

class _OneToolProvider:
    def __init__(self, name, args):
        self.name, self.args, self.calls = name, args, 0

    def stream(self, messages, tools=None, should_stop=None):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(id="c1", name=self.name, arguments=self.args)
            yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("好了")
            yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
            yield Done(reason="stop")


def _reg(name, ran):
    reg = ToolRegistry()
    reg.register(Tool(name=name, description="x",
                      parameters={"type": "object", "properties": {}},
                      handler=lambda a: ran.append(True) or "执行结果"))
    return reg


def _run(name, args, choice):
    ran, asked = [], []
    a = Agent(_OneToolProvider(name, args), _reg(name, ran), system_prompt="你是助手",
              policy=PermissionPolicy(), ask_permission=lambda n, ar, ctx=None: asked.append(n) or choice)
    list(a.run_turn("调用"))
    return a, ran, asked


def test_deny_不执行喂回拒绝():
    a, ran, asked = _run("bash", {"command": "rm x"}, "deny")
    assert asked == ["bash"] and ran == []
    assert "拒绝" in next(m for m in a.messages if m.get("role") == "tool")["content"]


def test_once_执行():
    a, ran, asked = _run("bash", {"command": "rm x"}, "once")
    assert asked == ["bash"] and ran == [True]


def test_allow类命令_不问直跑():
    a, ran, asked = _run("bash", {"command": "ls"}, "deny")
    assert asked == [] and ran == [True]                               # ls 默认放行，不问


def test_always_经store落项目级permissions(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    ran = []
    a = Agent(_OneToolProvider("bash", {"command": "rm x"}), _reg("bash", ran),
              system_prompt="你是助手", store=st,
              policy=PermissionPolicy(), ask_permission=lambda n, ar, ctx=None: "always")
    list(a.run_turn("调用"))
    assert ran == [True]
    assert st.load_permissions() == {"bash": {"allow": ["rm x:*"]}}    # 落盘成 工具→spec（两词粒度）


def test_无policy_全放():
    ran = []
    a = Agent(_OneToolProvider("bash", {"command": "rm x"}), _reg("bash", ran),
              system_prompt="你是助手")
    list(a.run_turn("调用"))
    assert ran == [True]


def test_run_workflow_默认要问():
    # workflow = 一批子 agent + 数分钟 + 真金 token，分量重 → 不进默认放行（区别于单个 subagent）
    from mecode.permission import PermissionPolicy
    p = PermissionPolicy()
    assert p.decide("run_workflow", {"stages": [{"id": "a", "prompt": "x"}]}) == "ask"
    assert p.decide("subagent", {"prompt": "x"}) == "allow"      # 对照：单个 subagent 仍放行


def test_子agent里的总是允许全局生效且落盘():
    """用户点"总是允许"批的是【这个操作】本身，与"是哪个 agent 在问"无关 → 就该全局生效。

    曾经改成"子 agent 拿 policy 副本"以防授权外溢到父，但那让同一个授权在每个 workflow 阶段、
    每个兄弟子 agent 里都要重问一遍，且与弹窗承诺的"记住，不再问"相悖。现在共用同一个 policy，
    配套让它【落盘】——否则切一次模式（policy 从 permissions.json 重建）这条授权就凭空消失。"""
    from mecode.config import agent_config
    from mecode.subagent import SubagentRunner
    落盘 = []
    owner = type("O", (), {})()
    owner.policy = PermissionPolicy()
    owner.ask_permission = None
    owner._persist_perm = lambda tool, decision, spec: 落盘.append((tool, decision, spec))

    sub = SubagentRunner(object(), agent_config, owner=owner)._make(None)
    assert sub.policy is owner.policy                    # 共用同一对象，不是副本
    sub.policy.allow_always("bash", {"command": "rm -rf x"})
    assert sub.policy.decide("bash", {"command": "rm -rf x"}) == "allow"     # 子 agent 内生效
    assert owner.policy.decide("bash", {"command": "rm -rf x"}) == "allow"   # 父 agent 也生效

    # 落盘出口：子 agent 的 store 是 None，借的是主 agent 的（否则切模式后这条授权就没了）
    sub._persist_perm("bash", "allow", "rm -rf x:*")
    assert 落盘 == [("bash", "allow", "rm -rf x:*")]


def test_落盘出口默认走自己的store():
    # 主 agent 有 store → 默认就是 store.add_permission；无 store（旧用法/测试）→ 空操作，不炸
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(root=Path(d) / "s", cwd=d)
        a = Agent(_fake_provider(), ToolRegistry(), store=st)
        a._persist_perm("bash", ALLOW_, "ls:*")
        assert st.load_permissions() == {"bash": {"allow": ["ls:*"]}}   # 真落到 permissions.json
    Agent(_fake_provider(), ToolRegistry())._persist_perm("bash", "allow", "x")   # 无 store 不抛


def test_命令粒度_两词与首词_以及前导空白():
    root = _root()
    # 第二个词不是选项 → 连它一起记（否则批准只读的 git diff 会连 git push --force 一起放行）
    assert specs_for("bash", {"command": "git diff HEAD~1"}, root) == ["git diff:*"]
    assert specs_for("bash", {"command": "npm run test"}, root) == ["npm run:*"]
    assert specs_for("bash", {"command": "python 脚本.py"}, root) == ["python 脚本.py:*"]
    # 第二个词是选项 → 后面那个词是"选项的值"还是"子命令"判不了 → 整条精确，不发 :* 前缀
    assert specs_for("bash", {"command": "rm -rf x"}, root) == ["rm -rf x"]
    assert specs_for("bash", {"command": "python -m pytest -q"}, root) == ["python -m pytest -q"]
    assert specs_for("bash", {"command": "git --no-pager diff"}, root) == ["git --no-pager diff"]
    assert specs_for("bash", {"command": "ls -la"}, root) == ["ls -la"]
    # 精确 spec 里的 * ? 要转义，否则落 fnmatch 分支当模式用（`rm -rf *.tmp` 会放行 `rm -rf 别的.tmp`）
    assert specs_for("bash", {"command": "rm -rf *.tmp"}, root) == ["rm -rf [*].tmp"]
    p0 = PermissionPolicy.from_persisted({}, project_root=root)
    p0.allow_always("bash", {"command": "rm -rf *.tmp"})
    assert p0.decide("bash", {"command": "rm -rf *.tmp"}) == "allow"     # 自匹配（不变量①）
    assert p0.decide("bash", {"command": "rm -rf 别的.tmp"}) == "ask"    # 不当模式用
    # 前导空白对 shell 无意义，但会让判定与记下的 spec 对不上（点了不再问、下次还问）→ 归一化
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always("bash", {"command": "  rm -rf x"})
    assert p.decide("bash", {"command": "  rm -rf x"}) == "allow"
    assert p.decide("bash", {"command": "rm -rf x"}) == "allow"


def test_UNC共享根与非字符串path():
    root = _root()
    # UNC：\server 是整台机、\server\share 是整个共享 → 一次点击不该发出去，退回精确到该文件
    assert specs_for("read_file", {"path": "//server/share/x.txt"}, root) == ["//server/share/x.txt"]
    assert specs_for("read_file", {"path": "//server/x.txt"}, root) == ["//server/x.txt"]
    assert specs_for("read_file", {"path": "//server/share/sub/x.txt"}, root) == ["//server/share/sub/**"]
    # path 不是字符串（模型偶尔给数字/数组）：不能抛异常打断整轮、留下无结果的孤儿 tool_call
    p = PermissionPolicy.from_persisted({}, project_root=root)
    for bad in ({"path": 123}, {"path": ["a"]}, {"path": None}, {"path": {"k": 1}}):
        assert p.decide("read_file", bad) == "ask"
        assert specs_for("read_file", bad, root) == []


# ---------- 两道机制护栏（_guard）：spec 生成的不变量由它统一强制 ----------

def test_护栏1_spec匹配不回自己时退回精确形态():
    """护栏①是【安全网】：生成分支写错时兜住。故要直接喂给它写坏的 spec 来测——
    只测"生成就对"的路径，把护栏整个关掉测试也全绿（实测：24 个变异体里它就是这么存活的）。"""
    from mecode.permission import _guard
    root = _root()
    args = {"path": root + "/src/a.py", "content": "x"}
    # 故意给一条匹配不回本次调用的 spec（此前 _esc_glob 把精确 spec 转义成 x[[]1].txt 就是这种）
    assert _guard(["完全不相干/**"], "write_file", args, root) == [root + "/src/a.py"]
    # bash 也受护栏保护：退回整条命令原样（精确相等分支，最窄且必然自匹配）
    assert _guard(["不相干:*"], "bash", {"command": "rm -rf x"}, root) == ["rm -rf x"]
    # 本来就自匹配的不动它
    assert _guard([root + "/src/**"], "write_file", args, root) == [root + "/src/**"]


def test_护栏1的自检对管道要逐段比_不能拿整条命令比():
    """decide 对管道是【逐段】比对 allow 的，护栏①的自检必须同源。此前它拿整条命令去比，靠"前缀
    spec 恰好也是整条命令的前缀"蒙混过关；spec 一变成精确形态（`python -m pytest -q`）就对不上，
    护栏把好好的逐段 spec 收成一条整管道的精确 spec —— 那条 spec 在逐段比对里永远用不上 =
    点了"总是允许"下次还问。"""
    root = _root()
    for cmd, specs in (("python -m pytest -q | tail", ["python -m pytest -q", "tail"]),
                       ("cat x | jq .", ["cat x:*", "jq .:*"]),
                       ("ls -la | head -5", ["ls -la", "head -5"])):
        assert specs_for("bash", {"command": cmd}, root) == specs, cmd
        p = PermissionPolicy.from_persisted({}, project_root=root)
        p.allow_always("bash", {"command": cmd})
        assert p.decide("bash", {"command": cmd}) == "allow", cmd      # 不变量①
    # 自检真的破了时：管道没有可退的精确形态（整条 spec 在逐段比对里用不上）→ 不授权，别记死规则
    from mecode.permission import _guard
    assert _guard(["不相干:*"], "bash", {"command": "cat x | jq ."}, root) == []


def test_护栏2_spec圈住项目根时收窄到精确():
    """护栏②同样是安全网。这里【顺带钉住一个真实场景】：_root_like 至今只认"folder 等于项目根"、
    不认"folder 是项目根的祖先"，故对项目外一层的文件，_gen_specs 生成的仍是 <父目录>/** —— 全靠
    护栏当场改写。这条测试同时证明护栏不是装饰，而是正在实际生效。"""
    from mecode.permission import _gen_specs, _guard
    root = _root()
    parent = root.rsplit("/", 1)[0]
    args = {"path": "../report.md", "content": "x"}
    assert _gen_specs("write_file", args, root) == [parent + "/**"]        # 生成的仍是错的
    assert specs_for("write_file", args, root) == [parent + "/report.md"]  # 护栏改写成精确
    # 授权范围真的没溢出到项目里
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always("write_file", args)
    assert p.decide("write_file", {"path": parent + "/report.md"}) == "allow"   # 批过的本身
    for inside in ("/.mecode/permissions.json", "/.git/config", "/src/mecode/permission.py"):
        assert p.decide("write_file", {"path": root + inside}) == "ask", inside
    # 直接喂一条圈住根的 spec 也会被收窄
    assert _guard([parent + "/**"], "write_file", args, root) == [parent + "/report.md"]
    # 被批对象本来就在根内 → 圈到根内某层是正常的，不动
    inside_args = {"path": root + "/src/a.py", "content": "x"}
    assert _guard([root + "/src/**"], "write_file", inside_args, root) == [root + "/src/**"]


def test_无人可审批的拒绝文案走到工具结果里():
    # 只断言常量内容是同义反复（把 agent.py 里那处三元换成永远用 _DENY_TOOL，测试照样绿）→
    # 要从【工具结果】这个出口断言，才锁得住投递路径。
    from mecode.agent import _DENY_NO_APPROVER
    root = _root()
    policy = PermissionPolicy.from_persisted({}, project_root=root)
    call = ToolCall(id="c1", name="write_file", arguments={"path": root + "/x.py", "content": "a"})

    a = Agent(_OneToolProvider("write_file", call.arguments), _reg("write_file", []),
              policy=policy, ask_permission=None)          # 无头：没有可审批的用户
    list(a.run_turn("写文件"))
    msg = next(m for m in a.messages if m.get("role") == "tool")["content"]
    assert msg == _DENY_NO_APPROVER.format(name="write_file")
    assert "没有可审批的用户" in msg and "用户拒绝" not in msg   # 不谎称是用户拒的


def test_轮末只清没人等的孤儿弹窗():
    # 打断会把弹窗留在屏上（worker 已返回、没人再等）→ 收尾该清，否则下一轮会压着旧窗弹新窗。
    # 但后台子 agent / workflow 阶段跑在自己线程里，与主 agent 这一轮独立——主 agent 收尾时
    # 它们可能刚弹窗【正等你答】，无条件 dismiss 会让用户没答就被判"用户拒绝"并上报给主 agent。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import tui
    App = type("App", (tui.MecodeApp,), {"screen": None})    # screen 是只读属性 → 子类降级成普通属性
    app = App.__new__(App)
    dismissed = []
    app.screen = type("S", (tui.PermissionModal,),
                      {"dismiss": lambda self, r: dismissed.append(r)})("bash", {"command": "rm x"})

    app._perm_waiting, app._ask_waiting = True, False        # 有 worker 正等这张审批窗
    assert app._clear_orphan_modal() is False and dismissed == []
    app._perm_waiting, app._ask_waiting = False, True        # ask_user 那张有人等
    assert app._clear_orphan_modal() is False and dismissed == []
    app._perm_waiting, app._ask_waiting = False, False       # 没人等 → 是孤儿，清掉
    assert app._clear_orphan_modal() is True and dismissed == [None]
    # 屏上不是弹窗时什么都不做
    app.screen = None
    assert app._clear_orphan_modal() is False


def test_管道分支与单条命令的粒度同源():
    """孪生路径漏改的典型：两词粒度当初只改了单条命令分支，纯管道分支还在逐段只记首词 →
    `git log … | jq .` 记成 git:*（放行 push --force）、`cat x | jq .` 记成 cat:*（连读命令根闸
    都被整体解除）。同一件事换成管道形态就漏。"""
    root = _root()
    for cmd, probes in (
        ("git log --oneline | jq .", ["git push --force", "git reset --hard origin/main"]),
        ("cat pyproject.toml | jq .", ["cat C:/Users/31784/.ssh/id_rsa"]),
        ("npm run build | tail -5", ["npm publish", "tail -100 C:/Users/31784/.ssh/id_rsa"]),
        ("cat notes.txt | python parse.py", ["python C:/evil.py", 'python -c "import os"']),
    ):
        p = PermissionPolicy.from_persisted({}, project_root=root)
        p.allow_always("bash", {"command": cmd})
        assert p.decide("bash", {"command": cmd}) == "allow", cmd      # 不变量①：批过的自己要放行
        for bad in probes:
            assert p.decide("bash", {"command": bad}) == "ask", (cmd, bad)


def test_夹了选项的命令一律精确授权():
    """`-` 前缀分不出"选项的值"和"子命令"。曾按"取到第一个非选项词"发前缀，于是 spec 停在了选项的
    值上、真正的子命令还在后面自由着——和当初要修的 `python:*` 是同一个洞，只是多写一个前导选项就能
    触发：批准 `python -X utf8 script.py` 记成 `python -X utf8:*` → `python -X utf8 -c "任意代码"` 照样命中。
    断在哪判不了就不断：整条精确。"""
    root = _root()
    for cmd, bad in (
        ("python -X utf8 script.py", 'python -X utf8 -c "import os; os.system(1)"'),
        ("git -c core.pager=cat diff", "git -c core.pager=cat push --force"),
        ("docker -H tcp://x ps", "docker -H tcp://x rm -f 某容器"),
        ("npm --prefix x run build", "npm --prefix x publish"),
        ("python -m pytest -q", 'python -c "import os; os.system(1)"'),
        ("git --no-pager diff", "git --no-pager push --force"),
    ):
        assert specs_for("bash", {"command": cmd}, root) == [cmd], cmd   # 整条、不带 :*
        p = PermissionPolicy.from_persisted({}, project_root=root)
        p.allow_always("bash", {"command": cmd})
        assert p.decide("bash", {"command": cmd}) == "allow", cmd        # 批过的那条免问（不变量①）
        assert p.decide("bash", {"command": bad}) == "ask", bad
    # 首词后【紧跟】非选项词才发前缀：那个词一定是子命令，断得准
    assert specs_for("bash", {"command": "git diff HEAD~1"}, root) == ["git diff:*"]
    assert specs_for("bash", {"command": "npm run build"}, root) == ["npm run:*"]


def test_glob的畸形参数不抛异常():
    """权限闸在 tools.execute 的 catch-all【外面】：抛出去会中断整轮、留下无结果的孤儿 tool_call；
    走 pattern_for 那条异常落在 UI 线程的消息处理器里，整个 TUI 带 traceback 退出。
    _glob_escapes 被 _gen_specs 与 decide 直接喂【原始 args】，绕过了 _subject 那道闸 → 它自己也要消毒。"""
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    for args in ({"path": "src", "pattern": ["*.py", "*.md"]},      # 模型把 pattern 写成数组是常见幻觉
                 {"path": "src", "pattern": 3},
                 {"path": "src", "pattern": {"a": 1}},
                 {"path": ["src", "tests"], "pattern": "*.py"},
                 {"pattern": None}, None):
        assert p.decide("glob", args) in ("allow", "ask")            # 不抛
        assert isinstance(pattern_for("glob", args, root), str)      # 不抛（弹窗构造走这条）
        assert isinstance(specs_for("glob", args, root), list)
    # grep/read 同样不抛（这批工具共用一条类型闸）
    for tool in ("grep", "read_file", "write_file", "edit_file"):
        assert p.decide(tool, {"path": 123}) == "ask"
        assert specs_for(tool, {"path": 123}, root) == []


def test_路径里的通配符按落点判_不带进spec():
    """模型偶尔把通配符写进 path。不截的话这个串一路带进判定与 spec：_spec_match 见 * 走 fnmatch、
    * 跨 / → 那条 spec 等于"该目录下的一切"，弹窗却显示成 `桌面/*`（读起来像只批一层）；
    护栏②即便判出"圈住项目根"要收窄，收窄目标还是这个带 * 的串 —— 收了个寂寞。故在源头截到落点。"""
    root = _root()
    desktop = root.rsplit("/", 1)[0]
    args = {"path": desktop + "/*", "pattern": "TODO"}
    # spec 基于落点、不带通配：目录本身 + 其下（grep 递归，不受护栏②收窄，见 _guard）
    assert specs_for("grep", args, root) == [desktop, desktop + "/**"]
    assert "/*" not in pattern_for("grep", args, root).replace("/**", "")   # 落点里不带通配
    p = PermissionPolicy.from_persisted({}, project_root=root)
    p.allow_always("grep", args)
    assert p.decide("grep", args) == "allow"                       # 不变量①：批过的自己放行
    assert p.decide("grep", {"path": desktop + "/别的项目", "pattern": "x"}) == "allow"  # 批准范围之内
    assert p.decide("grep", {"path": "C:/Users/31784/.ssh", "pattern": "x"}) == "ask"    # 桌面之外：仍问
    # 文件类工具【受】护栏②：桌面是项目根的祖先，spec 会把整个项目圈进去 → 收窄到那一个文件
    assert specs_for("write_file", {"path": desktop + "/report.md"}, root) == [desktop + "/report.md"]
    # 只截 * 与 ?，不截 [ —— data[1] 是合法目录名（由 _esc_glob 处理，见不变量③那条）
    assert specs_for("write_file", {"path": "C:/tmp/data[1]/a.txt"}, root) == ["C:/tmp/data[[]1]/**"]


def test_双点各形态按落点判():
    # `..` 是"往上走"的导航指令、`*` 是通配符，两者都要先算出【落点】再判——判写法必然漏。
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    for tool, args in (("grep", {"path": root + "/..", "pattern": "x"}),
                       ("grep", {"path": root + "/../..", "pattern": "x"}),
                       ("grep", {"path": root + "/../*", "pattern": "x"}),
                       ("read_file", {"path": root + "/../机密.txt"}),
                       ("glob", {"path": root + "/..", "pattern": "**/*"})):
        assert p.decide(tool, args) == "ask", (tool, args)
    # 绕一圈回到根内 → 正常放行（判落点而非写法）
    assert p.decide("grep", {"path": root + "/src/..", "pattern": "x"}) == "allow"


def test_子agent批的授权切模式后依然在():
    """切模式 = TUI 拿 permissions.json 重新 from_persisted + apply_mode（见 tui._set_mode）。
    子 agent 的 store 是 None（按设计不落子会话盘），此前"权限落盘"被"会话落盘"连坐 →
    它点的"总是允许"只活在内存里，随手切个模式就没了，而弹窗写的是"记住，不再问"。"""
    import tempfile

    from mecode.config import agent_config
    from mecode.mode import apply_mode
    from mecode.subagent import SubagentRunner
    with tempfile.TemporaryDirectory() as d:
        st = SessionStore(root=Path(d) / "s", cwd=d)
        root = d.replace("\\", "/")
        main = Agent(_fake_provider(), ToolRegistry(), store=st,
                     policy=PermissionPolicy.from_persisted(st.load_permissions(), project_root=root))
        cmd = {"command": "npm run build"}
        sub = main._subagent._make(None)
        assert sub.policy is main.policy                         # 共用一套闸
        # 子 agent 里点"总是允许"（走的就是 _gate 里那两句：规则按【自己的工具名】落盘）
        for gtool, sp in sub.policy.allow_always("bash", cmd):
            sub._persist_perm(gtool, "allow", sp)
        assert sub.policy.decide("bash", cmd) == "allow"          # 子 agent 内生效
        assert main.policy.decide("bash", cmd) == "allow"         # 父 agent 同等生效
        assert st.load_permissions() == {"bash": {"allow": ["npm run:*"]}}   # 真落盘了
        # 切模式：policy 从盘上重建 —— 授权必须还在
        p2 = apply_mode(PermissionPolicy.from_persisted(st.load_permissions(), project_root=root), "auto")
        assert p2.decide("bash", cmd) == "allow"


def test_批准一个glob目录不等于放行它的逃逸pattern():
    """曾有一个"用户显式授权过该目录就跳过整道 pattern 闸"的出口。它其实是多余的——
    _glob_escapes 判的是"落点在根内【或 path 自己底下】"，被批准的根外目录配正常 pattern 本就不触发；
    留着它反倒把【逃逸 pattern】一并放行了，而那类调用 specs_for 明确判为"不可授权"（返回空列表），
    等于从另一扇门把它授权出去：批准 glob(C:/某目录) 换来 ../../* 的整片列举。"""
    root = _root()
    p = PermissionPolicy.from_persisted({}, project_root=root)
    approved = {"path": "C:/其他目录", "pattern": "*.py"}
    p.allow_always("glob", approved)
    assert p.decide("glob", approved) == "allow"                       # 不变量①
    assert p.decide("glob", {"path": "C:/其他目录", "pattern": "**/*.py"}) == "allow"   # 正常 pattern
    for pat in ("../../*", "{..,x}/**/*", "**/../*"):                  # 逃逸 pattern：仍要问
        assert p.decide("glob", {"path": "C:/其他目录", "pattern": pat}) == "ask", pat
        assert specs_for("glob", {"path": "C:/其他目录", "pattern": pat}, root) == []


def test_项目根是盘符根时权限闸不失灵():
    # realpath 给盘符根的是 "C:/"（带尾斜杠）→ 根规则拼成畸形的 "C://**"、_in_root 拿 "C://" 去比
    # 也永远不中 → 整道闸失灵，根内路径全落 ask（安全但没法用）。普通根不带尾斜杠，去尾是空操作。
    p = PermissionPolicy.from_persisted({}, project_root="C:/")
    assert p.root == "C:"
    assert p.rules["read_file"]["allow"][:2] == ["C:", "C:/**"]        # 不是畸形的 C://**
    assert p.decide("read_file", {"path": "C:/任意/文件.txt"}) == "allow"
    assert p.decide("grep", {"path": "C:/任意", "pattern": "x"}) == "allow"
    # 普通根不受影响
    cwd = _root()
    assert PermissionPolicy.from_persisted({}, project_root=cwd).root == cwd


def test_排队等审批锁时也能被打断():
    """`with self._perm_lock` 是无限阻塞的——排队中的 worker 一头扎进去就再也看不见打断标志，
    于是 Ctrl+C / kill_bgtask 在别人的弹窗被答掉之前完全不生效（用户以为没反应，其实卡在锁上）。
    入口那个"已在中断中"的检查只挡得住进来【之前】就被停的。"""
    import threading

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import tui
    App = type("App", (tui.MecodeApp,), {"screen": None})
    app = App.__new__(App)
    app._perm_seq = 0
    app._perm_screen_seq = None
    app._perm_result = "deny"
    app._perm_waiting = False
    app._perm_lock = threading.Lock()
    app._perm_event = threading.Event()
    app.post_message = lambda m: None
    app.agent = _fake_agent()

    app._perm_lock.acquire()                    # 模拟：别人的弹窗正占着锁
    ev = threading.Event()
    ctx = type("C", (), {"interrupt": ev, "is_sub": True, "can_stop": True})()
    结果 = []
    # daemon：万一实现退化成无限阻塞（改坏时就是这样），这条线程不会拖住 pytest 不退出——
    # 让它【断言失败】而不是整个测试套挂死（mutation 验收时踩过这个坑）。
    t = threading.Thread(target=lambda: 结果.append(app._ask_permission("bash", {"command": "rm x"}, ctx)),
                         daemon=True)
    t.start()
    t.join(0.3)
    assert t.is_alive() and not 结果                        # 正排队等锁

    ev.set()                                                # 此刻按 Ctrl+C / kill_bgtask
    t.join(2.0)
    app._perm_lock.release()                                # 先放锁，免得断言失败时卡住后续
    assert not t.is_alive() and 结果 == ["deny"]            # 能退出来，不再干等
    assert app._perm_lock.acquire(timeout=0.5)              # 锁没被泄漏（拿得到）
    app._perm_lock.release()


def test_chat的审批与REPL共用stdin锁():
    """stdin 只有一个：并发前台子 agent 的审批、后台任务的审批（守护线程，可能在 REPL 停在"你> "
    时冒出来）、REPL 自己——三方不排队就会两段提示交错打在同一屏，用户敲的一行 y 只被其中一个读到。
    chat.py 之前只给审批加了锁、REPL 的 input 没走它。这里从源码层面钉住三件事。"""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "chat.py").read_text(encoding="utf-8")
    assert "_stdin_lock = threading.Lock()" in src
    # 审批提示与 REPL 读行都在同一把锁下
    assert src.count("with _stdin_lock:") == 2
    assert "def _read_line(" in src
    assert "user = _read_line(" in src           # REPL 走 _read_line，不再裸 input
    assert 'user = input(' not in src            # 没有漏网的裸 input


def test_deny侧比allow侧多认一种形态():
    """同一件事在两侧语义相反：多一个比对形态，在 deny 是【多一次被拦的机会】（更安全），
    在 allow 是【多一条放行路】（更宽松）。所以 allow 侧只认解析后的落点，deny 侧额外保留原始串。"""
    root = _root()
    # ① 落点形态：写法绕来绕去，只要落在 deny 的范围里就拦得住
    p = PermissionPolicy.from_persisted({"read_file": {"deny": [root + "/secret/**"]}}, project_root=root)
    for path in (root + "/secret/a.txt",
                 root + "/secret/../secret/a.txt",
                 root + "/src/../secret/a.txt"):        # 原始串不以 deny 前缀开头，靠落点拦
        assert p.decide("read_file", {"path": path}) == "deny", path
    # ② 原始串形态：deny spec 本身带 .. 时，落点已经把 .. 解掉了 → 只有【未解析的原始串】能命中。
    #    这是 deny 侧比 allow 侧多认一种形态的唯一实效场景（allow 侧【绝不能】认它：
    #    `<根>/../../etc/x` 以根开头就会被 `<根>/**` 匹配上而放行，正是 V1 那个逃逸洞）。
    p2 = PermissionPolicy.from_persisted({"read_file": {"deny": [root + "/../secret/**"]}},
                                         project_root=root)
    assert p2.decide("read_file", {"path": root + "/../secret/a.txt"}) == "deny"
    # ③ 相对 deny spec：由归一化的相对形态命中（它也在 _allow_subjects 里）
    p3 = PermissionPolicy.from_persisted({"read_file": {"deny": ["secret/**"]}}, project_root=root)
    assert p3.decide("read_file", {"path": "secret/a.txt"}) == "deny"
    assert p3.decide("read_file", {"path": "src/../secret/a.txt"}) == "deny"   # 带 .. 也归一得到
    assert p3.decide("read_file", {"path": root + "/src/x.py"}) == "allow"     # 别的照常放行


def test_轮末清理接在轮收尾上_且stop会置打断标志():
    """两处零覆盖：① _clear_orphan_modal 有测试、但【它被谁调用】没测 —— 把轮收尾里那行删掉，
    孤儿窗再没人清，测试却全绿。② 选"停止此后台任务"要置提问方自己的打断标志（=终止那个任务）。"""
    import threading

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import tui
    # ① 轮收尾（_on_event 的 ev is None 分支）确实调了清理
    src = (Path(__file__).resolve().parent.parent / "scripts" / "tui.py").read_text(encoding="utf-8")
    收尾段 = src.split("if ev is None:")[1].split("match ev:")[0]
    assert "self._clear_orphan_modal()" in 收尾段

    # ② stop → 置那个 agent 自己的打断标志，并按拒绝返回
    App = type("App", (tui.MecodeApp,), {"screen": None})
    app = App.__new__(App)
    app._perm_seq = 0
    app._perm_screen_seq = None
    app._perm_waiting = False
    app._perm_lock = threading.Lock()
    app._perm_event = threading.Event()
    # 用 post_message 当触发点：UI 收到请求后"用户立刻点了停止"。
    # 不能在调用前先 set 事件——_ask_permission 一进来就 clear，那样会永远等不到结果（死循环）。
    def 立刻点停止(msg):
        app._perm_result = "stop"
        app._perm_event.set()
    app.post_message = 立刻点停止
    app.agent = _fake_agent()
    任务标志 = threading.Event()
    ctx = type("C", (), {"interrupt": 任务标志, "is_sub": True, "can_stop": True})()
    assert app._ask_permission("bash", {"command": "rm x"}, ctx) == "deny"   # 按拒绝返回
    assert 任务标志.is_set()                                                 # 且真的把那个任务停了
    assert not app.agent._interrupt.is_set()                                 # 没误伤主 agent
