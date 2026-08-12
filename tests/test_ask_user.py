"""ask_user 提问工具：参数校验、规范化、作答/跳过的结果格式化 + TUI 侧的摘要/输入框文案。

固化的不变量：① 非法参数返回错误字符串（不调回调）② 回调收到的 questions 已规范化
（multi_select 默认 False、header 截到显示上限）③ 跳过/部分作答/多选的返回文案形状
④ 收起行摘要与展开输入框的排版。弹窗交互不在此测（textual 真机行为，headless 测不到）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tui   # noqa: E402

from mecode.tools import ASK_HEADER_CHARS, format_ask_result, make_ask_user_tool   # noqa: E402


def _q(question="选哪个？", options=None, **kw):
    return {"question": question,
            "options": options or [{"label": "A", "description": "甲"},
                                   {"label": "B", "description": "乙"}],
            **kw}


def test_校验_questions非数组或数量越界():
    called = []
    tool = make_ask_user_tool(lambda qs: called.append(qs))
    assert "错误" in tool.handler({})
    assert "错误" in tool.handler({"questions": "x"})
    assert "错误" in tool.handler({"questions": []})
    assert "错误" in tool.handler({"questions": [_q()] * 5})
    assert called == []                                   # 非法参数不弹窗


def test_校验_选项数量与字段缺失():
    tool = make_ask_user_tool(lambda qs: None)
    assert "options" in tool.handler({"questions": [_q(options=[{"label": "A", "description": ""}])]})
    assert "label" in tool.handler({"questions": [_q(options=[{"label": "A", "description": ""},
                                                              {"description": "没标签"}])]})
    assert "question" in tool.handler({"questions": [{"options": [{"label": "A", "description": ""},
                                                                  {"label": "B", "description": ""}]}]})


def test_单选作答_回调收到规范化问题():
    seen = {}

    def cb(qs):
        seen["qs"] = qs
        return {"answers": {qs[0]["question"]: "A"}, "skipped": []}

    tool = make_ask_user_tool(cb)
    out = tool.handler({"questions": [_q(header="很长很长的标签超过十二个字符了")]})
    assert "用户已作答" in out and "→ A" in out
    q = seen["qs"][0]
    assert q["multi_select"] is False                     # 未传时补齐默认
    assert len(q["header"]) == ASK_HEADER_CHARS           # 超长截断显示、不报错
    assert q["options"][0] == {"label": "A", "description": "甲"}


def test_多选答案_列表顿号拼接():
    out = format_ask_result({"answers": {"问？": ["A", "其他：自定义"]}, "skipped": []})
    assert "A、其他：自定义" in out


def test_跳过_返回自行判断提示():
    tool = make_ask_user_tool(lambda qs: None)            # 回调 None = 全部跳过/中断
    out = tool.handler({"questions": [_q()]})
    assert "跳过" in out and "最佳判断" in out
    assert format_ask_result({"answers": {}, "skipped": ["问？"]}).startswith("用户跳过了提问")


def test_部分作答_列出未答问题():
    out = format_ask_result({"answers": {"第一问？": "A"}, "skipped": ["第二问？"]})
    assert "第一问？ → A" in out and "第二问？" in out and "跳过未答" in out


def test_schema_名称与必填():
    tool = make_ask_user_tool(lambda qs: None)
    assert tool.name == "ask_user"
    assert tool.parameters["required"] == ["questions"]
    assert tool.read_only is False                        # 阻塞交互，不进只读并发批


def test_校验_multi_select字符串布尔按语义解析():
    seen = {}
    tool = make_ask_user_tool(lambda qs: seen.update(qs=qs))
    tool.handler({"questions": [_q(multi_select="false"), _q(question="二？", multi_select="true")]})
    assert seen["qs"][0]["multi_select"] is False        # bool("false") 是 True，必须按语义解析
    assert seen["qs"][1]["multi_select"] is True


def test_校验_重复问题文本拒收():
    tool = make_ask_user_tool(lambda qs: None)
    out = tool.handler({"questions": [_q(question="同一问？"), _q(question="同一问？")]})
    assert "错误" in out and "重复" in out               # 答案按问题文本对应，重复会互相覆盖


def test_校验_description缺失宽松通过():
    seen = {}
    tool = make_ask_user_tool(lambda qs: seen.update(qs=qs))
    out = tool.handler({"questions": [_q(options=[{"label": "A"}, {"label": "B"}])]})
    assert "错误" not in out                             # schema required 只到 label，校验同步宽松
    assert seen["qs"][0]["options"][0]["description"] == ""


def test_TUI摘要_首题与多题条数():
    one = {"questions": [_q(question="只有一题？")]}
    two = {"questions": [_q(question="第一题？"), _q(question="第二题？")]}
    assert tui._summarize_tool("ask_user", one) == "只有一题？"
    assert tui._summarize_tool("ask_user", two) == "2 题：第一题？"


def test_TUI输入框_逐题铺选项含多选标记():
    text = tui._tool_input_text("ask_user", {"questions": [
        _q(question="要哪种？", multi_select=True),
        _q(question="第二问？"),
    ]})
    assert "1. 要哪种？（多选）" in text
    assert "   - A：甲" in text
    assert "2. 第二问？" in text and "（多选）" not in text.split("2. ")[1]


def test_TUI渲染_畸形参数不抛异常():
    """ToolStarted 渲染先于工具校验、跑在 UI 线程——畸形形状抛异常会崩整个 app，必须自防。"""
    malformed = [
        {"questions": 123},                              # 非数组
        {"questions": {"question": "对象不是数组"}},      # dict：真值判定过、qs[0] 是 KeyError
        {"questions": [{"question": None}]},             # 键在值空：get 默认值不生效
        {"questions": [{"question": 5, "options": 5}]},  # question 非串 + options 标量不可迭代
        {"questions": []},
    ]
    for args in malformed:
        assert isinstance(tui._summarize_tool("ask_user", args), str)
        assert isinstance(tui._tool_input_text("ask_user", args), str)
