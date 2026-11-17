"""统一参数 → MiniMax 请求体：**这台引擎认哪些、不认哪些，全写在明面上**。

VoiceHub 的上游只按 :class:`~app.engines.base.SynthesisRequest` 说话；这里把那套
统一参数落到 MiniMax 的 ``t2a_v2`` 请求体上（字段路径照官方文档，含嵌套层）。

**不认的参数要说出"为什么"，而不是悄悄丢掉**：上游给了 ``lead_silence``，这里没有
落点——是"这一家没有这个参数"，还是"我们忘了接"？两者从"请求成功"上看不出来，
所以由 ``unsupported()`` 逐项写原因，面板的映射表直接呈现。

**翻译发生在这一层，不在中转站**：统一格式里上游给的是"可读说明"（倍率、中文情绪词、
BCP-47 语言代码），平台要的是它自己那套（半音、英文枚举、``language_boost``）。
两边对不上的地方一律**报错而不是硬发**——硬发的后果是一次失败（且可能计费）的
请求，而平台那句错误通常只说"参数无效"，不会告诉你是哪一个。
"""

from __future__ import annotations

import math

from app.domain.emotions import EMOTION_SYNONYMS as _EMOTION_SYNONYMS
from app.engines.base import EngineError
from app.engines.mapping import (
    EngineMapping,
    ParamMapping,
    register_transform,
    transform_of,
)

__all__ = [
    "AUDIO_FORMATS",
    "EMOTION_ENUM",
    "EMOTION_MAP",
    "EMOTION_SYNONYMS",
    "EMOTION_UNMAPPED",
    "INTERJECTIONS",
    "INTERJECTION_MODELS",
    "LANGUAGE_BOOST_MAP",
    "MAPPING",
    "MARK_MAP",
    "NUMERIC_RANGES",
    "emotion_choices",
]

#: ``voice_setting.emotion`` 的枚举（官方）。
EMOTION_ENUM: tuple[str, ...] = (
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
    "calm",
    "fluent",
)

#: **中文情绪词 → 平台枚举**（规范里那一格给的是中文，见 ``domain.models.EMOTION_CHOICES``）。
#:
#: 左边是**规范词**：``EMOTION_CHOICES`` 逐字就是它（基础 9 + 复合 13 + 态度 5），
#: 改这边要连那边一起改（``tests/test_minimax.py`` 盯着）。
#:
#: ``中性 → fluent`` 是这一张表里唯一需要判断的一格：平台那八个词里没有"neutral"，
#: 而 ``fluent``（流畅、自然）正是"不加情绪、照文本念"的那一个；``calm`` 留给
#: 「平静」——它是**一种情绪**（语气放平），不是"没情绪"。
#:
#: **八档是天花板，规范词可以更细**：越细的词落到这里越糊（``calm`` 一档上已经压着
#: 平静 / 冷漠 / 无奈 / 释然 / 严肃五个词）。这不是失误，是这一家的能力边界——
#: 词表细化的收益只在能把情绪当自由文本用的引擎上兑现（MiMo 的自定义风格、
#: BreezeTTS 2 与 VoxCPM2 的风格描述、IndexTTS2 的 ``emotion_text``）。
EMOTION_MAP: dict[str, str] = {
    # 基础情绪（平台八档正好各有一档）
    "开心": "happy",
    "悲伤": "sad",
    "愤怒": "angry",
    "恐惧": "fearful",
    "惊讶": "surprised",
    "兴奋": "happy",
    "委屈": "sad",
    "平静": "calm",
    "冷漠": "calm",
    # 复合情绪：平台只有八档，按"更接近哪一档"落，落不上的（见 EMOTION_UNMAPPED）
    # 宁可剔掉，由调用方报一句"这一句没情绪"——硬塞一档等于替用户改台词。
    "怅然": "sad",
    "欣慰": "happy",
    "无奈": "calm",
    "愧疚": "sad",
    "释然": "calm",
    "嫉妒": "angry",
    "厌倦": "disgusted",
    "忐忑": "fearful",
    # 害羞按声学落 fearful：它的特征是"退缩、气息弱、音量收"，而 happy 会把这一句
    # 读得外放——正好反了。语义上的"含羞带笑"让位给念法。
    "害羞": "fearful",
    "疑惑": "surprised",
    "失望": "sad",
    "担忧": "fearful",
    # 态度。轻蔑落 disgusted（八档里唯一带"嫌弃"的一档），得意落 happy。
    "严肃": "calm",
    "轻蔑": "disgusted",
    "得意": "happy",
    # 旧词直通：候选里已经没有它们，但已有的行还写着；剔掉它们等于让老项目
    # 悄悄少一个参数（中性 = 不加情绪，正是 fluent）。
    "中性": "fluent",
    "厌恶": "disgusted",
}

#: **规范词在这台引擎上落不进任何一档**的那几个：情绪那一格剔掉，其余照发。
#:
#: 只列规范词，不列同义词（同义词先归一再查表）。判据是"八档里有没有一档能把
#: 这个念法表达出来"，不是"哪个档次看着最近"：
#:
#: - ``动情``：哽咽、气息起伏，八档里没有；
#: - ``恳求`` / ``撒娇``：都是"求"，八档里最近的 happy 会把"求"读成"乐"，
#:   而那正是这两句最要紧的地方——宁可这一句没有情绪，也不给它读反。
EMOTION_UNMAPPED: tuple[str, ...] = ("动情", "恳求", "撒娇")

#: **同义词 → 规范词**。
#:
#: 同一个英文在中文里有好几种说法（兴奋 / 高兴 / 快乐 都是 ``happy``），而有声书
#: 那边的情绪白名单是**用户自己填的自由文本**——所以他完全可能写「兴奋」，而我们
#: 只登记了「开心」。这就是这张表的来历：**先把说法归一，再查枚举**。
#:
#: 认得出的归一到规范词；**认不出的剔掉这一格**（见 :func:`_emotion`），不是报错。
#: 边界：只收"确实就是这一个情绪"的近义词，不收"看着有点像"的——把「阴沉」归一到
#: 「难过」是在替用户下判断，那种事不做。表在 :mod:`app.domain.emotions`（有声书也用它）。
EMOTION_SYNONYMS: dict[str, str] = dict(_EMOTION_SYNONYMS)

#: **BCP-47 语言代码 → ``language_boost``**（官方枚举）。
#:
#: 这一格填进去的是"增强"而不是"指定"：填了它，模型遇到该语言的读音时更稳，
#: 但**不会**阻止它念别的语言。所以认不出的代码就**不发**（而不是报错）——
#: 少一次增强不影响结果，而"编一个近似的"会让模型把中文按日语去念。
LANGUAGE_BOOST_MAP: dict[str, str] = {
    "zh": "Chinese",
    "yue": "Chinese,Yue",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ru": "Russian",
    "pt": "Portuguese",
    "it": "Italian",
    "ar": "Arabic",
    "nl": "Dutch",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "id": "Indonesian",
    "vi": "Vietnamese",
}

#: 这一家接受的输出格式。**与 ``plugin.synthesis_spec().formats`` 是同一张表**
#: （那边从这边取，免得两处各写一遍而对不上）。
AUDIO_FORMATS: tuple[str, ...] = ("mp3", "wav", "flac", "pcm")

#: 平台对各参数的**接受区间**（官方）。超范围会被拒，而拒发生在请求发出之后；
#: 把区间摆在这里，:meth:`MinimaxEngine.build_body` 就能在**发之前**拦下。
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "voice_setting.speed": (0.5, 2.0),
    "voice_setting.vol": (0.0, 10.0),
    "voice_setting.pitch": (-12.0, 12.0),
}


# --------------------------------------------------------------------------- #
# 取值换算器（登记进 app.engines.mapping.TRANSFORMS，映射表按名字引用）
# --------------------------------------------------------------------------- #


def emotion_choices() -> tuple[str, ...]:
    """这台引擎**认得的全部中文情绪词**（规范词在前，同义词在后）。

    上游要能在**发之前**判断"这一行的情绪这家认不认"，才能逐行给它打标；
    让他去猜或者抄一份词表回家，等于又养了一份规范——两边迟早对不上。
    所以它跟着 ``/api/v1/engines`` 一起下发（``value_choices.emotion``）。
    """
    return (*EMOTION_MAP, *EMOTION_SYNONYMS)


def _emotion(value: object) -> str | None:
    """中文情绪词 → 平台枚举。**翻不出来返回 None** = 这一格剔掉，其余照发。

    为什么不是报错：情绪是"少了也能好好念"的那类参数。一次整章合成里有
    一行写了平台不认的词，就为它让整段失败、并且把前面几行的钱也搭进去，
    代价太大。剔掉的结果是"这一句没情绪"，那是**能接受的降级**。

    但**必须留痕**：``apply_mapping`` 会把被剔掉的参数名收进 ``dropped``，
    由调用方回一句「这台引擎认不出情绪「激动」，这一句按无情绪合成」。
    悄悄剔掉的话，用户听到"没情绪"而以为自己要的是"激动"——
    那种错是查不出来的。映射表里这一格开了 ``drop_unmapped`` 就是这个意思。

    与 ``language`` 相反的口径是**刻意的**：情绪是"可读说明"，同一个意思有好几种
    中文说法（所以有 :data:`EMOTION_SYNONYMS`）；``language`` 是 BCP-47 标准码，
    写错了是**填错**，不是方言差别——填错就该说，好让他改。
    """
    text = str(value).strip()
    if text in EMOTION_ENUM:  # 上游直接给的就是平台枚举（规范允许原样透传）
        return text
    canonical = EMOTION_SYNONYMS.get(text, text)
    return EMOTION_MAP.get(canonical) or None


def _pitch(value: object) -> int:
    """音高：**倍率 → 半音**（规范里那一格是倍率，平台那一格是半音）。

    ``12 × log₂(r)``：倍率每翻一倍是十二个半音（一个八度）。规范里那一格的区间
    是 0.5–2.0，正好对上平台的 −12…+12（见规范 §7.1）——**两端对齐不是巧合**，
    这也是"倍率"被选作统一说法的原因。

    **平台那一格是整数**（官方原文：「取值范围为 [-12, 12]，且必须为整数」，``int64``）。
    带小数点的值会被平台以 ``2013 Mismatch type int64`` 拒掉，而那次拒发生在**请求发出之后**
    （真踩过：1.3 倍换出 4.54）。所以这里落到**最近的整半音**上：规范那一格的刻度是
    十分之一倍，换算结果几乎总是带小数，而平台只有 24 档。误差不超过半个半音（约 3%）——
    这是这台引擎的分辨率，**写在这里是因为不说就查不出来**（用户以为要的是 1.3 倍）。
    """
    try:
        ratio = float(str(value).strip())
    except (TypeError, ValueError):
        raise EngineError(f"音高应为数值，收到 {value!r}。") from None
    if ratio <= 0:
        raise EngineError(f"音高倍率必须大于 0，收到 {ratio}。")
    exact = 12.0 * math.log2(ratio)
    # 取整那一步借用映射层登记的 ``int`` 换算器，**不在这里再写一份取整规则**
    # （两份迟早不一样）。恰好落在 x.5 上要求倍率是 ``2^((2n+1)/24)`` 这种无理数，
    # 用十分之一刻度填不出来——所以「半数往哪边」那个边界实际上到不了。
    return transform_of("int")(exact)


def _language_boost(value: object) -> str:
    """语言代码 → ``language_boost``。

    认不出就**报错**，与情绪同一条口径：这一项是"增强"（填了它，模型遇到该语言时
    更稳，但不阻止它念别的语言），所以"认不出"看起来可以"算了"——但那等于把上游
    明确提的一个要求悄悄丢掉，而下一次它会在别的地方变成一个查不出来的小差别。
    要的就是"没增强"这个结果的话，把那一格空着即可（空 = 没要求，不发）。
    """
    code = str(value).strip().lower()
    mapped = LANGUAGE_BOOST_MAP.get(code)
    if mapped:
        return mapped
    raise EngineError(
        "MiniMax 没有「"
        + code
        + "」对应的语言增强值——它认的是 "
        + "、".join(sorted({v for v in LANGUAGE_BOOST_MAP.values()}))
        + "。把「正文语言」空着就能照常合成（不增强而已）。没有发出去。"
    )


register_transform("minimax_emotion", _emotion, label="中文词 → 平台枚举")
register_transform("minimax_pitch", _pitch, label="倍率 → 半音")
register_transform("minimax_language", _language_boost, label="语言代码 → 平台枚举")


#: **正文里的语气词标签**（:mod:`app.domain.marks` 那套统一写法 → 这一家的原生写法）。
#:
#: 左边是上游写在正文里的中文词，右边是平台认的那串英文标签——官方原文：
#: "语气词标签：仅当模型选择 speech-2.8-hd / speech-2.8-turbo 时支持"。
#: ``停顿`` 落到 ``<#x#>``，官方要求它**夹在两段可发音文本之间**（所以正文开头/结尾
#: 的静默写不出来）。
#:
#: **不在这张表里的指令会被从正文里删掉**——留着的下场是被当成字念出来。
MARK_MAP: dict[str, str] = {
    "停顿": "<#{sec}#>",
    "笑": "(laughs)",
    "轻笑": "(chuckle)",
    "叹气": "(sighs)",
    "换气": "(breath)",
    "喘气": "(pant)",
    "吸气": "(inhale)",
    "呼气": "(exhale)",
    "倒吸气": "(gasps)",
    "咳嗽": "(coughs)",
    "清嗓": "(clear-throat)",
    "喷嚏": "(sneezes)",
    "吸鼻子": "(sniffs)",
    "喷鼻息": "(snorts)",
    "咂嘴": "(lip-smacking)",
    "哼唱": "(humming)",
    "嗯": "(emm)",
    "呻吟": "(groans)",
    "嘶嘶声": "(hissing)",
    "打嗝": "(burps)",
}

#: 官方：语气词**只有这两个模型认**（2.6 / 02 系列不认）。别的模型发出去，
#: 最好情况是标签被当成字念出来，所以**在发之前**拦下。
INTERJECTION_MODELS: tuple[str, ...] = ("speech-2.8-hd", "speech-2.8-turbo")

#: 语气词在平台那边的写法（**不含停顿**——停顿是所有模型都认的）。
INTERJECTIONS: tuple[str, ...] = tuple(
    native for name, native in MARK_MAP.items() if name != "停顿"
)


#: 这张表就是"MiniMax 要哪几个键"的答案。顺序即装配顺序，便于与官方文档对照。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "text", required=True, note="要合成的正文。"),
        ParamMapping(
            "voice_id",
            "voice_setting.voice_id",
            required=True,
            note="平台音色 ID；命名空间前缀由核心剥离。",
        ),
        # 上游没给情绪时不发这个键——MiniMax 会自己按文本匹配语气，
        # 发一个空串反而可能被判成无效枚举。
        # 情绪：**认不出就剔掉这一格**（转换器返回 None → 装配时跳过），其余照发。
        # 整章里有一行写了平台不认的词，不该让整段失败——见 :func:`_emotion`。
        ParamMapping(
            "emotion",
            "voice_setting.emotion",
            transform="minimax_emotion",
            drop_unmapped=True,
            note="中文情绪词转为平台枚举；无法识别时移除本项并说明。",
            warning=True,
        ),
        ParamMapping(
            "speed", "voice_setting.speed", transform="number", note="语速倍率，原样发送。"
        ),
        ParamMapping(
            "volume", "voice_setting.vol", transform="number", note="音量倍率，原样发送。"
        ),
        ParamMapping(
            "pitch",
            "voice_setting.pitch",
            transform="minimax_pitch",
            note="倍率换算为半音；平台只收整数，取最近的整半音。",
        ),
        # 语言增强：认不出**报错不发**（与情绪相反，理由见 :func:`_emotion` 末尾）。
        ParamMapping(
            "language",
            "language_boost",
            transform="minimax_language",
            note="语言代码转为平台语言枚举；无法识别时拦截请求。",
            warning=True,
        ),
        ParamMapping("format", "audio_setting.format", note="输出容器格式。"),
        ParamMapping(
            "sample_rate",
            "audio_setting.sample_rate",
            transform="int",
            note="输出采样率，按整数发送。",
        ),
        ParamMapping(
            "bitrate", "audio_setting.bitrate", transform="int", note="输出码率，按整数发送。"
        ),
        ParamMapping(
            "channels", "audio_setting.channel", transform="int", note="输出声道数，按整数发送。"
        ),
    ),
    # 本层不做流式。**显式写 false 而不是省略**：平台默认就是 false，但把
    # "我们没接流式"这件事写进请求体，将来接了也不会忘了改这里。
    constants={"stream": False},
    cautions=(
        "情绪词无法识别时移除该项并继续合成；语言代码无法识别时拦截请求。",
        "数值超出平台区间（语速 0.5–2.0、音量 0–10、音高 ±12 半音）时，在发送前报错。",
        "正文中的停顿标记不能放在首尾；句首或句尾静默无法表达。",
    ),
)
