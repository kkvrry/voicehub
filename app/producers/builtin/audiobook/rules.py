"""分章与拆句规则：纯函数，不碰磁盘与网络。

只保留制作流程需要的最小规则集：
- 分章：Markdown 标题优先，其次中文/英文章节标题行；都没有则整本一章。
- 拆句：按段落与句末标点切分，**保留原文**，按引号标注叙述/对白。

拆句不改写、不删除任何文字：一句话属于旁白还是对白，只由它有没有选中写法的
引号决定，真名与情绪留给分析阶段回填。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DIALOGUE",
    "NARRATOR",
    "QUOTE_STYLES",
    "SENTENCE_MARKS",
    "SPLIT_MODES",
    "SPLIT_MODE_NAMES",
    "SplitOptions",
    "check_split",
    "is_broken",
    "split_chapters",
    "split_rows",
]

#: 坏源留下的残渣：替换符、控制字符，以及没被解析掉的属性碎片。
#: 行表的「需校对」按这一句判；章节栏的缺字灯也用它，两处只看这一份判据。
_BROKEN = re.compile(r'[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]|[A-Za-z-]{2,20}="[^"]{0,80}"')


def is_broken(text: str) -> bool:
    """这行正文里有没有坏源留下的残渣。

    两个分支各要求一个显眼的字符：属性碎片必须带 ASCII 双引号，替换符与控制字符
    各自是单独的字符集。三条都不成立时正则不可能命中，直接放过——行表每渲染一次
    就按行调它，实测快路径 0.2 µs、正则 0.94 µs。
    """
    if not text:
        return False
    if '"' not in text and "\ufffd" not in text and text.isprintable():
        return False
    return bool(_BROKEN.search(text))

#: 两个占位说话人：拆句只写它们，真名由分析阶段回填。
DIALOGUE = "对话"
NARRATOR = "旁白"

_MD_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
_CN_HEADING = re.compile(
    r"^\s*(第[0-9零一二两三四五六七八九十百千万]+[章回节卷篇部集].{0,40}"
    r"|序章.{0,20}|楔子.{0,20}|引子.{0,20}|终章.{0,20}|尾声.{0,20}|后记.{0,20})\s*$",
    re.MULTILINE,
)
SENTENCE_MARKS = "。；！？!?…"
_CLOSERS = "”」』\"'’）)》"
#: 句末终止符（半角的也认，英文书同样用得上）。
_ENDERS = "。；？！!?"
#: 分句标点：起始符紧跟在它后面，说明这里开的是**新的一句台词**（``他说：“……”``）。
_CLAUSE_BREAKS = "，、：,:"
#: 「文字」——判行内强调 / 引用 / 术语时都靠它。
_WORD = re.compile(r"[\w\u4e00-\u9fff]")


def _blocks(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    matches = list(pattern.finditer(text))
    if len(matches) < 2:
        return []
    out: list[tuple[str, str]] = []
    preface = text[: matches[0].start()].strip()
    if preface:
        out.append(("前言", preface))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(1).strip()
        body = text[match.end() : end].strip()
        if body:
            out.append((title, body))
    return out


def split_chapters(text: str) -> list[tuple[str, str]]:
    """源文本 → ``[(标题, 正文)]``；识别不到章节标题时整本作为一章。"""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not source:
        return []
    chapters = _blocks(source, _MD_HEADING) or _blocks(source, _CN_HEADING)
    if chapters:
        return chapters
    return [("全文", source)]


SPLIT_MODES: tuple[str, ...] = ("rule", "paragraph", "none")
SPLIT_MODE_NAMES = {"rule": "按句", "paragraph": "按段落", "none": "不拆"}


@dataclass(frozen=True, slots=True)
class QuoteStyle:
    """一种对白引号写法；``auto`` 不指定具体符号。"""

    key: str
    label: str
    open: str = ""
    close: str = ""


#: 默认只认中文双引号：一本书通常只用一种写法，自动混认容易误判。
DEFAULT_QUOTE = "cn_double"
QUOTE_STYLES: tuple[QuoteStyle, ...] = (
    QuoteStyle("cn_double", "中文双引号 “ ”", "“", "”"),
    QuoteStyle("corner", "直角引号 「 」", "「", "」"),
    QuoteStyle("double_corner", "双直角引号 『 』", "『", "』"),
    QuoteStyle("en_double", "英文双引号 \" \"", '"', '"'),
    QuoteStyle("cn_single", "中文单引号 ‘ ’", "‘", "’"),
    QuoteStyle("auto", "自动识别（混用易误判）"),
)
QUOTE_STYLE_MAP = {style.key: style for style in QUOTE_STYLES}


@dataclass(frozen=True, slots=True)
class SplitOptions:
    """拆句选项：模式 / 是否识别对白 / 引号写法 / 单行上限 / 短旁白阈值。

    ``quote`` 是 ``QUOTE_STYLES`` 的键；默认只认中文双引号。若一本书混用多种
    引号，请显式选定一种，避免自动识别把旁白里的其它引号也当成对白。
    """

    mode: str = "rule"
    dialogue: bool = True
    quote: str = DEFAULT_QUOTE
    max_chars: int = 200
    min_narration_chars: int = 4


def _quote_pairs(options: SplitOptions) -> tuple[tuple[str, str], ...]:
    """这次要认的引号对；关闭对话识别时一对都没有。"""
    if not options.dialogue:
        return ()
    style = QUOTE_STYLE_MAP.get(options.quote)
    if style is None or style.key == "auto":
        # 自动模式：几种常见写法一起认。英文单引号不在此列——撇号会被当成引号。
        return (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'))
    if not style.open:
        return ()
    return ((style.open, style.close or style.open),)


def _prev_char(text: str, index: int) -> str:
    """``index`` 之前紧邻的非空白字符（没有就是空串）。"""
    cursor = index - 1
    while cursor >= 0 and text[cursor].isspace():
        cursor -= 1
    return text[cursor] if cursor >= 0 else ""


def _starts_new_dialogue(text: str, index: int) -> bool:
    """起始符前是分句标点 → 这里开的是**新的一句台词**（``他说：“……”``）。"""
    previous = _prev_char(text, index)
    return bool(previous) and previous in _CLAUSE_BREAKS


def _quoted_content(
    text: str, open_idx: int, open_sym: str, close_sym: str
) -> tuple[str, int] | None:
    """从 ``open_idx`` 起那一对引号的 ``(内容, 闭合符之后的下标)``；没闭合给 None。"""
    inner = open_idx + len(open_sym)
    close_idx = text.find(close_sym, inner)
    if close_idx == -1:
        return None
    return text[inner:close_idx], close_idx + len(close_sym)


def _is_quoted_term(text: str, open_idx: int, open_sym: str, close_sym: str) -> bool:
    """这一对引号是在给一个词加引号（术语 / 专名 / 反讽），不是有人在说话。

    三条同时成立才算：引号里没有终止符、没有分句标点、末尾是文字——**判据就是
    "引号里有没有句读"**，话总得有句号或逗号收尾，术语不用。另保留一条位置条件：
    闭合符后面还要有内容（单独一个「好」和"只说了这一个词"形状一样，保守当台词）；
    被文字夹住的引号另有行内强调一道拦截。
    """
    found = _quoted_content(text, open_idx, open_sym, close_sym)
    if found is None:
        return False
    content, after_close = found
    if not content:
        return False
    if any(char in _ENDERS or char == "…" for char in content):
        return False
    if any(char in _CLAUSE_BREAKS for char in content):
        return False
    if _WORD.match(content[-1]) is None:
        return False
    return after_close < len(text)


def _is_inline_emphasis(text: str, open_idx: int, open_sym: str, close_sym: str) -> bool:
    """闭合符两侧（跳过空白）都是文字 → 行内强调（``所谓“重要”的事``）。"""
    previous = _prev_char(text, open_idx)
    if not previous or _WORD.match(previous) is None:
        return False
    close_idx = text.find(close_sym, open_idx + len(open_sym))
    if close_idx == -1:
        return False
    after = close_idx + len(close_sym)
    while after < len(text) and text[after].isspace():
        after += 1
    return after < len(text) and _WORD.match(text[after]) is not None


def _is_dialogue(text: str, options: SplitOptions) -> bool:
    """这一句是不是台词：以选中写法的引号开头、能找到闭合符、且不是给词加引号。

    只认句首——``他低声说“我不去”，然后就走了。`` 仍是旁白；引号没闭合也算旁白，
    闭合符找不到就不敢断言这是台词。
    """
    value = (text or "").strip()
    for open_sym, close_sym in _quote_pairs(options):
        if value.startswith(open_sym) and value.find(close_sym, len(open_sym)) != -1:
            return not _is_quoted_term(value, 0, open_sym, close_sym)
    return False


def _absorb_enders(
    text: str, index: int, buffer: list[str], closers: tuple[str, ...]
) -> int:
    """吸收紧随终止符的省略号 / 空白 / 闭合符，返回新游标。"""
    length = len(text)
    while True:
        if text.startswith("……", index):
            buffer.append("……")
            index += 2
            continue
        if text.startswith("…", index):
            buffer.append("…")
            index += 1
            continue
        if index < length and text[index] in _ENDERS:
            buffer.append(text[index])
            index += 1
            continue
        if index < length and text[index].isspace():
            buffer.append(text[index])
            index += 1
            continue
        closer = next((item for item in closers if text.startswith(item, index)), "")
        if closer:
            buffer.append(closer)
            index += len(closer)
            continue
        return index


def _split_with_quotes(text: str, options: SplitOptions) -> list[str]:
    """一段 → 几句：台词单独成句，提示语与旁白留在句首那一句里。

    逐字符扫描（旧版「对白优先」扫描器）：
    引号里**有句读** → 台词，从引号前切开，``老人说道：`` 单独成一句；
    引号里没有句读、或紧跟在文字后面（``他低声说“我不去”``）→ 留在旁白里；
    台词内部的句号不切句，一个引号对就是一句。台词里的字一个不动。
    """
    pairs = _quote_pairs(options)
    # 同一字符开闭的引号（英文双引号）不能当"句末闭合符"吞掉，否则会吃掉下一个开符。
    closers = tuple({close for open_sym, close in pairs if open_sym != close})
    sentences: list[str] = []
    buffer: list[str] = []
    in_dialogue = False
    close_sym = ""
    index = 0
    length = len(text)

    while index < length:
        if in_dialogue:
            if close_sym and text.startswith(close_sym, index):
                buffer.append(close_sym)
                index += len(close_sym)
                index = _absorb_enders(text, index, buffer, ())
                sentence = "".join(buffer).strip()
                if sentence:
                    sentences.append(sentence)
                buffer = []
                in_dialogue = False
                continue
            buffer.append(text[index])
            index += 1
            continue

        pair = next(((o, c) for o, c in pairs if text.startswith(o, index)), None)
        if pair is not None:
            open_sym, candidate_close = pair
            if (
                _is_inline_emphasis(text, index, open_sym, candidate_close)
                or _is_quoted_term(text, index, open_sym, candidate_close)
                or (
                    _WORD.search("".join(buffer)) is not None
                    and not _starts_new_dialogue(text, index)
                )
            ):
                buffer.append(open_sym)
                index += len(open_sym)
                continue
            head = "".join(buffer).strip()
            if head:
                sentences.append(head)
            buffer = [open_sym]
            index += len(open_sym)
            close_sym = candidate_close
            in_dialogue = True
            continue

        if text.startswith("……", index):
            buffer.append("……")
            index += 2
        elif text.startswith("…", index):
            buffer.append("…")
            index += 1
        elif text[index] in _ENDERS:
            buffer.append(text[index])
            index += 1
        else:
            buffer.append(text[index])
            index += 1
            continue
        index = _absorb_enders(text, index, buffer, closers)
        sentence = "".join(buffer).strip()
        if sentence:
            sentences.append(sentence)
        buffer = []

    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return _merge_punct_only(sentences)


def _merge_punct_only(sentences: list[str]) -> list[str]:
    """纯标点句并回相邻句（拆出来的孤零零的 ``。”`` 不该单独成行，也不删字）。"""
    merged: list[str] = []
    pending = ""
    for sentence in sentences:
        if not _WORD.search(sentence):
            if merged:
                merged[-1] += sentence
            else:
                pending += sentence
            continue
        merged.append(pending + sentence)
        pending = ""
    if pending:
        if merged:
            merged[-1] += pending
        else:
            merged.append(pending)
    return merged


def _sentence_units(text: str) -> list[str]:
    """按句末标点切出句子单元；省略号、右引号与空白跟着前一句走。"""
    units: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        buffer.append(char)
        index += 1
        if char in SENTENCE_MARKS:
            index = _absorb_enders(text, index, buffer, tuple(_CLOSERS))
            units.append("".join(buffer))
            buffer = []
    if buffer:
        units.append("".join(buffer))
    return units


def _cut_long_unit(text: str, limit: int) -> tuple[str, str]:
    """单句超过上限：优先在逗号/空格处断，找不到断点就按上限硬切。"""
    window = text[: max(1, limit)]
    cut = -1
    for mark in "，、；,; ":
        cut = max(cut, window.rfind(mark))
    if cut < 0:
        cut = len(window) - 1
    return text[: cut + 1], text[cut + 1 :]


def _wrap_long(text: str, limit: int) -> list[str]:
    """超过上限的文本按上限打包：优先把整句装进一行，装不下才在句读处断开。

    ``max_chars`` 是**每行的容量**，不是"一句一行"：长对白会先尽量装满，
    只有单句本身超过上限时才继续在逗号/空格处切。这样一段几百字的台词
    不会碎成十几行，引擎单次上限也仍然守得住。
    """
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    buffer = ""
    for unit in _sentence_units(text):
        while len(unit) > limit:
            if buffer.strip():
                pieces.append(buffer.strip())
                buffer = ""
            head, unit = _cut_long_unit(unit, limit)
            if head.strip():
                pieces.append(head.strip())
        if buffer and len(buffer) + len(unit) > limit:
            pieces.append(buffer.strip())
            buffer = ""
        buffer += unit
    if buffer.strip():
        pieces.append(buffer.strip())
    return _merge_punct_only(pieces)


def _make_rows(text: str, options: SplitOptions, cap: int) -> list[dict[str, str]]:
    """一句 → 行；超长时断成几行，归属跟着整句走（切开的半句仍是同一类）。"""
    value = text.strip()
    kind = "dialogue" if _is_dialogue(value, options) else "narration"
    speaker = DIALOGUE if kind == "dialogue" else NARRATOR
    return [
        {"kind": kind, "speaker": speaker, "text": part.strip()}
        for part in _wrap_long(value, cap)
        if part.strip()
    ]


def _pack_sentences(pieces: list[str], options: SplitOptions) -> list[str]:
    """同一段内把相邻旁白并到 ``max_chars``；台词各自成句，不并。"""
    if not pieces:
        return []
    cap = max(1, int(options.max_chars or 200))
    kinds = [_is_dialogue(piece, options) for piece in pieces]
    packed = [pieces[0]]
    for index in range(1, len(pieces)):
        joinable = not kinds[index] and not kinds[index - 1]
        if joinable and len(packed[-1]) + len(pieces[index]) <= cap:
            packed[-1] += pieces[index]
        else:
            packed.append(pieces[index])
    return packed


def _meaningful_len(text: str) -> int:
    return len(re.sub(r"[^\w\u4e00-\u9fff]", "", text))


def split_rows(
    text: str, *, options: SplitOptions | None = None
) -> list[dict[str, str]]:
    """章节正文 → 行（``kind`` / ``speaker`` / ``text``）。

    ``speaker`` 只写两个**占位值**：「对话」「旁白」，真名由分析阶段回填。
    ``options.mode`` 决定粒度：按句 / 按段落（超上限才拆）/ 不拆（整章一行）。

    **只切不改字**：台词单独成行，提示语与旁白留在它自己那一行
    （``老人说道：“你来了。”`` → 旁白「老人说道：」+ 对白「“你来了。”」）。
    引号里有没有句读决定它是不是台词：给词加引号、行内强调、紧跟在文字后面的
    引用都留在旁白里，不会被送去对白的音色。
    """
    opts = options or SplitOptions()
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return []
    if opts.mode == "none":
        # 整章一行只可能有一个说话人：固定旁白，也不拿引号去猜对白。
        return [{"kind": "narration", "speaker": NARRATOR, "text": source.strip()}]

    cap = max(1, int(opts.max_chars or 200))
    paragraphs = [line.strip() for line in source.split("\n") if line.strip()]
    paragraph_rows: list[list[dict[str, str]]] = []
    for paragraph in paragraphs:
        pieces = _split_with_quotes(paragraph, opts)
        if (
            opts.mode == "paragraph"
            and len(paragraph) <= cap
            and not any(_is_dialogue(piece, opts) for piece in pieces)
        ):
            # 没有台词（术语引号不算）：这一段可以整段念，不被引号切开。
            paragraph_rows.append(
                [{"kind": "narration", "speaker": NARRATOR, "text": paragraph}]
            )
            continue
        rows: list[dict[str, str]] = []
        for piece in _pack_sentences(pieces, opts):
            rows.extend(_make_rows(piece, opts, cap))
        paragraph_rows.append(rows)

    # 短旁白并回上一句，只在段内做、不跨段；对白行不参与合并。
    merged: list[dict[str, str]] = []
    for rows in paragraph_rows:
        local: list[dict[str, str]] = []
        for row in rows:
            short = (
                row["kind"] == "narration"
                and _meaningful_len(row["text"]) < opts.min_narration_chars
            )
            if (
                short
                and local
                and local[-1]["kind"] == "narration"
                and len(local[-1]["text"]) + len(row["text"]) <= cap
            ):
                local[-1]["text"] += row["text"]
                continue
            local.append(row)
        merged.extend(local)
    return merged


def _quote_warnings(source: str, options: SplitOptions) -> list[str]:
    """按实际选中的写法查配对；自动模式额外提醒「一本书混用多种引号」。"""
    warnings: list[str] = []
    style = QUOTE_STYLE_MAP.get(options.quote)
    if style is None or style.key == "auto":
        present = [
            item
            for item in QUOTE_STYLES
            if item.key != "auto" and (item.open in source or item.close in source)
        ]
        if len(present) > 1:
            labels = "、".join(item.label for item in present)
            warnings.append(
                f"正文同时出现多种对白引号（{labels}），自动识别可能误判；建议选定一种。"
            )
        styles = present
    else:
        styles = [style]
    for item in styles:
        if not item.open:
            continue
        opens = source.count(item.open)
        closes = source.count(item.close)
        if item.open == item.close:
            if opens % 2:
                warnings.append(f"引号不配对：{item.label}出现了 {opens} 次，对白可能切错。")
        elif opens != closes:
            warnings.append(f"引号不配对：{item.label} {opens}/{closes}，对白可能切错。")
    return warnings


def check_split(
    source: str, rows: list[dict[str, str]], options: SplitOptions
) -> dict[str, object]:
    """拆句质检：引号配对、行数分布、行合计与正文对不对得上。

    最后一条是"只切不改字"的守门人：拆句不删字，所以各行去掉空白后必须与正文
    等长；一旦以后有人在这里加了改写或过滤，真实书上当场就能看见。
    """
    warnings: list[str] = []
    if options.dialogue and options.mode != "none":
        warnings.extend(_quote_warnings(source, options))
    if not rows:
        warnings.append("没有切出任何行。")
    source_chars = len(re.sub(r"\s+", "", source or ""))
    rows_chars = sum(len(re.sub(r"\s+", "", row["text"])) for row in rows)
    if rows and rows_chars != source_chars:
        warnings.append(
            f"行合计 {rows_chars} 字与正文 {source_chars} 字对不上，拆句可能丢了字。"
        )
    dialogue = sum(1 for row in rows if row["kind"] == "dialogue")
    return {
        "rows": len(rows),
        "dialogue": dialogue,
        "narration": len(rows) - dialogue,
        "chars": source_chars,
        "rows_chars": rows_chars,
        "warnings": warnings,
    }
