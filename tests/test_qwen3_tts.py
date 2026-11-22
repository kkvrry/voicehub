"""Qwen3-TTS 插件：**照规格核过的那些点**，每条钉一个断言。

与 ``test_voxcpm2.py`` 同一套写法：不碰网络，假 httpx 客户端既让测试离线跑，
又能断言"到底发了什么"。

这一家最容易写错的地方：

1. **只有克隆一条路**：参考音频与逐字稿缺一不可，缺了要报错而不是降级；
2. **落点分两层**：语言与采样参数在 ``options``，种子在顶层；
3. **语言只认官方 10 种代码**，中文说法先归一，其它语种剔除并说明，不静默发；
4. **种子省略即随机**，插件仍抽一个发出去并报回抽到的数；
5. 没有的能力（内置音色 / 音色 id / 设计 / 情绪 / 语速）一条都不声明，
   给了就在结果说明里报"未发送"。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import ClassVar, Self

import httpx
import pytest

from app.engines.base import EngineError, SynthesisRequest
from app.engines.builtin.qwen3_tts.plugin import (
    SUPPORTED_LANGUAGES,
    Qwen3TtsEngine,
    resolve_seed,
)
from app.engines.builtin.qwen3_tts.settings import Qwen3TtsSettings
from app.engines.registry import list_engines


def _cfg(**over: object) -> Qwen3TtsSettings:
    data: dict[str, object] = {"base_url": "http://127.0.0.1:8080"}
    data.update(over)
    return Qwen3TtsSettings(**data)


def _req(**over: object) -> SynthesisRequest:
    data: dict[str, object] = {
        "text": "他停下脚步，回头看了她一眼。",
        "voice_id": "叶奈法",
        "reference_audio": _REF,
        "reference_text": "她说话时总带着一点倦意。",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> Qwen3TtsEngine:
    return Qwen3TtsEngine()


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


def _body(engine: Qwen3TtsEngine, **over: object) -> dict[str, object]:
    cfg = over.pop("cfg", None) or _cfg()
    body, _ = engine.build_body_with_notes(cfg, _req(**over))  # type: ignore[arg-type]
    return body


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
        assert "qwen3_tts" in engines
        assert isinstance(engines["qwen3_tts"], Qwen3TtsEngine)
        assert engines["qwen3_tts"].label == "Qwen3-TTS"

    def test_the_key_matches_the_audiocpp_family(self):
        # audiocpp 生产方按引擎键找同名的模型家族，两者错开就串不起来。
        assert Qwen3TtsEngine.key == "qwen3_tts"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        for name, field in Qwen3TtsSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_no_secret_fields(self):
        assert Qwen3TtsEngine().secret_fields() == frozenset()

    def test_host_fills_in_a_missing_scheme(self):
        assert _cfg(base_url="127.0.0.1:9000").host == "http://127.0.0.1:9000"

    def test_a_blank_host_falls_back_to_the_default_port(self):
        assert _cfg(base_url="  ").host == "http://127.0.0.1:8080"

    def test_the_defaults_follow_the_engine_spec(self):
        cfg = _cfg()
        assert cfg.model == "qwen3_tts"
        assert cfg.temperature == 0.9
        assert cfg.top_k == 50
        assert cfg.top_p == 1.0
        assert cfg.repetition_penalty == 1.05
        assert cfg.seed == -1


# --------------------------------------------------------------------------- #
# 能力与契约
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        assert _engine().capabilities().kind == "local"

    def test_does_not_claim_a_voice_list_it_cannot_get(self):
        assert _engine().capabilities().voice_listing is False

    def test_does_not_claim_cloning_by_id(self):
        caps = _engine().capabilities()
        assert caps.voice_cloning is False
        assert caps.sample_cloning is False

    def test_takes_reference_audio(self):
        assert _engine().capabilities().reference_audio is True

    def test_does_not_claim_design_or_instruction_or_emotion(self):
        caps = _engine().capabilities()
        assert caps.voice_design is False
        assert caps.instruction_control is False
        assert caps.emotion_control is False

    def test_runs_one_request_at_a_time(self):
        assert _engine().capabilities().max_concurrency == 1

    def test_declares_the_ten_official_languages(self):
        assert _engine().capabilities().languages == SUPPORTED_LANGUAGES
        assert len(SUPPORTED_LANGUAGES) == 10


class TestSynthesisSpec:
    def test_requires_the_reference_audio(self):
        assert _engine().synthesis_spec().requires == ("reference_audio",)

    def test_only_wav(self):
        spec = _engine().synthesis_spec()
        assert spec.formats == ("wav",)
        assert spec.reference_formats == ("wav",)

    def test_the_transcript_is_a_gate(self):
        assert _engine().synthesis_spec().needs_transcript is True

    def test_the_reference_duration_is_bounded(self):
        assert _engine().synthesis_spec().reference_duration == (0.0, 30.0)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


class TestAssembly:
    def test_plain_text_is_sent_as_input(self):
        assert _body(_engine())["input"] == "他停下脚步，回头看了她一眼。"

    def test_the_reference_audio_lands_at_the_top_level(self):
        body = _body(_engine())
        assert body["voice_ref"] == _REF

    def test_the_transcript_lands_at_the_top_level(self):
        body = _body(_engine())
        assert body["reference_text"] == "她说话时总带着一点倦意。"

    def test_the_model_id_is_sent(self):
        assert _body(_engine(), cfg=_cfg(model="我的模型"))["model"] == "我的模型"

    def test_a_missing_reference_audio_fails_loudly(self):
        with pytest.raises(EngineError) as exc:
            _engine().build_body(_cfg(), _req(reference_audio=""))
        assert "参考音频" in str(exc.value)

    def test_a_missing_reference_file_fails_loudly(self, tmp_path: pathlib.Path):
        with pytest.raises(EngineError) as exc:
            _engine().build_body(_cfg(), _req(reference_audio=str(tmp_path / "没有.wav")))
        assert "读不到参考音频" in str(exc.value)

    def test_a_missing_transcript_fails_loudly(self):
        with pytest.raises(EngineError) as exc:
            _engine().build_body(_cfg(), _req(reference_text=""))
        assert "逐字稿" in str(exc.value)


class TestGenerationOptions:
    def test_config_defaults_are_sent_into_options(self):
        options = _body(_engine())["options"]
        assert options["temperature"] == 0.9
        assert options["top_k"] == 50
        assert options["top_p"] == 1.0
        assert options["repetition_penalty"] == 1.05

    def test_upstream_sampling_wins(self):
        body = _body(
            _engine(),
            extra={"temperature": 0.4, "top_k": 10, "top_p": 0.8, "repetition_penalty": 1.2},
        )
        options = body["options"]
        assert options["temperature"] == 0.4
        assert options["top_k"] == 10
        assert options["top_p"] == 0.8
        assert options["repetition_penalty"] == 1.2

    def test_max_tokens_and_chunk_size_go_to_options(self):
        body = _body(_engine(), extra={"max_tokens": 512, "text_chunk_size": 300})
        assert body["options"]["max_tokens"] == 512
        assert body["options"]["text_chunk_size"] == 300

    def test_the_zero_defaults_are_not_sent(self):
        options = _body(_engine())["options"]
        assert "max_tokens" not in options
        assert "text_chunk_size" not in options

    def test_configured_chunk_size_is_used_when_upstream_is_silent(self):
        body = _body(_engine(), cfg=_cfg(text_chunk_size=600))
        assert body["options"]["text_chunk_size"] == 600


class TestLanguage:
    def test_a_code_is_sent_as_is(self):
        body = _body(_engine(), extra={"language": "en"})
        assert body["options"]["language"] == "en"

    def test_a_chinese_label_is_normalized(self):
        body = _body(_engine(), extra={"language": "日语"})
        assert body["options"]["language"] == "ja"

    def test_an_unsupported_language_is_reported_not_sent(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"language": "粤语"})
        )
        assert "language" not in body["options"]
        assert any("未发送" in note and "粤语" in note for note in notes)

    def test_no_language_means_no_field(self):
        assert "language" not in _body(_engine())["options"]

    def test_language_is_an_accepted_extra_key(self):
        assert "language" in _engine().accepted_extra_keys()


class TestSeed:
    def test_the_default_is_drawn(self):
        body = _body(_engine())
        assert 0 <= int(body["seed"]) <= 2**32 - 1

    def test_the_drawn_seed_is_reported(self):
        body, notes = _engine().build_body_with_notes(_cfg(), _req())
        assert f"seed={body['seed']}" in notes

    def test_zero_is_a_fixed_seed(self):
        assert _body(_engine(), extra={"seed": 0})["seed"] == 0

    def test_an_upstream_seed_wins(self):
        assert _body(_engine(), extra={"seed": 7})["seed"] == 7

    def test_a_configured_seed_is_used_when_upstream_is_silent(self):
        assert _body(_engine(), cfg=_cfg(seed=99))["seed"] == 99

    def test_a_non_integer_seed_is_refused(self):
        with pytest.raises(EngineError):
            resolve_seed("随便")

    def test_a_negative_seed_is_drawn_and_named(self):
        seed, note = resolve_seed(-1)
        assert seed >= 0
        assert note == f"seed={seed}"


class TestMapping:
    def test_the_mapping_table_covers_the_spec(self):
        from app.domain.request_spec import SPEC_FIELDS

        mapping = _engine().mapping()
        targets = {item.source for item in mapping.params} | set(_engine().dynamic_mappings()) | set(_engine().dynamic_mappings())
        unsupported = set(_engine().unsupported())
        for field in SPEC_FIELDS:
            if field.key in {"text", "voice_id", "reference_audio", "reference_text"}:
                continue
            assert field.key in targets or field.key in unsupported, field.key

    def test_seed_has_a_landing(self):
        assert "seed" in _engine().dynamic_mappings()
        assert "seed" in _engine().accepted_extra_keys()

    def test_the_unmappable_params_are_reported_not_dropped(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"speed": 1.3, "volume": 1.2})
        )
        assert any("未发送" in note and "语速" in note for note in notes)
        assert any("音量" in note for note in notes)

    def test_no_marks(self):
        assert _engine().mark_map() == {}

    def test_the_cautions_state_the_three_traps(self):
        joined = " ".join(_engine().mapping().cautions)
        assert "逐字稿" in joined
        assert "种子" in joined
        assert "10 种" in joined


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
        assert call["json"]["model"] == "qwen3_tts"

    def test_a_happy_response_comes_back_as_wav(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(content=_wav())
            return await _engine().synthesize(_cfg(), _req())

        result = asyncio.run(_run())
        assert result.audio[:4] == b"RIFF"
        assert result.format == "wav"
        assert any(note.startswith("seed=") for note in result.notes)

    def test_a_4xx_envelope_is_translated(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=400,
                content_type="application/json",
                json_body={"error": {"message": "voice_ref must be a WAV file"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as exc:
            asyncio.run(_run())
        assert "voice_ref must be a WAV file" in str(exc.value)

    def test_a_busy_server_is_retryable(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=503,
                content_type="application/json",
                json_body={"error": {"message": "server_busy"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as exc:
            asyncio.run(_run())
        assert exc.value.retryable is True

    def test_other_500s_are_not_retryable(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                status_code=500,
                content_type="application/json",
                json_body={"error": {"message": "cuda out of memory"}},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as exc:
            asyncio.run(_run())
        assert exc.value.retryable is False

    def test_a_non_audio_response_is_refused(self):
        async def _run() -> object:
            _FakeAsyncClient.response = _FakeResponse(
                content_type="application/json",
                json_body={"ok": True},
            )
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError):
            asyncio.run(_run())

    def test_a_connect_error_names_the_service(self):
        async def _run() -> object:
            _FakeAsyncClient.raise_error = httpx.ConnectError("refused")
            return await _engine().synthesize(_cfg(), _req())

        with pytest.raises(EngineError) as exc:
            asyncio.run(_run())
        assert "Qwen3-TTS" in str(exc.value)
        assert exc.value.code == "engine_unreachable"

    def test_the_logged_body_hides_the_reference_path(self):
        from app.engines.audio_http import loggable_body

        logged = loggable_body({"input": "啊" * 400, "voice_ref": "D:/某目录/某参考.wav"})
        assert len(logged["input"]) < 200
        assert logged["voice_ref"] == "某参考.wav"


# --------------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------------- #


class TestIcon:
    def test_the_plugin_ships_its_own_icon(self):
        from app.engines.builtin.qwen3_tts import plugin as plugin_module

        svg = (pathlib.Path(plugin_module.__file__).resolve().parent / "icon.svg").read_text(
            encoding="utf-8"
        )
        assert svg.startswith("<svg")
        assert _engine().icon_svg() == svg
