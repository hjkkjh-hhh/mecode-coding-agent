"""运行模式（normal / auto / plan / yolo）：一层薄薄的【权限预设 + 提示词段】。

模式不是新控制流、也不是多代理——底下始终是同一个单 agent 循环。切换模式 =
  ① 在 from_persisted 出来的基础 policy 上叠一层权限覆盖（apply_mode）；
  ② 往 system prompt 注入该模式的行为段（build_system_prompt 里取 Mode.prompt）。

一个 Mode 把「一模式的三样语义」聚在一处（权限叠加 / 提示词段 / 显示名），加或调一个模式只改这一个条目。
【不含 UI】：颜色等渲染细节归 TUI（与 permission.py 一样，纯逻辑、可测、不耦合 UI）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .permission import ALLOW, DENY, PermissionPolicy


@dataclass(frozen=True)
class Mode:
    key: str                          # 内部标识
    label: str                        # 显示名（状态栏 chip 文本）
    desc: str = ""                    # 一句话简介（模式选择弹窗里给用户看；是语义内容、非渲染）
    prompt: str = ""                  # 注入 system prompt 的模式段（空=不注入）
    sub_prompt: str = ""              # 同模式下【给子 agent 的】说法（见下方各常量；空=不注入）
    overlay: dict = field(default_factory=dict)   # 叠到 policy 的规则：{工具: {"allow"/"deny": [spec]}}
    default: str | None = None        # 覆盖 policy.default（yolo=allow）；None=保持不变


# —— 各模式提示词段：auto/plan/yolo 的正文随各自模式落地时再填；normal 无段 ——
AUTO_PROMPT = ""
PLAN_PROMPT = (
    "当前是计划模式（只读）：先探索、弄清现状，别改任何【项目文件】、别跑改动性命令。"
    "要摸清一大片代码时可以派子 agent 去调研——它同样只能读，回来给你一段总结。"
    "把你的完整实施计划【写进下面给出的计划文件】——首次用 write_file 写全量计划，之后按用户意见用 edit_file "
    "增量修改它（别每次全量重写）。计划要讲清目标、涉及哪些文件、分几步做、关键取舍。"
    "计划就绪后调用 exit_plan（无参数）提交给用户审阅，然后停下等批准或修改意见。"
)
YOLO_PROMPT = (
    "当前是 YOLO 模式：你有调用全部工具的权限，所有调用都会自动执行、不再向用户确认。"
    "正因如此，每次执行前你要自己把好最后一道关——严格评估该动作的安全性与后果："
    "凡可能造成危险或难以挽回后果的（删库 / rm -rf、覆盖重要文件、对外泄露或发送数据、其他不可逆的破坏性命令），"
    "在执行之前必须先停下来告诉用户你想做什么、风险在哪，请其确认，用户批准后再执行。"
)

# —— 子 agent 版模式段（由 SubagentRunner 拼到 SUBAGENT_PROMPT 后）——
# 为什么不复用上面那几段：主 agent 版是写给"要产出计划文件、调 exit_plan 提交用户审阅"的角色看的，
# 而子 agent 没有用户、也没有 exit_plan 工具，照搬会让它去写计划文件、调一个不存在的工具。
PLAN_SUB_PROMPT = (
    "\n- 当前处于【只读】状态：写文件 / 改文件 / 跑命令都会被拒绝，你只能靠读取、搜索、浏览完成调研。"
    "被拒的动作别反复重试，把查清的事实与结论写进总结即可（主 agent 要拿它去写实施计划）。"
)
YOLO_SUB_PROMPT = (
    "\n- 当前所有工具调用都会自动执行、不再确认：凡不可逆的破坏性动作（删库 / rm -rf、覆盖重要文件、"
    "对外发送数据）你自己别做，把它写进总结交给主 agent 定夺。"
)

_WRITE = ("write_file", "edit_file")
# 计划模式只读：写/改/跑命令全禁。子 agent 【不】在禁用之列——它此前被一并禁掉，理由是"子 agent 内部
# policy=None 全放，会绕过只读"；现在子 agent 继承主 agent 的 policy（见 subagent.py 头），在计划模式下
# 它同样只能读，绕不过去 → 那条禁令的前提没了，放开它去做探索（正是计划模式最需要的活）。
_PLAN_DENY = ("write_file", "edit_file", "bash")

NORMAL = Mode("normal", "普通", "需要审核的改动和命令执行前都需手动批准")            # 空叠加 + 空段 = 现状（default=ask）
# auto：自动放行【项目内】的结构化编辑（write/edit）。@root 由 apply_mode 用 policy.root 展开成项目根；
# 项目外编辑不在 allow → 仍落 ask。不碰 raw bash（无 OS 沙箱，靠解析路径限定不可靠，见对话记录）。
AUTO = Mode("auto", "自动", "改动项目内文件自动批准；其他工具仍会询问",
            prompt=AUTO_PROMPT, overlay={t: {ALLOW: ["@root", "@root/**"]} for t in _WRITE})
PLAN = Mode("plan", "计划", "先探索并给出完整计划，批准后再动手",
            prompt=PLAN_PROMPT, sub_prompt=PLAN_SUB_PROMPT,
            overlay={t: {DENY: ["*"]} for t in _PLAN_DENY})              # 只读
YOLO = Mode("yolo", "YOLO", "所有工具自动批准、不再询问（慎用）",
            prompt=YOLO_PROMPT, sub_prompt=YOLO_SUB_PROMPT, default=ALLOW)   # 全放行

MODES: dict[str, Mode] = {m.key: m for m in (NORMAL, AUTO, PLAN, YOLO)}
DEFAULT_MODE = "normal"
CYCLE = ["normal", "auto", "plan", "yolo"]     # Shift+Tab 循环序


def apply_mode(policy: PermissionPolicy, key: str) -> PermissionPolicy:
    """把某模式的权限覆盖叠加到 policy（就地改并返回）。

    约定：policy 应是【刚 from_persisted 出来的新实例】——叠加只往规则里 add、不做清理；
    切换模式时先重新 from_persisted 再叠，避免上一个模式的覆盖残留。
    """
    m = MODES[key]
    for tool, groups in m.overlay.items():
        for decision, specs in groups.items():
            for spec in specs:
                policy.add(tool, decision, _expand(spec, policy.root))
    if m.default is not None:
        policy.default = m.default
    return policy


def _expand(spec: str, root: str | None) -> str:
    """把覆盖里的 @root 占位展开成项目根（auto 放行项目内编辑用）。无 root（无项目上下文）则保持字面——
    "@root" 永不匹配真实路径 → 自然落 ask，不误放行。"""
    return spec.replace("@root", root) if (root and "@root" in spec) else spec


def next_mode(cur: str) -> str:
    """循环序里的下一个（Shift+Tab）；不认得的 key 归位到序首。"""
    try:
        return CYCLE[(CYCLE.index(cur) + 1) % len(CYCLE)]
    except ValueError:
        return CYCLE[0]
