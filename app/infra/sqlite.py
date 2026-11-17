"""SQLite 连接：``with`` 块结束就真的关闭，而不是等引用计数回收。

``sqlite3.Connection`` 当上下文管理器用时只 commit / rollback，**不 close**
（Python 文档明确写了）。本地服务里每个请求都开新连接，Windows 上句柄与
``-wal`` / ``-shm`` 会多占一会儿，正好撞上 ffmpeg 改名的"文件被占用"重试。

这里用连接子类把 ``close`` 挂到 ``__exit__`` 上：调用点的 ``with conn:`` 写法
完全不变，退出时先按原语义提交/回滚，再关句柄。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["connect"]


class _ClosingConnection(sqlite3.Connection):
    """退出 ``with`` 时提交/回滚并关闭。"""

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(
    path: str | Path, *, timeout: float = 5.0, busy_ms: int = 3000
) -> sqlite3.Connection:
    """开一条连接：行工厂是 :class:`sqlite3.Row`，撞锁时等 ``busy_ms`` 毫秒。"""
    conn = sqlite3.connect(str(path), timeout=timeout, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    return conn
