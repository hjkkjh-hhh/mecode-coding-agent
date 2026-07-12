"""Workflow 编排：结构校验（无环/唯一/上限）、就绪即跑的调度顺序、并行扇出、失败继续汇合、
结果注入与截断外置、中断、工具包装。runner 用假的（记录调用顺序），不联网。"""
import threading
import time

from mecode.workflow import (
    MAX_STAGES, WorkflowRun, workflow_tools, run_workflow, validate_workflow,
)


class _FakeRunner:
    """假 SubagentRunner：记录 (stage prompt, 开始时刻)，可注入每阶段行为。run/run_for_bg 双接口。"""
    def __init__(self, behavior=None, delay=0.0):
        self.calls = []
        self.behavior = behavior or {}     # 关键词 → 返回值 或 Exception
        self.delay = delay
        self._lock = threading.Lock()

    def run(self, prompt: str) -> str:
        with self._lock:
            self.calls.append((prompt, time.monotonic()))
        if self.delay:
            time.sleep(self.delay)
        for key, val in self.behavior.items():
            if key in prompt:
                if isinstance(val, Exception):
                    raise val
                return val
        return f"完成：{prompt[:20]}"

    def run_for_bg(self, prompt: str, interrupt, on_slot) -> str:
        on_slot(object())                  # 模拟交出 ProcSlot
        return self.run(prompt)


# ---- 校验 ----

def test_validate_各种非法():
    assert "不能为空" in validate_workflow([])
    assert "超上限" in validate_workflow([{"id": f"s{i}", "prompt": "x"} for i in range(MAX_STAGES + 1)])
    assert "非空 id" in validate_workflow([{"id": "", "prompt": "x"}])
    assert "重复" in validate_workflow([{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}])
    assert "缺 prompt" in validate_workflow([{"id": "a", "prompt": " "}])
    assert "不存在" in validate_workflow([{"id": "a", "prompt": "x", "after": ["ghost"]}])
    assert "成环" in validate_workflow([{"id": "a", "prompt": "x", "after": ["b"]},
                                        {"id": "b", "prompt": "y", "after": ["a"]}])
    assert validate_workflow([{"id": "a", "prompt": "x"},
                              {"id": "b", "prompt": "y", "after": ["a"]}]) == ""


# ---- 调度 ----

def test_依赖顺序_上游先跑_结果注入下游():
    r = _FakeRunner(behavior={"审查A": "A没问题"})
    run = WorkflowRun()
    out = run_workflow([
        {"id": "a", "prompt": "审查A"},
        {"id": "b", "prompt": "汇总", "after": ["a"]},
    ], r, run)
    assert [c[0].split("\n")[0] for c in r.calls] == ["审查A", "汇总"]   # a 先 b 后
    assert "A没问题" in r.calls[1][0]                                    # 上游结果注入下游 prompt
    assert "【阶段 a 的结果】" in r.calls[1][0]
    assert run.by_id("b").status == "done" and "出口阶段 b" in out       # 出口=无人依赖的 b


def test_无依赖并行扇出():
    r = _FakeRunner(delay=0.15)
    t0 = time.monotonic()
    run_workflow([{"id": f"s{i}", "prompt": f"任务{i}"} for i in range(3)], r, WorkflowRun())
    elapsed = time.monotonic() - t0
    assert elapsed < 0.15 * 3 * 0.8          # 三个 0.15s 并行 → 远小于串行 0.45s
    assert len(r.calls) == 3


def test_失败标记_下游照跑_报告可见():
    r = _FakeRunner(behavior={"坏的": RuntimeError("boom")})
    run = WorkflowRun()
    out = run_workflow([
        {"id": "good", "prompt": "好的任务"},
        {"id": "bad", "prompt": "坏的任务"},
        {"id": "sum", "prompt": "汇总", "after": ["good", "bad"]},
    ], r, run)
    assert run.by_id("bad").status == "failed" and "boom" in run.by_id("bad").result
    sum_prompt = next(c[0] for c in r.calls if c[0].startswith("汇总"))
    assert "【阶段 bad 执行失败】" in sum_prompt        # 下游看得见失败
    assert "【阶段 good 的结果】" in sum_prompt          # 成功的照常注入
    assert run.by_id("sum").status == "done"
    assert "✗ bad" in out and "✓ good" in out            # 报告树里标了


def test_超长结果_截断加外置指针():
    r = _FakeRunner(behavior={"大输出": "长" * 9000})
    stored = {}
    def offload(name, content):
        stored[name] = content
        return f"/fake/{name}.txt"
    run_workflow([
        {"id": "big", "prompt": "大输出任务"},
        {"id": "next", "prompt": "下游", "after": ["big"]},
    ], r, WorkflowRun(), offload=offload)
    next_prompt = next(c[0] for c in r.calls if c[0].startswith("下游"))
    assert "完整输出已存盘：/fake/workflow-big.txt" in next_prompt   # 外置指针（同工具结果约定）
    assert len(next_prompt) < 9000                                   # 注入的是截断版
    assert stored["workflow-big"] == "长" * 9000                     # 全文落盘


def test_中断_未跑的标失败():
    interrupt = threading.Event()
    r = _FakeRunner(delay=0.1)
    orig = r.run
    def run_and_interrupt(prompt):
        interrupt.set()                      # 第一个阶段跑完就置位中断
        return orig(prompt)
    r.run = run_and_interrupt
    run = WorkflowRun()
    run_workflow([
        {"id": "a", "prompt": "先跑"},
        {"id": "b", "prompt": "后续", "after": ["a"]},
    ], r, run, interrupt=interrupt)
    assert run.by_id("b").status == "failed" and "中断" in run.by_id("b").result


# ---- 工具包装（后台语义）----

def test_workflow_tool_校验拦截和执行中拒绝():
    r = _FakeRunner()
    run = WorkflowRun()
    tool = workflow_tools(r, run)[0]
    assert tool.name == "run_workflow" and tool.takes_slot
    assert "错误" in tool.handler({"stages": "不是数组"})
    assert "成环" in tool.handler({"stages": [{"id": "a", "prompt": "x", "after": ["a"]}]})
    out = tool.handler({"stages": [{"id": "a", "prompt": "干活"}]})   # bg=None 兜底：同步直跑
    assert "workflow 完成" in out and "✓ a" in out
    run.active = True                        # 模拟执行中再发起
    assert "已有 workflow 在执行" in tool.handler({"stages": [{"id": "b", "prompt": "y"}]})


def test_workflow_tool_后台执行_立即返回_完成入队():
    from mecode.background import BackgroundManager
    r = _FakeRunner(delay=0.05)
    run = WorkflowRun()
    bg = BackgroundManager()
    tool = workflow_tools(r, run)[0]
    out = tool.handler({"stages": [{"id": "a", "prompt": "活A"},
                                   {"id": "b", "prompt": "汇总", "after": ["a"]}]},
                       slot=None, bg=bg)
    assert "后台启动" in out and "#1" in out            # 立即返回任务 id、不阻塞
    for _ in range(100):                                 # 等后台跑完（两阶段串行 ~0.1s）
        done = bg.drain_completions()
        if done:
            break
        time.sleep(0.03)
    assert done and "workflow 完成" in done[0].output    # 完成入队：汇总报告随通知注入主 agent
    assert "✓ a" in done[0].output and run.by_id("b").status == "done"
    assert run.active is False


def test_validate_非dict元素拒绝():
    assert "都要是对象" in validate_workflow(["不是对象"])
    assert "都要是对象" in validate_workflow([{"id": "a", "prompt": "x"}, 42])
