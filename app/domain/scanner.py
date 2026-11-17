"""目录扫描的纯规则（零 IO）：遍历与 stat 归 infra，这里只做判定。

回答三件事：一层文件里有没有音频（是不是音色）、文件名算不算音频 / 情绪是什么、
哪条是主样例。判定的唯一产地在 domain，walker 与测试都调它。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from app.domain.models import AUDIO_EXTS, EMOTIONS

__all__ = [
    "DEFAULT_MARKS",
    "audio_files_of",
    "emotion_of",
    "is_audio",
    "is_voice_folder",
    "main_sample_of",
    "relative_category",
]

#: 挑主样例时的优先标记（与 audiobook 同口径：默认标记 → 文件名升序）。必须确定性。
#:
#: 这几个词是文件名的结构标记，不是情绪：``emotion_of`` 认到它们时给空串。
DEFAULT_MARKS: tuple[str, ...] = ("默认", "default", "主")

#: 情绪词表：规范词加上训练语料与日常命名的常见同义词。
#:
#: 文件名去壳剩下的未必是情绪（可能是正文式标题），认不出就空着（界面显示「未标」）；
#: 词表外的值用户可手填（进 sidecar，重扫不覆盖）。
_EMOTION_LEXICON: frozenset[str] = frozenset(
    {
        *EMOTIONS,
        # 常见同义词 / 近义写法
        "悲伤", "愤怒", "恐惧", "惊恐", "吃惊", "兴奋", "激动", "温柔", "冷淡",
        "撒娇", "严肃", "傲慢", "羞涩", "哭泣", "哭", "笑", "耳语", "低语",
        "喊叫", "呐喊", "疲惫", "陶醉", "疑惑", "冷漠", "慈祥", "沧桑", "轻蔑",
        "惊慌", "慌张", "哀伤", "忧愁", "释然", "坚定", "猖狂", "狂笑",
        # 英文（大小写不敏感，匹配时折小写）
        "happy", "sad", "angry", "afraid", "fearful", "surprised", "disgusted",
        "neutral", "calm", "excited", "gentle", "whisper", "shout", "cry", "laugh",
    }
)

#: 情绪段之间的分隔符：``胡桃-开心``、``开心 开心`` 这类写法按段拆开再认。
_SEGMENT_SPLIT = re.compile(r"[_\-\s·—]+")

_INDEX_TAIL = re.compile(r"[_\-\s]+\d+$")


def is_audio(filename: str) -> bool:
    """这个文件名算不算音频（按扩展名，大小写不敏感）。"""
    lowered = filename.lower()
    return any(lowered.endswith(ext) for ext in AUDIO_EXTS)


def audio_files_of(filenames: Iterable[str]) -> tuple[str, ...]:
    """一层文件列表里的音频文件（**升序**——主样例回落与界面排序共用这一个次序）。"""
    return tuple(sorted(name for name in filenames if is_audio(name)))


def is_voice_folder(filenames: Iterable[str]) -> bool:
    """这一层**直接**含有音频 → 这个文件夹是一个音色（不再深入它的子文件夹）。"""
    return any(is_audio(name) for name in filenames)


def relative_category(voice_folder: Path, root: Path) -> str:
    """音色的分类 = 音色文件夹相对扫描根的路径（``/`` 连接，根下即空串）。

    必须存整条相对路径而非上一级名：多层结构（``三国杀/魏国/曹操``）里只记
    「魏国」会让「三国杀」那层组不回来。
    """
    try:
        rel = voice_folder.parent.relative_to(root)
    except ValueError:  # 不在这个根下（不该发生；调用方保证了前缀关系）
        return ""
    return "/".join(rel.parts)


def emotion_of(filename: str, *, voice_name: str = "") -> str:
    """从文件名推导情绪标签，只在词表命中时给值（用户可改，此处仅初值）。

    依次：去扩展名与尾部序号 → 去音色名前缀 → 按 ``_`` / ``-`` / 空格拆段
    逐段对词表 → 默认标记（``默认`` / ``default``）不算情绪，给空串。
    """
    stem = filename.rsplit(".", 1)[0].strip() if "." in filename else filename.strip()
    stem = _INDEX_TAIL.sub("", stem).strip()
    if voice_name and stem.lower().startswith(voice_name.lower()):
        rest = stem[len(voice_name) :].lstrip("_-—· ").strip()
        stem = rest or stem
    if stem.lower() in DEFAULT_MARKS:
        return ""
    for segment in _SEGMENT_SPLIT.split(stem):
        if segment and segment.lower() in _EMOTION_LEXICON:
            return segment
    return ""


def main_sample_of(filenames: Iterable[str], configured: str = "") -> str:
    """一组音频里哪条是主样例：用户指定 → 默认标记 → 文件名升序第一条。

    :param configured: sidecar 里用户设的文件名；文件已不在则当作未设。
    """
    files = audio_files_of(filenames)
    if not files:
        return ""
    if configured and configured in files:
        return configured
    lowered = {name.lower(): name for name in files}
    for mark in DEFAULT_MARKS:
        for low, original in lowered.items():
            stem = low.rsplit(".", 1)[0]
            if stem == mark or stem.startswith((mark + "_", mark + "-")):
                return original
    return files[0]
