"""Agent.run_turn 的压缩相关行为（用假 provider，不联网）。

固化的不变量：
- 压缩发生在 append 之前：当前问题不会被卷进摘要
- 未超阈值不压缩：旧历史原样、无 reminder 注入
（会话存储/单一真相/工具外置见 test_session.py）
"""
from dataclasses import replace
from pathlib import Path

import pytest

from mecode.agent import Agent
from mecode.config import agent_config
from mecode.events import Done, Notice, ReasoningDelta, TextDelta, ToolCall, ToolResult, Usage
from mecode.registry import INERT, KIMI_FORCED, ThinkingProfile
from mecode.tools import Tool, ToolRegistry, default_registry


class _FakeProvider:
    """最小假 provider：回一句正文 + usage + done（摘要和正式回复都走它）。"""
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("回答")
        yield Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
        yield Done(reason="stop")


def _agent(tmp, **cfg_over):
    cfg = replace(agent_config, **cfg_over)
    return Agent(_FakeProvider(), default_registry(), system_prompt="你是助手", config=cfg)


def test_压缩发生在append之前_当前问题不被摘要(tmp_path):
    a = _agent(tmp_path, context_limit=10, compact_threshold=0.5)
    a.messages += [
        {"role": "user", "content": "老问题"},
        {"role": "assistant", "content": "老回答"},
    ]
    a.context_tokens = 9999                       # 远超阈值 → 触发压缩
    list(a.run_turn("全新问题"))
    # 当前问题作为裸 user 存在，且没被任何 reminder 包住（= 没被压进摘要）
    assert any(m.get("content") == "全新问题" and m["role"] == "user" for m in a.messages)
    assert all("全新问题" not in m.get("content", "")
               for m in a.messages if "<system-reminder>" in m.get("content", ""))


def test_未超阈值不压缩(tmp_path):
    a = _agent(tmp_path, context_limit=8000, compact_threshold=0.7)
    a.messages += [
        {"role": "user", "content": "老问题"},
        {"role": "assistant", "content": "老回答"},
    ]
    a.context_tokens = 100                        # 远低于阈值
    list(a.run_turn("新问题"))
    assert any(m.get("content") == "老问题" for m in a.messages)        # 原样还在
    assert not any("<system-reminder>" in m.get("content", "") for m in a.messages)


def test_后端不回usage_本地估算兜底触发压缩(tmp_path):
    # context_tokens 始终 0（模拟后端从不回 usage）——修复前压缩永不触发
    a = _agent(tmp_path, context_limit=100, compact_threshold=0.5)
    a.messages += [
        {"role": "user", "content": "旧问题" * 100},                    # 300 个中文字 ≈300 token > 50
        {"role": "assistant", "content": "旧回答"},
    ]
    events = list(a.run_turn("新问题"))
    assert any(isinstance(e, Notice) and "已压缩" in e.text and "约" in e.text
               for e in events)                    # 触发压缩，且提示标明是估算值（"约"）
    assert not any(m.get("content") == "旧问题" * 100 for m in a.messages)   # 旧历史进了摘要


def test_有usage时不走估算(tmp_path):
    # 同样的大历史，但后端回过精确 usage（100 token，低于阈值）→ 以精确值为准、不压缩
    a = _agent(tmp_path, context_limit=8000, compact_threshold=0.7)
    a.messages += [{"role": "user", "content": "旧问题" * 2000}]        # 估算会远超阈值
    a.context_tokens = 100
    list(a.run_turn("新问题"))
    assert any(m.get("content") == "旧问题" * 2000 for m in a.messages)  # 没被压


# ---- Ctrl-C 打断回滚 ----

class _InterruptProvider:
    """流到一半抛 KeyboardInterrupt，模拟用户 Ctrl-C。"""
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("思考")
        raise KeyboardInterrupt


class _SlowProvider:
    """连续 yield，给 close() 测试一个中途挂起点。"""
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("a")
        yield TextDelta("b")
        yield Done(reason="stop")


def _agent_with(provider, tmp):
    return Agent(provider, default_registry(), system_prompt="你是助手")


def test_流式打断_保留user_需独立标记(tmp_path):
    # 流式被打断（无工具）：user 保留（不回滚）、置 pending（下轮需单独插标记）、产出 Notice
    a = _agent_with(_InterruptProvider(), tmp_path)
    evs = list(a.run_turn("我的问题"))
    assert any(m.get("content") == "我的问题" for m in a.messages)      # 保留，不回滚
    assert a._pending_interrupt_marker is True
    assert any(isinstance(e, Notice) and "打断" in e.text for e in evs)


def test_close也置pending并保留(tmp_path):
    a = _agent_with(_SlowProvider(), tmp_path)
    gen = a.run_turn("我的问题")
    next(gen)                                            # user 已 append、循环挂起
    gen.close()                                          # 模拟 render 中途被打断
    assert any(m.get("content") == "我的问题" for m in a.messages)      # 保留
    assert a._pending_interrupt_marker is True


def test_fill补齐缺失工具结果_返回True(tmp_path):
    a = _agent_with(_SlowProvider(), tmp_path)
    a.messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果1"},      # 只有 c1 有结果
    ]
    assert a._fill_interrupted_tool_results() is True                  # 补过 → 标记已在上下文
    answered = {m["tool_call_id"] for m in a.messages if m.get("role") == "tool"}
    assert answered == {"c1", "c2"}                                    # c2 被补上 → 无孤儿
    assert any(m.get("tool_call_id") == "c2" and "interrupted" in m["content"].lower()
               for m in a.messages)


def test_fill无孤儿_返回False(tmp_path):
    # 流式被打断：末尾是 user，无 assistant tool_calls → 不补、返回 False
    a = _agent_with(_SlowProvider(), tmp_path)
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    assert a._fill_interrupted_tool_results() is False


def test_heal_orphans_孤儿后跟user也补_就地插在assistant后(tmp_path):
    # resume 场景：硬杀留下的孤儿后面常还跟着 user 消息（_fill 从尾扫到 user 就停、处理不了），
    # _heal_orphans 全量扫、按 id 配对，把缺的结果【就地】插在 assistant 之后、user 之前。
    a = _agent_with(_SlowProvider(), tmp_path)
    a.messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "user", "content": "下一条"},          # 孤儿：c1 无结果，后面直接是 user
    ]
    a._heal_orphans()
    roles = [m["role"] for m in a.messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]   # tool 就地插在 assistant 后
    assert a.messages[3]["tool_call_id"] == "c1"
    assert "interrupted" in a.messages[3]["content"].lower()


def test_heal_orphans_不碰已答且幂等(tmp_path):
    # 已有结果的 tool_call 不重复补；"运行中"这类【有结果】的也不碰；重复调用条数不变（幂等）。
    a = _agent_with(_SlowProvider(), tmp_path)
    a.messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "已在后台启动 · 运行中"},   # c1 有结果（含"运行中"）
    ]
    a._heal_orphans()
    answered = {m["tool_call_id"] for m in a.messages if m.get("role") == "tool"}
    assert answered == {"c1", "c2"}                       # 只补缺的 c2
    assert any(m.get("tool_call_id") == "c1" and m["content"] == "已在后台启动 · 运行中"
               for m in a.messages)                       # c1 原结果原样保留、没被覆盖
    n = len(a.messages)
    a._heal_orphans()
    assert len(a.messages) == n                           # 再跑一次不重复补


def test_下一轮_pending时插独立marker(tmp_path):
    a = _agent_with(_SlowProvider(), tmp_path)
    a._pending_interrupt_marker = True
    list(a.run_turn("新指令"))
    contents = [m["content"] for m in a.messages if m.get("role") == "user"]
    assert "[Request interrupted by user]" in contents                # 单独一条
    assert "新指令" in contents                                       # 新输入是另一条
    assert a._pending_interrupt_marker is False                       # 已清


def test_下一轮_无pending不插marker(tmp_path):
    a = _agent_with(_SlowProvider(), tmp_path)
    a._pending_interrupt_marker = False                                # 工具打断那种：标记已在 tool 结果里
    list(a.run_turn("新指令"))
    contents = [m["content"] for m in a.messages if m.get("role") == "user"]
    assert "[Request interrupted by user]" not in contents            # 不重复插
    assert "新指令" in contents


# ---- 思考存储：无条件存（"存全、发时过滤"——该不该发给当前模型由 provider._for_wire 决定）----

class _ReasoningProvider:
    """回一段思考 + 正文。"""
    def __init__(self, profile):
        self.profile = profile

    def stream(self, messages, tools=None, should_stop=None):
        yield ReasoningDelta("想一想")
        yield TextDelta("答案")
        yield Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12)
        yield Done(reason="stop")


def test_思考无条件存_与档案无关(tmp_path):
    """transcript 是完整事实：INERT（不发思考给模型）也照存——切到别的模型时 CoT 不丢。"""
    for profile in (KIMI_FORCED, INERT, ThinkingProfile(keep_reasoning="tool_calls")):
        a = Agent(_ReasoningProvider(profile), default_registry(), system_prompt="你是助手")
        list(a.run_turn("问"))
        asst = [m for m in a.messages if m["role"] == "assistant"][-1]
        assert asst.get("reasoning_content") == "想一想"


class _ReasoningToolProvider:
    """两轮：round1 思考+调工具，round2 思考+正文收尾。"""
    def __init__(self, profile):
        self.profile = profile
        self.calls = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.calls += 1
        if self.calls == 1:
            yield ReasoningDelta("要调工具了")
            yield ToolCall(id="c1", name="noop", arguments={})
            yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
            yield Done(reason="tool_calls")
        else:
            yield ReasoningDelta("收尾思考")
            yield TextDelta("完成")
            yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
            yield Done(reason="stop")


def _noop_reg():
    reg = ToolRegistry()
    reg.register(Tool(name="noop", description="x",
                      parameters={"type": "object", "properties": {}},
                      handler=lambda a: "ok"))
    return reg


def test_工具回合与纯答案回合的思考都存(tmp_path):
    a = Agent(_ReasoningToolProvider(ThinkingProfile(keep_reasoning="tool_calls")),
              _noop_reg(), system_prompt="你是助手")          # 无 policy → 全放，noop 直跑
    list(a.run_turn("问"))
    asst = [m for m in a.messages if m["role"] == "assistant"]
    assert asst[0].get("reasoning_content") == "要调工具了"       # 工具回合
    assert asst[-1].get("reasoning_content") == "收尾思考"        # 纯答案回合也存（老架构在这里丢）


# ---- 协作式打断标志（TUI 用） ----

class _SelfInterruptProvider:
    """流到一半把 agent 的打断标志置位（模拟 UI 线程调 request_interrupt）。"""
    def __init__(self):
        self.agent = None

    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("部分回答")
        self.agent.request_interrupt()                                 # 置位（流式期间被打断）
        yield Done(reason="stop")


def test_request_interrupt_流式中打断(tmp_path):
    p = _SelfInterruptProvider()
    a = _agent_with(p, tmp_path)
    p.agent = a
    evs = list(a.run_turn("问题"))
    assert a._pending_interrupt_marker is True                         # 流式打断 → 需独立标记
    assert any(isinstance(e, Notice) and "打断" in e.text for e in evs)
    assert a.messages[-1]["content"] == "问题"                         # assistant 未 append，末尾仍是 user


def test_每轮开头清打断标志(tmp_path):
    a = _agent_with(_SlowProvider(), tmp_path)
    a._interrupt.set()                                                 # 模拟上一轮残留
    list(a.run_turn("新问题"))                                         # _SlowProvider 不打断 → 正常跑完
    assert any(m.get("content") == "新问题" for m in a.messages)        # 没被残留标志误伤
    assert not a._interrupt.is_set()                                   # 开头已清


# ---- Step 3：后台任务完成 → 注入 user+<system-reminder> + run_bg_turn 自动接续 ----

def test_后台完成被注入为user_reminder(tmp_path):
    from mecode.background import BackgroundTask
    a = _agent(tmp_path)
    a._bg._done.append(BackgroundTask(id=3, command="npm test", proc=None,
                                      status="done", output="3 passed", exit_code=0))
    list(a.run_turn("继续"))
    inj = [m for m in a.messages if m["role"] == "user" and "后台任务 #3 完成" in m.get("content", "")]
    assert inj and "<system-reminder>" in inj[0]["content"]            # 身份=user+reminder
    assert "npm test" in inj[0]["content"] and "3 passed" in inj[0]["content"]  # 带命令+输出


def test_run_bg_turn_有完成则跑_无则空操作(tmp_path):
    from mecode.background import BackgroundTask
    a = _agent(tmp_path)
    assert list(a.run_bg_turn()) == []                                # 无待处理完成 → 空操作
    assert not any(m["role"] == "assistant" for m in a.messages)
    a._bg._done.append(BackgroundTask(id=1, command="sleep 10", proc=None,
                                      status="timeout", output="", exit_code=None))
    events = list(a.run_bg_turn())                                     # 有完成 → 注入 + 模型回应
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(m["role"] == "user" and "超时被终止" in m.get("content", "") for m in a.messages)


def test_后台checkin被注入为还在运行(tmp_path):
    import time
    from mecode.background import BackgroundTask
    a = _agent(tmp_path)
    task = BackgroundTask(id=5, command="npm run dev", proc=None, status="running",
                          started_at=time.monotonic() - 10, log_path="",
                          next_checkin_at=time.monotonic() - 1)        # 已到点 check-in
    a._bg._tasks[5] = task
    list(a.run_turn("继续"))
    inj = [m for m in a.messages if m["role"] == "user" and "后台任务 #5 还在运行" in m.get("content", "")]
    assert inj and "npm run dev" in inj[0]["content"]
    assert "wait_bgtask(5" in inj[0]["content"] and "kill_bgtask(5)" in inj[0]["content"]


def test_clip_offload_头尾两模式(tmp_path):
    class FakeStore:
        def offload_tool_output(self, name, content):
            self.last = content
            return f"/fake/{name}.txt"
    a = _agent(tmp_path)
    a.store = FakeStore()
    full = "HEAD" + "Z" * 100 + "TAIL"
    r = a._clip_offload("bash", full, 10, tail_mode=True)          # bash：内联尾部 + 存全文
    assert r.endswith(full[-10:]) and "完整输出已存盘" in r and a.store.last == full
    r2 = a._clip_offload("grep", full, 10, tail_mode=False)        # grep：内联头部 + 存"头部之后"
    assert r2.startswith(full[:10]) and "后续输出已存盘" in r2 and a.store.last == full[10:]


def test_switch_backend_历史不被改写_切回CoT无损(tmp_path):
    """热切后端【不清洗历史】（"存全、发时过滤"）：live messages 原样保留，
    该不该发给新模型由 provider._for_wire 在发送时决定。切走再切回，CoT 完整。"""
    a = _agent(tmp_path)
    history = [
        {"role": "assistant", "content": "答A", "reasoning_content": "想A"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}], "reasoning_content": "想B"},
    ]
    a.messages.extend(history)

    class _P:   # 假 provider：只带 profile / backend（switch_backend 只碰这两个）
        def __init__(self, profile):
            self.profile = profile
            self.backend = None

    a.switch_backend(_P(ThinkingProfile(keep_reasoning="tool_calls")))   # 切 DeepSeek 类
    a.switch_backend(_P(INERT))                                          # 再切完全不认思考的
    assert a.messages[-2]["reasoning_content"] == "想A"                  # 全程无损
    assert a.messages[-1]["reasoning_content"] == "想B"


def test_工具结果先记进上下文再发显示事件(tmp_path):
    """`yield ToolResult` 是生成器交出控制权的点：消费方在那里 close 掉生成器（chat.py 的 Ctrl+C
    处理就是 gen.close()），后面的 _record 就永远不执行 → 这个 tool_call 变成孤儿 → 被无条件补上
    [Request interrupted by user]，**而工具其实已经执行成功、文件真改了**，模型据此会再改一遍。
    先记后发之后 execute→_record 之间没有 yield，关不掉、插不进。七个出口都要守这个顺序。"""
    import re

    class _CallOnce:
        def __init__(self):
            self.n = 0

        def stream(self, messages, tools=None, should_stop=None):
            self.n += 1
            if self.n == 1:
                yield ToolCall(id="c1", name="写点东西", arguments={})
            else:
                yield TextDelta("好了")
            yield Done(reason="tool_calls" if self.n == 1 else "stop")

    副作用 = []
    reg = ToolRegistry()
    reg.register(Tool(name="写点东西", description="", parameters={"type": "object", "properties": {}},
                      handler=lambda a: 副作用.append(1) or "已写盘"))
    a = Agent(_CallOnce(), reg, system_prompt="x")
    gen = a.run_turn("改个文件")
    for ev in gen:
        if isinstance(ev, ToolResult):
            gen.close()                       # 模拟"结果刚显示出来就被 Ctrl+C"
            break
    assert 副作用 == [1]                       # 工具确实执行了（文件真改了）
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" and "已写盘" in m["content"]
               for m in a.messages), "结果没进上下文 → 下轮会被补成[被打断]，模型会重复执行"

    # 七个出口都得是这个顺序（新增分支照抄反了不会有测试变红，故在源码层守）
    src = (Path(__file__).resolve().parent.parent / "src" / "mecode" / "agent.py").read_text("utf-8")
    assert not re.search(r'yield ToolResult\(tc\.name, result, tc\.id\)\n\s*self\._record\(', src), \
        "有出口写成了先 yield 后 _record"


def test_撞上限要往对话里留标记_不只是Notice():
    """Notice 只给 UI 看、不进 messages。不留标记的话历史停在"调了一堆工具拿到结果然后没下文"，
    模型下一轮读不出自己是被强制停的 → 要么当已完成、要么从头重做。其余中断出口都记了。"""
    class _永远调工具:
        def stream(self, messages, tools=None, should_stop=None):
            yield ToolCall(id="c", name="空转", arguments={})
            yield Done(reason="tool_calls")

    reg = ToolRegistry()
    reg.register(Tool(name="空转", description="", parameters={"type": "object", "properties": {}},
                      handler=lambda a: "ok"))
    a = Agent(_永远调工具(), reg, system_prompt="x", config=replace(agent_config, max_iterations=3))
    evs = list(a.run_turn("干活"))
    assert any(isinstance(e, Notice) and "强制停止" in e.text for e in evs)     # UI 看得到
    assert any(m["role"] == "user" and "最大工具调用次数" in m.get("content", "")
               for m in a.messages), "对话里没留标记 → 模型不知道自己被砍了"


def test_流中途断_已产出的正文要进对话历史():
    """流式是边收边显示的：断的时候那半句【已经在用户屏幕上了】，而 _record 在流循环之后 →
    异常一抛就跳过，模型下次看不到自己说过什么，用户说"继续"它会从头再说一遍（屏幕上内容重复）。
    与 A3 同一个家族：副作用已发生、记录没跟上。重试不做——流式重来会把已产出 token 重复一遍。"""
    class _流断:
        def stream(self, messages, tools=None, should_stop=None):
            yield ReasoningDelta("先想一下")
            yield TextDelta("我先说前半句")
            raise ConnectionError("连接被重置")

    a = Agent(_流断(), ToolRegistry(), system_prompt="x")
    显示 = ""
    with pytest.raises(ConnectionError):          # 报错行为不变，照旧往上抛
        for ev in a.run_turn("帮我分析"):
            if isinstance(ev, TextDelta):
                显示 += ev.text
    asst = [m for m in a.messages if m.get("role") == "assistant"]
    assert len(asst) == 1 and asst[0]["content"] == 显示 == "我先说前半句"
    assert asst[0].get("reasoning_content") == "先想一下"        # 正文非空时思考一并留（发送时按档案过滤）
    assert not asst[0].get("tool_calls")


def test_只出了思考就断_不补空消息():
    """content="" 的补记是负收益：发送时 reasoning 按档案被剥掉（多数模型只收到光秃秃的空 assistant），
    而它还会顶掉压缩里"硬保留最后一轮 assistant 原文"的位置——_last_content 撞到空串就 return None，
    等于拿一条没内容的把有内容的挤掉。也与"空响应不记录空回合"的既有决定相悖。"""
    class _只思考就断:
        def stream(self, messages, tools=None, should_stop=None):
            yield ReasoningDelta("我在想")
            raise ConnectionError("断了")

    a = Agent(_只思考就断(), ToolRegistry(), system_prompt="x")
    a.messages.append({"role": "assistant", "content": "上一轮的完整结论"})
    with pytest.raises(ConnectionError):
        list(a.run_turn("问题"))
    assert [m for m in a.messages if m.get("role") == "assistant"] == [
        {"role": "assistant", "content": "上一轮的完整结论"}]        # 没多出空消息
    from mecode.compact import _last_content
    assert _last_content(a.messages, "assistant") == "上一轮的完整结论"   # 保留位没被顶掉


# ---- 轮【内】压缩（_turn_loop 顶部；_pre_turn 只管轮开头） ----

class _RampToolProvider:
    """每次调用都调一次工具，prompt_tokens 逐轮递增 —— 模拟单轮内上下文一路顶满。
    n_tools 次之后收尾，防止测试无限循环。"""
    def __init__(self, n_tools=4, step=100):
        self.calls = 0
        self.n_tools = n_tools
        self.step = step
        self.seen_msgs = []            # 每次请求时的消息条数（压缩会让它缩水）

    def stream(self, messages, tools=None, should_stop=None):
        # 压缩用的摘要请求是 tools=None 的那一次，得回一段【真的摘要文本】：
        # 回空的话 compact() 会判定"这次没压成"并原样保留 messages（那条闸是防止
        # 流断/后端回空时把整段上下文换成一个空摘要）。也不计进 calls/seen_msgs——
        # 那两个量测的是"工具循环里的请求"，混进摘要请求会让断言量错东西。
        if tools is None:
            yield TextDelta("【摘要】前面调了几次工具，还在干活")
            yield Done(reason="stop")
            return
        self.calls += 1
        self.seen_msgs.append(len(messages))
        if self.calls <= self.n_tools:
            yield ToolCall(id=f"c{self.calls}", name="noop", arguments={})
            yield Usage(prompt_tokens=self.step * self.calls, completion_tokens=1,
                        total_tokens=self.step * self.calls + 1)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("完成")
            yield Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
            yield Done(reason="stop")


def _ramp_agent(provider, **cfg_over):
    cfg = replace(agent_config, **cfg_over)
    return Agent(provider, _noop_reg(), system_prompt="你是助手", config=cfg)


def test_轮内超阈值会压缩_不再等到下一轮开头(tmp_path):
    p = _RampToolProvider(n_tools=4, step=100)
    a = _ramp_agent(p, context_limit=250, compact_threshold=0.8)   # 阈值 200 → 第 3 次请求后越线
    list(a.run_turn("干活"))
    assert p.calls > 2, "provider 没被调够次数，测试前提不成立"
    # 压缩后 messages 会缩水：某次请求看到的条数比上一次少
    assert any(b < x for x, b in zip(p.seen_msgs, p.seen_msgs[1:])), \
        f"消息数全程单调不减={p.seen_msgs} —— 轮内压缩没发生"


def test_轮内压缩会注入续跑说明_且点明不是用户打断(tmp_path):
    p = _RampToolProvider(n_tools=4, step=100)
    a = _ramp_agent(p, context_limit=250, compact_threshold=0.8)
    list(a.run_turn("干活"))
    hints = [m["content"] for m in a.messages
             if m["role"] == "user" and "不是用户打断" in m.get("content", "")]
    assert hints, "轮内压缩后没注入续跑说明 —— 模型会以为被用户叫停"
    assert "<system-reminder>" in hints[0]           # 带外注入，不显示成用户消息


def test_轮开头压缩不注入续跑说明(tmp_path):
    """轮开头压缩后紧跟着就是用户的新消息，不需要（也不该）说"接着做原来的任务"。"""
    a = _agent(tmp_path, context_limit=10, compact_threshold=0.5)
    a.messages += [{"role": "user", "content": "老问题"},
                   {"role": "assistant", "content": "老回答"}]
    a.context_tokens = 9999
    list(a.run_turn("新问题"))
    assert all("不是用户打断" not in m.get("content", "") for m in a.messages)


def test_轮内压缩后_tool_call与tool结果仍然配对(tmp_path):
    """压缩在工具循环中途整体替换 messages —— 最大的风险是把 tool_call 和它的结果拆散，
    留下孤儿 tool_call（后端直接 400）。"""
    p = _RampToolProvider(n_tools=4, step=100)
    a = _ramp_agent(p, context_limit=250, compact_threshold=0.8)
    list(a.run_turn("干活"))
    pending = []
    for m in a.messages:
        if m["role"] == "assistant":
            pending = [tc["id"] for tc in (m.get("tool_calls") or [])]
        elif m["role"] == "tool":
            if m.get("tool_call_id") in pending:
                pending.remove(m["tool_call_id"])
    assert not pending, f"存在没有结果的孤儿 tool_call：{pending}"


def test_未超阈值时轮内不压缩(tmp_path):
    p = _RampToolProvider(n_tools=3, step=10)
    a = _ramp_agent(p, context_limit=100_000, compact_threshold=0.8)
    list(a.run_turn("干活"))
    assert p.seen_msgs == sorted(p.seen_msgs), f"没超阈值却缩水了：{p.seen_msgs}"
    assert all("不是用户打断" not in m.get("content", "") for m in a.messages)


def test_压缩后重新注入任务清单快照(tmp_path):
    """任务清单是带外状态（存 tasks.json，不在被压的历史里）：压缩会把 task_* 调用折叠进摘要，
    不重新注入一份，模型压缩后就看不见自己的清单了。轮开头/轮内两条路径共用同一段代码。"""
    p = _RampToolProvider(n_tools=4, step=100)
    a = _ramp_agent(p, context_limit=250, compact_threshold=0.8)
    a._tasks.render_reminder = lambda: "当前清单：[ ] 把活干完"       # 装一份非空清单
    list(a.run_turn("干活"))
    assert any("把活干完" in m.get("content", "") for m in a.messages), \
        "压缩后没把任务清单重新注入 —— 模型会丢掉自己的待办"


class _SlowSummaryProvider:
    """摘要请求会一直吐字，直到 should_stop 说停 —— 用来验证"压缩中途能不能停"。"""
    def __init__(self):
        self.summary_calls = 0
        self.saw_should_stop = None

    def stream(self, messages, tools=None, should_stop=None):
        if tools is None:                       # 摘要请求
            self.summary_calls += 1
            self.saw_should_stop = should_stop
            for i in range(1000):
                if should_stop is not None and should_stop():
                    return                      # provider 的协作式停止：直接不再产出
                yield TextDelta(f"摘要片段{i} ")
            yield Done(reason="stop")
            return
        yield TextDelta("好的")
        yield Usage(prompt_tokens=99999, completion_tokens=1, total_tokens=100000)
        yield Done(reason="stop")


def test_压缩中途能停下_且不留半截摘要(tmp_path):
    """摘要那次请求原来【没传 should_stop】：打断标志置位了它也照跑到底，
    手动压缩时停止按钮按下去毫无反应，长历史要干等几十秒。

    停下来之后更要紧的是【别把半截摘要顶上去】——那等于把上下文换成一段不完整的转述。
    约定是返回空串，由 compact() 的空摘要闸判定"本次没压成"、原样保留 messages。
    """
    p = _SlowSummaryProvider()
    a = _ramp_agent(p, context_limit=250, compact_threshold=0.8)
    a.messages += [{"role": "user", "content": "老问题"},
                   {"role": "assistant", "content": "老回答"}]
    a.context_tokens = 9999                       # 已超阈值 → 一定会走压缩
    before = list(a.messages)

    a._interrupt.set()                            # 模拟用户在压缩过程中按了停止
    evs = list(a._maybe_compact())

    assert p.summary_calls == 1
    assert p.saw_should_stop is not None, "摘要请求没传 should_stop —— 停止按钮对它无效"
    assert a.messages == before, "被打断却还是把（半截）摘要顶上去了"
    assert any("取消压缩" in getattr(e, "text", "") for e in evs), \
        "打断后没告诉用户，看起来像点了停止没反应"


# ---- 输出用量：跟着 assistant 消息落盘（只存不发，供 UI 从 transcript 累计）----

class _UsageProvider:
    """两轮：调一次工具再收尾，各带一条 Usage。"""
    profile = INERT

    def __init__(self):
        self.calls = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.calls += 1
        if self.calls == 1:
            yield ReasoningDelta("想")
            yield ToolCall(id="c1", name="noop", arguments={})
            yield Usage(prompt_tokens=100, completion_tokens=30,
                        total_tokens=130, reasoning_tokens=25)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("好了")
            yield Usage(prompt_tokens=200, completion_tokens=868,
                        total_tokens=1068, reasoning_tokens=842)
            yield Done(reason="stop")


def test_用量跟着assistant消息存(tmp_path):
    """每条 assistant 记下【自己那次响应】的用量，累加才是整个会话的真实产出。
    prompt 不存：多轮之间大量重叠，累加没有意义（上下文占用另有其数）。"""
    reg = default_registry()
    reg.register(Tool("noop", "空工具", {"type": "object", "properties": {}}, lambda a: "ok"))
    a = Agent(_UsageProvider(), reg, system_prompt="你是助手")
    list(a.run_turn("问"))
    asst = [m for m in a.messages if m["role"] == "assistant"]
    assert [m["usage"] for m in asst] == [
        {"completion": 30, "reasoning": 25},
        {"completion": 868, "reasoning": 842},
    ]
    assert sum(m["usage"]["completion"] for m in asst) == 898
    assert "prompt" not in asst[0]["usage"]


def test_没有usage事件时不写空字段():
    """老后端/不返回 usage 的情况：不写这个字段，而不是写一堆 0——
    0 和"没测到"在累计里是一回事，但空字段会让 transcript 多出一堆噪声。"""
    class _NoUsage:
        profile = INERT

        def stream(self, messages, tools=None, should_stop=None):
            yield TextDelta("答")
            yield Done(reason="stop")

    a = Agent(_NoUsage(), default_registry(), system_prompt="你是助手")
    list(a.run_turn("问"))
    asst = [m for m in a.messages if m["role"] == "assistant"][-1]
    assert "usage" not in asst
