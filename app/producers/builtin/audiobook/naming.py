"""文件名与目录名：把书名、章节名变成能在 Windows 上建出来的名字。

- :func:`safe_name`：文件名，非法字符换成下划线，保留可读；
- :func:`folder_name`：目录名，非法字符直接去掉（下划线更难认），保留设备名加前缀。

两者长度上限不同：文件名要能塞进「章名 + 行号 + 音色」这类组合，目录名只需短。
"""

from __future__ import annotations

import re

__all__ = ["folder_name", "safe_name"]

#: 文件名里不能出现的字符（Windows 口径，Linux 上同样避开）。
_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')

#: Windows 保留设备名：拿它们当目录名建不出来。
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def safe_name(value: str, fallback: str = "未命名") -> str:
    """书名 / 章节名 → 文件名：去掉路径分隔符，去掉首尾的点与空格。"""
    name = _BAD_NAME.sub("_", str(value or "")).strip().strip(".")
    return name[:80] or fallback


def folder_name(value: str) -> str:
    """项目目录名：非法字符直接去掉（换下划线更难认），保留名加前缀。"""
    text = _BAD_NAME.sub("", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "未命名项目"
    if text.upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text[:60].strip(" .") or "未命名项目"
