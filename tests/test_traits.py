"""音色属性识别（:mod:`app.engines.traits`）——**用真实清单钉住**。

语料是 2026-10-12 从这台机器的 MiniMax 账号里拉下来的那份清单（303 条），存在
``tests/fixtures/minimax_voice_list.tsv``：``id / 界面名 / 说明`` 三列原样。

**为什么拿真数据测**：这条链上真正会出错的地方是**真实的写法**——``Chinese (Mandarin)``
的那对括号、``female-yujie`` 那种拼音名、``一位柔和的青年女性粤语声音`` 这种没有词边界的
中文，以及 ``Gentle_Senior``（"温柔**学姐**"）这种**看着像年龄的陷阱**。编出来的
``foo_zh`` 之类假名只能测"这个实现能跑"。

三条纪律各自的钉子：命名先散文后（:class:`TestTwoSignals`）、先出现的赢
（:class:`TestAge`）、**认不出就空着**（:class:`TestTheVoicesWeRefuseToGuess`）。
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from app.domain.models import (
    AGE_CHOICES,
    GENDER_CHOICES,
    LANGUAGE_CHOICES,
)
from app.engines.traits import (
    AGES,
    CHARACTERISTICS,
    GENDERS,
    LANGUAGES,
    ages_in,
    characteristics_in,
    detect_age,
    detect_characteristics,
    detect_gender,
    detect_language,
    genders_in,
    languages_in,
    read_traits,
)

FIXTURE = Path(__file__).parent / "fixtures" / "minimax_voice_list.tsv"

#: 真实清单里每种语言挑一条（``(voice_id, description, 期望代码)``）。
#: 括号里那个 ``Chinese (Mandarin)`` 是官方的原样写法，不是笔误。
REAL_VOICES: tuple[tuple[str, str, str], ...] = (
    ("Arabic_CalmWoman", "一位宁静的青年女性阿拉伯语声音。", "ar"),
    ("Cantonese_CuteGirl", "一位柔和可爱的青年女性粤语声音。", "yue"),
    ("Chinese (Mandarin)_Crisp_Girl", "一位温暖清脆的少女声音，标准普通话。", "zh"),
    ("Dutch_KindLady", "一位和蔼的青年女性荷兰语声音。", "nl"),
    ("English_Graceful_Lady", "一位优雅的青年女性英语声音。", "en"),
    ("French_Female_Friend", "一位友好的青年女性法语声音。", "fr"),
    ("German_SteadyMan", "一位沉稳的青年男性德语声音。", "de"),
    ("Indonesian_SoftGirl", "一位温柔的青年女性印尼语声音。", "id"),
    ("Italian_SensitiveWoman", "一位敏感的青年女性意大利语声音。", "it"),
    ("Japanese_IntellectualSenior", "一位知性的青年男性日语声音。", "ja"),
    ("Korean_SweetGirl", "一位甜美的青年女性韩语声音。", "ko"),
    ("Portuguese_ThoughtfulWoman", "一位深情的青年女性葡萄牙语声音。", "pt"),
    ("Russian_HandsomeChildhoodFriend", "一位亲切的青年男性俄语声音。", "ru"),
    ("Spanish_Sevillano", "一位热情的青年男性西班牙语声音。", "es"),
    ("Turkish_CalmWoman", "一位平静的青年女性土耳其语声音。", "tr"),
    ("Ukrainian_CalmWoman", "一位平静的青年女性乌克兰语声音。", "uk"),
    ("Vietnamese_kindhearted_girl", "一位善良的青年女性越南语声音。", "vi"),
)


def _corpus() -> tuple[tuple[str, str, str], ...]:
    """那份快照：``(id, 界面名, 说明)``。"""
    rows: list[tuple[str, str, str]] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        voice_id, name, description = (line.split("\t") + ["", ""])[:3]
        rows.append((voice_id, name, description))
    return tuple(rows)


CORPUS = _corpus()

#: **认不出性别的那 14 条**（语料是冻住的，所以这张单子也该冻住）。
#: 它们没有一个是漏认：``Santa Claus``/``Grinch`` 是名字，``卡通猪小琪`` 是一只猪，
#: ``智慧精灵`` 是精灵，``嘴硬竹马`` 的"竹马"说的是关系不是性别。
#: 哪天这张单子里少了一条，就是有人往表里加了"看着像"的词——那正是要拦住的事。
GENDER_BLANKS: tuple[str, ...] = (
    "Arnold",
    "Charming_Santa",
    "Chinese (Mandarin)_Stubborn_Friend",
    "Cute_Elf",
    "Grinch",
    "Korean_AthleticStudent",
    "Korean_ConfidentBoss",
    "Korean_GentleBoss",
    "Korean_LonelyWarrior",
    "Korean_WiseElf",
    "Rudolph",
    "Santa_Claus",
    "cartoon_pig",
    "tianxin_xiaoling",
)

#: **认不出年龄的那 20 条**。前 10 条只有英文名（里面没有任何年龄词）；
#: ``成熟女性音色`` 那 4 条与 ``French_FemaleAnchor`` 唯一的年龄线索是"成熟"——
#: 而"成熟"是音色词（"妩媚成熟的青年御姐"里它和"青年"并存），**不收**。
AGE_BLANKS: tuple[str, ...] = (
    "Arnold",
    "Attractive_Girl",
    "Charming_Lady",
    "Charming_Santa",
    "Cute_Elf",
    "French_FemaleAnchor",
    "Grinch",
    "Korean_AirheadedGirl",
    "Korean_ChildhoodFriendGirl",
    "Korean_WiseElf",
    "Rudolph",
    "Santa_Claus",
    "Serene_Woman",
    "Sweet_Girl",
    "cartoon_pig",
    "female-chengshu",
    "female-chengshu-jingpin",
    "female-tianmei",
    "female-tianmei-jingpin",
    "tianxin_xiaoling",
)


class TestTheRealCorpus:
    """拿 303 条真实音色跑一遍——**这张表值不值得信，看它。**"""

    def test_the_fixture_itself(self):
        assert len(CORPUS) == 303
        # 有说明的 263 条 / 没说明的 40 条——这两个数在文档里被引用，钉住它们。
        assert sum(1 for _v, _n, d in CORPUS if d.strip()) == 263

    def test_every_voice_yields_a_value_from_the_vocabulary(self):
        """认出来的值必须是词表里那几个——它们是**直接写进字段**的。"""
        gender_codes = {code for code, _ in GENDERS}
        age_codes = {code for code, _ in AGES}
        language_codes = {code for code, _ in LANGUAGES}
        for voice_id, name, description in CORPUS:
            traits = read_traits(voice_id=voice_id, name=name, description=description)
            assert traits.gender in gender_codes | {""}, voice_id
            assert traits.age in age_codes | {""}, voice_id
            assert traits.language in language_codes | {""}, voice_id

    def test_the_coverage(self):
        """识别率。语料冻住 + 表冻住 ⇒ 这两个数也是冻住的（改表就会红，改完要重新数）。"""
        counts = collections.Counter(
            read_traits(voice_id=v, name=n, description=d).gender or "(空)" for v, n, d in CORPUS
        )
        assert counts == {"male": 145, "female": 144, "(空)": 14}

        ages = collections.Counter(
            read_traits(voice_id=v, name=n, description=d).age or "(空)" for v, n, d in CORPUS
        )
        assert ages == {"young": 198, "middle": 66, "(空)": 20, "teen": 10, "senior": 6, "child": 3}

    def test_the_same_voice_is_never_read_two_ways(self):
        """同一个音色的 id / 界面名 / 说明**三路都认出的东西必须一致**。

        实测 163 条至少两路都认得出性别，**零冲突**——这条断言就是那句"实测"本身。
        真出现冲突时它红：那时候要决定的是"谁说了算"，而不是随手让某一路上风。
        """
        for voice_id, name, description in CORPUS:
            got = {
                code
                for code in (
                    detect_gender(voice_id),
                    detect_gender(name),
                    detect_gender(description),
                )
                if code
            }
            assert len(got) <= 1, f"{voice_id} 三路认出了 {got}"

    def test_the_voices_we_refuse_to_guess_stay_blank(self):
        """**认不出就空着，不猜**——这条是这门手艺的全部诚信所在。"""
        assert tuple(sorted(v for v, _n, _d in CORPUS if not detect_gender(v, _n, _d))) == (
            GENDER_BLANKS
        )
        assert tuple(sorted(v for v, _n, _d in CORPUS if not detect_age(_n, _d))) == AGE_BLANKS

    def test_how_much_of_the_corpus_we_dare_to_say(self):
        """把覆盖率**钉成具体的数**——它们是对这几张表的一次体检，不是装饰性的断言。

        哪天有人往表里塞了一颗"看着像"的词，这几条立刻红，而他得先解释：为什么
        一夜之间多认出了一批原本认不出的音色。认宽了很容易，难的是解释为什么认宽了。
        """
        assert len(CORPUS) == 303
        assert sum(1 for _v, _n, d in CORPUS if d) == 263  # 有说明的
        found = [
            voice_id
            for voice_id, name, description in CORPUS
            if detect_characteristics(name, description)
        ]
        assert len(found) == 206
        # 只算有说明的那部分：名字里也藏着一些特点（``傲娇御姐`` 的"傲娇"），
        # 所以纯靠说明是 193 条。
        assert (
            sum(1 for _v, n, d in CORPUS if d and detect_characteristics(n, d)) == 193
        )
        assert sum(1 for v, n, d in CORPUS if len(detect_characteristics(n, d)) > 1) == 88

    def test_a_recognized_trait_never_carries_a_code(self):
        """特点是**一串无代码的词**，与语言/性别/年龄（各一个代码）是两回事。

        这话听着像常识，但它决定了落点：没有代码的东西进不了"一格一个值"的字段，
        所以它落在 ``tags`` 里。这条断言把那个前提钉住。
        """
        for voice_id, name, description in CORPUS:
            words = detect_characteristics(name, description)
            assert all(word in CHARACTERISTICS for word in words), voice_id
            assert len(words) == len(set(words)), voice_id


class TestRealVoicesByLanguage:
    """每种语言都要有一条真实音色读得对。"""

    @pytest.mark.parametrize(("voice_id", "description", "code"), REAL_VOICES)
    def test_a_real_voice_of_every_language_is_recognized(self, voice_id, description, code):
        assert detect_language(voice_id, description) == code

    def test_mandarin_and_cantonese_never_merge(self):
        """MiniMax 自己把它们分开列（26 条 Mandarin / 6 条 Cantonese），合掉就再也分不回来了。"""
        assert detect_language("Chinese (Mandarin)_Gentleman", "") == "zh"
        assert detect_language("Cantonese_ProfessionalHost（F)", "") == "yue"

    def test_the_parentheses_in_the_name_are_part_of_the_real_spelling(self):
        """``Chinese (Mandarin)`` 带括号与空格——按整词匹配要能穿过它们。"""
        assert languages_in("Chinese (Mandarin)_News_Anchor") == ("zh",)

    def test_engine_reported_bcp47_codes_are_recognized(self):
        """识别引擎回报的是代码（SenseVoice 的 ``zh``），与名称一样落到同一个码。"""
        assert detect_language("zh") == "zh"
        assert detect_language("zh-CN") == "zh"
        assert detect_language("en_US") == "en"
        assert detect_language("yue") == "yue"

    def test_a_short_word_in_prose_is_not_a_language_code(self):
        """只在整段就是代码时认；散文里的 ``id`` 不能变成印尼语。"""
        assert detect_language("voice id") == ""


class TestGender:
    """性别：id 里有平台刻意的命名，界面名与说明里是中文说法。"""

    @pytest.mark.parametrize(
        ("voice_id", "name", "description", "expected"),
        [
            # 系统音色那一批：id 前缀就是答案，界面名多半也带
            ("male-qn-qingse", "青涩青年音色", "", "male"),
            ("female-shaonv", "少女音色", "", "female"),
            ("female-yujie", "御姐音色", "", "female"),
            ("clever_boy", "聪明男童", "", "male"),
            ("lovely_girl", "萌萌女童", "", "female"),
            # 拼音 id + 中文界面名：读的是界面名那个词
            ("bingjiao_didi", "病娇弟弟", "", "male"),
            ("diadia_xuemei", "嗲嗲学妹", "", "female"),
            ("junlang_nanyou", "俊朗男友", "", "male"),
            # 国际音色：id 里写着 Female/Male，界面名是英文
            ("French_Female_News Anchor", "Patient Female Presenter", "", "female"),
            ("French_MaleNarrator", "Male Narrator", "", "male"),
            # 说明里写着「一位……青年女性……声音」
            ("Chinese (Mandarin)_News_Anchor", "新闻女声", "", "female"),
            ("Korean_WiseTeacher", "Wise Teacher", "一位睿智的的中年男教师声音，韩语。", "male"),
            (
                "Portuguese_NaughtySchoolgirl",
                "Naughty Schoolgirl",
                "一位诱人顽皮的青年女学生声音，葡萄牙语。",
                "female",
            ),
        ],
    )
    def test_real_voices(self, voice_id, name, description, expected):
        assert read_traits(voice_id=voice_id, name=name, description=description).gender == expected

    def test_girl_inside_a_word_is_not_a_girl(self):
        """``Schoolgirl`` 里那个 ``girl`` 不算——整词匹配。那一条的性别写在中文说明里。"""
        assert detect_gender("Portuguese_NaughtySchoolgirl", "Naughty Schoolgirl") == ""
        assert (
            detect_gender(
                "Portuguese_NaughtySchoolgirl",
                "Naughty Schoolgirl",
                "一位诱人顽皮的青年女学生声音，葡萄牙语。",
            )
            == "female"
        )

    def test_man_inside_a_word_is_not_a_man(self):
        assert detect_gender("Spanish_DeterminedManager", "Determined Manager") == ""
        assert detect_gender("English_Trustworthy_Man", "Trustworthy Man") == "male"

    def test_the_words_that_look_like_gender_but_are_not(self):
        """两个踩过的坑，都在真实语料里。

        ``中性`` 说的是**音色**（"一位中性、专业的青年女性粤语主持人"是女性，不是"其他"）；
        ``竹马`` 说的是**关系**（"一位嘴硬心软、不羁的青年竹马声音"里没有性别信息）。
        """
        assert detect_gender("Cantonese_ProfessionalHost（F)", "一位中性、专业的青年女性粤语主持人声音。") == "female"
        assert detect_gender("X_Wise_Lady", "一位中性、智慧的中年女士声音。") == "female"
        assert detect_gender("Chinese (Mandarin)_Stubborn_Friend", "一位嘴硬心软、不羁的青年竹马声音。") == ""

    def test_a_silent_id_falls_back_to_the_description(self):
        """真实数据里 id 与界面名都没写、只有说明写了的那种。"""
        assert detect_gender("Arrogant_Miss", "嚣张小姐", "一位嚣张自信的青年女性声音。") == "female"
        assert detect_gender("Korean_GentleWoman", "Gentle Woman") == "female"


class TestAge:
    """年龄：**只读中文**（界面名 → 说明），先出现的那个赢。"""

    @pytest.mark.parametrize(
        ("name", "description", "expected"),
        [
            # 平台的说明句式是「一位{形容词}的{年龄}{性别}{角色}声音」——年龄在最前面
            ("沉稳高管", "一位沉稳可靠的中年男性高管声音，标准普通话。", "middle"),
            ("新闻女声", "一位专业、播音腔的中年女性新闻主播，标准普通话。", "middle"),
            ("温热少年", "一位呆萌可爱的少年男声。", "teen"),
            ("温暖少女", "一位温柔温暖的少年女声，标准普通话。", "teen"),
            ("花甲奶奶", "一位慈祥和蔼的老年女性奶奶声音，标准普通话。", "senior"),
            ("搞笑大爷", "一位爽朗幽默的老年男性大爷声音，带有北方口音。", "senior"),
            ("聪明男童", "", "child"),
            ("御姐音色", "", "young"),
            ("青涩青年音色", "", "young"),
        ],
    )
    def test_real_voices(self, name, description, expected):
        assert detect_age(name, description) == expected

    def test_the_first_age_word_wins(self):
        """一段里有两个年龄词时的规矩（实测 11 条，逐条对下来与平台的写法一致）。"""
        # "长者"在这里是角色名，平台的年龄写在它前面
        assert detect_age("Serene Elder", "一位沉静深思的中年男性长者声音") == "middle"
        # "老人"是"圣诞老人"的一部分
        assert detect_age("Santa Claus", "一位欢乐的中年男性圣诞老人声音") == "middle"
        # "少年老成"是成语
        assert detect_age("Intellectual Senior", "一位成熟知性的青年男性日语声音，听起来少年老成。") == "young"

    def test_maybe_the_most_important_one_the_latin_id_is_never_read(self):
        """``Senior`` 在平台自己的命名里指**学长**——按 id 读会把一批年轻人读成"老年"。

        ``Gentle_Senior`` 是"温柔学姐"、"IntellectualSenior" 是"青年学长"、
        ``Humorous_Elder`` 才是真的"长者"。所以年龄只看中文文本。
        """
        assert read_traits(voice_id="Chinese (Mandarin)_Gentle_Senior", name="温柔学姐").age == "young"
        assert read_traits(voice_id="Korean_IntellectualSenior", name="Intellectual Senior").age == ""
        assert (
            read_traits(
                voice_id="Korean_IntellectualSenior",
                name="Intellectual Senior",
                description="一位具有磁性、知性的青年学长声音，韩语。",
            ).age
            == "young"
        )
        assert detect_age("Gentle_Senior", "") == ""
        assert detect_age("IntellectualSenior", "Humorous_Elder") == ""

    def test_mature_is_a_timbre_word_not_an_age(self):
        """``成熟`` 不收：它和"青年"并存（"妩媚成熟的青年御姐"），是音色词。

        代价就是那 5 条只有"成熟"一条线索的音色年龄留空——**宁可空着**。
        """
        assert detect_age("傲娇御姐", "一位妩媚成熟的青年御姐声音。") == "young"
        assert detect_age("成熟女性音色", "") == ""
        assert detect_age("Female Anchor", "一位成熟的女性法语主播声音。") == ""

    def test_child_is_the_narrowest_one(self):
        """儿童只有 ``男童``/``女童`` 这种明确的词；``女孩`` 不是（平台把它和"青年"一起用）。"""
        assert detect_age("萌萌女童", "") == "child"
        assert detect_age("软软女孩", "一位温暖柔软的青年女性声音。") == "young"
        assert detect_age("Kind-hearted Girl", "一位明亮善良的青年女性声音。") == "young"


class TestTwoSignals:
    """几段字各自的职责与先后。"""

    def test_the_id_speaks_first(self):
        """id 是平台刻意的命名，说明是散文——散文可能顺带提到别的。"""
        assert detect_language("English_Graceful_Lady", "一位柔和的青年女性粤语声音。") == "en"
        assert detect_gender("male-qn-qingse", "温柔女声") == "male"

    def test_a_silent_id_falls_back_to_the_description(self):
        """真实数据里只有 2 条是这样的（id 没写语言，说明里写了）。"""
        assert detect_language("Arrogant_Miss", "一位嚣张自信的青年女性声音，标准普通话。") == "zh"
        assert detect_language("Robot_Armor", "……适合科幻内容的标准普通话。") == "zh"

    def test_the_display_name_is_the_platforms_own_label(self):
        """界面名是平台给这个音色的名字（``少女音色``），说明是散文——所以年龄先读界面名。

        代价是 2 条（``率真弟弟``/``清澈邻家弟弟``）取了界面名的"弟弟"而不是说明里的
        "少年"，两条都是 young。**这条优先级与语言那边同一条纪律**：命名的说法优先。
        """
        assert detect_age("率真弟弟", "一位认真率真的少年弟弟声音，标准普通话。") == "young"

    def test_nothing_is_read_from_empty_text(self):
        assert detect_language("", "") == ""
        assert detect_language() == ""
        assert languages_in("") == ()
        assert detect_gender("", "", "") == ""
        assert detect_age("", "") == ""
        assert read_traits() == read_traits(voice_id="")

    def test_a_value_is_reported_once(self):
        assert languages_in("粤语……还有粤语") == ("yue",)
        assert genders_in("男性，非常男性") == ("male",)

    def test_the_order_is_the_order_of_appearance(self):
        assert languages_in("粤语和日语") == ("yue", "ja")
        assert languages_in("日语和粤语") == ("ja", "yue")
        assert ages_in("中年与青年") == ("middle", "young")
        assert ages_in("青年与中年") == ("young", "middle")


class TestFalseFriends:
    """看着像但不是——**宁可漏，不可错认**。"""

    @pytest.mark.parametrize(
        "text",
        [
            "一位适合语音助手场景的青年女性声音。",
            "适合轻声耳语的内容。",
            "语调平缓，语气自然。",
            "双语主播，语言能力很强。",
            "适合中文字幕朗读。",  # 这一条**认得出**（中文）
        ],
    )
    def test_words_containing_the_character_for_language(self, text):
        """``语音``/``耳语``/``语气`` 里的那个「语」不是语言名——中文写法必须是完整的语言名。"""
        expected = "zh" if "中文字幕" in text else ""
        assert detect_language("Some_Name", text) == expected

    @pytest.mark.parametrize("voice_id", ["ChineseFood_Lady", "JapanesePodcast", "Englishman"])
    def test_english_words_must_stand_alone(self, voice_id):
        """英文按整词匹配：``ChineseFood`` 里那个 Chinese 后面紧跟着字母，不算。

        代价是"真叫 ``ChineseFood_Lady`` 的中文音色会被漏掉"，收益是**不会把
        别的词认成语言**。真实清单里所有语言名后面都跟着 ``_``、空格或 ``(``，
        所以这条边界没有丢掉任何一条真实数据；而错认的代价是一枚错的标签。
        """
        assert detect_language(voice_id, "") == ""

    @pytest.mark.parametrize("text", ["男性化了的嗓音", "一位女性化的少年音"])
    def test_a_gender_word_inside_a_longer_word(self, text):
        """中文按子串匹配，``男性``/``女性`` 会穿过 ``男性化``——那里说的还是性别，留着。

        真正要挡的是**音色词**（``中性``）与**关系词**（``青梅竹马``），
        它们在 :class:`TestGender` 里各有一条钉子。
        """
        assert detect_gender("X", text) in {"male", "female"}


class TestTables:
    """表本身的自洽——加一行时不必再手写一遍测试。"""

    @pytest.mark.parametrize(("table", "detect"), [(GENDERS, detect_gender), (AGES, detect_age)])
    def test_every_row_is_reachable_by_its_own_words(self, table, detect):
        for code, words in table:
            assert words, f"{code} 没有写任何可认的写法"
            for word in words:
                text = f"一位{word}的声音" if not word.isascii() else f"a {word} voice"
                assert detect("", text) == code, f"{word!r} 认不出 {code}"

    @pytest.mark.parametrize("table", [GENDERS, AGES, LANGUAGES])
    def test_codes_are_unique(self, table):
        codes = [code for code, _ in table]
        assert len(codes) == len(set(codes))

    @pytest.mark.parametrize("table", [GENDERS, AGES, LANGUAGES])
    def test_no_word_is_claimed_twice(self, table):
        """同一个写法归两行时，扫描的先后是**位置**决定的——认出来的就成了偶然。

        （这也是把 ``竹马`` 从性别表里删掉的第二个理由：它和年龄表还会互相打架。）
        """
        owner: dict[str, str] = {}
        for code, words in table:
            for word in words:
                assert word not in owner, f"{word!r} 同时归 {owner.get(word)} 与 {code}"
                owner[word] = code

    def test_the_language_codes_are_exactly_the_ones_the_field_stores(self):
        """语言这一样要**两边完全相同**（不是子集）。

        性别那边只敢要子集（``other`` 留给手填，认不出来）；语言没有这种"手填才有的值"
        ——17 个代码全是从文字里认出来的。所以这里要求严格相等：任意一边多一个，
        界面那第三个下拉里就会出现一个永远筛不出东西的选项，或者反过来——认得出来
        却没地方存。
        """
        assert {code for code, _ in LANGUAGES} == {code for code, _ in LANGUAGE_CHOICES}

    def test_characteristics_have_no_duplicates(self):
        """同一颗词写两遍 = 同一枚标签挂两次（计数也会跟着错）。"""
        assert len(CHARACTERISTICS) == len(set(CHARACTERISTICS))

    @pytest.mark.parametrize("word", CHARACTERISTICS)
    def test_every_characteristic_word_is_used_by_the_corpus(self, word):
        """这张表不许有"听起来很对但从没出现过"的词（纪律见表上方的注释）。

        上面三张表允许补进"同一概念的常见写法"（``先生``/``女士`` 一次都没出现也收着）；
        特点是**开集**的，谁都能往里加一个很像的词——所以每一颗词都得在语料里
        真的读到过。这条测试就是那道闸。
        """
        hits = [vid for vid, name, desc in CORPUS if word in name or word in desc]
        assert hits, f"{word!r} 在这份 303 条语料里一次都没出现——它没有语料支撑"

    @pytest.mark.parametrize("word", CHARACTERISTICS)
    def test_every_characteristic_word_can_be_recognized(self, word):
        assert word in characteristics_in(f"一位{word}的声音")

    @pytest.mark.parametrize(
        ("table", "choices", "what"),
        [
            (GENDERS, GENDER_CHOICES, "性别"),
            (AGES, AGE_CHOICES, "年龄感"),
            (LANGUAGES, LANGUAGE_CHOICES, "语言"),
        ],
    )
    def test_the_codes_are_the_same_ones_the_field_stores(self, table, choices, what):
        """认出来的代码要**直接写进音色那两个字段**，而那两个字段的候选是
        ``domain.models`` 里的词表——两边说着同一批代码。

        这条不靠 import 把两边绑在一起（``app.engines`` 是独立的一层，不认识 domain），
        而是靠这条断言：谁改了其中一边，另一边立刻红。

        只要求**子集**：``other``（其他）在性别词表里，而识别永远不会给出"其他"
        ——那是留给手填的，没有任何文字依据能说"这是其他性别"。
        """
        assert {code for code, _ in table} <= {code for code, _label in choices}, what
