"""辩论主持人网关的结构化工具:调度(dispatch_speakers)与分歧账本(manage_disputes)。

两件工具都只做"结构校验 + 存入 agent._serve_meta",由门面在响应里附带给调用方
(mecode_dispatch / mecode_disputes 字段);名册校验、状态机、与发言记录的交叉核验
全部在调用方(辩论引擎)侧——网关每次请求不持有辩论状态。
"""
from mecode.tools import Tool

# 调度工具参数结构。名册校验不在此做:网关不知道场上有哪些专家,名字对错由调用方(引擎)核对
DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string", "enum": ["single", "sequential", "parallel"],
            "description": "发言方式。single=单人;sequential=依次发言,按 assignments 顺序,后者能看到前者本手发言;parallel=并行发言,各自基于同一上下文快照独立作答,本手内互相不可见",
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "expert": {"type": "string", "description": "专家名称"},
                    "task": {"type": "string", "description": "只发给该专家的任务指令"},
                },
                "required": ["expert", "task"],
            },
            "description": "点名清单,按发言顺序排列;status=finish 时必须为空数组(点名与收官不得同手提交)",
        },
        "status": {
            "type": "string", "enum": ["continue", "finish"],
            "description": "continue=辩论继续;finish=无需再辩,进入收官流程",
        },
        "reason": {"type": "string", "description": "本次调度或收官判定的理由"},
    },
    "required": ["mode", "assignments", "status"],
}


def make_dispatch_tool(agent) -> Tool:
    """构造本请求专用的调度工具:结构校验通过即存入 _serve_meta,由响应附带给调用方。"""
    def _handler(args: dict) -> str:
        mode = args.get("mode")
        assignments = args.get("assignments") or []
        status = args.get("status")
        errs = []
        if status == "continue" and not assignments:
            errs.append("status=continue 时 assignments 不能为空")
        if status == "finish" and assignments:
            errs.append("status=finish 时 assignments 必须为空;要安排最后陈词,先以 status=continue 点名,下一手再提交 finish")
        if mode == "single" and len(assignments) > 1:
            errs.append("mode=single 只能点一人;多人请用 sequential 或 parallel")
        if any(not str(a.get("expert") or "").strip() or not str(a.get("task") or "").strip()
               for a in assignments):
            errs.append("assignments 中有空的 expert 或 task")
        if errs:
            return "调度未受理,请修正后重新调用:" + ";".join(errs)
        if mode == "parallel" and len(assignments) == 1:
            mode = "single"                        # 单人无并行语义,降级
        payload = {
            "mode": mode,
            "assignments": [{"expert": str(a["expert"]).strip(), "task": str(a["task"])}
                            for a in assignments],
            "status": status,
            "reason": str(args.get("reason") or ""),
        }
        meta = getattr(agent, "_serve_meta", None)
        if not isinstance(meta, dict):
            meta = {}
            agent._serve_meta = meta
        meta["dispatch"] = payload                 # 重复调用以最后一次为准
        names = "、".join(a["expert"] for a in payload["assignments"]) or "(无点名)"
        return f"调度已受理:mode={mode} status={status} 点名={names}"

    return Tool(
        name="dispatch_speakers",
        description="提交本手调度决策:发言方式(single/sequential/parallel)、点名专家与各自任务。"
                    "每手决策调用一次;重复调用以最后一次为准;status=finish 表示提前收官,此时 assignments 必须为空。",
        parameters=DISPATCH_SCHEMA,
        handler=_handler,
    )


# 分歧账本操作结构。编号存在性/当事人名册/确认字段与发言记录的交叉核验都在调用方(引擎)做:
# 网关每次请求无状态,不持有账本
DISPUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string", "enum": ["open", "update", "close"],
            "description": "open=登记新分歧;update=记录进展;close=结清",
        },
        "id": {"type": "string", "description": "分歧编号。open 时自取(D1、D2 依次编号),update/close 时引用已登记的编号"},
        "severity": {
            "type": "string", "enum": ["致命", "非致命"],
            "description": "open 必填。致命=不解决则本场结论不可信;非致命=模型不确定性或呈现层分歧",
        },
        "parties": {
            "type": "array", "items": {"type": "string"},
            "description": "open 必填:分歧当事专家名单",
        },
        "description": {"type": "string", "description": "open 必填:分歧内容一句话,含各方主张的关键数值或立场"},
        "note": {"type": "string", "description": "update 必填:本次进展(某方修正主张、范围收窄等)"},
        "resolution": {
            "type": "string", "enum": ["已收敛", "采纳一方", "搁置"],
            "description": "close 必填。已收敛=各方明确达成一致;采纳一方=主持人裁决采纳某方主张;搁置=不再辩论,进终报未决分歧节",
        },
        "adopted": {"type": "string", "description": "resolution=采纳一方时必填:采纳哪位专家的哪个主张(含数值)"},
        "counterparty_position": {"type": "string", "description": "resolution=采纳一方时必填:未被采纳方的最后立场要点"},
        "counterparty_confirmed": {"type": "boolean", "description": "resolution=采纳一方时必填:未被采纳方是否明确表态接受该裁决。系统会与发言记录交叉核验"},
    },
    "required": ["action", "id"],
}


def make_dispute_tool(agent) -> Tool:
    """构造本请求专用的分歧账本工具:按 action 校验字段,操作按序累积进 _serve_meta,
    由响应附带给调用方;账本真身由调用方(引擎)维护并逐手注入状态行。"""
    def _handler(args: dict) -> str:
        action = args.get("action")
        did = str(args.get("id") or "").strip()
        errs = []
        if not did:
            errs.append("缺少分歧编号 id")
        op = {"action": action, "id": did}
        if action == "open":
            severity = args.get("severity")
            parties = [str(p).strip() for p in (args.get("parties") or []) if str(p).strip()]
            desc = str(args.get("description") or "").strip()
            if severity not in ("致命", "非致命"):
                errs.append("open 需要 severity(致命/非致命)")
            if not parties:
                errs.append("open 需要 parties(当事专家名单)")
            if not desc:
                errs.append("open 需要 description(分歧内容)")
            op.update(severity=severity, parties=parties, description=desc)
        elif action == "update":
            note = str(args.get("note") or "").strip()
            if not note:
                errs.append("update 需要 note(进展内容)")
            op["note"] = note
        elif action == "close":
            res = args.get("resolution")
            if res not in ("已收敛", "采纳一方", "搁置"):
                errs.append("close 需要 resolution(已收敛/采纳一方/搁置)")
            op["resolution"] = res
            if res == "采纳一方":
                adopted = str(args.get("adopted") or "").strip()
                cpos = str(args.get("counterparty_position") or "").strip()
                if not adopted:
                    errs.append("采纳一方需要 adopted(采纳谁的哪个主张)")
                if not cpos:
                    errs.append("采纳一方需要 counterparty_position(未被采纳方最后立场)")
                if "counterparty_confirmed" not in args:
                    errs.append("采纳一方需要 counterparty_confirmed(对方是否明确接受)")
                op.update(adopted=adopted, counterparty_position=cpos,
                          counterparty_confirmed=bool(args.get("counterparty_confirmed")))
        else:
            errs.append("action 需为 open/update/close")
        if errs:
            return "分歧操作未受理,请修正后重新调用:" + ";".join(errs)
        meta = getattr(agent, "_serve_meta", None)
        if not isinstance(meta, dict):
            meta = {}
            agent._serve_meta = meta
        meta.setdefault("disputes", []).append(op)   # 多次调用按序累积,一次调用操作一条分歧
        if action == "open":
            return f"分歧已登记:{did}({op['severity']}|{'、'.join(op['parties'])})"
        if action == "update":
            return f"分歧进展已记录:{did}"
        return f"分歧已结清:{did}({op['resolution']})"

    return Tool(
        name="manage_disputes",
        description="维护辩论分歧账本:open=登记新分歧,update=记录进展,close=结清并给出裁决。"
                    "一次调用操作一条分歧,可多次调用;账本由调用方系统保存并逐手注入状态行,不依赖本会话记忆。",
        parameters=DISPUTE_SCHEMA,
        handler=_handler,
    )
