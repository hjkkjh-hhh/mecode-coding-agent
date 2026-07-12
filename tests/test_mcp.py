"""MCPClient：和一个真·mock MCP server 子进程走完整 stdio JSON-RPC 流程。

固化的不变量：
- start() 握手 + tools/list 拿到工具 schema
- call_tool() 走 tools/call、从 content 文本块取结果
- isError 结果标成"错误："；工具出错/超时不挂死
"""
import json
import sys

import mecode.mcp as mcp
from mecode.mcp import (
    MCPClient, _extract_text, connect_servers, load_mcp_config, mcp_tools, setup_mcp,
)
from mecode.tools import Tool, ToolRegistry

# 一个最小 mock MCP server：读 JSON 行、按方法回响应。echo 回显，boom 返回 isError。
MOCK_SERVER = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    def send(result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
        sys.stdout.flush()
    if method == "initialize":
        send({"protocolVersion": "2024-11-05", "capabilities": {},
              "serverInfo": {"name": "mock", "version": "0"}})
    elif method == "notifications/initialized":
        pass  # 通知无响应
    elif method == "tools/list":
        send({"tools": [
            {"name": "echo", "description": "回显文本",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "boom", "description": "总是出错",
             "inputSchema": {"type": "object", "properties": {}}},
        ]})
    elif method == "tools/call":
        p = msg["params"]
        if p["name"] == "echo":
            send({"content": [{"type": "text", "text": "echo: " + p["arguments"].get("text", "")}]})
        else:
            send({"content": [{"type": "text", "text": "炸了"}], "isError": True})
'''


def _client(tmp_path):
    server = tmp_path / "mock_srv.py"
    server.write_text(MOCK_SERVER, encoding="utf-8")
    return MCPClient("mock", sys.executable, [str(server)])


def test_握手_列工具(tmp_path):
    c = _client(tmp_path)
    try:
        tools = c.start(timeout=10)
        assert [t["name"] for t in tools] == ["echo", "boom"]
        assert tools[0]["inputSchema"]["properties"]["text"]["type"] == "string"
    finally:
        c.stop()


def test_call_tool_取文本结果(tmp_path):
    c = _client(tmp_path)
    try:
        c.start(timeout=10)
        assert c.call_tool("echo", {"text": "hi"}, timeout=10) == "echo: hi"
    finally:
        c.stop()


def test_isError结果标成错误(tmp_path):
    c = _client(tmp_path)
    try:
        c.start(timeout=10)
        assert c.call_tool("boom", {}, timeout=10) == "错误：炸了"
    finally:
        c.stop()


def test_extract_text_纯函数():
    assert _extract_text({"content": [{"type": "text", "text": "a"},
                                      {"type": "text", "text": "b"}]}) == "a\nb"
    assert _extract_text({"content": [{"type": "image", "data": "x"}]}) == "(无文本输出)"
    assert _extract_text({"content": [{"type": "text", "text": "坏了"}], "isError": True}) == "错误：坏了"


# ---------- C2：适配进 mecode ----------

def test_mcp_tools_包成Tool_带前缀_handler可调(tmp_path):
    c = _client(tmp_path)
    try:
        c.start(timeout=10)
        tools = mcp_tools(c)
        assert [t.name for t in tools] == ["mock__echo", "mock__boom"]   # 加 server 前缀
        assert tools[0].parameters["properties"]["text"]["type"] == "string"
        assert tools[0].handler({"text": "yo"}) == "echo: yo"            # handler 走 tools/call
    finally:
        c.stop()


def test_load_mcp_config_合并(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        {"mcpServers": {"x": {"command": "cx"}}}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(
        {"mcpServers": {"y": {"command": "cy"}, "x": {"command": "cx2"}}}), encoding="utf-8")
    servers = load_mcp_config([tmp_path / "a.json", tmp_path / "b.json", tmp_path / "missing.json"])
    assert set(servers) == {"x", "y"}
    assert servers["x"]["command"] == "cx2"                             # 后者覆盖同名


def test_connect_servers_实连_注册进registry能execute(tmp_path):
    server = tmp_path / "mock_srv.py"
    server.write_text(MOCK_SERVER, encoding="utf-8")
    servers = {"mock": {"command": sys.executable, "args": [str(server)]}}
    clients, tools, errors = connect_servers(servers)
    try:
        assert errors == [] and [t.name for t in tools] == ["mock__echo", "mock__boom"]
        reg = ToolRegistry()
        for t in tools:
            reg.register(t)
        assert reg.execute("mock__echo", {"text": "hey"}) == "echo: hey"  # 和本地工具同走 execute
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "mock__echo" in names
    finally:
        for c in clients:
            c.stop()


def test_connect_servers_坏server不影响其他(tmp_path):
    servers = {"bad": {"command": "definitely-not-a-real-cmd-xyz", "args": []}}
    clients, tools, errors = connect_servers(servers)
    assert clients == [] and tools == [] and len(errors) == 1            # 起不来 → 记错、跳过、不抛


def test_setup_mcp_端到端_配置到注册(tmp_path):
    server = tmp_path / "mock_srv.py"
    server.write_text(MOCK_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mock": {"command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")
    reg = ToolRegistry()
    reg.register(Tool(name="local", description="x",                     # 本地工具
                      parameters={"type": "object", "properties": {}}, handler=lambda a: "local"))
    clients = setup_mcp(reg, paths=[cfg])                                # 消费端走的入口
    try:
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "local" in names and "mock__echo" in names               # 本地 + MCP 同一张表
        assert reg.execute("mock__echo", {"text": "z"}) == "echo: z"
    finally:
        for c in clients:
            c.stop()


def test_exec_cmd_windows_cmd批处理经cmd_c起_用原始名(monkeypatch):
    # Windows 真机坑①：npx/uvx 是 .cmd 批处理，裸 Popen 报 WinError 2 → 必须 cmd /c 起。
    # 真机坑②：npx 全路径在 C:\Program Files\（带空格），`cmd /c "带空格路径"` 触发 cmd 的
    # 引号剥离 → 去执行 C:\Program 失败（表现=进程没起、initialize 超时）→ 用【原始名】让 cmd 自己解析。
    from mecode import mcp
    monkeypatch.setattr(mcp.os, "name", "nt")
    monkeypatch.setattr(mcp.shutil, "which",
                        lambda n: r"C:\Program Files\nodejs\npx.CMD" if n in ("npx", "npx.cmd") else None)
    assert mcp._exec_cmd(["npx", "-y", "pkg"]) == ["cmd", "/c", "npx", "-y", "pkg"]


def test_exec_cmd_windows_exe用全路径不包cmd(monkeypatch):
    from mecode import mcp
    monkeypatch.setattr(mcp.os, "name", "nt")
    monkeypatch.setattr(mcp.shutil, "which", lambda n: r"C:\py\python.exe" if n == "python" else None)
    assert mcp._exec_cmd(["python", "srv.py"]) == [r"C:\py\python.exe", "srv.py"]   # .exe 直接起


def test_exec_cmd_非windows原样(monkeypatch):
    from mecode import mcp
    monkeypatch.setattr(mcp.os, "name", "posix")
    assert mcp._exec_cmd(["npx", "-y", "pkg"]) == ["npx", "-y", "pkg"]   # 非 nt 不动


def test_server启停_写状态文件_禁用不连(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "MCP_STATE_PATH", tmp_path / "mcp_state.json")
    servers = {"a": {"command": "x"}, "b": {"command": "y", "args": ["--z"]}}
    assert mcp.enabled_servers(servers) == servers            # 默认全启用
    mcp.set_server_enabled("a", False)
    assert list(mcp.enabled_servers(servers)) == ["b"]        # 禁用的被过滤（connect 据此跳过不起进程）
    states = {s["name"]: s for s in mcp.server_states(servers)}
    assert states["a"]["enabled"] is False and states["b"]["enabled"] is True
    assert states["b"]["command"] == "y --z"                  # 面板展示用的命令行
    mcp.set_server_enabled("a", True)
    assert mcp.enabled_servers(servers) == servers            # 重新启用


def test_connect_servers_跳过禁用的(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "MCP_STATE_PATH", tmp_path / "mcp_state.json")
    mcp.set_server_enabled("bad", False)
    # bad 的 command 根本不存在——若没被跳过会进 errors；被跳过则 clients/errors 都空
    clients, tools, errors = mcp.connect_servers({"bad": {"command": "不存在的命令xyz"}})
    assert clients == [] and errors == []                     # 直接跳过，没试着起进程


def test_expand_placeholders_cwd和env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-123")
    cfg = {"command": "npx", "args": ["-y", "pkg", "${cwd}", "${cwd}/data"],
           "env": {"TOKEN": "${env:MY_KEY}", "MODE": "prod"}, "cwd": "${cwd}/sub"}
    out = mcp.expand_placeholders(cfg, r"C:\proj")
    assert out["args"] == ["-y", "pkg", "C:/proj", "C:/proj/data"]   # 反斜杠归一 + 展开
    assert out["env"] == {"TOKEN": "sk-123", "MODE": "prod"}
    assert out["cwd"] == "C:/proj/sub"
    assert cfg["args"][2] == "${cwd}"                                # 原配置不被改（配置文件保持占位符形态）


def test_expand_placeholders_无占位符原样_未设env变量为空(monkeypatch):
    monkeypatch.delenv("NOT_SET_VAR", raising=False)
    cfg = {"command": "python", "args": ["srv.py", "C:/固定路径"]}
    out = mcp.expand_placeholders(cfg, "C:/proj")
    assert out["args"] == ["srv.py", "C:/固定路径"]                   # 旧配置零影响
    out2 = mcp.expand_placeholders({"command": "x", "args": ["${env:NOT_SET_VAR}"]}, "C:/p")
    assert out2["args"] == [""]                                       # 未设 → 空串（server 报错更直白）


# ---------- 握手超时（本机偏好，存 mcp_state.json，TUI 循环预设） ----------

def test_超时读写循环_与启停共存不互相抹掉(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "MCP_STATE_PATH", tmp_path / "mcp_state.json")
    assert mcp.get_server_timeout("x") == 15                  # 默认
    mcp.set_server_enabled("x", False)                        # 先写 disabled
    mcp.set_server_timeout("x", 60)                           # 再写 timeout：读-改-写保留两键
    state = json.loads((tmp_path / "mcp_state.json").read_text(encoding="utf-8"))
    assert "x" in state["disabled"] and state["timeouts"]["x"] == 60   # 两键共存（旧 bug：整覆盖会抹掉）
    assert mcp.get_server_timeout("x") == 60
    # 循环预设：15→30→60→120→15
    assert [mcp.cycle_server_timeout("y") for _ in range(5)] == [30, 60, 120, 15, 30]
    states = {s["name"]: s for s in mcp.server_states({"x": {"command": "c"}, "y": {"command": "d"}})}
    assert states["x"]["timeout"] == 60 and states["y"]["timeout"] == 30   # server_states 带 timeout


def test_connect_servers_用每server自己的超时(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "MCP_STATE_PATH", tmp_path / "mcp_state.json")
    mcp.set_server_timeout("slow", 60)                        # slow=60，fast 用默认 15
    got = {}
    def fake_start(self, timeout=15.0):                       # 假 start：只记它收到的超时
        got[self.name] = timeout
        self.tools = []
        return []
    monkeypatch.setattr(mcp.MCPClient, "start", fake_start)
    connect_servers({"slow": {"command": "c"}, "fast": {"command": "d"}})
    assert got == {"slow": 60, "fast": 15}                    # 各 server 用各自的超时


def test_connect_servers_并发_坏的不连累好的且保序(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "MCP_STATE_PATH", tmp_path / "mcp_state.json")
    server = tmp_path / "mock_srv.py"
    server.write_text(MOCK_SERVER, encoding="utf-8")
    good = {"command": sys.executable, "args": [str(server)]}
    servers = {"bad": {"command": "definitely-not-real-xyz"},   # 坏的排最前：串行会先卡它
              "s1": good, "s2": good}
    clients, tools, errors = connect_servers(servers)
    try:
        assert [c.name for c in clients] == ["s1", "s2"]      # 好的都连上、且按【配置顺序】（map 保序）
        assert len(errors) == 1 and errors[0].startswith("bad:")   # 坏的记进 errors、不连累好的
        assert [t.name for t in tools] == ["s1__echo", "s1__boom", "s2__echo", "s2__boom"]  # 工具也保序
    finally:
        for c in clients:
            c.stop()
