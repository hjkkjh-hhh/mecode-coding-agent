"""桌面端服务（scripts/deskserve.py）：信任闸、事件序列化、阻塞往返、状态快照、分页历史。

固化的不变量（每条都对应一个真踩过或差点踩到的坑）：
- 信任闸：无 token / 伪造 Origin / 伪造 Host 一律 403；【首页也要拦】——token 就注入在它的
  HTML 里，放行等于把 token 白送给本机任意进程
- 路径闸用 is_relative_to 而不是字符串前缀：startswith 会把同级的 tool_outputs2 也放进来
- 阻塞往返：答复按 id 路由、陈旧 id 丢弃；被打断时按 deny 返回【并主动下发 dismiss】收窗，
  不收窗的话前端留一张幽灵窗，用户下次点击被错配给下个请求（含把"总是允许"落到别的工具名下）
- 没有前端连着时不能无限等审批，超时判 deny，否则工作线程永久挂起
- 状态快照排除 elapsed 这类每秒都在变的量，否则每个 tick 都判成"变了"、每 0.5 秒推一帧
- 后台任务的 started_at 是 monotonic 值，跨进程无意义，必须换算成墙钟
- 事件泵 finally 无条件发 turn_end：少了它，一次崩溃就让前端永远卡在"忙"
- 历史带 tool_calls：少了它前端没法把工具结果贴回对应调用下方
- 配置接口【绝不下发 api_key 原文】：页面有 token 闸也不行，key 没理由离开本机进程；
  切换已存后端只送 (base_url, model)，key 由服务端自己从 config.json 取
- MCP server 名会成为工具名前缀，非标识符字符必须在写入前挡掉
- 改上限/阈值不重建 Provider：后端没换，重建只会把思考态重置回档案默认（纯副作用）
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deskserve  # noqa: E402
from mecode.events import (  # noqa: E402
    Done, Notice, ReasoningDelta, TextDelta, ToolResult, ToolStarted,
)
from mecode.permission import ALWAYS, DENY, ONCE  # noqa: E402
from mecode.registry import profile_for  # noqa: E402
from mecode.session import SessionStore  # noqa: E402


class _FakeProvider:
    """不联网的假后端：吐一句话就结束。

    profile / thinking_on / effort 是真 Provider 必有的运行态，桩也要有——
    设置面板（config_view / set_thinking）直接读它们，缺了就只能在产品代码里加
    getattr 兜底，那是拿产品代码迁就测试桩。用 INERT 档案（未注册模型的默认）。
    """
    profile = profile_for("")
    thinking_on = False
    effort = ""

    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("好的")
        yield Done(reason="stop")


@pytest.fixture
def workdir():
    """短路径的临时工作区。

    不用 pytest 的 tmp_path：它的目录名带上了测试函数名（本文件全是中文名），再套一层
    project_slug（slug 里含完整 cwd 路径）+ 会话 uuid + tool_outputs + 纳秒文件名，
    轻松超过 Windows 的 260 字符上限，表现成莫名其妙的 FileNotFoundError。
    """
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="dsk")).resolve()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def desk(workdir, monkeypatch):
    """一个不连真后端、不连 MCP、会话落在临时目录下的 Desk。

    agent_config 是 frozen dataclass（改不了字段），只能 replace 出副本再把两处引用换掉：
    bootstrap 建 store 时读它，deskserve 列会话/resume 时也读它——只换一处会让两边指向
    不同的根目录，测试里表现成"会话建好了但列不出来"。
    """
    from dataclasses import replace

    from mecode import bootstrap
    from mecode import config as cfg
    patched = replace(cfg.agent_config, session_root=str(workdir / "s"))
    monkeypatch.setattr(bootstrap, "agent_config", patched)
    monkeypatch.setattr(deskserve, "agent_config", patched)
    d = deskserve.Desk(workdir, "normal", mcp=False, provider=_FakeProvider())
    yield d
    d.close()


# ---------------------------------------------------------------- 事件序列化

def test_encode_各事件类型():
    assert deskserve.encode(TextDelta("a")) == {"type": "text", "text": "a"}
    assert deskserve.encode(ReasoningDelta("b"))["type"] == "reasoning"
    assert deskserve.encode(Notice("n")) == {"type": "notice", "text": "n"}
    assert deskserve.encode(Done("stop")) == {"type": "done", "reason": "stop"}
    s = deskserve.encode(ToolStarted("bash", {"command": "ls"}, "t1"))
    assert s == {"type": "tool_start", "id": "t1", "name": "bash", "args": {"command": "ls"}}


def test_encode_工具结果带出外置路径():
    """结果被外置时要把路径带给前端，否则"看全文"按钮无从下手。"""
    marker = "头部\n[后续输出已存盘：C:/x/tool_outputs/1-bash.txt（用 read_file 读取）]"
    m = deskserve.encode(ToolResult("bash", marker, "t1"))
    assert m["full_path"] == "C:/x/tool_outputs/1-bash.txt"
    # 没外置的结果不带这个字段，前端据此决定要不要给按钮
    assert "full_path" not in deskserve.encode(ToolResult("bash", "短输出", "t2"))


# ---------------------------------------------------------------- drain

def _q(msgs):
    q = queue.Queue()
    for m in msgs[1:]:
        q.put(m)
    return q, msgs[0]


def test_drain_合并相邻文本片():
    q, first = _q([{"type": "text", "text": "a"}, {"type": "text", "text": "b"},
                   {"type": "text", "text": "c"}])
    assert deskserve.drain(q, first) == [{"type": "text", "text": "abc"}]


def test_drain_不跨类型合并_也不合并工具事件():
    """工具事件合并会丢配对关系（tool_start/tool_result 靠 id 配对）。"""
    q, first = _q([{"type": "text", "text": "a"},
                   {"type": "tool_start", "id": "1", "name": "bash", "args": {}},
                   {"type": "text", "text": "b"}])
    out = deskserve.drain(q, first)
    assert [m["type"] for m in out] == ["text", "tool_start", "text"]


def test_drain_全量替换类只留最后一条():
    """bg/workflow/tasks 的语义是"当前全貌"，连渲三次中间态只会让侧栏抖。"""
    q, first = _q([{"type": "bg", "bg": [1]}, {"type": "bg", "bg": [1, 2]},
                   {"type": "text", "text": "x"}, {"type": "bg", "bg": []}])
    out = deskserve.drain(q, first)
    assert [m["type"] for m in out] == ["text", "bg"]
    assert out[-1]["bg"] == []          # 留的是最后那条


# ---------------------------------------------------------------- 阻塞往返

def test_pending_按id路由_陈旧id丢弃():
    p = deskserve.Pending()
    rid, rec = p.open({"type": "ask_permission"})
    assert p.answer("不存在的", ONCE) is False        # 未知 id：丢弃、不报错
    assert p.answer(rid, ONCE) is True
    assert rec["answer"] == ONCE
    p.close(rid)
    assert p.answer(rid, ALWAYS) is False             # 已摘除的槽：迟到答复必须丢


def test_pending_未答复的会被重放():
    p = deskserve.Pending()
    rid, _ = p.open({"type": "ask_permission", "tool": "bash"})
    assert [x["id"] for x in p.outstanding()] == [rid]
    p.answer(rid, DENY)
    assert p.outstanding() == []


def test_审批往返_答复后放行(desk):
    """跑在工作线程上的同步阻塞调用，HTTP 线程给答复后它继续。"""
    got = {}

    def worker():
        got["r"] = desk._ask_permission("bash", {"command": "ls"}, _Ctx())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    rid = _wait_pending(desk)
    desk.pending.answer(rid, ALWAYS)
    t.join(timeout=5)
    assert got["r"] == ALWAYS


def test_审批被打断_按deny返回且主动收窗(desk):
    """不收窗的话前端会留一张幽灵窗，把用户下一次点击错配给下一个请求。"""
    q = desk.bus.subscribe()
    ctx = _Ctx()
    got = {}
    t = threading.Thread(target=lambda: got.update(r=desk._ask_permission("bash", {}, ctx)),
                         daemon=True)
    t.start()
    rid = _wait_pending(desk)
    ctx.interrupt.set()
    t.join(timeout=5)
    assert got["r"] == DENY
    kinds = _drain_all(q)
    assert {"type": "dismiss", "id": rid} in kinds


def test_审批_没有前端连着时超时判deny(desk, monkeypatch):
    """没人能答就不能无限等，否则工作线程永久挂起、整个进程卡死。"""
    monkeypatch.setattr(deskserve, "NO_CLIENT_DENY_SECS", 0.2)
    got = {}
    t = threading.Thread(target=lambda: got.update(r=desk._ask_permission("bash", {}, _Ctx())),
                         daemon=True)
    t.start()
    t.join(timeout=5)
    assert got["r"] == DENY


def test_审批_返回值只认三个合法值(desk):
    """前端乱传（或被篡改）时保守拒绝，不能当成放行。"""
    t = threading.Thread(target=lambda: None)
    del t
    got = {}
    th = threading.Thread(target=lambda: got.update(r=desk._ask_permission("bash", {}, _Ctx())),
                          daemon=True)
    th.start()
    rid = _wait_pending(desk)
    desk.pending.answer(rid, "随便写的")
    th.join(timeout=5)
    assert got["r"] == DENY


class _Ctx:
    def __init__(self):
        self.interrupt = threading.Event()
        self.is_sub = False
        self.can_stop = False


def _wait_pending(desk, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        out = desk.pending.outstanding()
        if out:
            return out[0]["id"]
        time.sleep(0.01)
    raise AssertionError("等不到待答复请求")


def _drain_all(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


# ---------------------------------------------------------------- 状态快照

def test_快照排除每秒都在变的量(desk):
    """elapsed 每次调用都不同，进了比对键就等于每 0.5 秒推一帧。

    命令要选【足够长】的：用 echo 之类瞬时命令，第二次取快照时任务已经不在 running() 里，
    测的就成了"任务消失"而不是"时间流逝不改变快照"——测试自己会假绿/假红。
    """
    assert desk._snapshot()["bg"] == []
    desk.agent._bg.start(f'"{sys.executable}" -c "import time;time.sleep(6)"')
    s1 = _wait_until(lambda: desk._snapshot(), lambda s: bool(s["bg"]))
    row = s1["bg"][0]
    assert "elapsed" not in row and "next_checkin_at" not in row
    # started_at 是 monotonic 值、跨进程无意义 → 必须换算成墙钟
    assert abs(row["started_at_wall"] - time.time()) < 30
    time.sleep(TICK_GAP)
    assert desk._snapshot()["bg"] == s1["bg"]     # 只是时间流逝 → 快照必须一模一样


TICK_GAP = 1.2          # 明显长于 deskserve.TICK（0.5s），确保跨过了好几个轮询周期


def _wait_until(get, ok, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = get()
        if ok(v):
            return v
        time.sleep(0.05)
    raise AssertionError("等不到期望状态")


def test_workflow_run_id用阶段id拼_不用对象地址(desk):
    """id(run.stages) 是内存地址，跨进程毫无意义，而且原地改写时地址还不变。"""
    from mecode.workflow import Stage
    desk.agent._workflow.stages = [Stage(id="a", prompt="", after=[]),
                                   Stage(id="b", prompt="", after=["a"])]
    assert desk._snapshot()["workflow"]["run_id"] == "a|b"


# ---------------------------------------------------------------- 历史

def test_历史带tool_calls_和分页(desk):
    """少了 tool_calls，前端没法把工具结果贴回对应调用下方。"""
    st = desk.agent.store
    st.append_transcript({"role": "user", "content": "干活"})
    st.append_transcript({"role": "assistant", "content": "好",
                          "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "bash",
                                                       "arguments": '{"command":"ls"}'}}]})
    st.append_transcript({"role": "tool", "tool_call_id": "c1", "content": "结果"})
    h = desk.history()
    assert [x["role"] for x in h["items"]] == ["user", "assistant", "tool"]
    assert h["items"][1]["tool_calls"][0]["name"] == "bash"
    assert h["items"][2]["tool_call_id"] == "c1"      # 配对靠它
    # 分页：只要最后一条
    one = desk.history(before=0, limit=1)
    assert len(one["items"]) == 1 and one["total"] == 3


def test_历史保留注入的提醒并打标(desk):
    """直接丢掉会让"用户说了什么"和模型看到的对不上，排查问题时找不到北。"""
    desk.agent.store.append_transcript({"role": "user", "content": "<system-reminder>x</system-reminder>"})
    items = desk.history()["items"]
    assert items[-1]["reminder"] is True


# ---------------------------------------------------------------- 路径闸

def test_外置输出只能读本会话目录(desk, workdir):
    st = desk.agent.store
    p = Path(st.offload_tool_output("bash", "全文内容"))
    assert desk.tool_output(p.as_posix(), 0, 100)["chunk"] == "全文内容"
    # 同级目录必须挡住：字符串前缀比较会把 tool_outputs2 判成"在目录内"
    sibling = st.tool_outputs_dir.parent / (st.tool_outputs_dir.name + "2")
    sibling.mkdir(parents=True, exist_ok=True)
    evil = sibling / "evil.txt"
    evil.write_text("不该被读到", encoding="utf-8")
    assert "error" in desk.tool_output(evil.as_posix(), 0, 100)
    assert "error" in desk.tool_output(str(workdir / "外面.txt"), 0, 100)


def test_外置输出分页(desk):
    st = desk.agent.store
    p = Path(st.offload_tool_output("bash", "0123456789"))
    r = desk.tool_output(p.as_posix(), 0, 4)
    assert r["chunk"] == "0123" and r["eof"] is False and r["total"] == 10
    assert desk.tool_output(p.as_posix(), 8, 4)["eof"] is True


# ---------------------------------------------------------------- 跑一轮

def test_一轮跑完必发turn_end(desk):
    """finally 无条件发——少了它，一次崩溃就让前端永远卡在"忙"。"""
    q = desk.bus.subscribe()
    assert desk.send("你好") == "started"
    _wait_for(q, "turn_end")
    assert desk.busy is False


def test_模型抛异常也发turn_end(desk):
    class _Boom:
        def stream(self, *a, **k):
            raise RuntimeError("炸了")
            yield
    desk.agent.provider = _Boom()
    q = desk.bus.subscribe()
    desk.send("你好")
    msgs = _wait_for(q, "turn_end")
    assert any(m["type"] == "notice" and "炸了" in m["text"] for m in msgs)


def test_忙时排队而不是拒绝(desk):
    """直接回 409 会让用户以为自己白打了一段字。"""
    desk.busy = True
    q = desk.bus.subscribe()
    assert desk.send("插一句") == "queued"
    assert desk.agent.has_pending_user() is True
    assert any(m["type"] == "queued" for m in _drain_all(q))


def _wait_for(q, kind, timeout=15.0):
    end, seen = time.monotonic() + timeout, []
    while time.monotonic() < end:
        try:
            m = q.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(m)
        if m["type"] == kind:
            return seen
    raise AssertionError(f"等不到 {kind}，只收到 {[m['type'] for m in seen]}")


# ---------------------------------------------------------------- 信任闸

class _Req:
    """够用的假请求：只喂 Handler 的闸需要的东西。"""
    def __init__(self, path="/api/state", token=None, host="127.0.0.1:1", origin=None):
        self.path = path if token is None else f"{path}?token={token}"
        self.headers = {"Host": host}
        if origin:
            self.headers["Origin"] = origin
        self.sent = []

    def _json(self, code, body):
        self.sent.append((code, body))


def _guard(**kw):
    r = _Req(**kw)
    h = deskserve.Handler.__new__(deskserve.Handler)
    h.path, h.headers = r.path, r.headers
    h._json = r._json
    return deskserve.Handler._guard(h), r


def test_闸_无token拒绝():
    ok, r = _guard()
    assert ok is False and r.sent[0][0] == 403


def test_闸_正确token放行():
    ok, _ = _guard(token=deskserve.TOKEN)
    assert ok is True


def test_闸_伪造Host挡住DNS_rebinding():
    """把域名解析到 127.0.0.1 就能绕过同源，所以 Host 也要查。"""
    ok, r = _guard(token=deskserve.TOKEN, host="evil.com")
    assert ok is False and r.sent[0][0] == 403


def test_闸_跨站Origin挡住():
    """跨域 POST 属于简单请求、不触发预检，同源策略拦不住。"""
    ok, r = _guard(token=deskserve.TOKEN, origin="http://evil.com")
    assert ok is False and r.sent[0][0] == 403


def test_闸_同源Origin放行():
    ok, _ = _guard(token=deskserve.TOKEN, origin="http://127.0.0.1:8200")
    assert ok is True


def test_首页也在闸内():
    """token 就注入在首页 HTML 里，放行等于把 token 白送给本机任意进程。"""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "deskserve.py").read_text(
        encoding="utf-8")
    assert 'path in ("/", "", "/index.html")' in src


def test_接续轮不记空user消息(desk):
    """兜底接续必须走 run_bg_turn；拿空串走 run_turn 会往 transcript 记一条谁也没说过的话。

    桩要跟着 drain 排队消息：真 run_bg_turn 每轮开头就 drain，不 drain 的话 _pump 收尾时
    has_pending_user() 仍为真、立刻又起一轮，无限接续——断言"只起一轮"就变成了在赌时序。
    """
    calls = []

    def _bg():
        calls.append(("bg", None))
        desk.agent._drain_pending_user()
        return iter(())

    desk.agent.run_turn = lambda t: (calls.append(("turn", t)), iter(()))[1]
    desk.agent.run_bg_turn = _bg
    desk.agent.queue_user("插一句")
    desk.maybe_bg_turn()
    _wait_until(lambda: calls, bool)
    time.sleep(0.15)                      # 给"会不会多起一轮"留出暴露的时间
    assert calls == [("bg", None)]


def test_没有待办就不起空轮(desk):
    calls = []
    desk.agent.run_bg_turn = lambda: (calls.append(1), iter(()))[1]
    desk.maybe_bg_turn()
    time.sleep(0.2)
    assert calls == [] and desk.busy is False


def test_后台任务完成会触发接续轮(desk):
    """只推事件给前端而不告诉 agent，它会一直等一个永远不来的结果。"""
    calls = []
    desk.agent.run_bg_turn = lambda: (calls.append(1), iter(()))[1]
    desk.agent.queue_user("让它有待办")
    desk._on_bg_done(type("T", (), {"id": 1, "command": "c", "description": "",
                                    "is_subagent": False, "status": "done",
                                    "exit_code": 0, "output": ""})())
    _wait_until(lambda: calls, bool)
    assert calls == [1]


# ---------------------------------------------------------------- 设置面板：模型

def test_config_view_不下发_api_key(desk, monkeypatch):
    """页面上只该看到掩码。key 一旦下发，任何能读到页面内存的东西都拿得到它。"""
    monkeypatch.setattr(deskserve, "saved_configs", lambda: [
        {"base_url": "https://a.com", "model": "m1", "api_key": "sk-verysecret-12345"},
    ])
    v = desk.config_view()
    blob = json.dumps(v, ensure_ascii=False)
    assert "sk-verysecret-12345" not in blob
    assert v["saved"][0]["key_hint"]                      # 但要能认出是哪一条
    assert "api_key" not in v["saved"][0]


def test_mask_key():
    assert deskserve.mask_key("") == ""
    assert "verysecret" not in deskserve.mask_key("sk-verysecret-12345")
    assert deskserve.mask_key("sk-verysecret-12345").startswith("sk-ver")
    assert deskserve.mask_key("short").endswith("…")     # 短 key 不能把自己整个露出来
    assert "short" not in deskserve.mask_key("short")


def test_config_view_结构齐全(desk):
    v = desk.config_view()
    assert len(v["providers"]) >= 4                       # registry 里 seed 了四家
    assert all(p["models"] and p["key_url"] for p in v["providers"])
    assert set(v["prefs"]) >= {"context_cap", "compact_threshold", "context_limit", "threshold_now"}
    assert set(v["thinking"]) >= {"on", "effort", "supports", "toggleable", "efforts"}


def test_保存后端要三项齐全(desk):
    assert "error" in desk.save_backend("", "m", "k")
    assert "error" in desk.save_backend("https://a.com", "", "k")
    assert "error" in desk.save_backend("https://a.com", "m", "")
    assert "http" in desk.save_backend("a.com", "m", "k")["error"]   # 没协议头


def test_切到不存在的后端报错(desk):
    assert "error" in desk.switch_backend_to("https://nope", "nope")


def test_忙时切后端推迟到轮末(desk, monkeypatch):
    """中途换 Provider 会把正在跑的那条流搞乱，所以推迟；但【必须真的兑现】，
    否则用户只看到"已保存"、模型永远没换过。"""
    switched = []
    monkeypatch.setattr(desk, "_do_switch", lambda: switched.append(1))
    desk.busy = True
    r = desk._apply_backend()
    assert r["deferred"] is True and desk._pending_switch is True
    assert not switched                                   # 忙着，还没切
    desk.busy = False
    desk._pump(None)                                      # 走一遍收尾
    assert switched, "轮末没有兑现待切的后端"


def test_改上限阈值的校验(desk):
    assert "error" in desk.update_prefs(500, None)        # 太小
    assert "error" in desk.update_prefs("abc", None)      # 非数字
    assert "error" in desk.update_prefs(None, 0.99)       # 太高
    assert "error" in desk.update_prefs(None, 0.1)        # 太低


def test_改上限不重建_provider(desk, monkeypatch):
    """只改阈值就重建 Provider 的话，思考开关和深度档会被悄悄重置回档案默认。"""
    monkeypatch.setattr(deskserve, "update_settings", lambda **kw: None)
    prov = desk.agent.provider
    desk.update_prefs(None, 0.6)
    assert desk.agent.provider is prov


def test_思考档校验(desk):
    assert "error" in desk.set_thinking(None, "根本没有这一档")


# ---------------------------------------------------------------- 设置面板：技能

def test_技能清单字段齐(desk):
    for sk in desk.skills_view()["skills"]:
        assert set(sk) >= {"name", "description", "source", "path", "enabled"}


def test_跑不存在的技能报错(desk):
    assert "error" in desk.skill_run("根本没有这个技能")


def test_停用的技能不能直接使用(desk, monkeypatch):
    class _S:
        name, description, source, enabled = "s1", "d", "内置", False
        path = Path("x/SKILL.md")
    monkeypatch.setattr(deskserve, "discover_skills", lambda *a, **k: [_S()])
    monkeypatch.setattr(deskserve, "current_backend", lambda: type("B", (), {"model": "m"})())
    assert "停用" in desk.skill_run("s1")["error"]


# ---------------------------------------------------------------- 设置面板：MCP

def test_mcp_名字必须能当工具名前缀(desk):
    """名字会拼成 <name>__tool 发给模型；带空格/中文的名字模型调不出来。"""
    for bad in ("有 空格", "带#号", "", "中文名", "a" * 65):
        assert "error" in desk.mcp_save(bad, "echo", [], {}, "", "project")
    assert desk.mcp_save("ok-name_1", "echo", [], {}, "", "project").get("ok")


def test_mcp_增删读改写不动别人(desk):
    """读-改-写：加一条不能把同文件里已有的另一条冲掉。"""
    desk.mcp_save("a", "echo", ["1"], {}, "", "project")
    desk.mcp_save("b", "echo", ["2"], {}, "", "project")
    names = {sv["name"] for sv in desk.mcp_view()["servers"]}
    assert {"a", "b"} <= names
    desk.mcp_delete("a", "project")
    names = {sv["name"] for sv in desk.mcp_view()["servers"]}
    assert "a" not in names and "b" in names
    assert "error" in desk.mcp_delete("a", "project")     # 重复删不崩


def test_mcp_空命令被挡(desk):
    assert "error" in desk.mcp_save("x", "   ", [], {}, "", "project")


def test_mcp_重连忙时拒绝(desk):
    """重连要摘掉旧工具；工具表是发请求那一刻交给模型的，中途摘会让它调到不存在的名字。"""
    desk.busy = True
    try:
        assert "error" in desk.mcp_reload()
    finally:
        desk.busy = False


def test_mcp_重连摘干净旧工具(desk):
    """摘不干净 = 页面上 server 已经删了，模型手里还留着它的工具名。"""
    from mecode.tools import Tool
    fake = Tool(name="ghost__do", description="d", parameters={"type": "object", "properties": {}},
                handler=lambda a: "")
    desk.registry.register(fake)
    desk._mcp_tools = ["ghost__do"]
    assert "ghost__do" in {s["function"]["name"] for s in desk.registry.schemas()}
    desk.mcp_reload()
    assert "ghost__do" not in {s["function"]["name"] for s in desk.registry.schemas()}


# ---------------------------------------------------------------- 设置面板：system

def test_system_view_给的是真的_system_消息(desk):
    v = desk.system_view()
    assert v["chars"] == len(v["system"])
    assert v["system"] == desk.agent.messages[0]["content"]


def test_停机要关掉_mcp_子进程(desk):
    """MCP 客户端挂在 Desk 上（Desk 自己连的），不是 agent.mcp_clients。
    close() 读错地方 = 子进程全漏，长驻下来一堆僵尸；而且不会有任何报错。"""
    stopped = []

    class _C:
        name, tools = "fake", []

        def stop(self):
            stopped.append(1)

    desk.mcp_clients = [_C()]
    desk.close()
    assert stopped, "close() 没关掉 Desk 自己持有的 MCP 客户端"


def test_忙时不注入技能(desk, monkeypatch):
    """_pending_reminder 是下一轮开头注入的，排队的用户消息却被当前这一轮 drain 走——
    两者会落到不同轮，模型收到"请按技能执行"时手上还没有技能正文。所以忙时直接拒绝。"""
    class _S:
        name, description, source, enabled = "s1", "d", "内置", True
        path = Path("x/SKILL.md")

    monkeypatch.setattr(deskserve, "discover_skills", lambda *a, **k: [_S()])
    desk.busy = True
    try:
        r = desk.skill_run("s1")
    finally:
        desk.busy = False
    assert "error" in r and "还在跑" in r["error"]
    assert not desk.agent._pending_reminder, "拒绝了却把 reminder 留在了 agent 上"


def test_mcp_删除按它自己所在的文件(desk):
    """一律按 global 删的话，项目级 .mcp.json 里的那条永远删不掉，还会回"这个文件里没有 xxx"。"""
    desk.mcp_save("proj-only", "echo", [], {}, "", "project")
    sv = {x["name"]: x for x in desk.mcp_view()["servers"]}["proj-only"]
    assert sv["scope"] == "project", "没标出它来自项目配置，前端就会拿 global 去删"
    assert desk.mcp_delete("proj-only", sv["scope"]).get("ok")
    assert "proj-only" not in {x["name"] for x in desk.mcp_view()["servers"]}


def test_重存后端不抹掉已有的上限(desk, monkeypatch):
    """新增表单里没有"上限"那一项；重存一条已有后端时传 0 不等于"要清空"。"""
    written = {}
    monkeypatch.setattr(deskserve, "saved_configs", lambda: [
        {"base_url": "https://a.com", "model": "m1", "api_key": "k", "context_cap": 64000},
    ])
    monkeypatch.setattr(deskserve, "save_user_config",
                        lambda *a, **kw: written.update(kw, args=a))
    monkeypatch.setattr(desk, "_apply_backend", lambda: {"ok": True})
    desk.save_backend("https://a.com", "m1", "k2")
    assert written.get("context_cap") == 64000, "已有的上限被抹掉了"


# ---------------------------------------------------------------- 会话操作

def _seed(desk, n=2):
    """在临时 root 下造几个会话头，返回它们的 id（最近的在前）。"""
    ids = []
    for i in range(n):
        st = SessionStore(root=deskserve.agent_config.session_root, cwd=desk.cwd)
        st.write_header(title=f"会话{i}")
        st.append_transcript({"role": "user", "content": f"第{i}句"})
        ids.append(st.session_id)
    return ids


def test_改名无条件覆盖(desk):
    """write_header(title=) 只在没标题时才写（那是自动命名，不能盖掉用户改的名）。
    改名要的是无条件覆盖——两种语义塞一个参数，迟早有一边被写错。"""
    sid = _seed(desk, 1)[0]
    st = SessionStore(root=deskserve.agent_config.session_root, cwd=desk.cwd, session_id=sid)
    st.write_header(title="自动命名")                 # 已有"会话0"，写不进去
    assert desk.sessions()["sessions"][0]["title"] == "会话0"
    assert st.set_title("  用户改的  ") == "用户改的"   # 首尾空白去掉
    assert desk.rename_session(sid, "再改一次")["title"] == "再改一次"
    st.write_header(title="自动命名")                 # 改名之后自动命名仍然不该覆盖
    assert desk.sessions()["sessions"][0]["title"] == "再改一次"


def test_改名的校验(desk):
    sid = _seed(desk, 1)[0]
    assert "error" in desk.rename_session(sid, "   ")
    assert "error" in desk.rename_session("不存在的id", "x")
    assert len(desk.rename_session(sid, "长" * 200)["title"]) == 80


def test_归档只是收起来不是删除(desk):
    sid = _seed(desk, 1)[0]
    st = SessionStore(root=deskserve.agent_config.session_root, cwd=desk.cwd, session_id=sid)
    before = st.transcript_path.read_text(encoding="utf-8")
    assert desk.archive_session(sid, True)["ok"]
    assert not any(x["id"] == sid for x in desk.sessions()["sessions"])
    assert any(x["id"] == sid for x in desk.sessions(archived=True)["sessions"])
    assert desk.sessions()["archived_count"] == 1
    assert st.transcript_path.read_text(encoding="utf-8") == before, "归档动了 transcript"
    desk.archive_session(sid, False)
    assert any(x["id"] == sid for x in desk.sessions()["sessions"])


def test_不许归档当前会话(desk):
    """归档后它会从清单消失，而你还在里面聊——界面上就成了"标题栏有个会话、列表里找不到"。"""
    desk.agent.store.write_header(title="正在用的")
    r = desk.archive_session(desk.agent.store.session_id, True)
    assert "error" in r and "当前" in r["error"]
    # 取消归档不受此限（本来就不在清单里的才需要取消）
    assert desk.archive_session(desk.agent.store.session_id, False)["ok"]


def test_分叉复制历史且不动原会话(desk):
    sid = _seed(desk, 1)[0]
    src = SessionStore(root=deskserve.agent_config.session_root, cwd=desk.cwd, session_id=sid)
    new = src.fork()
    assert new.session_id != sid
    assert new.transcript_path.read_text(encoding="utf-8") == \
        src.transcript_path.read_text(encoding="utf-8")
    meta = {m["session_id"]: m for m in
            SessionStore.list_sessions(root=deskserve.agent_config.session_root, cwd=desk.cwd)}
    assert meta[new.session_id]["forked_from"] == sid
    assert "（分叉）" in meta[new.session_id]["title"]
    assert sid in meta, "原会话被动了"


def test_分叉要改写外置输出的路径(desk):
    """外置的大工具输出路径是复制前就写死在 transcript 里的绝对路径，指向源会话目录。
    不改写的话新会话里那些"看全文"全打不开——读取接口把路径限死在【本会话】目录内。"""
    sid = _seed(desk, 1)[0]
    src = SessionStore(root=deskserve.agent_config.session_root, cwd=desk.cwd, session_id=sid)
    blob = src.offload_tool_output("bash", "很长的输出" * 100)
    src.append_transcript({"role": "tool", "content": blob})
    assert sid in blob                                   # 路径里带源会话 id

    new = src.fork()
    text = new.transcript_path.read_text(encoding="utf-8")
    assert sid not in text, "还留着指向源会话的路径"
    assert new.session_id in text
    # 改写后的路径要真的指到一个存在的文件（整目录是复制过来的）
    moved = json.loads(text.splitlines()[-1])["content"]
    assert Path(moved).is_file(), f"路径改写了但文件没跟过来：{moved}"


def test_分叉忙时拒绝(desk):
    sid = _seed(desk, 1)[0]
    desk.busy = True
    try:
        assert "error" in desk.fork_session(sid)
    finally:
        desk.busy = False


def test_清单默认不含归档的(desk):
    a, b = _seed(desk, 2)
    desk.archive_session(a, True)
    normal = desk.sessions()
    assert {x["id"] for x in normal["sessions"]} == {b}
    assert normal["archived_count"] == 1
    assert {x["id"] for x in desk.sessions(archived=True)["sessions"]} == {a}


def test_历史分页首尾相接(desk):
    """一页页往前翻要【正好铺满】全量历史：不重不漏。

    前端靠 next_before 一路往前取，页与页之间错一条就会重复渲染或凭空少一段，
    而且是那种"翻上去看着眼熟但说不出哪不对"的错法。
    """
    st = desk.agent.store
    for i in range(100):
        st.append_transcript({"role": "user", "content": f"第{i}句"})

    seen, before, pages = [], 0, 0
    while True:
        r = desk.history(before=before, limit=40)
        pages += 1
        seen = [m["seq"] for m in r["items"]] + seen      # 越翻越早，接在前面
        assert r["total"] == 100
        if not r["next_before"]:
            break
        before = r["next_before"]
        assert pages < 10, "翻不完，next_before 没在推进"

    assert pages == 3, f"100 条按 40 一页应该是 3 页，实得 {pages}"
    assert seen == list(range(100)), "页与页之间有重叠或缺口"


def test_历史最后一页把_next_before_清零(desk):
    """不清零的话前端永远以为"还有更早的"，滚到顶就死循环请求。"""
    st = desk.agent.store
    for i in range(5):
        st.append_transcript({"role": "user", "content": str(i)})
    assert desk.history(before=0, limit=40)["next_before"] == 0


def test_空会话的历史不炸(desk):
    r = desk.history()
    assert r["items"] == [] and r["total"] == 0 and r["next_before"] == 0


# ---------------------------------------------------------------- 工作区

def test_换工作区要真的_chdir(desk, workdir):
    """工具是按【进程工作目录】解析相对路径的（bash 子进程直接继承，read_file 走
    Path(path).resolve()）。只换 store 的 cwd 而不 chdir，模型说"读 README.md"
    读的还是旧目录那个——而且不报错，只是内容对不上。"""
    import os
    other = workdir / "另一个工作区"
    other.mkdir()
    was = os.getcwd()
    try:
        r = desk.set_workspace(str(other))
        assert r.get("ok"), r
        assert Path(os.getcwd()).resolve() == other.resolve(), "进程工作目录没跟着换"
        assert desk.cwd.resolve() == other.resolve()
        assert desk.agent.store.cwd.resolve() == other.resolve(), "新会话没落在新工作区下"
    finally:
        os.chdir(was)


def test_换工作区的校验(desk, workdir):
    assert "error" in desk.set_workspace("")                    # 空路径 != 当前目录
    assert "error" in desk.set_workspace(str(workdir / "不存在"))
    f = workdir / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert "文件夹" in desk.set_workspace(str(f))["error"]      # 传文件
    desk.busy = True
    try:
        assert "error" in desk.set_workspace(str(workdir))
    finally:
        desk.busy = False


def test_工作区按文件夹分组(desk, workdir):
    """list_projects 横扫 projects/ 下全部，工作区路径从会话头里的 cwd 取——
    目录名是 slug（路径压平），压平有损，反推不回原路径。"""
    import os
    a, b = workdir / "wsA", workdir / "wsB"
    a.mkdir(); b.mkdir()
    was = os.getcwd()
    try:
        for d in (a, b):
            desk.set_workspace(str(d))
            desk.agent.store.write_header(title=f"在 {d.name} 里的会话")
        got = {w["name"]: w for w in desk.workspaces()["workspaces"]}
        assert {"wsA", "wsB"} <= set(got), list(got)
        assert got["wsB"]["current"], "当前工作区没标出来"
        assert desk.workspaces()["workspaces"][0]["current"], "当前工作区没排最前"
        assert got["wsA"]["cwd"] == str(a), got["wsA"]["cwd"]
    finally:
        os.chdir(was)


def test_浏览目录只给目录(desk, workdir):
    (workdir / "子目录").mkdir()
    (workdir / ".隐藏").mkdir()
    (workdir / "文件.txt").write_text("x", encoding="utf-8")
    r = desk.browse(str(workdir))
    names = {d["name"] for d in r["dirs"]}
    assert "子目录" in names
    assert "文件.txt" not in names, "列出了文件"
    assert ".隐藏" not in names, "列出了隐藏目录（.git/.venv 会把选择器塞满）"
    assert r["parent"], "没有上一级，选择器回不去"
    assert "error" in desk.browse(str(workdir / "不存在"))


# ---------------------------------------------------------------- 审批弹窗

def test_审批请求要说清总是允许什么(desk):
    """按钮上只写"总是允许"等于让用户盲签：落盘的可能是 bash(git:*)，
    一次点击把 git push --force / reset --hard 全放行，而弹窗上一个字没提。"""
    got = {}
    desk.bus.emit = lambda m: got.update(m) if m.get("type") == "ask_permission" else None
    t = threading.Thread(target=desk._ask_permission,
                         args=("bash", {"command": "git status"}, None), daemon=True)
    t.start()
    _wait_until(lambda: got.get("pattern"), bool)
    assert "git" in got["pattern"], got
    desk.pending.answer(got["id"], DENY)
    t.join(timeout=3)


def test_授权不了的调用不给总是允许(desk):
    """含元字符的 bash 生成不了有意义的规则（decide 压根不看 allow 表）。
    pattern 为空串是给前端的信号：别显示那个按钮，记不住的事别承诺。"""
    got = {}
    desk.bus.emit = lambda m: got.update(m) if m.get("type") == "ask_permission" else None
    t = threading.Thread(target=desk._ask_permission,
                         args=("bash", {"command": "echo hi > x.txt"}, None), daemon=True)
    t.start()
    _wait_until(lambda: "pattern" in got, bool)
    assert got["pattern"] == "", got
    desk.pending.answer(got["id"], DENY)
    t.join(timeout=3)


def test_配置里带上模型窗口(desk):
    """设置页要现算"上限 × 阈值 = 触发点"。窗口必须服务端给：
    前端只看得到 saved[].window，那个字段对没注册的本地模型是 0，
    而 config._context_limit 走的是 100K 兜底——两边算出来的触发点对不上。"""
    from mecode.config import effective_window
    view = desk.config_view()
    pf = view["prefs"]
    assert pf["window"] == effective_window(view["current"]["model"]), pf
    assert pf["window"] > 0
    # 服务端自己的有效上限就是 min(窗口, CAP)，前端照这个公式算才对得上
    assert pf["context_limit"] == min(pf["window"], pf["context_cap"] or 128_000)
