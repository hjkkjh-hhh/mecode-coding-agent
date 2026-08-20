"""desktop/ 三个前端文件的静态自检。

为什么要有它：这些文件是"一坨字符串"，写坏了没有编译器拦——最多是打开页面一片空白，
而空白页看不出是哪一行断的。这里把那些"坏了也不报错"的失效方式逐条钉住。

每条断言都对应一个真踩过的坑，坑写在断言的消息里。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "desktop"
HTML = WEB / "index.html"
_html_cache = HTML.read_text(encoding="utf-8")


_js_cache = (WEB / "settings.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html():
    return _html_cache


@pytest.fixture(scope="module")
def js():
    return _js_cache


# ---------------------------------------------------------------- 文件本身

@pytest.mark.parametrize("name", ["index.html", "markdown.js", "settings.js", "icon.svg"])
def test_文件干净(name):
    """行尾统一 LF、不含控制字符。

    两次真坏过：① 用 read_bytes() 读进来（\\r\\n 原样留着）再 write_text()（又把 \\n 转成
    \\r\\n）→ 变成 \\r\\r\\n，通用换行读时每行后面凭空多一个空行；② 想往源码里写 \\u0000
    当占位符，结果落盘成真的 NUL 字节，文件直接变二进制。写文件一律 newline=""。
    """
    raw = (WEB / name).read_bytes()
    assert b"\r" not in raw, f"{name} 混进了 \\r：写文件要带 newline=''"
    assert not any(bytes([c]) in raw for c in range(9)), f"{name} 混进了控制字符"


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
@pytest.mark.parametrize("name", ["markdown.js", "settings.js"])
def test_外链脚本语法(name):
    r = subprocess.run(["node", "--check", str(WEB / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_内联脚本语法(html):
    """页面主循环就在这段内联 script 里，语法一断整个页面全哑。"""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "找不到内联 <script>"
    for i, js in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        assert r.returncode == 0, f"内联 script #{i}：{r.stderr}"


# ---------------------------------------------------------------- 结构

def test_取的_id_都真的存在(html):
    """$('x') 写错一个字母不会报错，只会静默拿到 null，后面一路 TypeError。"""
    allsrc = html + "".join(f.read_text(encoding="utf-8") for f in sorted(WEB.glob("*.js")))
    declared = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', allsrc))
    # selHtml('mc-scope', …) 也是在声明 id——下拉把值放在一个隐藏 input 里，
    # 那个 id 是拼出来的，字面量 id="…" 匹配不到
    declared |= set(re.findall(r"selHtml\('([A-Za-z0-9_-]+)'", allsrc))
    used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", allsrc))
    assert not (used - declared), f"取了不存在的 id：{sorted(used - declared)}"


def test_主题图标是自绘的(html):
    """☀ ☾ 🖥 这类字符的粗细、大小、基线全由系统字体决定，
    🖥 在 Windows 上还会被渲成彩色 emoji，和旁边两个单色符号完全不是一套东西。"""
    block = re.search(r"const THEMES = \[(.*?)\];", html, re.S).group(1)
    assert "svg:" in block, "主题图标又变回字符了"
    assert not (set(block) & set("☀☾🖥")), block


def test_不用原生下拉(html, js):
    """原生 select 的选项列表由操作系统绘制：背景、圆角、字重、行距一概不吃 CSS，
    在这套深浅色主题里就是一块突兀的灰底方块。四处下拉全部走 selHtml 自绘。"""
    assert "<select" not in html + js, "又冒出原生 select 了"
    assert "selHtml(" in js


def test_设置窗切页不改大小(html):
    """高度跟着内容走的话，「通用」矮、「模型」高，来回点页签窗口就在跳。"""
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    body = re.search(r"\.set-body\{([^}]*)\}", css).group(1)
    assert "height:min(" in body and "min-height" not in body, body


def test_切页签不闪(js):
    """先清成「载入中」再 await，而接口都在本机、几十毫秒就回来——
    每次切页都闪一帧白。改成慢了才顶上去，快的时候根本看不到中间态。"""
    assert re.search(r"setTimeout\(\(\) => \{ box\.innerHTML = .*载入中.*\}, 150\)", js), \
        "载入提示又变回立刻显示了"
    assert "clearTimeout(slow)" in js


def test_标签与花括号平衡(html):
    for tag in ("div", "button", "select"):
        o = len(re.findall(rf"<{tag}[\s>]", html))
        c = len(re.findall(rf"</{tag}>", html))
        assert o == c, f"<{tag}> 开合不平：{o} 开 / {c} 合"
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert css.count("{") == css.count("}"), "CSS 花括号不平"


def test_脚本加载顺序(html):
    """settings.js 是经典脚本，靠共享全局词法环境用 index.html 里的 $ / esc / modal，
    必须排在内联脚本【之后】；markdown.js 反过来，要排在【之前】（paintMd 调它）。"""
    assert html.index('src="markdown.js"') < html.index("function paintMd"), \
        "markdown.js 排到内联脚本后面了，paintMd 调不到 renderMarkdown"
    assert html.index('src="settings.js"') > html.rindex("es.onmessage"), \
        "settings.js 排到内联脚本前面了，openSettings 取不到 $ / esc / modal"


def test_设置按钮延迟取函数(html):
    """openSettings 定义在后加载的 settings.js 里。
    写成 onclick = openSettings 会在绑定那一刻取到 undefined，点了没反应。"""
    assert "() => openSettings()" in html
    assert not re.search(r"onclick\s*=\s*openSettings\s*;", html), \
        "绑定写成了直接引用，会取到 undefined"


# ---------------------------------------------------------------- 主题三态

def test_主题三态齐全(html):
    """跟随系统 / 亮 / 暗三种写法缺一不可；颜色只写在 @media 里会让"未标记"状态没有定义，
    渲成一半亮一半暗。"""
    assert re.search(r"^:root \{", html, re.M), "缺 :root 基准调色板"
    assert '@media (prefers-color-scheme:dark)' in html
    assert ':root:not([data-theme="light"])' in html, \
        "跟随系统的暗色没有排除【显式选亮】，选了亮色也会被系统拽暗"
    assert ':root[data-theme="dark"]' in html, "缺显式选暗"


def test_hidden_压得住(html):
    """.circle{display:grid} 这类等权重规则会盖过 UA 的 [hidden]，元素藏不住。"""
    assert "[hidden]{display:none !important}" in html


# ---------------------------------------------------------------- Markdown 渲染

def test_md_气泡权重压得过_pre_wrap(html):
    """.bubble.md 是 (0,2,0)，压不过 .msg.bot .bubble 的 (0,3,0)。
    权重不够时 pre-wrap 关不掉，标签之间的换行全变成真空行、整段撑开一倍。"""
    assert ".msg.bot .bubble.md" in html, "关 pre-wrap 的选择器权重不够"


def test_复制按钮捕获当前节点(html):
    """live 每换一段流就被重新赋值。闭包直接引用它的话，
    翻上去点旧气泡的复制，复制到的是最后那一段。"""
    assert "const node = live;" in html
    assert "copyBtn(() => node._raw)" in html
    assert "copyBtn(() => live." not in html, "复制按钮又引用回全局的 live 了"


def test_渲染的是原文不是_textContent(html):
    """渲染是有损的（标记被吃掉了）。要复制/重渲都得回到 _raw，不能从 textContent 反推。"""
    assert "el._raw" in html and "live._raw += text" in html


# ---------------------------------------------------------------- 图标

def test_图标能在小尺寸认出来():
    """favicon 是 16px。SVG 里不能只靠细节，且必须自带 viewBox 才能缩放。"""
    svg = (WEB / "icon.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 32 32"' in svg
    assert "xmlns=" in svg, "缺 xmlns，当 <img src> 用时浏览器不认"
    assert 'stroke-width="2.8"' in svg, "笔画太细的话 16px 下会糊掉"


def test_favicon_挂上了(html):
    assert 'rel="icon"' in html and 'href="icon.svg"' in html

def test_字标图文同行(html):
    """.mark 是 display:block，装它的容器必须是 flex——
    普通流里块级元素独占一行，图标会掉到 mecode 上面去。"""
    logo = re.search(r"\.logo\{([^}]*)\}", html).group(1)
    assert "display:flex" in logo, "侧栏字标不是 flex 行，图标会自己占一行"
    h1 = re.search(r"\.hero h1\{([^}]*)\}", html).group(1)
    assert "display:flex" in h1, "首屏标题不是 flex 行"

def test_字标不被_flex_拆开(html):
    """flex 容器里的裸文字会被包成【匿名 flex 项】，于是 gap 也加在 me 和 code 之间，
    看着像多敲了个空格。字标文字必须整个裹进一个元素。"""
    for m in re.finditer(r'<span class="logo">(.*?)</span></span>|<h1>(.*?)</h1>', html):
        frag = m.group(1) or m.group(2)
        assert '<span class="wm">me' in frag, "字标文字没裹进 .wm，会被 gap 拆开：" + frag[:120]

def test_侧栏拖宽用指针捕获(html):
    """不能往 document 上挂 mousemove：指针拖出窗口再松开时收不到 mouseup，
    侧栏会一直粘着跟手走。setPointerCapture 之后事件必定回到把手上。"""
    assert "setPointerCapture" in html and "releasePointerCapture" in html
    assert "pointercancel" in html, "少了 pointercancel，被系统打断时状态复不了位"
    assert "body.resizing" in html, "拖动时没禁选，横向拖会把侧栏文字整片选蓝"


def test_会话标题给两行(html):
    """一行截断后，几条以同一段话开头的会话长得一模一样、分不出谁是谁。"""
    assert "-webkit-line-clamp:2" in html
    rule = re.search(r"#sessions \.item \.t\{([^}]*)\}", html).group(1)
    assert "white-space:normal" in rule, "不覆盖 nowrap 的话行夹不生效"


def test_三点按钮是自绘图标(html):
    """不用 ⋮ 字符：粗细和间距全看系统字体，Windows 上又细又挤，放大只是把糊的放得更大。"""
    assert "ICON.kebab" in html
    assert ">\u22ee<" not in html, "按钮里又用回 ⋮ 字符了"
    rule = re.search(r"#sessions \.item \.kebab\{([^}]*)\}", html).group(1)
    assert "width:26px" in rule, "点击区太小，不好点中"
    assert "place-items:center" in rule, "图标没居中（会贴左上）"
    assert "#sessions .item:hover .kebab, #sessions .item:focus-within .kebab{display:grid}" in html, \
        "显示态要用 grid，place-items 才生效"


def test_时间与三点占同一位置(html):
    """并排的话平时右下角就一直挂着个跟内容无关的按钮在抢注意力。"""
    assert "#sessions .item:hover .ago" in html and "display:none" in html


def test_上滑加载历史(html):
    """滚动加载三件套，少一件就出问题：
    监听器（没有就只能点按钮）、并发锁（一次滑动能发几十个 scroll 事件）、
    锚点补偿（前插内容会把正在看的那条推出视野）。"""
    assert "$('main').addEventListener('scroll'" in html, "没有滚动监听，只能靠点按钮"
    assert "loadingMore" in html, "没有并发锁，一次滑动会并发发出几十个请求、重复插同一页"
    assert "m.scrollTop = t0 + (m.scrollHeight - h0)" in html, \
        "没有锚点补偿，每加载一页正在看的那条就被推走"
    assert "col.querySelector('.loadmore')" in html, \
        "按钮不能删：历史比视口还短时滚不动，滚动事件永远不触发"


def test_运行中是绿色呼吸灯(html):
    """顶栏状态和工具调用共用同一套：绿点在呼吸 = 有东西在跑。"""
    assert "@keyframes ping" in html
    assert ".conn.run .cpip::after, .spin .cpip::after" in html, "两处没共用同一条规则"
    assert "prefers-reduced-motion" in html
    # 光晕必须是绝对定位的独立层：缩放圆点本身会撑动 flex 行高，整条状态栏跟着抖
    rule = re.search(r"\.conn\.run \.cpip::after[^{]*\{([^}]*)\}", html).group(1)
    assert "position:absolute" in rule

def test_可滚容器加了绘制隔离(html):
    """圆角 + overflow:hidden 的裁剪层套一个可滚的 <pre>：details 反复开合时那一层
    被复用而没重绘，留下上一帧的残影（summary 重复一遍、正文从中间截断）。
    contain:paint 让浏览器当独立绘制区处理，脏区域算得准。"""
    for sel in (r"\.tool\{", r"\.bubble\.md \.md-code\{"):
        rule = re.search(sel + r"([^}]*)\}", html).group(1)
        assert "contain:paint" in rule, f"{sel} 少了 contain:paint"
    pre = re.search(r"\.tool pre\{([^}]*)\}", html).group(1)
    assert "overflow:auto" in pre, "只写 overflow-x 时 y 是隐式提成 auto 的，隐式那轴是残影帮凶"

def test_工具摘要行基线对齐(html):
    """摘要行三段文字字体不同（工具名/限定语等宽、摘要无衬线），两种字体 ascent 比例不同：
    居中对齐时即使行盒等高，基线仍然错开，看着就是"描述比工具名低一点"。
    图标和箭头没有文字基线，要单独拨回居中。"""
    rule = re.search(r"\.tool > summary\{([^}]*)\}", html).group(1)
    assert "align-items:baseline" in rule, "居中对齐会让不同字体的基线错开"
    assert ".tool .ticon, .tool .caret, .tool .spin{align-self:center}" in html,         "图标/箭头没被拨回居中，会跟着文字基线掉下去"

def test_统计行每段各裹一个_span(html):
    """.statline 是 flex 容器，直接放进去的裸文字会被包成匿名 flex 项——
    "2" 和 "轮" 成了两个项，一 wrap 就一行一个字竖着排下来（和字标被 gap 拆开同一个坑）。"""
    assert "<span><b>${turns}</b> 轮</span>" in html, "统计行的段没裹 span"
    assert "<span>输出 <b>${lastUsage.completion}</b> tok" in html
    assert "</span>`);" in html, "输出那段没闭合 span"


def test_回显必须先于请求(html):
    """服务端是"先 emit 再 return"：turn_start / queued 在 HTTP 响应写回【之前】就上了 SSE。
    localEcho 设在 await 之后就晚一步，抢先到达的事件认领不到回显、自己又渲一个——
    表现成"新会话第一条消息发两遍"。"""
    body = re.search(r"async function send\(\)\{(.*?)\n\}", html, re.S).group(1)
    echo_at = body.index("pendingEcho.push(text)")
    post_at = body.index("await api('/api/send'")
    assert echo_at < post_at, "回显记号还是记在 await 之后，第一条消息会渲两遍"
    assert "node.remove()" in body, "发送失败没把乐观渲染的气泡撤掉"
    assert "if (!claimEcho(m.text)) userBubble(m.text, true);" in html, \
        "queued 事件没走认领，排队的消息会渲两遍"


def test_输入框不强制同步布局(html):
    """height='auto' 再读 scrollHeight 会逼浏览器立刻重算整页，而整页底下挂着整条对话
    （几十个工具卡片、代码块、表格）。粘一大段文本就是卡好几秒。改用离屏镜像量高。"""
    assert "ta-mirror" in html, "没有离屏镜像"
    assert "mirror.scrollHeight" in html
    assert "input.style.height = 'auto'" not in html, "又回到强制同步布局那条路上了"


def test_审批弹窗写清授权范围(html):
    """不写范围等于让用户盲签；pattern 为空时还必须【不给】那个按钮。"""
    assert "总是允许 <code>${esc(m.pattern)}</code>" in html, "按钮上没写授权范围"
    assert "const always = m.pattern" in html, "pattern 为空时没有隐藏按钮"
    assert "只能逐次允许" in html, "授权不了时没有向用户解释原因"


# ---------------------------------------------------------------- 对抗审查抓到的

def test_回显认领是队列不是单槽(html):
    """单个变量只装得下一条。忙时连发 a、b：发 b 时记号被覆盖成 b，
    a 的 queued 事件回来认领不到，于是又渲一遍——屏幕上是 "a b a"。"""
    assert "let pendingEcho = []" in html, "回显记号又变回单个变量了"
    assert "localEcho" not in html, "还有残留的单槽写法"
    # 三处调用：turn_start 认领、queued 认领、发送失败撤记号
    assert html.count("claimEcho(") == 3, "认领没走同一个入口"
    assert "pendingEcho.length = 0" in html, \
        "重连时没清空：断线期间的记号会去认领将来某条同内容的消息，把别人发的那条吃掉"


def test_面板重画有过期闸(js):
    """切页签切得快时两次重画并行，先发的请求可能后回来，
    把【上一页的内容】写进去，而左边导航高亮的是新页。"""
    assert "let paintSeq = 0" in js
    assert js.count("seq !== paintSeq") >= 7, "六个面板 + settle 都要过闸"
    assert "seq = paintSeq" in js, "面板没有默认编号，直接调用（测试）会被当成过期"


def test_浮层滚动时收掉(html):
    """.pop 是 fixed 定位、按打开那一刻的坐标钉死的。
    设置面板内容区能滚，一滚浮层就飘在错的地方。"""
    assert "document.addEventListener('scroll', close, true)" in html, "滚动没关浮层"
    assert "document.removeEventListener('scroll', close, true)" in html, "监听没摘，close 会一直挂着"


def test_触发点算式挡得住乱填(js):
    """parseInt('abc') 是 NaN，一路算下去会显示"= NaN tok"。
    NaN 参与的比较恒假，所以范围检查写成 >= && <= 正好把它一起挡住。"""
    assert "capRaw === '' ? 128000 : parseInt(capRaw, 10)" in js, "空串和乱填没分开"
    assert "if (!(cap >= 1000))" in js, "上限没做下界检查（后端会拒，界面却算得欢）"


def test_窗口值取服务端的(js):
    """saved[].window 对注册表不认识的本地模型是 0，而服务端那边走 100K 兜底。
    前端拿它去算 min(窗口, CAP) 会算出一个比真实值大的触发点。"""
    assert "pf.window" in js
    assert "saved.find(e => e.current)" not in js, "又回去从 saved[] 猜窗口了"
