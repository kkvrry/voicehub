"""运行形态与随包资源：源码直跑与 PyInstaller 冻结包共用一套路径。

冻结后 ``sys._MEIPASS`` 是包根（onedir 下为 ``_internal``）。``app``、``web/dist``
与 ``vendor/ffmpeg`` 都按同一相对布局落在那里，因此这里只回答“资源在哪”，
不关心当前是哪种形态。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["bundle_dir", "bundled_binary", "data_path", "is_frozen"]


def is_frozen() -> bool:
    """当前是不是 PyInstaller 打出来的进程。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """冻结包的资源根；源码运行时返回 ``None``。"""
    if not is_frozen():
        return None
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))


def data_path(relative: str) -> Path:
    """随包资源的绝对路径（源码下以仓库根为基准）。"""
    root = bundle_dir() or Path(__file__).resolve().parents[2]
    return root / relative


def bundled_binary(name: str) -> Path | None:
    """随包附带的可执行文件（``ffmpeg`` / ``ffprobe``）；源码形态或没带时返回 ``None``。"""
    root = bundle_dir()
    if root is None:
        return None
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = root / "vendor" / "ffmpeg" / f"{name}{suffix}"
    return candidate if candidate.is_file() else None
