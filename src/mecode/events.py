"""Provider 吐给上层的【事件】。

模型的原始流式响应是一堆碎片（reasoning 碎片 + tool_calls 碎片，需要自己拼）。
Provider 的职责就是把这些"脏"碎片整理成下面这几种干净事件，
主循环只管消费事件，不用碰原始 SSE。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReasoningDelta:
    """思考通道的一小片（原始流里 delta.reasoning）。仅供显示，不进答案。"""
    text: str


@dataclass
class TextDelta:
    """可见答案的一小片（原始流里 delta.content）。"""
    text: str


@dataclass
class ToolCall:
    """一个【完整的】工具调用——Provider 已把跨多片的 arguments 拼好并解析成 dict。

    即把流式下分多片到达的同一个工具调用，按 index 累加 arguments 后的成品。
    """
    id: str
    name: str
    arguments: dict


@dataclass
class Done:
    """本轮模型输出结束。reason 来自 finish_reason：
    - "tool_calls" → 模型要调工具（主循环去执行、喂回、继续）
    - "stop"       → 模型给了文字答案（主循环 break，本轮结束）
    思考不走这里：各家统一从 reasoning_content 流出（见 ReasoningDelta），上层自行累积。"""
    reason: str


@dataclass
class Usage:
    """本次请求的 token 用量（模型精确统计）。
    prompt_tokens = 我们发出去的（system+messages+tools）的真实大小 → 压缩看它。"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: int = 0


# 下面三个是 Agent 层产出的事件（Provider 不产出），供上层显示工具执行与提示。
@dataclass
class ToolStarted:
    """Agent 开始执行某个工具。id=对应的 tool_call_id，供 UI 把结果配回同一个工具块
    （并发子 agent 时多个工具同时在跑，只靠"当前工具"单指针会串位）。"""
    name: str
    arguments: dict
    id: str = ""


@dataclass
class ToolResult:
    """工具执行返回的结果。id=对应的 tool_call_id（与 ToolStarted 配对，见其说明）。"""
    name: str
    result: str
    id: str = ""


@dataclass
class Notice:
    """Agent 的系统级提示（如达到最大循环、放弃空响应重试）。"""
    text: str


@dataclass
class PlanProposed:
    """计划模式下模型调 exit_plan 提交了完整计划：本轮到此结束，等用户审阅/批准/给修改意见。
    plan=计划正文(markdown)；path=已落盘的 plan.md 的 posix 路径（无 store 时 None）。上层据此渲染
    计划 + 弹出"计划待批准"条。"""
    plan: str
    path: str | None = None


# 方便类型标注：Provider.stream() 产出 ReasoningDelta/TextDelta/ToolCall/Done/Usage；
# Agent.run_turn() 还会产出 ToolStarted/ToolResult/Notice/PlanProposed。
Event = (
    ReasoningDelta | TextDelta | ToolCall | Done | Usage
    | ToolStarted | ToolResult | Notice | PlanProposed
)
