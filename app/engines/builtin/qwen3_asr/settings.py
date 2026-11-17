"""直连一台 Qwen3-ASR 服务的识别配置。

- ``base_url``：那台识别服务的地址，默认 ``http://127.0.0.1:18999``。服务在
  局域网另一台机器时填那台的地址；引擎是单模型客户端，不连 hub 发现模型。
- ``model``：模型 id，与服务 ``GET /v1/models`` 列出的那一项一致。
- ``details``：用 ``/details`` 端点多拿一个语种。代价是多跑一遍（同一个片段
  0.4s → 0.8s）。默认开——补逐字稿时语种有用，而样例补一次就写回库存。

**没有「发送方式」**：引擎总是把音频以 multipart 上传。服务端路径只有服务与
本机共享文件系统时才成立，而上传在本地、局域网、云端都成立；这件事不该让用户
在两种失败模式之间选。非 WAV 由客户端先转成 16 kHz 单声道 WAV 再传。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "Qwen3AsrSettings"]

#: Qwen3-ASR 服务的默认地址。
DEFAULT_BASE_URL = "http://127.0.0.1:18999"

#: 默认模型 id（``GET /v1/models`` 的原话是 ``qwen3_asr``）。
DEFAULT_MODEL = "qwen3_asr"


class Qwen3AsrSettings(BaseModel):
    """直连一台 Qwen3-ASR 服务：连接与请求参数。"""

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "识别服务地址。默认直连本机的 Qwen3-ASR 实例（端口 18999）；"
            "服务在局域网另一台机器时填那台的地址。指错端口时「测试连接」"
            "会把它实际有的模型列出来。"
        ),
        json_schema_extra=ui_hints("服务地址", placeholder=DEFAULT_BASE_URL),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description=(
            "模型 id，与「服务地址」那台实际加载的一致（它自己的 GET /v1/models）。"
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
            "给模型的语种提示（如 zh / en / ja）。留空 = 让它自己判断——"
            "多语种书里这才是对的做法。"
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
