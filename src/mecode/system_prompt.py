"""组装系统提示词（system prompt）。

system prompt 是【拼装】出来的：
  身份+工作方式 + 项目说明(CLAUDE.md) + 可选额外说明 + 环境信息(动态)

顺序讲究：静态内容在前、动态环境（时间/目录）放最后——前缀稳定才好命中 prompt 缓存。
环境信息尤其重要：模型无状态，不知道“现在/这里”，得在 prompt 里告诉它。
"""
from __future__ import annotations

import datetime
import platform
from pathlib import Path

# 身份 + 工作方式 + 行为准则（静态部分）
BASE = """你是一个编程助手，可以用工具读、改、运行、搜索代码，帮用户完成开发任务。

工作方式：
- 探索：用 glob 按名字找文件、grep 搜内容、read_file 看文件，别凭空猜。
- 动手时机：从报错现场 / 失败的测试和最可能的实现文件入手；只在证据要求时才继续读更多文件。能提出具体假设就动手改、跑测试检验假设——这比继续读代码收敛得快。
- 完成判据：你跑的测试通过 ≠ 问题解决——测试验证的是代码正确，不是问题本身被修好；确实没办法验证就如实说明，别当作已完成。要求里提到的情形要全部覆盖，别只修好举例的那一个就收工。
- 修改：改局部用 edit_file（先 read_file 确认要改的原文）；新建或整体重写用 write_file。优先改现有文件而不是新建。临时脚本只用来复现问题或验证改动；如果发现自己在反复造探测脚本却迟迟没动源码，说明方向错了，回到代码上。
- 验证/执行：用 bash 跑命令（测试、git、装包等）。但读文件用 read_file、搜内容用 grep、找文件用 glob，别用 bash 的 cat/grep/find 代替——专用工具有行号/翻页/结构化输出。
- 联网查资料：web_search 搜（标题+URL+摘要）→ web_fetch 读全文。适用于查文档/找仓库/看报错解法；本地代码问题优先用本地工具，别上网猜。
- read_file 输出的行号只用于定位；传给 edit_file 的 old_string 不要带行号。
- 规划复杂任务：任务需 ≥3 个步骤、或用户一次给了多项时，先用 task_create 把计划拆成任务（每条一次调用）来规划和跟踪进度；开始做某条前用 task_update 标 in_progress、做完【立即】标 completed（别攒着批量标），同一时刻只保持一个任务 in_progress。单一直白的小任务别建清单，直接做。
- 委派子 agent：要【并行做多件独立的事】、或某段活会【读一大堆文件/多步试错、把主上下文撑爆】时，用 subagent 把它外包出去（子 agent 有自己干净的上下文，你只拿回一段总结）。要它的结果才能往下走→前台（同一条消息里发多个 subagent 调用会并发、等齐所有结果）；真正独立又不急着要→设 background=true 转后台、完成再通知你。别为一两步就能做完的小事派子 agent（那还不如自己做），也别自己再重复子 agent 已经在做的活。

行为准则：
- 回答简洁，直接给结论和关键改动，不啰嗦。
- 拿不准就先查（读文件 / 搜代码 / 跑命令），不要编造。
- 对话中出现 <system-reminder> 标签包裹的内容是按当前状态注入的重要上下文/指令/提示（不是用户发的），请当作高优先级信息遵从，别忽略。"""

# 指令安全 + 行为边界（常驻，所有模式都注入）。硬安全在 permission.py 落地；这里是行为层软引导。
#- 只在授权范围内协助安全测试、防御性安全、CTF 与教学；拒绝破坏性攻击、大规模伤害、隐蔽后门、规避检测等恶意用途。
SAFETY = """安全与边界：
- 难以撤销或对外产生影响的动作（删除/覆盖文件、git push、发布、对外发消息等），先说明意图再做，除非用户已明确授权。
- 不过度设计：实现用户要求的即可，别擅自扩大改动范围、别加没被要求的抽象或功能。
- 忠实汇报：测试失败就照实说、跳过的步骤讲清楚，别粉饰。"""

# 项目级指令文件：按顺序找第一个存在的
PROJECT_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md")


def load_project_context(root: Path | None = None) -> str | None:
    """从项目根读项目级指令（CLAUDE.md / AGENTS.md），有就返回内容，没有返回 None。"""
    root = root or Path.cwd()
    for name in PROJECT_CONTEXT_FILES:
        f = root / name
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return f"# 项目说明（来自 {name}）\n{text}"
    return None


def environment_block(session_dir: Path | None = None) -> str:
    """动态环境信息——每次创建会话时按真实情况生成。
    session_dir：本会话的存储目录（对话日志 transcript.jsonl、外置的工具大输出 tool_outputs/ 都在
    这下面）——告诉模型，它才答得上"日志在哪"、找得回被截断外置的大输出。环境块本就是会话级
    内容（含创建时间），加这行零缓存代价。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 环境信息",
        f"- 会话创建时间：{now}",
        f"- 工作目录：{Path.cwd().as_posix()}",   # 正斜杠：bash(C:/...) 和各工具都认，避免模型复制反斜杠进 bash 翻车
        f"- 操作系统：{platform.system()} {platform.release()}",
    ]
    if session_dir is not None:
        lines.append(f"- 会话目录：{Path(session_dir).as_posix()}/"
                     "（本会话的对话日志 transcript.jsonl、外置的工具大输出 tool_outputs/ 在此）")
    return "\n".join(lines)


def build_system_prompt(extra: str = "", project_context: str | None = None,
                        session_dir: Path | None = None) -> str:
    """拼出最终 system prompt：静态在前、环境放最后（利于 prompt 缓存）。

    project_context 默认（None）自动从项目根读 CLAUDE.md；显式传入则用传入的
    （传空串 "" 表示明确不要项目说明、也不自动加载，方便测试）。

    技能索引段：按需加载的领域流程（skills.py 渐进式披露）——索引常驻、全文模型自己 read_file。
    启停变更【新会话生效】（本函数只在会话创建时调，天然满足；当前会话前缀不动、不击穿缓存）。

    【与模式无关】：模式行为段【不】进 system prompt——否则切模式改前缀会击穿 prompt 缓存。改由 run_turn
    每轮把当前模式的提示词以 <system-reminder> 注入最新消息（见 agent._inject_mode_reminder）。"""
    from .skills import build_skills_prompt, discover_skills   # 局部 import：避免顶层循环
    if project_context is None:
        project_context = load_project_context()
    parts = [BASE, SAFETY]
    skills_part = build_skills_prompt(discover_skills(Path.cwd()))
    if skills_part:
        parts.append(skills_part)
    if project_context:
        parts.append(project_context)
    if extra:
        parts.append(extra)
    parts.append(environment_block(session_dir))   # 动态环境放最后
    return "\n\n".join(parts)
