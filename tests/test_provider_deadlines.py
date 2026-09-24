"""单次/跨重试时限：缩短计时的模拟 HTTP，不访问模型 API。"""
import asyncio
import json
import threading

import httpx
import pytest

from mecode.agent import Agent
from mecode.config import Backend
from mecode.events import Done, Notice, ReasoningDelta, Retrying, TextDelta, ToolCall
from mecode.provider import Provider, ProviderError
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


END = b'data: [DONE]\n\n'
BACKEND = Backend(base_url='http://test/v1', model='kimi-k2.7-code', api_key='dummy')


def sse(**delta):
    return ('data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n').encode()


def tool(call_id):
    return sse(tool_calls=[{'index': 0, 'id': call_id, 'function': {
        'name': 'noop', 'arguments': '{}'}}])


class Body(httpx.AsyncByteStream):
    def __init__(self, prefix=(), *, stall=False, heartbeat=None, on_cancel=None):
        self.prefix, self.stall, self.heartbeat = prefix, stall, heartbeat
        self.on_cancel = on_cancel
        self.closed = threading.Event()
        self.cancelled = False
        self.ticks = 0

    async def __aiter__(self):
        try:
            for part in self.prefix:
                yield part
            if self.heartbeat is not None:
                while True:
                    self.ticks += 1
                    yield self.heartbeat
                    await asyncio.sleep(0.005)
            if self.stall:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if self.on_cancel:
                self.on_cancel()
            raise

    async def aclose(self):
        self.closed.set()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.delenv('MECODE_REQUEST_TIMEOUT_SECONDS', raising=False)
    monkeypatch.delenv('MECODE_TOTAL_TIMEOUT_SECONDS', raising=False)


def make_provider(*bodies, **settings):
    requests = []

    def handler(request):
        if requests:
            assert bodies[len(requests) - 1].closed.is_set(), '上次响应应先关闭'
        requests.append(request)
        assert len(requests) <= len(bodies), '出现多余请求'
        return httpx.Response(200, stream=bodies[len(requests) - 1])

    defaults = dict(request_timeout=0.15, total_timeout=3, backoff_base=0)
    defaults.update(settings)
    return Provider(BACKEND, transport=httpx.MockTransport(handler), **defaults), requests


def test_timeout_defaults_environment_and_explicit_overrides(monkeypatch):
    provider = Provider(BACKEND)
    assert (provider.request_timeout, provider.total_timeout) == (600, 1200)
    monkeypatch.setenv('MECODE_REQUEST_TIMEOUT_SECONDS', '12.5')
    monkeypatch.setenv('MECODE_TOTAL_TIMEOUT_SECONDS', '30')
    provider = Provider(BACKEND)
    assert (provider.request_timeout, provider.total_timeout) == (12.5, 30)
    provider = Provider(BACKEND, request_timeout=1, total_timeout=2)
    assert (provider.request_timeout, provider.total_timeout) == (1, 2)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'bad'])
def test_invalid_timeout_configuration_is_rejected(monkeypatch, value):
    monkeypatch.setenv('MECODE_REQUEST_TIMEOUT_SECONDS', value)
    with pytest.raises(ValueError, match='MECODE_REQUEST_TIMEOUT_SECONDS'):
        Provider(BACKEND)
    monkeypatch.delenv('MECODE_REQUEST_TIMEOUT_SECONDS')
    with pytest.raises(ValueError, match='MECODE_TOTAL_TIMEOUT_SECONDS'):
        Provider(BACKEND, total_timeout=value)


@pytest.mark.parametrize('heartbeat', [None, b': heartbeat\n\n', sse(reasoning_content='thinking')])
def test_single_request_expires_even_if_data_keeps_arriving(heartbeat):
    body = Body(stall=True, heartbeat=heartbeat)
    provider, requests = make_provider(body, max_retries=0)
    events = []
    with pytest.raises(ProviderError, match='单次请求超过.*1 次尝试上限'):
        events.extend(provider.stream([]))
    assert len(requests) == 1 and body.closed.is_set() and body.cancelled
    assert not any(isinstance(e, (Retrying, ToolCall, Done)) for e in events)
    if heartbeat is not None:
        assert body.ticks > 1
    # 总时限之外仍保留原来各网络阶段的 120 秒超时。
    assert requests[0].extensions['timeout'] == dict(connect=120, read=120, write=120, pool=120)


def test_single_timeout_discards_old_thought_and_tools_before_retry(tmp_path):
    first = Body([sse(reasoning_content='discard'), tool('old')], stall=True)
    second = Body([sse(reasoning_content='new'), tool('new'), END])
    third = Body([sse(content='answer'), END])
    provider, requests = make_provider(first, second, third)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, tools, system_prompt='s', store=store)
    events = list(agent.run_turn('hi'))
    assert len(requests) == 3 and executed == [{}]
    assert json.loads(requests[0].content) == json.loads(requests[1].content)
    assert [m['tool_call_id'] for m in agent.messages if m['role'] == 'tool'] == ['new']
    message = next(m for m in store.load_messages() if m.get('tool_calls'))
    assert message['reasoning_content'] == 'new'
    assert [e.reason for e in events if isinstance(e, Retrying)] == ['单次请求超过 0.15 秒']
    assert all(body.closed.is_set() for body in (first, second, third))


def test_total_budget_is_not_reset_by_retry():
    bodies = [Body(stall=True), Body(stall=True)]
    provider, requests = make_provider(*bodies, request_timeout=0.4, total_timeout=0.7,
                                      max_retries=20)
    events = []
    with pytest.raises(ProviderError, match='总等待时间已达 0.7 秒'):
        events.extend(provider.stream([]))
    assert len(requests) == 2 and all(body.closed.is_set() for body in bodies)
    assert len([e for e in events if isinstance(e, Retrying)]) == 1
    assert not any(isinstance(e, Done) for e in events)


def test_total_budget_includes_backoff_and_does_not_start_another_request():
    requests = []
    body = Body()

    def handler(request):
        requests.append(request)
        return httpx.Response(503, headers={'retry-after': '10'}, stream=body)

    provider = Provider(BACKEND, request_timeout=5, total_timeout=0.15,
                        transport=httpx.MockTransport(handler))
    events = []
    with pytest.raises(ProviderError, match='总等待时间'):
        events.extend(provider.stream([]))
    assert len(requests) == 1 and body.closed.is_set()
    assert [e.delay for e in events if isinstance(e, Retrying)] == [10]


@pytest.mark.parametrize('scope', ['single', 'total'])
@pytest.mark.parametrize('stage', ['headers', 'error-body', 'unfinished-line'])
def test_timeouts_cover_headers_error_body_and_incomplete_sse_line(scope, stage):
    requests, cleaned = [], threading.Event()
    body = Body([b'data: {"choices":'] if stage == 'unfinished-line' else [], stall=True)

    async def handler(request):
        requests.append(request)
        if stage == 'headers':
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return httpx.Response(400 if stage == 'error-body' else 200, stream=body)

    provider = Provider(BACKEND, max_retries=0,
                        request_timeout=0.15 if scope == 'single' else 5,
                        total_timeout=0.15 if scope == 'total' else 5,
                        transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match='单次请求' if scope == 'single' else '总等待时间'):
        list(provider.stream([]))
    assert len(requests) == 1
    assert cleaned.is_set() if stage == 'headers' else body.closed.is_set()


@pytest.mark.parametrize('scope', ['single', 'total'])
def test_deadline_preserves_partial_text_without_executing_tools_or_retrying(tmp_path, scope):
    body = Body([sse(content='partial'), tool('pending')], stall=True)
    provider, requests = make_provider(body, request_timeout=0.15 if scope == 'single' else 5,
                                      total_timeout=0.15 if scope == 'total' else 5)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, tools, system_prompt='s', store=store)
    events = []
    with pytest.raises(ProviderError, match='单次请求' if scope == 'single' else '总等待时间'):
        events.extend(agent.run_turn('hi'))
    assert store.load_messages() == [
        {'role': 'user', 'content': 'hi'}, agent._mode_message(),
        {'role': 'assistant', 'content': 'partial'}]
    assert not executed and len(requests) == 1 and body.closed.is_set()
    assert not any(isinstance(e, (Retrying, ToolCall, Done)) for e in events)


@pytest.mark.parametrize('scope', ['single', 'total'])
def test_user_stop_racing_deadline_keeps_interrupt_semantics(tmp_path, scope):
    body = Body([sse(content='partial')], stall=True)
    provider, requests = make_provider(body, request_timeout=0.15 if scope == 'single' else 5,
                                      total_timeout=0.15 if scope == 'total' else 5)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), system_prompt='s', store=store)
    # 在计时取消到达读取操作时置停止标志，稳定覆盖二者相遇，而非靠睡眠碰概率。
    body.on_cancel = agent.request_interrupt
    events = list(agent.run_turn('hi'))
    assert store.load_messages()[-1] == {'role': 'user', 'content': '[Request interrupted by user]'}
    assert any(isinstance(e, Notice) and '已打断' in e.text for e in events)
    assert not any(isinstance(e, Retrying) for e in events)
    assert len(requests) == 1 and body.closed.is_set()


def test_single_timeout_while_consumer_paused_waits_for_ack_before_retry():
    first = Body([sse(reasoning_content='old')], stall=True)
    second = Body([sse(content='answer'), END])
    provider, requests = make_provider(first, second)
    gen = provider.stream([])
    try:
        assert next(gen) == ReasoningDelta('old')
        assert first.closed.wait(2), '消费端暂停也必须按时关闭请求'
        assert len(requests) == 1
        assert isinstance(next(gen), Retrying)
        assert len(requests) == 1, 'Retrying 尚未确认，不能抢跑下一次请求'
        assert list(gen) == [TextDelta('answer'), Done('stop')]
        assert len(requests) == 2 and second.closed.is_set()
    finally:
        gen.close()


def test_total_timeout_closes_response_while_consumer_paused():
    body = Body([sse(content='partial')], stall=True)
    provider, requests = make_provider(body, request_timeout=5, total_timeout=0.15)
    gen = provider.stream([])
    try:
        assert next(gen) == TextDelta('partial')
        assert body.closed.wait(2), '总时限不能依赖消费者推动下一步'
        with pytest.raises(ProviderError, match='总等待时间'):
            list(gen)
        assert len(requests) == 1
    finally:
        gen.close()


def test_total_deadline_during_attempt_cleanup_does_not_cancel_close_twice():
    class SlowClose(Body):
        async def aclose(self):
            # 单次时限先到，总时限在连接清理期间到；清理必须完成再返回。
            await asyncio.sleep(0.25)
            self.closed.set()

    body = SlowClose(stall=True)
    provider, requests = make_provider(body, request_timeout=0.1, total_timeout=0.2)
    with pytest.raises(ProviderError, match='总等待时间'):
        list(provider.stream([]))
    assert body.closed.is_set() and len(requests) == 1


def test_next_call_gets_a_new_total_budget_after_previous_timeout():
    first = Body(stall=True)
    second = Body([sse(content='resumed'), END])
    provider, requests = make_provider(first, second, request_timeout=5, total_timeout=0.15)
    with pytest.raises(ProviderError, match='总等待时间'):
        list(provider.stream([]))
    assert list(provider.stream([])) == [TextDelta('resumed'), Done('stop')]
    assert len(requests) == 2 and second.closed.is_set()


@pytest.mark.parametrize('scope', ['single', 'total'])
def test_user_stop_during_timeout_cleanup_waits_for_close(tmp_path, scope):
    class SlowClose(Body):
        async def aclose(self):
            agent.request_interrupt()
            # 给独立停止监视器/同步消费端机会发出取消，不能把清理再次打断。
            await asyncio.sleep(0.65)
            self.closed.set()

    body = SlowClose([sse(content='partial')], stall=True)
    provider, requests = make_provider(body, request_timeout=0.1 if scope == 'single' else 5,
                                      total_timeout=0.1 if scope == 'total' else 5)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), system_prompt='s', store=store)
    events = list(agent.run_turn('hi'))
    assert body.closed.is_set() and len(requests) == 1
    assert store.load_messages()[-1] == {'role': 'user', 'content': '[Request interrupted by user]'}
    assert any(isinstance(e, Notice) and '已打断' in e.text for e in events)
