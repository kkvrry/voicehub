"""情绪 → 一句引导指令：没有情绪枚举、但有自由文本指令的引擎共用的翻译表。

为什么放在 domain
----------------

同一张表要被 BreezeTTS 2 / CosyVoice3 / DotTTS 共用。措辞是产品决定，不是某台
引擎的私有参数——改一句，三台一起变，不各写一份迟早对不上。

**语言跟着正文走**：官方要求指令语言与正文语言一致（BreezeTTS 2 的 README 原话是
``Match the instruction language to the target text``；CosyVoice3 / DotTTS 的
instruct 模板同样按自然语言理解）。所以值成对存：中文正文用中文句，英文正文用
英文句。判法是"拉丁字母比中文字符多"——与 Breeze 选括号的尺子同一把。

**认不出的自定义情绪不丢**：套模板 ``用「X」的语气朗读。`` / ``Speak in a "X"
tone.``，至少让那个词进到模型能读懂的位置。空情绪返回空串，调用方按"没要求"处理。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from app.domain.emotions import canonical_emotion

__all__ = ["EMOTION_INSTRUCTIONS", "emotion_instruction"]

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_LATIN = re.compile(r"[A-Za-z]")

#: 规范情绪词 → ``(中文写法, 英文写法)``。
#:
#: 键是 :data:`~app.domain.models.EMOTIONS` 里的规范词；同义写法（兴奋 / 愤怒 /
#: 恐惧…）先经 :func:`~app.domain.emotions.canonical_emotion` 归一，免得同一情绪
#: 因为用词不同而落到模板句上。
#:
#: 为什么不在 ``instructions.json`` 里：那份文件是"给人挑的现成说法"，而这里是
#: "情绪必须有个落点"的映射，前者可以删改、后者删掉就是功能缺口。
EMOTION_INSTRUCTIONS: dict[str, tuple[str, str]] = {
    # 基础情绪
    "开心": ("用开心、轻快的语气朗读。", "Speak in a happy, cheerful tone."),
    "悲伤": ("用悲伤、低沉的语气朗读，语速稍慢。", "Speak in a sad, low tone, a little slower."),
    "愤怒": ("用愤怒、强硬的语气朗读。", "Speak in an angry, firm tone."),
    "恐惧": ("用恐惧、发颤的语气朗读，声音放轻。", "Speak in a frightened, trembling tone."),
    "惊讶": ("用惊讶、上扬的语气朗读。", "Speak in a surprised, rising tone."),
    "兴奋": ("用兴奋、上扬的语气朗读，语速稍快。", "Speak in an excited, upbeat tone."),
    "委屈": ("用委屈、低回的语气朗读。", "Speak in a wronged, subdued tone."),
    "平静": ("用平静、舒缓的语气朗读。", "Speak in a calm, gentle tone."),
    "冷漠": ("用冷漠、不带起伏的语气朗读。", "Speak in a cold, flat tone."),
    # 复合情绪
    "怅然": ("用怅然、失落而克制的语气朗读。", "Speak in a wistful, quietly disappointed tone."),
    "欣慰": ("用欣慰、温和的语气朗读。", "Speak in a gratified, warm tone."),
    "无奈": ("用无奈、放缓的语气朗读。", "Speak in a resigned, unhurried tone."),
    "愧疚": ("用愧疚、放低的语气朗读。", "Speak in a guilty, lowered tone."),
    "释然": ("用释然、松弛的语气朗读。", "Speak in a relieved, relaxed tone."),
    "嫉妒": ("用嫉妒、发紧的语气朗读。", "Speak in a jealous, tight tone."),
    "厌倦": ("用厌倦、提不起劲的语气朗读。", "Speak in a weary, listless tone."),
    "忐忑": ("用忐忑、迟疑的语气朗读。", "Speak in an uneasy, hesitant tone."),
    "动情": ("用动情、带哽咽感的语气朗读。", "Speak in a moved, slightly choked tone."),
    "害羞": (
        "用害羞、音量收着、气息偏弱的语气朗读。",
        "Speak in a shy, quiet tone with a soft, uneven breath.",
    ),
    "疑惑": (
        "用疑惑、上扬而迟疑的语气朗读。",
        "Speak in a puzzled, rising, hesitant tone.",
    ),
    "失望": (
        "用失望、尾音下坠、收短的语气朗读。",
        "Speak in a disappointed tone, the ending trailing off and cut short.",
    ),
    "担忧": (
        "用担忧、压低声音的语气朗读。",
        "Speak in a worried, lowered voice.",
    ),
    # 态度
    "严肃": (
        "用严肃、收住的语气朗读，不放笑意。",
        "Speak in a serious, contained tone, without warmth.",
    ),
    "轻蔑": (
        "用轻蔑、带冷笑的语气朗读。",
        "Speak in a contemptuous, sneering tone.",
    ),
    "恳求": (
        "用恳求、带着求人的急切的语气朗读。",
        "Speak in a pleading, urgent tone.",
    ),
    "得意": (
        "用得意、放松而上扬的语气朗读。",
        "Speak in a smug, relaxed, rising tone.",
    ),
    "撒娇": (
        "用撒娇、拖长尾音、偏软的语气朗读。",
        "Speak in a coquettish tone, drawing out the endings.",
    ),
    # 旧词直通：候选里没有它们了，但老项目的行还写着——照旧给一句，
    # 别让它们掉到模板句（「用「中性」的语气朗读。」读起来不像话）。
    "中性": ("用自然、平稳的语气朗读。", "Speak in a natural, neutral tone."),
    "厌恶": ("用厌恶、冷淡的语气朗读。", "Speak in a disgusted, cold tone."),
}


def emotion_instruction(
    emotion: str,
    text: str,
    *,
    overrides: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """情绪 → 引导指令；空情绪返回空串。

    正文语言按"中文字符与拉丁字母谁多"判，于是"中文正文配中文指令、英文正文配
    英文指令"不需要调用方再说一遍。

    ``overrides`` 给某一台引擎换更具体的说法（键仍是规范情绪词）：有的 instruct
    模板对短句、温和句响应很弱，同一档情绪写得更具体才动得起来。没覆盖到的情绪
    仍走共用表。

    认不出的自定义情绪走模板：中文正文 ``用「X」的语气朗读。``，英文正文
    ``Speak in a "X" tone.``——至少让那个词进到模型能读懂的位置，而不是当作
    "没有情绪"丢掉。
    """
    raw = (emotion or "").strip()
    if not raw:
        return ""
    key = canonical_emotion(raw) or raw
    pair = (overrides or {}).get(key) or EMOTION_INSTRUCTIONS.get(key)
    english = len(_LATIN.findall(text or "")) > len(_CJK.findall(text or ""))
    if pair is not None:
        return pair[1] if english else pair[0]
    return f'Speak in a "{key}" tone.' if english else f"用「{key}」的语气朗读。"
