"""音色处理配置：只有一个模型服务地址。

模型清单、已装与否都问那台服务，本插件不保存第二份；换服务地址即换全部模型。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.uischema import ui_hints

__all__ = ["GROUP_SERVICE", "VoiceCleanSettings"]

GROUP_SERVICE = "模型服务"


class VoiceCleanSettings(BaseModel):
    host: str = Field(
        default="http://127.0.0.1:8080",
        description="本机 audio.cpp 服务地址；模型安装与推理都发往这里。",
        json_schema_extra=ui_hints("服务地址", group=GROUP_SERVICE),
    )
    timeout: int = Field(
        default=600,
        ge=10,
        le=7200,
        description="单条音频的模型处理超时（秒）。长段与降噪模型都较慢。",
        json_schema_extra=ui_hints("处理超时（秒）", widget="number", group=GROUP_SERVICE),
    )
