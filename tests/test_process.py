"""停子进程：先 terminate，等不到才 kill，两条路都不把调用线程挂死。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.infra.process import ChildJob, stop


class _Fake:
    """够 :func:`stop` 用的最小 Popen：记下谁被调过、wait 抛不抛超时。"""

    def __init__(self, *, exited: bool = False, hangs: int = 0) -> None:
        self.exited = exited
        #: 前几次 wait 抛 TimeoutExpired（``-1`` = 永远不退出）。
        self.hangs = hangs
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def terminate(self) -> None:
        self.calls.append("terminate")
        self.exited = True  # 正常进程收到 terminate 就退

    def kill(self) -> None:
        self.calls.append("kill")
        self.exited = True

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait:{timeout}")
        if self.hangs < 0 or self.hangs > 0:
            if self.hangs > 0:
                self.hangs -= 1
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
        return 0


def test_an_already_exited_process_is_left_alone() -> None:
    process = _Fake(exited=True)
    stop(process)
    assert process.calls == []


def test_it_terminates_and_waits() -> None:
    process = _Fake()
    stop(process, grace=2.0)
    assert process.calls == ["terminate", "wait:2.0"]


def test_a_stubborn_process_is_killed() -> None:
    """terminate 之后等不到就 kill，kill 之后再等一次收句柄。"""
    process = _Fake(hangs=1)
    stop(process, grace=2.0)
    assert process.calls == ["terminate", "wait:2.0", "kill", "wait:2.0"]


def test_giving_up_does_not_hang_the_caller() -> None:
    """kill 也杀不掉（僵尸 / 权限）时返回，不再无限等。"""
    process = _Fake(hangs=-1)
    stop(process, grace=0.1)
    assert process.calls == ["terminate", "wait:0.1", "kill", "wait:0.1"]


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 机制")
def test_child_job_kills_children_when_handle_closes() -> None:
    """VoiceHub 被强杀时靠这个关掉子进程：句柄一关，组内进程全结束。"""
    job = ChildJob.create()
    assert job is not None
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert job.assign(process) is True
        job.close()
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
