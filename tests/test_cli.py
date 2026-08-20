"""`mecode` 命令的分流。

三条路各走各的入口，走错了不会报错、只会做成另一件事：
`mecode desk` 分流错了就掉进无头形态，报"缺少 -p"；
参数没换掉的话 deskserve 自己的 argparse 会看到多出来的 `desk`，直接退出。
"""
import sys

import pytest

from mecode import cli


@pytest.fixture
def spy(monkeypatch):
    """把真正的"起服务/开界面"换掉，只记下它被要求跑哪个脚本、带什么参数。"""
    calls = []
    monkeypatch.setattr(cli, "_run_script", lambda name, args: calls.append((name, list(args))))
    return calls


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["mecode", *argv])
    cli.main()


def test_无参数进_tui(monkeypatch, spy):
    run(monkeypatch)
    assert spy == [("tui.py", [])]


def test_desk_默认把浏览器一起开了(monkeypatch, spy):
    """`mecode desk` 是"开箱即用"那条路。只打印一行地址、要人自己去粘，就不叫快捷了。"""
    run(monkeypatch, "desk")
    assert spy == [("deskserve.py", ["--open"])]


def test_desk_透传参数(monkeypatch, spy):
    """`desk` 这个词必须【摘掉】再传下去——deskserve 的 argparse 不认识它。"""
    run(monkeypatch, "desk", "--port", "8360", "--cwd", "D:/x")
    assert spy == [("deskserve.py", ["--port", "8360", "--cwd", "D:/x", "--open"])]


def test_desk_可以不开浏览器(monkeypatch, spy):
    """--no-open 是 CLI 这一层的开关，不能原样递给 deskserve（它没这个参数，会报错退出）。"""
    run(monkeypatch, "desk", "--no-open", "--port", "1")
    assert spy == [("deskserve.py", ["--port", "1"])]


def test_desk_不重复加_open(monkeypatch, spy):
    run(monkeypatch, "desk", "--open")
    assert spy == [("deskserve.py", ["--open"])]


def test_带参数走无头(monkeypatch, spy):
    seen = []
    monkeypatch.setattr("mecode.__main__.main", lambda: seen.append(sys.argv))
    run(monkeypatch, "-p", "你好")
    assert seen and spy == [], "带 -p 不该去跑脚本"
