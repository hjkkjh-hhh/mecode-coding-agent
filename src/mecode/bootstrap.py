"""headless 编程入口 —— 把散在人机入口（TUI/chat）里的组装接线收拢成一个工厂。

人用 TUI / chat，【程序】用这里：
    from mecode import build_agent
    agent = build_agent()                        # 组装：config.json 后端 + 全套工具 + 记忆 + MCP + 权限 + 会话
    answer = agent.ask("这个 repo 的测试怎么跑？")  # 跑完整个工具循环，返回最终正文
    for ev in agent.run_turn("…"): ...            # 或自己消费事件流（与 TUI/chat 同款）
一次性命令行形态见 __main__.py：python -m mecode -p "问题"。

无头环境没人能点审批弹窗（Agent._gate：需要问却没人能问 → 保守拒绝），mode 选权限档位：
    normal  处处要批 → 无头下≈只读（读命令默认放行，写/命令被拒）
    auto    【默认】项目内 write/edit 自动放行，其余照旧——能干活又不出圈
    yolo    全放行（慎用；给完全信任的自动化场景）
    plan    不支持——计划要人批，无头场景无意义（要交互请用 TUI）
要交互审批：传 ask_permission 回调（(tool, args) -> "once"/"always"/"deny"，同 chat.py 的约定）。

工具的相对路径（bash 工作目录、grep/glob 默认根）解析自【进程工作目录】——cwd 参数管的是
会话归属 / 权限项目根 / MCP ${cwd}；跨目录使用请调用方自行 os.chdir 或让模型用绝对路径。
MCP 子进程默认 atexit 收尾；要提前关：[c.stop() for c in agent.mcp_clients]。
"""
from __future__ import annotations

import atexit
from pathlib import Path

from .agent import Agent
from .config import agent_config, current_agent_config, current_backend
from .mcp import setup_mcp
from .memory import build_memory_prompt, memory_tools
from .mode import MODES, apply_mode
from .permission import PermissionPolicy
from .provider import Provider
from .session import SessionStore
from .system_prompt import build_system_prompt
from .tools import ToolRegistry, default_registry


def build_agent(cwd: str | Path | None = None, *, mode: str = "auto",
                session_id: str | None = None, mcp: bool = True,
                ask_permission=None, registry: ToolRegistry | None = None,
                provider: Provider | None = None) -> Agent:
    """一个调用完成全部组装，返回可用的 Agent。

    cwd            项目根（默认进程当前目录）：决定会话归属（slug）、权限根、MCP ${cwd}
    mode           权限档位 normal/auto/yolo（默认 auto；plan 拒绝——无头没人批计划）
    session_id     给了就续那个会话（载入其历史消息）；None=新会话
    mcp            是否同步连 MCP server 并注册其工具（无配置时瞬时返回）
    ask_permission ask 类工具的审批回调；None=保守拒绝（纯无头）
    registry       复用已组装的工具表（如换会话重建 agent 时），此时跳过工具/MCP 组装
    provider       注入自定义 Provider（测试/自定义后端）；None=按此刻 config.json 造
    """
    if mode not in MODES or mode == "plan":
        raise ValueError(f"mode 必须是 normal/auto/yolo（收到 {mode!r}）——"
                         "plan 模式的计划需要人审批，无头场景不支持，请用 TUI。")
    cwd = Path(cwd) if cwd else Path.cwd()
    store = SessionStore(root=agent_config.session_root, cwd=cwd, session_id=session_id)

    mcp_clients: list = []
    if registry is None:
        registry = default_registry()
        for t in memory_tools(store.memory_dir):
            registry.register(t)
        if mcp:
            mcp_clients = setup_mcp(registry, cwd=str(cwd))
            if mcp_clients:      # 兜底收尾：进程退出时关 MCP 子进程（要提前关自己拿 mcp_clients）
                atexit.register(lambda cs=tuple(mcp_clients): [c.stop() for c in cs])

    if provider is None:
        b = current_backend()
        if not b.model:
            raise RuntimeError("还没有配置模型后端——先在 TUI 里 /config 配置一个，"
                               "或写 ~/.mecode/config.json，或传入自定义 provider。")
        provider = Provider(b)

    system = build_system_prompt(session_dir=store.dir)
    memory_prompt = build_memory_prompt(store.memory_dir)
    if memory_prompt:
        system = f"{system}\n\n{memory_prompt}"

    agent = Agent(provider, registry,
                  system_prompt=system,
                  config=current_agent_config(),
                  store=store,
                  resume_messages=(store.load_messages() or None) if session_id else None,
                  policy=apply_mode(PermissionPolicy.from_persisted(
                      store.load_permissions(), project_root=store.cwd), mode),
                  ask_permission=ask_permission)
    agent.mode = mode
    agent.mode_reminder = MODES[mode].prompt      # 模式段每轮注入（不进 system prompt，同 TUI 约定）
    agent.mcp_clients = mcp_clients               # 暴露给调用方显式 stop；registry 复用时为空（归首建者管）
    return agent
