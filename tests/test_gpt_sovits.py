"""GPT-SoVITS 插件：**照官方 ``api_v2.py`` 核过的那些点**，每一条都钉一个断言。

和 ``test_minimax.py`` 一样，这个文件里**没有一次真实网络请求**——假 httpx 客户端
既让测试离线跑，又能断言"到底发了什么"（路径、**方法**、body）。

这一家最容易被写错的三处，正是下面重点钉的：

1. 方法（GET 也能用，但正文会撞 URL 长度上限）；
2. "服务没起"与"这段合成不出来"要分开说——前者才是最常见的失败；
3. 它**没有音色清单也没有克隆**，谁要是在这里声明成 ``True``，
   界面上就会出现一颗点了才报错的按钮。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Self

import httpx
import pytest

from app.engines.base import KIND_CLOUD, KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.gpt_sovits import client as gsv_client
from app.engines.builtin.gpt_sovits.plugin import GptSovitsEngine
from app.engines.builtin.gpt_sovits.settings import GptSovitsSettings


def _cfg(**over: object) -> GptSovitsSettings:
    data = {"base_url": "http://127.0.0.1:9880"}
    data.update(over)
    return GptSovitsSettings(**data)


def _req(**over: object) -> SynthesisRequest:
    data = {
        "text": "你好，世界。",
        "voice_id": "胡桃",
        "reference_audio": "D:/library/原神/胡桃/默认.wav",
        "reference_text": "吃饱喝饱，一路走好！",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


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
    """替身 httpx.AsyncClient。**只实现 post**——这一家发 GET 就走不通。

    故意不提供 ``get``：真发成 GET 的话这里直接 ``AttributeError``，
    而不是"测试仍然绿、线上才发现"。
    """

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


def _wav(size: int = 4096) -> bytes:
    return b"RIFF" + b"\x00" * (size - 4)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        """**每个字段都得有界面上的说法**——漏一个就会在表单上显示成英文字段名。"""
        for name, field in GptSovitsSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_no_secret_fields(self):
        """本机服务没有凭据——**别为了对称摆一个空的密钥框**。"""
        assert GptSovitsEngine.secret_fields() == frozenset()

    def test_host_fills_in_a_missing_scheme(self):
        """省掉 ``http://`` 是常事，而 httpx 对它的报错是 ``unsupported protocol``。"""
        assert _cfg(base_url="127.0.0.1:9880").host == "http://127.0.0.1:9880"
        assert _cfg(base_url="http://127.0.0.1:9880/").host == "http://127.0.0.1:9880"
        assert _cfg(base_url="").host == "http://127.0.0.1:9880"

    def test_language_choices_cover_the_v2_table(self):
        """语言是服务端**会校验**的一格，写错就是一次 400——候选要照官方抄全。"""
        choices = GptSovitsSettings.model_fields["text_lang"].json_schema_extra["choices"]
        values = {value for value, _ in choices}
        assert {"auto", "zh", "en", "ja", "ko", "yue", "auto_yue"} <= values
        assert {"all_zh", "all_ja", "all_yue", "all_ko"} <= values

    def test_cut_method_choices_match_the_registered_names(self):
        """切分方法名是服务端注册的那几个（``cut0``–``cut5``），一个不多一个不少。"""
        choices = GptSovitsSettings.model_fields["text_split_method"].json_schema_extra["choices"]
        assert [value for value, _ in choices] == [f"cut{i}" for i in range(6)]

    def test_media_type_choices_are_the_official_whitelist(self):
        choices = GptSovitsSettings.model_fields["media_type"].json_schema_extra["choices"]
        assert {value for value, _ in choices} == {"wav", "ogg", "aac", "raw"}

    def test_the_v5_versions_are_selectable(self):
        """V5 是官方 2026-10 发布的版本线，gsv.cpp 也吃它——下拉里得能选到。"""
        choices = GptSovitsSettings.model_fields["model_version"].json_schema_extra["choices"]
        values = {value for value, _ in choices}
        assert {"v5turbo", "v5dev"} <= values

    def test_dbcache_can_be_left_unsent(self):
        """空串 = 不发送。它是"用服务端默认"的唯一入口，不能没有。"""
        choices = GptSovitsSettings.model_fields["dbcache"].json_schema_extra["choices"]
        assert next(value for value, _ in choices) == ""


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        caps = GptSovitsEngine().capabilities()
        assert caps.kind == KIND_LOCAL
        assert caps.kind != KIND_CLOUD

    def test_does_not_claim_a_voice_list_it_cannot_get(self):
        """**没有清单端点就别说有**：声明成 True 会让左栏多一个点了就报错的 ⟳。"""
        caps = GptSovitsEngine().capabilities()
        assert caps.voice_listing is False

    def test_does_not_claim_cloning(self):
        """训练音色是离线的另一套流程，不是一次 HTTP 调用。"""
        assert GptSovitsEngine().capabilities().voice_cloning is False

    def test_declares_the_reference_audio_capability(self):
        """这是它的看家能力：参考音频 + 参考文本 = 它的"音色"。"""
        assert GptSovitsEngine().capabilities().reference_audio is True

    def test_does_not_claim_emotion_control(self):
        """没有情绪参数——情绪跟着参考音频走，别让界面给出一颗没用的按钮。"""
        assert GptSovitsEngine().capabilities().emotion_control is False

    def test_does_not_cap_the_text_length(self):
        """长文本由它自己按 text_split_method 断句，这里没有可引用的上限。"""
        assert GptSovitsEngine().capabilities().max_chars == 0

    def test_spec_requires_the_reference_audio(self):
        assert GptSovitsEngine().synthesis_spec().requires == ("reference_audio",)

    def test_spec_wants_a_transcript(self):
        """参考文本是**必填**：V3/V4/V5 的声码器路线不收无文本模式。

        声明它之后，那一格空着时中转站会先听一遍再发（见 ``usecase.reference``），
        听出来的那句话还会写回样例，下次不用再听。
        """
        assert GptSovitsEngine().synthesis_spec().needs_transcript is True

    def test_spec_feeds_only_wav_as_the_reference(self):
        """参考音频只发 wav：gsv.cpp 的 ``prep_reference`` 只解 PCM。

        喂 mp3 回 500 ``prep_reference failed: …mp3``（实测）。GAG 自己的
        ``prepare_reference_audio`` 也是先把非 wav 转成单声道 PCM_16 wav 再上传——
        说明这条边界在引擎里，界面替它做了。声明之后中转站会先转一道（有缓存）。
        """
        assert GptSovitsEngine().synthesis_spec().reference_formats == ("wav",)

    def test_spec_pins_the_reference_duration(self):
        """服务端 3–10 秒是一条硬判据（``48000..160000`` 采样点）。

        越界时它**不报错**：``TTS.run`` 吞掉异常吐一秒静音，``api_v2`` 再包成 200，
        用户看到的是"合成成功但没声音"。所以声明区间，发之前自己量。
        """
        assert GptSovitsEngine().synthesis_spec().reference_duration == (3.0, 10.0)

    def test_a_transcript_is_required_regardless_of_version(self):
        """**不分版本，缺了就补。**

        gsv.cpp 对 V3/V4/V5 直接回 ``prompt_text is required (V3/V4/V5 do not support
        reference-text-free mode)``；官方 v1/v2/v2Pro 允许留空，但空着只是"少一点
        相似度"——用户看不出来的损失。而"服务端是哪一版"接口问不到，让用户为此
        去猜一版没有意义。
        """
        engine = GptSovitsEngine()
        for version in ("v5turbo", "v5dev", "v3", "v4", "v2ProPlus", "v2Pro", "v2", "v1"):
            assert engine.synthesis_spec_for(_cfg(model_version=version)).needs_transcript

    def test_spec_requires_an_ascii_reference_path(self):
        """gsv.cpp 把请求里的路径转本地代码页：中文名一律 500（实测），
        同一个文件换 ASCII 名就正常。声明它，中转站会先落一份 ASCII 副本。"""
        assert GptSovitsEngine().synthesis_spec().reference_path_ascii is True


# --------------------------------------------------------------------------- #
# 请求装配
# --------------------------------------------------------------------------- #


class TestBody:
    def _body(self, **over: object) -> dict[str, object]:
        cfg = _cfg()
        return GptSovitsEngine().build_body(cfg, _req(**over))

    def test_reference_audio_lands_on_ref_audio_path(self):
        """统一参数里的 ``reference_audio`` → 这一家的 ``ref_audio_path``。"""
        body = self._body()
        assert body["ref_audio_path"] == "D:/library/原神/胡桃/默认.wav"

    def test_spoken_text_lands_on_prompt_text(self):
        """样例的**说话内容**进 ``prompt_text``——这正是它在协议里的用途。"""
        body = self._body()
        assert body["prompt_text"] == "吃饱喝饱，一路走好！"

    def test_text_is_passed_through(self):
        assert self._body()["text"] == "你好，世界。"

    def test_streaming_is_pinned_off(self):
        """要**一整段**音频：调用方（有声书）要的是能落盘的文件，不是分片。"""
        assert self._body()["streaming_mode"] is False

    def test_languages_come_from_the_config(self):
        """上游两格都没给时，正文/参考语言取配置里那两格（服务端两格都必填）。"""
        cfg = _cfg(text_lang="zh", prompt_lang="ja")
        body = GptSovitsEngine().build_body(cfg, _req())
        assert body["text_lang"] == "zh"
        assert body["prompt_lang"] == "ja"

    def test_upstream_language_beats_the_config(self):
        """上游明确说了而且服务端认得 → 用上游的（配置只是"没说时"的默认）。"""
        cfg = _cfg(text_lang="auto", prompt_lang="auto")
        body = GptSovitsEngine().build_body(
            cfg, _req(extra={"language": "zh", "reference_language": "ja"})
        )
        assert body["text_lang"] == "zh"
        assert body["prompt_lang"] == "ja"

    def test_a_language_it_does_not_know_falls_back_to_the_config(self):
        """统一词表里的西班牙语这一家没有——**剔掉这一格**，别硬发去换一个 400。

        正常做法是"用回配置"而不是"整段失败"：一次 400 会拖死整章合成。
        """
        cfg = _cfg(text_lang="auto", prompt_lang="auto")
        body = GptSovitsEngine().build_body(
            cfg, _req(extra={"language": "es", "reference_language": "fr"})
        )
        assert body["text_lang"] == "auto"
        assert body["prompt_lang"] == "auto"

    def test_split_method_and_format_come_from_the_config(self):
        cfg = _cfg(text_split_method="cut3", media_type="ogg")
        body = GptSovitsEngine().build_body(cfg, _req())
        assert body["text_split_method"] == "cut3"
        assert body["media_type"] == "ogg"

    def test_upstream_format_beats_the_configured_default(self):
        """**上游说了就用上游的**——配置里那一格是"没说时"的默认值。"""
        cfg = _cfg(media_type="ogg")
        body = GptSovitsEngine().build_body(cfg, _req(format="wav"))
        assert body["media_type"] == "wav"

    def test_upstream_speed_beats_the_configured_default(self):
        """顺序反了的话，上游明确要求的倍速会被这台引擎自己的默认值盖掉。"""
        cfg = _cfg(speed_factor=1.0)
        body = GptSovitsEngine().build_body(cfg, _req(extra={"speed": 1.4}))
        assert body["speed_factor"] == 1.4

    def test_configured_speed_is_used_when_upstream_is_silent(self):
        cfg = _cfg(speed_factor=0.9)
        assert GptSovitsEngine().build_body(cfg, _req())["speed_factor"] == 0.9

    def test_seed_from_extra_reaches_the_body(self):
        body = self._body(extra={"seed": 42})
        assert body["seed"] == 42

    def test_batch_size_comes_from_the_config(self):
        """一段长文本会被它自己切成多段；``batch_size`` 决定同时算几段。"""
        assert self._body()["batch_size"] == 4
        assert GptSovitsEngine().build_body(_cfg(batch_size=8), _req())["batch_size"] == 8

    def test_v5_knobs_are_left_out_when_unset(self):
        """0 / 空串 = 不发送。官方 api_v2 根本没有 ``dbcache``，发个空串只是噪音；
        gsv.cpp 也不认 ``batch_size``，两边都会静默丢掉不认识的字段。"""
        body = self._body()
        assert "sample_steps" not in body
        assert "dbcache" not in body

    def test_v5_knobs_are_sent_when_configured(self):
        cfg = _cfg(sample_steps=4, dbcache="auto")
        body = GptSovitsEngine().build_body(cfg, _req())
        assert body["sample_steps"] == 4
        assert body["dbcache"] == "auto"

    def test_missing_reference_audio_is_refused_here(self):
        """**发出去之前**就拦下：缺参考音频是"平台音色喂给本地引擎"的样子。"""
        with pytest.raises(ValueError, match="reference_audio"):
            self._body(reference_audio="")

    def test_empty_reference_text_is_simply_omitted(self):
        """v1/v2 不要求参考文本，省了只是少一点相似度——**不要发一个空串过去**。"""
        assert "prompt_text" not in self._body(reference_text="")

    def test_emotion_has_no_landing_spot(self):
        """这一家没有情绪参数，映射表里也就没有这一行——**明说没有，不是漏接**。"""
        body = self._body(emotion="开心")
        assert "emotion" not in body

    def test_mapping_table_says_what_is_not_supported(self):
        """不认的参数必须有一句解释，否则"请求成功"会把它盖过去。

        语言**不在这张表里**：它有落点（``text_lang`` / ``prompt_lang``）。
        """
        engine = GptSovitsEngine()
        missing = engine.unsupported()
        for word in ("emotion", "voice_id", "volume", "pitch"):
            assert word in missing, f"说明里没提 {word}"
        assert "language" not in missing
        assert "reference_language" not in missing
        assert any("模型" in item for item in engine.mapping().cautions)


# --------------------------------------------------------------------------- #
# 换模型版本时的参数跟随
# --------------------------------------------------------------------------- #


class TestVersionSwitch:
    """选了哪一版，采样参数就跟到那一版的推荐值——**但只动没被手改过的格子**。"""

    def _merge(self, incoming: dict[str, object], stored: dict[str, object]) -> dict[str, object]:
        return GptSovitsEngine().reconcile_settings(incoming, stored)

    def test_switching_to_v5_brings_its_own_knobs(self):
        """V5 的预设（``GAG_config.json``）：top_k 5、cut1、4 步、dbcache auto。

        这几格当时都还是 v2ProPlus 的推荐值（= 用户没动过），所以一起换过去。
        """
        merged = self._merge(
            {"model_version": "v5turbo", "top_k": 15, "text_split_method": "cut5",
             "sample_steps": 0, "dbcache": ""},
            {"model_version": "v2ProPlus", "top_k": 15, "text_split_method": "cut5",
             "sample_steps": 0, "dbcache": ""},
        )
        assert merged["top_k"] == 5
        assert merged["text_split_method"] == "cut1"
        assert merged["sample_steps"] == 4
        assert merged["dbcache"] == "auto"

    def test_hand_tuned_values_survive_the_switch(self):
        """用户把 top_k 调到 8 再切版本——8 是他的选择，不该被覆盖；
        同一批里没动过的断句方式仍然跟着换。"""
        merged = self._merge(
            {"model_version": "v5turbo", "top_k": 8},
            {"model_version": "v2ProPlus", "top_k": 8, "text_split_method": "cut5",
             "sample_steps": 0, "dbcache": ""},
        )
        assert merged["top_k"] == 8
        assert merged["text_split_method"] == "cut1"

    def test_switching_back_restores_the_ordinary_defaults(self):
        """从 V5 切回 v2ProPlus，那几格回到官方默认（15 / cut5 / 不带 sample_steps）。"""
        merged = self._merge(
            {"model_version": "v2ProPlus", "top_k": 5, "text_split_method": "cut1",
             "sample_steps": 4, "dbcache": "auto"},
            {"model_version": "v5turbo", "top_k": 5, "text_split_method": "cut1",
             "sample_steps": 4, "dbcache": "auto"},
        )
        assert merged["top_k"] == 15
        assert merged["text_split_method"] == "cut5"
        assert merged["sample_steps"] == 0
        assert merged["dbcache"] == ""

    def test_without_a_version_change_nothing_moves(self):
        incoming = {"model_version": "v2ProPlus", "top_k": 30}
        assert self._merge(incoming, {"model_version": "v2ProPlus", "top_k": 15}) == incoming

    def test_two_versions_with_the_same_defaults_do_not_touch_anything(self):
        """v3 与 v4 的推荐值一样，来回切不该改动任何格子。"""
        incoming = {"model_version": "v4", "top_k": 15}
        assert self._merge(incoming, {"model_version": "v3", "top_k": 15}) == incoming


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #


class TestRequests:
    def test_synthesis_posts_json_to_the_tts_endpoint(self, fake_http):
        """**POST + JSON body**：GET 也能用，但正文塞进查询串会撞 URL 长度上限。"""
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

        call = fake_http.calls[0]
        assert call["path"] == gsv_client.SYNTH_PATH == "/tts"
        assert isinstance(call["json"], dict)

    def test_base_url_is_the_configured_host(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(GptSovitsEngine().synthesize(_cfg(base_url="127.0.0.1:19999"), _req()))
        assert fake_http.calls[0]["init"]["base_url"] == "http://127.0.0.1:19999"

    def test_success_returns_the_audio_bytes(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav(8192), content_type="audio/wav")
        result = asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))
        assert len(result.audio) == 8192
        assert result.format == "wav"

    def test_local_inference_reports_no_cost(self, fake_http):
        """本机推理不按量计费——**空着，不编一个数字**。"""
        fake_http.response = _FakeResponse(content=_wav())
        assert asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req())).cost_note == ""

    def test_returns_the_format_that_was_actually_asked_for(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav(), content_type="audio/ogg")
        result = asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req(format="ogg")))
        assert result.format == "ogg"

    # ---- 错误形状：三种要分得开（服务没起 / 参数不认 / 这段合成不出来） ---- #

    def test_connection_refused_says_the_service_is_not_running(self, fake_http):
        """只说"哪个引擎、哪个地址连不上"：原因可能是没起、端口错、刚崩掉，不替用户挑。"""
        fake_http.raise_error = httpx.ConnectError("connection refused")
        with pytest.raises(EngineError, match=r"GPT-SoVITS（.*）无法连接"):
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

    def test_timeout_names_the_address_and_the_wait(self, fake_http):
        fake_http.raise_error = httpx.ReadTimeout("too slow")
        with pytest.raises(EngineError, match=r"GPT-SoVITS（.*）响应超时"):
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

    def test_synthesis_failure_carries_the_servers_own_words(self, fake_http):
        """官方异常分支：``{"message": "tts failed", "Exception": "..."}``。"""
        fake_http.response = _FakeResponse(
            status_code=400,
            content_type="application/json",
            json_body={"message": "tts failed", "Exception": "prompt_text cannot be empty"},
        )
        with pytest.raises(EngineError) as info:
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))
        assert "tts failed" in str(info.value)
        assert "prompt_text cannot be empty" in str(info.value)

    def test_param_validation_carries_the_servers_own_words(self, fake_http):
        """``check_params`` 分支：``{"message": "ref_audio_path is required"}``。"""
        fake_http.response = _FakeResponse(
            status_code=400,
            content_type="application/json",
            json_body={"message": "ref_audio_path is required"},
        )
        with pytest.raises(EngineError, match="ref_audio_path is required"):
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

    def test_a_200_that_is_not_audio_is_reported_with_its_content_type(self, fake_http):
        """状态码说成功、内容却不是音频——多半是中间隔了个反向代理。"""
        fake_http.response = _FakeResponse(
            content=b"<html>proxy</html>", content_type="text/html"
        )
        with pytest.raises(EngineError, match="text/html"):
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

    def test_a_header_only_wav_is_refused(self, fake_http):
        """只有 44 字节的文件头 = 一个采样点都没出（文本为空之类）。"""
        fake_http.response = _FakeResponse(content=b"RIFF" + b"\x00" * 40)
        with pytest.raises(EngineError, match="合理的最小长度"):
            asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))

    def test_short_audio_threshold_is_far_below_a_real_sentence(self):
        """门槛只用来抓"空音频"：一秒真语音（32k/16bit 单声道）就是 64KB。"""
        assert gsv_client.MIN_AUDIO_BYTES < 64_000 / 10


# --------------------------------------------------------------------------- #
# 与中转站、注册表的衔接
# --------------------------------------------------------------------------- #


class TestWiring:
    def test_it_is_discovered_as_a_builtin(self):
        from app.engines.registry import list_engines

        assert "gpt_sovits" in list_engines()

    def test_a_platform_voice_is_refused_before_any_request(self, data_dir, service):
        """**平台音色喂给这台引擎**是最容易犯的错——它没有 id、只有参考音频。

        这条走完整条网关链（解析 → 校验 → 路由），并在**发请求之前**报出来。
        """
        from app.engines.base import VoiceMeta
        from app.usecase.gateway import synthesize

        voice = service.add_platform_voice("minimax", VoiceMeta(id="MX_V", name="林晚"))
        with pytest.raises(EngineError, match="参考音频"):
            asyncio.run(synthesize(service, engine_key="gpt_sovits", voice_id=voice.id, text="你好"))

    def test_a_local_voice_carries_its_sample_into_the_request(self, demo_library, service):
        """本地音色 → 参考音频（主样例）+ 参考文本（那条样例的说话内容）。"""
        from app.usecase.gateway import normalize_voice

        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        # 主样例挑「开心.wav」——它旁边有一份同名 .txt（说话内容）。
        # 默认的「默认.wav」在演示库里没有配套文本，那是下面那条用例的事。
        service.set_main_sample(hutao.id, "开心.wav")
        request = normalize_voice(service, hutao.id)
        assert request.reference_audio.endswith("开心.wav")
        assert request.reference_text == "吃饱喝饱，一路走好！"

    def test_a_main_sample_without_spoken_text_leaves_it_empty(self, demo_library, service):
        """主样例没有配套文本时**参考文本就是空的**——不编一段充数。

        这一格只有样例自己知道（谁念的、念的什么），猜不出来。v3/v4 的模型会因此
        报 ``prompt_text cannot be empty``——那是服务端在说"请把说话内容填上"，
        也正是映射表「说明」列提前提醒过的那一条。
        """
        from app.usecase.gateway import normalize_voice

        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(hutao.id, "默认.wav")
        assert normalize_voice(service, hutao.id).reference_text == ""

    def test_refresh_says_it_has_no_listing(self, data_dir, service):
        """点 ⟳ 时如实说"这台不提供清单"，而不是报一个网络错误。"""
        from app.usecase.engines import refresh_voices

        result = asyncio.run(refresh_voices(service, "gpt_sovits"))
        assert result["ok"] is False
        assert "不提供音色清单" in result["reason"]

    def test_clone_is_refused_with_a_400_not_a_404(self, data_dir, service):
        """引擎**在**、只是不做这件事 → 该报 400。

        :class:`EngineConfigError` 翻成 404（那是"没有这台引擎"），所以这里抛的是
        :class:`EngineError`。报 404 会让调用方去查引擎名拼错了没有——而它没拼错。
        """
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        with pytest.raises(EngineError, match="不支持音色克隆"):
            asyncio.run(clone_voice(service, "gpt_sovits", CloneRequest(name="x")))

    def test_local_requests_ignore_the_system_proxy(self, fake_http):
        """``trust_env=False``：**本机请求不能让系统代理接管**。

        实测踩过：环境里有 ``HTTP_PROXY`` 时，发给 ``127.0.0.1:9880`` 的请求会被
        绕到代理上，代理再连它自己的 upstream，抛出来的是一句代理的话
        （"upstream connect failed……"）——于是用户去查代理，
        而问题只是"本机那个端口没人监听"。
        """
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(GptSovitsEngine().synthesize(_cfg(), _req()))
        assert fake_http.calls[0]["init"].get("trust_env") is False
