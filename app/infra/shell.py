"""本机 shell 操作：打开目录时把资源管理器窗口带回前台。

后台服务直接 ``os.startfile`` 时，Windows 的前台锁会把新窗口压在浏览器后面；
这里显式放开前台权限，并在窗口出现后补一次 ``SetForegroundWindow``。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["open_folder"]

_ASFW_ANY = -1
_SW_RESTORE = 9
_WAIT_SECONDS = 0.45


def _foreground_explorer(name: str) -> None:
    time.sleep(_WAIT_SECONDS)
    user32 = ctypes.windll.user32
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, 64)
        if class_name.value != "CabinetWClass":
            return True
        size = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(size + 1)
        user32.GetWindowTextW(hwnd, title, size + 1)
        if name in title.value:
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(callback_type(visit), 0)
        if not found:
            return
        user32.AllowSetForegroundWindow(_ASFW_ANY)
        user32.ShowWindow(found[0], _SW_RESTORE)
        user32.SetForegroundWindow(found[0])
    except OSError:
        # 前台权限拿不到时窗口仍已打开，只是可能留在后面。
        return


def open_folder(path: Path) -> None:
    """打开目录；只在 Windows 上可用。"""
    if os.name != "nt":
        raise OSError("只有 Windows 能打开目录。")
    target = str(path)
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)
    except (AttributeError, OSError):
        pass
    subprocess.Popen(["explorer.exe", target])
    threading.Thread(
        target=_foreground_explorer, args=(path.name,), daemon=True
    ).start()
