"""IndexTTS2 的配置模型——前端照这个模型的 JSON Schema 直接渲染表单。

分两组：

- **连接与服务**（``base_url`` / ``model`` / ``timeout``）：指向本机 audio.cpp 的
  IndexTTS2 实例（默认 18091）。
- **采样**（``temperature`` / ``top_p`` / ``top_k`` / ``repetition_penalty`` /
  ``max_tokens``）：生成默认值，**随每次请求落进 ``options``**（摆在顶层会被
  服务端静默忽略，与情感参数同一条纪律）。范围与默认值照 audio.cpp 的模型声明
  （``paramSchema``）写，且逐个实测过生效：同 seed 下改变任一参数输出互不相同，
  ``max_tokens`` 调小会直接截断。

**情感不在这里配**：它跟着请求走（引导指令 → ``emotion_text``，
CFG → ``emotion_alpha``），见 :mod:`plugin`。

``interval_silence_ms`` 虽然在模型声明里，但这个 build **不生效**（实测 0 与
3000 的输出逐字节相同）——所以没有那一格：不画"改了不生效"的栏。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "IndexTts2Settings"]

#: audio.cpp 里 IndexTTS2 实例的默认端口（用户在 ``audiocpp`` 里起的那个服务）。
DEFAULT_BASE_URL = "http://127.0.0.1:18091"

#: 模型 id——必须与 ``GET /v1/models`` 里的 ``id`` 一致（这台机器上是 ``indextts2``）。
DEFAULT_MODEL = "indextts2"


class IndexTts2Settings(BaseModel):
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="本机 audio.cpp 的地址（模型列表见 /v1/models）。",
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description="模型 id，必须与 /v1/models 里那一条的 id 一致。",
        json_schema_extra=ui_hints("模型", group="连接与服务"),
    )
    timeout: float = Field(
        default=600.0,
        ge=30.0,
        description="单次请求超时（秒）。本机推理没有并发优势，长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    # ---- 采样参数：默认值随请求落进 options（实测均生效） ---- #
    temperature: float = Field(
        default=0.8,
        ge=0.1,
        le=2.0,
        description="采样温度。调低更稳，调高更活（也更可能读出怪音）。",
        json_schema_extra=ui_hints("温度", group="采样", widget="number"),
    )
    top_p: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="核采样阈值：只从累积概率前 top_p 的候选里选。1 = 不裁剪。",
        json_schema_extra=ui_hints("top_p", group="采样", widget="number"),
    )
    top_k: int = Field(
        default=30,
        ge=0,
        le=100,
        description="每步只从前 top_k 个候选里选；0 = 不限。",
        json_schema_extra=ui_hints("top_k", group="采样", widget="number"),
    )
    repetition_penalty: float = Field(
        default=10.0,
        ge=1.0,
        le=20.0,
        description="重复惩罚：抑制循环复读。调太高会让语调变平。",
        json_schema_extra=ui_hints("重复惩罚", group="采样", widget="number"),
    )
    max_tokens: int = Field(
        default=1500,
        ge=50,
        le=8192,
        description="单次最多生成的 token 数。长句被截断时调大。",
        json_schema_extra=ui_hints("生成上限", group="采样", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
