"""DotTTS 插件：**照规格与实测核过的那些点**，每一条都钉一个断言。

和 ``test_breeze_tts.py`` 一样，这个文件里**没有一次真实网络请求**——假 httpx
客户端既让测试离线跑，又能断言"到底发了什么"（路径、body）。

这一家最容易被写错的**六**处，正是下面重点钉的：

1. **请求体分两层**：模型参数必须放进 ``options``——摆在顶层的会被引擎
   **静默忽略**（与 IndexTTS2 同一条实测结论）；``input`` / ``voice_ref`` /
   ``reference_text`` / ``seed`` 四样在顶层（audio.cpp 网页界面就这么摆的）；
2. **参考音频走本机绝对路径**（与 IndexTTS2 一致，不像 BreezeTTS 2 那样
   base64）——路径给了就必须读得到，读不到要当场报错，不能发一个空引用；
3. **不给参考音频也能走**（引擎自己挑音色）——所以 ``requires`` 不能写死
   ``reference_audio``，那是 IndexTTS2 的身位不是这一家的；
4. **种子默认 -1 = 随机，且随机必须插件抽**：引擎不收负数（500），
   **省略 seed 时引擎用定值 42**——"不传就是随机"在这家不成立；
5. **引导指令只对两个模板起作用**（``instruction_tts`` / ``edit``）：
   默认 ``tts`` 模板下发了也不消费——不消费就要在结果里说清，不静默；
6. **没有落点的项逐一报"未发送"**（指令强度 / 语速 / 音量 / 音高 / 静默）
   ——只丢不说是最坏的一种；情绪有落点（翻成 instruct），不在这张表里。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.marks import MARKS
from app.engines.base import KIND_CLOUD, KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.dots_tts.plugin import DotsTtsEngine, resolve_seed
from app.engines.builtin.dots_tts.settings import DotsTtsSettings
from app.engines.registry import list_engines


def _cfg(**over: object) -> DotsTtsSettings:
    data = {"base_url": "http://127.0.0.1:18090"}
    data.update(over)
    return DotsTtsSettings(**data)


#: 这整套用例默认用的参考音频路径——**会被下面那个 autouse 夹具换成真实文件**。
_REF = "D:/library/巫师3/叶奈法/默认.wav"


def _req(**over: object) -> SynthesisRequest:
    data = {
        "text": "他停下脚步，回头看了她一眼。",
        "voice_id": "叶奈法",
        "reference_audio": _REF,
        "reference_text": "某个角度说，的确。",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> DotsTtsEngine:
    return DotsTtsEngine()


def _wav(size: int = 4096) -> bytes:
    """一段够长、且**文件头是正经 WAV** 的假数据（48kHz 单声道一秒约 96KB，
    所以 4096 字节也过了 ``MIN_AUDIO_BYTES`` 那道坎）。"""
    head = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WAVE"
    return head + b"\x00" * (size - len(head))


@pytest.fixture(autouse=True)
def _real_reference_audio(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """把默认那个参考音频路径换成一个**真写出来的小 WAV**。

    **文件名故意带非 ASCII 字符**：这一家收本机路径（不像 BreezeTTS 2 走
    base64），中文目录要能原样发出去——插件只校验"读得到"，不校验编码。
    """
    path = tmp_path / "巫师3" / "叶奈法 默认.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav(512))
    monkeypatch.setitem(globals(), "_REF", str(path))
    return str(path)


# --------------------------------------------------------------------------- #
# 假 httpx：记录"发了什么"，离线断言
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(
        self,
        *,
        content: bytes = b"",
        status_code: int = 200,
        content_type: str = "audio/wav",
        json_body: object = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._json = json_body
        self.text = (
            json.dumps(json_body, ensure_ascii=False)
            if json_body is not None
            else content.decode("utf-8", "replace")
        )

    def json(self) -> object:
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeAsyncClient:
    """替身 httpx.AsyncClient。**只实现 post**——这一家只有合成一个端点。"""

    calls: ClassVar[list[dict[str, object]]] = []
    response: _FakeResponse = _FakeResponse()
    raise_error: Exception | None = None

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"path": path, "init": self.init_kwargs, **kwargs})
        if _FakeAsyncClient.raise_error is not None:
            raise _FakeAsyncClient.raise_error
        return _FakeAsyncClient.response


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    """把 httpx.AsyncClient 换成替身（client 里是函数内 import，所以打真模块就行）。"""
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    _FakeAsyncClient.raise_error = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


# --------------------------------------------------------------------------- #
# 发现：扫目录就接入，主代码零改动
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_registry_discovers_it_by_directory(self):
        """**不写名单**：放进 ``builtin/dots_tts/`` 就被发现（与其余引擎同一条规则）。"""
        engines = list_engines()
        assert "dots_tts" in engines
        assert isinstance(engines["dots_tts"], DotsTtsEngine)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        """**每个字段都得有界面上的说法**——漏一个就会在表单上显示成英文字段名。"""
        for name, field in DotsTtsSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_no_secret_fields(self):
        """本机服务没有凭据——**别为了对称摆一个空的密钥框**。"""
        assert DotsTtsEngine.secret_fields() == frozenset()

    def test_host_fills_in_a_missing_scheme(self):
        """省掉 ``http://`` 是常事，而 httpx 对它的报错是 ``unsupported protocol``。"""
        assert _cfg(base_url="127.0.0.1:18090").host == "http://127.0.0.1:18090"
        assert _cfg(base_url="http://127.0.0.1:18090/").host == "http://127.0.0.1:18090"
        assert _cfg(base_url="").host == "http://127.0.0.1:18090"

    def test_the_default_seed_is_random(self):
        """**默认就是随机**：这一家的定值在引擎侧（省略 = 42），要"每次都不一样"
        只能显式给 -1，所以插件默认替用户选随机。"""
        assert _cfg().seed == -1


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        caps = _engine().capabilities()
        assert caps.kind == KIND_LOCAL
        assert caps.kind != KIND_CLOUD

    def test_does_not_claim_a_voice_list_it_cannot_get(self):
        """**没有清单端点就别说有**：声明成 True 会让左栏多一个点了就报错的 ⟳。"""
        assert _engine().capabilities().voice_listing is False

    def test_does_not_claim_cloning(self):
        """zero-shot：参考音频本身就是音色，没有"注册一个音色 id"这一步。"""
        assert _engine().capabilities().voice_cloning is False

    def test_declares_the_reference_audio_capability(self):
        """参考音频 + 逐字稿 = 它的"音色"，这是看家能力。"""
        assert _engine().capabilities().reference_audio is True

    def test_does_not_claim_emotion_control(self):
        """没有情绪参数——语气由参考音频与 instruct 决定，别给出一颗没用的按钮。"""
        assert _engine().capabilities().emotion_control is False

    def test_does_not_cap_the_text_length(self):
        """长文本由它自己分块；本机推理也没有"超长 = 多花钱"这回事。"""
        assert _engine().capabilities().max_chars == 0

    def test_spec_does_not_require_a_reference_audio(self):
        """**与 IndexTTS2 不同**：这一家不给参考音频也能合成（引擎自己挑音色），
        写死 requires 会把那条路抹掉。"""
        assert _engine().synthesis_spec().requires == ()

    def test_spec_declares_wav_only(self):
        """audio.cpp 这一路恒定回 wav，别替它声明别的格式。"""
        assert _engine().synthesis_spec().formats == ("wav",)

    def test_spec_says_the_reference_audio_must_be_wav_too(self):
        """**参考音频这一侧也只认 WAV**——声明之后中转站会在发出去之前转一道，
        不声明的话用户手上那条 mp3 每次都得自己先转好。"""
        assert _engine().synthesis_spec().reference_formats == ("wav",)

    def test_spec_wants_a_transcript_but_not_unconditionally(self):
        """要逐字稿，**但只在给了参考音频时**（它提升克隆相似度，不是门槛）。"""
        spec = _engine().synthesis_spec()
        assert spec.needs_transcript is True
        assert "reference_text" not in spec.requires

    def test_extra_schema_carries_the_engine_params(self):
        """**只有进了 describe() 才到得了界面**——引擎面板那张表靠它。

        引擎特有参数（模板 / 音色缩放 / 采样器……）不在统一规范里，
        走的就是这个 ``extra_schema``；键名不能撞统一参数的名。"""
        schema = _engine().describe()["synthesis_spec"]["extra_schema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) >= {
            "template_name",
            "speaker_scale",
            "sampler_mode",
            "num_inference_steps",
            "text_chunk_mode",
            "use_xvector",
            "source_audio",
        }

    def test_extra_schema_keys_do_not_collide_with_unified_params(self):
        """撞名会出两条同键的路（统一那一格 + 高级那一格），前端分不清该发哪个。"""
        from app.domain.request_spec import SPEC_FIELDS

        unified = {f.key for f in SPEC_FIELDS}
        extra_keys = set(_engine().synthesis_spec().extra_schema["properties"])
        assert unified & extra_keys == set()


# --------------------------------------------------------------------------- #
# 装配：两层请求体
# --------------------------------------------------------------------------- #


class TestBodyAssembly:
    def test_the_basic_shape(self):
        """顶层四样 + options：正文、参考音频、参考文本、种子在外，
        模型参数在里头——两层不能摆错（摆错的那层会被静默忽略）。"""
        body = _engine().build_body(_cfg(), _req())
        assert body["model"] == "dots_tts"
        assert body["input"] == "他停下脚步，回头看了她一眼。"
        assert body["voice_ref"] == _REF
        assert body["reference_text"] == "某个角度说，的确。"
        assert isinstance(body["seed"], int)

    def test_text_is_required(self):
        """没正文 = 没请求——映射表的 required 在装配时就拦下。"""
        with pytest.raises(ValueError, match="text"):
            _engine().build_body(_cfg(), _req(text=""))

    def test_language_lands_under_options_only_when_given(self):
        """空着 = 不指定（不发，连空的 ``options`` 都不留）；给了才落进去。"""
        assert "options" not in _engine().build_body(_cfg(), _req())
        body = _engine().build_body(_cfg(), _req(extra={"language": "zh"}))
        assert body["options"]["language"] == "zh"

    def test_instruction_lands_under_options(self):
        """引导指令 → ``options.instruct``（顶层会被静默忽略）。"""
        body = _engine().build_body(_cfg(), _req(extra={"instruction": "语气温柔平静。"}))
        assert body["options"]["instruct"] == "语气温柔平静。"

    def test_engine_params_land_under_options(self):
        """高级参数随 extra 进来，逐个落进 options（换算照映射表）。"""
        body = _engine().build_body(
            _cfg(),
            _req(
                extra={
                    "template_name": "edit",
                    "speaker_scale": 1.2,
                    "sampler_mode": "rk4",
                    "num_inference_steps": 8,
                    "max_tokens": 400,
                    "text_chunk_size": 160,
                    "text_chunk_mode": "tag_aware",
                    "vocoder_merge_steps": 2,
                    "use_xvector": "on",
                    "source_audio": "D:/src/source.wav",
                }
            ),
        )
        options = body["options"]
        assert options["template_name"] == "edit"
        assert options["speaker_scale"] == 1.2
        assert options["sampler_mode"] == "rk4"
        assert options["num_inference_steps"] == 8
        assert options["max_tokens"] == 400
        assert options["text_chunk_size"] == 160
        assert options["text_chunk_mode"] == "tag_aware"
        assert options["vocoder_merge_steps"] == 2
        assert options["use_xvector"] == "on"
        assert options["source_audio"] == "D:/src/source.wav"

    def test_no_reference_audio_is_a_legal_request(self):
        """不给参考音频：请求体里就没有 voice_ref 那一项（引擎自己挑音色）。"""
        body = _engine().build_body(_cfg(), _req(reference_audio="", reference_text=""))
        assert "voice_ref" not in body

    def test_a_missing_reference_file_fails_loudly(self):
        """给了路径却读不到 = 当场报错——不能发一个空引用让引擎去猜。"""
        with pytest.raises(EngineError, match="读不到参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio="D:/不存在的音频.wav"))

    def test_the_unmappable_params_are_reported_not_dropped(self):
        """只丢不说是最坏的一种：每一项没落点的都要在对用户说的话里出现。"""
        _, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(extra={"speed": 1.5, "volume": 1.2}),
        )
        assert any("未发送" in note and "语速" in note for note in notes)
        assert any("未发送" in note and "音量" in note for note in notes)

    def test_an_instruction_switches_the_default_template(self):
        """没显式选模板时，有指令就切到 instruction_tts——否则默认 tts 不消费它。"""
        body = _engine().build_body(
            _cfg(), _req(extra={"instruction": "语气温柔平静。"})
        )
        assert body["options"]["template_name"] == "instruction_tts"

    def test_emotion_becomes_an_instruction_and_switches_template(self):
        """没有情绪参数，但情绪翻成一句 instruct 后就是一个有效落点。"""
        body, notes = _engine().build_body_with_notes(_cfg(), _req(emotion="开心"))
        assert body["options"]["template_name"] == "instruction_tts"
        assert body["options"]["instruct"] == "用开心、轻快的语气朗读。"
        assert not any("情绪" in note for note in notes)

    def test_emotion_instruction_follows_the_text_language(self):
        body = _engine().build_body(
            _cfg(), _req(text="She stopped and looked back.", emotion="开心")
        )
        assert body["options"]["instruct"] == "Speak in a happy, cheerful tone."

    def test_an_explicit_instruction_wins_over_the_emotion(self):
        body = _engine().build_body(
            _cfg(), _req(emotion="开心", extra={"instruction": "语气温柔平静。"})
        )
        assert body["options"]["instruct"] == "语气温柔平静。"

    def test_an_explicit_template_wins_over_the_auto_switch(self):
        """用户明确选了 tts：尊重他的选择，只说清这条指令不会被消费。"""
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(emotion="开心", extra={"template_name": "tts"})
        )
        assert body["options"]["template_name"] == "tts"
        assert any("当前模板不消费它" in note for note in notes)

    def test_instruction_with_a_template_that_ignores_it_says_so(self):
        """显式选了一个不消费 instruct 的模板——**要在结果里说清**，不静默。"""
        _, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(extra={"instruction": "语气温柔平静。", "template_name": "tts"}),
        )
        assert any("当前模板不消费它" in note for note in notes)

    def test_instruction_with_edit_template_stays_quiet(self):
        """edit 模板消费 instruct——那就没什么要说的。"""
        _, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(extra={"instruction": "把语速放慢", "template_name": "edit"}),
        )
        assert not any("当前模板不消费它" in note for note in notes)

    def test_those_two_reach_the_engine_payload(self):
        """**只有进了 describe() 才到得了界面**——引擎面板那张表靠它。"""
        described = _engine().describe()
        assert described["mapping_constants"] == {}
        assert any(
            row["source"] == "seed" and row["target"] == "seed"
            for row in described["mapping"]
        )
        assert any(
            row["source"] == "emotion" and row["target"] == "options.instruct"
            for row in described["mapping"]
        )
        assert described["synthesis_spec"]["extra_schema"]["properties"]["template_name"]["default"] == "tts"


# --------------------------------------------------------------------------- #
# 种子：默认 -1 = 随机，且随机必须插件抽（引擎不收负数、省略用定值 42）
# --------------------------------------------------------------------------- #


class TestSeed:
    """规格里钉死的两条事实，正是这一组要守住的：

    1. ``min=0``、``default=42``——**省略 seed 时引擎用定值 42**，
       "不传就是随机"在这家不成立；
    2. 负数被引擎拒——所以"随机"只能由插件做：抽一个、发出去、
       **把抽到的数说回来**。
    """

    def test_the_default_seed_is_drawn(self):
        """配置默认 -1 = 随机：发出去的那个数必须能过引擎那一关（无符号整数）。"""
        body = _engine().build_body(_cfg(), _req())
        assert isinstance(body["seed"], int)
        assert 0 <= body["seed"] <= 2**32 - 1

    def test_zero_is_a_fixed_seed(self):
        """0 是一个合法种子（固定、可复现），不是什么"没设置"。"""
        assert _engine().build_body(_cfg(seed=0), _req())["seed"] == 0

    def test_an_upstream_seed_wins(self):
        assert _engine().build_body(_cfg(), _req(extra={"seed": 1234}))["seed"] == 1234

    def test_a_configured_seed_is_used_when_upstream_is_silent(self):
        """上游没提 = 用配置里那一格（两个口子都要管）。"""
        assert _engine().build_body(_cfg(seed=77), _req())["seed"] == 77

    def test_a_blank_upstream_seed_falls_back_to_the_config(self):
        """给了空串 = 没要求——回落配置那一格，不当成具体某个数发出去。"""
        body = _engine().build_body(_cfg(seed=77), _req(extra={"seed": ""}))
        assert body["seed"] == 77

    def test_minus_one_is_drawn_into_a_real_seed(self):
        body = _engine().build_body(_cfg(), _req(extra={"seed": -1}))
        assert isinstance(body["seed"], int)
        assert 0 <= body["seed"] <= 2**32 - 1

    def test_a_non_integer_seed_is_refused(self):
        """认不出的种子当场报错——不能悄悄换成一个默认值发出去。"""
        with pytest.raises(EngineError, match="不是整数"):
            resolve_seed("随机")

    def test_the_drawn_seed_is_reported(self):
        """**抽到几号必须说出来**——不然"随机"过一次满意的结果就再也回不去了。"""
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": -1}))
        assert len(notes) == 1
        assert str(body["seed"]) in notes[0]

    def test_two_draws_are_not_pinned_to_the_same_number(self):
        """抽出来的不是常数——否则"随机"只是个说法。"""
        drawn = {_engine().build_body(_cfg(), _req(extra={"seed": -1}))["seed"] for _ in range(5)}
        assert len(drawn) > 1

    def test_a_fixed_seed_is_reported_too(self):
        """固定种子**也要报**：这一条是可复现的，用户得看得到用的是哪一号。"""
        _, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": 1234}))
        assert notes == ("seed=1234",)


# --------------------------------------------------------------------------- #
# 客户端：发、认错误、认"回的不是音频"
# --------------------------------------------------------------------------- #


class TestClient:
    @pytest.fixture(autouse=True)
    def _reset(self, fake_http: _FakeAsyncClient) -> _FakeAsyncClient:
        return fake_http

    def test_the_request_goes_to_the_speech_endpoint(self):
        """端点就一个：``POST /v1/audio/speech``（audio.cpp 的 OpenAI 兼容口）。"""
        _FakeAsyncClient.response = _FakeResponse(content=_wav())
        asyncio.run(_engine().synthesize(_cfg(), _req()))
        call = _FakeAsyncClient.calls[-1]
        assert call["path"] == "/v1/audio/speech"
        assert call["json"]["model"] == "dots_tts"

    def test_a_happy_response_comes_back_as_wav(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(content=_wav())
            return await _engine().synthesize(_cfg(), _req())

        result = asyncio.run(_run())
        assert result.audio[:4] == b"RIFF"
        assert result.format == "wav"

    def test_a_4xx_envelope_is_translated(self):
        """OpenAI 风格的错误信封要认出来，原文透传给用户。"""

        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=400,
                content_type="application/json",
                json_body={"error": {"message": "seed must be an unsigned integer"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="seed must be an unsigned integer"):
            asyncio.run(_run())

    def test_a_non_audio_response_is_refused(self):
        """回的不是音频（比如把 JSON 错误塞进了 200）——不能当音频交出去。"""

        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                content=b'{"ok":true}',
                content_type="application/json",
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="响应不是音频"):
            asyncio.run(_run())

    def test_a_tiny_audio_is_refused(self):
        """低于最小字节数 = 只有文件头（文本为空或参考音频没读到的典型表现）。"""

        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(content=b"RIFF____WAVE")
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="低于合理的最小长度"):
            asyncio.run(_run())

    def test_a_connect_error_names_the_service(self):
        """连不上时只说"哪个引擎、哪个地址连不上"，不替用户推断原因。"""

        async def _run() -> object:
            _FakeAsyncClient.raise_error = httpx.ConnectError("refused")
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match=r"DotTTS（.*）无法连接"):
            asyncio.run(_run())

    def test_the_logged_body_hides_the_long_text(self):
        """日志里那份请求体：长文本截断、参考音频只留文件名——别把全文写进日志。"""
        from app.engines.audio_http import loggable_body

        logged = loggable_body({"input": "啊" * 400, "voice_ref": "D:/某目录/某参考.wav"})
        assert len(logged["input"]) < 200
        assert logged["voice_ref"] == "某参考.wav"


# --------------------------------------------------------------------------- #
# 语气词：这一家一条都不认（没有 mark_map），不参与「通用」运算
# --------------------------------------------------------------------------- #


class TestMarks:
    def test_claims_no_marks(self):
        """规格里没有语气词通路——空表 = 一条都不认，正文里的标签由别家处理。"""
        assert _engine().mark_map() == {}

    def test_the_word_list_itself_is_unaffected(self):
        """词表是跨引擎的共同财产，不因少一家而缺词。"""
        assert MARKS
