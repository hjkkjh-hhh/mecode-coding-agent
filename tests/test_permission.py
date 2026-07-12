"""工具权限：按 tool(spec) 模式做参数细分的策略 + agent 执行前过闸。

固化的不变量：
- 默认：只读类/安全只读命令 allow，其余（写/改/未列 bash）落 default=ask
- 参数细分：bash(ls:*) 放行、bash(rm …) 仍 ask；spec 支持前缀/精确/glob
- 优先级 deny > ask > allow > default（窄规则压广规则）
- allow_always 生成 pattern（bash 记首词、其余记整工具）；agent 闸 deny/once/always
"""
import os

from mecode.agent import Agent
from mecode.events import Done, TextDelta, ToolCall, Usage
from mecode.permission import PermissionPolicy, pattern_for, spec_for
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


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
    assert spec_for("bash", {"command": "rm -rf x"}) == "rm:*"
    assert pattern_for("bash", {"command": "rm -rf x"}) == "bash(rm:*)"     # 显示用
    assert spec_for("write_file", {"path": "C:/a/b/x.txt"}) == "C:/a/b/*"   # 文件→所在文件夹
    assert spec_for("edit_file", {"path": "C:/a/b/x.txt"}) == "C:/a/b/*"
    assert pattern_for("write_file", {"path": "C:/a/b/x.txt"}) == "write_file(C:/a/b/*)"
    assert spec_for("read_file", {"path": "x.txt"}) == "x.txt"              # 无文件夹 → 精确到该文件，不放整工具


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
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("glob", {"pattern": "src/**/*", "path": "."}) == "allow"     # 就是弹窗那个 case
    assert p.decide("grep", {"pattern": "foo", "path": "src"}) == "allow"        # 相对子目录
    assert p.decide("read_file", {"path": "src/mecode/agent.py"}) == "allow"     # 相对文件
    assert p.decide("glob", {"pattern": "**/*"}) == "allow"                      # path 省略=当前目录
    assert p.decide("read_file", {"path": "../secret.txt"}) == "ask"            # 相对跳出根 → 仍问


def test_glob的pattern里双点不能绕过根内限制():
    # gate 判的是 path；但 glob 的 pattern 是路径通配、能带 .. 逃出 path → 合并判定，防绕过。
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("glob", {"pattern": "src/**/*", "path": "."}) == "allow"     # 正常根内
    assert p.decide("glob", {"pattern": "../../*", "path": "."}) == "ask"        # pattern 用 .. 逃出根 → 仍问
    assert p.decide("glob", {"pattern": "../*", "path": "src"}) == "allow"       # ../ 从 src 回到根内 → 放行


def test_allow_always_即时生效():
    p = PermissionPolicy()
    assert p.decide("bash", {"command": "rm x"}) == "ask"
    specs = p.allow_always("bash", {"command": "rm x"})
    assert specs == ["rm:*"]                                            # 返回 spec 列表（非管道=单条）
    assert p.decide("bash", {"command": "rm y"}) == "allow"             # 同首词都放行


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
    specs = p.allow_always("bash", {"command": "cat x | jq ."})
    assert specs == ["cat:*", "jq:*"]                                             # 两段头都记
    assert pattern_for("bash", {"command": "cat x | jq ."}) == "bash(cat:*, jq:*)"  # 显示多条
    assert p.decide("bash", {"command": "cat a | jq b"}) == "allow"               # 下次同类管道免问


def test_bash读命令路径参数受项目根闸约束():
    # 洞：read_file 被限制在项目根内，但 bash cat/head/tail 默认放行、只看命令头不看路径 →
    # `cat ~/.ssh/id_rsa` 能绕过根闸读根外文件。命中放行后补一道路径根闸堵上。
    root = os.getcwd().replace("\\", "/")
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
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "grep -rn TODO src"}) == "allow"            # pattern TODO 不当路径,src 根内
    assert p.decide("bash", {"command": "grep 'a.*b' src/x.py"}) == "allow"         # 正则 pattern 含 * 也不误拦（被跳过）
    assert p.decide("bash", {"command": "grep root /etc/passwd"}) == "ask"          # 文件根外 → 问
    assert p.decide("bash", {"command": "grep --regexp=root /etc/passwd"}) == "ask" # pattern 藏 flag 里,位置参数是文件 → 不漏判


def test_bash读命令管道每段都受根闸():
    root = os.getcwd().replace("\\", "/")
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
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cut -f1 src/mecode/agent.py"}) == "allow"      # 根内文件
    assert p.decide("bash", {"command": "cut -d: -f1 README.md"}) == "allow"            # -d: 是分隔符不是路径
    assert p.decide("bash", {"command": "cut -c1- ../secret"}) == "ask"                 # 根外 → 拦
    assert p.decide("bash", {"command": "cut -f1 ~/.gitconfig"}) == "ask"
    assert p.decide("bash", {"command": "cut -c1- ../a | tr a b"}) == "ask"             # 管道段里也拦


def test_bash_grep贴附flag形式不绕过根闸():
    # 红队发现：贴附形式让"跳过 pattern"误判/让 -f 文件路径藏进 flag token 逃过核查
    root = os.getcwd().replace("\\", "/")
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
    root = os.getcwd().replace("\\", "/")
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
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat */../../../.gitconfig"}) == "ask"          # 通配后接 .. 爬出根
    assert p.decide("bash", {"command": "cat src/*/../../../x"}) == "ask"
    assert p.decide("bash", {"command": "cat src/*.py"}) == "allow"                     # 正常根内 glob 不受影响


def test_bash_花括号展开不能藏双点爬出根():
    # 二轮红队发现：`cat {.,..}/x` 被 bash 展开成 ./x 和 ../x，.. 藏在花括号里躲过"裸 .. 段"检查
    # 且 abspath 把 {.,..} 当字面目录名 → 误判根内。含 {}（无法静态定落点）一律保守拦。
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat {.,..}/server_config.md"}) == "ask"
    assert p.decide("bash", {"command": "cut -c1-30 {.,..}/x"}) == "ask"
    assert p.decide("bash", {"command": "cat {.,..}/x | head -1"}) == "ask"             # 管道段内也拦
    assert p.decide("bash", {"command": "grep -f{.,..}/pat README.md"}) == "ask"        # 藏进 -f 贴附路径也拦


def test_bash_命令名shell污染不能短路根闸():
    # 五轮红队：cat$'' 被 shlex 切成 'cat$'（不在 _READ_CMDS）→ 根闸短路放行，但 bash 折叠成 cat 读文件。
    # 用与 allow 同源的 _spec_match 重判原始命令：匹配读命令 spec 就保守拦。
    root = os.getcwd().replace("\\", "/")
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
    root = os.getcwd().replace("\\", "/")
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


def test_bash读命令_用户显式always过则放行全部根外():
    # 根闸只拦【默认】放行：用户此前 always-allow 过 cat（permissions.json 存了 cat:*）→ 视为已授权,放行全部
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({"bash": {"allow": ["cat:*"]}}, project_root=root)
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "allow"       # 用户授权过 cat → 根外也放
    assert p.decide("bash", {"command": "cat ~/.ssh/id_rsa"}) == "allow"
    assert p.decide("bash", {"command": "cat /a | grep x"}) == "allow"       # 管道段命中用户 cat:* 也放
    assert p.decide("bash", {"command": "head /etc/passwd"}) == "ask"        # 没授权的 head 仍受根闸
    assert p.decide("bash", {"command": "grep x /etc/passwd"}) == "ask"      # grep 也没授权 → 仍拦


def test_bash读命令_运行时always后即时对根外放行():
    root = os.getcwd().replace("\\", "/")
    p = PermissionPolicy.from_persisted({}, project_root=root)
    assert p.decide("bash", {"command": "cat /etc/hosts"}) == "ask"          # 默认根闸拦
    p.allow_always("bash", {"command": "cat /etc/hosts"})                    # 用户选"总是允许" → 记 cat:*
    assert p.decide("bash", {"command": "cat /etc/passwd"}) == "allow"       # 之后 cat 全放（含别的根外文件）


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
              policy=PermissionPolicy(), ask_permission=lambda n, ar: asked.append(n) or choice)
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
              policy=PermissionPolicy(), ask_permission=lambda n, ar: "always")
    list(a.run_turn("调用"))
    assert ran == [True]
    assert st.load_permissions() == {"bash": {"allow": ["rm:*"]}}       # 落盘成 工具→spec


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
