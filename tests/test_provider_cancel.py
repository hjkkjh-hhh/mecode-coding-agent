"""异步 HTTP 取消：真实本地套接字 + 受控异步流，不调用模型服务。"""
import asyncio
import json
import select
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import mecode.provider as provider_module
from mecode.agent import Agent
from mecode.config import Backend
from mecode.events import Notice, ReasoningDelta, Retrying, TextDelta
from mecode.provider import Provider
from mecode.session import SessionStore
from mecode.tools import ToolRegistry


def sse(**delta):
    return ('data: ' + json.dumps({"choices": [{"delta": delta}]}) + '\n\n').encode()


END = b'data: [DONE]\n\n'
MARKER = {"role": "user", "content": "[Request interrupted by user]"}


@pytest.fixture
def workers(monkeypatch):
    """捕获本次测试实际创建的线程/循环，验证在返回调用者前已完成清理。"""
    captured = []
    original = provider_module._StreamWorker

    class Worker(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(provider_module, '_StreamWorker', Worker)
    yield captured
    for worker in captured:
        worker.cancel()
        worker.thread.join(3)
        assert not worker.thread.is_alive(), '遗留 I/O 线程'
        assert worker.done.is_set()
        assert worker._loop is None and worker._request is None


class Consumer:
    def __init__(self, events):
        self.events, self.errors = [], []
        self.text_seen = threading.Event()
        self.reasoning_seen = threading.Event()

        def run():
            try:
                for event in events:
                    self.events.append(event)
                    if isinstance(event, TextDelta):
                        self.text_seen.set()
                    if isinstance(event, ReasoningDelta):
                        self.reasoning_seen.set()
            except BaseException as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def finish(self):
        self.thread.join(2)
        assert not self.thread.is_alive(), '停止后仍在等待 HTTP 数据'
        assert not self.errors


@contextmanager
def stalled_server(stage):
    """收完请求后停住；通过 recv 的 EOF 确认客户端实际断开，而非仅退出显示。"""
    entered = threading.Event()
    disconnected = threading.Event()
    shutdown = threading.Event()
    requests = []
    state = {'stage': stage}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            current = state['stage']
            self.close_connection = True
            if current == 'ok':
                data = sse(content='resumed') + END
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if current != 'headers':
                self.send_response(400 if current == 'error-body' else 200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if current == 'partial':
                    self.wfile.write(sse(content='partial'))
                elif current == 'reasoning':
                    self.wfile.write(sse(reasoning_content='thinking'))
                elif current == 'unfinished-line':
                    self.wfile.write(b'data: {"choices":')
                self.wfile.flush()
            entered.set()
            while not shutdown.is_set():
                ready, _, _ = select.select([self.connection], [], [], 0.05)
                if ready:
                    try:
                        data = self.connection.recv(1024)
                    except ConnectionError:
                        data = b''
                    if not data:
                        disconnected.set()
                        return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield server.server_port, entered, disconnected, requests, state
    finally:
        shutdown.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('stage', ['headers', 'body', 'unfinished-line', 'partial', 'reasoning', 'error-body'])
def test_real_http_stop_and_resume(stage, tmp_path, monkeypatch, workers):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    with stalled_server(stage) as (port, entered, disconnected, requests, state):
        provider = Provider(Backend(base_url=f'http://127.0.0.1:{port}/v1',
                                    model='kimi-k2.7-code', api_key='dummy'))
        store = SessionStore(root=tmp_path, cwd=tmp_path)
        agent = Agent(provider, ToolRegistry(), system_prompt='s', store=store)
        consumer = Consumer(agent.run_turn('hi'))
        try:
            assert entered.wait(4), '请求没有到达本地测试服务器'
            if stage == 'partial':
                assert consumer.text_seen.wait(2)
            if stage == 'reasoning':
                assert consumer.reasoning_seen.wait(2)
            started = time.perf_counter()
            agent.request_interrupt()
            consumer.finish()
            elapsed = time.perf_counter() - started
            assert disconnected.wait(2), '客户端任务返回了，但服务器仍看不到连接关闭'
            assert len(requests) == 1, '主动停止不能重试'
            assert not workers[0].thread.is_alive()
            assert not any(isinstance(e, Retrying) for e in consumer.events)
            assert any(isinstance(e, Notice) and '已打断' in e.text for e in consumer.events)
            expected = [{'role': 'user', 'content': 'hi'}, agent._mode_message()]
            if stage == 'partial':
                expected.append({'role': 'assistant', 'content': 'partial'})
            expected.append(MARKER)
            assert store.load_messages() == expected
            print(f'{stage}: cancel+cleanup {elapsed:.3f}s')

            # 同一 Provider 下一轮还能使用，不继承已经取消的任务/循环。
            state['stage'] = 'ok'
            list(agent.run_turn('continue'))
            assert len(requests) == 2
            wire_expected = [{k: v for k, v in m.items() if k != '_mode'} for m in expected]
            assert requests[1]['messages'] == [{'role': 'system', 'content': 's'}] + wire_expected + [
                {'role': 'user', 'content': 'continue'}]
            assert store.load_messages()[-1]['content'] == 'resumed'
        finally:
            agent.request_interrupt()
            consumer.thread.join(3)


class PausedStream(httpx.AsyncByteStream):
    def __init__(self, *, first=True):
        self.first = first
        self.reading = threading.Event()
        self.closed = threading.Event()
        self.cancelled = threading.Event()
        self.read_past_first = threading.Event()

    async def __aiter__(self):
        if self.first:
            yield sse(content='first')
        self.read_past_first.set()
        self.reading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def aclose(self):
        self.closed.set()


def make_provider(handler):
    return Provider(Backend(base_url='http://test/v1', model='kimi-k2.7-code', api_key='dummy'),
                    transport=httpx.MockTransport(handler), backoff_base=0)


def test_close_generator_cleans_up_without_read_ahead(workers):
    body = PausedStream()
    provider = make_provider(lambda req: httpx.Response(200, stream=body))
    gen = provider.stream([])
    assert next(gen) == TextDelta('first')
    # 消费者仍停在 yield 时，HTTP 线程不读取下一片。
    assert not body.read_past_first.wait(0.1)
    gen.close()
    gen.close()
    assert body.closed.is_set() and not workers[0].thread.is_alive()


def test_stop_while_consumer_paused_at_yield(workers):
    body = PausedStream()
    stop = threading.Event()
    provider = make_provider(lambda req: httpx.Response(200, stream=body))
    gen = provider.stream([], should_stop=stop.is_set)
    assert next(gen) == TextDelta('first')
    stop.set()
    assert workers[0].done.wait(2), '消费者暂停时，独立监视器也必须能取消 HTTP'
    assert body.closed.is_set()
    assert list(gen) == []


def test_cancel_before_response_exists(workers):
    entered, cleaned = threading.Event(), threading.Event()
    stop = threading.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()  # 连接/握手等尚未返回 response 的阶段
        finally:
            cleaned.set()

    provider = make_provider(handler)
    consumer = Consumer(provider.stream([], should_stop=stop.is_set))
    try:
        assert entered.wait(2)
        stop.set()
        consumer.finish()
        assert cleaned.is_set() and not workers[0].thread.is_alive()
    finally:
        stop.set()
        consumer.thread.join(3)


def test_shared_provider_cancels_only_its_own_request(workers):
    first = PausedStream(first=False)
    release_second = threading.Event()
    second_entered, second_closed = threading.Event(), threading.Event()

    class SecondStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            second_entered.set()
            while not release_second.is_set():
                await asyncio.sleep(0.01)
            yield sse(content='second') + END

        async def aclose(self):
            second_closed.set()

    def handler(request):
        name = json.loads(request.content)['messages'][0]['content']
        return httpx.Response(200, stream=first if name == 'first' else SecondStream())

    provider = make_provider(handler)
    stop = threading.Event()
    a = Consumer(provider.stream([{'role': 'user', 'content': 'first'}], should_stop=stop.is_set))
    b = Consumer(provider.stream([{'role': 'user', 'content': 'second'}]))
    try:
        assert first.reading.wait(2) and second_entered.wait(2)
        stop.set()
        a.finish()
        assert first.closed.is_set() and first.cancelled.is_set()
        assert not second_closed.is_set() and b.thread.is_alive()
        release_second.set()
        b.finish()
        assert [e.text for e in b.events if isinstance(e, TextDelta)] == ['second']
        assert second_closed.is_set()
    finally:
        stop.set()
        release_second.set()
        a.thread.join(3)
        b.thread.join(3)


def test_keyboard_interrupt_in_sync_consumer_cleans_up(monkeypatch, workers):
    body = PausedStream(first=False)
    provider = make_provider(lambda req: httpx.Response(200, stream=body))
    def interrupted_get(queue, *args, **kwargs):
        assert body.reading.wait(2)
        raise KeyboardInterrupt

    monkeypatch.setattr(provider_module.queue.Queue, 'get', interrupted_get)
    with pytest.raises(KeyboardInterrupt):
        list(provider.stream([]))
    assert body.closed.is_set() and body.cancelled.is_set()
    assert not workers[0].thread.is_alive()


def test_cancel_before_worker_registers_loop(monkeypatch, workers):
    entered = threading.Event()
    original = provider_module._StreamWorker._run_async
    stop = threading.Event()
    requests = []

    async def delayed_start(worker):
        entered.set()
        while not worker._cancelled.is_set():
            await asyncio.sleep(0.01)
        await original(worker)

    monkeypatch.setattr(provider_module._StreamWorker, '_run_async', delayed_start)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse(content='unexpected') + END)

    consumer = Consumer(make_provider(handler).stream([], should_stop=stop.is_set))
    try:
        assert entered.wait(2)
        stop.set()
        consumer.finish()
        assert not requests and not workers[0].thread.is_alive()
    finally:
        stop.set()
        consumer.thread.join(3)


def test_interrupt_just_after_thread_start(monkeypatch, workers):
    original_start = threading.Thread.start

    def interrupted_start(thread):
        original_start(thread)
        if thread.name.startswith('mecode-http-'):
            raise KeyboardInterrupt

    monkeypatch.setattr(threading.Thread, 'start', interrupted_start)
    provider = make_provider(lambda req: httpx.Response(200, content=sse(content='unused') + END))
    with pytest.raises(KeyboardInterrupt):
        list(provider.stream([]))
    assert workers and not workers[0].thread.is_alive()
