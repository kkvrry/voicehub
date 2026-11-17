"""AI 辅助提示词：默认文本、用户覆盖，以及向设置页与各任务分发的那一份。

四组：

- **对话分析**：多条可切换的预设（默认在 :mod:`prompts`），用户可改、可增删；
- **AI 匹配音色** / **角色画像** / **文本转正则**：一条系统提示词 + 一条用户模板，
  模板占位由程序填入。

默认文本跟着仓库走；用户改过的只存覆盖，文件在
``<数据目录>/producers/audiobook/prompts.json``。设置页「词表」读
:func:`state`，保存走 :func:`save`；各任务读 :func:`assist_messages` /
:func:`analysis_preset`，所以设置里改的就是界面下次实际用的那一份。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json
from app.infra.config import data_dir
from app.producers.builtin.audiobook.prompts import (
    ANALYSIS_PRESETS,
    DEFAULT_OUTPUT,
    OUTPUT_FIELDS,
    infer_fields,
)

__all__ = [
    "MAX_ANALYSIS_ITEMS",
    "MAX_ANALYSIS_TEXT_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_PROMPT_CHARS",
    "MAX_SYSTEM_CHARS",
    "PromptError",
    "analysis_fields",
    "analysis_preset",
    "assist_messages",
    "groups_meta",
    "presets_payload",
    "revision",
    "save",
    "state",
]

PROMPTS_FILE = "prompts.json"

MAX_ANALYSIS_ITEMS = 30
MAX_ANALYSIS_TEXT_CHARS = 4000
MAX_PROMPT_CHARS = 8000
MAX_SYSTEM_CHARS = 2000
MAX_LABEL_CHARS = 30

#: 对话分析那一组的说明（设置页卡片上的副标题）。
ANALYSIS_LABEL = "对话分析"
ANALYSIS_NOTE = "对话分析发给模型的提示词预设；这里改的是所有项目下一次分析用的那一份。"

_VA_SYSTEM = "你是中文有声书的配音导演，只输出 JSON。"
_VA_TEMPLATE = """为《{book}》里的这些角色选配音。

规则：
{rules}

角色：
{roles}
{candidates}"""

_PROFILE_SYSTEM = "你是中文有声书的配音导演，只输出 JSON。"
_PROFILE_TEMPLATE = """为{title}里的角色各写一份声音画像，供之后从音色库里挑音色。

角色（括号里是这本书里 TA 的台词行数，可用于判断戏份）：
{roles}

只输出 JSON：
{"roles":[{"speaker":"角色名","known":true,"gender":"男|女","age":"童声|少年|青年|中年|老年","timbre":["低沉","略带沙哑"],"tone":["沉稳","克制"],"pace":"偏慢","note":"一句身份"}]}

规则：
1. 先按你自己的了解判断；书名与作者已在开头给出，不需要再查作者。认不准的角色可以联网查证；查过仍不知道声音的，写 known:false 并把其余字段留空——不要按名字猜。
2. timbre 写声音本身（3~5 个词）：音高、厚薄、明暗、沙哑或圆润；tone 写说话的语气（1~3 个词）。两者不重复。
3. 不写剧情、不写台词、不写角色名，也不写「适合配音」这类话。
4. gender 与 age 按作品设定填；作品没写明就留空。pace 只填 偏慢 / 适中 / 偏快。
5. 每个角色一条，不重复；角色名原样照抄。
6. note 写一句「身份 + 年龄感 + 说话方式」（30~60 字），让没读过这本书的人也能想象这个声音；不要复述剧情。"""

_REGEX_SYSTEM = "你是中文出版物文本清洗的 Python 正则专家，只输出 JSON。"
_REGEX_TEMPLATE = """{action}

输入（可能是一句需求描述，也可能是一条文本样例）：
<输入>
{input}
</输入>

生成一条 Python 正则解决这个输入。只输出 JSON：
{"pattern":"...","replacement":"...","explanation":"..."}
pattern 只放正则本身，不要用代码围栏或引号把整个正则包起来，也不要解释文字；
后端不额外传 flags，按行匹配时自己写 (?m)；删除时 replacement 留空。"""


class PromptError(ValueError):
    """提示词不合法（api 层翻成 400）。"""


#: 固定两段式的三组；顺序即设置页卡片顺序。
FIXED_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "voice_assign",
        "label": "AI 匹配音色",
        "note": "AI 匹配音色时发给对话模型；占位由程序填入，删掉必需占位会保存失败。",
        "items": (
            {
                "key": "system",
                "label": "系统提示词",
                "required": (),
                "default": _VA_SYSTEM,
            },
            {
                "key": "template",
                "label": "用户提示词",
                "required": ("{book}", "{rules}", "{roles}", "{candidates}"),
                "default": _VA_TEMPLATE,
            },
        ),
    },
    {
        "key": "voice_profile",
        "label": "角色画像",
        "note": "AI 写角色声音画像时发给对话模型；{title} 与 {roles} 由程序填入。",
        "items": (
            {
                "key": "system",
                "label": "系统提示词",
                "required": (),
                "default": _PROFILE_SYSTEM,
            },
            {
                "key": "template",
                "label": "用户提示词",
                "required": ("{title}", "{roles}"),
                "default": _PROFILE_TEMPLATE,
            },
        ),
    },
    {
        "key": "regex_rule",
        "label": "文本转正则",
        "note": "文本转正则时发给对话模型；{action} 与 {input} 由程序填入。",
        "items": (
            {
                "key": "system",
                "label": "系统提示词",
                "required": (),
                "default": _REGEX_SYSTEM,
            },
            {
                "key": "template",
                "label": "用户提示词",
                "required": ("{action}", "{input}"),
                "default": _REGEX_TEMPLATE,
            },
        ),
    },
)

_FIXED_BY_KEY = {group["key"]: group for group in FIXED_GROUPS}

_ANALYSIS_DEFAULT_KEYS = {preset.key for preset in ANALYSIS_PRESETS}

#: 读盘缓存：路径 + mtime + 大小。路径也进键——测试每个用例换数据目录。
_CACHE: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def _path() -> Path:
    return data_dir() / "producers" / "audiobook" / PROMPTS_FILE


def _stamp(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return str(path), 0, 0
    return str(path), stat.st_mtime_ns, stat.st_size


def _clean_analysis(raw: Any) -> dict[str, Any]:
    """读盘用：坏条目丢掉，重复键只留先写的，不让手改文件拖垮设置页。"""
    if not isinstance(raw, dict):
        return {"hidden": [], "custom": []}
    hidden: list[str] = []
    for value in raw.get("hidden") if isinstance(raw.get("hidden"), list) else []:
        key = str(value or "").strip()
        if key and key not in hidden:
            hidden.append(key)
    custom: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    for value in raw.get("custom") if isinstance(raw.get("custom"), list) else []:
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "").strip()
        label = str(value.get("label") or "").strip()
        text = str(value.get("text") or "").strip()
        if not label or not text:
            continue
        if key and key in seen_keys:
            continue
        if not key and label in seen_labels:
            continue
        if key:
            seen_keys.add(key)
        seen_labels.add(label)
        custom.append({"key": key, "label": label, "text": text})
    return {"hidden": hidden, "custom": custom}


def _clean_fixed(raw: Any) -> dict[str, dict[str, str]]:
    """读盘用：只认已知分组与条目，空串等于没改。"""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for group_key, values in raw.items():
        group = _FIXED_BY_KEY.get(str(group_key))
        if group is None or not isinstance(values, dict):
            continue
        known = {item["key"] for item in group["items"]}
        items = {
            item_key: str(text or "").strip()
            for item_key, text in values.items()
            if item_key in known and str(text or "").strip()
        }
        if items:
            cleaned[group["key"]] = items
    return cleaned


def _user() -> dict[str, Any]:
    global _CACHE
    path = _path()
    stamp = _stamp(path)
    if _CACHE is not None and _CACHE[0] == stamp:
        return _CACHE[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    data = {"analysis": _clean_analysis(raw.get("analysis")), "fixed": _clean_fixed(raw.get("fixed"))}
    _CACHE = (stamp, data)
    return data


def _write(user: dict[str, Any]) -> None:
    global _CACHE
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, user)
    _CACHE = None


def _merge_analysis(user: dict[str, Any]) -> dict[str, Any]:
    """用户差别 + 仓库默认 → 生效预设；保存校验也走这一份。"""
    defaults = [
        {"key": preset.key, "label": preset.label, "note": preset.note, "text": preset.text}
        for preset in ANALYSIS_PRESETS
    ]
    hidden = [key for key in user["hidden"] if key in _ANALYSIS_DEFAULT_KEYS]
    hidden_set = set(hidden)
    custom = user["custom"]
    overrides = {
        item["key"]: item for item in custom if item["key"] in _ANALYSIS_DEFAULT_KEYS
    }
    fields_by_key = {preset.key: preset.fields for preset in ANALYSIS_PRESETS}
    effective: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    for item in defaults:
        if item["key"] in hidden_set:
            continue
        override = overrides.get(item["key"])
        text = override["text"] if override else item["text"]
        label = override["label"] if override else item["label"]
        effective.append(
            {
                **item,
                # 改过正文就不再用默认那句说明：说法和提示词对不上比没有说明更误导。
                "note": "" if override else item["note"],
                "label": label,
                "text": text,
                "fields": fields_by_key.get(item["key"], OUTPUT_FIELDS[DEFAULT_OUTPUT]),
            }
        )
        used_keys.add(item["key"])
    number = 0
    for item in custom:
        if item["key"] in _ANALYSIS_DEFAULT_KEYS:
            continue
        number += 1
        key = item["key"] or f"user-{number}"
        while key in used_keys:
            number += 1
            key = f"user-{number}"
        effective.append(
            {
                "key": key,
                "label": item["label"],
                "note": "",
                "text": item["text"],
                "fields": infer_fields(item["text"]),
            }
        )
        used_keys.add(key)
    return {"defaults": defaults, "hidden": hidden, "custom": custom, "effective": effective}


def _analysis_state() -> dict[str, Any]:
    return _merge_analysis(_user()["analysis"])


def _fixed_state() -> list[dict[str, Any]]:
    user = _user()["fixed"]
    groups: list[dict[str, Any]] = []
    for group in FIXED_GROUPS:
        values = user.get(group["key"], {})
        groups.append(
            {
                "key": group["key"],
                "label": group["label"],
                "note": group["note"],
                "items": [
                    {
                        "key": item["key"],
                        "label": item["label"],
                        "text": values.get(item["key"], item["default"]),
                        "default": item["default"],
                        "required": list(item["required"]),
                    }
                    for item in group["items"]
                ],
            }
        )
    return groups


def state() -> dict[str, Any]:
    """设置页「词表」里 AI 提示词那一块：生效预设 + 三组固定提示词。"""
    analysis = _analysis_state()
    return {
        "object": "producer_prompts",
        "analysis": {
            "label": ANALYSIS_LABEL,
            "note": ANALYSIS_NOTE,
            "defaults": analysis["defaults"],
            "hidden": analysis["hidden"],
            "custom": analysis["custom"],
            "effective": [
                {key: item[key] for key in ("key", "label", "note", "text")}
                for item in analysis["effective"]
            ],
        },
        "fixed": _fixed_state(),
    }


def groups_meta() -> list[dict[str, str]]:
    """插件自描述里的分组清单（不带正文）：设置页据此知道这家有提示词可编。"""
    return [
        {"key": "analysis", "label": ANALYSIS_LABEL, "note": ANALYSIS_NOTE},
        *[
            {"key": group["key"], "label": group["label"], "note": group["note"]}
            for group in FIXED_GROUPS
        ],
    ]


def presets_payload() -> list[dict[str, Any]]:
    """分析弹窗下拉照这一份列；顺序与生效值一致。"""
    return [
        {"key": item["key"], "label": item["label"], "note": item["note"], "text": item["text"]}
        for item in _analysis_state()["effective"]
    ]


def analysis_preset(key: str) -> dict[str, Any] | None:
    """按 key 取生效预设；没有就回 ``None``。"""
    wanted = str(key or "").strip()
    if not wanted:
        return None
    for item in _analysis_state()["effective"]:
        if item["key"] == wanted:
            return item
    return None


def analysis_fields(prompt: str, preset_key: str = "") -> tuple[str, ...]:
    """这次要模型回哪几格：先认生效预设的正文，再按正文关键词推断。"""
    text = str(prompt or "").strip()
    if text:
        for item in _analysis_state()["effective"]:
            if str(item["text"]).strip() == text:
                return tuple(item["fields"])
        return infer_fields(text)
    preset = analysis_preset(preset_key)
    if preset is not None:
        return tuple(preset["fields"])
    return OUTPUT_FIELDS[DEFAULT_OUTPUT]


def assist_messages(group_key: str) -> tuple[str, str]:
    """一组固定提示词的生效值：``(系统提示词, 用户提示词模板)``。"""
    group = _FIXED_BY_KEY.get(group_key)
    if group is None:
        raise PromptError(f"没有这组 AI 提示词：{group_key}。")
    values = _user()["fixed"].get(group_key, {})
    items = {item["key"]: item for item in group["items"]}
    system = values.get("system", items["system"]["default"])
    template = values.get("template", items["template"]["default"])
    return system, template


def revision(group_key: str) -> str:
    """一组提示词的版本指纹：改过模板就让画像之类的旧缓存失效。"""
    system, template = assist_messages(group_key)
    material = f"{system}\0{template}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #


def _check_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise PromptError(f"{label}不能为空。")
    if len(text) > limit:
        raise PromptError(f"{label}太长：{len(text)} 字，最多 {limit} 字。")
    return text


def _save_analysis(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PromptError("分析提示词必须是对象。")
    hidden: list[str] = []
    for value in payload.get("hidden") or []:
        key = str(value or "").strip()
        if key in _ANALYSIS_DEFAULT_KEYS and key not in hidden:
            hidden.append(key)
    raw_custom = payload.get("custom")
    if raw_custom is None:
        raw_custom = []
    if not isinstance(raw_custom, list):
        raise PromptError("分析提示词的自定义条目必须是列表。")
    custom: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    for entry in raw_custom:
        if not isinstance(entry, dict):
            raise PromptError("分析提示词的自定义条目格式不对。")
        key = str(entry.get("key") or "").strip()
        if key and key in seen_keys:
            raise PromptError(f"提示词重复：{key}。")
        label = _check_text(entry.get("label"), "提示词短名", MAX_LABEL_CHARS)
        text = _check_text(entry.get("text"), f"「{label}」的正文", MAX_ANALYSIS_TEXT_CHARS)
        if label in seen_labels:
            raise PromptError(f"提示词短名重复：「{label}」。")
        if key:
            seen_keys.add(key)
        seen_labels.add(label)
        custom.append({"key": key, "label": label, "text": text})
    if len(custom) > MAX_ANALYSIS_ITEMS:
        raise PromptError(f"分析提示词太多：{len(custom)} 条，最多 {MAX_ANALYSIS_ITEMS} 条。")
    # 新加的预设当场发一个稳定 key：项目存的是这个 key，删掉别的预设不能让它换身份。
    used = set(_ANALYSIS_DEFAULT_KEYS) | seen_keys
    number = 0
    for entry in custom:
        if entry["key"]:
            continue
        number += 1
        key = f"user-{number}"
        while key in used:
            number += 1
            key = f"user-{number}"
        entry["key"] = key
        used.add(key)
    candidate = {"hidden": hidden, "custom": custom}
    # 生效值里不能有同一条：改名撞上默认、两条默认改成同名都在这里拦下。
    labels: list[str] = []
    for entry in _merge_analysis(candidate)["effective"]:
        if entry["label"] in labels:
            raise PromptError(f"提示词短名重复：「{entry['label']}」。")
        labels.append(entry["label"])
    return candidate


def _save_fixed(payload: Any) -> dict[str, dict[str, str]]:
    if not isinstance(payload, dict):
        raise PromptError("AI 提示词必须是对象。")
    updated: dict[str, dict[str, str]] = {}
    for group_key, values in payload.items():
        group = _FIXED_BY_KEY.get(str(group_key))
        if group is None:
            raise PromptError(f"没有这组 AI 提示词：{group_key}。")
        if not isinstance(values, dict):
            raise PromptError(f"「{group['label']}」的提示词必须是对象。")
        known = {item["key"]: item for item in group["items"]}
        cleaned: dict[str, str] = {}
        for item_key, text in values.items():
            item = known.get(str(item_key))
            if item is None:
                raise PromptError(f"「{group['label']}」没有这一项：{item_key}。")
            limit = MAX_SYSTEM_CHARS if item_key == "system" else MAX_PROMPT_CHARS
            value = _check_text(text, f"{group['label']} · {item['label']}", limit)
            for placeholder in item["required"]:
                if placeholder not in value:
                    raise PromptError(
                        f"「{group['label']} · {item['label']}」必须保留占位 {placeholder}。"
                    )
            # 与出厂一字不差就不算差别：以后仓库改默认，这一项能跟着走。
            if value == item["default"]:
                continue
            cleaned[str(item_key)] = value
        updated[group["key"]] = cleaned
    return updated


def save(patch: Any) -> dict[str, Any]:
    """按块保存：给哪一块就改哪一块，没给的保持原样。"""
    if not isinstance(patch, dict):
        raise PromptError("提示词保存体必须是对象。")
    unknown = set(patch) - {"analysis", "fixed"}
    if unknown:
        raise PromptError(f"未知的分组：{'、'.join(sorted(unknown))}。")
    analysis = _save_analysis(patch["analysis"]) if "analysis" in patch else None
    fixed = _save_fixed(patch["fixed"]) if "fixed" in patch else None
    user = _user()
    if analysis is not None:
        user["analysis"] = analysis
    if fixed is not None:
        merged = dict(user["fixed"])
        for group_key, values in fixed.items():
            if values:
                merged[group_key] = values
            else:
                # 空组 = 恢复这一组的出厂内容：直接删覆盖，不把默认正文抄进文件。
                merged.pop(group_key, None)
        user["fixed"] = merged
    _write(user)
    return state()
