"""配音服务分轨与转录阶段（由 DubbingService 组合）。"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra import ffmpeg as ffmpeg_tools
from app.infra.audioprobe import probe_duration
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import tasks as audiocpp_tasks
from app.producers.builtin.dubbing.helpers import (
    _STEM_CHUNK_S,
    _STEM_OVERLAP_S,
    _STEM_TOLERANCE_S,
    _now_ms,
    _publish_media,
    _running_speaker_models,
    _speaker_labels_from_turns,
    _speech_segments,
    _stamp,
    _tc_text,
)
from app.usecase import asr as asr_uc
from app.usecase import assembly


class _TranscribeMixin:
    def start_separate(self, project_id: str, model_id: str = "") -> dict[str, Any]:
        """把原音轨分成人声与伴奏；后台任务，按十分钟一块跑。"""
        project = self._project(project_id)
        self._ensure_idle(project_id)
        if not self.original(project).is_file():
            self.ensure_audio(project_id)
            project = self._project(project_id)
        original = self.original(project)
        duration = probe_duration(original) or project["duration_ms"] / 1000.0
        if duration <= 0:
            raise ProducerError("原音轨读不出时长，无法分轨。")
        running = self._running_stem_models(fresh=True)
        if not running:
            raise ProducerError(
                "没有正在运行的分离模型：到 audio.cpp 页安装并加载一个分轨模型后再试。"
            )
        chosen = next((row for row in running if row["id"] == model_id), None)
        if chosen is None:
            chosen = running[0]
        if not chosen.get("url"):
            raise ProducerError("选中的分轨模型找不到服务地址，重新进一次页面再试。")
        chunks = max(1, math.ceil(duration / _STEM_CHUNK_S))

        def work(job_id: str) -> str:
            lock = self._lock_project(project_id)
            try:
                seconds = self._separate(
                    project_id,
                    chosen,
                    job_id=job_id,
                    chunks=chunks,
                    stage=lambda done, message: self._stage(job_id, done, message),
                    check=self._cancel_check(job_id),
                )
            finally:
                lock.release()
            return (
                f"分轨完成：人声 + 伴奏 · {chosen['label']}"
                f" · {seconds:.1f}s · {chunks} 块"
            )

        return self._spawn(project_id, "separate", chunks, work)


    def _separate(
        self,
        project_id: str,
        model: dict[str, Any],
        *,
        job_id: str,
        chunks: int,
        stage: Callable[[int, str], None] | None = None,
        check: Callable[[], None] | None = None,
    ) -> float:
        """逐块分离再拼回整条；返回人声时长。

        模型按服务端本机路径读音频、把结果编成 base64 放回响应：整片一次调用会把
        两小时的音频同时留在响应与解码后的内存里，必须分块。相邻块重叠一点，
        拼之前把重叠切掉，接缝处才有上下文。
        """
        project = self._project(project_id)
        original = self.original(project)
        duration = probe_duration(original) or project["duration_ms"] / 1000.0
        work = self._work(project_id, "stems")
        # 上一轮的块文件按当前块数重写，旧名字不会再被引用。
        for stale in work.iterdir():
            if stale.is_file():
                stale.unlink(missing_ok=True)

        vocals_parts: list[Path] = []
        accompaniment_parts: list[Path] = []
        for index in range(chunks):
            if check is not None:
                check()
            start = index * _STEM_CHUNK_S
            end = min(duration, start + _STEM_CHUNK_S)
            lead = _STEM_OVERLAP_S if index else 0.0
            left = max(0.0, start - lead)
            if stage is not None:
                stage(
                    index,
                    f"分轨 {index + 1}/{chunks}"
                    f" · {_tc_text(int(start * 1000))}–{_tc_text(int(end * 1000))}"
                    f" · {model['label']}",
                )
            clip = work / f"in-{index:03d}.wav"
            ffmpeg_tools.slice_audio(
                original, clip, start=left, end=end, rate=44100, channels=2
            )
            try:
                outputs = audiocpp_tasks.run_task(
                    str(model["url"]),
                    model=str(model["id"]),
                    audio=clip,
                    options={},
                    timeout=max(600.0, (end - left) * 8.0),
                )
            except ProducerError as exc:
                raise ProducerError(f"分轨失败（{model['label']}）：{exc}") from exc
            vocal_key = audiocpp_tasks.vocal_name(outputs)
            if not vocal_key:
                raise ProducerError("分轨模型没有返回人声。")
            vocal_bytes = outputs.pop(vocal_key)
            if not outputs:
                raise ProducerError(
                    f"「{model['label']}」只返回了人声，没有伴奏；"
                    "换一个同时输出两轨的分离模型。"
                )

            # 人声：写进磁盘再统一成 44.1k 立体声 PCM，拼之前才有一致的样本格式。
            raw_vocal = work / f"raw-v-{index:03d}.wav"
            raw_vocal.write_bytes(vocal_bytes)
            vocal = work / f"v-{index:03d}.wav"
            ffmpeg_tools.normalize(
                raw_vocal, vocal, rate=44100, channels=2, peak=0
            )
            self._check_stem_span(vocal, end - left, model)
            raw_vocal.unlink(missing_ok=True)
            if lead:
                trimmed = work / f"tv-{index:03d}.wav"
                ffmpeg_tools.slice_audio(
                    vocal, trimmed, start=lead, end=None, rate=44100, channels=2
                )
                vocals_parts.append(trimmed)
                vocal.unlink(missing_ok=True)
            else:
                vocals_parts.append(vocal)

            # 伴奏：htdemucs 会给出鼓 / 贝斯 / 其他多路，相加成一条伴奏。
            accompaniment_paths: list[Path] = []
            for stem_index, data in enumerate(outputs.values()):
                raw = work / f"raw-a-{index:03d}-{stem_index:02d}.wav"
                raw.write_bytes(data)
                normalized = work / f"a-{index:03d}-{stem_index:02d}.wav"
                ffmpeg_tools.normalize(
                    raw, normalized, rate=44100, channels=2, peak=0
                )
                self._check_stem_span(normalized, end - left, model)
                raw.unlink(missing_ok=True)
                accompaniment_paths.append(normalized)
            if len(accompaniment_paths) == 1:
                accompaniment = accompaniment_paths[0]
            else:
                accompaniment = work / f"a-{index:03d}.wav"
                try:
                    assembly.mix_tracks(
                        [(path, 0.0) for path in accompaniment_paths],
                        accompaniment,
                        loudnorm="",
                        check=check,
                    )
                except assembly.AssemblyError as exc:
                    raise ProducerError(str(exc)) from exc
                for path in accompaniment_paths:
                    path.unlink(missing_ok=True)
            if lead:
                trimmed = work / f"ta-{index:03d}.wav"
                ffmpeg_tools.slice_audio(
                    accompaniment,
                    trimmed,
                    start=lead,
                    end=None,
                    rate=44100,
                    channels=2,
                )
                accompaniment_parts.append(trimmed)
                accompaniment.unlink(missing_ok=True)
            else:
                accompaniment_parts.append(accompaniment)

            clip.unlink(missing_ok=True)
            del outputs, vocal_bytes

        if check is not None:
            check()
        vocals_target = work / "vocals-full.wav"
        accompaniment_target = work / "accompaniment-full.wav"
        options = assembly.AssemblyOptions(channels=2, rate=44100)
        try:
            assembly.concat_audio(
                vocals_parts,
                vocals_target,
                work_dir=work,
                options=options,
                check=check,
            )
            assembly.concat_audio(
                accompaniment_parts,
                accompaniment_target,
                work_dir=work,
                options=options,
                check=check,
            )
        except assembly.AssemblyError as exc:
            raise ProducerError(str(exc)) from exc
        vocals_seconds = probe_duration(vocals_target) or 0.0
        accompaniment_seconds = probe_duration(accompaniment_target) or 0.0
        if (
            abs(vocals_seconds - duration) > _STEM_TOLERANCE_S
            or abs(accompaniment_seconds - duration) > _STEM_TOLERANCE_S
        ):
            raise ProducerError(
                f"分轨结果时长对不上：人声 {vocals_seconds:.2f}s / "
                f"伴奏 {accompaniment_seconds:.2f}s，原片 {duration:.2f}s。"
            )
        audio_dir = self.folder(project_id) / "audio"
        try:
            _publish_media(vocals_target, audio_dir / "vocals.wav")
            _publish_media(accompaniment_target, audio_dir / "accompaniment.wav")
        except OSError as exc:
            raise ProducerError(
                "分轨结果写不进项目：可能有播放器正占用人声或伴奏文件。"
            ) from exc
        for path in work.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)

        fresh = self._project(project_id)
        options = dict(fresh["options"])
        options["stems"] = {
            "key": _stamp(original),
            "model": str(model["id"]),
            "label": str(model["label"]),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        options.setdefault("dub_gain", 0.0)
        options.setdefault("accompaniment_gain", 0.0)
        self.store.update_project(project_id, options=options)
        # 分轨换了一版：旧混音不能继续代表当前声音。
        self.store.touch(project_id)
        if stage is not None:
            stage(chunks, "分轨完成")
        CHANNEL.info(
            f"分轨完成：{model['label']} · 人声 {vocals_seconds:.1f}s"
            f" · 伴奏 {accompaniment_seconds:.1f}s · {chunks} 块",
            source="dubbing",
            project=project_id,
            job=job_id,
            model=model["id"],
            chunks=chunks,
            duration=round(vocals_seconds, 1),
        )
        return vocals_seconds


    @staticmethod
    def _check_stem_span(path: Path, expected: float, model: dict[str, Any]) -> None:
        got = probe_duration(path) or 0.0
        if got <= 0 or abs(got - expected) > _STEM_TOLERANCE_S:
            raise ProducerError(
                f"「{model['label']}」返回的音频时长对不上"
                f"（{got:.2f}s vs {expected:.2f}s），分轨已停止。"
            )


    def remove_stems(self, project_id: str) -> dict[str, Any]:
        """删除分轨产物：回到整条原声的混音方式，并释放两份 WAV 的空间。"""
        project = self._project(project_id)
        self._ensure_idle(project_id)
        lock = self._lock_project(project_id)
        try:
            folder = self.folder(project_id) / "audio"
            for name in ("vocals.wav", "accompaniment.wav"):
                (folder / name).unlink(missing_ok=True)
            options = dict(self._project(project_id)["options"])
            options.pop("stems", None)
            self.store.update_project(project_id, options=options)
            self.store.touch(project_id)
        finally:
            lock.release()
        CHANNEL.info(
            f"已移除分轨：{project['name']}",
            source="dubbing",
            project=project_id,
        )
        return self.detail(project_id)


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
        stems = self._stem_paths(project)
        # 分轨后切段与识别都走人声：音乐会把停顿填满，静音检测只剩硬切。
        source = stems[0] if stems else self.original(project)
        duration = probe_duration(source) or project["duration_ms"] / 1000.0
        settings = self.settings
        started = time.monotonic()
        CHANNEL.info(
            f"开始转录：{'人声轨' if stems else '原音轨'} {duration:.1f}s"
            f" · 静音阈值 {settings.silence_db} dB"
            f" · 最短停顿 {settings.silence_min}s · 最长段 {settings.max_segment}s",
            source="dubbing",
            project=project_id,
            job=job_id,
            silence_db=settings.silence_db,
            silence_min=settings.silence_min,
            max_segment=settings.max_segment,
            duration=round(duration, 1),
        )

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
        # 改一句话不该让整张角色表白填。认不出来的归旁白（第一条角色），
        # 明确写进去而不是留空——留空就没人说得清它到底该不该配音。
        old = self.store.cues(project_id)
        narrator = next((role["id"] for role in self.store.roles(project_id)), "")
        work = self._work(project_id, "segments")
        # 片段缓存按「源指纹 + 序号 + 边界」命名：改了静音阈值后同一序号不再是
        # 同一段音频，沿用旧文件名会把上一轮另一段时间的识别结果贴进来。
        stamp = _stamp(source)
        keep: set[str] = set()
        items: list[dict[str, Any]] = []
        failed = 0
        first_error = ""
        for index, (start, end) in enumerate(segments):
            if self._cancelled(job_id):
                return "已取消"
            digest = hashlib.sha1(
                f"{stamp}:{start:.3f}:{end:.3f}".encode()
            ).hexdigest()[:10]
            clip = work / f"{index:04d}-{digest}.wav"
            keep.add(clip.name)
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
                        "role_id": self._nearest_role(old, start, end) or narrator,
                        "_seg": index,
                    }
                )
            self.store.update_job(
                job_id,
                done=index + 1,
                failed=failed,
                message=f"识别 {index + 1} / {len(segments)}",
            )
        speaker_total = self._mark_speakers(job_id, project_id, items, segments, source)
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
        for cue in old:
            self._remove_cue_audio(project_id, cue)
        self.store.replace_cues(project_id, items)
        self.store.update_project(project_id, status="transcribed")
        # 转录完就把字幕落进成品区：等贴齐才写的话，用户得先混一遍音才能下载
        # 自己刚识别出来的字幕。
        self.sync_subtitles(project_id, announce=True)
        # 这一轮没用的片段删掉：缓存只为重试服务，不为上一套切分留档。
        for stale in work.glob("*.wav"):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)
        suffix = f"，{failed} 段识别失败" if failed else ""
        speakers = f" · {speaker_total} 个说话人" if speaker_total else ""
        elapsed = time.monotonic() - started
        CHANNEL.info(
            f"转录完成：{len(items)} 段{suffix}{speakers} · {elapsed:.1f}s"
            f" · {sum(len(item['text']) for item in items)} 字",
            source="dubbing",
            project=project_id,
            job=job_id,
            cue_count=len(items),
            failed=failed,
            speakers=speaker_total,
            elapsed=round(elapsed, 1),
        )
        return f"识别出 {len(items)} 段{suffix}"


    def _mark_speakers(
        self,
        job_id: str,
        project_id: str,
        items: list[dict[str, Any]],
        segments: Sequence[tuple[float, float]],
        source: Path,
    ) -> int:
        """按设置把说话人分离折到段落的角色上：每个说话人建一个
        「说话人 N」角色（已有同名角色就复用，保留用户的改名与音色），
        段落的 ``role_id`` 直接落到该角色。没有运行中的 diar 模型或分离
        失败只记日志，不拖垮转录；没被轮次覆盖的段保持按时间继承的归属。

        ``items`` 里带 ``_seg``（切段序号）。返回说话人数（未开启或没标出来
        时为 0）。
        """
        if not items:
            return 0
        if self._cancelled(job_id):
            return 0
        if not self.settings.speaker_diar:
            return 0
        models = _running_speaker_models()
        if not models:
            CHANNEL.warning(
                "说话人标注跳过：没有运行中的说话人分离模型（audio.cpp，diar 任务）。",
                source="dubbing",
                project=project_id,
                job=job_id,
            )
            return 0
        try:
            from app.producers.builtin.audiocpp import tasks as audiocpp_tasks

            # 分离模型只吃 16 kHz 单声道：项目音轨的采样率跟随源，先转一份。
            converted = self._work(project_id, "speakers") / "diar-16k.wav"
            ffmpeg_tools.slice_audio(source, converted, start=0.0)
            row = models[0]
            turns = audiocpp_tasks.run_diar(row["url"], model=row["id"], audio=converted)
        except Exception as exc:  # noqa: BLE001 - 标注失败不该拖垮整条转录
            CHANNEL.warning(
                f"说话人标注失败：{exc}",
                source="dubbing",
                project=project_id,
                job=job_id,
            )
            return 0
        labels = _speaker_labels_from_turns(segments, turns)
        if not labels:
            return 0
        # 每个簇一个角色：同名角色复用，重新转录时用户的改名与音色不丢。
        by_name = {role["name"]: role["id"] for role in self.store.roles(project_id)}
        role_of_cluster: dict[int, str] = {}
        for cluster in sorted(set(labels.values())):
            name = f"说话人 {cluster + 1}"
            role_id = by_name.get(name)
            if role_id is None:
                role_id = self.store.add_role(project_id, name=name)["id"]
                CHANNEL.info(
                    f"说话人分离新建角色：{name}",
                    source="dubbing",
                    project=project_id,
                    job=job_id,
                )
            role_of_cluster[cluster] = role_id
        for item in items:
            cluster = labels.get(int(item.pop("_seg", -1)))
            if cluster in role_of_cluster:
                item["role_id"] = role_of_cluster[cluster]
        return len(role_of_cluster)


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

