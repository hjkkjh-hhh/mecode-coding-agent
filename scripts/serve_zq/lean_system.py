"""辩论网关专属精简 system(--lean-system):替换默认编程助手模板。

编码工作流/任务清单/记忆指南与辩论无关,每次调用白占约 6K 字;检索优先级规矩来自实测:
模型偏爱内置 web_search(DDGS 对中文金融查询质量差、常返回无关结果),而付费的
default-search 技能从未被主动使用。
"""
from pathlib import Path

LEAN_BASE = """你是接入多专家辩论系统的分析 agent。调用方会在本消息末尾给出你的专家角色与任务设定,发言始终以该角色进行。
全程使用中文,包括思考过程;正文直接以角色发言开头,不写"Now I have..."/"现在我已获取..."这类过渡语句。

工具使用要点:
- read_file 读文件(全文/分段);glob 按文件名找、grep 按内容搜(通配扫描默认跳过 .mecode/.git 等目录,path 显式指进这些目录时照常搜);bash 跑命令(技能自带的 python 脚本用它执行)。
- 联网检索优先级:搜索与读网页都优先用 default-search 技能(先 read_file 读其 SKILL.md;search.py 搜索、fetch.py 抓网页正文——付费代理接口,能绕过直连常见的 403/反爬拦截)。内置 web_search/web_fetch 仅在该技能不可用时兜底。
- 数据类问题优先用下方技能重算或核实,严禁凭记忆编造数值。"""


def strip_builtin_web_if_skill(agent, cwd) -> bool:
    """工作区存在 default-search 技能时,把内置 web_search/web_fetch 从工具表移除。
    提示词层的检索优先级约束实测合规率仅 1/3(读没读 SKILL.md 决定走向);物理移除后
    联网检索只剩技能一条路。返回是否移除(build_lean_system 据工具表自动改写检索文案)。"""
    skill = Path(cwd or Path.cwd()) / ".mecode" / "skills" / "default-search" / "SKILL.md"
    if not skill.is_file():
        return False
    agent.tools.unregister("web_search")
    agent.tools.unregister("web_fetch")
    return True


def build_lean_system(agent) -> str:
    """精简头(工具使用要点)+安全边界+技能索引+环境块。
    工具使用要点随 agent 实际工具表自适应:内置 web 工具被移除时检索规矩改为技能
    唯一通道、不再提及被移除的工具;调度/分歧工具的要点只在注册了它们的网关
    (主持人 8110)出现,专家网关不见,不误导。"""
    from mecode.skills import build_skills_prompt, discover_skills
    from mecode.system_prompt import SAFETY, environment_block
    tool_names = {s["function"]["name"] for s in agent.tools.schemas()}
    base = LEAN_BASE
    if "web_search" not in tool_names:
        base = base.replace(
            "- 联网检索优先级:搜索与读网页都优先用 default-search 技能(先 read_file 读其 SKILL.md;search.py 搜索、fetch.py 抓网页正文——付费代理接口,能绕过直连常见的 403/反爬拦截)。内置 web_search/web_fetch 仅在该技能不可用时兜底。",
            "- 联网检索一律用 default-search 技能:先 read_file 读其 SKILL.md;search.py 搜索、fetch.py 抓网页正文——付费代理接口,能绕过直连常见的 403/反爬拦截。")
    extra = []
    if "dispatch_speakers" in tool_names:
        extra.append("- dispatch_speakers 提交本手调度决策(mode/assignments/status/reason):"
                     "点名与收官判定一律经此工具提交,不写在正文。")
    if "manage_disputes" in tool_names:
        extra.append("- manage_disputes 维护辩论分歧账本(open 登记/update 记录进展/close 结清裁决):"
                     "账本真身由调用方系统保存并注入【调度状态】,以注入的账本为准。")
    if extra:
        base = base + "\n" + "\n".join(extra)
    parts = [base, SAFETY]
    skills_part = build_skills_prompt(discover_skills(Path.cwd()))
    if skills_part:
        parts.append(skills_part)
    store = getattr(agent, "store", None)
    parts.append(environment_block(store.dir if store else None))
    return "\n\n".join(parts)
