"""字幕 cue：本插件用到的名字，实现在宿主 ``app.usecase.subtitles``。

有声书与配音写的是同一种字幕，格式与切分不该有两份：这里只保留本插件
历史上使用的导出名，改行为一律改宿主那份。
"""

from __future__ import annotations

from app.usecase.subtitles import (
    SUBTITLE_ENCODING,
    SUBTITLE_MAX_CHARS,
    lrc_text,
    lrc_time,
    split_cues,
    split_subtitle_text,
    srt_text,
    srt_time,
)

__all__ = [
    "SUBTITLE_ENCODING",
    "SUBTITLE_MAX_CHARS",
    "lrc_text",
    "lrc_time",
    "split_cues",
    "split_subtitle_text",
    "srt_text",
    "srt_time",
]
