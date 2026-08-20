"""`mecode` 全局命令入口（pyproject [project.scripts]，pip install -e . 装出）。

    mecode              # 打开 TUI（textual 界面）
    mecode desk         # 打开桌面端：起本地服务 + 自动开浏览器（--no-open 则只打印地址）
    mecode -p "问题"     # 无头一次性：跑完打印最终答案退出（参数同 python -m mecode，见 __main__.py）

tui.py / deskserve.py 都是应用层，留在仓库 scripts/ 下【不进包】——editable 安装
（pip install -e .）时包目录就是仓库源码，能定位到；真轮子安装（无仓库）则给明确指引。
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


def _run_script(name: str, args: list[str]) -> None:
    """把 scripts/<name> 当成 __main__ 跑起来，argv 换成给它的那一份。

    换 sys.argv 是必须的：这些脚本自己用 argparse 读参数，不换的话它们会看到
    `mecode desk --port 8360` 里的 `desk`，直接报"无法识别的参数"。"""
    path = _SCRIPTS / name
    if not path.is_file():
        sys.exit(f"找不到 scripts/{name}——它在仓库里，请在仓库根用 pip install -e . 的开发安装方式。")
    import runpy
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path), *args]
    runpy.run_path(str(path), run_name="__main__")   # 触发脚本的 __main__ 守卫


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "desk":
        rest = argv[1:]
        # `mecode desk` 就是"开箱即用"那条：默认把浏览器一起开了，不想开就 --no-open。
        # 端口不给就让系统分配——固定端口在同时开两个工作区时会撞上。
        if "--no-open" in rest:
            rest = [a for a in rest if a != "--no-open"]
        elif "--open" not in rest:
            rest = [*rest, "--open"]
        return _run_script("deskserve.py", rest)
    if argv:                               # 带参数（-p/--mode/--cwd/--no-mcp/--help）→ 无头形态
        from .__main__ import main as headless
        return headless()
    _run_script("tui.py", [])              # 无参数 → TUI


if __name__ == "__main__":
    main()
