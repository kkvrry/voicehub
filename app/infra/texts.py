"""文案库：生成样例时"挑一句现成的"那几组句子（``<数据目录>/texts.json``）。

从前这批句子**写死在前端**（``web/src/presets.js`` 的 ``AUDITION_TEXTS``）：加一句要改代码
重构建，而"念什么"是每个人自己的口味——有人要诗句，有人要念一段梗。现在它是**用户
自己的一份配置**：设置页里分组增删，改完立刻生效。

- **默认文案随仓库走**：``texts_default.json`` 与本模块同目录，是版本库里的正本；
  没改过的用户跟着仓库更新，改过的人认自己的 ``data/texts.json``；
- **默认只在你没改过时存在**：文件不在 = 用默认那一份，**不写盘**（否则用户清空后
  一刷新又长回来，"删不掉"比"空着"更难解释）；
- 改过之后（哪怕删成空）就认文件里那一份，空就是空；
- 「恢复默认」是显式的一个动作（``POST /texts/reset``），不是"读不到就补"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json
from app.infra.config import data_dir

__all__ = ["DEFAULT_GROUPS", "TextsError", "defaults", "load", "reset", "save"]

#: 文件里那一份的形状：``[{"name": "诗句", "lines": ["……"]}, …]``。
#: 一条一句，不存标点以外的结构——它只是"念什么"，不是正文模板。
_GROUPS_KEY = "groups"

#: 只限「份量」，不限句子长度：整段原典、长台词都得能整条存进来，截掉的是
#: 用户自己敲的字（读盘时也截更糟，等于每次刷新都少一截）。
#: 组数 / 句数越界要报错而不是悄悄截断，句子本身没有字数上限。
MAX_GROUPS = 24
MAX_LINES = 60

#: 默认文案库的正本：随仓库同步的一个 JSON（分组与句子只在那里写一份）。
_DEFAULTS_PATH = Path(__file__).with_name("texts_default.json")


def _load_defaults() -> list[dict[str, Any]]:
    """读仓库里那份默认文案库；文件不在 / 坏了就当空（界面仍有「恢复默认」）。"""
    try:
        raw = json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get(_GROUPS_KEY) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    groups: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "") or "").strip()
        lines = [str(line).strip() for line in row.get("lines", []) if str(line).strip()]
        if name and lines:
            groups.append({"name": name, "lines": lines})
    return groups


#: 导入时读一次：默认库跟着版本走，"恢复默认"与"文件不在"都用它。
DEFAULT_GROUPS: list[dict[str, Any]] = _load_defaults()


class TextsError(Exception):
    """文案库写入时的用户输入错误（``code`` 供 api 层翻状态码）。"""

    def __init__(self, message: str, *, code: str = "texts_invalid") -> None:
        super().__init__(message)
        self.code = code


def _path() -> Path:
    return data_dir() / "texts.json"


def defaults() -> list[dict[str, Any]]:
    """默认那一份（**副本**：调用方改它不该改到这份常量上）。"""
    return [{"name": row["name"], "lines": list(row["lines"])} for row in DEFAULT_GROUPS]


def load() -> list[dict[str, Any]]:
    """读文案库。文件不在 / 坏了 → 默认那份（不写盘）。"""
    path = _path()
    if not path.exists():
        return defaults()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults()
    rows = raw.get(_GROUPS_KEY) if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return defaults()
    return _clean(rows, strict=False)


def _clean(rows: list[Any], *, strict: bool) -> list[dict[str, Any]]:
    """洗一遍：只收"名字 + 一串句子"，空行丢掉。

    :param strict: 为 true 时组数/句数越界或坏形状报错（写盘那条路）；为 false 时
        挑出能用的（读盘那条路：文件可能被人手改过，读不出来就整个回默认，不能拦住界面）。
    """
    if not isinstance(rows, list):
        if strict:
            raise TextsError("文案库得是一组一组的（列表）。")
        return []
    if strict and len(rows) > MAX_GROUPS:
        raise TextsError(f"组太多了：{len(rows)} 组，最多 {MAX_GROUPS} 组。")
    groups: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            if strict:
                raise TextsError("每一组得是一个对象（名字 + 句子）。")
            continue
        raw_lines = row.get("lines")
        if not isinstance(raw_lines, list):
            if strict:
                raise TextsError("每一组都要有 lines（一串句子）。")
            continue
        name = str(row.get("name", "") or "").strip()
        lines = [str(line).strip() for line in raw_lines if str(line).strip()]
        if strict:
            if not name:
                raise TextsError("每一组都要有名字。")
            if len(lines) > MAX_LINES:
                raise TextsError(f"「{name}」句数太多：{len(lines)} 句，一组最多 {MAX_LINES} 句。")
        elif not name:
            name = "未命名"
        groups.append({"name": name, "lines": lines})
        if len(groups) >= MAX_GROUPS:
            break
    return groups


def save(groups: list[Any]) -> list[dict[str, Any]]:
    """写盘（原子写：临时文件 + rename）。越界与坏形状一律 400，不悄悄改用户的字。"""
    cleaned = _clean(groups, strict=True)
    write_json(_path(), {_GROUPS_KEY: cleaned})
    return cleaned


def reset() -> list[dict[str, Any]]:
    """恢复默认那一份（并写盘，从此它就是你改过的那一份）。"""
    return save(DEFAULT_GROUPS)
