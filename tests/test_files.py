"""文件指纹：路径 + 大小 + mtime 归一处，判据一致才不会读到旧缓存。"""

from __future__ import annotations

import os
from pathlib import Path

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
