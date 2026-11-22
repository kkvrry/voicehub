"""字幕：时间码、长句切分与五种格式的序列化。

口径是 ``(起, 止, 文本)`` 三元组。写文件与切分都只在这一处——配音与有声书
共用同一份实现，换格式不改调用方。

几条已踩过的坑：

- **不带 BOM**：BOM 紧贴首条序号，部分播放器读成非法序号而跳过整条，
  表现就是每份字幕的第一句都不显示；
- **一条别太长**：超过上限在手机与车机上会溢出，按标点切开并按字数比例分时间，
  总时长不变；
- **ASS 的时间码用厘秒**：写成毫秒会让整条字幕晚十倍，这是最常见的格式错。
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = [
    "SUBTITLE_ENCODING",
    "SUBTITLE_MAX_CHARS",
    "ass_text",
    "lrc_text",
    "lrc_time",
    "parse_subtitles",
    "split_cues",
    "split_subtitle_text",
    "srt_text",
    "srt_time",
    "txt_text",
    "vtt_text",
    "write_subtitles",
]

#: 一条时间码行：SRT 用逗号，VTT 用点；两侧允许带 VTT 的 cue 设置。
_TIME_LINE = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
#: VTT 的内联标记：``<v 角色>`` / ``<i>`` / 内联时间戳。进正文只会被念出来。
_VTT_TAG = re.compile(r"<[^>]*>")

#: 一条字幕最多多少字：再长在手机与车机上会溢出。
SUBTITLE_MAX_CHARS = 32
#: 字幕文件编码。**不带 BOM**：见模块说明。
SUBTITLE_ENCODING = "utf-8"
#: 长句切分时优先在这些标点后面断。
_SUBTITLE_BREAKS = "，。；！？、,.!?;"

#: 写进 ASS 的样式：中文字号按 1080p 调，底部留出安全边距。
_ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,54,&H00FFFFFF,&H000000FF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,72,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_ASS_TAG = re.compile(r"\{[^}]*\}")


def srt_time(seconds: float) -> str:
    """秒 → ``00:00:08,608``。"""
    total = round(max(0.0, seconds) * 1000)
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def lrc_time(seconds: float) -> str:
    """秒 → ``[00:08.60]`` 里的时间部分。"""
    total = round(max(0.0, seconds) * 100)
    minutes, rest = divmod(total, 6000)
    secs, cents = divmod(rest, 100)
    return f"{minutes:02d}:{secs:02d}.{cents:02d}"


def _ass_time(seconds: float) -> str:
    """秒 → ``0:00:08.60``（ASS 用厘秒，且小时不补零）。"""
    total = round(max(0.0, seconds) * 100)
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, cents = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def split_subtitle_text(text: str, limit: int) -> list[str]:
    """把一句字幕按标点切成不超过 ``limit`` 的几段；找不到标点就按上限硬切。"""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(mark) for mark in _SUBTITLE_BREAKS)
        cut = cut + 1 if cut >= limit // 2 else limit
        piece = rest[:cut].strip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def split_cues(
    cues: Sequence[tuple[float, float, str]], limit: int
) -> list[tuple[float, float, str]]:
    """长句按字数切成几条 cue，时间按字数比例分；总时长不变。"""
    out: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        parts = split_subtitle_text(text, limit)
        if len(parts) <= 1:
            out.append((start, end, text))
            continue
        weights = [max(1, len(part)) for part in parts]
        total = sum(weights)
        cursor = start
        for index, (part, weight) in enumerate(zip(parts, weights, strict=True)):
            span = (end - start) * weight / total
            stop = end if index == len(parts) - 1 else cursor + span
            out.append((cursor, stop, part))
            cursor = stop
    return out


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    # 毫秒位允许 1–3 位：``0:00:01.5`` 在 VTT 里就是 500 毫秒。
    fraction = int(millis.ljust(3, "0")[:3]) / 1000.0
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + fraction


def parse_subtitles(text: str) -> list[tuple[float, float, str]]:
    """SRT / VTT → ``(起, 止, 文本)``。认不出时间码的行留在上一条文本里。

    导入现成字幕是配音项目最常见的起点之一：源片已有字幕时，重新识别一遍
    只会把已经校对好的断句再打散。这里只管时间码与文本：VTT 的 ``<v 角色>``
    等内联标记去掉，角色不替用户猜，导入后由用户在行内改。
    """
    body = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    out: list[tuple[float, float, str]] = []
    current: tuple[float, float, list[str]] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        start, end, parts = current
        # VTT 的内联标记（<v 角色>、<i>、内联时间戳）与 HTML 实体都不该被念出来。
        content = html.unescape(_VTT_TAG.sub("", "\n".join(parts))).strip()
        # VTT 的 NOTE / STYLE / REGION 块没有时间码，会自然落到这里；空文本丢弃。
        if content and end > start:
            out.append((start, end, content))
        current = None

    for line in lines:
        match = _TIME_LINE.match(line)
        if match:
            flush()
            start = _seconds(*match.group(1, 2, 3, 4))
            end = _seconds(*match.group(5, 6, 7, 8))
            current = (start, end, [])
            continue
        if current is None:
            continue
        if not line.strip():
            flush()
            continue
        parts = current[2]
        # 序号行不属于正文：只在文本还空着时出现。
        if not parts and line.strip().isdigit():
            continue
        parts.append(line)
    flush()
    return out


def srt_text(cues: Iterable[tuple[float, float, str]]) -> str:
    """cue 列表 → SRT 全文。"""
    blocks = [
        f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def vtt_text(cues: Iterable[tuple[float, float, str]]) -> str:
    """cue 列表 → WebVTT 全文（时间码用点号，且带宽高）。"""
    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{srt_time(start).replace(',', '.')} --> {srt_time(end).replace(',', '.')}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def lrc_text(cues: Iterable[tuple[float, float, str]]) -> str:
    """cue 列表 → LRC 全文（只要起始时间）。"""
    return "\n".join(f"[{lrc_time(start)}]{text}" for start, _, text in cues) + "\n"


def txt_text(cues: Iterable[tuple[float, float, str]]) -> str:
    """cue 列表 → 纯文本：一句一行，不带时间码（校对稿用）。"""
    return "\n".join(text for _, _, text in cues) + "\n"


def ass_text(cues: Iterable[tuple[float, float, str]]) -> str:
    """cue 列表 → ASS：带样式头，可直接交给播放器或封装进 mp4。"""
    lines = [_ASS_HEAD]
    for index, (start, end, text) in enumerate(cues, start=1):
        # 花括号在 ASS 里是样式覆盖，文本里的同名符号必须去掉；换行转成 \N。
        body = _ASS_TAG.sub("", text).replace("{", "（").replace("}", "）")
        body = body.replace("\n", r"\N")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            f"{body}\n"
        )
    return "".join(lines)


#: 格式名 → 写出函数与后缀（后缀就是键，界面只认这几个）。
_FORMATS = {
    "srt": (srt_text, ".srt"),
    "vtt": (vtt_text, ".vtt"),
    "lrc": (lrc_text, ".lrc"),
    "txt": (txt_text, ".txt"),
    "ass": (ass_text, ".ass"),
}


def write_subtitles(
    cues: Sequence[tuple[float, float, str]],
    folder: Path,
    stem: str,
    *,
    formats: Iterable[str] = ("srt",),
    encoding: str = SUBTITLE_ENCODING,
    max_chars: int = 0,
) -> dict[str, Path]:
    """写出指定格式的字幕，返回 ``{格式: 路径}``。

    :param max_chars: ``> 0`` 时先按字数切开（只影响字幕文件，不改时间轴上的段落）。
        切成几条 cue 是**导出时**的决定：段落表里一段就是一句话，切了就没法逐段配音。
    """
    wanted = [name for name in formats if name in _FORMATS]
    if not wanted:
        return {}
    final = list(cues)
    if max_chars > 0:
        final = split_cues(final, max_chars)
    if not final:
        return {}
    folder.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in wanted:
        writer, suffix = _FORMATS[name]
        path = folder / f"{stem}{suffix}"
        # newline="" 让 \n 原样落盘：播放器对 CRLF 无所谓，但对 BOM 很敏感。
        with path.open("w", encoding=encoding, newline="\n") as handle:
            handle.write(writer(final))
        written[name] = path
    return written


def normalize_encoding(raw: str) -> str:
    """编码设置 → Python 编码名。只认三种：字幕播放器对别的编码支持参差。"""
    key = (raw or "").strip().lower().replace("-", "").replace("_", "")
    if key in {"utf8bom", "utf8sig"}:
        return "utf-8-sig"
    if key in {"gb18030", "gbk", "gb2312"}:
        return "gb18030"
    return SUBTITLE_ENCODING
