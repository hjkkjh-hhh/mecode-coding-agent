"""mecode —— 我们自己的 agent harness。

程序里用（headless，见 bootstrap.py）：
    from mecode import build_agent
    agent = build_agent()
    print(agent.ask("这个 repo 的测试怎么跑？"))
人机交互入口在 scripts/：tui.py（textual 界面）、chat.py（命令行 REPL）。
"""
from .agent import Agent
from .bootstrap import build_agent
from .config import AgentConfig, Backend, agent_config, current_backend
from .provider import Provider
from .session import SessionStore
from .tools import Tool, ToolRegistry, default_registry

__version__ = "0.0.1"
__all__ = [
    "Agent", "build_agent",
    "AgentConfig", "Backend", "agent_config", "current_backend",
    "Provider", "SessionStore",
    "Tool", "ToolRegistry", "default_registry",
    "__version__",
]
