"""后台任务管理器：守护线程跑命令、自然完成入队+通知、用户杀静默、超时判定、退出全杀。

后台命令经 _bash_command 走选定的 shell；这些测试用 POSIX 命令（echo/sleep/exit），
没有 bash 就跳过（和 test_tools 的 shell 分支一致）。
"""
import shutil
import threading
import time

import pytest

from mecode.background import BackgroundManager

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="后台命令测试需要 POSIX shell")


def _wait(cond, timeout=8.0) -> bool:
    """轮询等条件成立（守护线程是异步的）；超时返回 False。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_start立即返回id_自然完成进drain并通知():
    notified = []
    m = BackgroundManager(on_complete=lambda t: notified.append(t))
    tid = m.start("echo hi", timeout=10)
    assert tid == 1                                       # 立即拿到 id
    assert _wait(lambda: notified)                        # on_complete 在入队后回调 → 此时已可 drain
    done = m.drain_completions()
    assert len(done) == 1 and done[0].id == tid
    assert done[0].status == "done" and "hi" in done[0].output and done[0].exit_code == 0
    assert notified and notified[0].id == tid             # 通知带上了 task
    assert m.drain_completions() == []                    # 取过即空
    assert m.running() == []                              # 跑完已从在跑表移除


def test_退出码非零也算自然完成():
    notified = []
    m = BackgroundManager(on_complete=lambda t: notified.append(t))
    m.start("exit 3", timeout=10)
    assert _wait(lambda: notified)
    done = m.drain_completions()
    assert done and done[0].status == "done" and done[0].exit_code == 3


def test_kill静默_不进drain不通知():
    notified = []
    m = BackgroundManager(on_complete=lambda t: notified.append(t))
    tid = m.start("sleep 5", timeout=30)
    assert m.running() and m.running()[0].id == tid       # 起来了、在跑
    m.kill(tid)
    assert _wait(lambda: not m.running())                 # 被杀 → 很快从在跑表移除
    assert m.drain_completions() == []                    # 用户杀的：不入完成队列
    assert notified == []                                 # 也不通知（不触发自动接续）


def test_停止按钮note记便条_AI的kill不记_便条不计入has_pending():
    m = BackgroundManager()
    tid = m.start("sleep 30")
    assert m.running() and m.running()[0].id == tid
    assert m.kill(tid, note=True) is not None              # 停止按钮：note=True（返回终止前快照，非 None）
    assert m.drain_killed() == [(tid, "sleep 30", "")]     # 便条 (id,命令,终止前输出)；sleep 无输出→空串
    assert m.drain_killed() == []                          # 取过即空
    assert m.has_pending() is False                        # 便条不计入 → 停止动作不自动起轮

    tid2 = m.start("sleep 30")
    assert _wait(lambda: m.running())
    assert m.kill(tid2) is not None                        # AI 的 kill_bgtask：默认 note=False（返回快照）
    assert m.drain_killed() == []                          # 不记便条（AI 已有"已终止"工具结果）
    m.kill_all()


def test_kill带回终止前输出_AI工具结果附():
    from mecode.background import background_tools
    bg = BackgroundManager()
    tid = bg.start("echo KILLMARK; sleep 30", timeout=60)
    assert _wait(lambda: "KILLMARK" in (bg.check_output(tid) or ""))
    out = {t.name: t for t in background_tools(bg)}["kill_bgtask"].handler({"id": tid})
    assert "已终止" in out and "KILLMARK" in out            # AI kill_bgtask：结果里带上终止前输出
    assert _wait(lambda: not bg.running())


def test_停止按钮便条带终止前输出():
    bg = BackgroundManager()
    tid = bg.start("echo STOPMARK; sleep 30", timeout=60)
    assert _wait(lambda: "STOPMARK" in (bg.check_output(tid) or ""))
    snap = bg.kill(tid, note=True)
    assert snap is not None and "STOPMARK" in snap         # kill 返回终止前快照
    notes = bg.drain_killed()
    assert len(notes) == 1 and notes[0][0] == tid and "STOPMARK" in notes[0][2]  # 便条第三位=输出
    assert _wait(lambda: not bg.running())


def test_kill_task_子agent_置打断标志且杀其proc_slot():
    import threading
    from mecode.background import BackgroundManager, BackgroundTask
    ev = threading.Event()
    killed = []

    class _FakeSlot:
        def kill(self):
            killed.append(1)

    task = BackgroundTask(id=1, command="prompt", is_subagent=True, interrupt=ev, sub_slot=_FakeSlot())
    BackgroundManager._kill_task(task)
    assert ev.is_set() and killed == [1]              # 置打断标志 + 杀它正跑的前台 bash（proc_slot）


def test_timeout判超时_进drain():
    m = BackgroundManager()
    m.start("sleep 5", timeout=1)                         # 1s 超时，远早于 sleep 5
    assert _wait(lambda: m.running() == [], timeout=8)
    done = m.drain_completions()
    assert len(done) == 1 and done[0].status == "timeout"


def test_kill_all全杀():
    m = BackgroundManager()
    a, b = m.start("sleep 5", timeout=30), m.start("sleep 5", timeout=30)
    assert len(m.running()) == 2
    m.kill_all()
    assert _wait(lambda: not m.running())                 # 两个都没了
    assert m.drain_completions() == []                    # 全是 killed → 静默
    assert a != b                                         # id 递增、不重复


# ---- Step 2：bash ↔ bg 接线 + kill_bgtask 工具 ----

def test_bash_background_转后台_立即返回id():
    from mecode.tools import default_registry
    bg = BackgroundManager()
    out = default_registry().execute("bash", {"command": "sleep 5", "background": True}, bg=bg)
    assert "#1" in out and "后台" in out                  # 立即返回任务编号，不阻塞
    assert len(bg.running()) == 1 and bg.running()[0].command == "sleep 5"
    bg.kill_all()
    assert _wait(lambda: not bg.running())


def test_bash_无background仍走前台():
    from mecode.tools import default_registry
    bg = BackgroundManager()
    out = default_registry().execute("bash", {"command": "echo hi"}, bg=bg)
    assert "hi" in out and bg.running() == []             # 前台跑完、没进后台


def test_kill_bgtask工具():
    from mecode.background import background_tools
    bg = BackgroundManager()
    tid = bg.start("sleep 5", timeout=30)
    tools = {t.name: t for t in background_tools(bg)}
    assert set(tools) == {"kill_bgtask", "check_bgtask", "wait_bgtask"}
    assert f"#{tid}" in tools["kill_bgtask"].handler({"id": tid})       # 已终止
    assert _wait(lambda: not bg.running())
    assert "没有在运行" in tools["kill_bgtask"].handler({"id": 999})     # 不存在 → 友好提示


def test_agent注册了kill_bgtask():
    from mecode.agent import Agent
    from mecode.tools import ToolRegistry
    a = Agent(type("P", (), {"stream": lambda *x, **k: iter(())})(), ToolRegistry(), system_prompt="x")
    assert "kill_bgtask" in {s["function"]["name"] for s in a.tools.schemas()}


# ---- Step 5：运行中读输出快照 + check-in 定时 + check/wait 工具 ----

def test_check_bgtask读运行中输出():
    from mecode.background import background_tools
    bg = BackgroundManager()
    tid = bg.start("echo hello; sleep 5", timeout=30)
    assert _wait(lambda: "hello" in (bg.check_output(tid) or ""))      # 运行中就能读到部分输出
    tools = {t.name: t for t in background_tools(bg)}
    assert "hello" in tools["check_bgtask"].handler({"id": tid})
    bg.kill(tid)
    assert _wait(lambda: not bg.running())
    assert bg.check_output(tid) is None                               # 已结束 → None


def test_checkin到点_due_重排_has_pending():
    import time
    bg = BackgroundManager()
    tid = bg.start("sleep 5", timeout=30)
    task = bg.running()[0]
    assert not bg.has_pending()                                       # 刚起，远未到 120s
    task.next_checkin_at = time.monotonic() - 1                       # 手动调成已到点
    assert bg.has_pending()
    due = bg.due_checkins()
    assert len(due) == 1 and due[0][0].id == tid                      # drain 出该任务（带输出快照）
    assert not bg.has_pending()                                       # 重排到未来 → 不再 pending
    bg.kill_all()
    assert _wait(lambda: not bg.running())


def test_wait_bgtask推迟checkin():
    import time
    from mecode.background import background_tools
    bg = BackgroundManager()
    tid = bg.start("sleep 5", timeout=30)
    bg.running()[0].next_checkin_at = time.monotonic() - 1           # 已到点
    assert bg.has_pending()
    tools = {t.name: t for t in background_tools(bg)}
    assert "300" in tools["wait_bgtask"].handler({"id": tid, "seconds": 300})
    assert not bg.has_pending()                                       # 推到 300s 后 → 不再 pending
    bg.kill_all()
    assert _wait(lambda: not bg.running())


def test_check_output_运行中尾部截断_留最新砍最早():
    bg = BackgroundManager(output_cap=80)
    tid = bg.start("echo STARTMARKER; for i in $(seq 1 40); do echo filler$i; done; sleep 5", timeout=30)
    assert _wait(lambda: "filler40" in (bg.check_output(tid) or ""))
    out = bg.check_output(tid)                                # 运行中快照：尾部 + 提示
    assert "filler40" in out and "STARTMARKER" not in out     # 留最新尾部、砍最早的头
    assert "更早内容已略" in out and len(out) <= 80           # 标了截断、整段不超 output_cap
    bg.kill(tid)
    assert _wait(lambda: not bg.running())


def test_完成output是全文_截断交给agent():
    notified = []
    bg = BackgroundManager(on_complete=lambda t: notified.append(t), output_cap=80)
    bg.start("echo STARTMARKER; echo ENDMARKER", timeout=10)
    assert _wait(lambda: notified)
    out = bg.drain_completions()[0].output                    # 完成 = 尽量全（内联/外置由 agent 决定）
    assert "STARTMARKER" in out and "ENDMARKER" in out


def test_子agent被停止分支后不算自然完成():
    # 审批弹窗上的"停止此后台任务"只【置打断标志】、不走 kill_bgtask（它没有任务号）。
    # 若守护线程照旧把 status 置成 done，那半截正文会被当"总结"上报——用户明明是叫停，
    # 主 agent 却以为它干完了。故：收尾时看打断标志，被停过就记 killed + 留一条便条。
    class _Runner:
        def run_for_bg(self, prompt, interrupt, on_slot):
            interrupt.set()                      # 模拟：跑到一半用户在弹窗上点了"停止此后台任务"
            return "半截正文，不是总结"

    m = BackgroundManager()
    tid = m.start_subagent("干活", _Runner(), description="x")
    assert _wait(lambda: m.running() == [], timeout=8)
    # _finish 会把任务从在跑表摘掉，故从"完成队列/便条"这两个出口判它被归成了哪一类
    assert m.drain_completions() == []               # 不当完成上报（否则主 agent 拿半截当结论）
    assert [n[0] for n in m._killed_notes] == [tid]  # 留便条：否则"#N 运行中"在上下文里永远悬着


def test_子agent自然完成仍算done():
    class _Runner:
        def run_for_bg(self, prompt, interrupt, on_slot):
            return "总结"

    m = BackgroundManager()
    tid = m.start_subagent("干活", _Runner(), description="x")
    assert _wait(lambda: m.running() == [], timeout=8)
    done = m.drain_completions()
    assert [t.id for t in done] == [tid] and done[0].status == "done"
    assert done[0].output == "总结" and m._killed_notes == []


def test_start_fn被停止后不算自然完成():
    # 与 test_子agent被停止分支后不算自然完成 是【孪生路径】：workflow 走 start_fn、子 agent 走
    # _run_subagent，两条守护线程体都要按打断标志定性。此前只补了后者，workflow 被停后照旧把
    # 半截汇总报告当最终产物上报给主 agent（用户明明叫停了，主 agent 却以为干完了）。
    stop = threading.Event()

    def fake_workflow():
        stop.set()                            # 模拟：跑到一半用户在审批弹窗上点了"停止此后台任务"
        return "半截汇总，不是最终产物"

    m = BackgroundManager()
    tid = m.start_fn(fake_workflow, command="run_workflow（3 个阶段）",
                     description="wf", interrupt=stop)
    assert _wait(lambda: m.running() == [], timeout=8)
    assert m.drain_completions() == []               # 不当完成上报
    assert [n[0] for n in m._killed_notes] == [tid]  # 留便条，否则"#N 运行中"在上下文里永远悬着


def test_start_fn自然完成仍算done():
    m = BackgroundManager()
    tid = m.start_fn(lambda: "汇总报告", command="run_workflow", description="wf",
                     interrupt=threading.Event())
    assert _wait(lambda: m.running() == [], timeout=8)
    done = m.drain_completions()
    assert [t.id for t in done] == [tid] and done[0].status == "done"
    assert done[0].output == "汇总报告" and m._killed_notes == []


def test_wait_bgtask不能让模型以为时间过去了():
    """wait_bgtask 立即返回，只是把下次自动 check-in 挪到 N 秒后——它【不等】。

    可它原来回的是"好的，35 秒后再来看 #1 的进展"，读起来完全像"已经等过了"。
    模型没有钟、只能信这句话，于是 wait 35 → check → wait 35 之后认定"都 70 秒了
    怎么还没输出"，而实际才过两秒。工具结果必须自己把这件事说破。
    """
    import time
    from mecode.background import background_tools
    bg = BackgroundManager()
    tid = bg.start("sleep 5", timeout=30)
    tools = {t.name: t for t in background_tools(bg)}

    out = tools["wait_bgtask"].handler({"id": tid, "seconds": 35})
    assert "没有花掉任何时间" in out, out
    assert "结束本轮" in out, "没告诉模型唯一正确的下一步，它只会继续空转：" + out
    assert "好的，35 秒后再来看" not in out, "又回到那句像'已经等过了'的话术"
    # 说明里也得写死，否则模型在【决定调不调】的时候就已经理解错了
    assert "不会让时间流逝" in tools["wait_bgtask"].description

    # 真实读数：模型不必自己记账（它记的账正是这个 bug 的成因）
    assert bg.elapsed(tid) is not None
    time.sleep(1.1)
    assert bg.elapsed(tid) >= 1
    assert f"已运行 {bg.elapsed(tid)} 秒" in tools["check_bgtask"].handler({"id": tid})

    bg.kill_all()
    assert _wait(lambda: not bg.running())
    assert bg.elapsed(tid) is None                # 不在跑就没有"已运行"这回事
