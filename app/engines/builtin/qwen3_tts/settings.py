"""Qwen3-TTS 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

分三组：服务地址与模型、采样、长文本与超时。

**默认值照官方推荐**：温度 0.9、top_k 50、top_p 1.0、重复惩罚 1.05。这些是
audio.cpp 的 TTS 通用选项，进请求体的 ``options``；与别家一样，上游显式给了
才覆盖，没给才用这里的一格。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "Qwen3TtsSettings"]

#: audio.cpp 的服务默认端口。一台实例可挂多个模型。
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: 模型 id——与实例里注册的那一条 ``models[].id`` 一致，默认取家族名。
DEFAULT_MODEL = "qwen3_tts"


class Qwen3TtsSettings(BaseModel):
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "提供 /v1/audio/speech 的 audio.cpp 服务地址；一台实例可挂多个模型。"
            "模型 id 以 GET /v1/models 里那条为准。"
        ),
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="发给服务的 model 字段，需与该实例注册的 models[].id 一致。",
        json_schema_extra=ui_hints("模型", group="连接与服务"),
    )
    temperature: float = Field(
        default=0.9,
        gt=0,
        description="主说话器（talker）的采样温度。调低更稳、调高更活，过高会走音。",
        json_schema_extra=ui_hints("采样温度", group="采样", widget="number"),
    )
    top_k: int = Field(
        default=50,
        ge=0,
        description="采样候选数；0 = 不截断。",
        json_schema_extra=ui_hints("Top-K", group="采样", widget="number"),
    )
    top_p: float = Field(
        default=1.0,
        gt=0,
        le=1.0,
        description="累积概率截断；1.0 = 不截断。",
        json_schema_extra=ui_hints("Top-P", group="采样", widget="number"),
    )
    repetition_penalty: float = Field(
        default=1.05,
        gt=0,
        description="重复惩罚。长句反复、尾音粘连时调高；过高会让语流发紧。",
        json_schema_extra=ui_hints("重复惩罚", group="采样", widget="number"),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（插件抽一个发出去并报回来）；0 及以上 = 固定，"
            "同一段文本可复现。"
        ),
        json_schema_extra=ui_hints("随机种子", group="采样", widget="number"),
    )
    max_tokens: int = Field(
        default=0,
        ge=0,
        description="每块最多生成多少个语音 token。0 = 用引擎默认（权重里的 2048）。",
        json_schema_extra=ui_hints("生成长度上限", group="长文本与超时", widget="number"),
    )
    text_chunk_size: int = Field(
        default=0,
        ge=0,
        description="长文本每块最多多少个字符。0 = 用引擎默认（8192）。",
        json_schema_extra=ui_hints("分块字数", group="长文本与超时", widget="number"),
    )
    timeout: float = Field(
        default=600.0,
        gt=0,
        description="单次请求超时（秒）。1.7B 模型长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="长文本与超时", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
