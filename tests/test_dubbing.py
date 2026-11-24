"""配音生产方：切段规则、时间轴拼装与完整链路。

切段与拼装是纯计算，真跑；导入 / 识别 / 导出走 ffmpeg，没装就跳过——
这条路本来就依赖一个外部程序，环境缺它不该把整份测试弄红。
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import wave
from pathlib import Path

import pytest

from app.infra import ffmpeg as ffmpeg_tools
from app.infra.audioprobe import probe_duration
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.service import DubbingService, _speech_segments
from app.producers.builtin.dubbing.settings import DubbingSettings


@pytest.fixture()
def ffmpeg_ready() -> None:
    if not ffmpeg_tools.find_ffmpeg():
        pytest.skip("这台机器上没有 ffmpeg")


@pytest.fixture()
def captured_logs():
    """接住这一条用例期间的日志行；用例结束就摘掉，不串到别的用例。"""
    from app.infra.loghub import CHANNEL

    lines: list[dict] = []

    class Capture:
        def emit(self, record) -> None:
            lines.append(record.as_dict())

    capture = Capture()
    CHANNEL.add_sink(capture)
    try:
        yield lines
    finally:
        CHANNEL.remove_sink(capture)


def make_tone(path: Path, seconds: float, *, rate: int = 44100) -> Path:
    """写一段立体声正弦波（用来当"源音轨"）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b"".join(
            struct.pack("<hh", value, value)
            for value in (
                int(9000 * math.sin(2 * math.pi * 220 * i / rate))
                for i in range(int(rate * seconds))
            )
        )
        handle.writeframes(frames)
    return path


def make_silence(path: Path, seconds: float, *, rate: int = 44100) -> Path:
    """写一段立体声静音（用来当"没有这一路声音"的轨）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0\0\0" * int(rate * seconds))
    return path


def quiet_tone(path: Path, seconds: float, *, rate: int = 44100) -> Path:
    """写一段单声道低电平正弦波（用来当"引擎产物"）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(1200 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


def rms_of(path: Path, start: float, end: float) -> float:
    """取一段的均方根：用来判断这条轨道这一刻有没有声音。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        handle.setpos(int(start * rate))
        raw = handle.readframes(int((end - start) * rate))
    values = struct.unpack(f"<{len(raw) // 2}h", raw)
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


class TestLinkedProject:
    """引用导入：源文件留在原处，项目只存转出的音轨；移动后能重新指定。"""

    def _service(self, tmp_path: Path) -> DubbingService:
        return DubbingService(tmp_path / "svc", DubbingSettings())

    def test_source_stays_outside_the_project(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "原片.wav", 2.0)
        service = self._service(tmp_path)
        detail = service.create_linked(name="引用样片", path=source)
        project = detail["project"]
        assert project["source_linked"] is True
        assert detail["source_ok"] is True
        assert detail["source_linked"] is True
        # 项目目录里没有源文件的副本，只有抽出来的音轨。
        folder = service.folder(project["id"])
        assert not (folder / source.name).exists()
        assert (folder / "audio" / "original.wav").is_file()
        assert service.source_file(project) == source.resolve()

        # 删除项目不能碰项目外的源文件。
        service.delete(project["id"])
        assert source.is_file()

    def test_moved_source_can_be_relinked(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "原片.wav", 2.0)
        service = self._service(tmp_path)
        detail = service.create_linked(name="引用样片", path=source)
        project_id = detail["project"]["id"]

        moved = source.with_name("改名后.wav")
        source.rename(moved)
        missing = service.detail(project_id)
        assert missing["source_ok"] is False
        assert "源文件不在了" in missing["steps"][0]["summary"]

        fixed = service.relink(project_id, path=moved)
        assert fixed["source_ok"] is True
        assert fixed["project"]["source_name"] == "改名后.wav"
        assert (service.folder(project_id) / "audio" / "original.wav").is_file()

    def test_relink_refuses_a_different_length(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "原片.wav", 2.0)
        service = self._service(tmp_path)
        detail = service.create_linked(name="引用样片", path=source)
        other = make_tone(tmp_path / "别的片子.wav", 3.0)
        with pytest.raises(ProducerError, match="不是同一条片子"):
            service.relink(detail["project"]["id"], path=other)


class TestSpeechSegments:
    """静音取补集：切得太碎会让字幕一行只有两三个字，切得太粗会跨句。"""

    def test_silence_is_cut_out(self) -> None:
        spans = [(2.0, 2.4), (4.0, 4.4)]
        segments = _speech_segments(spans, 6.0, max_segment=15.0)
        assert len(segments) == 3
        # 两端各留 60ms，但不越过 0 与总时长：第一段从 0 开始，
        # 后面两段各比静音结束早 60ms。
        assert segments[0] == (0.0, 2.06)
        assert segments[1][0] == pytest.approx(2.34, abs=0.02)
        assert segments[2][0] == pytest.approx(4.34, abs=0.02)

    def test_short_gap_is_not_a_sentence_break(self) -> None:
        # 0.05 秒的"静音"是辅音闭合，不是停顿：补集里不产生新段。
        segments = _speech_segments([(1.0, 1.05)], 3.0, max_segment=15.0)
        assert len(segments) == 2

    def test_long_speech_is_split_by_max_segment(self) -> None:
        segments = _speech_segments([], 40.0, max_segment=15.0)
        assert len(segments) == 3
        assert all(end - start <= 15.5 for start, end in segments)

    def test_no_speech_returns_nothing(self) -> None:
        assert _speech_segments([(0.0, 6.0)], 6.0, max_segment=15.0) == []


class TestDubbingProject:
    def _service(self, tmp_path: Path, **settings) -> DubbingService:
        return DubbingService(tmp_path / "svc", DubbingSettings(**settings))

    def test_create_reads_duration_and_extracts_audio(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "source.wav", 3.0)
        service = self._service(tmp_path)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project = detail["project"]
        assert project["kind"] == "audio"
        assert 2900 <= project["duration_ms"] <= 3100
        assert detail["paths"]["original"] == "audio/original.wav"
        assert (service.folder(project["id"]) / "audio" / "original.wav").is_file()
        # 旁白先建出来：不属于任何角色的段落都归它。
        assert [role["name"] for role in detail["roles"]] == ["旁白"]

    def test_workflow_steps_own_their_models_and_parameters(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """角色不单列一步；识别 / 配音 / 贴齐 / 导出的参数各归自己的步骤。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        keys = [item["key"] for item in detail["steps"]]
        assert keys == ["extract", "separate", "transcribe", "synth", "align", "export"]
        separate = next(item for item in detail["steps"] if item["key"] == "separate")
        # 可选步骤没跑过时是「可选」，不是卡在等待。
        assert separate["state"] == "off"

        properties = DubbingSettings.model_json_schema()["properties"]
        assert properties["silence_db"]["step"] == "transcribe"
        assert properties["speaker_diar"]["step"] == "transcribe"
        assert properties["concurrency"]["step"] == "synth"
        assert properties["fit"]["step"] == "align"
        assert properties["loudness"]["step"] == "align"
        assert properties["audio_format"]["step"] == "export"
        assert "step" not in properties["project_dir"]

    def test_transcribe_splits_on_silence_and_synthesizes_timeline(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 3.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]

        # 识别走宿主用例；这里替掉它，只验切段与时间码。
        from app.usecase import asr as asr_uc

        monkeypatch.setattr(asr_uc, "unavailable_reason", lambda: "")
        monkeypatch.setattr(asr_uc, "reachable", lambda: (True, ""))
        monkeypatch.setattr(asr_uc, "reachable", lambda: (True, ""))
        monkeypatch.setattr(
            asr_uc,
            "transcribe_file",
            lambda path: {"text": f"第{path.stem}段"},
        )
        job = service.start_transcribe(project_id)
        done = service.wait_job(job["id"], timeout=120)
        assert done["state"] == "done", done["message"]
        cues = service.store.cues(project_id)
        assert cues, "应当切出至少一段"
        # 时间码来自实测边界，不是按字数估的：第一段从 0 开始。
        assert cues[0]["start_ms"] == 0
        assert all(cue["end_ms"] > cue["start_ms"] for cue in cues)

    def test_cue_edit_marks_audio_stale(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "原句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF")
        relative = audio.relative_to(service.folder(project_id)).as_posix()
        role = service.store.roles(project_id)[0]
        service.store.update_cue(
            cue["id"],
            audio=relative,
            audio_key=service._audio_key(cue, role),
            state="ready",
        )

        updated = service.update_cue(project_id, cue["id"], {"text": "改过的句子。"})
        assert updated["text"] == "改过的句子。"
        # 文本改了，旧音频不再对应：标成过期（文件还在，改回去能听）。
        assert updated["state"] == "stale"
        assert audio.is_file()

    def test_voice_change_back_restores_ready(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """换音色把段标成过期；换回原来那一档时合成键对得上，仍算「已配音」。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF")
        service.store.update_cue(
            cue["id"],
            audio=audio.relative_to(service.folder(project_id)).as_posix(),
            audio_key=service._audio_key(cue, service.store.role(role["id"])),
            state="ready",
        )

        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v2"})
        assert service.store.cue(cue["id"])["state"] == "stale"
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        # 换回来时旧产物仍然对应这一句：不该白跑一轮重配。
        assert service.store.cue(cue["id"])["state"] == "ready"

    def test_missing_audio_is_not_restored_as_ready(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """换回音色时只有文件还在才恢复「已配音」；文件没了就继续等重配。

        创作页的复活判据是"键对得上 + 文件还在"。配音这边少了后半句，
        于是会出现"显示已配音、点播放却读不出来"的段落。
        """
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF")
        service.store.update_cue(
            cue["id"],
            audio=audio.relative_to(service.folder(project_id)).as_posix(),
            audio_key=service._audio_key(cue, service.store.role(role["id"])),
            state="ready",
        )

        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v2"})
        audio.unlink()
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        assert service.store.cue(cue["id"])["state"] == "stale"
        view = service.detail(project_id)["cues"][0]
        assert view["audio_ok"] is False
        assert view["audio_version"] == ""
        updated = service.update_cue(project_id, cue["id"], {"text": "一句。"})
        assert updated["audio_ok"] is False

    def test_missing_audio_file_is_pending_not_ready(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """文件被删掉后不能再算「已配音」：统计、步骤与导出都要退回待配。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF")
        service.store.update_cue(
            cue["id"],
            audio=audio.relative_to(service.folder(project_id)).as_posix(),
            audio_key=service._audio_key(cue, service.store.role(role["id"])),
            state="ready",
        )
        audio.unlink()

        detail = service.detail(project_id)
        assert detail["stats"]["ready"] == 0
        assert detail["stats"]["pending"] == 1
        assert detail["stats"]["failed"] == 0
        synth = next(item for item in detail["steps"] if item["key"] == "synth")
        assert synth["state"] == "wait"
        assert detail["export"]["ready"] is False

    def test_split_inherits_the_original_role(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """拆一句话不该把后半句退回旁白。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        speaker = service.add_role(project_id, {"name": "甲"})
        cue = service.store.replace_cues(
            project_id,
            [
                {
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "前半句，后半句。",
                    "role_id": speaker["id"],
                }
            ],
        )[0]
        cues = service.split_cue(project_id, cue["id"], 1000)
        assert [item["role_id"] for item in cues] == [speaker["id"], speaker["id"]]

    def test_sample_strategy_is_part_of_the_key_and_request(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """样例策略要参与合成键，并原样交给网关：按情绪匹配 / 钉住固定样例。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        legacy = service._audio_key(cue, service.store.role(role["id"]))
        service.update_role(
            project_id, role["id"], {"sample_mode": "emotion", "sample_file": ""}
        )
        emotion_key = service._audio_key(cue, service.store.role(role["id"]))
        assert emotion_key != legacy
        service.update_role(
            project_id,
            role["id"],
            {"sample_mode": "fixed", "sample_file": "开心.wav"},
        )
        fixed_key = service._audio_key(cue, service.store.role(role["id"]))
        assert len({legacy, emotion_key, fixed_key}) == 3

        calls: list[dict] = []

        async def fake_synthesize(*args, **kwargs):
            from app.engines.base import SynthesisResult

            calls.append(kwargs)
            mono = quiet_tone(tmp_path / "engine.wav", 1.0)
            return SynthesisResult(audio=mono.read_bytes(), format="wav")

        from app.usecase import gateway

        monkeypatch.setattr(gateway, "synthesize", fake_synthesize)
        job = service.start_synth(project_id, None, [cue["id"]], force=True)
        service.wait_job(job["id"], timeout=30)
        assert calls[-1]["sample_file"] == "开心.wav"
        assert calls[-1]["match_emotion"] is False

        service.update_role(
            project_id, role["id"], {"sample_mode": "emotion", "sample_file": ""}
        )
        job = service.start_synth(project_id, None, [cue["id"]], force=True)
        service.wait_job(job["id"], timeout=30)
        assert calls[-1]["sample_file"] == ""
        assert calls[-1]["match_emotion"] is True

    def test_synth_logs_carry_cue_role_voice_and_engine_hints(
        self,
        tmp_path: Path,
        ffmpeg_ready: None,
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: list[dict],
    ) -> None:
        """日志页只显示 message：段号、角色、音色、引擎提示都要写进这一句。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(
            project_id,
            role["id"],
            {
                "name": "主角",
                "engine": "fake",
                "voice_id": "v1",
                "voice_name": "某音色",
                "sample_mode": "emotion",
            },
        )
        service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "日志要写清楚。"}]
        )

        async def fake_synthesize(*args, **kwargs):
            from app.engines.base import SynthesisResult

            mono = quiet_tone(tmp_path / "engine.wav", 1.0)
            return SynthesisResult(
                audio=mono.read_bytes(),
                format="wav",
                notes=("情绪词被引擎忽略",),
                prep_notes=(("reference", "参考音频已转码"),),
            )

        from app.usecase import gateway

        monkeypatch.setattr(gateway, "synthesize", fake_synthesize)
        job = service.start_synth(project_id, None, force=True)
        service.wait_job(job["id"], timeout=30)
        messages = "\n".join(
            str(line.get("message") or "") for line in captured_logs
        )
        assert "开始配音" in messages and "主角 / 某音色 / fake" in messages
        assert "第 1 段" in messages and "日志要写清楚。" in messages
        assert "合成完成" in messages
        assert "按情绪匹配样例" in messages
        assert "引擎提示：情绪词被引擎忽略" in messages
        assert "已自动处理：参考音频已转码" in messages
        assert "配音完成" in messages

    def test_audio_version_follows_the_file(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """产物被覆盖时版本号要变：播放地址靠它绕开浏览器里的旧音频。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 40)
        service.store.update_cue(
            cue["id"],
            audio=audio.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )

        first = service.detail(project_id)["cues"][0]["audio_version"]
        assert first
        # 重配写的是同一个路径：只有 mtime/大小能区分"这是新的一版"。
        audio.write_bytes(b"RIFF" + b"\x00" * 80)
        second = service.detail(project_id)["cues"][0]["audio_version"]
        assert second and second != first
        # 没有产物的段不发版本号，界面也就不会拼出一个假地址。
        service.store.update_cue(cue["id"], audio="", state="empty")
        assert service.detail(project_id)["cues"][0]["audio_version"] == ""

    def test_synth_clears_failed_when_audio_already_matches(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """上一轮失败、但产物与合成键都对得上的段：再跑一次要回到「已配音」。

        以前这种段在 `_synth` 里走"键没变就跳过"的分支，直接 return，`failed`
        永远擦不掉——界面上就是"显示失败，播放起来却是对的"。
        """
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]
        service.update_role(project_id, role["id"], {"engine": "fake", "voice_id": "v1"})
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        audio = service._work(project_id, "tts") / "cue.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 40)
        key = service._audio_key(cue, service.store.role(role["id"]))
        service.store.update_cue(
            cue["id"],
            audio=audio.relative_to(service.folder(project_id)).as_posix(),
            audio_key=key,
            state="failed",
            error="写不进去",
        )

        job = service.start_synth(project_id, None)
        done = service.wait_job(job["id"], timeout=30)
        assert done["state"] == "done", done["message"]
        fixed = service.store.cue(cue["id"])
        assert fixed is not None
        assert fixed["state"] == "ready"
        assert fixed["error"] == ""

    def test_split_and_merge_keep_order(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 2000, "text": "前半句，后半句。"}]
        )[0]
        cues = service.split_cue(project_id, cue["id"], 1000)
        assert [item["ordinal"] for item in cues] == [1, 2]
        assert cues[0]["start_ms"] == 0 and cues[0]["end_ms"] == 1000
        assert cues[1]["start_ms"] == 1000 and cues[1]["end_ms"] == 2000
        assert cues[0]["text"] and cues[1]["text"]

        merged = service.merge_cues(project_id, cues[0]["id"])
        assert len(merged) == 1
        assert merged[0]["text"] == "前半句，后半句。"
        assert merged[0]["end_ms"] == 2000

    def test_synth_writes_middle_wav_and_skips_unchanged(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """合成产物统一成 44.1k 单声道 PCM；参数没变的段第二次不再请求。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        narrator = detail["roles"][0]
        service.update_role(
            project_id, narrator["id"], {"engine": "fake", "voice_id": "v1"}
        )
        service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1200, "text": "要合成的一句。"}]
        )

        calls: list[str] = []

        async def fake_synthesize(*args, **kwargs):
            from app.engines.base import SynthesisResult

            calls.append(kwargs["text"])
            mono = tmp_path / "engine.wav"
            if not mono.is_file():
                # 引擎给的是 24 kHz 单声道 wav：必须被转成中间件口径。
                with wave.open(str(mono), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(24000)
                    handle.writeframes(
                        b"".join(
                            struct.pack("<h", 6000)
                            for _ in range(24000)
                        )
                    )
            return SynthesisResult(audio=mono.read_bytes(), format="wav")

        from app.usecase import gateway

        monkeypatch.setattr(gateway, "synthesize", fake_synthesize)
        job = service.start_synth(project_id, None)
        done = service.wait_job(job["id"], timeout=60)
        assert done["state"] == "done", done["message"]
        cue = service.store.cues(project_id)[0]
        assert cue["state"] == "ready"
        from app.infra.audioprobe import probe_stream

        audio = service.folder(project_id) / cue["audio"]
        assert probe_stream(audio) == ("pcm_s16le", 44100, 1)

        # 第二次：文本与音色都没变，不再请求引擎。
        job = service.start_synth(project_id, None)
        service.wait_job(job["id"], timeout=60)
        assert calls == ["要合成的一句。"]

        # 点名重配（force）：键没变也要重新请求引擎，不能被缓存顶回去。
        job = service.start_synth(project_id, None, [cue["id"]], force=True)
        service.wait_job(job["id"], timeout=60)
        assert calls == ["要合成的一句。", "要合成的一句。"]

    def test_assemble_and_export_audio(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 3.0)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        original = service.folder(project_id) / "audio" / "original.wav"
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 900, "text": "第一句。"},
                {"start_ms": 1400, "end_ms": 2400, "text": "第二句。"},
            ],
        )
        for cue in cues:
            target = service._work(project_id, "tts") / f"{cue['id']}.wav"
            ffmpeg_tools.slice_audio(
                original,
                target,
                start=cue["start_ms"] / 1000,
                end=cue["end_ms"] / 1000,
                rate=44100,
                channels=1,
            )
            service.store.update_cue(
                cue["id"],
                audio=target.relative_to(service.folder(project_id)).as_posix(),
                state="ready",
            )

        result = service.assemble(project_id)
        assert result["duration"] >= 2.3
        out = service.folder(project_id) / "out"
        assert (out / "vocal.wav").is_file()
        assert (out / "mix.wav").is_file()
        # 还没导出时字幕跟项目名；导出后要改成和成片同名。
        subtitle = out / "样片.srt"
        srt = subtitle.read_text(encoding="utf-8")
        assert "第一句。" in srt and "第二句。" in srt

        # 混完还没导出：中间产物不算成品，导出这一步仍应是「等待」。
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "wait"

        exported = service.export(project_id)
        # 导出时间在最前面：长项目名会把末尾的版本号截掉，看上去就像旧文件。
        assert re.match(r"^\d{8}-\d{6}-", exported["file"])
        assert exported["file"].endswith(".m4a")
        assert (out / exported["file"]).is_file()
        # 字幕与成片同名：播放器能自动把这一对挂在一起；旧的项目名那一份不再留。
        subtitles = list(out.glob("*.srt"))
        assert len(subtitles) == 1
        assert Path(exported["file"]).stem == subtitles[0].stem
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "done"
        assert exported["file"] in detail["paths"]["exports"]

        # 导出后再改一段：成片与混音都落后于段落，必须重新导出，不能沿用旧文件。
        service.update_cue(project_id, cues[0]["id"], {"text": "改过的第一句。"})
        detail = service.detail(project_id)
        assert detail["paths"]["mix_current"] is False
        assert detail["paths"]["export_current"] is False
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "wait"

        # 每次导出都是新文件：同名会覆盖上一版，用户拿到的就是"永远是第一次那个名"。
        again = service.export(project_id, allow_incomplete=True)
        assert again["file"] != exported["file"]
        assert (out / again["file"]).is_file()
        assert (out / exported["file"]).is_file()
        assert Path(again["file"]).stem == next(out.glob("*.srt")).stem
        detail = service.detail(project_id)
        assert detail["paths"]["exports"][0] == again["file"]
        assert detail["paths"]["export_current"] is True


class TestAutoEngine:
    """角色引擎写 auto：每次开始合成前按当前运行实例重选，调用方不必跟随改动。"""

    def _service(self, tmp_path: Path) -> DubbingService:
        return DubbingService(tmp_path / "svc", DubbingSettings())

    def test_legacy_auto_role_reads_as_auto(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """升级前就写进库里的 engine=auto：读出来是自动标记，不拿 "auto" 当引擎。"""
        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="旧库", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = service.store.add_role(
            project_id, name="旧调用方", engine="auto", voice_id="v1"
        )
        assert role["engine"] == ""
        assert role["engine_auto"] is True

        saved = next(
            item
            for item in service.detail(project_id)["roles"]
            if item["id"] == role["id"]
        )
        assert saved["engine"] == ""
        assert saved["engine_auto"] is True

    def test_auto_role_follows_the_running_engine(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.engines.base import SynthesisResult
        from app.producers.builtin.dubbing import service as dubbing_service
        from app.usecase import gateway

        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="自动", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]

        picks = [["fake_a"]]
        monkeypatch.setattr(
            dubbing_service, "auto_engine_keys", lambda voice=None: picks[0]
        )
        updated = service.update_role(
            project_id, role["id"], {"engine": "auto", "voice_id": "v1"}
        )
        assert updated["engine"] == "fake_a"
        assert updated["engine_auto"] is True

        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        calls: list[dict] = []

        async def fake_synthesize(*args, **kwargs):
            calls.append(kwargs)
            mono = quiet_tone(tmp_path / "engine.wav", 1.0)
            return SynthesisResult(audio=mono.read_bytes(), format="wav")

        monkeypatch.setattr(gateway, "synthesize", fake_synthesize)

        # 原来那台停了、另一台起来了：开始合成时自动跟随，调用方存下的 auto 不用改。
        picks[0] = ["fake_b"]
        job = service.start_synth(project_id, None, [cue["id"]], force=True)
        done = service.wait_job(job["id"], timeout=30)
        assert done["state"] == "done", done["message"]
        assert service.store.role(role["id"])["engine"] == "fake_b"
        assert [item["engine_key"] for item in calls] == ["fake_b"]
        assert service.detail(project_id)["cues"][0]["state"] == "ready"

        # 两台都在跑时保持上一次那台：多起一台引擎不该让整本书换嗓。
        picks[0] = ["fake_a", "fake_b"]
        job = service.start_synth(project_id, None, [cue["id"]], force=True)
        service.wait_job(job["id"], timeout=30)
        assert service.store.role(role["id"])["engine"] == "fake_b"
        assert [item["engine_key"] for item in calls] == ["fake_b", "fake_b"]

        # 钉死一台：自动标记让位，下一次合成不再改。
        updated = service.update_role(project_id, role["id"], {"engine": "fake_c"})
        assert updated["engine"] == "fake_c"
        assert updated["engine_auto"] is False

    def test_auto_role_without_a_running_engine_waits(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.engines.base import SynthesisResult
        from app.producers.builtin.dubbing import service as dubbing_service
        from app.usecase import gateway

        service = self._service(tmp_path)
        source = make_tone(tmp_path / "source.wav", 2.0)
        detail = service.create(name="自动", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        role = detail["roles"][0]

        picks = [[]]
        monkeypatch.setattr(
            dubbing_service, "auto_engine_keys", lambda voice=None: picks[0]
        )
        updated = service.update_role(
            project_id, role["id"], {"engine": "auto", "voice_id": "v1"}
        )
        assert updated["engine"] == ""
        assert updated["engine_auto"] is True

        cue = service.store.replace_cues(
            project_id, [{"start_ms": 0, "end_ms": 1000, "text": "一句。"}]
        )[0]
        with pytest.raises(ProducerError, match="自动引擎暂无可用实例"):
            service.start_synth(project_id, None)

        async def fake_synthesize(*args, **kwargs):
            mono = quiet_tone(tmp_path / "engine.wav", 1.0)
            return SynthesisResult(audio=mono.read_bytes(), format="wav")

        monkeypatch.setattr(gateway, "synthesize", fake_synthesize)
        # 实例起来之后再开始合成：当场补上实际引擎，段照常配出来。
        picks[0] = ["fake"]
        job = service.start_synth(project_id, None, [cue["id"]])
        done = service.wait_job(job["id"], timeout=30)
        assert done["state"] == "done", done["message"]
        assert service.store.role(role["id"])["engine"] == "fake"


class TestAssemblyPlan:
    """拼装规则：补静音、加速贴齐、越界顺延。这两条直接决定成片对齐。"""

    def test_long_cue_is_fitted_into_its_slot(self, tmp_path: Path, ffmpeg_ready: None) -> None:
        from app.usecase.assembly import AssemblyOptions, Slot, plan_timeline

        one = make_tone(tmp_path / "a.wav", 1.0)
        long = make_tone(tmp_path / "b.wav", 1.6)
        slots = [
            Slot("a", 1, 0, 1000, one, "第一句"),
            # 1.6 秒要塞进 1.4 秒的时隙：1.14 倍，能贴进去就不算顺延。
            Slot("b", 2, 1000, 2400, long, "第二句"),
        ]
        plan = plan_timeline(
            slots,
            work_dir=tmp_path / "work",
            options=AssemblyOptions(fit=True, max_speed=1.35),
        )
        assert plan.fitted == [2]
        assert plan.delayed == []
        assert 2.35 <= plan.duration <= 2.45

    def test_overrun_is_delayed_not_truncated(self, tmp_path: Path, ffmpeg_ready: None) -> None:
        from app.usecase.assembly import AssemblyOptions, Slot, plan_timeline

        one = make_tone(tmp_path / "a.wav", 1.0)
        long = make_tone(tmp_path / "b.wav", 2.0)
        short = make_tone(tmp_path / "c.wav", 0.5)
        slots = [
            Slot("a", 1, 0, 1000, one, "第一句"),
            Slot("b", 2, 1000, 2400, long, "第二句"),
            # 上一段实际要放到 3.0 秒，这一段却写在 2.0 秒开始：
            # 不截断、不丢弃，从当前位置接着放，并记一条顺延。
            Slot("c", 3, 2000, 3200, short, "第三句"),
        ]
        plan = plan_timeline(
            slots,
            work_dir=tmp_path / "work",
            options=AssemblyOptions(fit=False),
        )
        assert plan.delayed == [3]
        assert plan.fitted == []
        # 三段全在：顺延不等于删掉。
        assert [text for _, _, text in plan.cues] == ["第一句", "第二句", "第三句"]
        assert plan.duration >= 3.4


class TestSegmentReplacement:
    """配音替换原声：配过的段落盖掉原声，没配的段落仍放源文件的声音。"""

    def _project(self, tmp_path: Path, seconds: float = 10.0):
        service = DubbingService(
            tmp_path / "svc", DubbingSettings(loudness="off", fit=False)
        )
        source = make_tone(tmp_path / "source.wav", seconds)
        detail = service.create(name="样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        narrator = detail["roles"][0]
        service.update_role(project_id, narrator["id"], {"mode": "original"})
        speaker = service.add_role(project_id, {"name": "甲"})
        service.update_role(
            project_id, speaker["id"], {"engine": "fake", "voice_id": "v1"}
        )
        return service, project_id, narrator, speaker

    def test_original_role_is_not_synthesized(self, tmp_path: Path) -> None:
        service, project_id, narrator, speaker = self._project(tmp_path)
        service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 5000, "end_ms": 6000, "text": "旁白。", "role_id": narrator["id"]},
            ],
        )
        targets = service._synth_targets(project_id, None)
        assert [item["text"] for item in targets] == ["甲说。"]

    def test_assemble_keeps_original_outside_dubbed_ranges(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service, project_id, narrator, speaker = self._project(tmp_path)
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 5000, "end_ms": 6000, "text": "旁白。", "role_id": narrator["id"]},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        # 配过音的区间是引擎产物（低电平），没配的区间仍是原声（高电平）。
        assert rms_of(mix, 0.1, 0.9) < 3000
        assert rms_of(mix, 5.1, 5.9) > 5000

    def test_assemble_runs_as_job_with_progress_and_summary(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """接口层把贴齐放进任务：任务条要能看到阶段，完成后报替换/截断。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )

        job = service.start_assemble(project_id)
        assert job["kind"] == "assemble"
        assert job["total"] == 3
        done = service.wait_job(job["id"], timeout=30)
        assert done["state"] == "done", done["message"]
        assert "替换 1 段" in done["message"]
        assert service.detail(project_id)["paths"]["mix_current"] is True

    def test_export_runs_as_job(self, tmp_path: Path, ffmpeg_ready: None) -> None:
        service, project_id, _, speaker = self._project(tmp_path)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )

        job = service.start_export(project_id, allow_incomplete=True)
        assert job["kind"] == "export"
        assert job["total"] == 4
        done = service.wait_job(job["id"], timeout=60)
        assert done["state"] == "done", done["message"]
        assert "已导出" in done["message"]
        exports = service.detail(project_id)["paths"]["exports"]
        assert exports
        assert (service.folder(project_id) / "out" / exports[0]).is_file()

    def test_assembly_setting_change_makes_the_mix_stale(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """旧混音是旧设置做的：改响度后不能继续显示为当前版本。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        assert service.detail(project_id)["paths"]["mix_current"] is True

        changed = DubbingService(
            tmp_path / "svc", DubbingSettings(loudness="video", fit=True)
        )
        detail = changed.detail(project_id)
        assert detail["paths"]["mix_current"] is False
        assert detail["paths"]["export_current"] is False

    def test_engine_identity_change_marks_audio_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_ready: None
    ) -> None:
        """换模型/升级插件后，旧产物的合成键对不上：详情里要退回待重配。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 1.0)
        role = service.store.role(speaker["id"])
        key = service._audio_key(cue, role, identity="1.0|model-a")
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            audio_key=key,
            state="ready",
        )
        monkeypatch.setattr(service, "_identity_for", lambda engine_key: "1.0|model-a")
        assert service.detail(project_id)["cues"][0]["state"] == "ready"

        monkeypatch.setattr(service, "_identity_for", lambda engine_key: "1.0|model-b")
        assert service.detail(project_id)["cues"][0]["state"] == "stale"

    def test_legacy_audio_key_still_matches(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """升级键口径不能把旧项目整批判成待重配：旧键仍认。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 1.0)
        old_key = service._legacy_audio_key(cue, service.store.role(speaker["id"]))
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            audio_key=old_key,
            state="ready",
        )
        assert service.detail(project_id)["cues"][0]["state"] == "ready"

    def test_short_voice_keeps_the_rest_of_the_original_line_silent(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """产物比原句短时，原声要在**整句**区间静音，不能只静音产物时长。

        只按产物时长静音，后半个原句会照常播出来，成片变成"TTS + 原声"两句。
        """
        service, project_id, _, speaker = self._project(tmp_path, seconds=6.0)
        cue = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 5000, "text": "甲说。", "role_id": speaker["id"]}],
        )[0]
        target = service._work(project_id, "tts") / f"{cue['id']}.wav"
        quiet_tone(target, 2.0)
        service.store.update_cue(
            cue["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        result = service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        assert result["truncated"] == []
        assert rms_of(mix, 0.1, 1.9) < 3000
        # 2–5 秒是这一句没被念到的部分：必须静音，不能播放原声。
        assert rms_of(mix, 2.2, 4.8) < 3000
        # 句子结束之后仍是原声。
        assert rms_of(mix, 5.2, 5.8) > 5000
        assert 5.9 <= probe_duration(mix) <= 6.1

    def test_long_voice_is_truncated_instead_of_swallowing_the_next_original(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """配音塞不进原句时按槽位截断，不能靠顺延去盖掉下一句保留原声的台词。"""
        service, project_id, narrator, speaker = self._project(tmp_path, seconds=6.0)
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 1000, "end_ms": 2000, "text": "旁白。", "role_id": narrator["id"]},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 3.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        result = service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        assert result["delayed"] == []
        assert result["truncated"] == [1]
        # 下一句保留原声的台词仍然完整。
        assert rms_of(mix, 1.1, 1.9) > 5000

    def test_deleted_cue_makes_the_mix_stale(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """删段不会留下新的 cue.updated_at，混音过期必须由项目级内容时间兜住。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 5000, "end_ms": 6000, "text": "保留。", "role_id": ""},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        assert service.detail(project_id)["paths"]["mix_current"] is True

        service.delete_cue(project_id, cues[1]["id"])
        detail = service.detail(project_id)
        assert detail["paths"]["mix_current"] is False
        assert detail["paths"]["export_current"] is False

    def test_role_mode_change_makes_the_mix_stale(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """角色改回原声不重算任何 cue，但成片的声音已经变了。"""
        service, project_id, _, speaker = self._project(tmp_path)
        cues = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]}],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        assert service.detail(project_id)["paths"]["mix_current"] is True

        service.update_role(project_id, speaker["id"], {"mode": "original"})
        assert service.detail(project_id)["paths"]["mix_current"] is False

    def test_missing_voice_keeps_original_audio_and_subtitle(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """该配但还没产物的段仍按原声入片，字幕不能把它丢掉。"""
        service, project_id, _, speaker = self._project(tmp_path, seconds=6.0)
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 2000, "end_ms": 3000, "text": "还没配。", "role_id": speaker["id"]},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        result = service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        srt = (service.folder(project_id) / "out" / "样片.srt").read_text(encoding="utf-8")
        assert "还没配。" in srt
        assert result["skipped"] == 1
        # 没有产物的段没有被静音：成片里仍是原声。
        assert rms_of(mix, 2.1, 2.9) > 5000

    def test_muted_original_leaves_only_voice(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """原声音量拉到静音时，成片只剩配过的段，长度也不再是原片长度。"""
        service, project_id, _, speaker = self._project(tmp_path, seconds=3.0)
        cues = service.store.replace_cues(
            project_id,
            [{"start_ms": 0, "end_ms": 1000, "text": "唯一一句。", "role_id": speaker["id"]}],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.store.update_project(project_id, options={"original_gain": -60.0})
        service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        assert rms_of(mix, 0.1, 0.9) < 3000
        with wave.open(str(mix), "rb") as handle:
            assert handle.getnframes() / handle.getframerate() < 1.2

    def test_mask_is_split_into_windows(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """替换区间多到一行命令装不下时按窗口切开，静音仍落在正确的时间点。"""
        service, project_id, _, _ = self._project(tmp_path, seconds=2.0)
        project = service.store.project(project_id)
        assert project is not None
        monkeypatch.setattr(
            "app.producers.builtin.dubbing.service._MASK_RANGES_PER_CALL", 1
        )
        work = service._work(project_id, "assemble")
        bed = service._silenced_original(
            service.original(project),
            [(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)],
            work,
            service._out(project_id) / "bed.wav",
        )
        assert bed.is_file()
        assert rms_of(bed, 0.12, 0.18) < 3000
        assert rms_of(bed, 0.22, 0.28) > 5000
        assert rms_of(bed, 0.52, 0.58) < 3000


class TestStemMix:
    """分轨后的混音：配音段只静音原声人声，伴奏整条继续。"""

    def _project(self, tmp_path: Path, seconds: float = 10.0):
        service = DubbingService(
            tmp_path / "svc", DubbingSettings(loudness="off", fit=False)
        )
        # 原片用静音：没分轨的旧路径只会得到静音，能一眼看出混音到底读了哪条轨。
        source = make_silence(tmp_path / "source.wav", seconds)
        detail = service.create(name="分轨样片", upload=source, suffix=".wav")
        project_id = detail["project"]["id"]
        narrator = detail["roles"][0]
        service.update_role(project_id, narrator["id"], {"mode": "original"})
        speaker = service.add_role(project_id, {"name": "甲"})
        service.update_role(
            project_id, speaker["id"], {"engine": "fake", "voice_id": "v1"}
        )
        return service, project_id, narrator, speaker

    @staticmethod
    def _install_stems(
        service: DubbingService,
        project_id: str,
        vocals: Path,
        accompaniment: Path,
    ) -> None:
        folder = service.folder(project_id) / "audio"
        shutil.copyfile(vocals, folder / "vocals.wav")
        shutil.copyfile(accompaniment, folder / "accompaniment.wav")
        original = folder / "original.wav"
        stat = original.stat()
        options = dict(service.store.project(project_id)["options"])
        options["stems"] = {
            "key": f"{stat.st_size:x}-{stat.st_mtime_ns:x}",
            "model": "fake",
            "label": "假分轨",
        }
        service.store.update_project(project_id, options=options)

    def test_accompaniment_continues_under_dubbed_spans(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service, project_id, narrator, speaker = self._project(tmp_path)
        self._install_stems(
            service,
            project_id,
            make_silence(tmp_path / "vocals.wav", 10.0),
            make_tone(tmp_path / "accompaniment.wav", 10.0),
        )
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 5000, "end_ms": 6000, "text": "旁白。", "role_id": narrator["id"]},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        # 被配音的那一句底下仍有伴奏；旧行为（整条原声静音）这里会是静音。
        assert rms_of(mix, 0.1, 0.9) > 5000
        # 保留原声的段同样有伴奏。
        assert rms_of(mix, 5.1, 5.9) > 5000

    def test_vocal_is_muted_only_over_dubbed_spans(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        service, project_id, narrator, speaker = self._project(tmp_path)
        self._install_stems(
            service,
            project_id,
            make_tone(tmp_path / "vocals.wav", 10.0),
            make_silence(tmp_path / "accompaniment.wav", 10.0),
        )
        cues = service.store.replace_cues(
            project_id,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说。", "role_id": speaker["id"]},
                {"start_ms": 5000, "end_ms": 6000, "text": "旁白。", "role_id": narrator["id"]},
            ],
        )
        target = service._work(project_id, "tts") / f"{cues[0]['id']}.wav"
        quiet_tone(target, 1.0)
        service.store.update_cue(
            cues[0]["id"],
            audio=target.relative_to(service.folder(project_id)).as_posix(),
            state="ready",
        )
        service.assemble(project_id)
        mix = service.folder(project_id) / "out" / "mix.wav"
        # 配音段：原声人声被静音，只剩低电平的配音产物。
        assert rms_of(mix, 0.1, 0.9) < 3000
        # 保留段：声音来自分出来的人声轨，不是原片（原片是静音）。
        assert rms_of(mix, 5.1, 5.9) > 5000

    def test_separation_job_writes_both_stems_then_can_be_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_ready: None
    ) -> None:
        service = DubbingService(
            tmp_path / "svc", DubbingSettings(loudness="off", fit=False)
        )
        source = make_tone(tmp_path / "source.wav", 2.0)
        project_id = service.create(
            name="分轨任务", upload=source, suffix=".wav"
        )["project"]["id"]
        fake = {
            "id": "fake-sep",
            "label": "假分轨",
            "family": "bs_roformer",
            "note": "",
            "prep": [44100, 2],
            "url": "http://127.0.0.1:1",
        }
        monkeypatch.setattr(
            service, "_running_stem_models", lambda *, fresh=False: [dict(fake)]
        )
        from app.producers.builtin.audiocpp import tasks as audiocpp_tasks

        vocals = make_tone(tmp_path / "fake-vocals.wav", 2.0).read_bytes()
        accompaniment = make_tone(tmp_path / "fake-accompaniment.wav", 2.0).read_bytes()
        monkeypatch.setattr(
            audiocpp_tasks,
            "run_task",
            lambda *args, **kwargs: {
                "vocals": vocals,
                "accompaniment": accompaniment,
            },
        )

        job = service.start_separate(project_id, "fake-sep")
        assert job["kind"] == "separate"
        done = service.wait_job(job["id"], timeout=120)
        assert done["state"] == "done", done["message"]
        detail = service.detail(project_id)
        assert detail["separation"]["ready"] is True
        folder = service.folder(project_id) / "audio"
        assert (folder / "vocals.wav").is_file()
        assert (folder / "accompaniment.wav").is_file()

        removed = service.remove_stems(project_id)
        assert removed["separation"]["ready"] is False
        assert not (folder / "vocals.wav").exists()
        assert not (folder / "accompaniment.wav").exists()


class TestSubtitleImport:
    """导入现成字幕：时间码错开时要修，VTT 的标记不能念出来。"""

    def test_vtt_markup_is_not_spoken(self) -> None:
        from app.usecase.subtitles import parse_subtitles

        text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\n<v 旁白>你好</v> &amp; 再见\n"
        assert parse_subtitles(text) == [(1.0, 2.5, "你好 & 再见")]


class TestDubbingApi:
    def test_link_project_reads_source_in_place(
        self, client, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "引用源.wav", 2.0)
        created = client.post(
            "/api/v1/producers/dubbing/projects/link",
            json={"path": str(source), "name": "引用接口"},
        )
        assert created.status_code == 200, created.text
        detail = created.json()
        project_id = detail["project"]["id"]
        assert detail["project"]["source_linked"] is True
        assert detail["source_ok"] is True

        played = client.get(
            f"/api/v1/producers/dubbing/projects/{project_id}/media/source"
        )
        assert played.status_code == 200
        assert played.content

        # 引用路径必须是绝对路径：相对路径不交给进程当前目录去猜。
        refused = client.post(
            "/api/v1/producers/dubbing/projects/link",
            json={"path": "相对路径.wav"},
        )
        assert refused.status_code == 400

        # 原片被删掉：详情要说清，播放接口给出能照着做的提示。
        source.unlink()
        missing = client.get(
            f"/api/v1/producers/dubbing/projects/{project_id}"
        ).json()
        assert missing["source_ok"] is False
        broken = client.get(
            f"/api/v1/producers/dubbing/projects/{project_id}/media/source"
        )
        assert broken.status_code == 400
        assert "重新指定" in broken.json()["error"]["message"]

    def test_import_subtitles_and_batch_edit(
        self, client, tmp_path: Path, data_dir: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "subtitle.wav", 3.0)
        with source.open("rb") as handle:
            created = client.post(
                "/api/v1/producers/dubbing/projects",
                files={"file": ("subtitle.wav", handle, "audio/wav")},
                data={"name": "字幕样片"},
            )
        assert created.status_code == 200, created.text
        project_id = created.json()["project"]["id"]
        role = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/roles",
            json={"name": "甲"},
        ).json()

        srt = (
            "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
            "2\n00:00:00,800 --> 00:00:01,200\n第二句\n\n"
            "3\n00:00:02,000 --> 00:00:02,500\n第三句\n"
        )
        imported = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/subtitles",
            files={"file": ("sample.srt", srt.encode("utf-8"), "application/x-subrip")},
        )
        assert imported.status_code == 200, imported.text
        cues = imported.json()["cues"]
        assert [cue["text"] for cue in cues] == ["第一句", "第二句", "第三句"]
        # 重叠的字幕不能出现起止倒挂：第二条的起点推到第一条结束处。
        assert cues[1]["start_ms"] >= cues[0]["end_ms"]
        assert all(cue["end_ms"] > cue["start_ms"] for cue in cues)

        # 导入完成就要有一份能下载的字幕成品，不必先贴齐、导出。
        # 还没有成片，文件名先用项目名；导出后会改成与成片同名。
        out = data_dir / "producers" / "dubbing" / "projects" / project_id / "out"
        subtitle = out / "字幕样片.srt"
        assert subtitle.is_file()
        assert "第一句" in subtitle.read_text(encoding="utf-8")
        assert "字幕样片.srt" in imported.json()["paths"]["subtitles"]
        download = client.get(
            f"/api/v1/producers/dubbing/projects/{project_id}/media/subtitle",
            params={"name": "字幕样片.srt"},
        )
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]

        batch = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/cues/batch",
            json={"action": "role", "cue_ids": [cue["id"] for cue in cues], "role_id": role["id"]},
        )
        assert batch.status_code == 200, batch.text
        assert all(cue["role_id"] == role["id"] for cue in batch.json()["data"])

        edited = client.put(
            f"/api/v1/producers/dubbing/projects/{project_id}/cues/{cues[2]['id']}",
            json={"text": "改过的第三句"},
        )
        assert edited.status_code == 200, edited.text
        assert "改过的第三句" in subtitle.read_text(encoding="utf-8")

        removed = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/cues/batch",
            json={"action": "delete", "cue_ids": [cues[0]["id"], cues[1]["id"]]},
        )
        assert removed.status_code == 200, removed.text
        left = removed.json()["data"]
        assert [cue["text"] for cue in left] == ["改过的第三句"]
        assert left[0]["ordinal"] == 1
        # 删除段落也要同步字幕成品：留下的文件不能还念着已删的那两句。
        remain = subtitle.read_text(encoding="utf-8")
        assert "改过的第三句" in remain and "第一句" not in remain and "第二句" not in remain

    def test_role_engine_accepts_auto(
        self, client, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """调用方可以只写 auto：没有实例在跑时先留空，钉死一台后自动标记让位。"""
        source = make_tone(tmp_path / "auto.wav", 2.0)
        with source.open("rb") as handle:
            created = client.post(
                "/api/v1/producers/dubbing/projects",
                files={"file": ("auto.wav", handle, "audio/wav")},
                data={"name": "自动引擎"},
            )
        assert created.status_code == 200, created.text
        project_id = created.json()["project"]["id"]

        role = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/roles",
            json={"name": "甲", "engine": "auto", "voice_id": "库里没有这条"},
        )
        assert role.status_code == 200, role.text
        body = role.json()
        assert body["engine_auto"] is True
        assert body["engine"] == ""

        pinned = client.put(
            f"/api/v1/producers/dubbing/projects/{project_id}/roles/{body['id']}",
            json={"engine": "fake"},
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["engine"] == "fake"
        assert pinned.json()["engine_auto"] is False

    def test_upload_edit_and_delete(
        self, client, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        source = make_tone(tmp_path / "输入.wav", 2.0)
        with source.open("rb") as handle:
            response = client.post(
                "/api/v1/producers/dubbing/projects",
                files={"file": ("输入.wav", handle, "audio/wav")},
                data={"name": "接口样片"},
            )
        assert response.status_code == 200, response.text
        detail = response.json()
        project_id = detail["project"]["id"]
        assert detail["project"]["name"] == "接口样片"

        overview = client.get("/api/v1/producers/dubbing/overview").json()
        assert any(item["id"] == project_id for item in overview["projects"])

        cue_response = client.post(
            f"/api/v1/producers/dubbing/projects/{project_id}/roles",
            json={"name": "旁白2"},
        )
        assert cue_response.status_code == 200
        role_id = cue_response.json()["id"]

        update = client.put(
            f"/api/v1/producers/dubbing/projects/{project_id}/cues/不存在",
            json={"text": "x"},
        )
        assert update.status_code == 400

        deleted = client.delete(f"/api/v1/producers/dubbing/projects/{project_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert role_id
