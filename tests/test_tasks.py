"""任务清单：数据模型 + TaskManager（增改查/持久化）+ 4 个工具。

固化的不变量：
- create 一律出生 pending、分配自增 id；status/owner 只能经 update 改
- 持久化：tasks.json 存 {next_id, tasks}，新 manager 从盘还原（resume）
（依赖 blocked_by/blocks 已整体注释停用——见 tasks.py 模块注释"依赖"段；对应测试一并注释保留。）
"""
from mecode.session import SessionStore
from mecode.tasks import Task, TaskManager, task_tools


def _tools(mgr):
    return {t.name: t.handler for t in task_tools(mgr)}


# ---------- TaskManager：增改 ----------

def test_create_出生pending_自增id():
    m = TaskManager()
    a = m.create("搭脚手架")
    b = m.create("写测试", description="覆盖核心")
    assert (a.id, a.status) == ("1", "pending")
    assert (b.id, b.status, b.description) == ("2", "pending", "覆盖核心")


def test_update_状态迁移():
    m = TaskManager()
    t = m.create("活")
    m.update(t.id, status="in_progress", active_form="正在干活")
    assert m.get(t.id).status == "in_progress" and m.get(t.id).active_form == "正在干活"
    m.update(t.id, status="completed")
    assert m.get(t.id).status == "completed"


def test_update_未知状态_警告且不改():
    m = TaskManager()
    t = m.create("活")
    _, warnings = m.update(t.id, status="done")          # 不是合法枚举
    assert m.get(t.id).status == "pending" and warnings   # 状态没变 + 给了警告


def test_update_不存在的任务_返回None():
    m = TaskManager()
    task, warnings = m.update("99", status="completed")
    assert task is None and warnings == []


def test_delete_删到返回True_删不到False():
    m = TaskManager()
    t = m.create("活")
    assert m.delete(t.id) is True
    assert m.get(t.id) is None
    assert m.delete(t.id) is False


# ---------- 依赖：镜像边 + 推导式解锁 + 删边时机（功能已注释停用，测试一并保留） ----------

# def test_add_blocked_by_维护双向镜像():
#     m = TaskManager()
#     a = m.create("迁移")
#     b = m.create("改API")
#     m.update(b.id, add_blocked_by=[a.id])                 # b 依赖 a
#     assert m.get(b.id).blocked_by == [a.id]               # 正向
#     assert m.get(a.id).blocks == [b.id]                   # 反向镜像自动建
#
#
# def test_add_blocks_维护双向镜像():
#     m = TaskManager()
#     a = m.create("迁移")
#     b = m.create("改API")
#     m.update(a.id, add_blocks=[b.id])                     # a 挡住 b（等价于 b 依赖 a）
#     assert m.get(a.id).blocks == [b.id]
#     assert m.get(b.id).blocked_by == [a.id]
#
#
# def test_完成上游_自动解锁下游_但不删边():
#     m = TaskManager()
#     a = m.create("迁移")
#     b = m.create("改API")
#     m.update(b.id, add_blocked_by=[a.id])
#     assert [r for r in m.summaries() if r["id"] == b.id][0]["open_blockers"] == [a.id]   # 被挡
#     m.update(a.id, status="completed")                    # 上游完成
#     row_b = [r for r in m.summaries() if r["id"] == b.id][0]
#     assert row_b["open_blockers"] == []                   # 推导式：completed 的不计入 → 解锁
#     assert m.get(b.id).blocked_by == [a.id]               # 但原始边仍在（存证、未删）
#
#
# def test_删除节点_清掉别人指向它的悬空边():
#     m = TaskManager()
#     a = m.create("迁移")
#     b = m.create("改API")
#     m.update(b.id, add_blocked_by=[a.id])
#     m.delete(a.id)                                        # 节点消失 = 唯一真正删边的时机
#     assert m.get(b.id).blocked_by == []                   # 悬空边被清，不留指向已删任务的引用
#
#
# def test_自依赖与不存在id_不建边给警告():
#     m = TaskManager()
#     a = m.create("活")
#     _, w1 = m.update(a.id, add_blocked_by=[a.id])         # 自依赖
#     _, w2 = m.update(a.id, add_blocked_by=["404"])        # 不存在
#     assert m.get(a.id).blocked_by == [] and w1 and w2


# ---------- 持久化：tasks.json 还原 ----------

def test_持久化_新manager从盘还原(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    m1 = TaskManager(store=st)
    a = m1.create("迁移")
    b = m1.create("改API")
    m1.update(b.id, status="in_progress")
    m2 = TaskManager(store=st)                            # 模拟 resume：另起一个读同一份盘
    got_b = m2.get(b.id)
    assert got_b.status == "in_progress" and got_b.subject == "改API"
    assert m2.create("第三个").id == "3"                  # next_id 续号（不从 1 重来）


def test_无store_只在内存不落盘(tmp_path):
    m = TaskManager()                                     # store=None
    m.create("活")
    assert not (tmp_path / "tasks.json").exists()         # 没有任何盘文件


# ---------- on_change 回调 ----------

def test_变更回调on_change被触发():
    hits = []
    m = TaskManager(on_change=lambda: hits.append(1))
    t = m.create("活")
    m.update(t.id, status="in_progress")
    m.delete(t.id)
    assert len(hits) == 3                                 # create/update/delete 各触发一次


# ---------- 4 个工具：返回串 ----------

def test_工具_create_返回带id():
    m = TaskManager()
    out = _tools(m)["task_create"]({"subject": "搭脚手架"})
    assert "#1" in out and "搭脚手架" in out
    assert _tools(m)["task_create"]({"subject": "  "}).startswith("错误")   # 空 subject


def test_工具_update_含删除():
    m = TaskManager()
    h = _tools(m)
    h["task_create"]({"subject": "活"})
    assert "status=in_progress" in h["task_update"]({"task_id": "1", "status": "in_progress"})
    assert "已删除" in h["task_update"]({"task_id": "1", "status": "deleted"})
    assert "没有任务" in h["task_update"]({"task_id": "1", "status": "completed"})   # 删后再点不到


def test_工具_get_与list():
    m = TaskManager()
    h = _tools(m)
    assert "为空" in h["task_list"]({})                                   # 空清单提示
    h["task_create"]({"subject": "实现登录", "description": "用 JWT"})
    h["task_update"]({"task_id": "1", "status": "in_progress", "active_form": "正在实现登录"})
    got = h["task_get"]({"task_id": "1"})
    assert "实现登录" in got and "用 JWT" in got and "in_progress" in got
    lst = h["task_list"]({})
    assert "#1" in lst and "实现登录" in lst and "▶" in lst               # in_progress 图标


def test_render_reminder_紧凑快照():
    m = TaskManager()
    m.create("A")
    b = m.create("B")
    m.update(b.id, status="completed")
    snap = m.render_reminder()
    assert "当前任务清单" in snap and "#1" in snap and "#2" in snap
    assert "✓ #2" in snap                                 # 完成图标
    assert TaskManager().render_reminder() == ""          # 空清单 → 空串
