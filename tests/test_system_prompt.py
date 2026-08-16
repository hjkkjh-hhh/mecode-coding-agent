"""系统提示词组装：工具工作流、按可用工具裁剪、项目说明加载、静态在前环境在后。"""
from mecode.system_prompt import (
    BASE,
    base_prompt,
    build_system_prompt,
    load_project_context,
)

def test_BASE_含全部工具的工作流():
    for kw in ("read_file", "edit_file", "write_file", "bash", "grep", "glob"):
        assert kw in BASE


def test_没禁用任何工具_与BASE一致():
    assert base_prompt() == BASE == base_prompt([])


def test_禁用web工具_联网那条整条消失():
    """工具被 MECODE_DISABLE_TOOLS 摘掉后，提示词还教模型用它 → 模型照调、拿回"没有这个工具"。
    实测无外网跑批 80 次，模型仍尝试 web_search 37 次，全部报错收场。"""
    p = base_prompt(["web_search", "web_fetch"])
    assert "web_search" not in p and "web_fetch" not in p
    assert "联网查资料" not in p
    assert "read_file" in p and "grep" in p                # 其余工作方式不受牵连


def test_禁用一半web工具_也整条不发():
    """"web_search 搜 → web_fetch 读全文"少一半就不成立，发出去是误导。"""
    assert "联网查资料" not in base_prompt(["web_fetch"])
    assert "联网查资料" not in base_prompt(["web_search"])


def test_禁用任务清单或子agent_各自那条消失():
    assert "规划复杂任务" not in base_prompt(["task_create"])
    assert "委派子 agent" not in base_prompt(["subagent"])
    assert "完成判据" in base_prompt(["task_create", "subagent"])   # 核心条目还在


def test_禁用核心工具_不影响其余条目():
    """核心条目不做条件化（禁用它们等于让 agent 没法干活，不为此保留半份提示词）。"""
    p = base_prompt(["read_file", "bash", "grep", "glob"])
    for kw in ("探索", "动手时机", "完成判据", "修改", "验证/执行", "行为准则"):
        assert kw in p


def test_build_system_prompt_默认自己读环境变量(monkeypatch):
    """调用方不必传：默认就按 MECODE_DISABLE_TOOLS 裁。踩过的坑——原先想按"注册表里有什么"
    过滤，但 subagent/task_* 是 Agent.__init__ 里注册的、比这里晚，会把那两条误删。"""
    monkeypatch.setenv("MECODE_DISABLE_TOOLS", "web_search,web_fetch")
    sp = build_system_prompt(project_context="")
    assert "web_search" not in sp
    assert "委派子 agent" in sp and "规划复杂任务" in sp     # 这两条禁不掉，必须还在
    monkeypatch.delenv("MECODE_DISABLE_TOOLS")
    assert "web_search" in build_system_prompt(project_context="")


def test_load_project_context_读到CLAUDE_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("项目规矩：用 4 空格缩进", encoding="utf-8")
    ctx = load_project_context(tmp_path)
    assert ctx is not None
    assert "用 4 空格缩进" in ctx and "CLAUDE.md" in ctx


def test_load_project_context_没有则None(tmp_path):
    assert load_project_context(tmp_path) is None


def test_build_顺序_静态在前环境在最后():
    sp = build_system_prompt(extra="额外X", project_context="项目Y")
    assert sp.index("编程助手") < sp.index("项目Y") < sp.index("额外X") < sp.index("# 环境信息")
    # 环境块确实是最后一段
    assert sp.rindex("# 环境信息") > sp.rindex("额外X")


def test_build_空串项目说明_不注入也不自动加载():
    sp = build_system_prompt(project_context="")
    assert "项目说明" not in sp
    assert "# 环境信息" in sp and "编程助手" in sp


def test_build_技能索引_内置mcp_install只留指针(tmp_path, monkeypatch):
    """MCP 指南收编成内置 skill：system prompt 只常驻技能索引（一行 name+description+路径），
    正文留在盘上，模型场景命中自己 read_file（渐进式披露）。"""
    from pathlib import Path
    import mecode.skills as sk
    # 隔离全局启停状态（~/.mecode/skills_state.json）：指向空临时文件 → 内置技能默认全启用，
    # 否则本机若停用了全部技能，索引段为空、本测试会假失败（与真机启停耦合）。
    monkeypatch.setattr(sk, "STATE_PATH", tmp_path / "skills_state.json")
    sp = build_system_prompt(project_context="")
    assert "# 可用技能" in sp and "mcp-install" in sp and "read_file" in sp   # 索引在
    assert "npx --version" not in sp and "mcpServers" not in sp               # 指南正文不常驻
    doc = sk.BUILTIN_DIR / "mcp-install" / "SKILL.md"
    assert doc.is_file()                                      # 索引指向的文件确实随包存在
    body = doc.read_text(encoding="utf-8")
    assert "mcpServers" in body and "npx --version" in body and "Ctrl+Q" in body
