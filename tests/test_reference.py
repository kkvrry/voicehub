"""参考音频的准备：**发出去之前**该转的转、该听的听。

这一层是"引擎声明什么，中转站就替它办什么"，所以每个用例都是同一句话的两半：
声明（``SynthesisSpec``）+ 现场（那条录音长什么样），然后看**实际发出去的那个请求**。

转换与识别都**打桩**：真起 ffmpeg、真加载几 GB 的识别模型都不该是单测的事
（真转换那一条在 ``test_ffmpeg.py`` 里，用真 ffmpeg 跑）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engines.base import EngineError, EnginePlugin, SynthesisRequest, SynthesisSpec
from app.infra import ffmpeg
from app.usecase import asr as asr_use
from app.usecase import reference as reference_mod
from app.usecase.gateway import synthesize
from app.usecase.reference import prepare


def _items(resp):
    """集合端点统一信封：取 data 段（兼容仍返回裸数组的旧形状）。"""
    payload = resp.json()
    return payload["data"] if isinstance(payload, dict) and "data" in payload else payload



def _engine(
    *,
    formats: tuple[str, ...] = (),
    transcript: bool = False,
    duration: tuple[float, float] | None = None,
    path_ascii: bool = False,
) -> EnginePlugin:
    """一台只声明参考音频要求的引擎——别的都不参合。"""

    class Spec(EnginePlugin):
        key = "spec_engine"
        label = "声明引擎"

        def synthesis_spec(self) -> SynthesisSpec:
            return SynthesisSpec(
                requires=("reference_audio",),
                reference_formats=formats,
                needs_transcript=transcript,
                reference_duration=duration,
                reference_path_ascii=path_ascii,
            )

    return Spec()


@pytest.fixture(autouse=True)
def _asr_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """自动识别的前置检测默认通过：本层只回答"要不要听"，引擎起没起另有测试。

    不打这个桩，每个用例都会去问默认地址上那台识别服务，结果随环境飘。
    """
    monkeypatch.setattr(asr_use, "unavailable_reason", lambda: "")
    monkeypatch.setattr(asr_use, "reachable", lambda: (True, ""))


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch):
    """把两件外设换成可数的替身：**调了几次、拿到的路径是什么**都记下来。

    用"没人叫过它"来钉"不该做的时候一件事都没做"——比只看结果强：
    结果是同一份，但也可能白转了一道（那件事用户看不见，只是每次都慢半秒）。
    """
    calls: dict[str, list[Path]] = {"convert": [], "listen": []}

    def convert(path: Path, **kwargs: object) -> Path:
        calls["convert"].append(path)
        return path.with_name(f"{path.stem}-转好的.wav")

    def listen(path: Path, **_kw: object):
        calls["listen"].append(path)
        return {"text": "某个角度说，的确。", "model": "sensevoice", "seconds": 1.0}

    monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", convert)
    monkeypatch.setattr(reference_mod.asr_use, "transcribe_for_reference", listen)
    return calls


def _mp3(tmp_path: Path) -> Path:
    """一条"是 mp3"的文件：这一层只认文件头，所以 ``ID3`` 开头就够。

    （真 mp3 的编码在 ``test_ffmpeg.py`` 里由 ffmpeg 自己造。）
    """
    path = tmp_path / "素材.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 64)
    return path


def _wav(tmp_path: Path) -> Path:
    from conftest import make_wav

    path = tmp_path / "素材.wav"
    make_wav(path)
    return path


# --------------------------------------------------------------------------- #
# 不做的时候一件事都不做
# --------------------------------------------------------------------------- #


class TestNothingToDo:
    @pytest.mark.parametrize("key", ["breeze_tts", "mimo"])
    def test_design_skips_reference_preparation(self, service, spy, key):
        from app.engines.registry import list_engines

        request = SynthesisRequest(
            text="你好。",
            voice_id="旁白",
            reference_audio="missing.mp3",
            extra={"voice_design": "沉稳男声。", "reference_audio": "also-missing.mp3"},
        )
        out, notes = prepare(service, list_engines()[key], request)
        assert out == request
        assert notes == ()
        assert spy == {"convert": [], "listen": []}

    def test_blank_design_does_not_skip_reference_preparation(self, service, tmp_path, spy):
        from app.engines.builtin.breeze_tts.plugin import BreezeTtsEngine

        request = SynthesisRequest(
            text="你好。",
            voice_id="旁白",
            reference_audio=str(_wav(tmp_path)),
            extra={"voice_design": "  "},
        )
        out, _ = prepare(service, BreezeTtsEngine(), request)
        assert out.reference_text == "某个角度说，的确。"
        assert len(spy["listen"]) == 1

    def test_design_is_not_used_by_an_engine_without_the_capability(self, service, tmp_path, spy):
        request = SynthesisRequest(
            text="你好。",
            voice_id="旁白",
            reference_audio=str(_wav(tmp_path)),
            extra={"voice_design": "沉稳男声。"},
        )
        out, _ = prepare(service, _engine(transcript=True), request)
        assert out.reference_text == "某个角度说，的确。"
        assert len(spy["listen"]) == 1

    def test_without_a_reference_audio_the_request_is_untouched(
        self, service, tmp_path: Path, spy
    ) -> None:
        """没有参考音频 = 多半走了另一条路（只有一句描述的"音色设计"）。"""
        engine = _engine(formats=("wav",), transcript=True)
        request = SynthesisRequest(text="你好。", voice_id="某人")
        out, notes = prepare(service, engine, request)
        assert out == request
        assert notes == ()
        assert spy == {"convert": [], "listen": []}

    def test_an_engine_that_declares_nothing_is_left_alone(
        self, service, tmp_path: Path, spy
    ) -> None:
        """**不声明就不动**：别人家的约束不该让所有引擎都多转一道。"""
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_mp3(tmp_path)), reference_text=""
        )
        out, notes = prepare(service, _engine(), request)
        assert out.reference_audio == request.reference_audio
        assert notes == ()
        assert spy == {"convert": [], "listen": []}

    def test_a_wav_is_not_converted_for_a_wav_only_engine(
        self, service, tmp_path: Path, spy
    ) -> None:
        """**转的判据是"实际是什么"，不是"声明了什么"**——已经对的就不转。"""
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        out, notes = prepare(service, _engine(formats=("wav",)), request)
        assert out.reference_audio == request.reference_audio
        assert notes == ()
        assert spy["convert"] == []

    def test_an_existing_transcript_is_not_listened_to_again(
        self, service, tmp_path: Path, spy
    ) -> None:
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(_wav(tmp_path)),
            reference_text="已经填好了的一句话",
        )
        out, notes = prepare(service, _engine(transcript=True), request)
        assert out.reference_text == "已经填好了的一句话"
        assert notes == ()

    def test_auto_transcript_can_be_turned_off(self, service, tmp_path: Path, spy) -> None:
        """关掉自动识别后，缺逐字稿是**明确的配置错误**，不是悄悄什么都不做。"""
        asr_use.save_options({"auto_transcript": False})
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(_wav(tmp_path)),
        )
        with pytest.raises(EngineError, match="已关闭"):
            prepare(service, _engine(transcript=True), request)
        assert spy["listen"] == []


class TestWriteback:
    def test_the_result_stays_out_of_the_sample_when_writeback_is_off(
        self, service, demo_library: Path, spy
    ) -> None:
        from conftest import make_wav

        asr_use.save_options({"writeback": False})
        service.save_folders([str(demo_library)])
        service.scan()
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        target = Path(voice.folder) / "无字.wav"
        make_wav(target)
        service.scan()

        out, notes = prepare(
            service,
            _engine(transcript=True),
            SynthesisRequest(
                text="你好。", voice_id="某人", reference_audio=str(target)
            ),
            library_voice_id=voice.id,
        )
        assert out.reference_text == "某个角度说，的确。"
        assert "自动写回已关闭" in notes[0][1]
        sample = next(s for s in service.get_voice(voice.id).samples if s.file == "无字.wav")
        assert sample.text == ""
        assert len(spy["listen"]) == 1


# --------------------------------------------------------------------------- #
# 转换
# --------------------------------------------------------------------------- #


class TestConversion:
    def test_an_mp3_is_converted_before_it_goes_out(self, service, tmp_path: Path, spy) -> None:
        source = _mp3(tmp_path)
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(source))
        out, notes = prepare(service, _engine(formats=("wav",)), request)
        assert spy["convert"] == [source]
        assert out.reference_audio.endswith("转好的.wav")
        assert len(notes) == 1
        # 那句话说清了"原来是 mp3、引擎只读 wav、已经转过"——三件都要有，
        # 少哪一件用户都得自己猜。
        assert "mp3" in notes[0][1] and "wav" in notes[0][1] and "ffmpeg" in notes[0][1]

    def test_the_unknown_extension_falls_back_to_the_suffix(
        self, service, tmp_path: Path, spy
    ) -> None:
        """嗅探认不出时看后缀——下载来的音频常常是"内容对、后缀是线索"的样子。"""
        path = tmp_path / "怪东西.wav"
        path.write_bytes(b"\x00\x01\x02\x03" * 8)
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(path))
        out, notes = prepare(service, _engine(formats=("wav",)), request)
        assert out.reference_audio == str(path)
        assert notes == ()
        assert spy["convert"] == []

    def test_a_conversion_failure_says_which_engine_and_what_to_do(
        self, service, tmp_path: Path, monkeypatch
    ) -> None:
        def boom(_path: Path) -> Path:
            raise ffmpeg.FfmpegError("这台机器上没找到 ffmpeg")

        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", boom)
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_mp3(tmp_path))
        )
        with pytest.raises(EngineError) as caught:
            prepare(service, _engine(formats=("wav",)), request)
        message = str(caught.value)
        # 报的是"哪台引擎要什么、现在是什么、为什么没做成"——四件事齐了才叫说得清。
        assert "声明引擎" in message and "需要 wav" in message
        assert "mp3" in message and "没找到 ffmpeg" in message

    def test_the_converted_path_is_what_gets_heard(self, service, tmp_path: Path, spy) -> None:
        """要听就听**转好那一份**：引擎读进去的是它，识别也该读它。"""
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_mp3(tmp_path))
        )
        prepare(service, _engine(formats=("wav",), transcript=True), request)
        assert [p.suffix for p in spy["listen"]] == [".wav"]

    def test_it_does_not_claim_to_convert_something_that_is_not_wav(
        self, service, tmp_path: Path, spy
    ) -> None:
        """声明的是多种格式时，里面那一种就算数——不要"不在第一个就转"。"""
        source = _wav(tmp_path)
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(source))
        out, notes = prepare(service, _engine(formats=("wav", "ogg", "aac")), request)
        assert out.reference_audio == str(source)
        assert notes == ()


# --------------------------------------------------------------------------- #
# 逐字稿
# --------------------------------------------------------------------------- #


class TestAsciiPath:
    """引擎读不了非 ASCII 路径时（gsv.cpp 把路径转本地代码页），换一份 ASCII 名再发。"""

    def test_a_chinese_file_name_is_staged_before_it_goes_out(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「素材.wav」这种名字是常态，而 gsv.cpp 拿到它会回 500。"""
        source = _wav(tmp_path)
        staged = tmp_path / "ref-abc123.wav"
        staged.write_bytes(source.read_bytes())
        seen: list[Path] = []

        def stage(path: Path) -> Path:
            seen.append(path)
            return staged

        monkeypatch.setattr(reference_mod.ffmpeg, "stage_ascii_path", stage)
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(source))
        out, notes = prepare(service, _engine(path_ascii=True), request)

        assert seen == [source]
        assert out.reference_audio == str(staged)
        # 换了个文件名不是用户需要知道的事：音色和内容都没变。
        assert notes == ()

    def test_an_ascii_path_is_sent_as_is(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路径本来就全是 ASCII 时不复制——省一次落盘。"""
        from conftest import make_wav

        source = tmp_path / "plain.wav"
        make_wav(source)
        called: list[Path] = []
        monkeypatch.setattr(
            reference_mod.ffmpeg, "stage_ascii_path", lambda path: called.append(path) or path
        )
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(source))
        out, _ = prepare(service, _engine(path_ascii=True), request)

        assert called == []
        assert out.reference_audio == str(source)

    def test_an_engine_that_does_not_declare_it_is_left_alone(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**不声明就不动**：官方 Python 版按 UTF-8 读路径，没有这条约束。"""
        source = _wav(tmp_path)
        called: list[Path] = []
        monkeypatch.setattr(
            reference_mod.ffmpeg, "stage_ascii_path", lambda path: called.append(path) or path
        )
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(source))
        out, _ = prepare(service, _engine(), request)

        assert called == []
        assert out.reference_audio == str(source)

    def test_a_copy_failure_names_the_engine_and_the_reason(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """复制失败时不能装没事发生——那是"为什么这一行一直失败"的唯一线索。"""

        def boom(path: Path) -> Path:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(reference_mod.ffmpeg, "stage_ascii_path", boom)
        request = SynthesisRequest(text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path)))
        with pytest.raises(EngineError, match="声明引擎"):
            prepare(service, _engine(path_ascii=True), request)


class TestTranscript:
    def test_a_missing_transcript_is_heard_and_filled_in(
        self, service, demo_library: Path, spy
    ) -> None:
        service.save_folders([str(demo_library)])
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        # 「难过.wav」在演示库里没有配对的 .txt —— 正是"说话内容空着"的样子。
        service.set_main_sample(voice.id, "难过.wav")
        request = SynthesisRequest(
            text="你好。",
            voice_id=voice.shown_name,
            reference_audio=str(Path(voice.folder) / "难过.wav"),
        )
        out, notes = prepare(service, _engine(transcript=True), request, library_voice_id=voice.id)
        assert out.reference_text == "某个角度说，的确。"
        assert len(notes) == 1
        assert "某个角度说，的确。" in notes[0][1]

    def test_a_reused_transcript_is_announced_only_once(
        self, service, tmp_path: Path, monkeypatch
    ) -> None:
        """同一段录音在一次运行里只真听一次，第二行也不再重复"已转写"那句。

        "样例的说话内容为空"是**每行**都成立的事实（写回关掉时尤其如此），
        说明只该给一次；否则一本书的警告栏会被同一句话刷满。
        """
        listened: list[Path] = []

        def listen(path: Path, **_kw: object):
            listened.append(path)
            return {
                "text": "某个角度说，的确。",
                "model": "stub",
                "seconds": 1.0,
                # 第二次起换成"复用"（真实的转中继在 asr 层，真听的那一次返回 False）。
                "cached": len(listened) > 1,
            }

        monkeypatch.setattr(reference_mod.asr_use, "transcribe_for_reference", listen)
        asr_use.save_options({"writeback": False})
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        engine = _engine(transcript=True)
        _first, first_notes = prepare(service, engine, request)
        second, second_notes = prepare(service, engine, request)
        assert [code for code, _ in first_notes] == ["transcript_inferred"]
        assert second_notes == ()
        assert second.reference_text == "某个角度说，的确。", "文本仍要照常发给引擎"

    def test_the_heard_language_fills_an_empty_voice(
        self, service, demo_library: Path, monkeypatch
    ) -> None:
        """自动补逐字稿时听的语种也按同一条纪律写回：主样例、只填空。"""
        service.save_folders([str(demo_library)])
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(voice.id, "难过.wav")
        monkeypatch.setattr(
            reference_mod.asr_use,
            "transcribe_for_reference",
            lambda _path, **_kw: {
                "text": "某个角度说，的确。",
                "language": "Japanese",
                "model": "stub",
                "seconds": 1.0,
            },
        )
        request = SynthesisRequest(
            text="你好。",
            voice_id=voice.shown_name,
            reference_audio=str(Path(voice.folder) / "难过.wav"),
        )
        _out, notes = prepare(
            service, _engine(transcript=True), request, library_voice_id=voice.id
        )
        assert service.get_voice(voice.id).language == "ja"
        assert "识别语种已写入音色（ja）" in notes[0][1]

    def test_the_heard_language_stays_out_when_writeback_is_off(
        self, service, demo_library: Path, monkeypatch
    ) -> None:
        """关掉语种写回：逐字稿照常补，音色的语言格子不动。"""
        asr_use.save_options({"language_writeback": False})
        service.save_folders([str(demo_library)])
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(voice.id, "难过.wav")
        monkeypatch.setattr(
            reference_mod.asr_use,
            "transcribe_for_reference",
            lambda _path, **_kw: {
                "text": "某个角度说，的确。",
                "language": "Japanese",
                "model": "stub",
                "seconds": 1.0,
            },
        )
        request = SynthesisRequest(
            text="你好。",
            voice_id=voice.shown_name,
            reference_audio=str(Path(voice.folder) / "难过.wav"),
        )
        _out, notes = prepare(
            service, _engine(transcript=True), request, library_voice_id=voice.id
        )
        assert service.get_voice(voice.id).language == ""
        assert "识别语种已写入" not in notes[0][1]

    def test_the_heard_line_is_written_back_to_the_sample(
        self, service, demo_library: Path, spy
    ) -> None:
        """**识别结果要存下来**——那是同一件事的两半，下次就不用再听一遍。"""
        service.save_folders([str(demo_library)])
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(voice.id, "难过.wav")
        request = SynthesisRequest(
            text="你好。",
            voice_id=voice.shown_name,
            reference_audio=str(Path(voice.folder) / "难过.wav"),
        )
        prepare(service, _engine(transcript=True), request, library_voice_id=voice.id)

        sample = next(s for s in service.get_voice(voice.id).samples if s.file == "难过.wav")
        assert sample.text == "某个角度说，的确。"

    def test_a_voice_outside_the_library_still_synthesizes(
        self, service, tmp_path: Path, spy
    ) -> None:
        """写不回去**不算失败**：这一次照样能发，只是下次还得再听一遍。
        但那句说明里必须讲清楚——不说的话用户会以为库里已经存下来了。"""
        request = SynthesisRequest(
            text="你好。", voice_id="不在库里的音色", reference_audio=str(_wav(tmp_path))
        )
        out, notes = prepare(service, _engine(transcript=True), request, library_voice_id="没有这个")
        assert out.reference_text == "某个角度说，的确。"
        assert "未能写回" in notes[0][1]

    def test_without_a_model_it_says_which_field_to_fill(
        self, service, tmp_path: Path, monkeypatch
    ) -> None:
        def no_model(_path: Path, **_kw: object):
            raise asr_use.AsrError("这台机器上还没有能用的识别模型")

        monkeypatch.setattr(reference_mod.asr_use, "transcribe_for_reference", no_model)
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        with pytest.raises(EngineError) as caught:
            prepare(service, _engine(transcript=True), request)
        message = str(caught.value)
        # 报的是"这一家要逐字稿、而我没法给你弄出来" + 手工那条路。
        assert "逐字稿" in message and "没有能用的识别模型" in message
        assert "说话内容" in message

    def test_an_empty_transcription_says_so_instead_of_sending_a_blank(
        self, service, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            reference_mod.asr_use,
            "transcribe_for_reference",
            lambda _path, **_kw: {"text": "   ", "model": "sensevoice", "seconds": 1.0},
        )
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        with pytest.raises(EngineError) as caught:
            prepare(service, _engine(transcript=True), request)
        assert "识别出文字" in str(caught.value)
        assert "逐字稿" in str(caught.value)

    def test_a_stopped_service_is_named_before_listening(
        self, service, tmp_path: Path, spy, monkeypatch
    ) -> None:
        """服务没起就不要去听：报"引擎没启动"，而不是让它失败后再报"自动识别失败"。"""
        monkeypatch.setattr(
            asr_use, "reachable", lambda: (False, "连不上 http://127.0.0.1:18999")
        )
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        with pytest.raises(EngineError) as caught:
            prepare(service, _engine(transcript=True), request)
        message = str(caught.value)
        assert "识别服务未启动（http://127.0.0.1:18999）" in message
        # 「未启动」与探测那句「连不上」是同一件事，不说两遍。
        assert "连不上" not in message
        assert spy["listen"] == []

    def test_an_unconfigured_engine_is_named_before_listening(
        self, service, tmp_path: Path, spy, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            asr_use, "unavailable_reason", lambda: "桩识别 还不能用：还没填服务地址。"
        )
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        with pytest.raises(EngineError, match="还不能用"):
            prepare(service, _engine(transcript=True), request)
        assert spy["listen"] == []


# --------------------------------------------------------------------------- #
# 归一：请求里那两个"入口"要合成一个
# --------------------------------------------------------------------------- #


class TestNormalisingTheRequest:
    def test_the_extra_slot_wins_over_the_field(self, service, tmp_path: Path, spy) -> None:
        """生成页挑的那条样例走的是 ``extra``（``setReferenceAudio`` 放的就是它），
        而合并时 **extra 覆盖**请求自己的格子——判断"用哪一段"必须认这件事。"""
        preset = _wav(tmp_path)
        picked = _mp3(tmp_path)
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(preset),
            extra={"reference_audio": str(picked)},
        )
        out, _notes = prepare(service, _engine(formats=("wav",)), request)
        assert spy["convert"] == [picked]  # 转的是**生效的那一个**
        assert out.reference_audio.endswith("转好的.wav")

    def test_after_prepare_both_slots_agree(self, service, tmp_path: Path, spy) -> None:
        """归一之后只剩一处可看：校准那条 ``requires`` 与映射表看到的是同一份。"""
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(_wav(tmp_path)),
            reference_text="老的",
            extra={"reference_text": "extra 里那个", "speed": 1.1},
        )
        out, _notes = prepare(service, _engine(), request)
        assert out.reference_text == "extra 里那个"
        assert "reference_text" not in out.extra
        assert "reference_audio" not in out.extra
        # **别的键一个不动**：归一那两个字段，不是顺手把 extra 洗一遍。
        assert out.extra == {"speed": 1.1}


# --------------------------------------------------------------------------- #
# 逐字稿跟着音频走（不能跨着借）
# --------------------------------------------------------------------------- #


class TestTheTranscriptBelongsToItsAudio:
    """挑了 A 段音频，就不能拿 B 段音频的逐字稿去配它。

    这一条是踩出来的：生成页上挑一条**没有**「说话内容」的样例时，请求格子里还躺着
    主样例那句话（中转站从主样例补的），而 ``extra`` 又覆盖它——于是引擎收到的是
    "A 段录音 + B 段录音的文字"。它不报错、声音也出得来，只是克隆出来的音色是偏的：
    一个错都没有，全靠人耳去发现。**不看这条规矩，那个坑就在。**
    """

    def test_a_different_audio_does_not_borrow_the_main_sample_s_transcript(
        self, service, tmp_path: Path, spy
    ) -> None:
        picked = _wav(tmp_path)  # extra 里那条（没有逐字稿）
        main = tmp_path / "主样例.wav"
        from conftest import make_wav

        make_wav(main)
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(main),
            reference_text="主样例说的那句话",
            extra={"reference_audio": str(picked)},
        )
        out, notes = prepare(service, _engine(transcript=True), request)
        # 借来的那句话**没有被当成那段音频的逐字稿**，而是去听了。
        assert spy["listen"] == [picked]
        assert out.reference_text == "某个角度说，的确。"
        assert notes

    def test_without_a_pick_the_main_sample_brings_its_own_text(
        self, service, tmp_path: Path, spy
    ) -> None:
        """没挑 = 就是主样例那一段，于是它自己的文本**照旧生效**（不该白听一遍）。"""
        main = tmp_path / "主样例.wav"
        from conftest import make_wav

        make_wav(main)
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(main), reference_text="主样例说的"
        )
        out, notes = prepare(service, _engine(transcript=True), request)
        assert out.reference_text == "主样例说的"
        assert notes == ()
        assert spy["listen"] == []

    def test_a_pick_that_is_the_main_sample_still_works(self, service, tmp_path: Path, spy) -> None:
        """挑的正好是主样例那一条时，前端会把它的文本一起带过来——照旧用。"""
        main = tmp_path / "主样例.wav"
        from conftest import make_wav

        make_wav(main)
        request = SynthesisRequest(
            text="你好。",
            voice_id="某人",
            reference_audio=str(main),
            extra={"reference_audio": str(main), "reference_text": "就是这一段的文字"},
        )
        out, notes = prepare(service, _engine(transcript=True), request)
        assert out.reference_text == "就是这一段的文字"
        assert notes == ()


# --------------------------------------------------------------------------- #
# 走一遍中转站
# --------------------------------------------------------------------------- #


class TestThroughTheGateway:
    def test_the_notes_reach_the_result(self, service, demo_library: Path, spy) -> None:
        """准备那一步做的改动**要跟着结果回到界面上**——否则用户只会看到
        "我挑的是 mp3，它却成功了"，而下一次在别处失败时他以为是随机的。"""
        import asyncio

        service.save_folders([str(demo_library)])
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        # 往这条音色里放一条 mp3 再重扫：这一次**两件事都要做**（转格式 + 现听逐字稿），
        # 于是"两句说明都回到了结果里"这件事才测得出来。
        picked = Path(voice.folder) / "额外.mp3"
        picked.write_bytes(b"ID3" + b"\x00" * 64)
        service.scan()
        voice = next(v for v in service.list_voices() if v.name == "胡桃")

        engine = _engine(formats=("wav",), transcript=True)

        class Seen(EnginePlugin):
            """把真正收到的请求留下来，供断言。"""
            key = "spec_engine"
            label = "声明引擎"
            seen: SynthesisRequest | None = None

            def synthesis_spec(self) -> SynthesisSpec:
                return engine.synthesis_spec()

            async def synthesize(self, cfg, req):
                from app.engines.base import SynthesisResult

                type(self).seen = req
                return SynthesisResult(audio=b"RIFF-fake", format="wav")

        plugin = Seen()
        result = asyncio.run(
            synthesize(
                service,
                engine_key="spec_engine",
                voice_id=voice.id,
                text="你好。",
                extra={"reference_audio": str(picked)},
                engines={"spec_engine": plugin},
            )
        )
        seen = Seen.seen
        assert seen is not None
        # 引擎拿到的是**转好、也补好逐字稿**的那一份。
        assert Path(seen.reference_audio).suffix == ".wav"
        assert seen.reference_text == "某个角度说，的确。"
        assert len(result.prep_notes) == 2
        assert "ffmpeg" in result.prep_notes[0][1]  # 先交代动作
        assert "识别引擎" in result.prep_notes[1][1]
        # 听出来的那句话**写回了那条 mp3 样例**（不是那条 wav）。
        samples = {s.file: s.text for s in service.get_voice(voice.id).samples}
        assert samples["额外.mp3"] == "某个角度说，的确。"


# --------------------------------------------------------------------------- #
# 走一遍 HTTP：**除了模型本体，整条链都是真的**
# --------------------------------------------------------------------------- #


@pytest.fixture()
def model_installed(monkeypatch: pytest.MonkeyPatch) -> str:
    """把"这台机器上能识别"做出来：只换最后那一步（推理），前面全是真的。

    识别现在是一类插件（``app/engines/asr_base``），真的那台要连本机 Qwen3-ASR
    服务。这里要钉的是**这条链**：HTTP → 中转站 → 准备参考音频（转格式、
    听逐字稿）→ 写回那条样例 → 引擎真正收到的是什么，所以只把"听"换成桩。
    """
    from app.usecase import asr as asr_use

    heard = "听出来的那句话。"
    monkeypatch.setattr(
        asr_use,
        "transcribe_for_reference",
        lambda path, **_kw: {
            "text": heard,
            "language": "",
            "emotion": "",
            "model": "stub",
            "seconds": 0.1,
        },
    )
    return heard


class _NeedyEngine(EnginePlugin):
    """要 wav、也要逐字稿的那一台（真的那家要连识别服务，这里不把它拉进来）。"""

    key = "needy"
    label = "要料引擎"
    seen: SynthesisRequest | None = None

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            requires=("reference_audio",),
            reference_formats=("wav",),
            needs_transcript=True,
        )

    async def synthesize(self, cfg, req):
        from app.engines.base import SynthesisResult

        type(self).seen = req
        return SynthesisResult(audio=b"RIFF-fake", format="wav")


class TestThroughTheHttpApi:
    def test_translate_and_transcribe_both_happen_on_the_way_out(
        self, client, library, demo_library: Path, monkeypatch: pytest.MonkeyPatch, model_installed: str
    ) -> None:
        """**一次请求里把两件事都做掉**：mp3 转成 wav、逐字稿现听一句。

        这一条走的是真的 HTTP（TestClient）与真的用例层，只有识别模型是桩——
        它钉的是那条链本身：界面只需要"挑一段录音、写字、按下生成"。
        """
        from conftest import rescan_and_wait

        from app.usecase import gateway

        monkeypatch.setattr(gateway, "active_engines", lambda: {"needy": _NeedyEngine()})
        client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
        rescan_and_wait(client)
        voices = _items(client.get("/api/v1/voices", params={"q": "胡桃"}))
        voice = _items(client.get(f"/api/v1/voices/{voices[0]['id']}"))

        # 往这条音色里放一条 mp3 再重扫：它既不是 wav，也没有「说话内容」——
        # 正是"素材库里的样子"，也正是这条路存在的理由。
        picked = Path(voice["folder"]) / "额外.mp3"
        picked.write_bytes(b"ID3" + b"\x00" * 64)
        rescan_and_wait(client)

        # **转换这一步在这里打桩**（真的 ffmpeg 由 test_ffmpeg.py 那一份去跑，
        # 它造得出真 mp3；这一份要钉的是"链"）。转出来的那一份得是**真文件**：
        # 下一边的识别要读它。
        from conftest import make_wav

        converted = Path(voice["folder"]) / "转好的.wav"
        make_wav(converted)
        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", lambda _src: converted)

        response = client.post(
            "/api/v1/synthesize",
            json={
                "engine": "needy",
                "voice_id": voice["id"],
                "text": "你好。",
                "extra": {"reference_audio": str(picked)},
                "persist": "cache",
            },
        )
        assert response.status_code == 200, response.text
        notes = response.json()["notes"]
        assert any("ffmpeg" in note for note in notes)
        assert any("识别引擎" in note for note in notes)

        seen = _NeedyEngine.seen
        assert seen is not None
        # 转好那一份（不是用户挑的那条 mp3）发给了引擎。
        assert seen.reference_audio != str(picked)
        assert Path(seen.reference_audio).suffix == ".wav"
        assert seen.reference_text == model_installed

        # **识别结果写回了那条样例**：再取一次详情，那一格的「说话内容」有了。
        again = _items(client.get(f"/api/v1/voices/{voice['id']}"))
        sample = next(s for s in again["samples"] if s["file"] == "额外.mp3")
        assert sample["text"] == model_installed


# --------------------------------------------------------------------------- #
# 时长
# --------------------------------------------------------------------------- #


class TestDuration:
    """引擎声明了硬区间时：超了截到上限以内，短了拒绝，量不出长度就不动。

    GPT-SoVITS 的 3–10 秒是服务端的一条 ``OSError``。它越界时**不返回错误**——
    ``TTS.run`` 吞掉异常吐一秒静音，客户端拿到的是一个 200 的"成功"文件。
    所以这一层必须自己量，不能等它报。
    """

    def test_a_long_reference_is_trimmed_and_the_transcript_is_dropped(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limits: list[float | None] = []

        def convert(path: Path, **kwargs: object) -> Path:
            limits.append(kwargs.get("limit"))  # type: ignore[arg-type]
            return path.with_name("截好的.wav")

        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", convert)
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 12.4)
        # 断点由 ffmpeg 的音量扫描给（真跑见 ``test_ffmpeg.py``）：这里钉"按它截"。
        monkeypatch.setattr(
            reference_mod.ffmpeg, "cut_point", lambda *_a, **_kw: (8.7, True)
        )

        out, notes = prepare(
            service,
            _engine(transcript=True, duration=(3.0, 10.0)),
            SynthesisRequest(
                text="你好。",
                voice_id="某人",
                reference_audio=str(_wav(tmp_path)),
                reference_text="原来是整段十二秒的那句话",
            ),
        )

        # 截在 8.7 秒那句说完的地方，而不是硬切到上限。
        assert limits == [8.7]
        assert out.reference_audio.endswith("截好的.wav")
        # 原逐字稿对应整段录音，**不能拿去配截出来的半句**：作废后重听。
        assert out.reference_text == "某个角度说，的确。"
        assert [code for code, _ in notes] == ["reference_trimmed", "transcript_inferred"]
        assert "12.4" in notes[0][1] and "8.7" in notes[0][1]
        assert "句读停顿" in notes[0][1]

    def test_a_failed_transcription_after_trimming_blames_the_trim(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """截断后重听失败：**必须说是"截了才要重听"**，不能说"说话内容为空"。

        用户明明填过逐字稿，照着"去填说话内容"再填一遍也填不出截出来那半句的字，
        只会更困惑。真正卡住的是识别服务，报错得先交代音频被截这件事。
        """
        monkeypatch.setattr(
            reference_mod.ffmpeg, "to_wav", lambda path, **_kw: path.with_name("截好的.wav")
        )
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 12.4)
        monkeypatch.setattr(reference_mod.ffmpeg, "cut_point", lambda *_a, **_kw: (8.7, True))

        def no_asr(_path: Path, **_kw: object):
            raise asr_use.AsrError("连不上识别服务（http://127.0.0.1:18999）")

        monkeypatch.setattr(reference_mod.asr_use, "transcribe_for_reference", no_asr)

        with pytest.raises(EngineError) as caught:
            prepare(
                service,
                _engine(transcript=True, duration=(3.0, 10.0)),
                SynthesisRequest(
                    text="你好。",
                    voice_id="某人",
                    reference_audio=str(_wav(tmp_path)),
                    reference_text="原来是整段十二秒的那句话",
                ),
            )

        message = str(caught.value)
        assert "截取后需重新识别逐字稿" in message
        assert "3–10 秒" in message
        assert "连不上识别服务" in message
        # 用户填过逐字稿，不能再把原因说成"说话内容为空"。
        assert "说话内容为空" not in message

    def test_it_widens_the_ceiling_once_before_giving_up(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """句子的尾巴常常正好落在留出的余量里：先在 9.5 找，找不到再放宽到 9.9。

        服务端的判据是 10 秒（160000 采样点），9.9 秒仍在里面——比硬切在 9.5
        更可能保住句子。
        """
        ceilings: list[float] = []
        limits: list[float | None] = []

        def cut(_source: str, *, low: float, high: float, **_kw: object):
            ceilings.append(high)
            return (9.5, False) if high < 9.9 else (9.7, True)

        def convert(path: Path, **kwargs: object) -> Path:
            limits.append(kwargs.get("limit"))  # type: ignore[arg-type]
            return path.with_name("截好的.wav")

        monkeypatch.setattr(reference_mod.ffmpeg, "cut_point", cut)
        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", convert)
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 14.0)

        _, notes = prepare(
            service,
            _engine(duration=(3.0, 10.0)),
            SynthesisRequest(
                text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
            ),
        )
        assert ceilings == [9.5, 9.9]
        assert limits == [9.7]
        assert "句读停顿" in notes[0][1]

    def test_without_a_pause_it_cuts_at_the_ceiling(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全片没有停顿也别把样例拒掉：按上限硬切，并如实说"没找到"。"""
        limits: list[float | None] = []

        def convert(path: Path, **kwargs: object) -> Path:
            limits.append(kwargs.get("limit"))  # type: ignore[arg-type]
            return path.with_name("截好的.wav")

        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", convert)
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 13.0)
        monkeypatch.setattr(
            reference_mod.ffmpeg, "cut_point", lambda *_a, **_kw: (9.5, False)
        )
        _, notes = prepare(
            service,
            _engine(duration=(3.0, 10.0)),
            SynthesisRequest(
                text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
            ),
        )
        assert limits == [9.5]
        assert "未找到停顿" in notes[0][1]

    def test_a_short_reference_is_refused_before_it_is_sent(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 2.2)
        with pytest.raises(EngineError, match="短了补不回来"):
            prepare(
                service,
                _engine(duration=(3.0, 10.0)),
                SynthesisRequest(
                    text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
                ),
            )
        assert spy["convert"] == []

    def test_an_unreadable_duration_is_left_alone(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """量不出来（ffprobe 缺席）就别猜着截——让服务端自己报，比截错一段强。"""
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 0.0)
        request = SynthesisRequest(
            text="你好。", voice_id="某人", reference_audio=str(_wav(tmp_path))
        )
        out, notes = prepare(service, _engine(duration=(3.0, 10.0)), request)
        assert out.reference_audio == request.reference_audio
        assert notes == ()
        assert spy["convert"] == []

    def test_a_trimmed_sample_does_not_overwrite_its_transcript(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """截取后识别出的半句话**不能写回样例**：样例仍是整段录音。"""
        from conftest import make_wav

        service.save_folders([str(demo_library)])
        service.scan()
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        target = Path(voice.folder) / "长录音.wav"
        make_wav(target)
        service.scan()

        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 11.0)
        monkeypatch.setattr(
            reference_mod.ffmpeg, "cut_point", lambda *_a, **_kw: (9.0, True)
        )
        monkeypatch.setattr(
            reference_mod.ffmpeg, "to_wav", lambda path, **_: path.with_name("截好的.wav")
        )
        monkeypatch.setattr(
            reference_mod.asr_use,
            "transcribe_for_reference",
            lambda _p, **_kw: {"text": "半句话", "model": "sensevoice", "seconds": 1.0},
        )

        out, notes = prepare(
            service,
            _engine(transcript=True, duration=(3.0, 10.0)),
            SynthesisRequest(text="你好。", voice_id=voice.id, reference_audio=str(target)),
            library_voice_id=voice.id,
        )
        assert out.reference_text == "半句话"
        assert "未写回样例" in notes[-1][1]
        sample = next(s for s in service.get_voice(voice.id).samples if s.file == "长录音.wav")
        assert sample.text == ""

    def test_voxcpm2_trim_relistens_to_keep_ultimate_clone(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VoxCPM2 不自己裁：声明 15 秒上限后由核心截；给了逐字稿就重听保住终极克隆。

        16.25 秒正是撞 AudioVAE 容量（240000/16000=15 秒）的那条样例。截到上限
        以内后原逐字稿作废——但用户给了逐字稿（终极克隆），核心像 GPT-SoVITS 那样
        重听一句，让发出去的逐字稿仍配得上截出来的那段，而不是悄悄退成普通克隆。
        """
        from app.engines.builtin.voxcpm2.plugin import VoxCpm2Engine

        limits: list[float | None] = []
        ceilings: list[float] = []

        def convert(path: Path, **kwargs: object) -> Path:
            limits.append(kwargs.get("limit"))  # type: ignore[arg-type]
            return path.with_name("截好的.wav")

        def cut(_source: str, *, low: float, high: float, **_kw: object):
            ceilings.append(high)
            return (14.0, True)

        monkeypatch.setattr(reference_mod.ffmpeg, "to_wav", convert)
        monkeypatch.setattr(reference_mod.ffmpeg, "cut_point", cut)
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 16.25)

        out, notes = prepare(
            service,
            VoxCpm2Engine(),
            SynthesisRequest(
                text="你好。",
                voice_id="曼波",
                reference_audio=str(_wav(tmp_path)),
                reference_text="原来是整段十六秒的那句话",
            ),
        )
        # 上限 15 留半秒余量 → 在 14.5 以内找停顿，截在 14.0。
        assert ceilings == [14.5]
        assert limits == [14.0]
        assert out.reference_audio.endswith("截好的.wav")
        # 重听的是截出来那一份，逐字稿换成新听到的那句（终极克隆得以保留）。
        assert [p.name for p in spy["listen"]] == ["截好的.wav"]
        assert out.reference_text == "某个角度说，的确。"
        assert [code for code, _ in notes] == ["reference_trimmed", "transcript_inferred"]
        assert "16.2" in notes[0][1] or "16.3" in notes[0][1]
        assert "15" in notes[0][1]

    def test_voxcpm2_trim_without_transcript_stays_plain_clone(
        self, service, tmp_path: Path, spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没给逐字稿的普通克隆：截断后不强制识别，空着发（逐字稿本就是加分项）。"""
        from app.engines.builtin.voxcpm2.plugin import VoxCpm2Engine

        monkeypatch.setattr(
            reference_mod.ffmpeg, "to_wav", lambda path, **_kw: path.with_name("截好的.wav")
        )
        monkeypatch.setattr(reference_mod.ffmpeg, "cut_point", lambda *_a, **_kw: (14.0, True))
        monkeypatch.setattr(reference_mod.audioprobe, "probe_duration", lambda _p: 16.25)

        out, notes = prepare(
            service,
            VoxCpm2Engine(),
            SynthesisRequest(
                text="你好。", voice_id="曼波", reference_audio=str(_wav(tmp_path))
            ),
        )
        assert out.reference_audio.endswith("截好的.wav")
        assert out.reference_text == ""
        assert spy["listen"] == []
        assert [code for code, _ in notes] == ["reference_trimmed"]

