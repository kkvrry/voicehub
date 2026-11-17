"""读音替换用例：把用户写的「词 → 同音词」应用到发给引擎的正文。

为什么是同音词而不是拼音
------------------------

已装的六台引擎都只收纯文本，不认拼音 / 音素 / SSML。因此这里不做音素标注，
而是让用户把多音词换成读法相同的词（``重庆 → 崇庆``、``银行 → 银航``）——
所有引擎都通用，不需要为每家再写一套音素映射。

匹配规则
--------

- **最长词优先**：同时有「长」和「长大」时，``长大`` 整体替换，不会被拆开；
- **方括号内不替换**：``[笑]`` / ``[停0.4]`` 是语气标记，不是正文；
- **单遍替换**：替换出来的文字不会再次命中别的词，避免链式改写；
- 条目按写入顺序保存，重复词在保存时拒绝，手改文件里重复则后写的那条生效。

数据文件是 :mod:`app.infra.pronunciation`，编译结果按文件指纹缓存——
一次批量合成里每条正文都读盘会白白拖慢整条流水线。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.infra import pronunciation as store

__all__ = [
    "MAX_ENTRIES",
    "MAX_REPLACEMENT_CHARS",
    "MAX_WORD_CHARS",
    "PronunciationError",
    "apply",
    "matches",
    "revision",
    "save",
    "state",
]

MAX_ENTRIES = 500
MAX_WORD_CHARS = 48
MAX_REPLACEMENT_CHARS = 96

_BRACKET = re.compile(r"(\[[^\]]*\])")
_FORBIDDEN = "[]\r\n"

_Cache = tuple[
    tuple[str, int, int],
    list[dict[str, str]],
    re.Pattern[str] | None,
    dict[str, str],
]
_CACHE: _Cache | None = None


class PronunciationError(ValueError):
    """读音替换条目不合法（api 层翻成 400）。"""

    def __init__(self, message: str, *, code: str = "pronunciation_invalid") -> None:
        super().__init__(message)
        self.code = code


def _tolerant(values: Any) -> list[dict[str, str]]:
    """读盘用：坏条目丢掉，重复词后写的赢，不让手改坏文件拖垮合成。"""
    if not isinstance(values, list):
        return []
    by_word: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        word = str(value.get("word") or "").strip()
        replacement = str(value.get("replacement") or "").strip()
        if not word or not replacement:
            continue
        if len(word) > MAX_WORD_CHARS or len(replacement) > MAX_REPLACEMENT_CHARS:
            continue
        if any(char in word or char in replacement for char in _FORBIDDEN):
            continue
        by_word[word] = replacement
    entries = [
        {"word": word, "replacement": replacement} for word, replacement in by_word.items()
    ]
    return entries[:MAX_ENTRIES]


def _clean(values: Any) -> list[dict[str, str]]:
    """保存用：形状、长度、重复都严格校验——不悄悄截断或吞掉用户敲的字。"""
    if not isinstance(values, list):
        raise PronunciationError("读音替换必须是条目列表。")
    if len(values) > MAX_ENTRIES:
        raise PronunciationError(f"读音替换太多：{len(values)} 条，最多 {MAX_ENTRIES} 条。")
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise PronunciationError(f"第 {index} 条读音替换格式不对。")
        word = str(value.get("word") or "").strip()
        replacement = str(value.get("replacement") or "").strip()
        if not word or not replacement:
            raise PronunciationError(f"第 {index} 条读音替换缺少「词」或「读作」。")
        if len(word) > MAX_WORD_CHARS:
            raise PronunciationError(f"第 {index} 条的词太长（最多 {MAX_WORD_CHARS} 字）。")
        if len(replacement) > MAX_REPLACEMENT_CHARS:
            raise PronunciationError(
                f"第 {index} 条的读作太长（最多 {MAX_REPLACEMENT_CHARS} 字）。"
            )
        if any(char in word or char in replacement for char in _FORBIDDEN):
            raise PronunciationError(f"第 {index} 条不能包含方括号或换行。")
        if word in seen:
            raise PronunciationError(f"「{word}」重复了。")
        seen.add(word)
        cleaned.append({"word": word, "replacement": replacement})
    return cleaned


def _compiled() -> tuple[list[dict[str, str]], re.Pattern[str] | None, dict[str, str]]:
    """当前替换表：条目、最长优先的正则、词 → 读作。按文件指纹缓存。"""
    global _CACHE
    current = store.stamp()
    if _CACHE is not None and _CACHE[0] == current:
        return _CACHE[1], _CACHE[2], _CACHE[3]

    entries = _tolerant(store.load_entries())
    mapping = {item["word"]: item["replacement"] for item in entries}
    pattern = None
    if mapping:
        words = sorted(mapping, key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(word) for word in words))
    _CACHE = (current, entries, pattern, mapping)
    return entries, pattern, mapping


def state() -> dict[str, Any]:
    """设置页读的那一份；坏文件按空条目处理。"""
    return {"entries": _tolerant(store.load_entries())}


def revision() -> str:
    """词表版本：条目为空时返回空串，幂等指纹只在真有替换时才跟着变。"""
    entries, _, _ = _compiled()
    if not entries:
        return ""
    material = json.dumps(sorted(entries, key=lambda item: item["word"]), ensure_ascii=False)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def matches(text: str) -> str:
    """这一段正文**真正命中**的替换（词 → 读作）的摘要；没命中返回空串。

    合成指纹用它，而不是整张词表的版本：改一条与这行无关的规则，不该让整本书
    重新合成、云端重新计费。
    """
    _, hit = apply(text)
    if not hit:
        return ""
    _, _, mapping = _compiled()
    material = json.dumps(
        [(word, mapping.get(word, "")) for word in hit], ensure_ascii=False
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def apply(text: str) -> tuple[str, list[str]]:
    """替换正文，返回 ``(新正文, 命中的词)``；没有条目或没命中就原样返回。"""
    _, pattern, mapping = _compiled()
    if pattern is None or not text:
        return text, []

    applied: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        word = match.group(0)
        applied.append(word)
        return mapping[word]

    # 方括号标记原样保留：只替换偶数段（标记之外）。
    parts = _BRACKET.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = pattern.sub(_replace, parts[index])
    if not applied:
        return text, []
    return "".join(parts), list(dict.fromkeys(applied))


def save(values: Any) -> dict[str, Any]:
    """整份保存并让编译缓存失效。"""
    global _CACHE
    cleaned = _clean(values)
    store.save_entries(cleaned)
    _CACHE = None
    return {"entries": cleaned}
