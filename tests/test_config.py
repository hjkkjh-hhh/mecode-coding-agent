"""配置解析：后端【单一真相源 = config.json】（.env 只作检测源）；上下文上限按模型窗口自动定；
config.json 往返 + saved 列表 + is_configured/env_backend。"""
import mecode.config as cfg


def test_current_backend_只认config_未配置为空(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("MECODE_BASE_URL", "http://env/v1")   # env 不参与后端解析
    be = cfg.current_backend()
    assert (be.base_url, be.model, be.api_key) == ("", "", "")   # 未配置=空串，无假默认
    cfg.save_user_config("http://cfg/v1", "m", "k")
    be = cfg.current_backend()
    assert (be.base_url, be.model, be.api_key) == ("http://cfg/v1", "m", "k")


def test_context_limit_大窗封顶CAP_可调(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")   # 隔离真实 config
    monkeypatch.delenv("MECODE_CONTEXT_CAP", raising=False)
    assert cfg._context_limit("MiniMax-M3") == 128_000       # 1M 窗口封顶到默认 CAP 128K
    assert cfg._context_limit("没注册的模型") == 100_000       # 未知 → 兜底 100K（< CAP）
    monkeypatch.setenv("MECODE_CONTEXT_CAP", "64000")
    assert cfg._context_limit("MiniMax-M3") == 64_000        # CAP 可调（压测压缩就把它设小）


def test_save_load_user_config往返(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    cfg.save_user_config("http://x/v1", "m", "sk-1")
    got = cfg.load_user_config()
    assert (got["base_url"], got["model"], got["api_key"]) == ("http://x/v1", "m", "sk-1")
    assert got["saved"] == [{"base_url": "http://x/v1", "model": "m", "api_key": "sk-1"}]


def test_save_user_config_可选字段_传0或空则不写(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    cfg.save_user_config("b", "m", "k")                     # 全默认 → 必填三字段 + saved 列表
    assert set(cfg.load_user_config()) == {"base_url", "model", "api_key", "saved"}
    cfg.save_user_config("b", "m", "k", keep_reasoning="all", context_cap=64000, compact_threshold=0.8)
    got = cfg.load_user_config()
    assert {k: got[k] for k in ("base_url", "model", "api_key", "keep_reasoning",
                                "context_cap", "compact_threshold")} == {
        "base_url": "b", "model": "m", "api_key": "k",
        "keep_reasoning": "all", "context_cap": 64000, "compact_threshold": 0.8}


def test_context_limit_cap_config大于env(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("MECODE_CONTEXT_CAP", "30000")
    assert cfg._context_limit("MiniMax-M3") == 30_000        # 没配 config → env 兜着
    cfg.save_user_config("b", "m", "k", context_cap=50000)
    assert cfg._context_limit("MiniMax-M3") == 50_000        # config.json（UI 设的）优先于 env


def test_compact_threshold_config大于env大于默认(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("MECODE_COMPACT_THRESHOLD", raising=False)
    assert cfg._compact_threshold() == 0.7                   # 无 env 无 config → 默认
    monkeypatch.setenv("MECODE_COMPACT_THRESHOLD", "0.5")
    assert cfg._compact_threshold() == 0.5                   # 没配 config → env 兜着
    cfg.save_user_config("b", "m", "k", compact_threshold=0.85)
    assert cfg._compact_threshold() == 0.85                  # config.json（UI 设的）优先于 env


def test_load_user_config_无文件或坏文件返回空(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "nope.json")
    assert cfg.load_user_config() == {}                      # 不存在
    (tmp_path / "bad.json").write_text("{坏", encoding="utf-8")
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "bad.json")
    assert cfg.load_user_config() == {}                      # 坏 JSON 不崩


def test_saved_configs_upsert原位更新(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    cfg.save_user_config("b1", "m1", "k1")
    cfg.save_user_config("b2", "m2", "k2", keep_reasoning="all")
    assert [(e["model"], e.get("keep_reasoning")) for e in cfg.saved_configs()] == \
        [("m1", None), ("m2", "all")]                        # 每次保存都进 saved 列表
    cfg.save_user_config("b1", "m1", "k1-new")               # 同 (base,model) 再存 → 原位更新、顺序不变
    models = [e["model"] for e in cfg.saved_configs()]
    assert models == ["m1", "m2"] and cfg.saved_configs()[0]["api_key"] == "k1-new"


def test_delete_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    cfg.save_user_config("b1", "m1", "k1")
    cfg.save_user_config("b2", "m2", "k2")
    cfg.delete_saved("b1", "m1")
    assert [e["model"] for e in cfg.saved_configs()] == ["m2"]   # 删掉指定条
    assert cfg.load_user_config()["model"] == "m2"               # 顶层当前后端不受影响


def test_env_backend(monkeypatch):
    for k in ("MECODE_MODEL", "MECODE_BASE_URL", "MECODE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert cfg.env_backend() == {}                           # 没设 MECODE_MODEL → 无 env 后端
    monkeypatch.setenv("MECODE_MODEL", "m")
    monkeypatch.setenv("MECODE_BASE_URL", "http://e/v1")
    assert cfg.env_backend() == {"base_url": "http://e/v1", "model": "m", "api_key": ""}


def test_is_configured_只看config(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("MECODE_MODEL", "glm-5")
    assert cfg.is_configured() is False                      # .env 配了也不算（单一真相源=config）
    cfg.save_user_config("http://x", "m", "k")
    assert cfg.is_configured() is True                       # config.json 有 model 才算
