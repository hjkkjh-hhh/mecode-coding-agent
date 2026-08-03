"""mecode OpenAI 兼容网关 —— 把整个 agent(工具循环 + skill + 权限)折叠成一个
/v1/chat/completions 端点,让任何程序把 mecode 当"一个模型"调用。

    python scripts/serve.py --port 8100 --cwd 工作区目录

调用方视角:发标准 OpenAI chat 请求(stream 可选),收一条 assistant 消息——
内部可能经历了 模型↔bash/skill 的多轮工具循环,对外折叠成一次调用(agent-as-gateway,
与 OpenClaw 的 /v1 网关同构)。多 agent 编排系统(如 agentscope)可以把 api_base 指到
这里,零改动换上 mecode 运行时。

设计取舍:
- 【每请求无状态】:调用方(agentscope 等)每次全量重发 system+历史,网关每请求新建
  agent、用完即弃。会话照常落盘(transcript)——事后可审计每个"专家"内部跑了什么工具。
- 【system 合并】:来访 system(调用方设定的人格/任务)拼在 mecode 自身 system(工具
  说明+技能索引)之后——两者缺一不可:少前者模型没人格,少后者模型不知道自己有工具。
- 【默认 yolo 模式】:无头环境没人点审批,跑 skill 脚本必须自动放行 bash。网关属
  专用受信部署(自己起的服务、自己的工作区);要收紧可 --mode auto(bash 会被拒)。
- 【自包含会话存档】:mecode 核心不落 system(可重建的运行时配置);网关场景的人格/
  装载历史/合并后 system 都是外来输入、本地不可重建 → 每请求写 request.json
  进会话目录(transcript 之外的旁车,核心存储设计不动;写失败只警告不拖垮请求)。--tag N 时会话目录名 =
  "N-序号"(专家 N 的第几次发言,序号扫已有目录自增),多轮测试后按名归档。
- 【stdlib,零新依赖】:ThreadingHTTPServer,一连接一线程,多专家并发各跑各的。
- 【流式 + 断连即中断】:stream=true 走 SSE——agent 在工作线程跑,响应线程每
  HEARTBEAT_SECS 发一个空 delta 心跳块;心跳写失败(客户端断开连接)即
  request_interrupt():流式生成当场停、正在跑的 bash 被杀(与 vLLM/llama.cpp 的
  "断连即取消"同一契约)。正文完成后以单块发出,对外折叠语义与非流式一致。
  另有 POST /v1/interrupt 中断全部在飞请求(手动刹车)。
- 后端 = ~/.mecode/config.json 当前模型(/config 可切);工作区 = --cwd(技能放
  <cwd>/.mecode/skills/,权限根/会话归属也是它)。
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mecode.bootstrap import build_agent          # noqa: E402
from mecode.compact import estimate_tokens        # noqa: E402
from mecode.events import Notice, ReasoningDelta, TextDelta, ToolResult, ToolStarted  # noqa: E402
from serve_zq import debate_meta, debate_tools, lean_system  # noqa: E402(证券辩论专属适配包)

_PERSONA_HEADER = "\n\n===== 以下为调用方为本次对话设定的角色与任务(务必遵循) =====\n"
_LEAN_SYSTEM = False    # --lean-system:用辩论专属精简 system 替换默认编程助手模板
_DISPATCH_TOOL = False  # --dispatch-tool:注册 dispatch_speakers 工具,结构化承接调度决策
_DISPUTE_TOOL = False   # --dispute-tool:注册 manage_disputes 工具,结构化承接分歧账本操作
_PERSISTENT = False     # --persistent-sessions:同 tag 一场辩论一条持续会话,调用方只发增量
_LIVE: dict[str, dict] = {}       # tag → 存活会话 {agent/debate/sid/last_turn/turn_base/guard}
_LIVE_LOCK = threading.Lock()

# 辩论专属部分(调度/分歧工具、场次/账本元数据、精简 system)收拢在 serve_zq 包;
# 门面保留原名与可变全局,测试与调用点不变
_DISPATCH_SCHEMA = debate_tools.DISPATCH_SCHEMA
_make_dispatch_tool = debate_tools.make_dispatch_tool
_DISPUTE_SCHEMA = debate_tools.DISPUTE_SCHEMA
_make_dispute_tool = debate_tools.make_dispute_tool
_LEAN_BASE = lean_system.LEAN_BASE
_build_lean_system = lean_system.build_lean_system

_REPLY_CONTRACT = ("\n\n# 回复契约\n本次调用只有你最后一条消息的正文会返回给调用方,"
                   "中间消息对调用方不可见。最后一条消息必须自包含你的完整交付内容,"
                   "不要只写摘要或收场白。")

HEARTBEAT_SECS = 2.0    # SSE 心跳间隔 = 断连的最大发现延迟(测试会调小)
_SSE_PIECE = 8000       # SSE 单帧正文/思考上限(字符):防单行超 aiohttp 客户端 64KB 行缓冲

_INFLIGHT: dict[str, object] = {}     # 在飞请求 id → agent(断连/手动中断时定位)
_INFLIGHT_LOCK = threading.Lock()


def _register(agent) -> str:
    rid = uuid.uuid4().hex
    with _INFLIGHT_LOCK:
        _INFLIGHT[rid] = agent
    return rid


def _unregister(rid: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(rid, None)


def interrupt_all() -> int:
    """中断全部在飞请求(POST /v1/interrupt)。走 TUI ESC 同一套协作式打断:
    流式消费当场停、正在跑的 bash 子进程被杀、孤儿 tool_calls 由 agent 收尾补齐。"""
    with _INFLIGHT_LOCK:
        agents = list(_INFLIGHT.values())
    for a in agents:
        a.request_interrupt()
    return len(agents)


_SHARED_DIR: Path | None = None   # --shared-dir:场次计数 + 全场账本(多网关共用;不配则关闭)

_INSTR_MARKERS = debate_meta.INSTR_MARKERS


def _debate_no() -> int:
    return debate_meta.debate_no(_SHARED_DIR)


def _debate_next() -> int:
    return debate_meta.debate_next(_SHARED_DIR)


_call_kind = debate_meta.call_kind
_extract_instruction = debate_meta.extract_instruction
_extract_round = debate_meta.extract_round


def _alloc_session_id(tag: str, cwd, user_input: str = "") -> tuple[str, int, int, str]:
    return debate_meta.alloc_session_id(tag, cwd, user_input, _SHARED_DIR)


def _bump_seq() -> tuple[int, int]:
    return debate_meta.bump_seq(_SHARED_DIR)


def _append_ledger(agent, user_input: str, reply: str, interrupted: bool = False) -> None:
    debate_meta.append_ledger(agent, user_input, reply, _SHARED_DIR, interrupted)


def _dump_request_context(agent, persona: str, prior: list, user_input: str, model: str,
                          filename: str | None = None) -> None:
    """自包含存档:把本请求的外来上下文(人格/装载历史)+ 合并后 system + 本轮输入
    写进会话目录。这些在 mecode 核心里不落 transcript(system=可重建、外来上下文=
    调用方所有),网关场景两者都不可本地重建,故由网关自己留档。"""
    store = getattr(agent, "store", None)
    if store is None:
        return
    real_model = getattr(getattr(getattr(agent, "provider", None), "backend", None), "model", "")
    ctx = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": real_model or model,     # 网关实际后端模型;测试假 provider 无 backend 时退回声称值
        "model_claimed": model,           # 调用方请求体里的标签(网关不使用,仅留证)
        "persona": persona,
        "history": prior,
        "user_input": user_input,
        "system_merged": agent.messages[0]["content"] if agent.messages else "",
    }
    try:
        store.dir.mkdir(parents=True, exist_ok=True)
        (store.dir / (filename or "request.json")).write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:                          # 存档是辅助日志:写失败(如超长路径)不拖垮请求
        print(f"[serve] 请求存档写入失败:{e}")


def _stamp_name(m: dict) -> str:
    """消息带 name 字段(agentscope Msg 序列化会带)时缝进正文开头:多专家辩论里,
    各方历史发言的 role 一律是 assistant,模型无法从协议层分辨说话人;name 又只透传
    role/content 而被丢弃——工牌缝进正文是它抵达模型的唯一通道。无 name 的请求原样不动。"""
    content = m.get("content") or ""
    name = m.get("name")
    if not (name and content):
        return content
    if content.startswith(f"[{name}]"):   # 模型会学舌历史里的工牌自带前缀,不去重会逐轮叠加
        return content
    return f"[{name}] {content}"


def _prepare_persistent(payload: dict, sess: dict, system_in: str, prior: list,
                        user_input: str, **build_kwargs):
    """持续会话路径:同 tag 一场辩论共用一个存活 agent,调用方只发增量。
    - reset / 场次变化 → 新建(persona/lean/回复契约只在创建时装一次,后续请求的 system 忽略);
    - 同 turn 号重试 → 回滚到该轮起点,防增量重复追加;回滚前校验锚点对象仍在原位
      (轮内压缩会整体替换 messages,锚点失效则跳过回滚,宁重复不错删);
    - 增量与本轮输入照常追加,工具循环/账本/存档与无状态路径同构。
    不做重启恢复:进程重启后首个续用请求会因无存活会话而新建——调用方应整场重开。"""
    kwargs = dict(build_kwargs)
    tag = str(kwargs.pop("tag"))
    debate = _debate_no()
    with _LIVE_LOCK:
        rec = _LIVE.get(tag)
        if sess.get("reset") or rec is None or rec["debate"] != debate:
            # sid 必须全新唯一:build_agent 对已存在的 session_id 会载入其历史续会话——
            # 随机后缀保证 reset/进程重启都从零开始(设计决定:不做断点恢复)
            stamp = uuid.uuid4().hex[:6]
            sid = f"{tag}-第{debate}场-{stamp}" if debate else f"{tag}-持续-{stamp}"
            kwargs["session_id"] = sid
            agent = build_agent(**kwargs)
            if _DISPATCH_TOOL:
                agent.tools.register(_make_dispatch_tool(agent))
            if _DISPUTE_TOOL:
                agent.tools.register(_make_dispute_tool(agent))
            if _LEAN_SYSTEM:
                lean_system.strip_builtin_web_if_skill(agent, kwargs.get("cwd"))
                agent.messages[0]["content"] = _build_lean_system(agent)
            if system_in:
                agent.messages[0]["content"] += _PERSONA_HEADER + system_in
            agent.messages[0]["content"] += _REPLY_CONTRACT
            rec = {"agent": agent, "debate": debate, "sid": sid,
                   "last_turn": 0, "turn_base": len(agent.messages), "guard": None}
            _LIVE[tag] = rec
        agent = rec["agent"]
        turn = int(sess.get("turn") or rec["last_turn"] + 1)
        if turn == rec["last_turn"]:
            base, guard = rec["turn_base"], rec["guard"]
            if base <= len(agent.messages) and (base == 0 or agent.messages[base - 1] is guard):
                del agent.messages[base:]
        elif turn < rec["last_turn"]:
            raise ValueError(f"轮次号回退:{turn} < 已受理 {rec['last_turn']}")
        else:
            rec["turn_base"] = len(agent.messages)
            rec["guard"] = agent.messages[-1] if agent.messages else None
            rec["last_turn"] = turn
    d, k = _bump_seq()
    kind = str(sess.get("kind") or "") or _call_kind(user_input)
    agent._serve_meta = {"sid": f"{tag}-{d}-{k:02d}-{kind}" if d else rec["sid"],
                         "tag": tag, "debate": d, "seq": k, "kind": kind}
    agent.messages += prior
    _dump_request_context(agent, system_in, prior, user_input, payload.get("model", ""),
                          filename=f"request-{k:03d}-{kind}.json" if d else None)
    return agent, user_input


def _prepare(payload: dict, **build_kwargs):
    """把一次 OpenAI chat 请求拆成 (装好上下文的 agent, 本轮用户输入)。
    build_kwargs 透传 build_agent(测试注入 provider/cwd 用);tag 键在此消费(会话命名)。"""
    messages = payload.get("messages") or []
    system_in = "\n\n".join(m.get("content") or "" for m in messages
                            if m.get("role") == "system").strip()
    history = [m for m in messages if m.get("role") != "system"]
    if not history:
        raise ValueError("messages 里没有用户消息")
    # 末条按惯例是本轮输入(调用方总以 user 收尾);史料只留 role/content,别的键不透传给后端
    user_input = _stamp_name(history[-1])
    prior = [{"role": m.get("role", "user"), "content": _stamp_name(m)}
             for m in history[:-1]]

    sess = payload.get("mecode_session")
    if _PERSISTENT and isinstance(sess, dict) and build_kwargs.get("tag") is not None:
        return _prepare_persistent(payload, sess, system_in, prior, user_input, **build_kwargs)

    kwargs = dict(build_kwargs)
    tag = kwargs.pop("tag", None)
    meta = None
    if tag is not None and "session_id" not in kwargs:
        sid, debate, seq, kind = _alloc_session_id(str(tag), kwargs.get("cwd"), user_input)
        kwargs["session_id"] = sid
        meta = {"sid": sid, "tag": str(tag), "debate": debate, "seq": seq, "kind": kind}
    agent = build_agent(**kwargs)
    if meta:
        agent._serve_meta = meta          # 账本用(场次/发言序),挂在 agent 上随请求走
    if _DISPATCH_TOOL:
        agent.tools.register(_make_dispatch_tool(agent))
    if _DISPUTE_TOOL:
        agent.tools.register(_make_dispute_tool(agent))
    if _LEAN_SYSTEM:
        lean_system.strip_builtin_web_if_skill(agent, kwargs.get("cwd"))
        agent.messages[0]["content"] = _build_lean_system(agent)
    if system_in:
        agent.messages[0]["content"] += _PERSONA_HEADER + system_in
    agent.messages[0]["content"] += _REPLY_CONTRACT
    agent.messages += prior
    _dump_request_context(agent, system_in, prior, user_input, payload.get("model", ""))
    return agent, user_input


def _openai_response(answer: str, model: str, agent, reasoning: str = "") -> dict:
    """标准 OpenAI chat.completion 响应。usage:prompt 用后端回的精确值(没有则本地估算,
    与压缩同口径),completion 走估算——调用方一般只看 content,usage 尽量诚实但不苛求。"""
    prompt_tokens = agent.context_tokens or estimate_tokens(agent.messages, agent.tools.schemas())
    completion_tokens = estimate_tokens([{"role": "assistant", "content": answer}])
    meta = getattr(agent, "_serve_meta", None) or {}
    extra = {}
    if meta.get("dispatch"):
        extra["mecode_dispatch"] = meta["dispatch"]
    if meta.get("disputes"):
        extra["mecode_disputes"] = meta["disputes"]
    if meta.get("compacted"):
        extra["mecode_compacted"] = True
    return {
        **extra,
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": ({"role": "assistant", "content": answer, "reasoning_content": reasoning}
                        if reasoning else {"role": "assistant", "content": answer}),
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _run_and_capture(agent, user_input: str, emit=None) -> tuple[str, str]:
    """跑完一整轮,返回 (本轮最终正文, 思考)。本轮消息定位:跑完后从尾部找本轮 user
    消息、取其后切片——不能预取 len(messages) 当基准,轮内压缩会整体替换 messages
    使旧索引失效(思考丢、正文退化)。不用 agent.ask():它反查全部 messages,会把调
    用方装载的历史旧回答当答案。契约(见 _REPLY_CONTRACT)是末条自包含完整交付,故
    正常只返回末条(调用方原意:中间过程不进辩论);模型违约——主体写在带工具调用
    的消息里、末条只是收场白(实测丢过 97% 正文)——即末条不足本轮正文一半时,拼接
    全部正文与思考返回并告警。定位失败/被打断时用事件流累积兜底。
    emit(dict):实时显示事件回调(mecode_live 通道,仅供展示)——思考/正文碎片、
    工具起止、系统提示;不参与最终正文折叠,权威正文仍走返回值。"""
    deltas: list[str] = []
    rdeltas: list[str] = []
    for ev in agent.run_turn(user_input):
        if isinstance(ev, TextDelta):
            deltas.append(ev.text)
            if emit:
                emit({"kind": "text", "delta": ev.text})
        elif isinstance(ev, ReasoningDelta):
            rdeltas.append(ev.text)
            if emit:
                emit({"kind": "reasoning", "delta": ev.text})
        elif isinstance(ev, ToolStarted):
            if emit:
                try:
                    _args = json.dumps(ev.arguments, ensure_ascii=False)
                except (TypeError, ValueError):
                    _args = str(ev.arguments)
                emit({"kind": "tool", "id": ev.id, "name": ev.name, "args": _args[:300]})
        elif isinstance(ev, ToolResult):
            if emit:
                _r = ev.result or ""
                emit({"kind": "tool_done", "id": ev.id, "name": ev.name,
                      "preview": _r[:200], "chars": len(_r)})
        elif isinstance(ev, Notice):          # 压缩/达上限/空响应等系统提示:网关调用里外界原本无感知
            print(f"[serve] Notice: {ev.text}")
            if emit:
                emit({"kind": "notice", "text": ev.text})
            if "已压缩旧历史" in ev.text:      # 压缩信号透传调用方(mecode_compacted):引擎据此补发全量分歧账本
                _m = getattr(agent, "_serve_meta", None)
                if isinstance(_m, dict):
                    _m["compacted"] = True
    start = None
    for i in range(len(agent.messages) - 1, -1, -1):
        m = agent.messages[i]
        if m.get("role") == "user" and m.get("content") == user_input:
            start = i + 1
            break
    turn = agent.messages[start:] if start is not None else []
    reasonings = [m["reasoning_content"] for m in turn
                  if m.get("role") == "assistant" and m.get("reasoning_content")]
    reasoning = reasonings[-1] if reasonings else "".join(rdeltas).strip()
    parts = [c for m in turn
             if m.get("role") == "assistant" and (c := (m.get("content") or "").strip())]
    if not parts:
        return "".join(deltas).strip(), reasoning
    last, total = parts[-1], sum(len(p) for p in parts)
    if len(parts) == 1 or len(last) * 2 >= total:
        return last, reasoning
    print(f"[serve] 末条正文仅 {len(last)}/{total} 字,判定为收场白——已拼接本轮全部正文返回")
    return "\n\n".join(parts), "\n\n".join(reasonings) or reasoning


def handle_chat(payload: dict, **build_kwargs) -> dict:
    """一次完整调用:装上下文 → 跑完整个工具循环 → 折叠成一条 assistant 消息。"""
    agent, user_input = _prepare(payload, **build_kwargs)
    rid = _register(agent)
    try:
        answer, reasoning = _run_and_capture(agent, user_input)
    finally:
        _unregister(rid)
    _append_ledger(agent, user_input, answer)
    return _openai_response(answer, payload.get("model", ""), agent, reasoning)


def _sse_chunk(cid: str, model: str, delta: dict, finish: str | None = None,
               extra: dict | None = None) -> bytes:
    """一个标准 OpenAI chat.completion.chunk 的 SSE 帧。空 delta 用作心跳(解析器安全跳过);
    extra 合并进帧顶层(如 mecode_dispatch),标准解析器不受影响。"""
    obj = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if extra:
        obj.update(extra)
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # 类属性:main() 按命令行参数填(mode/mcp);测试可注入 build_kwargs
    build_kwargs: dict = {}

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:                     # 健康检查(部署脚本/编排方探活用)
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": f"没有 {self.path};POST /v1/chat/completions"}})

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path == "/v1/interrupt":
            self._send(200, {"interrupted": interrupt_all()})
            return
        if path == "/v1/debate/next":
            if _SHARED_DIR is None:
                self._send(400, {"error": {"message": "网关未配置 --shared-dir,场次功能未启用"}})
            else:
                self._send(200, {"debate": _debate_next()})
            return
        if path != "/v1/chat/completions":
            self._send(404, {"error": {"message": f"没有 {self.path};用 /v1/chat/completions"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": {"message": f"请求体不是合法 JSON:{e}"}})
            return
        if payload.get("stream"):
            self._stream_chat(payload)
            return
        try:
            self._send(200, handle_chat(payload, **self.build_kwargs))
        except ValueError as e:                   # 请求形状问题(如没有消息)
            self._send(400, {"error": {"message": str(e)}})
        except Exception as e:                    # 后端/工具层异常:OpenAI 风格错误,别裸 traceback
            self._send(500, {"error": {"message": f"{type(e).__name__}: {e}"}})

    def _stream_chat(self, payload: dict) -> None:
        """SSE 路径:agent 在工作线程跑完整循环,事件经队列实时转发为 mecode_live 帧
        (思考/正文碎片、工具起止——仅供展示);静默间隙发心跳保活;写失败 = 客户端断开
        (编排层取消)→ request_interrupt() 止损。权威正文仍在完成后按原契约整块发出
        (思考先行、分片、尾帧带 meta)+ [DONE],调用方不解析 live 帧也不受影响。"""
        try:
            agent, user_input = _prepare(payload, **self.build_kwargs)
        except ValueError as e:
            self._send(400, {"error": {"message": str(e)}})
            return
        model = payload.get("model", "")
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        result: dict = {}
        live_q: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                result["answer"], result["reasoning"] = _run_and_capture(
                    agent, user_input, emit=live_q.put)
            except Exception as e:                # noqa: BLE001 —— 错误进流帧,别掉线程里
                result["error"] = e
            finally:
                live_q.put(None)                  # 结束哨兵:转发循环据此收尾

        rid = _register(agent)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            try:
                self.wfile.write(_sse_chunk(cid, model, {"role": "assistant"}))
                self.wfile.flush()
                last_beat = time.time()
                while True:
                    try:
                        item = live_q.get(timeout=1.0)
                    except queue.Empty:
                        if time.time() - last_beat >= HEARTBEAT_SECS:
                            self.wfile.write(_sse_chunk(cid, model, {}))   # 心跳:空 delta
                            self.wfile.flush()
                            last_beat = time.time()
                        continue
                    if item is None:
                        break
                    batch = [item]
                    ended = False
                    while True:                   # 把积压一次取空,同类碎片合并成单帧
                        try:
                            nxt = live_q.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is None:
                            ended = True
                            break
                        batch.append(nxt)
                    merged: list[dict] = []
                    for it in batch:
                        if (merged and it["kind"] in ("text", "reasoning")
                                and merged[-1]["kind"] == it["kind"]):
                            merged[-1] = {"kind": it["kind"],
                                          "delta": merged[-1]["delta"] + it["delta"]}
                        else:
                            merged.append(it)
                    for it in merged:
                        self.wfile.write(_sse_chunk(cid, model, {}, extra={"mecode_live": it}))
                    self.wfile.flush()
                    last_beat = time.time()
                    if ended:
                        break
                worker.join()
            except OSError:                       # 客户端断开(Windows 上多为 10053/10054)
                agent.request_interrupt()         # 断连即中断:生成停、bash 杀,token 止损
                worker.join(30)                   # 等 agent 收尾(补孤儿 tool 结果、会话落盘)
                _append_ledger(agent, user_input, result.get("answer", ""), interrupted=True)
                return
        finally:
            _unregister(rid)
        if "error" not in result:
            _append_ledger(agent, user_input, result.get("answer", ""))
        try:
            if "error" in result:
                e = result["error"]
                # 错误走标准 chunk 而非裸 error 帧:调用方(SimpleChatAgent)只解析 choices/delta,
                # 裸帧会被整帧吞掉、静默变成空发言;哨兵文案命中其既有的重试判定词
                self.wfile.write(_sse_chunk(cid, model, {
                    "content": f"[No response from OpenClaw gateway] {type(e).__name__}: {e}"}))
                self.wfile.write(_sse_chunk(cid, model, {}, finish="stop"))
            else:
                # 思考先行,且长文分帧:整段单帧的 data: 行会撞 aiohttp 客户端 64KB 行缓冲上限
                reasoning = result.get("reasoning") or ""
                for i in range(0, len(reasoning), _SSE_PIECE):
                    self.wfile.write(_sse_chunk(cid, model, {"reasoning_content": reasoning[i:i + _SSE_PIECE]}))
                answer = result.get("answer") or ""
                if answer:
                    for i in range(0, len(answer), _SSE_PIECE):
                        self.wfile.write(_sse_chunk(cid, model, {"content": answer[i:i + _SSE_PIECE]}))
                else:
                    self.wfile.write(_sse_chunk(cid, model, {"content": ""}))
                meta = getattr(agent, "_serve_meta", None) or {}
                extra = {}
                if meta.get("dispatch"):
                    extra["mecode_dispatch"] = meta["dispatch"]
                if meta.get("disputes"):
                    extra["mecode_disputes"] = meta["disputes"]
                if meta.get("compacted"):
                    extra["mecode_compacted"] = True
                self.wfile.write(_sse_chunk(cid, model, {}, finish="stop", extra=extra or None))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:                           # 写尾帧时才发现断开:循环已结束,无需再中断
            pass

    def log_message(self, fmt, *args):            # 精简访问日志:一行一请求
        print(f"[serve] {self.address_string()} {fmt % args}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="mecode OpenAI 兼容网关(/v1/chat/completions)")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--cwd", default=None, help="工作区(权限根/会话归属/<cwd>/.mecode/skills 技能);默认当前目录")
    ap.add_argument("--mode", default="yolo", choices=["normal", "auto", "yolo"],
                    help="权限档位(默认 yolo:无头跑 skill 脚本需自动放行 bash)")
    ap.add_argument("--mcp", action="store_true", help="每请求同步连 MCP(默认关:网关场景用不上、拖慢每次调用)")
    ap.add_argument("--tag", default=None, help="专家编号:会话目录命名 <tag>-<场次>-<全场序>-<类型>,并写请求存档")
    ap.add_argument("--shared-dir", default=None,
                    help="多网关共享目录:场次计数 debate.json + 全场账本 第D场.jsonl(不配则关闭场次功能)")
    ap.add_argument("--lean-system", action="store_true",
                    help="辩论专属精简 system:替换编程助手模板,保留安全边界/技能索引/环境块,并内置检索优先级(default-search 技能优先)")
    ap.add_argument("--dispatch-tool", action="store_true",
                    help="注册 dispatch_speakers 工具:结构化承接调度决策(发言方式/人员/各自任务),经响应 mecode_dispatch 字段回传调用方")
    ap.add_argument("--dispute-tool", action="store_true",
                    help="注册 manage_disputes 工具:结构化承接分歧账本操作(open/update/close),经响应 mecode_disputes 字段回传调用方")
    ap.add_argument("--persistent-sessions", action="store_true",
                    help="持续会话:请求带 mecode_session:{reset,turn,kind} 时同 tag 一场辩论共用一条会话,调用方只发增量;不带该字段的请求仍走无状态路径")
    args = ap.parse_args(argv)

    global _LEAN_SYSTEM, _DISPATCH_TOOL, _DISPUTE_TOOL, _PERSISTENT
    if args.lean_system:
        _LEAN_SYSTEM = True
    if args.persistent_sessions:
        _PERSISTENT = True
    if args.dispute_tool:
        _DISPUTE_TOOL = True
    if args.dispatch_tool:
        _DISPATCH_TOOL = True

    if args.cwd:
        os.chdir(args.cwd)                        # 程序入口名正言顺 chdir:bash/相对路径/技能发现都以它为准
    _Handler.build_kwargs = {"mode": args.mode, "mcp": args.mcp}
    if args.tag:
        _Handler.build_kwargs["tag"] = args.tag
    if args.shared_dir:
        global _SHARED_DIR
        _SHARED_DIR = Path(args.shared_dir)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"[serve] mecode 网关就绪 http://0.0.0.0:{args.port}/v1/chat/completions"
          f"  工作区={Path.cwd()}  模式={args.mode}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] 已停止")


if __name__ == "__main__":
    main()
