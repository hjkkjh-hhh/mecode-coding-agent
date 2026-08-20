"""后台任务：把长命令丢到守护线程里跑，不阻塞 agent 主循环。

和前台 bash（tools.py 的 ProcSlot）互补：
  前台 —— communicate 阻塞当前轮，Ctrl+C 杀；短命令用。
  后台 —— start() 立即返回 id，守护线程跑；长命令（dev server / 构建 / 测试）用。

输出落【临时文件】（stdout+stderr 合并重定向到一个文件），而不是 PIPE：
  PIPE 缓冲填满会让话痨子进程写阻塞、卡死；落文件则随时能【按需读快照】（读文件尾），
  不必持续抽取——check-in / check_bgtask / 完成注入都只是读一次文件。任务结束后删文件。

再唤起 AI 的两类事件（都走 agent 的 drain → 注入 user+<system-reminder>）：
  完成（done/timeout）→ drain_completions；on_complete(task) 通知 UI（空闲起轮 + 闪/刷新）。
  还在跑的【定时 check-in】→ due_checkins（跑过 N 秒自动让 AI 瞄一眼部分输出、自己定下次）。
杀的语义：用户杀（kill/kill_all）静默——不入队、不通知、不自动起轮。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .tools import Tool, _bash_command, _kill_tree

DEFAULT_CHECKIN = 120.0          # 后台任务首次/默认 check-in 间隔（秒）；AI 可用 wait_bgtask 改
FULL_CAP = 500_000               # 完成时读日志做"全文"的上限（字符）：够大、又防超大日志 OOM


@dataclass
class BackgroundTask:
    id: int
    command: str                     # bash 任务=命令；子 agent 任务=派的 prompt（都用于显示）
    proc: subprocess.Popen | None = None   # 子进程（子 agent 任务为 None）
    status: str = "running"          # running / done / timeout / killed
    output: str = ""                 # 结束时的最终输出（bash=读文件；子 agent=总结）
    exit_code: int | None = None
    started_at: float = 0.0          # time.monotonic()；给 TUI 显示“已 Ns”
    log_path: str = ""               # bash：stdout+stderr 重定向到的临时文件；结束后删（子 agent 无）
    next_checkin_at: float = 0.0     # 下次该 check-in 的时刻（monotonic）；子 agent 设 inf（不做 check-in）
    checkin_interval: float = DEFAULT_CHECKIN
    # 子 agent 任务专用：kind、自带的独立打断标志（Ctrl+C 不碰、只 kill 停）、它的 ProcSlot（供 kill 杀其 bash）
    is_subagent: bool = False
    interrupt: "threading.Event | None" = None
    sub_slot: object = None
    description: str = ""            # 子 agent 的 3-5 词短标签（收起行显示；command 存完整 prompt 供展开）

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


_TRUNC_NOTE = "（输出较长，以下仅为末尾部分，更早内容已略）\n"


class BackgroundManager:
    """管理后台任务：start 起、kill 停、drain_completions/due_checkins 取事件、running 列在跑。线程安全。"""

    def __init__(self, on_complete: Callable[[BackgroundTask], None] | None = None,
                 output_cap: int = 8000) -> None:
        # 自然完成时回调（TUI 注入：post_message 唤醒 → 空闲起接续轮 + 闪/刷新显示）。带上 task 供 UI 用。
        self.on_complete = on_complete
        # 注入上下文的输出快照上限（字符）：与工具结果同口径，由 agent 传 config.tool_result_max_chars。
        self._output_cap = output_cap
        self._tasks: dict[int, BackgroundTask] = {}     # 在跑的任务（完成/被杀后移除）
        self._done: list[BackgroundTask] = []           # 自然完成、待 agent drain 注入的
        self._killed_notes: list[tuple[int, str, str]] = []  # 停止按钮杀的便条 (id,命令,终止前输出)，待 agent drain 注入
        self._next_id = 0
        self._lock = threading.Lock()                   # 守护线程们 + agent 线程 + UI 线程共访

    def start(self, command: str, timeout: int | None = None) -> int:
        """起一个后台任务，立即返回 id（守护线程跑命令、不阻塞调用方）。输出落临时文件。
        timeout=None（默认）不限时——后台多是 dev server 等长任务，靠完成/kill 收尾；
        传了 timeout 则超时自动判超时杀掉（有界后台任务）。"""
        fd, log_path = tempfile.mkstemp(suffix=".mecode-bg.log")
        logf = os.fdopen(fd, "wb")
        # 后台子进程必须【脱离 TUI 所在的控制台】，否则子进程会改动该控制台的模式 → TUI 鼠标/键盘失灵、
        # 事件循环像冻死（Ctrl+C 透到 cmd 的 "Terminate batch job"）。典型肇事者是 python：它见 stdin
        # 是控制台就去读/改 Windows 控制台输入模式（bash 内建 for/echo/sleep 不碰，故只 python 触发）。
        # stdin=DEVNULL 让子进程看不到控制台输入；Windows 再加 CREATE_NO_WINDOW 给它独立隐藏控制台，彻底隔离。
        # 输出仍走 stdout=文件，不受影响；杀进程靠 PID（taskkill /T）也不受影响。
        extra: dict = {"stdin": subprocess.DEVNULL}
        if os.name == "nt":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            extra["start_new_session"] = True
        try:
            proc = subprocess.Popen(_bash_command(command), stdout=logf,
                                    stderr=subprocess.STDOUT, **extra)
        except Exception:
            logf.close()                                # 起不来：关句柄、删临时文件，错误上抛给 execute 兜
            try:
                os.remove(log_path)
            except OSError:
                pass
            raise
        logf.close()                                    # 父进程不需要写句柄；子进程有自己的 dup
        now = time.monotonic()
        with self._lock:
            self._next_id += 1
            tid = self._next_id
            task = BackgroundTask(id=tid, command=command, proc=proc, started_at=now,
                                  log_path=log_path, next_checkin_at=now + DEFAULT_CHECKIN)
            self._tasks[tid] = task
        threading.Thread(target=self._run, args=(task, timeout), daemon=True).start()
        return tid

    def start_subagent(self, prompt: str, runner, description: str = "") -> int:
        """把一个【子 agent】作为后台任务起（fire-and-forget）：守护线程里 runner.run_for_bg 跑一个干净子 agent、
        立即返回 id、不阻塞。完成→_finish 入完成队列+通知（同 bash，主 agent 自动接续拿到总结）。
        子 agent 无日志/无 check-in（next_checkin_at=inf）；自带独立打断标志，kill 时置它 + 杀其前台 bash。
        description=3-5 词短标签（收起行显示）；command 存完整 prompt（展开看全文）。"""
        now = time.monotonic()
        with self._lock:
            self._next_id += 1
            tid = self._next_id
            task = BackgroundTask(id=tid, command=prompt, started_at=now,
                                  next_checkin_at=float("inf"),   # 子 agent 不做 check-in（无中间日志可瞄）
                                  is_subagent=True, interrupt=threading.Event(),
                                  description=description)
            self._tasks[tid] = task
        threading.Thread(target=self._run_subagent, args=(task, runner), daemon=True).start()
        return tid

    def start_fn(self, fn, command: str, description: str = "",
                 interrupt: "threading.Event | None" = None, sub_slot=None) -> int:
        """把一个可调用 fn()->str 作为后台任务跑（workflow 等编排用）：语义同 start_subagent——
        立即返回 id、完成入队通知（主 agent 自动接续拿结果）、kill 走 is_subagent 路径
        （置 interrupt + sub_slot.kill()，sub_slot 只需实现 .kill()，如 workflow 的 SlotGroup）。"""
        with self._lock:
            self._next_id += 1
            tid = self._next_id
            task = BackgroundTask(id=tid, command=command, started_at=time.monotonic(),
                                  next_checkin_at=float("inf"), is_subagent=True,
                                  interrupt=interrupt, description=description)
            task.sub_slot = sub_slot
            self._tasks[tid] = task

        def _worker() -> None:
            try:
                task.output = fn()
            except Exception as e:
                task.output = f"执行出错：{type(e).__name__}: {e}"
            with self._lock:
                # 与 _run_subagent 同款判据（两条守护线程体是孪生路径，改一处必须改另一处）：
                # 打断标志被置过 = 有人停了它（kill_bgtask 会顺手改 status，而审批弹窗上的
                # "停止此后台任务"只置标志、不走 kill）→ 不能当自然完成，否则 workflow 那份
                # 半截汇总会被当最终产物上报给主 agent（用户明明叫停了，主 agent 却以为干完了）。
                self._mark_finished(task)
            self._finish(task)

        threading.Thread(target=_worker, daemon=True).start()
        return tid

    def _run_subagent(self, task: BackgroundTask, runner) -> None:
        """守护线程体（子 agent 版）：跑 runner.run_for_bg 拿总结；异常也当结果、不拖垮。收尾走 _finish（复用）。"""
        try:
            task.output = runner.run_for_bg(
                task.command, task.interrupt,
                on_slot=lambda s: setattr(task, "sub_slot", s))   # 交出它的 ProcSlot，供 kill 杀其 bash
        except Exception as e:
            task.output = f"子 agent 执行出错：{type(e).__name__}: {e}"
        with self._lock:
            self._mark_finished(task)
        self._finish(task)

    def _mark_finished(self, task: BackgroundTask) -> None:
        """守护线程收尾时定性这个任务（须在 self._lock 内调）。子 agent 与 start_fn（workflow）
        两条守护线程体【共用它】——它们是孪生路径，此前只给其中一条补了判据，另一条照旧报"自然完成"。

        打断标志被置过 = 有人停了它：kill_bgtask 会顺手把 status 改成 killed，但审批弹窗上的
        "停止此后台任务"只置标志、不走 kill（它没有任务号）→ 走到这里 status 还是 running。
        若当自然完成，那半截输出会被当成"总结/汇总报告"上报给主 agent（用户明明叫停了，
        主 agent 却以为干完了）。"""
        if task.status != "running":
            return                                       # 已被 kill 定过性
        if task.interrupt is not None and task.interrupt.is_set():
            task.status = "killed"
            # 记一条便条：否则主 agent 的上下文里那条"#N 运行中"永远悬着、还会去 check 幽灵任务
            self._killed_notes.append((task.id, task.command, ""))
        else:
            task.status = "done"                         # 没被停 → 自然完成

    def _run(self, task: BackgroundTask, timeout: int | None) -> None:
        """守护线程体：等命令结束 / 超时；收尾读日志、删文件、走 _finish。被外部 kill 时 status 已是 killed。"""
        try:
            task.proc.wait(timeout=timeout)
            with self._lock:
                if task.status == "running":             # 没被 kill → 自然完成
                    task.status = "done"
            task.exit_code = task.proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(task.proc)
            try:
                task.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with self._lock:
                if task.status == "running":
                    task.status = "timeout"
        finally:
            task.output = self._read_completed(task.log_path)  # 读日志拿"全文"（尽量全），交 agent 决定内联/外置
            try:
                os.remove(task.log_path)                 # 子进程已结束、句柄已关 → 可删
            except OSError:
                pass
            self._finish(task)

    def _finish(self, task: BackgroundTask) -> None:
        # 从在跑表移除；自然完成(done/timeout)入完成队列 + 通知，用户杀(killed)静默。
        with self._lock:
            self._tasks.pop(task.id, None)
            notify = None
            if task.status != "killed":
                self._done.append(task)
                notify = self.on_complete
        if notify is not None:
            notify(task)                                  # 在锁外回调，免回调里再操作 manager 死锁

    @staticmethod
    def _kill_task(task: BackgroundTask) -> None:
        """按类型杀一个任务（锁外调，taskkill 可能慢）：
        - bash：taskkill /T 杀进程树；
        - 子 agent：置它的打断标志（run_turn 下个检查点停）+ 杀它正跑的前台 bash（ProcSlot.kill → 树杀含孙进程）
          → 解除 communicate 阻塞、守护线程随即退出（Python 无法强杀线程，靠协作式停 + daemon 不挡退出）。"""
        if task.is_subagent:
            if task.interrupt is not None:
                task.interrupt.set()
            if task.sub_slot is not None:
                task.sub_slot.kill()
        elif task.proc is not None:
            _kill_tree(task.proc)

    def kill(self, tid: int, *, note: bool = False) -> str | None:
        """杀掉指定后台任务（停止按钮 / AI 的 kill_bgtask）。返回【终止前的输出快照】——bash=日志尾部
        （无输出则空串）、子 agent=""（无中间输出可看）；任务不存在（已结束/编号错）→ None。
        和前台 bash 被 Ctrl+C 对齐：把"停之前干到哪"带回去，比只说"已终止"更有判断依据。
        静默：守护线程见 status=killed 不入完成队列、不通知、不自动起轮。
        note=True（停止按钮用）：另记一条"已被用户停止"便条（含输出快照），给 agent 下次跑时注入——否则
        AI 上下文仍以为 #id 在跑，会白白 check 一次。AI 自己调 kill_bgtask 不记 note（它拿到了带输出的
        工具结果、本就知道）。便条不计入 has_pending，故停止动作不会平白起一轮。"""
        with self._lock:
            task = self._tasks.get(tid)
            if task is None:
                return None
            task.status = "killed"                        # 先标记，抢在守护线程把它判成 done/timeout 之前
            is_sub, path, cmd = task.is_subagent, task.log_path, task.command
        snap = "" if is_sub else (self._snapshot(path) or "")   # 锁外读日志：终止前输出（子 agent 无 → ""）
        if note:
            with self._lock:
                self._killed_notes.append((tid, cmd, snap))
        self._kill_task(task)                             # 杀在锁外：taskkill 可能慢，不占锁
        return snap

    def kill_all(self) -> None:
        """杀掉所有后台任务（Ctrl+Q 退出时调，防泄漏 OS 进程 / 子 agent 线程及其 bash）。"""
        with self._lock:
            tasks = list(self._tasks.values())
            for t in tasks:
                t.status = "killed"
        for t in tasks:
            self._kill_task(t)

    def drain_completions(self) -> list[BackgroundTask]:
        """取走并清空“自然完成”的任务（agent 调模型前 drain、注入上下文）。"""
        with self._lock:
            done, self._done = self._done, []
        return done

    def drain_killed(self) -> list[tuple[int, str, str]]:
        """取走并清空“用户停止”便条 (id,命令,终止前输出)（agent 注入上下文用）。
        【不】计入 has_pending，故只在已经因别的原因起轮时被捎带注入，停止动作本身不触发新一轮。"""
        with self._lock:
            notes, self._killed_notes = self._killed_notes, []
        return notes

    def due_checkins(self) -> list[tuple[BackgroundTask, str]]:
        """到点该 check-in 的【运行中】任务：重排下次 check-in、附当前输出快照（agent drain 时调）。
        重排即“消费”——同一次到点只会被 drain 一次。"""
        now = time.monotonic()
        with self._lock:
            due = [t for t in self._tasks.values()
                   if t.status == "running" and now >= t.next_checkin_at]
            for t in due:
                t.next_checkin_at = now + t.checkin_interval
        return [(t, self._snapshot(t.log_path)) for t in due]   # 读文件在锁外

    def has_pending(self) -> bool:
        """有没有待 agent 处理的后台事件：自然完成、或到点该 check-in 的运行中任务。
        （消费端 0.5s 轮询 + 空闲就起一轮自动接续。只查不消费。）"""
        now = time.monotonic()
        with self._lock:
            if self._done:
                return True
            return any(t.status == "running" and now >= t.next_checkin_at
                       for t in self._tasks.values())

    def elapsed(self, tid: int) -> int | None:
        """某【运行中】任务已经跑了多少秒（取整）。不在跑则 None。

        存在的理由：模型没有钟。它只能靠自己记"我调了几次 wait、每次多少秒"来推断
        过了多久，而 wait_bgtask 根本不等——记出来的账必然是错的。
        把真实秒数写进工具结果，它就不必记账了。"""
        with self._lock:
            task = self._tasks.get(tid)
            if task is None or task.status != "running":
                return None
            return int(task.elapsed())

    def check_output(self, tid: int) -> str | None:
        """读某【运行中】后台任务的当前输出快照（check_bgtask 拉取）。不在跑则 None。
        子 agent 无中间日志可看 → 回一句占位（完成后总结会经完成事件返回）。"""
        with self._lock:
            task = self._tasks.get(tid)
            if task is None or task.status != "running":
                return None
            if task.is_subagent:
                return "（子 agent 运行中；无中间输出可看，完成后会返回总结）"
            path = task.log_path
        return self._snapshot(path)

    def _tail(self, path: str, cap: int) -> tuple[str, bool]:
        """读文件【末尾】最多 cap 字（utf-8 容错；按字节读尾再解码，免切断多字节字符）。
        返回 (文本, 是否截了开头)。读不到 → ("", False)。"""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - cap * 4))           # 每字符最多 4 字节，读够覆盖 cap 字
                raw = f.read()
        except OSError:
            return "", False
        text = raw.decode("utf-8", errors="replace")
        truncated = size > len(raw)                      # 没读到文件开头 = 头被截
        if len(text) > cap:
            text, truncated = text[-cap:], True
        return text.rstrip(), truncated

    def _snapshot(self, path: str) -> str:
        """运行中读快照（check-in / check_bgtask）：尾部 output_cap + 截了头就标“更早已略”。
        日志是流式输出、最近的才要紧 → 只留末尾（不像工具结果头尾都留，免砍掉最新内容）；
        不许诺 read_file（运行中临时日志难定位）。整段含提示也不超 output_cap。"""
        text, truncated = self._tail(path, self._output_cap - len(_TRUNC_NOTE))
        return (_TRUNC_NOTE + text) if truncated else text

    def _read_completed(self, path: str) -> str:
        """完成时读输出：尽量全（末尾 FULL_CAP，不加提示）——内联尾部/全文外置由 agent 决定（见 _clip_offload）。"""
        return self._tail(path, FULL_CAP)[0]

    def defer_checkin(self, tid: int, sec: int) -> bool:
        """把某任务下次 check-in 推到 now+sec、后续间隔也设成 sec（wait_bgtask）。返回是否成功。"""
        with self._lock:
            task = self._tasks.get(tid)
            if task is None or task.status != "running":
                return False
            task.next_checkin_at = time.monotonic() + sec
            task.checkin_interval = sec
            return True

    def running(self) -> list[BackgroundTask]:
        """当前在跑的任务（TUI 显示用：命令 · 已 Ns）。"""
        with self._lock:
            return [t for t in self._tasks.values() if t.status == "running"]


def background_tools(bg: BackgroundManager) -> list[Tool]:
    """AI 可调的后台任务工具（闭包绑定到本会话的 bg，和 memory_tools 一个套路）：
    kill_bgtask（停）/ check_bgtask（拉当前输出）/ wait_bgtask（推迟下次 check-in、定 cadence）。"""
    def _tid(args: dict) -> int | None:
        try:
            return int(args.get("id"))
        except (TypeError, ValueError):
            return None

    def _kill(args: dict) -> str:
        tid = _tid(args)
        if tid is None:
            return "错误：id 必须是后台任务编号（整数，来自 bash 后台启动返回的 #编号）"
        snap = bg.kill(tid)
        if snap is None:
            return f"没有在运行的后台任务 #{tid}（可能已结束或编号错了）"
        if snap.strip():                       # bash：附上终止前的输出（和前台 bash 被 Ctrl+C 对齐）
            return f"已终止后台任务 #{tid}（以下为终止前的输出）\n{snap}"
        return f"已终止后台任务 #{tid}"

    def _check(args: dict) -> str:
        tid = _tid(args)
        if tid is None:
            return "错误：id 必须是后台任务编号（整数）"
        out = bg.check_output(tid)
        if out is None:
            return f"没有在运行的后台任务 #{tid}（可能已结束）"
        # 带上真实已运行秒数：模型没有钟，不给它读数它就只能自己记账，而它记的账是错的
        return f"[后台任务 #{tid} · 已运行 {bg.elapsed(tid)} 秒 · 当前输出]\n{out or '（暂无输出）'}"

    def _wait(args: dict) -> str:
        tid = _tid(args)
        if tid is None:
            return "错误：id 必须是后台任务编号（整数）"
        sec = int(args.get("seconds", DEFAULT_CHECKIN))
        run = bg.elapsed(tid)
        if not bg.defer_checkin(tid, sec):
            return f"没有在运行的后台任务 #{tid}（可能已结束）"
        # 【别写成"好的，N 秒后再来看"】——那句话读起来像"已经等过了"，模型会信，
        # 于是 wait 35 → check → wait 35 之后认定"都 70 秒了"，实际才过两秒。
        # 说清楚"这次调用没花掉时间"，并给出唯一正确的下一步：收尾、等叫醒。
        return (f"已把 #{tid} 的下次自动查看安排在 {sec} 秒之后（它到现在已运行 {run} 秒）。\n"
                f"注意：本次调用【立即返回，没有花掉任何时间】。想让时间过去，唯一的办法是"
                f"结束本轮——把当前进展告诉用户然后停下；到点或任务结束时系统会自动叫醒你，"
                f"届时再继续。接着调工具只是空转，时间不会因此流逝。")

    return [
        Tool(
            name="kill_bgtask",
            description="终止一个仍在运行的后台任务（id = bash 以 background 启动时返回的 #编号）。"
                        "用于后台任务卡住或不再需要时把它停掉（停掉后可重新用 bash 跑）。",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "后台任务编号（bash 后台启动返回的 #id）"}},
                "required": ["id"],
            },
            handler=_kill,
        ),
        Tool(
            name="check_bgtask",
            description="立即查看一个仍在运行的后台任务的当前输出（不等它完成）。"
                        "用于你想马上确认进展时主动拉一次。",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "后台任务编号"}},
                "required": ["id"],
            },
            handler=_check,
            read_only=True,   # check_output 只读快照（锁内读状态+读日志文件），不改任务状态
        ),
        Tool(
            name="wait_bgtask",
            description="给一个仍在运行的后台任务【安排】下次自动查看的时间：推迟到 N 秒后"
                        "（后续也按这个间隔）。用于任务还在正常跑、你不想频繁被打断时控制查看节奏。"
                        "【它立即返回、不会阻塞、不会让时间流逝】——不是 sleep。"
                        "调用后应当结束本轮（把进展告诉用户就停），到点系统会自动叫醒你；"
                        "接着调工具不会让任务跑得更快，只是空转。",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "后台任务编号"},
                    "seconds": {"type": "integer", "description": "多少秒后再 check-in（默认 120）"},
                },
                "required": ["id"],
            },
            handler=_wait,
        ),
    ]
