"""把文件/文件夹移进回收站（可还原）——本地音色「移除」走这条路。

不直接删：界面上一颗按钮背后动的是用户音库文件，一次误点不该不可逆。
不用 ``os.remove`` / ``shutil.rmtree``：删完无法找回。只实现 Windows：
其他平台上宁可报错也不退化成真删。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from pathlib import Path

__all__ = ["TrashError", "move_to_trash", "trash_supported"]

#: ``SHFileOperationW`` 的 ``wFunc``。
_FO_DELETE = 3

#: ``fFlags``：允许撤销（= 进回收站）+ 不弹确认框（界面已经问过一次了）+
#: 出错不弹系统对话框（错误要回到调用方去给出说明，不是弹一个看不懂的框）。
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_NOERRORUI = 0x0400
_FOF_SILENT = 0x0004


class TrashError(RuntimeError):
    """移不进回收站——文件原封不动，调用方照此文案回给用户。"""


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("wFunc", wt.UINT),
        ("pFrom", wt.LPCWSTR),
        ("pTo", wt.LPCWSTR),
        ("fFlags", wt.WORD),
        ("fAnyOperationsAborted", wt.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wt.LPCWSTR),
    ]


def trash_supported() -> bool:
    return os.name == "nt"


def move_to_trash(path: str | Path) -> None:
    """把 ``path``（文件或文件夹）移进回收站。失败一律抛 :class:`TrashError`。

    ``pFrom`` 是双空结尾的路径串：少一个空字符，API 会把后面的内存当第二个路径。
    """
    target = Path(path)
    if not trash_supported():
        raise TrashError("移入回收站仅在 Windows 上实现，本次未修改任何文件。")
    if not target.exists():
        # 不存在不算错误（重扫之间的时间差里文件可能已没了）。
        return

    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = str(target) + "\0"
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI | _FOF_SILENT
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise TrashError(
            f"移入回收站失败（系统返回 {code}），文件未变更，库中记录保留。"
        )
