"""agent 记忆：跨会话持久化"用户画像 / 协作反馈 / 项目上下文 / 外部引用"，又不占上下文。

借鉴 Claude Code 的 # auto memory：记忆 = 【文件 + 索引】。每条记忆一个 .md（带 name/description/type
头 + 正文），MEMORY.md 是索引（每条一行）。启动时把【索引】注入 system prompt——agent 由此知道有哪些
记忆、何时该写/读；要某条细节时再用 recall_memory 读具体文件。这样持久化又不污染上下文。

四类（type）：
  user      用户画像（角色/目标/偏好/知识）——据此调整怎么帮他。
  feedback  他给的协作指导（纠正 + 确认都记），正文带 **Why:** / **How to apply:**。
  project   项目上下文（在做什么/为什么/期限，代码/git 推不出来的），正文带 Why/How，相对日期转绝对。
  reference 外部资源指针（看板/频道/文档）。
不该记：能从代码/git/CLAUDE.md 读出来的、调试解法、纯当前会话的临时状态。

放项目级 ~/.mecode/projects/<slug>/memory/（跨会话、按项目；和 session 存储同根）。

两个【沙箱】工具（只动 memory_dir，安全 → 权限默认放行；强制格式比让模型手写可靠）：
  save_memory(name, type, description, content)  写/覆盖一条 + upsert 索引
  recall_memory(name)                            读一条
"""
from __future__ import annotations

import re
from pathlib import Path

from .tools import Tool

_NAME_RE = re.compile(r"[^0-9A-Za-z_-]")
INDEX_FILE = "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _safe_name(name: str) -> str:
    """名字清洗成纯文件名（防 ../ 穿越；只留字母数字_-），空则兜底。
    避开和索引文件 MEMORY.md 同名——Windows 不分大小写，否则 'memory' 会撞上索引。"""
    s = _NAME_RE.sub("_", name).strip("_") or "untitled"
    if s.lower() == Path(INDEX_FILE).stem.lower():
        s += "_mem"
    return s


def save_memory(memory_dir: Path, name: str, type: str, description: str, content: str) -> str:
    """写/覆盖一条记忆 <name>.md（frontmatter + 正文）并 upsert 索引。返回确认串。"""
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(name)
    type = type if type in MEMORY_TYPES else "project"
    (memory_dir / f"{safe}.md").write_text(
        f"---\nname: {safe}\ndescription: {description}\ntype: {type}\n---\n\n{content}\n",
        encoding="utf-8")
    _upsert_index(memory_dir, safe, description)
    return f"已记住「{safe}」（{type}）。"


def recall_memory(memory_dir: Path, name: str) -> str:
    """按名字读回一条记忆原文。容错：模型常漏掉 type 前缀（recall 'xiangtan-cs-sophomore' 实际存的是
    'user-xiangtan-cs-sophomore'）→ 精确没命中时按子串唯一匹配兜底；仍不唯一/没有则列出现有名字让它自纠。"""
    memory_dir = Path(memory_dir)
    safe = _safe_name(name)
    path = memory_dir / f"{safe}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    names = [p.stem for p in memory_dir.glob("*.md") if p.name != INDEX_FILE]
    near = [n for n in names if safe in n or n in safe]       # 漏前缀/部分名都能中
    if len(near) == 1:
        return (memory_dir / f"{near[0]}.md").read_text(encoding="utf-8")
    if names:
        return f"没有名为「{safe}」的记忆；现有记忆：{', '.join(names)}（用准确名字重新 recall）。"
    return "还没有任何记忆。"


def _upsert_index(memory_dir: Path, name: str, description: str) -> None:
    """在 MEMORY.md 里替换同名行或追加一行：'- [name](name.md) — description'。"""
    idx = memory_dir / INDEX_FILE
    line = f"- [{name}]({name}.md) — {description}"
    lines = idx.read_text(encoding="utf-8").splitlines() if idx.is_file() else ["# 记忆索引", ""]
    prefix = f"- [{name}]("
    for i, ln in enumerate(lines):
        if ln.startswith(prefix):
            lines[i] = line
            break
    else:
        lines.append(line)
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_index(memory_dir: Path) -> str:
    """读索引内容（注入 system prompt 用）；还没有记忆则返回空串。"""
    idx = Path(memory_dir) / INDEX_FILE
    return idx.read_text(encoding="utf-8").strip() if idx.is_file() else ""


MEMORY_INSTRUCTIONS = """\
# 记忆

你有跨会话的文件记忆（下方"当前记忆索引"列出已有的）。先会【用】，再会【写】——价值在用。

## 用（读 + 应用）
- 每轮看一眼记忆索引，相关的就用：user 记忆 → 按用户的角色/水平/偏好调整怎么解释、怎么帮他；
  feedback 记忆 → 照他给过的指导做，别让他纠正第二次；project 记忆 → 理解请求背后的动机；
  reference 记忆 → 知道去哪查。
- 索引的一句话钩子够用就直接用；要某条的具体内容，用 recall_memory(name) 读原文。
- 用户明确让你"想想/记得/回忆"时，必须 recall。
- 记忆可能过时：据它行动前（尤其它点名了某文件/函数/路径）先核对当前实际状态；冲突时信现状，
  并更新/删除那条过时记忆，而不是照旧执行。

## 写（save_memory）—— 持久化【对未来对话有用】的事，别塞进当前回复
四类 type：
- user：用户画像（角色/目标/偏好/知识），学到就记，据此调整怎么帮他。
- feedback：他给的协作指导（纠正"别那样"、确认"对就这么做"都记），正文带 **Why:** 和 **How to apply:**。
- project：项目上下文（谁在做什么/为什么/期限），代码或 git 推不出来的；带 Why/How，相对日期转绝对。
- reference：外部资源指针（看板/频道/文档及其用途）。
不要记：能从代码/git/CLAUDE.md 读出来的（结构/路径/约定/历史）、调试解法、纯当前会话的临时状态。
即使用户让你记这些，也先问"其中【意外/不显然】的是什么"，只记那部分。
写法：save_memory(name=短横线英文名, type=四类之一, description=一句话钩子, content=正文)；同名覆盖、
先查索引别重复。"""


def build_memory_prompt(memory_dir: Path) -> str:
    """拼"记忆指令 + 当前索引"，给 system prompt 注入。"""
    index = load_index(memory_dir) or "（还没有任何记忆）"
    return f"{MEMORY_INSTRUCTIONS}\n\n## 当前记忆索引\n{index}"


def memory_tools(memory_dir: Path) -> list[Tool]:
    """把 save_memory / recall_memory 包成绑定到本项目 memory_dir 的沙箱工具。"""
    md = Path(memory_dir)
    return [
        Tool(
            name="save_memory",
            description="把对未来对话有用的事持久化进文件记忆（用户画像/协作反馈/项目上下文/外部引用）。"
                        "同名覆盖。不要记能从代码/git 读出来的东西。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "短横线英文名，如 user-role、feedback-testing"},
                    "type": {"type": "string", "enum": list(MEMORY_TYPES),
                             "description": "user/feedback/project/reference"},
                    "description": {"type": "string", "description": "一句话钩子（索引里显示，用于将来判断相关性）"},
                    "content": {"type": "string",
                                "description": "正文；feedback/project 带 **Why:** 和 **How to apply:** 行"},
                },
                "required": ["name", "type", "description", "content"],
            },
            handler=lambda a: save_memory(md, a.get("name", ""), a.get("type", "project"),
                                          a.get("description", ""), a.get("content", "")),
        ),
        Tool(
            name="recall_memory",
            description="按名字读回一条记忆的原文（名字见 system prompt 里的记忆索引）。",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "记忆名（索引里的）"}},
                "required": ["name"],
            },
            handler=lambda a: recall_memory(md, a.get("name", "")),
            read_only=True,
        ),
    ]
