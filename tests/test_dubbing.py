"""配音生产方：切段规则、时间轴拼装与完整链路。

切段与拼装是纯计算，真跑；导入 / 识别 / 导出走 ffmpeg，没装就跳过——
这条路本来就依赖一个外部程序，环境缺它不该把整份测试弄红。
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.infra import ffmpeg as ffmpeg_tools
from app.producers.builtin.dubbing.service import DubbingService, _speech_segments
from app.producers.builtin.dubbing.settings import DubbingSettings


@pytest.fixture()
def ffmpeg_ready() -> None:
    if not ffmpeg_tools.find_ffmpeg():
        pytest.skip("这台机器上没有 ffmpeg")


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
        service.store.update_cue(cue["id"], audio=relative, state="ready")

        updated = service.update_cue(project_id, cue["id"], {"text": "改过的句子。"})
        assert updated["text"] == "改过的句子。"
        # 文本改了，旧音频不再对应：标成过期（文件还在，改回去能听）。
        assert updated["state"] == "stale"
        assert audio.is_file()

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
        srt = (out / "subtitles.srt").read_text(encoding="utf-8")
        assert "第一句。" in srt and "第二句。" in srt

        # 混完还没导出：中间产物不算成品，导出这一步仍应是「等待」。
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "wait"

        # 混完还没导出：中间产物不算成品，导出这一步仍应是「等待」。
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "wait"

        exported = service.export(project_id)
        assert exported["file"].endswith(".m4a")
        assert (out / exported["file"]).is_file()
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "done"
        assert exported["file"] in detail["paths"]["exports"]
        detail = service.detail(project_id)
        step = next(item for item in detail["steps"] if item["key"] == "export")
        assert step["state"] == "done"
        assert exported["file"] in detail["paths"]["exports"]


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


class TestDubbingApi:
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
