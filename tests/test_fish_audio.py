"""Fish Audio 插件：**照官方文档核过的那些点**，每一条都钉一个断言。

文件里没有一次真实网络请求——假 httpx 客户端既让测试离线跑，又能断言"到底发了什么"
（路径、方法、请求头、body）。这一家有三处容易踩：模型走请求头、成功响应是裸音频、
克隆是 multipart。三处都钉在这里。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.request_spec import SPEC_FIELDS
from app.engines.base import KIND_CLOUD, CloneRequest, EngineError, SynthesisRequest
from app.engines.builtin.fish_audio import client as fish_client
from app.engines.builtin.fish_audio.mapping import (
    MAPPING,
    MP3_BITRATES,
    emotion_tag,
)
from app.engines.builtin.fish_audio.plugin import FishAudioEngine
from app.engines.builtin.fish_audio.settings import MODEL_CHOICES, FishAudioSettings
from app.engines.builtin.fish_audio.voices import parse_voice_list
from app.engines.mapping import apply_mapping


def _cfg(**over: object) -> FishAudioSettings:
    data: dict[str, object] = {"api_key": "sk-test"}
    data.update(over)
    return FishAudioSettings(**data)  # type: ignore[arg-type]


def _request(text: str = "你好", **extra: object) -> SynthesisRequest:
    return SynthesisRequest(
        text=text,
        voice_id="fish-voice-1",
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
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self.headers = {"content-type": content_type}

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
        _FakeAsyncClient.calls.append({"method": "GET", "path": path, "init": self.init_kwargs, **kwargs})
        return _FakeAsyncClient.response

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"method": "POST", "path": path, "init": self.init_kwargs, **kwargs})
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
        for name, field in FishAudioSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_api_key_is_the_only_secret(self):
        assert FishAudioEngine.secret_fields() == frozenset({"api_key"})

    def test_deprecated_model_is_not_offered(self):
        """s1 已宣布停用，且标签写法与 S2 不同：给了候选就等于给了一个坏选择。"""
        values = [value for value, _ in MODEL_CHOICES]
        assert "s1" not in values
        assert values[0] == "s2.1-pro"

    def test_custom_base_url_wins(self):
        assert _cfg().host == "https://api.fish.audio"
        assert _cfg(base_url="https://proxy.local/").host == "https://proxy.local"

    def test_readiness_names_what_is_missing(self):
        engine = FishAudioEngine()
        assert "API Key" in engine.readiness(_cfg(api_key=""))
        assert engine.readiness(_cfg()) == ""


# --------------------------------------------------------------------------- #
# 请求体装配
# --------------------------------------------------------------------------- #


class TestBuildBody:
    def _body(self, cfg: FishAudioSettings | None = None, text: str = "你好", **extra: object):
        engine = FishAudioEngine()
        return engine.build_body(cfg or _cfg(), _request(engine.prepare_text(text), **extra))

    def test_voice_id_lands_on_reference_id(self):
        body = self._body()
        assert body["reference_id"] == "fish-voice-1"
        assert body["text"] == "你好"

    def test_config_fills_what_upstream_did_not_ask_for(self):
        body = self._body(_cfg(audio_format="wav", sample_rate=24000, latency="balanced"))
        assert body["format"] == "wav"
        assert body["sample_rate"] == 24000
        assert body["latency"] == "balanced"
        assert body["chunk_length"] == 200
        assert body["normalize"] is True
        assert body["temperature"] == 0.7
        assert body["top_p"] == 0.7

    def test_upstream_format_beats_the_configured_default(self):
        assert self._body(_cfg(audio_format="mp3"), format="wav")["format"] == "wav"

    def test_speed_is_sent_as_a_number(self):
        assert self._body(speed="1.2")["prosody"]["speed"] == 1.2

    @pytest.mark.parametrize(("ratio", "db"), [(2.0, 6.0), (1.0, 0.0), (0.5, -6.0)])
    def test_volume_ratio_becomes_decibels(self, ratio: float, db: float):
        """平台那一格是分贝，统一层是倍率：``20×log₁₀``。"""
        assert self._body(volume=ratio)["prosody"]["volume"] == db

    @pytest.mark.parametrize(("bps", "kbps"), [(128000, 128), (48000, 64), (200000, 192)])
    def test_bitrate_is_snapped_to_a_platform_tier(self, bps: int, kbps: int):
        """平台只收 64/128/192 三档，其余值取最接近的一档（映射表里写明了）。"""
        assert self._body(bitrate=bps)["mp3_bitrate"] == kbps
        assert kbps in MP3_BITRATES

    @pytest.mark.parametrize(("word", "tag"), [("开心", "happy"), ("高兴", "happy"), ("愤怒", "angry")])
    def test_emotion_becomes_a_leading_tag(self, word: str, tag: str):
        assert self._body(emotion=word)["text"] == f"[{tag}] 你好"

    def test_an_unknown_emotion_is_passed_through_not_dropped(self):
        """这一家接受自由文本标签：认不出就用原词，替用户删掉才是丢信息。"""
        assert emotion_tag("恍惚") == "恍惚"
        assert self._body(emotion="恍惚")["text"] == "[恍惚] 你好"

    def test_instruction_becomes_a_leading_tag(self):
        assert self._body(instruction="语速放慢")["text"] == "[语速放慢] 你好"

    def test_emotion_and_instruction_share_the_text_prefix(self):
        body = self._body(emotion="悲伤", instruction="压低嗓音，音量收着说")
        assert body["text"] == "[sad] [压低嗓音，音量收着说] 你好"

    def test_brackets_in_a_direction_do_not_break_the_tag(self):
        assert self._body(instruction="[放慢]")["text"] == "[放慢] 你好"

    def test_without_expression_the_text_is_untouched(self):
        assert self._body()["text"] == "你好"

    @pytest.mark.parametrize(("mark", "native"), [("笑", "[laughing]"), ("咳嗽", "[coughs]")])
    def test_a_mark_in_the_text_becomes_the_platform_tag(self, mark: str, native: str):
        assert self._body(text=f"你好[[{mark}]]啊")["text"] == f"你好{native}啊"

    def test_an_unknown_mark_is_passed_through(self):
        """官方接受自由文本标签，所以表外的标签不删，原样带方括号。"""
        assert self._body(text="你好[[口哨]]啊")["text"] == "你好[口哨]啊"

    def test_a_pause_loses_its_seconds_here(self):
        """平台不接受秒数——这是能力边界，写在 cautions 里，不假装支持。"""
        assert self._body(text="你好[[停顿:0.4]]啊")["text"] == "你好[pause]啊"
        assert any("0.4 秒" in line for line in MAPPING.cautions)

    def test_speed_out_of_range_is_blocked_before_sending(self):
        with pytest.raises(EngineError, match="没有发出去"):
            self._body(speed=3.0)

    def test_the_table_covers_every_spec_field(self):
        """规范里的每一项要么有落点，要么在 unsupported 里写原因——不许空着不说。"""
        engine = FishAudioEngine()
        placed = {item.source for item in engine.mapping().params}
        dynamic = set(engine.dynamic_mappings())
        unsupported = set(engine.unsupported())
        for field in SPEC_FIELDS:
            assert field.key in placed | dynamic | unsupported, field.key

    def test_emotion_is_an_extra_key_the_gateway_will_pass(self):
        """``instruction`` 走 extra：没登记进 dynamic_mappings 就会被网关剔除。"""
        assert "instruction" in FishAudioEngine().accepted_extra_keys()

    def test_the_whole_table_agrees_with_apply_mapping(self):
        mapping = FishAudioEngine().mapping()
        body = apply_mapping(mapping, {"text": "你好", "voice_id": "v"})
        assert body == {"text": "你好", "reference_id": "v"}
        assert mapping.required_sources() == ("text", "voice_id")


# --------------------------------------------------------------------------- #
# 音色清单解析
# --------------------------------------------------------------------------- #


class TestVoices:
    @staticmethod
    def _item(**over: object) -> dict:
        item: dict[str, object] = {
            "_id": "abc123",
            "title": "旁白男声",
            "description": "沉稳的中年男性旁白",
            "type": "tts",
            "state": "trained",
            "tags": ["旁白"],
            "languages": ["zh", "en"],
        }
        item.update(over)
        return item

    def test_id_title_and_state(self):
        items = parse_voice_list({"items": [self._item()]})
        assert [v.id for v in items] == ["abc123"]
        assert items[0].name == "旁白男声"

    def test_platform_language_is_read_before_guessing(self):
        """``languages`` 是平台明确声明的那一格，不该被说明文字里的猜测盖掉。"""
        items = parse_voice_list({"items": [self._item(languages=["ja"])]})
        assert items[0].language == "ja"

    def test_a_language_outside_the_vocabulary_falls_back_to_traits(self):
        """平台给了词表外的代码时，退回按说明文字识别，而不是留一个空值。"""
        items = parse_voice_list(
            {"items": [self._item(languages=["xx"], description="粤语旁白，中年男声")]}
        )
        assert items[0].language == "yue"

    def test_what_no_one_states_stays_empty(self):
        """两处都没提语言就空着——不猜。"""
        items = parse_voice_list({"items": [self._item(languages=[])]})
        assert items[0].language == ""

    def test_speech_to_text_models_are_not_voices(self):
        items = parse_voice_list({"items": [self._item(type="svc")]})
        assert items == ()

    def test_failed_models_are_skipped(self):
        items = parse_voice_list({"items": [self._item(state="failed")]})
        assert items == ()

    def test_duplicates_are_dropped(self):
        items = parse_voice_list({"items": [self._item(), self._item()]})
        assert len(items) == 1

    def test_a_gateway_shape_is_also_read(self):
        assert parse_voice_list({"data": [self._item()]})[0].id == "abc123"

    def test_garbage_yields_an_empty_list(self):
        assert parse_voice_list(None) == ()
        assert parse_voice_list({"items": "nope"}) == ()


# --------------------------------------------------------------------------- #
# 真的出去的那几个请求
# --------------------------------------------------------------------------- #


class TestRequests:
    def test_synthesis_posts_to_v1_tts_with_the_model_header(self, fake_http):
        result = asyncio.run(
            fish_client.synthesize(_cfg(model="s2.1-pro"), {"text": "你好"}, label="Fish Audio")
        )

        call = fake_http.calls[0]
        assert call["path"] == fish_client.TTS_PATH == "/v1/tts"
        assert call["json"] == {"text": "你好"}
        assert call["init"]["headers"]["model"] == "s2.1-pro"
        assert call["init"]["headers"]["Authorization"] == "Bearer sk-test"
        assert call["init"]["base_url"] == "https://api.fish.audio"
        assert result.format == "mp3"
        assert len(result.audio) == 400

    def test_rate_limit_is_marked_retryable(self, fake_http):
        fake_http.response = _FakeResponse(
            {"status": 429, "message": "too many requests"}, status_code=429
        )
        with pytest.raises(EngineError) as exc:
            asyncio.run(fish_client.synthesize(_cfg(), {"text": "你好"}, label="Fish Audio"))
        assert exc.value.code == "rate_limited"
        assert exc.value.retryable is True
        assert "too many requests" in str(exc.value)

    def test_insufficient_balance_is_explained(self, fake_http):
        fake_http.response = _FakeResponse(
            {"status": 402, "message": "insufficient balance"}, status_code=402
        )
        with pytest.raises(EngineError) as exc:
            asyncio.run(fish_client.synthesize(_cfg(), {"text": "你好"}, label="Fish Audio"))
        assert "余额不足" in str(exc.value)
        assert "insufficient balance" in str(exc.value)

    def test_suspiciously_short_audio_is_refused(self, fake_http):
        fake_http.response = _FakeResponse(content=b"\x00" * 8)
        with pytest.raises(EngineError, match="合理的最小长度"):
            asyncio.run(fish_client.synthesize(_cfg(), {"text": "你好"}, label="Fish Audio"))

    def test_voice_listing_asks_for_own_models(self, fake_http):
        fake_http.response = _FakeResponse(
            {"total": 1, "items": [{"_id": "v1", "title": "音色一", "type": "tts", "state": "trained"}]},
            content_type="application/json",
        )
        result = asyncio.run(fish_client.fetch_voices(_cfg(), label="Fish Audio"))

        call = fake_http.calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "/model"
        assert call["params"]["self"] == "true"
        assert [v.id for v in result.items] == ["v1"]
        assert result.why == ""

    def test_public_scope_does_not_send_self(self, fake_http):
        fake_http.response = _FakeResponse({"items": []}, content_type="application/json")
        asyncio.run(fish_client.fetch_voices(_cfg(voice_scope="public"), label="Fish Audio"))
        assert "self" not in fake_http.calls[0]["params"]

    def test_platform_refusal_becomes_the_reason_not_an_exception(self, fake_http):
        fake_http.response = _FakeResponse(
            {"status": 401, "message": "invalid token"}, status_code=401, content_type="application/json"
        )
        result = asyncio.run(fish_client.fetch_voices(_cfg(), label="Fish Audio"))
        assert result.items == ()
        assert "invalid token" in result.why

    def test_empty_listing_explains_how_to_get_a_voice(self, fake_http):
        fake_http.response = _FakeResponse({"items": []}, content_type="application/json")
        result = asyncio.run(fish_client.fetch_voices(_cfg(), label="Fish Audio"))
        assert result.items == ()
        assert "克隆" in result.why

    def test_clone_uploads_the_sample_as_multipart(self, fake_http, tmp_path):
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 64)
        fake_http.response = _FakeResponse({"_id": "new-id", "state": "trained"}, status_code=201)

        meta = asyncio.run(
            FishAudioEngine().clone_voice(
                _cfg(),
                CloneRequest(name="旁白", audio_path=str(sample), transcript="这是一段话"),
            )
        )

        call = fake_http.calls[0]
        assert call["path"] == "/model"
        fields = dict(call["data"])
        assert fields["type"] == "tts"
        assert fields["train_mode"] == "fast"
        assert fields["visibility"] == "private"
        assert fields["texts"] == "这是一段话"
        assert "voices" in call["files"]
        assert meta.id == "new-id"
        assert meta.name == "旁白"

    def test_clone_without_audio_is_refused_before_uploading(self, fake_http):
        with pytest.raises(EngineError, match="本地音源音频"):
            asyncio.run(FishAudioEngine().clone_voice(_cfg(), CloneRequest(name="空")))
        assert fake_http.calls == []

    def test_a_requested_voice_id_is_refused_not_ignored(self, fake_http, tmp_path):
        """平台的 id 由它自己生成；答应了却不去用，用户会在平台后台按这个名字白找一遍。"""
        sample = tmp_path / "s.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 64)
        with pytest.raises(EngineError, match="由平台生成"):
            asyncio.run(
                FishAudioEngine().clone_voice(
                    _cfg(),
                    CloneRequest(name="旁白", voice_id="my_voice", audio_path=str(sample)),
                )
            )

    def test_a_failed_training_is_reported(self, fake_http, tmp_path):
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 64)
        fake_http.response = _FakeResponse(
            {"_id": "x", "state": "failed", "quality": {"quality_reason": "音频过短"}},
            status_code=201,
        )
        with pytest.raises(EngineError, match="音频过短"):
            asyncio.run(
                FishAudioEngine().clone_voice(
                    _cfg(), CloneRequest(name="旁白", audio_path=str(sample))
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
        result = asyncio.run(fish_client.fetch_voices(_cfg(api_key=""), label="Fish Audio"))
        assert result.items == ()
        assert "API Key" in result.why

    def test_synthesis_says_what_is_missing(self, no_network):
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(fish_client.synthesize(_cfg(api_key=""), {"text": "你好"}, label="Fish Audio"))

    def test_clone_says_what_is_missing(self, no_network, tmp_path):
        sample = tmp_path / "s.wav"
        sample.write_bytes(b"RIFF")
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(
                FishAudioEngine().clone_voice(
                    _cfg(api_key=""), CloneRequest(name="x", audio_path=str(sample))
                )
            )


# --------------------------------------------------------------------------- #
# 自描述
# --------------------------------------------------------------------------- #


class TestDescribe:
    def test_declares_itself_a_cloud_service(self):
        caps = FishAudioEngine().capabilities()
        assert caps.kind == KIND_CLOUD
        assert caps.voice_listing is True
        assert caps.voice_cloning is True
        assert caps.reference_audio is False
        assert caps.max_chars == 0  # 平台没有按字数的硬上限

    def test_synthesis_spec_asks_only_for_a_voice_id(self):
        spec = FishAudioEngine().synthesis_spec()
        assert spec.requires == ("voice_id",)
        assert spec.formats == ("mp3", "wav", "pcm", "opus")

    def test_describe_carries_everything_the_ui_needs(self):
        data = FishAudioEngine().describe()
        assert data["key"] == "fish_audio"
        assert data["kind_label"] == "云端"
        assert data["secret_fields"] == ["api_key"]
        assert data["settings_schema"]["properties"]["api_key"]["widget"] == "password"
        assert data["mapping_constants"] == {}  # 模型走请求头，没有常量
        assert all(row["label"] for row in data["mapping"])
        assert any("请求头" in line for line in data["mapping_cautions"])

    def test_the_emotion_row_points_at_the_text(self):
        rows = {row["source"]: row for row in FishAudioEngine().describe()["mapping"]}
        assert rows["emotion"]["target"] == "text"
        assert rows["instruction"]["target"] == "text"

    def test_the_panel_does_not_claim_two_independent_expression_channels(self):
        """两者落在同一个地方，就不触发「只发一路」那条约束。"""
        assert FishAudioEngine().dual_expression() is False

    def test_presets_are_loaded_from_the_plugin_directory(self):
        presets = FishAudioEngine().field_presets()["instruction"]
        assert presets and all(label and text for label, text in presets)

    def test_the_clone_page_states_the_sample_requirement(self):
        sections = {section.key: section for section in FishAudioEngine().ui_sections()}
        assert "10 秒" in sections["clone"].note

    def test_icon_comes_from_the_plugin_directory(self):
        svg = FishAudioEngine().icon_svg()
        assert svg.lstrip().startswith("<svg")
        assert "<path" in svg or "<rect" in svg
