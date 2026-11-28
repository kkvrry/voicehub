"""文件指纹：路径 + 大小 + mtime 归一处，判据一致才不会读到旧缓存。"""

from __future__ import annotations

import os
from pathlib import Path

from app.infra.atomic import write_json
from app.infra.files import file_stamp


def test_the_stamp_carries_path_size_and_mtime(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    target.write_text("{}", encoding="utf-8")
    stamp = file_stamp(target)
    assert stamp is not None
    assert stamp[0] == os.path.normcase(str(target))
    assert stamp[1] == 2
    assert stamp[2] == target.stat().st_mtime_ns


def test_a_touched_file_changes_its_stamp(tmp_path: Path) -> None:
    """只比大小会漏掉原地改写：内容等长时全靠 mtime 那一格。"""
    target = tmp_path / "a.json"
    target.write_text("ab", encoding="utf-8")
    before = file_stamp(target)
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert file_stamp(target) != before


def test_a_missing_file_has_no_stamp(tmp_path: Path) -> None:
    """读不到返回 None：调用方要区分"文件没了"与"文件是空的"。"""
    assert file_stamp(tmp_path / "没有这个.json") is None


def test_the_case_of_the_path_does_not_matter_on_windows(tmp_path: Path) -> None:
    target = tmp_path / "Voice.json"
    target.write_text("{}", encoding="utf-8")
    upper = Path(str(target).upper())
    if not upper.exists():  # Linux 上大小写是两个文件，这条判据不适用
        return
    assert file_stamp(upper) == file_stamp(target)


def test_write_json_keeps_original_on_serialization_error_and_leaves_no_tmp(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    write_json(target, {"ok": True})
    assert not (tmp_path / "config.tmp").exists()
    try:
        write_json(target, {"bad": float("nan")}, allow_nan=False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for NaN")
    assert target.read_text(encoding="utf-8") == '{\n  "ok": true\n}'
    assert [p.name for p in tmp_path.glob("config.json*")] == ["config.json"]


def test_write_json_retries_when_target_is_temporarily_locked(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    write_json(target, {"step": 1})
    real_replace = os.replace
    attempts = 0

    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr("app.infra.atomic.os.replace", flaky_replace)
    write_json(target, {"step": 2})
    assert attempts == 3
    assert '"step": 2' in target.read_text(encoding="utf-8")
