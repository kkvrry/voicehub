"""统一参数 → Fish Audio 请求体：这台引擎认哪些、不认哪些，全写在明面上。

平台形状（官方文档 ``docs.fish.audio``）：

======================  ==========================================  ==========================
统一参数                Fish Audio 落点                             备注
======================  ==========================================  ==========================
``text``                ``text``                                    正文；情绪与引导指令以标签拼在其前面
``voice_id``            ``reference_id``                            **平台音色 id**，不是音色库里的 id
``emotion``             ``[标签]`` 写在正文开头                      没有情绪参数，标签是唯一手段
``instruction``         ``[标签]`` 写在正文开头                      同上，自由文本
``speed``               ``prosody.speed``                           同为 0.5–2.0 倍率
``volume``              ``prosody.volume``                          倍率 → 分贝
``format``              ``format``                                  mp3 / wav / pcm / opus
``sample_rate``         ``sample_rate``                             整数
``bitrate``             ``mp3_bitrate``                             bps → kbps，只收 64/128/192
======================  ==========================================  ==========================

**模型不进请求体**：官方用请求头 ``model`` 选模型（``s2.1-pro`` 等），所以
``EngineMapping.constants`` 是空的，这一条写在 ``cautions`` 里以免被当成漏接。

**标签写法随模型变**：S1 用圆括号、S2 用方括号。S1 已停用且不在候选里（见
:mod:`settings`），本表按方括号写。
"""

from __future__ import annotations

import math

from app.domain.emotions import EMOTION_SYNONYMS as _EMOTION_SYNONYMS
from app.engines.base import EngineError
from app.engines.mapping import EngineMapping, ParamMapping, register_transform

__all__ = [
    "AUDIO_FORMATS",
    "EMOTION_SYNONYMS",
    "EMOTION_TAGS",
    "MAPPING",
    "MARK_MAP",
    "MARK_PASSTHROUGH",
    "NUMERIC_RANGES",
    "emotion_tag",
    "instruction_tag",
]

#: 这一家接受的输出格式。**与 ``plugin.synthesis_spec().formats`` 是同一张表**。
AUDIO_FORMATS: tuple[str, ...] = ("mp3", "wav", "pcm", "opus")

#: Mp3 码率的三档（官方只收这三个整数，单位 kbps）。
MP3_BITRATES: tuple[int, ...] = (64, 128, 192)

#: 规范情绪词 → 标签内容（方括号里那一段英文）。
#:
#: 平台没有情绪枚举，标签是自由文本；用英文是因为官方示例与标签训练素材都是英文，
#: 中文词留着不保证被当成"表演指令"而不是台词。
#:
#: **认不出的词原样放进去**（``[恍惚]``），像 MiMo 那样交给模型自己理解：这一家明确
#: 接受不在列表里的标签，替用户删掉才是丢信息。因此这张表没有"落不下"的分支。
EMOTION_TAGS: dict[str, str] = {
    # 基础情绪
    "开心": "happy",
    "悲伤": "sad",
    "愤怒": "angry",
    "恐惧": "fearful",
    "惊讶": "surprised",
    "兴奋": "excited",
    "委屈": "aggrieved",
    "平静": "calm",
    "冷漠": "indifferent",
    # 复合情绪
    "怅然": "wistful",
    "欣慰": "gratified",
    "无奈": "resigned",
    "愧疚": "guilty",
    "释然": "relieved",
    "嫉妒": "jealous",
    "厌倦": "weary",
    "忐忑": "anxious",
    "动情": "moved to tears",
    "害羞": "shy",
    "疑惑": "puzzled",
    "失望": "disappointed",
    "担忧": "worried",
    # 态度
    "严肃": "serious",
    "轻蔑": "contemptuous",
    "恳求": "pleading",
    "得意": "smug",
    "撒娇": "coquettish",
    # 旧词直通：候选里已经没有，已有行还写着
    "中性": "neutral",
    "厌恶": "disgusted",
}

#: **同义词 → 规范词**。与别家共用 :mod:`app.domain.emotions` 那一张表：
#: 上游写的「高兴」与「开心」是同一个意思，落点不该是两个。
EMOTION_SYNONYMS: dict[str, str] = dict(_EMOTION_SYNONYMS)

#: 正文里的语气词标签（统一写法 → 这一家的原生写法）。
#:
#: 方括号是 S2 的文档语法（``[happy]``、``[whispers sweetly]``），且官方明说接受
#: 自由文本的标签，所以表里只登记常见项，其余由 ``MARK_PASSTHROUGH`` 兜住。
#:
#: ``停顿`` 落 ``[pause]``：平台不接受秒数，``[[停顿:0.4]]`` 的 0.4 秒在这里被丢弃，
#: 写进 ``cautions`` 而不是假装支持精确停顿时长。
MARK_MAP: dict[str, str] = {
    "停顿": "[pause]",
    "笑": "[laughing]",
    "轻笑": "[chuckle]",
    "叹气": "[sigh]",
    "换气": "[breath]",
    "喘气": "[panting]",
    "吸气": "[inhale]",
    "清嗓": "[clears throat]",
    "咳嗽": "[coughs]",
    "喷嚏": "[sneezes]",
    "哼唱": "[humming]",
    "嗯": "[hmm]",
}

#: 表外标签按什么写。官方原话是接受自由文本标签，所以原样带方括号转过去。
MARK_PASSTHROUGH = "[{name}]"

#: 平台各参数的接受区间（官方）。超范围在发之前拦下。
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "prosody.speed": (0.5, 2.0),
}


def _strip_brackets(value: str) -> str:
    """标签内容里不能有方括号：它会提前结束这条标签。"""
    return value.replace("[", "").replace("]", "").strip()


def emotion_tag(word: str) -> str:
    """规范情绪词 → 标签内容（不含方括号）。认不出返回原词。"""
    text = _strip_brackets(str(word))
    if not text:
        return ""
    canonical = EMOTION_SYNONYMS.get(text, text)
    return _strip_brackets(EMOTION_TAGS.get(canonical, canonical))


def instruction_tag(text: str) -> str:
    """引导指令 → 标签内容（不含方括号）；空串表示"没要求"。"""
    return _strip_brackets(str(text))


# --------------------------------------------------------------------------- #
# 取值换算器
# --------------------------------------------------------------------------- #


def _volume_db(value: object) -> float | None:
    """音量倍率 → 分贝：``20 × log₁₀(倍率)``。

    平台那一格是**分贝偏移**（0 = 原样，正数更响），统一层那一格是倍率。
    0.2 倍 → −14 dB、2.0 倍 → +6 dB，与听感一致。
    """
    try:
        ratio = float(str(value).strip())
    except (TypeError, ValueError):
        raise EngineError(f"音量应为数值，收到 {value!r}。") from None
    if ratio <= 0:
        raise EngineError(f"音量倍率必须大于 0，收到 {ratio}。")
    return round(20.0 * math.log10(ratio), 1)


def _mp3_bitrate(value: object) -> int | None:
    """码率 bps → kbps 三档（64 / 128 / 192）中最接近的一档。

    平台只收这三个整数，统一层是连续的 bps。取最近的一档是**有损**的，所以这一行在
    参数映射页写了「取最接近的一档」：用户按 128000 填，拿到的是 128，不是 64。
    """
    try:
        bps = float(str(value).strip())
    except (TypeError, ValueError):
        raise EngineError(f"码率应为数值，收到 {value!r}。") from None
    if bps <= 0:
        raise EngineError(f"码率必须大于 0，收到 {bps}。")
    kbps = bps / 1000.0
    return min(MP3_BITRATES, key=lambda tier: abs(tier - kbps))


register_transform("fish_volume_db", _volume_db, label="倍率 → 分贝")
register_transform("fish_bitrate", _mp3_bitrate, label="bps → kbps（取最接近档）")


#: 这张表就是"Fish Audio 要哪几个键"的答案。顺序即装配顺序。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "text", required=True, note="要合成的正文。"),
        ParamMapping(
            "voice_id",
            "reference_id",
            required=True,
            note="平台音色 id（清单或克隆返回的 _id）；音色库的命名空间前缀由核心剥离。",
        ),
        ParamMapping(
            "speed",
            "prosody.speed",
            transform="number",
            note="语速倍率，原样发送；平台区间 0.5–2.0。",
        ),
        ParamMapping(
            "volume",
            "prosody.volume",
            transform="fish_volume_db",
            note="倍率换算为分贝（20×log₁₀），平台按分贝偏移接收。",
        ),
        ParamMapping("format", "format", note="输出容器：mp3 / wav / pcm / opus。"),
        ParamMapping(
            "sample_rate", "sample_rate", transform="int", note="输出采样率，按整数发送。"
        ),
        ParamMapping(
            "bitrate",
            "mp3_bitrate",
            transform="fish_bitrate",
            note="bps 换算为 kbps；平台只收 64/128/192 三档，取最接近的一档。仅 mp3 生效。",
            warning=True,
        ),
    ),
    cautions=(
        "模型由请求头 model 指定，不写在请求体里。",
        "平台只收 64/128/192 kbps 三档码率，其余值按最接近的一档发送。",
        "情绪与引导指令都写成正文开头的 [标签]，没有独立字段。",
        "停顿不接受秒数：[[停顿:0.4]] 只发 [pause]，0.4 秒被忽略。",
    ),
)
