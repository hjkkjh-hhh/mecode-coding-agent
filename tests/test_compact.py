"""compact 及其辅助：压缩后的 CC 风格上下文重组。

固化的不变量：
- _last_content 取最后一条某 role 的正文
- _rescue_reads 按【区间】去重留最新（同一文件可占多个名额）、只认 read 类工具
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


# ---- 救援去重按【区间】而非【参数原文】/【文件】（_rescue_key） ----

def _reads(*calls):
    """calls: (id, arguments_json, content) —— 拼成 assistant 调用 + tool 结果的历史。"""
    out = []
    for cid, args, content in calls:
        out.append({"role": "assistant", "tool_calls": [
            {"id": cid, "function": {"name": "read_file", "arguments": args}}]})
        out.append({"role": "tool", "tool_call_id": cid, "content": content})
    return out


def test_救援去重_同一文件两种路径写法算一个(tmp_path):
    f = tmp_path / "q.py"
    f.write_text("x", encoding="utf-8")
    import os
    os.chdir(tmp_path)                                    # 让相对路径能 resolve 到同一处
    old = _reads(("c1", '{"path": "%s"}' % f.as_posix(), "v1"),
                 ("c2", '{"path": "q.py"}', "v2"))
    reads = _rescue_reads(old, ("read_file",), 5)
    assert len(reads) == 1, "绝对/相对两种写法应归一成同一个文件"
    assert reads[0][2] == "v2"                            # 留最新


def test_救援去重_同一文件不同区间各算一个(tmp_path):
    """按【区间】去重：正在攻坚的文件会被读很多段，只留一段的话模型压缩后立刻要重读回来
    （实测压缩后头 3 轮 read_file 频率是平均的 2.26 倍）。总量由 max_tokens 封顶。"""
    f = tmp_path / "big.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(("c1", '{"path": "%s", "offset": 0}' % p, "第1段"),
                 ("c2", '{"path": "%s", "offset": 200}' % p, "第2段"),
                 ("c3", '{"path": "%s", "offset": 400}' % p, "第3段"))
    reads = _rescue_reads(old, ("read_file",), 5)
    assert [r[2] for r in reads] == ["第1段", "第2段", "第3段"]   # 最早在前


def test_救援去重_同一文件同一区间仍算一个(tmp_path):
    """完全相同的重复读只留最新那份——这是去重要挡的，跟"不同区间"要区分开。"""
    f = tmp_path / "big.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(("c1", '{"path": "%s", "offset": 200, "limit": 60}' % p, "旧"),
                 ("c2", '{"path": "%s", "offset": 200, "limit": 60}' % p, "新"))
    reads = _rescue_reads(old, ("read_file",), 5)
    assert len(reads) == 1 and reads[0][2] == "新"


def test_救援_热点文件可占满名额(tmp_path):
    """count=3 时，同一文件最近 3 个区间应全部保住（旧行为只会留 1 个）。"""
    f = tmp_path / "big.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(*[(f"c{i}", '{"path": "%s", "offset": %d}' % (p, i * 100), f"段{i}")
                   for i in range(6)])
    reads = _rescue_reads(old, ("read_file",), 3)
    assert [r[2] for r in reads] == ["段3", "段4", "段5"]


def test_救援去重_参数顺序不同算一个(tmp_path):
    f = tmp_path / "a.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(("c1", '{"path": "%s", "offset": 0}' % p, "v1"),
                 ("c2", '{"offset": 0, "path": "%s"}' % p, "v2"))
    assert len(_rescue_reads(old, ("read_file",), 5)) == 1


def test_救援去重_不同文件不合并(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("x", encoding="utf-8"); b.write_text("y", encoding="utf-8")
    old = _reads(("c1", '{"path": "%s"}' % a.as_posix(), "A"),
                 ("c2", '{"path": "%s"}' % b.as_posix(), "B"))
    assert len(_rescue_reads(old, ("read_file",), 5)) == 2


def test_救援去重_参数不是合法json时退回原文(tmp_path):
    old = _reads(("c1", "不是json", "X"), ("c2", "也不是json", "Y"))
    assert len(_rescue_reads(old, ("read_file",), 5)) == 2   # 两个不同原文 → 两条


def test_救援_token预算耗尽即停(tmp_path):
    """预算封顶：原先总量靠「截断×条数」间接封住，调了截断值就失控。"""
    files = []
    for i in range(4):
        f = tmp_path / f"f{i}.py"; f.write_text("x", encoding="utf-8"); files.append(f.as_posix())
    big = "内" * 2000                                    # 每条约 1500 token
    old = _reads(*[(f"c{i}", '{"path": "%s"}' % p, big) for i, p in enumerate(files)])
    assert len(_rescue_reads(old, ("read_file",), 5, 0)) == 4          # 不限 → 全收
    got = _rescue_reads(old, ("read_file",), 5, 3000)                  # 预算 3000
    assert 1 <= len(got) < 4, f"预算该砍掉更早的，实收 {len(got)}"


def test_救援_单条超预算也保留最近一份(tmp_path):
    """一份都不留的话，压缩后模型手里没有任何原文。"""
    f = tmp_path / "huge.py"; f.write_text("x", encoding="utf-8")
    old = _reads(("c1", '{"path": "%s"}' % f.as_posix(), "内" * 50000))
    assert len(_rescue_reads(old, ("read_file",), 5, 10)) == 1


def test_救援_count为0则不限条数_只受token预算约束(tmp_path):
    """默认配置就是 count=0：同一文件读了 10 段、没超预算就 10 段全留。"""
    f = tmp_path / "big.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(*[(f"c{i}", '{"path": "%s", "offset": %d}' % (p, i * 100), f"段{i}")
                   for i in range(10)])
    assert len(_rescue_reads(old, ("read_file",), 0, 0)) == 10        # 都不限 → 全收
    assert len(_rescue_reads(old, ("read_file",), 3, 0)) == 3         # count>0 仍生效


def test_救援_count为0时token预算仍封顶(tmp_path):
    f = tmp_path / "big.py"; f.write_text("x", encoding="utf-8")
    p = f.as_posix()
    old = _reads(*[(f"c{i}", '{"path": "%s", "offset": %d}' % (p, i * 100), "x" * 3000)
                   for i in range(10)])
    got = _rescue_reads(old, ("read_file",), 0, 2000)
    assert 0 < len(got) < 10, "不限条数，但预算必须仍然卡住"


def test_救援_文件数上限_满了跳过新文件但已收文件的更早区间照收(tmp_path):
    a, b, c = (tmp_path / f"{n}.py" for n in "abc")
    for f in (a, b, c):
        f.write_text("x", encoding="utf-8")
    # 时间顺序：a旧段 → c → b → a新段（倒着走先遇到 a新段，再 b，再 c）
    old = _reads(("c1", '{"path": "%s", "offset": 0}' % a.as_posix(), "a旧"),
                 ("c2", '{"path": "%s"}' % c.as_posix(), "c"),
                 ("c3", '{"path": "%s"}' % b.as_posix(), "b"),
                 ("c4", '{"path": "%s", "offset": 500}' % a.as_posix(), "a新"))
    got = [r[2] for r in _rescue_reads(old, ("read_file",), 0, 0, 2)]
    assert got == ["a旧", "b", "a新"], "文件数上限 2 → 收 a 和 b；c 是第三个文件被跳过，a 的更早区间照收"


def test_救援_文件数上限为0则不限(tmp_path):
    files = []
    for n in "abcdef":
        f = tmp_path / f"{n}.py"; f.write_text("x", encoding="utf-8"); files.append(f)
    old = _reads(*[(f"c{i}", '{"path": "%s"}' % f.as_posix(), n)
                   for i, (f, n) in enumerate(zip(files, "abcdef"))])
    assert len(_rescue_reads(old, ("read_file",), 0, 0, 0)) == 6
    assert len(_rescue_reads(old, ("read_file",), 0, 0, 3)) == 3


def test_救援_同一文件多个区间只占一个文件名额(tmp_path):
    """文件数配额按【不同文件】算，不能被同一文件的多个区间吃掉。
    时间序 c → b → a旧 → a新；倒着走先遇 a 的两段，若重复计数会把 b 挤掉。"""
    a, b, c = (tmp_path / f"{n}.py" for n in "abc")
    for f in (a, b, c):
        f.write_text("x", encoding="utf-8")
    old = _reads(("c1", '{"path": "%s"}' % c.as_posix(), "c"),
                 ("c2", '{"path": "%s"}' % b.as_posix(), "b"),
                 ("c3", '{"path": "%s", "offset": 0}' % a.as_posix(), "a旧"),
                 ("c4", '{"path": "%s", "offset": 500}' % a.as_posix(), "a新"))
    got = [r[2] for r in _rescue_reads(old, ("read_file",), 0, 0, 2)]
    assert got == ["b", "a旧", "a新"], "a 的两段只该占 1 个文件名额，b 仍应收进来"
