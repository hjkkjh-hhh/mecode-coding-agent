"""真工具：read_file / write_file / edit_file / bash。

固化各自的不变量（行窗口、create/update、前置校验、shell 选择/超时等）。
"""
import os
import shutil

import pytest

from mecode.tools import MAX_WRITE_BYTES, _bash_command, default_registry


def _exec(name, **args):
    return default_registry().execute(name, args)


# ---- read_file ----

def test_read_带行号与总行数(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("aaa\nbbb\nccc", encoding="utf-8")
    out = _exec("read_file", path=str(p))
    assert "共 3 行" in out
    assert "     1\taaa" in out and "     3\tccc" in out      # cat -n 行号前缀


def test_read_limit窗口_并提示翻页(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("\n".join(f"L{i}" for i in range(10)), encoding="utf-8")
    out = _exec("read_file", path=str(p), limit=3)
    assert "显示第 1-3 行" in out
    assert "offset=3" in out                                  # 翻页提示
    assert "L0" in out and "L3" not in out                    # 只给了前 3 行


def test_read_offset(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("L0\nL1\nL2\nL3", encoding="utf-8")
    out = _exec("read_file", path=str(p), offset=2, limit=10)
    assert "     3\tL2" in out and "     4\tL3" in out
    assert "L0" not in out


def test_read_二进制拒绝(tmp_path):
    p = tmp_path / "bin"
    p.write_bytes(b"abc\x00def")                              # 含 NUL
    assert "二进制" in _exec("read_file", path=str(p))


def test_read_不存在(tmp_path):
    assert "不存在" in _exec("read_file", path=str(tmp_path / "nope"))


def test_read_file_有更大的截断预算():
    reg = default_registry()
    assert reg.output_limit("read_file", 4000) > 4000        # read_file 自带大预算
    assert reg.output_limit("write_file", 4000) == 4000      # 没设 → 用默认
    assert reg.output_limit("不存在的工具", 4000) == 4000     # 查不到 → 用默认


def test_write_新建文件_含自动建父目录(tmp_path):
    p = tmp_path / "sub" / "dir" / "a.txt"      # 父目录 sub/dir 不存在
    out = _exec("write_file", path=str(p), content="第一行\n第二行")
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == "第一行\n第二行"
    assert "已创建" in out and "2 行" in out


def test_write_更新文件_返回增删行数(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("a\nb\nc", encoding="utf-8")
    out = _exec("write_file", path=str(p), content="a\nB\nc\nd")   # 改1行(b→B)、加1行(d)
    assert p.read_text(encoding="utf-8") == "a\nB\nc\nd"
    assert "已更新" in out and "+2" in out and "-1" in out          # B、d 算增；b 算删


def test_write_超大小拒绝(tmp_path):
    p = tmp_path / "big.txt"
    out = _exec("write_file", path=str(p), content="x" * (MAX_WRITE_BYTES + 1))
    assert "超过" in out and "拒绝" in out
    assert not p.exists()                                          # 拒绝后没落盘


def test_write_注册进了registry():
    names = [t["function"]["name"] for t in default_registry().schemas()]
    assert "write_file" in names


# ---- edit_file ----

def test_edit_唯一匹配_替换成功(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\ny = 2\n", encoding="utf-8")
    out = _exec("edit_file", path=str(p), old_string="y = 2", new_string="y = 20")
    assert p.read_text(encoding="utf-8") == "x = 1\ny = 20\n"
    assert "已编辑" in out and "替换 1 处" in out


def test_edit_找不到old报错(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1", encoding="utf-8")
    out = _exec("edit_file", path=str(p), old_string="不存在", new_string="z")
    assert "找不到" in out
    assert p.read_text(encoding="utf-8") == "x = 1"          # 没动


def test_edit_不唯一且未replace_all报错(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("a\na\na", encoding="utf-8")
    out = _exec("edit_file", path=str(p), old_string="a", new_string="b")
    assert "不唯一" in out and "3 次" in out
    assert p.read_text(encoding="utf-8") == "a\na\na"        # 没动


def test_edit_replace_all_全替(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("a\na\na", encoding="utf-8")
    out = _exec("edit_file", path=str(p), old_string="a", new_string="b", replace_all=True)
    assert p.read_text(encoding="utf-8") == "b\nb\nb"
    assert "替换 3 处" in out


def test_edit_文件不存在报错(tmp_path):
    out = _exec("edit_file", path=str(tmp_path / "nope.py"), old_string="a", new_string="b")
    assert "不存在" in out


def test_edit_old空字符串报错(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x", encoding="utf-8")
    assert "不能为空" in _exec("edit_file", path=str(p), old_string="", new_string="y")


def test_edit_old等于new报错(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x", encoding="utf-8")
    assert "无需修改" in _exec("edit_file", path=str(p), old_string="x", new_string="x")


# ---- bash ----

def test_bash_命令放在argv最后():
    # 不论哪个平台分支，命令字符串都作为最后一个参数传给 shell
    assert _bash_command("echo hi")[-1] == "echo hi"


# ---- Windows 退出码翻译 ----
# 命令无任何 stdout/stderr、只剩一个裸退出码时，模型无从判断。9009 是 Windows"命令找不到"的
# 约定码（cmd 的"不是内部或外部命令"、商店应用执行别名未装应用时都返回它），bash 截成 8 位 = 49。
# 【只翻译不建议】：下一步换命令还是装依赖，交给模型自己判断。

def test_退出码翻译_9009与截断的49都认(monkeypatch):
    from mecode import tools
    monkeypatch.setattr(tools.os, "name", "nt")
    assert "命令找不到" in tools._exit_code_note(9009)
    assert "命令找不到" in tools._exit_code_note(49)     # bash 把 9009 截成 8 位，同一件事


def test_退出码翻译_只给含义_不给建议也不讲词源(monkeypatch):
    # 刻意不出现：①"请改用 X"这类指令（会掐掉模型的其他选项：装依赖/查 PATH/换工具）；
    # ②"49 是 9009 截断"这类词源（对模型不构成行动信息，该待在代码注释里给人看）。
    from mecode import tools
    monkeypatch.setattr(tools.os, "name", "nt")
    note = tools._exit_code_note(49)
    assert "请改用" not in note and "python" not in note.lower()
    assert "9009" not in note and "截断" not in note
    assert len(note) < 60                              # 一句话，别再长回去


def test_退出码翻译_无关码与非windows都不吭声(monkeypatch):
    from mecode import tools
    monkeypatch.setattr(tools.os, "name", "nt")
    assert tools._exit_code_note(1) == "" and tools._exit_code_note(127) == ""   # 别的码有各自含义，不瞎猜
    monkeypatch.setattr(tools.os, "name", "posix")
    assert tools._exit_code_note(49) == ""      # POSIX 上 49 是普通退出码，翻译反而误导


def test_bash_静默的找不到_结果里带翻译(monkeypatch):
    from mecode import tools
    monkeypatch.setattr(tools, "_exit_code_note", lambda c: "（假翻译）")
    out = tools._bash({"command": "exit 49"})
    assert "(exit code: 49)" in out and "（假翻译）" in out


def test_bash_有输出的失败_不加翻译(monkeypatch):
    # 已经有 stderr 可读时不画蛇添足（翻译只补"零线索"那种失败）
    from mecode import tools
    monkeypatch.setattr(tools, "_exit_code_note", lambda c: "（不该出现）")
    out = tools._bash({"command": "echo boom 1>&2; exit 3"})
    assert "(exit code: 3)" in out and "不该出现" not in out


def test_procslot_kill杀登记的进程_clear后不再杀(monkeypatch):
    import threading
    from mecode import tools
    killed = []
    monkeypatch.setattr(tools, "_kill_tree", lambda p: killed.append(p))
    slot = tools.ProcSlot(threading.Event())
    fake = object()
    slot.register(fake)
    slot.kill()
    assert killed == [fake]            # 杀的正是登记的那个进程
    slot.clear()
    slot.kill()
    assert killed == [fake]            # clear 后槽空 → 不再调 _kill_tree


def test_bash_窗口1_起进程前已打断则不起():
    import threading
    from mecode.tools import ProcSlot, _bash
    ev = threading.Event()
    ev.set()                           # 进程还没起就已"被打断"
    out = _bash({"command": "echo hi"}, ProcSlot(ev))
    assert "未执行" in out             # 起进程前查标志命中 → 直接返回，不 Popen


@pytest.mark.skipif(shutil.which("bash") is None, reason="需 git-bash 才能用 sleep")
def test_bash_窗口2_打断保留中断前输出():
    import threading
    import time as _t
    from mecode.tools import ProcSlot, _bash
    ev = threading.Event()
    slot = ProcSlot(ev)

    def stop():                                    # 模拟 TUI 的 request_interrupt：置标志 + 杀进程
        _t.sleep(0.5)
        ev.set()
        slot.kill()

    th = threading.Thread(target=stop)
    th.start()
    out = _bash({"command": "echo HELLO-BEFORE-STOP; sleep 5"}, slot=slot)   # 先输出，再卡在 sleep 被杀
    th.join()
    assert "HELLO-BEFORE-STOP" in out              # 中断前的输出保留了（不再一律丢弃）
    assert "被用户打断" in out                      # 并附了打断说明


def test_bash描述_反映当前可用shell():
    desc = next(t["function"]["description"] for t in default_registry().schemas()
                if t["function"]["name"] == "bash")
    if shutil.which("bash"):
        assert "Unix" in desc                 # 有 bash → 引导用 Unix 语法
    else:
        assert "cmd" in desc or "PowerShell" in desc


def test_bash_空命令报错():
    assert "不能为空" in _exec("bash", command="   ")


def test_bash_echo回显():
    out = _exec("bash", command="echo hello123")          # echo 在 cmd 和 sh 都可用
    assert "hello123" in out


def test_bash_非零退出码():
    out = _exec("bash", command="cd /路径肯定不存在xyz")    # cd 失败 → 非零退出
    assert "exit code:" in out or "[stderr]" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="需 git-bash 才能用 sleep 测超时")
def test_bash_超时被终止():
    out = _exec("bash", command="sleep 3", timeout=1)
    assert "超时" in out


# ---- grep ----

def _make_tree(root):
    (root / "a.py").write_text("import os\nx = 1  # TODO fix\n", encoding="utf-8")
    (root / "b.py").write_text("y = 2\n# TODO later\n", encoding="utf-8")
    (root / "c.txt").write_text("nothing here\n", encoding="utf-8")
    (root / "d.txt").write_text("TODO in text\n", encoding="utf-8")
    g = root / ".git"
    g.mkdir()
    (g / "config").write_text("TODO in git\n", encoding="utf-8")   # 噪音目录，应被跳过


def test_grep_列出含匹配的文件_跳过噪音目录(tmp_path):
    _make_tree(tmp_path)
    out = _exec("grep", pattern="TODO", path=str(tmp_path))
    assert "a.py" in out and "b.py" in out and "d.txt" in out
    assert "config" not in out                               # .git 被跳过
    assert "3 个文件" in out


def test_grep_content模式带行号(tmp_path):
    _make_tree(tmp_path)
    out = _exec("grep", pattern="TODO fix", path=str(tmp_path), output_mode="content")
    assert "a.py:2:" in out and "TODO fix" in out            # path:行号: 内容


def test_grep_count模式(tmp_path):
    _make_tree(tmp_path)
    out = _exec("grep", pattern="TODO", path=str(tmp_path), output_mode="count")
    assert "处" in out


def test_grep_glob只搜py(tmp_path):
    _make_tree(tmp_path)
    out = _exec("grep", pattern="TODO", path=str(tmp_path), glob="*.py")
    assert "a.py" in out and "b.py" in out
    assert "d.txt" not in out                                # glob 过滤掉 .txt


def test_grep_忽略大小写(tmp_path):
    _make_tree(tmp_path)
    out = _exec("grep", pattern="todo", path=str(tmp_path), case_insensitive=True)
    assert "a.py" in out


def test_grep_无匹配(tmp_path):
    _make_tree(tmp_path)
    assert "没有匹配" in _exec("grep", pattern="ZZZ不存在", path=str(tmp_path))


def test_grep_正则非法(tmp_path):
    assert "正则不合法" in _exec("grep", pattern="[", path=str(tmp_path))


# ---- glob ----

def _make_glob_tree(root):
    (root / "a.py").write_text("x", encoding="utf-8")
    (root / "readme.md").write_text("x", encoding="utf-8")
    (root / "c.txt").write_text("x", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("x", encoding="utf-8")
    g = root / ".git"
    g.mkdir()
    (g / "hook.py").write_text("x", encoding="utf-8")        # 噪音目录里的 .py，应被跳过


def test_glob_顶层非递归(tmp_path):
    _make_glob_tree(tmp_path)
    out = _exec("glob", pattern="*.py", path=str(tmp_path))
    assert "a.py" in out and "b.py" not in out               # *.py 不递归


def test_glob_递归跳噪音目录(tmp_path):
    _make_glob_tree(tmp_path)
    out = _exec("glob", pattern="**/*.py", path=str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "hook.py" not in out                              # .git 被跳过


def test_glob_花括号展开(tmp_path):
    _make_glob_tree(tmp_path)
    out = _exec("glob", pattern="*.{py,md}", path=str(tmp_path))
    assert "a.py" in out and "readme.md" in out and "c.txt" not in out


def test_glob_无匹配(tmp_path):
    _make_glob_tree(tmp_path)
    assert "没有匹配" in _exec("glob", pattern="*.rs", path=str(tmp_path))


def test_glob_按修改时间倒序(tmp_path):
    old = tmp_path / "old.py"
    old.write_text("x", encoding="utf-8")
    new = tmp_path / "new.py"
    new.write_text("x", encoding="utf-8")
    os.utime(old, (1, 1))                                    # 把 old 的时间设得很旧
    out = _exec("glob", pattern="*.py", path=str(tmp_path))
    assert out.index("new.py") < out.index("old.py")         # 新的在前
