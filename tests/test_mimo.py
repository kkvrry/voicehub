"""MiMo 插件：**照官方文档核过的那些点**，每一条都钉一个断言。

这个文件里**没有一次真实网络请求**——假 httpx 客户端既让测试离线跑，又能断言
"到底发了什么"（路径、方法、body、请求头）。这一家最特别的两处（台词在 assistant
消息里、``audio.voice`` 两种含义）都是"请求照样 200、只是东西不对"的形状，
不把形状钉住是抓不到的。
"""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from pathlib import Path
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.marks import MARKS
from app.engines.base import KIND_CLOUD, EngineError, SynthesisRequest
from app.engines.builtin.mimo import client
from app.engines.builtin.mimo.mapping import MARK_MAP, MODEL_CLONE, MODEL_DESIGN, MODEL_PRESET
from app.engines.builtin.mimo.plugin import MimoEngine
from app.engines.builtin.mimo.settings import MimoSettings
from app.engines.builtin.mimo.voices import PRESET_VOICES, VOICE_IDS
from app.engines.registry import list_engines


def _cfg(**over: object) -> MimoSettings:
    data: dict[str, object] = {"api_key": "sk-test"}
    data.update(over)
    return MimoSettings(**data)


def _req(text: str = "你好。", **kw: object) -> SynthesisRequest:
    return SynthesisRequest(text=text, **kw)  # type: ignore[arg-type]


def _sent(text: str) -> str:
    """正文过网关那一道：语气词标签在发出前换成这一家的写法（``prepare_text``）。"""
    return MimoEngine().prepare_text(text)


# --------------------------------------------------------------------------- #
# 假 httpx：记录"发了什么"，离线断言
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload, ensure_ascii=False)

    def json(self) -> object:
        return self._payload


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：把每次 ``post`` 的参数记在 ``calls`` 上。

    ``payload`` / ``status_code`` 是类属性——用例各设各的，互不影响。
    """

    calls: ClassVar[list[dict[str, object]]] = []
    payload: object = {}
    status_code: int = 200
    text: str = ""

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"path": path, "init": self.init_kwargs, **kwargs})
        return _FakeResponse(
            _FakeAsyncClient.payload, _FakeAsyncClient.status_code, _FakeAsyncClient.text
        )


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    """把 httpx.AsyncClient 换成替身（client 里是函数内 import，所以打真模块就行）。"""
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.payload = {}
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.text = ""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _audio_payload(size: int = 4000, *, finish_reason: str = "stop") -> dict[str, object]:
    """一份成功的合成响应（音频是 base64 串，与官方形状一致）。"""
    return {
        "id": "resp-1",
        "choices": [
            {
                "finish_reason": finish_reason,
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "audio": {
                        "id": "aud-1",
                        "data": base64.b64encode(b"\x11\x22" * (size // 2)).decode(),
                        "expires_at": None,
                        "transcript": None,
                    },
                },
            }
        ],
        "object": "chat.completion",
    }


def _wav_bytes(seconds: float = 0.2) -> bytes:
    import io
    import struct

    rate = 24000
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", 1000) for _ in range(frames)))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 配置与注册
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_api_key_is_the_only_secret(self):
        engine = MimoEngine()
        assert engine.secret_fields() == frozenset({"api_key"})

    def test_base_url_defaults_to_the_official_endpoint(self):
        cfg = _cfg()
        assert cfg.host == "https://api.xiaomimimo.com"
        assert cfg.configured is True

    def test_custom_base_url_wins_and_gets_a_scheme(self):
        assert _cfg(base_url="http://127.0.0.1:9").host == "http://127.0.0.1:9"
        assert _cfg(base_url="proxy.local/mimo").host == "https://proxy.local/mimo"

    def test_every_preset_voice_is_a_choice(self):
        choices = MimoSettings.model_json_schema()["properties"]["voice"]["choices"]
        assert [value for value, _ in choices] == list(VOICE_IDS)

    def test_readiness_names_what_is_missing(self):
        engine = MimoEngine()
        assert "API Key" in engine.readiness(MimoSettings())
        assert engine.readiness(_cfg()) == ""


class TestRegistry:
    def test_mimo_is_discovered_by_directory(self):
        assert "mimo" in list_engines()

    def test_it_is_a_cloud_engine(self):
        assert MimoEngine().capabilities().kind == KIND_CLOUD


# --------------------------------------------------------------------------- #
# 能力与契约
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_the_nine_presets_are_the_list(self):
        caps = MimoEngine().capabilities()
        assert caps.voice_listing is True

    def test_clone_is_sample_based(self):
        """平台没有音色 id：克隆结果 = 一段样本，以后每次合成带着它走 voiceclone。"""
        caps = MimoEngine().capabilities()
        assert caps.voice_cloning is False  # 平台没有音色 id，不能算“注册式克隆”
        assert caps.sample_cloning is True

    def test_clone_note_does_not_claim_a_registered_voice(self):
        """能力行只说“样本复刻”；页面上不能再写“注册为可复用的音色”。"""
        notes = MimoEngine().describe()["capability_notes"]
        cloning = [note for note in notes if note["key"] == "voice_cloning"]
        assert len(cloning) == 1
        assert "不保存音色 id" in cloning[0]["note"]

    def test_clone_tab_declares_its_model(self):
        """模型由插件声明，前端只渲染——Vue 里不写死任何模型 id。"""
        section = next(s for s in MimoEngine().ui_sections() if s.key == "clone")
        assert section.meta == {"model": "mimo-v2.5-tts-voiceclone"}

    def test_voice_design_is_declared(self):
        assert MimoEngine().capabilities().voice_design is True

    def test_design_tab_declares_its_model(self):
        section = next(s for s in MimoEngine().ui_sections() if s.key == "design")
        assert section.meta == {"model": MODEL_DESIGN}

    def test_spec_accepts_the_design_prompt(self):
        assert "voice_design" in MimoEngine().accepted_extra_keys()

    def test_reference_audio_is_the_second_path(self):
        assert MimoEngine().capabilities().reference_audio is True

    def test_emotion_is_a_style_tag(self):
        """情绪翻成这一家的「整体风格」词，挂到正文最前面。"""
        assert MimoEngine().capabilities().emotion_control is True

    def test_the_context_window_is_the_text_limit(self):
        """官方「模型规格」写 8K tokens 上下文且**不自己分块**，所以要有上限。

        字符数是折算的（中文 1 字 ≈ 1–1.5 token，还要给 user 那条风格指令留位置），
        所以钉的是"有这一格、且是保守值"，不是某个官方字符数——官方没给字符数。
        """
        limit = MimoEngine().capabilities().max_chars
        assert 0 < limit <= 8_000

    def test_spec_declares_two_paths_not_one(self):
        """**不写 ``requires``**：预置音色 id 与参考音频二选一，静态元组表达不了。"""
        assert MimoEngine().synthesis_spec().requires == ()

    def test_formats_are_the_four_the_platform_takes(self):
        assert MimoEngine().synthesis_spec().formats == ("wav", "mp3", "pcm", "pcm16")

    def test_reference_formats_and_no_transcript(self):
        spec = MimoEngine().synthesis_spec()
        assert spec.reference_formats == ("mp3", "wav")
        assert spec.needs_transcript is False

    def test_channel_defaults_to_own_but_library_is_allowed(self, data_dir):
        from app.usecase.engines import channel_of, require_engine

        channel = channel_of(require_engine("mimo"))
        assert channel.mode == "own"
        # 复刻那一路吃参考音频 → 库桥也允许配（本地音色经参考音频供给）。
        from app.domain.channel import allowed_modes

        assert set(allowed_modes(voice_listing=True, reference_audio=True)) == {"own", "library"}


# --------------------------------------------------------------------------- #
# 音色清单
# --------------------------------------------------------------------------- #


class TestVoiceList:
    def test_the_nine_presets_come_straight_from_the_docs(self):
        assert [v.id for v in PRESET_VOICES] == [
            "mimo_default",
            "冰糖",
            "茉莉",
            "苏打",
            "白桦",
            "Mia",
            "Chloe",
            "Milo",
            "Dean",
        ]

    def test_language_and_gender_are_the_documented_ones(self):
        by_id = {v.id: v for v in PRESET_VOICES}
        assert (by_id["茉莉"].language, by_id["茉莉"].gender) == ("zh", "female")
        assert (by_id["白桦"].language, by_id["白桦"].gender) == ("zh", "male")
        assert (by_id["Mia"].language, by_id["Mia"].gender) == ("en", "female")
        assert (by_id["Dean"].language, by_id["Dean"].gender) == ("en", "male")
        # 默认音色的语言随集群而变（官方原话）——留空不猜。
        assert by_id["mimo_default"].language == ""
        assert by_id["mimo_default"].gender == ""

    def test_words_in_the_descriptions_become_tags_and_age(self):
        """说明里那句「英文女声：偏活泼」里的词要落成能筛的字段。

        以前只抄了语言与性别：特点与年龄感留在简介那段散文里，卡片上没有、
        标签云里也筛不到——看着就像"识别没跑"。
        """
        by_id = {v.id: v for v in PRESET_VOICES}
        assert by_id["Chloe"].tags == ("活泼",)
        assert by_id["Dean"].tags == ("成熟",)
        assert by_id["白桦"].tags == ("稳重",)
        assert by_id["茉莉"].tags == ("清亮",)
        assert by_id["苏打"].age == "young"
        assert by_id["Milo"].age == "young"
        # 说明里没有这类词的，两样都留空（不猜）。
        assert by_id["Mia"].tags == ()
        assert by_id["冰糖"].age == ""

    def test_list_voices_needs_no_key_and_no_network(self):
        """官方没有列表接口：这 9 个是静态事实，密钥没配时下拉里也该有得挑。"""
        result = asyncio.run(MimoEngine().list_voices(MimoSettings()))
        assert [v.id for v in result.items] == list(VOICE_IDS)
        assert "限时免费" in result.cost_note

    def test_refresh_imports_them_into_the_library(self, data_dir, service):
        from app.usecase.engines import refresh_voices

        result = asyncio.run(refresh_voices(service, "mimo"))
        assert result["ok"] is True
        assert result["added"] == 9
        assert len(service.list_voices(source="mimo")) == 9
        # 入库那一刻就把说明里的词带进库：标签云与年龄筛选当场可用。
        by_name = {v.name: v for v in service.list_voices(source="mimo")}
        assert by_name["Chloe"].tags == ("活泼",)
        assert by_name["白桦"].tags == ("稳重",)
        assert by_name["苏打"].age == "young"


# --------------------------------------------------------------------------- #
# 请求装配
# --------------------------------------------------------------------------- #


class TestBuildBody:
    def test_the_text_lands_in_the_assistant_message(self):
        """官方规定：目标文本在 ``role=assistant`` 那条，**位置不能换**。"""
        body = MimoEngine().build_body(_cfg(), _req("要念的话。", voice_id="茉莉"))
        assert body["messages"] == [{"role": "assistant", "content": "要念的话。"}]

    def test_the_instruction_lands_in_the_user_message_before_it(self):
        """user 那条是风格指令，**不进语音**——放在 assistant 里会被念出来。"""
        body = MimoEngine().build_body(
            _cfg(), _req("要念的话。", voice_id="茉莉", extra={"instruction": "语速稍慢。"})
        )
        assert body["messages"] == [
            {"role": "user", "content": "语速稍慢。"},
            {"role": "assistant", "content": "要念的话。"},
        ]

    def test_no_instruction_means_no_user_message(self):
        body = MimoEngine().build_body(_cfg(), _req("要念的话。", voice_id="茉莉"))
        assert [m["role"] for m in body["messages"]] == ["assistant"]

    def test_voice_id_lands_in_audio_voice(self):
        body = MimoEngine().build_body(_cfg(), _req("话。", voice_id="苏打"))
        assert body["audio"]["voice"] == "苏打"

    def test_configured_voice_is_the_fallback(self):
        body = MimoEngine().build_body(_cfg(voice="白桦"), _req("话。", voice_id=""))
        assert body["audio"]["voice"] == "白桦"

    def test_no_voice_at_all_is_refused_before_sending(self):
        with pytest.raises(EngineError, match="预置音色"):
            MimoEngine().build_body(_cfg(voice=""), _req("话。", voice_id=""))

    def test_an_unknown_voice_is_sent_as_is(self):
        """不在那 9 个里也照发——平台会自己拒，而它的原话比我们猜的准。"""
        body = MimoEngine().build_body(_cfg(), _req("话。", voice_id="平台以后加的新音色"))
        assert body["audio"]["voice"] == "平台以后加的新音色"

    def test_reference_audio_switches_to_the_clone_model(self, tmp_path):
        """带参考音频 → 复刻模型；模型由**请求自己的形状**决定，没有配置栏可配。"""
        sample = tmp_path / "ref.wav"
        sample.write_bytes(_wav_bytes())
        body, notes = MimoEngine().build_body_with_notes(
            _cfg(), _req("话。", voice_id="茉莉", reference_audio=str(sample))
        )
        assert body["model"] == MODEL_CLONE
        assert body["audio"]["voice"].startswith("data:audio/wav;base64,")
        # 只钉"说清了走的是复刻这条路"，不钉具体措辞：那句话是给人看的交代，
        # 改文案不该连带改测试。
        assert any("复刻" in note for note in notes)

    def test_preset_model_when_no_reference_audio(self):
        body, notes = MimoEngine().build_body_with_notes(_cfg(), _req("话。", voice_id="茉莉"))
        assert body["model"] == MODEL_PRESET
        assert notes == ()

    def test_a_design_prompt_switches_to_the_design_model(self):
        """描述在 user 消息、台词在 assistant 消息；audio.voice 整个不传。"""
        body, notes = MimoEngine().build_body_with_notes(
            _cfg(),
            _req("话。", voice_id="茉莉", extra={"voice_design": "低沉的老者，语速缓慢。"}),
        )
        assert body["model"] == MODEL_DESIGN
        assert "voice" not in body["audio"]
        assert body["messages"] == [
            {"role": "user", "content": "低沉的老者，语速缓慢。"},
            {"role": "assistant", "content": "话。"},
        ]
        assert any("重新设计" in note for note in notes)

    def test_design_merges_the_instruction_into_the_user_message(self):
        body, _ = MimoEngine().build_body_with_notes(
            _cfg(),
            _req(
                "话。",
                voice_id="",
                extra={"voice_design": "低沉的老者。", "instruction": "气声多一点。"},
            ),
        )
        assert body["messages"][0]["content"] == "低沉的老者。\n气声多一点。"

    def test_design_takes_priority_over_a_reference_audio(self, tmp_path):
        """设计音色后来挂了样例也不能被引擎当成参考音频走克隆模型。"""
        sample = tmp_path / "ref.wav"
        sample.write_bytes(_wav_bytes())
        body, notes = MimoEngine().build_body_with_notes(
            _cfg(),
            _req(
                "话。",
                voice_id="",
                reference_audio=str(sample),
                extra={"voice_design": "低沉的老者。"},
            ),
        )
        assert body["model"] == MODEL_DESIGN
        assert "voice" not in body["audio"]
        assert any("参考音频" in note for note in notes)

    def test_an_empty_design_prompt_leaves_the_preset_path_alone(self):
        body, _ = MimoEngine().build_body_with_notes(
            _cfg(), _req("话。", voice_id="茉莉", extra={"voice_design": "  "})
        )
        assert body["model"] == MODEL_PRESET
        assert body["audio"]["voice"] == "茉莉"

    def test_the_clone_path_still_takes_an_instruction(self, tmp_path):
        sample = tmp_path / "ref.wav"
        sample.write_bytes(_wav_bytes())
        body, _ = MimoEngine().build_body_with_notes(
            _cfg(),
            _req(
                "话。",
                voice_id="",
                reference_audio=str(sample),
                extra={"instruction": "气声多一点。"},
            ),
        )
        assert body["messages"][0] == {"role": "user", "content": "气声多一点。"}

    def test_format_lands_in_audio_format(self):
        body = MimoEngine().build_body(_cfg(), _req("话。", voice_id="茉莉", format="mp3"))
        assert body["audio"]["format"] == "mp3"

    def test_no_format_means_no_format_key(self):
        """省略才是"没要求"——平台自己默认 wav，不替它发明一个。"""
        body = MimoEngine().build_body(_cfg(), _req("话。", voice_id="茉莉"))
        assert "format" not in body["audio"]

    def test_stream_is_explicitly_false(self):
        body = MimoEngine().build_body(_cfg(), _req("话。", voice_id="茉莉"))
        assert body["stream"] is False

    def test_marks_are_translated_into_the_platform_notation(self):
        body = MimoEngine().build_body(
            _cfg(), _req(_sent("[[笑]]他说，[[叹气]]算了。"), voice_id="茉莉")
        )
        assert body["messages"][0]["content"] == "(笑)他说，(叹气)算了。"

    def test_a_mark_with_a_parameter_loses_only_the_parameter(self):
        """这一家的标签不带时长——``[[叹气:0.9]]`` 翻成 ``(叹气)``，0.9 没有落点。"""
        body = MimoEngine().build_body(_cfg(), _req(_sent("[[叹气:0.9]]算了。"), voice_id="茉莉"))
        assert body["messages"][0]["content"] == "(叹气)算了。"

    def test_an_unknown_mark_is_passed_through(self):
        """这一家支持自定义标签：表里没有的原样转成 ``(名字)``，不删。"""
        body = MimoEngine().build_body(
            _cfg(), _req(_sent("[[停顿:0.4]]他说，[[打嗝]]。"), voice_id="茉莉")
        )
        assert body["messages"][0]["content"] == "(停顿)他说，(打嗝)。"

    def test_accepts_any_tag_is_declared_not_only_implemented(self):
        """「认任意标签」是**声明**出来的：装配、跨引擎分组与界面都读这一处。

        只写在翻译那一步的话，界面会把这家当成"只认六条"——通用项被少算、
        自定义标签也打不了。
        """
        engine = MimoEngine()
        assert engine.mark_passthrough() == "({name})"
        assert engine.describe()["marks_passthrough"] == "({name})"

    def test_emotion_lands_as_a_leading_style_tag(self):
        body = MimoEngine().build_body(
            _cfg(), _req("话。", voice_id="茉莉", emotion="开心")
        )
        assert body["messages"][0]["content"] == "(开心)话。"

    def test_emotion_is_translated_and_merged_with_an_existing_style(self):
        """难过→悲伤；开头已有风格标签时并进同一对括号，不再开一对。"""
        body = MimoEngine().build_body(
            _cfg(), _req("(温柔)话。", voice_id="茉莉", emotion="难过")
        )
        assert body["messages"][0]["content"] == "(悲伤，温柔)话。"

    def test_a_bracket_style_is_merged_too(self):
        """官方三种括号都认（半角 / 全角圆括号、方括号），合并时不能只认圆括号。"""
        body = MimoEngine().build_body(
            _cfg(), _req("[温柔]话。", voice_id="茉莉", emotion="难过")
        )
        assert body["messages"][0]["content"] == "(悲伤，温柔)话。"

    def test_a_full_width_style_is_merged_too(self):
        body = MimoEngine().build_body(
            _cfg(), _req("（温柔）话。", voice_id="茉莉", emotion="难过")
        )
        assert body["messages"][0]["content"] == "(悲伤，温柔)话。"

    def test_empty_text_is_refused(self):
        with pytest.raises(EngineError, match="正文不能为空"):
            MimoEngine().build_body(_cfg(), _req("   ", voice_id="茉莉"))

    def test_empty_text_with_an_emotion_is_still_refused(self):
        """情绪会拼成 ``(开心)``——先拼再判空的话，这就成了一段没有台词的请求。"""
        with pytest.raises(EngineError, match="正文不能为空"):
            MimoEngine().build_body(_cfg(), _req("   ", voice_id="茉莉", emotion="开心"))

    def test_params_without_a_landing_are_handed_back(self):
        """只剔不说是最坏的：声音照出，用户却以为自己要的语速生效了。"""
        _, notes = MimoEngine().build_body_with_notes(
            _cfg(),
            _req("话。", voice_id="茉莉", extra={"emotion": "开心", "speed": 1.5}),
        )
        assert len(notes) == 1
        assert "语速" in notes[0] and "情绪" not in notes[0]

    def test_nothing_dropped_means_nothing_said(self):
        _, notes = MimoEngine().build_body_with_notes(
            _cfg(), _req("话。", voice_id="茉莉", extra={"guidance_scale": ""})
        )
        assert notes == ()

    def test_every_registered_mark_is_one_the_vocabulary_has(self):
        """登记之前先翻成统一名——照抄原生写法会让「通用」永远算不出来。"""
        assert set(MARK_MAP) <= {m.name for m in MARKS}


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #


class TestClient:
    def test_it_posts_to_the_official_path_with_both_auth_headers(self, fake_http):
        fake_http.payload = _audio_payload()
        asyncio.run(client.synthesize(_cfg(api_key="sk-secret"), {"messages": []}, label="MiMo"))
        call = fake_http.calls[-1]
        assert call["path"] == "/v1/chat/completions"
        assert call["init"]["base_url"] == "https://api.xiaomimimo.com"
        # 官方示例发 Bearer、文档页发 api-key——两处不一致，两份一起发。
        assert call["init"]["headers"] == {
            "Authorization": "Bearer sk-secret",
            "api-key": "sk-secret",
        }

    def test_the_audio_comes_back_from_choices_message_audio_data(self, fake_http):
        fake_http.payload = _audio_payload(size=6000)
        result = asyncio.run(client.synthesize(_cfg(), {"audio": {"format": "wav"}}, label="MiMo"))
        assert len(result.audio) == 6000
        assert result.format == "wav"

    def test_a_request_without_a_key_never_goes_out(self, fake_http):
        with pytest.raises(EngineError, match="API Key"):
            asyncio.run(client.synthesize(MimoSettings(), {"messages": []}, label="MiMo"))
        assert fake_http.calls == []

    def test_an_error_hidden_in_a_200_is_reported(self, fake_http):
        """这一家的拒绝不在状态码里——先拆信封再挖音频，顺序反了只能得到
        一句"没返回音频"。"""
        fake_http.payload = {"error": {"message": "quota exceeded", "type": "quota"}}
        with pytest.raises(EngineError, match="quota exceeded"):
            asyncio.run(client.synthesize(_cfg(), {"messages": []}, label="MiMo"))

    def test_a_4xx_carries_the_platform_words(self, fake_http):
        fake_http.status_code = 401
        fake_http.payload = {"error": {"message": "invalid api key"}}
        with pytest.raises(EngineError, match="HTTP 401.*invalid api key"):
            asyncio.run(client.synthesize(_cfg(), {"messages": []}, label="MiMo"))

    def test_a_4xx_with_a_plain_body_still_says_something(self, fake_http):
        fake_http.status_code = 502
        fake_http.text = "<html>Bad Gateway</html>"
        with pytest.raises(EngineError, match="HTTP 502"):
            asyncio.run(client.synthesize(_cfg(), {"messages": []}, label="MiMo"))

    def test_content_filter_is_named_as_such(self, fake_http):
        fake_http.payload = _audio_payload(finish_reason="content_filter")
        with pytest.raises(EngineError, match="content_filter"):
            asyncio.run(client.synthesize(_cfg(), {"messages": []}, label="MiMo"))

    def test_missing_audio_data_is_not_a_silent_empty_file(self, fake_http):
        fake_http.payload = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        with pytest.raises(EngineError, match="没有返回音频"):
            asyncio.run(client.synthesize(_cfg(), {"messages": []}, label="MiMo"))

    def test_a_tiny_audio_is_refused(self, fake_http):
        fake_http.payload = _audio_payload(size=40)
        with pytest.raises(EngineError, match="低于合理的最小长度"):
            asyncio.run(client.synthesize(_cfg(), {"audio": {"format": "wav"}}, label="MiMo"))

    def test_raw_pcm_is_wrapped_into_a_wav_container(self, fake_http):
        """平台给的 pcm16 是 24kHz 裸样，没有文件头——下游存不了也播不了。

        包一层 wav 壳不改变内容（44 字节头），只是让它能落盘。
        """
        fake_http.payload = _audio_payload(size=48000)
        result = asyncio.run(client.synthesize(_cfg(), {"audio": {"format": "pcm16"}}, label="MiMo"))
        assert result.format == "wav"
        assert result.audio[:4] == b"RIFF"
        assert result.audio[8:12] == b"WAVE"
        assert len(result.audio) == 48000 + 44

    def test_pcm_and_pcm16_are_the_same_thing(self, fake_http):
        fake_http.payload = _audio_payload(size=1000)
        result = asyncio.run(client.synthesize(_cfg(), {"audio": {"format": "pcm"}}, label="MiMo"))
        assert result.format == "wav"


class TestVoiceSample:
    def test_a_wav_sample_becomes_a_data_uri(self, tmp_path):
        sample = tmp_path / "ref.wav"
        sample.write_bytes(_wav_bytes())
        uri = client.voice_sample_data_uri(str(sample), label="MiMo")
        assert uri.startswith("data:audio/wav;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == _wav_bytes()

    def test_an_mp3_sample_uses_the_mpeg_mime(self, tmp_path):
        sample = tmp_path / "ref.mp3"
        sample.write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 100)
        uri = client.voice_sample_data_uri(str(sample), label="MiMo")
        assert uri.startswith("data:audio/mpeg;base64,")

    def test_a_container_the_platform_refuses_is_named(self, tmp_path):
        sample = tmp_path / "ref.flac"
        sample.write_bytes(b"fLaC" + b"\x00" * 64)
        with pytest.raises(EngineError, match="flac"):
            client.voice_sample_data_uri(str(sample), label="MiMo")

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(EngineError, match="无法读取"):
            client.voice_sample_data_uri(str(tmp_path / "没有.wav"), label="MiMo")

    def test_an_oversized_sample_is_refused_before_sending(self, tmp_path):
        """base64 后超过 10 MB 平台会拒——在发出去之前就说清实际大小。"""
        sample = tmp_path / "big.wav"
        # 一个合法的 wav 头（RIFF....WAVE）+ 8 MB 内容：base64 后约 10.7 MB。
        sample.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * (8 * 1024 * 1024))
        with pytest.raises(EngineError, match="超出平台上限"):
            client.voice_sample_data_uri(str(sample), label="MiMo")


# --------------------------------------------------------------------------- #
# 端到端：网关 → 插件
# --------------------------------------------------------------------------- #


class TestThroughTheGateway:
    def test_a_bare_preset_name_resolves_through_the_engine_list(
        self, data_dir, service, fake_http
    ):
        """库里没有这条音色时，问一次引擎的实时清单（仅限有清单能力的引擎）。"""
        from app.usecase.engines import save_engine_settings
        from app.usecase.gateway import synthesize

        save_engine_settings("mimo", {"api_key": "sk-gw"})
        fake_http.payload = _audio_payload()
        result = asyncio.run(
            synthesize(service, engine_key="mimo", voice_id="茉莉", text="你好。")
        )
        assert len(result.audio) == 4000
        body = fake_http.calls[-1]["json"]
        assert body["audio"]["voice"] == "茉莉"
        assert body["messages"][-1]["content"] == "你好。"

    def test_a_registered_preset_voice_goes_through_the_preset_model(
        self, data_dir, service, fake_http, seed_platform_voice
    ):
        """入库后的平台音色：网关只发平台那一侧的 id，走预置模型。"""
        from app.usecase.engines import save_engine_settings
        from app.usecase.gateway import synthesize

        save_engine_settings("mimo", {"api_key": "sk-gw"})
        seed_platform_voice(engine="mimo", voice_id="苏打", name="苏打")
        fake_http.payload = _audio_payload()
        result = asyncio.run(
            synthesize(service, engine_key="mimo", voice_id="mimo:苏打", text="你好。")
        )
        assert result.format == "wav"
        body = fake_http.calls[-1]["json"]
        assert body["model"] == MODEL_PRESET
        assert body["audio"]["voice"] == "苏打"


class TestSampleClone:
    """MiMo 的克隆：样本即音色。库里的行带 ``payload.ref_audio``，以后每次合成带着它。"""

    def test_clone_copies_the_sample_and_registers_a_payload(
        self, data_dir, service, tmp_path
    ) -> None:
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        source = tmp_path / "源.wav"
        source.write_bytes(_wav_bytes())
        meta = asyncio.run(
            clone_voice(
                service, "mimo", CloneRequest(name="胡桃克隆", audio_path=str(source))
            )
        )
        voice = service.get_voice(f"mimo:{meta.id}")
        ref = Path(voice.payload["ref_audio"])
        assert ref.is_file()
        assert ref.read_bytes() == source.read_bytes()

    def test_later_synthesis_uses_the_stored_sample_and_clone_model(
        self, data_dir, service, fake_http, tmp_path
    ) -> None:
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice, save_engine_settings
        from app.usecase.gateway import synthesize

        save_engine_settings("mimo", {"api_key": "sk-gw"})
        source = tmp_path / "源.wav"
        source.write_bytes(_wav_bytes())
        meta = asyncio.run(
            clone_voice(
                service, "mimo", CloneRequest(name="胡桃克隆", audio_path=str(source))
            )
        )
        fake_http.payload = _audio_payload()
        asyncio.run(
            synthesize(
                service,
                engine_key="mimo",
                voice_id=f"mimo:{meta.id}",
                text="你好。",
            )
        )
        body = fake_http.calls[-1]["json"]
        assert body["model"] == MODEL_CLONE
        assert body["audio"]["voice"].startswith("data:audio/wav;base64,")
        assert body["messages"][-1]["content"] == "你好。"


class TestVoiceDesign:
    """设计音色：描述入库，以后每次合成带着它走 voicedesign，不重传音色 id。"""

    def test_design_voice_is_registered_with_its_prompt(self, data_dir, service) -> None:
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        meta = asyncio.run(
            clone_voice(
                service,
                "mimo",
                CloneRequest(name="老者旁白", design_prompt="低沉的老者，吐字慢。"),
            )
        )
        voice = service.get_voice(f"mimo:{meta.id}")
        assert voice.payload["design_prompt"] == "低沉的老者，吐字慢。"
        assert voice.category == "设计音色"

    def test_later_synthesis_sends_the_prompt_without_a_voice(
        self, data_dir, service, fake_http
    ) -> None:
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice, save_engine_settings
        from app.usecase.gateway import synthesize

        save_engine_settings("mimo", {"api_key": "sk-gw"})
        meta = asyncio.run(
            clone_voice(
                service,
                "mimo",
                CloneRequest(name="老者旁白", design_prompt="低沉的老者，吐字慢。"),
            )
        )
        fake_http.payload = _audio_payload()
        asyncio.run(
            synthesize(
                service,
                engine_key="mimo",
                voice_id=f"mimo:{meta.id}",
                text="你好。",
            )
        )
        body = fake_http.calls[-1]["json"]
        assert body["model"] == MODEL_DESIGN
        assert "voice" not in body["audio"]
        assert body["messages"][0]["content"] == "低沉的老者，吐字慢。"
        assert body["messages"][-1]["content"] == "你好。"

    def test_a_second_synthesis_sends_the_same_prompt(
        self, data_dir, service, fake_http
    ) -> None:
        """平台不保存音色：两次请求是两次独立的设计，凭据只有同一句描述。"""
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice, save_engine_settings
        from app.usecase.gateway import synthesize

        save_engine_settings("mimo", {"api_key": "sk-gw"})
        meta = asyncio.run(
            clone_voice(service, "mimo", CloneRequest(name="旁白", design_prompt="沉稳男声。"))
        )
        fake_http.payload = _audio_payload()
        for text in ("第一句。", "第二句。"):
            asyncio.run(
                synthesize(service, engine_key="mimo", voice_id=f"mimo:{meta.id}", text=text)
            )
        bodies = [call["json"] for call in fake_http.calls]
        assert len(bodies) == 2
        assert all(body["model"] == MODEL_DESIGN for body in bodies)
        assert all(body["messages"][0]["content"] == "沉稳男声。" for body in bodies)

    def test_an_engine_without_voice_design_refuses_it(self, data_dir, service) -> None:
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        with pytest.raises(EngineError, match="不支持音色设计"):
            asyncio.run(
                clone_voice(
                    service,
                    "gpt_sovits",
                    CloneRequest(name="旁白", design_prompt="沉稳男声。"),
                )
            )


def _meta(voice_id: str):
    from app.engines.base import VoiceMeta

    return VoiceMeta(id=voice_id, name=voice_id)
