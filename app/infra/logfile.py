"""日志落盘：``<数据目录>/logs/voicehub.log``，全量留存（界面清屏只清视图）。

一行一条、人眼可读；``ctx`` 以 ``键=值`` 缀在行尾，可 grep。
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.domain.log import LogRecord

__all__ = ["FileSink"]

#: 单份文件上限，超了挪成 ``voicehub.log.1`` 再开新的（本文件随进程一直追加，须设上限）。
_MAX_BYTES = 2 * 1024 * 1024


def _line(record: LogRecord) -> str:
    """一条 → 一行。``ctx`` 缀在行尾（空 dict 不缀）。"""
    head = f"{record.at.isoformat()} [{record.level.upper()}] {record.source or 'unknown'} {record.message}"
    if not record.ctx:
        return head
    tail = " ".join(f"{key}={value}" for key, value in record.ctx.items())
    return f"{head} | {tail}"


class FileSink:
    """追加式落地端（**按大小轮转一次**，见 :data:`_MAX_BYTES`）。

    :param path: 落点，目录不存在会自建。
    :param max_bytes: 单份上限；``0`` = 不轮转。

    线程安全：写入方可能是任何工作线程，加锁。永不抛——磁盘满、权限错不得带崩任务。
    """

    def __init__(self, path: Path, *, max_bytes: int = _MAX_BYTES) -> None:
        self._path = Path(path)
        self._max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 建不出来就等第一次写的时候再失败一次，不在这里炸

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, record: LogRecord) -> None:
        text = _line(record) + "\n"
        with self._lock:
            try:
                self._rotate_if_needed()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError:
                # 落不下去只丢这一条，不在这里再记日志（防重入）。
                pass

    def _rotate_if_needed(self) -> None:
        """超上限就把当前那份挪成 ``.1``（覆盖上一代），再从头开——只留一份上一代。"""
        if not self._max_bytes:
            return
        try:
            if self._path.stat().st_size < self._max_bytes:
                return
        except OSError:
            return  # 文件尚不存在（第一次写）
        try:
            self._path.replace(self._path.with_name(self._path.name + ".1"))
        except OSError:
            pass
