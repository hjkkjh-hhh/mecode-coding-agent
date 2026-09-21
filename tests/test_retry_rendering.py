"""重试事件到各消费端：只清理当前请求的思考，保留已完成的回合。"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deskserve
import serve
import tui
from mecode.events import ReasoningDelta, Retrying, TextDelta, ToolStarted


RETRY = Retrying(attempt=1, max_retries=3, delay=1, reason="请求超时")


def test_桌面序列化保留重试信号():
    assert deskserve.encode(RETRY) == {
        "type": "retrying", "text": RETRY.text, "attempt": 1, "max_retries": 3, "delay": 1,
    }


def test_TUI丢弃本次思考后从新思考开始(monkeypatch):
    mounted, activities = [], []
    # 只验证事件消费，不启动 Textual；保留传给 Static 的真实 Rich Text。
    monkeypatch.setattr(tui, "Static", lambda text: text)
    state = SimpleNamespace(_reasoning="", _answer="",
                            _mount=lambda widget, **kw: mounted.append(widget),
                            _set_activity=activities.append)
    for ev in [ReasoningDelta("discard"), RETRY, ReasoningDelta("new")]:
        tui.MecodeApp._on_event(state, tui.AgentEvent(ev))
    assert state._reasoning == "new" and state._answer == ""
    assert activities == ["思考中", "等待重试", "思考中"]
    assert len(mounted) == 1 and RETRY.text in mounted[0].plain


def test_CLI标明旧思考被丢弃且不串接换行(capsys, monkeypatch):
    import mecode.bootstrap

    def forbidden(**kwargs):
        raise AssertionError("导入 CLI 不能创建 Agent 或调用真实模型")

    monkeypatch.setattr(mecode.bootstrap, "build_agent", forbidden)
    import chat

    result = chat.render([ReasoningDelta("discard\n\n"), RETRY,
                          ReasoningDelta("new"), TextDelta("answer")])
    output = capsys.readouterr().out
    assert result == "answer"
    assert "本次未完成的思考已丢弃" in output
    assert output.index("discard") < output.index(RETRY.text) < output.index("new")


def test_网关回退结果只清理本次思考并透传重试事件():
    class Agent:
        messages = []  # 无可定位存档时，走事件累积兜底

        def run_turn(self, user_input):
            yield ReasoningDelta("previous")
            yield ToolStarted(name="noop", arguments={}, id="c1")
            yield ReasoningDelta("discard")
            yield RETRY
            yield ReasoningDelta("new")
            yield TextDelta("answer")

    emitted = []
    assert serve._run_and_capture(Agent(), "hi", emit=emitted.append) == ("answer", "previousnew")
    assert [e["kind"] for e in emitted] == ["reasoning", "tool", "reasoning", "retrying", "reasoning", "text"]
