"""时长备忘：同一条文件只量一次；量不出来（0.0）不记。"""

from __future__ import annotations

import os
import struct
import wave
from pathlib import Path

import pytest

from app.infra import audioprobe


def _file(tmp_path: Path, name: str = "a.wav") -> Path:
    target = tmp_path / name
    target.write_bytes(b"RIFF....WAVEfmt ")
    return target


def _counting(monkeypatch, value: float) -> list[Path]:
    calls: list[Path] = []

    def fake(path: Path, **_kw: object) -> float:
        calls.append(path)
        return value

    monkeypatch.setattr(audioprobe, "probe_duration", fake)
    return calls


def test_the_second_call_uses_the_memo(tmp_path: Path, monkeypatch) -> None:
    calls = _counting(monkeypatch, 1.5)
    target = _file(tmp_path)
    assert audioprobe.probe_duration_cached(target) == 1.5
    assert audioprobe.probe_duration_cached(target) == 1.5
    assert calls == [target]


def test_a_touched_file_is_probed_again(tmp_path: Path, monkeypatch) -> None:
    calls = _counting(monkeypatch, 1.5)
    target = _file(tmp_path)
    audioprobe.probe_duration_cached(target)
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    audioprobe.probe_duration_cached(target)
    assert len(calls) == 2


def test_a_failed_probe_is_not_remembered(tmp_path: Path, monkeypatch) -> None:
    """读不出多半是临时性失败；记成 0 会把它永久钉成「未知」。"""
    calls = _counting(monkeypatch, 0.0)
    target = _file(tmp_path)
    audioprobe.probe_duration_cached(target)
    audioprobe.probe_duration_cached(target)
    assert len(calls) == 2


def _float_wav(tmp_path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    """一个 ``format 3``（32 位浮点）的 WAV：:mod:`wave` 认不出，ffprobe 认得出。"""
    frames = int(rate * seconds)
    data = b"\x00\x00\x00\x00" * frames
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    target = tmp_path / "float.wav"
    target.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return target


def test_a_wav_the_header_reader_cannot_parse_falls_back_to_ffprobe(
    tmp_path: Path, monkeypatch
) -> None:
    """浮点 WAV 不是 :mod:`wave` 认得的那种，但它有长度——别把整批样例判成"没时长"。"""
    target = _float_wav(tmp_path)
    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
    monkeypatch.setattr(audioprobe, "_ffprobe_duration", lambda path, _bin: 1.0)
    assert audioprobe.probe_duration(target) == 1.0


def test_ffprobe_spawns_without_a_console_window(tmp_path: Path, monkeypatch) -> None:
    """无控制台的父进程（pythonw / 打包 exe）不该为每次探测闪一个黑窗。"""
    seen: dict[str, object] = {}

    class Done:
        stdout = '{"format": {"duration": "1.5"}}'

    def fake(command: list[str], **kwargs: object) -> Done:
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(audioprobe.subprocess, "run", fake)
    assert audioprobe._ffprobe_duration(tmp_path / "a.mp3", "ffprobe") == 1.5
    assert seen["creationflags"] == audioprobe.CREATE_NO_WINDOW


def test_a_readable_wav_never_starts_ffprobe(tmp_path: Path, monkeypatch) -> None:
    """读得出头就不起进程（扫描里每个 wav 起一次 ffprobe 是不可接受的慢）。"""
    target = tmp_path / "ok.wav"
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 4000)
    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
    monkeypatch.setattr(
        audioprobe, "_ffprobe_duration", lambda path, _bin: pytest.fail("不该起 ffprobe")
    )
    assert audioprobe.probe_duration(target) == 0.5

def test_stream_probe_reads_codec_rate_and_channels(tmp_path: Path, monkeypatch) -> None:
    """concat 前要确认两段流参数一致；这里只测解析，不起进程。"""
    target = _file(tmp_path)
    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")

    def fake_run(*_args: object, **_kwargs: object) -> object:
        reply = b'{"streams":[{"codec_name":"pcm_s16le","sample_rate":"16000","channels":1}]}'
        return type("R", (), {"stdout": reply})()

    monkeypatch.setattr(audioprobe.subprocess, "run", fake_run)
    assert audioprobe.probe_stream(target) == ("pcm_s16le", 16000, 1)


def test_stream_probe_says_nothing_without_ffprobe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "")
    assert audioprobe.probe_stream(_file(tmp_path)) == ("", 0, 0)


def test_stream_probe_survives_a_junk_reply(tmp_path: Path, monkeypatch) -> None:
    """服务端不按套路回（空 / 没 streams / 采样率是文字）→ 三个空值，绝不抛。"""
    target = _file(tmp_path)
    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
    replies = (b"", b"{}", b"not json", b'{"streams":[]}', b'{"streams":[{"sample_rate":"x"}]}')
    for reply in replies:

        def fake_run(*_args: object, _reply: bytes = reply, **_kwargs: object) -> object:
            return type("R", (), {"stdout": _reply})()

        monkeypatch.setattr(audioprobe.subprocess, "run", fake_run)
        assert audioprobe.probe_stream(target) == ("", 0, 0)
