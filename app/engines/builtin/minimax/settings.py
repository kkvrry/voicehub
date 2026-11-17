"""MiniMax 引擎的配置模型——**前端照这个模型的 JSON Schema 直接渲染表单**。

字段分三类，各自的道理写在 ``description`` 里（那是给用户看的可读说明）；
界面提示（叫什么、用什么控件、是不是机密）走 :func:`~app.engines.base.ui_hints`。

1. **凭据**（``api_key``）：标了 ``secret``，读接口一律掩码、日志里不打。
2. **站点归属**（``region``）：MiniMax 的**国内站与国际站是两套独立基础设施，
   密钥不通用**。发错站的表现是"密钥无效"——而那句话在"密钥真的错了"与
   "密钥属于另一个站"两种情况下**一模一样**。所以这一栏不是可选项，是必答项。
3. **合成默认值**（模型、采样率……）：请求里没给时用它们。它们不改变请求的形状，
   只是让"没特别说"的那部分有个合理的值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = [
    "MODEL_CHOICES",
    "REGION_CHOICES",
    "REGION_HOSTS",
    "SOUND_EFFECT_CHOICES",
    "VOICE_TYPE_CHOICES",
    "MinimaxSettings",
    "base_url_for",
]

#: 两站的 API 主机。**不是同一套账号**——见模块顶部。
REGION_HOSTS: dict[str, str] = {
    "cn": "https://api.minimaxi.com",  # 国内站（platform.minimaxi.com）
    "intl": "https://api.minimax.io",  # 国际站（platform.minimax.io）
}

REGION_CHOICES: tuple[tuple[str, str], ...] = (
    ("cn", "国内站 · api.minimaxi.com"),
    ("intl", "国际站 · api.minimax.io"),
)

#: ``model`` 的候选，**照官方文档那道枚举抄的**，一个不多一个不少。
#:
#: 为什么要有这份表：``model`` 是平台**只认枚举**的一格，写错一个字母就是一次
#: 失败且**计费的**请求，而错误文案不会告诉你正确的拼法。给了候选，用户不必猜。
MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("speech-2.8-hd", "speech-2.8-hd（当前代 · 音质优先）"),
    ("speech-2.8-turbo", "speech-2.8-turbo（当前代 · 更省更快，能力相同）"),
    ("speech-2.6-hd", "speech-2.6-hd（上一代 · 音质优先）"),
    ("speech-2.6-turbo", "speech-2.6-turbo（上一代 · 更省更快）"),
)

VOICE_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    ("system", "系统音色"),
    ("voice_cloning", "克隆音色"),
    ("voice_generation", "文生音色"),
)

#: ``voice_modify.sound_effects`` 的候选（官方枚举）。
SOUND_EFFECT_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "不加效果"),
    ("spacious_echo", "空旷回响"),
    ("auditorium_echo", "礼堂回响"),
    ("lofi_telephone", "电话音"),
    ("robotic", "机器人"),
)


def base_url_for(region: str) -> str:
    """站点 → API 主机。认不出的站点当国内站（这是中国用户的默认场景）。"""
    return REGION_HOSTS.get((region or "").strip(), REGION_HOSTS["cn"])


class MinimaxSettings(BaseModel):

    api_key: str = Field(
        default="",
        description="前往platform.minimaxi.com（或 .io）的「接口密钥」中创建。",
        json_schema_extra=ui_hints("API Key", group="连接与密钥", widget="password", secret=True),
    )
    region: Literal["cn", "intl"] = Field(
        default="cn",
        json_schema_extra=ui_hints("站点", group="连接与密钥", widget="select", choices=REGION_CHOICES),
    )
    base_url: str = Field(
        default="",
        description="只在走中转/代理时改。留空即按站点自动取。",
        json_schema_extra=ui_hints("自定义地址", group="连接与密钥"),
    )
    model: str = Field(
        default="speech-2.8-hd",
        description="hd 与 turbo 同代同能力，turbo 更偏速度与成本。",
        json_schema_extra=ui_hints("合成模型", group="合成模型", widget="select", choices=MODEL_CHOICES),
    )
    voice_type: Literal["all", "system", "voice_cloning", "voice_generation"] = Field(
        default="all",
        description="「刷新」时拉取音色范围。",
        json_schema_extra=ui_hints("拉取范围", group="合成模型", widget="select", choices=VOICE_TYPE_CHOICES),
    )
    language_boost: str = Field(
        default="auto",
        description="语言增强。auto自动识别。",
        json_schema_extra=ui_hints("语言", group="合成模型"),
    )
    text_normalization_mode: Literal["", "basic", "quality"] = Field(
        default="",
        description="quality 走混合策略、数字读得更准，但更慢（仅非流式 + 2.6/2.8 生效）。留空用平台默认。",
        json_schema_extra=ui_hints("文本归一化", group="字幕与音效", widget="select", choices=(("", "平台默认"), ("basic", "basic · 快"), ("quality", "quality · 准"))),
    )
    #: 字幕。**只有非流式合成会给**——这一层恒写 ``stream: false``，而平台那个
    #: ``word_streaming`` 只在 ``stream: true`` 时可用，所以它不在候选里。
    #:
    #: 它不是"怎么念"的参数（那类在 :data:`MAPPING` 的映射表里），而是**额外产物**：
    #: 开了以后合成响应里会多一条字幕下载链接（句级、每句不超过 50 字、毫秒时间戳）。
    subtitle_enable: bool = Field(
        default=False,
        description="平台生成字幕并返回下载链接（有有效期）。",
        json_schema_extra=ui_hints("生成字幕", group="字幕与音效", widget="select", choices=((False, "否"), (True, "是"))
        ),
    )
    subtitle_type: Literal["sentence", "word"] = Field(
        default="sentence",
        description="不生成字幕时无效。",
        json_schema_extra=ui_hints("字幕粒度", group="字幕与音效", widget="select", choices=(("sentence", "句级"), ("word", "字级"))
        ),
    )
    sample_rate: int = Field(
        default=32000,
        description="常用 16000 / 24000 / 32000 / 44100。",
        json_schema_extra=ui_hints("采样率", group="输出", widget="number"),
    )
    bitrate: int = Field(
        default=128000,
        description="仅 mp3 生效。",
        json_schema_extra=ui_hints("码率", group="输出", widget="number"),
    )
    channel: Literal[1, 2] = Field(
        default=1,
        description="1 单声道 / 2 立体声。",
        json_schema_extra=ui_hints("声道", group="输出", widget="select", choices=((1, "单声道"), (2, "立体声"))),
    )
    audio_format: Literal["mp3", "wav", "flac", "pcm"] = Field(
        default="mp3",
        json_schema_extra=ui_hints("默认格式", group="输出", widget="select", choices=(("mp3", "mp3"), ("wav", "wav"), ("flac", "flac"), ("pcm", "pcm"))),
    )
    sound_effect: str = Field(
        default="",
        description="给整段音频添加效果。",
        json_schema_extra=ui_hints("音效", group="字幕与音效", widget="select", choices=SOUND_EFFECT_CHOICES),
    )
    timeout: float = Field(
        default=45.0,
        gt=0,
        description="单次请求超时（秒）。根据文本长度设定。",
        json_schema_extra=ui_hints("超时", group="连接与密钥", widget="number"),
    )
    #: 官方 T2A 的限流是**两个维度**：RPM（免费 3、国内付费 20、国际站 60）与
    #: CONN（同时在跑的任务数：免费 1、付费 3）。两者都要卡：并发决定同时在跑几条，
    #: 管不住一分钟发了多少——短句几百毫秒就返回，并发 3 也能打出每分钟数百次。
    concurrency: int = Field(
        default=3,
        ge=1,
        le=3,
        description="同时在跑的任务数（官方 CONN：免费 1、付费 3）。",
        json_schema_extra=ui_hints("合成并发", group="并发", widget="number"),
    )
    rpm: int = Field(
        default=10,
        ge=0,
        le=600,
        description=(
            "每分钟最多发出的请求数（官方 RPM：免费 3、国内付费 20、国际站 60）。"
            "按账单档位填，填 0 不限。"
        ),
        json_schema_extra=ui_hints("每分钟请求数", group="并发", widget="number"),
    )

    @property
    def host(self) -> str:
        """这一份配置实际要打的主机（自定义优先）。"""
        return (self.base_url or "").strip().rstrip("/") or base_url_for(self.region)

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())
