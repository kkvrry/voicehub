"""MiniMax 插件：**照官方文档核过的那些点**，每一条都钉一个断言。

这个文件里**没有一次真实网络请求**——假 httpx 客户端既让测试离线跑，又能断言
"到底发了什么"（路径、方法、body、请求头）。方法写错（把 ``get_voice`` 发成 GET）
的症状是"拿到一个与清空长得一样的空结果"，不把方法钉住是抓不到的。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.models import AGES, GENDERS
from app.engines.base import KIND_CLOUD, EngineError, SynthesisRequest
from app.engines.builtin.minimax import client as mm_client
from app.engines.builtin.minimax.mapping import (
    EMOTION_MAP,
    EMOTION_SYNONYMS,
    EMOTION_UNMAPPED,
    MAPPING,
    emotion_choices,
)
from app.engines.builtin.minimax.plugin import MinimaxEngine, normalize_voice_id
from app.engines.builtin.minimax.settings import MinimaxSettings, base_url_for
from app.engines.builtin.minimax.voices import parse_voice_list
from app.engines.mapping import apply_mapping
from app.engines.traits import characteristics_in


def _cfg(**over: object) -> MinimaxSettings:
    data = {"api_key": "sk-test"}
    data.update(over)
    return MinimaxSettings(**data)


# --------------------------------------------------------------------------- #
# 假 httpx：记录"发了什么"，离线断言
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> object:
        return self._payload


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：把每次 ``post`` 的路径与参数记在 ``calls`` 上。

    ``calls`` 是**类属性**（各处共用一个记录本），夹具每次都会清空它。
    """

    calls: ClassVar[list[dict[str, object]]] = []
    payload: object = {}
    status_code: int = 200

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append(
            {"path": path, "init": self.init_kwargs, **kwargs}
        )
        return _FakeResponse(_FakeAsyncClient.payload, _FakeAsyncClient.status_code)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    """把 httpx.AsyncClient 换成替身（client 里是函数内 import，所以打真模块就行）。"""
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.payload = {}
    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _hex_audio(size: int = 400) -> str:
    return (b"\x00\x01\x02\x03" * (size // 4)).hex()


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        """**每个字段都得有界面上的说法**——漏一个就会在表单上显示成英文字段名。

        这条是给将来加字段的人守的：加配置项时忘了 ``ui_hints``，这里就红。
        """
        for name, field in MinimaxSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_api_key_is_the_only_secret(self):
        assert MinimaxEngine.secret_fields() == frozenset({"api_key"})

    def test_two_regions_have_distinct_hosts(self):
        """**两站密钥不通用**——地址搞错的表现是"密钥无效"，与密钥真的错了一模一样。"""
        assert base_url_for("cn") == "https://api.minimaxi.com"
        assert base_url_for("intl") == "https://api.minimax.io"
        assert base_url_for("") == "https://api.minimaxi.com"  # 认不出当国内站
        assert base_url_for("Mars") == "https://api.minimaxi.com"

    def test_custom_base_url_wins(self):
        assert _cfg(base_url="https://proxy.local/").host == "https://proxy.local"
        assert _cfg(region="intl").host == "https://api.minimax.io"

    def test_readiness_names_what_is_missing(self):
        engine = MinimaxEngine()
        assert "API Key" in engine.readiness(_cfg(api_key=""))
        assert engine.readiness(_cfg()) == ""

    def test_model_choices_are_declared_for_the_form(self):
        """模型是平台**只认枚举**的一格：写错一个字母就是一次失败的计费请求。

        所以候选表本身也要经得起看：值不重复、每个都有中文说法、当前代在里面。
        """
        choices = MinimaxSettings.model_fields["model"].json_schema_extra["choices"]
        values = [value for value, _ in choices]
        assert "speech-2.8-hd" in values
        assert len(values) == len(set(values))
        assert all(label and label != value for value, label in choices)


# --------------------------------------------------------------------------- #
# 音色 id
# --------------------------------------------------------------------------- #


class TestVoiceId:
    def test_valid_id_is_used_as_is(self):
        assert normalize_voice_id("MyVoice_01", "随便") == "MyVoice_01"

    @pytest.mark.parametrize("bad", ["short", "1abc12345", "abc-", "abc_", "有中文的名字啊", "-abc1234"])
    def test_invalid_id_is_reported_not_silently_rewritten(self, bad: str):
        """给了不合法就**报出来**——悄悄改掉的话，用户在平台后台按自己起的名字找不到它。"""
        with pytest.raises(EngineError, match="8–256"):
            normalize_voice_id(bad, "名字")

    def test_generated_id_matches_the_platform_rule(self):
        generated = normalize_voice_id("", "胡桃")
        assert generated.startswith("vh_")
        assert len(generated) >= 8
        assert generated[0].isalpha()
        assert generated[-1] not in "-_"

    def test_chinese_names_do_not_collide(self):
        """纯中文名经 ASCII 过滤后一个字符都不剩——只用名字的话它们会撞成同一个 id。"""
        ids = {normalize_voice_id("", name) for name in ("胡桃", "钟离", "阿米娅", "林晚")}
        assert len(ids) == 4

    def test_same_name_twice_still_differs(self):
        """同一个名字克隆第二次是常事，而平台不允许重复的 voice_id。"""
        assert normalize_voice_id("", "胡桃") != normalize_voice_id("", "胡桃")

    def test_name_starting_with_a_digit_gets_a_letter(self):
        assert normalize_voice_id("", "123号")[0].isalpha()

    def test_empty_name_still_yields_a_valid_id(self):
        assert normalize_voice_id("", "").startswith("vh_")


# --------------------------------------------------------------------------- #
# 音色清单里的语言标签 / 性别 / 年龄感
# --------------------------------------------------------------------------- #


class TestVoiceLanguageField:
    """语言是**从 id 与说明里认出来**的（这一家没有 language 字段），落成**那一格**。

    识别的规则本身在 ``tests/test_traits.py`` 里钉；这里钉的是**接线**：认出来的东西
    有没有真的进 ``VoiceMeta.language``。

    它**从前是一条标签**（一条叫"粤语"的字符串，混在 ``tags`` 里）。那样做的问题
    不是不好看，是**筛不了**：标签只是一串自由的词，没有词表约束，界面上也就不可能
    为它摆出一个候选下拉——只能一个个点开看。现在它与性别、年龄感并排，
    成为筛选栏第三个下拉。
    """

    @staticmethod
    def _meta(**over: object):
        item = {"voice_id": "v1", "voice_name": "v1"}
        item.update(over)
        return parse_voice_list({"system_voice": [item]})[0]

    def test_the_language_in_the_id_becomes_the_field(self):
        assert self._meta(voice_id="Cantonese_CuteGirl").language == "yue"

    def test_the_language_only_written_in_the_description_is_also_caught(self):
        """真实清单里有 2 条是这样的：id 没写语言，说明里的中文句子写了。"""
        assert (
            self._meta(
                voice_id="Arrogant_Miss",
                description=["一位嚣张自信的青年女性声音，标准普通话，展现出优越感。"],
            ).language
            == "zh"
        )

    def test_mandarin_and_cantonese_stay_two_values(self):
        """MiniMax 自己分开列（26 条 Mandarin / 6 条 Cantonese），合掉就再也分不回来了。"""
        assert self._meta(voice_id="Chinese (Mandarin)_Gentleman").language == "zh"
        assert self._meta(voice_id="Cantonese_ProfessionalHost（F)").language == "yue"

    def test_a_voice_that_never_names_a_language_stays_empty(self):
        """**不猜。** 40 条真实音色两处都没提语言，它们这一格就该是空串。"""
        assert self._meta(voice_id="Arnold").language == ""
        assert self._meta(voice_id="male-qn-badao-jingpin").language == ""

    def test_the_language_never_leaks_into_the_tags(self):
        """**每个值只有一个宿主。** 语言有了自己的格子，标签就不该再出现它——

        混进去的症状不是"多了一眼看着不舒服"，而是**同一个信息有两个地方说**：
        用户在下拉里改了这一格（那是正本），卡片上的标签却还是识别时的那个值，
        两边永远对不上。
        """
        meta = self._meta(voice_id="Korean_SweetGirl", tag_list=["新闻"])
        assert meta.language == "ko"
        assert meta.tags == ("新闻",)


class TestVoiceCharacteristicTags:
    """音色特点（声音本身的性格）落成**标签**——它是这一格目前唯一的内容来源。

    与语言/性别/年龄不同：那一门三样各自只有**一个值**（多选一，所以各占一格），
    特点是**一串词**（可以同时"温暖、善良、体贴"），所以只能落在天生装一串词的
    ``tags`` 里。
    """

    @staticmethod
    def _tags(**over: object) -> tuple[str, ...]:
        item = {"voice_id": "v1", "voice_name": "v1"}
        item.update(over)
        return parse_voice_list({"system_voice": [item]})[0].tags

    def test_the_adjectives_in_the_description_become_tags(self):
        assert self._tags(
            voice_id="Chinese (Mandarin)_Kind-hearted_Antie",
            description=["一位温和善良的中年大婶声音，标准普通话，温暖而体贴。"],
        ) == ("善良", "温暖", "体贴")

    def test_the_name_counts_too(self):
        """平台把"傲娇"写在名字里（``傲娇御姐``），说明里没有——名字也得读。"""
        assert self._tags(voice_id="x", voice_name="傲娇御姐") == ("傲娇",)

    def test_several_traits_keep_the_order_they_appear(self):
        assert characteristics_in("一位温柔甜美的声音") == ("温柔", "甜美")
        assert characteristics_in("一位甜美温柔的声音") == ("甜美", "温柔")

    def test_a_voice_whose_description_says_nothing_gets_no_tags(self):
        """**不猜**：认不出的连一颗标签都不挂（``Santa Claus`` 那条就是空的）。"""
        assert self._tags(voice_id="Santa_Claus") == ()

    def test_a_platform_tag_is_preserved_behind_the_traits(self):
        """平台自己的标签排在后面，顺序与内容一个都不动。"""
        assert self._tags(
            voice_id="x",
            voice_name="温柔女声",
            description=["一位温柔的青年女性声音。"],
            tag_list=["新闻", "旁白"],
        ) == ("温柔", "新闻", "旁白")

    def test_gender_age_and_language_never_leak_into_the_tags(self):
        """**每个值只有一个宿主**：那三样各有各的格子，标签里不该出现它们。"""
        assert self._tags(
            voice_id="female-shaonv",
            voice_name="少女音色",
            description=["一位温暖清脆的少女声音。"],
        ) == ("温暖",)


class TestVoiceGenderAndAge:
    """性别与年龄感落**它们自己的格子**（不是标签）——于是筛选栏那两个下拉也能用了。

    这两个值是我们**猜**的（这一家的清单里没有它们，见 :mod:`app.engines.traits`），
    所以规矩是：只在那一格空着的时候写（``usecase.library``），认不出就空着。
    """

    @staticmethod
    def _meta(**over: object):
        item = {"voice_id": "v1", "voice_name": "v1"}
        item.update(over)
        return parse_voice_list({"system_voice": [item]})[0]

    def test_the_id_prefix_gives_the_gender_the_name_gives_the_age(self):
        meta = self._meta(voice_id="female-shaonv", voice_name="少女音色")
        assert (meta.gender, meta.age) == ("female", "teen")

    def test_a_real_international_voice(self):
        meta = self._meta(
            voice_id="Korean_CheerfulBoyfriend",
            voice_name="Cheerful Boyfriend",
            description=["一位犀利开朗的中年男性男友声音，韩语。"],
        )
        assert (meta.gender, meta.age) == ("male", "middle")

    def test_the_senior_trap_is_not_walked_into(self):
        """``Gentle_Senior`` 是"温柔**学姐**"——年龄只看中文，读 id 会读成"老年"。

        这条**必须**钉在解析这一层：识别函数自己有测试，可这里是它接进去的地方，
        接线接错了（比如把 id 也传进年龄那一路），函数再对也没用。
        """
        meta = self._meta(
            voice_id="Chinese (Mandarin)_Gentle_Senior",
            voice_name="温柔学姐",
            description=["一位温暖温柔的青年学姐声音，标准普通话。"],
        )
        assert meta.age == "young"
        assert meta.gender == "female"

    def test_what_we_cannot_tell_stays_empty(self):
        """``Arnold`` 只有个名字，``卡通猪小琪`` 是一只猪——两格都空着。"""
        assert self._meta(voice_id="Arnold", voice_name="Arnold").gender == ""
        assert self._meta(voice_id="Arnold", voice_name="Arnold").age == ""
        assert self._meta(voice_id="cartoon_pig", voice_name="卡通猪小琪").gender == ""

    def test_the_values_land_in_the_fields_the_ui_already_has(self):
        """识别结果直接是那两个字段里存的值（同 ``domain.models`` 的候选），
        不是"我们的另一套说法"——否则界面上会显示成（词表外）。"""
        meta = self._meta(voice_id="male-qn-dadao", voice_name="霸道青年音色")
        assert meta.gender in GENDERS
        assert meta.age in AGES


# --------------------------------------------------------------------------- #
# 请求体装配
# --------------------------------------------------------------------------- #


class TestBuildBody:
    def _body(self, cfg: MinimaxSettings | None = None, **extra: object) -> dict:
        return self._text_body("你好", cfg, **extra)

    def _text_body(self, text: str, cfg: MinimaxSettings | None = None, **extra: object) -> dict:
        """正文先过网关那一道（语气词标签在发出前换成平台写法），再装配。"""
        engine = MinimaxEngine()
        req = SynthesisRequest(
            text=engine.prepare_text(text),
            voice_id="MX_1",
            extra={k: v for k, v in extra.items() if v is not None},
        )
        return engine.build_body(cfg or _cfg(), req)

    def test_mapping_lands_on_the_official_paths(self):
        body = self._body()
        assert body["text"] == "你好"
        assert body["voice_setting"]["voice_id"] == "MX_1"

    def test_stream_is_explicitly_false(self):
        """写进请求体而不是省略：省略了的话，"我们没接流式"这件事就没地方记。"""
        assert self._body()["stream"] is False

    def test_config_fills_what_upstream_did_not_ask_for(self):
        body = self._body(_cfg(model="speech-2.6-hd", sample_rate=24000, channel=2))
        assert body["model"] == "speech-2.6-hd"
        assert body["audio_setting"]["sample_rate"] == 24000
        assert body["audio_setting"]["channel"] == 2
        assert body["audio_setting"]["format"] == "mp3"  # cfg.audio_format

    def test_upstream_format_beats_the_configured_default(self):
        body = self._body(format="wav")
        assert body["audio_setting"]["format"] == "wav"

    def test_absent_emotion_sends_no_emotion_key(self):
        """MiniMax 自己按文本匹配语气；发个空串反而可能被判成无效枚举。"""
        assert "emotion" not in self._body()["voice_setting"]

    def test_emotion_is_passed_through_when_given(self):
        assert self._body(emotion="happy")["voice_setting"]["emotion"] == "happy"

    def test_numeric_params_are_converted(self):
        body = self._body(speed="1.2", volume="3")
        assert body["voice_setting"]["speed"] == 1.2
        assert body["voice_setting"]["vol"] == 3

    @pytest.mark.parametrize(
        ("ratio", "semitones"),
        [(2.0, 12), (0.5, -12), (1.0, 0), (1.5, 7), (1.3, 5), (0.9, -2)],
    )
    def test_pitch_is_a_ratio_and_becomes_semitones(self, ratio: float, semitones: int):
        """规范那一格是**倍率**，平台那一格是**半音**：``12 × log₂(r)``。

        两端（0.5 / 2.0）正好落在平台的 −12 / +12 上——统一格式选倍率不是随口的。
        ``1.3`` 那条管的是另一半：换出来是 ``4.54``，只能发 ``5``（平台只有整半音）。
        """
        assert self._body(pitch=ratio)["voice_setting"]["pitch"] == semitones

    def test_pitch_is_sent_as_a_whole_number(self):
        """平台那一格是 ``int64``，带小数点会被 ``2013 Mismatch type int64`` 拒掉（真踩过）。

        断言要落在**类型**上：``7 == 7.0`` 在 Python 里成立，比"值相等"看不出"发了个
        带小数的数"；而 ``bool`` 又是 ``int`` 的子类，所以按 ``type(...) is int`` 比。
        最后那条比的是**平台真正看到的东西**——一段 JSON 文本，里面不能有小数点。
        """
        value = self._body(pitch=1.3)["voice_setting"]["pitch"]
        assert type(value) is int
        assert json.dumps(value) == "5"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_pitch_ratio_that_has_no_semitone_is_refused(self, bad: float):
        """`log₂` 对非正数没有定义——**报错**而不是送一个 NaN 给平台。"""
        with pytest.raises(EngineError, match="大于 0"):
            self._body(pitch=bad)

    @pytest.mark.parametrize(
        ("chinese", "enum_value"),
        [("开心", "happy"), ("难过", "sad"), ("平静", "calm"), ("中性", "fluent")],
    )
    def test_chinese_emotion_is_translated_to_the_platform_enum(
        self, chinese: str, enum_value: str
    ):
        """统一格式里给的是中文词，平台只认它那八个枚举——这一层翻。"""
        assert self._body(emotion=chinese)["voice_setting"]["emotion"] == enum_value

    def test_a_platform_enum_passes_straight_through(self):
        assert self._body(emotion="angry")["voice_setting"]["emotion"] == "angry"

    @pytest.mark.parametrize(
        ("alias", "enum_value"),
        [("兴奋", "happy"), ("高兴", "happy"), ("快乐", "happy"), ("悲伤", "sad"), ("愤怒", "angry")],
    )
    def test_a_synonym_lands_on_the_same_enum(self, alias: str, enum_value: str):
        """同一个英文有好几种中文说法，而有声书的白名单是**用户自由填的**。

        他写「兴奋」也是那个意思——认得出就归一到规范词，别把他的意思弄丢。
        """
        assert self._body(emotion=alias)["voice_setting"]["emotion"] == enum_value

    def test_an_emotion_we_cannot_translate_is_dropped_not_refused(self):
        """认不出 → **剔掉这一格**，其余照发，而不是让整段失败。

        情绪是"少了也能念"的参数：一次整章合成里有一行写了平台不认的词，
        就为它让整段失败、把前面几行的钱一起搭进去，代价太大。
        """
        body = self._body(emotion="恍惚", speed="1.2")
        assert "emotion" not in body["voice_setting"]
        assert body["voice_setting"]["speed"] == 1.2

    def test_a_dropped_emotion_is_handed_back_so_the_caller_can_say_so(self):
        """剔掉了却不说 = 哑巴。

        用户听到的是"没情绪"，而他以为自己要的是"动情"——那种错查不出来。
        """
        dropped: list[str] = []
        req = SynthesisRequest(text="你好", voice_id="MX_1", emotion="动情")
        apply_mapping(MAPPING, req.as_values(), dropped)
        assert dropped == ["emotion"]

    def test_emotion_choices_covers_the_spec_words_and_the_synonyms(self):
        """这台引擎认得的词 = 规范词 + 同义词；上游照它逐行打标。"""
        choices = emotion_choices()
        assert choices[: len(EMOTION_MAP)] == tuple(EMOTION_MAP)
        for alias, canonical in EMOTION_SYNONYMS.items():
            assert alias in choices
            # 平台只有八档：落不上的那几个故意不给映射（剔掉比塞错档诚实）。
            assert canonical in EMOTION_MAP or canonical in EMOTION_UNMAPPED
        # 界面拿的是 ``/api/v1/engines`` 那份（``describe()`` 已转成 list，JSON 友好）。
        assert MinimaxEngine().describe()["value_choices"] == {"emotion": list(choices)}

    def test_language_becomes_language_boost(self):
        assert self._body(language="yue")["language_boost"] == "Chinese,Yue"

    def test_a_language_with_no_boost_value_is_refused_not_dropped(self):
        """**给了又默默丢掉**比报错更难查——要"不增强"这个结果就把那一格空着。"""
        with pytest.raises(EngineError, match="语言增强"):
            self._body(language="xx")

    def test_output_shape_lands_in_audio_setting(self):
        body = self._body(sample_rate=24000, bitrate=128000, channels=2)
        assert body["audio_setting"]["sample_rate"] == 24000
        assert body["audio_setting"]["bitrate"] == 128000
        assert body["audio_setting"]["channel"] == 2

    def test_the_integer_audio_fields_are_sent_as_integers(self):
        """``audio_setting`` 那三格平台都是**整型**——与 ``pitch`` 同一类问题。

        上游把采样率写成 ``24000.0``（JSON 里的另一种写法）、声道写成字符串 ``"2"``，
        发出去都得是整数写法；带小数点的那些会被按类型校验的网关拒掉。
        """
        audio = self._body(sample_rate=24000.0, bitrate="128000", channels="2")["audio_setting"]
        assert type(audio["sample_rate"]) is int
        assert type(audio["bitrate"]) is int
        assert type(audio["channel"]) is int

    @pytest.mark.parametrize(("mark", "native"), [("笑", "(laughs)"), ("咳嗽", "(coughs)")])
    def test_a_mark_in_the_text_becomes_the_platform_tag(self, mark: str, native: str):
        """正文里的统一指令（``[[笑]]``）→ 平台那串英文标签。**翻译由网关在发出前做**。"""
        assert self._text_body(f"你好[[{mark}]]啊")["text"] == f"你好{native}啊"

    def test_a_pause_mark_with_no_seconds_gets_a_natural_one(self):
        assert self._text_body("你好[[停顿]]啊")["text"] == "你好<#0.50#>啊"

    def test_marks_this_engine_does_not_know_are_deleted_not_read_aloud(self):
        """留着的下场是被当成字念出来（"左括号左括号口哨右括号"），那比没有更糟。"""
        assert self._text_body("你好[[口哨]]啊")["text"] == "你好啊"

    def test_lead_and_lag_silence_have_no_landing(self):
        """这一家的 ``<#x#>`` **必须夹在两段可发音文本之间**——正文的开头与结尾
        放不了。所以"这一句之前/之后静默"在这一家做不到（不是漏接）；
        句**中间**的停顿请写 ``[[停顿:0.4]]``。"""
        assert self._body(lead_silence=0.4, lag_silence=1.5)["text"] == "你好"

    def test_interjections_are_refused_on_models_that_do_not_know_them(self):
        """官方：语气词**只有 2.8 那两个模型认**。别的模型发出去会被当成字念出来。"""
        with pytest.raises(EngineError, match="speech-2.8"):
            self._text_body("你好[[笑]]啊", _cfg(model="speech-2.6-hd"))

    def test_a_pause_mark_is_fine_on_any_model(self):
        """停顿不是语气词——所有模型都认 ``<#x#>``。"""
        body = self._text_body("你好[[停顿:0.4]]啊", _cfg(model="speech-2.6-hd"))
        assert body["text"] == "你好<#0.40#>啊"

    def test_sound_effect_goes_to_voice_modify(self):
        body = self._body(_cfg(sound_effect="robotic"))
        assert body["voice_modify"]["sound_effects"] == "robotic"

    def test_empty_text_normalization_is_not_sent(self):
        assert "text_normalization_mode" not in self._body(_cfg(text_normalization_mode=""))
        assert (
            self._body(_cfg(text_normalization_mode="quality"))["text_normalization_mode"]
            == "quality"
        )

    @pytest.mark.parametrize(("value", "expect_in_message"), [(9, "0.5–2.0"), (0.1, "0.5–2.0")])
    def test_speed_out_of_range_is_blocked_before_sending(self, value: float, expect_in_message: str):
        """区间是官方写死的，所以在**发之前**拦是确定的，不是猜测。"""
        with pytest.raises(EngineError, match="没有发出去"):
            self._body(speed=value)

    def test_pitch_out_of_range_is_blocked(self):
        with pytest.raises(EngineError, match="pitch"):
            self._body(pitch=99)

    def test_non_numeric_speed_is_blocked_before_sending(self):
        """``speed="很快"`` 在**装配这一步**就被拦下（而不是静默丢掉）。"""
        with pytest.raises(ValueError, match="speed"):
            self._body(speed="很快")

    def test_range_guard_also_refuses_a_non_number_body(self):
        """区间接管的是"装配好了的请求体"——所以它自己也得挡非数字。

        这条路正常走不到（上面那条先拦了），留着是防别的路：将来谁往请求体里
        塞一个常量式的 ``speed``，这一层仍然会把"不是数字"说出来。
        """
        with pytest.raises(EngineError, match="只接受数字"):
            MinimaxEngine._guard_ranges({"voice_setting": {"speed": "很快"}})

    def test_mapping_of_the_whole_table_agrees_with_apply_mapping(self):
        """装配的第一步就是这张表——两张口径对不上时以这里为准，防止悄悄漂移。"""
        mapping = MinimaxEngine().mapping()
        body = apply_mapping(mapping, {"text": "你好", "voice_id": "MX_1"})
        assert body["stream"] is False
        assert mapping.required_sources() == ("text", "voice_id")
        assert mapping.cautions  # 重点说明必须写在声明里，不是 TODO 注释里
        assert MinimaxEngine().unsupported()  # 「不认的参数」逐条有原因


# --------------------------------------------------------------------------- #
# 响应信封
# --------------------------------------------------------------------------- #


class TestEnvelope:
    def test_business_error_inside_http_200_is_caught(self):
        """这一家把业务错误放在 200 里——不先拆信封就只能挖到"没返回音频"。"""
        payload = {"base_resp": {"status_code": 1004, "status_msg": "login fail: no key"}}
        with pytest.raises(EngineError) as exc:
            mm_client.check_envelope(payload, "这次合成", label="MiniMax")
        assert "1004" in str(exc.value)
        assert "login fail" in str(exc.value)  # 平台的原话，后面不接我们自己的解释

    def test_success_passes_through(self):
        payload = {"data": {"audio": "00ff"}, "base_resp": {"status_code": 0}}
        assert mm_client.check_envelope(payload, "这次合成", label="MiniMax") is payload

    def test_absent_base_resp_is_fine(self):
        payload = {"data": {"audio": "00ff"}}
        assert mm_client.check_envelope(payload, "这次合成", label="MiniMax") is payload

    def test_non_dict_response_says_so(self):
        with pytest.raises(EngineError, match="不是 JSON 对象"):
            mm_client.check_envelope(["nope"], "拉音色清单", label="MiniMax")

    def test_missing_reason_still_says_something(self):
        payload = {"base_resp": {"status_code": 2001}}
        with pytest.raises(EngineError, match="未提供原因"):
            mm_client.check_envelope(payload, "这次合成", label="MiniMax")


class TestDecodeAudio:
    def test_hex_is_decoded(self):
        assert mm_client.decode_audio({"data": {"audio": "00010203"}}, label="MiniMax") == b"\x00\x01\x02\x03"

    def test_empty_audio_mentions_the_likely_cause(self):
        payload = {"data": {"audio": ""}, "extra_info": {"audio_length": 0}}
        with pytest.raises(EngineError, match="未返回音频") as exc:
            mm_client.decode_audio(payload, label="MiniMax")
        assert "文本为空" in str(exc.value)

    def test_non_hex_says_the_platform_changed_encoding(self):
        """不是 hex 时得说清是哪一种，否则用户会去查我们的解码代码。"""
        with pytest.raises(EngineError, match="不是合法的十六进制"):
            mm_client.decode_audio({"data": {"audio": "zzzz"}}, label="MiniMax")


# --------------------------------------------------------------------------- #
# 真的出去的那几个请求
# --------------------------------------------------------------------------- #


class TestRequests:
    """真的会"发出去"的那几个请求——用替身 httpx 断言**发了什么**。

    一律从 ``fake_http.calls`` 的第 0 条读（夹具每次都清过），
    免得读到上一个用例留下的调用。
    """

    def test_voice_listing_uses_post_with_a_json_body(self, fake_http):
        """**官方是 POST + body**，不是 GET 查询串（写成 GET 拿到的是空结果）。"""
        fake_http.payload = {
            "system_voice": [{"voice_id": "v1", "voice_name": "音色一"}],
            "base_resp": {"status_code": 0},
        }
        result = asyncio.run(mm_client.fetch_voices(_cfg(voice_type="system"), label="MiniMax"))

        call = fake_http.calls[0]
        assert call["path"] == mm_client.VOICES_PATH == "/v1/get_voice"
        assert call["json"] == {"voice_type": "system"}
        assert [v.id for v in result.items] == ["v1"]
        assert result.why == ""

    def test_headers_carry_the_bearer_key_and_the_region_host(self, fake_http):
        fake_http.payload = {"system_voice": [], "base_resp": {"status_code": 0}}
        asyncio.run(mm_client.fetch_voices(_cfg(api_key="sk-abc", region="intl"), label="MiniMax"))

        init = fake_http.calls[0]["init"]
        assert init["headers"]["Authorization"] == "Bearer sk-abc"
        assert init["base_url"] == "https://api.minimax.io"

    def test_platform_refusal_becomes_the_reason_not_an_exception(self, fake_http):
        """拉清单**不抛异常**——空表与"这个平台没有音色"长得一样，那句话必须有人说。"""
        fake_http.payload = {"base_resp": {"status_code": 1004, "status_msg": "login fail"}}
        result = asyncio.run(mm_client.fetch_voices(_cfg(), label="MiniMax"))

        assert result.items == ()
        assert "1004" in result.why and "login fail" in result.why

    def test_empty_listing_explains_that_cloned_voices_need_one_synthesis(self, fake_http):
        fake_http.payload = {"system_voice": [], "base_resp": {"status_code": 0}}
        result = asyncio.run(mm_client.fetch_voices(_cfg(), label="MiniMax"))

        assert result.items == ()
        assert "克隆" in result.why

    def test_synthesis_posts_to_t2a_v2_and_reports_the_character_cost(self, fake_http):
        fake_http.payload = {
            "data": {"audio": _hex_audio(400)},
            "extra_info": {"audio_length": 1200, "usage_characters": 42},
            "base_resp": {"status_code": 0},
        }
        result = asyncio.run(
            mm_client.synthesize(
                _cfg(), {"text": "你好", "audio_setting": {"format": "wav"}}, label="MiniMax"
            )
        )

        assert fake_http.calls[0]["path"] == "/v1/t2a_v2"
        assert result.format == "wav"
        assert "42 字符" in result.cost_note
        assert len(result.audio) == 400

    def test_suspiciously_short_audio_is_refused(self, fake_http):
        """短得像"几十个字节"的返回值不是语音——多半是文本为空或编码变了。"""
        fake_http.payload = {"data": {"audio": (b"\x00" * 8).hex()}}
        with pytest.raises(EngineError, match="合理的最小长度"):
            asyncio.run(mm_client.synthesize(_cfg(), {"text": "你好"}, label="MiniMax"))

    def test_clone_uploads_source_then_labels_the_prompt_file(self, fake_http, monkeypatch):
        """两步走：先传音源（``voice_clone``），再传提示音频（``prompt_audio``）。"""
        uploaded: list[str] = []

        async def fake_upload(_cfg, _path, purpose, **_kw):
            uploaded.append(purpose)
            return f"file-{len(uploaded)}"

        monkeypatch.setattr(mm_client, "upload_audio", fake_upload)
        fake_http.payload = {"demo_audio": "https://x/demo.mp3"}

        meta = asyncio.run(
            MinimaxEngine().clone_voice(
                _cfg(),
                _clone_request(
                    audio_path="source.wav", prompt_audio="hint.wav", prompt_text="提示"
                ),
            )
        )

        assert uploaded == ["voice_clone", "prompt_audio"]
        call = fake_http.calls[0]
        assert call["path"] == "/v1/voice_clone"
        assert call["json"]["clone_prompt"] == {"prompt_audio": "file-2", "prompt_text": "提示"}
        assert call["json"]["voice_id"] == meta.id
        assert "7 天" in meta.description

    def test_prompt_audio_without_text_is_refused(self):
        """官方要求这两个成对出现——只给一个，平台会拒，不如在这里说清。"""
        with pytest.raises(EngineError, match="成对"):
            asyncio.run(
                MinimaxEngine().clone_voice(
                    _cfg(), _clone_request(audio_path="source.wav", prompt_audio="hint.wav")
                )
            )
        with pytest.raises(EngineError, match="成对"):
            asyncio.run(
                MinimaxEngine().clone_voice(
                    _cfg(), _clone_request(audio_path="source.wav", prompt_text="只有文本")
                )
            )

    def test_clone_without_audio_is_refused(self):
        with pytest.raises(EngineError, match="本地音源音频"):
            asyncio.run(MinimaxEngine().clone_voice(_cfg(), _clone_request()))

    @pytest.mark.parametrize(
        ("over", "expect"),
        [
            ({"transcript": "字" * 201}, "200 字"),
            ({"preview_text": "字" * 1001}, "1000 字"),
        ],
    )
    def test_overlong_text_is_refused_without_uploading(self, over, expect, fake_http):
        """**本地判得了的，别等传完再判**——那一次上传是白花的（见 plugin.clone_voice）。"""
        with pytest.raises(EngineError, match=expect):
            asyncio.run(
                MinimaxEngine().clone_voice(
                    _cfg(), _clone_request(audio_path="source.wav", **over)
                )
            )
        assert fake_http.calls == []  # 一个字节都没出去


def _clone_request(**over: object):
    from app.engines.base import CloneRequest

    data: dict[str, object] = {
        "name": "胡桃",
        "audio_path": "",
        "transcript": "",
        "prompt_audio": "",
        "prompt_text": "",
        "preview_text": "",
        "model": "",
    }
    data.update(over)
    return CloneRequest(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 没配密钥时：**一次网络请求都不该发出去**
# --------------------------------------------------------------------------- #


class TestOfflineGuards:
    @pytest.fixture()
    def no_network(self, monkeypatch: pytest.MonkeyPatch):
        """把出口堵死：谁没听守卫的话真去发请求，这里就会抛出来。"""

        def boom(*args: object, **kwargs: object):
            raise AssertionError("没配密钥却发出了网络请求")

        monkeypatch.setattr(httpx, "AsyncClient", boom)

    def test_voice_listing_says_what_is_missing(self, no_network):
        result = asyncio.run(mm_client.fetch_voices(_cfg(api_key=""), label="MiniMax"))
        assert result.items == ()
        assert "API Key" in result.why

    def test_synthesis_says_what_is_missing(self, no_network):
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(mm_client.synthesize(_cfg(api_key=""), {"text": "你好"}, label="MiniMax"))

    def test_upload_and_clone_say_what_is_missing(self, no_network):
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(
                mm_client.upload_audio(
                    _cfg(api_key=""), "x.wav", "voice_clone", label="MiniMax"
                )
            )
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(mm_client.clone_voice(_cfg(api_key=""), {"text": "你好"}, label="MiniMax"))

    def test_missing_source_file_is_reported(self, no_network, tmp_path):
        with pytest.raises(EngineError, match="音源文件不存在"):
            asyncio.run(
                mm_client.upload_audio(
                    _cfg(), str(tmp_path / "没有这个.wav"), "voice_clone", label="MiniMax"
                )
            )


# --------------------------------------------------------------------------- #
# 自描述（界面全靠它）
# --------------------------------------------------------------------------- #


class TestDescribe:
    def test_declares_itself_a_cloud_service(self):
        caps = MinimaxEngine().capabilities()
        assert caps.kind == KIND_CLOUD
        assert caps.is_cloud is True
        assert caps.voice_listing is True
        assert caps.voice_cloning is True
        assert caps.max_chars == 10_000

    def test_synthesis_spec_asks_only_for_a_voice_id(self):
        spec = MinimaxEngine().synthesis_spec()
        assert spec.requires == ("voice_id",)
        assert spec.formats[0] == "mp3"

    def test_describe_carries_everything_the_ui_needs(self):
        data = MinimaxEngine().describe()
        assert data["key"] == "minimax"
        assert data["kind_label"] == "云端"
        assert data["icon_url"] == "/api/v1/engines/minimax/icon"
        assert data["secret_fields"] == ["api_key"]
        assert data["mapping_constants"]["stream"] is False
        assert data["settings_schema"]["properties"]["api_key"]["widget"] == "password"
        # 能力行必须有标签；说明可以有（"跨语言合成"这类只给标签、不写解释）。
        assert all(note["label"] for note in data["capability_notes"])
        assert any(note["note"] for note in data["capability_notes"])
        assert any(note["key"] == "voice_cloning" for note in data["capability_notes"])

    def test_icon_comes_from_the_plugin_directory(self):
        svg = MinimaxEngine().icon_svg()
        assert svg.lstrip().startswith("<svg")
        assert "<path" in svg or "<rect" in svg  # 不是那个中性占位图标
