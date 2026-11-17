"""书稿解析：EPUB / MOBI / AZW3 / 纯文本 → 章节。"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from app.producers.base import ProducerError
from app.producers.builtin.audiobook.ebooks import read_book

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

PACKAGE = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <manifest>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""

PACKAGE_WITH_NCX = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <manifest>
    <item id="info" href="text/info.xhtml" media-type="application/xhtml+xml"/>
    <item id="p1" href="text/p1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/c2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="info"/>
    <itemref idref="p1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1"><navLabel><text>书籍信息</text></navLabel>
      <content src="text/info.xhtml"/></navPoint>
    <navPoint id="n2"><navLabel><text>第一部</text></navLabel>
      <content src="text/p1.xhtml"/>
      <navPoint id="n3"><navLabel><text>第二章</text></navLabel>
        <content src="text/c2.xhtml"/></navPoint>
    </navPoint>
  </navMap>
</ncx>"""

PACKAGE_WITH_NAV = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="nav"/>
    <itemref idref="c1"/>
  </spine>
</package>"""

NAV = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol>
  <li><a href="text/ch1.xhtml">序章 · 雪</a></li>
</ol></nav></body></html>"""



def _epub_bytes(*, drm: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", PACKAGE)
        archive.writestr(
            "OEBPS/text/ch1.xhtml",
            "<html><head><title>第一章</title></head><body>"
            "<h1>第一章 起</h1><p>少年推门而入。</p></body></html>",
        )
        archive.writestr(
            "OEBPS/text/ch2.xhtml",
            "<html><body><h2>第二章 承</h2><p>「坐吧。」老人说道。</p></body></html>",
        )
        if drm:
            archive.writestr("META-INF/encryption.xml", "<encryption/>")
    return buffer.getvalue()


def _pdb(records: list[bytes]) -> bytes:
    header = bytearray(78)
    header[0:9] = b"test-book"
    struct.pack_into(">H", header, 76, len(records))
    entries = bytearray(8 * len(records))
    cursor = 78 + 8 * len(records) + 2
    for index, record in enumerate(records):
        struct.pack_into(">I", entries, index * 8, cursor)
        cursor += len(record)
    return bytes(header) + bytes(entries) + b"\x00\x00" + b"".join(records)


def _mobi_bytes(text: str, compression: int = 1) -> bytes:
    payload = text.encode("utf-8")
    chunks = [payload[index : index + 4096] for index in range(0, len(payload), 4096)]
    book = bytearray(16)
    struct.pack_into(">H", book, 0, compression)
    struct.pack_into(">I", book, 4, len(payload))
    struct.pack_into(">H", book, 8, len(chunks))
    struct.pack_into(">H", book, 10, 4096)
    struct.pack_into(">H", book, 12, 0)
    book += b"MOBI"
    book += struct.pack(">I", 232)  # 头长
    book += struct.pack(">I", 2)  # 类型：书
    book += struct.pack(">I", 65001)  # 编码：UTF-8
    book += b"\x00" * max(0, 0xF4 - len(book))
    return _pdb([bytes(book), *chunks])


def _vwi(value: int) -> bytes:
    """Mobipocket 变长正整数：7 位一组，末字节置最高位。"""
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(value & 0x7F)
        value >>= 7
    parts.reverse()
    parts[-1] |= 0x80
    return bytes(parts)


def _indx(tags: list[tuple[int, int, int, int]], entries: list[bytes], controls: int):
    """一组 INDX：主记录带 TAGX，后续记录带 IDXT 与条目。"""
    tagx = b"TAGX" + struct.pack(">LL", 12 + 4 * len(tags), controls)
    for tag in tags:
        tagx += bytes(tag)
    main = bytearray(b"INDX")
    main += struct.pack(">13L", 56, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0)
    main += tagx

    offsets = []
    cursor = 56
    for entry in entries:
        offsets.append(cursor)
        cursor += len(entry)
    section = bytearray(b"INDX")
    section += struct.pack(">13L", 56, 0, 0, 0, cursor, len(entries), 0, 0, 0, 0, 0, 0, 0)
    section += b"".join(entries)
    section += b"IDXT" + b"".join(struct.pack(">H", offset) for offset in offsets)
    return bytes(main), bytes(section)


def _entry(text: bytes, control: bytes, values: bytes) -> bytes:
    return bytes([len(text)]) + text + control + values


def _azw3_bytes(parts: list[tuple[bytes, list[bytes]]]) -> bytes:
    """(skeleton, fragments) → KF8/AZW3；每片插在该 skeleton 的 </body> 前。"""
    raw = bytearray()
    skel_entries: list[bytes] = []
    frag_entries: list[bytes] = []
    for skeleton, fragments in parts:
        skel_pos = len(raw)
        raw += skeleton
        base = len(raw)
        anchor = skeleton.rfind(b"</body>")
        for fragment in fragments:
            raw += fragment
            insert = skel_pos + (anchor if anchor >= 0 else len(skeleton))
            frag_entries.append(
                _entry(str(insert).encode(), b"\x01", _vwi(base) + _vwi(len(fragment)))
            )
            base += len(fragment)
        skel_entries.append(
            _entry(
                b"part%04d.xhtml" % len(skel_entries),
                b"\x01\x01",
                _vwi(len(fragments)) + _vwi(skel_pos) + _vwi(len(skeleton)),
            )
        )

    header = bytearray(16 + 264)
    struct.pack_into(">H", header, 0, 1)  # 不压缩
    struct.pack_into(">I", header, 4, len(raw))
    struct.pack_into(">H", header, 8, 1)
    struct.pack_into(">H", header, 10, 4096)
    header[16:20] = b"MOBI"
    struct.pack_into(">I", header, 20, 264)
    struct.pack_into(">I", header, 24, 2)
    struct.pack_into(">I", header, 28, 65001)
    struct.pack_into(">I", header, 36, 8)  # KF8
    struct.pack_into(">I", header, 0xC0, 0xFFFFFFFF)  # 不分流
    struct.pack_into(">I", header, 0xC4, 0)
    struct.pack_into(">H", header, 0xF2, 0)

    records = [bytes(header), bytes(raw)]
    skel_main, skel_section = _indx(
        [(1, 1, 0x01, 0), (0, 0, 0, 1), (6, 2, 0x01, 0), (0, 0, 0, 1)],
        skel_entries,
        2,
    )
    struct.pack_into(">I", header, 0xFC, len(records))
    records += [skel_main, skel_section]
    if frag_entries:
        frag_main, frag_section = _indx([(6, 2, 0x01, 0), (0, 0, 0, 1)], frag_entries, 1)
        struct.pack_into(">I", header, 0xF8, len(records))
        records += [frag_main, frag_section]
    else:
        struct.pack_into(">I", header, 0xF8, 0xFFFFFFFF)
    records[0] = bytes(header)
    return _pdb(records)


def _combo_bytes(book: str) -> bytes:
    """MOBI7 + BOUNDARY + 一个没有 SKEL 的 KF8 头：组合文件里的 KF8 坏了。"""
    from app.producers.builtin.audiobook.ebooks import _palm_records

    records = _palm_records(_mobi_bytes(book))
    header = bytearray(16 + 264)
    struct.pack_into(">H", header, 0, 1)
    struct.pack_into(">I", header, 4, 0)
    struct.pack_into(">H", header, 8, 0)
    header[16:20] = b"MOBI"
    struct.pack_into(">I", header, 20, 264)
    struct.pack_into(">I", header, 28, 65001)
    struct.pack_into(">I", header, 36, 8)
    struct.pack_into(">I", header, 0xFC, 0xFFFFFFFF)
    struct.pack_into(">I", header, 0xF8, 0xFFFFFFFF)
    return _pdb([*records, b"BOUNDARY", bytes(header)])


def test_epub_reads_spine_in_order() -> None:
    title, source, chapters = read_book("样书.epub", _epub_bytes())
    assert title == "样书"
    assert [item[0] for item in chapters] == ["第一章 起", "第二章 承"]
    assert chapters[0][1] == "少年推门而入。"
    assert "「坐吧。」" in chapters[1][1]
    # 原文也留一份：文本处理与「重新分章」都从它出发。
    assert "第一章 起" in source and "第二章 承" in source


def test_epub_footnote_markers_are_dropped() -> None:
    """脚注引用是排版记号，不是正文：标记摘掉，前面那格空格也不留。"""
    from app.producers.builtin.audiobook.ebooks import _html_text

    assert _html_text("<p>尽想些戈罗赫王 <a href=\"#fn1\">[1]</a>的事。</p>")[1] == (
        "尽想些戈罗赫王的事。"
    )
    assert _html_text("<p>他说<sup>2</sup>了什么。</p>")[1] == "他说了什么。"
    assert _html_text("<p>戈罗赫王<a href=\"#fn2\">②</a>的事。</p>")[1] == "戈罗赫王的事。"
    # 指向别的文件的链接是正文里的交叉引用，不能当脚注删掉。
    assert _html_text("<p>见<a href=\"other.xhtml\">附录</a>。</p>")[1] == "见附录。"
    # 英文靠空格分词：摘掉标记后那个空格得留下。
    assert _html_text("<p>see note <a href=\"#n1\">[3]</a>for details.</p>")[1] == (
        "see note for details."
    )


def test_epub_footnote_blocks_are_not_read() -> None:
    """脚注的定义块（aside epub:type=footnote）整块不念：念出来就是正文后面挂尾巴。"""
    from app.producers.builtin.audiobook.ebooks import _html_text

    html = (
        '<body><p>正文<a epub:type="noteref" href="#n1"><sup><img src="n.png"/></sup></a>继续。</p>'
        '<aside epub:type="footnote" id="n1"><ol><li>编按：这里不该被念出来</li></ol></aside>'
        "<p>下一段。</p></body>"
    )
    assert _html_text(html)[1] == "正文继续。\n下一段。"


def test_broken_text_source_is_scrubbed() -> None:
    """坏源留下的方框、控制字符与残标：导入时清掉，别再念出来。"""
    raw = '少年走了进来。\x00\x12\n他看见了你\ufffd。\n<span style="x">下一段</span>\n'
    _, text, _ = read_book("样书.txt", raw.encode("utf-8"))
    assert text.splitlines() == ["少年走了进来。", "他看见了你。", "下一段"]


def test_both_markdown_suffixes_keep_inline_html() -> None:
    """两种 Markdown 后缀同口径：作者写的 HTML 不是转换残标。"""
    raw = "<p>正文</p>\n".encode()
    for name in ("样书.md", "样书.markdown"):
        _, text, _ = read_book(name, raw)
        assert "<p>" in text


def test_epub_with_drm_is_refused() -> None:
    with pytest.raises(ProducerError, match="加密"):
        read_book("样书.epub", _epub_bytes(drm=True))


def _epub_with_ncx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", PACKAGE_WITH_NCX)
        archive.writestr("OEBPS/toc.ncx", NCX)
        archive.writestr(
            "OEBPS/text/info.xhtml",
            "<html><body><p>书名：样书</p></body></html>",
        )
        archive.writestr(
            "OEBPS/text/p1.xhtml",
            "<html><body><h1>第一部</h1><p>开篇。</p></body></html>",
        )
        archive.writestr(
            "OEBPS/text/c2.xhtml",
            "<html><body><h1>第二章</h1><p>正文。</p></body></html>",
        )
    return buffer.getvalue()


def test_epub_titles_follow_the_book_toc() -> None:
    """章节名取书内目录：h1 只剩「第二章」时，目录里的「第一部」这层不能丢。"""
    _, _, chapters = read_book("样书.epub", _epub_with_ncx())
    assert [item[0] for item in chapters] == ["书籍信息", "第一部", "第一部 第二章"]


def test_epub3_nav_is_read_when_there_is_no_ncx() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", PACKAGE_WITH_NAV)
        archive.writestr("OEBPS/nav.xhtml", NAV)
        archive.writestr(
            "OEBPS/text/ch1.xhtml",
            "<html><body><h1>第一章</h1><p>雪落下来。</p></body></html>",
        )
    _, _, chapters = read_book("样书.epub", buffer.getvalue())
    assert [item[0] for item in chapters] == ["序章 · 雪"]


def test_epub_without_container_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    with pytest.raises(ProducerError, match="container.xml"):
        read_book("样书.epub", buffer.getvalue())


def test_mobi_splits_at_pagebreaks() -> None:
    """KindleGen 在每个文件开头放硬分页：MOBI 导入即按它成章。"""
    book = (
        "<html><body><h1>第一章 起</h1><p>少年推门而入。</p><mbp:pagebreak/>"
        "<h1>第二章 承</h1><p>风从窗缝里进来。</p></body></html>"
    )
    title, source, chapters = read_book("样书.mobi", _mobi_bytes(book))
    assert title == "样书"
    assert [item[0] for item in chapters] == ["第一章 起", "第二章 承"]
    assert chapters[0][1] == "少年推门而入。"
    assert "第一章 起" in source and "<h1>" not in source


def test_mobi_plain_title_line_is_not_read_twice() -> None:
    """没有 h1–h3 时首行当选标题：正文里不再保留这一行。"""
    book = (
        "<html><body><p>Chapter 1</p><p>It is a truth.</p><mbp:pagebreak/>"
        "<p>Chapter 2</p><p>Second.</p></body></html>"
    )
    _, _, chapters = read_book("样书.mobi", _mobi_bytes(book))
    assert [item[0] for item in chapters] == ["Chapter 1", "Chapter 2"]
    assert chapters[0][1] == "It is a truth."


def test_mobi_falls_back_to_headings() -> None:
    """没有分页标记时，最上层、至少出现两次的标题就是章节边界。"""
    book = (
        "<html><body><h1>第一章 起</h1><p>少年推门而入。</p>"
        "<h1>第二章 承</h1><p>风从窗缝里进来。</p></body></html>"
    )
    _, _, chapters = read_book("样书.mobi", _mobi_bytes(book))
    assert [item[0] for item in chapters] == ["第一章 起", "第二章 承"]
    assert chapters[1][1] == "风从窗缝里进来。"


def test_mobi_without_markers_waits_for_the_split_step() -> None:
    """没有分页也没有标题：不硬切，留给「分章」选模式。"""
    book = "<html><body><p>只有正文，没有标题。</p><p>第二段。</p></body></html>"
    _, _, chapters = read_book("样书.mobi", _mobi_bytes(book))
    assert chapters == []


def test_azw3_kf8_parts_become_chapters() -> None:
    """KF8 的每份 skeleton 就是一个原始 XHTML；片段按插入位置补回正文。"""
    parts = [
        (b"<html><body><h1>Chapter 1</h1></body></html>", [b"<p>Alpha.</p>"]),
        (b"<html><body><h1>Chapter 2</h1></body></html>", [b"<p>Beta.</p>"]),
    ]
    title, source, chapters = read_book("样书.azw3", _azw3_bytes(parts))
    assert title == "样书"
    assert [item[0] for item in chapters] == ["Chapter 1", "Chapter 2"]
    assert chapters[0][1] == "Alpha."
    assert chapters[1][1] == "Beta."
    assert "Alpha." in source and "<p>" not in source


def test_azw3_whole_book_falls_back_to_headings() -> None:
    """整本只装在一个 skeleton 里时，再按标题切。"""
    book = b"<html><body><h1>One</h1><p>A.</p><h1>Two</h1><p>B.</p></body></html>"
    _, _, chapters = read_book("样书.azw3", _azw3_bytes([(book, [])]))
    assert [item[0] for item in chapters] == ["One", "Two"]
    assert chapters[0][1] == "A."


def test_azw3_combo_falls_back_to_mobi7() -> None:
    """组合文件里 KF8 索引坏了，就用前面的 MOBI7 正文。"""
    book = (
        "<html><body><h1>第一章 起</h1><p>少年推门而入。</p>"
        "<h1>第二章 承</h1><p>风从窗缝里进来。</p></body></html>"
    )
    _, source, chapters = read_book("样书.azw3", _combo_bytes(book))
    assert [item[0] for item in chapters] == ["第一章 起", "第二章 承"]
    assert "少年推门而入。" in source


def test_azw3_without_kf8_index_is_refused() -> None:
    """纯 KF8 但索引缺失：说清原因，别拿半截正文凑数。"""
    html = "<html><body><p>正文。</p></body></html>".encode()
    data = bytearray(_azw3_bytes([(html, [])]))
    from app.producers.builtin.audiobook.ebooks import _palm_records

    records = [bytearray(record) for record in _palm_records(bytes(data))]
    struct.pack_into(">I", records[0], 0xFC, 0xFFFFFFFF)
    with pytest.raises(ProducerError, match="SKEL"):
        read_book("样书.azw3", _pdb([bytes(record) for record in records]))


def test_palmdoc_back_reference() -> None:
    """0x80–0xBF：高位是距离、低位是长度（3–10），从已解出的内容里回看复制。"""
    from app.producers.builtin.audiobook.ebooks import _palmdoc

    # 距离 2、长度 3 → 在 "AB" 之后复制出 "ABA"。
    assert _palmdoc(b"AB\x80\x10") == b"ABABA"
    # 0xC0 以上是「空格 + 该字节去掉最高位」，不是复制。
    assert _palmdoc(b"\xC1") == b" A"


def test_mobi_with_drm_is_refused() -> None:
    data = bytearray(_mobi_bytes("<p>正文</p>"))
    offset = struct.unpack(">I", data[78:82])[0]
    struct.pack_into(">H", data, offset + 12, 2)  # encryption = 2
    with pytest.raises(ProducerError, match="加密"):
        read_book("样书.mobi", bytes(data))


def test_plain_text_falls_back_to_gb18030() -> None:
    raw = "第一章 起\n少年推门而入。\n第二章 承\n风从窗缝里进来。\n".encode("gb18030")
    title, source, chapters = read_book("样书.txt", raw)
    assert title == "样书"
    assert chapters == []
    assert source.startswith("第一章 起")


def test_unknown_extension_is_refused() -> None:
    with pytest.raises(ProducerError, match="不支持") as error:
        read_book("样书.kfx", b"whatever")
    assert ".markdown" in str(error.value) and ".azw3" in str(error.value)
