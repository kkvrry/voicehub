"""生产方插件用例：发现、配置存取、服务实例。

配置与引擎/对话模型同口径：存服务端 ``config.json``（机密字段 DPAPI 加密后落盘），
机密字段读时为空串，提交空串表示不修改，``_clear`` 才真正清除。
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel, ValidationError

from app.infra import secrets as secret_store
from app.infra.config import data_dir, load_config, save_config
from app.infra.shell import open_folder
from app.producers.base import ProducerError, ProducerPlugin
from app.producers.registry import discover, require

__all__ = [
    "find",
    "is_enabled",
    "list_payload",
    "open_field",
    "reset_runtimes",
    "runtime_for",
    "save_settings",
    "secret_fields",
    "service_for",
    "set_enabled",
    "settings_for",
    "settings_payload",
]


def find(key: str) -> ProducerPlugin:
    try:
        return require(key)
    except KeyError as exc:
        raise ProducerError(str(exc)) from exc


def service_for(plugin: ProducerPlugin) -> Any:
    """建插件服务实例。构造成本只有一次建表，按需新建即可避免数据目录串用。"""
    root = data_dir() / "producers" / plugin.key
    root.mkdir(parents=True, exist_ok=True)
    return plugin.service(root)


def is_enabled(key: str) -> bool:
    """插件是否启用。停用是服务端状态：重启后仍然不启动。"""
    return key not in load_config().producers_disabled


class ProducerRuntime:
    """插件服务的延迟代理：停用的插件在启用前不构造。

    audio.cpp 的自动启动就发生在服务构造函数里，先构造再拦等于没停。
    路由照常挂着（运行期开关不必重挂），走停用插件的接口一律回一句话。
    """

    def __init__(self, plugin: ProducerPlugin) -> None:
        self.plugin = plugin
        self._service: Any = None

    @property
    def started(self) -> bool:
        """服务实例是否已经构造（诊断与测试用；正常流程不需要）。"""
        return self._service is not None

    def get(self) -> Any:
        if not is_enabled(self.plugin.key):
            raise ProducerError(
                f"插件「{self.plugin.label}」已停用：到「设置 → 插件」里重新启用。"
            )
        with _RUN_LOCK:
            if self._service is None:
                self._service = service_for(self.plugin)
            return self._service

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)

    def shutdown(self) -> None:
        """停用：丢掉实例。有 stop_all / shutdown / close 的由它自己收尾进程与线程。"""
        with _RUN_LOCK:
            service, self._service = self._service, None
        if service is None:
            return
        for hook in ("stop_all", "shutdown", "close"):
            stop = getattr(service, hook, None)
            if callable(stop):
                stop()
                return


_RUNTIMES: dict[str, ProducerRuntime] = {}
_RUN_LOCK = threading.RLock()


def reset_runtimes() -> None:
    """丢掉全部实例：宿主每次建 app 都该从头来。

    生产里 ``create_app()`` 只跑一次；测试会反复建 app、每次换数据目录，
    缓存的旧实例会把上一个用例的项目带进来。停掉再清，进程与线程一起收。
    """
    with _RUN_LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    for runtime in runtimes:
        runtime.shutdown()


def runtime_for(plugin: ProducerPlugin) -> ProducerRuntime:
    """一个插件一个代理；启动挂路由与设置页开关共用同一个实例。"""
    with _RUN_LOCK:
        return _RUNTIMES.setdefault(plugin.key, ProducerRuntime(plugin))


def set_enabled(key: str, enabled: bool) -> dict[str, Any]:
    """启用 / 停用插件：先落盘，再起服务或把它收掉。

    停用后重启也不会自动起来；启用时立刻构造（audio.cpp 若开着自动启动，
    这一刻就是"启动"）。构造失败就回滚这次启用，不留下"已启用但没服务"的假象。
    """
    plugin = find(key)
    runtime = runtime_for(plugin)
    config = load_config()
    config.producers_disabled = [
        item for item in config.producers_disabled if item != key
    ] + ([] if enabled else [key])
    save_config(config)
    if enabled:
        try:
            runtime.get()
        except Exception:
            config = load_config()
            config.producers_disabled = [
                item for item in config.producers_disabled if item != key
            ]
            save_config(config)
            raise
    else:
        runtime.shutdown()
    return settings_payload(key)


def _schema_of(plugin: ProducerPlugin) -> type[BaseModel] | None:
    return plugin.settings_schema()


def settings_for(plugin: ProducerPlugin) -> BaseModel | None:
    """读配置；坏值回落默认（配置可手改，不能拦住工作区）。"""
    schema = _schema_of(plugin)
    if schema is None:
        return None
    raw = secret_store.reveal_fields(
        load_config().producers.get(plugin.key, {}), secret_fields(schema)
    )
    try:
        return schema(**raw)
    except ValidationError:
        return schema()


def secret_fields(schema: type[BaseModel]) -> set[str]:
    return {
        name
        for name, field in schema.model_fields.items()
        if (field.json_schema_extra or {}).get("secret")
    }


def _mask(model: BaseModel) -> dict[str, Any]:
    data = model.model_dump()
    for name in secret_fields(type(model)):
        if name in data:
            data[name] = ""
    return data


def _secrets_set(model: BaseModel) -> list[str]:
    data = model.model_dump()
    return sorted(name for name in secret_fields(type(model)) if str(data.get(name) or ""))


def open_field(key: str, field: str) -> dict[str, Any]:
    """在资源管理器里打开某个配置字段指向的目录（服务在本机运行才做得到）。"""
    plugin = find(key)
    try:
        path = plugin.open_settings_field(field)
    except KeyError as exc:
        raise ProducerError("这个字段没有可打开的目录。") from exc
    if not path.exists():
        raise ProducerError("目录还不存在——先保存一次设置或生成一次。")
    try:
        open_folder(path)
    except OSError as exc:
        raise ProducerError(f"打不开这个目录：{exc}") from exc
    return {"object": "producer_field_open", "producer": key, "field": field, "path": str(path)}


def _payload_of(plugin: ProducerPlugin) -> dict[str, Any]:
    model = settings_for(plugin)
    note = plugin.readiness(model)
    payload: dict[str, Any] = {
        **plugin.describe(),
        "enabled": is_enabled(plugin.key),
        "ready": not note,
        "note": note,
    }
    if model is not None:
        payload["values"] = _mask(model)
        payload["secrets_set"] = _secrets_set(model)
    else:
        payload["values"] = {}
        payload["secrets_set"] = []
    return payload


def settings_payload(key: str) -> dict[str, Any]:
    return _payload_of(find(key))


def list_payload() -> list[dict[str, Any]]:
    """一次发现供整张列表用：逐个 ``find`` 会把插件目录重扫 N 遍。"""
    return [_payload_of(plugin) for plugin in discover()]


def save_settings(key: str, body: dict[str, Any]) -> dict[str, Any]:
    plugin = find(key)
    schema = _schema_of(plugin)
    if schema is None:
        return settings_payload(key)

    current = settings_for(plugin)
    assert isinstance(current, BaseModel)
    data = current.model_dump()
    secrets = secret_fields(schema)

    for name in schema.model_fields:
        if name not in body:
            continue
        value = body[name]
        if name in secrets and str(value or "") == "":
            continue
        data[name] = value

    for name in body.get("_clear") or []:
        if name in secrets:
            data[name] = ""

    if body.get("_reset"):
        defaults = schema()
        keep = {name: data.get(name, "") for name in secrets}
        data = defaults.model_dump()
        data.update({name: value for name, value in keep.items() if value})

    try:
        model = schema(**data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "配置"
        raise ProducerError(f"{field}：{first.get('msg', '取值不合法')}") from exc

    config = load_config()
    config.producers[plugin.key] = secret_store.seal_fields(model.model_dump(), secrets)
    save_config(config)
    return settings_payload(key)
