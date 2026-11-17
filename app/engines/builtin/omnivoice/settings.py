"""OmniVoice 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

分四组：连接与服务、生成（CFG / 推理步数 / 种子）、长文本、超时。

**没有语速格**：语速是逐条请求的参数（合成参数里的「语速」直接落 ``options.speed``），
引擎设置里再放一份只会让两处打架。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_GUIDANCE_SCALE", "DEFAULT_MODEL", "OmniVoiceSettings"]

#: audio.cpp 的服务默认端口（与其它几台本机引擎各占一个实例的做法一致）。
DEFAULT_BASE_URL = "http://127.0.0.1:18093"

#: 模型 id——与实例里注册的那一条 ``models[].id`` 一致，默认取家族名。
DEFAULT_MODEL = "omnivoice"

#: 官方默认的 CFG 强度。
DEFAULT_GUIDANCE_SCALE = 2.0


class OmniVoiceSettings(BaseModel):
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "提供 /v1/audio/speech 的 audio.cpp 服务地址；模型 id 以 GET /v1/models 为准。"
        ),
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="发给服务的 model 字段，需与该实例注册的 models[].id 一致。",
        json_schema_extra=ui_hints("模型", group="连接与服务"),
    )
    guidance_scale: float = Field(
        default=DEFAULT_GUIDANCE_SCALE,
        ge=0.0,
        description="分类器无关引导强度（官方 2.0）。调高更贴参考与描述，过高会失真。",
        json_schema_extra=ui_hints("引导强度", group="生成", widget="number"),
    )
    num_inference_steps: int = Field(
        default=32,
        ge=1,
        description="扩散推理步数（官方 32）。调高保真度上升、耗时同比增加。",
        json_schema_extra=ui_hints("推理步数", group="生成", widget="number"),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（不发送这一格，沿用会话的随机状态）。"
            "0 及以上 = 固定，同一段文本可复现。"
        ),
        json_schema_extra=ui_hints("随机种子", group="生成", widget="number"),
    )
    text_chunk_size: int = Field(
        default=0,
        ge=0,
        description="长文本每块最多多少个字符。0 = 按预估时长自动分块。",
        json_schema_extra=ui_hints("分块字数", group="长文本", widget="number"),
    )
    timeout: float = Field(
        default=900.0,
        gt=0,
        description="单次请求超时（秒）。扩散解码较慢，长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
