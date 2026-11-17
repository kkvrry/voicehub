"""读音替换的数据文件（``<数据目录>/pronunciations.json``）。

文件只存用户自己加的「词 → 同音词」列表，按写入顺序保存：

```json
{"entries": [{"word": "重庆", "replacement": "崇庆"}]}
```

默认没有这份文件，读不到 / 写坏都当空表——和 ``emotions.json`` 一样，它是给人
手改的数据文件，不该因为一个坏括号让合成整条链失败。校验与生效在
:mod:`app.usecase.pronunciation`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.infra.atomic import write_json
from app.infra.config import data_dir
from app.infra.files import file_stamp

__all__ = ["PRONUNCIATION_FILE", "load_entries", "save_entries", "stamp"]

PRONUNCIATION_FILE = "pronunciations.json"


def _path() -> Path:
    return data_dir() / PRONUNCIATION_FILE


def stamp() -> tuple[str, int, int]:
    """文件指纹：用例层按它缓存编译好的替换表。"""
    path = _path()
    return file_stamp(path) or (os.path.normcase(str(path)), 0, 0)


def load_entries() -> list[dict[str, str]]:
    """读原始条目；坏形状丢掉，不做长度/重复校验（那是用例层的事）。"""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return []
    found: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        word = str(value.get("word") or "").strip()
        replacement = str(value.get("replacement") or "").strip()
        if word and replacement:
            found.append({"word": word, "replacement": replacement})
    return found


def save_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """整份原子写；调用方传进来的条目应已洗过。"""
    cleaned: list[dict[str, str]] = []
    for item in entries:
        word = str(item.get("word") or "").strip()
        replacement = str(item.get("replacement") or "").strip()
        if word and replacement:
            cleaned.append({"word": word, "replacement": replacement})
    write_json(_path(), {"entries": cleaned})
    return cleaned
