"""同类工具批折叠的纯逻辑：_tool_class（分组键）+ _batch_summary（折叠行文案）。

固化的不变量：① 分组键映射正确、非折叠类返回 None ② 各类摘要文案（单个/多个、去重、
文件名/命令直显、编辑处数追加）符合蓝图。只测纯函数，不起 textual（导入 tui 即可）。
"""
import sys
from pathlib import Path

# 让 tests 能 import scripts/tui（conftest 只加了 src）。tui 顶部会 import textual/mecode，装了即可。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tui   # noqa: E402


def test_VS16字宽校准保留彩色emoji原文():
    warning = "⚠️"
    rocket = "🚀"
    try:
        tui._set_vs16_cell_width(1)
        assert warning == "⚠️"                 # 校准只改计宽，不能删 VS16 / 改成文本符号
        assert tui.cell_len(warning) == 1
        assert tui.cell_len(rocket) == 2        # 原生双宽 emoji 不受影响

        tui._set_vs16_cell_width(2)
        assert tui.cell_len(warning) == 2
        assert tui.cell_len(rocket) == 2
    finally:
        tui._set_vs16_cell_width(2)              # 不污染后续测试的全局 Rich 计宽状态


def test_分组键映射():
    assert tui._tool_class("read_file") == "explore"
    assert tui._tool_class("grep") == "explore"
    assert tui._tool_class("glob") == "explore"
    assert tui._tool_class("bash") == "bash"
    assert tui._tool_class("write_file") == "edit"
    assert tui._tool_class("edit_file") == "edit"
    assert tui._tool_class("subagent") == "subagent"
    for n in ("task_create", "task_update", "task_get", "task_list"):
        assert tui._tool_class(n) == "task"


def test_非折叠类返回None():
    for n in ("save_memory", "recall_memory", "check_bgtask", "wait_bgtask",
              "kill_bgtask", "不存在的工具"):
        assert tui._tool_class(n) is None


# ---- explore ----

def test_explore_单个read_file显示文件名():
    icon, detail = tui._batch_summary("explore", [("read_file", {"path": "a/b/config.py"})])
    assert icon == "⌕"
    assert detail == "探索 · config.py"


def test_explore_文件与文件夹计数去重():
    calls = [
        ("read_file", {"path": "x.py"}),
        ("read_file", {"path": "x.py"}),      # 重复 → 去重后仍 1 个文件
        ("read_file", {"path": "y.py"}),
        ("grep", {"path": "src"}),
        ("glob", {}),                          # 无 path → 计作 "."
        ("grep", {"path": "src"}),             # 重复目录 → 去重
    ]
    icon, detail = tui._batch_summary("explore", calls)
    assert detail == "探索 · 2 个文件, 2 个文件夹"   # 文件 {x,y}=2；目录 {src, "."}=2


def test_explore_只有文件夹():
    calls = [("grep", {"path": "a"}), ("glob", {"pattern": "*.py"})]
    _, detail = tui._batch_summary("explore", calls)
    assert detail == "探索 · 2 个文件夹"           # glob 无 path → "."；a 与 "." 两个目录


# ---- bash ----

def test_bash_单条显示命令():
    icon, detail = tui._batch_summary("bash", [("bash", {"command": "pytest"})])
    assert icon == "$"
    assert detail == "命令 · pytest"


def test_bash_多条计数():
    calls = [("bash", {"command": "a"}), ("bash", {"command": "b"}), ("bash", {"command": "c"})]
    _, detail = tui._batch_summary("bash", calls)
    assert detail == "命令 · 3 条"


# ---- edit ----

def test_edit_单文件单次():
    icon, detail = tui._batch_summary("edit", [("write_file", {"path": "d/foo.py"})])
    assert icon == "✎"
    assert detail == "编辑 · foo.py"


def test_edit_单文件多次追加处数():
    calls = [("edit_file", {"path": "foo.py"}), ("edit_file", {"path": "foo.py"})]
    _, detail = tui._batch_summary("edit", calls)
    assert detail == "编辑 · foo.py · 2 处"


def test_edit_多文件():
    calls = [("write_file", {"path": "a.py"}), ("edit_file", {"path": "b.py"})]
    _, detail = tui._batch_summary("edit", calls)
    assert detail == "编辑 · 2 个文件"            # N==M(=2) → 不追加处数


def test_edit_多文件多处():
    calls = [("edit_file", {"path": "a.py"}), ("edit_file", {"path": "a.py"}),
             ("edit_file", {"path": "b.py"})]
    _, detail = tui._batch_summary("edit", calls)
    assert detail == "编辑 · 2 个文件 · 3 处"      # M=2 个文件，N=3 次


# ---- subagent ----

def test_subagent_单个显示description():
    calls = [("subagent", {"description": "查一下配置", "prompt": "长长的 prompt"})]
    icon, detail = tui._batch_summary("subagent", calls)
    assert icon == "◇"
    assert detail == "启用子 agent · 查一下配置"


def test_subagent_多个计数():
    calls = [("subagent", {"description": "a"}), ("subagent", {"description": "b"})]
    _, detail = tui._batch_summary("subagent", calls)
    assert detail == "启用子 agent · 2 个"


# ---- task ----

def test_task_计数():
    calls = [("task_create", {}), ("task_update", {}), ("task_list", {})]
    icon, detail = tui._batch_summary("task", calls)
    assert icon == "≡"
    assert detail == "更新任务清单 · 3 次"


# ---- 健壮性：args 非 dict ----

def test_args非dict按空处理不报错():
    calls = [("bash", None), ("bash", "oops")]
    _, detail = tui._batch_summary("bash", calls)
    assert detail == "命令 · 2 条"


# ---- 超长文案截断（折叠行是一行，别被超长命令/description 撑成多行）----

def test_超长命令截断():
    _, detail = tui._batch_summary("bash", [("bash", {"command": "echo " + "x" * 200})])
    assert detail.startswith("命令 · echo ") and detail.endswith("…") and len(detail) < 80


def test_超长description截断():
    _, detail = tui._batch_summary("subagent", [("subagent", {"description": "d" * 200})])
    assert detail.startswith("启用子 agent · ") and detail.endswith("…") and len(detail) < 80


# ---- 显示口径：glob 摘要带 path（权限判的是 path，别只显 pattern 误导）----

def test_glob摘要带上搜索路径():
    assert tui._summarize_tool("glob", {"pattern": "src/**/*", "path": "."}) == "src/**/* in ."
    assert tui._summarize_tool("glob", {"pattern": "**/*.py"}) == "**/*.py"     # 无 path → 只显 pattern


# ---- resume 重渲：拒绝结果只显第一句（发给模型的全文不变）----

def test_拒绝结果预览只取第一句():
    denied = ("错误：用户拒绝执行工具 glob。不要重试或换工具绕过该操作；"
              "直接简短告诉用户你想做什么、为何需要，然后停下等用户指示。")
    assert tui._result_preview(denied) == "错误：用户拒绝执行工具 glob"      # 只第一句
    assert tui._result_preview("匹配 5 个文件\n第二行") == "匹配 5 个文件"     # 普通结果照常取首行
    assert tui._result_preview("") == ""
