"""VoxCPM2 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

分四组：服务地址与模型、生成（引导强度 / 推理步数 / 种子）、长文本（分块字数）、超时。

**合成参数里没有"语速"与"音量"**：这一家的快慢、情绪、方言都靠正文前那句括号描述
表达，不是数值旋钮（见 :mod:`plugin`）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_GUIDANCE_SCALE", "DEFAULT_MODEL", "VoxCpm2Settings"]

#: audio.cpp 的服务默认端口。一台实例可挂多个模型，与别的引擎共用也可以。
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: 模型 id——与实例里注册的那一条 ``models[].id`` 一致，默认取家族名（管理页注册时就是这个）。
DEFAULT_MODEL = "voxcpm2"

#: 官方默认引导强度（1–5 的刻度，约 2 更像"照着描述念"）。
DEFAULT_GUIDANCE_SCALE = 2.0


class VoxCpm2Settings(BaseModel):
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
    guidance_scale: float = Field(
        default=DEFAULT_GUIDANCE_SCALE,
        ge=0.0,
        description=(
            "引导强度（这一家 1–5 的刻度，默认 2）。请求里的「指令强度」是 0–1 刻度，"
            "换算为 0.5 → 2；这一格是上游没给时用的值。调高更贴描述，过高会失真。"
        ),
        json_schema_extra=ui_hints("引导强度", group="生成", widget="number"),
    )
    num_inference_steps: int = Field(
        default=10,
        ge=1,
        description="每个分块的扩散步数（官方推荐 10）。调高保真度上升、耗时同比增加。",
        json_schema_extra=ui_hints("推理步数", group="生成", widget="number"),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（插件抽一个发出去并报回来——引擎省略种子时用定值 1234，"
            "不抽就不会变）。0 及以上 = 固定，同一段文本可复现。"
        ),
        json_schema_extra=ui_hints("随机种子", group="生成", widget="number"),
    )
    text_chunk_size: int = Field(
        default=0,
        ge=0,
        description="长文本每块最多多少个字符。0 = 用引擎默认（2048）。",
        json_schema_extra=ui_hints("分块字数", group="长文本", widget="number"),
    )
    timeout: float = Field(
        default=600.0,
        gt=0,
        description="单次请求超时（秒）。2B 模型的扩散解码比小模型慢，长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
