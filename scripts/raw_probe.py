"""显微镜脚本：把 provider 层的原始数据一层层摊开看。

目的不是"验证通过"，而是让你【亲眼看到】：
  1. 我们发出去的请求体长什么样
  2. 非流式响应的【完整原始 JSON】(tool_calls 藏在哪、arguments 为什么是字符串)
  3. 流式响应的【每一个 SSE 分片】逐条打印 —— 看清工具调用怎么被切碎、再按 index 拼回

用法：
    python scripts/raw_probe.py            # 默认两段都看
    python scripts/raw_probe.py stream     # 只看流式(分片最有料)
    python scripts/raw_probe.py nostream   # 只看非流式
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import httpx
from mecode.config import backend

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市当前的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}
MESSAGES = [{"role": "user", "content": "帮我查一下长沙现在的天气"}]
HEADERS = {"Authorization": f"Bearer {backend.api_key}"}
URL = f"{backend.base_url}/chat/completions"


def banner(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def show_request(stream: bool) -> dict:
    payload = {"model": backend.model, "messages": MESSAGES, "tools": [TOOL], "stream": stream}
    banner(f"① 我们发出去的请求体 (stream={stream})")
    print("POST", URL)
    print("Headers:", {**HEADERS, "Authorization": "Bearer ***"})
    print("Body (这就是 agent 每一轮发的东西):")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def raw_nonstream(client: httpx.Client) -> None:
    payload = show_request(stream=False)
    r = client.post(URL, headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    body = r.json()
    banner("② 非流式：服务器返回的【完整原始 JSON】")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    banner("③ 划重点：tool_call 藏在哪")
    msg = body["choices"][0]["message"]
    print("路径 choices[0].message.tool_calls =")
    print(json.dumps(msg.get("tool_calls"), ensure_ascii=False, indent=2))
    if msg.get("tool_calls"):
        args = msg["tool_calls"][0]["function"]["arguments"]
        print(f"\n注意 arguments 的类型是: {type(args).__name__}  值: {args!r}")
        print("它是【字符串】不是对象 —— 用前要 json.loads 一次：")
        print("  json.loads(args) =>", json.loads(args))


def raw_stream(client: httpx.Client) -> None:
    payload = show_request(stream=True)
    banner("② 流式：服务器吐回的【每一行原始 SSE】(逐条打印)")
    print("每行格式: data: {...json...}，最后是 data: [DONE]\n")

    acc: dict[int, dict] = {}        # 按 index 累积 name / 参数分片
    arg_pieces: list[str] = []       # 单独记下 arguments 的每一片，给你看拼接过程
    n = 0
    with client.stream("POST", URL, headers=HEADERS, json=payload, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            n += 1
            print(f"[SSE #{n:02d}] {line}")          # ← 原始字节, 一行不漏
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                continue
            # modelscope 等会发 choices 为空的块（纯 usage），跳过避免越界
            choices = json.loads(data).get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                slot = acc.setdefault(tc["index"], {"name": "", "args": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    arg_pieces.append(fn["arguments"])

    banner("③ 看清楚：arguments 是被切成这些碎片流过来的")
    for i, piece in enumerate(arg_pieces, 1):
        print(f"  第 {i} 片: {piece!r}")
    print("\n把碎片依次【累加】(就是 += 那一行干的事):")
    print("  拼接结果:", repr("".join(arg_pieces)))

    banner("④ 拼完的最终工具调用")
    for idx, slot in sorted(acc.items()):
        ok = _valid(slot["args"])
        print(f"  tool_call[{idx}]: name={slot['name']}  args={slot['args']!r}  合法JSON={ok}")
        if ok:
            print("     json.loads =>", json.loads(slot["args"]))


def _valid(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    print(f"后端 {backend.base_url}  模型 {backend.model}")
    with httpx.Client() as client:
        if mode in ("both", "nostream"):
            raw_nonstream(client)
        if mode in ("both", "stream"):
            raw_stream(client)


if __name__ == "__main__":
    main()
