"""模型档案表 registry：门控查表 + Kimi/DeepSeek/GLM/MiniMax 命中各自 profile、未知模型 inert。"""
from mecode.registry import (
    DEEPSEEK, GLM_52, GLM_PLAIN, INERT, KIMI_FORCED, KIMI_K26, MINIMAX_FORCED, MINIMAX_M3,
    PROVIDERS, profile_for,
)


def test_kimi_k27强制_k26可开关():
    for mid in ("kimi-k2.7-code", "kimi-k2.7-code-highspeed"):
        assert profile_for(mid) is KIMI_FORCED     # k2.7：思考强制开
    assert profile_for("kimi-k2.6") is KIMI_K26    # k2.6：思考可开关


def test_deepseek两模型都命中DEEPSEEK档案():
    for mid in ("deepseek-v4-flash", "deepseek-v4-pro"):
        assert profile_for(mid) is DEEPSEEK


def test_KIMI_FORCED_强制思考_keep_all_每轮保留():
    assert KIMI_FORCED.supports_thinking is True
    assert KIMI_FORCED.toggleable is False     # 强制开，不显开关 UI
    assert KIMI_FORCED.keep == "all"
    assert KIMI_FORCED.effort_tiers == ()      # 无深度档
    assert KIMI_FORCED.keep_reasoning == "all"  # 每轮 assistant 都回传


def test_KIMI_K26_思考可开关_keep仍all_无深度():
    assert KIMI_K26.toggleable is True         # 只 type 可切 → 弹窗显"思考 开/关"
    assert KIMI_K26.keep == "all"              # keep 不做成开关，恒 all
    assert KIMI_K26.effort_tiers == ()         # 无深度档（Kimi 都没有）
    assert KIMI_K26.keep_reasoning == "all"


def test_DEEPSEEK档案_可开关_深度high_max默认max_仅工具回合保留():
    assert DEEPSEEK.supports_thinking is True
    assert DEEPSEEK.toggleable is True         # 可关 → 显开关 UI
    assert DEEPSEEK.keep is None               # 不带 keep 字段
    assert DEEPSEEK.effort_tiers == ("high", "max")
    assert DEEPSEEK.default_effort == "max"
    assert DEEPSEEK.keep_reasoning == "tool_calls"


def test_glm_只5_2有深度_其余无_都tool_calls():
    assert profile_for("glm-5.2") is GLM_52
    for mid in ("glm-5.1", "glm-5", "glm-5-turbo"):
        assert profile_for(mid) is GLM_PLAIN
    # 5.2：可开关 + 深度 high/max 默认 max；其余：可开关无深度；都只保留工具调用回合思考
    assert GLM_52.toggleable is True and GLM_52.effort_tiers == ("high", "max")
    assert GLM_52.default_effort == "max" and GLM_52.keep == None
    assert GLM_PLAIN.toggleable is True and GLM_PLAIN.effort_tiers == ()
    assert GLM_52.keep_reasoning == "tool_calls" and GLM_PLAIN.keep_reasoning == "tool_calls"


def test_glm_5v_turbo不注册_视觉不支持():
    assert profile_for("glm-5v-turbo") is INERT   # 视觉模型没 seed → inert


def test_minimax_M3可开关_M2_7强制_都adaptive_split_all():
    assert profile_for("MiniMax-M3") is MINIMAX_M3
    for mid in ("MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        assert profile_for(mid) is MINIMAX_FORCED
    # 都：on 值=adaptive、发 reasoning_split、每轮回传、无深度
    for p in (MINIMAX_M3, MINIMAX_FORCED):
        assert p.on_value == "adaptive" and p.reasoning_split is True
        assert p.keep_reasoning == "all" and p.effort_tiers == ()
    assert MINIMAX_M3.toggleable is True        # M3 可关
    assert MINIMAX_FORCED.toggleable is False   # M2.7 强制开


def test_未知模型_inert_不支持思考_不保留():
    prof = profile_for("Qwen3.6")
    assert prof is INERT
    assert prof.supports_thinking is False     # 不发 thinking
    assert prof.keep_reasoning == ""           # 不回传 reasoning


def test_seed_四家():
    names = {p.name for p in PROVIDERS}
    assert names == {"kimi", "deepseek", "glm", "minimax"}
    ds = next(p for p in PROVIDERS if p.name == "deepseek")
    assert ds.base_url == "https://api.deepseek.com"
    assert {m.id for m in ds.models} == {"deepseek-v4-flash", "deepseek-v4-pro"}
    glm = next(p for p in PROVIDERS if p.name == "glm")
    assert glm.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert {m.id for m in glm.models} == {"glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo"}
    mm = next(p for p in PROVIDERS if p.name == "minimax")
    assert mm.base_url == "https://api.minimaxi.com/v1"
    assert {m.id for m in mm.models} == {"MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"}
