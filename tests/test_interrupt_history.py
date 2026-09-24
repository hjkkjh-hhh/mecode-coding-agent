"""主动中断后的正文、磁盘历史和下一次请求保持一致；不调用真实模型。"""
from copy import deepcopy

import pytest

from mecode.agent import Agent
from mecode.events import Done, ReasoningDelta, TextDelta, ToolCall, Usage
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


MARKER = {"role": "user", "content": "[Request interrupted by user]"}


class PartialProvider:
    def __init__(self, mode, text="已经输出的半段正文"):
        self.mode = mode
        self.text = text
        self.agent = None

    def stream(self, messages, tools=None, should_stop=None):
        yield ReasoningDelta("已产生的思考")
        if self.text:
            yield TextDelta(self.text)
        if self.mode == "keyboard":
            raise KeyboardInterrupt
        if self.mode == "cooperative":
            self.agent.request_interrupt()
            assert should_stop()
            return  # 与真实 provider 一样，停止消费后直接结束流
        pytest.fail("close 测试应在收到 delta 时关闭生成器")


class CaptureProvider:
    def __init__(self):
        self.requests = []

    def stream(self, messages, tools=None, should_stop=None):
        self.requests.append(deepcopy(messages))
        yield TextDelta("继续后的回答")
        yield Done(reason="stop")


def interrupt_turn(agent, mode, text):
    gen = agent.run_turn("请分析")
    for event in gen:
        if mode == "close" and isinstance(event, TextDelta if text else ReasoningDelta):
            gen.close()
            gen.close()  # 再次 close 不应重复写入正文或标记
            break


@pytest.mark.parametrize("mode", ["keyboard", "cooperative", "close"])
@pytest.mark.parametrize("resume", [False, True], ids=["same-agent", "resume"])
def test_中断正文立即落盘且下一次请求只带一次(tmp_path, mode, resume):
    store = SessionStore(root=tmp_path, cwd=tmp_path, session_id="interrupted")
    provider = PartialProvider(mode)
    agent = Agent(provider, ToolRegistry(), system_prompt="s", store=store)
    provider.agent = agent
    interrupt_turn(agent, mode, provider.text)

    expected = [
        {"role": "user", "content": "请分析"},
        agent._mode_message(),
        {"role": "assistant", "content": provider.text,
         "reasoning_content": "已产生的思考"},
        MARKER,
    ]
    # 此时尚未发下一条用户消息；存档必须已经完整，不能依赖下一轮补写。
    assert store.load_messages() == expected
    assert store.read_transcript_messages() == expected
    assert agent.messages[1:] == expected

    capture = CaptureProvider()
    if resume:
        store = SessionStore(root=tmp_path, cwd=tmp_path, session_id="interrupted")
        agent = Agent(capture, ToolRegistry(), system_prompt="s", store=store,
                      resume_messages=store.load_messages())
    else:
        agent.provider = capture
    list(agent.run_turn("继续"))
    assert capture.requests == [[{"role": "system", "content": "s"}]
                                + expected + [{"role": "user", "content": "继续"}]]
    assert store.load_messages() == expected + [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "继续后的回答"},
    ]


@pytest.mark.parametrize("mode", ["keyboard", "cooperative", "close"])
@pytest.mark.parametrize("text", ["", " \n"])
def test_只有思考或空白时不制造空assistant(tmp_path, mode, text):
    store = SessionStore(root=tmp_path, cwd=tmp_path)
    provider = PartialProvider(mode, text=text)
    agent = Agent(provider, ToolRegistry(), store=store)
    provider.agent = agent
    interrupt_turn(agent, mode, text)
    assert store.load_messages() == [{"role": "user", "content": "请分析"},
                                     agent._mode_message(), MARKER]


def test_流式阶段中断不执行或存入尚未提交的工具调用(tmp_path):
    executed = []
    registry = ToolRegistry()
    registry.register(Tool(name="write", description="", parameters={"type": "object"},
                           handler=lambda args: executed.append(args) or "done"))

    class Provider:
        def stream(self, messages, tools=None, should_stop=None):
            yield TextDelta("准备操作")
            yield ToolCall(id="uncommitted", name="write", arguments={})
            yield Usage(prompt_tokens=10, completion_tokens=3, total_tokens=13)
            raise KeyboardInterrupt

    store = SessionStore(root=tmp_path, cwd=tmp_path)
    agent = Agent(Provider(), registry, store=store)
    list(agent.run_turn("执行"))
    assert not executed
    assert store.load_messages() == [
        {"role": "user", "content": "执行"},
        agent._mode_message(),
        {"role": "assistant", "content": "准备操作", "usage": {"completion": 3, "reasoning": 0}},
        MARKER,
    ]
