from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from app.infra.ffmpeg import find_ffmpeg
from app.producers.base import ProducerError
from app.producers.builtin.audiobook import loudness


@pytest.fixture(scope="module")
def binary() -> str:
    found = find_ffmpeg()
    if not found:
        pytest.skip("本机未安装 FFmpeg")
    return found


def _wav(path: Path, samples: np.ndarray, rate: int = 44100) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.round(samples * 32767).astype("<i2").tobytes())
    return path


def _tone(
    path: Path,
    amplitude: float = 0.1,
    seconds: float = 1.2,
    rate: int = 44100,
    frequency: float = 1000,
    phase: float = 0,
) -> Path:
    time = np.arange(round(seconds * rate)) / rate
    return _wav(path, amplitude * np.sin(2 * np.pi * frequency * time + phase), rate)


def _decode(binary: str, path: Path) -> np.ndarray:
    done = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_f32le",
            "-threads",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(done.stdout, dtype="<f4")


def _item(
    identifier: str,
    group: str,
    integrated: float | None,
    peak: float | None = -10,
    expressive: bool = False,
) -> dict:
    return {
        "id": identifier,
        "group": group,
        "integrated": integrated,
        "true_peak": peak,
        "expressive": expressive,
    }


def test_measure_batch_matches_single_with_mixed_rates_lengths_and_silence(
    binary: str,
    tmp_path: Path,
) -> None:
    paths = [
        _tone(tmp_path / "静音.wav", amplitude=0, seconds=0.5),
        _tone(tmp_path / "quiet.wav", amplitude=0.03, seconds=1, rate=24000),
        _tone(tmp_path / "loud.wav", amplitude=0.4, seconds=2, rate=48000),
        _tone(tmp_path / "middle.wav", amplitude=0.1, seconds=1.5),
    ]
    batch = loudness.measure_audio(binary, paths)
    for path in paths:
        assert batch[path] == loudness.measure_audio(binary, [path])[path]
    assert batch[paths[0]] == {"integrated": None, "rms": None, "true_peak": None}
    for path, amplitude in zip(paths[1:], [0.03, 0.4, 0.1], strict=True):
        assert batch[path]["rms"] == pytest.approx(
            20 * math.log10(amplitude / math.sqrt(2)), abs=0.02
        )
        assert batch[path]["true_peak"] == pytest.approx(20 * math.log10(amplitude), abs=0.2)
    assert batch[paths[2]]["integrated"] - batch[paths[1]]["integrated"] == pytest.approx(
        20 * math.log10(0.4 / 0.03),
        abs=0.2,
    )
    json.dumps(list(batch.values()), allow_nan=False)


def test_measure_batch_is_bounded_and_uses_named_executed_filters(
    binary: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [_tone(tmp_path / f"row-{i}.wav", amplitude=0.03 + i * 0.02) for i in range(25)]
    original = loudness.subprocess.run
    calls = []
    logs = []

    def recording(command, **kwargs):
        calls.append(command)
        result = original(command, **kwargs)
        logs.append(result.stderr)
        return result

    monkeypatch.setattr(loudness.subprocess, "run", recording)
    measurements = loudness.measure_audio(binary, paths)
    assert [command.count("-i") for command in calls] == [12, 12, 1]
    assert len(measurements) == 25
    for i, path in enumerate(paths):
        assert measurements[path]["rms"] == pytest.approx(
            20 * math.log10((0.03 + i * 0.02) / math.sqrt(2)),
            abs=0.02,
        )
    for command, log in zip(calls, logs, strict=True):
        graph = command[command.index("-filter_complex") + 1]
        assert "loudnorm" not in graph
        assert command[command.index("-filter_complex_threads") + 1] == "1"
        assert all(command[i + 1] == "1" for i, arg in enumerate(command) if arg == "-threads")
        for i in range(command.count("-i")):
            assert f"ebur128@r{i}=" in graph
            assert f"astats@r{i}=" in graph
            assert f"[sink{i}]anullsink" in graph
            assert f"[out{i}]" in command
            assert f"[r{i} @" in log or f"[ebur128@r{i} @" in log
            assert f"[r{i} @" in log or f"[astats@r{i} @" in log


@pytest.mark.parametrize(
    "seconds,amplitude", [(1.2, 0), (0.3, 0), (0.08, 0.5), (0.399, 0.5), (1.2, 0.0001)]
)
def test_unmeasurable_lufs_preserves_peak_and_rms_and_is_json_safe(
    binary: str,
    tmp_path: Path,
    seconds: float,
    amplitude: float,
) -> None:
    source = _tone(tmp_path / "invalid-level.wav", amplitude=amplitude, seconds=seconds)
    measured = loudness.measure_audio(binary, [source])[source]
    assert measured["integrated"] is None
    if amplitude == 0:
        assert measured == {"integrated": None, "true_peak": None, "rms": None}
    else:
        samples = _decode(binary, source).astype(np.float64)
        assert measured["rms"] == pytest.approx(
            10 * math.log10(float(np.mean(samples**2))), abs=0.001
        )
        assert math.isfinite(measured["true_peak"])
        assert measured["true_peak"] >= 20 * math.log10(float(np.max(np.abs(samples)))) - 0.1
    plan = loudness.master_gain(measured, preset="podcast", target_lufs=-19, peak_db=-1)
    assert plan["gain_db"] == 0
    assert plan["measurable"] is False
    evaluation = loudness.evaluate_master(measured, plan)
    assert evaluation["target_met"] is False
    assert evaluation["peak_ok"] is (amplitude != 0)
    assert any("测量" in warning for warning in evaluation["warnings"])
    json.dumps([measured, plan, evaluation], allow_nan=False)


def test_exactly_400ms_is_measurable(binary: str, tmp_path: Path) -> None:
    path = _tone(tmp_path / "400ms.wav", seconds=0.4)
    assert loudness.measure_audio(binary, [path])[path]["integrated"] is not None


def test_stereo_rms_is_overall_not_last_channel(binary: str, tmp_path: Path) -> None:
    time = np.arange(44100) / 44100
    sine = np.sin(2 * np.pi * 1000 * time)
    path = _wav(tmp_path / "stereo.wav", np.column_stack([0.1 * sine, 0.4 * sine]))
    measured = loudness.measure_audio(binary, [path])[path]
    assert measured["rms"] == pytest.approx(10 * math.log10((0.1**2 + 0.4**2) / 4), abs=0.02)
    assert measured["true_peak"] == pytest.approx(20 * math.log10(0.4), abs=0.2)


def test_true_peak_includes_intersample_peaks(binary: str, tmp_path: Path) -> None:
    source = _tone(tmp_path / "intersample.wav", amplitude=0.9, frequency=11025, phase=np.pi / 4)
    sample_peak = 20 * math.log10(float(np.max(np.abs(_decode(binary, source)))))
    measured = loudness.measure_audio(binary, [source])[source]
    assert measured["true_peak"] > sample_peak + 1


def test_empty_measurement_does_not_run_ffmpeg() -> None:
    assert loudness.measure_audio("nonexistent-ffmpeg", []) == {}


@pytest.mark.parametrize("kind", ["missing", "invalid", "truncated"])
def test_decode_errors_raise_instead_of_returning_fake_measurements(
    binary: str,
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / "broken.wav"
    if kind == "invalid":
        source.write_bytes(b"not a media file")
    elif kind == "truncated":
        _tone(source, seconds=2)
        source.write_bytes(source.read_bytes()[:10000])
    with pytest.raises(ProducerError, match="FFmpeg"):
        loudness.measure_audio(binary, [source])


def test_one_corrupt_input_fails_the_batch(binary: str, tmp_path: Path) -> None:
    source = _tone(tmp_path / "good.wav")
    with pytest.raises(ProducerError):
        loudness.measure_audio(binary, [source, tmp_path / "missing.wav"])


def test_missing_binary_is_producer_error(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ProducerError):
        loudness.measure_audio(str(tmp_path / "missing-ffmpeg.exe"), [source])
    with pytest.raises(ProducerError):
        loudness.apply_gain(
            str(tmp_path / "missing-ffmpeg.exe"), source, tmp_path / "target.wav", 1
        )


def test_timeout_is_producer_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 3600)

    monkeypatch.setattr(loudness.subprocess, "run", timeout)
    with pytest.raises(ProducerError):
        loudness.measure_audio("ffmpeg", [tmp_path / "source.wav"])


def test_summary_without_executed_astats_is_not_silence(tmp_path: Path) -> None:
    log = """  Stream #0:0: Audio: pcm_s16le, 44100 Hz, mono, s16
[ebur128@r0 @ 00000001] Summary:
  Integrated loudness:
    I: -70.0 LUFS
  True peak:
    Peak: -inf dBFS
"""
    with pytest.raises(ProducerError, match="完整"):
        loudness._parse_measurements(log, [tmp_path / "source.wav"])


def test_voice_gain_preserves_group_dynamic_difference() -> None:
    items = [
        _item("quiet", "a", -30),
        _item("normal", "a", -24),
        _item("loud", "a", -12, expressive=True),
        _item("other", "b", -18),
    ]
    gains, reference = loudness.balance_gains(items, mode="voice", strength=0.5, max_gain_db=6)
    assert reference == -24
    assert gains == {"quiet": 1.5, "normal": 1.5, "loud": 1.5, "other": -3}
    assert (-12 + gains["loud"]) - (-30 + gains["quiet"]) == 18


@pytest.mark.parametrize("mode", ["voice", "row"])
def test_strength_zero_never_attenuates_existing_high_peaks(mode: str) -> None:
    items = [
        _item("a", "a", -30, peak=2),
        _item("b", "b", -10, peak=0),
        _item("short", "a", None, peak=6),
    ]
    gains, reference = loudness.balance_gains(items, mode=mode, strength=0, max_gain_db=6)
    assert reference == -20
    assert gains == {"a": 0, "b": 0, "short": 0}


@pytest.mark.parametrize("mode", ["voice", "row"])
@pytest.mark.parametrize("peak", [-20, 2])
def test_unmeasurable_rows_with_peaks_do_not_receive_gain(mode: str, peak: float) -> None:
    items = [
        _item("quiet", "a", -30, peak=-15),
        _item("short", "a", None, peak=peak),
        _item("other", "b", -20),
    ]
    gains, reference = loudness.balance_gains(items, mode=mode, strength=1, max_gain_db=6)
    assert reference == -25
    assert gains["short"] == 0
    assert gains["quiet"] == (0 if mode == "voice" and peak > -1 else 5)
    assert gains["other"] == -5


@pytest.mark.parametrize("mode", ["voice", "row"])
def test_gain_is_bounded(mode: str) -> None:
    items = [_item("a", "a", -50, peak=-20), _item("b", "b", -10, peak=-2)]
    gains, _ = loudness.balance_gains(items, mode=mode, strength=1, max_gain_db=3)
    assert gains == {"a": 3, "b": -3}


def test_voice_positive_gain_respects_expressive_group_peak() -> None:
    items = [
        _item("normal", "a", -30, peak=-15),
        _item("shout", "a", -15, peak=-1.5, expressive=True),
        _item("other", "b", -20),
    ]
    gains, _ = loudness.balance_gains(items, mode="voice", strength=1, max_gain_db=6)
    assert gains == {"normal": 0.5, "shout": 0.5, "other": -5}


@pytest.mark.parametrize("mode", ["voice", "row"])
def test_positive_gain_never_turns_into_peak_attenuation(mode: str) -> None:
    gains, _ = loudness.balance_gains(
        [_item("a", "a", -30, peak=2), _item("b", "b", -20)],
        mode=mode,
        strength=1,
        max_gain_db=6,
    )
    assert gains == {"a": 0, "b": -5}


def test_row_mode_protects_expressive_rows_and_ignores_groups_without_baseline() -> None:
    items = [
        _item("quiet", "a", -32, peak=-20),
        _item("normal", "a", -24),
        _item("whisper", "a", -45, peak=-20, expressive=True),
        _item("other", "b", -20),
        _item("no-baseline", "c", -10, expressive=True),
        _item("silent", "a", None, peak=None),
    ]
    gains, reference = loudness.balance_gains(items, mode="row", strength=1, max_gain_db=6)
    assert reference == -24
    assert gains == {
        "quiet": 6,
        "normal": 0,
        "whisper": 4,
        "other": -4,
        "no-baseline": 0,
        "silent": 0,
    }


def test_no_normal_baseline_returns_zero_gains() -> None:
    items = [_item("a", "a", -20, expressive=True), _item("b", "b", None)]
    assert loudness.balance_gains(items, mode="row", strength=1, max_gain_db=6) == (
        {"a": 0, "b": 0},
        None,
    )


def test_nonfinite_levels_are_ignored() -> None:
    items = [_item("a", "a", float("nan")), _item("b", "b", float("inf"))]
    gains, reference = loudness.balance_gains(items, mode="voice", strength=1, max_gain_db=6)
    assert gains == {"a": 0, "b": 0}
    assert reference is None
    measured = {"integrated": float("nan"), "rms": float("-inf"), "true_peak": float("inf")}
    plan = loudness.master_gain(measured, preset="acx", target_lufs=-19, peak_db=-1)
    assert plan["measurable"] is False
    json.dumps([plan, loudness.evaluate_master(measured, plan)], allow_nan=False)


@pytest.mark.parametrize(
    "preset,target,expected_target,unit,gain",
    [
        ("podcast", -10, -19, "LUFS", 6),
        ("custom", -21, -21, "LUFS", 4),
        ("acx", -10, -20.5, "RMS", 3.5),
    ],
)
def test_master_uses_requested_metric_and_preset(
    preset: str,
    target: float,
    expected_target: float,
    unit: str,
    gain: float,
) -> None:
    plan = loudness.master_gain(
        {"integrated": -25, "rms": -24, "true_peak": -12},
        preset=preset,
        target_lufs=target,
        peak_db=-1,
    )
    assert plan == {
        "gain_db": gain,
        "requested_gain_db": gain,
        "target": expected_target,
        "unit": unit,
        "peak_ceiling": -3 if preset == "acx" else -1,
        "limited": False,
        "measurable": True,
    }


@pytest.mark.parametrize(
    "preset,peak_db,ceiling", [("acx", -1, -3), ("acx", -4, -4), ("custom", -1, -1)]
)
def test_master_peak_ceiling_and_headroom(preset: str, peak_db: float, ceiling: float) -> None:
    plan = loudness.master_gain(
        {"integrated": -30, "rms": -30, "true_peak": -2},
        preset=preset,
        target_lufs=-19,
        peak_db=peak_db,
        headroom_db=0.5,
    )
    assert plan["gain_db"] == ceiling - 0.5 + 2
    assert plan["limited"] is True
    assert plan["requested_gain_db"] > plan["gain_db"]
    assert plan["peak_ceiling"] == ceiling


def test_master_can_attenuate_already_excessive_peak() -> None:
    plan = loudness.master_gain(
        {"integrated": -20, "true_peak": 1},
        preset="podcast",
        target_lufs=-19,
        peak_db=-1,
    )
    assert plan["requested_gain_db"] == 1
    assert plan["gain_db"] == -2
    assert plan["limited"] is True


def test_master_loud_input_is_attenuated_to_target() -> None:
    plan = loudness.master_gain(
        {"integrated": -12, "true_peak": -1},
        preset="podcast",
        target_lufs=-19,
        peak_db=-1,
    )
    assert plan["gain_db"] == -7
    assert plan["limited"] is False


@pytest.mark.parametrize("measured", [{}, {"integrated": -25}, {"true_peak": -10}])
def test_incomplete_master_measurement_cannot_request_gain(measured: dict) -> None:
    plan = loudness.master_gain(measured, preset="podcast", target_lufs=-19, peak_db=-1)
    assert plan["measurable"] is False
    assert plan["gain_db"] == 0


@pytest.mark.parametrize("preset", ["podcast", "custom", "acx"])
@pytest.mark.parametrize("level", [None, float("nan"), float("-inf")])
@pytest.mark.parametrize("peak", [-20, -1, 3])
def test_master_without_target_metric_only_attenuates_for_peak_safety(
    preset: str, level: float | None, peak: float
) -> None:
    measured = {"integrated": level, "rms": level, "true_peak": peak}
    plan = loudness.master_gain(
        measured, preset=preset, target_lufs=-18, peak_db=-1, headroom_db=0.2
    )
    ceiling = -3 if preset == "acx" else -1
    expected = min(0, ceiling - 0.2 - peak)
    assert plan["gain_db"] == pytest.approx(expected)
    assert plan["requested_gain_db"] == 0
    assert plan["measurable"] is False
    assert plan["limited"] is (expected < 0)
    evaluation = loudness.evaluate_master({**measured, "true_peak": peak + expected}, plan)
    assert evaluation["target_met"] is False
    assert evaluation["peak_ok"] is True
    assert any("测量" in warning for warning in evaluation["warnings"])
    json.dumps([plan, evaluation], allow_nan=False)


@pytest.mark.parametrize("gain", [0, 4.5, -6, 12])
def test_apply_gain_is_samplewise_fixed_gain_without_dynamic_processing(
    binary: str,
    tmp_path: Path,
    gain: float,
) -> None:
    time = np.arange(88200) / 44100
    envelope = np.where(time < 1, 0.05, 0.3)
    source = _wav(tmp_path / "performance.wav", envelope * np.sin(2 * np.pi * 1000 * time))
    target = tmp_path / "float.wav"
    loudness.apply_gain(binary, source, target, gain)
    before = _decode(binary, source)
    after = _decode(binary, target)
    assert after.shape == before.shape
    np.testing.assert_allclose(after, before * 10 ** (gain / 20), atol=1e-7, rtol=1e-6)
    header = target.read_bytes()
    offset = header.index(b"fmt ")
    assert int.from_bytes(header[offset + 12 : offset + 16], "little") == 44100
    assert int.from_bytes(header[offset + 22 : offset + 24], "little") == 32


@pytest.mark.parametrize("channels", [None, 1])
def test_apply_gain_resamples_and_only_downmixes_when_requested(
    binary: str,
    tmp_path: Path,
    channels: int | None,
) -> None:
    time = np.arange(24000) / 24000
    sine = 0.1 * np.sin(2 * np.pi * 1000 * time)
    source = _wav(tmp_path / "stereo.wav", np.column_stack([sine, sine * 2]), rate=24000)
    target = tmp_path / "resampled.wav"
    loudness.apply_gain(binary, source, target, 0, channels=channels)
    header = target.read_bytes()
    offset = header.index(b"fmt ")
    assert int.from_bytes(header[offset + 10 : offset + 12], "little") == (channels or 2)
    assert int.from_bytes(header[offset + 12 : offset + 16], "little") == 44100
    assert _decode(binary, target).size == 44100 * (channels or 2)


def test_real_acx_master_uses_rms(binary: str, tmp_path: Path) -> None:
    source = _tone(tmp_path / "acx-source.wav", amplitude=0.03, seconds=2)
    measured = loudness.measure_audio(binary, [source])[source]
    plan = loudness.master_gain(measured, preset="acx", target_lufs=-19, peak_db=-1)
    target = tmp_path / "acx.wav"
    loudness.apply_gain(binary, source, target, plan["gain_db"])
    after = loudness.measure_audio(binary, [target])[target]
    evaluation = loudness.evaluate_master(after, plan)
    assert after["rms"] == pytest.approx(-20.5, abs=0.001)
    assert evaluation["target_met"] is True
    assert evaluation["peak_ok"] is True
    assert len(evaluation["warnings"]) == 1


def test_voice_balance_preserves_real_audio_group_difference(binary: str, tmp_path: Path) -> None:
    paths = [
        _tone(tmp_path / f"{i}.wav", amplitude=amplitude)
        for i, amplitude in enumerate([0.03, 0.08, 0.2, 0.3])
    ]
    before = loudness.measure_audio(binary, paths)
    items = [
        {"id": str(i), "group": "a" if i < 3 else "b", **before[path], "expressive": i == 2}
        for i, path in enumerate(paths)
    ]
    gains, _ = loudness.balance_gains(items, mode="voice", strength=0.5, max_gain_db=3)
    outputs = [tmp_path / f"out-{i}.wav" for i in range(3)]
    for i, output in enumerate(outputs):
        loudness.apply_gain(binary, paths[i], output, gains[str(i)])
    after = loudness.measure_audio(binary, outputs)
    assert after[outputs[2]]["rms"] - after[outputs[0]]["rms"] == pytest.approx(
        before[paths[2]]["rms"] - before[paths[0]]["rms"],
        abs=0.0001,
    )


@pytest.mark.parametrize("crest_limited", [False, True])
def test_real_fixed_gain_master_hits_target_or_reports_peak_limit(
    binary: str,
    tmp_path: Path,
    crest_limited: bool,
) -> None:
    time = np.arange(88200) / 44100
    samples = 0.05 * np.sin(2 * np.pi * 1000 * time)
    if crest_limited:
        samples[22050] = 0.95
    source = _wav(tmp_path / "master-source.wav", samples)
    before = loudness.measure_audio(binary, [source])[source]
    plan = loudness.master_gain(
        before,
        preset="podcast",
        target_lufs=-19,
        peak_db=-1,
        headroom_db=0.1,
    )
    target = tmp_path / "master.wav"
    loudness.apply_gain(binary, source, target, plan["gain_db"])
    after = loudness.measure_audio(binary, [target])[target]
    evaluation = loudness.evaluate_master(after, plan)
    assert plan["limited"] is crest_limited
    assert evaluation["target_met"] is not crest_limited
    assert evaluation["peak_ok"] is True
    assert after["rms"] - before["rms"] == pytest.approx(plan["gain_db"], abs=0.001)
    assert after["true_peak"] <= -1
    if crest_limited:
        assert any("上限约束" in warning for warning in evaluation["warnings"])
    else:
        assert not evaluation["warnings"]


@pytest.mark.parametrize("preset", ["podcast", "custom"])
def test_300ms_overfull_float_master_is_peak_safe_in_pcm_s16le(
    binary: str, tmp_path: Path, preset: str
) -> None:
    tone = _tone(tmp_path / "300ms.wav", amplitude=0.5, seconds=0.3)
    source = tmp_path / "overfull-float.wav"
    loudness.apply_gain(binary, tone, source, 12, codec="pcm_f32le")
    before_samples = _decode(binary, source)
    assert before_samples.size == round(0.3 * 44100)
    assert np.max(np.abs(before_samples)) > 1
    before = loudness.measure_audio(binary, [source])[source]
    assert before["integrated"] is None
    assert before["true_peak"] > 0
    assert before["rms"] is not None
    plan = loudness.master_gain(before, preset=preset, target_lufs=-18, peak_db=-1, headroom_db=0.2)
    assert plan["measurable"] is False
    assert plan["requested_gain_db"] == 0
    assert plan["gain_db"] == pytest.approx(-1.2 - before["true_peak"])
    assert plan["limited"] is True

    target = tmp_path / "master-s16.wav"
    loudness.apply_gain(binary, source, target, plan["gain_db"], codec="pcm_s16le")
    with wave.open(str(target), "rb") as output:
        assert output.getsampwidth() == 2
        assert output.getnframes() == before_samples.size
        pcm = np.frombuffer(output.readframes(output.getnframes()), dtype="<i2")
    assert np.all((pcm > -32768) & (pcm < 32767))
    after_samples = _decode(binary, target)
    np.testing.assert_allclose(
        after_samples, before_samples * 10 ** (plan["gain_db"] / 20), atol=1 / 32768, rtol=0
    )
    assert 20 * math.log10(float(np.max(np.abs(after_samples)))) <= -1.1
    after = loudness.measure_audio(binary, [target])[target]
    assert after["integrated"] is None
    assert after["true_peak"] <= -1.2
    assert after["rms"] - before["rms"] == pytest.approx(plan["gain_db"], abs=0.001)
    evaluation = loudness.evaluate_master(after, plan)
    assert evaluation["target_met"] is False
    assert evaluation["peak_ok"] is True
    assert any("测量" in warning for warning in evaluation["warnings"])
    assert any("上限约束" in warning for warning in evaluation["warnings"])


@pytest.mark.parametrize("extension,codec", [("mp3", "libmp3lame"), ("m4a", "aac"), ("aac", "aac")])
def test_apply_gain_encodes_requested_container(
    binary: str,
    tmp_path: Path,
    extension: str,
    codec: str,
) -> None:
    source = _tone(tmp_path / "source.wav", rate=24000)
    target = tmp_path / f"encoded.{extension}"
    loudness.apply_gain(binary, source, target, 1, codec=codec, bitrate="128k", channels=1)
    assert loudness.measure_audio(binary, [target])[target]["integrated"] is not None
    if extension == "m4a":
        content = target.read_bytes()
        assert content.index(b"moov") < content.index(b"mdat")


def test_metadata_is_preserved_and_overridden(binary: str, tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    tagged = tmp_path / "tagged.wav"
    target = tmp_path / "result.m4a"
    loudness.apply_gain(binary, source, tagged, 0, metadata={"title": "Old", "artist": "Narrator"})
    loudness.apply_gain(binary, tagged, target, 0, codec="aac", metadata={"title": "New"})
    result = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-i", str(target), "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert "New" in result.stderr
    assert "Narrator" in result.stderr
    assert "Old" not in result.stderr


def test_apply_gain_refuses_same_source_and_decode_errors(binary: str, tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    original = source.read_bytes()
    with pytest.raises(ProducerError, match="覆盖"):
        loudness.apply_gain(binary, source, source, 1)
    assert source.read_bytes() == original
    with pytest.raises(ProducerError):
        loudness.apply_gain(binary, tmp_path / "missing.wav", tmp_path / "output.wav", 1)
    with pytest.raises(ProducerError):
        loudness.apply_gain(binary, source, tmp_path / "bad.wav", float("nan"))


@pytest.mark.parametrize(
    "value,met", [(-19.5, True), (-18.5, True), (-19.6, False), (-18.4, False)]
)
def test_lufs_evaluation_tolerance(value: float, met: bool) -> None:
    plan = loudness.master_gain(
        {"integrated": -19, "true_peak": -5},
        preset="podcast",
        target_lufs=-19,
        peak_db=-1,
    )
    evaluation = loudness.evaluate_master({"integrated": value, "true_peak": -1}, plan)
    assert evaluation["target_met"] is met
    assert evaluation["peak_ok"] is True


@pytest.mark.parametrize("rms,met", [(-23, True), (-18, True), (-23.1, False), (-17.9, False)])
def test_acx_range_and_advisory_are_not_technical_failure(rms: float, met: bool) -> None:
    plan = loudness.master_gain(
        {"rms": -20.5, "true_peak": -4},
        preset="acx",
        target_lufs=-19,
        peak_db=-1,
    )
    evaluation = loudness.evaluate_master({"rms": rms, "integrated": -12, "true_peak": -3}, plan)
    assert evaluation["target_met"] is met
    assert evaluation["peak_ok"] is True
    assert any("底噪" in warning and "编码" in warning for warning in evaluation["warnings"])
    assert len(evaluation["warnings"]) == (1 if met else 2)


def test_evaluation_reports_encoded_peak_overshoot() -> None:
    plan = loudness.master_gain(
        {"integrated": -19, "true_peak": -2},
        preset="podcast",
        target_lufs=-19,
        peak_db=-1,
    )
    evaluation = loudness.evaluate_master({"integrated": -19, "true_peak": -0.5}, plan)
    assert evaluation["target_met"] is True
    assert evaluation["peak_ok"] is False
    assert any("真峰值超过" in warning for warning in evaluation["warnings"])
