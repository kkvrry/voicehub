from __future__ import annotations

import math
import re
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from statistics import median

from app.infra.process import CREATE_NO_WINDOW, stop
from app.producers.base import ProducerError

__all__ = ["apply_gain", "balance_gains", "evaluate_master", "master_gain", "measure_audio"]

_MEASURE_BATCH = 12
_MIN_SECONDS = 0.4
_ABSOLUTE_GATE = -70.0
_LINE_PEAK_CEILING = -1.0
_TIMEOUT = 3600
# FFmpeg 日志前缀：有的构建只带实例名 ``[r0 @ ...]``，有的带完整滤镜名
# ``[ebur128@r0 @ ...]``；两种都要认，解析不能依赖本机 FFmpeg 版本。
_SUMMARY = re.compile(
    r"^\[(?:ebur128@|astats@)?r(\d+)\s+@[^\]]+\]\s+Summary:\s*(.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
_STATS = re.compile(
    r"^\[(?:ebur128@|astats@)?r(\d+)\s+@[^\]]+\]\s+(RMS level dB|Number of samples):\s*(\S+)",
    re.MULTILINE,
)
_INPUT_RATE = re.compile(r"^\s*Stream #(\d+):\d+[^\n]*?Audio:[^\n]*?,\s*(\d+) Hz", re.MULTILINE)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _run(command: list[str], cancelled: Callable[[], bool] | None = None) -> str:
    if cancelled is None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProducerError(f"FFmpeg 音频处理失败：{exc}") from exc
        if result.returncode:
            detail = " ".join((result.stderr or "").split())[-1600:] or "未返回错误信息"
            raise ProducerError(f"FFmpeg 音频处理失败：{detail}")
        return result.stderr or ""
    if cancelled():
        raise ProducerError("音频处理已停止。")
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                while True:
                    try:
                        process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if cancelled is not None and cancelled():
                            raise ProducerError("音频处理已停止。") from None
            finally:
                stop(process)
            errors.seek(0)
            stderr = errors.read().decode("utf-8", "replace")
    except OSError as exc:
        raise ProducerError(f"FFmpeg 音频处理失败：{exc}") from exc
    if process.returncode:
        detail = " ".join(stderr.split())[-1600:] or "未返回错误信息"
        raise ProducerError(f"FFmpeg 音频处理失败：{detail}")
    return stderr


def _measurement_command(binary: str, paths: list[Path]) -> list[str]:
    command = [binary, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-xerror"]
    for path in paths:
        command += ["-threads", "1", "-i", str(path)]
    filters = []
    for i in range(len(paths)):
        # 每个分支必须映射到输出；仅映射原始输入会留下未执行的测量图与初始化 Summary。
        filters.append(
            f"[{i}:a:0]ebur128@r{i}=peak=true:framelog=verbose,"
            f"astats@r{i}=measure_perchannel=none:"
            f"measure_overall=RMS_level+Number_of_samples,asplit[out{i}][sink{i}];"
            f"[sink{i}]anullsink"
        )
    command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters)]
    for i in range(len(paths)):
        command += ["-map", f"[out{i}]"]
    command += ["-c:a", "pcm_f32le", "-threads", "1", "-f", "null", "-"]
    return command


def _parse_measurements(log: str, paths: list[Path]) -> dict[Path, dict]:
    rates = {}
    for match in _INPUT_RATE.finditer(log.split("Stream mapping:", 1)[0]):
        rates.setdefault(int(match[1]), int(match[2]))
    summaries = {}
    for match in _SUMMARY.finditer(log):
        integrated = re.search(r"^\s*I:\s*(\S+)\s+LUFS", match[2], re.MULTILINE)
        peak = re.search(r"True peak:\s*Peak:\s*(\S+)\s+dB(?:FS|TP)", match[2])
        if integrated and peak:
            # FFmpeg 在建图时也打印 Summary；按实例保留最后一份完整结果。
            summaries[int(match[1])] = (integrated[1], peak[1])
    stats: dict[int, dict] = defaultdict(dict)
    for match in _STATS.finditer(log):
        stats[int(match[1])][match[2]] = match[3]
    result = {}
    for i, path in enumerate(paths):
        sample_count = _finite(stats[i].get("Number of samples"))
        if (
            i not in summaries
            or "RMS level dB" not in stats[i]
            or sample_count is None
            or sample_count < 0
            or not rates.get(i)
        ):
            raise ProducerError(f"FFmpeg 未返回完整的响度测量：{path.name}")
        integrated, peak = map(_finite, summaries[i])
        rms = _finite(stats[i]["RMS level dB"])
        if (
            sample_count / rates[i] < _MIN_SECONDS
            or integrated is None
            or integrated <= _ABSOLUTE_GATE
            or peak is None
            or rms is None
            or rms <= _ABSOLUTE_GATE
        ):
            # 时长与绝对门限仅决定 LUFS 基线是否可用；短音频仍需峰值保护。
            integrated = None
        result[path] = {"integrated": integrated, "true_peak": peak, "rms": rms}
    return result


def measure_audio(
    binary: str,
    paths: list[Path],
    cancelled: Callable[[], bool] | None = None,
) -> dict[Path, dict]:
    measured = {}
    for start in range(0, len(paths), _MEASURE_BATCH):
        if cancelled is not None and cancelled():
            raise ProducerError("音频处理已停止。")
        batch = paths[start : start + _MEASURE_BATCH]
        measured.update(
            _parse_measurements(
                _run(_measurement_command(binary, batch), cancelled), batch
            )
        )
    return measured


def _clamp_gain(value: float, maximum: float) -> float:
    return max(-maximum, min(maximum, value))


def _limit_boost(gain: float, peaks: list[float]) -> float:
    if gain <= 0:
        return gain
    if not peaks:
        return 0.0
    return min(gain, max(0.0, _LINE_PEAK_CEILING - max(peaks)))


def balance_gains(
    items: list[dict], *, mode: str, strength: float, max_gain_db: float
) -> tuple[dict[str, float], float | None]:
    if mode not in {"voice", "row"}:
        raise ValueError(f"不支持的均衡模式：{mode}")
    baselines: dict[str, list[float]] = defaultdict(list)
    peaks: dict[str, list[float]] = defaultdict(list)
    levels = {}
    for item in items:
        level = _finite(item.get("integrated"))
        levels[item["id"]] = level
        if level is not None and not item.get("expressive", False):
            baselines[item["group"]].append(level)
        peak = _finite(item.get("true_peak"))
        if peak is not None:
            peaks[item["group"]].append(peak)
    normal = [level for group in baselines.values() for level in group]
    reference = float(median(normal)) if normal else None
    gains = {item["id"]: 0.0 for item in items}
    if reference is None or strength == 0:
        return gains, reference
    maximum = max(0.0, max_gain_db)
    group_gains = {
        group: _limit_boost(
            _clamp_gain((reference - median(values)) * strength, maximum), peaks[group]
        )
        for group, values in baselines.items()
    }
    for item in items:
        level = levels[item["id"]]
        if level is None or item["group"] not in group_gains:
            continue
        if mode == "voice" or item.get("expressive", False):
            gains[item["id"]] = group_gains[item["group"]]
        else:
            peak = _finite(item.get("true_peak"))
            gains[item["id"]] = _limit_boost(
                _clamp_gain((reference - level) * strength, maximum),
                [peak] if peak is not None else [],
            )
    return gains, reference


def master_gain(
    measured: dict, *, preset: str, target_lufs: float, peak_db: float, headroom_db: float = 0
) -> dict:
    if preset not in {"podcast", "acx", "custom"}:
        raise ValueError(f"不支持的响度预设：{preset}")
    target = {"podcast": -19.0, "acx": -20.5, "custom": target_lufs}[preset]
    ceiling = min(peak_db, -3.0) if preset == "acx" else peak_db
    if any(_finite(value) is None for value in (target, ceiling, headroom_db)):
        raise ValueError("响度目标、峰值上限与余量必须为有限数值")
    metric = "rms" if preset == "acx" else "integrated"
    level = _finite(measured.get(metric))
    peak = _finite(measured.get("true_peak"))
    plan = {
        "gain_db": 0.0,
        "requested_gain_db": 0.0,
        "target": float(target),
        "unit": "RMS" if preset == "acx" else "LUFS",
        "peak_ceiling": float(ceiling),
        "limited": False,
        "measurable": level is not None and peak is not None,
    }
    if peak is not None:
        # 缺少目标响度时不提升，只衰减超限峰值；measurable 仍保持 False。
        desired = target - level if level is not None else 0.0
        safe = ceiling - headroom_db - peak
        gain = min(desired, safe)
        plan.update(gain_db=gain, requested_gain_db=desired, limited=gain < desired)
    return plan


def apply_gain(
    binary: str,
    source: Path,
    target: Path,
    gain_db: float,
    *,
    codec: str = "pcm_f32le",
    bitrate: str = "",
    metadata: dict | None = None,
    channels: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and source.samefile(target)
    ):
        raise ProducerError("固定增益输出不能覆盖源音频。")
    gain = _finite(gain_db)
    if gain is None:
        raise ProducerError("固定增益必须为有限数值。")
    command = [
        binary,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-xerror",
        "-y",
        "-threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "0",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-af",
        f"volume={gain:.12g}dB:precision=double",
        "-ar",
        "44100",
    ]
    if channels is not None:
        command += ["-ac", str(channels)]
    command += ["-c:a", codec, "-threads", "1"]
    if bitrate:
        command += ["-b:a", bitrate]
    for key, value in (metadata or {}).items():
        command += ["-metadata", f"{key}={value}"]
    suffix = target.suffix.lower()
    formats = {".wav": "wav", ".mp3": "mp3", ".m4a": "ipod", ".aac": "adts"}
    if suffix in formats:
        command += ["-f", formats[suffix]]
    if suffix == ".m4a":
        command += ["-movflags", "+faststart"]
    command.append(str(target))
    _run(command, cancelled)
    if not target.is_file() or target.stat().st_size == 0:
        raise ProducerError(f"FFmpeg 未生成增益处理结果：{target.name}")


def evaluate_master(measured: dict, plan: dict) -> dict:
    integrated = _finite(measured.get("integrated"))
    rms = _finite(measured.get("rms"))
    peak = _finite(measured.get("true_peak"))
    acx = plan["unit"] == "RMS"
    level = rms if acx else integrated
    target_met = level is not None and (
        -23.0 <= level <= -18.0 if acx else abs(level - plan["target"]) <= 0.5
    )
    peak_ok = peak is not None and peak <= plan["peak_ceiling"]
    warnings = []
    if level is None or peak is None:
        warnings.append("音频缺少有效响度或真峰值测量，无法完整确认目标与峰值。")
    if plan.get("limited"):
        warnings.append("固定增益受到真峰值上限约束；未使用限峰器或动态压缩。")
    if level is not None and not target_met:
        warnings.append(
            "成品 RMS 未达到 ACX 的 -23 至 -18 dBFS 范围。"
            if acx
            else "成品响度与目标相差超过 0.5 LUFS。"
        )
    if peak is not None and not peak_ok:
        warnings.append("成品真峰值超过上限；未自动限峰或压缩。")
    if acx:
        warnings.append("ACX 检查未检测底噪或编码要求；响度与峰值达标不等于完整合规。")
    return {
        "target_met": target_met,
        "peak_ok": peak_ok,
        "warnings": warnings,
        "integrated": integrated,
        "rms": rms,
        "true_peak": peak,
    }
