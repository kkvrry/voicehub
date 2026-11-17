"""声明式参数映射：**加一台引擎改的是表，不是代码**。

这一层是"中转站"的核心声明方式，所以它的每条规矩都要有断言钉住——
尤其是"没要求 ≠ 要求 0"与"用了没登记的换算器要报出来"这两条：
它们错了的表现都是**请求悄悄发出去**，而那是要花钱的。
"""

from __future__ import annotations

import pytest

from app.engines.mapping import (
    TRANSFORMS,
    EngineMapping,
    ParamMapping,
    apply_mapping,
    register_transform,
    transform_of,
)


class TestParamMapping:
    def test_accepts_dotted_path(self):
        assert ParamMapping("voice_id", "voice_setting.voice_id").target == "voice_setting.voice_id"

    @pytest.mark.parametrize("bad", ["voice setting", "voice_setting..id", "1abc", ".x", "a.", ""])
    def test_rejects_non_path_target(self, bad: str):
        """落点不像字段路径就**当场**报错——它错了的症状是请求体里凭空多一个怪键。"""
        with pytest.raises(ValueError, match="不像字段路径"):
            ParamMapping("x", bad)

    def test_defaults_are_skip_empty_not_required(self):
        item = ParamMapping("speed", "speed")
        assert item.skip_empty is True
        assert item.required is False
        assert item.transform == ""


class TestTransforms:
    def test_unknown_transform_raises(self):
        """**不静默恒等**——静默了的话"换算没生效"永远查不出来。"""
        with pytest.raises(KeyError, match="没有登记"):
            transform_of("sec_to_fortnight")

    def test_empty_name_means_identity(self):
        assert transform_of("")("abc") == "abc"
        assert transform_of("  ")("abc") == "abc"

    @pytest.mark.parametrize(
        ("name", "value", "expected"),
        [
            ("number", "0.9", 0.9),
            ("number", 2, 2),
            ("number", "3", 3),  # 整数就还它整数，别变成 3.0
            ("number", "abc", None),
            ("number", True, None),  # bool 是 int 的子类，得单独挡掉
            ("int", "24000.0", 24000),
            ("int", "3", 3),
            ("int", 2.5, 3),  # 半数**向外**：内置 round 是"半数取偶"，这里给 2
            ("int", -2.5, -3),
            ("int", "abc", None),
            ("sec_to_ms", 1.5, 1500),
            ("ms_to_sec", 1500, 1.5),
            ("percent_to_ratio", 50, 0.5),
            ("ratio_to_percent", 0.5, 50),
            ("bool", "是", True),
            ("bool", "off", False),
            ("bool", "也许", None),
            ("str", "  x  ", "x"),
        ],
    )
    def test_converters(self, name: str, value: object, expected: object):
        assert transform_of(name)(value) == expected

    def test_int_converter_returns_a_real_int(self):
        """比"值相等"更狠的一条：``24000 == 24000.0`` 在 Python 里成立，而 JSON 里
        ``24000`` 与 ``24000.0`` 是**两种写法**——按类型校验的网关只认前者。

        这条换算器的用途就是"平台那一格只收整数"，所以断言必须落在**类型**上。
        """
        converted = transform_of("int")("24000.0")
        assert type(converted) is int
        assert converted == 24000

    def test_registered_transform_is_visible_in_the_registry(self):
        register_transform("smoke_double", lambda value: value * 2)
        try:
            assert TRANSFORMS["smoke_double"](3) == 6
        finally:
            TRANSFORMS.pop("smoke_double", None)


class TestApplyMapping:
    def test_writes_nested_path_and_constants(self):
        mapping = EngineMapping(
            params=(ParamMapping("text", "text"), ParamMapping("voice_id", "voice_setting.voice_id")),
            constants={"stream": False, "audio_setting.format": "mp3"},
        )
        body = apply_mapping(mapping, {"text": "你好", "voice_id": "MX_1"})
        assert body == {
            "stream": False,
            "audio_setting": {"format": "mp3"},
            "text": "你好",
            "voice_setting": {"voice_id": "MX_1"},
        }

    def test_constants_do_not_override_upstream_values(self):
        """固定量先落，映射后落——上游明确给的那一项要赢。"""
        mapping = EngineMapping(params=(ParamMapping("format", "format"),), constants={"format": "wav"})
        assert apply_mapping(mapping, {"format": "flac"})["format"] == "flac"

    def test_absent_source_is_left_out_entirely(self):
        """缺的这一项**整个不出现在请求体里**——省略才是"没要求"的表示法。"""
        mapping = EngineMapping(params=(ParamMapping("emotion", "voice_setting.emotion"),))
        assert apply_mapping(mapping, {}) == {}

    def test_blank_string_counts_as_absent(self):
        mapping = EngineMapping(params=(ParamMapping("emotion", "voice_setting.emotion"),))
        assert apply_mapping(mapping, {"emotion": "   "}) == {}

    def test_zero_is_a_value_not_an_absence(self):
        """**语速 0 与不提语速是两回事**——0 必须发得出去。"""
        mapping = EngineMapping(params=(ParamMapping("speed", "voice_setting.speed", transform="number"),))
        assert apply_mapping(mapping, {"speed": 0}) == {"voice_setting": {"speed": 0}}

    def test_skip_empty_off_sends_whatever_it_got(self):
        """``skip_empty=False`` = "这一项每次都带"。

        于是空串会作为空串发出去（调用方明确要的就是"带一个空值"）；
        而**压根没给**的那一项会变成 JSON ``null``——两种都记在这里，
        免得将来有人看到请求体里多一个 ``null`` 时以为是 bug 而去"修"它。
        """
        mapping = EngineMapping(params=(ParamMapping("voice_id", "voice_id", skip_empty=False),))
        assert apply_mapping(mapping, {"voice_id": ""}) == {"voice_id": ""}
        assert apply_mapping(mapping, {}) == {"voice_id": None}

    def test_required_missing_raises_with_the_name(self):
        mapping = EngineMapping(params=(ParamMapping("text", "text", required=True),))
        with pytest.raises(ValueError, match="text"):
            apply_mapping(mapping, {})

    def test_required_blank_also_raises(self):
        """必填项给了个空串，等于没给——**发出去白花一次钱**，所以在这里拦。"""
        mapping = EngineMapping(params=(ParamMapping("text", "text", required=True),))
        with pytest.raises(ValueError, match="缺少这台引擎必填的参数"):
            apply_mapping(mapping, {"text": "   "})

    def test_unknown_transform_surfaces_as_value_error(self):
        mapping = EngineMapping(params=(ParamMapping("speed", "speed", transform="nope"),))
        with pytest.raises(ValueError, match="没有登记"):
            apply_mapping(mapping, {"speed": 1})

    def test_unconvertible_value_is_reported_not_dropped(self):
        """**"给了个用不了的值"与"没给"必须分开**。

        从前 ``speed="很快"`` 被按"没要求"扔掉：请求成功、平台用了默认语速，
        上游要的倍速就这么没了——而"请求成功"让人根本不会去查。
        """
        mapping = EngineMapping(params=(ParamMapping("speed", "speed", transform="number"),))
        with pytest.raises(ValueError, match="用不了的值"):
            apply_mapping(mapping, {"speed": "很快"})

    def test_unconvertible_value_still_fails_when_required(self):
        mapping = EngineMapping(
            params=(ParamMapping("speed", "speed", transform="number", required=True),)
        )
        with pytest.raises(ValueError, match="speed"):
            apply_mapping(mapping, {"speed": "abc"})

    def test_blank_is_absence_not_a_bad_value(self):
        """空串/空格仍然是"没要求"——那不是"给了个坏值"，别把它也报成错。"""
        mapping = EngineMapping(params=(ParamMapping("speed", "speed", transform="number"),))
        assert apply_mapping(mapping, {"speed": ""}) == {}
        assert apply_mapping(mapping, {"speed": "   "}) == {}
        assert apply_mapping(mapping, {"speed": None}) == {}

    def test_drop_unmapped_skips_only_that_one_cell(self):
        """认不出且开了 ``drop_unmapped`` → **只剔这一格**，其余照发。

        情绪是典型：少了它平台照样能念，只是念得平。把它跟"必填"或
        "换算不了就报错"混在一起，一次请求会整个失败，那才是浪费。
        （这里用 ``bool`` 换算器造一个"认不出"：它不是 ``None`` 的分支走不到，
        而 ``"也许"`` 确实换不出来。）
        """
        mapping = EngineMapping(
            params=(
                ParamMapping(
                    "emotion", "voice_setting.emotion", transform="bool", drop_unmapped=True
                ),
                ParamMapping("speed", "voice_setting.speed", transform="number"),
            )
        )
        body = apply_mapping(mapping, {"emotion": "也许", "speed": 1.2})
        assert "emotion" not in body["voice_setting"]
        assert body["voice_setting"]["speed"] == 1.2

    def test_dropped_names_are_handed_back_so_the_caller_can_say_so(self):
        """剔掉了却不说 = **哑巴**。

        用户配了「也许」，听到的是"没按这个来"，界面上什么提示都没有——
        这是查不出来的那种错。所以收集器要把名字交出去，由调用方回话。
        """
        dropped: list[str] = []
        mapping = EngineMapping(
            params=(
                ParamMapping(
                    "emotion", "voice_setting.emotion", transform="bool", drop_unmapped=True
                ),
            )
        )
        apply_mapping(mapping, {"emotion": "也许"}, dropped)
        assert dropped == ["emotion"]

    def test_a_missing_value_is_never_counted_as_dropped(self):
        """没给 ≠ 被剔掉。

        收集器要是把"压根没配"的项也算进剔除，界面会到处报"引擎不支持
        你没填的这一项"——那是噪声，会让人学会无视提示。
        """
        dropped: list[str] = []
        mapping = EngineMapping(
            params=(
                ParamMapping(
                    "emotion", "voice_setting.emotion", transform="bool", drop_unmapped=True
                ),
            )
        )
        apply_mapping(mapping, {}, dropped)
        assert dropped == []

    def test_without_drop_unmapped_the_value_is_refused(self):
        """默认不动：**别家引擎不受影响**。

        这条机制是为一个具体参数（情绪）加的，不该顺手改掉所有引擎的
        容错口径——除情绪外，"认不出"仍然是错，因为静默丢掉用户明确
        给的值是更坏的那种失败。
        """
        dropped: list[str] = []
        mapping = EngineMapping(
            params=(ParamMapping("speed", "voice_setting.speed", transform="number"),)
        )
        with pytest.raises(ValueError, match="用不了的值"):
            apply_mapping(mapping, {"speed": "很快"}, dropped)
        assert dropped == []

    def test_describe_is_a_flat_table_for_the_ui(self):
        mapping = EngineMapping(
            params=(
                ParamMapping(
                    "voice_id",
                    "voice_setting.voice_id",
                    required=True,
                    note="平台音色 ID。",
                    warning=True,
                ),
            ),
            cautions=("参考音频由核心在发送前转码。",),
        )
        row = mapping.describe()[0]
        assert row == {
            "source": "voice_id",
            "target": "voice_setting.voice_id",
            "transform": "",
            "transform_label": "原样",
            "required": True,
            "skip_empty": True,
            "drop_unmapped": False,
            "note": "平台音色 ID。",
            "warning": True,
            "label": "",
        }
        assert mapping.cautions == ("参考音频由核心在发送前转码。",)
        assert mapping.required_sources() == ("voice_id",)
        assert mapping.targets() == ("voice_setting.voice_id",)

    def test_does_not_mutate_the_input_values(self):
        values = {"text": "你好", "speed": "1.2"}
        mapping = EngineMapping(params=(ParamMapping("speed", "speed", transform="number"),))
        apply_mapping(mapping, values)
        assert values == {"text": "你好", "speed": "1.2"}
