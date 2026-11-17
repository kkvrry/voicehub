from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

from app.infra.audioprobe import find_ffprobe
from app.infra.ffmpeg import find_ffmpeg
from app.producers.builtin.audiobook.loudness import measure_audio
from app.producers.builtin.audiobook.merge_stage import MergeCancelled, _MergeControl
from app.producers.builtin.audiobook.paths import abs_path
from app.producers.builtin.audiobook.pipeline import AudiobookService
from app.producers.builtin.audiobook.settings import AudiobookSettings

pytestmark = pytest.mark.skipif(not find_ffmpeg(), reason="FFmpeg unavailable")


def tone(path: Path, amplitude: float = 0.08, seconds: float = 1.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"".join(struct.pack("<h", round(
            32767 * amplitude * math.sin(2 * math.pi * 440 * i / rate)
        )) for i in range(round(rate * seconds))))


def project(tmp_path: Path, settings: AudiobookSettings):
    svc = AudiobookService(tmp_path, lambda: settings)
    book = svc.create("响度验证", "第一章 测试\n正文。")
    project_id = book["id"]
    svc.store.replace_chapters(project_id, [("章节一", "正文"), ("章节二", "正文")])
    for chapter in svc.store.chapters(project_id):
        svc.store.replace_rows(chapter["id"], [
            {"speaker": "甲", "text": "常态一。"},
            {"speaker": "甲", "text": "常态二。"},
            {"speaker": "乙", "text": "另一音色。"},
            {"speaker": "甲", "text": "轻声。", "emotion": "耳语"},
        ])
        for row, amplitude in zip(svc.store.rows(chapter["id"]), [0.04, 0.05, 0.18, 0.01], strict=True):
            path = svc.project_folder(project_id) / "audio" / f"{row['id']}.wav"
            tone(path, amplitude)
            svc.store.set_row_audio(row["id"], path.relative_to(svc.project_folder(project_id)).as_posix())
        svc.store.set_chapter_stage(chapter["id"], "audio")
    return svc, project_id


def process(svc: AudiobookService, project_id: str):
    return svc.loudness.process(project_id, svc.settings, lambda *args: None)


def test_disabled_has_no_measurement_or_derived_files(tmp_path: Path, monkeypatch) -> None:
    settings = AudiobookSettings(merge_format="wav", chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    monkeypatch.setattr("app.producers.builtin.audiobook.loudness_stage.measure_audio",
                        lambda *args: pytest.fail("disabled loudness must not measure"))
    first = svc.store.chapters(project_id)[0]
    result = svc.merge(project_id, first["id"])
    assert result["loudness"]["rows"]["enabled"] is False
    assert result["loudness"]["master"] == {}
    assert not (svc.project_folder(project_id) / "loudness").exists()


def test_originals_cache_gain_change_and_switch_off(tmp_path: Path) -> None:
    settings = AudiobookSettings(row_balance_enabled=True)
    svc, project_id = project(tmp_path, settings)
    root = svc.project_folder(project_id)
    rows = svc.store.all_rows(project_id)
    originals = {row["id"]: hashlib.sha256(abs_path(root, row["audio"]).read_bytes()).hexdigest()
                 for row in rows}
    cold = process(svc, project_id)
    assert cold["measured"] == 8
    assert cold["rendered"] > 0
    first, note = svc.loudness.row_audio(root, rows[0])
    assert first != abs_path(root, rows[0]["audio"])
    assert "均衡" in note
    metrics = measure_audio(find_ffmpeg(), [first, abs_path(root, rows[0]["audio"])])
    plan = svc.loudness.plan(root)
    gain = plan["rows"][rows[0]["id"]]["gain_db"]
    assert metrics[first]["integrated"] - metrics[abs_path(root, rows[0]["audio"])]["integrated"] == pytest.approx(gain, abs=0.15)
    hot = process(svc, project_id)
    assert hot["measured"] == hot["rendered"] == 0
    settings.row_balance_strength = 25
    assert "待响度处理" in svc.loudness.row_audio(root, rows[0])[1]
    changed = process(svc, project_id)
    assert changed["measured"] == 0
    assert changed["rendered"] > 0
    settings.row_balance_enabled = False
    assert svc.loudness.row_audio(root, rows[0])[0] == abs_path(root, rows[0]["audio"])
    for row in rows:
        assert hashlib.sha256(abs_path(root, row["audio"]).read_bytes()).hexdigest() == originals[row["id"]]


def test_changed_source_invalidates_measurement_and_preview(tmp_path: Path) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))
    process(svc, project_id)
    root = svc.project_folder(project_id)
    row = svc.store.all_rows(project_id)[0]
    path = abs_path(root, row["audio"])
    tone(path, 0.09, 1.4)
    assert svc.loudness.row_audio(root, row)[0] == path
    result = process(svc, project_id)
    assert result["measured"] == 1


def test_cancelled_processing_does_not_activate_partial_plan(tmp_path: Path) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))
    result = svc.loudness.process(project_id, svc.settings, lambda *args: None, lambda: True)
    assert result["cancelled"]
    assert svc.loudness.plan(svc.project_folder(project_id)) == {}


def test_processing_invalidates_only_changed_chapters(tmp_path: Path) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))
    process(svc, project_id)
    chapters = svc.store.chapters(project_id)
    for chapter in chapters:
        svc.store.set_chapter_stage(chapter["id"], "merged", "old.wav")
    process(svc, project_id)
    assert all(chapter["audio"] == "old.wav" for chapter in svc.store.chapters(project_id))
    svc.settings.row_balance_strength = 20
    process(svc, project_id)
    assert all(chapter["audio"] == "" for chapter in svc.store.chapters(project_id))


def test_reenabling_after_raw_merge_invalidates_only_that_chapter(tmp_path: Path) -> None:
    settings = AudiobookSettings(row_balance_enabled=True, merge_format="wav",
                                chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    process(svc, project_id)
    chapters = svc.store.chapters(project_id)
    for chapter in chapters:
        svc.store.set_chapter_stage(chapter["id"], "merged", "old.wav")
    settings.row_balance_enabled = False
    svc.merge(project_id, chapters[0]["id"])
    settings.row_balance_enabled = True
    result = process(svc, project_id)
    assert result["measured"] == result["rendered"] == 0
    refreshed = svc.store.chapters(project_id)
    assert refreshed[0]["audio"] == ""
    assert refreshed[1]["audio"] == "old.wav"


def test_database_failure_does_not_publish_plan(tmp_path: Path, monkeypatch) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))
    root = svc.project_folder(project_id)
    for chapter in svc.store.chapters(project_id):
        svc.store.set_chapter_stage(chapter["id"], "merged", "old.wav")
    with monkeypatch.context() as patch:
        def locked(*args, **kwargs):
            raise RuntimeError("database locked")
        patch.setattr(svc.store, "set_chapters_stage", locked)
        with pytest.raises(RuntimeError, match="database locked"):
            process(svc, project_id)
    assert svc.loudness.plan(root) == {}
    assert all(chapter["audio"] == "old.wav" for chapter in svc.store.chapters(project_id))
    result = process(svc, project_id)
    assert result["measured"] == result["rendered"] == 0
    assert all(chapter["audio"] == "" for chapter in svc.store.chapters(project_id))
    assert svc.loudness.plan(root)["rows"]


def test_speed_cache_tracks_effective_audio_and_source_rewrites(tmp_path: Path) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))
    root = svc.project_folder(project_id)
    row = svc.store.all_rows(project_id)[0]
    raw = abs_path(root, row["audio"])
    folder = root / "fits"
    folder.mkdir()
    binary, probe = find_ffmpeg(), find_ffprobe()
    before = svc.output._speed_file(binary, folder, row, raw, 1.2, probe)
    process(svc, project_id)
    balanced, _ = svc.loudness.row_audio(root, row)
    after = svc.output._speed_file(binary, folder, row, balanced, 1.2, probe)
    assert before != after
    values = measure_audio(binary, [before, after])
    gain = svc.loudness.plan(root)["rows"][row["id"]]["gain_db"]
    assert values[after]["integrated"] - values[before]["integrated"] == pytest.approx(gain, abs=0.2)
    svc.settings.row_balance_enabled = False
    assert svc.output._speed_file(binary, folder, row, raw, 1.2, probe) == before
    tone(raw, 0.15, 1.3)
    replaced = svc.output._speed_file(binary, folder, row, raw, 1.2, probe)
    assert replaced not in {before, after}
    svc.settings.master_loudness_enabled = True
    floating = svc.output._speed_file(binary, folder, row, raw, 1.2, probe)
    assert floating != replaced


@pytest.mark.parametrize("fmt", ["wav", "m4a", "mp3"])
def test_master_presets_write_measurable_final_audio(tmp_path: Path, fmt: str) -> None:
    settings = AudiobookSettings(row_balance_enabled=True, master_loudness_enabled=True,
                                merge_format=fmt, row_gap_ms=50,
                                chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    chapter = svc.store.chapters(project_id)[0]
    result = svc.merge(project_id, chapter["id"])
    report = result["loudness"]["master"]["voice"]
    assert report["target_met"]
    assert report["peak_ok"]
    assert report["integrated"] == pytest.approx(-19, abs=0.5)
    assert result["timings"]["master_loudness"] > 0
    assert svc.output.progress(project_id)["stage"] == "done"
    assert result["check"]["ok"]


def test_bgm_master_is_measured_after_mix(tmp_path: Path) -> None:
    bgm = tmp_path / "music"
    tone(bgm / "song.wav", 0.16, 2)
    settings = AudiobookSettings(master_loudness_enabled=True, bgm_enabled=True,
                                bgm_folder=str(bgm), bgm_normalize=False,
                                bgm_gain_db=-10, merge_format="m4a",
                                chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    chapter = svc.store.chapters(project_id)[0]
    result = svc.merge(project_id, chapter["id"])
    assert set(result["loudness"]["master"]) == {"voice", "bgm"}
    assert result["loudness"]["master"]["bgm"]["peak_ok"]
    assert svc.chapter_audio(project_id, chapter["id"]) == Path(result["bgm"])
    assert result["loudness"]["master"]["bgm"]["integrated"] == pytest.approx(-19, abs=0.5)


def test_acx_reference_uses_rms_and_does_not_claim_full_compliance(tmp_path: Path) -> None:
    settings = AudiobookSettings(master_loudness_enabled=True, master_loudness_preset="acx",
                                merge_format="wav", chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    chapter = svc.store.chapters(project_id)[0]
    result = svc.merge(project_id, chapter["id"])
    report = result["loudness"]["master"]["voice"]
    assert report["plan"]["unit"] in ("RMS", "dBFS RMS", "dBFS")
    assert -23 <= report["rms"] <= -18
    assert any("底噪" in warning for warning in report["warnings"])


def test_book_level_baseline_warning_is_not_repeated_on_cached_merge(tmp_path: Path) -> None:
    settings = AudiobookSettings(row_balance_enabled=True, merge_format="wav",
                                chapter_lead_ms=0, chapter_tail_ms=0)
    svc = AudiobookService(tmp_path, lambda: settings)
    project_id = svc.create("告警验证", "第一章 测试\n正文。")["id"]
    svc.store.replace_chapters(project_id, [("章节一", "正文")])
    chapter = svc.store.chapters(project_id)[0]
    svc.store.replace_rows(chapter["id"], [
        {"speaker": "甲", "text": "常态一。"},
        {"speaker": "甲", "text": "常态二。"},
        {"speaker": "丙", "text": "另一个音色。"},
        {"speaker": "乙", "text": "只有情绪。", "emotion": "耳语"},
    ])
    root = svc.project_folder(project_id)
    for row, amplitude in zip(svc.store.rows(chapter["id"]), [0.04, 0.05, 0.18, 0.01], strict=True):
        path = root / "audio" / f"{row['id']}.wav"
        tone(path, amplitude)
        svc.store.set_row_audio(row["id"], path.relative_to(root).as_posix())
    svc.store.set_chapter_stage(chapter["id"], "audio")
    process(svc, project_id)

    cached = svc.merge(project_id, chapter["id"])
    assert not any("缺少常态语音基线" in warning for warning in cached["loudness"]["warnings"])

    settings.row_balance_strength = 25
    refreshed = svc.merge(project_id, chapter["id"])
    warnings = [warning for warning in refreshed["loudness"]["warnings"]
                if "缺少常态语音基线" in warning]
    assert warnings == ["全书 1 行所属音色缺少常态语音基线，保持原样。"]


def test_cancel_terminates_running_ffmpeg(tmp_path: Path) -> None:
    svc = AudiobookService(tmp_path, lambda: AudiobookSettings())
    control = _MergeControl("test")
    errors = []

    def run() -> None:
        try:
            svc.output._run_ffmpeg(
                [sys.executable, "-c", "import time; time.sleep(30)"], None, control
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里要把异常带回主线程
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    control.stop()
    thread.join(5)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], MergeCancelled)


def test_cancel_mid_merge_keeps_previous_product(tmp_path: Path, monkeypatch) -> None:
    settings = AudiobookSettings(merge_format="wav", chapter_lead_ms=0, chapter_tail_ms=0)
    svc, project_id = project(tmp_path, settings)
    chapter = svc.store.chapters(project_id)[0]
    first = svc.merge(project_id, chapter["id"])
    output = Path(first["audio"])
    before = output.read_bytes()
    entered = threading.Event()
    errors = []

    def blocking_concat(binary, fmt, bitrate, inputs, target, folder, **kwargs):
        del binary, fmt, bitrate, inputs, folder
        target.write_bytes(b"partial")
        entered.set()
        control = kwargs.get("control")
        while control is not None and not control.event.is_set():
            time.sleep(0.05)
        if control is not None:
            control.check()

    monkeypatch.setattr(svc.output, "_concat", blocking_concat)

    def run() -> None:
        try:
            svc.merge(project_id, chapter["id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(10)
    assert svc.output.cancel(project_id)
    thread.join(10)
    assert not thread.is_alive()
    assert errors and "合并已停止" in str(errors[0])
    assert output.read_bytes() == before
    refreshed = svc.store.chapter(project_id, chapter["id"])
    assert refreshed["stage"] == "merged"
    assert abs_path(svc.project_folder(project_id), refreshed["audio"]) == output
    assert svc.output.progress(project_id)["stage"] == "cancelled"
    assert svc.output.cancel(project_id) is False


def test_async_job_reports_progress_and_completes(tmp_path: Path) -> None:
    svc, project_id = project(tmp_path, AudiobookSettings(row_balance_enabled=True))

    async def run():
        job = svc.loudness.start(project_id)
        assert job["kind"] == "loudness"
        await svc._tasks[job["id"]]
        return svc.store.job(job["id"])

    job = asyncio.run(run())
    assert job["state"] == "done"
    assert job["done"] == job["total"] == 8
    assert "测量" in job["message"]
