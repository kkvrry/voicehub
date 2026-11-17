"""GPT-SoVITS 引擎的配置模型——**前端照这个模型的 JSON Schema 直接渲染表单**。

字段分三类：

1. **服务地址**（``base_url``）：这是**本机自己起的**推理服务（常见
   ``http://127.0.0.1:9880``）。它不是云平台，没有密钥。
2. **模型/权重**：GPT-SoVITS 是**整台服务共用一个模型**的——具体哪套权重由服务端
   启动参数或 ``/set_gpt_weights``、``/set_sovits_weights`` 决定，**不在请求里**
   。所以这里没有"模型"这一格。
3. **合成默认值**（语言、切分方式、采样参数）：请求里没给时用它们。

语言有两格而不是一格，因为这家**分开问**：要念的正文是什么语言、参考音频里那句话
是什么语言。合成一段中文却拿日文片段当参考是常见做法，合成端会按各自的规则处理。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = [
    "CUT_METHOD_CHOICES",
    "DBCACHE_CHOICES",
    "LANGUAGE_CHOICES",
    "MEDIA_TYPE_CHOICES",
    "MODEL_VERSION_CHOICES",
    "VERSION_DEFAULTS",
    "GptSovitsSettings",
    "defaults_for",
]

#: 语言代码（照官方 ``TTS.py`` 的 ``v2_languages``）。
#:
#: **v1 版本的模型只认前 6 个**（没有 ``auto_yue`` / ``yue`` / ``ko`` 与三个
#: ``all_*``）——服务端按自己加载的版本校验，发一个它不认的会回 400 并说明版本。
#: 这里列全表是因为"服务端是什么版本"客户端问不到，列全至少让 v2+ 的人不用查文档。
LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "auto · 自动判断（多语种混排）"),
    ("zh", "zh · 中文"),
    ("en", "en · 英文"),
    ("ja", "ja · 日文"),
    ("ko", "ko · 韩文"),
    ("yue", "yue · 粤语"),
    ("auto_yue", "auto_yue · 自动 + 粤语（仅 v2+）"),
    ("all_zh", "all_zh · 全部当中文读（仅 v2+）"),
    ("all_ja", "all_ja · 全部当日文读（仅 v2+）"),
    ("all_yue", "all_yue · 全部当粤语读（仅 v2+）"),
    ("all_ko", "all_ko · 全部当韩文读（仅 v2+）"),
)

#: 长文本怎么切（官方 ``text_segmentation_method.py`` 里注册的方法名）。
#:
#: 这一格影响的是**断句位置**：切得好，句子之间不会在词中间断开。默认 ``cut5``
#: 是"按标点切再按字数合并"，也是官方接口的默认值。
CUT_METHOD_CHOICES: tuple[tuple[str, str], ...] = (
    ("cut0", "cut0 · 不切"),
    ("cut1", "cut1 · 凑四句一切"),
    ("cut2", "cut2 · 凑 50 字一切"),
    ("cut3", "cut3 · 按中文句号「。」切"),
    ("cut4", "cut4 · 按英文句号「.」切"),
    ("cut5", "cut5 · 按标点切 + 按字数合并（默认）"),
)

#: 返回的音频格式（官方 ``check_params`` 里的白名单）。
#: ``raw`` 是**裸 PCM**，没有文件头——除非下游明确要，否则别选它。
MEDIA_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("wav", "wav"),
    ("ogg", "ogg"),
    ("aac", "aac"),
    ("raw", "raw · 裸 PCM（无文件头）"),
)

#: 服务端实际加载的权重版本。**它不在请求里**（整台服务共用一份权重），
#: 填它有两件事：让采样参数跟着那一版的推荐值走（见 :func:`defaults_for`），
#: 以及让人一眼看出"这台服务现在加载的是哪一版"。
#:
#: ``v5turbo`` / ``v5dev`` 是官方 2026-10 发布的 V5（gsv.cpp 也吃这一版）；
#: 其余照官方 README 的版本线。
MODEL_VERSION_CHOICES: tuple[tuple[str, str], ...] = (
    ("v5turbo", "v5turbo · V5 快速版"),
    ("v5dev", "v5dev · V5 开发版"),
    ("v2ProPlus", "v2ProPlus · 推荐"),
    ("v2Pro", "v2Pro"),
    ("v4", "v4 · 48kHz"),
    ("v3", "v3"),
    ("v2", "v2"),
    ("v1", "v1"),
)

#: cache-dit 的缓存档位（gsv.cpp 专有）。空串 = 不发送，由服务端决定。
DBCACHE_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "不发送"),
    ("auto", "auto · 自动"),
    ("off", "off · 关闭"),
    ("f4", "f4"),
    ("f8", "f8"),
    ("f16", "f16"),
)

#: 换版本时该带过去的那组采样参数，按版本分组。
#:
#: **只有 V5 这一档有依据**：``GAG_config.json`` 的默认预设就是 ``top_k=5``、
#: ``text_split_method=cut1``、``sample_steps=4``、``dbcache=auto``；官方
#: ``api_v2`` 的默认则是 ``top_k=15`` / ``cut5`` / 不带 sample_steps。
#: 其余版本不编数值——没有权威来源的"推荐值"写进来就是替用户做主。
VERSION_DEFAULTS: dict[str, dict[str, object]] = {
    "v5turbo": {"top_k": 5, "text_split_method": "cut1", "sample_steps": 4, "dbcache": "auto"},
    "v5dev": {"top_k": 5, "text_split_method": "cut1", "sample_steps": 4, "dbcache": "auto"},
}

#: 不属于 V5 那一档时用的取值（= 官方 api_v2 的默认）。
BASE_DEFAULTS: dict[str, object] = {
    "top_k": 15,
    "text_split_method": "cut5",
    "sample_steps": 0,
    "dbcache": "",
}


def defaults_for(version: str) -> dict[str, object]:
    """某一版的推荐取值（:data:`BASE_DEFAULTS` 打底，V5 覆盖）。"""
    return {**BASE_DEFAULTS, **VERSION_DEFAULTS.get(version, {})}

#: 各家的本地服务习惯端口差不多，这里给一个最常见的默认值，用户基本不用改。
DEFAULT_BASE_URL = "http://127.0.0.1:9880"


class GptSovitsSettings(BaseModel):

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "本机推理服务地址（api_v2.py ）。"
            "端口启动时用 -p 指定，默认 9880。"
        ),
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model_version: str = Field(
        default="v2ProPlus",
        description=(
            "服务端实际加载的权重版本（tts_infer.yaml 的 version）。"
            "不在请求里，也不改服务端；换版本时采样参数按那一版的推荐值走。"
        ),
        json_schema_extra=ui_hints(
            "模型版本", group="连接与服务", widget="select", choices=MODEL_VERSION_CHOICES
        ),
    )
    text_lang: str = Field(
        default="auto",
        description=(
            "auto 服务端自己判断；"
            "v1 版本的模型只认 auto/zh/en/ja/all_zh/all_ja。"
        ),
        json_schema_extra=ui_hints("正文语言", group="语言与断句", widget="select", choices=LANGUAGE_CHOICES),
    )
    prompt_lang: str = Field(
        default="auto",
        description=(
            "参考音频语言"
            "拿日文片段当参考来合成中文是常见做法。"
        ),
        json_schema_extra=ui_hints("参考语言", group="语言与断句", widget="select", choices=LANGUAGE_CHOICES),
    )
    text_split_method: str = Field(
        default="cut5",
        description="长文本断句方法。切错位置会在词中间断开，听感上很明显。",
        json_schema_extra=ui_hints("断句方式", group="语言与断句", widget="select", choices=CUT_METHOD_CHOICES),
    )
    media_type: str = Field(
        default="wav",
        description="返回的音频格式。以实际请求为准",
        json_schema_extra=ui_hints("默认格式", group="语言与断句", widget="select", choices=MEDIA_TYPE_CHOICES),
    )
    speed_factor: float = Field(
        default=1.0,
        description="语速倍率：1 = 原速。这是**采样阶段**的加速，比变速播放自然。",
        json_schema_extra=ui_hints("语速", group="采样", widget="number"),
    )
    temperature: float = Field(
        default=1.0,
        description="采样温度。调低更稳、调高更活（也更可能读出怪音）。",
        json_schema_extra=ui_hints("温度", group="采样", widget="number"),
    )
    top_k: int = Field(
        default=15,
        description="采样时只从概率最高的 K 个 token 里选。",
        json_schema_extra=ui_hints("top_k", group="采样", widget="number"),
    )
    top_p: float = Field(
        default=1.0,
        description="核采样阈值。1 表示不裁剪。",
        json_schema_extra=ui_hints("top_p", group="采样", widget="number"),
    )
    repetition_penalty: float = Field(
        default=1.35,
        description="重复惩罚。调高可以减少复读，调太高会让语调变平。",
        json_schema_extra=ui_hints("重复惩罚", group="采样", widget="number"),
    )
    fragment_interval: float = Field(
        default=0.3,
        description="切出来的句子之间的停顿（秒）。",
        json_schema_extra=ui_hints("句间停顿", group="采样", widget="number"),
    )
    sample_steps: int = Field(
        default=0,
        ge=0,
        le=64,
        description=(
            "采样步数，仅 V5 有效（预设值 4）。"
            "0 = 不发送，由服务端按模型决定。"
        ),
        json_schema_extra=ui_hints("采样步数", group="采样", widget="number"),
    )
    dbcache: str = Field(
        default="",
        description="cache-dit 缓存档位，仅 gsv.cpp 版服务端认。不发送则用它的默认值。",
        json_schema_extra=ui_hints("缓存档位", group="采样", widget="select", choices=DBCACHE_CHOICES),
    )
    batch_size: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "一次推理同时算几段。只对**被切分成多段**的长文本有效，"
            "单句请求仍是逐段；调大更吃显存。"
        ),
        json_schema_extra=ui_hints("批大小", group="采样", widget="number"),
    )
    seed: int = Field(
        default=-1,
        description="随机种子。-1 = 每次都不一样；固定一个数便于复现同一段音频。",
        json_schema_extra=ui_hints("随机种子", group="采样", widget="number"),
    )
    timeout: float = Field(
        default=180.0,
        gt=0,
        description="单次请求超时（秒）。本机推理**没有并发优势**，长文本要慢慢等，别设太小。",
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。

        补 ``http://`` 是因为省掉协议头太常见了（``127.0.0.1:9880``），而 httpx
        对它的报错是 ``unsupported protocol``——那句话不会让人想到"少写了 http://"。
        """
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
