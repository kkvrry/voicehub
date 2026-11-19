"""内录：状态机、错误码与 PCM 落盘。

不碰真实声卡：采集设备在 CI 与开发机上都不保证存在，也不该在测试里发出声音。
被替掉的只有设备那一层（``loopback.Recorder``），状态机、HTTP 契约与 wav 写入
仍然按真实代码走。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.infra import loopback
from app.usecase import capture as capture_uc


class FakeRecorder:
    """假设备：开口即"正在录"，停在哪个状态由实例记录。"""

    peak = 0.4
    seconds = 12.5

    def __init__(self) -> None:
        self.running = False
        self.error = ""

    def start(self) -> Path:
        self.running = True
        return Path("内录-测试.wav")

    def stop(self) -> dict[str, object]:
        self.running = False
        return {
            "file": "内录-测试.wav",
            "seconds": self.seconds,
            "bytes": 240_000,
            "peak": self.peak,
            "truncated": False,
        }

    def status(self) -> dict[str, object]:
        return {
            "recording": self.running,
            "seconds": self.seconds if self.running else 0.0,
            "peak": self.peak if self.running else 0.0,
            "file": "内录-测试.wav" if self.running else "",
            "error": self.error,
        }


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> capture_uc.CaptureService:
    """把应用层单例换成假设备，并放开"这台机器可用"的判定。"""
    instance = capture_uc.CaptureService(FakeRecorder)
    monkeypatch.setattr(capture_uc, "service", instance)
    monkeypatch.setattr(capture_uc.loopback, "available", lambda: (True, ""))
    return instance


def test_status_reports_hotkey_and_idle(service, client) -> None:
    payload = client.get("/api/v1/capture/status").json()

    assert payload["object"] == "capture_status"
    assert payload["available"] is True
    assert payload["recording"] is False
    assert payload["hotkey"]["text"] == "Ctrl+Alt+R"
    # 托盘还没注册：状态是"未知"，不是"可用"。
    assert payload["hotkey_ok"] is None
    assert payload["label"] and payload["hint"]


def test_start_then_stop(service, client) -> None:
    started = client.post("/api/v1/capture/start").json()
    assert started["recording"] is True
    assert started["seconds"] > 0

    # 第二次开始要明确报"已在进行中"，而不是悄悄再开一路。
    again = client.post("/api/v1/capture/start")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "capture_in_progress"

    stopped = client.post("/api/v1/capture/stop").json()
    assert stopped["object"] == "capture_result"
    assert stopped["seconds"] == 12.5
    assert stopped["silent"] is False


def test_stop_without_recording(service, client) -> None:
    response = client.post("/api/v1/capture/stop")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capture_idle"


def test_unavailable_device(service, client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capture_uc.loopback, "available", lambda: (False, "没有可用的输出设备。")
    )

    status = client.get("/api/v1/capture/status").json()
    assert status["available"] is False
    assert status["reason"] == "没有可用的输出设备。"

    response = client.post("/api/v1/capture/start")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "capture_unavailable"


def test_silent_recording_is_flagged(service, client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeRecorder, "peak", 0.0)
    client.post("/api/v1/capture/start")

    assert client.post("/api/v1/capture/stop").json()["silent"] is True


def test_hotkey_failure_is_reported(service, client) -> None:
    service.note_hotkey(False, "热键被其他程序占用。")

    payload = client.get("/api/v1/capture/status").json()
    assert payload["hotkey_ok"] is False
    assert payload["hotkey_reason"] == "热键被其他程序占用。"


def test_write_mixes_channels_to_mono(tmp_path: Path) -> None:
    """设备给的是多声道，落盘必须是单声道 PCM16：取平均，不做截断取左。"""
    recorder = loopback.Recorder(samplerate=8000)
    target = tmp_path / "mono.wav"
    block = np.full((100, 2), 0.5, dtype=np.float32)

    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        assert recorder._write(out, block) == 100

    with wave.open(str(target), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 8000
        assert handle.getnframes() == 100
        sample = int.from_bytes(handle.readframes(1), "little", signed=True)
    assert 16000 < sample < 16700  # 0.5 × 32767
