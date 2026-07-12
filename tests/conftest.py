"""让 tests 能 `from mecode...` 导入：把 src 加进 sys.path。

这样不必安装包就能跑 `pytest`。和 scripts/chat.py 里那句 path 注入同理。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
