"""`mecode` 全局命令入口（pyproject [project.scripts]，pip install -e . 装出）。

    mecode              # 打开 TUI（textual 界面）
    mecode -p "问题"     # 无头一次性：跑完打印最终答案退出（参数同 python -m mecode，见 __main__.py）
"""
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) > 1:                  # 带参数（-p/--mode/--cwd/--no-mcp/--help）→ 无头形态
        from .__main__ import main as headless
        headless()
        return
    # 无参数 → TUI。tui.py 是应用层、留在仓库 scripts/ 下不进包——editable 安装（pip install -e .）
    # 时包目录就是仓库源码，能定位到；真轮子安装（无仓库）则给明确指引。
    tui = Path(__file__).resolve().parent.parent.parent / "scripts" / "tui.py"
    if not tui.is_file():
        sys.exit("找不到 scripts/tui.py——TUI 入口在仓库里，请在仓库根用 pip install -e . 的开发安装方式。")
    import runpy
    sys.path.insert(0, str(tui.parent))
    runpy.run_path(str(tui), run_name="__main__")   # 触发 tui.py 的 __main__ 守卫：app.run() + 退出关 MCP


if __name__ == "__main__":
    main()
