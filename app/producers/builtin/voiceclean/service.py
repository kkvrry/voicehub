"""音色处理服务：按目标跑步骤、留 A/B 产物、按确认替换或下载。

目标是一组勾选的样例（整条音色、单条样例、或其中几条），也可以是一份从电脑上传的
音频文件。一条音频的流程固定为「原声副本 → 勾选的模型步骤 → 可选的收尾」，
两条产物（``origin.wav`` / ``result.wav``）留在任务目录里供试听。音色目标在**确认
之前不动音色库**；文件目标只产出可下载的结果，不写回任何样例。裁剪范围在生成
原声副本时一次切好（可以保留多段），后续步骤都只看保留的这几截。

模型与地址不单独配置：从 audio.cpp 插件的运行实例里取（``/v1/models`` 注册了什么
就能选什么）。一台都没启动时页面仍可用——只做裁剪也可以生成结果。

三条口径：

1. 开了收尾时，原声与结果都过同一套声道与峰值处理，试听听到的差异只来自模型，
   不来自电平与声道；关掉时两边都不做高通与限峰，只统一成单声道；
2. 时长偏差超过 :data:`DURATION_TOLERANCE` 的条目不参与替换：贴齐、逐字稿、平台凭据
   都按这一条文件的位置对齐，长度变了它们全错位；
3. 单模型并发 1，队列按文件名顺序推进，中途取消在条与条之间生效。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.infra import atomic, audioprobe, ffmpeg
from app.infra.config import data_dir
from app.infra.db import HubDB
from app.producers.base import ProducerError
from app.producers.builtin.voiceclean import client, models
from app.usecase.library import LibraryError, LibraryService

__all__ = [
    "DURATION_TOLERANCE",
    "MAX_SOURCE_BYTES",
    "MIN_TRIM_SECONDS",
    "SOURCE_SUFFIXES",
    "VoiceCleanService",
]

#: 时长允许的偏差（秒）。50 ms 以内听不出来，超过就是内容变了或对齐错了。
DURATION_TOLERANCE = 0.05
#: 裁剪后至少保留的时长（秒）：再短就听不出处理前后差异了。
MIN_TRIM_SECONDS = 0.1
#: 电脑文件上传的上限：一段用于处理的音频，不是整本书。
MAX_SOURCE_BYTES = 50 * 1024 * 1024
#: 接受的上传容器：与库里样例的可处理范围一致。
SOURCE_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
#: 上传目录名；12 位随机十六进制，同时是接口里的 token。
_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")
#: 上传文件的保留时长（秒）：离开页面后不再有人引用它。
_SOURCE_TTL = 24 * 60 * 60
#: 收尾参数：口播素材 60 Hz 以下没有内容；限峰 0.891 ≈ −1 dBFS。
_HIGHPASS = 60
_PEAK = 0.891
#: 运行模型清单的缓存时长（秒）：state 会被反复打开，每次去连一遍实例没有必要。
_PROBE_TTL = 5.0
#: 没有任何可跑的模型时，步骤卡上的说明。
_NO_SERVICE = "没有正在运行的 audio.cpp 模型。到 audio.cpp 页安装并加载后，这里会出现选项。"


def _runtime_models() -> list[dict[str, Any]]:
    """audio.cpp 插件运行实例注册的模型：``[{id, family, task, path, package, url}]``。

    取现成的服务实例，不为了读状态去构造它——构造会按自动启动清单拉起进程。
    """
    from app.usecase.producers import started_service

    service = started_service("audiocpp")
    if service is None:
        return []
    try:
        return list(service.running_models())
    except Exception:  # noqa: BLE001 - 运行时读不到就是没有可选模型
        return []


def _segments_for(
    trims: dict[str, Any], file: str, duration: float
) -> tuple[list[tuple[float, float]], bool]:
    """一条样例要保留的区间 ``([(start, end), ...], trimmed)``。

    两种写法都认：``segments`` 是删除过若干区间后剩下的段；旧的 ``start`` /
    ``end`` 当成一段（``end = 0`` 表示到结尾）。没有记录表示整段。
    空表、以及恰好覆盖整段的一段，都按"没有裁剪"处理。
    """
    raw = trims.get(file)
    if not isinstance(raw, dict):
        return [], False
    rows: list[tuple[float, float]] = []
    entries = raw.get("segments")
    if isinstance(entries, list) and entries:
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise ProducerError(f"「{file}」第 {index} 段裁剪范围格式不对。")
            try:
                start = float(entry.get("start") or 0.0)
                end = float(entry.get("end") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ProducerError(f"「{file}」第 {index} 段的裁剪时间不是有效数字。") from exc
            if end <= start:
                raise ProducerError(f"「{file}」第 {index} 段的裁剪起点必须早于终点。")
            rows.append((start, end))
    else:
        try:
            start = float(raw.get("start") or 0.0)
            end = float(raw.get("end") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ProducerError(f"「{file}」的裁剪范围不是有效的时间。") from exc
        if end <= 0:
            if duration <= 0 and start <= 0:
                # 旧调用方在时长未知时可能给一对 0：没有可裁的范围，按整段处理。
                return [], False
            end = duration
        if end <= start:
            raise ProducerError(f"「{file}」的裁剪起点必须早于终点。")
        rows.append((start, end))

    limited: list[tuple[float, float]] = []
    for start, end in rows:
        start = max(0.0, start)
        if duration > 0:
            start = min(start, duration)
            end = min(end, duration)
        if end - start > 0.001:
            limited.append((start, end))
    if not limited:
        raise ProducerError(f"「{file}」的裁剪范围落在音频之外。")
    limited.sort()
    merged: list[tuple[float, float]] = []
    for start, end in limited:
        if merged and start <= merged[-1][1] + 0.005:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    kept = sum(end - start for start, end in merged)
    if kept < MIN_TRIM_SECONDS:
        raise ProducerError(
            f"「{file}」裁剪后不足 {MIN_TRIM_SECONDS:.1f} 秒，至少保留 {MIN_TRIM_SECONDS:.1f} 秒。"
        )
    if (
        len(merged) == 1
        and duration > 0
        and merged[0][0] <= 0.005
        and merged[0][1] >= duration - 0.005
    ):
        return [], False
    return [(round(start, 3), round(end, 3)) for start, end in merged], True


@dataclass
class _Item:
    """一条待处理音频。``source`` 是库里的原件，产物另存。"""

    index: int
    voice_id: str
    voice: str
    file: str
    source: str
    segments: list[tuple[float, float]] = field(default_factory=list)
    trim_start: float = 0.0
    trim_end: float = 0.0
    trimmed: bool = False
    origin_seconds: float = 0.0
    result_seconds: float = 0.0
    state: str = "pending"  # pending / running / done / applied / failed / skipped
    error: str = ""
    replaceable: bool = False
    selected: bool = True

    def view(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "voice_id": self.voice_id,
            "voice": self.voice,
            "file": self.file,
            "state": self.state,
            "error": self.error,
            "selected": self.selected,
            "replaceable": self.replaceable,
            "trim_start": round(self.trim_start, 3),
            "trim_end": round(self.trim_end, 3),
            "segments": [[round(start, 3), round(end, 3)] for start, end in self.segments],
            "trimmed": self.trimmed,
            "origin_seconds": round(self.origin_seconds, 3),
            "result_seconds": round(self.result_seconds, 3),
        }


@dataclass
class _Job:
    id: str
    folder: Path
    scope: dict[str, Any]
    steps: list[dict[str, Any]]
    items: list[_Item]
    normalize: bool = True
    state: str = "running"
    note: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        finished = sum(
            1
            for item in self.items
            if item.state in {"done", "applied", "failed", "skipped"}
        )
        current = next(
            (item.file for item in self.items if item.state == "running"), ""
        )
        return {
            "id": self.id,
            "state": self.state,
            "note": self.note,
            "scope": self.scope,
            "steps": self.steps,
            "normalize": self.normalize,
            "total": len(self.items),
            "finished": finished,
            "current": current,
            "failed": sum(1 for item in self.items if item.state == "failed"),
            "items": [item.view() for item in self.items],
        }


class VoiceCleanService:
    """一个插件实例一个服务；任务同一时刻只跑一个。"""

    def __init__(
        self,
        root: Path,
        settings_reader: Any,
        running_models: Any = None,
    ) -> None:
        self._root = root
        self._settings = settings_reader
        self._models_fn = running_models
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._thread: threading.Thread | None = None
        self._library_cache: LibraryService | None = None
        self._models_at = 0.0
        self._models_cache: list[dict[str, Any]] | None = None
        self._root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 电脑文件

    def save_source(self, raw: bytes, filename: str) -> dict[str, Any]:
        """收一份电脑文件，放进任务自己的来源目录。

        文件名只取最后一段；后缀决定容器，不信任浏览器给的 MIME。来源目录按 token
        隔离，处理任务只引用这一份，不碰音色库。超过保留期的旧上传顺手清掉。
        """
        name = Path(filename or "").name.strip()
        suffix = Path(name).suffix.lower()
        if suffix not in SOURCE_SUFFIXES:
            raise ProducerError(
                "这个文件不是可处理的音频。支持 WAV / FLAC / MP3 / M4A / AAC / OGG / OPUS。"
            )
        if not raw:
            raise ProducerError("文件是空的。")
        token = uuid.uuid4().hex[:12]
        folder = self._root / "sources" / token
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"source{suffix}"
        atomic.write_bytes(path, raw)
        duration = audioprobe.probe_duration(path)
        atomic.write_json(
            folder / "source.json",
            {"name": name, "duration": round(duration, 3)},
            indent=None,
        )
        self._prune_sources(keep=token)
        return {
            "token": token,
            "name": name,
            "duration": round(duration, 3),
            "suffix": suffix,
        }

    def source_file(self, token: str) -> Path:
        """一个上传来源的音频文件；token 不对或文件已被清掉时给出可读错误。"""
        return self._source(token)[0]

    def source_name(self, token: str) -> str:
        return self._source(token)[1]

    def _source(self, token: str) -> tuple[Path, str, float]:
        token = str(token or "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ProducerError("文件来源已失效，重新选一次。")
        folder = self._root / "sources" / token
        meta_path = folder / "source.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProducerError("文件来源已失效，重新选一次。") from exc
        name = str(meta.get("name") or "").strip()
        suffix = Path(name).suffix.lower()
        path = folder / f"source{suffix}"
        if not path.is_file():
            raise ProducerError("文件来源已失效，重新选一次。")
        try:
            duration = float(meta.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return path, name, duration

    def _prune_sources(self, *, keep: str = "") -> None:
        """清掉超过保留期的上传目录；失败只跳过，不影响这次导入。"""
        root = self._root / "sources"
        if not root.is_dir():
            return
        deadline = time.time() - _SOURCE_TTL
        for folder in root.iterdir():
            if not folder.is_dir() or folder.name == keep:
                continue
            try:
                if folder.stat().st_mtime >= deadline:
                    continue
                shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue

    # ---------------------------------------------------------------- 配置与运行模型

    def _cfg(self) -> Any:
        return self._settings()

    def _timeout(self) -> float:
        try:
            return max(10.0, float(getattr(self._cfg(), "timeout", 600) or 600))
        except (TypeError, ValueError):
            return 600.0

    def _library(self) -> LibraryService:
        if self._library_cache is None:
            self._library_cache = LibraryService(HubDB(data_dir() / "hub.db"))
        return self._library_cache

    def _running_models(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        """运行中的实例注册了哪些模型。``run`` 用 ``fresh``，刚加载的模型立即可选。"""
        now = time.monotonic()
        if self._models_cache is not None and not fresh and now - self._models_at < _PROBE_TTL:
            return self._models_cache
        reader = self._models_fn or _runtime_models
        rows = list(reader())
        self._models_cache = rows
        self._models_at = now
        return rows

    # ---------------------------------------------------------------- 界面状态

    def state(self) -> dict[str, Any]:
        return {
            "steps": self._steps_view(self._running_models()),
            "core": models.CORE_STEP,
            "job": self.job_view(),
            "limits": {"tolerance": DURATION_TOLERANCE},
        }

    def _steps_view(self, running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for step in models.STEPS:
            options = models.options_for(step.task, running)
            rows.append(
                {
                    "key": step.key,
                    "label": step.label,
                    "note": step.note,
                    "order": step.order,
                    "task": step.task,
                    "default": step.default,
                    "models": options,
                    "reason": ""
                    if options
                    else (
                        _NO_SERVICE
                        if not running
                        else f"运行中的实例没有可用于「{step.label}」的模型；到 audio.cpp 页安装并加载后，这里会出现选项。"
                    ),
                }
            )
        return rows

    def job_view(self) -> dict[str, Any] | None:
        with self._lock:
            job = self._job
        return job.view() if job else None

    # ---------------------------------------------------------------- 目标

    def _resolve(self, scope: dict[str, Any]) -> tuple[list[_Item], dict[str, Any]]:
        """目标音色或电脑文件 → 待处理条目。

        音色只收本地的：平台音色的样例在引擎目录里，替换会让平台侧凭据指向被换的
        文件。``files`` 是处理前勾选的样例；为空时退回 ``file``（单条）或整条音色。
        ``kind='file'`` 时只认上传得到的 ``token``，不查音色库。
        ``trims`` 按文件名给裁剪区间，未给的按整段处理。
        """
        voice_id = str(scope.get("voice_id") or "").strip()
        only_file = str(scope.get("file") or "").strip()
        want_files = [
            str(value).strip()
            for value in (scope.get("files") or [])
            if str(value).strip()
        ]
        trims = scope.get("trims") or {}
        if not isinstance(trims, dict):
            raise ProducerError("裁剪范围格式不对，刷新页面后重试。")

        if str(scope.get("kind") or "voice").strip() == "file":
            token = str(scope.get("token") or "").strip()
            path, name, duration = self._source(token)
            segments, trimmed = _segments_for(trims, name, duration)
            item = _Item(
                index=0,
                voice_id=f"file:{token}",
                voice=name,
                file=name,
                source=str(path),
                segments=segments,
                trim_start=segments[0][0] if segments else 0.0,
                trim_end=segments[-1][1] if segments else 0.0,
                trimmed=trimmed,
            )
            return [item], {
                "kind": "file",
                "token": token,
                "voice_id": item.voice_id,
                "file": name,
                "label": f"文件「{name}」",
                "count": 1,
                "files": [name],
            }

        if not voice_id:
            raise ProducerError("未选择音色。")
        library = self._library()
        try:
            detail = library.get_voice(voice_id)
        except LibraryError as exc:
            raise ProducerError(str(exc)) from exc
        if not detail.is_local or not detail.folder:
            raise ProducerError(f"「{detail.shown_name}」是平台音色，样例不在本机，无法处理。")

        items: list[_Item] = []
        matched: set[str] = set()
        folder = Path(detail.folder)
        for sample in detail.samples:
            if only_file and sample.file != only_file:
                continue
            if want_files and sample.file not in want_files:
                continue
            path = folder / sample.file
            if path.suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}:
                continue
            if not path.is_file():
                continue
            segments, trimmed = _segments_for(trims, sample.file, sample.duration)
            start, end = (
                (segments[0][0], segments[-1][1]) if segments else (0.0, 0.0)
            )
            matched.add(sample.file)
            items.append(
                _Item(
                    index=len(items),
                    voice_id=detail.id,
                    voice=detail.shown_name,
                    file=sample.file,
                    source=str(path),
                    segments=segments,
                    trim_start=start,
                    trim_end=end,
                    trimmed=trimmed,
                )
            )
        missing = [name for name in want_files if name not in matched]
        if missing:
            raise ProducerError(
                f"这些音频不在「{detail.shown_name}」的可处理范围里："
                + "、".join(missing[:3])
                + ("…" if len(missing) > 3 else "")
            )
        if not items:
            if only_file:
                raise ProducerError(f"「{detail.shown_name}」里没有这条音频：{only_file}")
            if want_files:
                raise ProducerError("没有选中任何可处理的音频。")
            raise ProducerError(f"「{detail.shown_name}」没有可处理的音频。")
        items.sort(key=lambda item: item.file)
        for index, item in enumerate(items):
            item.index = index
        label = f"音色「{detail.shown_name}」"
        if only_file:
            label = f"「{detail.shown_name}」· {only_file}"
        elif want_files and len(items) == 1:
            label = f"「{detail.shown_name}」· {items[0].file}"
        elif want_files:
            label = f"「{detail.shown_name}」· 已选 {len(items)} 条"
        return items, {
            "kind": "voice",
            "voice_id": detail.id,
            "file": only_file,
            "label": label,
            "count": len(items),
            "files": [item.file for item in items],
        }

    # ---------------------------------------------------------------- 执行

    def run(self, scope: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise ProducerError("上一次处理还在进行，先等它结束或取消。")

        running = self._running_models(fresh=True)
        wanted: list[dict[str, Any]] = []
        normalize = True
        saw_core = False
        seen_steps: set[str] = set()
        for step in steps:
            key = str(step.get("key") or "")
            if key == "normalize":
                saw_core = True
                normalize = bool(step.get("enabled", False))
                continue
            if not step.get("enabled", True):
                continue
            declaration = models.step_of(key)
            if declaration is None:
                continue
            if declaration.key in seen_steps:
                continue
            seen_steps.add(declaration.key)
            model_id = str(step.get("model") or "").strip()
            if not model_id:
                raise ProducerError(f"「{declaration.label}」没有选模型。")
            chosen = next(
                (
                    row
                    for row in models.options_for(declaration.task, running)
                    if row["id"] == model_id
                ),
                None,
            )
            if chosen is None:
                raise ProducerError(
                    f"「{declaration.label}」选的模型现在没有在运行；"
                    "到 audio.cpp 页启动后再试。"
                )
            host = str(chosen.get("url") or "")
            if not host:
                raise ProducerError(f"「{declaration.label}」的模型找不到服务地址，重新进一次页面再试。")
            wanted.append(
                {
                    "key": declaration.key,
                    "label": declaration.label,
                    "order": declaration.order,
                    "model": model_id,
                    "model_label": chosen["label"],
                    "prep": chosen["prep"],
                    "host": host,
                    "options": dict(step.get("options") or {}),
                }
            )
        # 旧调用方没带 core 步骤：沿用"固定执行收尾"的旧语义。
        if not saw_core:
            normalize = True
        wanted.sort(key=lambda row: int(row.get("order") or 0))

        items, info = self._resolve(scope)
        # 只裁剪也是有效任务：没有模型步骤、也不做收尾时，裁剪本身就是这次改动。
        if not wanted and not normalize and not any(item.trimmed for item in items):
            raise ProducerError("没有要执行的步骤：选择处理步骤，或先裁剪一条音频。")
        job_id = uuid.uuid4().hex[:12]
        folder = self._root / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        job = _Job(
            id=job_id,
            folder=folder,
            scope=info,
            steps=wanted,
            items=items,
            normalize=normalize,
        )
        with self._lock:
            self._job = job
        self._save(job)
        self._thread = threading.Thread(target=self._work, args=(job,), name=f"voiceclean-{job_id}", daemon=True)
        self._thread.start()
        return job.view()

    def _work(self, job: _Job) -> None:
        try:
            for item in job.items:
                if job.cancel.is_set():
                    job.state = "cancelled"
                    return
                item.state = "running"
                self._save(job)
                try:
                    self._process(job, item)
                except (ffmpeg.FfmpegError, ProducerError, OSError) as exc:
                    item.state = "failed"
                    item.error = _brief(exc)
                except Exception as exc:  # noqa: BLE001 - 一条坏掉不该中断整批
                    item.state = "failed"
                    item.error = _brief(exc)
                self._save(job)
            job.state = "cancelled" if job.cancel.is_set() else "done"
        finally:
            self._save(job)

    def _process(self, job: _Job, item: _Item) -> None:
        work = job.folder / f"{item.index:04d}"
        work.mkdir(parents=True, exist_ok=True)
        origin = work / "origin.wav"
        # 原声副本就是后面所有步骤的输入：裁剪在这里一次完成。关掉收尾时不做
        # 高通与限峰，只统一成单声道；开则两边同一套参数，试听差异只来自模型。
        ffmpeg.normalize(
            Path(item.source),
            origin,
            highpass=_HIGHPASS if job.normalize else 0,
            peak=_PEAK if job.normalize else 0,
            segments=item.segments or None,
        )
        item.origin_seconds = audioprobe.probe_duration(origin)

        current = origin
        for step in job.steps:
            current = self._run_step(job, item, step, current, work)

        result = work / "result.wav"
        if job.normalize:
            ffmpeg.normalize(current, result, highpass=_HIGHPASS, peak=_PEAK)
        else:
            shutil.copyfile(current, result)
        item.result_seconds = audioprobe.probe_duration(result)
        if job.scope.get("kind") == "file":
            # 电脑文件只产出结果，不替换任何样例：时长由模型决定，不做 50 ms 校验。
            item.replaceable = item.result_seconds > 0
        else:
            delta = abs(item.result_seconds - item.origin_seconds)
            item.replaceable = item.result_seconds > 0 and delta <= DURATION_TOLERANCE
            if not item.replaceable:
                item.error = f"时长偏差 {delta * 1000:.0f} ms"
        item.state = "done"

    def _run_step(self, job: _Job, item: _Item, step: dict[str, Any], source: Path, work: Path) -> Path:
        rate, channels = (int(step["prep"][0]), int(step["prep"][1]))
        prepared = work / f"{step['key']}-in.wav"
        ffmpeg.normalize(source, prepared, rate=rate, channels=channels, peak=0)
        audio = client.run_task(
            str(step["host"]),
            model=step["model"],
            audio=prepared,
            options=step.get("options") or {},
            timeout=self._timeout(),
        )
        target = work / f"{step['key']}.wav"
        target.write_bytes(audio)
        return target

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            job = self._job
        if job is None or job.state != "running":
            return self.job_view() or {}
        job.cancel.set()
        job.note = "正在结束当前这一条…"
        return job.view()

    def clear(self) -> dict[str, Any]:
        """丢掉已完成 / 已替换 / 已取消的任务，回到可编辑状态。"""
        with self._lock:
            job = self._job
            if job is not None and job.state == "running":
                raise ProducerError("当前任务还在进行，先取消再清空。")
            self._job = None
        return {"cleared": True}

    # ---------------------------------------------------------------- 试听与替换

    def audio_path(self, job_id: str, index: int, side: str) -> Path:
        with self._lock:
            job = self._job
        if job is None or job.id != job_id:
            raise ProducerError("任务已过期，重新跑一遍。")
        name = "origin.wav" if side == "origin" else "result.wav"
        path = job.folder / f"{int(index):04d}" / name
        if not path.is_file():
            raise ProducerError("这一段还没有可试听的音频。")
        return path

    def download_name(self, job_id: str, index: int) -> str:
        """电脑文件结果的下载名：原文件名 + 「（处理）」，容器统一为 wav。"""
        with self._lock:
            job = self._job
        if job is None or job.id != job_id:
            raise ProducerError("任务已过期，重新跑一遍。")
        item = next((row for row in job.items if row.index == int(index)), None)
        if item is None:
            raise ProducerError("这一段不在当前任务里。")
        stem = Path(item.file).stem.strip() or "音频"
        return f"{stem}（处理）.wav"

    def apply(self, job_id: str, indexes: list[int]) -> dict[str, Any]:
        """按勾选的条目替换；原件进回收站，sidecar 记处理链。"""
        with self._lock:
            job = self._job
        if job is None or job.id != job_id:
            raise ProducerError("任务已过期，重新跑一遍。")
        if job.scope.get("kind") == "file":
            raise ProducerError("电脑文件只产出结果，没有可替换的音色样例；请下载处理结果。")
        wanted = {int(value) for value in indexes}
        parts = [f"{step['label']} {step['model_label']}" for step in job.steps]
        if job.normalize:
            parts.append(models.CORE_STEP["label"])
        chain = " · ".join(parts)
        replaced = 0
        failures: list[dict[str, Any]] = []
        library = self._library()
        for item in job.items:
            if item.index not in wanted:
                continue
            if item.state != "done" or not item.replaceable:
                failures.append({"index": item.index, "reason": item.error or "未完成"})
                continue
            work = job.folder / f"{item.index:04d}"
            source = work / "result.wav"
            item_chain = chain
            if item.segments:
                if len(item.segments) > 1:
                    cut = f"裁剪 {len(item.segments)} 段"
                else:
                    begin, end = item.segments[0]
                    cut = f"裁剪 {begin:.2f}–{end:.2f} s"
                item_chain = f"{cut} · {item_chain}" if item_chain else cut
            if not item_chain:
                item_chain = "裁剪"
            seconds = item.result_seconds
            # 样例的文件名不变，容器也必须不变：WAV 结果直接写进 .flac 会让后缀说谎。
            suffix = Path(item.file).suffix.lower()
            if suffix and suffix != ".wav":
                encoded = work / f"result{suffix}"
                try:
                    ffmpeg.encode_audio(source, encoded, rate=0, channels=0)
                except ffmpeg.FfmpegError as exc:
                    failures.append({"index": item.index, "reason": _brief(exc)})
                    continue
                source = encoded
                seconds = 0.0
            try:
                library.replace_sample_audio(
                    item.voice_id,
                    item.file,
                    source,
                    params={"处理": item_chain, "来源": "音色处理"},
                    seconds=seconds,
                )
            except LibraryError as exc:
                failures.append({"index": item.index, "reason": str(exc)})
                continue
            item.state = "applied"
            replaced += 1
        job.state = "applied" if replaced else job.state
        self._save(job)
        return {"replaced": replaced, "failures": failures, "job": job.view()}

    # ---------------------------------------------------------------- 持久化

    def _save(self, job: _Job) -> None:
        payload = {"view": job.view(), "scope": job.scope, "steps": job.steps}
        try:
            (job.folder / "job.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass


def _brief(exc: BaseException) -> str:
    text = " ".join(str(exc).split()).strip().rstrip("。")
    return text[:300] or exc.__class__.__name__
