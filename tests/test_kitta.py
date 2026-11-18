"""Kitta Audio 插件：**照官方文档核过的那些点**，每一条都钉一个断言。

没有真实网络请求——假 httpx 客户端既让测试离线跑，又能断言"到底发了什么"。
这一家有三处必须钉住：模型走请求体 ``modelId``、成功响应是裸音频、
被忽略的参数只写在响应头里。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.request_spec import SPEC_FIELDS
from app.engines.base import KIND_CLOUD, CloneRequest, EngineError, SynthesisRequest
from app.engines.builtin.kitta import client as kitta_client
from app.engines.builtin.kitta.mapping import with_emotion
from app.engines.builtin.kitta.plugin import KittaEngine
from app.engines.builtin.kitta.settings import KittaSettings
from app.engines.builtin.kitta.voices import parse_voice_list
from app.engines.mapping import apply_mapping


def _cfg(**over: object) -> KittaSettings:
    data: dict[str, object] = {"api_key": "sk-test"}
    data.update(over)
    return KittaSettings(**data)  # type: ignore[arg-type]


def _request(text: str = "你好", **extra: object) -> SynthesisRequest:
    return SynthesisRequest(
        text=text,
        voice_id="voice-1",
        extra={key: value for key, value in extra.items() if value is not None},
    )


# --------------------------------------------------------------------------- #
# 假 httpx
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(
        self,
        payload: object = None,
        *,
        status_code: int = 200,
        content: bytes = b"\x00" * 400,
        content_type: str = "audio/mpeg",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self.headers = {"content-type": content_type, **(headers or {})}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class _FakeAsyncClient:
    calls: ClassVar[list[dict[str, object]]] = []
    response: _FakeResponse = _FakeResponse()
    is_closed: bool = False

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append(
            {"method": "GET", "path": path, "init": self.init_kwargs, **kwargs}
        )
        return _FakeAsyncClient.response

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append(
            {"method": "POST", "path": path, "init": self.init_kwargs, **kwargs}
        )
        return _FakeAsyncClient.response


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        for name, field in KittaSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_api_key_is_the_only_secret(self):
        assert KittaEngine.secret_fields() == frozenset({"api_key"})

    def test_host_and_custom_address(self):
        assert _cfg().host == "https://fishaudio.org"
        assert _cfg(base_url="https://proxy.local/").host == "https://proxy.local"

    def test_readiness_names_what_is_missing(self):
        engine = KittaEngine()
        assert "API Key" in engine.readiness(_cfg(api_key=""))
        assert engine.readiness(_cfg()) == ""


# --------------------------------------------------------------------------- #
# 请求体装配
# --------------------------------------------------------------------------- #


class TestBuildBody:
    def _body(self, cfg: KittaSettings | None = None, **extra: object):
        engine = KittaEngine()
        return engine.build_body(cfg or _cfg(), _request(**extra))

    def test_voice_id_lands_on_camel_case(self):
        body = self._body()
        assert body["voiceId"] == "voice-1"
        assert body["text"] == "你好"

    def test_config_fills_defaults(self):
        body = self._body(_cfg(audio_format="wav", text_normalization=False))
        assert body["format"] == "wav"
        assert body["textNormalization"] is False

    def test_upstream_format_beats_the_configured_default(self):
        assert self._body(_cfg(audio_format="mp3"), format="ogg")["format"] == "ogg"

    def test_model_id_is_only_sent_when_configured(self):
        """留空是"由音色所属引擎决定"，补一个默认值等于替平台做了选择。"""
        assert "modelId" not in self._body()
        assert self._body(_cfg(model_id="fishaudio-s21pro-flash"))["modelId"] == (
            "fishaudio-s21pro-flash"
        )

    def test_speed_is_sent_as_a_number(self):
        assert self._body(speed="1.2")["speed"] == 1.2

    @pytest.mark.parametrize(("ratio", "db"), [(2.0, 6.0), (1.0, 0.0), (0.5, -6.0)])
    def test_volume_ratio_becomes_decibels(self, ratio: float, db: float):
        assert self._body(volume=ratio)["volume"] == db

    @pytest.mark.parametrize(("ratio", "semitones"), [(2.0, 12.0), (1.0, 0.0), (0.5, -12.0)])
    def test_pitch_ratio_becomes_semitones(self, ratio: float, semitones: float):
        """统一层是倍率，平台那一格是 −12…12 的半音。"""
        assert self._body(pitch=ratio)["pitch"] == semitones

    def test_a_non_positive_pitch_is_refused(self):
        with pytest.raises(EngineError, match="大于 0"):
            self._body(pitch=0)

    def test_language_is_passed_through_as_a_code(self):
        assert self._body(language="yue")["language"] == "yue"

    def test_emotion_becomes_a_leading_instruction_sentence(self):
        assert self._body(emotion="开心")["instruction"] == "情绪：开心。"
        assert "emotion" not in self._body(emotion="开心")

    def test_a_synonym_is_normalised(self):
        assert with_emotion("", "高兴") == "情绪：开心。"

    def test_an_unknown_emotion_is_kept(self):
        """这一格是自由文本，平台听得懂自然语言——替用户删掉才是丢信息。"""
        assert with_emotion("", "恍惚") == "情绪：恍惚。"

    def test_emotion_and_instruction_are_merged(self):
        body = self._body(emotion="悲伤", instruction="语速放慢")
        assert body["instruction"] == "情绪：悲伤。语速放慢"

    def test_an_overlong_instruction_is_refused(self):
        with pytest.raises(EngineError, match="1600"):
            self._body(instruction="字" * 1601)

    @pytest.mark.parametrize(("key", "value"), [("speed", 3), ("volume", 40), ("pitch", 30)])
    def test_out_of_range_values_are_blocked_before_sending(self, key: str, value: float):
        with pytest.raises(EngineError, match="没有发出去"):
            self._body(**{key: value})

    def test_the_table_covers_every_spec_field(self):
        engine = KittaEngine()
        placed = {item.source for item in engine.mapping().params}
        dynamic = set(engine.dynamic_mappings())
        unsupported = set(engine.unsupported())
        for field in SPEC_FIELDS:
            assert field.key in placed | dynamic | unsupported, field.key

    def test_instruction_is_an_extra_key_the_gateway_will_pass(self):
        extra = KittaEngine().accepted_extra_keys()
        assert {"instruction", "speed", "volume", "pitch", "language"} <= extra

    def test_the_whole_table_agrees_with_apply_mapping(self):
        mapping = KittaEngine().mapping()
        body = apply_mapping(mapping, {"text": "你好", "voice_id": "v"})
        assert body == {"text": "你好", "voiceId": "v"}
        assert mapping.required_sources() == ("text", "voice_id")
        assert mapping.cautions


# --------------------------------------------------------------------------- #
# 音色清单解析
# --------------------------------------------------------------------------- #


class TestVoices:
    @staticmethod
    def _item(**over: object) -> dict:
        item: dict[str, object] = {
            "voiceId": "voice_1",
            "title": "旁白男声",
            "description": "沉稳的中年男性旁白",
            "isPersonal": False,
            "primaryLanguage": "zh",
            "languages": ["zh", "en"],
        }
        item.update(over)
        return item

    def test_id_and_title(self):
        items = parse_voice_list({"items": [self._item()]})
        assert [v.id for v in items] == ["voice_1"]
        assert items[0].name == "旁白男声"

    def test_the_v3_name_field_is_also_read(self):
        assert parse_voice_list({"items": [self._item(title=None, name="Clara")]})[0].name == "Clara"

    def test_primary_language_wins(self):
        items = parse_voice_list({"items": [self._item(primaryLanguage="ja", languages=["ja"])]})
        assert items[0].language == "ja"

    def test_languages_are_used_when_primary_is_missing(self):
        items = parse_voice_list({"items": [self._item(primaryLanguage=None, languages=["en"])]})
        assert items[0].language == "en"

    def test_what_no_one_states_stays_empty(self):
        items = parse_voice_list({"items": [self._item(primaryLanguage=None, languages=[])]})
        assert items[0].language == ""

    def test_duplicates_are_dropped(self):
        assert len(parse_voice_list({"items": [self._item(), self._item()]})) == 1

    def test_a_gateway_shape_is_also_read(self):
        assert parse_voice_list({"data": [self._item()]})[0].id == "voice_1"

    def test_garbage_yields_an_empty_list(self):
        assert parse_voice_list(None) == ()
        assert parse_voice_list({"items": "nope"}) == ()


# --------------------------------------------------------------------------- #
# 真的出去的那几个请求
# --------------------------------------------------------------------------- #


class TestRequests:
    def test_synthesis_posts_to_the_v3_path(self, fake_http):
        result = asyncio.run(
            kitta_client.synthesize(_cfg(), {"text": "你好", "format": "wav"}, label="Kitta Audio")
        )

        call = fake_http.calls[0]
        assert call["path"] == kitta_client.TTS_PATH == "/api/open/v3/speech/tts"
        assert call["json"] == {"text": "你好", "format": "wav"}
        assert call["init"]["headers"]["Authorization"] == "Bearer sk-test"
        assert call["init"]["base_url"] == "https://fishaudio.org"
        assert result.format == "wav"
        assert len(result.audio) == 400

    def test_rate_limit_is_marked_retryable(self, fake_http):
        fake_http.response = _FakeResponse(
            {"code": "ERR_RATE_LIMITED", "message": "too many requests", "requestId": "req_1"},
            status_code=429,
        )
        with pytest.raises(EngineError) as exc:
            asyncio.run(kitta_client.synthesize(_cfg(), {"text": "你好"}, label="Kitta Audio"))
        assert exc.value.code == "rate_limited"
        assert exc.value.retryable is True
        assert "requestId=req_1" in str(exc.value)

    def test_insufficient_quota_is_explained(self, fake_http):
        fake_http.response = _FakeResponse(
            {"code": "ERR_INSUFFICIENT_QUOTA", "message": "quota exceeded"}, status_code=402
        )
        with pytest.raises(EngineError) as exc:
            asyncio.run(kitta_client.synthesize(_cfg(), {"text": "你好"}, label="Kitta Audio"))
        assert "额度不足" in str(exc.value)
        assert "quota exceeded" in str(exc.value)

    def test_ignored_parameters_are_reported_not_swallowed(self, fake_http):
        """平台只在这条响应头里说"这个参数没用上"——不转出来就永远查不出"调了没生效"。"""
        fake_http.response = _FakeResponse(
            headers={
                "x-openapi-ignored-parameters": "pitch",
                "x-openapi-credits-used": "6",
            }
        )
        result = asyncio.run(
            kitta_client.synthesize(_cfg(), {"text": "你好"}, label="Kitta Audio")
        )
        assert any("pitch" in note and "音高" in note for note in result.notes)
        assert "6 积分" in result.cost_note

    def test_suspiciously_short_audio_is_refused(self, fake_http):
        fake_http.response = _FakeResponse(content=b"\x00" * 8)
        with pytest.raises(EngineError, match="合理的最小长度"):
            asyncio.run(kitta_client.synthesize(_cfg(), {"text": "你好"}, label="Kitta Audio"))

    def test_voice_listing_asks_for_the_configured_scope(self, fake_http):
        fake_http.response = _FakeResponse(
            {"items": [{"voiceId": "v1", "title": "音色一"}]}, content_type="application/json"
        )
        result = asyncio.run(kitta_client.fetch_voices(_cfg(), label="Kitta Audio"))

        call = fake_http.calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "/api/open/v3/voices"
        assert call["params"]["includePersonal"] == "true"
        assert [v.id for v in result.items] == ["v1"]
        assert result.why == ""

    def test_public_scope_excludes_personal_voices(self, fake_http):
        fake_http.response = _FakeResponse({"items": []}, content_type="application/json")
        asyncio.run(kitta_client.fetch_voices(_cfg(voice_scope="public"), label="Kitta Audio"))
        params = fake_http.calls[0]["params"]
        assert params["includePersonal"] == "false"
        assert "modelType" not in params

    def test_personal_scope_filters_by_model_type(self, fake_http):
        fake_http.response = _FakeResponse({"items": []}, content_type="application/json")
        asyncio.run(kitta_client.fetch_voices(_cfg(voice_scope="personal"), label="Kitta Audio"))
        assert fake_http.calls[0]["params"]["modelType"] == "personal"

    def test_platform_refusal_becomes_the_reason_not_an_exception(self, fake_http):
        fake_http.response = _FakeResponse(
            {"code": "ERR_UNAUTHORIZED", "message": "invalid token", "requestId": "req_9"},
            status_code=401,
            content_type="application/json",
        )
        result = asyncio.run(kitta_client.fetch_voices(_cfg(), label="Kitta Audio"))
        assert result.items == ()
        assert "invalid token" in result.why and "req_9" in result.why

    def test_empty_listing_explains_how_to_get_a_voice(self, fake_http):
        fake_http.response = _FakeResponse({"items": []}, content_type="application/json")
        result = asyncio.run(
            kitta_client.fetch_voices(_cfg(voice_scope="personal"), label="Kitta Audio")
        )
        assert "克隆" in result.why

    def test_clone_uploads_the_sample_as_multipart(self, fake_http, tmp_path):
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 64)
        fake_http.response = _FakeResponse(
            {"voiceId": "new-id", "title": "旁白"}, status_code=200, content_type="application/json"
        )

        meta = asyncio.run(
            KittaEngine().clone_voice(
                _cfg(),
                CloneRequest(name="旁白", audio_path=str(sample), transcript="这是一段话"),
            )
        )

        call = fake_http.calls[0]
        assert call["path"] == "/api/open/v1/voices"
        fields = dict(call["data"])
        assert fields["name"] == "旁白"
        assert fields["visibility"] == "private"
        assert fields["referenceText"] == "这是一段话"
        assert "audioFiles" in call["files"]
        assert meta.id == "new-id"

    def test_clone_without_audio_is_refused_before_uploading(self, fake_http):
        with pytest.raises(EngineError, match="参考音频"):
            asyncio.run(KittaEngine().clone_voice(_cfg(), CloneRequest(name="空")))
        assert fake_http.calls == []

    def test_an_unsupported_container_is_refused_locally(self, fake_http, tmp_path):
        """上传一次是白花的，平台回的 400 也只会说"文件不支持"。"""
        sample = tmp_path / "sample.txt"
        sample.write_bytes(b"nope")
        with pytest.raises(EngineError, match="不受支持"):
            asyncio.run(
                KittaEngine().clone_voice(_cfg(), CloneRequest(name="旁白", audio_path=str(sample)))
            )
        assert fake_http.calls == []

    def test_an_oversized_sample_is_refused_locally(self, fake_http, tmp_path):
        sample = tmp_path / "big.wav"
        sample.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
        with pytest.raises(EngineError, match="10 MB"):
            asyncio.run(
                KittaEngine().clone_voice(_cfg(), CloneRequest(name="旁白", audio_path=str(sample)))
            )
        assert fake_http.calls == []

    def test_a_requested_voice_id_is_refused_not_ignored(self, fake_http, tmp_path):
        sample = tmp_path / "s.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 64)
        with pytest.raises(EngineError, match="由平台生成"):
            asyncio.run(
                KittaEngine().clone_voice(
                    _cfg(),
                    CloneRequest(name="旁白", voice_id="my_voice", audio_path=str(sample)),
                )
            )


# --------------------------------------------------------------------------- #
# 没配密钥时：一次网络请求都不该发出去
# --------------------------------------------------------------------------- #


class TestOfflineGuards:
    @pytest.fixture()
    def no_network(self, monkeypatch: pytest.MonkeyPatch):
        def boom(*args: object, **kwargs: object):
            raise AssertionError("没配密钥却发出了网络请求")

        monkeypatch.setattr(httpx, "AsyncClient", boom)

    def test_voice_listing_says_what_is_missing(self, no_network):
        result = asyncio.run(kitta_client.fetch_voices(_cfg(api_key=""), label="Kitta Audio"))
        assert result.items == ()
        assert "API Key" in result.why

    def test_synthesis_says_what_is_missing(self, no_network):
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(
                kitta_client.synthesize(_cfg(api_key=""), {"text": "你好"}, label="Kitta Audio")
            )

    def test_clone_says_what_is_missing(self, no_network, tmp_path):
        sample = tmp_path / "s.wav"
        sample.write_bytes(b"RIFF")
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(
                KittaEngine().clone_voice(
                    _cfg(api_key=""), CloneRequest(name="x", audio_path=str(sample))
                )
            )


# --------------------------------------------------------------------------- #
# 自描述
# --------------------------------------------------------------------------- #


class TestDescribe:
    def test_declares_itself_a_cloud_service(self):
        caps = KittaEngine().capabilities()
        assert caps.kind == KIND_CLOUD
        assert caps.voice_listing is True
        assert caps.voice_cloning is True
        assert caps.reference_audio is False
        assert caps.max_chars == 10_000

    def test_emotion_is_not_declared_as_a_native_parameter(self):
        """情绪走引导指令，不占「情绪控制」那枚标签（与 BreezeTTS 同一口径）。"""
        caps = KittaEngine().capabilities()
        assert caps.instruction_control is True
        assert caps.emotion_control is False

    def test_synthesis_spec_asks_only_for_a_voice_id(self):
        spec = KittaEngine().synthesis_spec()
        assert spec.requires == ("voice_id",)
        assert spec.formats == ("mp3", "wav", "ogg")

    def test_describe_carries_everything_the_ui_needs(self):
        data = KittaEngine().describe()
        assert data["key"] == "kitta"
        assert data["kind_label"] == "云端"
        assert data["secret_fields"] == ["api_key"]
        assert data["settings_schema"]["properties"]["api_key"]["widget"] == "password"
        assert all(row["label"] for row in data["mapping"])
        assert any("provider" in line for line in data["mapping_cautions"])

    def test_the_emotion_row_points_at_the_instruction(self):
        rows = {row["source"]: row for row in KittaEngine().describe()["mapping"]}
        assert rows["emotion"]["target"] == "instruction"

    def test_presets_are_loaded_from_the_plugin_directory(self):
        presets = KittaEngine().field_presets()["instruction"]
        assert presets and all(label and text for label, text in presets)

    def test_the_clone_page_states_the_sample_requirement(self):
        sections = {section.key: section for section in KittaEngine().ui_sections()}
        assert "10 MB" in sections["clone"].note

    def test_icon_comes_from_the_plugin_directory(self):
        svg = KittaEngine().icon_svg()
        assert svg.lstrip().startswith("<svg")
        assert "<path" in svg or "<rect" in svg
