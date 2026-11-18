"""配音服务：抽音轨 → 转录 → 逐段合成 → 贴齐 → 混音 → 导出。

每一步只做"读输入、写输出"，宿主负责切段、重采样、缓存与串行；
识别与合成走宿主已有的用例（``usecase.asr`` / ``usecase.gateway``），
本插件不认识任何一家模型。

时间轴只有一条规则：**段落的时间码来自源音频的实测边界**，不是按字数估的。
估出来的时间轴一定与口型错位，而错位无法在后期修正。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.infra import ffmpeg as ffmpeg_tools
from app.infra.archive import safe_segment
from app.infra.audioprobe import probe_duration, probe_media
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.settings import LOUDNESS_TARGETS, DubbingSettings
from app.producers.builtin.dubbing.store import DubbingStore
from app.usecase import asr as asr_uc
from app.usecase import assembly
from app.usecase import subtitles as subtitle_uc
from app.usecase.subtitles import normalize_encoding

if TYPE_CHECKING:
    from app.usecase.library import LibraryService

__all__ = ["STEPS", "DubbingService"]

#: 段落状态：``empty`` 未合成 / ``ready`` 有产物 / ``failed`` 失败 / ``stale`` 上游变了。
#: 只在这四处出现，界面按它决定行上的主操作。
STEPS: tuple[tuple[str, str, str], ...] = (
    ("extract", "抽音轨", "解复用源文件，取出一条完整音轨"),
    ("transcribe", "转录", "按静音切段逐段识别，段落边界即字幕时间码"),
    ("cast", "角色", "给每段指定说话人与音色"),
    ("synth", "配音", "按角色逐段合成"),
    ("align", "贴齐", "按时间码补静音、加速贴齐、越界顺延"),
    ("mix", "混音", "人声与原声按电平混合"),
    ("export", "导出", "封装成片，写出字幕"),
)

#: 太短的段不单独成句：那是呼吸或杂音，识别出来也只有一个字。
_MIN_SEGMENT_S = 0.35
#: 切段时两端各留一点余量，避免把首尾的辅音切掉。
_SEGMENT_PAD_S = 0.06
#: 任务线程的回收：跑完就删，不必等 GC。
_JOB_THREADS: dict[str, threading.Thread] = {}
_JOB_GUARD = threading.Lock()


def _join_text(left: str, right: str) -> str:
    """合并两段文本：中文直接相连，西文之间补一个空格。

    中文句子之间插空格会让字幕看着像机器拼接的；西文不补空格又会粘成一个词。
    判据只看边界字符，不猜语种。
    """
    left, right = left.strip(), right.strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "，。！？；：、,.!?;:）)" or right[0] in "，。！？；：、,.!?;:（(":
        return left + right
    if "\u4e00" <= left[-1] <= "\u9fff" or "\u4e00" <= right[0] <= "\u9fff":
        return left + right
    return f"{left} {right}"


def _now_ms(seconds: float) -> int:
    return round(max(0.0, seconds) * 1000)


def _speech_segments(
    spans: Sequence[tuple[float, float]],
    duration: float,
    *,
    max_segment: float,
    min_speech: float = _MIN_SEGMENT_S,
    pad: float = _SEGMENT_PAD_S,
) -> list[tuple[float, float]]:
    """静音区间取补集 → 说话区间。

    - 两端各留 ``pad`` 秒，不切断字头字尾；
    - 短于 ``min_speech`` 的丢弃（呼吸、翻页、椅子响）；
    - 超过 ``max_segment`` 的按等长硬切：没有停顿就没有依据，硬切至少让每段
      能被一次识别请求容纳，字数分配由识别结果决定。
    """
    if duration <= 0:
        return []
    edges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        start = max(0.0, float(start))
        end = min(duration, float(end))
        if start - cursor >= min_speech:
            edges.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_speech:
        edges.append((cursor, duration))

    out: list[tuple[float, float]] = []
    for start, end in edges:
        start = max(0.0, start - pad)
        end = min(duration, end + pad)
        if end - start < min_speech:
            continue
        if max_segment > 0 and end - start > max_segment:
            pieces = int((end - start) // max_segment) + 1
            span = (end - start) / pieces
            out.extend((start + span * i, start + span * (i + 1)) for i in range(pieces))
        else:
            out.append((start, end))
    return [(round(a, 3), round(b, 3)) for a, b in out]


class DubbingService:
    def __init__(
        self,
        root: Path,
        settings: Callable[[], DubbingSettings] | DubbingSettings | None = None,
    ) -> None:
        self.root = root
        self.store = DubbingStore(root)
        #: 插件传 getter（设置页改完立即生效），测试可以直接传一份模型。
        self._settings = settings or DubbingSettings

    @property
    def settings(self) -> DubbingSettings:
        value = self._settings
        return value() if callable(value) else value

    # ---- 路径 -------------------------------------------------------------

    @property
    def projects_root(self) -> Path:
        folder = self.settings.project_dir.strip()
        root = Path(folder).expanduser() if folder else self.root / "projects"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def folder(self, project_id: str) -> Path:
        return self.projects_root / project_id

    def _work(self, project_id: str, name: str) -> Path:
        path = self.folder(project_id) / "work" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _out(self, project_id: str) -> Path:
        path = self.folder(project_id) / "out"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.project(project_id)
        if project is None:
            raise ProducerError("找不到这个配音项目：它可能已被删除。")
        return project

    def _abs(self, project: dict[str, Any], rel: str) -> Path:
        return self.folder(project["id"]) / rel

    def source_file(self, project: dict[str, Any]) -> Path:
        return self._abs(project, project["source_path"])

    def original(self, project: dict[str, Any]) -> Path:
        """抽出来的那条完整音轨（44.1k 立体声 PCM）；不存在时返回约定路径。"""
        return self._abs(project, project["audio_path"] or "audio/original.wav")

    # ---- 项目 -------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        reason = asr_uc.unavailable_reason()
        return {
            "storage": str(self.projects_root),
            "projects": self.store.list_projects(),
            "asr": {"ready": not reason, "note": reason},
            "steps": [{"key": k, "label": v, "note": n} for k, v, n in STEPS],
        }

    def create(
        self,
        *,
        name: str,
        upload: Path,
        suffix: str,
        stream: int = 0,
        source_name: str = "",
    ) -> dict[str, Any]:
        """从一份上传建立项目：先探时长，探不出就拒收。

        时长不可知就没有时间轴，项目建出来也只能停在第一步——不如当场说清楚。
        """
        info = probe_media(upload)
        if info.duration <= 0:
            raise ProducerError(
                "读不出这个文件的时长：它可能不是音视频，或者 ffprobe 没装好。"
                "时长未知时无法生成时间轴，已取消导入。"
            )
        if not info.audio:
            raise ProducerError("这个文件里没有音轨：视频至少要有声音才能转写。")
        chosen = next((item for item in info.audio if item.index == stream), None)
        if chosen is None:
            chosen = info.default_audio
        assert chosen is not None

        title = (name or Path(upload).stem or "未命名项目").strip()
        project = self.store.create_project(
            name=title,
            # 原始文件名来自上传那一层：`upload` 是临时文件，名字是一串随机码。
            source_name=source_name or Path(upload).name,
            source_path="",
            kind="video" if info.has_video else "audio",
            duration_ms=_now_ms(info.duration),
            stream_index=chosen.index,
            audio_path="",
        )
        folder = self.folder(project["id"])
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"source{suffix or Path(upload).suffix or '.bin'}"
        # 上传文件在临时目录：同一分区时 rename 即可，跨分区会退化成复制。
        shutil.move(str(upload), str(source))
        try:
            rel = source.relative_to(folder).as_posix()
        except ValueError:  # pragma: no cover - 同目录必然成立
            rel = source.name
        self.store.update_project(
            project["id"], source_path=rel, status="ready", stream_index=chosen.index
        )
        # 旁白先建出来：不属于任何角色的段落都归它，界面左列不会从空表开始。
        self.store.add_role(project["id"], name="旁白")
        try:
            self.ensure_audio(project["id"])
        except ProducerError as exc:
            # 抽不出来不该把项目变成"不存在"：段落、源文件都还在，
            # 界面在第一步显示原因，修好 ffmpeg 后点重试即可。
            CHANNEL.warning(f"抽音轨未完成：{exc}", source="dubbing")
        return self.detail(project["id"])

    def delete(self, project_id: str) -> None:
        self._project(project_id)
        self.store.delete_project(project_id)
        folder = self.folder(project_id)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)

    def ensure_audio(self, project_id: str) -> Path:
        """确保有一条统一格式的原音轨；已有就复用（文件名固定，不带指纹）。"""
        project = self._project(project_id)
        target = self.folder(project_id) / "audio" / "original.wav"
        if target.is_file() and target.stat().st_size > 44:
            if project["audio_path"] != "audio/original.wav":
                self.store.update_project(project_id, audio_path="audio/original.wav")
            return target
        source = self.source_file(project)
        if not source.is_file():
            raise ProducerError("源文件不在了：项目目录被移动或删除过。")
        stream = int(project.get("stream_index") or 0)
        try:
            ffmpeg_tools.extract_audio(source, target, stream=stream)
        except ffmpeg_tools.FfmpegError as exc:
            raise ProducerError(str(exc)) from exc
        self.store.update_project(project_id, audio_path="audio/original.wav")
        CHANNEL.info(
            f"抽音轨完成：{source.name}（第 {stream} 条）",
            source="dubbing",
            project=project_id,
        )
        return target

    def detail(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        roles = self.store.roles(project_id)
        cues = self.store.cues(project_id)
        stats = self.store.stats(project_id)
        job = self.store.latest_job(project_id)
        out = self._out(project_id)
        duration = project["duration_ms"] / 1000.0
        has_audio = self.original(project).is_file()
        vocal = out / "vocal.wav"
        mix = out / "mix.wav"
        # 成品只认"导出那一步写出来的文件"：vocal/mix 与字幕是中间产物，
        # 把它们算进来会让「导出」在没导出时也显示完成。
        skip = {"vocal.wav", "mix.wav"}
        subtitle_suffixes = {".srt", ".vtt", ".lrc", ".txt", ".ass"}
        exports = (
            sorted(
                (
                    item.name
                    for item in out.iterdir()
                    if item.is_file()
                    and item.name not in skip
                    and item.suffix.lower() not in subtitle_suffixes
                ),
                key=str.lower,
            )
            if out.is_dir()
            else []
        )
        reason = asr_uc.unavailable_reason()

        steps = [
            {
                "key": "extract",
                "label": "抽音轨",
                "state": "done" if has_audio else "wait",
                "summary": (
                    f"{project['kind'] == 'video' and '视频' or '音频'} · "
                    f"{duration:.1f}s · 第 {int(project['stream_index']) + 1} 条音轨"
                    if has_audio
                    else "等待 ffmpeg"
                ),
            },
            {
                "key": "transcribe",
                "label": "转录",
                "state": (
                    "run" if job and job["kind"] == "transcribe" and job["state"] == "running"
                    else ("done" if stats["total"] else "wait")
                ),
                "summary": (
                    f"{stats['total']} 段" if stats["total"] else (reason or "未开始")
                ),
            },
            {
                "key": "cast",
                "label": "角色",
                "state": "done" if any(role["voice_id"] or role["mode"] != "tts" for role in roles) else "wait",
                "summary": f"{len(roles)} 个角色" if roles else "未分配",
            },
            {
                "key": "synth",
                "label": "配音",
                "state": (
                    "run" if job and job["kind"] == "synth" and job["state"] == "running"
                    else ("done" if stats["total"] and stats["ready"] == stats["total"] else "wait")
                ),
                "summary": f"{stats['ready']} / {stats['total']} 段" if stats["total"] else "未开始",
            },
            {
                "key": "align",
                "label": "贴齐",
                "state": "done" if vocal.is_file() else "wait",
                "summary": (
                    f"贴齐 {duration:.1f}s 时间轴" if vocal.is_file() else "合成后自动执行"
                ),
            },
            {
                "key": "mix",
                "label": "混音",
                "state": "done" if mix.is_file() else "wait",
                "summary": "人声 + 原声" if mix.is_file() else "未混音",
            },
            {
                "key": "export",
                "label": "导出",
                "state": "done" if exports else "wait",
                "summary": exports[-1] if exports else "未导出",
            },
        ]
        return {
            "object": "dubbing_project",
            "project": project,
            "roles": roles,
            "cues": cues,
            "stats": stats,
            "job": job,
            "steps": steps,
            "paths": {
                "source": project["source_path"],
                "original": project["audio_path"],
                "vocal": "out/vocal.wav" if vocal.is_file() else "",
                "mix": "out/mix.wav" if mix.is_file() else "",
                "exports": exports,
            },
            "asr": {"ready": not reason, "note": reason},
            "settings": self.settings.model_dump(),
        }

    # ---- 段落编辑 ---------------------------------------------------------

    def update_cue(self, project_id: str, cue_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """改一段：文本或角色变了，这一段及其产物即失效（只影响这一段）。"""
        self._project(project_id)
        cue = self.store.cue(cue_id)
        if cue is None or cue["project_id"] != project_id:
            raise ProducerError("找不到这一段。")
        fields: dict[str, Any] = {}
        for key in ("start_ms", "end_ms"):
            if key in patch:
                fields[key] = max(0, int(patch[key] or 0))
        if "text" in patch:
            fields["text"] = str(patch["text"] or "").strip()
        if "role_id" in patch:
            fields["role_id"] = str(patch["role_id"] or "")
        audio_changed = "text" in fields or "role_id" in fields
        if audio_changed and cue["audio"]:
            # 文本或角色变了，旧音频不再对应任何东西：标成过期而不是删掉，
            # 用户改回去时还能听回原来那条（重合成会覆盖）。
            fields["state"] = "stale"
        if fields:
            self.store.update_cue(cue_id, **fields)
        updated = self.store.cue(cue_id)
        assert updated is not None
        return updated

    def split_cue(self, project_id: str, cue_id: str, at_ms: int) -> list[dict[str, Any]]:
        """按时间点拆分一段；文本按字数比例分（没有词级时间戳时的唯一依据）。"""
        self._project(project_id)
        cue = self.store.cue(cue_id)
        if cue is None or cue["project_id"] != project_id:
            raise ProducerError("找不到这一段。")
        start, end = int(cue["start_ms"]), int(cue["end_ms"])
        if not start < at_ms < end:
            raise ProducerError("拆分点要落在这一段的起止之间。")
        text = str(cue["text"] or "")
        ratio = (at_ms - start) / max(1, end - start)
        head_len = max(1, round(len(text) * ratio))
        head, tail = text[:head_len].strip(), text[head_len:].strip()
        self.store.update_cue(cue_id, end_ms=at_ms, text=head, state="stale" if cue["audio"] else "empty")
        self.store.insert_cue(
            project_id,
            after_ordinal=int(cue["ordinal"]),
            start_ms=at_ms,
            end_ms=end,
            text=tail,
        )
        return self.store.cues(project_id)

    def merge_cues(self, project_id: str, cue_id: str) -> list[dict[str, Any]]:
        """与下一段合并：时间码取两段的并集，文本用空格连起来。"""
        self._project(project_id)
        cues = self.store.cues(project_id)
        index = next((i for i, item in enumerate(cues) if item["id"] == cue_id), -1)
        if index < 0:
            raise ProducerError("找不到这一段。")
        if index + 1 >= len(cues):
            raise ProducerError("这是最后一段，后面没有可合并的内容。")
        this, nxt = cues[index], cues[index + 1]
        text = _join_text(this["text"], nxt["text"])
        self.store.update_cue(
            cue_id,
            end_ms=int(nxt["end_ms"]),
            text=text,
            state="stale" if this["audio"] else "empty",
        )
        self.store.delete_cue(nxt["id"])
        # 删除后序号会出现空洞：重排一次，让「第 N 段」与界面上的序号一致。
        self.store.renumber(project_id)
        return self.store.cues(project_id)

    def delete_cue(self, project_id: str, cue_id: str) -> list[dict[str, Any]]:
        self._project(project_id)
        self.store.delete_cue(cue_id)
        return self.store.cues(project_id)

    # ---- 角色 -------------------------------------------------------------

    def add_role(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._project(project_id)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ProducerError("角色要有名字。")
        role = self.store.add_role(
            project_id,
            name=name,
            mode=str(payload.get("mode") or "tts"),
            engine=str(payload.get("engine") or ""),
            voice_id=str(payload.get("voice_id") or ""),
            voice_name=str(payload.get("voice_name") or ""),
            params=payload.get("params") or {},
        )
        return role

    def update_role(self, project_id: str, role_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._project(project_id)
        role = self.store.role(role_id)
        if role is None or role["project_id"] != project_id:
            raise ProducerError("找不到这个角色。")
        fields = {key: payload[key] for key in ("name", "mode", "engine", "voice_id", "voice_name", "params", "color") if key in payload}
        self.store.update_role(role_id, **fields)
        if {"engine", "voice_id", "params"} & set(fields):
            # 换了音色：这个角色名下的段落都要重配，标成过期而不是清空。
            self.store.stale_role_cues(role_id)
        updated = self.store.role(role_id)
        assert updated is not None
        return updated

    def delete_role(self, project_id: str, role_id: str) -> None:
        self._project(project_id)
        role = self.store.role(role_id)
        if role is None or role["project_id"] != project_id:
            raise ProducerError("找不到这个角色。")
        self.store.delete_role(role_id)

    # ---- 任务 -------------------------------------------------------------

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.job(job_id)
        return bool(job and job["cancel_requested"])

    def _spawn(
        self, project_id: str, kind: str, total: int, work: Callable[[str], str]
    ) -> dict[str, Any]:
        """起一个后台任务；同一项目同一类任务只允许一个在跑。

        串行是刻意的：识别与合成共用一张显卡时，两个任务同时压模型只会都变慢，
        而用户看到的进度会来回跳。
        """
        current = self.store.latest_job(project_id)
        if current and current["kind"] == kind and current["state"] == "running":
            raise ProducerError("这一类任务已经在跑，等它结束或先取消。")
        job = self.store.start_job(project_id, kind, total)
        thread = threading.Thread(
            target=self._run_job, args=(job["id"], kind, work), daemon=True
        )
        with _JOB_GUARD:
            _JOB_THREADS[job["id"]] = thread
        thread.start()
        return job

    def _run_job(self, job_id: str, kind: str, work: Callable[[str], str]) -> None:
        try:
            message = work(job_id)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落到任务上，不能让线程静默死掉
            self.store.update_job(job_id, state="failed", message=str(exc))
            CHANNEL.error(f"配音任务失败（{kind}）：{exc}", source="dubbing")
            return
        finally:
            with _JOB_GUARD:
                _JOB_THREADS.pop(job_id, None)
        if self._cancelled(job_id):
            self.store.update_job(job_id, state="cancelled", message="已取消")
            return
        self.store.update_job(job_id, state="done", message=message or "完成")

    # ---- 转录 -------------------------------------------------------------

    def start_transcribe(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        reason = asr_uc.unavailable_reason()
        if reason:
            raise ProducerError(reason)
        # 配置就绪不代表服务已经起来（本地识别服务常常是分开起的）。
        # 一次探测换掉一批注定失败的请求：半小时的音频会切出几百段，
        # 每段各自超时一轮，用户等到的是一个半小时的错误。
        ok, note = asr_uc.reachable()
        if not ok:
            raise ProducerError(f"识别服务连不上：{note}")
        if not self.original(project).is_file():
            self.ensure_audio(project_id)
        return self._spawn(project_id, "transcribe", 0, self._transcribe)

    def _transcribe(self, job_id: str) -> str:
        job = self.store.job(job_id)
        assert job is not None
        project = self._project(job["project_id"])
        project_id = project["id"]
        source = self.original(project)
        duration = probe_duration(source) or project["duration_ms"] / 1000.0
        settings = self.settings

        spans = ffmpeg_tools.silence_spans(
            source, noise_db=settings.silence_db, min_silence=settings.silence_min
        )
        segments = _speech_segments(spans, duration, max_segment=settings.max_segment)
        if not segments:
            raise ProducerError("没有切出可识别的语音段：检查静音阈值是否高于人声。")
        self.store.update_job(
            job_id,
            total=len(segments),
            message=f"切出 {len(segments)} 段，开始识别",
        )

        # 重新识别时保住角色归属：新段与旧段按重叠时长取最近的一条，
        # 改一句话不该让整张角色表白填。
        old = self.store.cues(project_id)
        work = self._work(project_id, "segments")
        items: list[dict[str, Any]] = []
        failed = 0
        first_error = ""
        for index, (start, end) in enumerate(segments):
            if self._cancelled(job_id):
                return "已取消"
            clip = work / f"{index:04d}.wav"
            if not clip.is_file() or clip.stat().st_size <= 44:
                ffmpeg_tools.slice_audio(source, clip, start=start, end=end)
            try:
                result = asr_uc.transcribe_file(clip)
                text = str(result.get("text") or "").strip()
            except Exception as exc:  # 单段失败不拖垮整条
                failed += 1
                first_error = first_error or str(exc)
                text = ""
                # 前几段全失败通常是服务没起 / 地址错：继续跑只会把同一个错误
                # 重复几百遍，不如当场停下来说明原因。
                if failed >= 3 and not items:
                    raise ProducerError(f"连续 {failed} 段识别失败：{first_error}") from exc
            if text:
                items.append(
                    {
                        "start_ms": _now_ms(start),
                        "end_ms": _now_ms(end),
                        "text": text,
                        "role_id": self._nearest_role(old, start, end),
                    }
                )
            self.store.update_job(
                job_id,
                done=index + 1,
                failed=failed,
                message=f"识别 {index + 1} / {len(segments)}",
            )
        if not items:
            raise ProducerError(
                f"没有识别出任何文字。{('第一次失败：' + first_error) if first_error else ''}"
            )
        # 时间码重叠到同一毫秒在长音频里会出现：排一次序并保证后一段起点不早于前一段。
        items.sort(key=lambda item: item["start_ms"])
        for position in range(1, len(items)):
            items[position]["start_ms"] = max(
                items[position]["start_ms"], items[position - 1]["end_ms"]
            )
        self.store.replace_cues(project_id, items)
        self.store.update_project(project_id, status="transcribed")
        suffix = f"，{failed} 段识别失败" if failed else ""
        CHANNEL.info(
            f"转录完成：{len(items)} 段{suffix}",
            source="dubbing",
            project=project_id,
            cue_count=len(items),
        )
        return f"识别出 {len(items)} 段{suffix}"

    @staticmethod
    def _nearest_role(old: list[dict[str, Any]], start: float, end: float) -> str:
        best, best_overlap = "", 0.0
        for cue in old:
            left = max(start, cue["start_ms"] / 1000.0)
            right = min(end, cue["end_ms"] / 1000.0)
            overlap = right - left
            if overlap > best_overlap:
                best, best_overlap = str(cue["role_id"] or ""), overlap
        return best

    # ---- 合成 -------------------------------------------------------------

    def start_synth(
        self, project_id: str, library: LibraryService, cue_ids: Sequence[str] | None = None
    ) -> dict[str, Any]:
        self._project(project_id)
        targets = self._synth_targets(project_id, cue_ids)
        if not targets:
            raise ProducerError("没有需要合成的段落：先转录，或检查角色是否选了音色。")
        return self._spawn(
            project_id, "synth", len(targets), lambda job_id: self._synth(job_id, library, targets)
        )

    def _synth_targets(
        self, project_id: str, cue_ids: Sequence[str] | None
    ) -> list[dict[str, Any]]:
        """要合成的段落：有音色的角色名下、且文本非空的所有段；指定了就只做这些。"""
        roles = {role["id"]: role for role in self.store.roles(project_id)}
        wanted = set(cue_ids or [])
        out: list[dict[str, Any]] = []
        for cue in self.store.cues(project_id):
            if wanted and cue["id"] not in wanted:
                continue
            if not str(cue["text"] or "").strip():
                continue
            role = roles.get(cue["role_id"] or "")
            if role is None:
                # 没有角色的段归旁白：建项目时就会有一条，这里只防老数据。
                role = next((item for item in roles.values() if item["mode"] == "tts"), None)
            if role is None or role["mode"] != "tts" or not (role["engine"] and role["voice_id"]):
                continue
            out.append({**cue, "_role": role})
        return out

    def _audio_key(self, cue: dict[str, Any], role: dict[str, Any]) -> str:
        from app.usecase import pronunciation

        payload = json.dumps(
            {
                "text": cue["text"],
                "engine": role["engine"],
                "voice": role["voice_id"],
                "params": role["params"],
                "pron": pronunciation.revision(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _synth(self, job_id: str, library: LibraryService, targets: list[dict[str, Any]]) -> str:
        job = self.store.job(job_id)
        assert job is not None
        project_id = job["project_id"]
        self._project(project_id)
        work = self._work(project_id, "tts")
        concurrency = max(1, int(self.settings.concurrency))

        async def run() -> tuple[int, int, str]:
            semaphore = asyncio.Semaphore(concurrency)
            done = 0
            failed = 0
            first_error = ""
            lock = asyncio.Lock()

            async def one(cue: dict[str, Any], position: int) -> None:
                nonlocal done, failed, first_error
                if self._cancelled(job_id):
                    return
                role = cue["_role"]
                key = self._audio_key(cue, role)
                if cue["audio"] and cue["audio_key"] == key:
                    existing = self.folder(project_id) / cue["audio"]
                    if existing.is_file():
                        async with lock:
                            done += 1
                            self.store.update_job(job_id, done=done, failed=failed)
                        return
                async with semaphore:
                    try:
                        from app.usecase import gateway

                        result = await gateway.synthesize(
                            library,
                            engine_key=role["engine"],
                            voice_id=role["voice_id"],
                            text=cue["text"],
                            extra=role["params"] or None,
                            log_failure=False,
                        )
                        suffix = (result.format or "wav").lower()
                        raw = work / f"{cue['id']}.{suffix}"
                        raw.write_bytes(result.audio)
                        # 中间件统一 44.1k 单声道 PCM：各家引擎的采样率不同，
                        # 不统一就没法按时间轴拼。
                        wav = self._to_middle(raw, cue["id"])
                        rel = wav.relative_to(self.folder(project_id)).as_posix()
                        self.store.update_cue(
                            cue["id"], audio=rel, audio_key=key, state="ready", error=""
                        )
                        async with lock:
                            done += 1
                            self.store.update_job(job_id, done=done, failed=failed)
                    except Exception as exc:  # noqa: BLE001
                        async with lock:
                            failed += 1
                            if not first_error:
                                first_error = str(exc)
                            self.store.update_cue(
                                cue["id"], state="failed", error=str(exc)
                            )
                            self.store.update_job(job_id, done=done, failed=failed)

            tasks = [asyncio.create_task(one(cue, index)) for index, cue in enumerate(targets)]
            for index, task in enumerate(asyncio.as_completed(tasks), start=1):
                await task
                if self._cancelled(job_id):
                    for pending in tasks:
                        pending.cancel()
                    break
                self.store.update_job(
                    job_id, done=done, failed=failed, message=f"合成 {index} / {len(targets)}"
                )
            return done, failed, first_error

        done, failed, first_error = asyncio.run(run())
        self.store.update_project(project_id, status="synthesized")
        suffix = f"，{failed} 段失败" if failed else ""
        if failed and done == 0:
            raise ProducerError(first_error or "全部段落合成失败。")
        CHANNEL.info(
            f"配音完成：{done} 段{suffix}",
            source="dubbing",
            project=project_id,
            cue_count=done,
        )
        return f"合成 {done} 段{suffix}"

    def _to_middle(self, raw: Path, cue_id: str) -> Path:
        """引擎产物 → 中间件 wav（44.1k 单声道 PCM16）。

        ``raw`` 与目标同名时先挪成 ``.src.wav`` 再转：ffmpeg 不允许输入输出是
        同一个文件（"same as Input #0"），而引擎给 wav 时这两条路正好同名。
        """
        target = raw.with_name(f"{cue_id}.wav")
        codec, rate, channels = (
            self._stream_of(raw) if raw.suffix.lower() == ".wav" else ("", 0, 0)
        )
        if codec == "pcm_s16le" and rate == 44100 and channels == 1:
            if raw != target:
                raw.replace(target)
            return target
        if raw == target:
            staging = raw.with_name(f"{cue_id}.src{raw.suffix}")
            raw.replace(staging)
            try:
                ffmpeg_tools.extract_audio(staging, target, channels=1, rate=44100)
            finally:
                staging.unlink(missing_ok=True)
            return target
        ffmpeg_tools.extract_audio(raw, target, channels=1, rate=44100)
        raw.unlink(missing_ok=True)
        return target

    @staticmethod
    def _stream_of(path: Path) -> tuple[str, int, int]:
        from app.infra.audioprobe import probe_stream

        return probe_stream(path)

    # ---- 拼装 / 混音 / 导出 ----------------------------------------------

    def assemble(self, project_id: str) -> dict[str, Any]:
        """贴齐 + 混音 + 写字幕。返回产物路径与统计（顺延 / 变速的段号）。"""
        project = self._project(project_id)
        cues = [cue for cue in self.store.cues(project_id) if cue["audio"]]
        if not cues:
            raise ProducerError("还没有可拼装的音频：先把段落合成出来。")
        missing = [cue for cue in self.store.cues(project_id) if not cue["audio"]]
        folder = self.folder(project_id)
        work = self._work(project_id, "assemble")
        out = self._out(project_id)
        settings = self.settings

        slots = []
        for index, cue in enumerate(cues, start=1):
            path = folder / cue["audio"]
            slots.append(
                assembly.Slot(
                    key=cue["id"],
                    # 序号用段落自己的，顺延与变速的段号才对得上界面上的那一行。
                    ordinal=int(cue["ordinal"]) or index,
                    start_ms=int(cue["start_ms"]),
                    end_ms=int(cue["end_ms"]),
                    audio=path,
                    text=str(cue["text"] or ""),
                )
            )
        options = assembly.AssemblyOptions(
            fit=settings.fit, max_speed=settings.max_speed
        )
        try:
            plan = assembly.plan_timeline(slots, work_dir=work, options=options)
            vocal = assembly.concat_audio(
                plan.inputs, out / "vocal.wav", work_dir=work, options=options
            )
        except assembly.AssemblyError as exc:
            raise ProducerError(str(exc)) from exc

        level = float(project["options"].get("original_gain", -60.0))
        tracks = [(vocal, 0.0)]
        original = self.original(project)
        if level > -60 and original.is_file():
            tracks.append((original, level))
        try:
            assembly.mix_tracks(
                tracks,
                out / "mix.wav",
                loudnorm=LOUDNESS_TARGETS.get(settings.loudness, ""),
            )
        except assembly.AssemblyError as exc:
            raise ProducerError(str(exc)) from exc

        formats = [part.strip().lower() for part in settings.subtitle_formats.split(",") if part.strip()]
        written = subtitle_uc.write_subtitles(
            plan.cues,
            out,
            "subtitles",
            formats=formats or ["srt"],
            encoding=normalize_encoding(settings.subtitle_encoding),
            max_chars=int(settings.subtitle_max_chars),
        )
        self.store.update_project(project_id, status="assembled")
        CHANNEL.info(
            f"贴齐完成：{len(cues)} 段 / {plan.duration:.1f}s"
            f" / 顺延 {len(plan.delayed)} / 变速 {len(plan.fitted)}",
            source="dubbing",
            project=project_id,
        )
        return {
            "object": "dubbing_assembly",
            "duration": round(plan.duration, 3),
            "cues": len(plan.cues),
            "delayed": plan.delayed,
            "fitted": plan.fitted,
            "skipped": len(missing),
            "files": {
                "vocal": "out/vocal.wav",
                "mix": "out/mix.wav",
                **{f"subtitle_{key}": f"out/{path.name}" for key, path in written.items()},
            },
        }

    def export(self, project_id: str) -> dict[str, Any]:
        """导出成品：视频项目封装 MP4（画面拷贝），音频项目转成品容器。"""
        project = self._project(project_id)
        out = self._out(project_id)
        mix = out / "mix.wav"
        if not mix.is_file():
            self.assemble(project_id)
        assert mix.is_file()
        stem = safe_segment(project["name"], "配音")
        settings = self.settings
        # 烧进容器的字幕轨从已写出的那几种里挑：设置的格式可能不含 SRT，
        # 而 MP4 的字幕轨只认 mov_text（由 mux_media 转），源文件用哪一种都行。
        subtitle = None
        if settings.burn_subtitles:
            for suffix in (".srt", ".vtt", ".ass"):
                candidate = out / f"subtitles{suffix}"
                if candidate.is_file():
                    subtitle = candidate
                    break
        try:
            if project["kind"] == "video":
                target = out / f"{stem}.mp4"
                ffmpeg_tools.mux_media(
                    self.source_file(project),
                    mix,
                    target,
                    subtitles=subtitle,
                )
            else:
                suffix = settings.audio_format if settings.audio_format in {"mp3", "m4a", "wav"} else "m4a"
                target = out / f"{stem}.{suffix}"
                ffmpeg_tools.encode_audio(mix, target, bitrate=settings.bitrate)
        except ffmpeg_tools.FfmpegError as exc:
            raise ProducerError(str(exc)) from exc
        self.store.update_project(project_id, status="exported")
        CHANNEL.info(
            f"导出完成：{target.name}（{target.stat().st_size / 1024 / 1024:.1f} MB）",
            source="dubbing",
            project=project_id,
        )
        return {
            "object": "dubbing_export",
            "file": target.name,
            "path": f"out/{target.name}",
            "size": target.stat().st_size,
        }

    # ---- 媒体 -------------------------------------------------------------

    def media_path(self, project_id: str, kind: str, name: str = "") -> Path:
        """取一个可下载/可播放的产物路径；越界与不存在都在这里挡掉。"""
        project = self._project(project_id)
        folder = self.folder(project_id).resolve()
        if kind == "source":
            path = self.source_file(project)
        elif kind == "original":
            path = self.original(project)
        elif kind == "vocal":
            path = folder / "out" / "vocal.wav"
        elif kind == "mix":
            path = folder / "out" / "mix.wav"
        elif kind == "subtitle":
            path = folder / "out" / (name or "subtitles.srt")
        elif kind == "export":
            path = folder / "out" / name
        elif kind == "cue":
            cue = self.store.cue(name)
            if cue is None or cue["project_id"] != project_id or not cue["audio"]:
                raise ProducerError("这一段还没有音频。")
            path = folder / cue["audio"]
        else:
            raise ProducerError(f"不认识这种产物：{kind}。")
        resolved = path.resolve()
        if not resolved.is_relative_to(folder) or not resolved.is_file():
            raise ProducerError("产物不存在或已被清理。")
        return resolved

    def wait_job(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """给测试与脚本用：等一个任务落定。界面走轮询，不等。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.job(job_id)
            if job and job["state"] != "running":
                return job
            time.sleep(0.05)
        job = self.store.job(job_id)
        raise ProducerError(f"任务 {job_id} 在 {timeout:.0f} 秒内没有结束：{(job or {}).get('message', '')}")
