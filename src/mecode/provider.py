"""Provider 层：和模型对话的唯一出入口。

把"脏"的 OpenAI 兼容 SSE 流，整理成干净的 events（见 events.py）。
这是 raw_probe.py 里那段拼包逻辑的"产品化"——但不再 print，而是 yield 事件。

设计：
- reasoning / content 碎片：来一片 yield 一片（流式 UX，逐字显示）
- tool_calls：跨多片，按 index 累加 arguments，【攒完整】再 yield（上层要完整的才能执行）
- 最后 yield Done(finish_reason)，让主循环判断"该执行工具还是该结束"
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Iterator

import httpx

from .config import Backend
from .events import Done, Event, ReasoningDelta, TextDelta, ToolCall, Usage
from .registry import ThinkingProfile, context_window_for, profile_for

# 可重试的瞬时 HTTP 状态：限流 + 网关/服务端临时错误
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ProviderError(Exception):
    """后端请求失败的【人话】版本（4xx/重试用尽的 5xx/连不上）。
    str(e) 即给用户看的完整信息——上层（TUI/CLI）不用再翻 httpx 细节。"""


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


class Provider:
    def __init__(self, backend: Backend, *, max_retries: int = 3,
                 backoff_base: float = 1.0, backoff_cap: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
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

    def _filter_reasoning(self, messages: list[dict]) -> list[dict]:
        """发送前按【自家档案】过滤历史思考——"存全、发时过滤"的发送闸。
        存储层无条件存 reasoning_content（见 agent._turn_loop），这里决定每条 assistant 带不带：
        keep_reasoning："all"=每轮带 / "tool_calls"=只带工具调用的回合带 / ""=全不带。
        载体统一 reasoning_content（DeepSeek 首创的事实标准，vLLM/GLM/Kimi 通行；MiniMax 双发也收它——
        官方 Thinking Control 有背书 + 回传实测 200）。
        浅拷贝该改的消息，live messages / transcript 永不被改——切模型不丢 CoT。"""
        scope = self.profile.keep_reasoning
        out = []
        for m in messages:
            if m.get("role") != "assistant" or "reasoning_content" not in m:
                out.append(m)
                continue
            keep = scope == "all" or (scope == "tool_calls" and bool(m.get("tool_calls")))
            if not keep:
                m = dict(m)
                m.pop("reasoning_content")
            out.append(m)
        return out

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[Event]:
        """发一轮请求，把流式响应整理成 events。瞬时错误（连接失败/读超时/429/5xx）退避重试；
        但【一旦开始消费流就不再重试】——否则会把已产出的 token 重复一遍。
        should_stop: 协作式打断回调，返回 True 就停止消费（上层置位、流式中途停下）。"""
        payload: dict = {
            "model": self.backend.model,
            "messages": self._filter_reasoning(messages),
            "stream": True,
            "stream_options": {"include_usage": True},   # 流式末尾带 token 用量
        }
        if tools:
            payload["tools"] = tools
        payload.update(self._thinking_fields())      # 命中档案才加 thinking / reasoning_effort
        if self.max_output:                          # 命中档案才封顶单次响应（见 ThinkingProfile.max_output）
            payload["max_tokens"] = self.max_output

        for attempt in range(self.max_retries + 1):
            streaming = False                  # 是否已开始消费流（标记后不再重试）
            try:
                with httpx.Client(transport=self._transport) as client:
                    with client.stream("POST", self._url, headers=self._headers,
                                       json=payload, timeout=120) as r:
                        # 可重试状态（429/5xx）：读掉响应体、退避、再来
                        if _is_retriable_status(r.status_code) and attempt < self.max_retries:
                            r.read()
                            time.sleep(_backoff(attempt, self.backoff_base, self.backoff_cap,
                                                r.headers.get("retry-after")))
                            continue
                        if r.status_code >= 400:    # 4xx / 重试用尽的 5xx → 人话报错（含服务端消息摘要）
                            r.read()
                            raise _friendly_http_error(r, self.backend.model)
                        streaming = True
                        yield from self._consume(r, should_stop)
                return
            except httpx.TransportError as e:   # 连接失败/读超时等瞬时网络错误
                if streaming or attempt >= self.max_retries:
                    if streaming:
                        raise                    # 流中途断（已产出部分 token）→ 保留原始异常语义
                    raise ProviderError(         # 重试用尽 → 人话（最常见：base_url 错/网络不通/服务没起）
                        f"连不上后端 {self.backend.base_url}（{type(e).__name__}）："
                        f"请检查网络、base_url 是否正确、服务是否在运行") from e
                time.sleep(_backoff(attempt, self.backoff_base, self.backoff_cap, None))

    def _consume(self, r: httpx.Response,
                 should_stop: Callable[[], bool] | None = None) -> Iterator[Event]:
        """连接已建立后，解析 SSE 流为事件（含工具调用按 index 拼接 + 末尾 Done）。"""
        # 按 index 累积工具调用碎片（arguments 跨多片到达），攒完整再 yield
        tool_acc: dict[int, dict] = {}
        finish_reason = "stop"
        for line in r.iter_lines():
            if should_stop is not None and should_stop():
                return                  # 协作式打断：上层置位 → 停止消费（不再 yield 后续/Done）
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break

            chunk = json.loads(data)
            # token 用量常在末尾一个 choices 为空的块里，只在 prompt_tokens 有真实值时产出
            usage = chunk.get("usage")
            if usage and usage.get("prompt_tokens"):
                yield Usage(
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
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

        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                # 拼出来不是合法 JSON —— 多半是流式分片在服务层被截断
                args = {"__raw__": slot["args"], "__error__": "invalid JSON"}
            yield ToolCall(id=slot["id"], name=slot["name"], arguments=args)

        yield Done(reason=finish_reason)