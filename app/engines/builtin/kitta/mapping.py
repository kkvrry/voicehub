"""统一参数 → Kitta Audio 请求体（HTTP TTS v3）。

这一家是**供应商无关的统一协议**：上游用公共的 ``voiceId``、可选的 ``modelId`` 与一组
通用控制参数，平台负责路由到具体供应商。因此参数名与统一层接近，但仍有三处需要判定：

======================  ======================  ==================================
统一参数                落点                    备注
======================  ======================  ==================================
``text``                ``text``                1–10000 字符，另受模型 maxTextLength 限制
``voice_id``            ``voiceId``             平台音色 id
``emotion``             ``instruction``         写成「情绪：X」一句，见下
``instruction``         ``instruction``         自由文本，上限 1600 字
``speed``               ``speed``               同为 0.5–2.0 倍率
``volume``              ``volume``              倍率 → 分贝（平台本就是分贝）
``pitch``               ``pitch``               倍率 → 半音；模型不支持时平台会忽略并回报
``language``            ``language``            语言提示，原样发送代码
``format``              ``format``              mp3 / wav / ogg
======================  ======================  ==================================

**情绪走引导指令，不发 ``emotion`` 字段**：平台那一格是「全局情绪 ID」，词表由账号侧
定义、文档没有公开；发一个自造的 ID 是一次 400。而 ``instruction`` 明确接受情绪描述，
所以把中文情绪词写成一句前置指令——这与 BreezeTTS 的处理同一口径。

**平台不支持的参数不静默生效**：它会把这些参数名列在 ``X-OpenAPI-Ignored-Parameters``
里，插件把它转成 ``notes`` 回给用户（见 :mod:`client`），不留在日志里。
"""

from __future__ import annotations

import math

from app.domain.emotions import canonical_emotion
from app.engines.base import EngineError
from app.engines.mapping import EngineMapping, ParamMapping, register_transform

__all__ = [
    "AUDIO_FORMATS",
    "INSTRUCTION_LIMIT",
    "MAPPING",
    "NUMERIC_RANGES",
    "with_emotion",
]

#: 这一家接受的输出格式。**与 ``plugin.synthesis_spec().formats`` 是同一张表**。
AUDIO_FORMATS: tuple[str, ...] = ("mp3", "wav", "ogg")

#: ``instruction`` 的字符上限（官方）。
INSTRUCTION_LIMIT = 1600

#: 平台各参数的接受区间（官方）。超范围在发之前拦下。
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "speed": (0.5, 2.0),
    "volume": (-20.0, 20.0),
    "pitch": (-12.0, 12.0),
}


def with_emotion(instruction: str, emotion: str) -> str:
    """情绪并进引导指令：``情绪：开心。`` + 原指令。

    认识的词先归一（「高兴」也写成「开心」）；**认不出的原样保留**——这一格是自由
    文本，平台听得懂自然语言，替用户删掉才是丢信息。
    """
    word = str(emotion or "").strip()
    if not word:
        return instruction
    head = f"情绪：{canonical_emotion(word) or word}。"
    return f"{head}{instruction}".strip()


# --------------------------------------------------------------------------- #
# 取值换算器
# --------------------------------------------------------------------------- #


def _pitch(value: object) -> float | None:
    """音高：**倍率 → 半音**（``12 × log₂(r)``），保留一位小数。

    平台那一格是 ``-12…12`` 的数值，不要求整数；保留一位小数即可，不必像 MiniMax
    那样落到整半音上。
    """
    try:
        ratio = float(str(value).strip())
    except (TypeError, ValueError):
        raise EngineError(f"音高应为数值，收到 {value!r}。") from None
    if ratio <= 0:
        raise EngineError(f"音高倍率必须大于 0，收到 {ratio}。")
    return round(12.0 * math.log2(ratio), 1)


def _volume_db(value: object) -> float | None:
    """音量：倍率 → 分贝（``20 × log₁₀(r)``）。0.2 倍 → −14 dB，2.0 倍 → +6 dB。"""
    try:
        ratio = float(str(value).strip())
    except (TypeError, ValueError):
        raise EngineError(f"音量应为数值，收到 {value!r}。") from None
    if ratio <= 0:
        raise EngineError(f"音量倍率必须大于 0，收到 {ratio}。")
    return round(20.0 * math.log10(ratio), 1)


register_transform("kitta_pitch", _pitch, label="倍率 → 半音")
register_transform("kitta_volume", _volume_db, label="倍率 → 分贝")


#: 这张表就是"Kitta Audio 要哪几个键"的答案。顺序即装配顺序。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "text", required=True, note="要合成的正文。"),
        ParamMapping(
            "voice_id",
            "voiceId",
            required=True,
            note="平台音色 id（清单或克隆返回的 voiceId）；音色库的命名空间前缀由核心剥离。",
        ),
        ParamMapping(
            "instruction",
            "instruction",
            note=f"风格、方言、角色或情绪指令，上限 {INSTRUCTION_LIMIT} 字；情绪写在其前面。",
        ),
        ParamMapping(
            "speed", "speed", transform="number", note="语速倍率，原样发送；平台区间 0.5–2。"
        ),
        ParamMapping(
            "volume",
            "volume",
            transform="kitta_volume",
            note="倍率换算为分贝；平台区间 −20–20 dB，0 为原样。",
        ),
        ParamMapping(
            "pitch",
            "pitch",
            transform="kitta_pitch",
            note="倍率换算为半音；当前模型不支持时平台忽略该参数并回报。",
            warning=True,
        ),
        ParamMapping(
            "language",
            "language",
            note="语言提示，原样发送语言代码（如 zh / en）。",
        ),
        ParamMapping("format", "format", note="输出容器：mp3 / wav / ogg。"),
    ),
    cautions=(
        "平台按音色与模型路由；请求里的 provider 字段与供应商原生字段一律 400，本插件不发。",
        "音高与情绪不是每个模型都支持：被忽略的参数会在响应头回报，界面按提示显示。",
        "情绪写进 instruction，不单独发送 emotion 字段（平台的取值是账号侧的情绪 ID 词表）。",
    ),
)
