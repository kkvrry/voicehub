"""DeepSeek 的配置模型——**界面照这个模型的 JSON Schema 直接渲染表单**。

字段分三类：

1. **凭据**（``api_key``）：标了 ``secret``，读接口一律掩码、日志里不打。
2. **地址**（``base_url``）：只在走中转/代理时改；填到 ``/v1`` 或站点根都行，
   客户端自己补 ``/chat/completions``。
3. **默认值**（模型、温度、上限、超时）：请求里没给时用它们。

``model`` 用**官方模型名**：``deepseek-flash``（V4.1 Flash，认图）与
``deepseek-v4-pro``（V4 Pro，纯文本）。平台的 ``/models`` 会给每个模型的上下文、
输入模态与思考档位，界面照它列；这里只放"选哪个"的默认值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.llm.base import ui_hints

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "MODEL_CHOICES",
    "THINKING_CHOICES",
    "WEB_SEARCH_MODEL",
    "DeepseekSettings",
]

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
#: 服务端联网搜索只由 Flash 提供：选了 Pro 的请求在搜索时改用这一格。
WEB_SEARCH_MODEL = "deepseek-flash"

#: 官方模型名（2026-10 文档：V4.1 Flash / V4 Pro）。
MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("deepseek-flash", "deepseek-flash（V4.1 Flash）"),
    ("deepseek-v4-pro", "deepseek-v4-pro（V4 Pro）"),
)

#: 思考模式。``auto`` = 不发这个参数，跟随平台（现在的缺省是开启、力度 high）。
THINKING_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "跟随平台"),
    ("off", "关闭（温度生效）"),
    ("low", "开启 · 低"),
    ("high", "开启 · 高"),
    ("max", "开启 · 最高"),
)


class DeepseekSettings(BaseModel):

    api_key: str = Field(
        default="",
        description="前往 platform.deepseek.com 的「API keys」创建。",
        json_schema_extra=ui_hints("API Key", widget="password", secret=True),
    )
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="只在走中转/代理时改。站点根或 /v1 都行。",
        json_schema_extra=ui_hints("接口地址"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="Flash 认图、上下文 1M、支持联网搜索；V4 Pro 只读文本，联网搜索会自动改用 Flash。",
        json_schema_extra=ui_hints("默认模型", widget="select", choices=MODEL_CHOICES),
    )
    thinking: Literal["auto", "off", "low", "high", "max"] = Field(
        default="off",
        description="默认关闭：结构化抽取（认人、标情绪）不需要先想一遍。需要时再开。",
        json_schema_extra=ui_hints("思考", widget="select", choices=THINKING_CHOICES),
    )
    temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="0–2。分析类任务 0.2–0.7，创作类 1.0；思考模式下不生效。",
        json_schema_extra=ui_hints("温度", widget="number"),
    )
    max_tokens: int = Field(
        default=8192,
        ge=1,
        description="单次回复上限；被截断时响应里会有一句说明。",
        json_schema_extra=ui_hints("回复上限", widget="number"),
    )
    timeout: float = Field(
        default=120.0,
        ge=5.0,
        description="单次请求的整体超时（秒）。长文本分析给足，别按普通接口的 30 秒算。",
        json_schema_extra=ui_hints("超时", widget="number"),
    )

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())

    @property
    def root(self) -> str:
        """实际要打的站点根（自定义优先，去掉尾部斜杠）。"""
        return ((self.base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")
