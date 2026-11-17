"""启动迁移：把 ``config.json`` 里旧的无前缀密钥换成 DPAPI 密文。

保存路径从这一版起就加密了；但已经存在的明文不会自己变。启动时扫一遍三处插件
配置，只碰"有值且没有前缀"的机密字段。幂等，失败也不拦住服务。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.infra import secrets as secret_store
from app.infra.config import load_config, save_config
from app.infra.loghub import CHANNEL

__all__ = ["migrate"]


def _seal_section(
    section: dict[str, dict[str, Any]], key: str, fields: Iterable[str]
) -> int:
    stored = section.get(key)
    if not isinstance(stored, dict):
        return 0
    changed = 0
    for name in fields:
        value = stored.get(name)
        if not isinstance(value, str) or not value or secret_store.sealed(value):
            continue
        sealed = secret_store.seal(value)
        if sealed != value:
            stored[name] = sealed
            changed += 1
    return changed


def migrate() -> int:
    """返回这次加密了几项；0 = 没有可迁移的（或平台不支持）。"""
    if not secret_store.available():
        return 0

    config = load_config()
    changed = 0

    from app.engines.registry import list_engines

    for key, engine in list_engines().items():
        changed += _seal_section(config.engines, key, engine.secret_fields())

    from app.llm.registry import list_providers

    for key, provider in list_providers().items():
        changed += _seal_section(config.llm, key, provider.secret_fields())

    from app.producers.registry import discover
    from app.usecase.producers import secret_fields

    for plugin in discover():
        schema = plugin.settings_schema()
        if schema is not None:
            changed += _seal_section(config.producers, plugin.key, secret_fields(schema))

    if changed:
        save_config(config)
        CHANNEL.info(f"已把 {changed} 项明文密钥加密为本机密文。", source="config")
    return changed
