"""引擎的语言面：念得了哪些语言、音色能不能跨语言、方言走哪条路。

这三样**不是** :data:`CAPABILITY_LABELS` 里的开关——它们不 gate 任何按钮或页签，
所以不占 ``Capabilities`` 的布尔位。展示分两处：

- 「跨语言合成」是能力名，**只摆一枚标签、不写解释**，在「能力」卡里；
- 语言与方言是清单，摆在下方的「支持语言」一栏：一行语言、一行方言。
  同时算方言的词（粤语）只在方言里出现一次。

这里钉住两件事：
1. 声明怎么变成界面（标签样式；哪些不画；清单怎么去重）；
2. 每台内置引擎声明了什么——改口径必须先改这张表，顺带改插件注释里的出处。
"""

from __future__ import annotations

import pytest

from app.engines.base import (
    Capabilities,
    EnginePlugin,
    language_items,
    language_names,
    supported_language_codes,
)
from app.engines.registry import clear_cache, list_engines

#: 每台内置引擎的语言面 ``(语言代码, 是否跨语言, 方言)``。``None`` = 官方没写、也不声明。
#:
#: 数字与说法都来自各插件注释里记的出处（官方 README / 平台枚举 / 服务端白名单），
#: 不是"看着像"补的：MiniMax 40 种枚举里统一词表能填 17 个；VoxCPM2 官方 30 种语言
#: 里对得上 15 个 + 9 种方言；dots.tts 的覆盖是从 24 语言评测集读出来的。
EXPECTED: dict[str, tuple[tuple[str, ...], bool | None, tuple[str, ...]]] = {
    "minimax": (
        ("zh", "yue", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar",
         "nl", "tr", "uk", "id", "vi"),
        True,
        ("粤语",),
    ),
    "fish_audio": (
        ("zh", "yue", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar",
         "nl", "tr", "uk", "id", "vi"),
        True,
        ("粤语",),
    ),
    "kitta": (
        ("zh", "yue", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar",
         "nl", "tr", "uk", "id", "vi"),
        True,
        ("粤语",),
    ),
    "breeze_tts": (("zh", "en"), None, ()),
    "cosyvoice3": (
        (
            "zh",
            "yue",
            "cmn-cd",
            "cmn-wh",
            "cmn-xa",
            "cmn-zz",
            "cmn-jn",
            "cmn-nj",
            "wuu-sz",
            "wuu-hz",
            "nan-qz",
            "hsn-cs",
            "en",
            "ja",
            "ko",
            "de",
            "es",
            "fr",
            "it",
            "ru",
        ),
        True,
        (
            "粤语",
            "成都话",
            "武汉话",
            "西安话",
            "郑州话",
            "济南话",
            "南京话",
            "苏州话",
            "杭州话",
            "泉州话",
            "长沙话",
        ),
    ),
    "dots_tts": (
        ("zh", "yue", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar",
         "nl", "tr", "uk", "id", "vi"),
        True,
        ("粤语",),
    ),
    "gpt_sovits": (("zh", "yue", "en", "ja", "ko"), True, ("粤语",)),
    "indextts2": (("zh", "en", "ja", "es", "ar"), True, ()),
    "mimo": (("zh", "en"), None, ()),
    "omnivoice": (
        ("zh", "yue", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar",
         "nl", "tr", "uk", "id", "vi"),
        None,
        ("粤语",),
    ),
    "voxcpm2": (
        ("zh", "en", "ja", "ko", "es", "fr", "de", "ru", "pt", "it", "ar", "nl",
         "tr", "id", "vi"),
        None,
        ("粤语", "四川话", "吴语", "东北话", "河南话", "陕西话", "山东话", "天津话", "闽南话"),
    ),
}


class _Bare(EnginePlugin):
    """什么都不声明的引擎：语言面必须整段消失，而不是画一句"不支持"。"""

    key = "bare"
    label = "Bare"


def _rows(caps: Capabilities) -> dict[str, str]:
    return {row["key"]: row["note"] for row in _Bare._capability_notes(caps)}


class TestCapabilityTag:
    def test_nothing_declared_draws_nothing(self):
        assert _rows(Capabilities()) == {}

    def test_cross_lingual_is_a_bare_tag(self):
        """能力名本身就是结论：标签后面不跟解释。"""
        assert _rows(Capabilities(cross_lingual=True)) == {"cross_lingual": ""}

    @pytest.mark.parametrize("value", [False, None])
    def test_no_cross_lingual_no_tag(self, value):
        assert "cross_lingual" not in _rows(Capabilities(cross_lingual=value))

    def test_languages_are_not_capability_rows(self):
        """语言是清单不是能力：它占「支持语言」那一栏，不在能力卡里占一行。"""
        assert _rows(Capabilities(languages=("zh", "en"), dialects=("粤语",))) == {}


class TestLanguageItems:
    def test_unknown_codes_are_shown_as_is(self):
        """词表外的代码原样显示（标签不是白名单，与音色的性别/语言同一口径）。"""
        assert language_names(("zh", "th")) == ["中文", "th"]

    def test_a_dialect_is_not_repeated_as_a_language(self):
        """粤语既是语言代码又是方言：清单里只出现一次（在方言那一行）。"""
        items = language_items(
            Capabilities(languages=("zh", "yue", "en"), dialects=("粤语",))
        )
        assert [item["name"] for item in items] == ["中文", "英语"]
        assert [item["code"] for item in items] == ["zh", "en"]

    def test_order_follows_the_declaration(self):
        items = language_items(Capabilities(languages=("en", "zh")))
        assert [item["name"] for item in items] == ["英语", "中文"]


class TestChineseDialectsAreNotLanguages:
    def test_cantonese_counts_as_both(self):
        """粤语既是统一词表里的语言代码（``yue``），也是中文方言——两张表都要有它。"""
        for key, (codes, _, dialects) in EXPECTED.items():
            if "yue" in codes:
                assert "粤语" in dialects, key

    def test_mandarin_is_not_a_dialect(self):
        for key, (_, _, dialects) in EXPECTED.items():
            assert "普通话" not in dialects, key


class TestEveryEngine:
    def test_the_table_covers_every_builtin_engine(self):
        """新装一台引擎必须在这里表态：语言面是会被无声漂移的那一类事实。"""
        assert set(list_engines()) == set(EXPECTED)

    @pytest.mark.parametrize("key", sorted(EXPECTED))
    def test_declaration_matches_the_table(self, key):
        clear_cache()
        caps = list_engines()[key].capabilities()
        assert (caps.languages, caps.cross_lingual, caps.dialects) == EXPECTED[key]

    @pytest.mark.parametrize("key", sorted(EXPECTED))
    def test_the_panel_gets_the_list_and_the_tag(self, key):
        clear_cache()
        data = list_engines()[key].describe()
        block = data["languages"]
        codes, cross, dialects = EXPECTED[key]

        assert block["codes"] == list(codes)
        assert block["dialects"] == list(dialects)
        # 判「所选语言认不认」的那一份 = 语言代码 + 方言归一成的代码。
        assert block["supported_codes"] == sorted(
            supported_language_codes(Capabilities(languages=codes, dialects=dialects))
        )
        shown = [item["name"] for item in block["items"]]
        # 同一个词不在两行里各摆一遍
        assert not (set(shown) & set(dialects))
        # 语言那一行 = 声明的语言减去方言里已经有的那几个
        assert set(shown) == set(language_names(codes)) - set(dialects)

        keys = [row["key"] for row in data["capability_notes"]]
        assert ("cross_lingual" in keys) == (cross is True)

    @pytest.mark.parametrize("key", sorted(EXPECTED))
    def test_the_note_is_a_card_subtitle(self, key):
        """说明不是挂在标签后面，而是「支持语言」那一栏的副标题。"""
        clear_cache()
        engine = list_engines()[key]
        assert engine.describe()["languages"]["note"] == engine.capabilities().language_note
        assert engine.describe()["languages"]["hint"] == engine.capabilities().language_hint


def test_supported_codes_are_what_the_synth_page_checks():
    """生成页「语言」栏按这一份判哪台引擎不认所选语言。"""
    clear_cache()
    engines = list_engines()
    omni_block = engines["omnivoice"].describe()["languages"]
    omni = set(omni_block["supported_codes"])
    assert "yue" in omni
    assert "cmn-xa" not in omni
    # 方言点不在声明里，但这一家给了说明：生成页摆「由参考音频决定」，
    # 不把它列成「不支持」。
    assert omni_block["hint"]
    vox = set(engines["voxcpm2"].describe()["languages"]["supported_codes"])
    # 方言在 capabilities 里登记的是说法，界面判的是代码。
    assert {"yue", "cmn-cd", "wuu-sz", "nan-qz"} <= vox


def test_a_dialect_without_a_code_is_not_a_supported_code():
    """统一词表里没有可选项的方言（东北话）不进集合，界面也没法选它。"""
    codes = supported_language_codes(Capabilities(languages=("zh",), dialects=("东北话",)))
    assert codes == frozenset({"zh"})


class TestPlacement:
    def test_the_tag_sits_after_the_feature_rows(self):
        clear_cache()
        keys = [row["key"] for row in list_engines()["minimax"].describe()["capability_notes"]]
        assert keys.index("cross_lingual") > keys.index("voice_cloning")
        assert keys[-1] == "cross_lingual"
