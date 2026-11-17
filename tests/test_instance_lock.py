"""数据目录单实例锁：第二个进程拿不到，前一个退出后立刻能拿。"""

from __future__ import annotations

from pathlib import Path


def test_the_second_holder_is_refused(tmp_path: Path) -> None:
    from app.infra.instancelock import InstanceLock

    first = InstanceLock(tmp_path / "service.lock")
    second = InstanceLock(tmp_path / "service.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_a_leftover_file_from_a_crashed_process_does_not_block(tmp_path: Path) -> None:
    """崩溃后锁文件还在，但没有人持有它——新进程要能直接启动。"""
    from app.infra.instancelock import InstanceLock

    path = tmp_path / "service.lock"
    path.write_text("999999\n", encoding="utf-8")
    lock = InstanceLock(path)
    assert lock.acquire()
    assert lock.owner_pid() > 0
    lock.release()


def test_write_bytes_leaves_no_partial_file(tmp_path: Path) -> None:
    from app.infra.atomic import write_bytes

    target = tmp_path / "line.mp3"
    target.write_bytes(b"old")
    write_bytes(target, b"new-audio")
    assert target.read_bytes() == b"new-audio"
    assert not list(tmp_path.glob("*.part"))


def test_write_bytes_keeps_the_old_copy_when_replacing_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """改名失败（文件被占）时，目标仍是上一份完整内容，不会出现半份。"""
    from app.infra import atomic

    target = tmp_path / "line.mp3"
    target.write_bytes(b"old")

    def refuse(_src, _dst):
        raise PermissionError("busy")

    monkeypatch.setattr(atomic.os, "replace", refuse)
    try:
        atomic.write_bytes(target, b"new", attempts=2)
    except PermissionError:
        pass
    else:  # pragma: no cover - 上面必定抛
        raise AssertionError("应当把占用异常交给调用方")
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.part"))
