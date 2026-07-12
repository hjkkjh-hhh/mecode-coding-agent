"""Agent.run_turn 的压缩相关行为（用假 provider，不联网）。

固化的不变量：
- 压缩发生在 append 之前：当前问题不会被卷进摘要
- 未超阈值不压缩：旧历史原样、无 reminder 注入
（会话存储/单一真相/工具外置见 test_session.py）
"""
from dataclasses import replace

from mecode.agent import Agent
from mecode.config import agent_config
from mecode.events import Done, Notice, ReasoningDelta, TextDelta, ToolCall, Usage
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


# ---- 思考存储：无条件存（"存全、发时过滤"——该不该发给当前模型由 provider._filter_reasoning 决定）----

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
    该不该发给新模型由 provider._filter_reasoning 在发送时决定。切走再切回，CoT 完整。"""
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
