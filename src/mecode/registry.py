"""模型档案表（per-model registry）：给不同厂商/模型"下药"的唯一真相。

两处消费：
- provider/agent：拿当前 model 查 `profile_for(model)` → 决定发不发 thinking 参数、
  要不要多轮回传 reasoning_content（门控就是这张表，命中才发、未命中 inert=老后端零回归）。
- 首次配置 UI（后续做）：`PROVIDERS` 就是可选模型目录——一键配置时选 provider→选 model，
  base_url 由我们填、只让用户去开放平台复制 api_key 粘进来。

档案粒度是【模型】不是【提供商】：同一家不同模型思考能力可能不同（如 Kimi k2.7-code 强制思考、
k2.6 可关）。当前 Kimi 三个模型对我们发的参数一致（都 type=enabled + keep=all），故共用一份 profile。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThinkingProfile:
    """一个模型的思考【能力】档案。provider 按"能力 + 当前运行态(开关/深度)"拼 payload，
    TUI 按能力决定显不显思考控件——故这里存能力，不存死的参数值。

    - supports_thinking：发不发 `thinking.type`。
    - toggleable：用户能否关思考（False=强制开，不显开关 UI，如 Kimi k2.7）。
    - on_value：`thinking.type` 的"开"值（多数厂商 "enabled"；MiniMax 是 "adaptive"）。
    - keep：`thinking.keep` 的值（Kimi="all"；无=不带该字段）。
    - effort_tiers：`reasoning_effort` 可选档（空 tuple=不支持深度/不显深度 UI）。
    - default_effort：默认深度档（如 DeepSeek="max"）。
    - reasoning_split：True → 发 `reasoning_split=True`，思考出独立字段而非 content 里的 `<think>`
        标签（MiniMax；它把同一份思考双发 reasoning_content + reasoning_details，我们统一只认前者）。
    - keep_reasoning：多轮里历史 assistant 的思考【发送时】带不带（存储永远全存，
        见 provider._filter_reasoning）——
        ""           不带（未注册/老后端；也避免往不认识的服务端塞未知字段被拒）
        "all"        每一轮 assistant 都带（Kimi keep=all / MiniMax）
        "tool_calls" 只有【带工具调用】的 assistant 带，纯答案回合不带（DeepSeek/GLM）
    """
    supports_thinking: bool = False
    toggleable: bool = False
    on_value: str = "enabled"
    keep: str | None = None
    effort_tiers: tuple[str, ...] = ()
    default_effort: str = ""
    reasoning_split: bool = False
    keep_reasoning: str = ""


# Kimi k2.7-code / -highspeed：思考【强制开】(toggleable=False，不显开关)+ keep 恒 all + 每轮回传 reasoning。
KIMI_FORCED = ThinkingProfile(supports_thinking=True, toggleable=False, keep="all", keep_reasoning="all")

# Kimi k2.6：思考【可开关】(toggleable=True，弹窗显"思考 开/关")；keep 仍恒 all（只 type 可改、keep 不做成开关）。
KIMI_K26 = ThinkingProfile(supports_thinking=True, toggleable=True, keep="all", keep_reasoning="all")

# DeepSeek(v4-flash/pro)：思考可开关 + 深度 high/max（默认 max）+ 只保留工具调用回合的思考。
DEEPSEEK = ThinkingProfile(supports_thinking=True, toggleable=True,
                           effort_tiers=("high", "max"), default_effort="max",
                           keep_reasoning="tool_calls")

# GLM(智谱)：type 可开关(所有 GLM-5.x)；无 keep(那是 Kimi 专属)。
# 多轮：带工具调用的 assistant 必须回传 reasoning_content、纯答案回合不带 → keep_reasoning="tool_calls"(同 DeepSeek)。
# GLM-5.2：额外支持 reasoning_effort（官方 max/xhigh/high/../none，我们只留 high/max、默认 max；
#   none=放弃思考=思考关，由 type=disabled 覆盖，不单列）。
GLM_52 = ThinkingProfile(supports_thinking=True, toggleable=True,
                         effort_tiers=("high", "max"), default_effort="max",
                         keep_reasoning="tool_calls")
# GLM-5.2 以下(5.1/5/turbo)：type 可开关，但无深度档。
GLM_PLAIN = ThinkingProfile(supports_thinking=True, toggleable=True, keep_reasoning="tool_calls")

# MiniMax：思考"开"值是 adaptive（非 enabled）；发 reasoning_split=True 让思考出独立字段（它会双发
# reasoning_content + reasoning_details 两份相同内容，统一只认前者）；工具+纯文本回合都要回传 →
# keep_reasoning="all"；无深度档(reasoning_effort 被忽略)。M3 可开关；M2.7 / -highspeed 强制开（传 disabled 无效）。
MINIMAX_M3 = ThinkingProfile(supports_thinking=True, toggleable=True, on_value="adaptive",
                             reasoning_split=True, keep_reasoning="all")
MINIMAX_FORCED = ThinkingProfile(supports_thinking=True, toggleable=False, on_value="adaptive",
                                 reasoning_split=True, keep_reasoning="all")

# 默认档：不发 thinking、不回传 reasoning_content → 未注册/自填未命中的模型（本地 vLLM、Qwen 等）零回归。
INERT = ThinkingProfile()


@dataclass(frozen=True)
class ModelSpec:
    """一个可选模型。id = 厂商 API 的 model 字符串，同时作为展示名（不翻译，便于用户去平台对照）。
    context_window = 该模型上下文窗口（token）；0=未知。用于按模型自动定压缩阈值（见 config._context_limit），
    也在首次配置界面展示。这些云端模型都远超 100k，填了压缩几乎不触发；本地小模型才会早压。"""
    id: str
    profile: ThinkingProfile = INERT
    context_window: int = 0


@dataclass(frozen=True)
class ProviderSpec:
    """一个提供商分组：base_url 由我们填；key_help/key_url 指引用户去开放平台复制 api_key。"""
    name: str
    base_url: str
    key_help: str
    key_url: str
    models: tuple[ModelSpec, ...]


KIMI_PROVIDER = ProviderSpec(
    name="kimi",
    base_url="https://api.moonshot.cn/v1",
    key_help="在 Kimi 开放平台控制台创建 API Key，复制后粘贴到这里",
    key_url="https://platform.kimi.com/console/api-keys",
    models=(
        ModelSpec("kimi-k2.7-code", KIMI_FORCED, context_window=256_000),
        ModelSpec("kimi-k2.7-code-highspeed", KIMI_FORCED, context_window=256_000),
        ModelSpec("kimi-k2.6", KIMI_K26, context_window=256_000),   # 思考可开关，其余同 k2.7
    ),
)

DEEPSEEK_PROVIDER = ProviderSpec(
    name="deepseek",
    base_url="https://api.deepseek.com",
    key_help="在 DeepSeek 开放平台控制台创建 API Key，复制后粘贴到这里",
    key_url="https://platform.deepseek.com/api_keys",
    models=(
        ModelSpec("deepseek-v4-flash", DEEPSEEK, context_window=1_000_000),
        ModelSpec("deepseek-v4-pro", DEEPSEEK, context_window=1_000_000),
    ),
)

GLM_PROVIDER = ProviderSpec(
    name="glm",
    base_url="https://open.bigmodel.cn/api/paas/v4",
    key_help="在智谱开放平台创建 API Key，复制后粘贴到这里",
    key_url="https://bigmodel.cn/apikey/platform",
    models=(
        ModelSpec("glm-5.2", GLM_52, context_window=1_000_000),   # 唯一带深度档(high/max)的; 1M
        ModelSpec("glm-5.1", GLM_PLAIN, context_window=200_000),
        ModelSpec("glm-5", GLM_PLAIN, context_window=200_000),
        ModelSpec("glm-5-turbo", GLM_PLAIN, context_window=200_000),
        # 不含 glm-5v-turbo：视觉模型，mecode 不支持发图片消息
    ),
)

MINIMAX_PROVIDER = ProviderSpec(
    name="minimax",
    base_url="https://api.minimaxi.com/v1",
    key_help="在 MiniMax 开放平台创建 API Key，复制后粘贴到这里",
    key_url="https://platform.minimaxi.com/",
    models=(
        ModelSpec("MiniMax-M3", MINIMAX_M3, context_window=1_000_000),   # 可开关；1M
        ModelSpec("MiniMax-M2.7", MINIMAX_FORCED, context_window=204_800),
        ModelSpec("MiniMax-M2.7-highspeed", MINIMAX_FORCED, context_window=204_800),
    ),
)

# 已 seed：Kimi、DeepSeek、GLM、MiniMax。
PROVIDERS: tuple[ProviderSpec, ...] = (
    KIMI_PROVIDER, DEEPSEEK_PROVIDER, GLM_PROVIDER, MINIMAX_PROVIDER,
)

# model_id → profile / context_window（查表用）
_BY_MODEL: dict[str, ThinkingProfile] = {m.id: m.profile for p in PROVIDERS for m in p.models}
_WINDOW_BY_MODEL: dict[str, int] = {m.id: m.context_window for p in PROVIDERS for m in p.models}


def profile_for(model: str) -> ThinkingProfile:
    """查某 model 的思考档案；未注册返回 INERT（不发 thinking、不回传 reasoning）。"""
    return _BY_MODEL.get(model, INERT)


def context_window_for(model: str) -> int:
    """查某 model 的上下文窗口（token）；未注册或未填返回 0（上层回落全局默认）。"""
    return _WINDOW_BY_MODEL.get(model, 0)
