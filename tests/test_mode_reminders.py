"""模式声明的请求序列与存档行为；假 provider，不调用模型 API。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from mecode.agent import Agent
from mecode.config import Backend, agent_config
from mecode.events import Done, TextDelta, ToolCall, Usage
from mecode.mode import MODES
from mecode.permission import ALLOW, DENY
from mecode.provider import Provider
from mecode.session import SessionStore
from mecode.tools import Tool, ToolRegistry


class RecordingProvider:
    def __init__(self, first_call=None):
        self.requests = []
        self.first_call = first_call
        self.summary = "历史摘要"

    def stream(self, messages, tools=None, should_stop=None):
        if tools is None:  # 压缩请求
            yield TextDelta(self.summary)
        else:
            self.requests.append(deepcopy(messages))
            if len(self.requests) == 1 and self.first_call is not None:
                yield from self.first_call()
            else:
                yield TextDelta("完成")
        yield Done(reason="stop")


def make_agent(tmp_path, *, store=None, resume=None, provider=None, subagent=False):
    store = store or SessionStore(root=tmp_path, cwd=Path("C:/mode-test"), session_id="s")
    return Agent(provider or RecordingProvider(), ToolRegistry(), system_prompt="SYS",
                 store=store, resume_messages=resume, subagent=subagent,
                 config=replace(agent_config, context_limit=100_000, no_edit_reminder_turns=0))


def declarations(messages):
    return [m for m in messages if m.get("role") == "user" and "_mode" in m]


@pytest.mark.parametrize("mode", list(MODES))
def test_unchanged_mode_keeps_prefix_across_user_and_background_turns(tmp_path, mode):
    a = make_agent(tmp_path)
    a.set_mode(mode)
    a.ask("调查项目")
    previous = deepcopy(a.messages)
    a.ask("继续")
    assert a.provider.requests[1][:len(previous)] == previous
    a.queue_user("后台续跑")
    list(a.run_bg_turn())
    assert [m["_mode"] for m in declarations(a.messages)] == [mode]
    assert a.store.load_messages() == a.messages[1:]
    assert a.messages[0] == {"role": "system", "content": "SYS"}


@pytest.mark.parametrize("target", ["normal", "auto", "yolo"])
def test_leave_plan_appends_explicit_state_without_approving_plan(tmp_path, target):
    a = make_agent(tmp_path)
    a.set_mode("plan")
    a.ask("调查")
    old = deepcopy(a.messages)
    a.set_mode(target)
    assert a.messages == old  # 切换线程不写上下文。
    assert json.loads(a.store.session_json.read_text(encoding="utf-8"))["mode"] == target
    a.ask("继续")
    assert a.messages[:len(old)] == old
    ds = declarations(a.messages)
    assert [m["_mode"] for m in ds] == ["plan", target]
    assert "不再受计划模式" in ds[-1]["content"]
    assert "不代表计划已获批准" in ds[-1]["content"]


def test_switch_during_tool_batch_waits_until_all_results_recorded(tmp_path):
    def first():
        yield ToolCall("a", "switch", {})
        yield ToolCall("b", "noop", {})

    a = make_agent(tmp_path, provider=RecordingProvider(first))
    a.tools.register(Tool("switch", "test", {}, lambda _: (a.set_mode("auto"), "switched")[1]))
    a.tools.register(Tool("noop", "test", {}, lambda _: "done"))
    a.ask("执行")
    second = a.provider.requests[1]
    i = next(i for i, m in enumerate(second) if m.get("tool_calls"))
    assert [m["role"] for m in second[i:i + 4]] == ["assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in second[i + 1:i + 3]] == ["a", "b"]
    assert second[i + 3]["_mode"] == "auto"
    assert len(declarations(second)) == 2


def test_unobserved_switches_coalesce_and_changed_prompt_is_detected(tmp_path):
    a = make_agent(tmp_path)
    a.ask("hi")
    a.set_mode("plan")
    a.set_mode("normal")
    a.ask("继续")
    assert len(declarations(a.messages)) == 1
    a.mode_reminder += "\n新版模式说明"
    a.ask("继续")
    assert len(declarations(a.messages)) == 2
    assert "新版模式说明" in declarations(a.messages)[-1]["content"]


@pytest.mark.parametrize("changed", [False, True])
def test_resume_reuses_matching_state_and_repairs_unsent_switch(tmp_path, changed):
    a = make_agent(tmp_path)
    a.set_mode("plan")
    a.ask("调查")
    if changed:
        a.set_mode("auto")  # 没有下一次请求就退出。
    saved = a.store.load_messages()
    mode = json.loads(a.store.session_json.read_text(encoding="utf-8"))["mode"]
    b = make_agent(tmp_path, store=a.store, resume=saved)
    b.set_mode(mode)
    b.ask("继续")
    assert b.messages[1:1 + len(saved)] == saved
    assert len(declarations(b.messages)) == (2 if changed else 1)
    assert declarations(b.messages)[-1]["_mode"] == mode


def test_legacy_or_user_written_mode_text_does_not_count_as_program_state(tmp_path):
    a = make_agent(tmp_path, resume=[{"role": "user", "content": MODES["plan"].prompt}])
    a.set_mode("auto")
    a.ask("继续")
    assert [m["_mode"] for m in declarations(a.messages)] == ["auto"]


def test_changed_plan_path_is_announced_on_resume(tmp_path):
    a = make_agent(tmp_path)
    a.set_mode("plan")
    a.ask("调查")
    other = SessionStore(root=tmp_path, cwd=Path("C:/mode-test"), session_id="other")
    b = make_agent(tmp_path, store=other, resume=a.store.load_messages())
    b.set_mode("plan")
    b.ask("继续")
    ds = declarations(b.messages)
    assert len(ds) == 2
    assert other.plan_path.as_posix() in ds[-1]["content"]


def test_compaction_checkpoint_has_current_mode_even_if_closed_at_notice(tmp_path):
    a = make_agent(tmp_path)
    a.set_mode("plan")
    a.ask("真正的用户任务")
    a.provider.summary = "旧模式可能被错误概括：允许随意编辑。"
    a.context_tokens = 999_999
    compacting = a._maybe_compact()
    next(compacting)  # UI 刚收到压缩通知，用户直接关程序。
    compacting.close()
    saved = a.store.load_messages()
    assert saved == a.messages
    assert saved[-1]["_mode"] == "plan"
    assert "真正的用户任务" in "\n".join(m.get("content", "") for m in saved)
    b = make_agent(tmp_path, store=a.store, resume=saved)
    b.set_mode("plan")
    b.ask("继续")
    assert len(declarations(b.messages)) == 1


def test_inner_loop_compaction_reinserts_once(tmp_path):
    def first():
        yield ToolCall("a", "noop", {})
        yield Usage(prompt_tokens=999_999, completion_tokens=1, total_tokens=1_000_000)

    a = make_agent(tmp_path, provider=RecordingProvider(first))
    a.tools.register(Tool("noop", "test", {}, lambda _: "done"))
    a.set_mode("auto")
    a.ask("执行")
    assert len(a.provider.requests) == 2
    second = a.provider.requests[1]
    assert "历史摘要" in second[1]["content"]
    assert [m["_mode"] for m in declarations(second)] == ["auto"]
    assert a.store.load_messages() == a.messages


def test_failed_compaction_does_not_duplicate_declaration(tmp_path):
    a = make_agent(tmp_path)
    a.ask("hi")
    old = deepcopy(a.messages)
    a.provider.summary = ""
    a.context_tokens = 999_999
    assert list(a._maybe_compact()) == []
    assert a.messages == old
    a.context_tokens = 1
    a.ask("继续")
    assert len(declarations(a.messages)) == 1


def test_subagent_keeps_its_own_prompt_without_main_mode_override(tmp_path):
    a = make_agent(tmp_path, subagent=True)
    a.messages[0]["content"] = "子 agent 只读"
    a.ask("调查")
    assert declarations(a.messages) == []
    a.context_tokens = 999_999
    list(a._maybe_compact())
    assert declarations(a.messages) == []
    assert a.messages[0]["content"] == "子 agent 只读"


def test_wire_strips_mode_metadata_without_mutating_history():
    p = Provider(Backend(base_url="http://test/v1", model="deepseek-v4-flash", api_key="test"))
    messages = [{"role": "user", "content": "模式说明", "_mode": "auto"},
                {"role": "assistant", "content": "回答", "reasoning_content": "思考",
                 "usage": {"completion": 2}}]
    old = deepcopy(messages)
    assert p._for_wire(messages) == [{"role": "user", "content": "模式说明"},
                                     {"role": "assistant", "content": "回答"}]
    assert messages == old


def test_mode_switch_rebuilds_permissions_and_keeps_plan_path_exception(tmp_path):
    a = make_agent(tmp_path)
    a.set_mode("plan")
    path = a.store.plan_path.as_posix()
    assert a.policy.decide("write_file", {"path": path}) == ALLOW
    assert a.policy.decide("write_file", {"path": "C:/mode-test/a.py"}) == DENY
    a.set_mode("auto")
    assert a.policy.decide("write_file", {"path": "C:/mode-test/a.py"}) == ALLOW
    assert a.subagent_reminder == ""
