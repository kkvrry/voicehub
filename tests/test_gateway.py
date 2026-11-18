"""中转站：规范化、按引擎声明的契约校验、路由。"""

import asyncio
from dataclasses import replace as dreplace
from pathlib import Path

import pytest

from app.engines.base import (
    CODE_UNREACHABLE,
    KIND_CLOUD,
    KIND_LOCAL,
    Capabilities,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
    VoiceList,
    VoiceMeta,
)
from app.engines.mapping import EngineMapping, ParamMapping
from app.usecase.gateway import (
    _call_engine,
    _settle_extra,
    _settle_language,
    normalize_voice,
    synthesize,
    validate_against_spec,
)


class FakeRefEngine(EnginePlugin):
    """必须带参考音频 + 参考文本的引擎（GPT-SoVITS 那一类）。"""

    key = "fake_ref"
    label = "假参考引擎"
    #: 合成那一次真正收到的请求——测试用它断言"传给插件的是什么"。
    seen = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id", "reference_audio", "reference_text"))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"RIFF-fake", format=req.format)


class FakeCloudEngine(EnginePlugin):
    """只认音色 id 的云平台引擎。"""

    key = "fake_cloud"
    label = "假云引擎"

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3", "wav"))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        return SynthesisResult(audio=b"ID3-fake", format=req.format)


class FakeListEngine(EnginePlugin):
    """能报清单的云平台引擎（清单**没入库**的情形用它钉）。"""

    key = "fake_list"
    label = "假清单引擎"
    #: 合成那一次真正收到的请求——测试用它断言"传给插件的是什么"。
    seen = None

    def capabilities(self) -> Capabilities:
        return Capabilities(voice_listing=True)

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    async def list_voices(self, cfg) -> VoiceList:
        return VoiceList(items=(VoiceMeta(id="Arrogant_Miss", name="嚣张小姐"),))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


class FakeDualEngine(EnginePlugin):
    """情绪与引导指令各有原生落点的引擎（MiMo 那一类）。

    两路都是"风格"，同时下给引擎会互相调和，所以中转站只发一路。
    """

    key = "fake_dual"
    label = "假双落点引擎"
    seen = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    def dynamic_mappings(self):
        return {
            "emotion": ("style.emotion", "整体风格。"),
            "instruction": ("style.instruction", "风格指令。"),
        }

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


class FakeFoldedEngine(EnginePlugin):
    """情绪只是翻译成引导指令的引擎（BreezeTTS 那一类）：两者落点相同。"""

    key = "fake_folded"
    label = "假同落点引擎"
    seen = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    def mapping(self):
        from app.engines.mapping import EngineMapping, ParamMapping

        return EngineMapping(
            params=(
                ParamMapping("emotion", "options.instruction"),
                ParamMapping("instruction", "options.instruction"),
            )
        )

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


class FakePlainEngine(EnginePlugin):
    """一条语气词都不认的引擎（本地克隆那一类）：标签要从正文里删掉。"""

    key = "fake_plain"
    label = "假克隆引擎"
    seen = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


class FakeMarkEngine(EnginePlugin):
    """登记了语气词的引擎：标签换成它的写法。"""

    key = "fake_mark"
    label = "假标签引擎"
    seen = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    def mark_map(self) -> dict[str, str]:
        return {"倒吸气": "(gasps)"}

    async def synthesize(self, cfg, req) -> SynthesisResult:
        type(self).seen = req
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


ENGINES = {
    engine.key: engine
    for engine in (
        FakeRefEngine(),
        FakeCloudEngine(),
        FakeListEngine(),
        FakeDualEngine(),
        FakeFoldedEngine(),
        FakePlainEngine(),
        FakeMarkEngine(),
    )
}


@pytest.fixture()
def scanned(service, demo_library: Path):
    service.save_folders([str(demo_library)])
    return service


def _hutao(service):
    return next(v for v in service.list_voices() if v.name == "胡桃")


class TestNormalize:
    def test_local_voice_brings_main_sample_and_text(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        req = normalize_voice(scanned, hutao.id)
        assert req.reference_audio.endswith("开心.wav")
        assert req.reference_text == "吃饱喝饱，一路走好！"

    def test_a_sample_emotion_is_never_filled_in(self, scanned):
        """样例上的情绪标签不补到请求里——空 = 没要求。

        参考音频本身已经带着原声的语气与节奏；再补一层情绪是替调用方提了他没提的
        要求，认情绪的那台引擎会照做，效果反而更差。要加情绪由调用方自己填。
        """
        hutao = _hutao(scanned)
        assert normalize_voice(scanned, hutao.id).emotion == ""
        scanned.set_main_sample(hutao.id, "开心.wav")  # 标签明确是「开心」也不补
        assert normalize_voice(scanned, hutao.id).emotion == ""

    def test_the_caller_s_emotion_is_sent_as_given(self, scanned):
        """调用方明说了就照他说的发。"""
        hutao = _hutao(scanned)
        assert normalize_voice(scanned, hutao.id, emotion="难过").emotion == "难过"

    def test_a_pinned_sample_wins_over_the_main_one(self, scanned):
        """角色上钉了样例：不管情绪是什么，都用这一条。"""
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")
        req = normalize_voice(scanned, hutao.id, emotion="难过", sample_file="开心.wav")
        assert req.reference_audio.endswith("开心.wav")

    def test_emotion_matching_goes_through_the_synonym_table(self, scanned):
        """行上写「高兴」、样例标的是「开心」——归一后是同一条。"""
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")
        req = normalize_voice(scanned, hutao.id, emotion="高兴", match_emotion=True)
        assert req.reference_audio.endswith("开心.wav")

    def test_a_sample_tagged_with_several_emotions_serves_each_of_them(self, scanned):
        """一条样例标了两种情绪（「严肃、轻蔑」）：要哪一种都该挑到它。

        一段声音本来就可能同时是这两种味道——标两种是为了让它覆盖两行，
        而不是把第二种当装饰。
        """
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")
        scanned.update_sample(hutao.id, "开心.wav", {"emotion": "开心、轻蔑"})
        for wanted in ("轻蔑", "开心"):
            req = normalize_voice(scanned, hutao.id, emotion=wanted, match_emotion=True)
            assert req.reference_audio.endswith("开心.wav"), wanted
        # 标了两种不等于样样都认：要「悲伤」照样回落主样例。
        req = normalize_voice(scanned, hutao.id, emotion="悲伤", match_emotion=True)
        assert req.reference_audio.endswith("默认.wav")

    def test_unmatched_emotion_falls_back_to_the_main_sample(self, scanned):
        """没有对应样例不是失败：按主样例合成。"""
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")
        req = normalize_voice(scanned, hutao.id, emotion="生气", match_emotion=True)
        assert req.reference_audio.endswith("默认.wav")

    def test_a_preset_voice_always_sends_its_id(self):
        """预置音色（有自己的平台）一律发 id，即使手上有样例也不拿去复刻。

        平台认识它、自己也存着它；用它的样例去复刻只会更差，与通道怎么配无关。
        """
        from types import SimpleNamespace

        from app.usecase.gateway import _library_request

        voice = SimpleNamespace(
            is_local=False,
            shown_name="冰糖",
            name="冰糖",
            source="mimo",
            folder="/voices/冰糖",
            main_sample="a.wav",
            samples=[SimpleNamespace(file="a.wav", text="你好", emotion="")],
            payload={},
            has_sample=True,
        )
        req = _library_request(voice, emotion="", engine_key="mimo")
        assert req.voice_id == "冰糖"
        assert req.reference_audio == ""

    def test_another_platform_s_sample_feeds_a_reference_engine(self):
        """样例是通用素材：别的平台留下的音频，走参考音频的引擎照样能拿来复刻。"""
        from types import SimpleNamespace

        from app.usecase.gateway import _library_request

        voice = SimpleNamespace(
            is_local=False,
            shown_name="林晚",
            name="MX_001",
            source="minimax",
            folder="/voices/林晚",
            main_sample="a.wav",
            samples=[SimpleNamespace(file="a.wav", text="你好", emotion="")],
            payload={},
            has_sample=True,
        )
        req = _library_request(voice, emotion="", engine_key="mimo")
        assert req.reference_audio.endswith("a.wav")
        assert req.reference_text == "你好"

    def test_platform_voice_without_samples_keeps_its_platform_id(self):
        """没有样例就只剩平台 id：不能凭空给别的引擎发一条空参考音频。"""
        from types import SimpleNamespace

        from app.usecase.gateway import _library_request

        voice = SimpleNamespace(
            is_local=False,
            shown_name="Chloe",
            name="mimo:Chloe",
            folder="",
            main_sample="",
            samples=[],
            payload={},
        )
        req = _library_request(voice, emotion="")
        assert req.voice_id == "mimo:Chloe"
        assert req.reference_audio == ""

    def test_another_platform_s_voice_without_a_sample_is_refused(self):
        """跨平台又没样例：那个音色 id 发过去只会换来"音色不存在"。"""
        from types import SimpleNamespace

        import pytest as _pytest

        from app.engines.base import EngineError
        from app.usecase.gateway import _library_request

        voice = SimpleNamespace(
            is_local=False,
            shown_name="林晚",
            name="MX_001",
            source="minimax",
            folder="",
            main_sample="",
            samples=[],
            payload={},
            sample_count=0,
        )
        with _pytest.raises(EngineError, match="没有可当参考音频的样例"):
            _library_request(voice, emotion="", engine_key="mimo")

    def test_a_clone_credential_is_sent_as_reference(self):
        """样本即音色的复刻：库里没有样例目录，凭据就是那段样本。"""
        from app.domain.credential import REFERENCE, credential_of
        from app.domain.models import Voice
        from app.usecase.gateway import _library_request

        voice = Voice(
            id="mimo:clone-1",
            source="mimo",
            name="clone-1",
            display="卞夫人",
            payload={"ref_audio": "/archive/mimo/卞夫人/s1.mp3"},
        )
        assert credential_of(voice, engine_key="mimo") == REFERENCE
        req = _library_request(voice, emotion="", engine_key="mimo")
        assert req.reference_audio == "/archive/mimo/卞夫人/s1.mp3"

    def test_platform_voice_sends_the_platform_id(self, service):
        from app.engines.base import VoiceMeta

        voice = service.add_platform_voice("minimax", VoiceMeta(id="MX_001", name="林晚"))
        req = normalize_voice(service, voice.id)
        assert req.voice_id == "MX_001"
        assert req.reference_audio == ""

    def test_unknown_voice_says_so_before_any_request(self, service):
        with pytest.raises(Exception, match="不存在"):
            normalize_voice(service, "nope")


class TestValidate:
    def test_missing_reference_text_is_caught_before_sending(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")  # 这条没有文本
        req = normalize_voice(scanned, hutao.id)
        problems = validate_against_spec(ENGINES["fake_ref"], req)
        assert any("参考文本" in p for p in problems)

    def test_full_request_passes(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        req = normalize_voice(scanned, hutao.id)
        assert validate_against_spec(ENGINES["fake_ref"], req) == ()

    def test_wrong_format_is_caught(self, scanned):
        hutao = _hutao(scanned)
        req = normalize_voice(scanned, hutao.id)
        req = dreplace(req, format="flac")
        problems = validate_against_spec(ENGINES["fake_cloud"], req)
        assert any("格式" in p for p in problems)


class TestExpressionSettling:
    """情绪与引导指令都认的引擎只发一路；其余引擎照旧两格都发。"""

    def test_instruction_wins_over_emotion(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        FakeDualEngine.seen = None

        asyncio.run(synthesize(
            scanned,
            engine_key="fake_dual",
            voice_id=hutao.id,
            text="你好",
            emotion="开心",
            extra={"instruction": "压低嗓子，缓慢地说"},
            engines=ENGINES,
        ))

        assert FakeDualEngine.seen.emotion == ""
        assert FakeDualEngine.seen.extra["instruction"] == "压低嗓子，缓慢地说"

    def test_emotion_survives_without_an_instruction(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        FakeDualEngine.seen = None

        asyncio.run(synthesize(
            scanned,
            engine_key="fake_dual",
            voice_id=hutao.id,
            text="你好",
            emotion="开心",
            engines=ENGINES,
        ))

        assert FakeDualEngine.seen.emotion == "开心"

    def test_a_blank_instruction_does_not_clear_the_emotion(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        FakeDualEngine.seen = None

        asyncio.run(synthesize(
            scanned,
            engine_key="fake_dual",
            voice_id=hutao.id,
            text="你好",
            emotion="开心",
            extra={"instruction": "   "},
            engines=ENGINES,
        ))

        assert FakeDualEngine.seen.emotion == "开心"

    def test_one_shared_target_keeps_the_emotion(self, scanned):
        """情绪只是翻译成引导指令（落点相同）的引擎不套这条规则。"""
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        FakeFoldedEngine.seen = None

        asyncio.run(synthesize(
            scanned,
            engine_key="fake_folded",
            voice_id=hutao.id,
            text="你好",
            emotion="开心",
            extra={"instruction": "压低嗓子"},
            engines=ENGINES,
        ))

        assert FakeFoldedEngine.seen.emotion == "开心"


class TestRoute:
    def test_synthesize_reaches_the_plugin(self, scanned):
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")  # 带文本的那条
        result = asyncio.run(synthesize(
            scanned,
            engine_key="fake_ref",
            voice_id=hutao.id,
            text="你好",
            engines=ENGINES,
        ))
        assert result.audio == b"RIFF-fake"

    def test_pronunciation_replacement_reaches_the_plugin(self, scanned):
        from app.usecase import pronunciation

        pronunciation.save([{"word": "重庆", "replacement": "崇庆"}])
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "开心.wav")
        FakeRefEngine.seen = None

        asyncio.run(synthesize(
            scanned,
            engine_key="fake_ref",
            voice_id=hutao.id,
            text="[笑]重庆",
            engines=ENGINES,
        ))

        assert FakeRefEngine.seen.text == "[笑]崇庆"

    def test_unknown_engine_lists_the_available_ones(self, scanned):
        hutao = _hutao(scanned)
        with pytest.raises(EngineError, match="fake_cloud"):
            asyncio.run(synthesize(
                scanned, engine_key="nope", voice_id=hutao.id, text="你好", engines=ENGINES
            ))

    def test_empty_text_is_refused(self, scanned):
        hutao = _hutao(scanned)
        with pytest.raises(EngineError, match="不能为空"):
            asyncio.run(synthesize(
                scanned, engine_key="fake_ref", voice_id=hutao.id, text="  ", engines=ENGINES
            ))

    def test_missing_reference_text_blocks_the_call(self, scanned):
        """主样例没文本时，参考类引擎的请求在**发出去之前**被拦下。"""
        hutao = _hutao(scanned)
        scanned.set_main_sample(hutao.id, "默认.wav")
        with pytest.raises(EngineError, match="参考文本"):
            asyncio.run(synthesize(
                scanned, engine_key="fake_ref", voice_id=hutao.id, text="你好", engines=ENGINES
            ))

    def test_real_registry_path_reports_the_engines_own_words(self, scanned):
        """真注册表链路（不注入 engines）：**没配密钥时说的也是插件自己的话**。

        从前这条拿内置的 ``manual`` 当靶子（它如实回答"还没接入"）。manual 删了，
        换成 MiniMax——它没密钥时会给出平台自己的理由，比中转站编一句更有用。
        """
        hutao = _hutao(scanned)
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(synthesize(scanned, engine_key="minimax", voice_id=hutao.id, text="你好"))


class TestMarks:
    """语气词标签：认的换成那一家的写法，不认的从正文里删掉。

    **闸口在网关这一处**：插件只声明 ``mark_map`` / ``mark_passthrough``，翻译由
    :meth:`EnginePlugin.prepare_text` 在发出前做一次。从前靠插件各自记得调，
    一台没调的（IndexTTS2）就把 ``[[倒吸气]]`` 原样发了出去，"倒吸气"三个字被念出来。
    """

    @staticmethod
    def _voice(service, engine_key: str):
        return service.add_platform_voice(engine_key, VoiceMeta(id="v1", name="甲"))

    def test_an_engine_that_knows_none_never_sees_the_tag(self, service):
        voice = self._voice(service, "fake_plain")
        FakePlainEngine.seen = None
        asyncio.run(synthesize(
            service,
            engine_key="fake_plain",
            voice_id=voice.id,
            text="[[倒吸气]]他往后退了一步。",
            engines=ENGINES,
        ))
        assert FakePlainEngine.seen.text == "他往后退了一步。"

    def test_a_tag_the_engine_knows_is_sent_in_its_own_writing(self, service):
        voice = self._voice(service, "fake_mark")
        FakeMarkEngine.seen = None
        asyncio.run(synthesize(
            service,
            engine_key="fake_mark",
            voice_id=voice.id,
            text="[[倒吸气]]他往后退了一步。",
            engines=ENGINES,
        ))
        assert FakeMarkEngine.seen.text == "(gasps)他往后退了一步。"

    def test_one_it_does_not_know_goes_even_when_it_knows_another(self, service):
        """认一半的引擎：认的那条换写法，不认的那条删——两条都不许当字念。"""
        voice = self._voice(service, "fake_mark")
        FakeMarkEngine.seen = None
        asyncio.run(synthesize(
            service,
            engine_key="fake_mark",
            voice_id=voice.id,
            text="[[倒吸气]]他咳了一声[[咳嗽]]。",
            engines=ENGINES,
        ))
        assert FakeMarkEngine.seen.text == "(gasps)他咳了一声。"

    def test_a_text_of_nothing_but_tags_is_refused(self, service):
        """整段只有一个标签、引擎又不认：删完没有可念的字，不能发一句空台词。"""
        voice = self._voice(service, "fake_plain")
        with pytest.raises(EngineError, match="语气词"):
            asyncio.run(synthesize(
                service,
                engine_key="fake_plain",
                voice_id=voice.id,
                text="[[倒吸气]]",
                engines=ENGINES,
            ))


class FakeShortEngine(EnginePlugin):
    """单次字数有硬上限的引擎（MiniMax / MiMo 那一类）。"""

    key = "fake_short"
    label = "假短段引擎"

    def capabilities(self) -> Capabilities:
        return Capabilities(max_chars=10)

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3",))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        return SynthesisResult(audio=b"ID3-fake", format="mp3")


class TestCharLimit:
    """字数上限是引擎声明的（``Capabilities.max_chars``），判据只有网关这一处。

    生产方另有计划期的一次预检（一次列出所有超长行）；单发与第三方接口从前不过这道
    闸，正文一路发到平台，换回一句上游的参数错。
    """

    @staticmethod
    def _voice(service):
        return service.add_platform_voice("fake_short", VoiceMeta(id="v1", name="甲"))

    def _run(self, service, text: str):
        return asyncio.run(synthesize(
            service,
            engine_key="fake_short",
            voice_id=self._voice(service).id,
            text=text,
            engines={**ENGINES, "fake_short": FakeShortEngine()},
        ))

    def test_a_text_over_the_declared_limit_is_refused_before_sending(self, service):
        with pytest.raises(EngineError, match="最多 10 字"):
            self._run(service, "一二三四五六七八九十一")

    def test_a_text_at_the_limit_goes_through(self, service):
        assert self._run(service, "一二三四五六七八九十").audio == b"ID3-fake"

    def test_marks_do_not_count_towards_the_limit(self, service):
        """算的是发出去的字：标签会被删掉，不该占上限。"""
        assert self._run(service, "[[倒吸气]]一二三四五").audio == b"ID3-fake"


class TestInstructionAliases:
    """``speaker`` / ``instruct`` 是引导指令的旧写法，网关归一到 ``instruction``。

    这份别名表从前在各插件里各写一份：认三个、认两个、一个都不认都有。漏认的那台
    就是"给了指令却静默收不到"——``speaker`` 尤其：网关认它（据此撤掉情绪），
    CosyVoice3 / VoxCPM2 那几台不认，结果一路都不生效。归一放在网关，插件只读正名。
    """

    @staticmethod
    def _voice(service):
        return service.add_platform_voice("fake_dual", VoiceMeta(id="v1", name="甲"))

    def _seen(self, service, **extra):
        FakeDualEngine.seen = None
        asyncio.run(synthesize(
            service,
            engine_key="fake_dual",
            voice_id=self._voice(service).id,
            text="你好",
            extra=extra,
            engines=ENGINES,
        ))
        return FakeDualEngine.seen

    def test_the_old_names_arrive_as_the_canonical_one(self, service):
        for name in ("speaker", "instruct"):
            seen = self._seen(service, **{name: "压低嗓子"})
            assert seen.extra["instruction"] == "压低嗓子"
            assert name not in seen.extra

    def test_the_canonical_name_wins_when_both_are_given(self, service):
        seen = self._seen(service, instruction="正名那句", speaker="别名那句")
        assert seen.extra["instruction"] == "正名那句"
        assert "speaker" not in seen.extra


class _BrokenEngine(FakeCloudEngine):
    """连不上：与批量合成里「引擎没起」同一种失败。"""

    key = "fake_broken"

    async def synthesize(self, cfg, req) -> SynthesisResult:
        raise EngineError("连不上 http://127.0.0.1:8080", code=CODE_UNREACHABLE)


class TestFailureLogging:
    """同一次失败只该有一条 warning：批量自带行号记，网关那条就让位。"""

    def _levels(self, scanned, log_failure: bool) -> list[str]:
        from app.domain.log import MemorySink
        from app.infra.loghub import CHANNEL

        hutao = _hutao(scanned)
        engine = _BrokenEngine()
        sink = MemorySink(limit=50)
        CHANNEL.add_sink(sink)
        try:
            with pytest.raises(EngineError, match="连不上"):
                asyncio.run(synthesize(
                    scanned,
                    engine_key=engine.key,
                    voice_id=hutao.id,
                    text="你好",
                    engines={engine.key: engine},
                    log_failure=log_failure,
                ))
        finally:
            CHANNEL.remove_sink(sink)
        return [r.level for r in sink.recent(50) if "合成未完成" in r.message]

    def test_the_default_is_a_warning(self, scanned):
        """单发（试听、单条接口）没有第二层，网关必须自己报。"""
        assert self._levels(scanned, True) == ["warning"]

    def test_batch_callers_can_take_it_over(self, scanned):
        """批量自己记一条带行号的，这时再报 warning 就是同一件事写两遍。"""
        assert self._levels(scanned, False) == ["debug"]


class TestBareNameFallback:
    """上游拿「问一句现在有什么」给的**裸音色名**原样传回——不该被库拒掉。

    有声书那条链路就是这个名字的形状：它从 ``/api/v1/engines/{key}/voices`` 拿到
    ``Arrogant_Miss``，合成时传回 ``Arrogant_Miss``。库里平台音色的 id 带
    ``引擎键:`` 前缀，直接查永远查不到——真踩过一次，报回来的是
    「没有这个音色（voicehub:Arrogant_Miss）」，而名字其实没错。
    """

    def test_bare_name_resolves_through_the_library(self, service):
        """清单拉过（库里有一条 ``引擎键:名字``）→ 按带前缀的名字再查一次命中。"""
        from app.domain.models import platform_voice_id

        service.add_platform_voice("fake_list", VoiceMeta(id="MX_001", name="林晚"))
        result = asyncio.run(synthesize(
            service,
            engine_key="fake_list",
            voice_id="MX_001",
            text="你好",
            engines=ENGINES,
        ))
        assert result.audio == b"ID3-fake"
        # 插件收到的是**登记时存的原 id**——翻译仍是插件那一层的事。
        assert ENGINES["fake_list"].seen.voice_id == "MX_001"
        assert platform_voice_id("fake_list", "MX_001")

    def test_name_not_in_library_is_matched_against_the_live_list(self, service):
        """清单没入库过 → 问一次引擎**现在**有什么，认得出就合成（不入库）。"""
        result = asyncio.run(synthesize(
            service,
            engine_key="fake_list",
            voice_id="Arrogant_Miss",
            text="你好",
            engines=ENGINES,
        ))
        assert result.audio == b"ID3-fake"
        assert ENGINES["fake_list"].seen.voice_id == "Arrogant_Miss"

    def test_a_truly_unknown_voice_still_says_so(self, service):
        """两步都认不出 → 原话照旧（不是把失败吞成另一次请求）。"""
        with pytest.raises(EngineError, match="不存在"):
            asyncio.run(synthesize(
                service,
                engine_key="fake_list",
                voice_id="nope",
                text="你好",
                engines=ENGINES,
            ))

    def test_local_voices_do_not_get_the_fallback(self, scanned):
        """本地音色的 id 是扫描时定的——上游只能原样传回，传不回就是真没有
        （拿裸名去引擎清单里"认"只会把错的名字当对的发出去）。"""
        hutao = _hutao(scanned)
        with pytest.raises(EngineError, match="不存在"):
            asyncio.run(synthesize(
                service=scanned,
                engine_key="fake_ref",
                voice_id=hutao.name,  # 裸名"胡桃"，不是库里的 id
                text="你好",
                engines=ENGINES,
            ))


class _BusyEngine(EnginePlugin):
    """第一次合成回 "忙"，第二次成功——用来钉重试口径。"""

    key = "busy"
    label = "忙引擎"

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self.calls = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(kind=self._kind)

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",))

    async def synthesize(self, cfg, req) -> SynthesisResult:
        self.calls += 1
        if self.calls == 1:
            raise EngineError("server busy", retryable=True)
        return SynthesisResult(audio=b"RIFF-busy", format="wav")


def _busy_request() -> SynthesisRequest:
    return SynthesisRequest(voice_id="x", text="你好")


def test_a_busy_local_engine_is_retried(monkeypatch):
    """本机 503 是"过一会儿就好"：退避一次再发，不把这一行直接判死。"""
    monkeypatch.setattr("app.usecase.gateway._RATE_BACKOFF", (0.0,))
    engine = _BusyEngine(KIND_LOCAL)
    result = asyncio.run(_call_engine(engine, object(), _busy_request()))
    assert result.audio == b"RIFF-busy"
    assert engine.calls == 2


def test_a_busy_cloud_engine_is_not_retried(monkeypatch):
    """云端 503 不自动重发：边界情况下重试可能变成两次计费。"""
    monkeypatch.setattr("app.usecase.gateway._RATE_BACKOFF", (0.0,))
    engine = _BusyEngine(KIND_CLOUD)
    with pytest.raises(EngineError, match="busy"):
        asyncio.run(_call_engine(engine, object(), _busy_request()))
    assert engine.calls == 1


class FakeParamEngine(FakeCloudEngine):
    """映射表 + 动态映射各给一格，用来钉 extra 白名单的口径。"""

    key = "fake_param"

    def mapping(self) -> EngineMapping:
        return EngineMapping(params=(ParamMapping("speed", "speed"),))

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        return {"seed": ("seed", "固定种子")}


def _param_request(**extra) -> SynthesisRequest:
    return SynthesisRequest(voice_id="x", text="你好", extra=extra)


def test_extra_outside_the_declaration_is_dropped_and_reported():
    """插件没声明过的键不往下发，并在 warnings 里说清「丢了哪些、能发哪些」。"""
    request = _param_request(speed=1.2, seed=7, pitch=-2, spead=1.0)
    settled, notes = _settle_extra(FakeParamEngine(), request)
    assert settled.extra == {"speed": 1.2, "seed": 7}
    assert len(notes) == 1
    code, message = notes[0]
    assert code == "parameter_ignored"
    assert "pitch" in message and "spead" in message
    assert "speed" in message and "seed" in message


def test_declared_extra_keys_are_left_alone():
    request = _param_request(speed=1.2, seed=7)
    settled, notes = _settle_extra(FakeParamEngine(), request)
    assert settled is request
    assert notes == ()


class FakeLanguageEngine(FakeCloudEngine):
    """语言面写死两门 + 一个方言点，用来钉「不支持就不发」的口径。"""

    key = "fake_lang"
    label = "假语言引擎"

    def capabilities(self) -> Capabilities:
        return Capabilities(languages=("zh", "yue"), dialects=("四川话",))


def test_an_unsupported_language_is_dropped_not_sent():
    request = _param_request(language="cmn-xa")
    settled, notes = _settle_language(FakeLanguageEngine(), request)
    assert "language" not in settled.extra
    code, message = notes[0]
    assert code == "language_ignored"
    assert "西安话" in message and "按不指定" in message


def test_a_supported_language_is_left_alone():
    # 写法与代码都认：粤语 → yue，四川话 → cmn-cd。
    for value in ("粤语", "yue", "四川话"):
        request = _param_request(language=value)
        settled, notes = _settle_language(FakeLanguageEngine(), request)
        assert settled is request, value
        assert notes == (), value


def test_a_language_outside_the_shared_table_is_left_to_the_plugin():
    """统一词表不收的代码不替引擎关门：600+ 语言的引擎可能认得它。"""
    request = _param_request(language="th")
    settled, notes = _settle_language(FakeLanguageEngine(), request)
    assert settled is request
    assert notes == ()


def test_an_unsupported_reference_language_is_dropped_too():
    request = _param_request(reference_language="cmn-xa")
    settled, notes = _settle_language(FakeLanguageEngine(), request)
    assert "reference_language" not in settled.extra
    code, message = notes[0]
    assert code == "language_ignored"
    assert "参考语言" in message
