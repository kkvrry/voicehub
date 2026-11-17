"""语气词标签的统一写法——正文里"不是字"的东西（笑声、咳嗽、停顿等）。

各引擎写法互不相同，本层定一套中立写法 ``[[标签]]`` / ``[[标签:参数]]``，
由各引擎声明翻译表；**不认的标签从正文删掉**（留着会被念出来）。
用双中括号：识别性强、单条正则可解析，且不与各家原生写法撞车。
标签名用中文——这是写给人看的正文，翻译成引擎写法是引擎层的事。

界面按两级分组（:func:`mark_groups`）：**通用** = 所有支持语气词引擎的交集；
**<引擎名>** = 该引擎认但不在交集里的。不支持语气词的引擎不参与运算；
只有一台参与者时交集就是它自己。加新引擎无需改本模块。

**同一概念的多种写法由 :data:`MARKS` 的 ``aliases`` 归一**（:func:`normalize_marks`）：
插件可以照平台的原文写法登记（``gasp`` / ``喘息``），装载时换成规范名，
交集按语义求，不按字面求。归不上的名字算该引擎自有概念——它是扩充大库的入口
（见 ``scripts/marks_audit.py``），不是错误。

**认任意标签**的引擎（MiMo 这类"标签随你写"）由插件声明
:meth:`~app.engines.base.EnginePlugin.mark_passthrough`：支持面按全集算（它确实发得
出去，也不该因为只登记了几条就把别人的通用项挤掉）；界面上另给一格自定义输入。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MARKS",
    "MARK_PATTERN",
    "Mark",
    "canonical_mark",
    "engine_marks",
    "mark_groups",
    "mark_names",
    "normalize_marks",
    "render_marks",
    "strip_marks",
]

#: ``[[标签]]`` / ``[[标签:参数]]``。**标签名里不许有冒号与方括号**——
#: 冒号是参数分隔符，方括号会破坏"这是一条标签"的判定。
MARK_PATTERN = re.compile(r"\[\[([^\[\]:]+?)(?::([^\[\]]*?))?\]\]")


@dataclass(frozen=True, slots=True)
class Mark:
    """一条统一语气词标签。

    :param name: 正文里写的那个词（``[[笑]]`` 的 ``笑``），也是规范名。
    :param label: 界面上的说法（插标签那颗按钮上的字）。
    :param arg: 参数的含义（空 = 这条不带参数）。``停顿`` 带的是**秒**。
    :param aliases: 同一概念的其他写法（近义词、平台原生词）。插件照原文登记也能
        归一到 ``name``；**别名不新起概念**，只消掉"同一个东西两个名字"。
    """

    name: str
    label: str
    arg: str = ""
    aliases: tuple[str, ...] = ()


#: 统一标签集，覆盖面按最全的那家（MiniMax 2.8）取；顺序即界面顺序（先换气/停顿，再情绪音）。
#:
#: 新引擎的标签若是这里已有的概念（如 ``(gasp)`` = ``喘气``），登记成这里的名字，
#: 不要新起名——否则同一概念在界面上出现两条，"通用"也算不出来。别的写法收进
#: ``aliases``：那一步替代了从前"作者必须记得住统一名"的人工约定。
MARKS: tuple[Mark, ...] = (
    Mark("停顿", "停顿", arg="秒", aliases=("静默", "沉默", "pause", "silence")),
    Mark("笑", "笑声", aliases=("笑声", "laugh", "laughs", "laughter")),
    Mark("轻笑", "轻笑", aliases=("chuckle", "轻笑声")),
    Mark("叹气", "叹气", aliases=("叹息", "sigh", "sighs")),
    Mark("换气", "换气", aliases=("breath", "换气声")),
    Mark("喘气", "喘气", aliases=("喘息", "pant", "pants", "gasp")),
    Mark("吸气", "吸气", aliases=("inhale", "抽气")),
    Mark("呼气", "呼气", aliases=("exhale",)),
    Mark("倒吸气", "倒吸气", aliases=("倒抽气", "倒抽一口气", "gasps")),
    Mark("咳嗽", "咳嗽", aliases=("咳", "cough", "coughs")),
    Mark("清嗓", "清嗓子", aliases=("清嗓子", "clear-throat", "clears throat")),
    Mark("喷嚏", "喷嚏", aliases=("打喷嚏", "sneeze", "sneezes")),
    Mark("吸鼻子", "吸鼻子", aliases=("抽鼻子", "sniff", "sniffs")),
    Mark("喷鼻息", "喷鼻息", aliases=("snort", "snorts")),
    Mark("咂嘴", "咂嘴", aliases=("咂嘴唇", "lip-smacking")),
    Mark("哼唱", "哼唱", aliases=("hum", "humming")),
    Mark("嗯", "嗯", aliases=("emm", "嗯嗯")),
    Mark("呻吟", "呻吟", aliases=("groan", "groans")),
    Mark("嘶嘶声", "嘶嘶声", aliases=("嘶声", "hiss", "hissing")),
    Mark("打嗝", "打嗝", aliases=("burp", "burps")),
)


def mark_names() -> tuple[str, ...]:
    return tuple(m.name for m in MARKS)


#: 统一写法里那些名字 → 它在 :data:`MARKS` 里的次序（**排序用**，见 :func:`_order`）。
_ORDER = {m.name: i for i, m in enumerate(MARKS)}
#: 统一写法里的 ``name → Mark``（给声明表补 ``label`` / ``arg`` 用）。
_BY_NAME = {m.name: m for m in MARKS}


def _alias_index() -> dict[str, str]:
    """任何写法 → 规范名。**先铺规范名、再铺别名**：别名不覆盖同名的规范名
    （``笑`` 的别名里也有 ``笑声``，它只能是别名，不能顶掉 ``笑``）。"""
    index: dict[str, str] = {m.name: m.name for m in MARKS}
    for mark in MARKS:
        for alias in mark.aliases:
            index.setdefault(alias, mark.name)
    return index


_ALIAS = _alias_index()


def canonical_mark(name: str) -> str:
    """写法 → 规范名；大库没收的原样返回（那是该引擎的自有概念）。"""
    key = (name or "").strip()
    return _ALIAS.get(key, key)


def normalize_marks(mapping: Mapping[str, str]) -> dict[str, str]:
    """插件声明的表 → 键一律换成规范名，原生写法不动。

    同一台引擎同时写了规范名与它的别名时，**以规范名那条为准**：别名只是"另一种
    写法"，作者显式写下的规范名才是他要的落点。
    """
    canonical: dict[str, str] = {}
    pending: list[tuple[str, str]] = []
    for name, native in (mapping or {}).items():
        if not name:
            continue
        target = canonical_mark(name)
        if target == name:
            canonical.setdefault(name, native)
        else:
            pending.append((target, native))
    for target, native in pending:
        canonical.setdefault(target, native)
    return canonical


def _order(names) -> list[str]:
    """按界面顺序排标签名：先按 :data:`MARKS`，未收录的按声明顺序排后面（保证跨引擎稳定）。"""
    return sorted(names, key=lambda name: (_ORDER.get(name, len(_ORDER)), name))


def _row(name: str, native: str = "") -> dict[str, str]:
    """一条标签 → 下发给界面的那一行（按钮文案与参数说明的唯一来源）。"""
    mark = _BY_NAME.get(name)
    row = {
        "name": name,
        "label": mark.label if mark else name,
        "arg": mark.arg if mark else "",
    }
    if native:
        row["native"] = native
    return row


def engine_marks(mapping: Mapping[str, str]) -> list[dict[str, str]]:
    """一台引擎声明的表 → 下发给界面的那一份（它认的全部，带原生写法），按 :data:`MARKS` 顺序。

    键先归一成规范名（见 :func:`normalize_marks`）：插件照平台原文写法登记也行，
    界面上一条概念只出现一次。
    """
    table = normalize_marks(mapping)
    return [_row(name, table[name]) for name in _order(table) if name]


def mark_groups(engines) -> dict[str, Any]:
    """跨引擎计算界面上的标签分组：``通用`` 与各引擎自己那一组。

    :param engines: ``[{"key": …, "label": …, "mark_map": {写法: 原生}, "mark_any": bool}, …]``
    :return: ``{"common": [...], "engines": [{"key","label","any","marks"}], "participants": [...]}``

    口径四条：

    1. ``mark_map`` 非空、或声明了 ``mark_any`` 的引擎才是参与者；
    2. 通用 = 参与者**支持面**的交集——"通用"就是"凡是支持语气词的引擎都认"，
       所以只要有一台不认，它就不是通用；
    3. 支持面按归一后的规范名算（同一概念换个写法登记不会漏掉）。认任意标签的引擎
       （``mark_any``）支持面是全集：它确实发得出去，也不会因为只登记了几条就把
       别人的通用项挤掉；
    4. 每台引擎组 = 它**登记**的那几条减去通用。``mark_any`` 的引擎只摆登记过的
       快捷入口，其余靠界面上的自定义输入，不铺满二十条。

    ``participants`` 供界面说明"通用"是对哪几台求的；``any`` 供界面决定是否给
    自定义输入框。
    """
    rows: list[tuple[str, str, dict[str, str], bool]] = []
    for row in engines or []:
        table = normalize_marks(row.get("mark_map") or {})
        any_tag = bool(row.get("mark_any"))
        if not table and not any_tag:
            continue
        rows.append((row.get("key", ""), row.get("label", ""), table, any_tag))

    everything = set(mark_names())
    faces = [set(table) | everything if any_tag else set(table) for _, _, table, any_tag in rows]
    common: set[str] = set.intersection(*faces) if faces else set()

    def own_of(table: dict[str, str]) -> list[dict[str, str]]:
        return [_row(name, table[name]) for name in _order(set(table) - common)]

    return {
        "common": [_row(name) for name in _order(common)],
        # 只有"多出来的"才单列一组；全认的引擎也可能一条都不多出来，那时它仍要有
        # 一组——自定义输入框挂在组上，没有组就没地方点。
        "engines": [
            {"key": key, "label": label, "any": any_tag, "marks": own_of(table)}
            for key, label, table, any_tag in rows
            if set(table) - common or any_tag
        ],
        "participants": [{"key": key, "label": label} for key, label, _, _ in rows],
    }


def render_marks(text: str, mapping: dict[str, str], *, passthrough: str = "") -> str:
    """把正文里的统一标签翻译成该引擎的写法；不认的整段删掉（留着会被念出来）。

    :param mapping: ``标签名或别名 → 原生写法``；值为空串 = 认但发不出去，同样删掉。
    :param passthrough: 认**任意**标签的引擎给的写法模板（``"({name})"``）。
        给了它，表里没有的标签就按这个名字原样转过去，而不是删掉——MiMo 这类
        "标签随你写、模型自己理解"的引擎用这一档。
    """

    table = normalize_marks(mapping)

    def _replace(match: re.Match[str]) -> str:
        name = canonical_mark(match.group(1))
        native = table.get(name, "").strip()
        if not native and passthrough:
            return passthrough.format(name=name)
        if not native:
            return ""
        if "{sec}" not in native:
            return native
        return native.format(sec=_seconds(match.group(2)))

    return MARK_PATTERN.sub(_replace, text or "")


def strip_marks(text: str) -> str:
    """把正文里的语气词标签全删掉（**一台引擎都不认时**的下场，也用于计数/校验）。"""
    return MARK_PATTERN.sub("", text or "")


def _seconds(raw: str | None) -> str:
    """``[[停顿:0.4]]`` 的参数 → 数字字符串。认不出或非正数按 0.5 秒（自然的换气长度）。"""
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        value = 0.5
    if value <= 0:
        value = 0.5
    return f"{min(value, 99.99):.2f}"
