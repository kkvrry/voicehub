"""向量结果的磁盘缓存：模型目录里的一个 SQLite 文件。

键 = 模型 key/版本 + 查询标记 + 文本 SHA1。音色信息不变就只读一次，重启后
也能直接复用；缓存跟着模型目录走，删除模型时一起消失，不另建需要清理的数据。

读写失败只影响「下次重算」，不阻塞召回——缓存是加速层，不是数据源。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from app.embedding.base import EmbeddingProvider
from app.infra.loghub import CHANNEL

__all__ = ["VectorCache", "cache_for"]

_CHUNK = 400
_CACHES: dict[str, VectorCache] = {}
_CACHES_LOCK = threading.Lock()
_WARNED: set[str] = set()


class VectorCache:
    """一个向量模型的缓存库。版本变化时清掉旧版本的行。"""

    def __init__(self, path: Path, version: str) -> None:
        self.path = Path(path)
        self.version = str(version)
        self._lock = threading.Lock()
        self._prepared = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            " key TEXT PRIMARY KEY, version TEXT NOT NULL,"
            " dim INTEGER NOT NULL, vector BLOB NOT NULL, updated REAL NOT NULL)"
        )
        if not self._prepared:
            conn.execute("DELETE FROM vectors WHERE version <> ?", (self.version,))
            conn.commit()
            self._prepared = True
        return conn

    def get(self, keys: Sequence[str], dimension: int) -> dict[str, np.ndarray]:
        """命中缓存的向量；维度对不上的行直接忽略。"""
        unique = list(dict.fromkeys(str(key) for key in keys))
        if not unique or dimension <= 0:
            return {}
        found: dict[str, np.ndarray] = {}
        try:
            with self._lock:
                conn = self._connect()
                try:
                    for start in range(0, len(unique), _CHUNK):
                        chunk = unique[start : start + _CHUNK]
                        marks = ",".join("?" for _ in chunk)
                        rows = conn.execute(
                            f"SELECT key, vector FROM vectors"
                            f" WHERE dim = ? AND key IN ({marks})",
                            (dimension, *chunk),
                        ).fetchall()
                        for key, blob in rows:
                            if len(blob) != dimension * 4:
                                continue
                            found[str(key)] = np.frombuffer(blob, dtype="<f4")
                finally:
                    conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._warn(f"读取向量缓存失败：{exc}")
            return {}
        return found

    def put(self, items: Mapping[str, Sequence[float]], dimension: int) -> None:
        if not items or dimension <= 0:
            return
        now = time.time()
        rows: list[tuple[str, str, int, bytes, float]] = []
        for key, vector in items.items():
            array = np.asarray(vector, dtype="<f4")
            if array.shape != (dimension,):
                continue
            rows.append((str(key), self.version, dimension, array.tobytes(), now))
        if not rows:
            return
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.executemany(
                        "INSERT OR REPLACE INTO vectors"
                        " (key, version, dim, vector, updated)"
                        " VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except (sqlite3.Error, OSError) as exc:
            self._warn(f"写入向量缓存失败：{exc}")

    def _warn(self, message: str) -> None:
        key = str(self.path)
        if key in _WARNED:
            return
        _WARNED.add(key)
        CHANNEL.warning(message, source="embedding")


def cache_for(provider: EmbeddingProvider) -> VectorCache:
    """一个模型目录一个缓存对象；模型换版本时换新对象并清扫旧行。"""
    path = provider.model_dir() / "vectors.db"
    key = str(path)
    with _CACHES_LOCK:
        cache = _CACHES.get(key)
        if cache is None or cache.version != str(provider.version):
            cache = VectorCache(path, str(provider.version))
            _CACHES[key] = cache
        return cache
