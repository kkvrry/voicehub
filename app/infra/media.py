"""音频容器的 MIME。

全项目只有这一份：合成结果直接回字节、章节成品下载、分章导出都从这里取。
认不出的容器一律 ``application/octet-stream``，不猜。
"""

from __future__ import annotations

__all__ = ["AUDIO_MIME", "audio_mime"]

#: 容器 → MIME。
AUDIO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "pcm": "application/octet-stream",
}

_FALLBACK = "application/octet-stream"


def audio_mime(value: str) -> str:
    """容器名或文件名 → MIME。``mp3``、``.MP3``、``/a/b.wav`` 都认。"""
    key = str(value or "").strip().lower()
    if "." in key:
        key = key.rsplit(".", 1)[-1]
    return AUDIO_MIME.get(key, _FALLBACK)
