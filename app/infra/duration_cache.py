"""时长缓存：``路径 → (大小, mtime, 时长)``，重扫不为没动过的文件再起 ffprobe 进程。

判据是大小 + mtime 都一致（只比大小会漏原地改写，只比 mtime 会被 touch 全量打穿）；
对不上就重量——错的值比慢更糟。缓存存 hub.db（同为派生物，可随时重建），
不清理死行（收益只有几百 KB，不值得加路径）；真嫌大删 hub.db 即可。

线程模型：一个实例服务一次扫描，不跨线程共用；写回库由调用方收尾一次做完（:meth:`entries`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.db import path_key

__all__ = ["DurationCache"]

#: 一条缓存记录：``路径 → (大小, mtime, 时长)``。
Known = Mapping[str, tuple[int, float, float]]


@dataclass(slots=True)
class DurationCache:
    """:param known: 上次扫描之后库里留着的那一份（``HubDB.duration_cache()``）。"""

    _known: dict[str, tuple[int, float, float]] = field(default_factory=dict)
    #: 本次**新量出来**的（收尾时写回库）。
    _fresh: list[tuple[str, int, float, float]] = field(default_factory=list)
    #: 查过、没命中的那几条的指纹——量完回来时不必再 ``stat`` 一遍。
    _staged: dict[str, tuple[int, float]] = field(default_factory=dict)

    @classmethod
    def loaded(cls, known: Known | None) -> DurationCache:
        """从库里那一份建一个（``known`` 为 ``None``/空 = 空缓存，全量重量一次）。"""
        return cls(_known=dict(known or {}))

    @property
    def is_empty(self) -> bool:
        """库里一条缓存都没有——通常是 ``hub.db`` 刚重建，或这个库从没扫过。

        walker 据此决定要不要认 sidecar 里的时长备份：缓存空 = 上次的实测值已经
        无从查考，备份是眼下最靠谱的；缓存不空 = 有指纹可比，改动照常靠 size/mtime 认。
        """
        return not self._known

    def lookup(self, path: Path) -> float | None:
        """命中给时长；没命中给 ``None``（调用方去量，量完调 :meth:`remember`）。

        折形规则与库那边共用 :func:`app.infra.db.path_key`：两边各写一套，
        ``C:\\x`` 与 ``c:/x`` 将永远不命中。
        """
        try:
            stat = path.stat()
        except OSError:
            return None  # 读不到 stat（权限 / 文件没了）：不进 _staged，也就不会写回
        return self.lookup_known(path, stat.st_size, stat.st_mtime)

    def lookup_known(self, path: Path, size: int, mtime: float) -> float | None:
        """同 :meth:`lookup`，但指纹由调用方给（walker 已在 :func:`os.scandir` 里
        stat 过一次，这里不该再 stat 一遍）。"""
        key = path_key(str(path))
        known = self._known.get(key)
        if known and known[0] == size and known[1] == mtime:
            return known[2]
        self._staged[key] = (size, mtime)
        return None

    def remember(self, path: Path, duration: float) -> None:
        """把刚量出来的一条记下来（写回库时用）。

        读不出的（``0.0``）不记：那多半是临时性失败（ffprobe 未装 / 超时 / 文件被占用），
        记下就永久钉成「未知」。指纹取 :meth:`lookup` 那一刻的；没查过或 stat 失败的
        路径也不记——记"大小 0、mtime 0"的行只会让下次扫描对不上。
        """
        if duration <= 0:
            return
        key = path_key(str(path))
        fingerprint = self._staged.pop(key, None)
        if fingerprint is None:
            return
        self._fresh.append((key, fingerprint[0], fingerprint[1], duration))

    def entries(self) -> list[tuple[str, int, float, float]]:
        """本次新量的那些（``(路径, 大小, mtime, 时长)``）——交给 ``HubDB`` 落盘。"""
        return list(self._fresh)
