"""成品阶段：逐行音频 → 章节成品 / 全书 / BGM 版 / ZIP，含字幕与时间轴。

合并的两条路：书稿项目按「章内逐行拼接」，字幕项目按「时间轴补静音」。
混采样率不能交给 concat 解复用器（它按第一个文件解释全部片段），
要么先统一采样率，要么走 aresample + concat 滤镜——这一点决定成品时长对不对。

`ffmpeg` 一律经 :func:`app.infra.ffmpeg.find_ffmpeg` 查找：先用户配置、再随包、后 PATH。
"""

from __future__ import annotations

import hashlib
import random
import re
import subprocess
import threading
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import TYPE_CHECKING, Any

from app.infra.audioprobe import (
    find_ffprobe,
    probe_duration,
    probe_duration_cached,
    probe_stream,
)
from app.infra.ffmpeg import find_ffmpeg
from app.infra.loghub import CHANNEL
from app.infra.process import CREATE_NO_WINDOW, stop
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.cues import (
    SUBTITLE_ENCODING,
    SUBTITLE_MAX_CHARS,
    lrc_text,
    split_cues,
    srt_text,
)
from app.producers.builtin.audiobook.naming import safe_name
from app.producers.builtin.audiobook.paths import abs_path

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

__all__ = ["MergeStage"]

#: 成品格式 → 编码器；WAV 无损，码率对它无意义。
_MERGE_CODECS = {"mp3": "libmp3lame", "wav": "pcm_s16le", "m4a": "aac",
                 "wav_float": "pcm_f32le"}

#: 合并时统一到这一档采样率：逐行产物各家不同（MiMo 24 kHz、BreezeTTS 44.1 kHz），
#: 而 concat 解复用器按第一个文件解释全部片段——不统一就谈不上时间轴。
_CONCAT_RATE = 44100

#: 一次 filter_complex 最多塞多少个输入：Windows 命令行 32k 上限，
#: 60 个（约 12–15k 字符）留足余量。
_CONCAT_BATCH = 60

#: 时间轴拼装容差：变速后的实际时长与 ffprobe 读数差几毫秒是常态，
#: 这个量级的「顺延」听众听不出来，报出来只是噪音。
_TIMELINE_TOLERANCE_MS = 30

#: 贴齐时至少值得变速多少（0.5%）：再小就只为几毫秒重编码一行，不划算。
_FIT_MIN_GAIN = 1.005

#: 字幕文本里要去掉的表达标注：``[[笑]]`` 念不出来，也不该写进字幕。
_SUBTITLE_MARK = re.compile(r"\[\[.*?\]\]")


def _concat_line(path: Path) -> str:
    """concat 清单的一行（``file '...'``）。

    ffmpeg 的清单用单引号包路径，路径里的单引号必须断开再转义。项目名允许
    撇号（``naming.py``），书名带 ``'`` 时不转义会把这一行截断、连后面的文件
    一起读错。
    """
    text = path.as_posix()
    return "file '" + text.replace("'", "'\\''") + "'"


def _meta_escape(text: str) -> str:
    """ffmetadata 的值转义：``\\ ; # =`` 是它的分隔符，出现在值里要加反斜杠。

    书名/章节名里带 ``=``、``;``、``#`` 时，不转义会把一行拆成"未知键 + 半截值"，
    ffmpeg 要么报错要么把章节标题写坏。换行由调用方先折叠成空格。
    """
    return re.sub(r"([\\;#=])", r"\\\1", text)


#: 成品标签里的来源地址。只写公开仓库；没有就退成项目 id——本地路径会带出
#: 用户名与盘符，随文件外流，且在别人机器上无法定位。
SOURCE_URL = "https://github.com/kkvrry/voicehub"

#: 能承载标签的容器 → ffmpeg 输出格式名。WAV 只有 RIFF INFO，认得书名与备注，
#: 不认专辑艺术家与作曲人（写进去会被丢掉）。
_TAG_FORMATS = {"m4a": "ipod", "mp3": "mp3", "wav": "wav"}


def _book_tags(project: Mapping[str, Any], engines: Sequence[str]) -> dict[str, str]:
    """书级标签：专辑=书名，专辑艺术家=作者，作曲人=引擎，备注=来源地址。

    ``artist`` 不写：标签里的 artist 是"演唱者"，填引擎名会被书库当成一位艺人。
    书名取项目名（用户改过就是改后的值，不是源文件名），空值不写。
    """
    project_id = str(project.get("id") or "").strip()
    author = str(project.get("author") or "").strip()
    tags = {
        "album": str(project.get("name") or "").strip(),
        "album_artist": author,
        "composer": "、".join(
            dict.fromkeys(str(item).strip() for item in engines if str(item).strip())
        ),
        "comment": SOURCE_URL or (f"audiobook:{project_id}" if project_id else ""),
        "genre": "Audiobook",
    }
    return {key: value for key, value in tags.items() if value}


def _chapter_tags(
    project: Mapping[str, Any],
    chapter: Mapping[str, Any],
    engines: Sequence[str],
) -> dict[str, str]:
    """章级标签：书级标签 + 章名 + 序号（序号从 1 起）。"""
    tags = _book_tags(project, engines)
    tags["title"] = str(chapter.get("title") or "").strip()
    tags["track"] = str(int(chapter["ordinal"]) + 1)
    return {key: value for key, value in tags.items() if value}


#: 章节前后没留静音时 BGM 的过渡时长：淡入短于淡出，开场不该拖。
_BGM_FADE_IN_FALLBACK = 1.0
_BGM_FADE_OUT_FALLBACK = 1.5

#: 同一项目的合并互斥。合并是同步请求：浏览器刷新不会让它中断，但界面也看不到它，
#: 于是"再点一次合并"就成了两个 ffmpeg 写同一个文件。按项目加锁，后来者直接报忙。
_MERGE_LOCKS: dict[str, threading.RLock] = {}
_MERGE_LOCKS_GUARD = threading.Lock()


def _merge_lock(project_id: str) -> threading.RLock:
    with _MERGE_LOCKS_GUARD:
        return _MERGE_LOCKS.setdefault(project_id, threading.RLock())


class MergeCancelled(ProducerError):
    """用户停止合并；不更新成品与章节状态。"""


@dataclass
class _MergeControl:
    project_id: str
    event: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self) -> None:
        if self.event.is_set():
            raise MergeCancelled("合并已停止。")

    def attach(self, process: subprocess.Popen) -> None:
        with self.lock:
            self.process = process

    def detach(self, process: subprocess.Popen) -> None:
        with self.lock:
            if self.process is process:
                self.process = None

    def stop(self) -> None:
        self.event.set()
        with self.lock:
            process = self.process
        if process is not None:
            stop(process)


def _bgm_level_chain(gain_db: float, normalize: bool) -> str:
    """单曲的电平处理：先按 EBU R128 拉平响度，再按用户增益衰减。

    ``aformat`` 落在末尾是给 ``concat`` 用的：曲目之间采样率、声道往往不一致，
    不先统一，多曲相接时后面几首会按第一首的解释去读，出来是变调或杂音。
    """
    chain = ""
    if normalize:
        chain += "loudnorm=I=-16:TP=-2.0:LRA=11,aresample=44100,"
    chain += (
        f"volume={gain_db}dB,"
        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    )
    return chain


def _bgm_fades_chain(fade_in: float, fade_out_start: float, fade_out: float) -> str:
    """整章一道淡入淡出，接在拼接之后：曲间不淡，只有成品头尾各一次。"""
    chain = ""
    if fade_in > 0:
        chain += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        chain += f",afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"
    return chain


def _bgm_filter(
    gain_db: float,
    normalize: bool,
    fade_in: float = 0.0,
    fade_out_start: float = 0.0,
    fade_out: float = 0.0,
) -> str:
    """读不到成品时长时的退路：一首曲子循环铺满整章。

    正常情况下走 :func:`_bgm_playlist_filter`（抽多首、一曲接一曲）；只有
    ffprobe 读不出人声长度时才知道不了要铺多久，这时退回单曲循环。
    用 ``aloop`` 而不是 ``-stream_loop``：后者在输出结束后仍不释放无限输入，
    ffmpeg 会一直挂着不退出（Windows / ffmpeg 6.0 实测）。
    """
    return (
        _bgm_level_chain(gain_db, normalize)
        + ",aloop=loop=-1:size=2147483647"
        + _bgm_fades_chain(fade_in, fade_out_start, fade_out)
    )


def _bgm_playlist_filter(
    count: int,
    gain_db: float,
    normalize: bool,
    fade_in: float = 0.0,
    fade_out_start: float = 0.0,
    fade_out: float = 0.0,
) -> str:
    """多曲顺序相接的 BGM 侧滤镜：每曲各自拉平电平，按抽签顺序拼成一条，末尾淡变。

    ``count`` 是曲目数，输入 1…count 为曲目、输入 0 是人声；返回的图以 ``[b]``
    收尾，调用方接 ``[0:a][b]amix…``。
    """
    level = _bgm_level_chain(gain_db, normalize)
    parts = [f"[{index + 1}:a]{level}[c{index}]" for index in range(count)]
    joined = "".join(f"[c{index}]" for index in range(count))
    fades = _bgm_fades_chain(fade_in, fade_out_start, fade_out)
    parts.append(f"{joined}concat=n={count}:v=0:a=1{fades}[b]")
    return ";".join(parts)


def _bgm_fades(total: float, lead_ms: int, tail_ms: int) -> tuple[float, float, float]:
    """BGM 的淡入淡出落在章节前后那段静音里：人声起时已经到位，收尾时落回静音。

    返回 ``(淡入时长, 淡出起点, 淡出时长)``，单位秒；总时长为 0（读不到时长）时
    全为 0，即不加淡变——没有长度依据就不猜。静音留了 0 时给一段短过渡：
    BGM 硬起硬收，比淡变更像断了。
    """
    if total <= 0:
        return 0.0, 0.0, 0.0
    fade_in = lead_ms / 1000 if lead_ms > 0 else min(_BGM_FADE_IN_FALLBACK, total / 4)
    fade_out = tail_ms / 1000 if tail_ms > 0 else min(_BGM_FADE_OUT_FALLBACK, total / 4)
    # 前后静音加起来长过成品（静音被截）时等比例让位：淡出起点不许为负。
    if fade_in + fade_out > total:
        scale = total / (fade_in + fade_out)
        fade_in *= scale
        fade_out *= scale
    return fade_in, total - fade_out, fade_out


#: 每次合并重新抽曲：同一章重跑一遍，听到的 BGM 与上次不同。
_BGM_RANDOM = random.Random()

#: 读不出曲目时长时按这个秒数估：只用来推进抽样，不参与混音。
_BGM_FALLBACK_SECONDS = 180.0

#: 一章最多抽多少首：防止时长读错（例如 ffprobe 给出天文数字）时拼出上千路输入。
_BGM_MAX_TRACKS = 64

def _track_seconds(probe: str, path: Path) -> float:
    """曲目时长（秒）；读不到返回 0。一章要抽十几首，同一文件只量一次。"""
    return probe_duration_cached(path, ffprobe_path=probe)


def _bgm_playlist(tracks: list[Path], need: float, probe: str) -> list[Path]:
    """随机抽曲，抽够 ``need`` 秒为止：一曲放完接下一曲，不做单曲循环。

    一轮之内不重复，让曲库尽量都露一次面；书比曲库长时再洗一轮，并跳过与上一轮
    末尾相同的那首——连着两遍同一曲最刺耳。读不出时长的按 3 分钟估，保证推进。
    """
    if not tracks:
        return []
    picked: list[Path] = []
    length = 0.0
    pool: list[Path] = []
    while length < need and len(picked) < _BGM_MAX_TRACKS:
        if not pool:
            pool = list(tracks)
            _BGM_RANDOM.shuffle(pool)
            if len(pool) > 1 and picked and pool[0] == picked[-1]:
                pool.append(pool.pop(0))
        track = pool.pop(0)
        picked.append(track)
        length += _track_seconds(probe, track) or _BGM_FALLBACK_SECONDS
    return picked


def _bgm_mix() -> str:
    """人声与 BGM 叠加。

    ``amix`` 默认把每条输入都除以输入数（两个人声电平整体会掉 6 dB）；
    BGM 的电平已经由「BGM 音量」控制，这里关掉自动归一，人声保持原样。
    人声峰值本来接近 0 dBFS，叠加后加一道限幅，避免个别行削波。
    """
    return (
        "amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        # latency=1 抵掉限幅器 5 ms 的前瞻延迟，字幕时间轴不受影响。
        "alimiter=limit=0.95:level=0:latency=1"
    )

class MergeStage:
    """成品阶段的实现；门面（``AudiobookService``）只负责转调。"""

    def __init__(self, service: AudiobookService) -> None:
        self.svc = service
        self._progress: dict[str, dict[str, Any]] = {}
        self._controls: dict[str, _MergeControl] = {}
        self._controls_guard = threading.Lock()

    def progress(self, project_id: str) -> dict[str, Any]:
        return self._progress.get(project_id, {"stage": "idle", "message": "", "done": 0, "total": 0})

    def cancel(self, project_id: str) -> bool:
        with self._controls_guard:
            control = self._controls.get(project_id)
        if control is None:
            return False
        self._report(project_id, "cancelling", 0, 0, "正在停止合并")
        control.stop()
        return True

    def _report(self, project_id: str, stage: str, done: int, total: int, message: str) -> None:
        self._progress[project_id] = {
            "stage": stage, "message": message, "done": done, "total": total,
        }

    @property
    def store(self) -> Any:
        return self.svc.store

    @property
    def settings(self) -> Any:
        return self.svc.settings

    def _merged_dir(self, project_id: str) -> Path:
        folder = self.svc._project_dir(project_id) / "merged"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _engines(
        self,
        project_id: str,
        rows: Sequence[Mapping[str, Any]],
        labels: dict[str, str],
    ) -> list[str]:
        """这批行用到的引擎显示名，按首次出现排序。

        只认已进成品的行（调用方传的就是带音频的行）：没生成的行不该出现在标签里。
        停用或卸载的插件取不到 ``label``，退回 key——标签写不出来不值得让导出失败。
        ``labels`` 是本次操作的显示名缓存：整本导出逐章调用，插件只装一次；
        没有任何行分配引擎时连装都不装。
        """
        voices = self.store.voice_map(project_id)
        keys: list[str] = []
        for row in rows:
            engine = str((voices.get(str(row.get("speaker") or "")) or {}).get("engine") or "")
            if engine and engine not in keys:
                keys.append(engine)
        if not keys:
            return []
        if not labels:
            from app.engines.registry import list_engines

            labels.update(
                {
                    key: str(getattr(plugin, "label", "") or key)
                    for key, plugin in list_engines().items()
                }
            )
        return [labels.get(key, key) for key in keys]

    def _tag_copy(
        self, source: Path, target: Path, tags: Mapping[str, str]
    ) -> bool:
        """流拷贝一份带标签的副本，返回是否写成。

        只重写标签，不重新编码。源文件损坏、容器不支持标签时返回 ``False``——
        让调用方退回原文件：标签缺失比导出失败轻。
        """
        fmt = _TAG_FORMATS.get(source.suffix.lower().lstrip("."))
        binary = find_ffmpeg()
        if not tags or fmt is None or not binary:
            return False
        command = [
            binary, "-y", "-loglevel", "error", "-i", str(source),
            "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1",
        ]
        for key, value in tags.items():
            command += ["-metadata", f"{key}={value}"]
        command += ["-f", fmt, str(target)]
        try:
            self._run_ffmpeg(command)
        except ProducerError:
            target.unlink(missing_ok=True)
            return False
        return target.is_file() and target.stat().st_size > 0

    def _concat(
        self,
        binary: str,
        fmt: str,
        bitrate: str,
        inputs: list[Path],
        output: Path,
        folder: Path,
        metadata: Mapping[str, str] | None = None,
        *,
        control: _MergeControl | None = None,
    ) -> None:
        """把若干音频按顺序拼成一个成品。

        **不用 concat 解复用器**：它按**第一个文件**的流参数去解释后面所有文件，
        采样率不一致时（MiMo 的逐行产物是 24 kHz，静音是 44.1 kHz）时长会走样
        ——实测 1.5 秒的内容拼出来只有 1.15 秒，加 `-ar` / `aresample` 都救不回来。
        所以每段先重采样到同一档（44.1 kHz 单声道 s16），再用 concat 滤镜拼。

        输入太多时分批（Windows 命令行 32k 上限）：批内走滤镜，批产物是同一档的
        WAV，批间再用解复用器拼——那时已经不存在混采样率的问题。
        """
        codec = _MERGE_CODECS.get(fmt)
        if not codec:
            raise ProducerError(f"不支持的合并格式：{fmt}")
        if not inputs:
            raise ProducerError("没有可合并的音频。")

        if len(inputs) <= _CONCAT_BATCH:
            self._concat_filter(
                binary, inputs, output, fmt=fmt, bitrate=bitrate,
                metadata=metadata, control=control,
            )
            return

        parts: list[Path] = []
        try:
            for index in range(0, len(inputs), _CONCAT_BATCH):
                if control is not None:
                    control.check()
                part = folder / f"part-{output.stem}-{index // _CONCAT_BATCH:03d}.wav"
                self._concat_filter(
                    binary, inputs[index : index + _CONCAT_BATCH], part,
                    fmt="wav_float" if fmt == "wav_float" else "wav", control=control,
                )
                parts.append(part)
            listing = folder / f"concat-{output.stem}.txt"
            listing.write_text("\n".join(_concat_line(path) for path in parts), encoding="utf-8")
            command = [binary, "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
            command += self._encode_args(fmt, bitrate, metadata) + [str(output)]
            self._run_ffmpeg(command, listing, control)
        finally:
            for part in parts:
                part.unlink(missing_ok=True)

    def _concat_filter(
        self,
        binary: str,
        inputs: list[Path],
        output: Path,
        *,
        fmt: str,
        bitrate: str = "",
        metadata: Mapping[str, str] | None = None,
        control: _MergeControl | None = None,
    ) -> None:
        """滤镜式拼接：每段 aresample 到同一档再 concat——采样率一统，时长才对得上。"""
        command = [binary, "-y"]
        for path in inputs:
            command += ["-i", str(path)]
        sample_format = "flt" if fmt == "wav_float" else "s16"
        chains = "".join(
            f"[{index}:a]aresample={_CONCAT_RATE},"
            f"aformat=sample_fmts={sample_format}:channel_layouts=mono"
            f"[a{index}];"
            for index in range(len(inputs))
        )
        labels = "".join(f"[a{index}]" for index in range(len(inputs)))
        command += [
            "-filter_complex",
            f"{chains}{labels}concat=n={len(inputs)}:v=0:a=1[out]",
            "-map",
            "[out]",
            *self._encode_args(fmt, bitrate, metadata),
            str(output),
        ]
        self._run_ffmpeg(command, None, control)

    @staticmethod
    def _encode_args(
        fmt: str, bitrate: str, metadata: Mapping[str, str] | None
    ) -> list[str]:
        """编码参数：压缩格式带码率与标签；M4A 追加 faststart。"""
        args = ["-c:a", _MERGE_CODECS[fmt]]
        if fmt in ("mp3", "m4a"):
            args += ["-b:a", bitrate]
            for key, value in (metadata or {}).items():
                if str(value or "").strip():
                    args += ["-metadata", f"{key}={value}"]
            if fmt == "m4a":
                args += ["-movflags", "+faststart"]
        return args

    def _run_ffmpeg(
        self,
        command: list[str],
        listing: Path | None = None,
        control: _MergeControl | None = None,
    ) -> None:
        """跑一条 ffmpeg 命令；停止时杀掉进程，失败时报最后一句话。"""
        control = control or _MergeControl("")
        control.check()
        try:
            with TemporaryFile() as errors:
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=errors,
                        creationflags=CREATE_NO_WINDOW,
                    )
                except OSError as exc:
                    raise ProducerError(f"ffmpeg 合并失败：{exc}") from exc
                control.attach(process)
                try:
                    while True:
                        try:
                            process.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            if control.event.is_set():
                                raise MergeCancelled("合并已停止。") from None
                finally:
                    control.detach(process)
                    stop(process)
                errors.seek(0)
                stderr = errors.read().decode("utf-8", "replace")
        finally:
            if listing is not None:
                listing.unlink(missing_ok=True)
        if control.event.is_set():
            raise MergeCancelled("合并已停止。")
        if process.returncode != 0:
            tail = stderr.strip().splitlines()[-1:]
            raise ProducerError(f"ffmpeg 合并失败：{' / '.join(tail) or '未知错误'}")

    @staticmethod
    def _ffmpeg() -> str:
        binary = find_ffmpeg()
        if not binary:
            raise ProducerError("未找到 ffmpeg，无法合并。")
        return binary

    def _write_subtitles(
        self,
        chapter: dict[str, Any],
        rows: list[dict[str, Any]],
        paths: list[Path],
        folder: Path,
        stem: str,
        gap_ms: int,
        start_ms: int = 0,
        control: _MergeControl | None = None,
    ) -> tuple[dict[str, str], float]:
        """按逐行音频的真实时长写 SRT / LRC；没有 ffprobe 就跳过，不影响合并。

        返回 ``(写出的文件, 最后一条字幕的结束秒)``——后者给成品自检对时长用。
        """
        del chapter
        probe = find_ffprobe()
        if not probe:
            CHANNEL.warning(
                "没有 ffprobe，字幕跳过（音频照常合并）", source="audiobook"
            )
            return {}, 0.0
        cues: list[tuple[float, float, str]] = []
        cursor = max(0, start_ms) / 1000
        for row, path in zip(rows, paths, strict=True):
            if control is not None:
                control.check()
            duration = probe_duration(path, ffprobe_path=probe)
            if duration <= 0:
                # 不能悄悄跳过：跳过后这条不出字幕，但它在成品里确实占时间，
                # 之后每一条都会提前这一行的时长，整章字幕全错。字幕项目那条路
                # （_timeline_segments）早就这么处理，这里与它统一口径。
                raise ProducerError(
                    f"第 {int(row['ordinal']) + 1} 行的音频读不出时长：{path.name}"
                )
            text = _SUBTITLE_MARK.sub("", str(row["text"] or "")).strip()
            text = " ".join(text.split())
            if text:
                cues.append((cursor, cursor + duration, text))
            cursor += duration + gap_ms / 1000
        if not cues:
            return {}, 0.0
        cues = split_cues(cues, SUBTITLE_MAX_CHARS)
        written: dict[str, str] = {}
        if self.svc.settings.subtitle_srt:
            srt = folder / f"{stem}.srt"
            srt.write_text(srt_text(cues), encoding=SUBTITLE_ENCODING)
            written["srt"] = str(srt)
        if self.svc.settings.subtitle_lrc:
            lrc = folder / f"{stem}.lrc"
            lrc.write_text(lrc_text(cues), encoding=SUBTITLE_ENCODING)
            written["lrc"] = str(lrc)
        return written, cues[-1][1]

    def _timeline_segments(
        self,
        project_id: str,
        rows: list[dict[str, Any]],
        binary: str,
        folder: Path,
        root: Path | None = None,
        control: _MergeControl | None = None,
    ) -> tuple[list[Path], list[tuple[float, float, str]], list[int], list[int]]:
        """字幕项目的拼装计划：按原时间码排片段。

        - 指针早于这一条的 ``start_ms``：补一段静音对齐；
        - 指针已经越过它：**不截断、不丢弃**，从当前位置接着放，记进「顺延」；
        - 开了「变速贴齐」（``subtitle_fit``）时，长过时隙的行先加速再放：贴得进去
          就不算顺延。**贴不进去的整条不动**——加速到上限仍然越界的话，语气白白被
          改变、字幕照样错位，不值；把上限调大就能贴更多行。
        - 只加速、不拉长：字幕时隙通常比语音长（字幕要停留到下一句），主动拖慢是
          另一种处理，不是「对齐」。
        - 每行音频的真实时长用 ffprobe 量，量不出直接报错——当成 0 会让后面全错位。

        静音按 10 毫秒量化、按毫秒复用文件：误差不会累积——每一条的间隔都按
        「已经写到的位置」算，上一条多写或少写几毫秒，下一条自己吃掉。
        """
        probe = find_ffprobe()
        if not probe:
            raise ProducerError(
                "字幕项目按时间轴拼装需要 ffprobe：随包的 ffprobe 应在程序目录里。"
            )
        # 项目根取一次：这一版以前是每行 ``resolve_audio``，一行一次 SQLite。
        base = root or self.svc.project_folder(project_id)
        fit = bool(self.svc.settings.subtitle_fit)
        # atempo 单级只认 0.5–2.0；配置被手改到更大也不让 ffmpeg 失败，夹住就好。
        max_speed = min(2.0, max(1.0, float(self.svc.settings.subtitle_fit_max)))
        inputs: list[Path] = []
        cues: list[tuple[float, float, str]] = []
        delayed: list[int] = []
        fitted: list[int] = []
        kept: set[Path] = set()
        cursor = 0
        for index, row in enumerate(rows):
            if control is not None:
                control.check()
            start = max(0, int(row.get("start_ms") or 0))
            gap = start - cursor
            if gap < 0:
                if -gap > _TIMELINE_TOLERANCE_MS:
                    delayed.append(int(row["ordinal"]) + 1)
                gap = 0
            quantized = round(gap / 10.0) * 10
            if quantized > 0:
                silence = self._silence_file(binary, folder, quantized, control)
                if silence is not None:
                    inputs.append(silence)
                    cursor += quantized
            path = abs_path(base, row["audio"])
            duration = probe_duration(path, ffprobe_path=probe)
            if duration <= 0:
                raise ProducerError(f"第 {int(row['ordinal']) + 1} 行的音频读不出时长：{path.name}")
            if fit:
                space = self._slot_end(rows, index) - max(cursor, start)
                need = duration / (space / 1000) if space > 0 else 0.0
                if _FIT_MIN_GAIN < need <= max_speed:
                    path = self._speed_file(binary, folder, row, path, need, probe, control)
                    kept.add(path)
                    duration = probe_duration(path, ffprobe_path=probe)
                    if duration <= 0:
                        raise ProducerError(
                            f"第 {int(row['ordinal']) + 1} 行变速后读不出时长：{path.name}"
                        )
                    fitted.append(int(row["ordinal"]) + 1)
            inputs.append(path)
            text = _SUBTITLE_MARK.sub("", str(row["text"] or "")).strip()
            text = " ".join(text.split())
            end = cursor + round(duration * 1000)
            if text:
                cues.append((cursor / 1000, end / 1000, text))
            cursor = end
        # 上一次合并留下的变速产物：本次没用到的清掉（改倍率会换文件名，不清就积着）。
        for stale in folder.glob("fit-*.wav"):
            if stale not in kept:
                stale.unlink(missing_ok=True)
        return inputs, cues, delayed, fitted

    @staticmethod
    def _slot_end(rows: list[dict[str, Any]], index: int) -> int:
        """这一行能用到哪：下一条的起点；最后一条用它自己的 ``end_ms``。

        末条没有 ``end_ms``（0）时返回 0——没有依据就不猜，交给顺延。
        """
        if index + 1 < len(rows):
            return max(0, int(rows[index + 1].get("start_ms") or 0))
        return max(0, int(rows[index].get("end_ms") or 0))

    def _speed_file(
        self,
        binary: str,
        folder: Path,
        row: dict[str, Any],
        source: Path,
        speed: float,
        probe: str,
        control: _MergeControl | None = None,
    ) -> Path:
        codec = "pcm_f32le" if self.svc.settings.master_loudness_enabled else "pcm_s16le"
        stat = source.stat()
        fingerprint = hashlib.sha256(
            f"{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{codec}".encode()
        ).hexdigest()[:16]
        target = folder / f"fit-{row['id']}-{speed:.3f}-{fingerprint}.wav"
        if target.is_file() and probe_duration(target, ffprobe_path=probe) > 0:
            return target
        try:
            self._run_ffmpeg(
                [
                    binary,
                    "-y",
                    "-i",
                    str(source),
                    "-filter:a",
                    f"atempo={speed:.3f}",
                    "-c:a",
                    codec,
                    str(target),
                ],
                None,
                control,
            )
        except ProducerError as exc:
            if isinstance(exc, MergeCancelled):
                raise
            raise ProducerError(
                f"第 {int(row['ordinal']) + 1} 行变速失败：{exc}"
            ) from exc
        return target

    def _write_timeline_subtitles(
        self, cues: list[tuple[float, float, str]], folder: Path, stem: str
    ) -> dict[str, str]:
        """按**实际时间轴**写 SRT / LRC。

        与 :meth:`_write_subtitles` 的区别：不切 cue、不重排——时间码就是拼装时
        算出来的那一条（字幕模式里时间码是给定的，切了就不对齐了）。
        """
        written: dict[str, str] = {}
        if not cues:
            return written
        if self.svc.settings.subtitle_srt:
            srt = folder / f"{stem}.srt"
            srt.write_text(srt_text(cues), encoding=SUBTITLE_ENCODING)
            written["srt"] = str(srt)
        if self.svc.settings.subtitle_lrc:
            lrc = folder / f"{stem}.lrc"
            lrc.write_text(lrc_text(cues), encoding=SUBTITLE_ENCODING)
            written["lrc"] = str(lrc)
        return written

    def chapter_audio(self, project_id: str, chapter_id: str) -> Path:
        """章节的**最终产物**：开了 BGM 且这次合并出了 ``-bgm`` 版，就播那一版。

        关掉 BGM 后旧 ``-bgm`` 文件可能还在，所以以当前设置为准；没有 BGM 版
        （目录为空、混音失败或本来就关着）回退原声。
        """
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter or not chapter.get("audio"):
            raise ProducerError("本章尚未合并。")
        base = self.svc.resolve_audio(project_id, chapter["audio"])
        if not base.is_file():
            raise ProducerError("成品文件已不存在。")
        if self.svc.settings.bgm_enabled:
            mixed = base.with_name(f"{base.stem}-bgm{base.suffix}")
            if mixed.is_file():
                return mixed
        return base

    def chapter_subtitle(
        self, project_id: str, chapter_id: str, fmt: str = "srt"
    ) -> Path:
        suffix = fmt.lower().lstrip(".")
        if suffix not in ("srt", "lrc"):
            raise ProducerError("字幕格式只支持 srt / lrc。")
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        if not chapter["audio"]:
            raise ProducerError("本章尚未合并，没有字幕。")
        audio = self.svc.resolve_audio(project_id, chapter["audio"])
        target = audio.with_suffix(f".{suffix}")
        if not target.is_file():
            raise ProducerError("字幕还没生成：先合并本章。")
        return target

    def _silence_file(
        self, binary: str, folder: Path, ms: int,
        control: _MergeControl | None = None,
    ) -> Path | None:
        """按毫秒生成一段单声道静音，复用同名文件；0 表示不要。"""
        if ms <= 0:
            return None
        target = folder / f"silence-{ms}.wav"
        if target.is_file():
            return target
        self._run_ffmpeg(
            [
                binary,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                f"{ms / 1000:.3f}",
                str(target),
            ],
            None,
            control,
        )
        return target

    @staticmethod
    def _bgm_tracks(folder: str) -> list[Path]:
        base = Path(folder) if folder else None
        if not base or not base.is_dir():
            return []
        tracks = [
            path
            for path in base.iterdir()
            if path.is_file() and path.suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg")
        ]
        return sorted(tracks, key=lambda path: path.name)

    def _write_bgm_version(
        self,
        binary: str,
        chapter: dict[str, Any],
        voice: Path,
        folder: Path,
        lead_ms: int = 0,
        tail_ms: int = 0,
        fmt: str = "",
        metadata: Mapping[str, str] | None = None,
        *,
        control: _MergeControl | None = None,
    ) -> Path | None:
        """随机抽曲铺满整章，另出一版混音；原声版保持不动。

        ``lead_ms`` / ``tail_ms`` 是这一章首尾的静音长度：BGM 在其中淡入淡出。
        抽几首由人声长度决定：一曲放完接下一曲，不放单曲循环。``metadata`` 与
        原声版同一份标签——两版都是成品，谁被导出都该带着书名与引擎。
        """
        if not self.svc.settings.bgm_enabled:
            return None
        tracks = self._bgm_tracks(self.svc.settings.bgm_folder)
        if not tracks:
            CHANNEL.warning(
                "开了 BGM 但目录里没有可用曲目，跳过", source="audiobook"
            )
            return None
        fmt = fmt or self.svc.settings.merge_format
        if fmt not in _MERGE_CODECS:
            raise ProducerError(f"不支持的合并格式：{fmt}")
        suffix = "wav" if fmt == "wav_float" else fmt
        target = folder / f"{voice.stem}-bgm.{suffix}"
        gain = float(self.svc.settings.bgm_gain_db)
        normalize = bool(self.svc.settings.bgm_normalize)
        probe = find_ffprobe()
        total = probe_duration(voice, ffprobe_path=probe) if probe else 0.0
        fade_in, fade_start, fade_out = _bgm_fades(total, lead_ms, tail_ms)
        playlist: list[Path] = _bgm_playlist(tracks, total, probe) if total > 0 else []
        if playlist:
            graph = _bgm_playlist_filter(
                len(playlist), gain, normalize, fade_in, fade_start, fade_out
            )
        else:
            # 目录里没有曲目，或读不出人声长度就无从定「铺几首」，退回单曲循环。
            playlist = [_BGM_RANDOM.choice(tracks)]
            graph = f"[1:a]{_bgm_filter(gain, normalize, fade_in, fade_start, fade_out)}[b]"
        mix = _bgm_mix() if fmt != "wav_float" else (
            "amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "aformat=channel_layouts=mono"
        )
        command = [binary, "-y", "-i", str(voice)]
        for track in playlist:
            command += ["-i", str(track)]
        command += [
            "-filter_complex",
            f"{graph};[0:a][b]{mix}[a]",
            "-map",
            "[a]",
            *self._encode_args(fmt, self.svc.settings.merge_bitrate, metadata),
            str(target),
        ]
        try:
            self._run_ffmpeg(command, None, control)
        except MergeCancelled:
            raise
        except ProducerError as exc:
            CHANNEL.warning(
                f"BGM 混音失败，保留原声版：{exc}", source="audiobook"
            )
            return None
        if gain < -30:
            CHANNEL.warning(
                f"BGM 音量 {gain:g} dB 很低，混入后可能几乎听不见；-20 dB 左右比较清楚。",
                source="audiobook",
                project=str(chapter.get("project_id") or ""),
                chapter=str(chapter.get("id") or ""),
            )
        CHANNEL.info(
            f"BGM 已混入：{target.name} · {len(playlist)} 首"
            f"（{playlist[0].name}{' …' if len(playlist) > 1 else ''}）· {gain:g} dB"
            f"{f' · 淡入 {fade_in:.1f}s / 淡出 {fade_out:.1f}s' if fade_in or fade_out else ''}",
            source="audiobook",
            project=str(chapter.get("project_id") or ""),
            chapter=str(chapter.get("id") or ""),
        )
        return target

    def merge(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        """合并一章。同一项目同时只允许一路合并：重复提交直接报忙。"""
        lock = _merge_lock(project_id)
        if not lock.acquire(blocking=False):
            raise ProducerError("这个项目正在合并，等当前合并结束再试。")
        control = _MergeControl(project_id)
        with self._controls_guard:
            self._controls[project_id] = control
        self._report(project_id, "prepare", 0, 0, "准备章节音频")
        try:
            result = self._merge(project_id, chapter_id, control)
            self._report(project_id, "done", 1, 1, "合并完成")
            return result
        except MergeCancelled:
            self._report(project_id, "cancelled", 0, 0, "合并已停止")
            raise
        except Exception as exc:
            self._report(project_id, "failed", 0, 0, str(exc))
            raise
        finally:
            with self._controls_guard:
                self._controls.pop(project_id, None)
            lock.release()

    def _merge(
        self, project_id: str, chapter_id: str, control: _MergeControl
    ) -> dict[str, Any]:
        control.check()
        self.svc._ensure_idle(project_id)
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        # 先结算一次：改回去的行本来就该算数，不该拦着合并。
        self.svc._settle_stale(project_id, chapter_id)
        all_rows = self.svc.store.rows(chapter_id)
        stale = sum(1 for row in all_rows if row["status"] == "stale")
        if stale:
            # 过期音频听着还是那个声音，但角色 / 情绪 / 文本已经变了——混进成品就是错的。
            raise ProducerError(
                f"本章有 {stale} 行改过角色、情绪或文本，旧音频已过期："
                "重新生成这几行（或改回原样）再合并。"
            )
        rows = [row for row in all_rows if row["audio"]]
        if not rows:
            raise ProducerError("本章还没有已生成的音频。")

        started = time.perf_counter()
        settings = self.svc.settings
        binary = self._ffmpeg()
        root = self.svc._project_dir(project_id)
        folder = self._merged_dir(project_id)
        fmt = settings.merge_format
        output = folder / f"{int(chapter['ordinal']):03d}-{chapter_id}.{fmt}"
        timings: dict[str, float] = {}
        def progress(stage: str, done: int, total: int, message: str) -> None:
            self._report(project_id, stage, done, total, message)
        row_report = self.svc.loudness.process(
            project_id, settings, progress, cancelled=control.event.is_set,
            announce=False,
        )
        if row_report.get("cancelled"):
            raise MergeCancelled("合并已停止。")
        control.check()
        if settings.row_balance_enabled:
            plan = self.svc.loudness.plan(root)
            rows = [dict(row, audio=str(self.svc.loudness.row_audio(root, row, plan)[0]))
                    for row in rows]
        timings["row_loudness"] = row_report["seconds"]
        project = self.svc.store.project_meta(project_id) or {}
        # 字幕项目：时间码是硬约束，按绝对时间点排（见 `_timeline_segments`）；
        # 书稿项目一律走原来的「顺序拼接 + 固定间歇」，这条判断是唯一的岔口。
        subtitle = str(project.get("kind") or "book") == "subtitle"

        CHANNEL.info(
            f"开始合并：{chapter['title']} · {len(rows)} 行 · "
            f"{'字幕时间轴' if subtitle else '顺序拼接'}",
            source="audiobook",
            project=project_id,
            chapter=chapter_id,
        )
        delayed: list[int] = []
        fitted: list[int] = []
        cues: list[tuple[float, float, str]] = []
        # 章节首尾静音：书稿项目按设置补，字幕项目按给定时间码排，没有这一层。
        lead = 0
        tail = 0
        if subtitle:
            inputs, cues, delayed, fitted = self._timeline_segments(
                project_id, rows, binary, folder, root, control
            )
            expected = cues[-1][1] if cues else 0.0
        else:
            # 项目根已经在手，逐行再走一次 ``resolve_audio`` 就是逐行查一次库。
            row_paths = [abs_path(root, row["audio"]) for row in rows]
            gap = int(self.svc.settings.row_gap_ms)
            lead = int(self.svc.settings.chapter_lead_ms)
            tail = int(self.svc.settings.chapter_tail_ms)
            silence = self._silence_file(binary, folder, gap, control)
            lead_silence = self._silence_file(binary, folder, lead, control)
            tail_silence = self._silence_file(binary, folder, tail, control)
            spaced: list[Path] = []
            if lead_silence is not None:
                spaced.append(lead_silence)
            for index, path in enumerate(row_paths):
                if index and silence is not None:
                    spaced.append(silence)
                spaced.append(path)
            if tail_silence is not None:
                spaced.append(tail_silence)
            inputs = spaced or row_paths
            expected = 0.0

        metadata = _chapter_tags(project, chapter, self._engines(project_id, rows, {}))
        # 行级告警是全书统计；缓存命中时不在每章重复播报，只有真的做了测量/处理才带出。
        row_warnings = list(row_report.get("warnings", []))
        if not row_report.get("measured") and not row_report.get("rendered"):
            row_warnings = []
        loudness: dict[str, Any] = {
            "rows": row_report, "warnings": row_warnings, "master": {},
        }
        with TemporaryDirectory(prefix="merge-", dir=folder) as temporary:
            work = Path(temporary)
            master = settings.master_loudness_enabled
            source = work / (f"{output.stem}.wav" if master else output.name)
            progress("concat", 0, len(rows), f"正在拼接 {len(rows)} 行音频")
            at = time.perf_counter()
            self._concat(
                binary, "wav_float" if master else fmt, settings.merge_bitrate,
                inputs, source, work, metadata=metadata, control=control,
            )
            timings["concat"] = time.perf_counter() - at
            progress("bgm", 0, 1, "背景音乐混音")
            at = time.perf_counter()
            mixed = self._write_bgm_version(
                binary, chapter, source, work, lead, tail,
                fmt="wav_float" if master else "", metadata=metadata, control=control,
            )
            timings["bgm"] = time.perf_counter() - at
            bgm = output.with_name(f"{output.stem}-bgm{output.suffix}") if mixed else None
            final_voice = source
            final_bgm = mixed
            if master:
                products = [("voice", source, work / f"final-{output.name}", output)]
                if mixed and bgm:
                    products.append(("bgm", mixed, work / f"final-{bgm.name}", bgm))
                for name, original, staged, _ in products:
                    try:
                        report = self.svc.loudness.master(
                            binary, original, staged, settings, metadata, progress,
                            cancelled=control.event.is_set,
                        )
                    except ProducerError:
                        if control.event.is_set():
                            raise MergeCancelled("合并已停止。") from None
                        raise
                    loudness["master"][name] = report
                    loudness["warnings"].extend(report["warnings"])
                final_voice = products[0][2]
                final_bgm = products[1][2] if len(products) > 1 else None
            control.check()
            progress("subtitles", 0, 1, "字幕与成品时长自检")
            at = time.perf_counter()
            if subtitle:
                subtitles = self._write_timeline_subtitles(cues, folder, output.stem)
            else:
                subtitles, last_cue = self._write_subtitles(
                    chapter, rows, row_paths, folder, output.stem, gap,
                    start_ms=lead, control=control,
                )
                expected = last_cue + max(0, tail) / 1000
            check = self._check_product(final_bgm or final_voice, expected)
            timings["subtitles_check"] = time.perf_counter() - at
            control.check()
            final_voice.replace(output)
            if final_bgm is not None and bgm is not None:
                final_bgm.replace(bgm)
            else:
                output.with_name(f"{output.stem}-bgm{output.suffix}").unlink(missing_ok=True)
        timings["master_loudness"] = sum(
            report["seconds"] for report in loudness["master"].values()
        )
        loudness["warnings"] = list(dict.fromkeys(loudness["warnings"]))
        for warning in loudness["warnings"]:
            CHANNEL.warning(warning, source="audiobook", project=project_id, chapter=chapter_id)
        if check["checked"] and not check["ok"]:
            CHANNEL.warning(
                f"成品自检：{chapter['title']} {check['note']}",
                source="audiobook", project=project_id, chapter=chapter_id,
            )

        if not settings.row_balance_enabled:
            # 原声重合并后，旧行计划不再代表这一章成品；再次处理仍复用测量与派生文件。
            self.svc.loudness.discard_row_plan(root, {row["id"] for row in all_rows})
        self.svc.store.set_chapter_stage(chapter_id, "merged", output.relative_to(root).as_posix())
        CHANNEL.info(
            f"合并完成：{chapter['title']} · {len(rows)} 行 · "
            f"{time.perf_counter() - started:.1f} 秒"
            f"{f' · {len(fitted)} 行变速贴齐' if fitted else ''}"
            f"{f' · {len(delayed)} 行顺延' if delayed else ''}"
            f"{' · 字幕 ' + str(len(subtitles)) + ' 份' if subtitles else ''}"
            f"{' · 含 BGM 版' if bgm else ''}"
            f" · 行级 {timings['row_loudness']:.2f}s / 拼接 {timings['concat']:.2f}s"
            f" / BGM {timings['bgm']:.2f}s / 成品响度 {timings['master_loudness']:.2f}s"
            f" / 字幕自检 {timings['subtitles_check']:.2f}s",
            source="audiobook",
            project=project_id,
            chapter=chapter_id,
        )
        if delayed:
            CHANNEL.warning(
                f"以下字幕行因前面的音频超长而顺延：{delayed[:20]}"
                f"{'…' if len(delayed) > 20 else ''}",
                source="audiobook",
                project=project_id,
                chapter=chapter_id,
            )
        return {
            "audio": str(output),
            "bgm": str(bgm) if bgm else "",
            "subtitles": subtitles,
            "check": check,
            "loudness": loudness,
            "timings": timings,
            "delayed": delayed,
            "fitted": fitted,
            "url": f"/api/v1/producers/audiobook/projects/{project_id}/chapters/{chapter_id}/audio",
        }

    @staticmethod
    def _check_product(output: Path, expected: float) -> dict[str, Any]:
        """成品自检：量一遍真实时长，与「字幕时间轴 + 章尾静音」对一下。

        量不到就记「没查」，不等于通过；只报告，不阻断合并（2% 或 1 秒以内算对）。
        """
        probe = find_ffprobe()
        if not probe or expected <= 0:
            return {"checked": False, "note": "没有 ffprobe 或没有时间轴，没查。"}
        actual = probe_duration(output, ffprobe_path=probe)
        if actual <= 0:
            return {"checked": False, "note": "读不到成品时长，没查。"}
        drift = actual - expected
        ok = abs(drift) <= max(1.0, expected * 0.02)
        return {
            "checked": True,
            "duration": round(actual, 3),
            "expected": round(expected, 3),
            "drift": round(drift, 3),
            "ok": ok,
            "note": "" if ok else f"成品时长与时间轴差 {drift:+.1f} 秒。",
        }

    def _merged_chapters(self, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """分章导出的前置条件：项目存在、有章节、每一章都合并过。"""
        project = self.svc.store.project_meta(project_id)
        if not project:
            raise ProducerError("项目不存在。")
        chapters = self.svc.store.chapters(project_id)
        if not chapters:
            raise ProducerError("还没有章节。")
        missing = [item for item in chapters if item["stage"] != "merged" or not item["audio"]]
        if missing:
            names = "、".join(item["title"] for item in missing[:3])
            more = " 等" if len(missing) > 3 else ""
            raise ProducerError(f"还有 {len(missing)} 章未合并：{names}{more}。先逐章合并。")
        return project, chapters

    def export_chapters(self, project_id: str) -> Path:
        """分章导出：每章只放一份最终音频，条目名 ``序号-章节名``，打包成 ZIP。

        最终产物按当前设置判定：开了 BGM 且有混音版就打包混音版，否则原声——两版
        不一起塞进包里，听众要的就是这一版。字幕跟着同一份成品一起打包。
        条目名不带书名（多个项目混在一个播放器里时，书名只会干扰排序）也不带
        ``-bgm`` 后缀；开头的三位序号按章节顺序给出，本身就是唯一的。
        """
        lock = _merge_lock(project_id)
        if not lock.acquire(blocking=False):
            raise ProducerError("这个项目正在合并，等当前合并结束再导出。")
        try:
            return self._export(project_id)
        finally:
            lock.release()

    def _export(self, project_id: str) -> Path:
        self.svc._ensure_idle(project_id)
        project, chapters = self._merged_chapters(project_id)
        book = safe_name(project["name"])
        folder = self._merged_dir(project_id)
        target = folder / f"{book}.zip"
        temporary = target.with_suffix(".zip.part")
        temporary.unlink(missing_ok=True)
        bgm = bool(self.svc.settings.bgm_enabled)
        # 章成品上的标签是合并那一刻写的：书名、作者改过之后就不作数了。导出前
        # 逐章流拷贝重写一遍，包里的书名始终是当前值，也带上引擎。
        rows_by_chapter: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.all_rows(project_id):
            rows_by_chapter.setdefault(str(row["chapter_id"]), []).append(row)
        labels: dict[str, str] = {}
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for chapter in chapters:
                    path = self.svc.resolve_audio(project_id, chapter["audio"])
                    if not path.is_file():
                        raise ProducerError(f"章节成品已不存在：{chapter['title']}。重新合并本章。")
                    final = path
                    if bgm:
                        mixed = path.with_name(f"{path.stem}-bgm{path.suffix}")
                        if mixed.is_file():
                            final = mixed
                    order = f"{int(chapter['ordinal']) + 1:03d}"
                    title = safe_name(chapter["title"])
                    name = f"{order}-{title}" if title else order
                    tags = _chapter_tags(
                        project,
                        chapter,
                        self._engines(
                            project_id,
                            rows_by_chapter.get(str(chapter["id"]), []),
                            labels,
                        ),
                    )
                    staged = folder / f".tag-{chapter['id']}{final.suffix}"
                    try:
                        tagged = self._tag_copy(final, staged, tags)
                        archive.write(staged if tagged else final, f"{name}{final.suffix}")
                    finally:
                        staged.unlink(missing_ok=True)
                    for suffix in ("srt", "lrc"):
                        subtitle = path.with_suffix(f".{suffix}")
                        if subtitle.is_file():
                            archive.write(subtitle, f"{name}.{suffix}")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(target)
        CHANNEL.info(
            f"分章导出：{project['name']} · {len(chapters)} 章 · {target.stat().st_size // 1024} KB",
            source="audiobook",
            project=project_id,
        )
        return target

    def export_m4b(self, project_id: str) -> Path:
        """整本导出为一个 M4B：每章一个章节锚点，播放器可直接跳章。"""
        lock = _merge_lock(project_id)
        if not lock.acquire(blocking=False):
            raise ProducerError("这个项目正在合并，等当前合并结束再导出。")
        try:
            return self._export_m4b(project_id)
        finally:
            lock.release()

    def _export_m4b(self, project_id: str) -> Path:
        self.svc._ensure_idle(project_id)
        project, chapters = self._merged_chapters(project_id)
        probe = find_ffprobe()
        if not probe:
            raise ProducerError("找不到 ffprobe，无法写入 M4B 章节。")
        book = safe_name(project["name"])
        folder = self._merged_dir(project_id)
        target = folder / f"{book}.m4b"
        temporary = folder / f".{book}.m4b.part"
        meta = folder / f".{book}.m4b.chapters.txt"
        listing = folder / f".{book}.m4b.list.txt"
        intermediate = folder / f".{book}.m4b.tmp.m4a"
        bgm = bool(self.svc.settings.bgm_enabled)
        finals: list[Path] = []
        durations: list[float] = []
        try:
            for chapter in chapters:
                path = self.svc.resolve_audio(project_id, chapter["audio"])
                if not path.is_file():
                    raise ProducerError(f"章节成品已不存在：{chapter['title']}。重新合并本章。")
                if bgm:
                    mixed = path.with_name(f"{path.stem}-bgm{path.suffix}")
                    if mixed.is_file():
                        path = mixed
                duration = probe_duration(path, ffprobe_path=probe)
                if duration <= 0:
                    raise ProducerError(f"读不到章节时长：{chapter['title']}。")
                finals.append(path)
                durations.append(duration)
            rows_by_chapter: dict[str, list[dict[str, Any]]] = {}
            for row in self.store.all_rows(project_id):
                rows_by_chapter.setdefault(str(row["chapter_id"]), []).append(row)
            used = [
                row
                for chapter in chapters
                for row in rows_by_chapter.get(str(chapter["id"]), [])
            ]
            self._write_m4b_meta(
                meta,
                {
                    "title": str(project["name"] or book),
                    **_book_tags(project, self._engines(project_id, used, {})),
                },
                chapters,
                durations,
            )
            binary = self._ffmpeg()
            signature = None
            if finals[0].suffix.lower() == ".m4a":
                signature = probe_stream(finals[0], ffprobe_path=probe)
                if signature[0] != "aac":
                    signature = None
            copyable = signature is not None and all(
                path.suffix.lower() == ".m4a" and probe_stream(path, ffprobe_path=probe) == signature
                for path in finals[1:]
            )
            if copyable:
                listing.write_text(
                    "\n".join(_concat_line(path) for path in finals), encoding="utf-8"
                )
                command = [
                    binary, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-f", "ffmetadata", "-i", str(meta),
                    "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
                    "-c:a", "copy", "-movflags", "+faststart", "-f", "ipod", str(temporary),
                ]
                self._run_ffmpeg(command, listing)
            else:
                self._concat(
                    binary,
                    "m4a",
                    self.svc.settings.merge_bitrate,
                    finals,
                    intermediate,
                    folder,
                )
                command = [
                    binary, "-y", "-i", str(intermediate),
                    "-f", "ffmetadata", "-i", str(meta),
                    "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
                    "-c:a", "copy", "-movflags", "+faststart", "-f", "ipod", str(temporary),
                ]
                self._run_ffmpeg(command)
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            meta.unlink(missing_ok=True)
            listing.unlink(missing_ok=True)
            intermediate.unlink(missing_ok=True)
        CHANNEL.info(
            f"整本 M4B 导出：{project['name']} · {len(chapters)} 章 · {target.stat().st_size // 1024} KB",
            source="audiobook",
            project=project_id,
        )
        return target

    @staticmethod
    def _write_m4b_meta(
        path: Path,
        book: Mapping[str, str],
        chapters: list[dict[str, Any]],
        durations: list[float],
    ) -> None:
        def value(text: Any) -> str:
            return " ".join(str(text or "").split())

        lines = [";FFMETADATA1"]
        for key, text in book.items():
            if value(text):
                lines.append(f"{key}={_meta_escape(value(text))}")
        start = 0
        for chapter, duration in zip(chapters, durations):
            end = start + max(1, round(duration * 1000))
            title = _meta_escape(value(chapter["title"])) or f"第 {int(chapter['ordinal']) + 1} 章"
            lines += [
                "",
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start}",
                f"END={end}",
                f"title={title}",
            ]
            start = end
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

