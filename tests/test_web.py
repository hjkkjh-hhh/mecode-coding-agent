"""联网工具：html 转文本、web_fetch 各内容类型、web_search 参数校验/缺依赖提示（全 mock，不真联网）。"""
import httpx

import mecode.web as web
from mecode.web import _web_fetch, _web_search, html_to_text, web_tools


def test_html_to_text_剥标签_块级换行_链接保留():
    html = ("<html><head><title>T</title><style>.x{}</style></head><body>"
            "<script>var a=1;</script><h1>标题</h1><p>第一段</p>"
            "<p>看<a href='https://ex.com/doc'>文档</a>继续</p>"
            "<div>第二段</div></body></html>")
    text = html_to_text(html)
    assert "标题" in text and "第一段" in text and "第二段" in text
    assert "var a=1" not in text and ".x{}" not in text and "T" != text[:1]   # script/style/head 剥掉
    assert "文档(https://ex.com/doc)" in text                                # 链接收成 文字(URL)
    assert "\n" in text                                                       # 块级标签换行


def test_html_to_text_畸形html不崩():
    assert html_to_text("<p>没闭合<div><<<>>") != ""


def test_web_fetch_html转文本(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><body><p>你好世界</p></body></html>",
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(web.httpx, "get", fake_get)
    assert _web_fetch({"url": "https://ex.com"}) == "你好世界"


def test_web_fetch_json原样返回(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              text='{"a": 1}', request=httpx.Request("GET", url))
    monkeypatch.setattr(web.httpx, "get", fake_get)
    assert _web_fetch({"url": "https://api.ex.com/x"}) == '{"a": 1}'


def test_web_fetch_错误分支(monkeypatch):
    assert "http://" in _web_fetch({"url": "ftp://x"})           # 非 http(s) 拒绝
    def fake_404(url, **kw):
        return httpx.Response(404, request=httpx.Request("GET", url))
    monkeypatch.setattr(web.httpx, "get", fake_404)
    assert "HTTP 404" in _web_fetch({"url": "https://ex.com/none"})
    def fake_boom(url, **kw):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr(web.httpx, "get", fake_boom)
    assert "抓取失败" in _web_fetch({"url": "https://ex.com"})


def test_web_search_空query拒绝():
    assert "不能为空" in _web_search({"query": "  "})


def test_web_search_缺依赖时人话提示(monkeypatch):
    import builtins
    orig = builtins.__import__
    def block_ddgs(name, *a, **kw):
        if name == "ddgs":
            raise ImportError("no ddgs")
        return orig(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", block_ddgs)
    out = _web_search({"query": "x"})
    assert "pip install ddgs" in out                             # 可选依赖：不崩、给安装指引


def test_web_tools_注册进默认表且权限放行():
    from mecode.permission import DEFAULT_RULES
    from mecode.tools import default_registry
    names = [t["function"]["name"] for t in default_registry().schemas()]
    assert "web_search" in names and "web_fetch" in names
    assert DEFAULT_RULES["web_search"]["allow"] == ["*"]
    assert DEFAULT_RULES["web_fetch"]["allow"] == ["*"]
