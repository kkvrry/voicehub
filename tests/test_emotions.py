"""样例上的一条情绪串：一个词，或者几个词（「严肃、轻蔑」）。

守两件事：

1. **拆分只有一处**：写入端（前端按 ``EMOTION_SEP`` 拼）与读取端（这里拆）认同一个字，
   单值不特殊对待——老数据、扫描推导出来的值都是一个词，走同一条路；
2. **命中任意一种即算**：标两种情绪正是因为这两种念法都适用，不是"只认第一个"。
"""

from __future__ import annotations

from app.domain.emotions import EMOTION_SEP, emotion_matches, emotion_terms


class TestTerms:
    def test_a_single_word_is_one_term(self):
        assert emotion_terms("开心") == ("开心",)

    def test_several_words_split_in_order(self):
        assert emotion_terms(f"严肃{EMOTION_SEP}轻蔑") == ("严肃", "轻蔑")

    def test_blanks_and_repeats_drop(self):
        """手改过的 sidecar 里什么都能出现：空格、连着两个分隔符、同一个词写两遍。"""
        raw = f" 严肃 {EMOTION_SEP}{EMOTION_SEP} 严肃{EMOTION_SEP}轻蔑 "
        assert emotion_terms(raw) == ("严肃", "轻蔑")

    def test_nothing_is_nothing(self):
        assert emotion_terms("") == ()
        assert emotion_terms(f" {EMOTION_SEP} ") == ()


class TestMatches:
    def test_hits_any_of_them(self):
        assert emotion_matches(f"严肃{EMOTION_SEP}轻蔑", "轻蔑")
        assert emotion_matches(f"严肃{EMOTION_SEP}轻蔑", "严肃")

    def test_synonyms_count(self):
        """样例写的是「高兴」、行上要的是「开心」——同一个情绪。"""
        assert emotion_matches(f"高兴{EMOTION_SEP}轻蔑", "开心")

    def test_an_unknown_word_compares_by_itself(self):
        """自填词进不了词表，但它也可能正是要找的那一个。"""
        assert emotion_matches(f"恍惚{EMOTION_SEP}轻蔑", "恍惚")

    def test_another_emotion_does_not_match(self):
        assert not emotion_matches(f"严肃{EMOTION_SEP}轻蔑", "悲伤")

    def test_no_target_never_matches(self):
        """调用方没要情绪时不该命中任何一条（空目标 = 不比）。"""
        assert not emotion_matches("严肃", "")
