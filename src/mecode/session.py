"""会话存储：一个会话一个文件夹。transcript.jsonl 是【唯一真相】，同时承载两种读法——
"人类视觉历史"（全量）和"AI 运行时工作记忆"（fold 后的压缩上下文）。

  transcript.jsonl  —— 只追加、永不重写：每条消息落定就追加；压缩只往末尾加一条 compaction
                       marker（带 summary + 压缩后镜像）。两种读法都从它来：
                       · 人类全量历史 = 顺读所有消息行（无视 marker）。
                       · AI 工作记忆  = load_messages() fold 到最后一个 marker（镜像 + 其后消息）。
                       续接 banner 的"完整存档指针"就指它——模型要被压掉的旧细节时 read_file 回查。
  tool_outputs/     —— 工具超长输出外置：<ns>-<tool>.txt（超预算才存，截断标记里留指针）。
  session.json      —— 会话头（id/cwd/标题/时间/轮数），给 /rl /rs 列会话用（后续）。
  notes/ scratch/   —— 阶段性记忆 md / agent 生成的调试脚本（后续接记忆与 B/C）。

布局：<root>/projects/<项目slug>/sessions/<uuid>/...
  root 默认 ~/.mecode；slug = Path(cwd).resolve() 里每个非 [0-9A-Za-z] 字符 → '-'（CC 的算法，
  如 e:\\实验室\\claw-code → E------claw-code）；uuid 纯 uuid4。

设计取舍：SessionStore 走"注入"——agent/消费端构造它并传进 Agent，测试可传临时 root、
不碰真实主目录；和 compact 的依赖注入一个味，保持各层可测。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

_SLUG_RE = re.compile(r"[^0-9A-Za-z]")
_TOOL_RE = re.compile(r"[^0-9A-Za-z_]")


def project_slug(cwd: str | Path) -> str:
    """把 cwd 绝对路径映射成文件夹名：每个非字母数字字符→'-'（同 Claude Code）。
    用 resolve() 取绝对路径，保证同一项目无论从哪进都落到同一 slug。"""
    return _SLUG_RE.sub("-", str(Path(cwd).resolve()))


class SessionStore:
    def __init__(self, root: str | Path | None = None,
                 cwd: str | Path | None = None,
                 session_id: str | None = None) -> None:
        self.root = Path(root).expanduser() if root else Path("~/.mecode").expanduser()
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.session_id = session_id or str(uuid.uuid4())
        self.dir = (self.root / "projects" / project_slug(self.cwd)
                    / "sessions" / self.session_id)

    # --- 路径（懒创建：写时才建目录，纯读路径不产生空文件夹）---
    @property
    def project_dir(self) -> Path:
        return self.root / "projects" / project_slug(self.cwd)   # 项目级（跨会话共享，如权限规则）

    @property
    def permissions_path(self) -> Path:
        return self.project_dir / "permissions.json"             # "总是允许"的工具，项目级持久

    @property
    def memory_dir(self) -> Path:
        return self.project_dir / "memory"                       # agent 跨会话记忆（.md + MEMORY.md 索引）

    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript.jsonl"

    @property
    def session_json(self) -> Path:
        return self.dir / "session.json"

    @property
    def tasks_path(self) -> Path:
        return self.dir / "tasks.json"                           # 会话级任务清单（task_create/update 持久化，resume 还原侧栏）

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.md"                              # 计划模式 exit_plan 落盘的计划正文（会话级，最新覆盖）

    @property
    def tool_outputs_dir(self) -> Path:
        return self.dir / "tool_outputs"

    @property
    def notes_dir(self) -> Path:
        return self.dir / "notes"

    @property
    def scratch_dir(self) -> Path:
        return self.dir / "scratch"

    # --- 人类视觉历史：只追加 ---
    def append_transcript(self, entry: dict) -> None:
        """追加一条记录到 transcript.jsonl（一行一条 JSON）。
        entry 可以是一条消息（带 role），或一条 marker（带 type，如压缩）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def mark_compaction(self, **fields) -> None:
        """压缩发生时往日志末尾加一条 marker（不重写已有行）。
        全量历史仍在 marker 之前的消息行里，故 banner 指针指 transcript.jsonl 即可。"""
        self.append_transcript({"type": "compaction", "ts": time.time_ns(), **fields})

    # --- resume：从全量日志 fold 出"上次的工作记忆" ---
    def load_messages(self) -> list[dict] | None:
        """重建上次关闭时的 live messages[]（=发给 API 的上下文），没有日志（新会话）返回 None。

        工作记忆 = 最后一个 compaction marker 的镜像（image）+ 其后的所有消息；没压缩过则是全部消息。
        【逆扫】：工作记忆有界（压缩钉在 ~一个上下文窗口），但 transcript 无界（全量人类历史）。
        从尾往前读，撞到第一个 marker（=文件里最后那个）就取镜像、停——压缩点之前那一大坨不必解析，
        开销从 O(全量历史) 降到 O(上下文窗口)。省的主要是逐行 json.loads（bulk 读本就便宜）；
        用文本模式 splitlines()+reversed() 不会切断 UTF-8（按字节倒读才有那坑）。
        （再极致可从文件尾按块 seek 倒读、连 bulk 读都省，但要处理字节边界/半行，收益边际，暂不做。）"""
        if not self.transcript_path.is_file():
            return None
        post: list[dict] = []                  # marker 之后的消息（逆序收集，返回前翻正）
        for line in reversed(self.transcript_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("type") == "compaction":
                return list(entry.get("image", [])) + list(reversed(post))
            if "role" in entry:
                post.append(entry)
        return list(reversed(post))            # 没 marker → 全部消息（原序）

    def read_transcript_messages(self) -> list[dict]:
        """另一种读法（和 load_messages 的 fold 相对）：顺读 transcript 里【全部对话消息】、无视
        compaction marker —— 这是"人类视觉历史"，给 UI 重渲完整滚动条用（resume 时还原旧对话）。"""
        if not self.transcript_path.is_file():
            return []
        out: list[dict] = []
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if "role" in entry:                # 消息行（marker 无 role，自然跳过）
                out.append(entry)
        return out

    # --- 会话头：给 /rl /rs 列会话用（不影响 resume，resume 只认 transcript） ---
    def write_header(self, *, title: str | None = None, mode: str | None = None,
                     context_tokens: int | None = None,
                     thinking_on: bool | None = None, effort: str | None = None) -> None:
        """写/更新 session.json 头：首次落 id/cwd/slug/created_at，每次刷新 updated_at，
        title 只设一次（首条用户消息）；mode 给了就覆盖为当前运行模式；context_tokens 给了就覆盖为
        最终上下文 token 数（供 resume 立即显示用量、不用等首条消息）；thinking_on/effort 给了就覆盖为
        当前思考运行态（resume 恢复）。只覆盖给了的字段，故各处分别调用不会互相清掉。
        这是会话清单的"封面"，与 transcript 解耦。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        meta: dict = {}
        if self.session_json.is_file():
            meta = json.loads(self.session_json.read_text(encoding="utf-8"))
        now = time.time()
        meta.setdefault("session_id", self.session_id)
        meta.setdefault("cwd", str(self.cwd))
        meta.setdefault("slug", project_slug(self.cwd))
        meta.setdefault("created_at", now)
        meta["updated_at"] = now
        if title and not meta.get("title"):
            meta["title"] = title[:80]
        if mode is not None:
            meta["mode"] = mode                    # 当前模式随会话头落盘（每轮覆盖），resume 从这里恢复
        if context_tokens is not None:
            meta["context_tokens"] = context_tokens   # 最终上下文 token 数：resume 立即显示，不用等首条消息
        if thinking_on is not None:
            meta["thinking_on"] = thinking_on         # 思考开关运行态，resume 恢复
        if effort is not None:
            meta["effort"] = effort                   # 思考深度档，resume 恢复
        self.session_json.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _patch_header(self, **fields) -> dict:
        """读-改-写会话头。读不动就当空 dict 起一份——一个坏文件不该让改名/归档整个失败。"""
        meta: dict = {}
        if self.session_json.is_file():
            try:
                meta = json.loads(self.session_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta.setdefault("session_id", self.session_id)
        meta.setdefault("cwd", str(self.cwd))
        meta.setdefault("slug", project_slug(self.cwd))
        meta.update(fields)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        return meta

    def set_title(self, title: str) -> str:
        """用户手动改名。

        和 write_header(title=...) 分开是【故意的】：那条只在没有标题时才写（首条用户消息自动命名），
        正是它保证了自动命名不会覆盖用户改过的名字。改名要的是"无条件覆盖"，
        两种语义塞进一个参数，迟早有一边被写错。
        改完之后自动命名那条也不会再动它了——它看到 title 已存在就跳过。
        """
        title = (title or "").strip()[:80]
        if not title:
            raise ValueError("标题不能为空")
        self._patch_header(title=title)
        return title

    def set_archived(self, archived: bool) -> None:
        """归档 / 取消归档。只是给会话头打个标记，transcript 一个字都不动——
        归档 = 从清单里收起来，不是删除，随时能翻回来。"""
        self._patch_header(archived=bool(archived))

    def fork(self, title: str = "") -> "SessionStore":
        """把本会话整个复制成一个新会话（新 uuid），返回新 store。

        【整目录复制】而不是只拷 transcript：外置的大工具输出在 tool_outputs/ 里，
        不跟过去的话新会话里那些"看全文"全打不开——读取接口把路径限死在【本会话】目录内，
        指向源会话的绝对路径会被闸直接挡下。
        复制完还要把 transcript 里写死的旧目录路径改写成新目录，那些指针才对得上。

        用途：想从当前这段对话岔出去试另一条路，又不想弄脏原来那条。
        """
        import shutil
        new = SessionStore(root=self.root, cwd=self.cwd)
        if self.dir.is_dir():
            shutil.copytree(self.dir, new.dir, dirs_exist_ok=True)
        # 外置输出的路径是【复制前就写死在 transcript 里的绝对路径】，指向旧目录。
        # 正斜杠和反斜杠两种形态都要换：Windows 上两种写法都可能落进去。
        if new.transcript_path.is_file():
            text = new.transcript_path.read_text(encoding="utf-8")
            for old_s, new_s in ((self.dir.as_posix(), new.dir.as_posix()),
                                 (str(self.dir), str(new.dir))):
                text = text.replace(old_s, new_s)
            new.transcript_path.write_text(text, encoding="utf-8", newline="")
        base = title.strip() or ""
        if not base:
            try:
                base = json.loads(self.session_json.read_text(encoding="utf-8")).get("title", "")
            except (json.JSONDecodeError, OSError):
                base = ""
        now = time.time()
        new._patch_header(session_id=new.session_id, title=(base or "未命名")[:70] + "（分叉）",
                          created_at=now, updated_at=now, archived=False,
                          forked_from=self.session_id)
        return new

    @classmethod
    def list_sessions(cls, root: str | Path | None = None,
                      cwd: str | Path | None = None) -> list[dict]:
        """列出本项目（同 slug）下的所有会话头，按 updated_at 倒序（最近的在前）。
        /rl 取第 0 个，/rs 列给用户挑。读不动的会话头跳过，不让一个坏文件搅黄整张清单。"""
        root = Path(root).expanduser() if root else Path("~/.mecode").expanduser()
        cwd = cwd if cwd is not None else Path.cwd()
        base = root / "projects" / project_slug(cwd) / "sessions"
        if not base.is_dir():
            return []
        out: list[dict] = []
        for d in base.iterdir():
            header = d / "session.json"
            if not header.is_file():
                continue
            try:
                meta = json.loads(header.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            meta.setdefault("session_id", d.name)
            out.append(meta)
        out.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
        return out

    @classmethod
    def list_projects(cls, root: str | Path | None = None) -> list[dict]:
        """所有【工作区】及各自的会话头，按最近活跃排序。

        和 list_sessions 的分工：那个只看当前 cwd 对应的一个项目（resume 用）；
        这个横扫 projects/ 下的全部（侧栏按文件夹分组用）。

        工作区路径从【会话头里的 cwd】取，不从目录名反推——目录名是 slug（路径压平成
        C--Users-x-proj 这种），压平是有损的，反推不回原路径。
        """
        root = Path(root).expanduser() if root else Path("~/.mecode").expanduser()
        base = root / "projects"
        out: list[dict] = []
        if not base.is_dir():
            return out
        for proj in base.iterdir():
            sdir = proj / "sessions"
            if not sdir.is_dir():
                continue
            metas: list[dict] = []
            for d in sdir.iterdir():
                header = d / "session.json"
                if not header.is_file():
                    continue
                try:
                    meta = json.loads(header.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                meta.setdefault("session_id", d.name)
                metas.append(meta)
            if not metas:
                continue
            metas.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
            cwd = metas[0].get("cwd", "")
            out.append({"slug": proj.name, "cwd": cwd,
                        "name": Path(cwd).name if cwd else proj.name,
                        "updated_at": metas[0].get("updated_at", 0),
                        "sessions": metas})
        out.sort(key=lambda p: p.get("updated_at", 0), reverse=True)
        return out

    # --- 工具权限：项目级规则（跨会话共享），格式 {tool: {"allow":[spec...], "deny":[...]}} ---
    def load_permissions(self) -> dict:
        """读项目级 permissions.json（只存用户加的 spec，不含内置默认）；没有则空 dict。"""
        if self.permissions_path.is_file():
            return json.loads(self.permissions_path.read_text(encoding="utf-8"))
        return {}

    def add_permission(self, tool: str, decision: str, spec: str) -> None:
        """把一条 spec 加进项目级某工具的某决策表并落盘（用户选"总是允许"时 decision=allow）。"""
        self.project_dir.mkdir(parents=True, exist_ok=True)
        data = self.load_permissions()
        lst = data.setdefault(tool, {}).setdefault(decision, [])
        if spec not in lst:
            lst.append(spec)
        self.permissions_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 工具超长输出外置 ---
    def offload_tool_output(self, tool: str, content: str) -> str:
        """把工具的完整输出存盘，返回 posix 路径（进截断标记给模型，bash/read_file 都认）。
        文件名带纳秒时间戳保证唯一 + 工具名（清洗成安全字符）便于人肉辨认。"""
        self.tool_outputs_dir.mkdir(parents=True, exist_ok=True)
        safe = _TOOL_RE.sub("_", tool)[:40] or "tool"
        path = self.tool_outputs_dir / f"{time.time_ns()}-{safe}.txt"
        path.write_text(content, encoding="utf-8")
        return path.as_posix()
