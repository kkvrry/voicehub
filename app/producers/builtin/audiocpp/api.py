"""audio.cpp 运行时接口：挂在 ``/api/v1/producers/audiocpp``。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.producers.base import ProducerError
from app.producers.builtin.audiocpp.runtime import RuntimeService

__all__ = ["build_router"]


class DownloadIn(BaseModel):
    runtime_id: str = ""


class PortIn(BaseModel):
    port: int | None = None


class PortEditIn(BaseModel):
    port: int
    value: int


class InstanceIn(BaseModel):
    port: int
    model: str
    label: str = ""
    family: str = ""


class SettingsIn(BaseModel):
    auto_start: list[str] | None = None
    backend: str | None = None
    runtime_id: str | None = None
    mirror: str | None = None
    exe: str | None = None
    models_root: str | None = None
    instances: list[InstanceIn] | None = None


class EndpointIn(BaseModel):
    url: str = ""


class PackageIn(BaseModel):
    id: str


class ModelIn(BaseModel):
    id: str


class UninstallIn(BaseModel):
    confirm: bool = False


def build_router(service: RuntimeService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/state", summary="服务、实例与模型状态")
    def state() -> dict[str, Any]:
        return service.state()

    @router.post("/endpoint", summary="填写或清除服务地址")
    def endpoint(body: EndpointIn) -> dict[str, Any]:
        service.settings(endpoint=body.url)
        return service.state()

    @router.post("/settings", summary="保存选项")
    def settings(body: SettingsIn) -> dict[str, Any]:
        rows = [row.model_dump() for row in body.instances] if body.instances is not None else None
        service.settings(
            mirror=body.mirror,
            backend=body.backend,
            runtime_id=body.runtime_id,
            exe=body.exe,
            models_root=body.models_root,
            instances=rows,
            auto_start=body.auto_start,
        )
        return service.state()

    @router.post("/download", summary="下载并解压运行包")
    def download(body: DownloadIn) -> dict[str, Any]:
        service.start_download(runtime_id=body.runtime_id)
        return service.state()

    @router.post("/start", summary="启动实例")
    def start(body: PortIn) -> dict[str, Any]:
        service.start(body.port)
        return service.state()

    @router.post("/port", summary="改实例端口（引擎带来的行会写回引擎地址）")
    def set_port(body: PortEditIn) -> dict[str, Any]:
        service.set_port(body.port, body.value)
        return service.state()

    @router.post("/stop", summary="停止实例")
    def stop(body: PortIn) -> dict[str, Any]:
        service.stop(body.port)
        return service.state()

    @router.post("/models/install", summary="安装模型包")
    def install(body: PackageIn) -> dict[str, Any]:
        service.install_model(body.id)
        return service.state()

    @router.post("/models/stop", summary="停止模型下载")
    def stop_install(body: PackageIn) -> dict[str, Any]:
        service.stop_model(body.id)
        return service.state()

    @router.post("/models/delete", summary="删除已安装的模型")
    def delete(body: PackageIn) -> dict[str, Any]:
        service.delete_model(body.id)
        return service.state()

    @router.post("/models/clean", summary="清理未完成的下载")
    def clean(body: PackageIn) -> dict[str, Any]:
        service.clean_model(body.id)
        return service.state()

    @router.post("/models/load", summary="加载已安装的模型")
    def load(body: ModelIn) -> dict[str, Any]:
        service.load_model(body.id)
        return service.state()

    @router.post("/models/unload", summary="卸载运行中的模型")
    def unload(body: ModelIn) -> dict[str, Any]:
        service.unload_model(body.id)
        return service.state()

    @router.post("/catalog/refresh", summary="从上游刷新模型清单")
    def refresh() -> dict[str, Any]:
        service.refresh_catalog()
        return service.state()

    @router.post("/uninstall", summary="卸载运行包")
    def uninstall(body: UninstallIn) -> dict[str, Any]:
        if not body.confirm:
            raise ProducerError("需要确认。")
        service.uninstall(True)
        return service.state()

    return router
