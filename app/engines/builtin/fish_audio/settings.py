"""Fish Audio 引擎配置——前端照这个模型的 JSON Schema 渲染表单。

字段分四类：

1. **凭据**（``api_key``）：标了 ``secret``，读接口掩码、日志不打。
2. **合成模型**：模型由请求头 ``model`` 指定（不在请求体里），见 :data:`MODEL_CHOICES`。
3. **合成默认值**：请求里没给时用它们。
4. **并发**：平台按**并发数**限流，不按 RPM，见 ``concurrency``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = [
    "HOST",
    "MODEL_CHOICES",
    "VOICE_SCOPE_CHOICES",
    "FishAudioSettings",
]

#: 官方 API 地址。自定义地址只在走中转时用。
HOST = "https://api.fish.audio"

#: ``model`` 请求头的候选（官方枚举）。
#:
#: ``s1`` 不在候选里：官方已宣布 2026-12-31 停用，届时按 ``s2.1-pro`` 计费与呈现；
#: 且 s1 的标签写法是圆括号、S2 是方括号，两套写法不能共用一份映射表。
MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("s2.1-pro", "s2.1-pro（当前代）"),
    ("s2.1-pro-free", "s2.1-pro-free（免费档，无时延保证）"),
    ("s2-pro", "s2-pro（上一代）"),
)

VOICE_SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("self", "我的音色"),
    ("public", "公开音色库"),
)

_YES_NO: tuple[tuple[bool, str], ...] = ((True, "是"), (False, "否"))


class FishAudioSettings(BaseModel):

    api_key: str = Field(
        default="",
        description="在 fish.audio 控制台的 API Keys 页创建。",
        json_schema_extra=ui_hints("API Key", group="连接与密钥", widget="password", secret=True),
    )
    base_url: str = Field(
        default="",
        description="只在走中转/代理时改。留空即官方地址。",
        json_schema_extra=ui_hints("自定义地址", group="连接与密钥"),
    )
    model: str = Field(
        default="s2.1-pro",
        description="模型由请求头指定，不写进请求体。s1 已停用，不在候选里。",
        json_schema_extra=ui_hints(
            "合成模型", group="合成模型", widget="select", choices=MODEL_CHOICES
        ),
    )
    voice_scope: Literal["self", "public"] = Field(
        default="self",
        description="「刷新」时拉取范围。公开音色库按平台热度排序，只取前 50 条。",
        json_schema_extra=ui_hints(
            "拉取范围", group="合成模型", widget="select", choices=VOICE_SCOPE_CHOICES
        ),
    )
    latency: Literal["normal", "balanced"] = Field(
        default="normal",
        description="normal 音质优先；balanced 首包更快。",
        json_schema_extra=ui_hints(
            "时延档位",
            group="合成模型",
            widget="select",
            choices=(("normal", "normal · 音质优先"), ("balanced", "balanced · 首包更快")),
        ),
    )
    chunk_length: int = Field(
        default=200,
        ge=100,
        le=300,
        description="每段生成的字符数。长文本按此切段；数值越小首包越快。",
        json_schema_extra=ui_hints("分段长度", group="合成模型", widget="number"),
    )
    normalize: bool = Field(
        default=True,
        description="文本归一化：数字、日期按读法展开。",
        json_schema_extra=ui_hints("文本归一化", group="合成模型", widget="select", choices=_YES_NO),
    )
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="取值越高越随机，越低越稳定。",
        json_schema_extra=ui_hints("温度", group="合成模型", widget="number"),
    )
    top_p: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="核采样阈值。",
        json_schema_extra=ui_hints("核采样", group="合成模型", widget="number"),
    )
    audio_format: Literal["mp3", "wav", "pcm", "opus"] = Field(
        default="mp3",
        json_schema_extra=ui_hints(
            "默认格式",
            group="输出",
            widget="select",
            choices=(("mp3", "mp3"), ("wav", "wav"), ("pcm", "pcm"), ("opus", "opus")),
        ),
    )
    sample_rate: int = Field(
        default=44100,
        description="mp3 与 wav 常用 44100；opus 固定 48000。",
        json_schema_extra=ui_hints("采样率", group="输出", widget="number"),
    )
    timeout: float = Field(
        default=120.0,
        gt=0,
        description="单次请求超时（秒）。长文本按字数上调。",
        json_schema_extra=ui_hints("超时", group="连接与密钥", widget="number"),
    )
    concurrency: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "同时在跑的请求数。平台按累计消费分档：100 美元以下 5、以上 15、1000 美元以上 50。"
        ),
        json_schema_extra=ui_hints("合成并发", group="并发", widget="number"),
    )

    @property
    def host(self) -> str:
        """这一份配置实际要打的主机（自定义优先）。"""
        return (self.base_url or "").strip().rstrip("/") or HOST

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())
