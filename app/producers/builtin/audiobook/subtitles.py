"""SRT 字幕解析：时间码 + 文本 +（可选）说话人标记。

与 `ebooks.py` 的分工：那边是「书稿 → 章节正文」，这边是「字幕 → 带时间码的行」。
字幕项目的性质不同——**时间码是硬约束，不拆句、不分章**，所以单独立一个模块，
书稿那条路（`read_book` 与分章/拆句）一行都不用改。

几条口径：
- **毫秒对齐**：时间码原样算成毫秒（`00:00:08,608` → 8608），不学某些实现
  只算到 1/100 秒。
- **样式标签剥掉**：`<i>`、`{\an8}` 这类念不出来，导入时去掉（原始文件另存一份，
  不会丢）；多行文本合成一行（字幕的软换行不是句子边界）。
- **说话人标记**：`张三：你好`、`[张三] 你好`、`- 张三：你好`。只出现一次的
  冒号前缀当正文——字幕里「他说：」比「某个角色只出现一次」常见得多。
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.producers.base import ProducerError
from app.producers.builtin.audiobook.ebooks import _decode

__all__ = ["SUPPORTED", "Cue", "parse_srt", "read_subtitle"]

#: 能直接导入的字幕格式。ASS/VTT 往后再说（它们的时间码与样式各一套）。
SUPPORTED = (".srt",)

#: SRT 时间码：`00:00:08,608`。也认分号毫秒（部分工具）与省略小时/毫秒的写法。
_TIME = re.compile(r"(?:(\d{1,3}):)?(\d{1,3}):(\d{1,2})(?:[,.;](\d{1,3}))?")

#: 样式标签与 ASS 覆写块：念不出来，导入时剥掉。
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")

#: 说话人标记：名字 1–10 字、不带数字（`10:30` 不会被当成说话人）。
_SPEAKER_COLON = re.compile(r"^(?:[-–—]\s*)?([^\s：:\[\]【】()（）\d]{1,10})\s*[：:]\s*(.+)$")
_SPEAKER_BRACKET = re.compile(r"^(?:[-–—]\s*)?[\[【]([^\]】\d]{1,10})[\]】]\s*(.+)$")


@dataclass(frozen=True, slots=True)
class Cue:
    """一条字幕：时间码（毫秒）+ 要念的文本 +（解析到的）说话人。"""

    start_ms: int
    end_ms: int
    text: str
    speaker: str = ""


def _time_ms(raw: str) -> int | None:
    """`00:00:08,608` → 8608；认 `,`/`.` 毫秒与省略小时的写法；认不出返回 ``None``。"""
    match = _TIME.fullmatch(raw.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _clean(raw: str) -> str:
    """剥标签、还原实体、空白归一：念出来只有一个空格的气口。"""
    text = _TAGS.sub("", raw)
    text = html.unescape(text).replace("\u00a0", " ")
    return " ".join(text.split())


def _split_speaker(text: str) -> tuple[str, str]:
    """`张三：你好` → (`张三`, `你好`)；没有标记 → (`""`, 原文)。"""
    for pattern in (_SPEAKER_COLON, _SPEAKER_BRACKET):
        match = pattern.match(text)
        if match:
            return match.group(1).strip(), match.group(2).strip()
    return "", text


def parse_srt(text: str) -> tuple[list[Cue], int]:
    """SRT 文本 → (条目, 跳过的坏块数)。

    坏块 = 没有时间行、时间解析不出、或者剥完标签没有正文。跳过不报错：
    字幕文件是从各处导出来的，个别块写坏不该让整份导入失败。
    """
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    raw: list[tuple[int, int, str]] = []
    skipped = 0
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        # 序号行可选（`1` 或 `1-2`，后者是某些工具复制行的写法）
        if re.fullmatch(r"\d+(?:-\d+)?", lines[0].strip()):
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            skipped += 1
            continue
        left, _, right = lines[0].partition("-->")
        # 时间行右侧可能跟着 `X1:...` 之类的定位参数，只取第一段
        head = right.strip().split()[0] if right.strip() else ""
        start = _time_ms(left)
        end = _time_ms(head)
        body = _clean(" ".join(lines[1:]))
        if start is None or end is None or not body:
            skipped += 1
            continue
        raw.append((start, max(end, start), body))

    # 说话人标记要"重复出现"才算：只出现一次的冒号前缀退回正文
    prefixes = [prefix for prefix, _ in (_split_speaker(body) for _, _, body in raw) if prefix]
    counts = Counter(prefixes)
    cues: list[Cue] = []
    for start, end, body in raw:
        prefix, spoken = _split_speaker(body)
        if prefix and counts[prefix] >= 2 and spoken:
            cues.append(Cue(start, end, spoken, prefix))
        else:
            cues.append(Cue(start, end, body))
    return cues, skipped


def read_subtitle(filename: str, data: bytes) -> tuple[str, list[Cue], int]:
    """文件 → (默认项目名, 条目, 跳过的坏块数)。"""
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix not in SUPPORTED:
        raise ProducerError(f"不支持的字幕格式：{suffix or '无扩展名'}。可用 .srt。")
    if not data:
        raise ProducerError("文件是空的。")
    title = PurePosixPath(filename).stem.strip() or "未命名字幕"
    cues, skipped = parse_srt(_decode(data))
    if not cues:
        raise ProducerError(
            "字幕里没有可用的条目：检查时间行是不是 `00:00:00,000 --> 00:00:05,000`。"
        )
    return title, cues, skipped
