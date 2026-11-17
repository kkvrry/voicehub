"""分章模式、文本处理预设与切分规则。

正则表的唯一产地是同目录的 ``patterns.json``（从旧 ``audiobook`` 包整体搬来）：
加模式、改预设都改那个文件，界面从 ``GET /patterns`` 读同一份。

切分语义逐条对齐旧版（不是重新设计）：
1. 逐行 ``match``，锚定行首；
2. 第一个标题之前的内容是「前言」，非空则独立成章；
3. 空章节（只有标题没有正文）不单独成章，标题挂起到下一个有正文的章节；
4. 全文没有标题 → 整篇一章，``matched=False``；
5. 末尾单个悬空标题也成章（旧版会丢，这里修掉）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.producers.base import ProducerError

__all__ = [
    "BRACKET_PRESETS",
    "CHAPTER_PATTERNS",
    "DEFAULT_PATTERN_KEY",
    "DIALOG_PRESETS",
    "ChapterPattern",
    "Preset",
    "SplitResult",
    "TextHit",
    "chapter_pattern",
    "check_conservation",
    "preprocess",
    "split_by_pattern",
    "text_payload",
]

_TABLE_PATH = Path(__file__).with_name("patterns.json")


@dataclass(frozen=True, slots=True)
class ChapterPattern:
    key: str
    name: str
    pattern: str
    example: str


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    pattern: str
    replacement: str = ""
    example: str = ""


def _load() -> dict:
    try:
        return json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - 文件坏了就起不来
        raise RuntimeError(f"分章模式表读不了：{_TABLE_PATH}：{exc}") from exc


_TABLES = _load()

CHAPTER_PATTERNS: tuple[ChapterPattern, ...] = tuple(
    ChapterPattern(item["key"], item["name"], item["pattern"], item.get("example", ""))
    for item in _TABLES["chapter_patterns"]
)
DIALOG_PRESETS: tuple[Preset, ...] = tuple(
    Preset(item["name"], item["pattern"], item.get("replacement", ""))
    for item in _TABLES["dialog_presets"]
)
BRACKET_PRESETS: tuple[Preset, ...] = tuple(
    Preset(item["name"], item["pattern"], example=item.get("example", ""))
    for item in _TABLES["bracket_presets"]
)
DEFAULT_PATTERN_KEY: str = _TABLES["default_pattern_key"]

_BY_KEY = {pattern.key: pattern for pattern in CHAPTER_PATTERNS}


def chapter_pattern(key: str) -> ChapterPattern:
    """未知 key 回落到默认模式：界面上的下拉总有选中项。"""
    return _BY_KEY.get(key) or _BY_KEY[DEFAULT_PATTERN_KEY]


def text_payload() -> dict:
    """下发给界面的规则表：预设带示例，分章模式只给名字（表达式是内部细节）。"""
    return {
        "chapter_patterns": [
            {"key": item.key, "name": item.name, "example": item.example}
            for item in CHAPTER_PATTERNS
        ],
        "default_pattern": DEFAULT_PATTERN_KEY,
        "dialog_presets": [
            {"name": item.name, "pattern": item.pattern, "replacement": item.replacement}
            for item in DIALOG_PRESETS
        ],
        "bracket_presets": [
            {"name": item.name, "pattern": item.pattern, "example": item.example}
            for item in BRACKET_PRESETS
        ],
    }


# --------------------------------------------------------------------------- #
# 文本处理
# --------------------------------------------------------------------------- #

#: 自定义正则一次最多处理这么多字：整本正文直接喂进去时先劝人分章，
#: 免得一条慢正则把请求线程拖上几分钟。
MAX_CUSTOM_CHARS = 200_000

#: 嵌套量词：``(a+)+``、``(.*)*``、``(\w+\s?)+`` 这类能指数级回退。
#: 静态判据只认形状，认不出 ``(a|a)+`` 这种"内容重复"——那不是正则写错，是写法问题。
_NESTED_QUANTIFIER = re.compile(r"\((?:\?:)?[^()]*[+*][^()]*\)\s*[+*{]")


@dataclass(frozen=True, slots=True)
class TextHit:
    """一处改动：哪条规则、改前、改后、所在段落与高亮位置。"""

    kind: str
    rule: str
    ordinal: int
    before: str
    after: str
    context: str = ""
    #: 命中在 ``context`` 里的起点与长度（按窗口裁过）——界面据此加高亮。
    context_at: int = -1
    context_len: int = 0

    @property
    def key(self) -> str:
        """一份文本内唯一：``dialog:移除双引号#3``。排除表用它点名。"""
        return f"{self.kind}:{self.rule}#{self.ordinal}"


@dataclass(frozen=True, slots=True)
class TextResult:
    text: str
    hits: tuple[TextHit, ...] = field(default=())


def _context(text: str, start: int, end: int, radius: int = 60) -> tuple[str, int, int]:
    """命中前后各取 ``radius`` 字，返回 ``(窗口, 命中在窗口里的下标, 可见长度)``。"""
    low = text.rfind("\n\n", 0, max(0, start))
    low = 0 if low < 0 else low + 2
    high = text.find("\n\n", end)
    high = len(text) if high < 0 else high
    win_low = max(low, start - radius)
    win_high = min(high, end + radius)
    prefix = "…" if win_low > low else ""
    suffix = "…" if win_high < high else ""
    window = prefix + text[win_low:win_high].replace("\n", " ") + suffix
    at = len(prefix) + (start - win_low)
    visible = max(0, min(end, win_high) - max(start, win_low))
    return window, at, visible


def _guard_custom(pattern: str, label: str) -> None:
    """自定义正则的护栏：嵌套量词直接拒。

    判据是形状（``(…+)+``），常见的指数回退都落在里面；认不出 ``(a|a)+``
    这种内容重复，但那种写法也很少出现在"删脚注"这类规则里。
    内置预设不走这里——它们是我们自己审过的。
    """
    if _NESTED_QUANTIFIER.search(pattern):
        raise ProducerError(
            f"自定义正则「{label}」里有嵌套量词（形如 (…+)+），遇到长文本会卡死；改写后再用。"
        )


def _compile(pattern: str, rule: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ProducerError(f"规则「{rule}」的正则写错了：{exc}") from None


def _apply(
    text: str,
    *,
    pattern: str,
    replacement: str,
    kind: str,
    rule: str,
    limit: int,
    hits: list[TextHit],
    exclude: frozenset[str],
) -> str:
    compiled = _compile(pattern, rule)
    out: list[str] = []
    cursor = 0
    for ordinal, match in enumerate(compiled.finditer(text), start=1):
        try:
            after = match.expand(replacement)
        except re.error as exc:
            raise ProducerError(f"规则「{rule}」的替换串写错了：{exc}") from None
        key = f"{kind}:{rule}#{ordinal}"
        if len(hits) < limit:
            context, at, visible = _context(text, match.start(), match.end())
            hits.append(
                TextHit(
                    kind=kind,
                    rule=rule,
                    ordinal=ordinal,
                    before=match.group(0),
                    after=after,
                    context=context,
                    context_at=at,
                    context_len=visible,
                )
            )
        if key in exclude:
            # 原样留下：**不推进 cursor**，这一段会跟着下一处一起被带出去。
            # 注意不能在这里先追加前缀——否则下一处再追加一次，同一段出现两遍。
            continue
        out.append(text[cursor : match.start()])
        out.append(after)
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def preprocess(
    text: str,
    *,
    dialog: list[str] | None = None,
    bracket: list[str] | None = None,
    custom_dialog: tuple[str, str] | None = None,
    custom_bracket: str = "",
    limit: int = 50,
    exclude: list[str] | None = None,
) -> TextResult:
    """按「查找替换 → 删除匹配」两条链清洗正文，并带回前若干处明细。

    预设按界面勾选顺序应用（顺序有意义：先摘双引号再删书名号与反过来做，
    遇到嵌套会得到不同结果）。``custom_*`` 是用户临时填的一条正则；
    ``exclude`` 是逐处排除表（:attr:`TextHit.key`），被点名的那些处原样保留。

    「全选」提交的就是 ``patterns.json`` 的表序，所以删除匹配那张表里**整行/整段
    删除必须排在局部删除之前**：``[1] 注释文字`` 这类行若先被「移除方括号数字」摘掉
    记号，「…整行」就再也没锚可认，注释文字会被念出来。手动勾选仍按点击顺序。
    """
    blocked = frozenset(exclude or ())
    dialog_names = list(dialog or [])
    bracket_names = list(bracket or [])
    presets = {item.name: item for item in DIALOG_PRESETS}
    removers = {item.name: item for item in BRACKET_PRESETS}
    unknown = [name for name in dialog_names if name not in presets] + [
        name for name in bracket_names if name not in removers
    ]
    if unknown:
        raise ProducerError(f"规则表里没有这条预设：{unknown[0]}。")

    if custom_dialog and custom_dialog[0]:
        _guard_custom(custom_dialog[0], "对白")
    if custom_bracket:
        _guard_custom(custom_bracket, "方括号")
    has_custom = bool((custom_dialog and custom_dialog[0]) or custom_bracket)
    if has_custom and len(text) > MAX_CUSTOM_CHARS:
        raise ProducerError(
            f"正文 {len(text)} 字，超过自定义正则一次能处理的 {MAX_CUSTOM_CHARS} 字；"
            f"先分章再处理。"
        )

    hits: list[TextHit] = []
    cleaned = text
    for name in dialog_names:
        preset = presets[name]
        cleaned = _apply(
            cleaned,
            pattern=preset.pattern,
            replacement=preset.replacement,
            kind="dialog",
            rule=name,
            limit=limit,
            hits=hits,
            exclude=blocked,
        )
    if custom_dialog and custom_dialog[0]:
        cleaned = _apply(
            cleaned,
            pattern=custom_dialog[0],
            replacement=custom_dialog[1],
            kind="dialog",
            rule="自定义",
            limit=limit,
            hits=hits,
            exclude=blocked,
        )
    for name in bracket_names:
        preset = removers[name]
        cleaned = _apply(
            cleaned,
            pattern=preset.pattern,
            replacement="",
            kind="bracket",
            rule=name,
            limit=limit,
            hits=hits,
            exclude=blocked,
        )
    if custom_bracket:
        cleaned = _apply(
            cleaned,
            pattern=custom_bracket,
            replacement="",
            kind="bracket",
            rule="自定义",
            limit=limit,
            hits=hits,
            exclude=blocked,
        )
    return TextResult(text=cleaned, hits=tuple(hits))


# --------------------------------------------------------------------------- #
# 分章
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SplitResult:
    chapters: tuple[tuple[str, str], ...]  # (标题, 正文)；正文**不含标题行**
    matched: bool
    pattern_key: str
    pattern_name: str
    warnings: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.chapters)


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def split_by_pattern(text: str, pattern_key: str) -> SplitResult:
    """按模式切章，返回 ``(标题, 正文)``。

    命中标题的章：正文里**不再含标题行**（标题单独存，拆句时不会把标题念出来）。
    没有标题的整篇 / 前言：第一条非空行当标题，正文仍是整段。
    """
    pattern = chapter_pattern(pattern_key)
    compiled = _compile(pattern.pattern, pattern.name)
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    chapters: list[tuple[str, str]] = []
    preface: list[str] = []
    first_found = False
    current: str | None = None
    body: list[str] = []
    pending: str | None = None

    def flush() -> None:
        nonlocal pending, body
        if current is None:
            return
        content = "\n".join(body).strip()
        if content:
            head = current
            extra = content
            if pending:
                head = _first_line(pending)
                extra = f"{pending.partition('\n')[2]}\n{current}\n\n{content}".strip()
                pending = None
            chapters.append((head, extra))
        elif pending:
            pending = f"{pending}\n{current}"
        else:
            pending = current
        body = []

    for line in lines:
        if compiled.match(line):
            if not first_found and preface:
                block = "\n".join(preface).strip()
                if block:
                    chapters.append((_first_line(block), block))
                preface = []
            first_found = True
            flush()
            current = line.strip()
        elif not first_found:
            preface.append(line)
        else:
            body.append(line)

    if current is not None:
        content = "\n".join(body).strip()
        if content:
            head = current
            extra = content
            if pending:
                head = _first_line(pending)
                extra = f"{pending.partition('\n')[2]}\n{current}\n\n{content}".strip()
            chapters.append((head, extra))
        elif pending:
            chapters.append((_first_line(pending), f"{pending}\n{current}"))
        else:
            # 末尾单个悬空标题：旧版静默丢掉这一行，这里补成一章。
            chapters.append((current, ""))

    if not first_found:
        block = "\n".join(preface).strip()
        if block:
            chapters = [(_first_line(block), block)]

    warnings: list[str] = []
    if not chapters:
        warnings.append("没有切出任何章节：正文是空的。")
    elif not first_found:
        warnings.append(f"没有匹配到「{pattern.name}」的章节标题，整篇按一章处理。")

    return SplitResult(
        chapters=tuple(chapters),
        matched=first_found,
        pattern_key=pattern.key,
        pattern_name=pattern.name,
        warnings=tuple(warnings),
    )


def count_chars(text: str) -> int:
    """去掉所有空白后计长（旧版口径，用于守恒对账）。"""
    return len(re.sub(r"\s+", "", text or ""))


def check_conservation(source: str, chapters: list[tuple[str, str]]) -> dict:
    """章节合计 vs 原文：偏差超过 1% 就值得说一句。"""
    source_chars = count_chars(source)
    chapter_chars = sum(count_chars(title) + count_chars(body) for title, body in chapters)
    deviation = abs(chapter_chars - source_chars) / source_chars if source_chars else 0.0
    return {
        "source_chars": source_chars,
        "chapter_chars": chapter_chars,
        "deviation": round(deviation, 4),
        "ok": deviation <= 0.01,
    }
