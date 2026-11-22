"""本机 shell 操作：打开目录、选择本机文件。

后台服务直接 ``os.startfile`` 时，Windows 的前台锁会把新窗口压在浏览器后面；
这里显式放开前台权限，并在窗口出现后补一次 ``SetForegroundWindow``。

文件选择框只能在本机弹：浏览器拿不到真实路径，引用导入必须先由宿主问出路径。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["open_folder", "pick_media_file"]

_ASFW_ANY = -1
_SW_RESTORE = 9
_WAIT_SECONDS = 0.45
_CREATE_NO_WINDOW = 0x08000000

#: 文件选择框的过滤器：与项目导入接受的容器一致，避免选中后才发现不认。
_PICK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.StartPosition = 'CenterScreen'
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择要引用的音视频'
$dialog.Filter = '音视频|*.mp4;*.mov;*.mkv;*.webm;*.avi;*.wav;*.mp3;*.m4a;*.aac;*.flac;*.ogg;*.opus|所有文件|*.*'
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.FileName)
}
$owner.Dispose()
"""


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


def pick_media_file() -> Path | None:
    """弹本机文件选择框；取消返回 ``None``。

    调用方负责限定本机来源：这个函数只回答"用户选了哪个文件"，不做权限判断。
    选择框停在用户桌面上，可以停留很久；超时只防进程僵死，不催用户。
    """
    if os.name != "nt":
        raise OSError("只有 Windows 能弹出本机文件选择框。")
    try:
        done = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-Command",
                _PICK_SCRIPT,
            ],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise OSError("文件选择框停留过久，已关闭；再点一次即可。") from None
    if done.returncode != 0:
        detail = " ".join((done.stderr or "").split())[:200] or "原因未知"
        raise OSError(f"打不开文件选择框：{detail}")
    chosen = (done.stdout or "").strip()
    return Path(chosen) if chosen else None
