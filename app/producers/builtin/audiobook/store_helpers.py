"""有声书项目库公共纯函数（由 store / store_rows / store_voices 共用）。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any

from app.producers.builtin.audiobook.rows import is_broken


def _stamp() -> float:
    return time.time()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _voice_tuple(value: dict[str, Any] | None) -> tuple[str, ...] | None:
    """一条音色映射的比较键；没指定（或没有音色 id）就是空。"""
    if not value or not value.get("voice_id"):
        return None
    return (
        str(value.get("engine") or ""),
        str(value.get("voice_id") or ""),
        str(value.get("voice_name") or ""),
        str(value.get("sample_mode") or "emotion"),
        str(value.get("sample_file") or ""),
    )


def _params_json(params: dict[str, Any]) -> str:
    """一行参数落库的形状：键排序，空表就是 ``{}``（指纹据此判定"没改过"）。"""
    return json.dumps(params or {}, ensure_ascii=False, sort_keys=True)


def _params_dict(raw: Any) -> dict[str, Any]:
    """库里那串参数 → 字典；坏 JSON 当没设过（不让一条脏数据废掉整行）。"""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


#: ``projects`` 里除 ``source`` 之外的全部列：详情与路径解析只需要这些。
_PROJECT_META = (
    "id, name, author, folder, kind, pattern_key, split_mode, split_dialogue,"
    " split_quote, split_max_chars, layout_version, created_at, updated_at"
)


def _broken_flag(text: str | None) -> int:
    """SQL 里的「需校对」判据：与行表筛选共用 :func:`is_broken` 同一句正则。"""
    return 1 if is_broken(text or "") else 0

