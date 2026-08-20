"""前端两块纯 JS（markdown 渲染器、设置面板）的测试，经 node 跑。

为什么用 node 而不是在 Python 里做：这两块是浏览器里跑的真代码，
拿 Python 重写一遍断言等于测了另一个东西。node 不在就跳过——
它不是 mecode 的运行依赖，只是开发期的测试工具。

对应的 JS 在 tests/js/ 下，也可以单独 `node tests/js/markdown.test.js` 跑。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent / "js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跳过前端测试")


@pytest.mark.parametrize("script", sorted(p.name for p in JS_DIR.glob("*.test.js")))
def test_前端(script):
    r = subprocess.run(["node", str(JS_DIR / script)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    # 失败时把 node 的输出整段带出来：只报 returncode 的话还得自己去跑一遍才知道哪条挂了
    assert r.returncode == 0, f"{script} 失败：\n{r.stdout}\n{r.stderr}"
