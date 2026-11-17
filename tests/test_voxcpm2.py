"""VoxCPM2 插件：**照规格核过的那些点**，每条钉一个断言。

与 ``test_dots_tts.py`` 同一套写法：不碰网络，假 httpx 客户端既让测试离线跑，
又能断言"到底发了什么"。

这一家最容易写错的地方：

1. **没有独立的指令字段**：音色/语气描述写在 ``input`` 最前面的括号里，
   ``instruction`` / ``emotion`` / ``voice_design`` 三样都在装配时拼进去；
2. **``ultimate_clone`` 只在参考音频与逐字稿同时在场、且没有风格/方言时发**；
   有风格时退成可控克隆，逐字稿不发送——官方 Hi-Fi 模式会忽略控制指令；
3. **种子省略时引擎用定值 1234**，"每次不一样"必须由插件抽；
4. **只有 offline 能用 retry_badcase**，插件不流式，所以不发这个字段；
5. 没有的能力（内置音色 / 音色 id / 情绪枚举 / 语速）一条都不声明，
   给了就在结果说明里报"未发送"。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.request_spec import SPEC_FIELDS
from app.engines.base import KIND_CLOUD, KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.voxcpm2.plugin import (
    VoxCpm2Engine,
    emotion_style,
    resolve_seed,
    unit_to_guidance,
)
from app.engines.builtin.voxcpm2.settings import VoxCpm2Settings
from app.engines.registry import list_engines


def _cfg(**over: object) -> VoxCpm2Settings:
    data: dict[str, object] = {"base_url": "http://127.0.0.1:8080"}
    data.update(over)
    return VoxCpm2Settings(**data)


def _req(**over: object) -> SynthesisRequest:
    data: dict[str, object] = {"text": "他停下脚步，回头看了她一眼。", "voice_id": "叶奈法"}
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> VoxCpm2Engine:
    return VoxCpm2Engine()


def _wav(size: int = 4096) -> bytes:
    head = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WAVE"
    return head + b"\x00" * (size - len(head))


#: 参考音频路径——由下面那个 autouse 夹具换成真实文件。
_REF = "D:/library/某书/某人/默认.wav"


@pytest.fixture(autouse=True)
def _real_reference_audio(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """真写一个小 WAV，**文件名故意带中文**：这一家收本机路径，只校验读得到。"""
    path = tmp_path / "某书" / "某人 默认.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav(512))
    monkeypatch.setitem(globals(), "_REF", str(path))
    return str(path)


# --------------------------------------------------------------------------- #
# 假 httpx
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
    calls: ClassVar[list[dict[str, object]]] = []
    response: _FakeResponse = _FakeResponse()
    raise_error: Exception | None = None

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.is_closed = False

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
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    _FakeAsyncClient.raise_error = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_registry_discovers_it_by_directory(self):
        engines = list_engines()
        assert "voxcpm2" in engines
        assert isinstance(engines["voxcpm2"], VoxCpm2Engine)
        assert engines["voxcpm2"].label == "VoxCPM2"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        for name, field in VoxCpm2Settings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_no_secret_fields(self):
        assert VoxCpm2Engine.secret_fields() == frozenset()

    def test_host_fills_in_a_missing_scheme(self):
        assert _cfg(base_url="127.0.0.1:8080").host == "http://127.0.0.1:8080"
        assert _cfg(base_url="").host == "http://127.0.0.1:8080"

    def test_the_defaults_follow_the_engine_spec(self):
        cfg = _cfg()
        assert cfg.model == "voxcpm2"
        assert cfg.guidance_scale == 2.0  # 官方默认
        assert cfg.num_inference_steps == 10
        # 引擎省略种子时用定值 1234，所以插件默认必须是"随机"才有差异。
        assert cfg.seed == -1
        assert cfg.text_chunk_size == 0  # 0 = 用引擎默认 2048


# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        caps = _engine().capabilities()
        assert caps.kind == KIND_LOCAL
        assert caps.kind != KIND_CLOUD

    def test_does_not_claim_a_voice_list_it_cannot_get(self):
        """没有内置音色，也没有清单端点。"""
        assert _engine().capabilities().voice_listing is False

    def test_does_not_claim_cloning_by_id(self):
        """零样本：参考音频本身就是音色，没有"注册一个音色 id"这一步。"""
        assert _engine().capabilities().voice_cloning is False

    def test_claims_voice_design_and_reference_audio(self):
        caps = _engine().capabilities()
        assert caps.voice_design is True
        assert caps.reference_audio is True

    def test_does_not_claim_emotion_control(self):
        """没有情绪枚举——语气写成描述，那是"翻译"，不是原生支持。"""
        assert _engine().capabilities().emotion_control is False

    def test_runs_one_request_at_a_time(self):
        assert _engine().capabilities().max_concurrency == 1

    def test_spec_requires_nothing(self):
        """三条路都通（纯正文 / 参考音频 / 描述），静态 requires 表达不了"至少给一条"。"""
        assert _engine().synthesis_spec().requires == ()

    def test_spec_declares_wav_reference_and_no_transcript_gate(self):
        spec = _engine().synthesis_spec()
        assert spec.formats == ("wav",)
        assert spec.reference_formats == ("wav",)
        # 逐字稿是"最高保真"的加分项，不是门槛。
        assert spec.needs_transcript is False
        assert "voice_design" in _engine().accepted_extra_keys()

    def test_spec_declares_the_audiovae_duration_ceiling(self):
        """上限 15 秒 = AudioVAE 容量 240000 / 16000；交给核心截取，不自己裁。

        下限取 0：这一家服务端没有最短判据，短样例照收（与 GPT-SoVITS 的 3 秒不同）。
        """
        assert _engine().synthesis_spec().reference_duration == (0.0, 15.0)

    def test_pages_include_design(self):
        from app.usecase.engines import page_sections

        keys = [section["key"] for section in page_sections(_engine())]
        assert keys == ["settings", "voices", "design", "about"]


# --------------------------------------------------------------------------- #
# 括号前缀：这一家没有独立指令字段
# --------------------------------------------------------------------------- #


class TestStylePrefix:
    def test_plain_text_is_sent_as_is(self):
        body = _engine().build_body(_cfg(), _req())
        assert body["input"] == "他停下脚步，回头看了她一眼。"
        assert "instruct" not in body

    def test_a_design_description_goes_into_the_prefix(self):
        body = _engine().build_body(
            _cfg(), _req(extra={"voice_design": "温柔年轻女性，声音清亮"})
        )
        assert body["input"] == "(温柔年轻女性，声音清亮)他停下脚步，回头看了她一眼。"

    def test_an_instruction_goes_into_the_prefix(self):
        body = _engine().build_body(_cfg(), _req(extra={"instruction": "语速偏慢，克制"}))
        assert body["input"].startswith("(语速偏慢，克制)")

    def test_the_design_wins_over_the_instruction(self):
        body = _engine().build_body(
            _cfg(),
            _req(extra={"voice_design": "沙哑老年男性", "instruction": "轻快"}),
        )
        assert body["input"].startswith("(沙哑老年男性)")

    def test_an_emotion_becomes_a_style_sentence(self):
        body = _engine().build_body(_cfg(), _req(emotion="难过"))
        assert body["input"].startswith("(低沉克制的语气，语速稍慢)")

    def test_the_emotion_sentence_follows_the_text_language(self):
        """官方要求描述语言与正文一致。"""
        zh = emotion_style("难过", "他停下脚步。")
        en = emotion_style("难过", "He stopped and turned around.")
        assert zh.startswith("低沉")
        assert en.startswith("a sad")

    def test_an_unknown_emotion_still_reaches_the_model(self):
        """认不出的词走模板句，而不是当作"没有情绪"丢掉。"""
        assert emotion_style("恍惚", "他停下脚步。") == "「恍惚」的语气"
        assert emotion_style("wistful", "He stopped.") == 'a "wistful" tone'

    def test_design_drops_the_reference_audio_and_says_so(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(reference_audio=_REF, reference_text="某段话", extra={"voice_design": "少年"}),
        )
        assert "voice_ref" not in body
        assert "reference_text" not in body
        assert any("参考音频未发送" in note for note in notes)


# --------------------------------------------------------------------------- #
# 克隆
# --------------------------------------------------------------------------- #


class TestClone:
    def test_reference_audio_lands_at_the_top_level(self):
        body = _engine().build_body(_cfg(), _req(reference_audio=_REF, reference_text="某段话"))
        assert body["voice_ref"] == _REF
        assert body["reference_text"] == "某段话"

    def test_audio_plus_transcript_triggers_ultimate_clone(self):
        body = _engine().build_body(_cfg(), _req(reference_audio=_REF, reference_text="某段话"))
        assert body["options"]["ultimate_clone"] is True

    def test_audio_alone_does_not_trigger_it(self):
        """只给音频也能克隆——那时不发 ultimate_clone。"""
        body = _engine().build_body(_cfg(), _req(reference_audio=_REF))
        assert "ultimate_clone" not in body["options"]

    def test_a_missing_reference_file_fails_loudly(self):
        with pytest.raises(EngineError, match="读不到参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio="D:/不存在的音频.wav"))

    def test_a_style_switches_off_ultimate_clone(self):
        """Hi-Fi 模式会忽略控制指令；有风格时退成可控克隆，逐字稿不发。"""
        body, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(
                reference_audio=_REF,
                reference_text="某段话",
                extra={"instruction": "语速偏慢，克制"},
            ),
        )
        assert "ultimate_clone" not in body["options"]
        assert "reference_text" not in body
        assert body["input"] == "(语速偏慢，克制)他停下脚步，回头看了她一眼。"
        assert "可控克隆" in " ".join(notes)

    def test_an_emotion_also_switches_off_ultimate_clone(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(reference_audio=_REF, reference_text="某段话", emotion="难过"),
        )
        assert "ultimate_clone" not in body["options"]
        assert "reference_text" not in body
        assert body["input"].startswith("(低沉克制的语气，语速稍慢)")
        assert "可控克隆" in " ".join(notes)

    def test_reference_only_clone_keeps_the_prefix_on_the_input(self):
        """没有逐字稿就不是终极克隆，前缀照旧放正文头上（这条路不会念出来）。"""
        body = _engine().build_body(
            _cfg(),
            _req(reference_audio=_REF, extra={"instruction": "轻快"}),
        )
        assert "ultimate_clone" not in body["options"]
        assert body["input"].startswith("(轻快)")
        assert "reference_text" not in body


# --------------------------------------------------------------------------- #
# 生成参数
# --------------------------------------------------------------------------- #


class TestGenerationParams:
    def test_config_defaults_are_sent(self):
        """没发就等于让引擎按自己的默认跑，而"发了默认值"与"没发"不是一回事。"""
        options = _engine().build_body(_cfg(), _req())["options"]
        assert options["guidance_scale"] == 2.0
        assert options["num_inference_steps"] == 10

    def test_the_unified_guidance_scale_is_converted(self):
        """规范是 0–1 刻度，这一家是 1–5：0.5 对应它的默认 2。"""
        body = _engine().build_body(_cfg(), _req(extra={"guidance_scale": 0.5}))
        assert body["options"]["guidance_scale"] == 2.0
        body = _engine().build_body(_cfg(), _req(extra={"guidance_scale": 1.0}))
        assert body["options"]["guidance_scale"] == 5.0

    def test_the_conversion_hits_the_documented_points(self):
        assert unit_to_guidance(0) == 1.0
        assert unit_to_guidance(0.5) == 2.0
        assert unit_to_guidance(1) == 5.0
        assert unit_to_guidance(1.7) == 5.0  # 钳住，不外推
        assert unit_to_guidance("很快") is None

    def test_the_configured_guidance_is_used_when_upstream_is_silent(self):
        body = _engine().build_body(_cfg(guidance_scale=3.5), _req())
        assert body["options"]["guidance_scale"] == 3.5

    def test_chunk_size_is_sent_only_when_configured(self):
        assert "text_chunk_size" not in _engine().build_body(_cfg(), _req())["options"]
        body = _engine().build_body(_cfg(text_chunk_size=400), _req())
        assert body["options"]["text_chunk_size"] == 400

    def test_retry_badcase_is_never_sent(self):
        """它只在离线模式可用，发错了会像"参数有问题"——这里保持引擎默认。"""
        assert "retry_badcase" not in _engine().build_body(_cfg(), _req())["options"]

    def test_the_unmappable_params_are_reported_not_dropped(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"speed": 1.2, "volume": 1.1})
        )
        said = " ".join(notes)
        assert "未发送" in said
        for label in ("语速", "音量"):
            assert label in said

    def test_a_dialect_is_written_into_the_control_instruction(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "yue"}))
        assert body["input"] == "(粤语)他停下脚步，回头看了她一眼。"
        assert "language" not in body.get("options", {})

    def test_a_dialect_label_is_recognized(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "粤语"}))
        assert body["input"].startswith("(粤语)")

    def test_a_dialect_label_without_a_code_is_recognized(self):
        """东北话、天津话没有统一词表代码，只能从引擎声明的候选原词进来。"""
        body = _engine().build_body(_cfg(), _req(extra={"language": "东北话"}))
        assert body["input"].startswith("(东北话)")

    def test_a_dialect_combines_with_the_emotion_sentence(self):
        body = _engine().build_body(
            _cfg(), _req(emotion="开心", extra={"language": "cmn-cd"})
        )
        assert body["input"].startswith("(四川话，")
        assert "轻快愉悦" in body["input"]

    def test_an_unsupported_dialect_is_reported_not_sent(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"language": "cmn-wh"})
        )
        assert body["input"] == "他停下脚步，回头看了她一眼。"
        said = " ".join(notes)
        assert "未发送" in said and "武汉话" in said

    def test_a_non_dialect_language_is_not_sent(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"language": "en"})
        )
        assert body["input"] == "他停下脚步，回头看了她一眼。"
        assert "未发送" not in " ".join(notes)

    def test_an_english_text_does_not_take_a_chinese_dialect(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(text="He stopped and looked back.", extra={"language": "yue"})
        )
        assert body["input"] == "He stopped and looked back."
        said = " ".join(notes)
        assert "未发送" in said and "只对中文正文生效" in said

    def test_the_dialect_switches_off_ultimate_clone(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(
                text="这是一句要合成的话。",
                reference_audio=_REF,
                reference_text="这是参考音频的逐字稿。",
                extra={"language": "yue"},
            ),
        )
        assert "ultimate_clone" not in body["options"]
        assert "reference_text" not in body
        assert body["input"] == "(粤语)这是一句要合成的话。"
        assert "可控克隆" in " ".join(notes)

    def test_language_is_an_accepted_extra_key(self):
        assert "language" in _engine().accepted_extra_keys()


# --------------------------------------------------------------------------- #
# 种子
# --------------------------------------------------------------------------- #


class TestSeed:
    def test_the_default_is_drawn(self):
        body = _engine().build_body(_cfg(), _req())
        assert isinstance(body["seed"], int)
        assert 0 <= body["seed"] < 2**31

    def test_the_drawn_seed_is_reported(self):
        body, notes = _engine().build_body_with_notes(_cfg(), _req())
        assert f"seed={body['seed']}" in notes

    def test_zero_is_a_fixed_seed(self):
        assert _engine().build_body(_cfg(), _req(extra={"seed": 0}))["seed"] == 0

    def test_an_upstream_seed_wins(self):
        assert _engine().build_body(_cfg(), _req(extra={"seed": 1234}))["seed"] == 1234

    def test_a_configured_seed_is_used_when_upstream_is_silent(self):
        assert _engine().build_body(_cfg(seed=77), _req())["seed"] == 77

    def test_a_blank_upstream_seed_falls_back_to_the_config(self):
        assert _engine().build_body(_cfg(seed=77), _req(extra={"seed": ""}))["seed"] == 77

    def test_a_non_integer_seed_is_refused(self):
        with pytest.raises(EngineError, match="不是整数"):
            resolve_seed("随机")


# --------------------------------------------------------------------------- #
# 自描述
# --------------------------------------------------------------------------- #


class TestDescribe:
    def test_the_mapping_table_covers_the_whole_spec(self):
        rows = _engine().describe()["mapping"]
        assert [row["source"] for row in rows[: len(SPEC_FIELDS)]] == [
            item.key for item in SPEC_FIELDS
        ]
        assert all(row["note"] for row in rows)

    def test_seed_has_a_landing(self):
        """种子确实落在顶层 seed：表里不能写成"无对应参数"。"""
        rows = {row["source"]: row for row in _engine().describe()["mapping"]}
        assert rows["seed"]["target"] == "seed"

    def test_the_prefix_rows_are_declared(self):
        """规范里的两格（指令 / 情绪）在表里指向正文前缀；设计描述只进动态映射。"""
        rows = {row["source"]: row for row in _engine().describe()["mapping"]}
        for key in ("instruction", "emotion"):
            assert "input 前缀" in rows[key]["target"], key
        assert "voice_design" in _engine().dynamic_mappings()

    def test_no_marks(self):
        assert _engine().describe()["marks"] == []

    def test_the_cautions_state_the_three_traps(self):
        cautions = " ".join(_engine().describe()["mapping_cautions"])
        assert "括号" in cautions
        assert "ultimate_clone" in cautions
        assert "1234" in cautions


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class TestClient:
    @pytest.fixture(autouse=True)
    def _reset(self, fake_http: _FakeAsyncClient) -> _FakeAsyncClient:
        return fake_http

    def test_the_request_goes_to_the_speech_endpoint(self):
        _FakeAsyncClient.response = _FakeResponse(content=_wav())
        asyncio.run(_engine().synthesize(_cfg(), _req()))
        call = _FakeAsyncClient.calls[-1]
        assert call["path"] == "/v1/audio/speech"
        assert call["json"]["model"] == "voxcpm2"

    def test_a_happy_response_comes_back_as_wav(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(content=_wav())
            return await _engine().synthesize(_cfg(), _req())

        result = asyncio.run(_run())
        assert result.audio[:4] == b"RIFF"
        assert result.format == "wav"

    def test_a_4xx_envelope_is_translated(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=400,
                content_type="application/json",
                json_body={"error": {"message": "voice_ref must be a WAV file"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="voice_ref must be a WAV file"):
            asyncio.run(_run())

    def test_an_unknown_model_id_says_where_to_look(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=400,
                content_type="application/json",
                json_body={"error": {"message": "unknown model id: voxcpm2"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as caught:
            asyncio.run(_run())
        assert "GET /v1/models" in caught.value.reason

    def test_a_busy_server_is_retryable(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=503,
                content_type="application/json",
                json_body={"code": "server_busy", "message": "model is busy"},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as caught:
            asyncio.run(_run())
        assert caught.value.retryable is True

    def test_other_500s_are_not_retryable(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=500,
                content_type="application/json",
                json_body={"error": {"message": "failed to load model"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as caught:
            asyncio.run(_run())
        assert caught.value.retryable is False
        assert "failed to load model" in caught.value.reason

    def test_a_non_audio_response_is_refused(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                content=b'{"ok":true}', content_type="application/json"
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="响应不是音频"):
            asyncio.run(_run())

    def test_a_tiny_audio_is_refused(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(content=b"RIFF____WAVE")
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match="低于合理的最小长度"):
            asyncio.run(_run())

    def test_a_connect_error_names_the_service(self):
        async def _run() -> object:
            _FakeAsyncClient.raise_error = httpx.ConnectError("refused")
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError, match=r"VoxCPM2（.*）无法连接"):
            asyncio.run(_run())

    def test_the_logged_body_hides_the_reference_path_and_long_text(self):
        from app.engines.audio_http import loggable_body

        logged = loggable_body({"input": "啊" * 400, "voice_ref": "D:/某目录/某参考.wav"})
        assert len(logged["input"]) < 200
        assert logged["voice_ref"] == "某参考.wav"


# --------------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------------- #


class TestIcon:
    def test_the_plugin_ships_its_own_icon(self):
        from app.engines.builtin.voxcpm2 import plugin as plugin_module

        svg = (pathlib.Path(plugin_module.__file__).resolve().parent / "icon.svg").read_text(
            encoding="utf-8"
        )
        assert svg.startswith("<svg")
        assert _engine().icon_svg() == svg
