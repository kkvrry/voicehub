"""Kitta Audio 引擎配置——前端照这个模型的 JSON Schema 渲染表单。

四类字段：

1. **凭据**（``api_key``）：标了 ``secret``，读接口掩码、日志不打。
2. **路由**（``model_id``）：平台在一批供应商模型之间路由；留空即按音色所属引擎取默认。
3. **合成默认值**：请求里没给时用它们。
4. **并发与限流**：平台回 429 时给 ``Retry-After``，未公布 RPM。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = [
    "HOST",
    "TTS_BASE_PATH",
    "VOICE_SCOPE_CHOICES",
    "KittaSettings",
]

#: 官方 API 主机。v3 是合成与音色清单的路径前缀，v1 是克隆的路径前缀。
HOST = "https://fishaudio.org"
TTS_BASE_PATH = "/api/open/v3"

VOICE_SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "公开音色 + 我的音色"),
    ("public", "仅公开音色"),
    ("personal", "仅我的音色"),
)


class KittaSettings(BaseModel):

    api_key: str = Field(
        default="",
        description="在 kittaaudio.com 的开发者页面创建，完整 Key 只展示一次。",
        json_schema_extra=ui_hints("API Key", group="连接与密钥", widget="password", secret=True),
    )
    base_url: str = Field(
        default="",
        description="只在走中转/代理时改。留空即官方地址。",
        json_schema_extra=ui_hints("自定义地址", group="连接与密钥"),
    )
    model_id: str = Field(
        default="",
        description=(
            "留空即按音色所属引擎取默认模型。可用取值见 "
            "GET /api/open/v3/speech/tts/capabilities 的 models[].id。"
        ),
        json_schema_extra=ui_hints(
            "模型 ID", group="合成模型", placeholder="留空 = 由音色决定"
        ),
    )
    voice_scope: Literal["all", "public", "personal"] = Field(
        default="all",
        description="「刷新」时拉取范围。只取第一页 100 条。",
        json_schema_extra=ui_hints(
            "拉取范围", group="合成模型", widget="select", choices=VOICE_SCOPE_CHOICES
        ),
    )
    text_normalization: bool = Field(
        default=True,
        description="结构化文本读音归一化：数字、日期按读法展开。",
        json_schema_extra=ui_hints(
            "文本归一化", group="合成模型", widget="select", choices=((True, "是"), (False, "否"))
        ),
    )
    audio_format: Literal["mp3", "wav", "ogg"] = Field(
        default="mp3",
        json_schema_extra=ui_hints(
            "默认格式",
            group="输出",
            widget="select",
            choices=(("mp3", "mp3"), ("wav", "wav"), ("ogg", "ogg")),
        ),
    )
    timeout: float = Field(
        default=120.0,
        gt=0,
        description="单次请求超时（秒）。长文本按字数上调。",
        json_schema_extra=ui_hints("超时", group="连接与密钥", widget="number"),
    )
    concurrency: int = Field(
        default=4,
        ge=1,
        le=20,
        description="同时在跑的请求数。平台未公布并发额度，按自己的额度填。",
        json_schema_extra=ui_hints("合成并发", group="并发", widget="number"),
    )
    rpm: int = Field(
        default=0,
        ge=0,
        le=600,
        description="每分钟最多发出的请求数。平台未公布配额，填 0 不限；被限流时按 429 退避。",
        json_schema_extra=ui_hints("每分钟请求数", group="并发", widget="number"),
    )

    @property
    def host(self) -> str:
        """这一份配置实际要打的主机（自定义优先）。"""
        return (self.base_url or "").strip().rstrip("/") or HOST

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())
