"""运行模式：权限叠加、循环序、模式声明按需追加；纯逻辑，不起 TUI。"""
from mecode.mode import CYCLE, DEFAULT_MODE, MODES, apply_mode, next_mode
from mecode.permission import ALLOW, DENY, PermissionPolicy
from mecode.system_prompt import SAFETY, build_system_prompt


def _policy() -> PermissionPolicy:
    # 带项目根：写类默认收紧到根内，便于对比 auto 放行 / plan 拒绝
    return PermissionPolicy.from_persisted({}, project_root="C:/proj")


def test_normal_不改动策略_写改都要问():
    p = _policy()
    apply_mode(p, "normal")
    assert p.decide("write_file", {"path": "C:/proj/a.py"}) == "ask"        # normal：项目内编辑也要问
    assert p.decide("edit_file", {"path": "C:/proj/a.py"}) == "ask"
    assert p.decide("read_file", {"path": "C:/proj/a.py"}) == ALLOW         # 只读类仍放行
    assert p.default == "ask"


def test_auto_放行项目内编辑_项目外与bash仍问():
    p = apply_mode(_policy(), "auto")                                       # _policy() root=C:/proj
    assert p.decide("write_file", {"path": "C:/proj/a.py"}) == ALLOW        # 项目内编辑：放行
    assert p.decide("edit_file", {"path": "C:/proj/src/b.py"}) == ALLOW
    assert p.decide("write_file", {"path": "C:/other/x.py"}) == "ask"       # 项目外编辑：仍问
    assert p.decide("bash", {"command": "rm x"}) == "ask"                   # raw bash 不受影响，仍问


def test_plan_只读_拒写改跑_但放行子agent做探索():
    p = apply_mode(_policy(), "plan")
    for tool, args in (("write_file", {"path": "C:/proj/a"}),
                       ("edit_file", {"path": "C:/proj/a"}),
                       ("bash", {"command": "ls"})):
        assert p.decide(tool, args) == DENY
    # 子 agent 曾被一并 deny，理由是"它内部 policy=None 全放、会绕过只读"；现在它继承主 agent 的
    # policy（见 subagent.py 头），计划模式下同样只能读 → 那条禁令的前提没了，放开它去做探索。
    assert p.decide("subagent", {"description": "x", "prompt": "y"}) == ALLOW
    assert p.decide("read_file", {"path": "C:/proj/a"}) == ALLOW            # 读仍放行
    assert p.decide("grep", {"path": "C:/proj"}) == ALLOW
    assert p.decide("exit_plan", {"plan": "x"}) == ALLOW                    # 提交计划：计划模式唯一动作，放行


def test_yolo_全放():
    p = apply_mode(_policy(), "yolo")
    assert p.default == ALLOW
    assert p.decide("bash", {"command": "rm -rf /"}) == ALLOW
    assert p.decide("write_file", {"path": "/etc/x"}) == ALLOW


def test_apply_mode_不污染源默认_每次from_persisted干净():
    # plan 叠 deny 后，重新 from_persisted 再叠 normal，不应残留 plan 的 deny
    apply_mode(_policy(), "plan")
    p2 = apply_mode(_policy(), "normal")
    assert p2.decide("bash", {"command": "ls"}) == ALLOW                    # ls 在默认放行名单，未被 plan 的 deny 污染


def test_next_mode_循环一圈回起点():
    seq = [DEFAULT_MODE]
    for _ in range(len(CYCLE)):
        seq.append(next_mode(seq[-1]))
    assert seq[-1] == seq[0]                     # 走 len(CYCLE) 步回到起点
    assert set(CYCLE) == set(MODES)              # 循环序 == 注册表（不漏不多）
    assert next_mode("不存在的模式") == CYCLE[0]  # 未知 key → 归位序首


def test_build_system_prompt_SAFETY常驻_不含模式段():
    sp = build_system_prompt(project_context="")
    assert SAFETY in sp and "编程助手" in sp        # SAFETY 常驻、BASE 仍在
    # 模式行为段不进 system prompt，变化时追加声明。
    for m in MODES.values():
        if m.prompt:
            assert m.prompt not in sp                # 如 yolo 的自查段


def _fake_agent(reminder: str = ""):
    from mecode.agent import Agent
    from mecode.tools import ToolRegistry
    a = Agent(type("P", (), {"stream": lambda *x, **k: iter(())})(), ToolRegistry(), system_prompt="s")
    a.mode_reminder = reminder
    return a


def test_mode_reminder_首次请求追加且后续不重复():
    a = _fake_agent("计划模式：只读探索、先出计划")
    list(a.run_turn("改一下 x"))
    conts = [m["content"] for m in a.messages if m["role"] == "user"]
    rem_i = next(i for i, c in enumerate(conts)
                 if "system-reminder" in c and "计划模式：只读探索、先出计划" in c)
    usr_i = next(i for i, c in enumerate(conts) if c == "改一下 x")
    assert rem_i > usr_i                              # 请求前追加，不从历史中抽出再搬到末尾。
    list(a.run_turn("继续"))
    assert sum("_mode" in m for m in a.messages) == 1


def test_mode_reminder_为空则不注入():
    a = _fake_agent("")                               # 显式不配置声明（生产入口四个模式均有正文）
    list(a.run_turn("hi"))
    assert not any("system-reminder" in m.get("content", "") for m in a.messages)


def test_pending_reminder_下一轮注入一次即清():
    a = _fake_agent("")
    a._pending_reminder = "执行提示X"                   # 如批准计划的执行提示（隐藏 reminder，不显示成用户消息）
    list(a.run_turn("hi"))
    us = [m["content"] for m in a.messages if m["role"] == "user"]
    assert any("system-reminder" in c and "执行提示X" in c for c in us)   # 本轮注入了
    assert a._pending_reminder == ""                                      # 注入后清空
    list(a.run_turn("hi2"))                                              # 再一轮不再注入
    n = sum("执行提示X" in m.get("content", "") for m in a.messages)
    assert n == 1


def test_agent_update_system_prompt_替换非新增():
    from mecode.agent import Agent
    from mecode.tools import ToolRegistry
    a = Agent(type("P", (), {"stream": lambda *x, **k: iter(())})(), ToolRegistry(), system_prompt="旧")
    assert a.messages[0] == {"role": "system", "content": "旧"}
    a.update_system_prompt("新")
    assert a.messages[0]["role"] == "system" and a.messages[0]["content"] == "新"
    assert sum(1 for m in a.messages if m.get("role") == "system") == 1     # 是替换、不是又插一条


# ---- 计划文件写例外 + exit_plan（无参读计划文件呈交、结束本轮、发 PlanProposed） ----

def test_plan_计划文件例外_读写改都放行_其它写仍拒():
    p = apply_mode(_policy(), "plan")
    p.plan_path = "C:/proj/plan.md"                                       # 计划文件例外
    assert p.decide("write_file", {"path": "C:/proj/plan.md"}) == ALLOW   # 首次写全量：放行
    assert p.decide("edit_file", {"path": "C:/proj/plan.md"}) == ALLOW    # 增量改它：放行
    assert p.decide("read_file", {"path": "C:/proj/plan.md"}) == ALLOW    # 回看计划：放行（执行期项目外也不问）
    assert p.decide("write_file", {"path": "C:/proj/src/x.py"}) == DENY   # 其它写：仍拒（只读之下）


class _CallsToolOnce:
    """第一次 stream 吐一个工具调用，之后吐文本。用于验证工具是否结束了本轮（结束→只调一次）。"""
    def __init__(self, name, args):
        self.name, self.args, self.calls = name, args, 0

    def stream(self, messages, tools=None, should_stop=None):
        from mecode.events import Done, TextDelta, ToolCall
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(id="c1", name=self.name, arguments=self.args)
            yield Done(reason="tool_calls")
        else:
            yield TextDelta("完成")
            yield Done(reason="stop")


def _plan_agent(tmp_path, mode):
    from mecode.agent import Agent
    from mecode.session import SessionStore
    from mecode.tools import default_registry
    store = SessionStore(root=tmp_path, cwd=tmp_path, session_id="s")
    prov = _CallsToolOnce("exit_plan", {})                   # exit_plan 无参（读计划文件）
    a = Agent(prov, default_registry(), system_prompt="s", store=store)
    a.mode = mode
    return a, prov, store


def test_exit_plan_计划模式_读计划文件呈交_结束本轮(tmp_path):
    from mecode.events import PlanProposed
    a, prov, store = _plan_agent(tmp_path, "plan")
    store.dir.mkdir(parents=True, exist_ok=True)
    store.plan_path.write_text("# 计划\n1. 改 x", encoding="utf-8")   # 模拟模型已 write_file 写好计划文件
    evs = list(a.run_turn("提交"))
    assert prov.calls == 1                                    # exit_plan 结束了本轮 → 没再调模型
    plans = [e for e in evs if isinstance(e, PlanProposed)]
    assert len(plans) == 1 and "改 x" in plans[0].plan and plans[0].path   # 从计划文件读出呈交


def test_exit_plan_无计划文件_不结束本轮_提示先写(tmp_path):
    a, prov, store = _plan_agent(tmp_path, "plan")           # 没写 plan.md
    list(a.run_turn("提交"))
    assert prov.calls == 2                                    # 没内容 → 不结束本轮（提示先写）→ 回循环再调一次


def test_exit_plan_非计划模式_不结束本轮(tmp_path):
    from mecode.events import PlanProposed
    a, prov, store = _plan_agent(tmp_path, "normal")         # 非计划模式
    evs = list(a.run_turn("hi"))
    assert prov.calls == 2                                    # 温和拒绝、不结束本轮
    assert not any(isinstance(e, PlanProposed) for e in evs)
