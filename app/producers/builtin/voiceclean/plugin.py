"""音色处理插件：工具型工作区，复用宿主左栏，处理的是音色库里的样例音频。

本插件不声明流水线步骤（``steps()`` 为空）：它没有"源文本 → 成品"的链路，宿主也不
校验它的步骤；与 audio.cpp 运行时插件同一形态。

分工：

- **核心**做 ffmpeg：单声道、采样率、高通、限峰、时长校验（:mod:`app.infra.ffmpeg`）；
- **模型**走 audio.cpp 运行实例的 ``POST /v1/tasks/run``（``task`` 为 ``sep`` / ``s2s``）：
  可选模型与地址都从正在运行的实例里取，插件不保存地址，也不在这里下载模型；
  一台都没启动时只做裁剪；
- **落库**走 :class:`app.usecase.library.LibraryService`：原件进回收站，sidecar 记处理链。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.producers.base import ProducerPlugin
from app.producers.builtin.voiceclean.api import build_router
from app.producers.builtin.voiceclean.service import VoiceCleanService
from app.producers.builtin.voiceclean.settings import VoiceCleanSettings

__all__ = ["VoiceClean"]


class VoiceClean(ProducerPlugin):
    key = "voiceclean"
    label = "音色处理"
    description = "对音色样例做分离、降噪与裁剪，逐条试听后替换。"
    version = "0.1.0"
    settings_label = "音色处理"
    homepage = "https://github.com/0xShug0/audio.cpp"
    order = 30
    #: 工作区要左栏，且是"点卡即选中"：处理目标是音色或它的一条样例。
    voice_rail = True
    rail_mode = "pick"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return VoiceCleanSettings

    def service(self, root: Path) -> Any:
        return VoiceCleanService(root, self.current_settings)

    def router(self, service: Any) -> APIRouter:
        return build_router(service)
