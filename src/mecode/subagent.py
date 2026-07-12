"""子 agent：派生一个【干净上下文】的 Agent 跑一个子任务、回一段总结。做成【工具】（CC 同款）。

灵魂 = 上下文隔离：子 agent 读文件/改代码/跑命令/搜索的一大堆【中间步骤不进主 agent 上下文】，
主 agent 只拿回【一段总结】。这是"用委派做上下文管理"——把整段脏活外包，主上下文不被子任务撑爆。

v1（同步·推式）：任务写在 prompt 里 → 造干净 Agent 跑到出最终文本 → 返回当工具结果。顺序、阻塞
（一轮里多个 subagent 会顺序跑，不并行——并行留到 v3 复用 bg）。
工具集 = core（read/write/edit/bash/grep/glob），【不含 subagent/bg/task】（防无限递归、保持精简）。
权限 = 全放行（policy=None）：子 agent 无用户可答"ask"，主 agent 既决定派活即信任它（选项 a）。
打断 = 与主 agent 共享同一个 interrupt Event：Ctrl+C 能连带停子 agent（在其下一个检查点，v1 不即时杀其 bash）。
"""
from __future__ import annotations

import threading

from .tools import Tool, default_registry

SUBAGENT_PROMPT = """你是一个【子 agent】，由主 agent 派来完成一个相对独立的子任务。
- 专注完成派给你的这一个任务：自主读文件 / 改代码 / 跑命令 / 搜索，别问用户——没有用户能回答你。
- 你【不能】起后台类型的任务，（bash 的 background 用不了、也不能再派子 agent）：要跑命令就前台跑、等它完成再继续。
- 完成后用【一段简洁的总结】作为最终回复：说清你做了什么、结果如何、关键结论或产物路径。
  这段总结会【原样返回给主 agent】、是它唯一能看到的东西，所以要自足、别复述中间过程。
- 做不到或遇到硬阻碍，也如实在总结里讲明，别假装完成。"""


_NO_SUMMARY = "（子 agent 未产出总结）"
_INTERRUPTED = "（子 agent 被用户中断，未完成）"


def _last_assistant_text(agent) -> str:
    """子 agent 跑完后取【最后一条有正文的 assistant 消息】= 最终总结。"""
    for m in reversed(agent.messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return m["content"].strip()
    return _NO_SUMMARY


class SubagentRunner:
    """派生子 agent 跑子任务、回总结。持有造子 agent 需要的依赖（provider / config / 前台共享打断标志）。
    两种入口：run（前台，共享主 agent 打断→Ctrl+C 连带停）/ run_for_bg（后台，自带独立打断、暴露 proc_slot 供 kill）。"""

    def __init__(self, provider, config, interrupt: threading.Event | None = None) -> None:
        self.provider = provider          # 和主 agent 同一个 provider（同后端）
        self.config = config
        self.interrupt = interrupt        # 前台：与主 agent 共享打断标志（Ctrl+C 连带停）
        # 当前在跑的【前台】子 agent 的 proc_slot（run 期间登记、跑完注销）。共享打断标志只让子 agent 在
        # 【检查点】停，但它若正卡在长 bash 的 communicate 里收不到——故 Ctrl+C 时主 agent 还要 kill_active
        # 把这些 bash 当场树杀，让 communicate 立即返回、子 agent 到检查点即停（否则并发批会卡到最慢的 bash 跑完）。
        self._active_slots: set = set()
        self._slots_lock = threading.Lock()

    def _make(self, interrupt: threading.Event | None):
        from .agent import Agent          # 延迟导入：避免与 agent.py 循环
        return Agent(
            self.provider, default_registry(),   # 干净的 core 工具集（read/write/edit/bash/grep/glob）
            system_prompt=SUBAGENT_PROMPT,
            config=self.config,
            store=None,                   # 不落子会话盘（只回总结；主 transcript 记 spawn 调用+总结）
            policy=None,                  # 全放行（选项 a）：无用户可答 ask，既派活即信任
            subagent=True,                # 不注册 subagent/bg/task 工具（防递归、保持精简）
            interrupt=interrupt,
        )

    def run(self, prompt: str) -> str:
        """前台：造干净子 agent 跑到完成、回总结。共享主 agent 打断（Ctrl+C 连带停）。
        run 期间把它的 proc_slot 登记进 _active_slots，使 Ctrl+C 能连带杀它正跑的 bash（见 kill_active）。"""
        sub = self._make(self.interrupt)
        with self._slots_lock:
            self._active_slots.add(sub._proc_slot)
        try:
            list(sub.run_turn(prompt))
        finally:
            with self._slots_lock:
                self._active_slots.discard(sub._proc_slot)
        summary = _last_assistant_text(sub)
        if summary == _NO_SUMMARY and self.interrupt is not None and self.interrupt.is_set():
            return _INTERRUPTED       # 被中断在跑 bash 时，还没走到出总结那步 → 讲明是中断、非它自己哑了
        return summary

    def kill_active(self) -> None:
        """树杀所有在跑的前台子 agent 正登记的 bash（Ctrl+C 时由主 agent 调）→ 它们的 communicate 立即返回、
        各自在检查点看到共享打断标志后停下 → 并发批不再卡到最慢子 agent 的 bash 跑完。"""
        with self._slots_lock:
            slots = list(self._active_slots)
        for s in slots:
            s.kill()

    def run_for_bg(self, prompt: str, interrupt: threading.Event, on_slot) -> str:
        """后台：用【自带的独立打断标志】造子 agent（Ctrl+C 不碰它，只 kill 停），把它的 ProcSlot 经 on_slot
        交给管理器（供 kill 杀它正跑的前台 bash 及后代）；跑到完成、回总结。"""
        sub = self._make(interrupt)
        on_slot(sub._proc_slot)           # 交出 proc_slot：kill 时置打断 + 杀这条 bash（树杀→含孙进程）
        list(sub.run_turn(prompt))
        return _last_assistant_text(sub)


def subagent_tools(runner: SubagentRunner) -> list[Tool]:
    """AI 可调的子 agent 工具（闭包绑定 runner，和 background_tools/task_tools 一个套路）。takes_slot=True：
    execute 会注入 (slot, bg)，后台档用 bg.start_subagent 起。"""
    def _spawn(args: dict, slot=None, bg=None) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "错误：prompt 不能为空（描述要子 agent 完成的子任务）"
        desc = (args.get("description") or "").strip()     # 短标签：显示用，不进子 agent 上下文
        if args.get("background") and bg is not None:      # 后台：立即返回 id、不阻塞；完成通知你
            tid = bg.start_subagent(prompt, runner, description=desc)
            return (f"已在后台派生子 agent · 任务 #{tid} · 运行中"
                    f"（完成会通知你；需要时用 kill_bgtask({tid}) 停）")
        return runner.run(prompt)         # 前台：跑到完成、回总结（并发批里被调，slot/bg 均 None）

    return [Tool(
        name="subagent",
        description="派生一个【子 agent】去完成一个相对独立的子任务：它有自己干净的上下文，自主读改跑搜，"
                    "完成后回一段总结给你——它的【中间过程不进你的上下文】，你只拿回结论，省上下文。"
                    "适合把'要读一大堆文件 / 试错很多步'的脏活外包出去（探索性调研、独立模块的写+测、"
                    "批量查找等）。任务要一次讲清（做什么、在哪、怎样算完成），它无法回问你；"
                    "它跑不动或失败也会在总结里说明。它自己【不能】再派子 agent。\n"
                    "【前台（默认）vs 后台】：要它的结果才能往下走（调研/多角度/验证）→ 用前台，"
                    "同一条消息里发【多个】subagent 调用会【并发】跑、等齐所有结果（三个各 30s 的，30s 跑完不是 90s）；"
                    "真正独立、不用马上要 → 设 background=true 转后台，立即返回编号、完成再通知你（可 kill_bgtask 停）。",
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "用 3-5 个词概括这个子任务（做标签显示，让你和用户一眼看清这个子 agent 在干嘛，"
                                               "如“探索认证模块”“审查并发安全”）。不发给子 agent、纯显示用。"},
                "prompt": {"type": "string",
                           "description": "交给子 agent 的子任务描述，越具体越好（目标 / 范围 / 验收标准）"},
                "background": {"type": "boolean",
                               "description": "true=转后台跑、立即返回编号、完成通知你（用于独立且不急的活）；"
                                              "默认 false=前台，多个前台调用会并发、等齐所有结果"},
            },
            "required": ["description", "prompt"],
        },
        handler=_spawn,
        takes_slot=True,
    )]
