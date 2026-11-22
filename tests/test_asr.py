"""识别：**插件契约 + 调度 + Qwen3-ASR / SenseVoice 两家**。

识别从"下载一档模型并进程内推理"改成了"一类插件"（见 ``app/engines/asr_base``）。
这一份测试要守住四件事，每一件都是"错了也不报错"的那种：

1. **核心不认识任何一家**：``usecase/asr.py`` 只调 ``transcribe`` / ``readiness``，
   换引擎 = 换插件，这里的一行代码都不用动。
2. **"没配好"要说成一句人能懂的话**，而不是空 200 或 KeyError。
3. **当前那台**：手动指定来自 ``config.asr_engine``；未指定时按 key 顺序取 audio.cpp
   上运行中的识别服务，没有则回落第一台；应用层选项（自动识别 / 写回 / 录音转写）
   单独存、单独改。
4. **Qwen3-ASR 那一家**的请求形状与响应解析：音频总是 multipart 上传，非 WAV
   先转 16 kHz 单声道；``details`` 给语言、普通端点不给。

真的识别要连本机那台 Qwen3-ASR 服务（默认 18999），测试里不拉它进来：
``_post`` 换成桩，验的是"我们到底发了什么、拿回来的怎么解释"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from app.engines import asr_http
from app.engines.asr_base import AsrCapabilities, AsrEngine, AsrError, Transcript
from app.engines.base import ui_hints
from app.engines.builtin.qwen3_asr import client as qwen3
from app.engines.builtin.qwen3_asr.settings import Qwen3AsrSettings
from app.engines.builtin.sense_asr.settings import SenseAsrSettings
from app.infra.config import load_config, save_config
from app.usecase import asr


class _StubSettings(BaseModel):
    label: str = Field(default="本地", json_schema_extra=ui_hints("名字"))
    api_key: str = Field(default="", json_schema_extra=ui_hints("密钥", secret=True))


class _StubEngine(AsrEngine):
    """一台假的识别引擎：只回答一件事——"谁在调我、给了什么"。"""

    key = "stub_asr"
    label = "桩识别"
    vendor = "测试"

    seen: ClassVar[list[Path]] = []
    text = "听出来的话。"
    language = "Chinese"
    failure = ""

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return _StubSettings

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities(languages="中 / 英", returns_language=True, local=True)

    def readiness(self, cfg: BaseModel) -> str:
        if not str(getattr(cfg, "label", "") or "").strip():
            return "还没填名字。"
        return ""

    def transcribe(self, cfg: BaseModel, path: Path) -> Transcript:
        if self.failure:
            raise AsrError(self.failure)
        type(self).seen.append(Path(path))
        return Transcript(text=self.text, language=self.language, seconds=0.12, model=self.key)


@pytest.fixture()
def stub(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> _StubEngine:
    """把注册表换成只有这一台假引擎（插件发现本身另有测试）。

    **必须带 ``data_dir``**：这一份里有写配置的用例，落到真实配置目录就会污染
    用户那台机器（''stub_asr'' 会一直留在 config.json 里）。
    """
    engine = _StubEngine()
    _StubEngine.seen = []
    _StubEngine.failure = ""
    monkeypatch.setattr(asr, "list_asr_engines", lambda: {engine.key: engine})
    return engine


def _audio(tmp_path: Path, *, name: str = "a.wav", head: bytes = b"RIFF-fake") -> Path:
    target = tmp_path / name
    target.write_bytes(head)
    return target


# --------------------------------------------------------------------------- #
# 调度：选哪台、怎么听、失败了说什么
# --------------------------------------------------------------------------- #


class TestDispatch:
    def test_a_single_engine_is_current_without_choosing(self, stub: _StubEngine) -> None:
        assert asr.current_key() == "stub_asr"
        assert asr.available() is True

    def test_auto_picks_the_running_engine_in_key_order(
        self, monkeypatch: pytest.MonkeyPatch, data_dir: Path
    ) -> None:
        """未指定时按 key 顺序取开着的；两台都开着取前一台。"""

        class _Other(_StubEngine):
            key = "another_asr"

        first, second = _Other(), _StubEngine()
        monkeypatch.setattr(
            asr, "list_asr_engines", lambda: {first.key: first, second.key: second}
        )
        config = load_config()
        config.asr_engine = ""
        save_config(config)
        monkeypatch.setattr(
            asr,
            "live_engine_keys",
            lambda keys: {second.key: {"id": "x", "family": second.key, "url": ""}},
        )
        assert asr.current_key() == second.key
        monkeypatch.setattr(
            asr, "live_engine_keys", lambda keys: {first.key: {}, second.key: {}}
        )
        assert asr.current_key() == first.key

    def test_auto_falls_back_when_nothing_is_running(
        self, monkeypatch: pytest.MonkeyPatch, data_dir: Path
    ) -> None:
        """没有运行实例时退回第一台，手动服务不因此判死。"""

        class _Other(_StubEngine):
            key = "another_asr"

        first, second = _Other(), _StubEngine()
        monkeypatch.setattr(
            asr, "list_asr_engines", lambda: {first.key: first, second.key: second}
        )
        config = load_config()
        config.asr_engine = ""
        save_config(config)
        monkeypatch.setattr(asr, "live_engine_keys", lambda keys: {})
        assert asr.current_key() == first.key

    def test_manual_choice_wins_over_running_state(
        self, stub: _StubEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """手动指定就是指定，不被自动探测覆盖。"""
        config = load_config()
        config.asr_engine = "stub_asr"
        save_config(config)
        monkeypatch.setattr(asr, "live_engine_keys", lambda keys: {})
        assert asr.current_key() == "stub_asr"

    def test_status_reports_auto_mode(
        self, stub: _StubEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(asr, "live_engine_keys", lambda keys: {})
        config = load_config()
        config.asr_engine = ""
        save_config(config)
        assert asr.status()["auto"] is True
        config.asr_engine = "stub_asr"
        save_config(config)
        assert asr.status()["auto"] is False

    def test_the_configured_engine_wins(self, stub: _StubEngine) -> None:
        config = load_config()
        config.asr_engine = "gone"
        save_config(config)
        # 配的那台已经不在了 → 回落第一台，而不是"什么都不用"
        assert asr.current_key() == "stub_asr"

    def test_transcribe_passes_the_file_and_reports_language(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        result = asr.transcribe_file(_audio(tmp_path))
        assert result["text"] == "听出来的话。"
        assert result["language"] == "Chinese"
        assert result["model"] == "stub_asr"
        assert _StubEngine.seen, "得真的把路径交给插件"

    def test_no_engine_at_all_says_where_to_configure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(asr, "list_asr_engines", dict)
        with pytest.raises(AsrError) as caught:
            asr.transcribe_file(_audio(tmp_path))
        assert "引擎 → 识别" in str(caught.value)
        assert caught.value.code == "asr_not_installed"

    def test_an_unconfigured_engine_says_what_is_missing(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        config = load_config()
        config.engines["stub_asr"] = {"label": ""}
        save_config(config)
        with pytest.raises(AsrError, match="还没填名字"):
            asr.transcribe_file(_audio(tmp_path))

    def test_a_missing_file_is_refused_before_the_engine(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        with pytest.raises(AsrError, match="不存在"):
            asr.transcribe_file(tmp_path / "nope.wav")
        assert not _StubEngine.seen

    def test_an_engine_failure_keeps_its_own_words(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        _StubEngine.failure = "服务没起。"
        with pytest.raises(AsrError, match="服务没起"):
            asr.transcribe_file(_audio(tmp_path))

    def test_the_status_carries_values_and_readiness(self, stub: _StubEngine) -> None:
        payload = asr.status()
        row = payload["engines"][0]
        assert row["key"] == "stub_asr"
        assert row["ready"] is True
        assert row["values"]["label"] == "本地"
        assert payload["ready"] is True
        # 机密字段读出来是空串（掩码），但它的存在要如实说
        assert row["values"]["api_key"] == ""


# --------------------------------------------------------------------------- #
# 补逐字稿那条路：同一段录音只听一次
# --------------------------------------------------------------------------- #


class TestReuseForReference:
    """合成本身是**按行**的，同一段参考音频每行都要问一次文本。

    自动写回关闭、或音色不在库中（写不回去）时，"样例的说话内容为空"每行都成立，
    于是同一段录音被听上千遍——一次几秒。这里钉的是"一次运行里只真听一次"，
    以及手动入口（``transcribe_file``）不受影响。
    """

    def test_the_same_audio_is_heard_once(self, stub: _StubEngine, tmp_path: Path) -> None:
        source = _audio(tmp_path)
        first = asr.transcribe_for_reference(source, reason="样例缺逐字稿")
        second = asr.transcribe_for_reference(source, reason="样例缺逐字稿")
        assert len(_StubEngine.seen) == 1, "同一份音频不该被听第二遍"
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["text"] == first["text"]

    def test_a_changed_audio_is_heard_again(self, stub: _StubEngine, tmp_path: Path) -> None:
        """样例换了一段录音（大小或时间变了）就是另一件事，不能拿旧文本顶上。"""
        source = _audio(tmp_path)
        asr.transcribe_for_reference(source)
        source.write_bytes(b"RIFF-fake-but-longer")
        again = asr.transcribe_for_reference(source)
        assert len(_StubEngine.seen) == 2
        assert again["cached"] is False

    def test_a_changed_engine_config_is_heard_again(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        """换了模型（同一台插件的另一套配置）也要重听：文本会不一样。"""
        source = _audio(tmp_path)
        asr.transcribe_for_reference(source)
        asr.save_settings("stub_asr", {"label": "换了模型"})
        again = asr.transcribe_for_reference(source)
        assert len(_StubEngine.seen) == 2
        assert again["cached"] is False

    def test_an_empty_result_is_not_reused(
        self, stub: _StubEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没听出字（可能不含人声）不能记成"已经听过"：下次还得再试。"""
        # 用 monkeypatch 而不是直接改类属性：类属性会漏到后面的用例（它们默认能听出字）。
        monkeypatch.setattr(_StubEngine, "text", "   ")
        source = _audio(tmp_path)
        asr.transcribe_for_reference(source)
        asr.transcribe_for_reference(source)
        assert len(_StubEngine.seen) == 2

    def test_the_manual_entry_always_really_listens(
        self, stub: _StubEngine, tmp_path: Path
    ) -> None:
        """用户点"识别"就该真听一遍：那条路走 ``transcribe_file``，不碰这个中转。"""
        source = _audio(tmp_path)
        asr.transcribe_for_reference(source)
        asr.transcribe_file(source)
        assert len(_StubEngine.seen) == 2


# --------------------------------------------------------------------------- #
# 设置：掩码 / 清空 / 恢复默认（与合成引擎同一套语义）
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_a_blank_secret_means_keep_it(self, stub: _StubEngine) -> None:
        asr.save_settings("stub_asr", {"api_key": "sk-1"})
        asr.save_settings("stub_asr", {"api_key": ""})
        row = asr.status()["engines"][0]
        assert row["secrets_set"] == ["api_key"]

    def test_clear_removes_the_secret(self, stub: _StubEngine) -> None:
        asr.save_settings("stub_asr", {"api_key": "sk-1"})
        row = asr.save_settings("stub_asr", {"_clear": ["api_key"]})["engines"][0]
        assert row["secrets_set"] == []

    def test_reset_returns_to_defaults_but_keeps_secrets(self, stub: _StubEngine) -> None:
        asr.save_settings("stub_asr", {"label": "改过", "api_key": "sk-1"})
        row = asr.save_settings("stub_asr", {"_reset": True})["engines"][0]
        assert row["values"]["label"] == "本地"
        assert row["secrets_set"] == ["api_key"]

    def test_an_unknown_engine_is_refused(self, stub: _StubEngine) -> None:
        with pytest.raises(AsrError) as caught:
            asr.save_settings("nope", {"label": "x"})
        assert caught.value.code == "engine_not_found"

    def test_choosing_an_unknown_engine_is_refused(self, stub: _StubEngine) -> None:
        with pytest.raises(AsrError, match="stub_asr"):
            asr.set_current("nope")


# --------------------------------------------------------------------------- #
# 应用层选项：默认全开，改一个不动另一个
# --------------------------------------------------------------------------- #


class TestOptions:
    def test_defaults_are_all_on(self, stub: _StubEngine) -> None:
        assert asr.options().model_dump() == {
            "auto_transcript": True,
            "auto_on_connect": False,
            "writeback": True,
            "transcribe_recordings": True,
            "language_writeback": True,
        }

    def test_a_partial_patch_changes_only_that_key(self, stub: _StubEngine) -> None:
        asr.save_options({"auto_transcript": False})
        saved = asr.options()
        assert saved.auto_transcript is False
        assert saved.writeback is True
        assert saved.transcribe_recordings is True
        assert saved.language_writeback is True

    def test_status_carries_the_options(self, stub: _StubEngine) -> None:
        payload = asr.save_options({"writeback": False})
        assert payload["options"]["writeback"] is False
        assert asr.status()["options"]["writeback"] is False

    def test_options_round_trip_over_http(self, client) -> None:
        resp = client.put("/api/v1/asr/options", json={"auto_transcript": False})
        assert resp.status_code == 200
        assert resp.json()["options"]["auto_transcript"] is False
        status = client.get("/api/v1/asr").json()
        assert status["options"] == {
            "auto_transcript": False,
            "auto_on_connect": False,
            "writeback": True,
            "transcribe_recordings": True,
            "language_writeback": True,
        }

    def test_language_writeback_round_trips_over_http(self, client) -> None:
        resp = client.put("/api/v1/asr/options", json={"language_writeback": False})
        assert resp.status_code == 200
        assert resp.json()["options"]["language_writeback"] is False


# --------------------------------------------------------------------------- #
# 语种写回：主样例 + 只填空 + 不覆盖
# --------------------------------------------------------------------------- #


class TestLanguageWriteback:
    """语种按样例识别，音色的 ``language`` 却只有一个格子。

    所以只有"主样例 + 音色还没语种"时才写：非主样例只回显，已有值永不覆盖。
    """

    def _hutao(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        return next(v for v in service.list_voices() if v.name == "胡桃")

    def test_the_main_sample_fills_an_empty_voice(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        voice = self._hutao(service, demo_library)
        assert voice.main_sample == "默认.wav"
        result = asr.transcribe(service, voice.id, voice.main_sample)
        assert result["language_written"] is True
        assert result["voice_language"] == "zh"
        assert service.get_voice(voice.id).language == "zh"

    def test_the_setting_can_switch_language_writeback_off(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        """关掉语种写回：识别照样回报语种，但不碰音色那一格。"""
        asr.save_options({"language_writeback": False})
        voice = self._hutao(service, demo_library)
        result = asr.transcribe(service, voice.id, voice.main_sample)
        assert result["language"] == "Chinese"
        assert result["language_written"] is False
        assert result["voice_language"] == ""
        assert service.get_voice(voice.id).language == ""

    def test_a_non_main_sample_is_not_written(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        voice = self._hutao(service, demo_library)
        result = asr.transcribe(service, voice.id, "开心.wav")
        assert result["language_written"] is False
        assert result["voice_language"] == ""
        assert service.get_voice(voice.id).language == ""

    def test_an_existing_language_is_never_overwritten(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        voice = self._hutao(service, demo_library)
        service.update_voice(voice.id, {"language": "ja"})
        result = asr.transcribe(service, voice.id, voice.main_sample)
        assert result["language_written"] is False
        assert result["voice_language"] == "ja"
        assert service.get_voice(voice.id).language == "ja"

    def test_an_unknown_language_is_not_guessed(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        stub.language = ""
        voice = self._hutao(service, demo_library)
        result = asr.transcribe(service, voice.id, voice.main_sample)
        assert result["language_written"] is False
        assert service.get_voice(voice.id).language == ""


# --------------------------------------------------------------------------- #
# Qwen3-ASR 那一家：发什么、怎么解释回来的东西
# --------------------------------------------------------------------------- #


class TestQwen3Client:
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_post(
            url: str, body: bytes, content_type: str, timeout: float, **_kwargs: object
        ) -> bytes:
            calls.append({"url": url, "body": body, "type": content_type, "timeout": timeout})
            return json.dumps(
                {"text": "你好。", "language": "Chinese", "timing": {"rtf": 0.1}},
                ensure_ascii=False,
            ).encode()

        monkeypatch.setattr(asr_http, "_post", fake_post)
        return calls

    def test_audio_always_goes_out_as_multipart_wav(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """没有"本机路径 / 上传"二选一：上传在本地与局域网都成立。"""
        calls = self._capture(monkeypatch)
        cfg = Qwen3AsrSettings(base_url="http://127.0.0.1:18999/")
        result = qwen3.transcribe(cfg, _audio(tmp_path))

        assert calls[0]["url"] == "http://127.0.0.1:18999/v1/audio/transcriptions/details"
        assert calls[0]["type"].startswith("multipart/form-data; boundary=")
        assert b'name="model"' in calls[0]["body"]
        assert b'filename="audio.wav"' in calls[0]["body"]
        assert b"Content-Type: audio/wav" in calls[0]["body"]
        assert result.text == "你好。"
        assert result.language == "Chinese"

    def test_details_off_uses_the_plain_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._capture(monkeypatch)
        qwen3.transcribe(Qwen3AsrSettings(details=False), _audio(tmp_path))
        assert calls[0]["url"].endswith("/v1/audio/transcriptions")

    def test_a_non_wav_is_converted_before_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """服务端核心只收 WAV；mp3/flac 的解码前端不一定开，所以统一先转。"""
        from conftest import make_wav

        source = _audio(tmp_path, name="素材.mp3", head=b"ID3" + b"\x00" * 64)
        converted = tmp_path / "素材-转好的.wav"
        make_wav(converted)
        seen: list[Path] = []

        def fake_convert(path: Path, **_kwargs: object) -> Path:
            seen.append(Path(path))
            return converted

        monkeypatch.setattr(asr_http.ffmpeg, "to_wav", fake_convert)
        calls = self._capture(monkeypatch)
        result = qwen3.transcribe(Qwen3AsrSettings(), source)

        assert seen == [source]
        assert converted.name.encode() not in calls[0]["body"], "发出去的是内容，不是路径"
        assert converted.exists() is False, "一次性转码：识别完就删"
        assert b'filename="audio.wav"' in calls[0]["body"]
        assert result.extra["transcoded_from"] == "mp3"

    def test_a_wav_is_not_converted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from conftest import make_wav

        def boom(*_args: object, **_kwargs: object) -> Path:
            raise AssertionError("已经是 WAV 了，不该再转")

        monkeypatch.setattr(asr_http.ffmpeg, "to_wav", boom)
        target = tmp_path / "真.wav"
        make_wav(target)
        self._capture(monkeypatch)
        result = qwen3.transcribe(Qwen3AsrSettings(), target)
        assert result.extra == {}

    def test_a_language_hint_is_sent_only_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = self._capture(monkeypatch)
        qwen3.transcribe(Qwen3AsrSettings(language="ja"), _audio(tmp_path))
        assert b"\r\n\r\nja\r\n" in calls[0]["body"]

    def test_plain_response_has_no_language(self) -> None:
        """普通端点只回 text 与 timing——**不猜**语种。"""
        result = qwen3.parse_transcript({"text": "hi", "timing": {"rtf": 0.1}})
        assert result.text == "hi"
        assert result.language == ""

    def test_an_empty_url_is_refused_with_a_readable_sentence(self, tmp_path: Path) -> None:
        with pytest.raises(AsrError, match="服务地址为空"):
            qwen3.transcribe(Qwen3AsrSettings(base_url=""), _audio(tmp_path))

    def test_upstream_error_message_is_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        payload = json.dumps({"error": {"message": "unknown model id: x"}}).encode()

        class _Boom(urllib.error.HTTPError):
            """带上游错误体的 400。``fp=None``：给了文件对象会留下未关闭的临时文件。"""

            def __init__(self) -> None:
                super().__init__("u", 400, "Bad Request", {}, None)

            def read(self, *_args, **_kwargs) -> bytes:
                return payload

        def fake_open(*_args, **_kwargs):
            raise _Boom()

        monkeypatch.setattr(asr_http._OPENER, "open", fake_open)
        target = Path(__file__).with_name("does-not-matter.wav")
        target.write_bytes(b"RIFF-fake")
        try:
            with pytest.raises(AsrError, match="unknown model id: x"):
                qwen3.transcribe(Qwen3AsrSettings(), target)
        finally:
            target.unlink(missing_ok=True)

    def test_probe_lists_the_models_it_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"data": [{"id": "qwen3_asr"}, {"id": "breeze-tts"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(asr_http._OPENER, "open", lambda *_a, **_k: _Resp())
        ok, message = qwen3.probe(Qwen3AsrSettings())
        assert ok is True
        assert "qwen3_asr" in message

    def test_probe_notices_a_wrong_model_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"data": [{"id": "breeze-tts"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(asr_http._OPENER, "open", lambda *_a, **_k: _Resp())
        ok, message = qwen3.probe(Qwen3AsrSettings(model="qwen3_asr"))
        assert ok is False
        assert "qwen3_asr" in message

    def test_the_default_address_is_the_instance(self) -> None:
        assert Qwen3AsrSettings().base_url == "http://127.0.0.1:18999"

    def test_probe_asks_the_configured_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """「测试连接」打的是「服务地址」那台，不是写死的 hub。"""
        seen: list[str] = []

        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"data": [{"id": "qwen3_asr"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_open(url: str, *_args: object, **_kwargs: object) -> _Resp:
            seen.append(url)
            return _Resp()

        monkeypatch.setattr(asr_http._OPENER, "open", fake_open)
        ok, _message = qwen3.probe(Qwen3AsrSettings(base_url="http://127.0.0.1:18999"))
        assert ok is True
        assert seen == ["http://127.0.0.1:18999/v1/models"]


class TestSenseVoiceClient:
    """SenseVoice 与 Qwen 共用线协议；这里守住它自己的默认值与回报形状。"""

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_post(
            url: str, body: bytes, content_type: str, timeout: float, **_kwargs: object
        ) -> bytes:
            calls.append({"url": url, "body": body, "type": content_type, "timeout": timeout})
            return json.dumps(
                {"text": "你好。", "language": "zh", "timing": {"rtf": 0.1}},
                ensure_ascii=False,
            ).encode()

        monkeypatch.setattr(asr_http, "_post", fake_post)
        return calls

    def test_defaults_point_at_the_audio_cpp_instance(self) -> None:
        conf = SenseAsrSettings()
        assert conf.base_url == "http://127.0.0.1:18102"
        assert conf.model == "sense-asr"

    def test_details_carries_the_language_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from app.engines.registry import list_asr_engines

        calls = self._capture(monkeypatch)
        engine = list_asr_engines()["sense_asr"]
        result = engine.transcribe(SenseAsrSettings(), _audio(tmp_path))

        assert calls[0]["url"] == "http://127.0.0.1:18102/v1/audio/transcriptions/details"
        assert b"sense-asr" in calls[0]["body"]
        assert result.text == "你好。"
        assert result.language == "zh"

    def test_capabilities_do_not_promise_emotion_or_events(self) -> None:
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["sense_asr"].capabilities()
        assert caps.returns_language is True
        assert caps.returns_emotion is False
        assert caps.returns_events is False
        assert caps.timestamps is False

    def test_the_language_catalog_matches_the_audio_cpp_spec(self) -> None:
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["sense_asr"].capabilities()
        assert caps.languages == "21 种语言"
        assert len(caps.language_groups) == 1
        assert len(caps.language_groups[0].items) == 21
        assert caps.language_groups[0].items[0].code == "zh"


# --------------------------------------------------------------------------- #
# 插件本身：注册表能扫到，能力别许愿
# --------------------------------------------------------------------------- #


class TestPlugin:
    def test_the_plugin_is_discovered(self) -> None:
        from app.engines.registry import list_asr_engines

        assert "qwen3_asr" in list_asr_engines()

    def test_it_does_not_promise_emotion(self) -> None:
        """当今没有识别引擎给情绪（Qwen3-ASR 只出文本 + 语种）。"""
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["qwen3_asr"].capabilities()
        assert caps.returns_language is True
        assert caps.returns_emotion is False

    def test_it_does_not_promise_events_or_timestamps(self) -> None:
        """声音事件与逐词时间轴不是这台模型的任务；不声明就不在界面上假装有。"""
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["qwen3_asr"].capabilities()
        assert caps.returns_events is False
        assert caps.timestamps is False

    def test_the_language_catalog_matches_the_official_counts(self) -> None:
        """30 种语言 + 22 种中文方言，逐项登记；方言没有独立语言代码。"""
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["qwen3_asr"].capabilities()
        assert caps.languages == "30 种语言 · 22 种中文方言"
        assert len(caps.language_groups) == 2
        languages, dialects = caps.language_groups
        assert len(languages.items) == 30
        assert len(dialects.items) == 22
        assert languages.items[0].code == "zh"
        assert any(item.name == "四川话" for item in dialects.items)
        assert all(item.code == "" for item in dialects.items)

    def test_describe_lists_every_capability_and_language_group(self) -> None:
        """能力全表与语言清单都由插件下发：界面不认识任何一家模型。"""
        from app.engines.registry import list_asr_engines

        payload = list_asr_engines()["qwen3_asr"].describe()
        assert [row["key"] for row in payload["returns"]] == [
            "text",
            "language",
            "emotion",
            "events",
            "timestamps",
        ]
        assert [row["has"] for row in payload["returns"]] == [
            True,
            True,
            False,
            False,
            False,
        ]
        assert [group["key"] for group in payload["language_groups"]] == [
            "languages",
            "dialects",
        ]
        assert len(payload["language_groups"][0]["items"]) == 30
        assert len(payload["language_groups"][1]["items"]) == 22

    def test_it_declares_input_formats_and_streaming(self) -> None:
        from app.engines.registry import list_asr_engines

        caps = list_asr_engines()["qwen3_asr"].capabilities()
        assert caps.formats == ("wav",)
        assert caps.streaming is False
        assert "SSE" in caps.streaming_note

    def test_there_is_no_send_setting(self) -> None:
        """发送方式是实现细节：音频总由项目上传，用户不需要二选一。"""
        assert "send" not in Qwen3AsrSettings.model_fields

    def test_the_tts_registry_does_not_pick_it_up(self) -> None:
        """识别插件不混进合成引擎那份表——两套契约，界面上也是两处。"""
        from app.engines.registry import list_engines

        assert "qwen3_asr" not in list_engines()


# --------------------------------------------------------------------------- #
# 连接时自动补逐字稿
# --------------------------------------------------------------------------- #


class TestFillOnConnect:
    """「连接时自动识别」：只挑没有逐字稿的样例，写回后不再重复。

    判据在库里（``samples.text`` 为空），不在"这条是不是本地音色"上：
    平台音色生成出来的样例同样要补。写回走 ``update_sample``，与手工填一格同一条路。
    """

    def test_it_fills_a_missing_transcript_and_stops_repeating(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        service.save_folders([str(demo_library)])
        missing = service.missing_transcripts()
        assert missing, "演示库里应当有没写文字的样例"

        stats = asr.fill_missing(service, limit=1)
        assert stats == {"total": 1, "heard": 1, "written": 1, "failed": 0}
        # 写回之后这条不再出现在待补清单里：下一轮连接不会重听同一段。
        assert len(service.missing_transcripts()) == len(missing) - 1

        voice_id, file = missing[0]
        samples = service.get_voice(voice_id).samples
        assert next(item for item in samples if item.file == file).text == _StubEngine.text

    def test_samples_that_already_have_text_are_not_listed(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        service.save_folders([str(demo_library)])
        # 胡桃的「开心.wav」旁边有 开心.txt，文字已经在库里
        assert "开心.wav" not in {file for _voice, file in service.missing_transcripts()}

    def test_the_switch_gates_the_background_run(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        service.save_folders([str(demo_library)])
        asr.save_options({"auto_on_connect": False})
        assert asr.maybe_fill(service) is False
        # 关闭时一条都不该动
        assert service.missing_transcripts()

    def test_it_does_not_start_when_the_engine_is_not_ready(
        self, stub: _StubEngine, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """引擎未就绪（设置页显示「未就绪」）就不起任务：与那一处的判据同一个。"""
        service.save_folders([str(demo_library)])
        asr.save_options({"auto_on_connect": True})
        monkeypatch.setattr(_StubEngine, "readiness", lambda self, cfg: "还没填服务地址。")
        assert asr.unavailable_reason() == "桩识别 尚未就绪：还没填服务地址。"
        assert asr.maybe_fill(service) is False
        assert service.missing_transcripts()

    def test_it_does_not_fill_when_the_service_is_not_running(
        self, stub: _StubEngine, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置齐了但服务没起：探一次就收手，不把整批请求挨个发出去。"""
        service.save_folders([str(demo_library)])
        asr.save_options({"auto_on_connect": True})
        monkeypatch.setattr(_StubEngine, "probe", lambda self, cfg: (False, "连不上"))
        assert asr.maybe_fill(service) is True
        with asr._fill_lock:  # 拿到这把锁 = 后台那轮已经收尾
            pass
        assert service.missing_transcripts()
        assert _StubEngine.seen == []
        # 服务没起时没有一条被听：进度不该报"在跑"，界面因此不亮 toast。
        assert asr.fill_state()["running"] is False

    def test_a_round_with_nothing_to_do_never_reports_running(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        """库里没有缺文本的样例：线程起了也立刻收手，同样不该亮 toast。"""
        service.save_folders([str(demo_library)])
        asr.save_options({"auto_on_connect": True})
        for voice_id, file in service.missing_transcripts():
            service.update_sample(voice_id, file, {"text": "已经填好了"})
        assert service.missing_transcripts() == []

        assert asr.maybe_fill(service) is True
        with asr._fill_lock:
            pass
        assert asr.fill_state()["running"] is False
        assert _StubEngine.seen == []

    def test_running_lasts_only_while_a_row_is_being_listened(
        self, stub: _StubEngine, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`running` 的判据是"真的在听这一条"：听的时候为真，收尾立刻为假。"""
        service.save_folders([str(demo_library)])
        asr._fill_cancel.clear()
        original = _StubEngine.transcribe
        seen: list[bool] = []

        def transcribe(self: _StubEngine, cfg: Any, path: Path) -> Transcript:
            seen.append(asr.fill_state()["running"])
            return original(self, cfg, path)

        monkeypatch.setattr(_StubEngine, "transcribe", transcribe)
        with asr._fill_lock:
            asr.fill_all(service)
            assert asr.fill_state()["running"] is False
        assert seen and all(seen)

    def test_the_option_round_trips_over_http(self, client) -> None:
        resp = client.put("/api/v1/asr/options", json={"auto_on_connect": True})
        assert resp.status_code == 200
        assert resp.json()["options"]["auto_on_connect"] is True
        assert client.get("/api/v1/asr").json()["options"]["auto_on_connect"] is True




    def test_it_keeps_filling_until_nothing_is_missing(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        """不是补一批就停：一直补到库里没有缺文本的样例为止。"""
        service.save_folders([str(demo_library)])
        asr._fill_cancel.clear()
        missing = len(service.missing_transcripts())
        assert missing > 1  # 一批之内补不完，才看得出"继续"

        # 上一格（HTTP 保存设置）可能起了一轮后台补全：它会重置进度计数，
        # 与本轮重叠时 `done` 会被清零。先占住那把锁，等它收尾再自己跑。
        with asr._fill_lock:
            stats = asr.fill_all(service)
            assert stats["written"] == missing
            assert stats["failed"] == 0
            assert service.missing_transcripts() == []
            assert asr.fill_state()["done"] == missing

    def test_a_stop_request_ends_the_round_before_the_next_file(
        self, stub: _StubEngine, service, demo_library: Path
    ) -> None:
        service.save_folders([str(demo_library)])
        asr._fill_cancel.set()
        stats = asr.fill_missing(service, stop=asr._fill_cancel)
        assert stats["written"] == 0
        assert service.missing_transcripts(), "置位后一条都不该动"
        asr._fill_cancel.clear()

    def test_cancel_is_safe_when_nothing_is_running(self) -> None:
        # 不判 running：其他用例可能刚起过后台线程，锁的状态不该由这一条守。
        asr._fill_cancel.clear()
        state = asr.cancel_fill()
        assert set(state) == {"running", "starting", "done", "failed", "total"}
        asr._fill_cancel.clear()

    def test_fill_endpoints_report_and_cancel(self, client) -> None:
        # 计数字段跨用例共享（模块级那一轮），这里只守契约形状。
        body = client.get("/api/v1/asr/fill").json()
        assert body["object"] == "asr_fill"
        assert {"running", "done", "failed", "total"} <= set(body)
        assert client.post("/api/v1/asr/fill/cancel").json()["object"] == "asr_fill"

