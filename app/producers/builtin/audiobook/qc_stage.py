"""行音频预检：先做声学前置检查（静音、尾音截断、时长异常），最后做 ASR 回读比对。

只读行音频、只写 ``row_qc``：不进音频指纹、不改行状态；是否阻断合并由用户设置决定。

ASR 比对在**无声调拼音的音节序列**上做，不在字上做。同音字（``医院`` / ``医愿``）
因此被当作一致，这正是识别模型转写的主要噪声源。代价是它测不出多音字读错：
识别输出的是字，字定读音，反推的拼音已丢掉声学信息。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import wave
from array import array
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pypinyin import Style, lazy_pinyin

from app.infra.audioprobe import probe_duration
from app.infra.ffmpeg import FfmpegError, to_wav
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.paths import abs_path

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

#: 识别模型会自己补的语气词。它们不算"多念了"，只在对齐后忽略。
FILLERS = frozenset("嗯啊呃哦噢诶欸呀嘛吧呢")

#: 双中括号标记是写给引擎的，不进比对。
_MARK = re.compile(r"\[\[[^\]]*\]\]")
#: 数字段整体折叠成一个记号：逐位读、口语读、阿拉伯数字，三种写法都归一。
#: 折叠放在字符层而不是音节层——``shi`` 既是「十」也是「是」，按音节折叠会误伤正文。
_NUMBER = re.compile(r"[0-9]+|[零〇一二三四五六七八九两十百千万亿]+")
_KEEP = re.compile(r"[^\w\u4e00-\u9fff]")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    value = _MARK.sub("", text or "")
    value = value.replace("　", " ").lower()
    value = _NUMBER.sub("\u03a9", value)
    value = _KEEP.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def syllables(text: str) -> list[str]:
    return [token.strip() for token in lazy_pinyin(text, style=Style.NORMAL) if token.strip()]


def _align(left: list[str], right: list[str]) -> list[tuple[str, int, int]]:
    """编辑距离 + 回溯。返回 (操作, 左下标, 右下标)；下标按各自序列记。"""
    rows, cols = len(left), len(right)
    back: list[list[int]] = []
    previous = list(range(cols + 1))
    for i in range(1, rows + 1):
        current = [i] + [0] * cols
        tags = [0] * cols
        for j in range(1, cols + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            diagonal = previous[j - 1] + cost
            up = previous[j] + 1
            inline = current[j - 1] + 1
            best = min(diagonal, up, inline)
            current[j] = best
            tags[j - 1] = 0 if best == diagonal else (1 if best == up else 2)
        back.append(tags)
        previous = current

    ops: list[tuple[str, int, int]] = []
    i, j = rows, cols
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            tag = back[i - 1][j - 1]
            if tag == 0:
                if left[i - 1] != right[j - 1]:
                    ops.append(("sub", i - 1, j - 1))
                i -= 1
                j -= 1
            elif tag == 1:
                ops.append(("del", i - 1, -1))
                i -= 1
            else:
                ops.append(("ins", -1, j - 1))
                j -= 1
        elif i > 0:
            ops.append(("del", i - 1, -1))
            i -= 1
        else:
            ops.append(("ins", -1, j - 1))
            j -= 1
    ops.reverse()
    return ops


def _longest_run(indexes: list[int]) -> int:
    """缺失音节的最长连续段：整句掉半句比零星漏几个虚词更值得报。"""
    if not indexes:
        return 0
    best = run = 1
    for previous, current in pairwise(indexes):
        run = run + 1 if current == previous + 1 else 1
        best = max(best, run)
    return best


def compare(source: str, heard: str) -> dict[str, Any]:
    """原文与识别文本的比对结论：``verdict`` = ok / suspect / unknown。"""
    left = syllables(normalize(source))
    right = syllables(normalize(heard))
    detail: dict[str, Any] = {"heard": heard, "chars": len(left)}

    if not left:
        return {"verdict": "unknown", "score": 0.0, "detail": detail}
    if not right:
        detail["issues"] = [{"kind": "silent"}]
        return {"verdict": "suspect", "score": 1.0, "detail": detail}

    total = len(left)
    gap = abs(len(right) - total) / total
    if gap > 0.5:
        # 长度对不上就不再做对齐：短了是漏读，长了是多念。
        kind = "missing" if len(right) < total else "repeat"
        detail["issues"] = [{"kind": kind, "expected": total, "heard_chars": len(right)}]
        return {"verdict": "suspect", "score": round(gap, 4), "detail": detail}

    missing: list[int] = []
    extra: list[int] = []
    subs: list[int] = []
    for kind, left_at, right_at in _align(left, right):
        if kind == "del":
            missing.append(left_at)
        elif kind == "ins" and right[right_at] not in FILLERS:
            extra.append(right_at)
        elif kind == "sub":
            subs.append(left_at)

    issues: list[dict[str, Any]] = []
    missing_run = _longest_run(missing)
    if missing and (missing_run >= 3 or len(missing) / total > 0.1
                    or (total < 8 and len(missing) >= 2)):
        issues.append({"kind": "missing", "count": len(missing), "run": missing_run,
                       "at": missing[:8]})
    if extra and (len(extra) >= 2 and len(extra) / total > 0.05):
        issues.append({"kind": "repeat", "count": len(extra)})
    wrong_hit = (
        (total >= 5 and len(subs) / total > 0.25)
        or (2 <= total <= 4 and len(subs) >= 2 and len(subs) / total > 0.4)
    )
    if subs and wrong_hit:
        issues.append({"kind": "wrong", "count": len(subs), "at": subs[:8]})

    score = round((len(missing) + len(extra) + len(subs)) / total, 4)
    detail["issues"] = issues
    return {"verdict": "suspect" if issues else "ok", "score": score, "detail": detail}


def _read_wav_pcm16(path: Path) -> tuple[int, int, array] | None:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2:
                return None
            rate = handle.getframerate()
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
            if not raw or not rate or not channels:
                return None
            samples = array("h")
            samples.frombytes(raw)
            return rate, channels, samples
    except (wave.Error, OSError, EOFError):
        return None


def _read_pcm16(path: Path) -> tuple[int, int, array] | None:
    direct = _read_wav_pcm16(path)
    if direct is not None:
        return direct
    transient: Path | None = None
    try:
        transient = to_wav(path, transient=True)
        return _read_wav_pcm16(transient)
    except (FfmpegError, OSError, RuntimeError):
        return None
    finally:
        if transient is not None and transient != path:
            transient.unlink(missing_ok=True)


def inspect_acoustic(path: Path, text: str) -> dict[str, Any]:
    """声学前置检查：静音/无声、尾音截断与时长异常（不依赖识别引擎）。"""
    issues: list[dict[str, Any]] = []
    if not path.exists():
        return {"issues": [{"kind": "silent", "reason": "音频文件不存在"}], "duration": 0.0}

    duration = probe_duration(path)
    if duration <= 0.0:
        return {"issues": [{"kind": "silent", "reason": "音频为空或无法读取"}], "duration": 0.0}

    units = len(syllables(normalize(text)))
    if units > 0:
        if duration < 0.15 or (units >= 4 and duration < max(0.25, units * 0.07)):
            issues.append({"kind": "duration", "detail": "too_short", "duration": round(duration, 2)})
        elif units >= 4 and duration > max(3.0, units * 0.9 + 1.5):
            issues.append({"kind": "duration", "detail": "too_long", "duration": round(duration, 2)})

    pcm = _read_pcm16(path)
    if pcm is not None:
        rate, channels, samples = pcm
        if samples:
            peak = max(abs(max(samples)), abs(min(samples))) / 32768.0
            if peak < 0.005:
                issues.append({"kind": "silent", "peak": round(peak, 4)})
            elif duration >= 0.3:
                tail_len = max(1, int(rate * channels * 0.03))
                stride = max(1, len(samples) // 4000)
                body_slice = samples[::stride]
                body_rms = math.sqrt(sum(s * s for s in body_slice) / len(body_slice)) / 32768.0
                tail_slice = samples[-tail_len:]
                tail_rms = math.sqrt(sum(s * s for s in tail_slice) / len(tail_slice)) / 32768.0
                if tail_rms > 0.05 and body_rms > 0 and tail_rms > body_rms * 0.4:
                    issues.append({"kind": "truncated", "tail_rms": round(tail_rms, 4)})

    return {"issues": issues, "duration": round(duration, 2)}


class QcStage:
    def __init__(self, service: AudiobookService) -> None:
        self.svc = service

    def report(self, project_id: str) -> dict[str, Any]:
        from app.usecase import asr as asr_uc

        counts = {"ok": 0, "suspect": 0, "unknown": 0}
        rows = self.svc.store.all_rows(project_id)
        alive = {row["id"]: row for row in rows}
        for row_id, item in self.svc.store.qc_map(project_id).items():
            row = alive.get(row_id)
            if not row or row.get("audio_key") != item.get("audio_key"):
                continue
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        checked = sum(counts.values())
        settings = self.svc.settings
        return {
            "counts": counts, "checked": checked, "rows": len(rows),
            "audio": sum(1 for row in rows if row.get("audio")),
            "pending": max(0, len(rows) - checked),
            # 空串 = 现在能识别；否则是禁用的原因，ASR 开关据此提示。
            "reason": asr_uc.unavailable_reason(),
            "engine": asr_uc.current_key(),
            "acoustic": bool(getattr(settings, "qc_acoustic", True)),
            "asr": bool(getattr(settings, "qc_asr", True)),
            "block_merge": bool(getattr(settings, "qc_block_merge", False)),
        }

    def start(self, project_id: str) -> dict[str, Any]:
        if not self.svc.store.project_meta(project_id):
            raise ProducerError("项目不存在。")
        self.svc._ensure_idle(project_id)

        async def run(job_id: str) -> None:
            await asyncio.to_thread(self._run_job, project_id, job_id)

        return self.svc._start_job(project_id, "qc", 0, run, exclusive=False)

    def _run_job(self, project_id: str, job_id: str) -> None:
        def progress(done: int, total: int, message: str) -> None:
            self.svc.store.update_job(job_id, done=done, total=total, message=message)

        result = self.run(
            project_id, progress, cancelled=lambda: self.svc.store.job_cancelled(job_id)
        )
        if not result.get("cancelled"):
            self.svc.store.update_job(job_id, message=result["message"])

    @staticmethod
    def _done_checks(item: dict[str, Any]) -> set[str]:
        detail = item.get("detail") or {}
        if isinstance(detail, dict) and isinstance(detail.get("checks"), list):
            return {str(c) for c in detail["checks"]}
        return {"asr"} if item.get("engine") else set()

    def run(
        self,
        project_id: str,
        progress: Any,
        cancelled: Any = lambda: False,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from app.usecase import asr as asr_uc

        started = time.perf_counter()
        settings = self.svc.settings
        use_acoustic = bool(getattr(settings, "qc_acoustic", True))
        use_asr = bool(getattr(settings, "qc_asr", True))

        if not use_acoustic and not use_asr:
            message = "预检跳过：未启用任何检查步骤"
            CHANNEL.warning(message, source="audiobook", project=project_id)
            return {"enabled": False, "message": message, "seconds": 0.0}

        if use_asr:
            reason = asr_uc.unavailable_reason()
            if reason:
                if not use_acoustic:
                    message = f"预检跳过：{reason}"
                    CHANNEL.warning(message, source="audiobook", project=project_id)
                    return {"enabled": False, "message": message, "seconds": 0.0}
                CHANNEL.warning(
                    f"ASR 不可用（{reason}），本次仅执行声学前置检查",
                    source="audiobook",
                    project=project_id,
                )
                use_asr = False

        required = {c for c, on in (("acoustic", use_acoustic), ("asr", use_asr)) if on}

        self.svc._settle_stale(project_id)
        root = self.svc.project_folder(project_id)
        all_rows = rows if rows is not None else self.svc.store.all_rows(project_id)
        live = [row for row in all_rows if row.get("audio") and row.get("status") != "stale"]
        self.svc.store.qc_prune(project_id, {row["id"] for row in all_rows})

        known = self.svc.store.qc_map(project_id)
        todo = [
            row for row in live
            if known.get(row["id"], {}).get("audio_key") != row.get("audio_key")
            or not required.issubset(self._done_checks(known.get(row["id"], {})))
        ]

        total = len(todo)
        if not total:
            message = f"预检完成：{len(live)} 行音频都已检过"
            progress(0, 0, message)
            return {"enabled": True, "checked": 0, "suspect": 0, "message": message,
                    "seconds": time.perf_counter() - started}

        progress(0, total, f"预检 0/{total}")
        engine = ""
        results: list[dict[str, Any]] = []
        checked = suspect = failed = 0
        for index, row in enumerate(todo, 1):
            if cancelled():
                return {"cancelled": True}
            path = abs_path(root, row["audio"])
            text = str(row.get("text") or "")
            issues: list[dict[str, Any]] = []
            checks_done: list[str] = []
            detail: dict[str, Any] = {}
            score = 0.0
            asr_failed = False

            if use_acoustic:
                acoustic = inspect_acoustic(path, text)
                checks_done.append("acoustic")
                detail["duration"] = acoustic["duration"]
                issues.extend(acoustic["issues"])

            is_silent = any(item.get("kind") == "silent" for item in issues)
            if use_asr and not is_silent:
                try:
                    heard = str(asr_uc.transcribe_file(path).get("text", "") or "")
                    asr_result = compare(text, heard)
                    checks_done.append("asr")
                    engine = asr_uc.current_key()
                    asr_detail = asr_result.get("detail") or {}
                    detail["heard"] = asr_detail.get("heard", "")
                    detail["chars"] = asr_detail.get("chars", 0)
                    issues.extend(asr_detail.get("issues") or [])
                    score = float(asr_result.get("score") or 0.0)
                except Exception as exc:  # noqa: BLE001 - 单行失败不该中断整批
                    asr_failed = True
                    detail["error"] = str(exc)[:200]
                    failed += 1

            detail["checks"] = checks_done
            detail["issues"] = issues
            if issues:
                verdict = "suspect"
                score = max(score, 1.0 if not score else score)
            elif asr_failed:
                verdict = "unknown"
            else:
                verdict = "ok"

            results.append({
                "row_id": row["id"], "audio_key": str(row.get("audio_key") or ""),
                "verdict": verdict, "score": score,
                "detail": detail, "engine": engine,
            })
            checked += 1
            if verdict == "suspect":
                suspect += 1
            if index % 20 == 0 or index == total:
                self.svc.store.qc_put(project_id, results[-20:])
                progress(index, total, f"预检 {index}/{total} · 疑似 {suspect}")
        if results and len(results) % 20:
            self.svc.store.qc_put(project_id, results[-(len(results) % 20):])

        message = (f"预检完成：{checked} 行 · 疑似 {suspect} 行"
                   + (f" · 识别失败 {failed} 行" if failed else ""))
        progress(checked, total, message)
        CHANNEL.info(message, source="audiobook", project=project_id)
        return {"enabled": True, "checked": checked, "suspect": suspect,
                "failed": failed, "message": message,
                "seconds": time.perf_counter() - started}
