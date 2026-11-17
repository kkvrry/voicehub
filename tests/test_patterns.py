"""分章模式与文本处理规则：逐条对齐旧版语义。"""

from __future__ import annotations

import pytest

from app.producers.base import ProducerError
from app.producers.builtin.audiobook.patterns import (
    BRACKET_PRESETS,
    check_conservation,
    preprocess,
    split_by_pattern,
    text_payload,
)


def test_general_pattern_splits_titles_and_body() -> None:
    text = "第一章 起\n\n少年推门。\n「你来了。」\n\n第二章 承\n\n风进来。\n"
    result = split_by_pattern(text, "general")
    assert result.matched is True
    assert [title for title, _ in result.chapters] == ["第一章 起", "第二章 承"]
    # 标题单独存，正文里不再念一遍。
    assert result.chapters[0][1] == "少年推门。\n「你来了。」"


def test_preface_before_first_title_becomes_a_chapter() -> None:
    text = "书名页\n作者：某人\n\n第一章 起\n正文。\n"
    result = split_by_pattern(text, "general")
    assert [title for title, _ in result.chapters] == ["书名页", "第一章 起"]


def test_empty_titles_hang_until_a_chapter_has_body() -> None:
    """目录式的连续标题不碎成一堆空章：挂到下一个有正文的标题前。"""
    text = "第一章\n第二章\n\n故事开始了。\n"
    result = split_by_pattern(text, "general")
    assert len(result.chapters) == 1
    assert result.chapters[0][0] == "第一章"
    assert result.chapters[0][1] == "第二章\n\n故事开始了。"


def test_trailing_dangling_title_is_kept() -> None:
    """末尾只有一个标题：旧版会静默丢掉，这里补成一章。"""
    text = "第一章 甲\n内容。\n第二章 乙"
    result = split_by_pattern(text, "general")
    assert [title for title, _ in result.chapters] == ["第一章 甲", "第二章 乙"]
    assert result.chapters[1][1] == ""


def test_no_match_falls_back_to_one_chapter_with_warning() -> None:
    result = split_by_pattern("只有一段正文，没有标题。", "general")
    assert result.matched is False
    assert result.count == 1
    assert result.chapters[0][0] == "只有一段正文，没有标题。"
    assert any("整篇按一章" in item for item in result.warnings)


def test_digits_and_paren_modes() -> None:
    digits = split_by_pattern("1.\n甲。\n2.\n乙。\n", "digits")
    assert [title for title, _ in digits.chapters] == ["1.", "2."]
    paren = split_by_pattern("（一）\n甲。\n（二）\n乙。\n", "paren")
    assert [title for title, _ in paren.chapters] == ["（一）", "（二）"]


def test_conservation_counts_everything() -> None:
    text = "第一章 起\n\n故事开始了。\n"
    result = split_by_pattern(text, "general")
    report = check_conservation(text, list(result.chapters))
    assert report["ok"] is True
    assert report["source_chars"] == report["chapter_chars"]


def test_preprocess_dialog_keeps_content_and_drops_marks() -> None:
    result = preprocess('他说：“你好。”\n', dialog=["移除双引号 “ ”"])
    assert result.text == "他说：你好。\n"
    assert result.hits and result.hits[0].before == "“你好。”"
    assert result.hits[0].after == "你好。"


def test_preprocess_bracket_removes_with_content() -> None:
    result = preprocess("正文[1]继续。\n", bracket=["移除方括号数字"])
    assert result.text == "正文继续。\n"


def test_consecutive_note_lines_all_go() -> None:
    """相邻两条注释都要删掉：吃掉行尾换行符会让下一行不再是行首，于是隔行漏删。"""
    result = preprocess("正文\n[1] 甲。\n[2] 乙。\n下一段。", bracket=["移除方括号数字整行"])
    assert result.text == "正文\n下一段。"


def test_punctuation_only_lines_are_removed_whole() -> None:
    """整行只有标点才删：行首标点后接正文不算，分隔符与省略号行算。

    分隔符（``***``）、情绪省略号（``.......``）这类整行删；
    ``……他不知道。`` 只是句首带标点，动了就是漏字。
    """
    text = "……他不知道。\n***\n.......\n正文。\n！？\n　——\n１２３\n下一段。"
    result = preprocess(text, bracket=["移除纯标点行"])
    assert result.text == "……他不知道。\n正文。\n１２３\n下一段。"
    assert len(result.hits) == 4


def test_whole_line_presets_come_before_their_inline_twins() -> None:
    """表序就是「全选」的应用序：整行删除必须排在会吃掉它的局部删除之前。

    局部删除先跑的话 ``[1]`` 记号一没，「…整行」再没有锚可认，只剩一行光秃秃的
    注释文字被念出来——这正是全选取而不掉「移除方括号数字整行」的原因。
    """
    names = [item.name for item in BRACKET_PRESETS]
    for whole, inline in (
        ("移除方括号数字整行", "移除方括号数字"),
        ("移除注释标记整行", "移除注释标记"),
        ("移除广告声明行", "移除网址"),
    ):
        assert names.index(whole) < names.index(inline), f"{whole} 要排在 {inline} 前面"
    assert "移除带圈数字整行" in names


def test_note_lines_go_whole_when_every_preset_is_checked() -> None:
    """全选：``[1]`` / ``[注1]`` / ``①`` 三种注释行整行删掉，前后段落也不粘成一行。"""
    text = (
        "正文\n[1] 嘎杂子们，指不正经，调皮胡闹的人。\n[2] 另一条注释。\n"
        "[注3] 又一条。\n④还有一条。\n下一段。"
    )
    result = preprocess(text, bracket=[item.name for item in BRACKET_PRESETS])
    assert result.text == "正文\n下一段。"
    assert {hit.rule for hit in result.hits} == {
        "移除方括号数字整行",
        "移除注释标记整行",
        "移除带圈数字整行",
    }


def test_preprocess_custom_rule_and_illegal_regex() -> None:
    result = preprocess("甲乙丙", custom_dialog=("乙", "X"))
    assert result.text == "甲X丙"
    with pytest.raises(ProducerError, match="正则写错了"):
        preprocess("甲乙丙", custom_bracket="（[）")


def test_unknown_preset_is_reported() -> None:
    with pytest.raises(ProducerError, match="没有这条预设"):
        preprocess("正文", dialog=["不存在的规则"])


def test_excluding_one_hit_keeps_that_match() -> None:
    """排除表按「哪一条的第几处」点名：只留下被取消勾选的那一处。"""
    text = "“甲”和“乙”"
    preview = preprocess(text, dialog=["移除双引号 “ ”"])
    assert preview.text == "甲和乙"
    first = preview.hits[0].key

    result = preprocess(text, dialog=["移除双引号 “ ”"], exclude=[first])
    assert result.text == "“甲”和乙"
    assert [hit.key for hit in result.hits] == [first, preview.hits[1].key]


def test_excluding_a_middle_hit_does_not_duplicate_text() -> None:
    """被跳过的命中两侧内容各出现一次（回归：从前会把命中前的整段追加两遍）。"""
    text = "他说：“甲”。她说：“乙”。"
    preview = preprocess(text, dialog=["移除双引号 “ ”"])
    assert preview.text == "他说：甲。她说：乙。"
    middle = preview.hits[0].key

    result = preprocess(text, dialog=["移除双引号 “ ”"], exclude=[middle])
    assert result.text == "他说：“甲”。她说：乙。"


def test_hit_context_has_highlight_coordinates() -> None:
    result = preprocess("前面的话。“重点”后面的话。", dialog=["移除双引号 “ ”"])
    hit = result.hits[0]
    start, end = hit.context_at, hit.context_at + hit.context_len
    assert hit.context[start:end] == "“重点”"
    assert "前面的话" in hit.context


def test_payload_exposes_patterns_and_presets() -> None:
    payload = text_payload()
    assert payload["default_pattern"] == "general"
    assert {"general", "digits", "paren"} <= {item["key"] for item in payload["chapter_patterns"]}
    assert payload["dialog_presets"] and payload["bracket_presets"]
    # 表达式不外发：界面只读名字与示例。
    assert "pattern" not in payload["chapter_patterns"][0]
