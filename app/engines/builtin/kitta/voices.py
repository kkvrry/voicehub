"""``GET /v3/voices`` 的响应解析。

官方响应体（``docs.kittaaudio.com/zh/docs/api-reference/voices/list``）：

.. code-block:: json

    {
      "total": 1, "page": 1, "pageSize": 20, "totalPages": 1,
      "items": [
        {"voiceId": "voice_123", "title": "Clara", "description": "",
         "isPersonal": false, "primaryLanguage": "zh", "languages": ["zh"]}
      ]
    }

两处要注意：

- 音色名在 v1 是 ``title``、在 v3 的示例里是 ``name``，两种都认——认错的表现是
  界面上显示成一串 id。
- 语言优先读平台自己的 ``primaryLanguage`` / ``languages``（明确声明），平台没给才退回
  :func:`app.engines.traits.read_traits` 从名称与说明里认。性别与年龄感没有平台字段，
  一律由 traits 认，认不出留空。
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
    """``primaryLanguage`` → ``languages`` 里第一个词表认得的代码 → traits 的结果。"""
    primary = str(item.get("primaryLanguage") or "").strip().lower()
    if primary in LANGUAGES:
        return primary
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


def parse_voice_list(payload: Any) -> tuple[VoiceMeta, ...]:
    """响应体 → 音色清单。``voiceId`` 是发给平台的值，名称只给界面。"""
    items: list[VoiceMeta] = []
    seen: set[str] = set()
    for item in voice_items(payload):
        voice_id = str(item.get("voiceId") or "").strip()
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        name = str(item.get("title") or item.get("name") or "").strip()
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
