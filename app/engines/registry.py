"""插件注册表——**一个插件 = 一个目录**，发现它不需要改这里。

两类插件扫描同一个内置包、各取一遍，但契约与 entry point 组都不同：

- 合成引擎（:class:`~app.engines.base.EnginePlugin`）：``voicehub.engines``；
- 识别引擎（:class:`~app.engines.asr_base.AsrEngine`）：``voicehub.asr``。

分两个组：一个包装了合成路线不必顺带声明识别。发现规则本身与其它插件域共用，
见 :mod:`app.infra.plugin_registry`。
"""

from __future__ import annotations

from app.engines.asr_base import AsrEngine
from app.engines.base import EnginePlugin
from app.infra.plugin_registry import clear_cache, discover

__all__ = [
    "ASR_ENTRY_POINT_GROUP",
    "ENTRY_POINT_GROUP",
    "clear_cache",
    "detect_asr_engine",
    "detect_engine",
    "list_asr_engines",
    "list_engines",
]

#: 第三方合成引擎包在自己的 ``pyproject.toml`` 里声明的组名。
ENTRY_POINT_GROUP = "voicehub.engines"
#: 第三方识别引擎的组名。与上面分开：一个包装了合成路线不必顺带声明识别。
ASR_ENTRY_POINT_GROUP = "voicehub.asr"

_BUILTIN = "app.engines.builtin"


def list_engines() -> dict[str, EnginePlugin]:
    """全部可用合成引擎（``key`` → 实例），按 key 排序。返回副本。"""
    return discover(ENTRY_POINT_GROUP, EnginePlugin, builtin_pkg=_BUILTIN)


def detect_engine(key: str) -> EnginePlugin | None:
    """按 key 找一台合成引擎；找不到返回 ``None``（由调用方给出说明，不在这里抛）。"""
    return list_engines().get((key or "").strip())


def list_asr_engines() -> dict[str, AsrEngine]:
    """全部可用识别引擎（``key`` → 实例），按 key 排序。返回副本。"""
    return discover(ASR_ENTRY_POINT_GROUP, AsrEngine, builtin_pkg=_BUILTIN)


def detect_asr_engine(key: str) -> AsrEngine | None:
    """按 key 找一台识别引擎；找不到返回 ``None``。"""
    return list_asr_engines().get((key or "").strip())
