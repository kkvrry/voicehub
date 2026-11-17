"""一条行的全部细节：参数校验、给前端的视图、正文与行的对应关系。

行是这条流水线的中心对象——正文切出来的投影、参数覆盖、情绪与说话人都在它身上。
这里只放与「单行」有关、不需要项目上下文的工具；涉及分章与拆句顺序的部分在
:mod:`app.producers.builtin.audiobook.text_stage`。
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.domain.request_spec import SPEC_FIELDS
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.rules import is_broken

__all__ = [
    "ROW_PARAM_FIELDS",
    "ROW_PARAM_GROUPS",
    "clean_row_params",
    "emotion_words",
    "merge_split_reports",
    "missing_text",
    "normalize_newlines",
    "plan_auto_merge",
    "rewrite_row_texts",
    "row_out",
    "row_params",
    "split_point",
]

#: 行级可覆盖的规范分组：只有真正影响这一行发音的几项才给逐行设置，
#: 输出格式、语言这类跟着引擎配置走的项不放进行里。
ROW_PARAM_GROUPS: tuple[str, ...] = ("expression", "silence")
#: 行级参数里唯一的长文本项，别让人把整章正文塞进引导指令。
_ROW_PARAM_MAX_CHARS = 500


def row_params(row: dict[str, Any]) -> dict[str, Any]:
    """行上存的参数：坏 JSON 当没设过（不让一条脏数据废掉整行）。"""
    raw = row.get("params") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def emotion_words() -> list[str]:
    """情绪候选的生效值：设置页「词表」里那一份（默认 8 词 + 自定义、去隐藏）。

    从前这里写死 :data:`~app.domain.models.EMOTIONS`：用户自己加的情绪词（或模型
    按新词表标的词）到"发不发这一格"的关口会被当成"不在表里"丢掉——自定义词因此
    根本到不了引擎。词表可改之后，这几处关口跟着改；每次运行读一次文件，
    改完下一次运行即生效（与 ``instructions.json`` 同一条"改一下刷新就见效"）。
    """
    from app.usecase import vocab as vocab_uc

    return vocab_uc.emotions()


def emotion_groups() -> list[dict[str, Any]]:
    """情绪候选的分组摆法：与 :func:`emotion_words` 同一份生效值，只多一层分组。

    下拉按它分组，词表本身仍只有后端一处产地——前端不另拼一份。
    """
    from app.usecase import vocab as vocab_uc

    return vocab_uc.emotion_groups()


def row_out(row: dict[str, Any]) -> dict[str, Any]:
    """给前端的行：参数从库里那串 JSON 摊成对象，前端不必再解一次。

    ``broken`` 也在这里给：行表的「需校对」筛选与章节栏的缺字灯共用后端这一句判据，
    前端不再各持一份正则（两处判得不一样时，灯亮了筛不出行）。
    """
    return {
        **row,
        "params": row_params(row),
        "broken": is_broken(str(row.get("text") or "")),
    }


#: 逐行可设的参数：规范里「合成参数」「静默」两组，其余跟着引擎配置走。
ROW_PARAM_FIELDS = {item.key: item for item in SPEC_FIELDS if item.group in ROW_PARAM_GROUPS}


def clean_row_params(raw: Any) -> dict[str, Any]:
    """逐行参数：键必须在白名单里，数值必须落在规范区间内。

    空串 / ``None`` 一律当"没设"剔掉——空着就是让引擎按自己的配置走，
    留一个空键在库里会让指纹误判成"改过"。
    """
    if not isinstance(raw, dict):
        raise ProducerError("参数必须是一张键值表。")
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        field = ROW_PARAM_FIELDS.get(str(key))
        if field is None:
            raise ProducerError(f"这一行不支持参数「{key}」。")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if field.widget == "textarea":
            text = str(value).strip()
            if len(text) > _ROW_PARAM_MAX_CHARS:
                raise ProducerError(
                    f"「{field.label}」最多 {_ROW_PARAM_MAX_CHARS} 字，现在 {len(text)} 字。"
                )
            clean[field.key] = text
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ProducerError(f"「{field.label}」必须是数字。") from None
        if not math.isfinite(number):
            raise ProducerError(f"「{field.label}」必须是有限的数字。")
        if field.widget == "seed":
            if not number.is_integer() or number < -1:
                raise ProducerError("种子必须是 -1 或非负整数。")
            clean[field.key] = int(number)
            continue
        low, high = field.min, field.max
        if (low is not None and number < low) or (high is not None and number > high):
            span = f"{low:g}–{high:g}" + (f"（{field.unit}）" if field.unit else "")
            raise ProducerError(f"「{field.label}」要在 {span} 之间。")
        clean[field.key] = round(number, 4)
    return clean


_SENT_ENDS = "。！？!?…；;"
_CLOSERS = "”」』\"'’）)》"


def split_point(text: str) -> int:
    """拆行位置：离中点最近的句末标点；没有标点就取中点。

    标点后紧跟右引号/右括号时把它留在前一段，避免拆出孤零零的 ``」``。
    """
    length = len(text)
    if length < 2:
        return 0
    middle = length // 2
    ends = [index + 1 for index, char in enumerate(text) if char in _SENT_ENDS]
    if ends:
        nearest = min(ends, key=lambda point: abs(point - middle))
        while nearest < length and text[nearest] in _CLOSERS:
            nearest += 1
        if 0 < nearest < length:
            return nearest
    return max(1, min(middle, length - 1))


def plan_auto_merge(rows: list[dict[str, Any]], *, max_chars: int) -> list[list[str]]:
    """旁白行的合并方案：短于 ``max_chars`` 的旁白往下并，累计到达上限即停。

    ``max_chars`` 是「短」的判据，不是合并后的天花板：短旁白后面接长旁白照样并
    ——那一行本来就是整句，不并就只剩半口气的短句。上限只管「够长了别再往下一路
    吞」，防止滚成整章一行；因此合并后可能超过上限，多出的是下一行本身的长度。

    只并旁白，**下一行是对白就断开**：台词并进旁白行会被念成同一个人的声音，
    角色、情绪也一起被改写。返回若干组行 id，组内按原顺序、每组至少两行；
    调用方按组落库。
    """
    cap = max(1, int(max_chars))
    groups: list[list[str]] = []
    index = 0
    while index < len(rows):
        head = rows[index]
        size = len(str(head.get("text") or ""))
        if str(head.get("kind")) != "narration" or size >= cap:
            index += 1
            continue
        cursor = index + 1
        while cursor < len(rows) and size < cap:
            follow = rows[cursor]
            if str(follow.get("kind")) != "narration":
                break
            size += len(str(follow.get("text") or ""))
            cursor += 1
        if cursor - index > 1:
            groups.append([str(item["id"]) for item in rows[index:cursor]])
            index = cursor
        else:
            index += 1
    return groups


def missing_text(rows: list[dict[str, Any]]) -> str:
    """第一行没有文本的行：返回它的行号说明，全部有文本则返回空串。"""
    for row in rows:
        if not str(row["text"] or "").strip():
            return f"第 {int(row['ordinal']) + 1} 行没有文本。"
    return ""


def normalize_newlines(text: str) -> str:
    """换行统一成 ``\\n``：库里的行文本与正文比对前先过这一道。"""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def merge_split_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐章质检合成一份；警告带上章名，界面只说一次。"""
    warnings: list[str] = []
    for report in reports:
        prefix = f"{report['chapter']}：" if report.get("chapter") else ""
        warnings.extend(prefix + str(item) for item in report.get("warnings", []))
    return {
        "chapters": len(reports),
        "rows": sum(int(item.get("rows", 0)) for item in reports),
        "dialogue": sum(int(item.get("dialogue", 0)) for item in reports),
        "narration": sum(int(item.get("narration", 0)) for item in reports),
        "kept": sum(int(item.get("kept", 0)) for item in reports),
        "warnings": warnings,
    }


def rewrite_row_texts(content: str, before: list[str], after: list[str]) -> str | None:
    """把行文本的改动按顺序写回章节正文；有一处定位不到就返回 ``None``。

    行文本是正文切出来的投影，改错别字必须写回正文——否则下一次重拆从正文算，
    改过的字就丢了。按顺序向后找，同一句重复出现时不会串到前面去。
    """
    if len(before) != len(after):
        return None
    pieces: list[str] = []
    cursor = 0
    for old, new in zip(before, after, strict=True):
        if old == new:
            continue
        index = content.find(old, cursor)
        if index < 0:
            return None
        pieces.append(content[cursor:index])
        pieces.append(new)
        cursor = index + len(old)
    if not pieces:
        return content
    pieces.append(content[cursor:])
    return "".join(pieces)
