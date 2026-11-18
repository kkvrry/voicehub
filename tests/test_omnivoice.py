"""OmniVoice 插件：**照规格核过的那些点**，每条钉一个断言。

与 ``test_cosyvoice3.py`` 同一套写法：不碰网络，假 httpx 客户端既让测试离线跑，
又能断言"到底发了什么"。

这一家最容易写错的地方：

1. **两条路靠输入分开**：只给正文 / 给参考音频，没有 ``template_name`` 这类开关；
2. **克隆必须带逐字稿**：给了参考音频而 ``reference_text`` 为空，audio.cpp 直接
   回 500；由 ``synthesis_spec().needs_transcript`` 声明，核心在发送前补；
3. **不对接 ``options.instruct``**：那是封闭属性词表，自由「引导指令」与自由
   「音色描述」都接不进来（服务端 500），两样都如实报"未发送"，音色只由参考音频决定；
4. **种子省略是"沿用会话随机状态"**，所以 ``-1`` 不是抽一个，而是**不发这一格**；
5. 语气词是正文里的方括号标签（``[laughter]`` / ``[sigh]``），由 ``mark_map`` 翻译。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import ClassVar, Self

import httpx
import pytest

from app.engines.base import KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.omnivoice.plugin import MARKS, OmniVoiceEngine
from app.engines.builtin.omnivoice.settings import OmniVoiceSettings
from app.engines.registry import list_engines


def _cfg(**over: object) -> OmniVoiceSettings:
    data: dict[str, object] = {"base_url": "http://127.0.0.1:18093"}
    data.update(over)
    return OmniVoiceSettings(**data)


def _wav(size: int = 4096) -> bytes:
    head = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WAVE"
    return head + b"\x00" * (size - len(head))


_REF = ""


@pytest.fixture(autouse=True)
def _real_reference_audio(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """真写一个小 WAV，文件名带中文：这一家收本机路径，只校验读得到。"""
    path = tmp_path / "某书" / "某人 默认.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav(512))
    monkeypatch.setitem(globals(), "_REF", str(path))
    return str(path)


def _req(**over: object) -> SynthesisRequest:
    data: dict[str, object] = {
        "text": "他停下脚步，回头看了她一眼。",
        "voice_id": "叶奈法",
        "reference_audio": _REF,
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> OmniVoiceEngine:
    return OmniVoiceEngine()


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
# 发现与配置
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_registry_discovers_it_by_directory(self):
        engines = list_engines()
        assert "omnivoice" in engines
        assert isinstance(engines["omnivoice"], OmniVoiceEngine)
        assert engines["omnivoice"].label == "OmniVoice"


class TestSettings:
    def test_every_field_declares_a_label(self):
        for name, field in OmniVoiceSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_the_defaults_follow_the_engine_spec(self):
        cfg = _cfg()
        assert cfg.model == "omnivoice"
        assert cfg.guidance_scale == 2.0  # 官方默认
        assert cfg.num_inference_steps == 32
        assert cfg.seed == -1
        assert cfg.text_chunk_size == 0  # 0 = 按预估时长自动分块

    def test_host_fills_in_a_missing_scheme(self):
        assert _cfg(base_url="127.0.0.1:18093").host == "http://127.0.0.1:18093"
        assert _cfg(base_url="").host == "http://127.0.0.1:18093"


# --------------------------------------------------------------------------- #
# 能力
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        assert _engine().capabilities().kind == KIND_LOCAL

    def test_no_voice_list_and_no_id_cloning(self):
        caps = _engine().capabilities()
        assert caps.voice_listing is False
        assert caps.voice_cloning is False

    def test_claims_reference_audio_but_not_design(self):
        caps = _engine().capabilities()
        # instruct 是封闭属性词表，收不下自由音色描述 → 不声明设计能力。
        assert caps.voice_design is False
        assert caps.reference_audio is True

    def test_does_not_claim_emotion_control(self):
        """没有情绪枚举，也不收自由引导指令。"""
        assert _engine().capabilities().emotion_control is False

    def test_language_surface(self):
        caps = _engine().capabilities()
        assert "zh" in caps.languages and "en" in caps.languages
        assert len(caps.languages) == 17
        # 官方没有单列"一把嗓子读另一种语言"，跨语言这一格留空。
        assert caps.cross_lingual is None
        # 粤语按本项目的口径算方言：语言行不再重复它。
        assert caps.dialects == ("粤语",)

    def test_single_concurrency(self):
        assert _engine().capabilities().max_concurrency == 1

    def test_reference_is_optional_but_clone_needs_transcript(self):
        """只给正文也能合成；一旦给参考音频克隆，逐字稿是硬要求。"""
        spec = _engine().synthesis_spec()
        assert spec.requires == ()
        assert "voice_design" not in _engine().accepted_extra_keys()
        assert spec.needs_transcript is True


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


class TestBody:
    def test_a_bare_text_needs_no_reference(self):
        body = _engine().build_body(_cfg(), _req(reference_audio=""))
        assert body["input"]
        assert "voice_ref" not in body
        assert "instruct" not in body.get("options", {})

    def test_a_reference_goes_to_voice_ref(self):
        body = _engine().build_body(_cfg(), _req())
        assert body["voice_ref"] == _REF

    def test_a_transcript_goes_to_reference_text(self):
        body = _engine().build_body(_cfg(), _req(reference_text="他停下脚步。"))
        assert body["reference_text"] == "他停下脚步。"

    def test_an_unreadable_reference_is_refused(self, tmp_path: pathlib.Path):
        with pytest.raises(EngineError, match="读不到参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio=str(tmp_path / "没了.wav")))

    def test_a_free_instruction_is_not_sent(self):
        """instruct 是设计音色的属性词表，自由引导指令塞进去会被服务端拒（500）。"""
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"instruction": "轻声、放慢"})
        )
        assert "instruct" not in body.get("options", {})
        # 参考音频照旧生效——引导指令不该顶掉它。
        assert body["voice_ref"] == _REF
        joined = "".join(notes)
        assert "未发送" in joined and "引导指令" in joined

    def test_a_free_design_is_not_sent(self):
        """自由音色描述也喂不进 instruct（封闭词表会 500）：不发送，参考音频照旧。"""
        body, notes = _engine().build_body_with_notes(
            _cfg(),
            _req(extra={"voice_design": "年轻女声，清亮"}),
        )
        assert "instruct" not in body.get("options", {})
        assert body["voice_ref"] == _REF
        joined = "".join(notes)
        assert "未发送" in joined and "音色描述" in joined

    def test_language_and_speed_land_in_options(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "ja", "speed": 1.2}))
        assert body["options"]["language"] == "ja"
        assert body["options"]["speed"] == 1.2

    def test_a_language_label_is_normalized_to_its_code(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "粤语"}))
        assert body["options"]["language"] == "yue"

    def test_cantonese_with_reference_audio_states_the_reference_limit(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"language": "yue"})
        )
        said = " ".join(notes)
        assert "粤语" in said and "参考" in said

    def test_cantonese_without_reference_audio_does_not_warn(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(reference_audio="", extra={"language": "yue"})
        )
        assert "粤语" not in " ".join(notes)

    def test_the_decoding_defaults_are_sent(self):
        body = _engine().build_body(_cfg(guidance_scale=2.5, num_inference_steps=16), _req())
        assert body["options"]["guidance_scale"] == 2.5
        assert body["options"]["num_inference_steps"] == 16

    def test_text_chunk_size_is_omitted_when_zero(self):
        assert "text_chunk_size" not in _engine().build_body(_cfg(), _req())["options"]
        assert (
            _engine().build_body(_cfg(text_chunk_size=160), _req())["options"]["text_chunk_size"]
            == 160
        )

    def test_random_seed_sends_nothing(self):
        """省略种子 = 沿用会话随机状态，所以 -1 不发这一格。"""
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": -1}))
        assert "seed" not in body
        assert not any(note.startswith("seed=") for note in notes)

    def test_a_fixed_seed_is_sent(self):
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": 7}))
        assert body["seed"] == 7
        assert "seed=7" in notes

    def test_unsupported_values_are_reported(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(emotion="难过", extra={"guidance_scale": 0.5})
        )
        joined = "".join(notes)
        assert "未发送" in joined
        assert "情绪" in joined and "指令强度" in joined


class TestMarks:
    def test_the_two_shared_tags_are_registered(self):
        assert MARKS == {"笑": "[laughter]", "叹气": "[sigh]"}

    def test_unified_tags_reach_the_engine_in_its_native_spelling(self):
        """登记了就得真发得出去：正文过 ``prepare_text`` 之后不留 ``[[``。

        从前这一条直接对 ``render_marks`` 断言——测的是那一层函数，插件没有调它，
        于是登记了标签、正文却原样发出去（``[[笑]]`` 被念成"左括号笑右括号"）。
        """
        engine = _engine()
        body = engine.build_body(
            _cfg(), _req(text=engine.prepare_text("他笑了[[笑]]，又[[叹气]]。"))
        )
        assert body["input"] == "他笑了[laughter]，又[sigh]。"

    def test_a_tag_it_does_not_know_is_deleted(self):
        engine = _engine()
        body = engine.build_body(_cfg(), _req(text=engine.prepare_text("[[倒吸气]]他退了一步。")))
        assert body["input"] == "他退了一步。"


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #


class TestClient:
    def test_it_posts_to_the_speech_route(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav(), content_type="audio/wav")
        result = asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert result.format == "wav"
        assert result.audio[:4] == b"RIFF"
        assert fake_http.calls[0]["path"] == "/v1/audio/speech"
        assert fake_http.calls[0]["json"]["model"] == "omnivoice"

    def test_a_busy_server_is_retryable(self, fake_http):
        fake_http.response = _FakeResponse(
            status_code=503, content_type="application/json", json_body={"error": "busy"}
        )
        with pytest.raises(EngineError) as caught:
            asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert caught.value.retryable is True

    def test_a_server_error_is_not_retryable(self, fake_http):
        fake_http.response = _FakeResponse(
            status_code=500, content_type="application/json", json_body={"error": "no weights"}
        )
        with pytest.raises(EngineError) as caught:
            asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert caught.value.retryable is False
        assert "no weights" in caught.value.reason

    def test_an_unknown_model_says_where_to_look(self, fake_http):
        fake_http.response = _FakeResponse(
            status_code=400,
            content_type="application/json",
            json_body={"error": {"message": "unknown model id: omnivoice"}},
        )
        with pytest.raises(EngineError, match="GET /v1/models"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_non_audio_response_is_refused(self, fake_http):
        fake_http.response = _FakeResponse(content=b"<html>", content_type="text/html")
        with pytest.raises(EngineError, match="不是音频"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_connect_error_names_the_service(self, fake_http):
        fake_http.raise_error = httpx.ConnectError("refused")
        with pytest.raises(EngineError, match=r"OmniVoice（.*）无法连接"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))
