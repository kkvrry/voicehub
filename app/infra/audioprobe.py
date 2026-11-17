"""音频时长探测。

ffprobe 先用配置的 ``ffprobe_path``，再看随包二进制，最后找 ``PATH``（多数用户已装好）。
三处都没有时只有 wav 读得出（标准库 ``wave``）；其余格式返回 ``0.0``（界面显示「未知」）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import wave
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from app.infra.config import load_config
from app.infra.files import file_stamp
from app.infra.process import CREATE_NO_WINDOW
from app.infra.runtime import bundled_binary

__all__ = [
    "AudioStream",
    "MediaInfo",
    "find_ffprobe",
    "probe_duration",
    "probe_duration_cached",
    "probe_many",
    "probe_media",
    "probe_stream",
]

#: PATH 探测只做一次（结果不会在一个进程里变）。
_PATH_FFPROBE: str | None = ""  # 空串 = 还没查过


def _resolve_ffprobe(configured: str) -> str:
    """配置 → 随包 → PATH；找不到返回空串（调用方跳过）。"""
    global _PATH_FFPROBE
    if configured:
        return configured
    bundled = bundled_binary("ffprobe")
    if bundled:
        return str(bundled)
    if _PATH_FFPROBE == "":
        _PATH_FFPROBE = shutil.which("ffprobe") or ""
    return _PATH_FFPROBE


def find_ffprobe(configured: str | None = None) -> str:
    """ffprobe 的落点；``None`` = 读配置（合并、字幕等调用方共用这一处）。"""
    if configured is None:
        configured = load_config().ffprobe_path
    return _resolve_ffprobe(configured)


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / float(rate) if rate else 0.0
    except (wave.Error, OSError, EOFError):
        return 0.0


def _ffprobe_duration(path: Path, ffprobe: str) -> float:
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            # Windows 的中文环境下 locale 是 GBK，ffprobe 输出的是 UTF-8：
            # 路径里有中文时读线程解码失败，stdout 直接变空，时长静默成 0。
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return 0.0


def probe_duration(path: Path, *, ffprobe_path: str = "") -> float:
    """时长（秒）；读不出 = ``0.0``。ffprobe 用配置的，没配置就找 PATH 上的。

    WAV 先自己读头（不起进程）；**读不动才走 ffprobe**——浮点 WAV（``format 3``）与
    ``WAVE_FORMAT_EXTENSIBLE`` 都不是 :mod:`wave` 认得的那种，别因此把它们当成"没时长"
    （从别的工具导出的 wav 常是这两种，界面上一整批样例没有秒数就是这么来的）。
    """
    if path.suffix.lower() == ".wav":
        seconds = _wav_duration(path)
        if seconds > 0:
            return seconds
    ffprobe = _resolve_ffprobe(ffprobe_path)
    if ffprobe:
        return _ffprobe_duration(path, ffprobe)
    return 0.0


def probe_stream(path: Path, *, ffprobe_path: str = "") -> tuple[str, int, int]:
    """音频流的 ``(编码, 采样率, 声道数)``；读不到返回三个空值。

    concat 要求各段流参数一致，合并前用它比对（m4a 中间产物与第一章）。
    空值之间相等——"两段都读不到"不构成"可以接"的结论，调用方要自己看第一个元素。
    """
    ffprobe = _resolve_ffprobe(ffprobe_path)
    if not ffprobe:
        return ("", 0, 0)
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ("", 0, 0)
    try:
        streams = json.loads(out.stdout or b"{}").get("streams") or []
        stream = streams[0]
        return (
            str(stream.get("codec_name") or ""),
            int(stream.get("sample_rate") or 0),
            int(stream.get("channels") or 0),
        )
    except (ValueError, TypeError, IndexError, AttributeError):
        return ("", 0, 0)


@dataclass(frozen=True, slots=True)
class AudioStream:
    """容器里的一条音频流。多语言 / 评论轨的容器靠它让用户选。"""

    index: int
    codec: str
    rate: int
    channels: int
    language: str = ""
    title: str = ""
    default: bool = False

    def label(self) -> str:
        """界面上的那一行：``#0 aac 2ch 中文（国语）``。"""
        parts = [f"#{self.index}", self.codec or "未知编码"]
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.language:
            parts.append(self.language)
        if self.title:
            parts.append(f"（{self.title}）")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """一个媒体文件的形状：时长、画面、全部音频流。

    时长读不出来时是 ``0.0``——调用方必须据此拒收：时长未知就生成不了时间轴，
    强行按 0 处理会让整条字幕压在开头。
    """

    duration: float
    format_name: str = ""
    width: int = 0
    height: int = 0
    video_codec: str = ""
    audio: tuple[AudioStream, ...] = ()

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)

    @property
    def default_audio(self) -> AudioStream | None:
        """默认音轨：有 ``default`` 标记取它，否则取第一条；没有音轨返回 ``None``。"""
        for stream in self.audio:
            if stream.default:
                return stream
        return self.audio[0] if self.audio else None


def probe_media(path: Path, *, ffprobe_path: str = "") -> MediaInfo:
    """读一个媒体文件的时长、画面与音频流；读不到时返回全空的 :class:`MediaInfo`。

    只跑一次 ffprobe：抽音轨前要选流、界面要显示"容器内几条音轨"、
    时间轴要用总时长，三处问的是同一份结构，不该各跑一次子进程。
    """
    ffprobe = _resolve_ffprobe(ffprobe_path)
    if not ffprobe:
        return MediaInfo(duration=0.0)
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name",
                "-show_entries",
                "stream=index,codec_type,codec_name,sample_rate,channels,width,height",
                "-show_entries",
                "stream_tags=language,title",
                "-show_entries",
                "stream_disposition=default",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return MediaInfo(duration=0.0)
    try:
        payload = json.loads(out.stdout or b"{}")
        info = payload.get("format") or {}
        duration = float(info.get("duration") or 0.0)
        streams = payload.get("streams") or []
    except (ValueError, TypeError, AttributeError):
        return MediaInfo(duration=0.0)

    audio: list[AudioStream] = []
    video_codec = ""
    width = height = 0
    position = 0
    for stream in streams:
        kind = str(stream.get("codec_type") or "")
        if kind == "video" and not video_codec:
            video_codec = str(stream.get("codec_name") or "")
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        elif kind == "audio":
            tags = stream.get("tags") or {}
            audio.append(
                AudioStream(
                    index=position,
                    codec=str(stream.get("codec_name") or ""),
                    rate=int(stream.get("sample_rate") or 0),
                    channels=int(stream.get("channels") or 0),
                    language=str(tags.get("language") or ""),
                    title=str(tags.get("title") or ""),
                    default=bool((stream.get("disposition") or {}).get("default")),
                )
            )
            position += 1
    if duration <= 0:
        # 部分容器的 format.duration 为空（流式 mp4、m4b），退回逐条流求和。
        longest = 0.0
        for stream in streams:
            with_duration = float(stream.get("duration") or 0.0)
            longest = max(longest, with_duration)
        duration = longest
    return MediaInfo(
        duration=round(duration, 3),
        format_name=str(info.get("format_name") or ""),
        width=width,
        height=height,
        video_codec=video_codec,
        audio=tuple(audio),
    )


#: 进程内备忘：``文件指纹 → 时长``（指纹见 :func:`app.infra.files.file_stamp`）。
#:
#: 列表与详情每次都会问生成样例的时长，而一次 ffprobe 是**一个子进程**（上限 10 秒）。
#: 键里带大小 + mtime_ns，文件一动就不再命中——与 ``DurationCache`` 同一口径，只是不进库。
_MEMO: dict[tuple[str, int, int], float] = {}
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 4096


def probe_duration_cached(path: Path, *, ffprobe_path: str = "") -> float:
    """同 :func:`probe_duration`，但同一条文件只量一次。

    只记"量出来了"的（``> 0``）：读不出多半是临时性失败（文件被占用 / ffprobe 未装），
    记成 0 会把它永久钉成"未知"。
    """
    key = file_stamp(path)
    if key is None:
        return 0.0
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None:
        return hit
    duration = probe_duration(path, ffprobe_path=ffprobe_path)
    if duration > 0:
        with _MEMO_LOCK:
            if len(_MEMO) >= _MEMO_MAX:
                _MEMO.clear()  # 上限只防"扫过几万个文件"这一种情况
            _MEMO[key] = duration
    return duration


#: 并发上限。瓶颈是 ffprobe 进程启动的 CPU，4 并发后收益趋零，封顶即可。
_WORKERS_MAX = 8


def _workers() -> int:
    return max(2, min(_WORKERS_MAX, (os.cpu_count() or 4) * 2))


def probe_many(paths: Sequence[Path], *, ffprobe_path: str = "") -> list[float]:
    """一次量一批时长，返回顺序与输入一致。

    没有 ffprobe 时不开线程池：只有 wav 读得出，且读 wav 是纯内存操作。
    想更快只有别重复量（见 :class:`app.infra.duration_cache.DurationCache`）。
    """
    items = list(paths)
    if not items:
        return []
    ffprobe = _resolve_ffprobe(ffprobe_path)
    if not ffprobe:
        return [_wav_duration(path) if path.suffix.lower() == ".wav" else 0.0 for path in items]
    workers = min(_workers(), len(items))
    if workers <= 1:
        return [probe_duration(path, ffprobe_path=ffprobe) for path in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # 走 probe_duration 保证 wav 短路判据两条路一致；map 保序供调用方对齐回填。
        return list(pool.map(lambda path: probe_duration(path, ffprobe_path=ffprobe), items))
