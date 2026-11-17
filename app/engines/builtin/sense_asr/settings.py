"""直连一台 SenseVoice-Small 服务的识别配置。

- ``base_url``：那台识别服务的地址，默认 ``http://127.0.0.1:18102``（audio.cpp
  给 ``sense_asr`` 族分配实例的端口）。服务在局域网另一台机器时填那台的地址。
- ``model``：模型 id。audio.cpp 同时列两个：``sense-asr`` 是 streaming 实例，
  ``sense_asr`` 是 offline 实例；后者要 Silero VAD 资产
  （``assets/framework/models/silero_vad``），内置运行包不带这份文件，会上报
  ``no VAD model ... can load``。因此默认用 ``sense-asr``。
- ``details``：用 ``/details`` 端点多拿一个语种。默认开。
- ``language``：语种提示，填空 = 服务端默认 auto。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "SenseAsrSettings"]

#: SenseVoice-Small 实例的默认地址（audio.cpp 的 sense_asr 族）。
DEFAULT_BASE_URL = "http://127.0.0.1:18102"

#: 默认模型 id：streaming 实例。offline 的 ``sense_asr`` 需要额外的 VAD 资产。
DEFAULT_MODEL = "sense-asr"


class SenseAsrSettings(BaseModel):
    """直连一台 SenseVoice-Small 服务：连接与请求参数。"""

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "识别服务地址。默认直连本机的 SenseVoice 实例（端口 18102）；"
            "服务在局域网另一台机器时填那台的地址。指错端口时「测试连接」"
            "会把它实际有的模型列出来。"
        ),
        json_schema_extra=ui_hints("服务地址", placeholder=DEFAULT_BASE_URL),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description=(
            "模型 id，与「服务地址」那台实际加载的一致。audio.cpp 上默认是 "
            "sense-asr（streaming）；sense_asr（offline）需要 Silero VAD 资产。"
        ),
        json_schema_extra=ui_hints("模型", placeholder=DEFAULT_MODEL),
    )
    details: bool = Field(
        default=True,
        description=(
            "用 /details 端点，多拿一个识别到的语种（比普通端点多跑一遍）。"
            "关掉就只取文字。"
        ),
        json_schema_extra=ui_hints("回报语种"),
    )
    language: str = Field(
        default="",
        description=(
            "给模型的语种提示（如 zh / en / ja）。留空 = 服务端默认 auto，"
            "由模型自己判断。"
        ),
        json_schema_extra=ui_hints("语种提示", placeholder="留空自动判断"),
    )
    timeout: float = Field(
        default=180.0,
        ge=5.0,
        le=1800.0,
        description="单次识别的超时（秒）。长音频按需调大。",
        json_schema_extra=ui_hints("超时（秒）", widget="number"),
    )
