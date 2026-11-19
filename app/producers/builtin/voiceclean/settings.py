"""音色处理配置：只有处理超时。

服务地址不在这里存：模型与地址都从 audio.cpp 插件的运行实例里取，运行着几台就
用几台；哪台都没有时，页面仍可只做裁剪，不要求先填一个地址。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.uischema import ui_hints

__all__ = ["GROUP_SERVICE", "VoiceCleanSettings"]

GROUP_SERVICE = "处理"


class VoiceCleanSettings(BaseModel):
    timeout: int = Field(
        default=600,
        ge=10,
        le=7200,
        description="单条音频的模型处理超时（秒）。长段与降噪模型都较慢。",
        json_schema_extra=ui_hints("处理超时（秒）", widget="number", group=GROUP_SERVICE),
    )
