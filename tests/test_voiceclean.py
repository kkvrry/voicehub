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
    return VoiceCleanService(tmp_path / "voiceclean", lambda: VoiceCleanSettings(host="http://127.0.0.1:9"))


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
    def test_state_is_mounted(self, client):
        payload = client.get("/api/v1/producers/voiceclean/state").json()
        assert [step["key"] for step in payload["steps"]] == ["separate", "denoise"]
        assert payload["core"]["key"] == "normalize"
        assert payload["ready"] is False  # 测试环境没有 audio.cpp

    def test_run_refuses_without_steps(self, client):
        response = client.post("/api/v1/producers/voiceclean/run", json={"scope": {}})
        assert response.status_code == 400
        assert "至少勾选" in response.json()["error"]["message"]

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


class _StubModel:
    """最小 audio.cpp：列模型、报模型目录、跑任务时回被处理的那一段。"""

    def __init__(self, wav: Path, models_root: Path) -> None:
        import http.server
        import json
        import threading

        payload = base64.b64encode(wav.read_bytes()).decode()
        root_text = str(models_root)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: D401 - 静音
                return

            def do_GET(self):  # noqa: N802 - 与 http.server 契约一致
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

            def do_POST(self):  # noqa: N802
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


class TestPipeline:
    def test_run_processes_and_is_replaceable(self, scanned, tmp_path: Path):
        from conftest import make_wav

        from app.producers.builtin.audiocpp import catalog

        models_root = tmp_path / "models"
        models_root.mkdir()
        for family in catalog.families():
            for item in family.get("packages") or []:
                if item.get("id") in {"sidon_f32", "mossformer2_se_48k_f32"}:
                    for path in catalog.package_files(models_root, item):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"x")

        sample_wav = tmp_path / "reply.wav"
        make_wav(sample_wav, seconds=0.5)
        stub = _StubModel(sample_wav, models_root)
        try:
            clean = VoiceCleanService(
                tmp_path / "vc", lambda: VoiceCleanSettings(host=stub.url, timeout=60)
            )
            job = clean.run(
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


def _first_local(service) -> str:
    return next(row.id for row in service.list_voices() if row.is_local)
