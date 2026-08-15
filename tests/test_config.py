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


def test_context_cap跟条目走_切后端各用各的(tmp_path, monkeypatch):
    """8K 窗口的自建模型和 1M 的线上模型，同一个全局上限没法同时合适。
    顶层那份是"当前后端的"，切后端时被整体重写（_write_user_config 是覆盖不是合并）。
    compact_threshold 不在此列——那是"我想多早压"的个人偏好，与模型无关。"""
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("MECODE_CONTEXT_CAP", raising=False)

    cfg.save_user_config("http://local/v1", "Qwen3.6", "k", context_cap=8192, compact_threshold=0.6)
    assert cfg._context_limit("Qwen3.6") == 8192
    got = cfg.load_user_config()
    assert got["saved"][0]["context_cap"] == 8192        # 进了条目
    assert "compact_threshold" not in got["saved"][0]    # 阈值是全局的，不进条目

    # 切到另一个后端（没设上限）→ 顶层那份被重写掉，各用各的，不被 8192 拖累
    cfg.save_user_config("http://api/v1", "MiniMax-M3", "k")
    assert cfg._context_limit("MiniMax-M3") == 128_000
    assert "context_cap" not in cfg.load_user_config()

    # 切回来：条目里的 8192 还在，UI 据此回填并带回顶层
    e = next(x for x in cfg.saved_configs() if x["model"] == "Qwen3.6")
    cfg.save_user_config(e["base_url"], e["model"], e["api_key"], context_cap=e["context_cap"])
    assert cfg._context_limit("Qwen3.6") == 8192


def test_update_settings_只改设置项_不动后端(tmp_path, monkeypatch):
    """/config 里那两个框的行内保存：改个阈值不该被迫走"确定并保存"（那条会热切后端）。
    上限同步进当前后端的 saved 条目（它跟条目走）；阈值只在顶层（全局偏好）。"""
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("MECODE_CONTEXT_CAP", raising=False)
    cfg.save_user_config("http://a/v1", "m1", "k1")
    cfg.save_user_config("http://b/v1", "m2", "k2")            # 当前是 m2

    cfg.update_settings(context_cap=8192)
    got = cfg.load_user_config()
    assert (got["base_url"], got["model"], got["api_key"]) == ("http://b/v1", "m2", "k2")  # 后端没动
    assert got["context_cap"] == 8192
    assert [e.get("context_cap") for e in got["saved"]] == [None, 8192]   # 只落到当前那条

    cfg.update_settings(compact_threshold=0.5)
    got = cfg.load_user_config()
    assert got["compact_threshold"] == 0.5 and got["context_cap"] == 8192  # 另一项不受影响
    assert all("compact_threshold" not in e for e in got["saved"])         # 阈值不进条目

    cfg.update_settings(context_cap=0)                                     # 0 = 清掉，回默认
    got = cfg.load_user_config()
    assert "context_cap" not in got and "context_cap" not in got["saved"][1]
    assert cfg._context_limit("m2") == 100_000        # 回兜底（m2 未注册，min(100K, CAP 128K)）


def test_migrate_context_cap_老的全局上限抄进各条(tmp_path, monkeypatch):
    """老配置里 context_cap 只在顶层（那时它是全局的）。改成跟条目走之后，一次切后端就会把顶层
    整体覆盖掉、凭空消失。开窗时迁移一次，之后各条各改各的。幂等。"""
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("MECODE_CONTEXT_CAP", raising=False)
    cfg.save_user_config("http://a/v1", "m1", "k")
    cfg.save_user_config("http://b/v1", "m2", "k")
    import json
    p = tmp_path / "config.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["context_cap"] = 100_000                       # 复原"老配置"：只有顶层有
    p.write_text(json.dumps(d), encoding="utf-8")

    cfg.migrate_context_cap()
    assert [e["context_cap"] for e in cfg.saved_configs()] == [100_000, 100_000]
    # 幂等：某条改小后再迁移，不会被顶层那份覆盖回去
    cfg.update_settings(context_cap=8192, for_backend=("http://a/v1", "m1"))
    cfg.migrate_context_cap()
    assert [e["context_cap"] for e in cfg.saved_configs()] == [8192, 100_000]


def test_update_settings_可指定写给别的后端(tmp_path, monkeypatch):
    """切换页可以【不切过去】就改别的模型的上限。顶层那份是"当前后端的副本"，给别人改时不能动它
    ——否则正在跑的 agent 会用上别人的值。"""
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.delenv("MECODE_CONTEXT_CAP", raising=False)
    cfg.save_user_config("http://a/v1", "m1", "k")
    cfg.save_user_config("http://b/v1", "m2", "k", context_cap=50_000)   # 当前是 m2

    cfg.update_settings(context_cap=8192, for_backend=("http://a/v1", "m1"))
    got = cfg.load_user_config()
    assert got["saved"][0]["context_cap"] == 8192        # 写给了 m1
    assert got["context_cap"] == 50_000                  # 顶层（当前 m2）没被动
    assert cfg._context_limit("m2") == 50_000
