"""配音服务贴齐、原声静音、混音与导出阶段（由 DubbingService 组合）。"""

from __future__ import annotations

import math
import shutil
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra import ffmpeg as ffmpeg_tools
from app.infra.archive import safe_segment
from app.infra.audioprobe import probe_duration
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.helpers import (
    _assembly_message,
    _export_message,
    _file_time,
    is_dubbed,
)
from app.producers.builtin.dubbing.settings import LOUDNESS_TARGETS
from app.usecase import assembly


class _MixMixin:
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


    def _fit_slot(
        self, cue: dict[str, Any], audio: Path, slot_s: float, work: Path
    ) -> tuple[Path, bool]:
        """把产物限制在这一句的时隙内：能变速就交给拼装，放不下就按槽位截断。

        配音不能像有声书那样把下一句往后推：成片的画面是固定的，原声也还在源
        时间轴上。产物越界只会盖住下一句；这里宁可截掉超出的一点，也不让两句
        同时出声。返回 ``(路径, 是否截断)``。
        """
        if slot_s <= 0:
            return audio, False
        duration = probe_duration(audio)
        if duration <= slot_s + 0.01:
            return audio, False
        settings = self.settings
        max_speed = max(1.0, min(2.0, float(settings.max_speed)))
        if settings.fit and duration / slot_s <= max_speed:
            # plan_timeline 会按 fit_to_slot 的时隙变速，不必再切一版。
            return audio, False
        target = work / f"trim-{cue['id']}-{round(slot_s * 1000):05d}.wav"
        try:
            fresh = (
                not target.is_file()
                or target.stat().st_size <= 44
                or target.stat().st_mtime < audio.stat().st_mtime
            )
            if fresh:
                ffmpeg_tools.normalize(
                    audio,
                    target,
                    rate=44100,
                    channels=1,
                    peak=0,
                    duration=slot_s,
                )
        except ffmpeg_tools.FfmpegError as exc:
            raise ProducerError(str(exc)) from exc
        return target, True


    def _assemble_service(
        self,
        project_id: str,
        *,
        subtitle_stem: str = "",
        job_id: str = "",
        stage: Callable[[int, str], None] | None = None,
        check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """贴齐 + 混音 + 写字幕。返回产物路径与统计（顺延 / 变速的段号）。

        成片的时间轴就是源音频的时间轴：**只有配过音的段落被替换**，其余位置
        照原样保留原声。所以这里不是"人声叠在原声上"，而是先把原声在被替换的
        区间里静音，再与配音轨合成——同一时刻只有一个声音。

        每次调用都重写 ``vocal.wav`` / ``mix.wav`` / 字幕：导出前必须走这一遍，
        否则改过的段落还在听上一版混音。
        """
        self._ensure_idle(project_id, allow_job_id=job_id)
        if check is not None:
            check()
        if stage is not None:
            stage(1, "贴齐时间轴")
        project = self._project(project_id)
        roles = self.store.roles(project_id)
        cues = self.store.cues(project_id)
        folder = self.folder(project_id)
        # 空文本的段不该再拿旧产物去配：合成清单本来就会跳过它们。
        dubbed = [
            cue
            for cue in cues
            if is_dubbed(cue, roles) and str(cue["text"] or "").strip()
        ]
        ready = [
            cue
            for cue in dubbed
            if cue["audio"]
            and int(cue["end_ms"]) > int(cue["start_ms"])
            and (folder / cue["audio"]).is_file()
        ]
        if not ready:
            raise ProducerError(
                "还没有可拼装的音频：先把需要配音的段落合成出来。"
            )
        ready_ids = {cue["id"] for cue in ready}
        missing = [cue for cue in dubbed if cue["id"] not in ready_ids]
        # 保留原声的段落：成片里它们的声音来自源文件，字幕照旧要写。
        kept = [
            cue
            for cue in cues
            if not is_dubbed(cue, roles) and str(cue["text"] or "").strip()
        ]
        work = self._work(project_id, "assemble")
        out = self._out(project_id)
        settings = self.settings
        started = time.monotonic()

        slots = []
        truncated: list[int] = []
        for index, cue in enumerate(ready, start=1):
            if check is not None:
                check()
            path = folder / cue["audio"]
            slot_s = max(0.0, (int(cue["end_ms"]) - int(cue["start_ms"])) / 1000.0)
            path, cut = self._fit_slot(cue, path, slot_s, work)
            if cut:
                truncated.append(int(cue["ordinal"]) or index)
            slots.append(
                assembly.Slot(
                    key=cue["id"],
                    # 序号用段落自己的，变速的段号才对得上界面上的那一行。
                    ordinal=int(cue["ordinal"]) or index,
                    start_ms=int(cue["start_ms"]),
                    end_ms=int(cue["end_ms"]),
                    audio=path,
                    text=str(cue["text"] or ""),
                )
            )
        options = assembly.AssemblyOptions(
            fit=settings.fit, max_speed=settings.max_speed, fit_to_slot=True
        )
        try:
            plan = assembly.plan_timeline(
                slots, work_dir=work, options=options, check=check
            )
            vocal = assembly.concat_audio(
                plan.inputs,
                out / "vocal.wav",
                work_dir=work,
                options=options,
                check=check,
            )
        except assembly.AssemblyError as exc:
            raise ProducerError(str(exc)) from exc
        if plan.delayed:
            CHANNEL.warning(
                f"仍有 {len(plan.delayed)} 段超出前面段落的收尾，时间轴会顺延："
                f"第 {'、'.join(str(item) for item in plan.delayed)} 段。",
                source="dubbing",
                project=project_id,
                delayed=plan.delayed,
            )

        if check is not None:
            check()
        if stage is not None:
            stage(2, "混音")
        options = project["options"]
        level = float(options.get("original_gain", 0.0))
        dub_gain = float(options.get("dub_gain", 0.0))
        accompaniment_gain = float(options.get("accompaniment_gain", 0.0))
        original = self.original(project)
        stems = self._stem_paths(project)
        # 原声要按**这一句在源音轨上的区间**静音，不是按 TTS 产物时长：
        # 产物通常比原句短，只静音产物会在后半个句子上冒出原声。
        mask = [
            (int(cue["start_ms"]) / 1000.0, int(cue["end_ms"]) / 1000.0)
            for cue in ready
        ]
        tracks = [(vocal, dub_gain)]
        if stems is not None:
            vocals_stem, accompaniment = stems
            # 分轨后只静音原声人声，伴奏整条保留：被配音的句子底下还有音乐。
            if accompaniment_gain > -60:
                tracks.append((accompaniment, accompaniment_gain))
            if level > -60 and kept and mask:
                try:
                    bed = self._silenced_original(
                        vocals_stem,
                        mask,
                        work,
                        out / "vocals-masked.wav",
                        check=check,
                    )
                except assembly.AssemblyError as exc:
                    raise ProducerError(str(exc)) from exc
                tracks.append((bed, level))
        elif level > -60 and original.is_file() and mask:
            # 没分轨：整条原声在配音段静音，音乐也一起让位。
            try:
                bed = self._silenced_original(
                    original, mask, work, out / "bed.wav", check=check
                )
            except assembly.AssemblyError as exc:
                raise ProducerError(str(exc)) from exc
            tracks.append((bed, level))
        try:
            assembly.mix_tracks(
                tracks,
                out / "mix.wav",
                loudnorm=LOUDNESS_TARGETS.get(settings.loudness, ""),
                check=check,
            )
        except assembly.AssemblyError as exc:
            raise ProducerError(str(exc)) from exc
        mix = out / "mix.wav"
        duration = probe_duration(mix) or plan.duration

        if check is not None:
            check()
        if stage is not None:
            stage(3, "写字幕")
        # 字幕是整片的：配过音的段按贴齐后的位置写，保留原声与待配的段按源时间码写。
        entries = list(plan.cues)
        entries += [
            (
                int(cue["start_ms"]) / 1000.0,
                int(cue["end_ms"]) / 1000.0,
                " ".join(str(cue["text"] or "").split()),
            )
            for cue in (*missing, *kept)
        ]
        entries.sort(key=lambda item: item[0])
        written = self._write_subtitles_for(
            project_id, entries, stem=subtitle_stem or None
        )
        formats = self._subtitle_formats()
        # 本轮用过的静音 / 变速 / 截断件留着只会越积越多：下一轮会按当前
        # 时隙重新生成，旧名字不再被引用。
        keep = {path.name for path in plan.inputs if path.parent == work}
        for stale in work.iterdir():
            if stale.is_file() and stale.name not in keep:
                stale.unlink(missing_ok=True)
        self.store.update_project(
            project_id,
            status="assembled",
            assemble_key=self._assemble_key(self._project(project_id)),
        )
        elapsed = time.monotonic() - started
        if truncated:
            CHANNEL.warning(
                f"有 {len(truncated)} 段配音超出原句时隙，已按槽位截断："
                f"第 {'、'.join(str(item) for item in truncated)} 段。"
                "缩短文本或提高「最大倍率」可减少截断。",
                source="dubbing",
                project=project_id,
                truncated=truncated,
            )
        CHANNEL.info(
            f"贴齐完成：替换 {len(ready)} 段 / 保留原声 {len(kept)} 段"
            f" / 待配 {len(missing)} 段 / {duration:.1f}s"
            f" / 变速 {len(plan.fitted)} / 截断 {len(truncated)}"
            f"{' / 分轨（伴奏保留）' if stems is not None else ''}"
            f" / {elapsed:.1f}s",
            source="dubbing",
            project=project_id,
            dubbed=len(ready),
            kept=len(kept),
            skipped=len(missing),
            delayed=len(plan.delayed),
            fitted=len(plan.fitted),
            truncated=truncated,
            duration=round(duration, 1),
            fit=settings.fit,
            max_speed=settings.max_speed,
            loudness=settings.loudness,
            stems=stems is not None,
            dub_gain=dub_gain,
            original_gain=level,
            accompaniment_gain=accompaniment_gain,
            subtitle_formats=formats,
            subtitle_encoding=settings.subtitle_encoding,
            subtitle_max_chars=settings.subtitle_max_chars,
            elapsed=round(elapsed, 1),
        )
        return {
            "object": "dubbing_assembly",
            "duration": round(duration, 3),
            "cues": len(entries),
            "dubbed": len(ready),
            "kept": len(kept),
            "delayed": plan.delayed,
            "fitted": plan.fitted,
            "truncated": truncated,
            "skipped": len(missing),
            "stemmed": stems is not None,
            "files": {
                "vocal": "out/vocal.wav",
                "mix": "out/mix.wav",
                **{f"subtitle_{key}": f"out/{path.name}" for key, path in written.items()},
            },
        }


    def assemble(self, project_id: str, *, subtitle_stem: str = "") -> dict[str, Any]:
        """同步贴齐；接口层走 ``start_assemble``，这里留给脚本与测试。"""
        lock = self._lock_project(project_id)
        try:
            return self._assemble_service(project_id, subtitle_stem=subtitle_stem)
        finally:
            lock.release()


    def start_assemble(self, project_id: str) -> dict[str, Any]:
        """贴齐进后台任务：完成后 ``jobs.message`` 就是替换 / 保留 / 截断的汇总。"""
        self._project(project_id)

        def work(job_id: str) -> str:
            lock = self._lock_project(project_id)
            try:
                result = self._assemble_service(
                    project_id,
                    job_id=job_id,
                    stage=lambda done, message: self._stage(job_id, done, message),
                    check=self._cancel_check(job_id),
                )
            finally:
                lock.release()
            return _assembly_message(result)

        return self._spawn(project_id, "assemble", 3, work)


    def _silenced_original(
        self,
        source: Path,
        spans: Sequence[tuple[float, float]],
        work: Path,
        target: Path,
        *,
        check: Callable[[], None] | None = None,
    ) -> Path:
        """原声里被配音覆盖的区间静音，得到一条与原片等长的底轨。

        用 ``volume=0:enable='between(t,…)+…'`` 做时间轴上的静音：整条原声只
        解一次、编一次。区间多到一行命令行装不下时按窗口切开——Windows 的
        命令行是 32767 字符，两千段的项目会超，切窗后每段只有几百个区间。
        """
        duration = probe_duration(source) or 0.0
        if duration <= 0:
            raise assembly.AssemblyError("原声音轨读不出时长，无法贴齐时间轴。")
        ranges = [
            (max(0.0, float(start)), min(duration, float(end)))
            for start, end in spans
            if float(end) > 0
        ]
        ranges = [(start, end) for start, end in ranges if end > start]
        if not ranges:
            shutil.copyfile(source, target)
            return target

        binary = ffmpeg_tools.find_ffmpeg()
        if not binary:
            raise assembly.AssemblyError("静音原声需要 ffmpeg：本机未找到。")
        from app.producers.builtin.dubbing import service as _dubbing_service

        windows = max(1, math.ceil(len(ranges) / _dubbing_service._MASK_RANGES_PER_CALL))
        step = duration / windows
        parts: list[Path] = []
        for index in range(windows):
            if check is not None:
                check()
            left = step * index
            right = duration if index == windows - 1 else step * (index + 1)
            inside = [
                (max(start, left) - left, min(end, right) - left)
                for start, end in ranges
                if end > left and start < right
            ]
            expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in inside) or "0"
            part = work / f"bed-{index:03d}.wav"
            graph = (
                f"[0:a]aresample=44100,aformat=channel_layouts=stereo,"
                f"atrim=start={left:.3f}:end={right:.3f},asetpts=PTS-STARTPTS,"
                # enable 按帧求值，默认 4096 样本一帧 ≈ 93 ms：段首会漏出近
                # 十分之一秒原声。先切成 256 样本的小帧，误差压到约 6 ms。
                f"asetnsamples=n=256,"
                f"volume=0:enable='{expr}'[out]"
            )
            command = [
                binary, "-hide_banner", "-nostdin", "-y", "-v", "error",
                "-i", str(source),
                "-filter_complex", graph,
                "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
                str(part),
            ]
            try:
                ffmpeg_tools.run_ffmpeg(
                    command, timeout=900, what=f"静音原声（{source.name}）"
                )
            except ffmpeg_tools.FfmpegError as exc:
                raise assembly.AssemblyError(str(exc)) from exc
            if not part.is_file():
                raise assembly.AssemblyError(
                    f"静音原声失败：ffmpeg 没有写出 {part.name}。"
                )
            parts.append(part)
        if len(parts) == 1:
            shutil.move(str(parts[0]), str(target))
            return target
        assembly.concat_audio(
            parts,
            target,
            work_dir=work,
            options=assembly.AssemblyOptions(channels=2, rate=44100),
        )
        return target


    def _export_service(
        self,
        project_id: str,
        *,
        allow_incomplete: bool = False,
        job_id: str = "",
        stage: Callable[[int, str], None] | None = None,
        check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """导出成品：**先按当前段落重新贴齐混音**，再封装。

        以前这一步只在 ``mix.wav`` 不存在时才拼装，于是改完一段再点导出，
        封进成片的还是上一版混音——"导出的永远是旧的"。现在每次导出都重跑
        ``assemble``，文件名带时间戳，项目目录里保留历次成品。
        """
        project = self._project(project_id)
        self._ensure_idle(project_id, allow_job_id=job_id)
        if check is not None:
            check()
        roles = self.store.roles(project_id)
        cues = self._cues_out(project_id, roles)
        stats = self._stats(cues, roles)
        blockers = stats["stale"] + stats["failed"] + stats["pending"]
        if blockers and not allow_incomplete:
            parts = []
            if stats["stale"]:
                parts.append(f"{stats['stale']} 段待重配")
            if stats["failed"]:
                parts.append(f"{stats['failed']} 段失败")
            if stats["pending"]:
                parts.append(f"{stats['pending']} 段未配音")
            raise ProducerError(
                "；".join(parts)
                + "。先重新合成，或确认“用旧音频导出”（成片可能与字幕不一致）。"
            )
        started = time.monotonic()
        out = self._out(project_id)
        stem = safe_segment(project["name"], "配音")
        settings = self.settings
        # 时间放在最前面：项目名常常很长（直接从源文件继承），排在后面的
        # 时间戳在成品列表与资源管理器里都会被截掉，看起来就像"还是旧同名"。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = (
            "mp4"
            if project["kind"] == "video"
            else (settings.audio_format if settings.audio_format in {"mp3", "m4a", "wav"} else "m4a")
        )
        target = out / f"{stamp}-{stem}.{suffix}"
        index = 2
        while target.exists():
            target = out / f"{stamp}-{stem}-{index}.{suffix}"
            index += 1
        # 字幕与成片同名：播放器会把 `xxx.mp4` 与 `xxx.srt` 当成一套，
        # 用户下载后也不用再改名。贴齐时就直接按这个名字写。
        self._assemble_service(
            project_id,
            subtitle_stem=target.stem,
            job_id=job_id,
            stage=stage,
            check=check,
        )
        mix = out / "mix.wav"
        assert mix.is_file()
        # 烧进容器的字幕轨从已写出的那几种里挑：设置的格式可能不含 SRT，
        # 而 MP4 的字幕轨只认 mov_text（由 mux_media 转），源文件用哪一种都行。
        subtitle = None
        if settings.burn_subtitles:
            for extension in (".srt", ".vtt", ".ass"):
                candidate = out / f"{target.stem}{extension}"
                if candidate.is_file():
                    subtitle = candidate
                    break
        if check is not None:
            check()
        if stage is not None:
            stage(4, "封装成片")
        try:
            if project["kind"] == "video":
                ffmpeg_tools.mux_media(
                    self.source_file(project),
                    mix,
                    target,
                    subtitles=subtitle,
                    bitrate=settings.bitrate,
                )
            else:
                ffmpeg_tools.encode_audio(mix, target, bitrate=settings.bitrate)
        except ffmpeg_tools.FfmpegError as exc:
            raise ProducerError(str(exc)) from exc
        self.store.update_project(project_id, status="exported")
        size = target.stat().st_size
        elapsed = time.monotonic() - started
        CHANNEL.info(
            f"导出完成：{target.name}（{size / 1024 / 1024:.1f} MB）"
            f" · 容器 {suffix} · 码率 {settings.bitrate if suffix != 'wav' else 'PCM'}"
            f" · 字幕 {'轨' if subtitle else (settings.subtitle_formats or '无')}"
            f" · {elapsed:.1f}s",
            source="dubbing",
            project=project_id,
            file=target.name,
            size=size,
            container=suffix,
            bitrate=settings.bitrate,
            subtitle=subtitle.name if subtitle else "",
            subtitle_formats=settings.subtitle_formats,
            fit=settings.fit,
            max_speed=settings.max_speed,
            loudness=settings.loudness,
            blocked=blockers,
            allow_incomplete=allow_incomplete,
            elapsed=round(elapsed, 1),
        )
        return {
            "object": "dubbing_export",
            "file": target.name,
            "path": f"out/{target.name}",
            "size": size,
            "at": _file_time(target),
            "duration": round(probe_duration(target) or 0.0, 3),
            "blocked": blockers,
        }


    def export(self, project_id: str, *, allow_incomplete: bool = False) -> dict[str, Any]:
        """同步导出；接口层走 ``start_export``，这里留给脚本与测试。"""
        lock = self._lock_project(project_id)
        try:
            return self._export_service(
                project_id, allow_incomplete=allow_incomplete
            )
        finally:
            lock.release()


    def start_export(
        self, project_id: str, *, allow_incomplete: bool = False
    ) -> dict[str, Any]:
        """导出进后台任务：四个阶段分别是贴齐、混音、字幕、封装。"""
        self._project(project_id)

        def work(job_id: str) -> str:
            lock = self._lock_project(project_id)
            try:
                result = self._export_service(
                    project_id,
                    allow_incomplete=allow_incomplete,
                    job_id=job_id,
                    stage=lambda done, message: self._stage(job_id, done, message),
                    check=self._cancel_check(job_id),
                )
            finally:
                lock.release()
            return _export_message(result)

        return self._spawn(project_id, "export", 4, work)

