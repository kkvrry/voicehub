"""音色处理服务：按目标音色跑步骤、留 A/B 产物、按确认替换。

目标只有两种：一条音色的全部样例，或它的一条样例（``file`` 非空）。一条音频的流程
固定为「原声副本 → 勾选的模型步骤 → 规整」，两条产物（``origin.wav`` / ``result.wav``）
留在任务目录里供试听，**确认之前不动音色库**。

三条口径：

1. 原声与结果都过同一套规整，试听听到的差异只来自模型，不来自电平与声道；
2. 时长偏差超过 :data:`DURATION_TOLERANCE` 的条目不参与替换：贴齐、逐字稿、平台凭据
   都按这一条文件的位置对齐，长度变了它们全错位；
3. 单模型并发 1，队列按文件名顺序推进，中途取消在条与条之间生效。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.infra import audioprobe, ffmpeg
from app.infra.config import data_dir
from app.infra.db import HubDB
from app.producers.base import ProducerError
from app.producers.builtin.voiceclean import client, models
from app.usecase.library import LibraryError, LibraryService

__all__ = ["DURATION_TOLERANCE", "VoiceCleanService"]

#: 时长允许的偏差（秒）。50 ms 以内听不出来，超过就是内容变了或对齐错了。
DURATION_TOLERANCE = 0.05
#: 规整参数：口播素材 60 Hz 以下没有内容；限峰 0.891 ≈ −1 dBFS。
_HIGHPASS = 60
_PEAK = 0.891
#: 探测结果的缓存时长（秒）：state 会被轮询，每次去连一遍服务没有必要。
_PROBE_TTL = 5.0


@dataclass
class _Item:
    """一条待处理音频。``source`` 是库里的原件，产物另存。"""

    index: int
    voice_id: str
    voice: str
    file: str
    source: str
    origin_seconds: float = 0.0
    result_seconds: float = 0.0
    state: str = "pending"  # pending / done / applied / failed / skipped
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
    state: str = "running"
    note: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        finished = sum(1 for item in self.items if item.state != "pending")
        return {
            "id": self.id,
            "state": self.state,
            "note": self.note,
            "scope": self.scope,
            "steps": self.steps,
            "total": len(self.items),
            "finished": finished,
            "failed": sum(1 for item in self.items if item.state == "failed"),
            "items": [item.view() for item in self.items],
        }


class VoiceCleanService:
    """一个插件实例一个服务；任务同一时刻只跑一个。"""

    def __init__(self, root: Path, settings_reader: Any) -> None:
        self._root = root
        self._settings = settings_reader
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._thread: threading.Thread | None = None
        self._library_cache: LibraryService | None = None
        self._probe_at = 0.0
        self._probe: tuple[bool, str, Path | None, set[str], list[str]] | None = None
        self._root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 配置与探测

    def _cfg(self) -> Any:
        return self._settings()

    def _host(self) -> str:
        return client.normalize_host(str(getattr(self._cfg(), "host", "") or ""))

    def _timeout(self) -> float:
        try:
            return max(10.0, float(getattr(self._cfg(), "timeout", 600) or 600))
        except (TypeError, ValueError):
            return 600.0

    def _library(self) -> LibraryService:
        if self._library_cache is None:
            self._library_cache = LibraryService(HubDB(data_dir() / "hub.db"))
        return self._library_cache

    def _probe_service(self, *, fresh: bool = False) -> tuple[bool, str, Path | None, set[str], list[str]]:
        """服务是否活着、模型目录、已装包、已注册模型。结果按 :data:`_PROBE_TTL` 缓存。"""
        now = time.monotonic()
        if self._probe is not None and not fresh and now - self._probe_at < _PROBE_TTL:
            return self._probe
        host = self._host()
        if not host:
            result: tuple[bool, str, Path | None, set[str], list[str]] = (
                False,
                "未填写模型服务地址（设置 → 音色处理）。",
                None,
                set(),
                [],
            )
        else:
            if not client.reachable(host):
                result = (
                    False,
                    f"无法连接 audio.cpp（{host}）：服务未启动或地址不对。",
                    None,
                    set(),
                    [],
                )
                self._probe = result
                self._probe_at = now
                return result
            try:
                loaded = client.loaded_models(host, timeout=2.0)
            except ProducerError as exc:
                result = (False, str(exc), None, set(), [])
            else:
                root = client.models_root(host, timeout=0.5)
                installed: set[str] = set()
                if root is not None:
                    try:
                        from app.producers.builtin.audiocpp import catalog

                        installed = catalog.installed_packages(root)
                    except Exception:  # noqa: BLE001 - 清单坏掉只影响"是否已装"的标注
                        installed = set()
                reason = "" if loaded else "服务在线，但没有注册任何模型；先在运行时页加载一个。"
                result = (True, reason, root, installed, loaded)
        self._probe = result
        self._probe_at = now
        return result

    # ---------------------------------------------------------------- 界面状态

    def state(self) -> dict[str, Any]:
        ready, reason, _root, installed, loaded = self._probe_service()
        return {
            "host": self._host(),
            "ready": ready,
            "reason": reason,
            "loaded": loaded,
            "steps": self._steps_view(installed),
            "core": models.CORE_STEP,
            "job": self.job_view(),
            "limits": {"tolerance": DURATION_TOLERANCE},
        }

    def _steps_view(self, installed: set[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for step in models.STEPS:
            rows.append(
                {
                    "key": step.key,
                    "label": step.label,
                    "note": step.note,
                    "order": step.order,
                    "task": step.task,
                    "default": step.default,
                    "models": models.candidates(step.task, None, installed),
                }
            )
        return rows

    def job_view(self) -> dict[str, Any] | None:
        with self._lock:
            job = self._job
        return job.view() if job else None

    # ---------------------------------------------------------------- 目标

    def _resolve(self, scope: dict[str, Any]) -> tuple[list[_Item], dict[str, Any]]:
        """目标音色（可再限定一条样例）→ 待处理条目。

        只收本地音色：平台音色的样例在引擎目录里，替换会让平台侧凭据指向被换的文件。
        """
        voice_id = str(scope.get("voice_id") or "").strip()
        only_file = str(scope.get("file") or "").strip()
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
        folder = Path(detail.folder)
        for sample in detail.samples:
            if only_file and sample.file != only_file:
                continue
            path = folder / sample.file
            if path.suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}:
                continue
            if not path.is_file():
                continue
            items.append(
                _Item(
                    index=len(items),
                    voice_id=detail.id,
                    voice=detail.shown_name,
                    file=sample.file,
                    source=str(path),
                )
            )
        if not items:
            if only_file:
                raise ProducerError(f"「{detail.shown_name}」里没有这条音频：{only_file}")
            raise ProducerError(f"「{detail.shown_name}」没有可处理的音频。")
        items.sort(key=lambda item: item.file)
        for index, item in enumerate(items):
            item.index = index
        label = f"音色「{detail.shown_name}」"
        if only_file:
            label = f"「{detail.shown_name}」· {only_file}"
        return items, {"kind": "voice", "label": label, "count": len(items)}

    # ---------------------------------------------------------------- 执行

    def run(self, scope: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise ProducerError("上一次处理还在进行，先等它结束或取消。")

        wanted: list[dict[str, Any]] = []
        for step in steps:
            if not step.get("enabled", True):
                continue
            declaration = models.step_of(str(step.get("key") or ""))
            if declaration is None:
                continue
            model_id = str(step.get("model") or "").strip()
            if not model_id:
                raise ProducerError(f"「{declaration.label}」没有选模型。")
            candidates = {
                row["id"]: row for row in models.candidates(declaration.task, None, self._probe_service()[3])
            }
            chosen = candidates.get(model_id)
            if chosen is None:
                raise ProducerError(f"「{declaration.label}」的模型「{model_id}」不在可用清单里。")
            if not chosen["installed"]:
                raise ProducerError(f"「{chosen['label']}」尚未安装；先在步骤卡上安装。")
            wanted.append(
                {
                    "key": declaration.key,
                    "label": declaration.label,
                    "model": model_id,
                    "model_label": chosen["label"],
                    "prep": chosen["prep"],
                    "options": dict(step.get("options") or {}),
                }
            )
        if not wanted:
            raise ProducerError("至少勾选一个步骤。")
        if not self._probe_service()[0]:
            raise ProducerError(self._probe_service()[1])

        items, info = self._resolve(scope)
        job_id = uuid.uuid4().hex[:12]
        folder = self._root / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        job = _Job(id=job_id, folder=folder, scope=info, steps=wanted, items=items)
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
        ffmpeg.normalize(Path(item.source), origin, highpass=_HIGHPASS, peak=_PEAK)
        item.origin_seconds = audioprobe.probe_duration(origin)

        current = origin
        for step in job.steps:
            current = self._run_step(job, item, step, current, work)

        result = work / "result.wav"
        ffmpeg.normalize(current, result, highpass=_HIGHPASS, peak=_PEAK)
        item.result_seconds = audioprobe.probe_duration(result)
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
            self._host(),
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

    def apply(self, job_id: str, indexes: list[int]) -> dict[str, Any]:
        """按勾选的条目替换；原件进回收站，sidecar 记处理链。"""
        with self._lock:
            job = self._job
        if job is None or job.id != job_id:
            raise ProducerError("任务已过期，重新跑一遍。")
        wanted = {int(value) for value in indexes}
        chain = " · ".join(f"{step['label']} {step['model_label']}" for step in job.steps)
        replaced = 0
        failures: list[dict[str, Any]] = []
        library = self._library()
        for item in job.items:
            if item.index not in wanted:
                continue
            if item.state != "done" or not item.replaceable:
                failures.append({"index": item.index, "reason": item.error or "未完成"})
                continue
            source = job.folder / f"{item.index:04d}" / "result.wav"
            try:
                library.replace_sample_audio(
                    item.voice_id,
                    item.file,
                    source,
                    params={"处理": chain, "来源": "音色处理"},
                    seconds=item.result_seconds,
                )
            except LibraryError as exc:
                failures.append({"index": item.index, "reason": str(exc)})
                continue
            item.state = "applied"
            replaced += 1
        job.state = "applied" if replaced else job.state
        self._save(job)
        return {"replaced": replaced, "failures": failures, "job": job.view()}

    def install(self, package_id: str) -> dict[str, Any]:
        if not package_id:
            raise ProducerError("缺模型包 id。")
        client.install(self._host(), package_id)
        self._probe = None
        return self.state()

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
