"""clean_summary：剥 <analysis> 草稿、抽 <summary> 正文。

三态：① analysis+summary 只留 summary ② 裸文本原样（思考走了 reasoning 通道）
③ 只有 analysis 没 summary 时剥掉 analysis。
"""
from mecode.compact import _extract_tag, clean_summary


def test_analysis加summary_只留summary正文():
    raw = "<analysis>\n这是草稿思考\n</analysis>\n\n<summary>\n正式摘要内容\n</summary>"
    assert clean_summary(raw) == "正式摘要内容"


def test_裸文本无标签_原样返回():
    assert clean_summary("就是一段没有标签的摘要") == "就是一段没有标签的摘要"


def test_只有analysis无summary_剥掉analysis留正文():
    assert clean_summary("<analysis>瞎想</analysis>\n实际摘要") == "实际摘要"


def test_extract_tag():
    assert _extract_tag("<summary>内容</summary>", "summary") == "内容"
    assert _extract_tag("没有这个标签", "summary") is None
    assert _extract_tag("<summary>只有左", "summary") is None     # 缺闭合标签
