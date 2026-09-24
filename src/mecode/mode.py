"""运行模式（normal / auto / plan / yolo）：一层薄薄的【权限预设 + 提示词段】。

模式不是新控制流、也不是多代理——底下始终是同一个单 agent 循环。切换模式 =
  ① 在 from_persisted 出来的基础 policy 上叠一层权限覆盖（apply_mode）；
  ② 更新模式声明；Agent 在下一次模型请求前按需追加，保留已发送的历史前缀。

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
    prompt: str = ""                  # 主 agent 的模式声明（变化时追加到历史，不修改 system）
    sub_prompt: str = ""              # 同模式下【给子 agent 的】说法（见下方各常量；空=不注入）
    overlay: dict = field(default_factory=dict)   # 叠到 policy 的规则：{工具: {"allow"/"deny": [spec]}}
    default: str | None = None        # 覆盖 policy.default（yolo=allow）；None=保持不变


# 最新声明替代旧的模式约束；切出 plan 本身不等于批准执行计划。
MODE_NOTICE = "以下是当前运行模式，替代此前的模式说明；用户任务与已有授权不因此改变。\n"
NORMAL_PROMPT = (
    "当前是普通模式（normal），不再受计划模式的只读限制。"
    "按用户要求推进，工具调用按权限系统审核；切换模式本身不代表计划已获批准。"
)
AUTO_PROMPT = (
    "当前是自动模式（auto），不再受计划模式的只读限制。"
    "项目内的 write_file/edit_file 默认自动批准，其他调用按权限系统处理，显式拒绝仍有效。"
    "按用户要求推进；切换模式本身不代表计划已获批准。"
)
PLAN_PROMPT = (
    "当前是计划模式（plan）：只读调查，可派子 agent 做只读调研；禁止修改项目文件或执行 bash。"
    "唯一写入例外是指定的计划文件：首次 write_file，后续 edit_file 增量更新，"
    "写清目标、涉及文件、实施步骤与关键取舍。"
    "完成后调用 exit_plan 提交审阅，等待用户批准或修改意见。"
)
YOLO_PROMPT = (
    "当前是 YOLO 模式（yolo），不再受计划模式的只读限制。工具默认自动执行，显式拒绝仍有效。"
    "执行前自行评估风险；删除重要数据、覆盖重要文件、对外发送数据等高风险操作，"
    "先说明动作与风险，取得用户明确授权后再执行。切换模式本身不代表计划已获批准。"
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

NORMAL = Mode("normal", "普通", "需要审核的改动和命令执行前都需手动批准", prompt=NORMAL_PROMPT)
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
