"""音色处理服务：按目标跑步骤、留 A/B 产物、按确认替换、入库或下载。

目标是一组勾选的样例（整条音色、单条样例、或其中几条），也可以是一份从电脑上传的
音频文件。一条音频的流程固定为「原声副本 → 勾选的模型步骤 → 可选的收尾（统一声道与
峰值）」，两条产物（``origin.wav`` / ``result.wav``）留在任务目录里供试听。音色目标在
**确认之前不动音色库**：确认后可以替换原样例，也可以把结果作为新样例入库；文件目标
只产出可下载的结果，不写回任何样例。裁剪范围在生成原声副本时一次切好（可以保留
多段），后续步骤都只看保留的这几截。

步骤顺序按处理常识固定：分离在最前（先取出人声，降噪与带宽扩展才只面对人声），
降噪其次（先去掉底噪，补高频时才不会把噪声当内容），带宽扩展最后（它的输出是模型
生成的，任何后续降噪都可能把它当噪声削掉）。收尾（高通与限峰）不属于模型链，放在
所有模型步骤之后、原声与结果各做一次：模型吃的是原始电平，A/B 的差异只来自模型。

模型与地址不单独配置：候选清单按步骤登记（官方默认包 + 内置工具），运行地址从
audio.cpp 插件的运行实例里取。未下载的模型可在卡片内下载；下载完成后自动登记/
加载，处理前也会补登记。没有实例时自动拉起可管理实例；一台都没有时页面仍可用——
只做裁剪也可以生成结果。

三条口径：

1. 开了收尾时，原声与结果在模型之后过同一套声道与峰值处理，试听听到的差异只来自
   模型，不来自电平与声道；关掉时两边都不做高通与限峰，只统一成单声道；
2. 时长偏差超过 :data:`DURATION_TOLERANCE` 的条目只拒绝**替换**：贴齐、逐字稿、平台
   凭据都按这一条文件的位置对齐，长度变了它们全错位；入库是新样例，不受这条限制；
3. 单模型并发 1，队列按文件名顺序推进，中途取消在条与条之间生效。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.infra import atomic, audioprobe, ffmpeg
from app.infra.config import data_dir
from app.infra.db import HubDB
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import runtimes as audiocpp_runtimes
from app.producers.builtin.audiocpp.manager import client as audiocpp_client
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
#: 没有任何可跑的模型时，步骤卡上的说明。卡片已经写了「无模型」，这里只给下一步动作。
_NO_SERVICE = "先到 audio.cpp 页启动实例并加载模型。"
#: 下载/加载任务的活跃状态；这些状态下不重复发起。
_ACTIVE_JOB_STATES = {"queued", "running", "cancelling", "downloading", "loading"}
#: 内置工具的权重目录名；与官方模型分开，由本插件自己下载。
_BUILTIN_DIR = "builtin_audio_utils"


def _audio_runtime() -> Any | None:
    """audio.cpp 插件实例；没启用或没构造时返回空。"""
    from app.usecase.producers import started_service

    return started_service("audiocpp")


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


def _chain_label(job: _Job, item: _Item) -> str:
    """这一条的处理链，写进 sidecar 的 ``params`` 与界面提示。

    裁剪写在最前：它是用户对内容的改动，不是模型给的。什么都没做时返回空串，
    由调用方决定怎么记。
    """
    parts = [f"{step['label']} {step['model_label']}" for step in job.steps]
    if job.normalize:
        parts.append(models.CORE_STEP["label"])
    chain = " · ".join(parts)
    if item.segments:
        if len(item.segments) > 1:
            cut = f"裁剪 {len(item.segments)} 段"
        else:
            begin, end = item.segments[0]
            cut = f"裁剪 {begin:.2f}–{end:.2f} s"
        chain = f"{cut} · {chain}" if chain else cut
    return chain


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
    state: str = "pending"  # pending / running / done / applied / saved / failed / skipped
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
            if item.state in {"done", "applied", "saved", "failed", "skipped"}
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
        runtime_reader: Any = None,
    ) -> None:
        self._root = root
        self._settings = settings_reader
        self._models_fn = running_models
        self._runtime_fn = runtime_reader
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._thread: threading.Thread | None = None
        self._library_cache: LibraryService | None = None
        self._models_at = 0.0
        self._models_cache: list[dict[str, Any]] | None = None
        #: 下载与自动加载任务：key 与候选模型的 ``models.candidate_key`` 一致。
        self._model_jobs: dict[str, dict[str, Any]] = {}
        #: 内置工具权重目录；与 audio.cpp 的官方模型目录分开。
        self._builtin_root = self._root / "models" / _BUILTIN_DIR
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
        if self._models_fn is not None:
            rows = list(self._models_fn())
        elif self._runtime_fn is not None:
            runtime = self._runtime_fn()
            rows = list(runtime.running_models()) if runtime is not None else []
        else:
            rows = _runtime_models()
        self._models_cache = rows
        self._models_at = now
        return rows

    def _runtime(self) -> Any | None:
        if self._runtime_fn is not None:
            return self._runtime_fn()
        return _audio_runtime()

    def _require_runtime(self) -> Any:
        runtime = self._runtime()
        if runtime is None:
            raise ProducerError(
                "audio.cpp 插件没有运行：到「设置 → 插件」启用 audio.cpp 后再试。"
            )
        return runtime

    def _installed(self, runtime: Any | None) -> set[str]:
        if runtime is None:
            return set()
        try:
            return set(runtime.installed_packages())
        except Exception:  # noqa: BLE001 - 读不到就当作都没装
            return set()

    @staticmethod
    def _job_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        try:
            progress = float(row.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        return {
            "state": str(row.get("state") or ""),
            "progress": max(0.0, min(100.0, progress)),
            "message": str(row.get("message") or ""),
        }

    def _model_jobs_view(self, runtime: Any | None) -> dict[str, dict[str, Any]]:
        """候选模型 key → 下载/加载状态；官方下载与内置下载合在一张表。"""
        rows: dict[str, dict[str, Any]] = {}
        if runtime is not None:
            try:
                for package_id, job in runtime.download_jobs().items():
                    view = self._job_view(job)
                    if view is not None:
                        rows[f"pkg:{package_id}"] = view
            except Exception:  # noqa: BLE001, S110 - 实例忙或退出时不影响页面
                pass
        with self._lock:
            rows.update({key: dict(job) for key, job in self._model_jobs.items()})
        return rows

    def _set_job(self, key: str, *, state: str, progress: float = 0.0, message: str = "") -> None:
        with self._lock:
            self._model_jobs[key] = {
                "state": state,
                "progress": round(max(0.0, min(100.0, progress)), 1),
                "message": message,
            }

    # ---------------------------------------------------------------- 界面状态

    def state(self) -> dict[str, Any]:
        runtime = self._runtime()
        return {
            "steps": self._steps_view(self._running_models(), runtime),
            "core": models.CORE_STEP,
            "job": self.job_view(),
            "limits": {"tolerance": DURATION_TOLERANCE},
        }

    def _steps_view(self, running: list[dict[str, Any]], runtime: Any | None) -> list[dict[str, Any]]:
        installed = self._installed(runtime)
        jobs = self._model_jobs_view(runtime)
        catalog_root = getattr(runtime, "root", None) if runtime is not None else None
        rows: list[dict[str, Any]] = []
        for step in models.STEPS:
            candidates = models.candidates_for(
                step, running, installed, self._builtin_root, catalog_root
            )
            for candidate in candidates:
                job = jobs.get(models.candidate_key(candidate))
                if job is not None:
                    candidate["job"] = job
            rows.append(
                {
                    "key": step.key,
                    "label": step.label,
                    "note": step.note,
                    "order": step.order,
                    "task": step.task,
                    "default": step.default,
                    "models": candidates,
                    "reason": ""
                    if candidates
                    else (
                        _NO_SERVICE
                        if not running
                        else "实例里没有登记这一步的模型。"
                    ),
                }
            )
        return rows

    # ---------------------------------------------------------------- 模型准备

    def _candidate(
        self,
        step: models.Step,
        model_id: str,
        runtime: Any | None,
        running: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        runtime = runtime if runtime is not None else self._runtime()
        rows = running if running is not None else self._running_models()
        catalog_root = getattr(runtime, "root", None) if runtime is not None else None
        found = models.find_candidate(
            step,
            model_id,
            rows,
            self._installed(runtime),
            self._builtin_root,
            catalog_root,
        )
        if found is None:
            raise ProducerError(f"「{step.label}」没有登记这个模型。")
        return found

    def prepare_model(self, step_key: str, model_id: str) -> dict[str, Any]:
        """下载一个候选模型，并在完成后自动登记/加载。

        已经下载的模型直接加载；下载由后台线程推进，页面只轮询状态。
        """
        step = models.step_of(step_key)
        if step is None:
            raise ProducerError("没有这一步。")
        runtime = self._require_runtime()
        candidate = self._candidate(step, model_id, runtime)
        key = models.candidate_key(candidate)
        with self._lock:
            job = self._model_jobs.get(key)
        if job is not None and job.get("state") in _ACTIVE_JOB_STATES:
            return self.state()
        if candidate["downloaded"]:
            self._set_job(key, state="loading", progress=100, message="加载中")
            threading.Thread(
                target=self._load_in_background,
                args=(candidate, runtime),
                name=f"voiceclean-load-{key}",
                daemon=True,
            ).start()
            return self.state()
        if candidate["source"] == "builtin":
            self._start_builtin(candidate)
            return self.state()
        runtime.ensure_family_instance(
            str(candidate["family"]), str(candidate["label"])
        )
        runtime.install_model(str(candidate["package"]))
        self._start_package_watch(candidate, runtime)
        return self.state()

    def _load_candidate(self, candidate: dict[str, Any], runtime: Any) -> None:
        if not candidate.get("downloaded"):
            raise ProducerError("模型还没下载。")
        if candidate.get("source") == "builtin":
            utility = str(candidate.get("utility") or candidate["id"])
            path = models.builtin_path(self._builtin_root, utility)
            runtime.load_builtin(utility, path.parent)
        else:
            runtime.ensure_management()
            runtime.load_model(str(candidate["package"]))
        self._models_cache = None
        self._set_job(
            models.candidate_key(candidate),
            state="done",
            progress=100,
            message="已就绪",
        )

    def _load_in_background(self, candidate: dict[str, Any], runtime: Any) -> None:
        key = models.candidate_key(candidate)
        try:
            self._load_candidate(candidate, runtime)
        except Exception as exc:  # noqa: BLE001 - 后台加载失败写回候选状态
            self._set_job(key, state="failed", message=_brief(exc))

    def _ensure_candidate(self, candidate: dict[str, Any], runtime: Any | None) -> None:
        """处理前保证模型已经登记：下载过但实例重启后丢了注册的，这里补上。"""
        if models.running_of(candidate, self._running_models(fresh=True)) is not None:
            return
        if not candidate.get("downloaded"):
            raise ProducerError(f"「{candidate['label']}」还没下载。")
        if runtime is None:
            runtime = self._require_runtime()
        self._load_candidate(candidate, runtime)

    def _start_builtin(self, candidate: dict[str, Any]) -> None:
        key = models.candidate_key(candidate)
        self._set_job(key, state="downloading", message="准备下载")
        threading.Thread(
            target=self._download_builtin,
            args=(candidate,),
            name=f"voiceclean-builtin-{candidate['id']}",
            daemon=True,
        ).start()

    def _download_builtin(self, candidate: dict[str, Any]) -> None:
        key = models.candidate_key(candidate)
        try:
            self._fetch_builtin(candidate)
            candidate["downloaded"] = True
            self._set_job(key, state="loading", progress=100, message="加载中")
            self._load_candidate(candidate, self._require_runtime())
        except Exception as exc:  # noqa: BLE001 - 下载/加载失败写回候选状态
            self._set_job(key, state="failed", message=_brief(exc))

    def _fetch_builtin(self, candidate: dict[str, Any]) -> None:
        utility = str(candidate.get("utility") or candidate["id"])
        asset = models.builtin_asset(utility)
        if asset is None:
            raise ProducerError("这个内置工具没有下载清单。")
        target = models.builtin_path(self._builtin_root, utility)
        target.parent.mkdir(parents=True, exist_ok=True)
        key = models.candidate_key(candidate)
        part = target.with_name(f"{target.name}.part-{uuid.uuid4().hex[:8]}")
        last: Exception | None = None
        try:
            for prefix in audiocpp_runtimes.mirror_order():
                digest = hashlib.sha256()
                try:
                    with (
                        audiocpp_client().stream(
                            "GET",
                            f"{prefix}{asset['url']}",
                            timeout=httpx.Timeout(60.0, connect=15.0),
                            headers={"User-Agent": "VoiceHub/voiceclean"},
                        ) as response,
                        part.open("wb") as handle,
                    ):
                        response.raise_for_status()
                        total = int(response.headers.get("content-length") or asset["bytes"])
                        written = 0
                        for chunk in response.iter_bytes(512 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                            ratio = (written / total * 100) if total else 0
                            self._set_job(
                                key,
                                state="downloading",
                                progress=ratio,
                                message=f"{written / 1048576:.1f} MB",
                            )
                    if int(asset["bytes"]) and part.stat().st_size != int(asset["bytes"]):
                        raise ProducerError("下载的文件大小不对，已丢弃。")
                    if digest.hexdigest().lower() != str(asset["sha256"]).lower():
                        raise ProducerError("校验和不匹配，文件已丢弃。")
                    os.replace(part, target)
                    return
                except (httpx.HTTPError, OSError, ProducerError) as exc:
                    last = exc
                    part.unlink(missing_ok=True)
            raise ProducerError(f"下载失败：{_brief(last) if last else '没有可用的地址'}")
        finally:
            part.unlink(missing_ok=True)

    def _start_package_watch(self, candidate: dict[str, Any], runtime: Any) -> None:
        key = models.candidate_key(candidate)
        self._set_job(key, state="queued", message="等待下载")
        threading.Thread(
            target=self._watch_package,
            args=(candidate, runtime),
            name=f"voiceclean-package-{candidate['id']}",
            daemon=True,
        ).start()

    def _watch_package(self, candidate: dict[str, Any], runtime: Any) -> None:
        package = str(candidate["package"])
        key = models.candidate_key(candidate)
        deadline = time.monotonic() + 2 * 60 * 60
        idle = 0
        while time.monotonic() < deadline:
            job: dict[str, Any] | None = None
            try:
                job = runtime.download_jobs().get(package)
            except Exception:  # noqa: BLE001 - 实例切换时下一轮再读
                job = None
            if job:
                view = self._job_view(job) or {}
                state = str(view.get("state") or "")
                self._set_job(
                    key,
                    state=state or "downloading",
                    progress=float(view.get("progress") or 0),
                    message=str(view.get("message") or ""),
                )
                if state in _ACTIVE_JOB_STATES:
                    time.sleep(1.2)
                    continue
                if state in {"failed", "error", "cancelled", "canceled"}:
                    self._set_job(
                        key,
                        state="failed",
                        message=str(view.get("message") or "下载失败"),
                    )
                    return
            if package in self._installed(runtime):
                try:
                    candidate["downloaded"] = True
                    self._set_job(key, state="loading", progress=100, message="加载中")
                    self._load_candidate(candidate, runtime)
                except Exception as exc:  # noqa: BLE001
                    self._set_job(key, state="failed", message=_brief(exc))
                return
            idle = idle + 1 if not job else 0
            if idle > 60:
                self._set_job(
                    key,
                    state="failed",
                    message="下载没有开始：检查 audio.cpp 的模型管理是否可用。",
                )
                return
            time.sleep(1.2)
        self._set_job(key, state="failed", message="下载超时。")

    def job_view(self) -> dict[str, Any] | None:
        with self._lock:
            job = self._job
        return job.view() if job else None

    # ---------------------------------------------------------------- 目标

    def _resolve(self, scope: dict[str, Any]) -> tuple[list[_Item], dict[str, Any]]:
        """目标音色、音色组或电脑文件 → 待处理条目。

        音色只收本地的：平台音色的样例在引擎目录里，替换会让平台侧凭据指向被换的
        文件。``files`` 是处理前选中的样例；为空时退回 ``file``（单条）或整条音色。
        ``voices`` 是跨音色的处理序列，按数组顺序拼接条目。``kind='file'`` 时只认
        上传得到的 ``token``，不查音色库。``trims`` 按文件名给裁剪区间，未给的按整段处理。
        """
        only_file = str(scope.get("file") or "").strip()
        want_files = [
            str(value).strip()
            for value in (scope.get("files") or [])
            if str(value).strip()
        ]
        want_files = list(dict.fromkeys(want_files))
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

        groups = scope.get("voices") or []
        if isinstance(groups, list) and groups:
            return self._resolve_groups(groups)

        voice_id = str(scope.get("voice_id") or "").strip()
        if not voice_id:
            raise ProducerError("未选择音色。")
        library = self._library()
        try:
            detail = library.get_voice(voice_id)
        except LibraryError as exc:
            raise ProducerError(str(exc)) from exc
        if not detail.is_local or not detail.folder:
            raise ProducerError(f"「{detail.shown_name}」是平台音色，样例不在本机，无法处理。")
        items = self._voice_items(detail, want_files, only_file, trims, start=0)
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

    def _voice_items(
        self,
        detail: Any,
        files: list[str],
        only_file: str,
        trims: dict[str, Any],
        start: int,
    ) -> list[_Item]:
        """一条音色里按 ``files`` 的顺序取可处理样例，条目编号从 ``start`` 接下去。"""
        if not isinstance(trims, dict):
            raise ProducerError("裁剪范围格式不对，刷新页面后重试。")
        items: list[_Item] = []
        matched: set[str] = set()
        folder = Path(detail.folder)
        order = {name: index for index, name in enumerate(files)}
        for sample in detail.samples:
            if only_file and sample.file != only_file:
                continue
            if files and sample.file not in files:
                continue
            path = folder / sample.file
            if path.suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}:
                continue
            if not path.is_file():
                continue
            segments, trimmed = _segments_for(trims, sample.file, sample.duration)
            trim_start, trim_end = (
                (segments[0][0], segments[-1][1]) if segments else (0.0, 0.0)
            )
            matched.add(sample.file)
            items.append(
                _Item(
                    index=0,
                    voice_id=detail.id,
                    voice=detail.shown_name,
                    file=sample.file,
                    source=str(path),
                    segments=segments,
                    trim_start=trim_start,
                    trim_end=trim_end,
                    trimmed=trimmed,
                )
            )
        missing = [name for name in files if name not in matched]
        if missing:
            raise ProducerError(
                f"这些音频不在「{detail.shown_name}」的可处理范围里："
                + "、".join(missing[:3])
                + ("…" if len(missing) > 3 else "")
            )
        if not items:
            if only_file:
                raise ProducerError(f"「{detail.shown_name}」里没有这条音频：{only_file}")
            if files:
                raise ProducerError("没有选中任何可处理的音频。")
            raise ProducerError(f"「{detail.shown_name}」没有可处理的音频。")
        if files:
            items.sort(key=lambda item: order.get(item.file, len(order)))
        else:
            items.sort(key=lambda item: item.file)
        for offset, item in enumerate(items):
            item.index = start + offset
        return items

    def _resolve_groups(
        self, groups: list[Any]
    ) -> tuple[list[_Item], dict[str, Any]]:
        """跨音色的处理序列：按组的先后拼接条目，组内按 ``files`` 顺序。"""
        library = self._library()
        items: list[_Item] = []
        summaries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in groups:
            if not isinstance(raw, dict):
                continue
            voice_id = str(raw.get("voice_id") or "").strip()
            if not voice_id:
                continue
            try:
                detail = library.get_voice(voice_id)
            except LibraryError as exc:
                raise ProducerError(str(exc)) from exc
            if not detail.is_local or not detail.folder:
                raise ProducerError(f"「{detail.shown_name}」是平台音色，样例不在本机，无法处理。")
            files: list[str] = []
            for value in raw.get("files") or []:
                name = str(value).strip()
                if name and name not in files:
                    files.append(name)
            only_file = str(raw.get("file") or "").strip()
            rows = self._voice_items(
                detail,
                files,
                only_file,
                raw.get("trims") or {},
                start=len(items),
            )
            fresh = [row for row in rows if (row.voice_id, row.file) not in seen]
            if not fresh:
                continue
            for row in fresh:
                seen.add((row.voice_id, row.file))
            items.extend(fresh)
            summaries.append(
                {"voice_id": detail.id, "label": detail.shown_name, "count": len(fresh)}
            )
        if not items:
            raise ProducerError("处理序列里没有可处理的音频。")
        for index, item in enumerate(items):
            item.index = index
        label = (
            f"{summaries[0]['label']} · {len(items)} 条"
            if len(summaries) == 1
            else f"{len(summaries)} 个音色 · {len(items)} 条"
        )
        return items, {
            "kind": "voices",
            "voice_id": summaries[0]["voice_id"],
            "voices": summaries,
            "file": "",
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
        runtime = self._runtime()
        installed = self._installed(runtime)
        catalog_root = getattr(runtime, "root", None) if runtime is not None else None
        selected: list[tuple[models.Step, dict[str, Any], dict[str, Any]]] = []
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
            candidate = models.find_candidate(
                declaration,
                model_id,
                running,
                installed,
                self._builtin_root,
                catalog_root,
            )
            if candidate is None:
                raise ProducerError(
                    f"「{declaration.label}」没有登记这个模型；重新进一次页面再选。"
                )
            selected.append((declaration, candidate, step))
        if selected:
            # 下载过但实例重启后丢了注册的模型，在这里自动补登记，不让用户手动加载。
            for _declaration, candidate, _step in selected:
                self._ensure_candidate(candidate, runtime)
            running = self._running_models(fresh=True)
        wanted: list[dict[str, Any]] = []
        for declaration, candidate, step in selected:
            row = models.running_of(candidate, running)
            if row is None:
                raise ProducerError(
                    f"「{declaration.label}」的模型没有加载成功，检查 audio.cpp 的模型管理。"
                )
            host = str(row.get("url") or "")
            if not host:
                raise ProducerError(f"「{declaration.label}」的模型找不到服务地址，重新进一次页面再试。")
            wanted.append(
                {
                    "key": declaration.key,
                    "label": declaration.label,
                    "order": declaration.order,
                    "model": str(row.get("id") or candidate["id"]),
                    "model_label": candidate["label"],
                    "prep": candidate["prep"],
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
        # 原声副本就是后面所有步骤的输入：裁剪在这里一次完成。处理前只做"能喂给
        # 模型"的对齐（单声道），高通与限峰属于收尾，放在所有模型步骤之后——
        # 模型看到的是原始电平，A/B 听到的差异只来自模型。
        ffmpeg.normalize(
            Path(item.source),
            origin,
            channels=1,
            peak=0,
            segments=item.segments or None,
        )
        origin_rate = audioprobe.probe_stream(origin)[1]

        current = origin
        for step in job.steps:
            current = self._run_step(job, item, step, current, work)

        result = work / "result.wav"
        # 回库的结果按原样例采样率落盘：模型输入率是模型的要求（分离 44.1 kHz、
        # 小工具 16 kHz），不能因此把库里的样例也改低。文件目标保留模型原生输出。
        target_rate = origin_rate if job.scope.get("kind") != "file" else 0
        if job.normalize:
            ffmpeg.normalize(
                current, result, rate=target_rate, highpass=_HIGHPASS, peak=_PEAK
            )
        elif target_rate:
            # 收尾关掉时同样只统一单声道，不做高通与限峰。
            ffmpeg.normalize(current, result, rate=target_rate, peak=0)
        else:
            shutil.copyfile(current, result)
        # 入库用：模型输出比原样例高时才保留原生采样率，带宽扩展补出的高频不会被
        # 降回去；低于原样例时仍按原样例落盘，不把库里的采样率改低。
        native_rate = origin_rate if current is origin else audioprobe.probe_stream(current)[1]
        if target_rate and native_rate > target_rate:
            native = work / "result-native.wav"
            if job.normalize:
                ffmpeg.normalize(
                    current, native, rate=native_rate, highpass=_HIGHPASS, peak=_PEAK
                )
            else:
                ffmpeg.normalize(current, native, rate=native_rate, peak=0)
        # 收尾对原声副本也做一次：两边同一套参数，试听差异只来自模型。
        if job.normalize:
            finished = work / "origin-finished.wav"
            ffmpeg.normalize(origin, finished, highpass=_HIGHPASS, peak=_PEAK)
            os.replace(finished, origin)
        item.origin_seconds = audioprobe.probe_duration(origin)
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
        """丢掉已完成 / 已替换 / 已入库 / 已取消的任务，回到可编辑状态。"""
        with self._lock:
            job = self._job
            if job is not None and job.state == "running":
                raise ProducerError("当前任务还在进行，先取消再清空。")
            self._job = None
        return {"cleared": True}

    # ---------------------------------------------------------------- 试听、替换与入库

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
        if job.state == "saved":
            raise ProducerError("这一轮已经入库；要替换就先「继续编辑」再处理一遍。")
        wanted = {int(value) for value in indexes}
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
            item_chain = _chain_label(job, item) or "裁剪"
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

    def save(self, job_id: str, indexes: list[int]) -> dict[str, Any]:
        """把勾选条目的处理结果作为**新样例**入库，原样例不动。

        与 :meth:`apply` 的分工：替换覆盖同一条样例，入库另存一条，原声留在库里。
        因此不要求时长一致——带宽扩展补出高频、修复改变了长度，都能入库；失败或
        还没跑完的条目仍然跳过。模型输出比原样例采样率高时用原生结果
        （``result-native.wav``），补出的高频不会被降回原采样率。
        """
        with self._lock:
            job = self._job
        if job is None or job.id != job_id:
            raise ProducerError("任务已过期，重新跑一遍。")
        if job.scope.get("kind") == "file":
            raise ProducerError("电脑文件没有可入库的音色样例；请下载处理结果。")
        if job.state == "applied":
            raise ProducerError("这一轮已经替换过样例；要入库就先「继续编辑」再处理一遍。")
        wanted = {int(value) for value in indexes}
        library = self._library()
        saved: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for item in job.items:
            if item.index not in wanted:
                continue
            if item.state != "done":
                failures.append({"index": item.index, "reason": item.error or "未完成"})
                continue
            work = job.folder / f"{item.index:04d}"
            native = work / "result-native.wav"
            source = native if native.is_file() else work / "result.wav"
            if not source.is_file():
                failures.append({"index": item.index, "reason": "处理结果不在了"})
                continue
            try:
                sample = library.add_sample(
                    item.voice_id,
                    source,
                    name=item.file,
                    params={
                        "处理": _chain_label(job, item) or "裁剪",
                        "来源": "音色处理",
                    },
                )
            except LibraryError as exc:
                failures.append({"index": item.index, "reason": str(exc)})
                continue
            item.state = "saved"
            saved.append(
                {
                    "index": item.index,
                    "voice_id": item.voice_id,
                    "voice": item.voice,
                    "file": sample.file,
                }
            )
        if saved:
            job.state = "saved"
        self._save(job)
        return {"saved": len(saved), "files": saved, "failures": failures, "job": job.view()}

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
