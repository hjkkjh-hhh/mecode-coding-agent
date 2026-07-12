"""Skill 系统：按需加载的领域流程/知识包（对齐 Claude Code / Codex 的 skill 机制）。

Skill = 一个文件夹里的 SKILL.md：frontmatter（name/description）+ 正文（给模型的完整流程指令）。
核心机制是【渐进式披露】——system prompt 只常驻一张索引（每个 skill 一行 name+description+路径，
几十 token/个），全文留在盘上：

  触发路径①（模型自动）：模型看索引判断场景命中 → 自己 read_file 读全文 → 照着做。
    复用现有工具，无新机制；全文只是一次工具结果躺在历史里，随对话推进/压缩自然衰减。
  触发路径②（用户显式）：/skill 面板选中 → 全文以 user+<system-reminder> 一次性注入、起一轮。
    确定性，不依赖模型"愿不愿意"读。

【没有"使用中"状态、没有退出机制】（CC/Codex 皆如此）：注入过的内容就是历史里的一段文本，
不重复注入、不需要收回。长任务压缩后索引仍在（system prompt 不被压），模型可按需重读全文。

发现目录（项目级同名覆盖用户级）：
  <项目>/.mecode/skills/<名>/SKILL.md    项目级
  ~/.mecode/skills/<名>/SKILL.md         用户级
  src/mecode/skills_builtin/<名>/SKILL.md  内置（随包分发，如 mcp-install）

启停：状态存 ~/.mecode/skills_state.json（{"disabled": [名...]}，默认全启用）。
停用 = 不进索引（模型不知道存在）+ 面板选中提示先启用。改状态【新会话生效】——
当前会话的 system prompt 不动（不击穿 prompt 缓存、也不产生"模型记得旧列表"的歧义）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"
BUILTIN_DIR = Path(__file__).resolve().parent / "skills_builtin"   # 内置（随包分发）
USER_SKILLS_DIR = Path("~/.mecode/skills").expanduser()            # 用户级（所有项目）
STATE_PATH = Path("~/.mecode/skills_state.json").expanduser()      # 启停状态（禁用名单）
INDEX_BUDGET = 2000     # 索引段字符预算（Codex 用 2% 窗口/8K 字符的思路）：超了截断+提示


@dataclass(frozen=True)
class Skill:
    """一个已发现的 skill。path 指 SKILL.md 本体（索引里给模型的就是这个路径）。"""
    name: str
    description: str
    path: Path
    source: str        # "项目" / "用户" / "内置"（面板展示用）
    enabled: bool = True

    def body(self) -> str:
        """读正文（frontmatter 之后的部分）——用户显式触发时注入用。"""
        text = self.path.read_text(encoding="utf-8", errors="replace")
        _, body = _split_frontmatter(text)
        return body.strip()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """把 '---\\nkey: value...\\n---\\n正文' 拆成 (头字典, 正文)。无 frontmatter → ({}, 全文)。
    手写解析（同 memory.py 思路），不引 yaml 依赖。除单行 `key: value` 外，还认 YAML 多行块
    `key: |` / `key: >`（后续缩进行拼成一段）——模型/别人写的 skill 常用这种写法，不认会把
    description 解析成一根竖线。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    lines = parts[1].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v in ("|", ">", "|-", ">-"):          # YAML 多行块：吃掉后面所有缩进行
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                block.append(lines[i].strip())
                i += 1
            v = " ".join(s for s in block if s)   # 折成一行（索引/面板里 description 本就该是一行）
        meta[k.strip()] = v
    return meta, parts[2]


def _scan_dir(root: Path, source: str) -> list[Skill]:
    """扫一个根目录下的 <名>/SKILL.md；头部缺 name 用文件夹名兜底，缺 description 跳过
    （没有描述模型无从判断何时用，进索引只是噪音）。"""
    out: list[Skill] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        f = d / SKILL_FILE
        if not (d.is_dir() and f.is_file()):
            continue
        try:
            meta, _ = _split_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        desc = meta.get("description", "").strip()
        if not desc:
            continue
        out.append(Skill(name=meta.get("name", "").strip() or d.name,
                         description=desc, path=f, source=source))
    return out


def _load_disabled() -> set[str]:
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("disabled", []))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return set()          # Attr/Type：文件被手改成数组/字符串等非 dict 形态 → 同"坏文件"，当空处理


def set_enabled(name: str, enabled: bool) -> None:
    """改一个 skill 的启停（写禁用名单）。新会话生效——调用方（TUI 面板）负责向用户提示这点。"""
    disabled = _load_disabled()
    (disabled.discard if enabled else disabled.add)(name)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def discover_skills(project_root: Path | None = None) -> list[Skill]:
    """发现所有 skill：项目级 > 用户级 > 内置（同名高优先级覆盖），带上启停状态。
    顺序 = 项目级在前（对当前项目更相关，索引截断时优先保住）。"""
    roots = []
    if project_root is not None:
        roots.append((Path(project_root) / ".mecode" / "skills", "项目"))
    roots += [(USER_SKILLS_DIR, "用户"), (BUILTIN_DIR, "内置")]
    disabled = _load_disabled()
    seen: set[str] = set()
    out: list[Skill] = []
    for root, source in roots:
        for s in _scan_dir(root, source):
            if s.name in seen:
                continue
            seen.add(s.name)
            out.append(Skill(s.name, s.description, s.path, s.source,
                             enabled=s.name not in disabled))
    return out


def build_skills_prompt(skills: list[Skill]) -> str:
    """拼技能索引段（system prompt 注入用）：只含启用的，每行 name+description+全文路径。
    没有启用的 skill → 返回空串（整段省略，不占一个字）。超预算按序截断+提示。"""
    lines = [f"- {s.name}: {s.description} → {s.path.as_posix()}"
             for s in skills if s.enabled]
    if not lines:
        return ""
    head = ("# 技能\n\n"
            "## 怎么用\n"
            "- 技能索引见下方列表，每行 = 名称：适用场景 → 完整流程文件路径。\n"
            "- 遇到匹配场景时，【先用 read_file 读该文件拿到完整流程再照着执行】，别凭记忆做；"
            "用户也可通过 /skill 面板手动触发。\n"
            "- 技能可能自带配套文件（脚本、参考文档），在 SKILL.md 旁边的同一个文件夹里。"
            "正文提到 scripts/xx.py 这类文件时，真实位置 = 把路径里的 SKILL.md 换成它——"
            "例：技能在 C:/foo/skills/demo/SKILL.md，正文说\"运行 scripts/gen.py\"，"
            "实际要运行的是 C:/foo/skills/demo/scripts/gen.py，别拿当前工作目录去找。\n\n"
            "## 可用技能列表")
    body = ""
    for i, ln in enumerate(lines):
        if len(body) + len(ln) > INDEX_BUDGET:   # 总预算（所有行累计），不是每个 skill 的
            dirs = " 、".join(p.as_posix() for p in (USER_SKILLS_DIR, BUILTIN_DIR))
            body += (f"\n（超出索引预算，还有 {len(lines) - i} 个技能未列出；"
                     f"需要时可自己 glob 技能目录发现它们：{dirs}，"
                     f"以及项目下 .mecode/skills/，每个技能是 <名>/SKILL.md）")
            break
        body += "\n" + ln
    return head + body


def validate_skill(skill_dir: Path | str) -> str:
    """校验一个 skill 文件夹是否合法（skill-install 装完/写完后自检用）。
    和发现逻辑（_scan_dir）同源判定——校验通过 = 一定能被 discover_skills 发现。
    返回 "ok" 或以"不合法："开头的原因（给模型看的，直接可读）。"""
    d = Path(skill_dir)
    f = d / SKILL_FILE
    if not d.is_dir():
        return f"不合法：{d.as_posix()} 不是文件夹"
    if not f.is_file():
        return f"不合法：缺 {SKILL_FILE}（必须叫这个名字、在文件夹根下）"
    text = f.read_text(encoding="utf-8", errors="replace")
    meta, body = _split_frontmatter(text)
    if not meta:
        return "不合法：SKILL.md 开头缺 YAML frontmatter（--- 包起来的 name/description）"
    if not meta.get("description", "").strip():
        return "不合法：frontmatter 缺 description（它是模型判断何时用的唯一依据，必填）"
    if len(meta.get("description", "")) > 200:
        return "不合法：description 超 200 字符——它是索引里的一行广告，写'干什么+何时用'即可"
    name = meta.get("name", "").strip() or d.name
    if len(name) > 64:
        return "不合法：name 超 64 字符"
    if not body.strip():
        return "不合法：正文为空（frontmatter 之后要有给模型执行的流程指令）"
    return "ok"


def skill_reminder(skill: Skill) -> str:
    """用户从 /skill 面板显式触发时注入的内容（包 <system-reminder> 由调用方做，
    与模式提示/后台事件同一约定）。一次性注入，进历史不重发。"""
    return (f"用户通过 /skill 面板启用了技能「{skill.name}」，本轮请严格按下面的流程执行：\n\n"
            f"{skill.body()}")
