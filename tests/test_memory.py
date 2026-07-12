"""agent 记忆：文件 + 索引、沙箱工具、system prompt 注入。

固化的不变量：
- save 写 <name>.md（frontmatter+正文）并 upsert 索引；同名覆盖、索引不重复
- recall 读原文；缺失给提示
- 名字沙箱化（防 ../ 穿越）；type 非法回退 project
- build_memory_prompt 含指令 + 当前索引；memory_tools 的 handler 可用
"""
from mecode.memory import (
    build_memory_prompt, load_index, memory_tools, recall_memory, save_memory,
)


def test_save_写文件带frontmatter_并upsert索引(tmp_path):
    msg = save_memory(tmp_path, "user-role", "user", "用户是大三学生", "正文内容")
    assert "user-role" in msg
    f = tmp_path / "user-role.md"
    body = f.read_text(encoding="utf-8")
    assert "name: user-role" in body and "type: user" in body and "正文内容" in body
    idx = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [user-role](user-role.md) — 用户是大三学生" in idx


def test_同名覆盖_索引不重复(tmp_path):
    save_memory(tmp_path, "x", "project", "旧描述", "旧")
    save_memory(tmp_path, "x", "project", "新描述", "新")
    body = (tmp_path / "x.md").read_text(encoding="utf-8")
    assert "新" in body and "旧" not in body                         # 文件被覆盖
    idx = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert idx.count("[x](x.md)") == 1 and "新描述" in idx and "旧描述" not in idx   # 索引行替换


def test_recall读原文_缺失给提示(tmp_path):
    save_memory(tmp_path, "fact", "project", "d", "记住这个")
    assert "记住这个" in recall_memory(tmp_path, "fact")
    miss = recall_memory(tmp_path, "nope")
    assert "没有名为" in miss and "fact" in miss            # 列出现有名字让模型自纠


def test_recall容错_漏前缀也能命中(tmp_path):
    # 模型常漏掉 type 前缀：存的是 user-xiangtan-cs-sophomore，recall 漏了 user- 也能中
    save_memory(tmp_path, "user-xiangtan-cs-sophomore", "user", "湘大大二", "画像正文")
    assert "画像正文" in recall_memory(tmp_path, "xiangtan-cs-sophomore")
    # 但子串匹配到多条就不猜，列出来
    save_memory(tmp_path, "user-other-xiangtan", "user", "d", "x")
    amb = recall_memory(tmp_path, "xiangtan")
    assert "现有记忆" in amb


def test_名字沙箱_防穿越(tmp_path):
    save_memory(tmp_path, "../evil", "project", "d", "x")
    assert not (tmp_path.parent / "evil.md").exists()              # 没写到上层目录
    assert list(tmp_path.glob("*.md"))                             # 文件落在 memory_dir 内


def test_type非法回退project(tmp_path):
    save_memory(tmp_path, "n", "乱写的type", "d", "x")
    assert "type: project" in (tmp_path / "n.md").read_text(encoding="utf-8")


def test_build_memory_prompt_含指令与索引(tmp_path):
    assert "还没有任何记忆" in build_memory_prompt(tmp_path)         # 空时
    save_memory(tmp_path, "user-role", "user", "大三学生", "x")
    p = build_memory_prompt(tmp_path)
    assert "save_memory" in p and "user-role" in p                 # 指令 + 索引都在


def test_memory_tools_handler可用(tmp_path):
    tools = {t.name: t for t in memory_tools(tmp_path)}
    assert set(tools) == {"save_memory", "recall_memory"}
    tools["save_memory"].handler(
        {"name": "k", "type": "feedback", "description": "d", "content": "**Why:** 因为"})
    assert "因为" in tools["recall_memory"].handler({"name": "k"})
    assert load_index(tmp_path).count("[k]") == 1


def test_agent_调save_memory_默认放行不审批且真写入(tmp_path):
    from mecode.agent import Agent
    from mecode.events import Done, TextDelta, ToolCall, Usage
    from mecode.permission import PermissionPolicy
    from mecode.tools import ToolRegistry

    reg = ToolRegistry()
    for t in memory_tools(tmp_path):
        reg.register(t)

    class P:
        def __init__(self):
            self.n = 0

        def stream(self, messages, tools=None, should_stop=None):
            self.n += 1
            if self.n == 1:
                yield ToolCall(id="c1", name="save_memory", arguments={
                    "name": "u", "type": "user", "description": "d", "content": "用户画像内容"})
                yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
                yield Done(reason="tool_calls")
            else:
                yield TextDelta("记好了")
                yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
                yield Done(reason="stop")

    asked = []
    a = Agent(P(), reg, system_prompt="x",
              policy=PermissionPolicy(), ask_permission=lambda n, ar: asked.append(n) or "deny")
    list(a.run_turn("记住我"))
    assert asked == []                                       # save_memory 默认放行，没弹审批
    assert "用户画像内容" in recall_memory(tmp_path, "u")    # 真写进了记忆
