"""任务清单：把"规划/跟踪复杂任务"做成结构化任务表，模型用 4 个工具增改查。

和后台任务（background.py）是【两套】系统、各自独立的 id 空间：
  后台任务 —— 跑进程（dev server / 构建 / 测试），check/kill；对应 CC 的 TaskOutput/TaskStop。
  任务清单 —— 规划与跟踪（本文件），对应 CC 的 TaskCreate/TaskUpdate/TaskGet/TaskList。
（CC 已把两者收敛进同一 id 空间；mecode 等多 agent 落地后再考虑收敛，现在分开。）

分层：Task 是"名词"（一条任务的状态），4 个工具是"动词"（对它增改查），
中间 TaskManager 持有全部 Task、分配 id、维护依赖镜像边、持久化、通知 UI。
create 只设"出生即定、属于任务自身"的字段；status/owner 随时间变、牵扯别的 agent，
只能经 update 改——故 update 比 create 多 status/owner。

依赖（blocked_by/blocks）——【已整体注释停用】（2026-07）：
  字段与"create 不设依赖、update 连边"的拆分均抄自 CC 的任务工具（TaskCreate/TaskUpdate）。
  CC 里它们的消费方是 Agent Teams（flag 后的实验性多 agent 模式）：队友按
  "pending + 无 owner + blockedBy 为空"从共享清单拉活，依赖边即调度依据。
  mecode 没有该模式——多 agent 走 workflow 的 after 边、不读任务清单——依赖边在这里
  只有展示作用，故整体注释掉（Task 两字段 / update 的 add_* / _add_edge /
  delete 的清边 / _open_blockers / _fmt_row·task_get·侧栏的 🔒 渲染 / 工具 schema）。
  将来若做"多 agent 抢活"再解注释恢复；原设计要点一并留在这里：
  边走【推导式】而非【销毁式】——完成任务【不删边】，"B 还被挡着吗" = B.blocked_by 里
  是否还有【未完成】的任务；唯一真正删边的时机是任务 deleted（节点消失，清悬空边）；
  blocked_by/blocks 互为反向镜像，"能开始吗""解锁了谁"都不必扫全表。

持久化：<session>/tasks.json（{next_id, tasks:[...]}），会话级旁路文件——压缩压不掉、
  resume 还原侧栏。无 store（测试/无盘）则只在内存。线程安全（worker 改、UI 读）。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable

from .tools import Tool

_STATUSES = ("pending", "in_progress", "completed")
_ICON = {"pending": "☐", "in_progress": "▶", "completed": "✓"}


@dataclass
class Task:
    id: str
    subject: str                                          # 祈使句标题
    description: str = ""                                 # 具体要做什么（task_get 读全需求时看这个）
    active_form: str = ""                                 # 进行时，in_progress 时展示（缺省回退 subject）
    status: str = "pending"                               # pending / in_progress / completed
    owner: str = ""                                       # 归属 agent 名，空=未认领（多 agent 用）
    # 依赖功能已停用（见模块注释"依赖"段）：
    # blocked_by: list[str] = field(default_factory=list)  # 必须先完成的任务 id
    # blocks: list[str] = field(default_factory=list)      # 被本任务挡住的任务 id
    metadata: dict = field(default_factory=dict)


class TaskManager:
    """管理任务清单：create/update/delete 增改，get/list/summaries 读，render_reminder 给模型注入快照。
    线程安全（worker 线程经工具改、UI 线程读渲侧栏）。每次变更落 tasks.json + 回调 on_change 刷 UI。"""

    def __init__(self, store=None, on_change: Callable[[], None] | None = None) -> None:
        self.store = store                                # SessionStore | None：持久化到 store.tasks_path
        self.on_change = on_change                        # 变更回调（TUI 注入：刷侧栏）；锁外调用
        self._tasks: dict[str, Task] = {}                 # id → Task，dict 保留插入序（= id 序）
        self._next_id = 1
        self._lock = threading.Lock()
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        """构造时从 tasks.json 还原（resume 时把上次清单读回内存 → 侧栏复原）。无 store / 文件坏则空。"""
        if self.store is None or not self.store.tasks_path.is_file():
            return
        try:
            data = json.loads(self.store.tasks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._next_id = int(data.get("next_id", 1))
        for d in data.get("tasks", []):
            t = Task(
                id=str(d["id"]), subject=d.get("subject", ""),
                description=d.get("description", ""), active_form=d.get("active_form", ""),
                status=d.get("status", "pending"), owner=d.get("owner", ""),
                # blocked_by=[str(x) for x in d.get("blocked_by", [])],   # 依赖已停用；旧盘文件里的键被忽略
                # blocks=[str(x) for x in d.get("blocks", [])],
                metadata=dict(d.get("metadata", {})),
            )
            self._tasks[t.id] = t

    def _save(self) -> None:
        """落盘（调用方已持锁）。无 store 不落盘（测试/无盘模式只在内存）。"""
        if self.store is None:
            return
        self.store.tasks_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"next_id": self._next_id, "tasks": [asdict(t) for t in self._tasks.values()]}
        self.store.tasks_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # ---- 增改（动词） ----
    def create(self, subject: str, description: str = "", active_form: str = "",
               metadata: dict | None = None) -> Task:
        """建一条 pending 任务，分配自增 id 并返回。出生即 pending——status 不在此设（见模块注释）。"""
        with self._lock:
            tid = str(self._next_id)
            self._next_id += 1
            task = Task(id=tid, subject=subject, description=description,
                        active_form=active_form, metadata=dict(metadata or {}))
            self._tasks[tid] = task
            self._save()
        self._notify()
        return task

    def update(self, task_id: str, *, status: str | None = None, subject: str | None = None,
               description: str | None = None, active_form: str | None = None,
               owner: str | None = None,
               # add_blocks: list | None = None, add_blocked_by: list | None = None,   # 依赖已停用
               metadata: dict | None = None) -> tuple[Task | None, list[str]]:
        """patch 单条任务，返回 (task, 警告列表)；任务不存在返回 (None, [])。
        status=deleted 不走这里（删除走 delete）。"""
        warnings: list[str] = []
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None, []
            if status is not None:
                if status in _STATUSES:
                    task.status = status
                else:
                    warnings.append(f"未知状态 {status!r}（忽略；只接受 pending/in_progress/completed）")
            if subject is not None:
                task.subject = subject
            if description is not None:
                task.description = description
            if active_form is not None:
                task.active_form = active_form
            if owner is not None:
                task.owner = owner
            if metadata:
                for k, v in metadata.items():
                    if v is None:
                        task.metadata.pop(k, None)
                    else:
                        task.metadata[k] = v
            # 依赖已停用：
            # for bid in (add_blocked_by or []):            # 本任务依赖 bid（bid 先完成）
            #     w = self._add_edge(blocked=task_id, blocker=str(bid))
            #     if w:
            #         warnings.append(w)
            # for bid in (add_blocks or []):                # 本任务挡住 bid（本任务先完成）
            #     w = self._add_edge(blocked=str(bid), blocker=task_id)
            #     if w:
            #         warnings.append(w)
            self._save()
        self._notify()
        return task, warnings

    # 依赖已停用（见模块注释"依赖"段）：
    # def _add_edge(self, *, blocked: str, blocker: str) -> str | None:
    #     """加一条依赖边：blocked 依赖 blocker（blocker 必须先完成）。维护双向镜像。持锁内调用。
    #     引用不存在的 id 或自依赖则不建边、返回一句警告。"""
    #     if blocked == blocker:
    #         return f"#{blocked} 不能依赖自己（已忽略）"
    #     b, k = self._tasks.get(blocked), self._tasks.get(blocker)
    #     if b is None or k is None:
    #         return f"#{blocked if b is None else blocker} 不存在（该依赖未建立）"
    #     if blocker not in b.blocked_by:
    #         b.blocked_by.append(blocker)
    #     if blocked not in k.blocks:
    #         k.blocks.append(blocked)
    #     return None

    def delete(self, task_id: str) -> bool:
        """删除任务（status=deleted 的落点）。返回是否删到。"""
        with self._lock:
            if self._tasks.pop(task_id, None) is None:
                return False
            # 依赖已停用（原为唯一真正删边的时机：节点消失 → 清悬空边）：
            # for t in self._tasks.values():
            #     if task_id in t.blocked_by:
            #         t.blocked_by.remove(task_id)
            #     if task_id in t.blocks:
            #         t.blocks.remove(task_id)
            self._save()
        self._notify()
        return True

    # ---- 读（名词） ----
    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._tasks

    # 依赖已停用（见模块注释"依赖"段）：
    # def _open_blockers(self, task: Task) -> list[str]:
    #     """持锁内：blocked_by 里【还没完成】的——已 completed 的当锁已解开、不计入。"""
    #     return [bid for bid in task.blocked_by
    #             if (b := self._tasks.get(bid)) is not None and b.status != "completed"]

    def summaries(self) -> list[dict]:
        """所有任务的摘要（一次锁内算齐，渲侧栏 / task_list / reminder 共用，口径一致）。"""
        with self._lock:
            return [{"id": t.id, "subject": t.subject, "status": t.status, "owner": t.owner,
                     "active_form": t.active_form,
                     # "open_blockers": self._open_blockers(t), "blocks": list(t.blocks),  # 依赖已停用
                     } for t in self._tasks.values()]

    def render_reminder(self) -> str:
        """给模型注入的紧凑快照（一行一任务）；空清单返回空串。压缩后补一发，确保模型仍看得见清单。"""
        rows = self.summaries()
        if not rows:
            return ""
        lines = [_fmt_row(r) for r in rows]
        done = sum(1 for r in rows if r["status"] == "completed")
        return f"[当前任务清单 · 共 {len(rows)} 完成 {done}]\n" + "\n".join(lines)


def _fmt_row(r: dict) -> str:
    """一条任务的单行渲染（reminder / task_list 共用）：图标 #id 标题（active_form）@owner。"""
    line = f"{_ICON.get(r['status'], '☐')} #{r['id']} {r['subject']}"
    if r["status"] == "in_progress" and r["active_form"]:
        line += f"（{r['active_form']}）"
    if r["owner"]:
        line += f" @{r['owner']}"
    # if r["open_blockers"]:                                              # 依赖已停用
    #     line += " 🔒待 " + ", ".join("#" + b for b in r["open_blockers"])
    return line


def task_tools(mgr: TaskManager) -> list[Tool]:
    """AI 可调的 4 个任务清单工具（闭包绑定到本会话的 mgr，和 background_tools 一个套路）：
    task_create（建）/ task_update（改状态·依赖·归属，含删）/ task_get（读一条全文）/ task_list（列全部摘要）。"""

    def _create(args: dict) -> str:
        subject = (args.get("subject") or "").strip()
        if not subject:
            return "错误：subject 不能为空"
        task = mgr.create(subject, args.get("description", "") or "",
                          args.get("active_form", "") or "", args.get("metadata"))
        return f"已创建任务 #{task.id}：{task.subject}（status=pending）"

    def _update(args: dict) -> str:
        tid = args.get("task_id")
        if tid is None:
            return "错误：task_id 必填（来自 task_create 返回的 #编号；用 task_list 可查看现有任务）"
        tid = str(tid)
        if args.get("status") == "deleted":
            return f"已删除任务 #{tid}" if mgr.delete(tid) else f"没有任务 #{tid}（用 task_list 查看）"
        task, warnings = mgr.update(
            tid, status=args.get("status"), subject=args.get("subject"),
            description=args.get("description"), active_form=args.get("active_form"),
            owner=args.get("owner"), metadata=args.get("metadata"))
            # 依赖已停用：add_blocks=args.get("add_blocks"), add_blocked_by=args.get("add_blocked_by")
        if task is None:
            return f"没有任务 #{tid}（用 task_list 查看现有任务）"
        msg = f"已更新任务 #{task.id}（status={task.status}）"
        if warnings:
            msg += "；注意：" + "；".join(warnings)
        return msg

    def _get(args: dict) -> str:
        tid = args.get("task_id")
        if tid is None:
            return "错误：task_id 必填"
        task = mgr.get(str(tid))
        if task is None:
            return f"没有任务 #{tid}（用 task_list 查看现有任务）"
        # 依赖已停用：
        # open_b = [b for b in task.blocked_by
        #           if (bt := mgr.get(b)) is not None and bt.status != "completed"]
        lines = [
            f"任务 #{task.id}",
            f"  标题  : {task.subject}",
            f"  状态  : {task.status}",
            f"  owner : {task.owner or '（未认领）'}",
            f"  描述  : {task.description or '（无）'}",
            # f"  依赖(未完成的 blocked_by): {', '.join('#'+b for b in open_b) or '（无，可开始）'}",
            # f"  阻塞(blocks): {', '.join('#'+b for b in task.blocks) or '（无）'}",
        ]
        return "\n".join(lines)

    def _list(args: dict) -> str:
        rows = mgr.summaries()
        if not rows:
            return "任务清单为空。复杂任务（≥3 步）或用户一次给多项时，先用 task_create 建清单再动手。"
        done = sum(1 for r in rows if r["status"] == "completed")
        return (f"任务清单（共 {len(rows)}，完成 {done}）：\n"
                + "\n".join(_fmt_row(r) for r in rows))

    return [
        Tool(
            name="task_create",
            description="在结构化任务清单里创建一条任务，用于规划和跟踪复杂任务、让你和用户都看得到进度。"
                        "何时用：任务需 ≥3 个步骤、或非平凡需要规划、或用户一次给了多项任务、或刚接到新需求先记下来。"
                        "何时【不】用：只有一个简单直接的小任务、或纯对话/查询——直接做，别建清单。"
                        "新任务一律 status=pending（开始做某条时再用 task_update 把它标成 in_progress）。"
                        "建多条任务就多次调用本工具、一次建一条。本工具返回新建任务的 id。",
                        # 依赖已停用，原描述："依赖不在创建时设：依赖要指向别的任务 id，需先把相关任务都创建出来，
                        # 再用 task_update 的 add_blocked_by/add_blocks 连边。"
            parameters={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "任务标题，祈使句（如「修复登录鉴权 bug」）"},
                    "description": {"type": "string", "description": "具体要做什么（task_get 读全需求时看这个；多 agent 里子 agent 据此干活）"},
                    "active_form": {"type": "string", "description": "进行时表述，in_progress 时展示（如「正在修复登录鉴权」）；缺省回退 subject"},
                    "metadata": {"type": "object", "description": "任意附加键值（可选）"},
                },
                "required": ["subject"],
            },
            handler=_create,
        ),
        Tool(
            name="task_update",
            description="更新任务清单里的一条任务（按 task_id）。"
                        "改状态：开始做时设 in_progress；【完全】做完才设 completed"
                        "（测试还挂 / 实现不全 / 有未解错误 → 别标 completed，保持 in_progress）；"
                        "不再需要的任务设 deleted 永久删除。同一时刻只保持一个任务 in_progress。"
                        "改内容：subject/description/active_form；认领任务设 owner。",
                        # 依赖已停用，原描述："设依赖：add_blocked_by=[id…] 标「本任务要等这些任务先完成」、
                        # add_blocks=[id…] 标「这些任务要等本任务先完成」（自动维护反向边；依赖只增不减——
                        # 某任务 completed 即自动解开它对别人的阻塞，无需也不应手动删边）。"
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要更新的任务编号（task_create 返回的 #id）"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"],
                               "description": "新状态；deleted=永久删除该任务"},
                    "subject": {"type": "string", "description": "改标题"},
                    "description": {"type": "string", "description": "改描述"},
                    "active_form": {"type": "string", "description": "改进行时表述"},
                    "owner": {"type": "string", "description": "认领/改归属（agent 名）"},
                    # 依赖已停用：
                    # "add_blocks": {"type": "array", "items": {"type": "string"},
                    #                "description": "本任务挡住的任务 id（这些要等本任务先完成）"},
                    # "add_blocked_by": {"type": "array", "items": {"type": "string"},
                    #                    "description": "本任务依赖的任务 id（这些必须先完成）"},
                    "metadata": {"type": "object", "description": "并入 metadata（某键设 null 删除该键）"},
                },
                "required": ["task_id"],
            },
            handler=_update,
        ),
        Tool(
            name="task_get",
            description="按 id 取一条任务的完整信息（标题/描述/状态/owner）。"
                        "开始做某条前用它读全需求；多 agent 里认领任务后用它读细节。看全部任务用 task_list。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务编号"},
                },
                "required": ["task_id"],
            },
            handler=_get,
            read_only=True,
        ),
        Tool(
            name="task_list",
            description="列出任务清单里所有任务（id/标题/状态/owner）。"
                        "用于查看整体进度、找可做的任务（pending、无 owner）、或完成一条后找下一条。"
                        "看某条完整细节用 task_get。",
            parameters={"type": "object", "properties": {}},
            handler=_list,
            read_only=True,
        ),
    ]
