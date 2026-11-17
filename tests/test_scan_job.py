"""后台扫描：起一次、报进度、结束带结果；连点两下不会变成两次扫描。

原来 ``POST /scan`` 同步扫完才返回，大库要几十秒，请求线程被占着、界面只能干等。
现在接口立刻返回任务快照，进度由 ``GET /scan/status`` 轮询——这个文件盯的就是
"立刻返回""状态会推进""失败要说出来"这三件事。
"""

import threading
import time
from pathlib import Path

from app.usecase.library import LibraryService


def _wait(service, timeout: float = 30.0) -> dict:
    """等后台任务落定（done / failed）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.scan_status()
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("扫描没有在预期时间内结束")


def test_status_is_idle_before_any_scan(service) -> None:
    job = service.scan_status()
    assert job["state"] == "idle"
    assert job["id"] == ""


def test_the_endpoint_returns_immediately_and_reports_the_result(
    client, demo_library: Path
) -> None:
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})  # 这条仍是同步的

    started = client.post("/api/v1/scan").json()
    assert started["object"] == "scan_job"
    assert started["kind"] == "scan"
    assert started["state"] in {"running", "done"}  # 小库可能已经扫完

    job = started
    for _ in range(300):
        if job["state"] != "running":
            break
        time.sleep(0.02)
        job = client.get("/api/v1/scan/status").json()

    assert job["state"] == "done"
    assert job["phase"] == "完成"  # 别停在"写索引"，那会让人以为还在写
    assert job["total"] == 1
    assert job["done"] == 1
    assert job["result"]["voices"] == 4
    assert job["result"]["samples"] >= 4
    assert job["elapsed"] >= 0


def test_a_second_start_reuses_the_running_job(service, demo_library: Path, monkeypatch) -> None:
    """连点两下 = 一个任务。再起一个会立刻撞锁，那不是用户想要的反馈。"""
    service.save_folders([str(demo_library)])

    gate = threading.Event()
    original = LibraryService._scan

    def slow(self, *, progress=None):
        gate.wait(10)
        return original(self, progress=progress)

    monkeypatch.setattr(LibraryService, "_scan", slow)
    first = service.start_scan()
    second = service.start_scan()
    assert second["id"] == first["id"]
    gate.set()
    assert _wait(service)["state"] == "done"


def test_progress_walks_the_roots_and_ends_with_the_write(service, demo_library: Path) -> None:
    service.save_folders([str(demo_library)])
    seen: list[dict] = []

    result = service.scan(progress=lambda **kw: seen.append(kw))

    assert result["voices"] == 4
    phases = [item["phase"] for item in seen if item.get("phase")]
    assert phases[0] == "扫描根目录"
    assert any(phase.startswith("扫描 1/1：") for phase in phases)
    assert phases[-1] == "写索引"
    assert seen[-1]["done"] == 1
    # 单个根里发现的条数会边扫边报，不必等整个根走完。
    assert any("voices" in item for item in seen)


def test_a_failing_scan_is_reported_instead_of_hanging(service, monkeypatch) -> None:
    def boom(self, *, progress=None):
        raise RuntimeError("磁盘拔了")

    monkeypatch.setattr(LibraryService, "_scan", boom)
    service.start_scan()

    job = _wait(service)
    assert job["state"] == "failed"
    assert "磁盘拔了" in job["error"]
    assert "result" not in job  # 失败就没有结果，别给个空对象装作有


def test_the_status_endpoint_carries_the_same_shape(client) -> None:
    """界面只认一套形状：idle 时字段也得齐，不然前端要写两套判断。"""
    job = client.get("/api/v1/scan/status").json()
    assert job["object"] == "scan_job"
    for key in ("id", "kind", "state", "phase", "total", "done", "voices", "samples", "elapsed"):
        assert key in job
