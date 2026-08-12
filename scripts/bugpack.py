"""bugpack —— 把"刚才那次跑挂了"打包成一个可交接的 zip。

评测的瓶颈从来不是跑，是上报：让人手动去 ~/.mecode 里翻 uuid 目录、找 transcript、
再写一份说明，多数人会直接放弃，于是问题就永远看不见。这个脚本把这一步压成一条命令。

产物 <输出目录>/<日期>-<姓名>-<序号>.zip 里有：
  report.md          —— 五栏说明（脚本问、你答，不用记格式）
  transcript.jsonl   —— 现场全量（单一真相，压缩也只在末尾加 marker，不会丢历史）
  session.json       —— 会话头（cwd / 轮数 / 时间）
  tool_outputs/      —— 被外置的超长工具输出（截断标记里的指针指向这里）

用法（在任意目录）：
    python scripts/bugpack.py            # 打包最近一次会话
    python scripts/bugpack.py --list     # 先看看有哪些会话，再挑
    python scripts/bugpack.py --session <uuid>

刻意不做的：不自动上传（内网环境各人网络不同，交付方式由组里定）、不解析
transcript 内容（解析要跟着格式走，格式一变脚本就烂，而 zip 永远能打开）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_ROOT = Path("~/.mecode").expanduser()          # 与 SessionStore 的默认 root 一致
DEFAULT_OUT = Path("~/mecode-bugs").expanduser()


def find_sessions(root: Path) -> list[tuple[float, Path]]:
    """扫出所有会话目录，按最后写入时间从新到旧。

    以 transcript.jsonl 的 mtime 排序而不是目录 mtime：目录 mtime 会被 tool_outputs 等
    子目录的写入带偏，transcript 才代表"这次对话最后说话是什么时候"。
    """
    found: list[tuple[float, Path]] = []
    for tr in root.glob("projects/*/sessions/*/transcript.jsonl"):
        try:
            found.append((tr.stat().st_mtime, tr.parent))
        except OSError:
            continue
    found.sort(key=lambda x: x[0], reverse=True)
    return found


def describe(session_dir: Path) -> str:
    """一行摘要，给 --list 和确认提示用。读不出来不报错——只是展示，不值得中断流程。"""
    info = {}
    meta = session_dir / "session.json"
    if meta.is_file():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
    tr = session_dir / "transcript.jsonl"
    try:
        lines = sum(1 for _ in tr.open(encoding="utf-8"))
    except OSError:
        lines = 0
    when = _dt.datetime.fromtimestamp(tr.stat().st_mtime).strftime("%m-%d %H:%M")
    title = info.get("title") or info.get("cwd") or "(无标题)"
    return f"{when}  {lines:>4} 条  {session_dir.name[:8]}  {title}"


def ask(prompt: str, default: str = "") -> str:
    hint = f"（回车＝{default}）" if default else ""
    try:
        got = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(1)
    return got or default


def build_report(answers: dict, session_dir: Path) -> str:
    return f"""# mecode 问题上报

| 项 | 内容 |
|---|---|
| 上报人 | {answers['who']} |
| 模型 | {answers['model']} |
| 维度 | {answers['dim']} |
| **必现 / 偶发** | **{answers['repro']}** |
| 会话 | `{session_dir.name}` |
| 时间 | {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} |

## 我想干什么
{answers['want']}

## 我期望它怎么做
{answers['expect']}

## 它实际怎么做的
{answers['actual']}

## 分类（勾一个，不确定就留空，分诊时一起定）
- [ ] harness 挂了（报错 / 卡死 / 循环退不出来）
- [ ] 模型没听懂（指令给了但没照做）
- [ ] 工具行为不对（改错文件 / 读不到 / 权限判断怪）
- [ ] 体验难受（能用但别扭）
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="把一次 mecode 会话打包成上报 zip")
    ap.add_argument("--list", action="store_true", help="列出最近的会话后退出")
    ap.add_argument("--session", metavar="UUID", help="指定会话（可只写前几位）")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="mecode 数据根目录")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="zip 输出目录")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    sessions = find_sessions(root)
    if not sessions:
        print(f"× 在 {root} 下没找到任何会话。")
        print("  跑过 mecode 吗？如果改过数据目录，用 --root 指定。")
        return 1

    if args.list:
        for _, d in sessions[:20]:
            print("  " + describe(d))
        return 0

    if args.session:
        hit = [d for _, d in sessions if d.name.startswith(args.session)]
        if not hit:
            print(f"× 没有以 {args.session} 开头的会话，先跑 --list 看看。")
            return 1
        session_dir = hit[0]
    else:
        session_dir = sessions[0][1]

    print("将打包这次会话：")
    print("  " + describe(session_dir))
    if ask("对吗？(y/n)", "y").lower() not in ("y", "yes", ""):
        print("那就先 python scripts/bugpack.py --list 挑一个，再用 --session 指定。")
        return 1

    print("\n下面五个问题，一句话就行（这五栏决定这个问题修不修得动）：\n")
    answers = {
        "who": ask("1/7 你的名字"),
        "model": ask("2/7 用的哪个模型（kimi / deepseek / glm / minimax）"),
        "dim": ask("3/7 你认领的维度（长会话 / 打断 / 多文件 / 权限 / 大文件 / 其他）"),
        "want": ask("4/7 你想让它干什么"),
        "expect": ask("5/7 你期望它怎么做"),
        "actual": ask("6/7 它实际怎么做的"),
        "repro": ask("7/7 又跑了一遍，还这样吗？(必现 / 偶发 / 没再跑)", "没再跑"),
    }

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_dt.date.today():%Y%m%d}-{answers['who'] or 'anon'}"
    n = 1
    while (out_dir / f"{stem}-{n}.zip").exists():       # 同一天同一人可以报多个
        n += 1
    zip_path = out_dir / f"{stem}-{n}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.md"
        report.write_text(build_report(answers, session_dir), encoding="utf-8")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(report, "report.md")
            for name in ("transcript.jsonl", "session.json", "tasks.json", "plan.md"):
                f = session_dir / name
                if f.is_file():
                    z.write(f, name)
            outs = session_dir / "tool_outputs"
            if outs.is_dir():
                for f in sorted(outs.iterdir()):
                    if f.is_file():
                        z.write(f, f"tool_outputs/{f.name}")

    size_kb = zip_path.stat().st_size / 1024
    print(f"\n✓ 已打包：{zip_path}")
    print(f"  大小 {size_kb:.0f} KB —— 如果只有几 KB，多半是会话没真跑起来，看一眼再交。")
    print("  把这个 zip 发到组里约定的地方就完事了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
