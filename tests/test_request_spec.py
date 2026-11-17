"""合成请求规范：字段与分组的一致性。

这一层没有逻辑，只有**一张表**——所以测试盯的是"表自身的约束"，而它们错了的表现
都很安静：

- 某一项写了一个 ``GROUPS`` 里没有的组名 → 界面上那一格**整个不出现**
  （前端按组画，组名对不上就没人画它），而接口、引擎、静态审查一律不报错；
- 某一组写了 ``folded`` 而里面装的是常用的参数 → 那几格会被收起来，用户以为参数没了。
"""

from __future__ import annotations

import pytest

from app.domain.request_spec import GROUPS, PARAM_LABELS, SPEC_FIELDS, spec_payload

GROUP_KEYS = [group.key for group in GROUPS]

#: 界面上默认收起来的那三组——"通常不用动"（语言 / 静默 / 输出）。
FOLDED = {"silence", "language", "output"}


class TestGroups:
    def test_keys_are_unique(self):
        assert len(GROUP_KEYS) == len(set(GROUP_KEYS))

    def test_every_field_belongs_to_a_declared_group(self):
        """字段的组名必须是 ``GROUPS`` 里那一个——对不上，界面上就没人画它。"""
        unknown = sorted({f.group for f in SPEC_FIELDS} - set(GROUP_KEYS))
        assert unknown == []

    def test_every_group_has_something_in_it(self):
        """空组会在界面上留一个小标题、底下什么都没有。"""
        used = {f.group for f in SPEC_FIELDS}
        assert [key for key in GROUP_KEYS if key not in used] == []

    def test_only_the_rarely_touched_groups_are_folded(self):
        assert {g.key for g in GROUPS if g.folded} == FOLDED

    def test_silence_is_its_own_group(self):
        """静默**不在**「合成参数」里：要折叠就得能单独折，而它和语速/音量/音高
        不是一类（那三个管"念成什么样"，静默管"前后留多少空"）。"""
        silence = {f.key for f in SPEC_FIELDS if f.group == "silence"}
        assert silence == {"lead_silence", "lag_silence"}
        assert {f.key for f in SPEC_FIELDS if f.group == "expression"} == {
            "speed",
            "volume",
            "pitch",
            # 「引导指令」与它的配套档位 CFG：两者一起决定"引导生不生效"
            # （实测 BreezeTTS 2 在 CFG=1 时会整个忽略指令），所以它们是一对、
            # 也归在"这一段念成什么样"这一组里。见 §7.14。
            "instruction",
            "guidance_scale",
            # 种子：它是"同一样东西再念一遍"的那一个旋钮，归在"合成参数"里。
            "seed",
        }

    def test_the_guidance_pair_is_whole(self):
        """引导指令与 CFG 必须同时在表里——**少一个就是半个功能**。

        这不是风格问题：CFG=1 那一档会完全忽略指令（实测逐字节相同），
        只给 `instruction` 不给 `guidance_scale`，用户填了引导却发现"没反应"，
        而界面上没有任何地方能救它。
        """
        keys = {f.key for f in SPEC_FIELDS}
        assert {"instruction", "guidance_scale"} <= keys

    def test_combo_fields_keep_their_candidates(self):
        """``combo`` = 候选 + 手输。**候选是一条都不能少的**：它正是"不用手打"的那一半。"""
        combos = [f for f in SPEC_FIELDS if f.widget == "combo"]
        assert [f.key for f in combos] == ["emotion"]
        # 首项是空值（"不指定"），其余是词表——与 ``select`` 那几格同一个形状。
        assert combos[0].options[0][0] == ""
        assert len(combos[0].options) > 1

    def test_group_order_puts_the_folded_ones_last(self):
        """常动的在前、收起来的在后——否则展开一次就会把常用的那几格挤下去。"""
        folded_at = [index for index, group in enumerate(GROUPS) if group.folded]
        plain_at = [index for index, group in enumerate(GROUPS) if not group.folded]
        assert folded_at == list(range(min(folded_at), len(GROUPS)))
        assert max(plain_at) < min(folded_at)


class TestSeed:
    """种子那一格：**填一个数，或者要随机**——而它不是滑块。

    这一格在表里是唯一一个"画法由值本身决定"的（不是画法偏好）：0 到 21 亿的行程上
    滑块停不到想要的那个数，而这一格需要的动作只有填 / 换 / 随机。
    """

    @staticmethod
    def _field():
        return next(f for f in SPEC_FIELDS if f.key == "seed")

    def test_it_is_its_own_widget(self):
        assert self._field().widget == "seed"

    def test_it_is_a_number_in_the_request_body(self):
        """值本身是整数——别把 ``"1234"`` 这个字符串发出去。"""
        field = self._field()
        payload = next(f for f in spec_payload()["fields"] if f["key"] == "seed")
        assert payload["type"] == "number"
        assert payload["widget"] == "seed"
        assert field.step == 1

    def test_random_is_minus_one_and_the_floor_is_declared(self):
        """``-1`` 是"随机"那个约定，而下界就写着它——界面上那颗「随机」按钮发的也是它。"""
        assert self._field().min == -1

    def test_there_is_no_uniform_default(self):
        """种子的默认值**属于那台引擎的配置**，不属于这一份请求。

        所以这一格 ``default`` 留空 = 界面不替任何一家宣布"默认种子是几"，
        空着就是"没要求"（与语速 1.0、静默 0 那几格不是一回事）。
        """
        assert self._field().default is None

    def test_the_ceiling_matches_what_a_drawn_seed_can_be(self):
        """上限就是抽种子那个区间的上界（``randrange(0, 2**31)``）——手填也照这个范围。"""
        assert self._field().max == 2**31 - 1

    def test_it_has_a_chinese_label(self):
        assert PARAM_LABELS["seed"] == "种子"


class TestPayload:
    """``GET /api/v1/spec`` 发的就是这一包——界面照它画，所以形状错=界面错。"""

    @pytest.fixture()
    def payload(self) -> dict:
        return spec_payload()

    def test_groups_carry_the_folded_flag(self, payload):
        """前端**不攒**"哪些组该收起来"这份名单：它只在 ``folded`` 里。"""
        assert [g["key"] for g in payload["groups"]] == GROUP_KEYS
        assert [g["key"] for g in payload["groups"] if g["folded"]] == [
            key for key in GROUP_KEYS if key in FOLDED
        ]
        assert all("label" in group for group in payload["groups"])

    def test_every_field_points_at_a_group_in_the_payload(self, payload):
        declared = {group["key"] for group in payload["groups"]}
        assert {f["group"] for f in payload["fields"]} <= declared

    def test_every_field_has_a_chinese_label(self, payload):
        assert {f["key"] for f in payload["fields"]} == set(PARAM_LABELS)

    def test_no_explanatory_text_is_sent(self, payload):
        """界面上一格只有名字、单位、区间与状态——**一句说明都不下发**。"""
        flat = payload["fields"] + payload["groups"]
        assert not [item for item in flat if "note" in item or "hint" in item]

    def test_a_combo_field_still_declares_its_type(self, payload):
        """``combo`` 也要给类型：前端按它决定发出去的是数字还是字符串。

        情绪是词（字符串）；而同一种控件将来装数值候选时，类型要跟着候选走——
        这条与 ``select`` 那条判据是同一份（``_value_type`` 里两个 widget 走同一个分支）。
        """
        emotion = next(f for f in payload["fields"] if f["key"] == "emotion")
        assert emotion["widget"] == "combo"
        assert emotion["type"] == "string"
        assert emotion["options"]
