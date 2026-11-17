"""对话模型注册表——一家模型 = 一个目录，发现它不需要改这里。

内置扫 ``app/llm/builtin/<key>/`` 子包；第三方走 ``voicehub.llm`` entry points。
发现规则与其它插件域共用，见 :mod:`app.infra.plugin_registry`。
"""

from __future__ import annotations

from app.infra.plugin_registry import clear_cache, discover
from app.llm.base import LlmProvider

__all__ = ["ENTRY_POINT_GROUP", "clear_cache", "detect_provider", "list_providers"]

#: 第三方模型包在自己的 ``pyproject.toml`` 里声明的组名。
ENTRY_POINT_GROUP = "voicehub.llm"

_BUILTIN = "app.llm.builtin"


def list_providers() -> dict[str, LlmProvider]:
    """全部可用模型（``key`` → 实例），按 key 排序。返回副本。"""
    return discover(ENTRY_POINT_GROUP, LlmProvider, builtin_pkg=_BUILTIN)


def detect_provider(key: str) -> LlmProvider | None:
    """按 key 找一家；找不到返回 ``None``（由调用方给出说明，不在这里抛）。"""
    return list_providers().get((key or "").strip())
