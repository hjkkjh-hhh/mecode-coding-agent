"""compact 及其辅助：压缩后的 CC 风格上下文重组。

固化的不变量：
- _last_content 取最后一条某 role 的正文
- _rescue_reads 按路径去重留最新、只认 read 类工具
- compact 布局顺序正确、[2] 最后一轮 user/assistant 原文保留、无 recent 尾巴、
  system 之后注入项全部包 <system-reminder>、transcript_pointer 进摘要块
- on_compacted 回调拿到压缩后镜像（摘要已嵌在镜像里，不另传）
- 无历史返回 None；continuation_banner 含三要素
- estimate_tokens：CJK ≈0.75 token/字、ASCII ≈1/3，中文不因 JSON 转义被虚高
"""
from mecode.compact import (
    _last_content,
    _rescue_reads,
    compact,
    continuation_banner,
    estimate_tokens,
)


def _stub_summary(_messages):
    return "<summary>这是摘要正文</summary>"


def _history():
    """一段含两次读同一文件（应去重留最新）+ 末轮问答的历史。"""
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "读 a.py"},
        {"role": "assistant", "content": "好", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py 内容 v1"},
        {"role": "user", "content": "再读一次 a.py"},
        {"role": "assistant", "content": "好", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "a.py 内容 v2"},
        {"role": "user", "content": "上一轮问题：解释截断"},
        {"role": "assistant", "content": "上一轮回答：头尾对齐"},
    ]


def test_last_content_取最后一条某role正文():
    old = _history()[1:]   # 去掉 system
    assert _last_content(old, "user") == "上一轮问题：解释截断"
    assert _last_content(old, "assistant") == "上一轮回答：头尾对齐"
    assert _last_content([], "user") is None


def test_rescue_reads_按路径去重留最新():
    reads = _rescue_reads(_history()[1:], ("read_file",), 5)
    assert len(reads) == 1                     # a.py 读了两次 → 去重成一条
    name, args, content = reads[0]
    assert name == "read_file"
    assert content == "a.py 内容 v2"            # 留最新那次


def test_rescue_reads_只认read类工具():
    old = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "晴"},
    ]
    assert _rescue_reads(old, ("read_file",), 5) == []


def test_compact_无历史返回None():
    assert compact(_stub_summary, [{"role": "system", "content": "x"}]) is None


def test_compact_布局顺序与保留():
    res = compact(
        _stub_summary, _history(),
        read_tools=("read_file",), rescue_count=5,
        transcript_pointer="/tmp/x.json",
    )
    # [0]system [1]摘要(含存档指针) [2]last-user [3]last-assistant [4]read调用 [5]read内容
    assert res[0] == {"role": "system", "content": "你是助手"}
    assert "这是摘要正文" in res[1]["content"]
    assert "/tmp/x.json" in res[1]["content"]                   # 存档指针在摘要块里（摘要之后）
    assert "上一轮问题：解释截断" in res[2]["content"] and res[2]["role"] == "user"
    assert "上一轮回答：头尾对齐" in res[3]["content"] and res[3]["role"] == "assistant"
    assert "调用了 read_file 工具" in res[4]["content"]
    assert "a.py 内容 v2" in res[5]["content"]                  # 最新版本
    # 注：单一真相下 compact 不再存盘；全量历史由 transcript 逐条追加承载，指针只是路径串。


def test_compact_on_compacted回调拿到镜像():
    got = {}
    res = compact(_stub_summary, _history(), transcript_pointer="/x",
                  on_compacted=lambda img: got.update(image=img))
    assert got["image"] is res                 # 回调拿到的就是最终压缩镜像（agent 据此写 marker）
    assert "这是摘要正文" in got["image"][1]["content"]   # 摘要已嵌在镜像第2条，无需单独传出


def test_compact_无recent尾巴():
    res = compact(_stub_summary, _history(), transcript_pointer="/x")
    # 末条应是注入项（reminder 包裹），不是裸的当前问题——当前问题由 run_turn 在压缩后才追加
    assert "<system-reminder>" in res[-1]["content"]


def test_compact_注入项全部包reminder():
    res = compact(_stub_summary, _history(), transcript_pointer="/x")
    for m in res[1:]:                          # system 之后每一条
        assert "<system-reminder>" in m["content"]


def test_compact_assistant原文丢工具调用():
    # 末轮 assistant 带 tool_calls，重组后只保留 content、不带 tool_calls
    hist = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "答", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]
    res = compact(_stub_summary, hist, transcript_pointer="/x")
    asst = [m for m in res if m["role"] == "assistant"]
    assert asst and "tool_calls" not in asst[0]
    assert "答" in asst[0]["content"]


def test_continuation_banner():
    b = continuation_banner("摘要内容", transcript_path="/path/x.json")
    assert "续接而来" in b                              # 续接说明
    assert "按原文保留" in b                            # 最近一轮/读取原文保留
    assert "摘要内容" in b                              # 摘要正文嵌入
    assert "中断处继续" in b and "不要复述" in b         # 续接指令（对齐 CC）
    assert "/path/x.json" in b and "完整对话存档" in b   # 存档指针在摘要后


def test_continuation_banner_无路径不出现存档指针():
    b = continuation_banner("摘要内容")                 # 不传 transcript_path
    assert "完整对话存档" not in b
    assert "摘要内容" in b and "中断处继续" in b


# ---------- estimate_tokens：usage 缺失时的本地粗估 ----------

def test_estimate_tokens_中文075_英文除3():
    zh = [{"role": "user", "content": "中" * 300}]
    en = [{"role": "user", "content": "a" * 300}]
    assert 200 <= estimate_tokens(zh) < 300             # CJK ≈0.75 token/字（按中文优化词表校准）+ JSON 骨架
    assert estimate_tokens(en) <= 300 // 3 + 20         # ASCII ≈1/3 + JSON 骨架
    # 若 json.dumps 忘了 ensure_ascii=False，300 个中文变 1800 个 ASCII → 按 /3 估出 600+（近三倍虚高）；
    # 上面的 < 300 上界同时锁住这个回归


def test_estimate_tokens_混排与全角标点():
    mixed = [{"role": "user", "content": "解释 def foo(): 的含义，谢谢。"}]
    assert estimate_tokens(mixed) >= len("解释的含义谢谢，。")   # 宽字符（含全角标点）逐个计入


def test_estimate_tokens_工具schema计入():
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "read_file", "description": "读取文件内容"}}]
    assert estimate_tokens(msgs, tools) > estimate_tokens(msgs)   # 传 tools 必须变大（precise usage 同口径）
    assert estimate_tokens(msgs, None) == estimate_tokens(msgs)   # 不传/None 行为不变
