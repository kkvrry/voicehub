"""用户词表：情绪候选与引导指令预设（``<数据目录>/emotions.json``、``instructions.json``）。

为什么记「差别」而不是「整份」
------------------------------

默认那份跟着仓库走——情绪是 :data:`~app.domain.models.EMOTION_CHOICES`，引导指令是各引擎
自己的 ``instructions.json``。用户这份只记**差别**：``hidden``（删掉哪几条默认）、
``renamed``（把默认条目改叫什么）与 ``custom``（自己加的）。于是仓库更新默认词表时，
没动过那些词的人自动跟着走；动过的人也只保留自己那一票。整份替换（像 ``texts.py``
那样）在这儿不合适——词表是**共享的地基**，每人重抄一遍 8 个情绪词没有意义。

``renamed`` 让默认条目原地改名（位置与分组不变）；删掉默认条目仍记在 ``hidden``，
它只影响候选，不动任何项目里已经写下的标签。

- 文件不在 = 没有差别（用默认），**不写盘**；
- 改过之后认文件里那一份，空差别就是空差别；
- 「恢复默认」是显式的一个动作（存一份空差别），不是"读不到就补"；
- 读失败（JSON 写坏了）当作没有差别：这文件是给人手改的，手改就会有写错的格式
  （同 :mod:`app.engines.presets` 的理由），为它让整个设置页 500 不值当。

与 ``texts.py`` 的分工：那边存"念什么"（整份、随个人口味），这边存"有哪些词可用"
（增量、与仓库默认合并）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json
from app.infra.config import data_dir

__all__ = [
    "EMOTIONS_FILE",
    "INSTRUCTIONS_FILE",
    "MAX_CUSTOM_EMOTIONS",
    "MAX_CUSTOM_PRESETS",
    "MAX_PRESET_TEXT_CHARS",
    "MAX_WORD_CHARS",
    "VocabError",
    "load_emotions",
    "load_instructions",
    "save_emotions",
    "save_instructions",
]

#: 两个用户文件的名字（数据目录下）。按概念分，不与 config.json 混——那段由
#: 设置页整份保存，词表是"手改也无妨"的数据文件。
EMOTIONS_FILE = "emotions.json"
INSTRUCTIONS_FILE = "instructions.json"

#: 上限：词表是手改的小东西，不是资料库。越界报错而不是悄悄截断（截掉的是用户
#: 自己敲的字，不说一声等于丢东西）——与 ``texts.py`` 同一条规矩。
MAX_CUSTOM_EMOTIONS = 40
MAX_CUSTOM_PRESETS = 60
MAX_WORD_CHARS = 12
MAX_PRESET_TEXT_CHARS = 200


class VocabError(Exception):
    """词表写入时的用户输入错误（``code`` 供 api 层翻状态码）。"""

    def __init__(self, message: str, *, code: str = "vocab_invalid") -> None:
        super().__init__(message)
        self.code = code


def _path(name: str) -> Path:
    return data_dir() / name


def _read(name: str) -> dict[str, Any]:
    """读一个用户文件；不在 / 坏了 = 空差别（不抛，见模块顶部）。"""
    try:
        raw = json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(name: str, payload: dict[str, Any]) -> None:
    write_json(_path(name), payload)


def _clean_words(values: Any) -> list[str]:
    """一串词：去空白、丢空、去重（保序）。不校验长度与归属——那要知道默认词表，
    是用例层的事（见 :mod:`app.usecase.vocab`）。"""
    if not isinstance(values, list):
        return []
    found: list[str] = []
    for value in values:
        word = str(value or "").strip()
        if word and word not in found:
            found.append(word)
    return found


def _clean_presets(values: Any) -> list[dict[str, str]]:
    """一串预设：只要「短名 + 句子」都在的，去重按短名（保序）。"""
    if not isinstance(values, list):
        return []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        label = str(value.get("label") or "").strip()
        text = str(value.get("text") or "").strip()
        if not label or not text or label in seen:
            continue
        seen.add(label)
        found.append({"label": label, "text": text})
    return found


def _clean_renames(values: Any) -> dict[str, str]:
    """默认条目改名表：``原名 → 新名``。空值与同名条目丢掉（它们不是差别）。"""
    if not isinstance(values, dict):
        return {}
    found: dict[str, str] = {}
    for key, value in values.items():
        word = str(key or "").strip()
        target = str(value or "").strip()
        if word and target and word != target:
            found[word] = target
    return found


# --------------------------------------------------------------------------- #
# 情绪
# --------------------------------------------------------------------------- #


def load_emotions() -> dict[str, Any]:
    """情绪那一节的差别：``{"hidden": [...], "renamed": {...}, "custom": [...]}``。"""
    raw = _read(EMOTIONS_FILE)
    return {
        "hidden": _clean_words(raw.get("hidden")),
        "renamed": _clean_renames(raw.get("renamed")),
        "custom": _clean_words(raw.get("custom")),
    }


def save_emotions(
    hidden: list[str], custom: list[str], renamed: dict[str, str] | None = None
) -> dict[str, Any]:
    """写盘并回当前差别（调用方传进来的已是洗过的）。"""
    cleaned = {
        "hidden": _clean_words(hidden),
        "renamed": _clean_renames(renamed),
        "custom": _clean_words(custom),
    }
    _write(EMOTIONS_FILE, cleaned)
    return cleaned


# --------------------------------------------------------------------------- #
# 引导指令预设
# --------------------------------------------------------------------------- #


def load_instructions() -> dict[str, Any]:
    """指令那一节的差别：``{"hidden", "renamed", "custom"}``。"""
    raw = _read(INSTRUCTIONS_FILE)
    return {
        "hidden": _clean_words(raw.get("hidden")),
        "renamed": _clean_renames(raw.get("renamed")),
        "custom": _clean_presets(raw.get("custom")),
    }


def save_instructions(
    hidden: list[str],
    custom: list[dict[str, str]],
    renamed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """写盘并回当前差别。"""
    cleaned = {
        "hidden": _clean_words(hidden),
        "renamed": _clean_renames(renamed),
        "custom": _clean_presets(custom),
    }
    _write(INSTRUCTIONS_FILE, cleaned)
    return cleaned
