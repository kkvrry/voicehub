"""实时对话生产方：工具型工作区，没有流水线步骤。

对话、合成与识别都复用宿主已有的插件域；本插件只提供角色、背景与聊天记录，
以及它们自己的路由。左栏音色卡的「创建」用这条音色新建一个角色。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.producers.base import ProducerPlugin
from app.producers.builtin.realtime_chat.api import build_router
from app.producers.builtin.realtime_chat.service import RealtimeChatService

__all__ = ["RealtimeChatProducer"]


class RealtimeChatProducer(ProducerPlugin):
    key = "realtime_chat"
    label = "角色对话"
    description = "和角色聊天：录音或打字，按当前模型回复并合成语音。"
    version = "0.1.0"
    settings_label = "角色对话"
    #: 日常页：和创作、配音并列；它不是运行时管理工具。
    order = 40
    nav_section = "main"
    #: 要左栏那条音色栏：卡右「创建」用这条音色新建一个角色（不是指定给已有角色）。
    voice_rail = True
    rail_mode = "create"

    def service(self, root: Path) -> Any:
        return RealtimeChatService(root)

    def router(self, service: Any) -> APIRouter:
        return build_router(service)
