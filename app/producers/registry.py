"""生产方插件注册表：扫目录 + entry points，与其它插件域同一套发现规则。

内置插件在 ``app/producers/builtin/<key>/plugin.py``；第三方包在
``pyproject.toml`` 声明 ``[project.entry-points."voicehub.producers"]``，
可以指向类，也可以指向返回实例的工厂。

与其它域的唯一差别是次序：生产方的 ``order`` 决定左栏与工作区的排列，
因此排序键是 ``(order, key)``。注册表不 import 任何具体插件，也不持有业务状态。
"""

from __future__ import annotations

from pathlib import Path

from app.infra.plugin_registry import discover as _discover
from app.producers.base import ProducerPlugin

__all__ = ["discover", "require"]

_BUILTIN = Path(__file__).resolve().parent / "builtin"
_BUILTIN_PKG = "app.producers.builtin"
_ENTRY_POINT_GROUP = "voicehub.producers"


def discover() -> list[ProducerPlugin]:
    """已安装的生产方，按 ``order`` / ``key`` 排序。"""
    found = _discover(
        _ENTRY_POINT_GROUP,
        ProducerPlugin,
        builtin_pkg=_BUILTIN_PKG,
        builtin_root=_BUILTIN,
        builtin_file="plugin.py",
        sort_key=lambda plugin: (plugin.order, plugin.key),
    )
    return list(found.values())


def require(key: str) -> ProducerPlugin:
    for plugin in discover():
        if plugin.key == key:
            return plugin
    raise KeyError(f"没有安装生产方插件「{key}」。")
