"""audio.cpp 运行中的模型 → 引擎可用性。

一处探测，供识别的自动选择与引擎清单共用。这里只回答"此刻有没有对应模型在跑"，
不改写引擎的就绪语义：云引擎与用户自行部署的服务不经过 audio.cpp，照旧可用。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from typing import Any

from app.engines.registry import list_asr_engines, list_engines
from app.producers.builtin.audiocpp.runtime import FAMILY_ALIASES

__all__ = ["live_engine_keys", "running_models"]

#: audio.cpp 自己的探测已有 1.5s 缓存；这里再压一层，避免每个接口各打一遍实例。
_TTL = 3.0
_guard = threading.Lock()
_cached: tuple[float, list[dict[str, Any]]] | None = None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def running_models(*, fresh: bool = False) -> list[dict[str, Any]]:
    """运行实例注册的模型 ``[{id, family, task, path, package, url}]``。

    只取已构造的 audio.cpp 服务，不为读状态去构造它（构造会按自动启动清单拉进程）。
    读不到返回空表——没有可报告的模型不等于没有可用引擎。
    """
    global _cached
    now = time.monotonic()
    if not fresh:
        with _guard:
            if _cached is not None and now - _cached[0] < _TTL:
                return [dict(row) for row in _cached[1]]
    rows: list[dict[str, Any]] = []
    try:
        from app.usecase.producers import started_service

        service = started_service("audiocpp")
        if service is not None:
            rows = list(service.running_models())
    except Exception:  # noqa: BLE001 - 运行时读不到就是没有可报告的模型
        rows = []
    with _guard:
        _cached = (now, rows)
    return [dict(row) for row in rows]


def live_engine_keys(
    keys: Iterable[str] | None = None, *, fresh: bool = False
) -> dict[str, dict[str, str]]:
    """``{engine_key: {id, family, url}}``：按族名把运行中模型归到本项目的引擎 key。

    ``keys`` 限定只关心哪几台；默认合成与识别全量。别名表只有一份，取自 audio.cpp
    的 ``FAMILY_ALIASES``，不在这里另写一份 key ↔ 族名对照。
    """
    wanted = (
        set(keys)
        if keys is not None
        else set(list_engines()) | set(list_asr_engines())
    )
    by_family: dict[str, str] = {}
    for key in wanted:
        for name in (FAMILY_ALIASES.get(key, ""), key):
            norm = _norm(name)
            if norm:
                by_family[norm] = key
    out: dict[str, dict[str, str]] = {}
    for row in running_models(fresh=fresh):
        key = by_family.get(_norm(row.get("family") or ""))
        if not key or key in out:
            continue
        out[key] = {
            "id": str(row.get("id") or ""),
            "family": str(row.get("family") or ""),
            "url": str(row.get("url") or ""),
        }
    return out
