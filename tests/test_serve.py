"""OpenAI 兼容网关(scripts/serve.py):system 合并、历史装载、响应形状、HTTP 层、流式与中断。

固化的不变量:
- 来访 system(人格)拼在 mecode 自身 system 之后——两者都在,缺一不可
- 历史只透传 role/content;末条 user 是本轮输入;无消息报 400 级错误
- 消息带 name 字段(agentscope 调用方)→ 缝进正文开头 [name],补齐 role 层表达不了的发言人身份
- 响应是标准 chat.completion 形状;工具循环在网关内走完、对外折叠成一条消息
- HTTP 层:/health 探活、错误走 OpenAI 风格 {"error":{...}} 不裸 traceback
- stream=true 走标准 SSE(role 块→心跳空 delta→单块正文→finish→[DONE])
- 客户端断连 → agent 被协作式中断(provider 的 should_stop 置位);/v1/interrupt 手动全场刹车
"""
import json
import sys
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import serve  # noqa: E402
from mecode import bootstrap  # noqa: E402
from mecode.config import agent_config  # noqa: E402
from mecode.events import Done, ReasoningDelta, TextDelta, ToolCall  # noqa: E402
from mecode.tools import Tool, ToolRegistry  # noqa: E402


class _TextProvider:
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("回答")
        yield Done(reason="stop")


class _SlowStopProvider:
    """持续吐字直到 should_stop 置位——模拟长生成,验证断连/手动中断真的把它停下。"""
    def __init__(self):
        self.noticed = threading.Event()   # 感知到 should_stop 时置位(测试断言用)

    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("开")
        for _ in range(200):               # 上限 ~20s,防测试失败时跑飞
            if should_stop is not None and should_stop():
                self.noticed.set()
                return
            time.sleep(0.1)
            yield TextDelta("字")
        yield Done(reason="stop")


class _ToolThenText:
    """第一轮调工具、第二轮出答案——验证工具循环在网关内走完、对外折叠。"""
    def __init__(self):
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n == 1:
            yield ToolCall(id="c1", name="noop", arguments={})
        else:
            yield TextDelta("跑完工具后的最终答案")
        yield Done(reason="stop")


def _kw(monkeypatch, tmp_path, provider=None):
    """网关调 build_agent 的注入参数:tmp 会话根 + 假 provider + 不连 MCP。"""
    monkeypatch.setattr(bootstrap, "agent_config",
                        replace(agent_config, session_root=str(tmp_path / "root")))
    return {"cwd": tmp_path, "mcp": False, "provider": provider or _TextProvider()}


_REQ = {
    "model": "test-model",
    "messages": [
        {"role": "system", "content": "你是价值分析师佩雷拉"},
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "开始你的分析"},
    ],
}


def test_prepare_persona拼接在mecode系统之后_历史装载(monkeypatch, tmp_path):
    agent, user_input = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    sysmsg = agent.messages[0]["content"]
    assert "价值分析师佩雷拉" in sysmsg                 # 调用方人格在
    assert sysmsg.index("价值分析师") > 100             # 且拼在 mecode 自身 system(工具/技能说明)之后
    assert user_input == "开始你的分析"                 # 末条 user = 本轮输入
    roles = [m["role"] for m in agent.messages]
    assert roles[-2:] == ["user", "assistant"]         # 中间历史已装载(system 后接前两条)


def test_handle_chat_标准响应形状(monkeypatch, tmp_path):
    resp = serve.handle_chat(_REQ, **_kw(monkeypatch, tmp_path))
    assert resp["object"] == "chat.completion" and resp["model"] == "test-model"
    choice = resp["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "回答"}
    assert choice["finish_reason"] == "stop"
    u = resp["usage"]
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"] > 0


def test_handle_chat_工具循环在网关内走完(monkeypatch, tmp_path):
    reg = ToolRegistry()
    reg.register(Tool(name="noop", description="测试用",
                      parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"))
    kw = _kw(monkeypatch, tmp_path, provider=_ToolThenText())
    kw["registry"] = reg
    resp = serve.handle_chat(_REQ, **kw)
    assert resp["choices"][0]["message"]["content"] == "跑完工具后的最终答案"


def test_tag命名与请求存档(monkeypatch, tmp_path):
    kw = _kw(monkeypatch, tmp_path)
    kw["tag"] = "7"
    agent, _ = serve._prepare(_REQ, **kw)
    agent2, _ = serve._prepare(_REQ, **kw)
    assert agent.store.dir.name == "7-1"                    # 目录名 = 专家编号-第几次发言
    assert agent2.store.dir.name == "7-2"                   # 序号扫目录自增
    ctx = json.loads((agent.store.dir / "request.json").read_text(encoding="utf-8"))
    assert ctx["persona"] == "你是价值分析师佩雷拉"                  # 调用方人格入档
    assert [m["content"] for m in ctx["history"]] == ["第一轮问题", "第一轮回答"]   # 装载的辩论历史入档
    assert ctx["user_input"] == "开始你的分析"
    assert "价值分析师佩雷拉" in ctx["system_merged"]               # 合并后 system 入档(mecode 半边+人格半边)
    assert ctx["system_merged"].index("价值分析师") > 100


def test_场次信号_三段命名_全场账本(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_SHARED_DIR", tmp_path / "shared")
    kw = _kw(monkeypatch, tmp_path)
    kw["tag"] = "7"
    req = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "你是专家"},
            {"role": "user", "content": "……前情……当前是 Round 2。候补专家席:[X]\n👉 [INSTRUCTION]: 请分析流动性缺口。"},
        ],
    }
    serve.handle_chat(req, **kw)
    proj = tmp_path / "root" / "projects"
    assert any(p.name == "7-1-01-发言" for p in proj.rglob("7-*"))  # 未收信号默认场次1,四段命名:编号-场次-全场序-类型
    line = json.loads((tmp_path / "shared" / "第1场.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["expert"] == "7" and line["发言序"] == 1
    assert line["kind"] == "发言"                                  # 调用类型入账(👉 指令 → 发言)
    assert line["轮次声明"] == 2                                   # "当前是 Round 2" 的声明被解析
    assert line["instruction"].startswith("👉 [INSTRUCTION]:")     # 触发段从主持人带的话开始
    assert line["reply"] == "回答"
    assert serve._debate_next() == 2                               # 未收信号的调用已占用场次1 → 开新场=2(不并入前场)
    serve.handle_chat(req, **kw)                                   # → 7-2-01(全场序随场清零)
    assert serve._debate_next() == 3                               # 再开新场
    serve.handle_chat(req, **kw)                                   # → 7-3-01
    names = {p.name for p in proj.rglob("7-*") if p.is_dir()}
    assert {"7-1-01-发言", "7-2-01-发言", "7-3-01-发言"} <= names
    assert (tmp_path / "shared" / "第2场.jsonl").is_file()         # 账本按场分文件


class _TextToolText:
    """第一轮正文+工具调用同一条消息、第二轮收尾——两种形态:主体在前(违约收场白)
    与主体在后(正常叙述+完整交付),验证末条为主+收场白检测兜底。"""
    def __init__(self, first: str, second: str):
        self.first, self.second = first, second
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n == 1:
            yield TextDelta(self.first)
            yield ToolCall(id="c1", name="noop", arguments={})
        else:
            yield TextDelta(self.second)
        yield Done(reason="stop")


def _noop_reg():
    reg = ToolRegistry()
    reg.register(Tool(name="noop", description="测试用",
                      parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"))
    return reg


def test_末条只是收场白_兜底拼接不丢正文(monkeypatch, tmp_path):
    kw = _kw(monkeypatch, tmp_path, provider=_TextToolText("四维推演主报告正文" * 50, "收尾摘要"))
    kw["registry"] = _noop_reg()
    resp = serve.handle_chat(_REQ, **kw)
    content = resp["choices"][0]["message"]["content"]
    assert "四维推演主报告正文" in content        # 主体在带工具调用的消息里,不能丢
    assert content.endswith("收尾摘要")           # 按时序拼接,收场白在尾


def test_末条自包含完整交付_不带过程叙述(monkeypatch, tmp_path):
    kw = _kw(monkeypatch, tmp_path, provider=_TextToolText("先看一下工作区环境", "完整的最终发言正文" * 50))
    kw["registry"] = _noop_reg()
    resp = serve.handle_chat(_REQ, **kw)
    content = resp["choices"][0]["message"]["content"]
    assert "先看一下工作区环境" not in content    # 正常形态:中间过程叙述不进辩论
    assert content == "完整的最终发言正文" * 50


class _ReasonProvider:
    def stream(self, messages, tools=None, should_stop=None):
        yield ReasoningDelta("思考流内容")
        yield TextDelta("回答")
        yield Done(reason="stop")


def test_思考透传(monkeypatch, tmp_path):        # 名字刻意短:长测试名会让 tmp_path 超 Windows 260 字符路径上限
    # 非流式:message 带 reasoning_content(无思考的请求不带该字段,形状测试另有保证)
    resp = serve.handle_chat(_REQ, **_kw(monkeypatch, tmp_path, provider=_ReasonProvider()))
    msg = resp["choices"][0]["message"]
    assert msg["content"] == "回答"
    assert msg["reasoning_content"] == "思考流内容"
    # 流式:思考块先于正文块到达
    base, srv = _http_srv(monkeypatch, tmp_path, provider=_ReasonProvider())
    try:
        req = dict(_REQ); req["stream"] = True
        reasoning, text, order = "", "", []
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                delta = json.loads(line[len("data: "):])["choices"][0]["delta"]
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]; order.append("r")
                if delta.get("content"):
                    text += delta["content"]; order.append("c")
        assert reasoning == "思考流内容" and text == "回答"
        assert order.index("r") < order.index("c")            # 思考先行
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_精简system_辩论专属(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_LEAN_SYSTEM", True)
    agent, _ = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    sysmsg = agent.messages[0]["content"]
    assert "编程助手" not in sysmsg                    # 编码工作流模板整体移除
    assert "default-search" in sysmsg                  # 检索优先级规矩内置
    assert "安全与边界" in sysmsg                      # SAFETY 保留
    assert "dispatch_speakers" not in sysmsg           # 未注册调度/分歧工具的网关不出现其要点
    names = {s["function"]["name"] for s in agent.tools.schemas()}
    assert "web_search" in names                       # 无 default-search 技能:内置 web 工具保留


def test_精简system_技能存在时移除内置web工具(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_LEAN_SYSTEM", True)
    skill = tmp_path / ".mecode" / "skills" / "default-search"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# default-search", encoding="utf-8")
    agent, _ = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    names = {s["function"]["name"] for s in agent.tools.schemas()}
    assert "web_search" not in names and "web_fetch" not in names   # 物理移除
    sysmsg = agent.messages[0]["content"]
    assert "联网检索一律用 default-search 技能" in sysmsg            # 检索文案随工具表改写
    assert "web_search" not in sysmsg and "web_fetch" not in sysmsg  # 被移除的工具只字不提


def test_精简system_辩论工具要点仅注册侧出现(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_LEAN_SYSTEM", True)
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    monkeypatch.setattr(serve, "_DISPUTE_TOOL", True)
    agent, _ = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    sysmsg = agent.messages[0]["content"]
    ptr = sysmsg.index("工具使用要点")
    end = sysmsg.index("安全与边界")
    seg = sysmsg[ptr:end]                              # 要点并入同一清单,不另起一节
    assert "dispatch_speakers 提交本手调度决策" in seg
    assert "manage_disputes 维护辩论分歧账本" in seg
    assert "价值分析师佩雷拉" in sysmsg                # 人格照常拼在其后
    assert "回复契约" in sysmsg                        # 契约照常注入


def test_回复契约注入system(monkeypatch, tmp_path):
    agent, _ = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    assert "回复契约" in agent.messages[0]["content"]      # 告知模型末条自包含,与末条语义配套


def test_name字段缝进正文_区分发言人(monkeypatch, tmp_path):
    req = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "你是趋势分析师"},
            {"role": "user", "content": "开案卷宗", "name": "User"},
            {"role": "assistant", "content": "价值派的分析", "name": "ValueAnalyst"},
            {"role": "user", "content": "👉 [INSTRUCTION]: 请继续", "name": "Moderator"},
        ],
    }
    agent, user_input = serve._prepare(req, **_kw(monkeypatch, tmp_path))
    contents = [m["content"] for m in agent.messages[1:]]
    assert contents[0] == "[User] 开案卷宗"
    assert contents[1] == "[ValueAnalyst] 价值派的分析"     # 他人发言 role=assistant 与自己无异,工牌是唯一区分
    assert user_input == "[Moderator] 👉 [INSTRUCTION]: 请继续"
    # 账本触发段从标记起下刀,工牌前缀不入账
    assert serve._extract_instruction(user_input) == "👉 [INSTRUCTION]: 请继续"
    # 无 name 的普通 OpenAI 请求原样不动
    _, plain = serve._prepare(_REQ, **_kw(monkeypatch, tmp_path))
    assert plain == "开始你的分析"
    # 正文已自带同名工牌(模型学舌)→ 不再叠加,防 [X] [X] 逐轮滚雪球
    dup = {"role": "assistant", "content": "[ValueAnalyst] 已带工牌的发言", "name": "ValueAnalyst"}
    assert serve._stamp_name(dup) == "[ValueAnalyst] 已带工牌的发言"


def test_没有消息_报错(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        serve._prepare({"messages": []}, **_kw(monkeypatch, tmp_path))


def _http_srv(monkeypatch, tmp_path, provider=None):
    """起一个真 HTTP 网关(端口 0 随机),返回 base url 与 server。"""
    serve._Handler.build_kwargs = _kw(monkeypatch, tmp_path, provider=provider)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def test_stream_标准SSE形状(monkeypatch, tmp_path):
    base, srv = _http_srv(monkeypatch, tmp_path)          # _TextProvider → "回答"
    try:
        req = dict(_REQ); req["stream"] = True
        chunks, done = [], False
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    done = True
                    break
                chunks.append(json.loads(data))
        assert done                                        # 以 [DONE] 收尾
        assert chunks[0]["object"] == "chat.completion.chunk"
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}   # 首块带 role
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert text == "回答"                              # 正文与非流式一致
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


class _DispatchProvider:
    """第一轮调 dispatch_speakers、第二轮出正文——验证调度决策的结构化透传。"""
    def __init__(self, args: dict):
        self.args = args
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n == 1:
            yield ToolCall(id="d1", name="dispatch_speakers", arguments=self.args)
        else:
            yield TextDelta("已完成本手调度")
        yield Done(reason="stop")


_DISPATCH_ARGS = {
    "mode": "sequential", "status": "continue", "reason": "接力质询",
    "assignments": [{"expert": "ValueAnalyst", "task": "先测算"},
                    {"expert": "TrendAnalyst", "task": "看完前者后质询"}],
}


def test_调度工具_透传与账本(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    shared = tmp_path / "sh"
    shared.mkdir()
    (shared / "debate.json").write_text('{"n": 1, "seq": 0}', encoding="utf-8")
    monkeypatch.setattr(serve, "_SHARED_DIR", shared)
    kw = _kw(monkeypatch, tmp_path, provider=_DispatchProvider(dict(_DISPATCH_ARGS)))
    kw["tag"] = "10"
    kw["mode"] = "yolo"                                    # 生产网关同档:自定义工具免审执行
    req = {"model": "m", "messages": [{"role": "user", "content": "【调度状态】请点名"}]}
    resp = serve.handle_chat(req, **kw)
    d = resp["mecode_dispatch"]
    assert d["mode"] == "sequential" and d["status"] == "continue"
    assert [a["expert"] for a in d["assignments"]] == ["ValueAnalyst", "TrendAnalyst"]
    row = json.loads((shared / "第1场.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["dispatch"]["assignments"][1]["task"] == "看完前者后质询"
    assert row["session"].endswith("-调度")


def test_调度工具_流式尾帧携带(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    base, srv = _http_srv(monkeypatch, tmp_path, provider=_DispatchProvider(dict(_DISPATCH_ARGS)))
    serve._Handler.build_kwargs["mode"] = "yolo"
    try:
        req = {"model": "m", "stream": True,
               "messages": [{"role": "user", "content": "请点名"}]}
        got = None
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                obj = json.loads(line[len("data: "):])
                if obj.get("mecode_dispatch"):
                    got = obj["mecode_dispatch"]
                    assert obj["choices"][0]["finish_reason"] == "stop"    # 挂在尾帧
        assert got and got["mode"] == "sequential"
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_调度工具_校验拒绝不透传(monkeypatch, tmp_path):
    bad = {"mode": "single", "status": "continue", "assignments": []}
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    kw = _kw(monkeypatch, tmp_path, provider=_DispatchProvider(bad))
    kw["mode"] = "yolo"
    resp = serve.handle_chat({"model": "m", "messages": [{"role": "user", "content": "点名"}]}, **kw)
    assert "mecode_dispatch" not in resp                   # 校验未过,不透传
    assert resp["choices"][0]["message"]["content"]        # 正常折叠出正文


def test_流式live帧_工具与碎片实时透传(monkeypatch, tmp_path):
    """mecode_live 显示通道:工具起止/正文碎片实时下发,权威正文尾帧契约不变。"""
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    base, srv = _http_srv(monkeypatch, tmp_path, provider=_DispatchProvider(dict(_DISPATCH_ARGS)))
    serve._Handler.build_kwargs["mode"] = "yolo"
    try:
        req = {"model": "m", "stream": True,
               "messages": [{"role": "user", "content": "请点名"}]}
        lives, final_text, got_dispatch = [], "", None
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                obj = json.loads(line[len("data: "):])
                if obj.get("mecode_live"):
                    lives.append(obj["mecode_live"])
                if obj.get("mecode_dispatch"):
                    got_dispatch = obj["mecode_dispatch"]
                final_text += (obj["choices"][0]["delta"].get("content") or "")
        kinds = [x["kind"] for x in lives]
        assert "tool" in kinds and "tool_done" in kinds       # 工具起止实时可见
        tool = next(x for x in lives if x["kind"] == "tool")
        assert tool["name"] == "dispatch_speakers" and tool["args"]
        assert "text" in kinds                                # 正文碎片实时可见
        assert final_text == "已完成本手调度"                   # 权威正文尾帧不受影响
        assert got_dispatch and got_dispatch["mode"] == "sequential"
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_调度工具_finish带点名拒绝(monkeypatch, tmp_path):
    bad = {"mode": "single", "status": "finish", "reason": "收官",
           "assignments": [{"expert": "ValueAnalyst", "task": "最后陈词"}]}
    monkeypatch.setattr(serve, "_DISPATCH_TOOL", True)
    kw = _kw(monkeypatch, tmp_path, provider=_DispatchProvider(bad))
    kw["mode"] = "yolo"
    resp = serve.handle_chat({"model": "m", "messages": [{"role": "user", "content": "点名"}]}, **kw)
    assert "mecode_dispatch" not in resp                   # finish 与点名同手提交,校验拒绝


class _DisputeProvider:
    """逐轮各调一次 manage_disputes、操作发完后出正文——验证分歧操作的按序累积透传。"""
    def __init__(self, ops: list):
        self.ops = list(ops)
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        if self.ops:
            self.n += 1
            yield ToolCall(id=f"q{self.n}", name="manage_disputes", arguments=self.ops.pop(0))
        else:
            yield TextDelta("分歧操作已提交")
        yield Done(reason="stop")


_DISPUTE_OPS = [
    {"action": "open", "id": "D1", "severity": "致命", "parties": ["RiskMonitor", "ValueAnalyst"],
     "description": "RELV 终值 4.6% vs 3.7%"},
    {"action": "close", "id": "D1", "resolution": "采纳一方", "adopted": "ValueAnalyst 的 3.7%",
     "counterparty_position": "最后陈述为 4.6%", "counterparty_confirmed": False},
]


def test_分歧工具_按序累积透传与账本(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_DISPUTE_TOOL", True)
    shared = tmp_path / "sh"
    shared.mkdir()
    (shared / "debate.json").write_text('{"n": 1, "seq": 0}', encoding="utf-8")
    monkeypatch.setattr(serve, "_SHARED_DIR", shared)
    kw = _kw(monkeypatch, tmp_path, provider=_DisputeProvider([dict(o) for o in _DISPUTE_OPS]))
    kw["tag"] = "10"
    kw["mode"] = "yolo"
    req = {"model": "m", "messages": [{"role": "user", "content": "【调度状态】请核账"}]}
    resp = serve.handle_chat(req, **kw)
    ops = resp["mecode_disputes"]
    assert [o["action"] for o in ops] == ["open", "close"]             # 两次调用按序累积
    assert ops[0]["severity"] == "致命" and ops[0]["parties"] == ["RiskMonitor", "ValueAnalyst"]
    assert ops[1]["resolution"] == "采纳一方" and ops[1]["counterparty_confirmed"] is False
    row = json.loads((shared / "第1场.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert [o["id"] for o in row["disputes"]] == ["D1", "D1"]          # 账本同步落原文


def test_分歧工具_流式尾帧携带(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_DISPUTE_TOOL", True)
    base, srv = _http_srv(monkeypatch, tmp_path,
                          provider=_DisputeProvider([dict(_DISPUTE_OPS[0])]))
    serve._Handler.build_kwargs["mode"] = "yolo"
    try:
        req = {"model": "m", "stream": True,
               "messages": [{"role": "user", "content": "请核账"}]}
        got = None
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                obj = json.loads(line[len("data: "):])
                if obj.get("mecode_disputes"):
                    got = obj["mecode_disputes"]
                    assert obj["choices"][0]["finish_reason"] == "stop"    # 挂在尾帧
        assert got and got[0]["id"] == "D1" and got[0]["action"] == "open"
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_分歧工具_校验拒绝不透传(monkeypatch, tmp_path):
    bad = [{"action": "open", "id": "D1", "severity": "致命", "parties": []}]   # 缺当事人与描述
    monkeypatch.setattr(serve, "_DISPUTE_TOOL", True)
    kw = _kw(monkeypatch, tmp_path, provider=_DisputeProvider(bad))
    kw["mode"] = "yolo"
    resp = serve.handle_chat({"model": "m", "messages": [{"role": "user", "content": "核账"}]}, **kw)
    assert "mecode_disputes" not in resp                   # 校验未过,不透传
    assert resp["choices"][0]["message"]["content"]        # 正常折叠出正文


class _RecordingProvider:
    """记录每次收到的完整 messages——验证持续会话真的在网关侧续上下文。"""
    def __init__(self):
        self.calls = []

    def stream(self, messages, tools=None, should_stop=None):
        self.calls.append([dict(m) for m in messages])
        yield TextDelta(f"第{len(self.calls)}次回复")
        yield Done(reason="stop")


def _psess(turn, reset=False, kind=""):
    s = {"turn": turn}
    if reset:
        s["reset"] = True
    if kind:
        s["kind"] = kind
    return s


def _pkw(monkeypatch, tmp_path, prov):
    monkeypatch.setattr(serve, "_PERSISTENT", True)
    monkeypatch.setattr(serve, "_LIVE", {})
    kw = _kw(monkeypatch, tmp_path, provider=prov)
    kw["tag"] = "10"
    kw["mode"] = "yolo"
    return kw


def test_持续会话_跨手保留上下文(monkeypatch, tmp_path):
    prov = _RecordingProvider()
    kw = _pkw(monkeypatch, tmp_path, prov)
    r1 = serve.handle_chat({"model": "m", "mecode_session": _psess(1, reset=True),
                            "messages": [{"role": "system", "content": "你是辩论主持人甲"},
                                         {"role": "user", "content": "第一手指令"}]}, **kw)
    assert "第1次回复" in r1["choices"][0]["message"]["content"]
    serve.handle_chat({"model": "m", "mecode_session": _psess(2),
                       "messages": [{"role": "user", "content": "广播A", "name": "ValueAnalyst"},
                                    {"role": "user", "content": "第二手指令"}]}, **kw)
    ctx = prov.calls[1]
    text = "\n".join(m.get("content") or "" for m in ctx)
    assert "你是辩论主持人甲" in ctx[0]["content"]       # persona 装在创建时的 system 里
    assert "第一手指令" in text                           # 上一手输入还在网关侧
    assert any(m["role"] == "assistant" and "第1次回复" in (m.get("content") or "")
               for m in ctx)                              # 自己上一手回复以 assistant 留存
    assert "[ValueAnalyst] 广播A" in text                 # 增量带工牌进入
    assert ctx[-1]["role"] == "user" and "第二手指令" in ctx[-1]["content"]


def test_持续会话_同轮重试不重复(monkeypatch, tmp_path):
    prov = _RecordingProvider()
    kw = _pkw(monkeypatch, tmp_path, prov)
    serve.handle_chat({"model": "m", "mecode_session": _psess(1, reset=True),
                       "messages": [{"role": "user", "content": "开场"}]}, **kw)
    for _ in range(2):                                    # 同 turn=2 发两次(模拟失败重试)
        serve.handle_chat({"model": "m", "mecode_session": _psess(2),
                           "messages": [{"role": "user", "content": "增量B"},
                                        {"role": "user", "content": "第二手指令"}]}, **kw)
    text = "\n".join(m.get("content") or "" for m in prov.calls[2])
    assert text.count("增量B") == 1                       # 回滚后重放,不重复追加
    assert text.count("第二手指令") == 1
    assert "第2次回复" not in text                        # 上次尝试的回复一并回滚


def test_持续会话_reset新建不带旧场(monkeypatch, tmp_path):
    prov = _RecordingProvider()
    kw = _pkw(monkeypatch, tmp_path, prov)
    serve.handle_chat({"model": "m", "mecode_session": _psess(1, reset=True),
                       "messages": [{"role": "user", "content": "旧场开场词X"}]}, **kw)
    serve.handle_chat({"model": "m", "mecode_session": _psess(1, reset=True),
                       "messages": [{"role": "user", "content": "新场开场词Y"}]}, **kw)
    text = "\n".join(m.get("content") or "" for m in prov.calls[1])
    assert "旧场开场词X" not in text                      # reset 后旧场内容不带入
    assert "新场开场词Y" in text


def test_stream_断连触发中断(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "HEARTBEAT_SECS", 0.2)      # 加快断连发现,别拖慢测试
    provider = _SlowStopProvider()
    base, srv = _http_srv(monkeypatch, tmp_path, provider=provider)
    try:
        req = dict(_REQ); req["stream"] = True
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=req, timeout=30) as r:
            assert r.status_code == 200
            next(r.iter_lines())                           # 收到首块后……
        # ……with 退出 = 客户端断开连接(编排层取消的物理形态)
        assert provider.noticed.wait(8), "断连后 agent 未被中断(should_stop 未置位)"
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_interrupt端点_手动全场刹车(monkeypatch, tmp_path):
    provider = _SlowStopProvider()
    base, srv = _http_srv(monkeypatch, tmp_path, provider=provider)
    try:
        done: dict = {}
        def call():
            done["resp"] = httpx.post(f"{base}/v1/chat/completions", json=_REQ, timeout=30)
        th = threading.Thread(target=call, daemon=True)
        th.start()
        time.sleep(0.6)                                    # 等慢请求进入生成
        r = httpx.post(f"{base}/v1/interrupt", json={}, timeout=10)
        assert r.status_code == 200 and r.json()["interrupted"] >= 1
        th.join(10)
        assert provider.noticed.is_set()                   # 生成真的被停了
        resp = done["resp"]
        assert resp.status_code == 200                     # 打断前的部分正文照常返回
        assert resp.json()["choices"][0]["message"]["content"].startswith("开")
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}


def test_http层_health与chat与错误(monkeypatch, tmp_path):
    serve._Handler.build_kwargs = _kw(monkeypatch, tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(f"{base}/health").json() == {"status": "ok"}
        r = httpx.post(f"{base}/v1/chat/completions", json=_REQ, timeout=30)
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "回答"
        bad = httpx.post(f"{base}/v1/chat/completions", json={"messages": []}, timeout=30)
        assert bad.status_code == 400 and "error" in bad.json()      # OpenAI 风格错误,不裸 traceback
        assert httpx.post(f"{base}/别的路径", json={}).status_code == 404
    finally:
        srv.shutdown()
        serve._Handler.build_kwargs = {}
