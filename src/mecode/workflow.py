"""Workflow 编排：声明式 JSON 描述"哪些阶段、谁依赖谁"，harness 按图【确定性】调度子 agent。

和主循环的分工（路线图定案：循环做核心、图当上层可选编排）：
- 循环（agent.py）：控制流交给模型——灵活，但同一任务两次可能走两条路。
- workflow（这里）：控制流交给结构——模型只负责【写出结构】和【在节点里干活】；
  该有哪些环节、谁等谁由代码保证（10 个审查一个不少、结果必然汇合、谁挂了看得见）。
  适合要覆盖度/交叉验证/批量的活（并行审查 N 个文件→汇总、多方案各写一版→评比）。

结构 = {"stages": [{"id", "prompt", "after": [依赖 id...], "description"?}]}：
- after 为空的阶段并行跑（扇出）；有依赖的等【所有】依赖完成（汇合 barrier）。
- 调度按"就绪即跑"：每轮把所有依赖已满足的阶段丢进线程池并发（执行器 = SubagentRunner.run，
  和前台 subagent 并发批同一套：干净上下文、共享打断、Ctrl+C 连带停）。
- 结果流：上游阶段的输出【全文注入】下游 prompt；超长走 truncate_output 头尾截断 +
  全文外置落盘（附"完整输出已存盘：<路径>"，和工具结果同一约定，模型自己会去读）。
- 失败语义：阶段异常 → 标记 failed，【下游照跑】，注入处写明"该依赖阶段失败"——
  汇总节点看得见谁挂了、还能基于成功的部分出结论。
"""
from __future__ import annotations
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .tools import Tool, truncate_output

MAX_STAGES = 20          # 阶段数上限：防模型写出失控的巨图（已远超日常"扇出+汇总"所需）
MAX_PARALLEL = 5         # 同时在跑的子 agent 上限：每个都是一条模型流，太多会打爆后端并发/限流
_STAGE_RESULT_MAX = 6000   # 上游结果注入下游的截断上限（字符），同工具结果默认
_STAGE_KEEP_TAIL = 1200


@dataclass
class Stage:
    id: str
    prompt: str
    after: list[str]
    description: str = ""
    status: str = "pending"     # pending / running / done / failed
    result: str = ""            # done=子 agent 总结；failed=错误信息


@dataclass
class WorkflowRun:
    """一次 workflow 执行的完整状态。TUI 轮询它画分支树（和 BackgroundManager 一样线程安全读）。"""
    stages: list[Stage] = field(default_factory=list)
    active: bool = False

    def by_id(self, sid: str) -> Stage:
        return next(s for s in self.stages if s.id == sid)


class SlotGroup:
    """workflow 各并行阶段的 ProcSlot 集合（后台模式用）：kill 一次杀掉所有在跑阶段的前台 bash。
    对齐 BackgroundTask.sub_slot 的 .kill() 协议 → _kill_task 不用改就能杀 workflow。"""
    def __init__(self) -> None:
        self._slots: set = set()
        self._lock = threading.Lock()

    def add(self, slot) -> None:
        with self._lock:
            self._slots.add(slot)

    def kill(self) -> None:
        with self._lock:
            slots = list(self._slots)
        for s in slots:
            try:
                s.kill()
            except Exception:
                pass          # 杀进程路径：一个 slot 杀失败不能挡住杀剩下的（否则其余阶段的 bash 泄漏）


def validate_workflow(stages_raw: list) -> str:
    """校验结构：id 非空唯一、prompt 非空、依赖存在、无环、数量上限。返回 "" 或错误说明（人话，给模型自纠）。"""
    if not stages_raw:
        return "stages 不能为空"
    if any(not isinstance(s, dict) for s in stages_raw):
        return "stages 每项都要是对象 {id, prompt, after}（不能是字符串等）"
    if len(stages_raw) > MAX_STAGES:
        return f"阶段数 {len(stages_raw)} 超上限 {MAX_STAGES}——拆成多次 workflow 或合并同类阶段"
    ids = [str(s.get("id", "")).strip() for s in stages_raw]
    if any(not i for i in ids):
        return "每个阶段都要有非空 id"
    if len(set(ids)) != len(ids):
        return "阶段 id 重复"
    idset = set(ids)
    for s in stages_raw:
        if not str(s.get("prompt", "")).strip():
            return f"阶段 {s.get('id')} 缺 prompt"
        for dep in s.get("after") or []:
            if dep not in idset:
                return f"阶段 {s.get('id')} 依赖的 {dep} 不存在"
    # 拓扑检查无环（Kahn）：能全部剥完 = 无环
    remaining = {str(s["id"]): set(s.get("after") or []) for s in stages_raw}
    while remaining:
        ready = [i for i, deps in remaining.items() if not deps]
        if not ready:
            return f"依赖成环：{', '.join(sorted(remaining))} 互相等待"
        for i in ready:
            remaining.pop(i)
        for deps in remaining.values():
            deps.difference_update(ready)
    return ""


def _inject_results(stage: Stage, run: WorkflowRun, offload) -> str:
    """拼该阶段的最终 prompt：自身 prompt + 每个依赖阶段的结果段（截断+外置，同工具结果约定）。"""
    if not stage.after:
        return stage.prompt
    parts = [stage.prompt, "\n\n以下是上游阶段的结果："]
    for dep_id in stage.after:
        dep = run.by_id(dep_id)
        if dep.status == "failed":
            parts.append(f"\n【阶段 {dep_id} 执行失败】{dep.result}\n（基于其余成功阶段继续；结论里注明该阶段缺失）")
            continue
        text = dep.result
        if len(text) > _STAGE_RESULT_MAX and offload is not None:
            path = offload(f"workflow-{dep_id}", text)
            text = truncate_output(text, _STAGE_RESULT_MAX, _STAGE_KEEP_TAIL) \
                + f"\n（阶段 {dep_id} 完整输出已存盘：{path}，需要全文用 read_file 读）"
        parts.append(f"\n【阶段 {dep_id} 的结果】\n{text}")
    return "\n".join(parts)


def run_workflow(stages_raw: list, runner, run_state: WorkflowRun,
                 offload=None, interrupt: threading.Event | None = None,
                 slots: SlotGroup | None = None) -> str:
    """执行 workflow（阻塞至全部完成/失败——工具层把它包成后台任务跑，见 workflow_tools）。
    runner=SubagentRunner；run_state 由调用方持有供 TUI 轮询；offload(name, content)->path 外置超长
    结果（无 store 时 None=只截断不存盘）。slots 给了则各阶段走 run_for_bg（独立打断 + 登记 ProcSlot，
    kill 能杀正跑的 bash）；不给则走 run（前台语义，测试用）。
    调度：就绪即跑（依赖全 done/failed 就入池），并发上限 MAX_PARALLEL。返回给模型的汇总报告。"""
    # 字段访问和 validate 同款用 .get：本函数契约上只接收已过 validate 的输入，但两处假设要一致——
    # validate 用 .get 宽取、这里 [] 直取，就埋了"校验过却构造炸"的错位（审查子 agent 抓到的真问题）。
    run_state.stages = [Stage(id=str(s.get("id", "")), prompt=str(s.get("prompt", "")),
                              after=[str(d) for d in (s.get("after") or [])],
                              description=str(s.get("description", "")))
                        for s in stages_raw]
    run_state.active = True
    try:
        pending = {s.id for s in run_state.stages}
        futures = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            while pending or futures:
                if interrupt is not None and interrupt.is_set():
                    for s in run_state.stages:            # 没跑的标失败（在跑的由共享打断标志自己停）
                        if s.status == "pending":
                            s.status, s.result = "failed", "用户中断"
                    break
                # 就绪 = 所有依赖都已终态（done/failed）
                done_ids = {s.id for s in run_state.stages if s.status in ("done", "failed")}
                ready = [sid for sid in list(pending)
                         if set(run_state.by_id(sid).after) <= done_ids]
                for sid in ready:
                    pending.discard(sid)
                    st = run_state.by_id(sid)
                    st.status = "running"
                    futures[pool.submit(_run_stage, st, run_state, runner, offload,
                                        interrupt, slots)] = sid
                if not futures:                            # 没有在跑也没有就绪（理论上只剩空）
                    break
                fin, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for fut in fin:
                    futures.pop(fut)
    finally:
        run_state.active = False
    # 汇总报告：树状一览 + 末端阶段（无人依赖的"出口"）的结果全文
    lines = ["workflow 完成："]
    for s in run_state.stages:
        mark = {"done": "✓", "failed": "✗"}.get(s.status, "?")
        dep = f"（依赖 {', '.join(s.after)}）" if s.after else ""
        lines.append(f"{mark} {s.id}{dep}：{s.description or s.prompt[:40]}"
                     + (f" —— 失败：{s.result[:100]}" if s.status == "failed" else ""))
    depended = {d for s in run_state.stages for d in s.after}
    for s in run_state.stages:
        if s.id not in depended and s.status == "done":    # 出口阶段：结果全文给模型
            lines.append(f"\n【出口阶段 {s.id} 的结果】\n{s.result}")
    return "\n".join(lines)


def _run_stage(stage: Stage, run: WorkflowRun, runner, offload,
               interrupt: threading.Event | None, slots: SlotGroup | None) -> None:
    try:
        prompt = _inject_results(stage, run, offload)
        if slots is not None:                 # 后台模式：独立打断 + 登记 ProcSlot（kill 可杀其正跑 bash）
            stage.result = runner.run_for_bg(prompt, interrupt or threading.Event(),
                                             on_slot=slots.add)
        else:                                 # 前台语义（测试/无 slots 场景）
            stage.result = runner.run(prompt)
        stage.status = "done"
    except Exception as e:
        stage.result = f"{type(e).__name__}: {e}"
        stage.status = "failed"


def workflow_tools(runner, run_state: WorkflowRun, offload=None) -> list[Tool]:
    """run_workflow 工具（只给主 agent，子 agent 不给——防递归，同 subagent 工具的约定）。
    【后台执行】：workflow 动辄几分钟（多个子 agent 串并联），前台会长时间阻塞本轮 → 包成后台任务
    （bg.start_fn，同 subagent background:true 的语义）：立即返回任务 id，完成自动通知主 agent
    接续（drain 注入汇总报告）；kill_bgtask 停（置独立打断 + SlotGroup 杀所有在跑阶段的 bash）。"""
    def _run(args: dict, slot=None, bg=None) -> str:
        stages = args.get("stages")
        if not isinstance(stages, list):
            return "错误：stages 要是数组（每项 {id, prompt, after}）"
        err = validate_workflow(stages)
        if err:
            return f"错误：{err}"
        if run_state.active:
            return "错误：已有 workflow 在执行，等它完成再发起新的"
        if bg is None:                       # 无后台管理器（子 agent 内不会有此工具；兜底直跑）
            return run_workflow(stages, runner, run_state, offload=offload)
        run_state.active = True              # 先占位：防同轮连发两个 workflow（后台线程启动有间隙）
        interrupt = threading.Event()        # 独立打断（同后台子 agent）：Ctrl+C 不停它，kill_bgtask 停
        slots = SlotGroup()
        n = len(stages)
        desc = f"workflow · {n} 阶段"
        tid = bg.start_fn(
            lambda: run_workflow(stages, runner, run_state,
                                 offload=offload, interrupt=interrupt, slots=slots),
            command=f"run_workflow（{n} 个阶段）：" + "、".join(str(s.get("id")) for s in stages),
            description=desc, interrupt=interrupt, sub_slot=slots)
        return (f"workflow 已在后台启动 · 任务 #{tid} · 共 {n} 个阶段"
                f"（完成会把汇总报告通知你；侧栏 Workflow 树可看进度；需要时 kill_bgtask({tid}) 停）")

    return [Tool(
        name="run_workflow",
        description="按【固定流程图】编排多个子 agent：你给出阶段列表（每阶段一个子任务 prompt）和依赖关系，"
                    "由系统确定性调度——after 为空的阶段【并行】跑，有依赖的等依赖全完成后自动开始，"
                    "上游结果自动注入下游 prompt。适合【要覆盖度/要汇合】的活：并行审查 N 个文件→汇总报告、"
                    "多方案各写一版→评比。和直接发多个 subagent 的区别：环节和汇合由系统保证、不会漏，"
                    "某阶段失败下游照跑（会看到失败标记）。简单的一两步任务别用它，直接做或用 subagent。"
                    "阶段 prompt 要一次讲清（做什么、在哪、怎样算完成），子 agent 无法回问。"
                    "【后台执行】：调用立即返回任务编号、不阻塞你，完成后汇总报告会自动通知你"
                    "（届时再基于报告继续）；进度在侧栏 Workflow 树；kill_bgtask(编号) 可停。",
        parameters={
            "type": "object",
            "properties": {
                "stages": {
                    "type": "array",
                    "description": f"阶段列表（最多 {MAX_STAGES} 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "阶段唯一标识，短英文，如 r1、summary"},
                            "prompt": {"type": "string", "description": "该阶段子 agent 的完整任务描述"},
                            "after": {"type": "array", "items": {"type": "string"},
                                      "description": "依赖的阶段 id 列表；空/省略=无依赖（立即并行跑）"},
                            "description": {"type": "string", "description": "3-5 词短标签（进度树显示用）"},
                        },
                        "required": ["id", "prompt"],
                    },
                },
            },
            "required": ["stages"],
        },
        handler=_run,
        takes_slot=True,   # execute 注入 (slot, bg)：bg 用来把 workflow 包成后台任务
    )]
