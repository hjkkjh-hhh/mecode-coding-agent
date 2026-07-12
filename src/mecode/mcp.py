"""MCP（Model Context Protocol）客户端：把外部 MCP server 的工具接进来。

MCP server 是个子进程，和它走 JSON-RPC 2.0 / stdio——每条消息一行 JSON、'\\n' 分隔。
握手流程：initialize → notifications/initialized → tools/list（拿工具 schema）→ tools/call（调工具）。

设计：一个后台【读线程】把 server 的输出按行解析、按 id 投递给等待的请求（跨平台 + 支持超时——
Windows 上管道不能 select，靠读线程最稳）。mecode 的工具调用是同步的（一轮里一个个来），
所以"发请求→等对应 id 的响应"这种简单模型够用。

接进来后，MCP 工具由 mcp_tools() 包成 mecode 的 Tool，和本地工具同走 schema/execute/截断/权限闸。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .tools import Tool


class MCPError(Exception):
    pass


def _exec_cmd(cmd: list[str]) -> list[str]:
    """把命令规整成当前平台 Popen 能直接起的形式（非 Windows 原样返回）。

    Windows 关键坑：npx/npm/uvx 这些是 .cmd/.bat 批处理，CreateProcess 不能直接执行——
    裸 Popen(['npx', ...]) 会报 WinError 2「找不到文件」（MCP server 因此连不上、显示未接入）。
    解析出真实路径：.cmd/.bat 经 `cmd /c` 起，其余(.exe)用解析到的全路径。"""
    if os.name != "nt" or not cmd:
        return cmd
    head, rest = cmd[0], cmd[1:]
    exe = shutil.which(head)
    # which 可能命中同名【无扩展】脚本（CreateProcess 照样跑不了）→ 退一步显式找 .cmd/.exe/.bat
    if exe is None or not exe.lower().endswith((".cmd", ".bat", ".exe", ".com")):
        for ext in (".cmd", ".exe", ".bat"):
            alt = shutil.which(head + ext)
            if alt:
                exe = alt
                break
    if exe is None:
        return cmd                                   # 找不到：原样交给 Popen 抛清楚的错
    if exe.lower().endswith((".cmd", ".bat")):
        # 用【原始名】而非解析出的全路径：npx 装在 C:\Program Files\...（带空格），
        # `cmd /c "带空格全路径" 参数...` 会触发 cmd 的首尾引号剥离 → 执行 C:\Program 直接失败
        # （表现=进程没起来、initialize 超时）。which 能找到说明在 PATH 上，cmd 自己也能解析。
        return ["cmd", "/c", head, *rest]
    return [exe, *rest]


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict | None = None, cwd: str | None = None) -> None:
        self.name = name
        self._cmd = [command, *(args or [])]
        self._env = env
        self._cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}   # 请求 id → 等响应的队列
        self.tools: list[dict] = []                   # tools/list 拿到的工具 schema

    # --- 生命周期 ---
    def start(self, timeout: float = 15.0) -> list[dict]:
        """起子进程 + 握手 + 列工具，返回工具 schema 列表。"""
        env = {**os.environ, **(self._env or {})} if self._env else None
        self._proc = subprocess.Popen(
            _exec_cmd(self._cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=self._cwd,
            text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mecode", "version": "0"},
        }, timeout)
        self._notify("notifications/initialized", {})
        self.tools = self._request("tools/list", {}, timeout).get("tools", [])
        return self.tools

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None

    # --- 调工具 ---
    def call_tool(self, name: str, arguments: dict, timeout: float = 60.0) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout)
        return _extract_text(result)

    # --- JSON-RPC over stdio ---
    def _read_loop(self) -> None:
        """读线程：按行解析 server 输出，有 id 的响应投给等待的请求；通知/未知行忽略。
        进程退出 → stdout 关闭 → 循环结束。"""
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            q = self._pending.get(rid) if rid is not None else None
            if q is not None:
                q.put(msg)

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"MCP {self.name} 的 {method} 超时（{timeout}s）")
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise MCPError(f"MCP {self.name} 的 {method} 出错：{msg['error']}")
        return msg.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError(f"MCP {self.name} 未启动")
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()


def _extract_text(result: dict) -> str:
    """tools/call 的结果是 content 块列表（text/image/…）；取文本块拼起来喂回模型。
    isError=True 时也返回内容（标成错误，让模型看到）。"""
    parts = [b.get("text", "") for b in result.get("content", [])
             if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        return f"错误：{text}" if text else "错误：MCP 工具返回错误"
    return text or "(无文本输出)"


# ---------- 适配进 mecode：MCP 工具 → Tool，配置加载，连接 ----------

def mcp_tools(client: MCPClient) -> list[Tool]:
    """把一个已连 MCPClient 发现的工具包成 mecode 的 Tool（handler 走 tools/call）。
    工具名加 '<server>__' 前缀：防和本地工具/别的 server 撞名，也标明来源。
    这样 MCP 工具和本地工具同走 schema/execute/截断/外置/权限闸（默认不在放行表 → ask）。"""
    out = []
    for t in client.tools:
        orig = t["name"]
        out.append(Tool(
            name=f"{client.name}__{orig}",
            description=t.get("description", ""),
            parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
            handler=lambda args, _c=client, _n=orig: _c.call_tool(_n, args),
            # read_only 不设（默认 False）：MCP 工具语义未知，一律按可写对待、不进只读并发组
        ))
    return out


def load_mcp_config(paths: list) -> dict:
    """从给定路径(们)读 MCP 配置并合并 mcpServers（后面的同名覆盖前面的）。
    格式：{"mcpServers": {name: {"command": str, "args": [str], "env": {}, "cwd": str}}}。读不动的跳过。"""
    servers: dict = {}
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers.update(data.get("mcpServers", {}))
    return servers


# ---------- 配置占位符（让项目级 .mcp.json 可提交可分享） ----------
# 写死绝对路径的配置（如 filesystem server 的目录参数）随仓库分享后在别人机器上必坏。
# 业界标准解法是占位符（VS Code 的 ${workspaceFolder}、Claude Code 的 ${VAR}）：
#   ${cwd}       → 项目根绝对路径（正斜杠）
#   ${env:NAME}  → 环境变量值（API key 这类机密该这么写，别把 key 明文写进要提交的 .mcp.json）
# 展开发生在【连接时】（connect_servers），配置文件本身永远保持占位符形态。无占位符的配置原样不动。

_PLACEHOLDER = re.compile(r"\$\{(cwd|env:([A-Za-z_][A-Za-z0-9_]*))\}")


def _expand_str(s: str, cwd: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) == "cwd":
            return cwd
        return os.environ.get(m.group(2), "")   # 未设的环境变量 → 空串（server 起不来会有明确报错）
    return _PLACEHOLDER.sub(sub, s)


def expand_placeholders(cfg: dict, cwd: str | None) -> dict:
    """展开一个 server 配置里的 ${cwd} / ${env:NAME}（command/args/env/cwd 四个字段的字符串值）。
    返回新 dict，不改原配置。"""
    root = (cwd or os.getcwd()).replace("\\", "/")
    out = dict(cfg)
    if isinstance(out.get("command"), str):
        out["command"] = _expand_str(out["command"], root)
    if isinstance(out.get("args"), list):
        out["args"] = [_expand_str(a, root) if isinstance(a, str) else a for a in out["args"]]
    if isinstance(out.get("env"), dict):
        out["env"] = {k: _expand_str(v, root) if isinstance(v, str) else v
                      for k, v in out["env"].items()}
    if isinstance(out.get("cwd"), str):
        out["cwd"] = _expand_str(out["cwd"], root)
    return out


# ---------- 启停 + 握手超时（本机偏好：状态文件 + 新会话生效） ----------
# 不做运行时热开关：server 是活进程，当场杀/连要处理僵尸和半截调用，复杂度不值——
# 停用/改超时 = 下次启动生效（配置文件 .mcp.json 不动，随时可再改）。
# 状态存 ~/.mecode/mcp_state.json：{"disabled": [名...], "timeouts": {名: 秒}}——本机偏好，
# 不进可提交分享的 .mcp.json（那里只放"连什么"，不放"本机怎么调"）。

MCP_STATE_PATH = Path("~/.mecode/mcp_state.json").expanduser()
DEFAULT_MCP_TIMEOUT = 15                       # 握手（initialize / tools/list）默认超时秒数
TIMEOUT_PRESETS = (15, 30, 60, 120)           # TUI 点击循环的档位（轻量 15 → 重型 Chromium 120）


def _load_mcp_state() -> dict:
    try:
        data = json.loads(MCP_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_mcp_state(state: dict) -> None:
    """整体写回（读-改-写：保留 disabled/timeouts 全部键，别让一个操作抹掉另一个）。"""
    MCP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_mcp_disabled() -> set[str]:
    return set(_load_mcp_state().get("disabled", []))


def _timeout_of(state: dict, name: str) -> int:
    """从状态取某 server 的超时（非正/未设 → 默认）。"""
    t = state.get("timeouts", {}).get(name)
    return int(t) if isinstance(t, (int, float)) and t > 0 else DEFAULT_MCP_TIMEOUT


def set_server_enabled(name: str, enabled: bool) -> None:
    """改一个 MCP server 的启停（写禁用名单）。新会话生效——调用方（TUI 面板）负责提示这点。"""
    state = _load_mcp_state()
    disabled = set(state.get("disabled", []))
    (disabled.discard if enabled else disabled.add)(name)
    state["disabled"] = sorted(disabled)
    _save_mcp_state(state)


def get_server_timeout(name: str) -> int:
    """某 server 的握手超时秒数（未设 → 默认 15）。"""
    return _timeout_of(_load_mcp_state(), name)


def set_server_timeout(name: str, seconds: int) -> None:
    """设某 server 的握手超时（写状态文件）。新会话生效。"""
    state = _load_mcp_state()
    timeouts = dict(state.get("timeouts", {}))
    timeouts[name] = int(seconds)
    state["timeouts"] = timeouts
    _save_mcp_state(state)


def cycle_server_timeout(name: str) -> int:
    """把某 server 的超时切到下一档预设并保存，返回新值（TUI 点击 chip 用）。
    当前值不在预设里 → 回到第一档。"""
    cur = get_server_timeout(name)
    nxt = TIMEOUT_PRESETS[(TIMEOUT_PRESETS.index(cur) + 1) % len(TIMEOUT_PRESETS)] \
        if cur in TIMEOUT_PRESETS else TIMEOUT_PRESETS[0]
    set_server_timeout(name, nxt)
    return nxt


def enabled_servers(servers: dict) -> dict:
    """过滤掉禁用的 server（启动连接前调）。"""
    disabled = _load_mcp_disabled()
    return {n: cfg for n, cfg in servers.items() if n not in disabled}


def server_states(servers: dict) -> list[dict]:
    """给管理面板的完整视图：配置里全部 server + 启停状态 + 握手超时。每项 {name, command, enabled, timeout}。"""
    state = _load_mcp_state()
    disabled = set(state.get("disabled", []))
    return [{"name": n, "command": " ".join([cfg.get("command", ""), *cfg.get("args", [])]).strip(),
             "enabled": n not in disabled, "timeout": _timeout_of(state, n)}
            for n, cfg in servers.items()]


def connect_servers(servers: dict, cwd: str | None = None) -> tuple[list[MCPClient], list[Tool], list[str]]:
    """【并发】起 server + 握手 + 列工具 + 包成 Tool——每个 server 一个线程、用自己的握手超时，
    一个慢/坏的 server 只拖自己那条线程（其超时决定总耗时上限），不再串行连坐、阻塞其他 server 接入。
    某个 server 起不来不影响其他（记进 errors、跳过）。禁用的 server 直接跳过不起进程。
    结果按【配置顺序】收集（ThreadPoolExecutor.map 保序）→ 侧栏 server 行/工具顺序稳定。
    返回 (已连客户端, 所有 MCP Tool, 错误信息列表)。接口与串行版一致——调用方（TUI）拿到的仍是
    连完全部后的完整三元组，_mcp_errors 红色显示逻辑完全不受影响。"""
    enabled = enabled_servers(servers)
    if not enabled:
        return [], [], []
    state = _load_mcp_state()                 # 一次读全，超时逐 server 从这里取（免每线程各读盘）

    def _connect_one(item: tuple[str, dict]) -> tuple[MCPClient | None, list[Tool], str | None]:
        name, cfg = item
        try:
            cfg = expand_placeholders(cfg, cwd)   # ${cwd}/${env:NAME} → 实际值（配置文件保持占位符形态）
            c = MCPClient(name, cfg["command"], cfg.get("args"), cfg.get("env"), cfg.get("cwd") or cwd)
            c.start(timeout=_timeout_of(state, name))
            return c, mcp_tools(c), None
        except Exception as e:
            return None, [], f"{name}: {type(e).__name__}: {e}"

    clients: list[MCPClient] = []
    tools: list[Tool] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(enabled))) as ex:
        for c, ts, err in ex.map(_connect_one, enabled.items()):   # map 保序：按 enabled 配置顺序返回
            if err is not None:
                errors.append(err)
            else:
                clients.append(c)
                tools.extend(ts)
    return clients, tools, errors


def default_config_paths(cwd: str | None = None) -> list:
    """默认 MCP 配置路径：~/.mecode/mcp.json（全局）+ <cwd>/.mcp.json（项目，覆盖同名）。"""
    return [Path("~/.mecode/mcp.json").expanduser(), Path(cwd or ".") / ".mcp.json"]


def setup_mcp(registry, cwd: str | None = None, paths: list | None = None) -> list[MCPClient]:
    """消费端一键接 MCP（同步）：读配置 → 连所有 server → 把工具注册进 registry。返回已连客户端（退出 stop）。
    无配置则瞬时返回。server 起不来不抛、不影响启动；起得来的工具进 registry，和本地工具同走一切（含权限闸）。
    （TUI 走后台连，不用这个同步版；chat 调试 REPL 用它。）"""
    servers = load_mcp_config(paths if paths is not None else default_config_paths(cwd))
    clients, tools, _errors = connect_servers(servers, cwd=cwd)
    for t in tools:
        registry.register(t)
    return clients
