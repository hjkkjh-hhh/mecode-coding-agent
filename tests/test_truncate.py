"""truncate_output：工具结果入场截断。

固化的不变量：① 短输出原样 ② 超长留头+尾+省略标记且有界
③ 头尾对齐整行（不显示半行）④ 超长单行退回硬切（不被一行撑爆）。
"""
from mecode.tools import truncate_output

# 每行约 45 字，300 行 ≈ 13500 字，远超 4000 上限
_LINES = "\n".join(f"L{i:03d}:" + "x" * 40 for i in range(300))


def test_短输出原样返回():
    assert truncate_output("hi\nbye", 4000, 800) == "hi\nbye"


def test_超长_有界且带省略标记():
    out = truncate_output(_LINES, 4000, 800)
    assert len(out) < 4200          # 有界（4000 + 标记几十字）
    assert "已省略" in out           # 省略标记在


def test_头保到开头_尾保到结尾():
    out = truncate_output(_LINES, 4000, 800)
    assert out.startswith("L000:")              # 头从最开始
    assert out.rstrip().endswith("x" * 40)      # 尾保到最后一行
    assert "L299:" in out.splitlines()[-1]


def test_头尾都对齐整行_不出现半行():
    out = truncate_output(_LINES, 4000, 800)
    lines = out.split("\n")
    marker_i = next(i for i, l in enumerate(lines) if "已省略" in l)
    head_last, tail_first = lines[marker_i - 1], lines[marker_i + 1]
    assert head_last.endswith("x" * 40)                          # 头末行完整
    assert tail_first.startswith("L") and tail_first.endswith("x" * 40)  # 尾首行完整


def test_超长单行_退回硬切_不被撑爆():
    out = truncate_output("A" * 50000, 4000, 800)   # 一行 5 万字、无换行
    assert len(out) < 4200
