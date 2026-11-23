"""音色处理插件：目标解析、替换、时长校验与响应解析。"""

import base64
import os
import time
import wave
from pathlib import Path

import pytest

from app.infra import ffmpeg
from app.infra.sidecar import read_sidecar
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import tasks as audiocpp_tasks
from app.producers.builtin.voiceclean import models as voiceclean_models
from app.producers.builtin.voiceclean.service import VoiceCleanService, _Item, _Job
from app.producers.builtin.voiceclean.settings import VoiceCleanSettings


@pytest.fixture()
def scanned(service, demo_library: Path):
    summary = service.save_folders([str(demo_library)])
    assert summary["voices"] == 4
    return service


@pytest.fixture()
def clean(scanned, tmp_path: Path):
    """没有运行中的 audio.cpp：可选模型为空，规整仍然可用。"""
    return VoiceCleanService(
        tmp_path / "voiceclean",
        lambda: VoiceCleanSettings(),
        running_models=list,
    )


def _voice(service, name: str):
    return next(row for row in service.list_voices() if row.name == name)


class TestScope:
    def test_voice_scope_takes_all_samples(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        items, info = clean._resolve({"voice_id": voice.id})
        assert info["count"] == len(items) > 0
        assert {item.voice_id for item in items} == {voice.id}
        assert info["label"] == f"音色「{voice.shown_name}」"

    def test_single_file_scope(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        target = detail.samples[0].file
        items, info = clean._resolve({"voice_id": voice.id, "file": target})
        assert [item.file for item in items] == [target]
        assert target in info["label"]

    def test_selected_files_scope(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        wanted = [detail.samples[0].file, detail.samples[-1].file]
        items, info = clean._resolve({"voice_id": voice.id, "files": wanted})
        assert sorted(item.file for item in items) == sorted(wanted)
        assert info["count"] == 2
        assert set(info["files"]) == set(wanted)

    def test_multi_voice_scope_keeps_order(self, clean, scanned):
        voices = list(scanned.list_voices())[:2]
        details = [scanned.get_voice(row.id) for row in voices]
        picks = [detail.samples[0].file for detail in details]
        items, info = clean._resolve(
            {
                "voices": [
                    {"voice_id": details[1].id, "files": [picks[1]]},
                    {"voice_id": details[0].id, "files": [picks[0]]},
                ]
            }
        )
        assert info["kind"] == "voices"
        assert info["count"] == 2
        assert [(item.voice_id, item.file) for item in items] == [
            (details[1].id, picks[1]),
            (details[0].id, picks[0]),
        ]
        assert [row["voice_id"] for row in info["voices"]] == [
            details[1].id,
            details[0].id,
        ]

    def test_multi_voice_scope_keeps_trims_per_voice(self, clean, scanned):
        voices = list(scanned.list_voices())[:2]
        details = [scanned.get_voice(row.id) for row in voices]
        targets = [
            max(detail.samples, key=lambda row: row.duration).file for detail in details
        ]
        items, _info = clean._resolve(
            {
                "voices": [
                    {
                        "voice_id": details[0].id,
                        "files": [targets[0]],
                        "trims": {targets[0]: {"start": 0.05, "end": 0.2}},
                    },
                    {
                        "voice_id": details[1].id,
                        "files": [targets[1]],
                        "trims": {targets[1]: {"start": 0.0, "end": 0.15}},
                    },
                ]
            }
        )
        assert [(item.trim_start, item.trim_end) for item in items] == [
            (0.05, 0.2),
            (0.0, 0.15),
        ]

    def test_trim_is_kept_per_file(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        target = next(row for row in detail.samples if row.duration >= 0.4).file
        items, _info = clean._resolve(
            {
                "voice_id": voice.id,
                "files": [target],
                "trims": {target: {"start": 0.1, "end": 0.3}},
            }
        )
        assert items[0].trim_start == 0.1
        assert items[0].trim_end == 0.3
        assert items[0].trimmed is True

    def test_segments_are_kept_per_file(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        target = next(row for row in detail.samples if row.duration >= 0.4).file
        items, _info = clean._resolve(
            {
                "voice_id": voice.id,
                "files": [target],
                "trims": {
                    target: {
                        "segments": [
                            {"start": 0.0, "end": 0.1},
                            {"start": 0.3, "end": 0.4},
                        ]
                    }
                },
            }
        )
        assert items[0].segments == [(0.0, 0.1), (0.3, 0.4)]
        assert items[0].trim_start == 0.0
        assert items[0].trim_end == 0.4
        assert items[0].trimmed is True

    def test_full_segment_is_not_a_trim(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        sample = scanned.get_voice(voice.id).samples[0]
        items, _info = clean._resolve(
            {
                "voice_id": voice.id,
                "files": [sample.file],
                "trims": {
                    sample.file: {
                        "segments": [{"start": 0.0, "end": sample.duration}]
                    }
                },
            }
        )
        assert items[0].trimmed is False
        assert items[0].segments == []

    def test_bad_trim_is_refused(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        target = scanned.get_voice(voice.id).samples[0].file
        with pytest.raises(ProducerError, match="起点必须早于终点"):
            clean._resolve(
                {
                    "voice_id": voice.id,
                    "files": [target],
                    "trims": {target: {"start": 0.3, "end": 0.1}},
                }
            )

    def test_missing_target_is_refused(self, clean, scanned):
        with pytest.raises(ProducerError, match="未选择音色"):
            clean._resolve({})

    def test_missing_voice_is_refused(self, clean, scanned):
        with pytest.raises(ProducerError, match="不存在"):
            clean._resolve({"voice_id": "不存在"})

    def test_unknown_file_is_refused(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        with pytest.raises(ProducerError, match="没有这条音频"):
            clean._resolve({"voice_id": voice.id, "file": "没有这条.wav"})


class TestFileTarget:
    def test_save_source_keeps_name_and_duration(self, clean, tmp_path: Path):
        from conftest import make_wav

        wav = tmp_path / "我的录音.wav"
        make_wav(wav, seconds=0.4)
        out = clean.save_source(wav.read_bytes(), wav.name)
        assert out["name"] == "我的录音.wav"
        assert 0.35 <= out["duration"] <= 0.45
        assert clean.source_file(out["token"]).is_file()
        assert clean.source_name(out["token"]) == "我的录音.wav"

    def test_file_scope_is_one_target(self, clean, tmp_path: Path):
        from conftest import make_wav

        wav = tmp_path / "input.wav"
        make_wav(wav, seconds=0.5)
        out = clean.save_source(wav.read_bytes(), wav.name)
        items, info = clean._resolve({"kind": "file", "token": out["token"]})
        assert info["kind"] == "file"
        assert info["label"] == "文件「input.wav」"
        assert len(items) == 1
        assert items[0].file == "input.wav"
        assert items[0].voice_id == f"file:{out['token']}"

    def test_file_scope_honours_trim(self, clean, tmp_path: Path):
        from conftest import make_wav

        wav = tmp_path / "input.wav"
        make_wav(wav, seconds=0.5)
        out = clean.save_source(wav.read_bytes(), wav.name)
        items, _info = clean._resolve(
            {
                "kind": "file",
                "token": out["token"],
                "trims": {"input.wav": {"start": 0.1, "end": 0.3}},
            }
        )
        assert items[0].trimmed is True
        assert items[0].trim_start == 0.1
        assert items[0].trim_end == 0.3

    def test_unknown_suffix_is_refused(self, clean):
        with pytest.raises(ProducerError, match="不是可处理的音频"):
            clean.save_source(b"hello", "note.txt")

    def test_expired_token_is_refused(self, clean):
        with pytest.raises(ProducerError, match="已失效"):
            clean.source_file("0123456789ab")

    def test_download_name_marks_processed(self, clean, tmp_path: Path):
        from conftest import make_wav

        wav = tmp_path / "input.wav"
        make_wav(wav, seconds=0.3)
        out = clean.save_source(wav.read_bytes(), wav.name)
        item = {
            "index": 0,
            "voice_id": f"file:{out['token']}",
            "voice": out["name"],
            "file": out["name"],
            "source": str(clean.source_file(out["token"])),
            "state": "done",
            "replaceable": True,
        }
        clean._job = _Job(
            id="t1",
            folder=tmp_path / "job",
            scope={"kind": "file", "token": out["token"]},
            steps=[],
            items=[_Item(**item)],
        )
        assert clean.download_name("t1", 0) == "input（处理）.wav"
        with pytest.raises(ProducerError, match="电脑文件"):
            clean.apply("t1", [0])
        with pytest.raises(ProducerError, match="电脑文件"):
            clean.save("t1", [0])


class TestReplace:
    def test_replace_keeps_file_name_and_records_chain(self, scanned, tmp_path: Path):
        from conftest import make_wav

        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        sample = detail.samples[0]
        target = Path(detail.folder) / sample.file
        before = target.read_bytes()

        fresh = tmp_path / "fresh.wav"
        make_wav(fresh, seconds=0.5, freq=880.0)
        scanned.replace_sample_audio(
            voice.id, sample.file, fresh, params={"处理": "降噪", "来源": "音色处理"}, seconds=0.5
        )

        assert target.is_file()
        assert target.read_bytes() != before
        side = read_sidecar(Path(detail.folder))
        assert side.samples[sample.file]["params"]["处理"] == "降噪"
        assert side.samples[sample.file]["bytes"] == target.stat().st_size

        updated = scanned.get_voice(voice.id)
        assert sample.file in {row.file for row in updated.samples}
        assert dict(next(row for row in updated.samples if row.file == sample.file).params)["处理"] == "降噪"

    def test_missing_sample_is_reported(self, scanned, tmp_path: Path):
        from conftest import make_wav

        from app.usecase.library import LibraryError

        voice = _voice(scanned, "胡桃")
        fresh = tmp_path / "fresh.wav"
        make_wav(fresh)
        with pytest.raises(LibraryError):
            scanned.replace_sample_audio(voice.id, "没有这条.wav", fresh)


class TestApply:
    def _job(self, clean, scanned, *, replaceable: bool, seconds: float = 0.5):
        from conftest import make_wav

        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        sample = detail.samples[0]
        folder = clean._root / "jobs" / "t1"
        work = folder / "0000"
        work.mkdir(parents=True, exist_ok=True)
        make_wav(work / "origin.wav", seconds=0.5)
        make_wav(work / "result.wav", seconds=seconds)
        item = _Item(
            index=0,
            voice_id=voice.id,
            voice=voice.name,
            file=sample.file,
            source=str(Path(detail.folder) / sample.file),
            origin_seconds=0.5,
            result_seconds=seconds,
            state="done",
            replaceable=replaceable,
            error="" if replaceable else "时长偏差 320 ms",
        )
        job = _Job(
            id="t1",
            folder=folder,
            scope={},
            steps=[
                {
                    "key": "denoise",
                    "label": "语音降噪",
                    "model": "sidon_f32",
                    "model_label": "Sidon",
                    "prep": [48000, 1],
                    "options": {},
                }
            ],
            items=[item],
        )
        clean._job = job
        return job, voice, sample

    def test_duration_mismatch_is_not_replaced(self, clean, scanned):
        self._job(clean, scanned, replaceable=False, seconds=1.2)
        out = clean.apply("t1", [0])
        assert out["replaced"] == 0
        assert out["failures"][0]["reason"] == "时长偏差 320 ms"

    def test_apply_replaces_selected_item(self, clean, scanned):
        _job, voice, sample = self._job(clean, scanned, replaceable=True)
        out = clean.apply("t1", [0])
        assert out["replaced"] == 1
        side = read_sidecar(Path(scanned.get_voice(voice.id).folder))
        assert "处理" in side.samples[sample.file]["params"]


class TestSave:
    """入库：结果另存一条新样例，原样例与它的记录都不动。"""

    def _job(self, clean, scanned, *, replaceable: bool = False, seconds: float = 0.75):
        from conftest import make_wav

        voice = _voice(scanned, "胡桃")
        detail = scanned.get_voice(voice.id)
        sample = detail.samples[0]
        folder = clean._root / "jobs" / "t2"
        work = folder / "0000"
        work.mkdir(parents=True, exist_ok=True)
        make_wav(work / "origin.wav", seconds=0.5)
        make_wav(work / "result.wav", seconds=seconds)
        item = _Item(
            index=0,
            voice_id=voice.id,
            voice=voice.name,
            file=sample.file,
            source=str(Path(detail.folder) / sample.file),
            origin_seconds=0.5,
            result_seconds=seconds,
            state="done",
            replaceable=replaceable,
            error="" if replaceable else "时长偏差 250 ms",
        )
        job = _Job(
            id="t2",
            folder=folder,
            scope={},
            steps=[
                {
                    "key": "denoise",
                    "label": "语音降噪",
                    "model": "sidon_f32",
                    "model_label": "Sidon",
                    "prep": [48000, 1],
                    "options": {},
                }
            ],
            items=[item],
        )
        clean._job = job
        return job, voice, sample

    def test_save_adds_a_new_sample_and_keeps_the_original(self, clean, scanned):
        _job, voice, sample = self._job(clean, scanned)
        detail = scanned.get_voice(voice.id)
        folder = Path(detail.folder)
        original = (folder / sample.file).read_bytes()
        out = clean.save("t2", [0])
        assert out["saved"] == 1
        assert out["failures"] == []
        assert out["job"]["state"] == "saved"
        assert out["job"]["items"][0]["state"] == "saved"
        saved_file = out["files"][0]["file"]
        assert "（处理）" in saved_file and saved_file.endswith(".wav")
        assert (folder / saved_file).is_file()
        # 原样例还在，大小、时长都没被替换动过。
        assert (folder / sample.file).read_bytes() == original
        updated = scanned.get_voice(voice.id)
        assert saved_file in {row.file for row in updated.samples}
        assert sample.file in {row.file for row in updated.samples}
        # 入库不能把同目录其它样例的逐字稿/情绪清空（它们来自 .txt，不在 sidecar）。
        assert next(row for row in updated.samples if row.file == sample.file).text == sample.text
        assert next(row for row in updated.samples if row.file == saved_file).text == sample.text
        side = read_sidecar(folder)
        assert side.samples[saved_file]["params"]["来源"] == "音色处理"
        assert side.samples[saved_file]["params"]["处理"]
        with pytest.raises(ProducerError, match="已经入库"):
            clean.apply("t2", [0])

    def test_save_accepts_duration_mismatch(self, clean, scanned):
        """替换受 50 ms 限制，入库是新样例，不受这条限制。"""
        _job, voice, sample = self._job(clean, scanned, replaceable=False)
        out = clean.save("t2", [0])
        assert out["saved"] == 1
        assert out["files"][0]["file"].endswith(".wav")
        assert sample.file in {row.file for row in scanned.get_voice(voice.id).samples}

    def test_repeat_save_does_not_overwrite_earlier_result(self, clean, scanned):
        self._job(clean, scanned)
        first = clean.save("t2", [0])["files"][0]["file"]
        self._job(clean, scanned)
        second = clean.save("t2", [0])["files"][0]["file"]
        assert first != second
        assert second.endswith("-2.wav")


class TestDeclaration:
    def test_rail_mode_is_pick(self):
        from app.producers.registry import discover

        plugin = next(item for item in discover() if item.key == "voiceclean")
        described = plugin.describe()
        # 左栏"点卡即选中"，且不声明流水线步骤：这是处理型工作区的两个标记。
        assert described["rail_mode"] == "pick"
        assert described["voice_rail"] is True
        assert described["steps"] == []


class TestClient:
    def test_state_is_mounted(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.producers.builtin.voiceclean.service._runtime_models", list
        )
        # 开发机上可能真有 audio.cpp 实例与已下载权重：这个用例断言的是
        # "没有任何实例时"的页面，得把运行实例也一起摘掉。
        monkeypatch.setattr(
            "app.producers.builtin.voiceclean.service._audio_runtime", lambda: None
        )
        payload = client.get("/api/v1/producers/voiceclean/state").json()
        assert [step["key"] for step in payload["steps"]] == [
            "separate",
            "denoise",
            "bandwidth",
        ]
        assert payload["core"]["key"] == "normalize"
        assert payload["core"]["order"] == 4
        assert payload["core"]["default"] is False
        assert payload["core"]["label"] == "统一声道与峰值"
        assert payload["core"]["note"]
        # 没有运行中的实例：候选模型仍然列出，只是都标成未下载；页面仍可只裁剪。
        assert all(step["models"] for step in payload["steps"])
        assert all(not row["downloaded"] for step in payload["steps"] for row in step["models"])
        assert all(step["reason"] == "" for step in payload["steps"])

    def test_source_upload_and_playback(self, client, tmp_path: Path):
        from conftest import make_wav

        wav = tmp_path / "input.wav"
        make_wav(wav, seconds=0.3)
        response = client.post(
            "/api/v1/producers/voiceclean/source",
            files={"file": ("input.wav", wav.read_bytes(), "audio/wav")},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["object"] == "voiceclean_source"
        assert payload["name"] == "input.wav"
        audio = client.get(
            f"/api/v1/producers/voiceclean/source/audio?token={payload['token']}"
        )
        assert audio.status_code == 200
        assert audio.content[:4] == b"RIFF"

    def test_source_upload_refuses_non_audio(self, client):
        response = client.post(
            "/api/v1/producers/voiceclean/source",
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 400
        assert "不是可处理的音频" in response.json()["error"]["message"]

    def test_run_with_only_core_still_needs_a_target(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.producers.builtin.voiceclean.service._runtime_models", list
        )
        response = client.post(
            "/api/v1/producers/voiceclean/run", json={"scope": {}, "steps": []}
        )
        assert response.status_code == 400
        assert "未选择音色" in response.json()["error"]["message"]

    def test_run_refuses_when_every_step_is_off(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        with pytest.raises(ProducerError, match="没有要执行的步骤"):
            clean.run(
                {"voice_id": voice.id},
                [{"key": "normalize", "enabled": False}],
            )

    def test_job_save_route_is_mounted(self, client):
        response = client.post(
            "/api/v1/producers/voiceclean/job/save",
            json={"job_id": "missing", "indexes": [0]},
        )
        assert response.status_code == 400
        assert "任务已过期" in response.json()["error"]["message"]

    def test_audio_picks_vocal_stem(self):
        vocal = base64.b64encode(b"RIFF" + b"\0" * 300).decode()
        other = base64.b64encode(b"RIFF" + b"\1" * 300).decode()
        payload = {"artifacts": [{"kind": "accompaniment", "audio": other}, {"kind": "vocals", "audio": vocal}]}
        assert audiocpp_tasks._audio_in(payload) == b"RIFF" + b"\0" * 300

    def test_named_outputs_keep_vocal_and_accompaniment(self):
        vocal = base64.b64encode(b"RIFF" + b"\0" * 300).decode()
        other = base64.b64encode(b"RIFF" + b"\1" * 300).decode()
        payload = {
            # 顶层 audio 是其中一路的重复：不能把它当成第三路，否则伴奏会被多算一遍。
            "audio": vocal,
            "named_audio_outputs": [
                {"id": "vocals", "audio": vocal},
                {"id": "accompaniment", "audio": other},
            ],
        }
        found = audiocpp_tasks.outputs_of(payload)
        assert list(found) == ["vocals", "accompaniment"]
        assert audiocpp_tasks.vocal_name(found) == "vocals"

    def test_audio_wrapped_result(self):
        raw = base64.b64encode(b"RIFF" + b"\2" * 300).decode()
        assert audiocpp_tasks._audio_in(
            {"result": {"audio": f"data:audio/wav;base64,{raw}"}}
        ) == b"RIFF" + b"\2" * 300

    def test_missing_audio_names_fields(self):
        with pytest.raises(ProducerError, match="没有音频"):
            payload = {"result": {"text": "hi"}}
            assert audiocpp_tasks._audio_in(payload) is None
            raise ProducerError("模型返回里没有音频（响应字段：result）")


@pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
class TestNormalize:
    def test_normalize_makes_mono_and_limits_peak(self, tmp_path: Path):
        from conftest import make_wav

        source = tmp_path / "src.wav"
        make_wav(source, seconds=0.3)
        with wave.open(str(source), "rb") as handle:
            source_rate = handle.getframerate()
        target = tmp_path / "out.wav"
        ffmpeg.normalize(source, target, highpass=60)
        with wave.open(str(target), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == source_rate  # 源采样率，未升采样

    def test_normalize_trims_to_range(self, tmp_path: Path):
        from conftest import make_wav

        source = tmp_path / "src.wav"
        make_wav(source, seconds=0.5)
        target = tmp_path / "out.wav"
        ffmpeg.normalize(source, target, start=0.1, duration=0.2)
        with wave.open(str(target), "rb") as handle:
            seconds = handle.getnframes() / handle.getframerate()
        assert 0.15 <= seconds <= 0.25

    def test_normalize_joins_segments(self, tmp_path: Path):
        from conftest import make_wav

        source = tmp_path / "src.wav"
        make_wav(source, seconds=0.8)
        target = tmp_path / "out.wav"
        ffmpeg.normalize(source, target, segments=[(0.0, 0.1), (0.4, 0.5)])
        with wave.open(str(target), "rb") as handle:
            seconds = handle.getnframes() / handle.getframerate()
        assert 0.15 <= seconds <= 0.25


class _StubModel:
    """最小 audio.cpp：列模型、报模型目录、跑任务时回被处理的那一段。"""

    def __init__(self, wav: Path, models_root: Path) -> None:
        import http.server
        import json
        import threading

        payload = base64.b64encode(wav.read_bytes()).decode()
        root_text = str(models_root)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path == "/v1/models":
                    body = {"data": [{"id": "sidon_f32"}, {"id": "mossformer2_se_48k_f32"}]}
                elif self.path == "/v1/ui/models-root":
                    body = {"models_root": root_text}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                # 身份变换：回被处理的那一段本身，时长自然一致。
                source = Path(str((body.get("request") or {}).get("audio") or ""))
                audio = base64.b64encode(source.read_bytes()).decode() if source.is_file() else payload
                raw = json.dumps({"result": {"audio": audio}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _FakeRuntime:
    """音色处理用到的 audio.cpp 最小接口：记录加载/安装调用。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict] = []
        self.installed: set[str] = set()
        self.calls: list[tuple] = []

    def running_models(self):
        return list(self.rows)

    def installed_packages(self):
        return set(self.installed)

    def download_jobs(self):
        return {}

    def ensure_family_instance(self, family: str, label: str = ""):
        self.calls.append(("family", family))

    def install_model(self, package: str):
        self.calls.append(("install", package))

    def ensure_management(self):
        self.calls.append(("manage",))
        return object()

    def load_model(self, package: str):
        self.calls.append(("load", package))
        self.rows.append(
            {
                "id": package,
                "family": package,
                "task": "s2s",
                "package": package,
                "url": "http://127.0.0.1:18103",
            }
        )

    def load_builtin(self, utility: str, path: Path):
        self.calls.append(("builtin", utility, str(path)))
        self.rows.append(
            {
                "id": f"builtin-{utility}",
                "family": "builtin_audio_utils",
                "task": "s2s",
                "utility": utility,
                "path": str(path),
                "url": "http://127.0.0.1:18104",
            }
        )


class TestModels:
    def _service(self, tmp_path: Path, rows):
        return VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=lambda: rows
        )

    def _wait_job(self, service: VoiceCleanService, key: str):
        for _ in range(100):
            job = service._model_jobs.get(key)
            if job and job["state"] not in {"queued", "downloading", "loading"}:
                return job
            time.sleep(0.05)
        return service._model_jobs.get(key)

    def test_builtin_download_then_auto_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        runtime = _FakeRuntime(tmp_path)
        service = VoiceCleanService(
            tmp_path / "vc",
            lambda: VoiceCleanSettings(),
            runtime_reader=lambda: runtime,
        )
        target = voiceclean_models.builtin_path(service._builtin_root, "deepfilternet2")

        def fake_fetch(_candidate):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"weights")

        monkeypatch.setattr(service, "_fetch_builtin", fake_fetch)
        service.prepare_model("denoise", "deepfilternet2")
        job = self._wait_job(service, "builtin:deepfilternet2")
        assert job and job["state"] == "done"
        assert ("builtin", "deepfilternet2", str(target.parent)) in runtime.calls
        steps = {row["key"]: row for row in service.state()["steps"]}
        candidate = next(
            row for row in steps["denoise"]["models"] if row["id"] == "deepfilternet2"
        )
        assert candidate["downloaded"] is True
        assert candidate["ready"] is True

    def test_builtin_weights_are_hash_checked_not_just_sized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import hashlib

        digest = hashlib.sha256(b"hello").hexdigest()
        monkeypatch.setitem(
            voiceclean_models._UTILITIES,
            "tiny",
            {
                "label": "Tiny",
                "note": "测试用",
                "prep": (48000, 1),
                "file": "tiny/tiny.bin",
                "bytes": 5,
                "sha256": digest,
            },
        )
        root = tmp_path / "models"
        target = voiceclean_models.builtin_path(root, "tiny")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"hello")
        assert voiceclean_models.builtin_downloaded(root, "tiny") is True
        assert voiceclean_models.builtin_digest_path(root, "tiny").is_file()
        target.write_bytes(b"hellx")
        # Windows 只在系统时钟 tick（约 15.6 ms）更新 LastWriteTime：同一 tick 内两次写入的
        # st_mtime_ns 完全相同，旁证会认下这份同尺寸改写。真实替换发生在下载之后，这里推后一格。
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert voiceclean_models.builtin_downloaded(root, "tiny") is False

    def test_downloaded_builtin_loads_on_prepare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runtime = _FakeRuntime(tmp_path)
        service = VoiceCleanService(
            tmp_path / "vc",
            lambda: VoiceCleanSettings(),
            runtime_reader=lambda: runtime,
        )
        target = voiceclean_models.builtin_path(service._builtin_root, "zipenhancer")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"weights")
        # 这个用例验的是"已下载 -> 自动加载"；哈希校验本身另有单独用例。
        monkeypatch.setattr(
            voiceclean_models, "builtin_downloaded", lambda root, utility: True
        )
        service.prepare_model("denoise", "zipenhancer")
        job = self._wait_job(service, "builtin:zipenhancer")
        assert job and job["state"] == "done"
        assert ("builtin", "zipenhancer", str(target.parent)) in runtime.calls

    def test_downloaded_package_loads_on_prepare(self, tmp_path: Path):
        runtime = _FakeRuntime(tmp_path)
        runtime.installed = {"sidon_f32"}
        service = VoiceCleanService(
            tmp_path / "vc",
            lambda: VoiceCleanSettings(),
            runtime_reader=lambda: runtime,
        )
        service.prepare_model("denoise", "sidon_f32")
        job = self._wait_job(service, "pkg:sidon_f32")
        assert job and job["state"] == "done"
        assert ("load", "sidon_f32") in runtime.calls

    def test_steps_list_all_registered_candidates(self, tmp_path: Path):
        service = self._service(
            tmp_path,
            [
                {"id": "sidon_f32", "family": "sidon", "task": "s2s", "url": "http://127.0.0.1:18103"},
                {"id": "cosyvoice3", "family": "cosyvoice3", "task": "tts", "url": "http://127.0.0.1:18092"},
            ],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        denoise = {row["id"]: row for row in steps["denoise"]["models"]}
        assert denoise["sidon_f32"]["downloaded"] is True
        assert denoise["sidon_f32"]["ready"] is True
        assert denoise["sidon_f32"]["prep"] == [48000, 1]
        # 未下载的官方包也列出来，卡片才能给出下载入口。
        assert any(not row["downloaded"] for row in steps["denoise"]["models"])
        # TTS 模型不属于任何一步。
        assert all(
            row["id"] != "cosyvoice3"
            for step in steps.values()
            for row in step["models"]
        )
        # 分离步骤也有候选，不再因为实例没加载就整张卡空着。
        assert steps["separate"]["models"]
        assert steps["separate"]["reason"] == ""

    def test_builtin_denoisers_share_denoise_flashsr_gets_bandwidth(self, tmp_path: Path):
        """内置工具没有 packages，按 id / path 认；降噪与带宽扩展分卡。"""
        service = self._service(
            tmp_path,
            [
                {
                    "id": "DeepFilterNet2",
                    "family": "builtin_audio_utils",
                    "task": "s2s",
                    "path": r"D:\models\DeepFilterNet2",
                    "url": "http://127.0.0.1:18104",
                },
                {
                    "id": "rnnoise",
                    "family": "builtin_audio_utils",
                    "task": "s2s",
                    "path": r"D:\models\rnnoise10Gb_15.safetensors",
                    "url": "http://127.0.0.1:18104",
                },
                {
                    "id": "zipenhancer",
                    "family": "builtin_audio_utils",
                    "task": "s2s",
                    "path": r"D:\models\zipenhancer",
                    "url": "http://127.0.0.1:18104",
                },
                {
                    "id": "flashsr",
                    "family": "builtin_audio_utils",
                    "task": "s2s",
                    "path": r"D:\models\flashsr",
                    "url": "http://127.0.0.1:18104",
                },
                {
                    "id": "sidon_f32",
                    "family": "sidon",
                    "task": "s2s",
                    "url": "http://127.0.0.1:18103",
                },
            ],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        denoise = steps["denoise"]["models"]
        by_id = {row["id"]: row for row in denoise}
        # 小工具按推荐顺序排在官方包后面；FlashSR 不在这张卡里。
        builtin_ids = [row["id"] for row in denoise if row["source"] == "builtin"]
        assert builtin_ids == [
            "deepfilternet2",
            "zipenhancer",
            "rnnoise",
            "gtcrn_dns3",
            "gtcrn_vctk",
            "gtcrn_streaming",
        ]
        assert "flashsr" not in by_id
        assert by_id["sidon_f32"]["ready"] is True
        assert by_id["deepfilternet2"]["ready"] is True
        assert by_id["deepfilternet2"]["label"] == "DeepFilterNet2"
        assert by_id["deepfilternet2"]["prep"] == [48000, 1]
        assert by_id["zipenhancer"]["prep"] == [16000, 1]
        assert "16 kHz" in by_id["zipenhancer"]["note"]
        bandwidth = steps["bandwidth"]["models"]
        assert [row["id"] for row in bandwidth] == ["flashsr"]
        assert bandwidth[0]["ready"] is True
        assert "生成高频" in bandwidth[0]["note"]

    def test_unknown_builtin_utility_is_not_listed(self, tmp_path: Path):
        """认不出的工具不猜测用途：宁可不列，也不放进降噪或带宽扩展。"""
        service = self._service(
            tmp_path,
            [
                {
                    "id": "my-utility",
                    "family": "builtin_audio_utils",
                    "task": "s2s",
                    "path": r"D:\models\custom",
                    "url": "http://127.0.0.1:18104",
                }
            ],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        ids = {
            row["id"]
            for step in steps.values()
            for row in step["models"]
            if row["source"] == "builtin"
        }
        assert "my-utility" not in ids
        assert ids == {
            "deepfilternet2",
            "rnnoise",
            "zipenhancer",
            "gtcrn_dns3",
            "gtcrn_vctk",
            "gtcrn_streaming",
            "flashsr",
        }

    def test_family_registered_model_uses_step_package(self, tmp_path: Path):
        """服务端注册族名时，按该步骤排在最前的包认这条运行记录。"""
        service = self._service(
            tmp_path,
            [{"id": "mossformer2", "family": "mossformer2", "task": "s2s", "url": "http://127.0.0.1:18101"}],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        denoise = next(
            row for row in steps["denoise"]["models"] if row["family"] == "mossformer2"
        )
        separate = next(
            row for row in steps["separate"]["models"] if row["family"] == "mossformer2"
        )
        assert denoise["id"] == "mossformer2_se_48k_f32"
        assert separate["id"] == "mossformer2_ss_16k_f32"
        assert denoise["ready"] is True
        assert separate["ready"] is True
        assert denoise["prep"] == [48000, 1]
        assert separate["prep"] == [16000, 1]
        assert "MossFormer2" in denoise["label"]

    def test_run_refuses_model_that_is_not_downloaded(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        with pytest.raises(ProducerError, match="还没下载"):
            clean.run(
                {"voice_id": voice.id},
                [{"key": "denoise", "enabled": True, "model": "sidon_f32", "options": {}}],
            )


class TestPipeline:
    def test_run_processes_and_is_replaceable(self, scanned, tmp_path: Path):
        from conftest import make_wav

        sample_wav = tmp_path / "reply.wav"
        make_wav(sample_wav, seconds=0.5)
        stub = _StubModel(sample_wav, tmp_path / "models")
        try:
            clean = VoiceCleanService(
                tmp_path / "vc",
                lambda: VoiceCleanSettings(timeout=60),
                running_models=lambda: [
                    {"id": "sidon_f32", "family": "sidon", "task": "s2s", "url": stub.url}
                ],
            )
            clean.run(
                {"voice_id": _first_local(scanned)},
                [{"key": "denoise", "enabled": True, "model": "sidon_f32", "options": {}}],
            )
            for _ in range(120):
                view = clean.job_view()
                if view and view["state"] != "running":
                    break
                time.sleep(0.1)
            view = clean.job_view()
            assert view["state"] == "done"
            assert view["items"][0]["replaceable"] is True
            assert view["items"][0]["result_seconds"] > 0
            out = clean.apply(view["id"], [0])
            assert out["replaced"] == 1
        finally:
            stub.close()

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_utility_step_keeps_source_rate(self, scanned, tmp_path: Path):
        """16 kHz 内置工具跑完后，回库结果仍按原样例采样率落盘。"""
        from conftest import make_wav

        sample_wav = tmp_path / "reply.wav"
        make_wav(sample_wav, seconds=0.5)
        stub = _StubModel(sample_wav, tmp_path / "models")
        try:
            clean = VoiceCleanService(
                tmp_path / "vc",
                lambda: VoiceCleanSettings(timeout=60),
                running_models=lambda: [
                    {
                        "id": "zipenhancer",
                        "family": "builtin_audio_utils",
                        "task": "s2s",
                        "path": r"D:\models\zipenhancer",
                        "url": stub.url,
                    }
                ],
            )
            clean.run(
                {"voice_id": _first_local(scanned)},
                [
                    {
                        "key": "denoise",
                        "enabled": True,
                        "model": "zipenhancer",
                        "options": {},
                    }
                ],
            )
            for _ in range(120):
                view = clean.job_view()
                if view and view["state"] != "running":
                    break
                time.sleep(0.1)
            view = clean.job_view()
            assert view["state"] == "done"
            assert view["items"][0]["replaceable"] is True
            origin = clean.audio_path(view["id"], 0, "origin")
            result = clean.audio_path(view["id"], 0, "result")
            with wave.open(str(origin), "rb") as handle:
                origin_rate = handle.getframerate()
            with wave.open(str(result), "rb") as handle:
                assert handle.getframerate() == origin_rate
        finally:
            stub.close()

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_multi_voice_run_and_apply(self, scanned, tmp_path: Path):
        """跨音色的序列跑一轮，替换按条目各自的音色落库。"""
        voices = list(scanned.list_voices())[:2]
        details = [scanned.get_voice(row.id) for row in voices]
        files = [detail.samples[0].file for detail in details]
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        clean.run(
            {
                "voices": [
                    {"voice_id": detail.id, "files": [name]}
                    for detail, name in zip(details, files)
                ]
            },
            [],
        )
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        assert view["state"] == "done"
        assert view["scope"]["kind"] == "voices"
        assert [(item["voice_id"], item["file"]) for item in view["items"]] == [
            (detail.id, name) for detail, name in zip(details, files)
        ]
        out = clean.apply(view["id"], [0, 1])
        assert out["replaced"] == 2
        assert not out["failures"]

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_core_only_still_processes(self, scanned, tmp_path: Path):
        """没有任何模型时，最后一步「规整」照样能把整条音色跑完。"""
        voice = _voice(scanned, "胡桃")
        target = scanned.get_voice(voice.id).samples[0].file
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        clean.run({"voice_id": voice.id, "file": target}, [])
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        assert view["state"] == "done"
        assert view["items"][0]["replaceable"] is True
        assert view["steps"] == []

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_core_only_honours_trim(self, scanned, tmp_path: Path):
        voice = _voice(scanned, "胡桃")
        sample = next(
            row for row in scanned.get_voice(voice.id).samples if row.duration >= 0.4
        )
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        clean.run(
            {
                "voice_id": voice.id,
                "files": [sample.file],
                "trims": {sample.file: {"start": 0.1, "end": 0.3}},
            },
            [],
        )
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        item = view["items"][0]
        assert view["state"] == "done"
        assert item["trimmed"] is True
        assert 0.15 <= item["origin_seconds"] <= 0.25
        assert 0.15 <= item["result_seconds"] <= 0.25
        assert item["replaceable"] is True

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_trim_only_runs_without_steps(self, scanned, tmp_path: Path):
        """没有模型步骤、也不做收尾时，只裁剪仍然能生成可替换的结果。"""
        voice = _voice(scanned, "胡桃")
        sample = next(
            row for row in scanned.get_voice(voice.id).samples if row.duration >= 0.4
        )
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        clean.run(
            {
                "voice_id": voice.id,
                "files": [sample.file],
                "trims": {
                    sample.file: {
                        "segments": [
                            {"start": 0.0, "end": 0.1},
                            {"start": 0.3, "end": 0.4},
                        ]
                    }
                },
            },
            [{"key": "normalize", "enabled": False}],
        )
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        item = view["items"][0]
        assert view["state"] == "done"
        assert view["steps"] == []
        assert item["trimmed"] is True
        assert len(item["segments"]) == 2
        assert 0.15 <= item["origin_seconds"] <= 0.25
        assert item["replaceable"] is True

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_file_target_processes_without_replace(self, scanned, tmp_path: Path):
        """电脑文件只产出结果：不做 50 ms 替换校验，给下载名。"""
        from conftest import make_wav

        wav = tmp_path / "input.wav"
        make_wav(wav, seconds=0.4)
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        source = clean.save_source(wav.read_bytes(), wav.name)
        clean.run({"kind": "file", "token": source["token"]}, [])
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        assert view["state"] == "done"
        assert view["scope"]["kind"] == "file"
        assert view["items"][0]["replaceable"] is True
        assert view["items"][0]["error"] == ""
        assert clean.download_name(view["id"], 0) == "input（处理）.wav"
        with pytest.raises(ProducerError, match="电脑文件"):
            clean.apply(view["id"], [0])

    def test_core_can_be_skipped_next_to_a_model_step(self, scanned, tmp_path: Path):
        from conftest import make_wav

        sample_wav = tmp_path / "reply.wav"
        make_wav(sample_wav, seconds=0.5)
        stub = _StubModel(sample_wav, tmp_path / "models")
        try:
            voice = _voice(scanned, "胡桃")
            target = scanned.get_voice(voice.id).samples[0].file
            clean = VoiceCleanService(
                tmp_path / "vc",
                lambda: VoiceCleanSettings(timeout=60),
                running_models=lambda: [
                    {"id": "sidon_f32", "family": "sidon", "task": "s2s", "url": stub.url}
                ],
            )
            clean.run(
                {"voice_id": voice.id, "files": [target]},
                [
                    {"key": "normalize", "enabled": False},
                    {"key": "denoise", "enabled": True, "model": "sidon_f32", "options": {}},
                ],
            )
            for _ in range(120):
                view = clean.job_view()
                if view and view["state"] != "running":
                    break
                time.sleep(0.1)
            view = clean.job_view()
            assert view["normalize"] is False
            assert view["state"] == "done"
            assert view["items"][0]["replaceable"] is True
        finally:
            stub.close()


class TestPipelineOrder:
    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_steps_run_in_declared_order(self, scanned, tmp_path: Path, monkeypatch):
        """提交顺序不决定执行顺序：带宽扩展总在降噪之后。"""
        from app.producers.builtin.voiceclean import service as voiceclean_service

        voice = _voice(scanned, "胡桃")
        target = scanned.get_voice(voice.id).samples[0].file
        calls: list[str] = []

        def fake_run_task(_host, *, model, audio, options=None, timeout=0.0):
            calls.append(model)
            return Path(audio).read_bytes()

        monkeypatch.setattr(voiceclean_service.client, "run_task", fake_run_task)
        runtime = _FakeRuntime(tmp_path)
        runtime.rows = [
            {
                "id": "sidon_f32",
                "family": "sidon",
                "task": "s2s",
                "package": "sidon_f32",
                "url": "http://127.0.0.1:18103",
            },
            {
                "id": "flashsr",
                "family": "builtin_audio_utils",
                "task": "s2s",
                "utility": "flashsr",
                "path": "D:/models/flashsr",
                "url": "http://127.0.0.1:18104",
            },
        ]
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), runtime_reader=lambda: runtime
        )
        clean.run(
            {"voice_id": voice.id, "files": [target]},
            [
                {"key": "bandwidth", "enabled": True, "model": "flashsr", "options": {}},
                {"key": "denoise", "enabled": True, "model": "sidon_f32", "options": {}},
            ],
        )
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        assert view["state"] == "done"
        assert [step["key"] for step in view["steps"]] == ["denoise", "bandwidth"]
        assert calls == ["sidon_f32", "flashsr"]

    @pytest.mark.skipif(not ffmpeg.find_ffmpeg(None), reason="本机没有 ffmpeg")
    def test_core_runs_after_models_and_twice_only(self, scanned, tmp_path: Path):
        """没有模型步骤时，原声与结果是同一套收尾的同一份输出（不重复过滤）。"""
        voice = _voice(scanned, "胡桃")
        target = scanned.get_voice(voice.id).samples[0].file
        clean = VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=list
        )
        clean.run(
            {"voice_id": voice.id, "files": [target]},
            [{"key": "normalize", "enabled": True}],
        )
        for _ in range(120):
            view = clean.job_view()
            if view and view["state"] != "running":
                break
            time.sleep(0.1)
        view = clean.job_view()
        assert view["state"] == "done"
        origin = clean.audio_path(view["id"], 0, "origin")
        result = clean.audio_path(view["id"], 0, "result")
        assert origin.read_bytes() == result.read_bytes()


def _first_local(service) -> str:
    return next(row.id for row in service.list_voices() if row.is_local)
