"""项目内的文件路径：行上记的相对路径、目录列一次的内存快照。

行上的音频路径有两种写法——新记录相对项目根、老记录是绝对路径，
:func:`abs_path` 两种都认；:class:`DirListing` 把「文件还在不在」的逐个磁盘询问
换成一次目录列出。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DirListing", "abs_path"]


def abs_path(root: Path, value: str) -> Path:
    """行上的音频路径 → 真实路径：新记录相对项目根，老记录是绝对路径，两种都认。

    **批量场景先取一次 ``root`` 再逐条用它**：``resolve_audio`` 每条都要查一次库
    拿项目根，一万行就是一万次 SQLite——合并、校对、时间轴这类整章遍历都吃过这个。
    """
    path = Path(value)
    return path if path.is_absolute() else root / path


class DirListing:
    """按目录缓存文件名：判断「这个文件还在不在」不必逐个问磁盘。

    Windows 上一次 ``is_file()`` 实测约 7ms（杀软要过一遍），一万行就是一分多钟；
    同一目录列一遍是毫秒级，之后全是内存查找。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[Path, set[str]] = {}

    def has(self, value: str) -> bool:
        path = abs_path(self.root, value)
        folder = path.parent
        names = self._cache.get(folder)
        if names is None:
            try:
                with os.scandir(folder) as entries:
                    names = {entry.name for entry in entries if entry.is_file()}
            except OSError:
                names = set()
            self._cache[folder] = names
        return path.name in names
