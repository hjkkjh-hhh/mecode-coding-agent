"""真实 Provider + 假 SSE：重试边界、上下文隔离、资源释放和停止优先级。"""
import json
import threading

import httpx
import pytest

from mecode.agent import Agent
from mecode.config import Backend
from mecode.events import Done, Notice, ReasoningDelta, Retrying, TextDelta, ToolCall, Usage
from mecode.provider import Provider, ProviderError
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


def sse(delta=None, **body):
    if delta is not None:
        body["choices"] = [{"delta": delta}]
    return ("data: " + json.dumps(body) + "\n\n").encode()


END = b"data: [DONE]\n\n"
ANSWER = [sse({"content": "answer"}), END]


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts, close_error=None):
        self.parts = parts
        self.closed = False
        self.close_error = close_error

    async def __aiter__(self):
        for part in self.parts:
            if callable(part):
                part()
            elif isinstance(part, Exception):
                raise part
            else:
                yield part

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def sequence(*streams, max_retries=3):
    requests = []

    def handler(request):
        # 第二次请求必须在第一条响应关闭之后开始。
        if requests:
            assert streams[len(requests) - 1].closed
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=streams[len(requests) - 1])

    provider = Provider(Backend(base_url="http://test/v1", model="kimi-k2.7-code", api_key="dummy"),
                        max_retries=max_retries, backoff_base=0,
                        transport=httpx.MockTransport(handler))
    return provider, requests


@pytest.mark.parametrize("prefix", [
    [],
    [b": heartbeat\n\n", sse({"role": "assistant"})],
    [sse({"reasoning_content": "discard this"})],
    [sse({"tool_calls": [{"index": 0, "id": "old", "function": {
        "name": "old_tool", "arguments": '{"x":'}}]})],
])
def test_首片或仅思考或工具碎片超时会重新请求(prefix):
    first = Stream(prefix + [httpx.ReadTimeout("timeout")])
    second = Stream(ANSWER)
    provider, requests = sequence(first, second)
    events = list(provider.stream([{"role": "user", "content": "hi"}]))
    assert len(requests) == 2 and requests[0] == requests[1]
    assert first.closed and second.closed
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["answer"]
    assert not any(isinstance(e, ToolCall) for e in events)
    assert [e.attempt for e in events if isinstance(e, Retrying)] == [1]
    assert isinstance(events[-1], Done)


def test_思考重试后存档和下一轮只包含新思考且保留已报告用量(tmp_path):
    first = Stream([
        sse({"reasoning_content": "discard this"}),
        sse(usage={"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
                   "completion_tokens_details": {"reasoning_tokens": 3}}),
        httpx.ReadTimeout("timeout"),
    ])
    second = Stream([
        sse({"reasoning_content": "new thought"}),
        sse({"content": "answer"}),
        sse(usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                   "completion_tokens_details": {"reasoning_tokens": 1}}), END,
    ])
    provider, requests = sequence(first, second, Stream(ANSWER))
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), system_prompt="s", store=store)
    events = list(agent.run_turn("hi"))
    message = store.load_messages()[-1]
    assert message == {"role": "assistant", "content": "answer",
                       "reasoning_content": "new thought",
                       "usage": {"completion": 8, "reasoning": 4}}
    assert any(isinstance(e, Retrying) for e in events)
    assert [e.completion_tokens for e in events if isinstance(e, Usage)] == [3, 5]
    assert requests[0] == requests[1]
    list(agent.run_turn("continue"))
    assert "discard this" not in json.dumps(requests[2])
    assert [m["reasoning_content"] for m in requests[2]["messages"]
            if m["role"] == "assistant"] == ["new thought"]


def test_已输出正文不重试且正文落盘(tmp_path):
    stream = Stream([sse({"content": "partial"}), httpx.ReadTimeout("timeout")])
    provider, requests = sequence(stream)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), store=store)
    with pytest.raises(httpx.ReadTimeout):
        list(agent.run_turn("hi"))
    assert len(requests) == 1 and stream.closed
    assert store.load_messages()[-1] == {"role": "assistant", "content": "partial"}


def test_已交付完整工具调用后关闭连接失败也不重试():
    stream = Stream([sse({"tool_calls": [{"index": 0, "id": "c1", "function": {
        "name": "noop", "arguments": "{}"}}]}), END],
        close_error=httpx.ReadTimeout("close failed"))
    provider, requests = sequence(stream)
    events = []
    with pytest.raises(httpx.ReadTimeout):
        for event in provider.stream([]):
            events.append(event)
    assert len(requests) == 1
    assert len([e for e in events if isinstance(e, ToolCall)]) == 1
    assert not any(isinstance(e, Retrying) for e in events)


def test_工具碎片重试不执行旧调用且新调用只执行一次():
    first = Stream([sse({"tool_calls": [{"index": 0, "id": "old", "function": {
        "name": "noop", "arguments": '{"x":'}}]}), httpx.ReadTimeout("timeout")])
    second = Stream([sse({"tool_calls": [{"index": 0, "id": "new", "function": {
        "name": "noop", "arguments": "{}"}}]}), END])
    provider, requests = sequence(first, second, Stream(ANSWER))
    executed = []
    registry = ToolRegistry()
    registry.register(Tool(name="noop", description="", parameters={"type": "object"},
                           handler=lambda args: executed.append(args) or "done"))
    agent = Agent(provider, registry, system_prompt="s")
    list(agent.run_turn("hi"))
    assert len(requests) == 3 and executed == [{}]
    assert [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"] == ["new"]


def test_超时重试用尽不被Agent当空响应再次重试():
    streams = [Stream([httpx.ReadTimeout("timeout")]) for _ in range(3)]
    provider, requests = sequence(*streams, max_retries=2)
    agent = Agent(provider, ToolRegistry(), system_prompt="s")
    events = []
    with pytest.raises(ProviderError, match="模型回复超时，3 次尝试均未完成"):
        for event in agent.run_turn("hi"):
            events.append(event)
    assert len(requests) == 3 and all(s.closed for s in streams)
    assert [e.attempt for e in events if isinstance(e, Retrying)] == [1, 2]
    assert not any(m["role"] == "assistant" for m in agent.messages)


def test_请求前已停止不发HTTP():
    provider, requests = sequence()
    assert list(provider.stream([], should_stop=lambda: True)) == []
    assert not requests


@pytest.mark.parametrize("kind", ["timeout", "503"])
def test_退避期间停止不会发下一次请求(monkeypatch, kind):
    import mecode.provider as module
    stop = threading.Event()
    waiting = threading.Event()
    delays = []
    closed = []
    requests = []

    class ClosingStream(Stream):
        async def aclose(self):
            await super().aclose()
            closed.append(True)

    def handler(request):
        requests.append(request)
        if kind == "503":
            return httpx.Response(503, headers={"retry-after": "20"}, stream=ClosingStream([]))
        return httpx.Response(200, stream=ClosingStream([httpx.ReadTimeout("timeout")]))

    wait = module._wait_before_retry

    async def observe_wait(delay, should_stop):
        assert closed, "不能占着上一次响应等待重试"
        delays.append(delay)
        waiting.set()
        return await wait(delay, should_stop)

    monkeypatch.setattr(module, "_wait_before_retry", observe_wait)
    provider = Provider(Backend(base_url="http://test", model="kimi-k2.7-code", api_key="dummy"),
                        transport=httpx.MockTransport(handler))
    events, errors = [], []

    def consume():
        try:
            events.extend(provider.stream([], should_stop=stop.is_set))
        except BaseException as exc:
            errors.append(exc)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        assert waiting.wait(2), "没有进入退避等待"
        stop.set()
        consumer.join(2)
        assert not consumer.is_alive(), "退避取消后没有收尾"
    finally:
        stop.set()
        consumer.join(2)
    assert not errors
    assert len(requests) == 1 and delays == [20 if kind == "503" else 1]
    retry = next(e for e in events if isinstance(e, Retrying))
    assert retry.delay == (20 if kind == "503" else 1)


@pytest.mark.parametrize("partial", [False, True])
def test_主动停止遇到读取超时仍保存中断标记(tmp_path, partial):
    stream = Stream([])
    provider, requests = sequence(stream)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), system_prompt="s", store=store)
    stream.parts = ([sse({"content": "partial"})] if partial else []) + [
        agent.request_interrupt, httpx.ReadTimeout("timeout"),
    ]
    events = list(agent.run_turn("hi"))
    expected = [{"role": "user", "content": "hi"}, agent._mode_message()]
    if partial:
        expected.append({"role": "assistant", "content": "partial"})
    expected.append({"role": "user", "content": "[Request interrupted by user]"})
    assert store.load_messages() == expected
    assert len(requests) == 1 and stream.closed
    assert not any(isinstance(e, Retrying) for e in events)
    assert any(isinstance(e, Notice) and "已打断" in e.text for e in events)


def test_收到重试事件时停止不再请求():
    stop = threading.Event()
    provider, requests = sequence(Stream([httpx.ReadTimeout("timeout")]))
    gen = provider.stream([], should_stop=stop.is_set)
    assert isinstance(next(gen), Retrying)
    stop.set()
    assert list(gen) == [] and len(requests) == 1
