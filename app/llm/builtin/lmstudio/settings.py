"""LM Studio 的配置模型——**界面照这个模型的 JSON Schema 直接渲染表单**。

LM Studio 是本机推理服务，OpenAI 兼容接口默认在 ``http://127.0.0.1:1234/v1``。
模型清单由本机服务自己给，插件不写死候选：``model`` 默认留空，请求前先问
``/v1/models``，优先用当前已加载的模型——用户不必先抄一遍模型 id。

``api_key`` 只在 LM Studio 0.4.0 起开启「需要 API Token」时填；默认不校验，
留空即可。

采样参数（``top_p`` / ``top_k`` / 两个惩罚 / ``seed``）**可空**：空着就不发
这一个键，用 LM Studio 给该模型调好的那套；只有填了才进请求体。
上下文长度不在请求里——那是加载模型时定的，清单里逐条读出展示。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.base import ui_hints

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT",
    "FOLLOW_LM_STUDIO",
    "LmStudioSettings",
]

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 300.0

#: 可空采样参数的占位文案：留空的那个键根本不发。
FOLLOW_LM_STUDIO = "留空 = 跟随 LM Studio"


class LmStudioSettings(BaseModel):

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="LM Studio 的本地服务地址（开发者 → 本地服务）。站点根或带 /v1 都行。",
        json_schema_extra=ui_hints(
            "服务地址", group="连接与服务", placeholder=DEFAULT_BASE_URL
        ),
    )
    api_key: str = Field(
        default="",
        description="只在开启「需要 API Token」时填（LM Studio 0.4.0 起支持）；默认不校验。",
        json_schema_extra=ui_hints(
            "API Token", widget="password", secret=True, group="连接与服务"
        ),
    )
    model: str = Field(
        default="",
        description=(
            "留空 = 用当前已加载的模型；填了就固定发这一个 id。"
            "可选值以「能力与模型」里的本机清单为准。"
        ),
        json_schema_extra=ui_hints(
            "模型", group="连接与服务", placeholder="留空 = 当前已加载的模型"
        ),
    )
    timeout: float = Field(
        default=DEFAULT_TIMEOUT,
        ge=5.0,
        description="单次请求的整体超时（秒）。本机推理长文本要慢慢等。",
        json_schema_extra=ui_hints("超时", widget="number", group="连接与服务"),
    )

    # 采样参数一律可空：**留空就不发这一个键**，跟着 LM Studio 里给这个模型
    # 调好的采样设置走。全填上默认值再发出去，会把用户在 LM Studio 侧的调整
    # 每次请求都覆盖掉——本机推理里那才是他们真正在用的那套参数。
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        ge=0.0,
        le=2.0,
        description="0–2。分析类任务由调用方另行指定，这一格只影响没带温度的请求。",
        json_schema_extra=ui_hints("温度", widget="number", group="采样"),
    )
    max_tokens: int = Field(
        default=DEFAULT_MAX_TOKENS,
        ge=1,
        description="单次回复上限；被截断时响应里会有一句说明。",
        json_schema_extra=ui_hints("回复上限", widget="number", group="采样"),
    )
    top_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="0–1。核采样阈值：只从累积概率到这一比例的候选词里取。",
        json_schema_extra=ui_hints(
            "top_p", widget="number", group="采样", placeholder=FOLLOW_LM_STUDIO
        ),
    )
    top_k: int | None = Field(
        default=None,
        ge=0,
        description="每步只在概率最高的 K 个词里取；0 = 关掉这项过滤。",
        json_schema_extra=ui_hints(
            "top_k", widget="number", group="采样", placeholder=FOLLOW_LM_STUDIO
        ),
    )
    repeat_penalty: float | None = Field(
        default=None,
        ge=0.0,
        description="1 = 不惩罚；大于 1 抑制重复用词。",
        json_schema_extra=ui_hints(
            "重复惩罚", widget="number", group="采样", placeholder=FOLLOW_LM_STUDIO
        ),
    )
    presence_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="−2–2。词只要出现过就压一次，鼓励换话题。",
        json_schema_extra=ui_hints(
            "存在惩罚", widget="number", group="采样", placeholder=FOLLOW_LM_STUDIO
        ),
    )
    frequency_penalty: float | None = Field(
        default=None,
        ge=-2.0,
        le=2.0,
        description="−2–2。按词出现的次数压，越高越不容易反复用同一个词。",
        json_schema_extra=ui_hints(
            "频率惩罚", widget="number", group="采样", placeholder=FOLLOW_LM_STUDIO
        ),
    )
    seed: int | None = Field(
        default=None,
        description="固定后同样的输入与参数给出同样的回复，便于复现。",
        json_schema_extra=ui_hints(
            "随机种子", widget="number", group="采样", placeholder="留空 = 每次随机"
        ),
    )

    @property
    def root(self) -> str:
        """实际要打的 OpenAI 兼容根：补协议、补 ``/v1``、去尾部斜杠。"""
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        if "://" not in raw:
            raw = f"http://{raw}"
        return raw if raw.endswith("/v1") else f"{raw}/v1"
