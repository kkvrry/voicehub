"""书稿解析：TXT / Markdown / EPUB / MOBI / AZW3 → 章节列表。

只做"取出正文并切成章"这一件事，不碰存储、不碰界面。外部依赖为零：
EPUB 是 ZIP + XML + HTML，MOBI 是 PalmDB + PalmDOC 压缩，KF8（AZW3）是
同一容器里的第二段正文加索引，标准库都能解。
"""

from __future__ import annotations

import io
import posixpath
import re
import struct
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree

from app.producers.base import ProducerError

__all__ = ["SUPPORTED", "read_book"]

#: 能直接导入的扩展名。AZW/PRC 与 MOBI 同为 PalmDB 容器；AZW3 走 KF8 正文。
SUPPORTED = (
    ".txt", ".md", ".markdown", ".epub", ".mobi", ".azw3", ".azw", ".prc",
)

_BLOCKS = {
    "p", "div", "br", "li", "tr", "section", "article", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "mbp:pagebreak",
}
_SKIP = {"script", "style", "head", "title", "svg", "math"}

#: 脚注引用在正文里留下的记号形如 ``[1]`` / ``①`` / 上标的 ``1``：整条都不念。
_NOTE_MARK = re.compile(r"^[\s\[\(（【]*(?:\d{1,3}|[①-⑳]|[¹²³⁴⁵⁶⁷⁸⁹⁰]+)[\s\]\)）】]*$")

#: 自闭合标签没有结束事件，不能算进"套了几层"——脚注的 `<a><img/></a>` 就是这么写的。
_VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "source", "wbr"}

#: 脚注/尾注的**定义块**（`<aside epub:type="footnote">` 之类）：整块不念。
#: 引用标记走 `_NOTE_MARK`，这里管的是正文后面那截注释文字。
_NOTE_ELEMENTS = {
    "footnote", "endnote", "rearnote",
    "footnotes", "endnotes", "rearnotes",
    "doc-footnote", "doc-endnote", "doc-rearnote",
}

#: 坏源留下的残渣：控制字符、替换符，以及没被解析掉的标签与属性。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG = re.compile(r"<[^<>\n]{0,120}>")
_ATTR = re.compile(r'[A-Za-z-]{1,20}="[^"<>\n]{0,120}"')
#: 残标被删后剩下的尖括号。「中文里的 < >」只可能是残渣，日文/英文正文不用它。
_STRAY = re.compile(r"(?<=[^\x00-\x7f])[<>]+|[<>]+(?=[^\x00-\x7f])")


def _scrub(text: str, *, markup: bool = False) -> str:
    """清掉转换失败留在正文里的残渣。

    ``markup`` 只给纯文本书稿开：那里残留的 ``<p>`` / ``width="2"`` 是转换掉出来的
    碎片，不是正文；EPUB 的标签在解析时已经去掉了，别再拿它去删正文里的 ``<``。
    替换符代表**已经丢掉的字**，删掉只是不再念出方框，补不回来。
    """
    text = _CONTROL.sub("", text).replace("\ufffd", "")
    if markup:
        text = _STRAY.sub("", _ATTR.sub("", _TAG.sub("", text)))
    return text


class _HtmlText(HTMLParser):
    """HTML → 纯文本；顺手记下第一个 h1–h3 作为章节标题。

    脚注引用（``<a href="#fn1">[1]</a>``、``<sup>1</sup>``）不是正文：标记丢掉，
    它前面那格空格一并收掉，免得留下「戈罗赫王 的事」。脚注的**定义块**
    （``<aside epub:type="footnote">``）整块不念——念出来就是正文后面挂一串尾巴。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        #: 正在丢弃的元素栈（script/style/head 与脚注定义块）：里面的字一律不念。
        self._muted: list[str] = []
        self._heading: list[str] | None = None
        self.heading = ""
        #: 可能是脚注标记的文本，结算前不入正文（判据要看完整文字，所以先缓冲）。
        self._suspect: list[str] | None = None
        self._suspect_depth = 0
        self._suspect_drop = False
        #: 刚摘掉一个标记：留着的那格空格等下一个字到了再决定收不收
        #: （中文收，英文留——``see note [1] for`` 不能粘成 ``notefor``）。
        self._trim_space = False

    def _emit(self, data: str) -> None:
        if self._trim_space and data:
            if not data[0].isascii() and self._chunks:
                self._chunks[-1] = self._chunks[-1].rstrip(" \t\u00a0")
            self._trim_space = False
        if self._heading is not None:
            self._heading.append(data)
        self._chunks.append(data)

    def _open_suspect(self, drop: bool = False) -> None:
        self._suspect = []
        self._suspect_depth = 0
        self._suspect_drop = drop

    def _note_kind(self, fields: dict[str, str]) -> str:
        """``<a>`` 是不是脚注引用：``noteref`` 明说了，``#`` 锚点多半是同页脚注。"""
        kind = f"{fields.get('epub:type') or ''} {fields.get('role') or ''}"
        if "noteref" in kind:
            return "noteref"
        return "anchor" if (fields.get("href") or "").startswith("#") else ""

    @staticmethod
    def _is_note_block(attrs) -> bool:
        kinds = f"{dict(attrs).get('epub:type') or ''} {dict(attrs).get('role') or ''}".split()
        return any(item in _NOTE_ELEMENTS for item in kinds)

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._muted:
            if tag not in _VOID:
                self._muted.append(tag)
            return
        if tag in _SKIP or self._is_note_block(attrs):
            if tag not in _VOID:
                self._muted.append(tag)
            return
        if self._suspect is not None:
            if tag not in _VOID:
                self._suspect_depth += 1
            return
        if tag in ("h1", "h2", "h3") and self._heading is None:
            self._heading = []
        if tag in _BLOCKS:
            self._chunks.append("\n")
        if tag == "sup":
            self._open_suspect()
        elif tag == "a":
            kind = self._note_kind(dict(attrs))
            if kind:
                self._open_suspect(kind == "noteref")

    def handle_endtag(self, tag: str) -> None:
        if self._muted:
            if tag in self._muted:
                while self._muted and self._muted.pop() != tag:
                    pass
            return
        if self._suspect is not None:
            if self._suspect_depth:
                self._suspect_depth -= 1
                return
            text = "".join(self._suspect)
            drop = self._suspect_drop or bool(_NOTE_MARK.match(text))
            self._suspect = None
            self._suspect_drop = False
            if drop:
                self._trim_space = True
            elif text:
                self._emit(text)
            return
        if tag in ("h1", "h2", "h3") and self._heading is not None:
            text = " ".join("".join(self._heading).split())
            if text and not self.heading:
                self.heading = text
            self._heading = None
        if tag in _BLOCKS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._muted:
            return
        if self._suspect is not None:
            self._suspect.append(data)
            return
        self._emit(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_text(markup: str, *, title_tag: bool = True) -> tuple[str, str]:
    """HTML → (标题, 正文)。标题取第一个 h1–h3；``title_tag`` 时才回退到 <title>。"""
    parser = _HtmlText()
    # HTMLParser 对残缺标签是容错的：喂进去的字符串不会因为标签不闭合而中断。
    parser.feed(markup)
    heading = parser.heading or (_title_tag(markup) if title_tag else "")
    return heading, parser.text()


def _title_tag(markup: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())


def _decode(data: bytes, encoding: str = "") -> str:
    """文本解码：认 BOM 与声明，其余按 UTF-8 → GB18030 回退。"""
    if encoding:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    for codec in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _epub_chapters(data: bytes) -> list[tuple[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ProducerError("EPUB 文件损坏：不是有效的 ZIP 包。") from None
    names = set(archive.namelist())
    if "META-INF/container.xml" not in names:
        raise ProducerError("EPUB 缺少 META-INF/container.xml，不是标准书稿。")
    if "META-INF/encryption.xml" in names:
        raise ProducerError("这本 EPUB 带加密（DRM），无法导入。")

    container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find(".//{*}rootfile")
    opf_path = unquote(rootfile.get("full-path", "")) if rootfile is not None else ""
    if not opf_path or opf_path not in names:
        raise ProducerError("EPUB 缺少内容清单（OPF），无法定位章节。")

    package = ElementTree.fromstring(archive.read(opf_path))
    manifest = {
        item.get("id"): item.get("href", "")
        for item in package.findall(".//{*}manifest/{*}item")
    }
    spine = [
        item.get("idref")
        for item in package.findall(".//{*}spine/{*}itemref")
        if item.get("idref") and (item.get("linear") or "").lower() != "no"
    ]
    base = PurePosixPath(opf_path).parent
    # 书名给的目录名优先：h1 常常只有「第二章」，目录里才有「第一部」这层。
    titles = _epub_titles(archive, package, base, names)
    # 导航页本身是目录，不是正文：EPUB3 常把它也塞进 spine。
    toc_pages = {
        _join(base, item.get("href", ""))
        for item in package.findall(".//{*}manifest/{*}item")
        if "nav" in (item.get("properties") or "").split()
    }

    chapters: list[tuple[str, str]] = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        path = _join(base, href)
        if path not in names or path in toc_pages:
            continue
        markup = _decode(archive.read(path))
        heading, text = _html_text(markup)
        text = _scrub(text)
        # 标题已经单独取了，正文里不要再念一遍。
        if heading and text.startswith(heading):
            text = text[len(heading) :].lstrip("\n")
        if not text:
            continue
        title = titles.get(path) or heading or f"第 {len(chapters) + 1} 章"
        chapters.append((title, text))
    if not chapters:
        raise ProducerError("EPUB 里没有可读的正文。")
    return chapters


def _join(base: PurePosixPath, href: str) -> str:
    """href（带 # 片段、可能含 ../ 或百分号编码）→ ZIP 内的规范路径。"""
    clean = unquote(href.split("#", 1)[0]).strip()
    if not clean:
        return ""
    if clean.startswith("/"):
        return posixpath.normpath(clean.lstrip("/"))
    return posixpath.normpath(posixpath.join(base.as_posix(), clean))


class _NavList(HTMLParser):
    """EPUB3 导航页里 ``nav[epub:type=toc]`` 的条目 → (文件, 层级标题)。

    层级由 ``<ol>`` 的嵌套给出，标题按「父 子」拼：只取 <a> 的文字，
    目录里那些说明性的旁支（页码、脚注）不参与。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._nav = 0
        self._labels: list[str] = []
        self._href = ""
        self._text: list[str] | None = None
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        fields = dict(attrs)
        if tag == "nav":
            kind = f"{fields.get('epub:type') or ''} {fields.get('role') or ''}"
            if self._nav or "toc" in kind.split():
                self._nav += 1
            return
        if not self._nav:
            return
        if tag == "ol":
            self._labels.append("")
        elif tag == "a" and fields.get("href"):
            self._href = fields["href"]
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self._nav:
            self._nav -= 1
            return
        if not self._nav:
            return
        if tag == "ol" and self._labels:
            self._labels.pop()
        elif tag == "a" and self._text is not None:
            label = " ".join("".join(self._text).split())
            self._text = None
            if not label:
                return
            if self._labels:
                self._labels[-1] = label
            self.links.append((self._href, " ".join(item for item in self._labels if item)))

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)


def _ncx_titles(archive, path: str, names: set[str]) -> dict[str, str]:
    """NCX（EPUB2 目录）：navPoint 树 → {文件: 层级标题}。"""
    if path not in names:
        return {}
    try:
        root = ElementTree.fromstring(archive.read(path))
    except ElementTree.ParseError:
        return {}
    base = PurePosixPath(path).parent
    titles: dict[str, str] = {}

    def walk(parent, prefix: str) -> None:
        for point in parent.findall("{*}navPoint"):
            label = " ".join((point.findtext("{*}navLabel/{*}text") or "").split())
            full = f"{prefix} {label}".strip() or prefix
            content = point.find("{*}content")
            target = _join(base, content.get("src", "")) if content is not None else ""
            if target and full:
                titles.setdefault(target, full)
            walk(point, full)

    nav_map = root.find("{*}navMap")
    if nav_map is not None:
        walk(nav_map, "")
    return titles


def _nav_titles(archive, path: str, names: set[str]) -> dict[str, str]:
    if path not in names:
        return {}
    parser = _NavList()
    parser.feed(_decode(archive.read(path)))
    base = PurePosixPath(path).parent
    titles: dict[str, str] = {}
    for href, title in parser.links:
        target = _join(base, href)
        if target:
            titles.setdefault(target, title)
    return titles


def _epub_titles(archive, package, base: PurePosixPath, names: set[str]) -> dict[str, str]:
    """书内目录 → {文件路径: 章节名}；两代格式都读，导航页优先。"""
    nav = ""
    ncx = ""
    for item in package.findall(".//{*}manifest/{*}item"):
        href = item.get("href") or ""
        properties = (item.get("properties") or "").split()
        if not nav and "nav" in properties:
            nav = href
        if not ncx and (item.get("media-type") or "") == "application/x-dtbncx+xml":
            ncx = href

    titles: dict[str, str] = {}
    if ncx:
        titles.update(_ncx_titles(archive, _join(base, ncx), names))
    if nav:
        titles.update(_nav_titles(archive, _join(base, nav), names))
    return titles


def _palmdoc(data: bytes) -> bytes:
    """PalmDOC 解压（0x80–0xBF 是"回看 distance 字节、复制 length 个"）。"""
    out = bytearray()
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        index += 1
        if 0x01 <= byte <= 0x08:
            out += data[index : index + byte]
            index += byte
        elif byte == 0x00 or 0x09 <= byte <= 0x7F:
            out.append(byte)
        elif byte >= 0xC0:
            out.append(0x20)
            out.append(byte ^ 0x80)
        else:
            if index >= length:
                break
            pair = (byte << 8) | data[index]
            index += 1
            distance = (pair >> 3) & 0x07FF
            count = (pair & 0x07) + 3
            if distance == 0 or distance > len(out):
                break
            for _ in range(count):
                out.append(out[-distance])
    return bytes(out)


#: PalmDB 里分隔 MOBI7 与 KF8 两部分的记录：8 字节，内容就是 "BOUNDARY"。
_BOUNDARY = b"BOUNDARY"

#: KindleGen 在每个原始 XHTML 文件开头放一个硬分页；MOBI7 靠它划章。
_PAGEBREAK = re.compile(r"<mbp:pagebreak\b[^>]*>", re.IGNORECASE)
#: 没有分页标记时退回标题：取最上层、至少出现两次的那一级。
_HEADING = re.compile(r"<h([1-3])\b[^>]*>.*?</h\1\s*>", re.IGNORECASE | re.DOTALL)

_INDX_FIELDS = (
    "len", "nul1", "type", "gen", "start", "count", "code",
    "lng", "total", "ordt", "ligt", "nligt", "nctoc",
)


@dataclass(frozen=True, slots=True)
class _MobiHeader:
    compression: int
    text_length: int
    text_records: int
    encryption: int
    encoding: str
    flags: int
    version: int


def _palm_records(data: bytes) -> list[bytes]:
    """PalmDB → 记录列表；记录表里存的是绝对字节偏移。"""
    if len(data) < 78:
        raise ProducerError("电子书文件损坏：PalmDB 头不完整。")
    count = struct.unpack(">H", data[76:78])[0]
    if count < 2:
        raise ProducerError("电子书文件损坏：没有正文记录。")
    offsets = [
        struct.unpack(">I", data[78 + index * 8 : 82 + index * 8])[0]
        for index in range(count)
    ]
    offsets.append(len(data))
    records: list[bytes] = []
    for index in range(count):
        start, end = offsets[index], offsets[index + 1]
        if end < start or end > len(data):
            raise ProducerError("电子书文件损坏：记录偏移越界。")
        records.append(data[start:end])
    return records


def _mobi_header(records: list[bytes], index: int) -> _MobiHeader:
    book = records[index]
    if len(book) < 16:
        raise ProducerError("电子书文件损坏：第一条记录不完整。")
    compression, _, text_length, text_records, _, encryption = struct.unpack(
        ">HHIHHH", book[:14]
    )
    encoding = "utf-8"
    flags = 0
    version = 0
    if len(book) >= 40 and book[16:20] == b"MOBI":
        encoding = "utf-8" if struct.unpack(">I", book[28:32])[0] == 65001 else "cp1252"
        version = struct.unpack(">I", book[36:40])[0]
        if len(book) >= 0xF4:
            flags = struct.unpack(">H", book[0xF2:0xF4])[0]
    return _MobiHeader(
        compression, text_length, text_records, encryption, encoding, flags, version
    )


def _trailing_size(record: bytes) -> int:
    """尾部附加数据的长度：末尾 4 字节里的后向变长整数，长度含自己。"""
    value = 0
    for byte in record[-4:]:
        if byte & 0x80:
            value = 0
        value = (value << 7) | (byte & 0x7F)
    return value


def _trim_trailing(record: bytes, flags: int) -> bytes:
    """按 Extra Data Flags 摘掉记录尾部的附加数据。

    位 1–15 每置一位就是一条 ``<data><size>``；位 0 的多字节重叠夹在正文与
    其他附加数据之间，所以最后再摘。
    """
    for bit in range(1, 16):
        if not flags & (1 << bit):
            continue
        size = _trailing_size(record)
        if size <= 0 or size > len(record):
            break
        record = record[: len(record) - size]
    if flags & 1 and record:
        record = record[: len(record) - ((record[-1] & 0x03) + 1)]
    return record


def _raw_records(records: list[bytes], header: _MobiHeader, start: int = 0) -> bytes:
    """解压全部正文记录，返回 HTML 原文。"""
    if header.encryption:
        raise ProducerError("这本电子书带加密（DRM），无法导入。")
    if header.compression == 17480:
        raise ProducerError("这本电子书用了 HUFF/CDIC 压缩，暂不支持；先用 Calibre 转成 EPUB。")
    if header.compression not in (1, 2):
        raise ProducerError(f"电子书压缩方式 {header.compression} 不认识，无法导入。")
    parts: list[bytes] = []
    end = min(start + header.text_records, len(records) - 1)
    for index in range(start + 1, end + 1):
        record = _trim_trailing(records[index], header.flags)
        parts.append(_palmdoc(record) if header.compression == 2 else record)
    raw = b"".join(parts)
    return raw[: header.text_length] if 0 < header.text_length <= len(raw) else raw


def _kf8_header(records: list[bytes]) -> int:
    """KF8 头所在记录号；不是 KF8 返回 -1。"""
    if _mobi_header(records, 0).version >= 8:
        return 0
    for index, record in enumerate(records[:-1]):
        if record == _BOUNDARY:
            try:
                return index + 1 if _mobi_header(records, index + 1).version >= 8 else -1
            except ProducerError:
                return -1
    return -1


def _header_word(records: list[bytes], start: int, offset: int) -> int:
    """MOBI 头里的记录号字段；头不够长或未设置都按「没有」处理。"""
    data = records[start]
    if len(data) < offset + 4:
        return 0xFFFFFFFF
    return struct.unpack_from(">L", data, offset)[0]


def _vwi(data: bytes, offset: int) -> tuple[int, int]:
    """Mobipocket 变长正整数：每字节 7 位，最高位置 1 表示结束。"""
    value = 0
    consumed = 0
    while True:
        if offset + consumed >= len(data):
            raise ProducerError("电子书索引损坏：变长数值越界。")
        byte = data[offset + consumed]
        consumed += 1
        value = (value << 7) | (byte & 0x7F)
        if byte & 0x80:
            return consumed, value


def _indx_header(data: bytes) -> dict[str, int]:
    values = struct.unpack_from(">13L", data, 4)
    return dict(zip(_INDX_FIELDS, values))


def _tagx(data: bytes, offset: int) -> tuple[int, list[tuple[int, int, int, int]]]:
    """INDX 头里的 TAGX：控制字节数与标签定义。"""
    if data[offset : offset + 4] != b"TAGX":
        return 0, []
    first, control = struct.unpack_from(">LL", data, offset + 4)
    tags: list[tuple[int, int, int, int]] = []
    for position in range(offset + 12, min(offset + first, len(data)), 4):
        if position + 4 > len(data):
            break
        tags.append(tuple(data[position : position + 4]))
    return control, tags


def _tag_values(
    data: bytes,
    start: int,
    control_count: int,
    tags: list[tuple[int, int, int, int]],
) -> dict[int, list[int]]:
    """按 TAGX 解一条索引条目的标签值；控制字节定「有几个值」，值本身是变长数。"""
    cursor = start + control_count
    control_index = 0
    pending: list[tuple[int, int | None, int | None, int]] = []
    for tag, per_entry, mask, end_flag in tags:
        if end_flag == 1:
            control_index += 1
            continue
        if control_index >= control_count or start + control_index >= len(data):
            break
        value = data[start + control_index] & mask
        if not value:
            continue
        if value == mask:
            if mask.bit_count() > 1:
                consumed, size = _vwi(data, cursor)
                cursor += consumed
                pending.append((tag, None, size, per_entry))
            else:
                pending.append((tag, 1, None, per_entry))
        else:
            shift = (mask & -mask).bit_length() - 1
            pending.append((tag, value >> shift, None, per_entry))

    found: dict[int, list[int]] = {}
    for tag, count, size, per_entry in pending:
        values: list[int] = []
        if count is not None:
            for _ in range(count):
                for _ in range(per_entry):
                    consumed, value = _vwi(data, cursor)
                    cursor += consumed
                    values.append(value)
        else:
            consumed_total = 0
            while consumed_total < (size or 0):
                consumed, value = _vwi(data, cursor)
                cursor += consumed
                consumed_total += consumed
                values.append(value)
        found[tag] = values
    return found


def _index_entries(
    records: list[bytes], index: int
) -> list[tuple[bytes, dict[int, list[int]]]]:
    """读一组 INDX：主记录给 TAGX，后续记录每条含若干条目。"""
    if index in (0, 0xFFFFFFFF) or index >= len(records):
        return []
    main = records[index]
    if main[:4] != b"INDX":
        return []
    try:
        header = _indx_header(main)
        control_count, tags = _tagx(main, header["len"])
    except (struct.error, ProducerError):
        return []
    if header["count"] <= 0 or not tags:
        return []

    entries: list[tuple[bytes, dict[int, list[int]]]] = []
    for section_index in range(index + 1, index + 1 + header["count"]):
        if section_index >= len(records):
            break
        section = records[section_index]
        if section[:4] != b"INDX":
            continue
        try:
            section_header = _indx_header(section)
            start = section_header["start"]
            count = section_header["count"]
            if start + 4 + 2 * count > len(section):
                continue
            positions = [
                struct.unpack_from(">H", section, start + 4 + 2 * j)[0]
                for j in range(count)
            ]
            positions.append(start)
            for position in range(count):
                low, high = positions[position], positions[position + 1]
                if not (0 <= low < high <= len(section)):
                    continue
                text_length = section[low]
                text = section[low + 1 : low + 1 + text_length]
                values = _tag_values(
                    section, low + 1 + text_length, control_count, tags
                )
                entries.append((text, values))
        except (struct.error, ProducerError):
            continue
    return entries


def _fdst_starts(records: list[bytes], index: int, raw_size: int) -> list[int]:
    """FDST 流表：正文之后还跟着 CSS/SVG 等流动部分，第一段才是 XHTML。"""
    if index in (0, 0xFFFFFFFF) or index >= len(records):
        return []
    data = records[index]
    if data[:4] != b"FDST" or len(data) < 12:
        return []
    count = struct.unpack_from(">L", data, 8)[0]
    if count < 2 or 12 + count * 8 > len(data):
        return []
    starts = [struct.unpack_from(">L", data, 12 + slot * 8)[0] for slot in range(count)]
    starts.append(raw_size)
    if any(starts[i] > starts[i + 1] for i in range(len(starts) - 1)):
        return []
    return starts


def _absolute(start: int, index: int) -> int:
    return 0xFFFFFFFF if index in (0, 0xFFFFFFFF) else index + start


def _kf8_parts(records: list[bytes], start: int) -> list[str]:
    """KF8 正文 → 一份文档一段。

    KF8 把每个原始 XHTML 拆成 skeleton 与 fragments：skeleton 先连续存放，
    fragments 排在后面；FRAG 索引给出每片的插入位置与长度。
    """
    header = _mobi_header(records, start)
    if header.version < 8:
        raise ProducerError("这本电子书不是 KF8 格式。")
    raw = _raw_records(records, header, start)
    if not raw.strip():
        raise ProducerError("这本电子书里没有可读的正文。")

    fdst = _header_word(records, start, 0xC0)
    fdst_count = _header_word(records, start, 0xC4)
    starts = _fdst_starts(records, _absolute(start, fdst), len(raw)) if fdst_count > 1 else []
    text = raw[starts[0] : starts[1]] if len(starts) >= 2 else raw

    skel = _index_entries(records, _absolute(start, _header_word(records, start, 0xFC)))
    frag = _index_entries(records, _absolute(start, _header_word(records, start, 0xF8)))
    if not skel:
        raise ProducerError("这本 AZW3 缺少章节索引（SKEL）；先用 Calibre 转成 EPUB。")

    parts: list[str] = []
    frag_pointer = 0
    for _, tags in skel:
        count = (tags.get(1) or [0])[0]
        span = tags.get(6) or []
        if len(span) < 2:
            continue
        skel_pos, skel_len = span[0], span[1]
        if skel_pos + skel_len > len(text):
            continue
        skeleton = text[skel_pos : skel_pos + skel_len]
        base = skel_pos + skel_len
        for _ in range(count):
            if frag_pointer >= len(frag):
                break
            frag_text, frag_tags = frag[frag_pointer]
            frag_pointer += 1
            frag_span = frag_tags.get(6) or []
            if len(frag_span) < 2:
                continue
            try:
                insert = int(frag_text)
            except ValueError:
                continue
            frag_len = frag_span[1]
            if base + frag_len > len(text):
                continue
            piece = text[base : base + frag_len]
            base += frag_len
            at = insert - skel_pos
            if 0 <= at <= len(skeleton):
                skeleton = skeleton[:at] + piece + skeleton[at:]
        parts.append(_decode(skeleton))
    if not parts:
        raise ProducerError("这本 AZW3 的正文索引不完整；先用 Calibre 转成 EPUB。")
    return parts


def _segment_chapter(markup: str) -> tuple[str, str] | None:
    """一段 HTML → (标题, 正文)。

    每份文件的 ``<title>`` 常是书名而不是章名，这里只认 h1–h3。没有标题标签时，
    首行若像标题（短、不带句末标点）就把它从正文里摘掉——标题已经单独存了，
    再念一遍就是重复。
    """
    heading, text = _html_text(markup, title_tag=False)
    text = _scrub(text)
    if not text:
        return None
    if heading:
        if text.startswith(heading):
            text = text[len(heading) :].lstrip("\n")
        if not text:
            return None
        return heading, text

    first, _, rest = text.partition("\n")
    title = first.strip()
    body = rest.strip()
    if body and len(title) <= 40 and title[-1:] not in "。！？.!?；;：:":
        text = body
    return title, text


def _pagebreak_chapters(markup: str) -> list[tuple[str, str]]:
    chapters = [
        chapter for part in _PAGEBREAK.split(markup) if (chapter := _segment_chapter(part))
    ]
    return chapters if len(chapters) >= 2 else []


def _heading_chapters(markup: str) -> list[tuple[str, str]]:
    matches = list(_HEADING.finditer(markup))
    levels = [int(match.group(1)) for match in matches]
    level = next((item for item in (1, 2, 3) if levels.count(item) >= 2), 0)
    if not level:
        return []
    starts = [matches[index].start() for index, item in enumerate(levels) if item == level]
    if len(starts) < 2:
        return []

    chapters: list[tuple[str, str]] = []
    preface = _segment_chapter(markup[: starts[0]])
    if preface:
        chapters.append(preface)
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(markup)
        chapter = _segment_chapter(markup[start:end])
        if chapter:
            chapters.append(chapter)
    return chapters if len(chapters) >= 2 else []


def _mobi_chapters(markup: str) -> list[tuple[str, str]]:
    return _pagebreak_chapters(markup) or _heading_chapters(markup)


def _kf8_chapters(parts: list[str]) -> list[tuple[str, str]]:
    """一份文档一章；整本一个文件时再按标题切。"""
    chapters = [chapter for part in parts if (chapter := _segment_chapter(part))]
    if len(chapters) >= 2:
        return chapters
    markup = "\n".join(parts)
    return _mobi_chapters(markup) or ([chapters[0]] if chapters else [])


def _mobi_source(markup: str) -> str:
    heading, text = _html_text(markup)
    text = _scrub(text)
    if heading and not text.startswith(heading):
        text = f"{heading}\n\n{text}"
    return text


def _chapters_source(chapters: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{name}\n{body}" for name, body in chapters)


def _palm_book(data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """MOBI7 / KF8（AZW3）/ PalmDOC → (正文全文, 章节)。"""
    records = _palm_records(data)
    kf8 = _kf8_header(records)
    if kf8 >= 0:
        try:
            parts = _kf8_parts(records, kf8)
        except ProducerError:
            if kf8 == 0:
                raise
            parts = []
        if parts:
            chapters = _kf8_chapters(parts)
            if chapters:
                return _chapters_source(chapters), chapters
            return _scrub("\n".join(parts)), []

    header = _mobi_header(records, 0)
    if header.version >= 8:
        raise ProducerError("这本电子书的 KF8 正文索引不完整；先用 Calibre 转成 EPUB。")
    markup = _decode(_raw_records(records, header), header.encoding)
    if not markup.strip():
        raise ProducerError("电子书里没有可读的正文。")
    chapters = _mobi_chapters(markup)
    if chapters:
        return _chapters_source(chapters), chapters
    return _mobi_source(markup), []


def read_book(filename: str, data: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """文件 → (默认书名, 正文全文, 已解出的章节)。

    ``chapters``：EPUB 按 spine、MOBI/KF8 按书内分页或标题成章；
    TXT / Markdown 与无章节标记的电子书交给用户在「分章」这一步选模式切。

    导入只做「认出正文、去掉排版记号」：控制字符与替换符一律清掉，
    TXT 里没被解析掉的标签碎片也一并清掉（Markdown 保留，它可能本来就写 HTML）。
    """
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix not in SUPPORTED:
        raise ProducerError(
            f"不支持的书稿格式：{suffix or '无扩展名'}。"
            "可用 .txt / .md / .markdown / .epub / .mobi / .azw3 / .azw / .prc。"
        )
    if not data:
        raise ProducerError("文件是空的。")

    title = PurePosixPath(filename).stem.strip() or "未命名书稿"
    if suffix == ".epub":
        chapters = _epub_chapters(data)
        return title, _chapters_source(chapters), chapters
    if suffix in (".mobi", ".azw3", ".azw", ".prc"):
        source, chapters = _palm_book(data)
        return title, source, chapters

    text = _scrub(_decode(data), markup=suffix not in (".md", ".markdown"))
    if not text.strip():
        raise ProducerError("文件里没有正文。")
    return title, text, []
