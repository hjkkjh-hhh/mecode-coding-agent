"""本地斜杠命令表（SLASH_COMMANDS 单一真相）的纯逻辑防漂移。

固化的不变量：
- 别名全局唯一、都以 / 开头、说明非空（/help 清单直接渲染自这张表）
- 表里的 handler 方法名都真实存在（getattr 分发不会 AttributeError）
- /help 可被发现；/rl /rs 续会话命令的匹配语义不变
只测纯数据/纯函数，不起 textual（导入 tui 即可）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tui   # noqa: E402


def test_命令表_别名唯一_都以斜杠开头_说明非空():
    seen = set()
    for aliases, desc, _method in tui.MecodeApp.SLASH_COMMANDS:
        assert desc.strip()
        for a in aliases:
            assert a.startswith("/"), a
            assert a not in seen, f"别名重复：{a}"
            seen.add(a)


def test_命令表_handler方法都存在():
    for _aliases, _desc, method in tui.MecodeApp.SLASH_COMMANDS:
        assert callable(getattr(tui.MecodeApp, method, None)), f"方法不存在：{method}"


def test_help命令在表里():
    all_aliases = {a for aliases, _d, _m in tui.MecodeApp.SLASH_COMMANDS for a in aliases}
    assert "/help" in all_aliases and "/?" in all_aliases


def test_续会话命令匹配语义不变():
    m = tui.MecodeApp._match_resume
    assert m("/rl") == "latest" and m("/resume latest") == "latest"
    assert m("/rs") == "session" and m("/resume session") == "session"
    assert m("/random") is None and m("随便说点什么") is None
