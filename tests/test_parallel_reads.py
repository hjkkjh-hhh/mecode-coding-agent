"""只读工具并发执行（read_only 标记 + run_turn 连续只读分组）。

固化的不变量：
- 连续的只读调用攒成一组并发跑：墙钟≈最慢那个，不是求和
- 非只读工具是屏障：读写交错时执行顺序同纯串行（不重排、写后读到新内容）
- 并发组内 ToolStarted/ToolResult 都带 tool_call_id，可一一配对（记录按完成序）
- 批内单个被拒不影响其余执行
"""
import threading
import time

from mecode.agent import Agent
from mecode.events import Done, TextDelta, ToolCall, ToolResult, ToolStarted
from mecode.permission import ALLOW, DENY
from mecode.tools import Tool, ToolRegistry, default_registry


class _BatchProvider:
    """第一次调用吐一批工具调用，之后回纯文本结束。"""
    def __init__(self, calls):
        self.calls = calls
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n == 1:
            for c in self.calls:
                yield c
        else:
            yield TextDelta("完")
        yield Done(reason="stop")


def _tool(name, handler, read_only=False):
    return Tool(name=name, description="测试用", read_only=read_only,
                parameters={"type": "object", "properties": {}}, handler=handler)


def _reg(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def test_核心只读工具已标_写类未标():
    reg = default_registry()
    for name in ("read_file", "grep", "glob", "web_search", "web_fetch"):
        assert reg.is_read_only(name), name
    for name in ("bash", "edit_file", "write_file", "exit_plan"):
        assert not reg.is_read_only(name), name
    assert not reg.is_read_only("不存在的工具")          # 未注册 → False（走串行）


def test_连续只读批_并发跑_墙钟约等于最慢而非求和():
    slow = _tool("slowread", lambda a: (time.sleep(0.3), "ok")[1], read_only=True)
    calls = [ToolCall(id=f"c{i}", name="slowread", arguments={}) for i in range(3)]
    a = Agent(_BatchProvider(calls), _reg(slow), system_prompt="s")
    t0 = time.perf_counter()
    evs = list(a.run_turn("go"))
    dt = time.perf_counter() - t0
    assert dt < 0.6, f"3 个各 0.3s 应≈0.3s(并发)、不是 0.9s(求和)；实际 {dt:.2f}s"
    results = [e for e in evs if isinstance(e, ToolResult)]
    assert len(results) == 3 and all(r.result == "ok" for r in results)
    # 并发组内 Started/Result 都带 id、可一一配对（TUI 不串位的底座）
    assert {e.id for e in evs if isinstance(e, ToolStarted)} == {"c0", "c1", "c2"}
    assert {e.id for e in results} == {"c0", "c1", "c2"}


def test_读写交错_非只读当屏障_不重排():
    order, lock = [], threading.Lock()

    def mk(name):
        def h(a, _n=name):
            with lock:
                order.append(_n)
            return "ok"
        return h

    reg = _reg(_tool("r1", mk("r1"), read_only=True),
               _tool("r2", mk("r2"), read_only=True),
               _tool("w", mk("w")),
               _tool("r3", mk("r3"), read_only=True))
    calls = [ToolCall(id="c1", name="r1", arguments={}),
             ToolCall(id="c2", name="r2", arguments={}),
             ToolCall(id="c3", name="w", arguments={}),
             ToolCall(id="c4", name="r3", arguments={})]
    a = Agent(_BatchProvider(calls), reg, system_prompt="s")
    list(a.run_turn("go"))
    assert set(order[:2]) == {"r1", "r2"}    # 屏障前的只读组先执行完（组内完成序不定）
    assert order[2] == "w"                   # 写按模型给的位置执行，没被只读组挤到前/后
    assert order[3] == "r3"                  # 屏障后的读最后（单个走串行路径）


def test_批内单个被拒_其余照跑():
    class _Policy:                           # bad 拒、其余放（只看工具名，够用）
        def decide(self, name, args):
            return DENY if name == "bad" else ALLOW

    ran = []
    reg = _reg(_tool("okread", lambda a: (ran.append(1), "ok")[1], read_only=True),
               _tool("bad", lambda a: "不该执行到", read_only=True))
    calls = [ToolCall(id="c1", name="okread", arguments={}),
             ToolCall(id="c2", name="bad", arguments={}),
             ToolCall(id="c3", name="okread", arguments={})]
    a = Agent(_BatchProvider(calls), reg, system_prompt="s", policy=_Policy())
    evs = list(a.run_turn("go"))
    by_id = {e.id: e.result for e in evs if isinstance(e, ToolResult)}
    assert "用户拒绝执行工具 bad" in by_id["c2"]        # 被拒的拿到统一叫停文案
    assert by_id["c1"] == "ok" and by_id["c3"] == "ok"  # 其余不受连坐
    assert len(ran) == 2                                # bad 的 handler 从未执行
    # 三个 id 的 tool 结果都记进对话（无孤儿 tool_call → 下轮请求不 400）
    recorded = {m["tool_call_id"] for m in a.messages if m.get("role") == "tool"}
    assert recorded == {"c1", "c2", "c3"}
