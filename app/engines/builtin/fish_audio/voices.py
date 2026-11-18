"""``GET /model`` 的响应解析。

官方响应体（``docs.fish.audio/api-reference/endpoint/model/list-models``）：

.. code-block:: json

    {
      "total": 42,
      "items": [
        {"_id": "…", "title": "我的音色", "description": "…", "type": "tts",
         "state": "trained", "tags": [], "languages": ["zh", "en"], "visibility": "private"}
      ],
      "has_more": false
    }

**只列能合成的条目**：``type`` 非 ``tts`` 的是语音识别模型，``state`` 为 ``failed``
的是训练失败——它们出现在音色里，点一次才失败。

语言这一格优先读平台自己的 ``languages`` 字段（它是明确声明，不是猜的）；平台没给
才退回 :func:`app.engines.traits.read_traits` 从标题与说明里认。性别与年龄感没有
平台字段，一律由 traits 认，认不出留空——那两格不许覆盖用户手改过的值。
"""

from __future__ import annotations

from typing import Any

from app.domain.models import LANGUAGES
from app.engines.base import VoiceMeta
from app.engines.traits import read_traits

__all__ = ["parse_voice_list", "voice_items"]


def voice_items(payload: Any) -> list[dict[str, Any]]:
    """从响应体里挖出音色对象（``items`` 是官方形状，``data`` 兼容中转网关）。"""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _language_of(item: dict[str, Any], fallback: str) -> str:
    """平台 ``languages`` 里第一个统一词表认得的代码；没有则用 traits 的结果。"""
    raw = item.get("languages")
    if isinstance(raw, list):
        for code in raw:
            key = str(code or "").strip().lower()
            if key in LANGUAGES:
                return key
    return fallback


def _tags_of(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        return ()
    return tuple(part.strip() for part in parts if part.strip())


def _usable(item: dict[str, Any]) -> bool:
    """能不能合成：``type`` 是 tts 且不是训练失败。"""
    kind = str(item.get("type") or "").strip().lower()
    if kind and kind != "tts":
        return False
    return str(item.get("state") or "").strip().lower() != "failed"


def parse_voice_list(payload: Any) -> tuple[VoiceMeta, ...]:
    """响应体 → 音色清单。``_id`` 是发给平台的值，``title`` 只给界面。"""
    items: list[VoiceMeta] = []
    seen: set[str] = set()
    for item in voice_items(payload):
        voice_id = str(item.get("_id") or "").strip()
        if not voice_id or voice_id in seen or not _usable(item):
            continue
        seen.add(voice_id)
        name = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        traits = read_traits(voice_id=voice_id, name=name, description=description)
        items.append(
            VoiceMeta(
                id=voice_id,
                name=name,
                description=description,
                tags=_tags_of(item.get("tags")),
                gender=traits.gender,
                age=traits.age,
                language=_language_of(item, traits.language),
            )
        )
    return tuple(items)
