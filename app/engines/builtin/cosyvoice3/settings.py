"""CosyVoice3 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

分四组：连接与服务、生成（跨语种 / 采样 / 推理步数 / 种子）、长文本、超时。

**没有语速与音量**：这一家的快慢、语气都由参考音频与指令决定，不是数值旋钮。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "DEFAULT_TOP_K", "CosyVoice3Settings"]

#: audio.cpp 的服务默认端口（与 dots.tts / IndexTTS2 各占一个实例的做法一致）。
DEFAULT_BASE_URL = "http://127.0.0.1:18092"

#: 模型 id——与实例里注册的那一条 ``models[].id`` 一致，默认取家族名。
DEFAULT_MODEL = "cosyvoice3"

#: 官方默认的 AR 采样 top-k。
DEFAULT_TOP_K = 25


class CosyVoice3Settings(BaseModel):
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
    cross_lingual: bool = Field(
        default=False,
        description=(
            "用参考音频的嗓子读另一种语言（cross_lingual 模板）。"
            "关闭时按同语种克隆；有引导指令时以指令为准。"
        ),
        json_schema_extra=ui_hints("跨语种", group="生成", widget="switch"),
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        description="语音 token 的采样上限（官方 25）。调低更稳，调高更活。",
        json_schema_extra=ui_hints("采样 top-k", group="生成", widget="number"),
    )
    num_inference_steps: int = Field(
        default=10,
        ge=1,
        description="声码器前流匹配的欧拉步数（官方 10）。调高保真度上升、耗时同比增加。",
        json_schema_extra=ui_hints("推理步数", group="生成", widget="number"),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（插件抽一个发出去并报回来——引擎省略种子时用定值 1986）。"
            "0 及以上 = 固定，同一段文本可复现。"
        ),
        json_schema_extra=ui_hints("随机种子", group="生成", widget="number"),
    )
    text_chunk_size: int = Field(
        default=0,
        ge=0,
        description="长文本每块最多多少个字符。0 = 用引擎默认（600）。",
        json_schema_extra=ui_hints("分块字数", group="长文本", widget="number"),
    )
    timeout: float = Field(
        default=600.0,
        gt=0,
        description="单次请求超时（秒）。本机推理没有并发优势，长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
