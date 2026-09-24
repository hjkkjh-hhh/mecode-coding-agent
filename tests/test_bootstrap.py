"""bootstrap.build_agent 工厂 + Agent.ask + 包级 re-export（headless 编程入口）。

固化的不变量：
- build_agent 一次调用组装齐全：本地+记忆工具、权限（含模式叠加）、会话归属=cwd
- plan 模式明确拒绝（无头没人批计划）；未配置后端在构造时报人话错（不是首次请求才炸）
- Agent.ask 跑完整个工具循环，返回【最后一条有正文的 assistant】（同 subagent 总结语义）
- session_id 续会话：历史消息接上
- from mecode import Agent/build_agent 可用（__init__ re-export，__all__ 收录）
"""
from dataclasses import replace
from pathlib import Path

import pytest

import mecode
from mecode import bootstrap
from mecode.agent import Agent
from mecode.config import Backend, agent_config
from mecode.events import Done, TextDelta, ToolCall
from mecode.tools import Tool, ToolRegistry


class _TextProvider:
    def stream(self, messages, tools=None, should_stop=None):
        yield TextDelta("回答")
        yield Done(reason="stop")


class _ToolThenText:
    """第一轮：说半句 + 调工具；第二轮：给最终答案。"""
    def __init__(self):
        self.n = 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n == 1:
            yield TextDelta("我先看看")
            yield ToolCall(id="c1", name="noop", arguments={})
        else:
            yield TextDelta("最终答案")
        yield Done(reason="stop")


def _tmp_root(monkeypatch, tmp_path):
    """把会话根挪进 tmp：测试不碰真 ~/.mecode。"""
    monkeypatch.setattr(bootstrap, "agent_config",
                        replace(agent_config, session_root=str(tmp_path / "mecode_root")))


def test_build_agent_组装齐全(monkeypatch, tmp_path):
    _tmp_root(monkeypatch, tmp_path)
    a = bootstrap.build_agent(cwd=tmp_path, mcp=False, provider=_TextProvider())
    names = {t["function"]["name"] for t in a.tools.schemas()}
    assert {"read_file", "bash", "save_memory", "recall_memory"} <= names   # 本地 + 记忆工具都在
    assert a.mode == "auto" and a.policy is not None                        # 默认 auto 档、权限闸在
    assert a.store.cwd == Path(tmp_path)                                    # 会话归属/权限根 = cwd
    assert a.messages[0]["role"] == "system" and a.messages[0]["content"].strip()
    assert a.mcp_clients == []                                              # mcp=False 不连
    assert not a.store.dir.exists()                                        # 初次组装仍懒创建


def test_plan模式_明确拒绝(monkeypatch, tmp_path):
    _tmp_root(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="plan"):
        bootstrap.build_agent(cwd=tmp_path, mode="plan", mcp=False, provider=_TextProvider())


def test_未配置后端_构造时报人话错(monkeypatch, tmp_path):
    _tmp_root(monkeypatch, tmp_path)
    monkeypatch.setattr(bootstrap, "current_backend",
                        lambda: Backend(base_url="", model="", api_key=""))
    with pytest.raises(RuntimeError, match="后端"):
        bootstrap.build_agent(cwd=tmp_path, mcp=False)


def test_ask_跑完工具循环_返回最终正文():
    reg = ToolRegistry()
    reg.register(Tool(name="noop", description="测试用",
                      parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"))
    a = Agent(_ToolThenText(), reg, system_prompt="s")
    assert a.ask("干活") == "最终答案"    # 最后一条有正文的 assistant，不是中途的"我先看看"


def test_session_id_续会话_载入历史(monkeypatch, tmp_path):
    _tmp_root(monkeypatch, tmp_path)
    a1 = bootstrap.build_agent(cwd=tmp_path, mcp=False, provider=_TextProvider())
    assert a1.ask("第一个问题") == "回答"                                   # 跑一轮 → transcript 落盘
    a2 = bootstrap.build_agent(cwd=tmp_path, mcp=False, provider=_TextProvider(),
                               session_id=a1.store.session_id)
    assert any(m.get("content") == "第一个问题" for m in a2.messages)       # 历史接上


def test_包级reexport():
    assert mecode.Agent is Agent
    assert mecode.build_agent is bootstrap.build_agent
    assert "build_agent" in mecode.__all__ and "Agent" in mecode.__all__


def test_headless入口_后端错误_一行人话_退出码1(monkeypatch, capsys):
    from mecode import __main__ as entry
    from mecode.provider import ProviderError

    class _Boom:
        def ask(self, q):
            raise ProviderError("限流或配额不足（HTTP 429，模型 x）：余额不足")

    monkeypatch.setattr(bootstrap, "build_agent", lambda **kw: _Boom())
    with pytest.raises(SystemExit) as ei:
        entry.main(["-p", "hello"])
    assert ei.value.code == 1                       # 脚本/CI 可判失败
    err = capsys.readouterr().err
    assert "mecode:" in err and "429" in err        # 一行人话进 stderr，不是 traceback
