"""一次性无头调用：python -m mecode -p "问题" —— 跑完整个工具循环、打印最终答案、退出。

给脚本 / CI / 定时任务用（人机交互请用 scripts/tui.py 或 scripts/chat.py）：
    python -m mecode -p "总结这个项目的测试怎么跑"
    python -m mecode -p "把 README 里的错别字改掉" --mode yolo
    python -m mecode -p "..." --cwd D:/some/repo --no-mcp
"""
import argparse
import os


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="mecode",
        description="mecode 无头一次性调用：跑完打印最终答案退出。"
                    "（不带任何参数的 mecode 命令则打开 TUI 界面）")
    ap.add_argument("-p", "--print", dest="prompt", required=True,
                    help="要执行的问题/任务")
    ap.add_argument("--mode", default="auto", choices=["normal", "auto", "yolo"],
                    help="权限档位（默认 auto=项目内编辑自动放行；normal 无头下≈只读；yolo 全放）")
    ap.add_argument("--cwd", default=None,
                    help="项目根（先 chdir 过去再跑，工具相对路径/权限根都以它为准；默认当前目录）")
    ap.add_argument("--no-mcp", action="store_true", help="不连 MCP server（启动更快）")
    args = ap.parse_args(argv)

    if args.cwd:
        os.chdir(args.cwd)      # 程序入口可以名正言顺 chdir（库形态 build_agent 不这么做）
    from .bootstrap import build_agent     # chdir 之后再 import：config/会话归属按目标目录算
    from .provider import ProviderError
    try:
        agent = build_agent(mode=args.mode, mcp=not args.no_mcp)
        print(agent.ask(args.prompt))
    except (ProviderError, RuntimeError) as e:
        # 后端报错（429 限流/401 鉴权/未配置后端…）已是人话——一行错误 + 退出码 1，
        # 别摔整页 traceback（脚本/CI 要能干净地判断失败）
        import sys
        print(f"mecode: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
