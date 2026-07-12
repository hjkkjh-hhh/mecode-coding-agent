"""跟你的 harness 对话。

用法:
    python scripts/chat.py                 # 进入 REPL，多轮对话
    python scripts/chat.py 帮我查长沙天气    # 单次提问

【显示】全在这里（render）：agent 只产出事件，怎么显示是调用方（应用层）的事。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mecode.bootstrap import build_agent
from mecode.config import agent_config, current_backend
from mecode.events import Notice, ReasoningDelta, TextDelta, ToolResult, ToolStarted
from mecode.permission import pattern_for
from mecode.session import SessionStore

GRAY, RED, RESET = "\033[90m", "\033[91m", "\033[0m"


def _is_error_result(result: str) -> bool:
    """工具结果是否表示出错（用于显示标红）。各工具报错以“错误：”开头，
    execute 兜异常含“执行出错：”，bash 非零退出含“(exit code:”。"""
    return (result.startswith("错误：")
            or "执行出错：" in result
            or "(exit code:" in result)


def render(events) -> str:
    """消费 agent 的事件流，负责显示；返回最终答案文本。
    思考末尾换行的清洗、颜色、工具行打印——这些“怎么显示”的活全在这层。"""
    answer = ""
    pending_ws, had_reasoning, answering = "", False, False
    for ev in events:
        match ev:
            case ReasoningDelta(text=t):                # 思考=灰色，清洗末尾换行
                had_reasoning = True
                s = pending_ws + t
                visible = s.rstrip("\n")
                pending_ws = s[len(visible):]
                if visible:
                    sys.stdout.write(f"{GRAY}{visible}{RESET}")
                    sys.stdout.flush()
            case TextDelta(text=t):                     # 正文
                if not answering:                       # 思考→答案的边界
                    if had_reasoning:
                        sys.stdout.write("\n\n")
                    answering = True
                    t = t.lstrip("\n")
                answer += t
                sys.stdout.write(t)
                sys.stdout.flush()
            case ToolStarted(name=name, arguments=args):
                print(f"\n{GRAY}🔧 {name}({args}){RESET}")
            case ToolResult(name=name, result=result):
                color = RED if _is_error_result(result) else GRAY   # 出错标红
                print(f"\n{color}🔧 {name} 结果\n{result}{RESET}")
            case Notice(text=text):
                print(f"\n{GRAY}（{text}）{RESET}")
    return answer


def _ask_permission(tool, args):
    """工具要执行 ask 类动作时问用户（CLI 版）。返回 once / always / deny。"""
    print(f"\n{RED}⚠ 工具请求执行：{tool}  {args}{RESET}")
    ans = input(f"允许？[y]一次 / [a]总是允许 {pattern_for(tool, args)} / [其他]拒绝 > ").strip().lower()
    return {"y": "once", "a": "always"}.get(ans, "deny")


def _fmt_time(ts):
    return time.strftime("%Y年%m月%d日 %H:%M", time.localtime(ts)) if ts else "未知时间"


# 组装全走 build_agent（bootstrap 工厂，headless 同款）：本地+记忆+MCP 工具、权限、会话、MCP atexit 收尾。
# mode="normal" = chat 的既有语义：需要审的都问（审批走 _ask_permission）。
agent = build_agent(mode="normal", ask_permission=_ask_permission)
store = agent.store
_b = current_backend()
tool_names = [t["function"]["name"] for t in agent.tools.schemas()]
print(f"后端 {_b.base_url}  模型 {_b.model}  工具 {tool_names}")
if agent.mcp_clients:
    print(f"MCP {', '.join(c.name for c in agent.mcp_clients)}")
print(f"会话 {store.dir.as_posix()}")

if len(sys.argv) > 1:                       # 命令行带了问题 → 单次
    render(agent.run_turn(" ".join(sys.argv[1:])))
    print()
else:                                        # 否则进 REPL
    print("输入问题（exit 退出；/rl 续最近会话；/rs 列会话、/rs N 续第 N 个）")
    while True:
        try:
            user = input("\n你> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        cmd = user.strip()
        if cmd in ("exit", "quit"):
            break
        # 续会话：/rl·/resume latest 续最近；/rs·/resume session [N] 列/续第 N 个
        if cmd.startswith("/resume "):                  # 别名归一化成 /rl、/rs[ N]
            rest = cmd[len("/resume "):].strip()
            if rest == "latest":
                cmd = "/rl"
            elif rest.startswith("session"):
                cmd = "/rs" + rest[len("session"):]     # "/resume session 2" → "/rs 2"
        if cmd == "/rl" or cmd.startswith("/rs"):
            sessions = SessionStore.list_sessions(root=agent_config.session_root)[:10]   # 最近 10 个
            if not sessions:
                print(f"{GRAY}（本项目还没有历史会话）{RESET}")
                continue
            cur = store.session_id
            arg = cmd[3:].strip()                       # "/rs 2" → "2"，"/rs" → ""
            if cmd == "/rl":                            # 续最近——排除当前会话
                others = [m for m in sessions if m.get("session_id") != cur]
                if not others:
                    print(f"{GRAY}（除当前会话外没有其他历史会话）{RESET}")
                    continue
                meta = others[0]
            elif arg.isdigit() and 0 <= int(arg) - 1 < len(sessions):
                meta = sessions[int(arg) - 1]           # 续第 N 个
            elif arg:                                   # /rs 带了非法序号
                print(f"{GRAY}（序号无效，用 /rs 看清单）{RESET}")
                continue
            else:                                       # "/rs" 无参 → 列清单（当前标注）
                for i, m in enumerate(sessions, 1):
                    tag = "  (当前)" if m.get("session_id") == cur else ""
                    print(f"{GRAY}[{i}] {m.get('title', '(无标题)')}{tag}  "
                          f"({_fmt_time(m.get('updated_at'))}){RESET}")
                continue
            agent = build_agent(mode="normal", session_id=meta["session_id"],
                                registry=agent.tools,      # 复用共享工具表（本地 + MCP + 记忆），不重连 MCP
                                ask_permission=_ask_permission)
            store = agent.store
            print(f"{GRAY}（已续会话「{meta.get('title', '')}」，{len(agent.messages)} 条上下文）{RESET}")
            continue
        print("助手> ", end="")
        gen = agent.run_turn(user)
        try:
            render(gen)
        except KeyboardInterrupt:        # 打断恰好落在 render（非生成器）时的兜底
            gen.close()                  # 触发 run_turn 的 GeneratorExit → 回滚残缺状态
            print(f"\n{GRAY}（已打断）{RESET}")
        print()
