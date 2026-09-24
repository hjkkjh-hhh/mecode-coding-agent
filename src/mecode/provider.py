"""Provider 层：和模型对话的唯一出入口。

把"脏"的 OpenAI 兼容 SSE 流，整理成干净的 events（见 events.py）。
这是 raw_probe.py 里那段拼包逻辑的"产品化"——但不再 print，而是 yield 事件。

设计：
- reasoning / content 碎片：来一片 yield 一片（流式 UX，逐字显示）
- tool_calls：跨多片，按 index 累加 arguments，【攒完整】再 yield（上层要完整的才能执行）
- 最后 yield Done(finish_reason)，让主循环判断"该执行工具还是该结束"
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import math
import os
import queue
import threading
from typing import AsyncIterator, Awaitable, Callable, Iterator, TypeVar

import httpx

from .config import Backend
from .events import Done, Event, ReasoningDelta, Retrying, TextDelta, ToolCall, Usage
from .registry import ThinkingProfile, context_window_for, profile_for

# 可重试的瞬时 HTTP 状态：限流 + 网关/服务端临时错误
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# 只存不发的【mecode 自有字段】：transcript 里带着，发送前由 _for_wire 剥掉。
#   usage —— 每条 assistant 响应的输出用量（agent._assistant_msg 写入），
#            存下来 UI 才能刷新后仍累计出整个会话产出了多少 token。
#   _mode —— 程序生成的模式声明标记，供去重与 resume 使用，不属于 API 消息字段。
# 加新的内部字段【必须】同步这里，否则会随消息发给后端（严格的后端见到不认识的键会 400）。
_INTERNAL_KEYS = frozenset({"usage", "_mode"})
_T = TypeVar("_T")


class ProviderError(Exception):
    """后端请求失败的【人话】版本（4xx/重试用尽的 5xx/连不上/流内错误）。
    str(e) 即给用户看的完整信息——上层（TUI/CLI）不用再翻 httpx 细节。"""


class _IncompleteStreamError(ProviderError):
    """HTTP 已结束，但模型没有确认结束；仅在尚未交付正文/工具时允许重试。"""


# 状态码 → 人话开头（后面拼服务端 error.message 摘要）
_STATUS_HINT = {
    400: "请求被后端拒绝（参数错误）",
    401: "API Key 无效或已过期",
    403: "无权限访问该模型（Key 权限不足或未开通）",
    404: "模型名不存在或接口路径错误",
    429: "限流或配额不足（余额/并发）",
}


def _friendly_http_error(resp: httpx.Response, model: str) -> ProviderError:
    """把 4xx/5xx 响应转成人话 ProviderError：状态码开头 + 服务端 error.message 摘要。"""
    code = resp.status_code
    hint = _STATUS_HINT.get(code, "服务端错误" if code >= 500 else "请求失败")
    detail = ""
    try:
        body = resp.json()
        detail = ((body.get("error") or {}).get("message")
                  or body.get("message") or "")[:200]
    except Exception:
        detail = resp.text[:200]
    msg = f"{hint}（HTTP {code}，模型 {model}）"
    if detail:
        msg += f"：{detail}"
    return ProviderError(msg)


def _is_retriable_status(code: int) -> bool:
    return code in _RETRIABLE_STATUS


def _backoff(attempt: int, base: float, cap: float, retry_after: str | None = None) -> float:
    # 指数退避封顶；429 带 Retry-After（秒）时优先听服务端的。
    if retry_after:
        try:
            return min(float(retry_after), cap)
        except ValueError:
            pass
    return min(base * (2 ** attempt), cap)


async def _wait_before_retry(delay: float, should_stop: Callable[[], bool] | None) -> bool:
    """异步退避可被请求任务的取消打断；True 表示可以继续请求。"""
    if should_stop is not None and should_stop():
        return False
    await asyncio.sleep(max(0.0, delay))
    return should_stop is None or not should_stop()


def _timeout_seconds(value: float | None, env: str, default: float) -> float:
    """显式参数优先于环境变量；时限必须是有限正数，单位为秒。"""
    try:
        seconds = float(value if value is not None else os.getenv(env, "") or default)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{env} 必须是大于 0 的有限秒数") from e
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{env} 必须是大于 0 的有限秒数")
    return seconds


async def _await_with_timeout(work: Awaitable[_T], timeout: float) -> _T:
    """到期/停止只取消 work 一次；嵌套总时限或用户停止不能再次打断连接清理。"""
    task = asyncio.ensure_future(work)
    try:
        # shield 让 wait_for 只结束等待；实际取消与等待清理由本函数统一负责。
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    finally:
        if not task.done():
            task.cancel()
            cleanup = asyncio.gather(task, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError   # 清理完成后再继续向上传递停止


_POLL_INTERVAL = 0.5
_STREAM_END = object()
_EventSink = Callable[[Event], Awaitable[None]]


class _StreamWorker:
    """每次 stream 独占一个 I/O 线程，把异步 HTTP 事件交给同步 Agent。

    队列容量为 1；消费者从 yield 返回后才确认事件，生产者才继续读。
    因而保持原来的逐次推动语义，不预读后续事件或抢跑下一次请求。
    停止监视独立于 SSE 读取，即使服务端不发数据也能取消；close() 必须 join。
    """

    def __init__(self, source: Callable[[_EventSink], Awaitable[None]],
                 should_stop: Callable[[], bool] | None):
        self._source = source
        self._should_stop = should_stop
        self.events: queue.Queue = queue.Queue(maxsize=1)
        self.done = threading.Event()
        self.error: BaseException | None = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._request: asyncio.Task | None = None
        self._ack: asyncio.Event | None = None
        self._cancel_sent = False
        self.thread = threading.Thread(target=self._run, name=f"mecode-http-{id(self):x}",
                                       daemon=True)

    def _cancel_request(self) -> None:
        # 只在所属事件循环执行。重复 cancel 会再次打断清理，因此只发一次。
        if self._request is not None and not self._request.done() and not self._cancel_sent:
            self._cancel_sent = True
            self._request.cancel()

    def cancel(self) -> None:
        self._cancelled.set()   # 线程尚未注册事件循环时也不会丢掉停止信号
        with self._lock:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._cancel_request)

    def advance(self) -> None:
        with self._lock:
            if self._loop is not None and self._ack is not None:
                self._loop.call_soon_threadsafe(self._ack.set)

    async def _pump(self) -> None:
        await self._source(self._emit)

    async def _emit(self, event: Event) -> None:
        # 确认等待也在请求计时范围内。一次尝试超时后，旧事件可能尚未被消费；
        # 先等它确认，才能放入 Retrying，避免容量为 1 的队列溢出或预读抢跑。
        await self._ack.wait()
        self._ack.clear()
        self.events.put_nowait(event)
        await self._ack.wait()

    async def _watch_stop(self) -> None:
        while not self._cancelled.is_set():
            if self._should_stop is not None and self._should_stop():
                self._cancelled.set()
                return
            await asyncio.sleep(_POLL_INTERVAL)

    async def _run_async(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._ack = asyncio.Event()
            self._ack.set()
            self._request = asyncio.create_task(self._pump(), name="mecode-http-request")
        watcher = asyncio.create_task(self._watch_stop(), name="mecode-http-stop")
        try:
            if self._cancelled.is_set():
                self._cancel_request()
            done, _ = await asyncio.wait({self._request, watcher},
                                         return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher           # 回调出错也要向调用者传递，并停止请求
                self._cancel_request()
            await self._request         # 等待 response/client 的退出清理
        except asyncio.CancelledError:
            if not self._cancelled.is_set():
                raise                   # 不吞掉来源不明的取消
        finally:
            self._cancel_request()
            watcher.cancel()
            await asyncio.gather(self._request, watcher, return_exceptions=True)
            # asyncio.run 关闭 loop 前摘掉引用，避免别的线程向已关闭的 loop 发取消。
            with self._lock:
                self._loop = self._request = self._ack = None

    def _run(self) -> None:
        try:
            asyncio.run(self._run_async())
        except BaseException as exc:
            self.error = exc
        finally:
            self.done.set()
            try:
                self.events.put_nowait(_STREAM_END)
            except queue.Full:
                pass                    # 消费者仍可由 done 感知结束，不阻塞清理

    def close(self) -> None:
        self.cancel()
        if self.thread.ident is not None:
            self.thread.join()          # 不把仍在读网络的线程遗留到下一轮


class Provider:
    def __init__(self, backend: Backend, *, max_retries: int = 3,
                 backoff_base: float = 1.0, backoff_cap: float = 30.0,
                 request_timeout: float | None = None, total_timeout: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.backend = backend
        p = profile_for(backend.model)
        if context_window_for(backend.model) == 0:
            # 未知模型：查用户是否在 config 里自定义了思维链保留方式。
            # "none" 是【显式关掉】的哨兵——不能用空串表示：空串同时也是"从没设过"（老配置里到处是），
            # 而 INERT 默认已改为保留，用空串会让用户点的"不保留"落回默认 = 开关点了没反应。
            from .config import load_user_config
            kr = load_user_config().get("keep_reasoning", "")
            if kr == "none":
                p = ThinkingProfile(supports_thinking=False, keep_reasoning="")
            elif kr:
                p = ThinkingProfile(supports_thinking=False, keep_reasoning=kr)
        self.profile = p   # 该模型的思考【能力】档案（拼包/回传 reasoning 都看它）
        # 思考运行态（TUI 弹窗切、随会话持久化）：thinking_on=开关，effort=深度档；默认开 + 档案默认档。
        self.thinking_on = True
        self.effort = self.profile.default_effort
        # 单次响应封顶：档案给默认，MECODE_MAX_OUTPUT_TOKENS 可覆盖（0=不发）。
        # 未命中档案的后端（本地 vLLM / 小上下文部署）默认为 0——发一个超过它自身上限的
        # max_tokens 就是 400，不发才是安全默认。要给这类后端加保护就显式设环境变量。
        self.max_output = int(os.getenv("MECODE_MAX_OUTPUT_TOKENS", "") or self.profile.max_output)
        self._url = f"{backend.base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {backend.api_key}"}
        self.max_retries = max_retries        # 瞬时错误最多重试几次
        self.backoff_base = backoff_base       # 退避基数（秒）：1, 2, 4, 8…
        self.backoff_cap = backoff_cap         # 退避上限（秒）
        self.request_timeout = _timeout_seconds(request_timeout, "MECODE_REQUEST_TIMEOUT_SECONDS", 600)
        self.total_timeout = _timeout_seconds(total_timeout, "MECODE_TOTAL_TIMEOUT_SECONDS", 1200)
        self._transport = transport            # 仅供测试注入 httpx.MockTransport

    def _thinking_fields(self) -> dict:
        """按档案能力 + 当前运行态（thinking_on/effort），拼出要加进 payload 的 thinking / reasoning_effort。
        未命中档案（INERT）返回空 → 老后端零回归。"""
        p = self.profile
        if not p.supports_thinking:
            return {}
        on = self.thinking_on or not p.toggleable    # 不可关的模型（Kimi k2.7 / MiniMax M2.7）恒开
        th: dict = {"type": p.on_value if on else "disabled"}   # "开"值各家不一（多数 enabled，MiniMax adaptive）
        if on and p.keep:                             # keep 只在思考开时发：type=disabled 收不到思考，keep 无意义
            th["keep"] = p.keep
        out: dict = {"thinking": th}
        if on and p.effort_tiers:                     # 深度只在支持且思考开时发
            out["reasoning_effort"] = self.effort or p.default_effort
        if on and p.reasoning_split:                  # MiniMax：思考出独立字段而非 content 里的 <think> 标签
            out["reasoning_split"] = True
        return out

    def _for_wire(self, messages: list[dict]) -> list[dict]:
        """发送前把消息整理成能上线的样子。两件事：

        ① 按【自家档案】过滤历史思考——"存全、发时过滤"的发送闸。
           存储层无条件存 reasoning_content（见 agent._turn_loop），这里决定每条 assistant 带不带：
           keep_reasoning："all"=每轮带 / "tool_calls"=只带工具调用的回合带 / ""=全不带。
           载体统一 reasoning_content（DeepSeek 首创的事实标准，vLLM/GLM/Kimi 通行；MiniMax 双发也收它——
           官方 Thinking Control 有背书 + 回传实测 200）。
        ② 剥掉【mecode 自己的字段】（_INTERNAL_KEYS）。transcript 是完整事实、可以带私货，
           但发出去的必须是干净的 OpenAI 消息——严格的后端见到不认识的键会 400。

        浅拷贝该改的消息，live messages / transcript 永不被改——切模型不丢 CoT、不丢用量。"""
        scope = self.profile.keep_reasoning
        out = []
        for m in messages:
            drop = set(_INTERNAL_KEYS & m.keys())
            if m.get("role") == "assistant" and "reasoning_content" in m:
                keep = scope == "all" or (scope == "tool_calls" and bool(m.get("tool_calls")))
                if not keep:
                    drop.add("reasoning_content")
            if drop:
                m = {k: v for k, v in m.items() if k not in drop}
            out.append(m)
        return out

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[Event]:
        """同步事件接口；异步 HTTP 在本次调用独占的线程中运行。

        停止/KeyboardInterrupt/生成器 close 均先取消并等待 I/O 清理，再返回 Agent。
        Provider 本身不存放活动请求句柄，因此共享 Provider 的并发调用互不误停。
        should_stop 会跨线程读取，应使用线程安全的回调（Agent 传入 threading.Event.is_set）。
        """
        if should_stop is not None and should_stop():
            return
        worker = _StreamWorker(
            lambda emit: self._stream_async(messages, tools, should_stop, emit), should_stop)
        try:
            worker.thread.start()        # 启动期间的 Ctrl+C 也必须经过 finally 取消
            while True:
                if should_stop is not None and should_stop():
                    return
                try:
                    event = worker.events.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    if not worker.done.is_set():
                        continue
                    event = _STREAM_END
                if should_stop is not None and should_stop():
                    return
                if event is _STREAM_END:
                    if worker.error is not None:
                        raise worker.error
                    return
                yield event
                worker.advance()
        finally:
            worker.close()

    async def _stream_async(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        should_stop: Callable[[], bool] | None,
        emit: _EventSink,
    ) -> None:
        """总时限包住全部尝试和退避；到期取消并等待连接清理，不再开启新尝试。"""
        deadline = asyncio.get_running_loop().time() + self.total_timeout
        try:
            await _await_with_timeout(
                self._stream_attempts(messages, tools, should_stop, emit, deadline),
                timeout=self.total_timeout)
        except asyncio.TimeoutError as e:
            if should_stop is not None and should_stop():
                return
            raise ProviderError(
                f"模型调用总等待时间已达 {self.total_timeout:g} 秒（含重试和退避）；"
                "已停止，请稍后重试或切换模型") from e

    async def _stream_attempts(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        should_stop: Callable[[], bool] | None,
        emit: _EventSink,
        deadline: float,
    ) -> None:
        """发一轮请求，把流式响应整理成 events。瞬时错误/缺少结束标志时退避重试；
        已交付 TextDelta/ToolCall 后不重试；只有思考时可丢弃本次思考重新请求。
        Retrying 通知上层清理临时思考；已报告的 Usage 不撤销。
        CancelledError 不进入 TransportError 重试分支，async with 负责清理连接。"""
        payload: dict = {
            "model": self.backend.model,
            "messages": self._for_wire(messages),
            "stream": True,
            "stream_options": {"include_usage": True},   # 流式末尾带 token 用量
        }
        if tools:
            payload["tools"] = tools
        payload.update(self._thinking_fields())      # 命中档案才加 thinking / reasoning_effort
        if self.max_output:                          # 命中档案才封顶单次响应（见 ThinkingProfile.max_output）
            payload["max_tokens"] = self.max_output

        for attempt in range(self.max_retries + 1):
            if should_stop is not None and should_stop():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            response_committed = False         # 是否已交付正文/完整工具调用
            retry_after = None

            async def request_once() -> tuple[str | None, str] | None:
                nonlocal response_committed
                if should_stop is not None and should_stop():
                    return
                async with httpx.AsyncClient(transport=self._transport) as client:
                    async with client.stream("POST", self._url, headers=self._headers,
                                             json=payload, timeout=120) as r:
                        if should_stop is not None and should_stop():
                            return
                        # 退出响应/连接的 with 后再等待，不能占着旧连接退避。
                        if _is_retriable_status(r.status_code) and attempt < self.max_retries:
                            return r.headers.get("retry-after"), f"后端暂不可用（HTTP {r.status_code}）"
                        else:
                            if r.status_code >= 400:
                                await r.aread()
                                raise _friendly_http_error(r, self.backend.model)
                            async with aclosing(self._consume(r, should_stop)) as events:
                                async for event in events:
                                    if isinstance(event, (TextDelta, ToolCall)):
                                        # 在交付前置位：上层接到内容后，不能再自动重放请求。
                                        response_committed = True
                                    await emit(event)
                            return

            try:
                # 每次重新计时；到期取消整个请求，心跳/思考不重置时钟。
                # 退出前等待 response/client 清理，不让另一道时限重复取消清理。
                retry = await _await_with_timeout(request_once(), timeout=self.request_timeout)
                if retry is None:
                    return
                retry_after, reason = retry
            except (httpx.TransportError, _IncompleteStreamError, asyncio.TimeoutError) as e:
                # 停止期间恰好超时/断网，仍按主动停止收尾；Agent 会保存正文并追加中断标记。
                if should_stop is not None and should_stop():
                    return
                if response_committed:
                    if isinstance(e, asyncio.TimeoutError):
                        raise ProviderError(
                            f"模型单次请求超过 {self.request_timeout:g} 秒；"
                            "已停止，已输出内容保留，请手动继续或重试") from e
                    raise                       # 半段正文由 Agent 保留，不自动重放
                if attempt >= self.max_retries:
                    if isinstance(e, _IncompleteStreamError):
                        raise ProviderError(
                            f"模型响应缺少结束标志，已达 {attempt + 1} 次尝试上限；"
                            "请稍后重试或切换模型") from e
                    if isinstance(e, asyncio.TimeoutError):
                        raise ProviderError(
                            f"模型单次请求超过 {self.request_timeout:g} 秒，"
                            f"已达 {attempt + 1} 次尝试上限；请稍后重试或切换模型") from e
                    if isinstance(e, httpx.TimeoutException):
                        raise ProviderError(
                            f"模型回复超时，{attempt + 1} 次尝试均未完成；请稍后重试或切换模型") from e
                    raise ProviderError(
                        f"连不上后端 {self.backend.base_url}（{type(e).__name__}）："
                        f"请检查网络、base_url 是否正确、服务是否在运行") from e
                if isinstance(e, _IncompleteStreamError):
                    reason = "响应缺少结束标志"
                elif isinstance(e, asyncio.TimeoutError):
                    reason = f"单次请求超过 {self.request_timeout:g} 秒"
                else:
                    reason = "请求超时" if isinstance(e, httpx.TimeoutException) else "网络连接中断"
            if should_stop is not None and should_stop():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            delay = _backoff(attempt, self.backoff_base, self.backoff_cap, retry_after)
            await emit(Retrying(attempt + 1, self.max_retries, delay, reason))
            if not await _wait_before_retry(delay, should_stop):
                return

    async def _consume(self, r: httpx.Response,
                       should_stop: Callable[[], bool] | None = None) -> AsyncIterator[Event]:
        """解析 SSE；正文实时交付，确认有效结束后才交付工具调用和 Done。"""
        # 按 index 累积工具调用碎片（arguments 跨多片到达），攒完整再 yield
        tool_acc: dict[int, dict] = {}
        finish_reason: str | None = None
        saw_done = False
        async for line in r.aiter_lines():
            if should_stop is not None and should_stop():
                return                  # 协作式打断：上层置位 → 停止消费（不再 yield 后续/Done）
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                # 首选结束信号：无需继续等 HTTP EOF；仍要在循环后检查结束原因。
                saw_done = True
                break

            chunk = json.loads(data)
            # HTTP 200 的流也可能返回错误；先识别，避免因没有 choices 而静默跳过。
            error = chunk.get("error")
            if error is not None:
                if isinstance(error, dict):
                    detail = error.get("message") or json.dumps(error, ensure_ascii=False)
                else:
                    detail = str(error)
                raise ProviderError(f"模型服务返回错误：{detail}")
            # 只认顶层 usage。Kimi 的 finish 帧还会在 choice 内重复带用量，不能重复计数。
            # 顶层用量可能与 finish 同帧，也可能在后续 choices=[] 的独立帧里。
            usage = chunk.get("usage")
            if usage and usage.get("prompt_tokens"):
                yield Usage(
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    # 思考 token 是 completion 的子集（各家都放在这个嵌套字段里）；
                    # 没有这个字段的后端拿到 0，上层据此不显示这一项
                    reasoning_tokens=(usage.get("completion_tokens_details")
                                      or {}).get("reasoning_tokens", 0) or 0,
                )
            choices = chunk.get("choices") or []   # 有的 provider 发空 choices 块，跳过
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            # 思考通道统一读 reasoning_content（DeepSeek 首创的事实标准，GLM/Kimi/MiniMax 都发；
            # vLLM 新版改名 reasoning，兜着）。MiniMax reasoning_split 时【同一份思考】双发
            # reasoning_content + reasoning_details（官方 Thinking Control 有一句背书，实测字节相同）——
            # 只认前者、忽略后者，既不重复显示、存储/回传也统一为纯文本单载体。
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield ReasoningDelta(reasoning)
            if delta.get("content"):
                yield TextDelta(delta["content"])

            for tc in delta.get("tool_calls") or []:
                slot = tool_acc.setdefault(tc["index"], {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]    # 累加，不是覆盖

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        if should_stop is not None and should_stop():
            return                          # 停止与 EOF 同时发生时，仍按主动停止收尾
        # 首选 [DONE]；没有它时，EOF 必须有已经记录的 finish_reason 才能兜底。
        # MiniMax 没有 [DONE]；它和 Kimi 都在 finish 后继续发 usage，不能提前 break。
        if not saw_done and finish_reason is None:
            raise _IncompleteStreamError("模型响应提前结束，未收到结束标志；请重试或切换模型")
        # 结束标志证明生成停止了，但达到输出上限/内容过滤不等于回答与工具参数完整。
        # 显式失败原因优先于 [DONE]；整批工具仍留在本地，不交给 Agent 执行。
        if finish_reason == "length":
            raise ProviderError("模型输出达到长度上限，响应可能不完整；请提高输出上限或缩小任务后重试")
        if finish_reason == "content_filter":
            raise ProviderError("模型输出被内容过滤中止，响应未完整完成")
        if finish_reason not in (None, "stop", "tool_calls", "function_call"):
            raise ProviderError(f"模型以未支持的结束原因终止：{finish_reason}")

        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                # 拼出来不是合法 JSON —— 多半是流式分片在服务层被截断
                args = {"__raw__": slot["args"], "__error__": "invalid JSON"}
            yield ToolCall(id=slot["id"], name=slot["name"], arguments=args)

        # 仅有 [DONE] 的兼容服务没有提供原因，按实际有没有工具调用推导。
        yield Done(reason=finish_reason or ("tool_calls" if tool_acc else "stop"))
