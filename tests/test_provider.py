"""Provider 的重试退避：瞬时错误（429/5xx/连接错）退避重试，4xx 直接抛，流中途不重试。

用 httpx.MockTransport 注入"前 N 次失败、之后成功"的假后端，不联网。
"""
import json

import httpx
import pytest

from mecode.config import Backend
from mecode.events import Done, ReasoningDelta, TextDelta
from mecode.provider import Provider, ProviderError, _backoff, _is_retriable_status
from mecode.registry import MAX_OUTPUT_DEFAULT

# 一段最小可用的 SSE：一片正文 + 末尾 usage + [DONE]
_SSE_OK = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n'
           b'data: [DONE]\n\n')


def _provider(handler):
    backend = Backend(base_url="http://test/v1", model="m", api_key="k")
    # backoff_base=0 → 退避不真睡，测试瞬间跑完
    return Provider(backend, max_retries=3, backoff_base=0,
                    transport=httpx.MockTransport(handler))


# ---- 纯函数 ----

def test_is_retriable_status():
    assert _is_retriable_status(429) and _is_retriable_status(503)
    assert not _is_retriable_status(400) and not _is_retriable_status(200)


def test_backoff_指数封顶_retry_after优先():
    assert _backoff(0, 1, 30) == 1
    assert _backoff(3, 1, 30) == 8
    assert _backoff(10, 1, 30) == 30          # 封顶
    assert _backoff(0, 1, 30, "5") == 5       # Retry-After（秒）优先


# ---- 重试行为 ----

def test_503两次后成功():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        return httpx.Response(503) if state["n"] <= 2 else httpx.Response(200, content=_SSE_OK)
    evs = list(_provider(handler).stream([{"role": "user", "content": "hi"}]))
    assert any(isinstance(e, TextDelta) and e.text == "hi" for e in evs)
    assert any(isinstance(e, Done) for e in evs)
    assert state["n"] == 3                     # 2 次 503 + 1 次成功


def test_连接错误后成功():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=_SSE_OK)
    evs = list(_provider(handler).stream([{"role": "user", "content": "hi"}]))
    assert any(isinstance(e, TextDelta) for e in evs)
    assert state["n"] == 2


def test_4xx不重试_人话报错含服务端消息():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid API key provided"}})
    with pytest.raises(ProviderError) as ei:
        list(_provider(handler).stream([{"role": "user", "content": "hi"}]))
    assert state["n"] == 1                      # 没重试
    msg = str(ei.value)
    assert "API Key 无效" in msg and "401" in msg and "Invalid API key" in msg   # 人话 + 状态码 + 服务端摘要


def test_重试用尽后抛人话():
    state = {"n": 0}
    def handler(req):
        state["n"] += 1
        return httpx.Response(503)              # 一直 503
    with pytest.raises(ProviderError) as ei:
        list(_provider(handler).stream([{"role": "user", "content": "hi"}]))
    assert state["n"] == 4                       # max_retries=3 → 共 4 次尝试
    assert "503" in str(ei.value)


def test_连不上_重试用尽转人话():
    def handler(req):
        raise httpx.ConnectError("boom")         # 一直连不上
    with pytest.raises(ProviderError) as ei:
        list(_provider(handler).stream([{"role": "user", "content": "hi"}]))
    assert "连不上后端" in str(ei.value) and "base_url" in str(ei.value)


# ---- 按 model 档案拼 thinking 参数 ----

def _payload_for(model, *, thinking_on=None, effort=None):
    """跑一轮，捕获发出去的 payload dict（handler 拿到请求体）；可选设思考运行态。"""
    seen = {}
    def handler(req):
        seen["payload"] = json.loads(req.content)
        return httpx.Response(200, content=_SSE_OK)
    backend = Backend(base_url="http://test/v1", model=model, api_key="k")
    p = Provider(backend, transport=httpx.MockTransport(handler))
    if thinking_on is not None:
        p.thinking_on = thinking_on
    if effort is not None:
        p.effort = effort
    list(p.stream([{"role": "user", "content": "hi"}]))
    return seen["payload"]


def test_kimi模型_payload带thinking_enabled_all():
    payload = _payload_for("kimi-k2.7-code-highspeed")
    assert payload["thinking"] == {"type": "enabled", "keep": "all"}


def test_kimi_k27强制思考_关不掉_恒enabled():
    # k2.7 toggleable=False → 即便 thinking_on=False 也恒发 enabled + keep
    payload = _payload_for("kimi-k2.7-code", thinking_on=False)
    assert payload["thinking"] == {"type": "enabled", "keep": "all"}


def test_kimi_k26_可关思考_disabled且不发keep():
    # k2.6 toggleable=True → 关得掉；type=disabled 时 keep 不发（收不到思考，keep 无意义）
    payload = _payload_for("kimi-k2.6", thinking_on=False)
    assert payload["thinking"] == {"type": "disabled"}
    assert "keep" not in payload["thinking"]


def test_kimi_k26_开思考_enabled带keep():
    payload = _payload_for("kimi-k2.6", thinking_on=True)
    assert payload["thinking"] == {"type": "enabled", "keep": "all"}


def test_deepseek_默认_思考开_深度max_无keep():
    payload = _payload_for("deepseek-v4-flash")
    assert payload["thinking"] == {"type": "enabled"}      # 无 keep 字段
    assert payload["reasoning_effort"] == "max"            # 默认档


def test_deepseek_关思考_disabled_且不发深度():
    payload = _payload_for("deepseek-v4-pro", thinking_on=False)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload              # 思考关 → 不发深度


def test_deepseek_深度可切high():
    payload = _payload_for("deepseek-v4-flash", effort="high")
    assert payload["reasoning_effort"] == "high"


def test_glm52_思考开_深度max():
    payload = _payload_for("glm-5.2")
    assert payload["thinking"] == {"type": "enabled"}     # 无 keep
    assert payload["reasoning_effort"] == "max"           # 5.2 有深度、默认 max


def test_glm5_无深度档_不发effort():
    payload = _payload_for("glm-5")
    assert payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in payload              # 5.2 以下无深度


def test_minimax_M3_思考开_adaptive且发split():
    payload = _payload_for("MiniMax-M3")
    assert payload["thinking"] == {"type": "adaptive"}    # 开值是 adaptive、非 enabled
    assert payload["reasoning_split"] is True             # 思考出独立字段（非 <think> 标签）
    assert "reasoning_effort" not in payload              # 无深度


def test_minimax_M3_关思考_disabled_不发split():
    payload = _payload_for("MiniMax-M3", thinking_on=False)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_split" not in payload


def test_minimax_M2_7强制开_关不掉():
    payload = _payload_for("MiniMax-M2.7", thinking_on=False)
    assert payload["thinking"] == {"type": "adaptive"} and payload["reasoning_split"] is True


# reasoning_split 时 MiniMax【同一份思考】双发 reasoning_content + reasoning_details（实测字节相同、
# 官方 Thinking Control 有背书）→ 统一只认 reasoning_content、忽略 details，不显两遍、单载体存储
_SSE_MM_BOTH = (
    b'data: {"choices":[{"delta":{"reasoning_content":"ab","reasoning_details":'
    b'[{"type":"reasoning.text","index":0,"text":"ab"}]}}]}\n\n'
    b'data: {"choices":[{"delta":{"reasoning_content":"cd","reasoning_details":[{"index":0,"text":"cd"}]}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"ans"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    b'data: [DONE]\n\n')


def test_minimax_双发两字段_只认content_不重复显示():
    p = Provider(Backend(base_url="http://t/v1", model="MiniMax-M2.7", api_key="k"),
                 transport=httpx.MockTransport(lambda req: httpx.Response(200, content=_SSE_MM_BOTH)))
    evs = list(p.stream([{"role": "user", "content": "hi"}]))
    # 只从 reasoning_content 出一份，不是 "ababcdcd"
    assert "".join(e.text for e in evs if isinstance(e, ReasoningDelta)) == "abcd"
    assert any(isinstance(e, TextDelta) and e.text == "ans" for e in evs)   # content 是纯答案


# ---- 发送闸 _for_wire（"存全、发时过滤"）----

def _mk(model):
    return Provider(Backend(base_url="http://t/v1", model=model, api_key="k"))


_HISTORY = [
    {"role": "user", "content": "问"},
    {"role": "assistant", "content": "答", "reasoning_content": "想1"},                              # 纯答案回合
    {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}], "reasoning_content": "想2"},   # 工具回合
]


def test_filter_all档案_每轮都带(monkeypatch):
    out = _mk("kimi-k2.7-code")._for_wire(_HISTORY)
    assert out[1]["reasoning_content"] == "想1" and out[2]["reasoning_content"] == "想2"


def test_filter_tool_calls档案_纯答案回合剥掉():
    out = _mk("deepseek-v4-flash")._for_wire(_HISTORY)
    assert "reasoning_content" not in out[1]              # 纯答案回合不带（带了 400）
    assert out[2]["reasoning_content"] == "想2"           # 工具回合带


def test_filter_未知模型_默认按工具回合带_且不改原消息(tmp_path, monkeypatch):
    """未知模型默认 keep_reasoning="tool_calls"：回传的是后端【自己发过来的】reasoning_content，
    不存在发错字段的风险（它没发，历史里就没有）。与"不发 thinking 参数"是两件事。"""
    import mecode.config as cfg
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")   # 隔离：会查真实 config
    out = _mk("未注册模型")._for_wire(_HISTORY)
    assert "reasoning_content" not in out[1]             # 纯答案回合仍剥掉
    assert out[2]["reasoning_content"] == "想2"          # 工具回合带回去
    assert _HISTORY[1]["reasoning_content"] == "想1"      # 原消息无损（浅拷贝，历史不被改写）


def test_filter_未知模型_显式关掉走none哨兵(tmp_path, monkeypatch):
    """显式"不保留"存的是 "none" 而不是 ""——空串同时也是"从没设过"（老配置里到处是），
    而默认已改为保留，用空串会让用户点的"不保留"落回默认 = 开关点了没反应。"""
    import json

    import mecode.config as cfg
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", p)
    p.write_text(json.dumps({"keep_reasoning": "none"}), encoding="utf-8")
    pv = _mk("未注册模型")
    assert all("reasoning_content" not in m for m in pv._for_wire(_HISTORY)
               if m["role"] == "assistant")
    # 哨兵要【归一化成 ""】而不是原样留着：留着的话行为虽然碰巧一样（未知 scope 也是全剥），
    # 但别处读 profile.keep_reasoning 会显示成 "none"（如 /config 的"思维链保留：{x or '不保留'}"）
    assert pv.profile.keep_reasoning == ""
    p.write_text(json.dumps({"keep_reasoning": ""}), encoding="utf-8")       # 老配置的空串 = 没设过
    assert _mk("未注册模型")._for_wire(_HISTORY)[2]["reasoning_content"] == "想2"


# ---- 内部字段：只存不发 ----

_WITH_USAGE = [
    {"role": "user", "content": "问"},
    # 只带 usage、不带 reasoning_content：光按"有没有思考"分支的话会整条原样放过去
    {"role": "assistant", "content": "答", "usage": {"completion": 868, "reasoning": 842}},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}],
     "reasoning_content": "想", "usage": {"completion": 20, "reasoning": 0}},
]


def test_内部字段usage发送前剥掉(tmp_path, monkeypatch):
    """usage 是 mecode 自己记的（供 UI 从 transcript 累计），不是 OpenAI 的字段。
    留在消息里会随每个请求发出去，严格的后端见到不认识的键会 400。"""
    import mecode.config as cfg
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    out = _mk("未注册模型")._for_wire(_WITH_USAGE)
    assert all("usage" not in m for m in out), "usage 必须在发送前剥掉"
    assert out[2]["reasoning_content"] == "想"            # 剥 usage 不能顺手把思考也剥了
    assert out[1]["content"] == "答"                      # 别的字段一个不少
    # 原消息无损：transcript 和 live messages 里那份必须还带着 usage，不然累计就没了
    assert _WITH_USAGE[1]["usage"]["completion"] == 868


def test_内部字段与思考过滤互不干扰(tmp_path, monkeypatch):
    """keep_reasoning="all" 时思考全留，但 usage 照样要剥——两条闸各管各的。"""
    out = _mk("kimi-k2.7-code")._for_wire(_WITH_USAGE)
    assert all("usage" not in m for m in out)
    assert out[2]["reasoning_content"] == "想"




def test_未知模型_payload不带thinking():
    payload = _payload_for("Qwen3.6")
    assert "thinking" not in payload            # 老后端零回归
    assert "reasoning_effort" not in payload
    assert "reasoning_split" not in payload


def test_should_stop_提前停止():
    # should_stop 第 2 次返回 True → 在消费中途停下，不再 yield 后续、也没到 [DONE]
    calls = {"n": 0}
    def stop():
        calls["n"] += 1
        return calls["n"] >= 2
    p = _provider(lambda req: httpx.Response(200, content=_SSE_OK))
    evs = list(p.stream([{"role": "user", "content": "hi"}], should_stop=stop))
    assert any(isinstance(e, TextDelta) for e in evs)   # 收到了开头的正文
    assert not any(isinstance(e, Done) for e in evs)    # 提前停 → 没有 Done


# ---- 单次响应封顶 max_tokens（防生成退化，见 registry.ThinkingProfile.max_output）----

def test_命中档案的模型_payload带max_tokens():
    """跑批实测：7233 次响应中位数 286 token，却有 2 次顶满 131072（模型试图回忆一串编号，
    卡进等差数列一路数下去），一轮吃掉大半个上下文。封顶就是防这个。"""
    for model in ("deepseek-v4-flash", "kimi-k2.7-code", "glm-5.2", "MiniMax-M3"):
        assert _payload_for(model)["max_tokens"] == MAX_OUTPUT_DEFAULT, model


def test_未命中档案的模型_不发max_tokens():
    """本地 vLLM / 小上下文部署（如 8192）收到一个超过自身上限的 max_tokens 就是 400。
    和 thinking 参数同一个道理：不发才是安全默认。"""
    assert "max_tokens" not in _payload_for("some-local-qwen")


def test_环境变量可覆盖(monkeypatch):
    monkeypatch.setenv("MECODE_MAX_OUTPUT_TOKENS", "4096")
    assert _payload_for("deepseek-v4-flash")["max_tokens"] == 4096
    assert _payload_for("some-local-qwen")["max_tokens"] == 4096      # 给未命中的后端补保护


def test_环境变量设0_关掉封顶(monkeypatch):
    monkeypatch.setenv("MECODE_MAX_OUTPUT_TOKENS", "0")
    assert "max_tokens" not in _payload_for("deepseek-v4-flash")
