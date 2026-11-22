"""时间轴拼装：把「一段一段的音频」按时间码接成一条轨。

这是配音与有声书字幕项目的公共算法，只做三件事，且都能被验证：

1. 指针早于这一段的时间码：补一段静音，把指针推到该到的位置；
2. 指针已经越过它：**不截断、不丢弃**，从当前位置接着放，并计入「顺延」；
3. 开了贴齐时，长过时隙的段先加速：贴得进去就不算顺延，贴不进去的整段不动。

为什么集中在一处：这套规则决定成片听感与字幕对齐，两个生产方各写一份，
第一次参数调整就会出现两套结果。安静环境下听不出差别的几毫秒，
在字幕上会体现成"这句话比声音早半秒"。

**只依赖 ffmpeg 与 ffprobe 的路径参数**，不读配置、不认识项目，方便直接测。
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.audioprobe import find_ffprobe, probe_duration
from app.infra.ffmpeg import find_ffmpeg
from app.infra.process import CREATE_NO_WINDOW

__all__ = [
    "AssemblyOptions",
    "AssemblyPlan",
    "Slot",
    "concat_audio",
    "mix_tracks",
    "plan_timeline",
]

#: 变速后的实际时长与 ffprobe 读数差几毫秒是常态，这个量级的顺延听不出来。
TOLERANCE_MS = 30
#: 至少值得变速多少（0.5%）：再小就只为几毫秒重编码一段。
MIN_GAIN = 1.005
#: 静音量化到 10 毫秒：误差不会累积——间隔按"已经写到的位置"算。
QUANTUM_MS = 10
#: atempo 单级只认 0.5–2.0；配置被手改到更大也不让 ffmpeg 失败。
ATEMPO_MAX = 2.0


@dataclass(frozen=True, slots=True)
class Slot:
    """时间轴上的一段：一条字幕加它对应的那条音频。

    :param key: 稳定标识（cue 或行的 id），变速产物按它命名，改一段不会覆盖另一段。
    :param ordinal: 1 起的序号，报错时用它指认"第几段"，不用文件路径。
    """

    key: str
    ordinal: int
    start_ms: int
    end_ms: int
    audio: Path
    text: str = ""


@dataclass(frozen=True, slots=True)
class AssemblyOptions:
    """拼装策略。默认值与界面上的默认值一致。"""

    fit: bool = True
    max_speed: float = 1.35
    #: 按每一段自己的 ``end_ms`` 算可用的时隙。有声书按下一段起点算（把段间空隙
    #: 也算进上一段），配音要按原句的收尾算——不然配音会越过原句、盖住下一句。
    fit_to_slot: bool = False
    #: 中间件统一 PCM，避免每段各自解码成浮点。
    codec: str = "pcm_s16le"
    #: 拼装输出的采样率与声道数（人声轨恒为单声道，混音阶段再展宽）。
    rate: int = 44100
    channels: int = 1


@dataclass
class AssemblyPlan:
    """拼装计划：要接哪些文件、时间轴上的字幕落在哪、哪些段被顺延或变速。"""

    inputs: list[Path] = field(default_factory=list)
    cues: list[tuple[float, float, str]] = field(default_factory=list)
    delayed: list[int] = field(default_factory=list)
    fitted: list[int] = field(default_factory=list)
    #: 拼完的实际总时长（秒），由累计指针算出，不必再探一次文件。
    duration: float = 0.0


class AssemblyError(RuntimeError):
    """拼装失败。``reason`` 是可读说明，调用方原样摆给用户。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _concat_line(path: Path) -> str:
    """concat 清单的一行。清单用单引号包路径，路径里的单引号要断开再转义。"""
    text = path.as_posix()
    return "file '" + text.replace("'", "'\\''") + "'"


def _slot_end(slots: Sequence[Slot], index: int, *, own_end: bool = False) -> int:
    """这一段能用到哪：下一段的起点；最后一段用它自己的 ``end_ms``（0 表示未知）。

    ``own_end`` 为真时每段都用自己的 ``end_ms``：配音要按原句收尾贴齐，
    不能把下一句之前的空隙也吃进来。
    """
    if own_end and int(slots[index].end_ms) > int(slots[index].start_ms):
        return max(0, int(slots[index].end_ms))
    if index + 1 < len(slots):
        return max(0, int(slots[index + 1].start_ms))
    return max(0, int(slots[index].end_ms))


def _silence_file(
    ffmpeg: str, work_dir: Path, milliseconds: int, options: AssemblyOptions
) -> Path | None:
    """按毫秒复用静音文件；``milliseconds <= 0`` 时返回 ``None``（不需要补）。"""
    if milliseconds <= 0:
        return None
    target = work_dir / f"sil-{milliseconds:07d}-{options.rate}-{options.channels}.wav"
    if target.is_file() and target.stat().st_size > 44:
        return target
    layout = "mono" if options.channels == 1 else "stereo"
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={options.rate}:cl={layout}",
        "-t",
        f"{milliseconds / 1000:.3f}",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if done.returncode != 0 or not target.is_file():
        detail = " ".join((done.stderr or "").split())[-200:] or "原因未知"
        raise AssemblyError(f"生成静音段失败：{detail}")
    return target


def _speed_file(
    ffmpeg: str,
    work_dir: Path,
    slot: Slot,
    source: Path,
    speed: float,
    options: AssemblyOptions,
) -> Path:
    """按「段 + 倍率 + 源文件指纹」缓存变速产物；同样的输入只转一次。"""
    stat = source.stat()
    digest = hashlib.sha256(
        f"{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{speed:.3f}".encode()
    ).hexdigest()[:12]
    target = work_dir / f"fit-{slot.key}-{speed:.3f}-{digest}.wav"
    if target.is_file() and target.stat().st_size > 44:
        return target
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-filter:a",
        f"atempo={speed:.3f}",
        "-ac",
        str(options.channels),
        "-ar",
        str(options.rate),
        "-c:a",
        options.codec,
        str(target),
    ]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if done.returncode != 0 or not target.is_file():
        detail = " ".join((done.stderr or "").split())[-200:] or "原因未知"
        raise AssemblyError(f"第 {slot.ordinal} 段变速失败：{detail}")
    return target


def plan_timeline(
    slots: Sequence[Slot],
    *,
    work_dir: Path,
    options: AssemblyOptions | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    check: Callable[[], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> AssemblyPlan:
    """算出拼装计划并生成所需的静音段与变速段。

    :param work_dir: 中间产物目录（静音段、变速段）。调用方负责清理自己的旧文件。
    :param check: 取消检查；抛异常即中断（异常原样传出去）。
    :param on_progress: ``(已处理, 总数)``，每段一次。
    :raises AssemblyError: 缺少 ffmpeg / 某段音频读不出时长。
    """
    opts = options or AssemblyOptions()
    binary = ffmpeg or find_ffmpeg()
    if not binary:
        raise AssemblyError(
            "拼装时间轴需要 ffmpeg：本机未找到。请安装并加入 PATH，"
            "或在设置里把「ffprobe 路径」指向它的安装目录。"
        )
    probe = ffprobe or find_ffprobe()
    if not probe:
        raise AssemblyError("拼装时间轴需要 ffprobe：随包的 ffprobe 应在程序目录里。")

    work_dir.mkdir(parents=True, exist_ok=True)
    max_speed = min(ATEMPO_MAX, max(1.0, float(opts.max_speed)))
    plan = AssemblyPlan()
    cursor = 0
    total = len(slots)

    for index, slot in enumerate(slots):
        if check is not None:
            check()
        start = max(0, int(slot.start_ms))
        gap = start - cursor
        if gap < 0:
            if -gap > TOLERANCE_MS:
                plan.delayed.append(slot.ordinal)
            gap = 0
        quantized = round(gap / QUANTUM_MS) * QUANTUM_MS
        if quantized > 0:
            silence = _silence_file(binary, work_dir, quantized, opts)
            if silence is not None:
                plan.inputs.append(silence)
                cursor += quantized

        path = slot.audio
        if not path.is_file():
            raise AssemblyError(f"第 {slot.ordinal} 段的音频不存在：{path.name}")
        duration = probe_duration(path, ffprobe_path=probe)
        if duration <= 0:
            raise AssemblyError(f"第 {slot.ordinal} 段的音频读不出时长：{path.name}")

        if opts.fit:
            space = _slot_end(slots, index, own_end=opts.fit_to_slot) - max(cursor, start)
            need = duration / (space / 1000) if space > 0 else 0.0
            if MIN_GAIN < need <= max_speed:
                path = _speed_file(binary, work_dir, slot, path, need, opts)
                plan.fitted.append(slot.ordinal)
                duration = probe_duration(path, ffprobe_path=probe)
                if duration <= 0:
                    raise AssemblyError(
                        f"第 {slot.ordinal} 段变速后读不出时长：{path.name}"
                    )

        plan.inputs.append(path)
        text = " ".join(str(slot.text or "").split())
        end = cursor + round(duration * 1000)
        if text:
            plan.cues.append((cursor / 1000.0, end / 1000.0, text))
        cursor = end
        if on_progress is not None:
            on_progress(index + 1, total)

    plan.duration = cursor / 1000.0
    return plan


def concat_audio(
    inputs: Sequence[Path],
    target: Path,
    *,
    work_dir: Path,
    options: AssemblyOptions | None = None,
    ffmpeg: str | None = None,
    check: Callable[[], None] | None = None,
) -> Path:
    """按顺序把若干 wav 接成一条；走 concat 解复用器，不吃命令行长度上限。

    输入统一是 44.1k PCM16（宿主在合成时就转过），所以清单里只写路径；
    输出仍重编码一次，避免某一段的样本格式与其余不同时整条被解释错。
    """
    opts = options or AssemblyOptions()
    binary = ffmpeg or find_ffmpeg()
    if not binary:
        raise AssemblyError("拼接音频需要 ffmpeg：本机未找到。")
    if not inputs:
        raise AssemblyError("没有可拼接的音频。")
    if check is not None:
        check()
    work_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    listing = work_dir / f"{target.stem}.concat.txt"
    listing.write_text("\n".join(_concat_line(path) for path in inputs) + "\n", encoding="utf-8")
    command = [
        binary,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-vn",
        "-ac",
        str(opts.channels),
        "-ar",
        str(opts.rate),
        "-c:a",
        opts.codec,
        str(target),
    ]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    listing.unlink(missing_ok=True)
    if done.returncode != 0 or not target.is_file():
        detail = " ".join((done.stderr or "").split())[-300:] or "原因未知"
        raise AssemblyError(f"拼接音频失败：{detail}")
    return target


def mix_tracks(
    tracks: Sequence[tuple[Path, float]],
    target: Path,
    *,
    ffmpeg: str | None = None,
    rate: int = 44100,
    loudnorm: str = "",
    check: Callable[[], None] | None = None,
) -> Path:
    """把若干条轨按各自电平混成一条立体声。

    :param tracks: ``(文件, 增益 dB)``；增益 ``<= -60`` 视为静音，直接不参与混音。
    :param loudnorm: 响度目标（如 ``I=-16:TP=-1.5:LRA=11``）；空串 = 不做响度校准。
    """
    binary = ffmpeg or find_ffmpeg()
    if not binary:
        raise AssemblyError("混音需要 ffmpeg：本机未找到。")
    usable = [(path, gain) for path, gain in tracks if gain > -60 and path.is_file()]
    if not usable:
        raise AssemblyError("没有可混音的轨道：全部静音或文件缺失。")
    if check is not None:
        check()
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [binary, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    for path, _ in usable:
        command += ["-i", str(path)]
    chains = []
    for index, (_, gain) in enumerate(usable):
        chains.append(
            f"[{index}:a]aresample={rate},aformat=channel_layouts=stereo,"
            f"volume={gain:.2f}dB[a{index}]"
        )
    joined = "".join(f"[a{index}]" for index in range(len(usable)))
    if len(usable) > 1:
        # normalize=0：各轨保留自己的电平，混音大小由滑块决定，不被均分掉。
        chains.append(f"{joined}amix=inputs={len(usable)}:duration=longest:normalize=0[mixed]")
        last = "[mixed]"
    else:
        last = "[a0]"
    if loudnorm:
        chains.append(f"{last}loudnorm={loudnorm}[out]")
        last = "[out]"
    filter_complex = ";".join(chains) if chains else ""

    if filter_complex:
        command += ["-filter_complex", filter_complex, "-map", last]
    else:
        command += ["-map", "0:a:0"]
    command += ["-c:a", "pcm_s16le", "-ar", str(rate), str(target)]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    if done.returncode != 0 or not target.is_file():
        detail = " ".join((done.stderr or "").split())[-300:] or "原因未知"
        raise AssemblyError(f"混音失败：{detail}")
    return target
