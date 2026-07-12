"""SessionStore 与 Agent 接入会话存储的行为。

固化的不变量：
- 布局：<root>/projects/<slug>/sessions/<uuid>/，slug=路径非字母数字→'-'
- transcript.jsonl 只追加；offload 落盘返回 posix 指针
- load_messages fold：无日志→None；无 marker→全部消息；有 marker→镜像+其后消息
- Agent 带 store：每条消息进 transcript；压缩后 fold 还原 == live messages[]（单一真相）
- 工具超长输出外置：超预算→存盘+尾部留指针；read 类（自带翻页）豁免
"""
import json
from dataclasses import replace
from pathlib import Path

from mecode.agent import Agent
from mecode.config import agent_config
from mecode.events import Done, TextDelta, ToolCall, Usage
from mecode.session import SessionStore, project_slug
from mecode.tools import Tool, ToolRegistry


# ---------- SessionStore 单测 ----------

def test_slug_非字母数字变横杠():
    assert project_slug("/a/实验/b-c") == project_slug("/a/实验/b-c")   # 稳定
    s = project_slug("/tmp/proj")
    assert all(c.isalnum() or c == "-" for c in s)                      # 只剩字母数字和'-'


def test_布局_root_projects_slug_sessions_uuid(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="sid-1")
    parts = st.dir.relative_to(tmp_path).parts
    assert parts[0] == "projects" and parts[-2] == "sessions" and parts[-1] == "sid-1"


def test_append_transcript_只追加(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.append_transcript({"role": "user", "content": "一"})
    st.append_transcript({"role": "assistant", "content": "二"})
    st.mark_compaction(context_tokens=999)
    lines = st.transcript_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x).get("content") for x in lines[:2]] == ["一", "二"]
    assert json.loads(lines[2])["type"] == "compaction"                 # 压缩只追加 marker，不重写前文


def test_load_messages_无日志返回None(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    assert st.load_messages() is None                                  # 新会话无 transcript


def test_load_messages_无marker返回全部消息(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    msgs = [{"role": "system", "content": "p"}, {"role": "user", "content": "你好"}]
    for m in msgs:
        st.append_transcript(m)
    assert st.load_messages() == msgs                                  # 没压缩 → 原样


def test_load_messages_有marker取镜像加其后(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.append_transcript({"role": "system", "content": "p"})           # marker 之前（被 fold 丢弃）
    st.append_transcript({"role": "user", "content": "老问题"})
    image = [{"role": "system", "content": "p"}, {"role": "user", "content": "<摘要>"}]
    st.mark_compaction(summary="摘要", image=image)                     # 压缩点
    st.append_transcript({"role": "user", "content": "新问题"})         # marker 之后（保留）
    assert st.load_messages() == image + [{"role": "user", "content": "新问题"}]


def test_read_transcript_messages_顺读全部无视marker(tmp_path):
    # 和 load_messages 相对：这是"人类视觉历史"读法，全量、跳过 marker、原序（给 UI 重渲滚动条）
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.append_transcript({"role": "user", "content": "一"})
    st.mark_compaction(image=[{"role": "system", "content": "x"}])
    st.append_transcript({"role": "assistant", "content": "二"})
    assert [m["content"] for m in st.read_transcript_messages()] == ["一", "二"]


def test_permissions_项目级持久化往返(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    assert st.load_permissions() == {}                                 # 初始空
    st.add_permission("bash", "allow", "rm:*")
    st.add_permission("bash", "deny", "sudo:*")
    st.add_permission("write_file", "allow", "*")
    assert st.permissions_path.parent == st.project_dir                # 落项目级、非会话级
    st2 = SessionStore(root=tmp_path, cwd=tmp_path, session_id="other")
    loaded = st2.load_permissions()                                    # 同项目另一会话能读到（共享）
    assert loaded == {"bash": {"allow": ["rm:*"], "deny": ["sudo:*"]},
                      "write_file": {"allow": ["*"]}}


def test_offload_tool_output_落盘返回指针(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    path = st.offload_tool_output("bash", "x" * 5000)
    assert "\\" not in path
    assert Path(path).read_text(encoding="utf-8") == "x" * 5000
    assert Path(path).parent == st.tool_outputs_dir


# ---------- Agent 接入 store 的集成 ----------

class _TextProvider:
    """只回一句正文（不调工具）。"""
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("回答")
        yield Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
        yield Done(reason="stop")


class _OneToolProvider:
    """第一次回一个工具调用，第二次回正文收尾。"""
    def __init__(self, tool_name):
        self.tool_name, self.calls = tool_name, 0

    def stream(self, messages, tools=None, should_stop=None):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(id="c1", name=self.tool_name, arguments={})
            yield Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("好了")
            yield Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
            yield Done(reason="stop")


def _reg(name, text):
    reg = ToolRegistry()
    reg.register(Tool(name=name, description="x",
                      parameters={"type": "object", "properties": {}},
                      handler=lambda a: text))
    return reg


def _store(tmp):
    return SessionStore(root=tmp, cwd=tmp, session_id="sess")


def test_一轮后_system不落盘_transcript只含对话(tmp_path):
    st = _store(tmp_path)
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="你是助手", store=st)
    list(a.run_turn("问题"))
    # system 是运行时配置、不入 transcript（resume 时换新鲜的）→ load_messages 给的是对话上下文，
    # 恰是 RAM 里去掉打头那条 system；未压缩故无 marker，原样还原。
    assert a.messages[0]["role"] == "system"                           # system 仍在 RAM
    assert st.load_messages() == a.messages[1:]
    contents = [json.loads(x).get("content")
                for x in st.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert "你是助手" not in contents                                   # system 不落盘
    assert "问题" in contents and "回答" in contents


def test_构造时不碰磁盘_首条消息才建文件夹(tmp_path):
    st = _store(tmp_path)
    Agent(_TextProvider(), _reg("noop", "x"), system_prompt="你是助手", store=st)
    assert not st.dir.exists()                                         # 懒创建：没发消息就没文件夹


def test_工具超长输出_非bash_内联头部_存头部之后(tmp_path):
    st = _store(tmp_path)
    big = "ABCDE" * 4000                                               # 20000 字，远超默认 6000 上限
    a = Agent(_OneToolProvider("bigtool"), _reg("bigtool", big),
              system_prompt="你是助手", store=st)
    list(a.run_turn("调用工具"))
    limit = a.config.tool_result_max_chars
    files = list(st.tool_outputs_dir.glob("*.txt"))
    # 列表型工具：内联【头部】，只存【头部之后】的部分（read_file 头 200 行不会重看已内联的头）
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == big[limit:]
    tool_msg = next(m for m in a.messages if m.get("role") == "tool")
    assert tool_msg["content"].startswith(big[:limit])                # 内联是头部
    assert "后续输出已存盘" in tool_msg["content"] and files[0].as_posix() in tool_msg["content"]


def test_bash超长输出_内联尾部_存全文(tmp_path):
    st = _store(tmp_path)
    big = "ABCDE" * 4000
    a = Agent(_OneToolProvider("bash"), _reg("bash", big), system_prompt="你是助手", store=st)
    list(a.run_turn("跑命令"))
    limit = a.config.tool_result_max_chars
    files = list(st.tool_outputs_dir.glob("*.txt"))
    # bash/日志：内联【尾部】最近输出，存【全文】（read_file 从头读即更早内容，和内联尾部不重复）
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == big
    tool_msg = next(m for m in a.messages if m.get("role") == "tool")
    assert tool_msg["content"].rstrip().endswith(big[-limit:])        # 内联是尾部
    assert "完整输出已存盘" in tool_msg["content"]


def test_read类工具_超长也不外置(tmp_path):
    st = _store(tmp_path)
    # read_file 在默认 rescue_read_tools 里 → 自带翻页，超长也不外置（避免绕圈）
    a = Agent(_OneToolProvider("read_file"), _reg("read_file", "L" * 20000),
              system_prompt="你是助手", store=st)
    list(a.run_turn("读文件"))
    assert not st.tool_outputs_dir.exists()                            # 没产生外置文件
    tool_msg = next(m for m in a.messages if m.get("role") == "tool")
    assert "完整输出已存盘" not in tool_msg["content"]


def test_压缩时_marker只带镜像_且fold还原等于live(tmp_path):
    st = _store(tmp_path)
    cfg = replace(agent_config, context_limit=10, compact_threshold=0.5)
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="你是助手",
              config=cfg, store=st)
    a.messages += [{"role": "user", "content": "老问题"},
                   {"role": "assistant", "content": "老回答"}]
    a.context_tokens = 9999                                           # 触发压缩
    list(a.run_turn("新问题"))
    lines = [json.loads(x) for x in st.transcript_path.read_text(encoding="utf-8").splitlines()]
    marker = next(x for x in lines if x.get("type") == "compaction")
    assert set(marker.keys()) == {"type", "ts", "image"}              # 压缩记录只含这三项（摘要嵌在 image 里）
    banner = next(m["content"] for m in a.messages if "<system-reminder>" in m.get("content", ""))
    assert st.transcript_path.as_posix() in banner                    # 续接 banner 指针指 transcript
    assert st.load_messages() == a.messages                           # ★fold 还原 == 工作记忆（单一真相闭环）


# ---------- 会话头 + resume ----------

def test_write_header_title只设一次_updated刷新(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.write_header(title="原标题")
    m1 = json.loads(st.session_json.read_text(encoding="utf-8"))
    st.write_header(title="新标题")
    m2 = json.loads(st.session_json.read_text(encoding="utf-8"))
    assert m2["title"] == "原标题"                                     # 标题只设一次（首条用户消息）
    assert m2["updated_at"] >= m1["updated_at"]                        # 时间每次刷新
    assert m2["session_id"] == "s" and "cwd" in m2 and "created_at" in m2


def test_list_sessions按updated倒序(tmp_path):
    for sid, t in [("s1", 100.0), ("s2", 300.0), ("s3", 200.0)]:
        st = SessionStore(root=tmp_path, cwd=tmp_path, session_id=sid)
        st.write_header(title=sid)
        meta = json.loads(st.session_json.read_text(encoding="utf-8"))
        meta["updated_at"] = t                                        # 钉死时间，不依赖真实时钟（同毫秒会并列）
        st.session_json.write_text(json.dumps(meta), encoding="utf-8")
    ids = [m["session_id"] for m in SessionStore.list_sessions(root=tmp_path, cwd=tmp_path)]
    assert ids == ["s2", "s3", "s1"]                                  # 300 > 200 > 100，最近在前


def test_list_sessions_无会话目录返回空(tmp_path):
    assert SessionStore.list_sessions(root=tmp_path, cwd=tmp_path) == []


def test_write_header_持久化mode与上下文用量_list可读回(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.write_header(title="t", mode="yolo", context_tokens=1234)     # 模式 + 上下文用量随会话头落盘
    m = json.loads(st.session_json.read_text(encoding="utf-8"))
    assert m["mode"] == "yolo" and m["context_tokens"] == 1234
    meta = SessionStore.list_sessions(root=tmp_path, cwd=tmp_path)[0]
    assert meta["mode"] == "yolo" and meta["context_tokens"] == 1234  # resume 从这里恢复模式 + 立即显示用量
    st.write_header(mode="normal", context_tokens=5678)             # 再覆盖：title 不动
    m = json.loads(st.session_json.read_text(encoding="utf-8"))
    assert m["mode"] == "normal" and m["context_tokens"] == 5678 and m["title"] == "t"


def test_write_header_持久化思考态_分开调用不互相清(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    st.write_header(title="t", mode="normal")                        # 先落 mode
    st.write_header(thinking_on=False, effort="high")               # 再单独落思考态
    m = json.loads(st.session_json.read_text(encoding="utf-8"))
    assert m["thinking_on"] is False and m["effort"] == "high"      # 思考态存下
    assert m["mode"] == "normal" and m["title"] == "t"             # 只覆盖给的字段，mode/title 不被清


def test_resume_丢弃旧system换新鲜():
    # resume_messages 打头那条旧 system 被丢，换成当前启动的新鲜 system；对话上下文原样接上
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="新SYS",
              resume_messages=[{"role": "system", "content": "旧SYS"},
                               {"role": "user", "content": "历史问"}])
    assert a.messages == [{"role": "system", "content": "新SYS"},
                          {"role": "user", "content": "历史问"}]


def test_前台subagent批_并发跑_墙钟约等于最慢而非求和():
    import time as _t
    from mecode.events import ToolCall
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    orig = a.tools.execute
    def slow(name, arguments, slot=None, bg=None):        # 每个 subagent"跑"0.3s
        if name == "subagent":
            _t.sleep(0.3)
            return f"done:{arguments.get('prompt')}"
        return orig(name, arguments, slot, bg)
    a.tools.execute = slow
    subs = [ToolCall(id=f"c{i}", name="subagent", arguments={"prompt": f"t{i}"}) for i in range(3)]
    t0 = _t.perf_counter()
    evs = list(a._exec_subagents_parallel(subs))
    dt = _t.perf_counter() - t0
    assert dt < 0.6, f"3 个各 0.3s 应≈0.3s(并发)、不是 0.9s(求和)；实际 {dt:.2f}s"
    results = [e.result for e in evs if e.__class__.__name__ == "ToolResult"]
    assert len(results) == 3 and all(r.startswith("done:") for r in results)   # 3 个结果都收齐


def test_并发子agent_事件带tool_call_id_可配对():
    # 修 TUI 串位 bug 的底座：ToolStarted/ToolResult 都要带上 tool_call_id，UI 才能把每个结果
    # 配回它自己的块（并发时结果按完成序乱序回来，只靠"当前工具"单指针会全塞给最后一个块）。
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    orig = a.tools.execute
    def fake(name, arguments, slot=None, bg=None):
        return f"done:{arguments.get('prompt')}" if name == "subagent" else orig(name, arguments, slot, bg)
    a.tools.execute = fake
    subs = [ToolCall(id=f"c{i}", name="subagent", arguments={"prompt": f"t{i}"}) for i in range(3)]
    evs = list(a._exec_subagents_parallel(subs))
    started = {e.id for e in evs if e.__class__.__name__ == "ToolStarted"}
    results = {e.id for e in evs if e.__class__.__name__ == "ToolResult"}
    assert started == {"c0", "c1", "c2"}          # 每个子 agent 各一个 ToolStarted、带自己的 id
    assert results == {"c0", "c1", "c2"}           # 每个结果也带 id → UI 一一配回、不串位


def test_subagent_runner_返回最终assistant文本为总结():
    from mecode.subagent import SubagentRunner
    r = SubagentRunner(_TextProvider(), agent_config)
    assert r.run("做点事") == "回答"                   # 子 agent 最后一段 assistant 文本 = 总结


def test_子agent_被中断且无总结_回中断文案():
    import threading
    from mecode.subagent import SubagentRunner, _INTERRUPTED
    ev = threading.Event()

    class _SilentThenInterrupt:
        # 不产出正文；模拟跑到一半被 Ctrl+C（run_turn 开头会 clear 打断标志，故在 stream 里再 set）
        def stream(self, messages, tools=None, should_stop=None):
            ev.set()
            yield Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            yield Done(reason="stop")

    r = SubagentRunner(_SilentThenInterrupt(), agent_config, interrupt=ev)
    assert r.run("干活") == _INTERRUPTED           # 被中断 + 没产出总结 → 中断文案（非“未产出总结”）


def test_subagent工具_空prompt报错_否则回总结():
    from mecode.subagent import SubagentRunner, subagent_tools
    h = {t.name: t.handler for t in subagent_tools(SubagentRunner(_TextProvider(), agent_config))}
    assert h["subagent"]({"prompt": "  "}).startswith("错误")
    assert h["subagent"]({"prompt": "干活"}) == "回答"


def test_后台子agent_start返回id_完成入drain回总结():
    import time as _t
    from mecode.background import BackgroundManager
    from mecode.subagent import SubagentRunner
    notified = []
    bg = BackgroundManager(on_complete=lambda t: notified.append(t))
    tid = bg.start_subagent("做点事", SubagentRunner(_TextProvider(), agent_config))
    assert tid == 1                                    # 立即拿到编号（不阻塞）
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < 8 and not notified:
        _t.sleep(0.02)
    assert notified and notified[0].id == tid          # 自然完成 → 通知
    done = bg.drain_completions()
    assert len(done) == 1 and done[0].is_subagent and done[0].output == "回答"   # 总结入完成队列
    assert bg.running() == []                          # 完成后从在跑表移除


def test_subagent工具_background转后台_返回编号不阻塞():
    from mecode.background import BackgroundManager
    from mecode.subagent import SubagentRunner, subagent_tools
    bg = BackgroundManager()
    h = {t.name: t.handler for t in subagent_tools(SubagentRunner(_TextProvider(), agent_config))}
    out = h["subagent"]({"prompt": "独立活", "background": True}, None, bg)   # takes_slot → (args, slot, bg)
    assert "后台" in out and "#1" in out               # 立即返回编号
    bg.kill_all()


def test_subagent工具_description必填_当短标签():
    from mecode.subagent import SubagentRunner, subagent_tools
    schema = subagent_tools(SubagentRunner(_TextProvider(), agent_config))[0].parameters
    assert "description" in schema["properties"]
    assert schema["required"] == ["description", "prompt"]   # 二者必填、description 在前（CC 同款）


def test_后台子agent_description透传task_完成文案用短标签():
    import time as _t
    from mecode.background import BackgroundManager
    from mecode.subagent import SubagentRunner
    bg = BackgroundManager()
    bg.start_subagent("完整的一大段任务描述 prompt", SubagentRunner(_TextProvider(), agent_config),
                      description="探索认证模块")
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < 8 and bg.running():
        _t.sleep(0.02)
    done = bg.drain_completions()[0]
    assert done.description == "探索认证模块"                 # 短标签存上 task（command 仍存完整 prompt）
    assert done.command == "完整的一大段任务描述 prompt"
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s", config=agent_config)
    txt = a._bg_done_text(done)
    assert "任务: 探索认证模块" in txt                         # 完成通知用短标签当"任务"（不贴整段 prompt）
    assert "总结:" in txt and "回答" in txt


def test_子agent_禁后台bash_不起后台任务_显式拒绝():
    from mecode.tools import default_registry
    a = Agent(_TextProvider(), default_registry(), system_prompt="s", subagent=True)
    assert a._is_subagent is True
    tc = ToolCall(id="c1", name="bash", arguments={"command": "echo hi", "background": True})
    results = [e.result for e in a._exec_tool(tc) if e.__class__.__name__ == "ToolResult"]
    assert results and "不支持后台" in results[0]     # 后台 bash 被显式拒绝（让它前台跑）
    assert a._bg.running() == []                       # bg.start 从未被调 → 子 agent 结束/被杀都无僵尸泄漏


def test_子agent模式_不注册subagent及bg及task工具_防递归():
    reg = _reg("noop", "x")                             # 起手只有 noop
    Agent(_TextProvider(), reg, system_prompt="s", subagent=True)
    names = {t["function"]["name"] for t in reg.schemas()}
    assert "subagent" not in names                     # 防无限递归
    assert "kill_bgtask" not in names                  # 不给后台
    assert "task_create" not in names                  # 不给任务清单
    assert "noop" in names                             # 原有的还在


def test_主agent模式_注册subagent及bg及task工具():
    reg = _reg("noop", "x")
    Agent(_TextProvider(), reg, system_prompt="s")
    names = {t["function"]["name"] for t in reg.schemas()}
    assert {"subagent", "kill_bgtask", "task_create"} <= names


def test_kill_running_procs_连带杀在跑的前台子agent的bash():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    killed = []

    class _FakeSlot:
        def kill(self):
            killed.append(1)

    a._subagent._active_slots.add(_FakeSlot())     # 模拟一个在跑的前台子 agent 的 proc_slot
    a._kill_running_procs()                         # 杀逻辑：主 bash + 在跑前台子 agent 的 bash
    assert killed == [1]                            # 连带树杀了前台子 agent 正跑的 bash


def test_request_interrupt_立即置标志_杀进程放后台():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    a.request_interrupt()
    assert a._interrupt.is_set()                    # 标志【立即】置位（杀进程在后台线程做，不卡调用方/UI）


def test_前台子agent_run跑完注销proc_slot():
    from mecode.subagent import SubagentRunner
    r = SubagentRunner(_TextProvider(), agent_config)
    assert r.run("做点事") == "回答"
    assert r._active_slots == set()                # 跑完（finally）注销，不残留


def test_注入共享打断标志_子agent复用主的():
    import threading
    ev = threading.Event()
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s", interrupt=ev)
    assert a._interrupt is ev                          # 用注入的、不自建 → Ctrl+C 可连带停子 agent


def test_运行时排队用户消息_下轮顶部注入为user消息():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    a.queue_user("追加需求A")
    assert a.has_pending_user()
    a._inject_bg_events()                              # 模拟下一轮顶部 drain
    assert not a.has_pending_user()                    # drain 后清空
    assert {"role": "user", "content": "追加需求A"} in a.messages   # 作为真·user 消息进上下文


def test_run_bg_turn_有排队用户消息也起轮_并注入():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    a.queue_user("你在干嘛")
    list(a.run_bg_turn())                              # 有排队消息 → 不空操作，跑一轮
    assert {"role": "user", "content": "你在干嘛"} in a.messages
    assert not a.has_pending_user()


def test_run_bg_turn_无任何pending_空操作():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="s")
    assert list(a.run_bg_turn()) == []                 # 无 bg、无排队 → 不起空轮
    assert a.messages == [{"role": "system", "content": "s"}]


def test_resume_历史起过后台任务_系统提示词后注入幻影提示():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="SYS",
              resume_messages=[{"role": "user", "content": "起个后台任务"},
                               {"role": "assistant", "content": "已在后台启动 · 任务 #1 · 运行中，我先做别的"}])
    assert a.messages[0] == {"role": "system", "content": "SYS"}                  # 新鲜 system
    assert a.messages[1]["role"] == "user" and "没有运行中的后台任务" in a.messages[1]["content"]  # 紧跟其后的提示
    assert "已在后台启动" in a.messages[3]["content"]                              # 旧历史在提示之后


def test_resume_历史无后台任务_不注入幻影提示():
    a = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="SYS",
              resume_messages=[{"role": "user", "content": "普通历史"}])
    assert a.messages == [{"role": "system", "content": "SYS"},                   # 没用过后台 → 不加这条
                          {"role": "user", "content": "普通历史"}]


def test_resume_续写同一transcript_且新鲜system不落盘(tmp_path):
    st = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s1")
    a1 = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="老SYS", store=st)
    list(a1.run_turn("问题1"))
    msgs = st.load_messages()                                         # [问题1, 回答]（system 不落盘）
    # 用同一 store 续会话：换新鲜 system，灌入历史对话
    a2 = Agent(_TextProvider(), _reg("noop", "x"), system_prompt="新SYS",
               store=st, resume_messages=msgs)
    assert a2.messages[0] == {"role": "system", "content": "新SYS"}   # 新鲜 system 打头
    assert "问题1" in [m.get("content") for m in a2.messages]         # 历史对话接上
    list(a2.run_turn("问题2"))
    contents = [json.loads(x).get("content")
                for x in st.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert contents == ["问题1", "回答", "问题2", "回答"]              # 续写同一 transcript，无 system、不重复
