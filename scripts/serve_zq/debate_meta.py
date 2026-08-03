"""场次/全场序/调用类型/全场账本——辩论编排的元数据层。

共享目录(shared_dir)由门面 serve.py 按调用传入(--shared-dir 是门面的可变全局,
测试直接 monkeypatch 门面);本模块自身不持有可变配置。
"""
import json
import re
import threading
import time
from contextlib import contextmanager

from mecode import bootstrap
from mecode.session import SessionStore

INSTR_MARKERS = ("👉 [INSTRUCTION]:", "当前是 Round", "辩论已正式结束", "结束。请严格总结",
                 "【调度状态", "辩论进行至中场", "收官前的系统调参询问", "辩论收官",
                 "结案前复核")   # 后五个:自由调度制的主持人提示词。截取按最后出现的标记起,
                                 # 故不可收录会出现在提示词中段的短语(旧"请严格总结"截丢中场预算前缀)
ROUND_RE = re.compile(r"当前是 Round\s*(\d+)|Round\s*(\d+)\s*结束")

_SEQ_LOCK = threading.Lock()
_DEBATE_LOCK = threading.Lock()


def call_kind(text: str) -> str:
    """调用类型(进目录名后缀与账本字段):从本轮输入的固定标记推断。
    自由调度下主持人有多种调用形态,目录名带类型才能一眼分辨。
    持续会话协议里调用方可经 mecode_session.kind 显式声明,此推断即兜底。"""
    if "👉 [INSTRUCTION]:" in text:
        return "异议" if "【异议席】" in text else "发言"
    if "收官前的系统调参询问" in text:
        return "预算"
    if "辩论进行至中场" in text:
        return "中场"
    if "辩论收官" in text:
        return "收官"
    if "辩论已正式结束" in text:
        return "终报"
    if "结案前复核" in text:
        return "复核"
    if "【调度状态" in text:
        return "调度"
    return "调用"


def extract_instruction(text: str) -> str:
    """账本的"触发段"(主持人带的话/任务书):取最后一个已知标记起的尾段,没有标记取末 300 字。
    完整原文永远在 request.json,这里截取失败只影响账本可读性,不丢信息。"""
    pos = max((text.rfind(m) for m in INSTR_MARKERS), default=-1)
    seg = text[pos:] if pos >= 0 else text[-300:]
    return seg[:2000]


def extract_round(text: str):
    """调用方声明的轮数("当前是 Round N"/"Round N 结束"),取最后一次出现;没有则 None。
    专家侧常无此声明——其轮次 = 发言序(辩论循环保证每轮每专家恰好一次)。"""
    last = None
    for m in ROUND_RE.finditer(text):
        last = int(m.group(1) or m.group(2))
    return last


def debate_no(shared_dir) -> int:
    """当前场次(共享文件)。没配 shared-dir → 0(命名退回两段式);配了但没收过信号 → 1。"""
    if shared_dir is None:
        return 0
    try:
        n = int(json.loads((shared_dir / "debate.json").read_text(encoding="utf-8-sig")).get("n", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        n = 0
    return max(n, 1)


@contextmanager
def shared_lock(shared_dir, name: str):
    """共享目录上的跨进程文件锁(msvcrt,仅 Windows;不可用时退化为进程内锁)。
    多网关进程会并发读写 debate.json(并行点名各自分配全场序),线程锁不够用。"""
    with _DEBATE_LOCK:
        shared_dir.mkdir(parents=True, exist_ok=True)
        try:
            import msvcrt
        except ImportError:
            yield
            return
        with (shared_dir / name).open("a+") as lk:
            msvcrt.locking(lk.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lk.seek(0)
                msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)


def debate_next(shared_dir) -> int:
    """场次 +1、全场序清零(POST /v1/debate/next):编排后端在每场辩论开始时调一次。"""
    with shared_lock(shared_dir, ".debate.lock"):
        try:
            n = int(json.loads((shared_dir / "debate.json").read_text(encoding="utf-8-sig")).get("n", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            n = 0
        n += 1
        (shared_dir / "debate.json").write_text(json.dumps({"n": n, "seq": 0}), encoding="utf-8")
        return n


def bump_seq(shared_dir) -> tuple:
    """全场序 +1(共享计数,持续会话路径用:会话目录固定,但账本行仍需全场序);
    没配 shared-dir → (0, 0),账本随之关闭。"""
    d = debate_no(shared_dir)
    if not d:
        return 0, 0
    with shared_lock(shared_dir, ".debate.lock"):
        try:
            data = json.loads((shared_dir / "debate.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            data = {}
        k = int(data.get("seq", 0)) + 1
        data["n"] = int(data.get("n", 0)) or d
        data["seq"] = k
        (shared_dir / "debate.json").write_text(json.dumps(data), encoding="utf-8")
    return d, k


def alloc_session_id(tag: str, cwd, user_input: str, shared_dir) -> tuple:
    """会话名 <tag>-<场次>-<全场序>-<类型>(没配 shared-dir 退回 <tag>-<自增序>)。
    全场序 = 本场所有网关共用的调用序号(debate.json 计数,跨进程锁)——自由调度下
    专家自数的发言序不再携带时序信息,全场序让目录名直接回答"这是全场第几手",
    且与账本发言序字段一一对应;类型后缀 = 调度/发言/异议/中场/收官/预算/终报/复核。
    返回 (名, 场次, 全场序, 类型)。"""
    from pathlib import Path
    kind = call_kind(user_input)
    d, k = bump_seq(shared_dir)
    if d:
        return f"{tag}-{d}-{k:02d}-{kind}", d, k, kind
    # 无共享目录:退回旧式 <tag>-<n>,同前缀目录最大值+1(磁盘即计数器,跨重启连续)
    prefix = f"{tag}-"
    root = getattr(bootstrap.agent_config, "session_root", None)
    sessions = SessionStore(root=root, cwd=cwd or Path.cwd()).dir.parent
    with _SEQ_LOCK:
        n = 0
        if sessions.is_dir():
            for p in sessions.iterdir():
                num = p.name[len(prefix):]
                if p.is_dir() and p.name.startswith(prefix) and num.isdigit():
                    n = max(n, int(num))
        n += 1
        return f"{prefix}{n}", 0, n, kind


def append_ledger(agent, user_input: str, reply: str, shared_dir, interrupted: bool = False) -> None:
    """全场账本:每次发言追加一行到 <shared>/第D场.jsonl——打完账本即整场辩论的合并成品。
    自由调度制的多点名会让多个网关进程并发完成、同时追加同一文件,故经锁文件串行化
    (msvcrt 仅 Windows;不可用时退回裸追加);写失败只警告不拖垮请求。"""
    meta = getattr(agent, "_serve_meta", None)
    if shared_dir is None or not meta or not meta.get("debate"):
        return
    line = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session": meta["sid"],
        "expert": meta["tag"],
        "发言序": meta["seq"],                      # 全场统一序号(与会话目录名第三段一致)
        "kind": meta.get("kind", ""),
        "轮次声明": extract_round(user_input),
        "instruction": extract_instruction(user_input),
        "reply": reply,
    }
    if meta.get("dispatch"):
        line["dispatch"] = meta["dispatch"]        # 调度决策的结构化原文(工具提交)
    if meta.get("disputes"):
        line["disputes"] = meta["disputes"]        # 分歧账本操作的结构化原文(工具提交)
    if interrupted:
        line["interrupted"] = True
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(line, ensure_ascii=False) + "\n"
        ledger = shared_dir / f"第{meta['debate']}场.jsonl"
        try:
            import msvcrt
            with (shared_dir / ".ledger.lock").open("a+") as lk:
                msvcrt.locking(lk.fileno(), msvcrt.LK_LOCK, 1)     # 阻塞拿锁(内部自动重试)
                try:
                    with ledger.open("a", encoding="utf-8") as f:
                        f.write(text)
                finally:
                    lk.seek(0)
                    msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            with ledger.open("a", encoding="utf-8") as f:
                f.write(text)
    except OSError as e:
        print(f"[serve] 账本写入失败:{e}")
