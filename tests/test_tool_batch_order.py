"""前台子 agent 必须位于批次末尾：验证真实主循环、调度与持久化，不访问模型 API。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from mecode.agent import Agent
from mecode.config import agent_config
from mecode.events import Done, Notice, TextDelta, ToolCall, ToolResult, ToolStarted
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


class ScriptedProvider:
    def __init__(self, batches, repeat=False):
        self.batches = batches
        self.repeat = repeat
        self.requests = []

    def stream(self, messages, tools=None, should_stop=None):
        index = len(self.requests)
        self.requests.append(deepcopy(messages))
        if self.repeat or index < len(self.batches):
            yield from self.batches[0 if self.repeat else index]
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("完成")
            yield Done(reason="stop")


@pytest.fixture
def build(tmp_path, monkeypatch):
    def make(*specs, repeat=False, max_iterations=8):
        target = tmp_path / "written.txt"
        operations, gates = [], []
        batches = []
        for batch_index, names in enumerate(specs):
            batch = []
            for index, name in enumerate(names):
                arguments = {}
                if name.startswith("fg") or name == "background":
                    arguments = {"prompt": name, "description": name}
                    if name == "background":
                        arguments["background"] = True
                    elif name == "fg1":
                        arguments["background"] = False
                    tool = "subagent"
                elif name in ("read", "write"):
                    tool = "read_file" if name == "read" else "write_file"
                    arguments = {"path": str(target), "content": "changed"}
                elif name == "background_bash":
                    tool, arguments = "bash", {"command": "test", "background": True}
                else:
                    tool = name
                batch.append(ToolCall(f"b{batch_index}-c{index}", tool, arguments))
            batches.append(batch)

        def read(args):
            operations.append("read")
            return target.read_text(encoding="utf-8") if target.exists() else "missing"

        def write(args):
            operations.append("write")
            target.write_text(args["content"], encoding="utf-8")
            return "written"

        def bash(args, slot, bg):
            operations.append("background_bash")
            return "已启动后台任务 #2"

        registry = ToolRegistry()
        for name, handler, read_only, takes_slot in (
            ("read_file", read, True, False),
            ("write_file", write, False, False),
            ("bash", bash, False, True),
        ):
            registry.register(Tool(name, "test", {}, handler,
                                   read_only=read_only, takes_slot=takes_slot))
        provider = ScriptedProvider(batches, repeat=repeat)
        # 短的逻辑 cwd 避免 Windows 把 tmp_path 再编码进存档路径后超过路径长度限制。
        store = SessionStore(root=tmp_path / "state", cwd=Path("C:/batch-test"), session_id="batch")
        config = replace(agent_config, max_iterations=max_iterations,
                         context_limit=1_000_000, no_edit_reminder_turns=0)
        agent = Agent(provider, registry, system_prompt="s", store=store, config=config)

        def gate(tc):
            gates.append(tc.id)
            return True, False

        def foreground(prompt):
            operations.append(prompt)
            return f"summary:{prompt}"

        def background(prompt, runner, description=""):
            operations.append("background")
            return 1

        monkeypatch.setattr(agent, "_gate", gate)
        monkeypatch.setattr(agent._subagent, "run", foreground)
        monkeypatch.setattr(agent._bg, "start_subagent", background)
        return SimpleNamespace(agent=agent, provider=provider, batches=batches, store=store,
                               target=target, operations=operations, gates=gates)

    return make


def tool_results(messages):
    return [message for message in messages if message.get("role") == "tool"]


@pytest.mark.parametrize("names", [
    ["fg1", "read"],
    ["write", "fg1", "read"],
    ["background", "write", "fg1", "read"],
    ["fg1", "background"],
    ["fg1", "background_bash"],
    ["fg1", "fg2", "read"],
    ["fg1", "exit_plan"],
    ["fg1", "not_exists"],
])
def test_invalid_batch_runs_nothing_and_returns_every_error_to_model(build, names):
    ctx = build(names)
    events = list(ctx.agent.run_turn("执行"))

    assert ctx.operations == []
    assert ctx.gates == []                       # 连审批也不应开始，更不能先执行前缀工具。
    assert not ctx.target.exists()
    assert len(ctx.provider.requests) == 2       # 返回规则错误后，正常让模型继续。
    expected_ids = [tc.id for tc in ctx.batches[0]]
    received = tool_results(ctx.provider.requests[1])
    assert [m["tool_call_id"] for m in received] == expected_ids
    assert all("工具调用顺序无效" in m["content"] and "本批所有工具均未执行" in m["content"]
               for m in received)
    assert all("下一轮" in m["content"] for m in received)
    assert tool_results(ctx.store.load_messages()) == received
    assert [ev.id for ev in events if isinstance(ev, ToolStarted)] == expected_ids
    assert [ev.id for ev in events if isinstance(ev, ToolResult)] == expected_ids


def test_model_can_correct_order_and_execute_once(build):
    ctx = build(["write", "fg1", "read"], ["write", "read", "fg1"])
    list(ctx.agent.run_turn("执行"))

    assert ctx.operations == ["write", "read", "fg1"]
    assert ctx.target.read_text(encoding="utf-8") == "changed"
    assert ctx.gates == [tc.id for tc in ctx.batches[1]]
    assert len(ctx.provider.requests) == 3
    results = tool_results(ctx.provider.requests[2])
    assert len(results) == 6
    assert all("本批所有工具均未执行" in m["content"] for m in results[:3])
    assert [m["content"] for m in results[3:]] == ["written", "changed", "summary:fg1"]


def test_foreground_suffix_waits_for_prefix_and_still_runs_in_parallel(build, monkeypatch):
    ctx = build(["read", "write", "read", "fg1", "fg2"])
    both_running = threading.Barrier(2)

    def foreground(prompt):
        assert ctx.operations == ["read", "write", "read"]
        assert ctx.target.read_text(encoding="utf-8") == "changed"
        both_running.wait(timeout=3)             # 串行实现会超时；不依赖机器快慢估算耗时。
        return f"summary:{prompt}"

    monkeypatch.setattr(ctx.agent._subagent, "run", foreground)
    list(ctx.agent.run_turn("执行"))
    received = tool_results(ctx.provider.requests[1])
    assert [m["content"] for m in received[:3]] == ["missing", "written", "changed"]
    assert {m["content"] for m in received[3:]} == {"summary:fg1", "summary:fg2"}
    assert {m["tool_call_id"] for m in received} == {tc.id for tc in ctx.batches[0]}


@pytest.mark.parametrize("names", [
    ["background", "read"],                     # 后台子 agent 不开启“前台末尾段”。
    ["write", "background", "read", "fg1"],
    ["background_bash", "read", "fg1"],
    ["fg1"],
])
def test_legal_background_launches_keep_their_position(build, names):
    ctx = build(names)
    list(ctx.agent.run_turn("执行"))
    assert ctx.operations == names
    assert len(ctx.provider.requests) == 2
    assert len(tool_results(ctx.provider.requests[1])) == len(names)


def test_close_while_showing_rejection_keeps_all_errors_persisted(build):
    ctx = build(["write", "fg1", "read"])
    stream = ctx.agent.run_turn("执行")
    assert isinstance(next(stream), ToolStarted)
    stream.close()

    assert ctx.operations == []
    received = tool_results(ctx.store.load_messages())
    assert [m["tool_call_id"] for m in received] == [tc.id for tc in ctx.batches[0]]
    assert all("本批所有工具均未执行" in m["content"] for m in received)
    assert len(ctx.provider.requests) == 1


def test_repeated_invalid_batches_are_bounded_by_iteration_limit(build):
    ctx = build(["fg1", "read"], repeat=True, max_iterations=2)
    events = list(ctx.agent.run_turn("执行"))
    assert len(ctx.provider.requests) == 2
    assert ctx.operations == []
    assert len(tool_results(ctx.agent.messages)) == 4
    assert any(isinstance(ev, Notice) and "最大循环" in ev.text for ev in events)
