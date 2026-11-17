"""audio.cpp 运行时插件：工具型工作区，不声明流水线步骤、不写宿主配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.producers.base import ProducerPlugin
from app.producers.builtin.audiocpp.api import build_router
from app.producers.builtin.audiocpp.runtime import RuntimeService

__all__ = ["AudiocppRuntime"]


class AudiocppRuntime(ProducerPlugin):
    key = "audiocpp"
    label = "audio.cpp"
    description = "接入本机 audio.cpp 服务，或托管运行包并拉起实例；模型安装走官方管理接口。"
    version = "0.2.0"
    settings_label = "audio.cpp 运行时"
    homepage = "https://github.com/0xShug0/audio.cpp"
    order = 80
    #: 运行时管理页，不是"给角色挑音色"的工作区：不要宿主那条音色栏。
    voice_rail = False
    #: 配置里能指定 ``exe``（本机任意可执行文件）：这个保存只能在本机发起。
    settings_local_only = True

    def service(self, root: Path) -> Any:
        return RuntimeService(root)

    def router(self, service: Any) -> APIRouter:
        return build_router(service)
