"""验证 vLLM 后端：列模型 + 测「JSON 工具调用」在非流式 / 流式下是否都通。

这是 mecode 的第一块砖，也是 provider 层的种子：
我们故意直接打原始 /v1/chat/completions，看清工具调用 JSON 的真实往返，
而不是用 openai SDK 把它藏起来。

用法：
    pip install -r requirements.txt
    python scripts/check_backend.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 让脚本能 import 到 src/mecode
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from mecode.config import backend

# 一个最小工具定义（OpenAI 函数调用格式）。我们要看模型会不会
# 把它解析成结构化的 tool_calls，而不是把 XML 原样塞进 content。
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市当前的天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，例如 长沙"},
            },
            "required": ["city"],
        },
    },
}

MESSAGES = [{"role": "user", "content": "帮我查一下长沙现在的天气"}]

HEADERS = {"Authorization": f"Bearer {backend.api_key}"}


def hr(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def check_models(client: httpx.Client) -> None:
    hr("1. 列出模型 /v1/models")
    r = client.get(f"{backend.base_url}/models", headers=HEADERS, timeout=15)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    print("可用模型:", ids)
    if backend.model not in ids:
        print(f"⚠️  配置的 MECODE_MODEL='{backend.model}' 不在列表里——请求可能 404，"
              f"改 .env 里的 MECODE_MODEL 成上面之一。")


def check_nonstream_tool_call(client: httpx.Client) -> bool:
    hr("2. 非流式：工具调用是否返回 JSON tool_calls")
    payload = {
        "model": backend.model,
        "messages": MESSAGES,
        "tools": [WEATHER_TOOL],
        "stream": False,
    }
    r = client.post(f"{backend.base_url}/chat/completions",
                    headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    body = r.json()
    print("【完整原始响应（一次性返回，无分片）】")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    print("—" * 30)
    msg = body["choices"][0]["message"]
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        tc = tool_calls[0]["function"]
        print(f"✅ 拿到 JSON tool_call: name={tc['name']} args={tc['arguments']}")
        return True
    print("❌ 没有 tool_calls。content =", repr((msg.get("content") or "")[:300]))
    print("   （若 content 里能看到 <tool_call> 之类 XML，说明服务层解析器没转 JSON）")
    return False


def check_stream_tool_call(client: httpx.Client) -> bool:
    hr("3. 流式：跨多个 SSE delta 能否拼出完整 tool_calls")
    payload = {
        "model": backend.model,
        "messages": MESSAGES,
        "tools": [WEATHER_TOOL],
        "stream": True,
    }
    # 按 index 累积每个工具调用的 name / arguments 分片
    acc: dict[int, dict] = {}
    n = 0
    with client.stream("POST", f"{backend.base_url}/chat/completions",
                       headers=HEADERS, json=payload, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            n += 1
            print(f"[SSE #{n:03d}] {line}")        # ← 逐条打印原始片段
            if data == "[DONE]":
                break
            # modelscope 等会发 choices 为空的块（纯 usage 统计），跳过避免越界
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
                    slot["args"] += fn["arguments"]  # 关键：分片要累加，不是覆盖

    if acc:
        for i, slot in sorted(acc.items()):
            ok = _valid_json(slot["args"])
            mark = "✅" if ok else "⚠️"
            print(f"{mark} tool_call[{i}]: name={slot['name']} args={slot['args']!r} "
                  f"(args 是合法 JSON: {ok})")
        return all(_valid_json(s["args"]) for s in acc.values())
    print("❌ 流式下没拼出任何 tool_calls。")
    print("   （这正是你当年踩的坑类型：流式分片在服务层被吃掉/截断）")
    return False


def _valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def main() -> int:
    print(f"后端: {backend.base_url}  模型: {backend.model}")
    with httpx.Client() as client:
        try:
            check_models(client)
            ns = check_nonstream_tool_call(client)
            st = check_stream_tool_call(client)
        except httpx.HTTPError as e:
            print(f"\n💥 请求失败: {e}")
            print("   检查：隧道是否开（ssh -L 8200:...）、vLLM 是否起好、MECODE_BASE_URL 对不对。")
            return 2

    hr("结论")
    print(f"非流式工具调用: {'✅ 通过' if ns else '❌ 失败'}")
    print(f"流式工具调用:   {'✅ 通过' if st else '❌ 失败'}")
    if ns and st:
        print("\n🎉 后端就绪，JSON 工具调用两条链路都通——可以开建主循环了。")
        return 0
    print("\n先把失败的那条修好（多半是 --tool-call-parser / 模型名 / 隧道），再往下建。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
