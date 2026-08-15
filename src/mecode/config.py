"""运行配置：后端【单一真相源 = 用户级 ~/.mecode/config.json】，无则内置默认。

.env 不再参与后端优先级——只作【检测源】：TUI 发现 .env 配了模型且 config 里没有时，
在 /config 切换页给一行"点此加载"，导入进 config 后统一从 config 走。这样避免"两处配置谁生效"的困惑。
（.env 仍加载：MECODE_MAX_ITERATIONS 等开发者调试旋钮还认环境变量；只有 base_url/model/api_key 三个后端字段不认。）
机密（api_key）永远在 repo 之外：config.json 在用户目录；**故意不"从工作目录往上找 .env"**（防项目里带 key 的 .env 被 git 提交）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]                      # mecode 安装根（src/mecode/config.py → 上三级）
USER_CONFIG_PATH = Path("~/.mecode/config.json").expanduser()    # 首次引导 / `/config` 写这里

try:
    from dotenv import load_dotenv

    # 按【绝对路径】加载 mecode 根的 .env（CWD 无关；tui/CLI/测试等所有入口一致，不再"往上找"）
    load_dotenv(_ROOT / ".env", override=True)
except ImportError:  # dotenv 可选；没装就只读真实环境变量
    pass


def load_user_config() -> dict:
    """读用户级 config.json（引导写的 {base_url, model, api_key}）；无/坏返回空 dict。"""
    try:
        return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_user_config(data: dict) -> None:
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_user_config(base_url: str, model: str, api_key: str, keep_reasoning: str = "",
                     context_cap: int = 0, compact_threshold: float = 0.0) -> None:
    """把后端写进用户级 config.json（首次引导 / `/config` 用）；目录懒建；key 在任何 repo 之外。
    context_cap / compact_threshold 传 0 = 不写字段（用内置默认）。
    **context_cap 跟条目走**（同 keep_reasoning）：8K 窗口的自建模型和 1M 的线上模型，同一个全局值
    没法同时合适——顶层那份是"当前后端的"，切后端时被整体重写（_write_user_config 是覆盖不是合并）。
    compact_threshold 仍是全局：它是"我想多早压"这种个人偏好，与模型无关。
    每次保存同时把该后端【upsert 进 saved 列表】——配置过的模型都留档（切换页从这里读）。
    upsert 是【原位更新】：重存已有条目只更新字段、不改它在列表里的位置（列表顺序=配置先后，稳定）。"""
    data: dict = {"base_url": base_url, "model": model, "api_key": api_key}
    entry: dict = {"base_url": base_url, "model": model, "api_key": api_key}
    if keep_reasoning:
        data["keep_reasoning"] = keep_reasoning
        entry["keep_reasoning"] = keep_reasoning
    if context_cap:
        data["context_cap"] = context_cap
        entry["context_cap"] = context_cap      # 跟【条目】走：它是"这个模型用多少上下文"，换后端就该换值
    if compact_threshold:
        data["compact_threshold"] = compact_threshold
    saved = load_user_config().get("saved", [])
    for i, e in enumerate(saved):
        if (e.get("base_url"), e.get("model")) == (base_url, model):
            saved[i] = entry
            break
    else:
        saved.append(entry)
    data["saved"] = saved
    _write_user_config(data)


def migrate_context_cap() -> None:
    """一次性迁移：老配置的 context_cap 只在顶层（那时它是全局的），改成跟条目走之后，
    一次切后端就会把它整体覆盖掉、凭空消失。这里把它抄进【所有】条目一次，之后各条各改各的。
    幂等：只要已经有任一条目带了 context_cap 就不再动。"""
    data = load_user_config()
    cap, saved = data.get("context_cap"), data.get("saved", [])
    if not cap or not saved or any(e.get("context_cap") for e in saved):
        return
    for e in saved:
        e["context_cap"] = cap
    _write_user_config(data)


def update_settings(*, context_cap: int | None = None,
                    compact_threshold: float | None = None,
                    for_backend: tuple[str, str] | None = None) -> None:
    """只改设置项，**不动后端三元组、不切后端**（/config 里那两个框的行内保存）。
    传 None = 该项不动；传 0 = 清掉该项（回内置默认）。

    context_cap 跟条目走：for_backend=(base_url, model) 指定写给【哪一条】（切换页可以不切过去就改
    别的模型）；不传则写当前后端。只有改的就是当前后端时才同步顶层那份——顶层是"当前后端的副本"，
    给别人改却动它，会让正在跑的 agent 用上别人的值。
    compact_threshold 只在顶层，它是与模型无关的全局偏好。"""
    data = load_user_config()
    cur = (data.get("base_url"), data.get("model"))
    target = for_backend or cur
    if compact_threshold is not None:
        if compact_threshold:
            data["compact_threshold"] = compact_threshold
        else:
            data.pop("compact_threshold", None)
    if context_cap is not None:
        for e in data.get("saved", []):
            if (e.get("base_url"), e.get("model")) == target:
                if context_cap:
                    e["context_cap"] = context_cap
                else:
                    e.pop("context_cap", None)
                break
        if target == cur:                       # 改的是当前后端 → 顶层那份跟着变
            if context_cap:
                data["context_cap"] = context_cap
            else:
                data.pop("context_cap", None)
    _write_user_config(data)


def saved_configs() -> list[dict]:
    """已保存过的后端列表（供"已保存"页展示/切换）；每项 {base_url, model, api_key[, keep_reasoning]}。"""
    return load_user_config().get("saved", [])


def delete_saved(base_url: str, model: str) -> None:
    """从 saved 列表删一条（key 过期/填错/不用了）。删的若是当前后端，顶层字段保留——
    当前连接不受影响，只是下次它不再出现在切换列表。"""
    data = load_user_config()
    data["saved"] = [e for e in data.get("saved", [])
                     if (e.get("base_url"), e.get("model")) != (base_url, model)]
    _write_user_config(data)


def env_backend() -> dict:
    """.env / 环境变量里检测到的后端（设了 MECODE_MODEL 才算数）；无返回 {}。
    仅作【检测源】给 UI 引导导入（"检测到 .env 有 xxx 模型，点此加载"），不直接参与后端解析。"""
    if not os.getenv("MECODE_MODEL"):
        return {}
    return {"base_url": os.getenv("MECODE_BASE_URL", ""),
            "model": os.getenv("MECODE_MODEL", ""),
            "api_key": os.getenv("MECODE_API_KEY", "")}


def is_configured() -> bool:
    """是否已有可用后端（决定要不要弹首次引导）：只看 config.json——它是后端唯一真相源。
    .env 配了模型也不算"已配置"：会在引导/切换页给"点此加载"入口导入进 config。"""
    return bool(load_user_config().get("model"))


# 有效上下文【封顶】：长上下文本身会拉低模型推理能力（不只是"记不住"——哪怕检索到位、输入越长越钝，
# 见 arXiv:2510.05381 等）。故大窗模型不按 70%×窗口 压（1M×0.7=700K 会长期泡在退化区），而是把
# context_limit 封顶到 CAP（默认 128K → 压缩在 ~90K 触发）。本地小窗模型窗口 < CAP，不受影响。
_CONTEXT_CAP_DEFAULT = 128_000
_DEFAULT_LIMIT = 100_000        # 未知/未注册模型的兜底窗口


def _context_limit(model: str) -> int:
    """压缩用的上下文上限 = min(模型窗口, CAP)。
    CAP 优先级：config.json context_cap（UI 可设，和后端字段同一真相源）> env MECODE_CONTEXT_CAP > 默认 128K
    （越低越聪明但压得越勤，越高压得少但推理越钝）。要压测压缩逻辑就把 CAP 设小（如 8000）。

    注：曾想给每个后端条目加一个自填 context_window（自建/本地模型注册表不认识 → 按兜底 100K 算，
    压缩永远触发不了）。后来去掉了——最终取的是 min(窗口, CAP)，而 CAP 本来就是 UI 可设的，
    单后端下"填窗口 8192"和"填上限 8192"完全等价，多一个框只是多一个概念。"""
    from .registry import context_window_for      # 局部 import：registry 不依赖 config，避免顶层耦合
    cap = int(load_user_config().get("context_cap", 0)) or int(os.getenv("MECODE_CONTEXT_CAP", "0")) \
        or _CONTEXT_CAP_DEFAULT
    return min(context_window_for(model) or _DEFAULT_LIMIT, cap)


def _compact_threshold() -> float:
    """压缩触发阈值（上下文占用超上限几成就压）：config.json（UI 可设）> env > 默认 0.7。"""
    return float(load_user_config().get("compact_threshold", 0)
                 or os.getenv("MECODE_COMPACT_THRESHOLD", 0) or 0.7)


_cfg = load_user_config()


@dataclass(frozen=True)
class Backend:
    """未配置时三字段为空串——不再有 localhost/Qwen3.6/dummy 假默认：
    没配就是没配（is_configured=False），由 UI 引导去 /config，发消息前也会拦截。"""
    base_url: str = _cfg.get("base_url", "")
    model: str = _cfg.get("model", "")
    api_key: str = _cfg.get("api_key", "")


@dataclass(frozen=True)
class AgentConfig:
    """Agent 运行行为配置。

    和 Backend 分工：Backend 管“连哪个后端”，AgentConfig 管“循环怎么跑”。
    都可用环境变量覆盖；context_limit 默认按当前模型窗口自动定（见 _context_limit）。
    """
    max_iterations: int = int(os.getenv("MECODE_MAX_ITERATIONS", "100"))           # 单轮内最多循环几次（防跑飞）
    context_limit: int = _context_limit(_cfg.get("model", ""))                     # 上下文 token 上限（按模型窗口自动定，可 /config 改 CAP）
    compact_threshold: float = _compact_threshold()                               # 超过上限几成就压（config.json > env > 0.7）
    tool_result_max_chars: int = int(os.getenv("MECODE_TOOL_RESULT_MAX_CHARS", "6000"))  # 单个工具结果入场上限（头+尾共留这么多字）约1000token
    tool_result_keep_tail: int = int(os.getenv("MECODE_TOOL_RESULT_KEEP_TAIL", "1200"))   # 末尾保底留多少（给报错/结尾状态）
    rescue_read_tools: str = os.getenv("MECODE_RESCUE_READ_TOOLS", "read_file")          # 压缩后保留原文的"read类"工具名（逗号分隔）
    rescue_read_count: int = int(os.getenv("MECODE_RESCUE_READ_COUNT", "0"))             # 压缩后保留最近几次 read 的原文（0=不限条数，全由 rescue_read_max_tokens 封顶）
    rescue_read_max_files: int = int(os.getenv("MECODE_RESCUE_READ_MAX_FILES", "5"))     # 救援最多涉及几个不同文件（0=不限）；这几个文件的【全部】区间都留，总量再由 max_tokens 封顶
    rescue_read_max_tokens: int = int(os.getenv("MECODE_RESCUE_READ_MAX_TOKENS", "5000"))  # 救援原文总量上限（0=不限）。默认 5000≈原先"6000字截断×5条"的实际量，零回归；显式封顶，免得改了截断值就失控
    no_edit_reminder_turns: int = int(os.getenv("MECODE_NO_EDIT_REMINDER_TURNS", "25"))   # 连续几次迭代没改工作目录下的文件就注一条提示（0=关）
    session_root: str = os.getenv("MECODE_SESSION_ROOT", "~/.mecode")                     # 会话存储根：~/.mecode/projects/<slug>/sessions/<uuid>/


backend = Backend()
agent_config = AgentConfig()


def current_backend() -> Backend:
    """按【此刻】的 config.json 现造 Backend（模块级 backend 是导入时冻结的快照）。
    热切模型（/config 保存后不重启）用这个拿最新后端。未配置时字段为空串。"""
    c = load_user_config()
    return Backend(base_url=c.get("base_url", ""), model=c.get("model", ""),
                   api_key=c.get("api_key", ""))


def current_agent_config() -> AgentConfig:
    """按【此刻】的 config.json 现造 AgentConfig（context_limit/compact_threshold 跟当前模型与配置走）。"""
    from dataclasses import replace
    c = load_user_config()
    return replace(agent_config,
                   context_limit=_context_limit(c.get("model", "")),
                   compact_threshold=_compact_threshold())
