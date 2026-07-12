"""联网工具：web_search（搜索）+ web_fetch（抓网页转正文）。

模型自装 skill/MCP、查文档、找仓库，大都从"搜一下"开始——这两个工具补上这最后一环，
和 read_file/grep 一样是只读操作（默认放行，见 permission.DEFAULT_RULES）。

- web_search 走 ddgs 库（原 duckduckgo-search）：免费、无 API key、元搜索（聚合
  DuckDuckGo/Bing/Google 等，auto 自动回落）。可选依赖：没装不崩，调用时人话提示安装。
  大陆直连 DuckDuckGo 不稳时 ddgs 会自动换引擎；仍失败的报错里提示配代理。
- web_fetch 用 httpx 抓 + 标准库 HTMLParser 转纯文本（剥 script/style/nav，把 <a> 收成
  "文字(URL)"）——原始 HTML 一页几十万字符全是标签噪音，转完才喂得起模型。
  超长走全局工具结果截断（tool_result_max_chars，头尾保留）。

用法链（模型自然会走）：web_search 拿"标题+URL+摘要"列表 → 挑 URL → web_fetch 读全文。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from .tools import Tool

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) mecode/0"   # 不少站对无 UA 的请求直接 403
_FETCH_LIMIT = 400_000     # 原始响应体上限（字节）：防超大页面撑爆内存；转完文本另有全局截断


# ---------- web_search ----------

def _web_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空"
    try:
        n = min(int(args.get("max_results") or 5), 10)
    except (TypeError, ValueError):     # 模型传 "五"/[3] 之类：报人话让它自纠，别裸 traceback 文案
        return "错误：max_results 要是数字（1~10）"
    try:
        from ddgs import DDGS
    except ImportError:
        return ("错误：搜索依赖未安装。请让用户运行 pip install ddgs 后重试"
                "（免费、无需 API key）。")
    try:
        results = list(DDGS().text(query, max_results=n))
    except Exception as e:
        return (f"错误：搜索失败（{type(e).__name__}: {str(e)[:120]}）。"
                "可能是网络不通或搜索引擎限流——稍后重试；若持续失败，告知用户可能需要代理。")
    if not results:
        return "没有搜到结果。换个关键词试试（更短、更通用的词效果更好）。"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '(无标题)')}\n   {r.get('href', '')}\n"
                     f"   {r.get('body', '')[:200]}")
    return "\n".join(lines) + "\n\n（需要看某条的全文，用 web_fetch 抓它的 URL）"


# ---------- web_fetch ----------

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "pre", "blockquote"}


class _TextExtract(HTMLParser):
    """HTML → 可读纯文本：跳过脚本样式，块级标签换行，<a href> 收成 '文字(URL)'（模型要能拿到链接继续 fetch）。"""

    def __init__(self) -> None:
        super().__init__()
        self._out: list[str] = []
        self._skip = 0            # 在 _SKIP_TAGS 内的嵌套深度
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")
        elif tag == "a":
            if self._href and self._href.startswith(("http://", "https://")):
                self._out.append(f"({self._href})")
            self._href = None

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def html_to_text(html: str) -> str:
    p = _TextExtract()
    try:
        p.feed(html)
    except Exception:
        pass                       # 畸形 HTML：能解析多少算多少
    return p.text()


def _web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "错误：url 必须以 http:// 或 https:// 开头"
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=30,
                      follow_redirects=True)
    except Exception as e:
        return f"错误：抓取失败（{type(e).__name__}: {str(e)[:120]}）"
    if r.status_code >= 400:
        return f"错误：HTTP {r.status_code}（{url}）"
    ctype = r.headers.get("content-type", "")
    body = r.text[:_FETCH_LIMIT]
    if "html" in ctype or body.lstrip()[:1] == "<":
        text = html_to_text(body)
        return text or "（页面没有可提取的文本内容）"
    return body        # JSON / 纯文本 / markdown 等：原样给（README raw、API 响应都很常见）


# ---------- 注册 ----------

def web_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description="联网搜索，返回结果列表（标题+URL+摘要）。找文档/仓库/资料时先用它拿 URL，再用 web_fetch 读全文。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词（更短、更通用的词效果更好）"},
                    "max_results": {"type": "integer", "description": "返回几条搜索结果（不填则默认 5，最多 10）"},
                },
                "required": ["query"],
            },
            handler=_web_search,
            read_only=True,
        ),
        Tool(
            name="web_fetch",
            description="抓取一个网页并转成可读文本（HTML 剥标签、保留正文和链接；JSON/纯文本原样返回）。配合 web_search：搜到 URL 后用它读内容。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL（http:// 或 https:// 开头）"},
                },
                "required": ["url"],
            },
            handler=_web_fetch,
            read_only=True,
        ),
    ]
