"""测试夹具：每个用例一套独立数据目录 + 演示音色库（**真文件**，走完整条链）。"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest


def make_wav(path: Path, *, seconds: float = 0.5, freq: float = 440.0) -> None:
    """写一段正弦波 wav（标准库，无依赖）。"""
    rate = 22050
    frames = int(rate * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        data = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(frames)
        )
        handle.writeframes(data)


@pytest.fixture()
def demo_library(tmp_path: Path) -> Path:
    """一套演示音色库：

    ```
    library/
      原神/胡桃/{开心.wav,开心.txt,难过.wav,默认.wav}
      原神/钟离/{默认.wav,list.txt}
      明日方舟/阿米娅/{默认.wav}
      散件.wav
    ```
    """
    root = tmp_path / "library"
    hutao = root / "原神" / "胡桃"
    make_wav(hutao / "开心.wav", seconds=0.4)
    (hutao / "开心.txt").write_text("吃饱喝饱，一路走好！", encoding="utf-8")
    make_wav(hutao / "难过.wav", seconds=0.6)
    make_wav(hutao / "默认.wav", seconds=0.5)
    zhongli = root / "原神" / "钟离"
    make_wav(zhongli / "默认.wav", seconds=0.5)
    (zhongli / "list.txt").write_text("默认|欲买桂花同载酒\n", encoding="utf-8")
    make_wav(root / "明日方舟" / "阿米娅" / "默认.wav", seconds=0.5)
    make_wav(root / "散件.wav", seconds=0.3)
    return root


def rescan_and_wait(client, timeout: float = 60.0) -> dict:
    """重扫并等后台任务落定（``POST /scan`` 现在是"起任务 + 轮询"）。

    依赖"扫完再用库"的测试用它，别自己写一遍轮询——两处阈值不一致时最难查。
    """
    import time

    client.post("/api/v1/scan")
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/v1/scan/status").json()
        if job["state"] != "running":
            assert job["state"] == "done", job.get("error")
            return job
        time.sleep(0.02)
    raise AssertionError("扫描没有在预期时间内结束")


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """独立数据目录（VOICEHUB_DATA_DIR 是 config 层认的唯一覆盖口）。"""
    target = tmp_path / "data"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOICEHUB_DATA_DIR", str(target))
    return target


def enable_every_engine() -> list[str]:
    """把发现到的每台引擎写进启用清单——测试的**默认起点**。

    产品默认相反：全新配置里一台也不启用，由用户在「引擎」页添加。绝大多数用例
    检的是"已启用之后"的行为，逐个用例先走一遍添加只会淹没被测的那件事。
    启停本身的用例自己改这份清单（见 tests/test_engines.py::TestEnableDisable）。
    """
    from app.engines.registry import list_engines
    from app.infra.config import load_config, save_config

    config = load_config()
    config.engines_enabled = sorted(list_engines())
    save_config(config)
    return config.engines_enabled


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的配置目录，且默认"全部引擎已启用"。

    两件事都是必需的：

    - **不隔离**就会读写开发机上真实的 ``config.json``——曾经把测试的启用清单写进了
      用户的配置里。
    - **不预设启用清单**，用例就会随"开发机启用了哪几台"而变：启用是产品行为，
      不该由跑测试的那台机器决定。直连 ``AudiobookService(tmp_path)`` 的用例没有
      ``client`` / ``service`` 夹具替它启用，全都会踩到这条。

    要"一台都没启用"的用例自己改这份清单（见 ``TestEnableDisable``）；要另一套目录的
    用例用 ``data_dir``——它比本夹具晚生效，会把 ``VOICEHUB_DATA_DIR`` 指到别处。
    """
    target = tmp_path / "profile"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VOICEHUB_DATA_DIR", str(target))
    enable_every_engine()
    return target


@pytest.fixture()
def service(data_dir: Path):
    from app.infra.db import HubDB
    from app.usecase.library import LibraryService

    enable_every_engine()
    return LibraryService(HubDB(data_dir / "hub.db"))


@pytest.fixture()
def client(data_dir: Path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    enable_every_engine()
    return TestClient(create_app())


@pytest.fixture()
def library(client):
    """``client`` 背后的那个 :class:`LibraryService`。

    给"需要先有条平台音色"的用例种数据用——**手动登记的 HTTP 口已经删了**
    （登记出来的条目没有任何引擎认得它的 id），但平台音色这个**东西**还在，
    只是改由引擎自己拉清单填进来。测试要造一条，就得直接调用例层。
    """
    return client.app.state.library


@pytest.fixture()
def seed_platform_voice(library):
    """种一条平台音色：``seed_platform_voice(voice_id="MX_001", name="林晚")``。"""

    def _seed(engine: str = "minimax", voice_id: str = "MX_V", name: str = "林晚"):
        from app.engines.base import VoiceMeta

        return library.add_platform_voice(engine, VoiceMeta(id=voice_id, name=name))

    return _seed
