"""上下文压缩（compaction）+ 压缩后的上下文重组。

模型无状态、每轮重发全部历史 → 历史太长会撑爆上下文窗口。
做法：在【追加本轮新提问之前】把整段历史摘要掉，并按 Claude Code 风格重组：
  system prompt → [摘要] → [最后一轮 user/assistant 原文] → [最近 N 个 read 原文]
  → [MCP] → [完整对话存盘路径]
本轮新提问由 run_turn 在压缩后追加。system 之后的注入都用 <system-reminder> 包裹。
完整布局见 compact() 的 docstring。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

# ---- 本地 token 估算（usage 缺失时的兜底） ----
# 压缩触发依赖上次请求回的 prompt_tokens；有的 OpenAI 兼容后端不回 usage（尤其流式），
# 那样 context_tokens 一直是 0 → 压缩永不触发、一路涨到撑爆窗口。这里按字符粗估兜底：
#   CJK 宽字符（中日韩表意/假名/谚文/全角）≈ 0.75 token/字；其余（英文/代码）≈ 1 token/3 字符。
# CJK 系数按中文优化词表（Qwen/Kimi/GLM 系，实测 ~1.4 字/token）校准、留 ~8% 余量；
# 老式英文词表（cl100k 系）中文约 1 字/token 会略低估，但 70% 触发阈值的余量兜得住。
_CJK_RE = re.compile(
    "[ᄀ-ᇿ"   # 谚文字母
    "⺀-鿿"    # CJK 部首/康熙部首/CJK 标点/假名/注音/兼容谚文/ExtA/统一表意
    "가-힯"    # 谚文音节
    "豈-﫿"    # CJK 兼容表意
    "︰-﹏"    # CJK 兼容形式
    "＀-￯]"   # 全角形式（全角标点/字母）
)


def estimate_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """请求体的 token 粗估：对话历史（含 system prompt）+ 工具 schema。
    tools 要传——它替代的精确值 prompt_tokens 就含工具 schema，口径要一致。
    ensure_ascii=False 必须：默认的 \\uXXXX 转义会把每个中文字符变 6 个 ASCII 字符、
    按英文系数算成 2 token/字，把中文高估近三倍。"""
    text = json.dumps(messages, ensure_ascii=False)
    if tools:
        text += json.dumps(tools, ensure_ascii=False)
    other = len(_CJK_RE.sub("", text))
    return (len(text) - other) * 3 // 4 + other // 3

# 压缩提示词：Claude Code 模板译成中文（9 段结构照搬：含“报错与修复”和“全部用户消息”两段）。
# <analysis>/<summary>/<example> 标签名保持不译——clean_summary 靠 <analysis>/<summary> 解析。
# 没抄两处：①顶部强力“禁用工具”块——summarize 已 tools=None（无工具可调），末尾另 append 一句即可；
# ②“自定义压缩指令”脚注——mecode 没有 per-project 指令机制。
COMPACT_PROMPT = """你的任务是对目前为止的对话做一份详尽的摘要，特别留意用户的明确诉求和你之前的操作。
这份摘要要充分记录技术细节、代码写法和架构决策，确保后续开发能在不丢失上下文的情况下继续。

在给出最终摘要前，先把你的分析放进 <analysis> 标签里，理清思路、确保覆盖了所有要点。分析时：

1. 按时间顺序逐条分析对话的每条消息和每个环节。对每个环节都要彻底弄清：
   - 用户的明确诉求和意图
   - 你为满足这些诉求所采取的思路
   - 关键决策、技术概念和代码写法
   - 具体细节，例如：
     - 文件名
     - 完整代码片段
     - 函数签名
     - 文件改动
   - 你遇到的报错以及如何修复
   - 特别留意你收到的具体用户反馈，尤其是用户要求你换种做法的地方。
2. 复核技术上的准确性与完整性，把每个必需的部分都写透。

你的摘要应包含以下几个部分：

1. 主要诉求与意图：详细记录用户所有的明确诉求和意图。
2. 关键技术概念：列出讨论到的所有重要技术概念、技术栈和框架。
3. 文件与代码片段：列举查看、修改或新建过的具体文件和代码段。特别关注最近的消息，凡适用处附上完整代码片段，并说明这个文件的读取或改动为什么重要。
4. 报错与修复：列出你遇到的所有报错以及如何修复。特别留意你收到的具体用户反馈，尤其是用户要求你换种做法的地方。
5. 问题解决：记录已解决的问题和仍在排查中的工作。
6. 全部用户消息：列出除工具结果外的所有用户消息。这些对理解用户的反馈和意图变化至关重要。
7. 待办任务：列出用户明确要求你做的、尚未完成的任务。
8. 当前工作：详细描述在本次摘要请求之前你正在做的事，特别关注用户和你最近的消息，凡适用处附上文件名和代码片段。
9. 可选的下一步：列出与你最近工作直接相关的下一步。重要：这一步必须与用户最近的明确诉求、以及你在本次摘要请求前正在做的任务【直接一致】。如果上一个任务已经收尾，那么只有当下一步明确契合用户诉求时才列出。不要在未与用户确认前，擅自开始跑题的、或早已完成的旧诉求。
                       如果有下一步，附上最近对话里的原文引用，准确说明你正在做的任务和停在了哪里。要逐字照抄，以免对任务理解产生漂移。

下面是你的输出该有的结构示例：

<example>
<analysis>
[你的思考过程，确保所有要点都被彻底而准确地覆盖]
</analysis>

<summary>
1. 主要诉求与意图：
   [详细描述]

2. 关键技术概念：
   - [概念 1]
   - [概念 2]
   - [...]

3. 文件与代码片段：
   - [文件名 1]
      - [这个文件为什么重要]
      - [对该文件所做改动的摘要，如有]
      - [重要代码片段]
   - [文件名 2]
      - [重要代码片段]
   - [...]

4. 报错与修复：
    - [报错 1 的详细描述]：
      - [你是如何修复的]
      - [用户对该报错的反馈，如有]
    - [...]

5. 问题解决：
   [已解决问题与仍在排查工作的描述]

6. 全部用户消息：
    - [非工具结果的用户消息，详细列出]
    - [...]

7. 待办任务：
   - [任务 1]
   - [任务 2]
   - [...]

8. 当前工作：
   [当前工作的准确描述]

9. 可选的下一步：
   [可选的下一步]

</summary>
</example>

请基于目前为止的对话给出你的摘要，遵循上述结构，确保回复准确、详尽。"""


def _render_history(messages: list[dict]) -> str:
    """把一段消息渲染成纯文本，喂给模型做摘要。"""
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if m.get("tool_calls"):
            calls = ", ".join(
                f"{tc['function']['name']}({tc['function']['arguments']})"
                for tc in m["tool_calls"]
            )
            content = (content + f" [调用工具: {calls}]").strip()
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _extract_tag(text: str, tag: str) -> str | None:
    """取 <tag>...</tag> 之间的内容；找不到成对标签就返回 None。"""
    open_t, close_t = f"<{tag}>", f"</{tag}>"
    start = text.find(open_t)
    if start == -1:
        return None
    start += len(open_t)
    end = text.find(close_t, start)
    if end == -1:
        return None
    return text[start:end].strip()


def clean_summary(raw: str) -> str:
    """清洗模型产出的摘要（对应 ClawCode 的 format_compact_summary）。

    CC 模板会让模型输出 <analysis>草稿</analysis> + <summary>正文</summary>。
    只有 <summary> 正文该进压缩历史；<analysis> 是思考草稿，不能污染上下文。
    （注：思考若走 reasoning 通道，summarize 只收 content，这里也能优雅处理——
      没有标签就原样返回。这就是为什么对 mecode 这步是实打实必须、而非空操作。）
    """
    summary = _extract_tag(raw, "summary")
    if summary is not None:
        return summary
    # 没有 <summary> 标签：模型没按格式包——退一步，至少把 <analysis> 整块剥掉
    start, end = raw.find("<analysis>"), raw.find("</analysis>")
    if start != -1 and end != -1:
        raw = raw[:start] + raw[end + len("</analysis>"):]
    return raw.strip()


_CONTINUATION_PREAMBLE = "本会话由一段因超出上下文而被压缩的对话续接而来，下面的摘要覆盖了较早的部分。"
_RECENT_VERBATIM_NOTE = "最近一轮对话与最近读取的文件已按原文保留。"
# 续接指令：对齐 Claude Code 的 "Continue the conversation... resume directly..."
_RESUME_NOTE = ("请从上次中断处继续，不要向用户额外提问。直接接着做——"
                "不要确认或复述这段摘要、不要复述刚才在做什么、也不要用“我来继续”之类的开场白；"
                "就当这次压缩从未发生，接着完成手头的任务。")


def continuation_banner(summary: str, transcript_path: str | None = None) -> str:
    """压缩后的“续接提示条”（对齐 Claude Code）：摘要 → 完整存档指针 → 续接指令。

    顺序与 CC 一致：摘要正文之后，先告诉模型"要更早的细节就去读完整存档(路径)"，
    再给"直接续接、别复述摘要、别寒暄"的指令。transcript_path 即记忆外置的指针。
    """
    parts = [_CONTINUATION_PREAMBLE, "", summary, "", _RECENT_VERBATIM_NOTE]
    if transcript_path:
        parts.append("如需压缩前的具体细节（确切的代码片段、报错信息、你生成过的内容），"
                     f"可读取完整对话存档：{transcript_path}")
    parts.append(_RESUME_NOTE)
    return "\n".join(parts)


def _reminder(text: str) -> str:
    """把内容包进 <system-reminder> —— 注入上下文的统一标记。"""
    return f"<system-reminder>\n{text}\n</system-reminder>"


def _last_content(old: list[dict], role: str) -> str | None:
    """old 里最后一条指定 role 的正文；模式声明不冒充最近用户任务，assistant 只取 content。"""
    for m in reversed(old):
        if m.get("role") == role and "_mode" not in m:
            text = (m.get("content") or "").strip()
            return text or None
    return None


def _rescue_key(name: str, args: str) -> tuple:
    """救援去重的键 = (工具名, 归一化路径, offset, limit)，凑不出路径就退回参数原文。

    不拿参数原文当键：同一文件两种写法、参数顺序不同都会算成两个键（都实测撞到过）。
    按【区间】而非按【文件】去重（曾按文件，实测推翻）：按文件时正在攻坚的那个文件只留
    最近一个窗口，压缩后头 3 轮 read_file 频率飙到平均的 2.26 倍——模型立刻重读回来。

    第二个返回值是归一化路径（取不出则 None），给 _rescue_reads 数"几个不同文件"用。
    """
    try:
        a = json.loads(args) or {}
        p = a.get("path")
    except (json.JSONDecodeError, TypeError, AttributeError):
        a, p = {}, None
    if not p:
        return (name, args), None
    try:
        norm = Path(p).resolve().as_posix()
    except OSError:                        # 路径非法/不可解析 → 退回原样，别抛
        norm = str(p)
    return (name, norm, a.get("offset"), a.get("limit")), norm


def _rescue_reads(
    old: list[dict], read_tools: tuple[str, ...], count: int,
    max_tokens: int = 0, max_files: int = 0,
) -> list[tuple[str, str, str]]:
    """倒序单趟捞最近读过的【max_files 个文件】的全部区间。最早在前返回。

    三把闸各管一件事，都可配 0 关掉：
      max_files  —— 最多涉及几个不同文件。倒着走，已收满后再遇到【新】文件就跳过，
                    但已收文件的更早区间照收——即"最近 N 个文件的全部读取都留下"。
      max_tokens —— 总量 token 预算，从最新往回收，耗尽即停。【至少保留一条】，
                    否则压缩后模型手里一份原文都没有。
      count      —— 条目数上限，默认 0（不限）。留给测试。

    关键：倒着走时一个调用的【结果(tool 消息)】先出现、【调用(assistant 消息)】后出现
    （正序“调用→结果”反过来就是“结果→调用”）。所以先把见到的结果暂存 id→内容，
    遇到对应调用时内容已在手边，直接取、去重。
    """
    result_by_id: dict[str, str] = {}      # 暂存：tool_call_id → 文件原文
    seen: set[tuple] = set()               # 已收的区间键，去重留最新
    files: list[str] = []                  # 已收的不同文件（按从新到旧的相遇顺序）
    deduped: list[tuple[str, str, str]] = []
    used = 0
    for m in reversed(old):
        if m.get("role") == "tool":
            result_by_id[m.get("tool_call_id")] = m.get("content") or ""
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name, args = fn.get("name") or "", fn.get("arguments") or ""
            key, path = _rescue_key(name, args)
            if name in read_tools and key not in seen and tc.get("id") in result_by_id:
                is_new_file = path is not None and path not in files
                if is_new_file and max_files and len(files) >= max_files:
                    continue                            # 文件数已满：跳过新文件，继续收已收文件的更早区间
                content = result_by_id[tc.get("id")]
                if max_tokens and deduped:              # 第一条无条件收，之后才看预算
                    cost = estimate_tokens([{"role": "tool", "content": content}])
                    if used + cost > max_tokens:
                        return list(reversed(deduped))  # 预算耗尽：更早的不再保留
                    used += cost
                seen.add(key)
                if is_new_file:
                    files.append(path)
                deduped.append((name, args, content))
                if count and len(deduped) >= count:
                    break
        if count and len(deduped) >= count:
            break
    return list(reversed(deduped))         # 还原成最早在前


def compact(
    summarize: Callable[[list[dict]], str],
    messages: list[dict],
    *,
    read_tools: tuple[str, ...] = ("read_file",),
    rescue_count: int = 0,
    rescue_max_tokens: int = 0,
    rescue_max_files: int = 0,
    transcript_pointer: str | None = None,
    on_compacted: Callable[[list[dict]], None] | None = None,
    claude_md: str | None = None,
    mcp_instructions: str | None = None,
) -> list[dict] | None:
    """把历史重组成 Claude Code 风格的压缩布局；没有可压的历史则返回 None。

    设计前提：本函数在【追加本轮新提问之前】被调用（见 Agent.run_turn），
    所以 messages 里还没有“当前待回答的问题”——它由 run_turn 在压缩后再追加。
    因此整段历史都交给摘要；同时把最后一轮 user/assistant 原文【硬性保留】，
    防摘要模型漂移（不照实引用最后一轮提问）时有原文兜底。

    布局（system 之后全是带 <system-reminder> 的注入）：
      [system prompt]
      [1 user] claude.md + 压缩摘要 + 完整存档指针 + 续接指令（对齐 Claude Code）
      [2 user/assistant] 压缩前最后一轮 user/assistant 原文（assistant 丢 tool_calls）
      [3 user×2N] 最近 N 个 read 的“调用提示 + 文件原文”
      [4 user] MCP instructions（可选）
    （当前 user 提问随后由 run_turn 追加，不在本函数内）

    依赖注入（保持 compact 纯逻辑、可测）：
      summarize          —— 吃 messages 吐摘要文本（agent 用 provider 实现）
      transcript_pointer —— 完整历史的指针（=transcript.jsonl 路径），塞进摘要块当"存档指针"。
                            单一真相下全量历史本就逐条在 transcript 里，这里只需指针、不再另存。
      on_compacted       —— 压缩完成回调 (压缩后镜像)；agent 用它把 compaction marker 写进
                            transcript（marker 带镜像 → resume 时 fold 还原工作记忆）。摘要无需单独
                            传出：它已嵌在镜像第 2 条的续接块里，镜像即唯一所需。
    """
    head = 1 if messages and messages[0].get("role") == "system" else 0
    old = messages[head:]            # system 之后的全部历史（当前提问还没 append）
    if not old:
        return None

    # 摘要：整段历史渲染成文本塞进 <conversation> + CC 模板 + 禁用工具（双保险）
    raw_summary = summarize([
        {"role": "user", "content":
            "以下是需要你摘要的对话历史：\n\n"
            "<conversation>\n" + _render_history(old) + "\n</conversation>\n\n"
            + COMPACT_PROMPT
            + "\n\n重要：不要调用任何工具或函数，只输出摘要文本。"},
    ])
    summary_text = clean_summary(raw_summary)   # 剥 <analysis>、抽 <summary>
    # 摘要空 = 这次压缩【没成】：流中途断了、后端回了空、或者用户中途按了停止。
    # 不拦的话下面照样把整段历史换成一个空摘要块——上下文当场清零，比不压缩糟得多。
    # 返回 None（= 本次不压），调用方保持 messages 原样。
    if not summary_text.strip():
        return None

    rebuilt: list[dict] = list(messages[:head])  # system prompt

    # 1) claude.md + 摘要(含存档指针 + 续接指令) → 第一个 user，各自 <system-reminder> 包
    blocks = []
    if claude_md:
        blocks.append(_reminder(claude_md))
    blocks.append(_reminder(continuation_banner(summary_text, transcript_pointer)))
    rebuilt.append({"role": "user", "content": "\n".join(blocks)})

    # 2) 压缩前最后一轮 user / assistant 原文（assistant 只留 content），各自 reminder 包。
    #    硬性保留这一轮——摘要可能漂移，留原文兜底。
    last_user = _last_content(old, "user")
    if last_user:
        rebuilt.append({"role": "user", "content": _reminder(last_user)})
    last_asst = _last_content(old, "assistant")
    if last_asst:
        rebuilt.append({"role": "assistant", "content": _reminder(last_asst)})

    # 3) 最近 N 个 read：每个拆成“调用提示 + 文件原文”两条 user 消息
    for name, args, content in _rescue_reads(old, read_tools, rescue_count,
                                             rescue_max_tokens, rescue_max_files):
        rebuilt.append({"role": "user", "content":
            _reminder(f"调用了 {name} 工具，输入如下：{args}")})
        rebuilt.append({"role": "user", "content": _reminder(content)})

    # 4) MCP instructions（mecode 暂无 → 通常 None，跳过）
    if mcp_instructions:
        rebuilt.append({"role": "user", "content": _reminder(mcp_instructions)})

    # 压缩完成：把 marker（带压缩后镜像）写进 transcript。镜像建好才回调，
    # resume 时 fold 到这条 marker 即取回这份工作记忆（= 上次关闭时的 messages[]）。
    if on_compacted is not None:
        on_compacted(rebuilt)
    return rebuilt
