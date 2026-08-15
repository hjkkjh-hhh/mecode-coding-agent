"""长时间只读不改时的提示注入（_NO_EDIT_REMINDER）。

背景：SWE-bench 实测发现，难题上模型会在临时目录反复造探测脚本、上百轮不碰源码。
提示词治不动（"从报错现场入手"那条实测零效果），改由 harness 按运行时状态注入。

固化的不变量：
- 连续 N 次迭代没改工作目录内的文件 → 注入一条提示
- 改了工作目录内的文件 → 计数清零，不注
- 只写到工作目录【外】（/tmp 之类）不算"在推进"，计数照涨
- 注入后清零：再攒够 N 次才重注，不会每轮刷屏
- 配 0 = 关闭
"""
from dataclasses import replace

import pytest

from mecode.agent import Agent
from mecode.config import agent_config
from mecode.events import Done, TextDelta, ToolCall
from mecode.tools import Tool, ToolRegistry


def _noop(args, **kw):
    return "ok"


def _reg():
    reg = ToolRegistry()
    for name in ("edit_file", "write_file", "grep"):
        reg.register(Tool(name=name, description="测试用",
                          parameters={"type": "object", "properties": {}},
                          handler=_noop, read_only=(name == "grep")))
    return reg


class _LoopProvider:
    """前 n_calls 次每次吐一个工具调用，之后回文本结束——用来把 _turn_loop 撑够圈数。"""
    def __init__(self, call, n_calls):
        self.call, self.n_calls, self.n = call, n_calls, 0

    def stream(self, messages, tools=None, should_stop=None):
        self.n += 1
        if self.n <= self.n_calls:
            yield ToolCall(id=f"c{self.n}", name=self.call.name, arguments=self.call.arguments)
        else:
            yield TextDelta("完")
        yield Done(reason="stop")


def _run(tmp_path, call, n_calls, turns=3, monkeypatch=None):
    cfg = replace(agent_config, no_edit_reminder_turns=turns, context_limit=10**9)
    a = Agent(_LoopProvider(call, n_calls), _reg(), system_prompt="你是助手", config=cfg)
    list(a.run_turn("干活"))
    return [m for m in a.messages
            if m["role"] == "user" and "没有修改工作目录下的文件" in (m.get("content") or "")]


def _call(name, path):
    return ToolCall(id="x", name=name, arguments={"path": path})


def test_连续只读不改_到阈值注入提示(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("grep", "a.py"), n_calls=5, turns=3)
    assert len(hits) == 1
    assert "忽略它" in hits[0]["content"]        # 带适用边界，不是命令式


def test_改了工作目录内的文件_计数清零_不注(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("edit_file", str(tmp_path / "a.py")), n_calls=10, turns=3)
    assert hits == []


def test_只写到工作目录外_不算推进_照样注(tmp_path, monkeypatch):
    """探测脚本写在 /tmp：这正是要抓的行为，不能因为调了 write_file 就清零。"""
    outside = tmp_path.parent / "outside_probe.py"
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("write_file", str(outside)), n_calls=5, turns=3)
    assert len(hits) == 1


def test_注入后清零_不每轮刷屏(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("grep", "a.py"), n_calls=8, turns=3)
    assert len(hits) == 2      # 8 次迭代、阈值 3 → 第 3、6 次各注一条（不是 6 条）


def test_配零则关闭(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("grep", "a.py"), n_calls=20, turns=0)
    assert hits == []


def test_相对路径算在项目内(tmp_path, monkeypatch):
    """模型经常给相对路径（src/x.py）——resolve 后落在 cwd 下，要算作在推进。"""
    monkeypatch.chdir(tmp_path)
    hits = _run(tmp_path, _call("edit_file", "src/x.py"), n_calls=10, turns=3)
    assert hits == []


def test_畸形路径不炸(tmp_path, monkeypatch):
    """含空字节等解析不了的路径：不抛异常即可，方向不作断言（各平台 resolve 行为不同）。"""
    monkeypatch.chdir(tmp_path)
    _run(tmp_path, _call("edit_file", "\x00bad"), n_calls=5, turns=3)
