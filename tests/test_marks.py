"""正文里的语气词标签（``[[笑]]`` / ``[[停顿:0.4]]``）：解析、翻译、不支持就删。

这一层是**中转站自己的写法**，所以它对每一台引擎都是同一份；认不认由引擎那张
``mark_map`` 回答。这里钉的是"写法本身"那几条规则——尤其是**不支持要删**这条：
留着的下场是被当成字念出来（"左括号左括号笑右括号右括号"），比没有更糟。

以及「通用」那一刀怎么切（:func:`mark_groups`）——它是**跨引擎**算的，也是这一层
最容易做错的地方：**所有支持语气词的引擎的交集**，一台都不认的引擎不参与
（算进来交集恒为空）。同一概念的两种写法由 ``MARKS`` 的 ``aliases`` 归一，
作者不必记得住统一名。
"""

import pytest

from app.domain.marks import (
    MARKS,
    canonical_mark,
    engine_marks,
    mark_groups,
    mark_names,
    render_marks,
    strip_marks,
)

#: 一台"全都认"的假引擎（正面用例用）：写法里留 ``{sec}`` 给停顿。
ALL = {m.name: ("<#{sec}#>" if m.arg else f"({m.name})") for m in MARKS}


class TestRender:
    def test_a_mark_becomes_the_engine_writing(self):
        assert render_marks("你好[[笑]]啊", {"笑": "(laughs)"}) == "你好(laughs)啊"

    def test_two_marks_in_one_sentence(self):
        assert (
            render_marks("[[咳嗽]]……[[叹气]]好吧", {"咳嗽": "(coughs)", "叹气": "(sighs)"})
            == "(coughs)……(sighs)好吧"
        )

    def test_a_pause_mark_carries_seconds(self):
        assert render_marks("你好[[停顿:0.4]]啊", {"停顿": "<#{sec}#>"}) == "你好<#0.40#>啊"

    def test_a_pause_without_seconds_gets_a_natural_one(self):
        """不给秒数 = 一个自然的换气长度，**不是 0**：``0`` 的意思是"不留空"。"""
        assert render_marks("你好[[停顿]]啊", {"停顿": "<#{sec}#>"}) == "你好<#0.50#>啊"

    def test_an_unknown_mark_is_deleted(self):
        assert render_marks("你好[[口哨]]啊", {"笑": "(laughs)"}) == "你好啊"

    def test_everything_is_deleted_when_the_engine_knows_none(self):
        assert render_marks("[[笑]][[停顿:1]]你好", {}) == "你好"

    def test_plain_text_is_untouched(self):
        assert render_marks("今天天气不错。", ALL) == "今天天气不错。"

    @pytest.mark.parametrize("text", ["", "   ", "[[笑]]"])
    def test_odd_input_does_not_crash(self, text: str):
        render_marks(text, ALL)  # 不抛就行


class TestStrip:
    def test_all_marks_go_away(self):
        assert strip_marks("你好[[笑]][[停顿:0.4]]啊") == "你好啊"

    def test_the_writing_of_another_engine_is_not_our_mark(self):
        """``(laughs)`` 是 MiniMax 自己的写法，**不是**统一指令——不该被当指令动过。"""
        assert strip_marks("你好(laughs)啊") == "你好(laughs)啊"


class TestVocabulary:
    def test_every_mark_the_ui_shows_is_renderable(self):
        """界面给的按钮必须都在这张表里——不然插进去的东西没人认得。"""
        assert len(mark_names()) == len(MARKS) == len(set(mark_names()))

    def test_names_have_no_bracket_or_colon(self):
        """冒号是参数分隔符、方括号是边界——名字里有了它们就解析不出来了。"""
        for name in mark_names():
            assert not ({"[", "]", ":"} & set(name)), name

    def test_only_the_pause_takes_an_argument(self):
        assert [m.name for m in MARKS if m.arg] == ["停顿"]


class TestEnginePayload:
    """一台引擎那份（引擎面板里那张映射表）：排法照 MARKS，不照声明顺序。"""

    def test_the_order_follows_the_core_vocabulary(self):
        assert [m["name"] for m in engine_marks({"叹气": "(sighs)", "笑": "(laughs)"})] == [
            "笑",
            "叹气",
        ]

    def test_a_row_carries_the_writing_and_the_ui_words(self):
        row = engine_marks({"笑": "(laughs)"})[0]
        assert row["native"] == "(laughs)"
        assert row["label"] == "笑声"
        assert row["arg"] == ""

    def test_a_pause_is_marked_as_taking_an_argument(self):
        assert engine_marks({"停顿": "<#{sec}#>"})[0]["arg"] == "秒"

    def test_a_name_the_vocabulary_does_not_have_still_shows_up(self):
        """没收录的名字（这一家独有的概念）照声明顺序排在后面，**名字当短名**。

        另起一个中文名会让正文里的 ``[[…]]`` 与按钮上的字对不上。
        """
        rows = engine_marks({"笑": "(laughs)", "哨音": "(whistles)"})
        assert [r["name"] for r in rows] == ["笑", "哨音"]
        assert rows[1]["label"] == "哨音"

    def test_an_engine_that_knows_none_gets_an_empty_table(self):
        assert engine_marks({}) == []


class TestCanonical:
    """另一种写法 → 规范名。这一步替代了从前"作者必须记得住统一名"的人工约定。"""

    def test_a_platform_writing_reaches_the_canonical_name(self):
        assert canonical_mark("gasp") == "喘气"
        assert canonical_mark("喘息") == "喘气"
        assert canonical_mark("喘气") == "喘气"

    def test_an_unknown_name_is_itself(self):
        assert canonical_mark("哨音") == "哨音"

    def test_every_writing_belongs_to_one_concept_only(self):
        """别名不许同时指两个概念：那会让归一变随机。"""
        seen: dict[str, str] = {}
        for mark in MARKS:
            for name in (mark.name, *mark.aliases):
                assert seen.setdefault(name, mark.name) == mark.name, name

    def test_a_table_declaring_both_spellings_collapses_to_one_row(self):
        rows = engine_marks({"gasp": "(gasp)", "喘气": "(pant)"})
        assert [r["name"] for r in rows] == ["喘气"]
        assert rows[0]["native"] == "(pant)"  # 作者显式写的规范名那条说了算

    def test_the_canonical_row_wins_in_either_declaration_order(self):
        rows = engine_marks({"喘息": "(gasp)", "喘气": "(pant)"})
        assert rows[0]["native"] == "(pant)"

    def test_a_text_written_with_an_alias_still_gets_translated(self):
        assert render_marks("你好[[喘息]]啊", {"喘气": "(pant)"}) == "你好(pant)啊"


class TestMarkGroups:
    """「通用」= **支持标签的那几台的交集**；其余各归各的引擎。"""

    @staticmethod
    def _engines(*pairs):
        return [
            {"key": key, "label": label, "mark_map": mapping} for key, label, mapping in pairs
        ]

    def test_what_every_participant_knows_is_common(self):
        groups = mark_groups(
            self._engines(
                ("a", "甲", {"笑": "(laughs)", "叹气": "(sighs)"}),
                ("b", "乙", {"笑": "[笑]", "咳嗽": "[咳嗽]"}),
            )
        )
        assert [m["name"] for m in groups["common"]] == ["笑"]
        assert [(g["key"], [m["name"] for m in g["marks"]]) for g in groups["engines"]] == [
            ("a", ["叹气"]),
            ("b", ["咳嗽"]),
        ]

    def test_an_engine_that_knows_none_does_not_shrink_the_common(self):
        """不支持语气词的引擎**不参与交集**——算进来交集恒为空，那不是"通用"的意思。"""
        groups = mark_groups(
            self._engines(
                ("a", "甲", {"笑": "(laughs)", "叹气": "(sighs)"}),
                ("b", "乙", {}),  # GPT-SoVITS 那类：一条都不认
            )
        )
        assert [m["name"] for m in groups["common"]] == ["笑", "叹气"]
        # 它也不该在界面上凭空多出一组（它没有"自己多出来的"那几条）。
        assert groups["engines"] == []

    def test_with_one_participant_the_common_is_all_of_it(self):
        """只有一台支持时，交集就是它自己那一批——「通用」不该凭空空掉。"""
        groups = mark_groups(self._engines(("a", "甲", {"笑": "a", "咳嗽": "b"})))
        assert [m["name"] for m in groups["common"]] == ["笑", "咳嗽"]
        assert groups["engines"] == []

    def test_nobody_supports_marks_gives_nothing(self):
        groups = mark_groups(self._engines(("a", "甲", {}), ("b", "乙", {})))
        assert groups == {"common": [], "engines": [], "participants": []}

    def test_common_follows_the_core_order_not_the_declaration(self):
        """排法要稳定：换一台引擎，通用那一组不该换位置。"""
        groups = mark_groups(
            self._engines(
                ("a", "甲", {"打嗝": "x", "笑": "y", "叹气": "z"}),
                ("b", "乙", {"笑": "1", "叹气": "2", "打嗝": "3"}),
            )
        )
        assert [m["name"] for m in groups["common"]] == ["笑", "叹气", "打嗝"]

    def test_participants_are_the_ones_the_common_was_taken_over(self):
        """「通用」那两个字要能核对——界面得说清这是对哪几台求的。"""
        groups = mark_groups(
            self._engines(("a", "甲", {"笑": "x"}), ("b", "乙", {}), ("c", "丙", {"笑": "y"}))
        )
        assert groups["participants"] == [{"key": "a", "label": "甲"}, {"key": "c", "label": "丙"}]

    def test_a_third_engine_joins_without_touching_this_layer(self):
        """加一台引擎后交集自己重算——**这正是"通用"不用手写维护的意思**。

        新来的只认一条：另外两条就不再是通用，落到原来那两家各自那一组。
        """
        two = mark_groups(
            self._engines(("a", "甲", {"笑": "x", "叹气": "y"}), ("b", "乙", {"笑": "z", "叹气": "w"}))
        )
        three = mark_groups(
            self._engines(
                ("a", "甲", {"笑": "x", "叹气": "y"}),
                ("b", "乙", {"笑": "z", "叹气": "w"}),
                ("c", "丙", {"笑": "1"}),  # 新来的只认一条
            )
        )
        assert [m["name"] for m in two["common"]] == ["笑", "叹气"]
        assert [m["name"] for m in three["common"]] == ["笑"]
        # 它没有"多出来的"那几条，所以不单列一组。
        assert [(g["key"], [m["name"] for m in g["marks"]]) for g in three["engines"]] == [
            ("a", ["叹气"]),
            ("b", ["叹气"]),
        ]

    def test_the_same_concept_under_two_names_is_still_common(self):
        """同一个概念的两种写法由大库的别名归一，**作者不必记得住统一名**。

        ``gasp`` 与 ``喘息`` 都是 ``喘气`` 的别名：两台引擎各照自己的原文写法登记，
        交集照样算得出来。归不上的名字才是这一家的自有概念。
        """
        groups = mark_groups(
            self._engines(("a", "甲", {"喘气": "(pant)"}), ("b", "乙", {"gasp": "(gasp)"}))
        )
        assert [m["name"] for m in groups["common"]] == ["喘气"]
        assert groups["engines"] == []

    def test_a_name_no_alias_covers_falls_to_its_own_engine(self):
        """大库没收的名字不猜：那条留在这一家自己那一组，等作者决定补别名还是认下它。"""
        groups = mark_groups(
            self._engines(("a", "甲", {"喘气": "(pant)"}), ("b", "乙", {"哨音": "(whistle)"}))
        )
        assert groups["common"] == []
        assert [m["name"] for m in groups["engines"][1]["marks"]] == ["哨音"]

    def test_one_engine_missing_it_keeps_it_out_of_the_common(self):
        """**只要有一台不认，它就不是通用**——「通用」的字面就是"都认"。

        三台里两台认不算：那第三台切过去，这一条会被从正文里删掉。
        """
        groups = mark_groups(
            self._engines(
                ("a", "甲", {"笑": "x", "喘气": "(pant)"}),
                ("b", "乙", {"笑": "y", "喘气": "(gasp)"}),
                ("c", "丙", {"笑": "z"}),
            )
        )
        assert [m["name"] for m in groups["common"]] == ["笑"]
        assert [(g["key"], [m["name"] for m in g["marks"]]) for g in groups["engines"]] == [
            ("a", ["喘气"]),
            ("b", ["喘气"]),
        ]

    def test_a_tag_only_one_engine_knows_is_its_own(self):
        """只有一台认的不进通用：它不是"同时支持"，是这一家的独有能力。"""
        groups = mark_groups(
            self._engines(("a", "甲", {"笑": "x", "打嗝": "y"}), ("b", "乙", {"笑": "z"}))
        )
        assert [m["name"] for m in groups["common"]] == ["笑"]
        assert [m["name"] for m in groups["engines"][0]["marks"]] == ["打嗝"]

    def test_an_engine_that_accepts_any_tag_does_not_shrink_the_common(self):
        """认任意标签的引擎支持面按全集算：它确实发得出去。

        按"只登记了这几条"算的话，它会把别人的通用项压成自己那张表——那是错的：
        表外的标签它一样按 ``(名)`` 发出去。
        """
        groups = mark_groups(
            [
                {"key": "a", "label": "甲", "mark_map": {"笑": "x", "咳嗽": "y", "打嗝": "z"}},
                {"key": "b", "label": "乙", "mark_map": {"笑": "1"}, "mark_any": True},
            ]
        )
        assert [m["name"] for m in groups["common"]] == ["笑", "咳嗽", "打嗝"]
        own = [g for g in groups["engines"] if g["key"] == "b"]
        assert own[0]["any"] is True
        assert own[0]["marks"] == []  # 只摆它登记过的；其余靠自定义输入

    def test_an_engine_that_accepts_any_tag_keeps_its_group_when_it_has_nothing_own(self):
        """自定义输入框挂在组上：没有多出来的标签也要留着那一组。"""
        groups = mark_groups(
            [
                {"key": "a", "label": "甲", "mark_map": {"笑": "x"}},
                {"key": "b", "label": "乙", "mark_map": {"笑": "z"}, "mark_any": True},
            ]
        )
        assert [g["key"] for g in groups["engines"]] == ["b"]
        assert groups["engines"][0]["any"] is True

    def test_common_rows_carry_the_ui_words(self):
        groups = mark_groups(self._engines(("a", "甲", {"笑": "x"})))
        assert groups["common"][0]["label"] == "笑声"
        assert groups["common"][0]["arg"] == ""
