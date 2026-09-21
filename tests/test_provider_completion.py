"""模型结束信号：不把 HTTP EOF 当完成，工具整批交付，兼容尾部 usage。仅 MockTransport。"""
import json
from pathlib import Path

import httpx
import pytest

from mecode.agent import Agent
from mecode.config import Backend
from mecode.events import Done, Notice, Retrying, TextDelta, ToolCall, Usage
from mecode.provider import Provider, ProviderError
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


END = b'data: [DONE]\n\n'


def chunk(delta=None, *, reason=None, **extra):
    body = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': reason}], **extra}
    return ('data: ' + json.dumps(body) + '\n\n').encode()


def tool_chunk(call_id='c1', args='{}'):
    return chunk({'tool_calls': [{'index': 0, 'id': call_id, 'function': {
        'name': 'noop', 'arguments': args}}]})


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aiter__(self):
        for part in self.parts:
            if callable(part):
                part()
            else:
                yield part

    async def aclose(self):
        self.closed = True


def make_provider(*streams, max_retries=0, model='MiniMax-M3'):
    requests = []

    def handler(request):
        if requests:
            assert streams[len(requests) - 1].closed, '重试前应关闭旧响应'
        requests.append(json.loads(request.content))
        assert len(requests) <= len(streams), '出现多余请求'
        return httpx.Response(200, stream=streams[len(requests) - 1])

    return Provider(Backend(base_url='http://test/v1', model=model, api_key='dummy'),
                    max_retries=max_retries, backoff_base=0,
                    transport=httpx.MockTransport(handler)), requests


_RECORDED_ENDINGS = json.loads(
    (Path(__file__).parent / 'fixtures' / 'provider_sse_endings.json').read_text(encoding='utf-8')
)['cases']


@pytest.mark.parametrize('case', _RECORDED_ENDINGS,
                         ids=[f"{case['model']}-{case['scenario']}" for case in _RECORDED_ENDINGS])
def test_recorded_cloud_endings_preserve_usage_exactly_once(case):
    has_tools = case['scenario'] == 'tool'
    prefix = tool_chunk() if has_tools else chunk({'content': 'OK'})
    stream = Stream([prefix, case['sse_tail'].encode('utf-8')])
    provider, requests = make_provider(stream, model=case['model'])
    events = list(provider.stream([]))
    assert [e for e in events if isinstance(e, Done)] == [Done(case['expected_finish_reason'])]
    # Kimi 在 choice 内和顶层各有一份相同用量，只能交付一次；其余实测形态也要保留。
    usage = case['expected_usage']
    assert [e for e in events if isinstance(e, Usage)] == [Usage(
        prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'],
        total_tokens=usage['total_tokens'],
        reasoning_tokens=usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0),
    )]
    assert [e for e in events if isinstance(e, ToolCall)] == (
        [ToolCall(id='c1', name='noop', arguments={})] if has_tools else [])
    if has_tools:
        assert next(i for i, e in enumerate(events) if isinstance(e, Usage)) < next(
            i for i, e in enumerate(events) if isinstance(e, ToolCall))
    assert len(requests) == 1 and stream.closed


def test_done_finishes_without_reading_to_http_eof():
    def must_not_read():
        pytest.fail('收到 [DONE] 后不应继续读取或等待 HTTP EOF')

    stream = Stream([chunk({'content': 'OK'}), END, must_not_read])
    provider, requests = make_provider(stream)
    assert list(provider.stream([])) == [TextDelta('OK'), Done('stop')]
    assert stream.closed and len(requests) == 1


@pytest.mark.parametrize('error, detail', [
    ({'message': '模拟服务错误', 'code': 'server_error'}, '模拟服务错误'),
    ('模拟服务错误', '模拟服务错误'),
    ({'code': 'server_error'}, '{"code": "server_error"}'),
])
def test_sse_error_surfaces_without_retry_or_empty_response_fallback(error, detail):
    frame = ('data: ' + json.dumps({'error': error}) + '\n\n').encode()
    stream = Stream([frame, END])
    provider, requests = make_provider(stream, max_retries=3)
    events = []
    with pytest.raises(ProviderError) as caught:
        events.extend(Agent(provider, ToolRegistry(), system_prompt='s').run_turn('hi'))
    assert str(caught.value) == f'模型服务返回错误：{detail}'
    assert not any(isinstance(e, (Retrying, ToolCall, Done)) for e in events)
    assert len(requests) == 1 and stream.closed


def test_sse_error_preserves_partial_text_and_does_not_execute_buffered_tools(tmp_path):
    frame = b'data: {"error":{"message":"upstream failed"}}\n\n'
    stream = Stream([chunk({'content': 'partial'}), tool_chunk(),
                     chunk(reason='tool_calls'), frame])
    provider, requests = make_provider(stream, max_retries=3)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    events = []
    with pytest.raises(ProviderError, match='upstream failed'):
        events.extend(Agent(provider, tools, system_prompt='s', store=store).run_turn('hi'))
    assert store.load_messages() == [
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'partial'}]
    assert not executed and len(requests) == 1 and stream.closed
    assert not any(isinstance(e, (Retrying, ToolCall, Done)) for e in events)


def test_null_error_does_not_interrupt_normal_response():
    stream = Stream([chunk({'content': 'OK'}, error=None), END])
    provider, requests = make_provider(stream)
    assert list(provider.stream([])) == [TextDelta('OK'), Done('stop')]
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize('has_tools', [False, True])
@pytest.mark.parametrize('ending', ['done', 'reason', 'both'])
def test_accept_explicit_endings_and_preserve_trailing_usage(has_tools, ending):
    reason = 'tool_calls' if has_tools else 'stop'
    parts = [tool_chunk() if has_tools else chunk({'content': 'OK'})]
    if ending in ('reason', 'both'):
        parts.append(chunk(reason=reason))
    # MiniMax-M3 实测：finish_reason 后是独立 usage 帧，再 EOF，没有 [DONE]。
    parts.append(b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n')
    if ending in ('done', 'both'):
        parts.append(END)
    stream = Stream(parts)
    provider, requests = make_provider(stream)
    events = list(provider.stream([]))
    assert events[-1] == Done(reason=reason)
    assert len([e for e in events if isinstance(e, Done)]) == 1
    assert [e.total_tokens for e in events if isinstance(e, Usage)] == [15]
    assert len([e for e in events if isinstance(e, ToolCall)]) == int(has_tools)
    if has_tools:
        assert next(i for i, e in enumerate(events) if isinstance(e, Usage)) < next(
            i for i, e in enumerate(events) if isinstance(e, ToolCall))
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize('parts', [
    [], [b': heartbeat\n\n'], [chunk({'role': 'assistant'})],
    [chunk({'reasoning_content': 'only thought'})],
    [b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":3}}\n\n'],
    [tool_chunk()], [tool_chunk(args='{"x":')],
])
def test_eof_without_marker_never_emits_tool_or_done(parts):
    stream = Stream(parts)
    provider, requests = make_provider(stream)
    events = []
    with pytest.raises(ProviderError, match='缺少结束标志'):
        events.extend(provider.stream([]))
    assert not any(isinstance(e, (ToolCall, Done)) for e in events)
    assert len(requests) == 1 and stream.closed


def test_partial_text_is_saved_without_retry_or_tool_execution(tmp_path):
    stream = Stream([chunk({'content': 'partial'}), tool_chunk()])
    provider, requests = make_provider(stream, max_retries=3)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, tools, system_prompt='s', store=store)
    events = []
    with pytest.raises(ProviderError, match='未收到结束标志'):
        events.extend(agent.run_turn('hi'))
    assert not executed and len(requests) == 1 and stream.closed
    assert store.load_messages() == [
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'partial'}]
    assert not any(isinstance(e, Retrying) for e in events)


def test_missing_marker_retries_without_executing_old_tool_or_mixing_reasoning():
    streams = [
        Stream([chunk({'reasoning_content': 'old reasoning'}), tool_chunk('old')]),
        Stream([chunk({'reasoning_content': 'new reasoning'}), tool_chunk('new'),
                chunk(reason='tool_calls')]),
        Stream([chunk({'content': 'answer'}), END]),
    ]
    provider, requests = make_provider(*streams, max_retries=1)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    agent = Agent(provider, tools, system_prompt='s')
    events = list(agent.run_turn('hi'))
    assert len(requests) == 3 and requests[0] == requests[1]
    assert executed == [{}] and all(stream.closed for stream in streams)
    assert [m['tool_call_id'] for m in agent.messages if m['role'] == 'tool'] == ['new']
    assistant = next(m for m in agent.messages if m.get('tool_calls'))
    assert assistant['reasoning_content'] == 'new reasoning'
    assert [e.reason for e in events if isinstance(e, Retrying)] == ['响应缺少结束标志']


def test_missing_marker_retry_limit_does_not_trigger_agent_empty_response_retry():
    streams = [Stream([chunk({'role': 'assistant'})]) for _ in range(3)]
    provider, requests = make_provider(*streams, max_retries=2)
    agent = Agent(provider, ToolRegistry(), system_prompt='s')
    events = []
    with pytest.raises(ProviderError, match='3 次尝试上限'):
        events.extend(agent.run_turn('hi'))
    assert len(requests) == 3 and all(stream.closed for stream in streams)
    assert [e.attempt for e in events if isinstance(e, Retrying)] == [1, 2]
    assert not any(isinstance(e, Notice) and '空响应' in e.text for e in events)
    assert not any(m['role'] == 'assistant' for m in agent.messages)


@pytest.mark.parametrize('reason', ['length', 'content_filter', 'unexpected_reason'])
@pytest.mark.parametrize('with_done', [False, True])
def test_non_success_finish_reason_overrides_done_and_blocks_entire_tool_batch(reason, with_done):
    # 即使参数恰好是合法 JSON，[DONE] 也不能把显式截断/失败变成成功。
    parts = [tool_chunk(), chunk(reason=reason)] + ([END] if with_done else [])
    stream = Stream(parts)
    provider, requests = make_provider(stream, max_retries=3)
    executed = []
    tools = ToolRegistry()
    tools.register(Tool(name='noop', description='', parameters={'type': 'object'},
                        handler=lambda args: executed.append(args) or 'done'))
    events = []
    with pytest.raises(ProviderError):
        events.extend(Agent(provider, tools, system_prompt='s').run_turn('hi'))
    assert not executed and len(requests) == 1 and stream.closed
    assert not any(isinstance(e, (ToolCall, Done, Retrying)) for e in events)


@pytest.mark.parametrize('reason', ['length', 'content_filter'])
def test_truncated_text_is_preserved_but_not_reported_as_success(tmp_path, reason):
    stream = Stream([chunk({'content': 'partial'}), chunk(reason=reason), END])
    provider, requests = make_provider(stream, max_retries=3)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    with pytest.raises(ProviderError):
        list(Agent(provider, ToolRegistry(), system_prompt='s', store=store).run_turn('hi'))
    assert store.load_messages()[-1] == {'role': 'assistant', 'content': 'partial'}
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize('has_marker', [False, True])
def test_stop_at_eof_remains_user_interrupt(tmp_path, has_marker):
    stream = Stream([])
    provider, requests = make_provider(stream, max_retries=3)
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(provider, ToolRegistry(), system_prompt='s', store=store)
    stream.parts = [chunk({'content': 'partial'}), tool_chunk()]
    if has_marker:
        stream.parts.append(chunk(reason='tool_calls'))
    stream.parts.append(agent.request_interrupt)
    events = list(agent.run_turn('hi'))
    assert len(requests) == 1 and stream.closed
    assert not any(isinstance(e, Retrying) for e in events)
    assert store.load_messages() == [
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'partial'},
        {'role': 'user', 'content': '[Request interrupted by user]'}]
    assert any(isinstance(e, Notice) and '已打断' in e.text for e in events)
