"""MiMo 引擎的配置模型——**前端照这个模型的 JSON Schema 直接渲染表单**。

字段分三类，各自的道理写在 ``description`` 里（那是给用户看的可读说明）；
界面提示（叫什么、用什么控件、是不是机密）走 :func:`~app.engines.base.ui_hints`。

1. **凭据**（``api_key``）：标了 ``secret``，读接口一律掩码、日志里不打。
2. **站点归属**（``base_url``）：官方端点就一个（``api.xiaomimimo.com``），
   这一栏只给走中转/代理时改。
3. **合成默认值**（``voice``）：请求里没带音色时用它。它不改变请求的形状，
   只是让"没特别说"的那部分有个合理的值。

**没有 ``model`` 这一栏**：这一次用哪个模型由请求自己的形状决定（带参考音频 →
音色复刻，否则预置音色），见 :mod:`app.engines.builtin.mimo.mapping` 顶部。
摆一格"模型"让人选，等于把一件已经确定的事再问一遍，而两处对不上时没有一处会说。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints
from app.engines.builtin.mimo.voices import VOICE_CHOICES

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_VOICE",
    "VOICE_CHOICES",
    "MimoSettings",
    "base_url_for",
]

#: 官方端点（``mimo.mi.com/docs`` 的请求地址）。国内国外同一个域名——
#: 与 MiniMax 那两站密钥不通用**不是**一回事，这一家只有一套账户。
DEFAULT_BASE_URL = "https://api.xiaomimimo.com"

#: 默认音色。官方表格里这一行的说明是"按部署集群而定（国内默认「冰糖」）"。
DEFAULT_VOICE = "mimo_default"


def base_url_for(raw: str) -> str:
    """配置里的地址 → 可用的主机：补协议、去尾部斜杠，留空回落官方端点。"""
    text = (raw or "").strip().rstrip("/") or DEFAULT_BASE_URL
    return text if "://" in text else f"https://{text}"


class MimoSettings(BaseModel):

    api_key: str = Field(
        default="",
        description="前往 platform.xiaomimimo.com 的「API Keys」创建。",
        json_schema_extra=ui_hints("API Key", group="连接与密钥", widget="password", secret=True),
    )
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="只在走中转/代理时改。留空即用官方端点。",
        json_schema_extra=ui_hints("自定义地址", group="连接与密钥"),
    )
    voice: str = Field(
        default=DEFAULT_VOICE,
        description="请求没带音色时用它。仅预置音色那一路生效。",
        json_schema_extra=ui_hints("默认音色", group="音色", widget="select", choices=VOICE_CHOICES),
    )
    timeout: float = Field(
        default=120.0,
        gt=0,
        description=(
            "单次请求超时（秒）。整段合成要等推理跑完才返回，长文本放慢，别设太小。"
        ),
        json_schema_extra=ui_hints("超时", group="连接与密钥", widget="number"),
    )
    #: 官方 TTS 限流：100 RPM / 10M TPM；只写「按账号设有模型并发上限」，没给数字。
    #: 并发与 RPM 是两维：并发决定同时在跑几条，RPM 决定一分钟发了多少。
    concurrency: int = Field(
        default=5,
        ge=1,
        le=16,
        description="同时在跑的任务数。官方未公布并发数，默认 5。",
        json_schema_extra=ui_hints("合成并发", group="并发", widget="number"),
    )
    rpm: int = Field(
        default=100,
        ge=0,
        le=600,
        description="每分钟最多发出的请求数（官方 100 RPM）。填 0 不限。",
        json_schema_extra=ui_hints("每分钟请求数", group="并发", widget="number"),
    )

    @property
    def host(self) -> str:
        """这一份配置实际要打的主机（自定义优先）。"""
        return base_url_for(self.base_url)

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())
