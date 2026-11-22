"""CosyVoice3 插件：**照规格核过的那些点**，每条钉一个断言。

与 ``test_voxcpm2.py`` 同一套写法：不碰网络，假 httpx 客户端既让测试离线跑，
又能断言"到底发了什么"。

这一家最容易写错的地方：

1. **模板由输入决定**：有引导指令走 ``instruct``、开了跨语种走 ``cross_lingual``、
   否则 ``zero_shot``——模板定不下来，其余参数说了也白说；
2. **参考音频是硬前提**（三种模板都吃它），缺了当场说清，不发请求；
3. **种子省略时引擎用定值 1986**，"每次不一样"必须由插件抽；
4. 没有的能力（内置音色 / 音色 id / 音色设计 / 音量 / 音高）一条都不声明，给了就在
   结果说明里报"未发送"；语速自 audio.cpp 0.9.6 起映射到 ``options.speed``；
   情绪没有枚举，但会翻成一句 instruct 发出去。
5. **方言/语言也走 instruct**（官方示例是「用四川话说这句话」），不是单独参数；
6. **正文发声事件用方括号原生标签**（``[laughter]`` / ``[breath]`` / ``[cough]`` /
   ``[sigh]``），而 ``<|endofprompt|>`` 不能发——这一层会把它前面的指令念出来；
   中文指令超过 20 字也会被念出后半段，插件在标点处截断并报出省略。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import ClassVar, Self

import httpx
import pytest

from app.engines.base import KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.cosyvoice3.plugin import (
    CosyVoice3Engine,
    resolve_seed,
)
from app.engines.builtin.cosyvoice3.settings import CosyVoice3Settings
from app.engines.registry import list_engines


def _cfg(**over: object) -> CosyVoice3Settings:
    data: dict[str, object] = {"base_url": "http://127.0.0.1:18092"}
    data.update(over)
    return CosyVoice3Settings(**data)


def _wav(size: int = 4096) -> bytes:
    head = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WAVE"
    return head + b"\x00" * (size - len(head))


_REF = ""


@pytest.fixture(autouse=True)
def _real_reference_audio(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """真写一个小 WAV，**文件名故意带中文**：这一家收本机路径，只校验读得到。"""
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
        "reference_text": "她站在原地，没有回头。",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> CosyVoice3Engine:
    return CosyVoice3Engine()


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
        assert "cosyvoice3" in engines
        assert isinstance(engines["cosyvoice3"], CosyVoice3Engine)
        assert engines["cosyvoice3"].label == "CosyVoice3"


class TestSettings:
    def test_every_field_declares_a_label(self):
        for name, field in CosyVoice3Settings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_the_defaults_follow_the_engine_spec(self):
        cfg = _cfg()
        assert cfg.model == "cosyvoice3"
        assert cfg.top_k == 25  # 官方默认
        assert cfg.num_inference_steps == 10
        # 引擎省略种子时用定值 1986，所以插件默认必须是"随机"才有差异。
        assert cfg.seed == -1
        assert cfg.cross_lingual is False
        assert cfg.text_chunk_size == 0  # 0 = 用引擎默认 600

    def test_host_fills_in_a_missing_scheme(self):
        assert _cfg(base_url="127.0.0.1:18092").host == "http://127.0.0.1:18092"
        assert _cfg(base_url="").host == "http://127.0.0.1:18092"


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

    def test_instruction_is_not_voice_design(self):
        """指令只调语气，造不出一个新音色（参考音频仍是硬前提）。"""
        assert _engine().capabilities().voice_design is False

    def test_reference_audio_and_cross_lingual(self):
        caps = _engine().capabilities()
        assert caps.reference_audio is True
        assert caps.cross_lingual is True

    def test_languages_and_dialects(self):
        caps = _engine().capabilities()
        # 9 种外语 + 官方列的 18+ 中文方言里统一词表覆盖到的方言点。
        for code in ("zh", "yue", "cmn-cd", "cmn-xa", "wuu-sz", "nan-qz", "en", "ja"):
            assert code in caps.languages
        assert "粤语" in caps.dialects
        assert "成都话" in caps.dialects

    def test_single_concurrency(self):
        assert _engine().capabilities().max_concurrency == 1

    def test_spec_requires_a_reference(self):
        spec = _engine().synthesis_spec()
        assert spec.requires == ("reference_audio",)
        assert spec.formats == ("wav",)
        assert spec.reference_formats == ("wav",)
        assert spec.needs_transcript is True


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


class TestBody:
    def test_default_template_is_zero_shot(self):
        body = _engine().build_body(_cfg(), _req())
        assert body["options"]["template_name"] == "zero_shot"
        assert "instruct" not in body["options"]

    def test_an_instruction_switches_the_template(self):
        body = _engine().build_body(_cfg(), _req(extra={"instruction": "轻声、放慢"}))
        assert body["options"]["template_name"] == "instruct"
        assert body["options"]["instruct"] == "轻声、放慢"

    def test_the_cross_lingual_setting_switches_the_template(self):
        body = _engine().build_body(_cfg(cross_lingual=True), _req())
        assert body["options"]["template_name"] == "cross_lingual"

    def test_an_instruction_wins_over_the_cross_lingual_setting(self):
        body = _engine().build_body(
            _cfg(cross_lingual=True), _req(extra={"instruction": "轻声"})
        )
        assert body["options"]["template_name"] == "instruct"

    def test_the_sampling_defaults_are_sent(self):
        body = _engine().build_body(_cfg(top_k=12, num_inference_steps=16), _req())
        assert body["options"]["top_k"] == 12
        assert body["options"]["num_inference_steps"] == 16

    def test_text_chunk_size_is_omitted_when_zero(self):
        body = _engine().build_body(_cfg(), _req())
        assert "text_chunk_size" not in body["options"]
        assert (
            _engine().build_body(_cfg(text_chunk_size=300), _req())["options"]["text_chunk_size"]
            == 300
        )

    def test_a_missing_reference_is_refused_before_the_request(self):
        with pytest.raises(EngineError, match="需要参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio=""))

    def test_an_unreadable_reference_is_refused(self, tmp_path: pathlib.Path):
        with pytest.raises(EngineError, match="读不到参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio=str(tmp_path / "没了.wav")))

    def test_a_fixed_seed_goes_straight_through(self):
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": 1986}))
        assert body["seed"] == 1986
        assert "seed=1986" in notes

    def test_random_seed_is_drawn_and_reported(self):
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": -1}))
        assert isinstance(body["seed"], int) and body["seed"] >= 0
        assert any(note.startswith("seed=") for note in notes)

    def test_unsupported_values_are_reported(self):
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"volume": 1.1})
        )
        joined = "".join(notes)
        assert "未发送" in joined
        assert "音量" in joined

    def test_speed_maps_into_options(self):
        """audio.cpp 0.9.6 起有原生语速：统一参数 speed 落到 options.speed。"""
        body = _engine().build_body(_cfg(), _req(extra={"speed": 1.2}))
        assert body["options"]["speed"] == 1.2
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"speed": 0.8})
        )
        assert not any("语速" in note for note in notes)

    def test_emotion_becomes_an_instruction(self):
        """没有情绪枚举，但 instruct 模板收自由文本：情绪翻成一句再发。"""
        body = _engine().build_body(_cfg(), _req(emotion="愤怒"))
        assert body["options"]["template_name"] == "instruct"
        assert body["options"]["instruct"] == "生气地质问，声音提高，语速加快。"

    def test_emotion_instruction_follows_the_text_language(self):
        body = _engine().build_body(
            _cfg(), _req(text="She stopped and looked back.", emotion="愤怒")
        )
        assert body["options"]["instruct"] == (
            "Speak angrily and loudly, faster, with sharp, rising endings."
        )

    def test_an_uncovered_emotion_uses_the_shared_table(self):
        body = _engine().build_body(_cfg(), _req(emotion="怅然"))
        assert body["options"]["instruct"] == "用怅然、失落而克制的语气朗读。"

    def test_an_explicit_instruction_wins_over_the_emotion(self):
        body = _engine().build_body(
            _cfg(), _req(emotion="愤怒", extra={"instruction": "轻声、放慢"})
        )
        assert body["options"]["instruct"] == "轻声、放慢"

    def test_a_long_instruction_is_clipped_at_a_clause(self):
        """长指令的后半段会被念进音频；截在标点上，并报出未发送的部分。"""
        full = "语气发紧，语速偏快，句子之间几乎不停，像有件急事压着。"
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"instruction": full})
        )
        assert body["options"]["instruct"] == "语气发紧，语速偏快，句子之间几乎不停。"
        joined = "".join(notes)
        assert "引导指令" in joined
        assert "其余未发送" in joined

    def test_a_short_instruction_is_not_clipped(self):
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"instruction": "轻声、放慢"})
        )
        assert body["options"]["instruct"] == "轻声、放慢"
        assert not any("其余未发送" in note for note in notes)

    def test_a_decimal_point_is_not_a_sentence_break(self):
        body = _engine().build_body(
            _cfg(), _req(extra={"instruction": "语速调到 1.5 倍，语气发紧。"})
        )
        assert body["options"]["instruct"] == "语速调到 1.5 倍，语气发紧。"

    def test_emotion_is_not_reported_as_unsent(self):
        _, notes = _engine().build_body_with_notes(_cfg(), _req(emotion="开心"))
        assert not any("情绪" in note for note in notes)

    def test_a_dialect_language_becomes_an_instruction(self):
        """官方的语言控制就是一句 instruct，不是单独的语言参数。"""
        body = _engine().build_body(_cfg(), _req(extra={"language": "cmn-cd"}))
        assert body["options"]["template_name"] == "instruct"
        assert body["options"]["instruct"] == "用四川话说这句话。"

    def test_a_dialect_label_is_accepted_too(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "成都话"}))
        assert body["options"]["instruct"] == "用四川话说这句话。"

    def test_mandarin_adds_no_instruction(self):
        body = _engine().build_body(_cfg(), _req(extra={"language": "zh"}))
        assert body["options"]["template_name"] == "zero_shot"
        assert "instruct" not in body["options"]

    def test_a_dialect_and_an_emotion_are_both_sent(self):
        body = _engine().build_body(
            _cfg(), _req(emotion="愤怒", extra={"language": "cmn-cd"})
        )
        instruct = body["options"]["instruct"]
        assert instruct.startswith("用四川话说这句话，")
        assert "生气地质问" in instruct

    def test_an_explicit_instruction_joins_the_dialect_and_wins_over_emotion(self):
        body = _engine().build_body(
            _cfg(),
            _req(
                emotion="愤怒",
                extra={"language": "cmn-cd", "instruction": "轻声、放慢"},
            ),
        )
        instruct = body["options"]["instruct"]
        assert instruct == "用四川话说这句话，轻声、放慢"
        assert "生气" not in instruct

    def test_the_library_delimiter_is_removed(self):
        """``<|endofprompt|>`` 是 Python 库的分隔符；这一层收了会被念出来。"""
        body = _engine().build_body(
            _cfg(), _req(extra={"instruction": "请用广东话表达。<|endofprompt|>"})
        )
        assert body["options"]["instruct"] == "请用广东话表达。"

    def test_the_dialect_is_accepted_by_the_gateway_whitelist(self):
        assert "language" in _engine().accepted_extra_keys()

    def test_a_wrapped_emotion_is_lifted_into_the_instruction(self):
        body = _engine().build_body(
            _cfg(), _req(text="<surprised>他停下脚步，回头看了她一眼。</surprised>")
        )
        assert body["input"] == "他停下脚步，回头看了她一眼。"
        assert body["options"]["template_name"] == "instruct"
        assert body["options"]["instruct"] == "用吃惊、上扬的语气说话，句首提高。"

    def test_a_wrapper_wins_over_the_emotion_field(self):
        body = _engine().build_body(
            _cfg(),
            _req(
                text="<surprised>他停下脚步。</surprised>",
                emotion="开心",
            ),
        )
        assert "吃惊" in body["options"]["instruct"]
        assert "明亮" not in body["options"]["instruct"]

    def test_a_wrapper_is_removed_but_loses_to_an_explicit_instruction(self):
        body = _engine().build_body(
            _cfg(),
            _req(
                text="<surprised>他停下脚步。</surprised>",
                extra={"instruction": "轻声、放慢"},
            ),
        )
        assert body["input"] == "他停下脚步。"
        assert body["options"]["instruct"] == "轻声、放慢"

    def test_multiple_wrappers_use_the_first_and_remove_all(self):
        body = _engine().build_body(
            _cfg(),
            _req(text="<surprised>第一句。</surprised><sad>第二句。</sad>"),
        )
        assert body["input"] == "第一句。第二句。"
        assert "吃惊" in body["options"]["instruct"]
        assert "悲伤" not in body["options"]["instruct"]

    def test_native_wrappers_are_left_alone(self):
        body = _engine().build_body(
            _cfg(), _req(text="<laughter>他停下脚步。</laughter>")
        )
        assert body["input"] == "<laughter>他停下脚步。</laughter>"
        assert "instruct" not in body["options"]

    def test_laughing_is_normalized_to_laughter(self):
        body = _engine().build_body(
            _cfg(), _req(text="<laughing>他停下脚步。</laughing>")
        )
        assert body["input"] == "<laughter>他停下脚步。</laughter>"

    def test_a_wrapper_only_text_is_refused(self):
        with pytest.raises(EngineError, match="只有包裹标签"):
            _engine().build_body(_cfg(), _req(text="<surprised>   </surprised>"))

    def test_direct_wrappers_use_the_official_instruct_sentences(self):
        cases = {
            "fast": "请用尽可能快地语速说一句话。",
            "slow": "请用尽可能慢地语速说一句话。",
            "peppa": "我想体验一下小猪佩奇风格，可以吗？",
            "robot": "你可以尝试用机器人的方式解答吗？",
        }
        for tag, expected in cases.items():
            body = _engine().build_body(
                _cfg(), _req(text=f"<{tag}>他停下脚步。</{tag}>")
            )
            assert body["input"] == "他停下脚步。"
            assert body["options"]["instruct"] == expected

    def test_a_direct_wrapper_combines_with_an_emotion(self):
        body = _engine().build_body(
            _cfg(), _req(text="<fast>他停下脚步。</fast>", emotion="开心")
        )
        instruct = body["options"]["instruct"]
        assert instruct.startswith("请用尽可能快地语速说一句话，")
        assert "明亮" in instruct

    def test_an_explicit_instruction_wins_over_a_direct_wrapper(self):
        body = _engine().build_body(
            _cfg(),
            _req(text="<fast>他停下脚步。</fast>", extra={"instruction": "轻声"}),
        )
        assert body["input"] == "他停下脚步。"
        assert body["options"]["instruct"] == "轻声"


class TestMarks:
    def test_native_tags_are_mapped(self):
        table = _engine().mark_map()
        assert table["笑"] == "[laughter]"
        assert table["咳嗽"] == "[cough]"
        assert table["叹气"] == "[sigh]"
        assert table["换气"] == "[breath]"

    def test_prepare_text_translates_and_strips(self):
        engine = _engine()
        assert engine.prepare_text("他[[笑]]。") == "他[laughter]。"
        # 这个运行时没有 [gasp] / [sneeze] 之类的标签：不认的整段删掉。
        assert engine.prepare_text("他[[喷嚏]]。") == "他。"

    def test_the_tags_are_visible_to_the_ui(self):
        rows = {row["name"]: row["native"] for row in _engine().describe()["marks"]}
        assert rows["笑"] == "[laughter]"


class TestSeed:
    def test_resolve_reports_the_value(self):
        assert resolve_seed(7) == (7, "seed=7")

    def test_resolve_draws_below_zero(self):
        value, note = resolve_seed(-1)
        assert 0 <= value < 2**31
        assert note == f"seed={value}"


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
        assert fake_http.calls[0]["json"]["model"] == "cosyvoice3"

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
            json_body={"error": {"message": "unknown model id: cosyvoice3"}},
        )
        with pytest.raises(EngineError, match="GET /v1/models"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_non_audio_response_is_refused(self, fake_http):
        fake_http.response = _FakeResponse(content=b"<html>", content_type="text/html")
        with pytest.raises(EngineError, match="不是音频"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_connect_error_names_the_service(self, fake_http):
        fake_http.raise_error = httpx.ConnectError("refused")
        with pytest.raises(EngineError, match=r"CosyVoice3（.*）无法连接"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))
