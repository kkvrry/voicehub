"""DotTTS 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

只有四格：服务地址、模型 id、种子、超时。**合成参数不在这里配**：它们跟着
请求走（见 :mod:`plugin` 的映射表）——这一家的 ``template_name`` /
``speaker_scale`` 等引擎特有参数走 ``extra_schema``，由前端照它渲染。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "DotsTtsSettings"]

#: 默认服务地址——**与 breeze_tts 同一台**本机 audio.cpp 推理服务
#:（这一台机器上 BreezeTTS 2 就配在 18090；hub 起的实例端口从 18090 起）。
DEFAULT_BASE_URL = "http://127.0.0.1:18090"

#: 模型 id——必须与 ``GET /v1/models`` 里的 ``id`` 一致
#:（在 audio.cpp 里按 ``dots_tts`` 家族加载 GGUF，id 由那边的配置定）。
DEFAULT_MODEL = "dots_tts"


class DotsTtsSettings(BaseModel):
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="本机 audio.cpp 推理服务的地址（与 BreezeTTS 2 同一台；模型列表见 /v1/models）。",
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="模型 id，必须与 /v1/models 里那一条的 id 一致。",
        json_schema_extra=ui_hints("模型", group="连接与服务"),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（插件抽一个无符号数发出去——引擎不收负数，"
            "且省略 seed 时引擎用定值 42，所以随机必须显式抽）。"
            "0 及以上 = 固定：同一个数、同一段文本，出来的音频可复现。"
        ),
        json_schema_extra=ui_hints("随机种子", group="生成", widget="number"),
    )
    timeout: float = Field(
        default=600.0,
        ge=30.0,
        description="单次请求超时（秒）。本机推理没有并发优势，长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
