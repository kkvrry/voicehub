"""向量模型插件发现：内置子包 + 第三方 entry points。

契约独立：这里只认 :class:`~app.embedding.base.EmbeddingProvider`，
不会把向量模型混进 TTS 引擎。发现规则与其它插件域共用，
见 :mod:`app.infra.plugin_registry`。
"""

from __future__ import annotations

from app.embedding.base import EmbeddingProvider
from app.infra.plugin_registry import clear_cache, discover

__all__ = [
    "ENTRY_POINT_GROUP",
    "clear_cache",
    "detect_provider",
    "list_providers",
]

ENTRY_POINT_GROUP = "voicehub.embeddings"

_BUILTIN = "app.embedding.builtin"


def list_providers() -> dict[str, EmbeddingProvider]:
    """全部可用向量模型（``key → 实例``），按 key 排序；返回副本。"""
    return discover(ENTRY_POINT_GROUP, EmbeddingProvider, builtin_pkg=_BUILTIN)


def detect_provider(key: str) -> EmbeddingProvider | None:
    return list_providers().get((key or "").strip())
