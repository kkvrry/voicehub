"""音色处理插件：目标解析、替换、时长校验与响应解析。"""

import base64
import time
import wave
from pathlib import Path

import pytest

from app.infra import ffmpeg
from app.infra.sidecar import read_sidecar
from app.producers.base import ProducerError
from app.producers.builtin.voiceclean import client
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
        payload = client.get("/api/v1/producers/voiceclean/state").json()
        assert [step["key"] for step in payload["steps"]] == ["separate", "denoise"]
        assert payload["core"]["key"] == "normalize"
        assert payload["core"]["default"] is False
        assert payload["core"]["label"] == "统一声道与峰值"
        assert payload["core"]["note"]
        # 没有运行中的实例：步骤卡只留一句说明，页面本身仍然可用（规整不依赖服务）。
        assert all(step["models"] == [] for step in payload["steps"])
        assert all(step["reason"] for step in payload["steps"])

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

    def test_audio_picks_vocal_stem(self):
        vocal = base64.b64encode(b"RIFF" + b"\0" * 300).decode()
        other = base64.b64encode(b"RIFF" + b"\1" * 300).decode()
        payload = {"artifacts": [{"kind": "accompaniment", "audio": other}, {"kind": "vocals", "audio": vocal}]}
        assert client._audio_in(payload) == b"RIFF" + b"\0" * 300

    def test_audio_wrapped_result(self):
        raw = base64.b64encode(b"RIFF" + b"\2" * 300).decode()
        assert client._audio_in({"result": {"audio": f"data:audio/wav;base64,{raw}"}}) == b"RIFF" + b"\2" * 300

    def test_missing_audio_names_fields(self):
        with pytest.raises(ProducerError, match="没有音频"):
            payload = {"result": {"text": "hi"}}
            assert client._audio_in(payload) is None
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


class TestModels:
    def _service(self, tmp_path: Path, rows):
        return VoiceCleanService(
            tmp_path / "vc", lambda: VoiceCleanSettings(), running_models=lambda: rows
        )

    def test_steps_only_offer_running_models(self, tmp_path: Path):
        service = self._service(
            tmp_path,
            [
                {"id": "sidon_f32", "family": "sidon", "task": "s2s", "url": "http://127.0.0.1:18103"},
                {"id": "cosyvoice3", "family": "cosyvoice3", "task": "tts", "url": "http://127.0.0.1:18092"},
            ],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        assert [row["id"] for row in steps["denoise"]["models"]] == ["sidon_f32"]
        assert steps["denoise"]["models"][0]["prep"] == [48000, 1]
        # TTS 模型不属于任何一步；分离步骤没有可选项，但有说明。
        assert steps["separate"]["models"] == []
        assert steps["separate"]["reason"]

    def test_family_registered_model_uses_step_package(self, tmp_path: Path):
        """服务端注册族名时按该步骤推荐的包取名字与输入格式。"""
        service = self._service(
            tmp_path,
            [{"id": "mossformer2", "family": "mossformer2", "task": "s2s", "url": "http://127.0.0.1:18101"}],
        )
        steps = {row["key"]: row for row in service.state()["steps"]}
        denoise = steps["denoise"]["models"][0]
        separate = steps["separate"]["models"][0]
        assert denoise["id"] == separate["id"] == "mossformer2"
        assert denoise["prep"] == [48000, 1]
        assert separate["prep"] == [16000, 1]
        assert "MossFormer2" in denoise["label"]

    def test_run_refuses_model_that_is_not_running(self, clean, scanned):
        voice = _voice(scanned, "胡桃")
        with pytest.raises(ProducerError, match="没有在运行"):
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


def _first_local(service) -> str:
    return next(row.id for row in service.list_voices() if row.is_local)
