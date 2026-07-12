"""edit_file 替换器级联：精确优先；行修剪/块锚点/空白归一/缩进弹性/转义归一各级容错；
唯一性与不成比例的安全拒绝；找不到时给最相似片段。参考 Claude Code / opencode 的 replacer cascade。"""
from pathlib import Path

from mecode.tools import _edit_file


def _write(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "t.py"
    f.write_text(content, encoding="utf-8")
    return f


def _edit(f: Path, old: str, new: str, **kw) -> str:
    return _edit_file({"path": str(f), "old_string": old, "new_string": new, **kw})


def test_精确匹配_原有行为不变(tmp_path):
    f = _write(tmp_path, "a = 1\nb = 2\n")
    out = _edit(f, "b = 2", "b = 3")
    assert out.startswith("已编辑") and "经" not in out          # 精确路径不带"经xx匹配"字样
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 3\n"


def test_行修剪_容忍行尾空格和缩进差(tmp_path):
    f = _write(tmp_path, "def foo():  \n    x = 1   \n    return x\n")   # 文件里有行尾空格
    out = _edit(f, "def foo():\n    x = 1\n    return x", "def foo():\n    return 2")
    assert "行修剪" in out
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


def test_块锚点_长块中间记错一行(tmp_path):
    body = "\n".join(f"    line{i} = {i}" for i in range(8))
    f = _write(tmp_path, f"def big():\n{body}\n    return 0\n")
    wrong_mid = body.replace("line4 = 4", "line4 = 44")          # 中间一行记错
    out = _edit(f, f"def big():\n{wrong_mid}\n    return 0", "def big():\n    return 9")
    assert "块锚点" in out
    assert f.read_text(encoding="utf-8") == "def big():\n    return 9\n"


def test_空白归一_tab与空格混用(tmp_path):
    f = _write(tmp_path, "if x:\n\ty = 1\n")                     # 文件是 tab
    out = _edit(f, "if x:\n    y = 1", "if x:\n    y = 2")       # 模型给 4 空格
    assert out.startswith("已编辑")
    assert "y = 2" in f.read_text(encoding="utf-8")


def test_缩进弹性_整块层级记浅了(tmp_path):
    f = _write(tmp_path, "class A:\n    def m(self):\n        return 1\n")
    out = _edit(f, "def m(self):\n    return 1", "def m(self):\n    return 2")   # 少了一层缩进
    assert "已编辑" in out
    text = f.read_text(encoding="utf-8")
    assert "return 2" in text and "class A:" in text


def test_转义归一_字面反斜杠n(tmp_path):
    f = _write(tmp_path, 'print("hi")\nprint("yo")\n')
    out = _edit(f, 'print("hi")\nprint("yo")', 'print("done")')   # \n 写成字面两字符
    assert "转义归一" in out
    assert f.read_text(encoding="utf-8") == 'print("done")\n'


def test_宽松匹配_歧义拒绝(tmp_path):
    # 两个块行尾空白不同（精确匹配 0 命中），行修剪后相同 → 两个不同候选，无法确定改哪处
    f = _write(tmp_path, "if a:  \n    b()\ny = 0\nif a:\t\n    b()\n")
    out = _edit(f, "if a:\n    b()", "if a:\n    c()")
    assert out.startswith("错误") and "多处近似" in out
    assert "c()" not in f.read_text(encoding="utf-8")             # 没动文件


def test_找不到_给最相似片段(tmp_path):
    f = _write(tmp_path, "def calc(a, b):\n    return a + b\n")
    out = _edit(f, "def calc(a, c):\n    return a + c", "x")      # 差两个字符、非空白差异
    assert out.startswith("错误") and "最相似的片段" in out and "return a + b" in out


def test_replace_all_不走宽松级联(tmp_path):
    f = _write(tmp_path, "v = 1  \nv = 1\n")
    out = _edit(f, "v = 1   ", "v = 2", replace_all=True)         # 三个空格：精确 0 命中 + 全替 → 拒绝
    assert "只支持精确匹配" in out
