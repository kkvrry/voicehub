"""配音服务：抽音轨 → 转录 → 逐段合成 → 贴齐 → 混音 → 导出。

每一步只做"读输入、写输出"，宿主负责切段、重采样、缓存与串行；
识别与合成走宿主已有的用例（``usecase.asr`` / ``usecase.gateway``），
本插件不认识任何一家模型。

时间轴只有一条规则：**段落的时间码来自源音频的实测边界**，不是按字数估的。
估出来的时间轴一定与口型错位，而错位无法在后期修正。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.infra import ffmpeg as ffmpeg_tools
from app.infra.archive import safe_segment
from app.infra.audioprobe import probe_duration, probe_media
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import separation as stem_models
from app.producers.builtin.dubbing.helpers import (
    _MIX_INTERMEDIATE,
    _RELINK_TOLERANCE_S,
    _SOURCE_SUFFIXES,
    _STEM_MODELS_TTL,
    _SUBTITLE_SUFFIXES,
    _engine_name,
    _file_time,
    _file_version,
    _JobCancelled,
    _join_text,
    _newest_first,
    _now_ms,
    _publish_media,
    _role_text,
    _sample_text,
    _speech_segments,  # noqa: F401
    _stamp,
    is_dubbed,
    role_of,
)
from app.producers.builtin.dubbing.settings import DubbingSettings
from app.producers.builtin.dubbing.store import DubbingStore
from app.usecase import asr as asr_uc
from app.usecase import subtitles as subtitle_uc
from app.usecase.engines import ENGINE_AUTO, auto_engine_keys
from app.usecase.library import LibraryError
from app.usecase.subtitles import normalize_encoding, parse_subtitles

if TYPE_CHECKING:
    from app.usecase.library import LibraryService

__all__ = ["STEP_MANIFEST", "DubbingService", "is_dubbed", "role_of"]

#: 流水线步骤的唯一清单：插件的步骤说明与项目详情里的状态卡共用这一份，
#: 改步骤只改这里，两处不会各写一套。
STEP_MANIFEST: tuple[tuple[str, str, str, str], ...] = (
    ("extract", "抽音轨", "解复用源文件，取出一条完整音轨。", "project"),
    (
        "separate",
        "分轨",
        "可选：分出人声与伴奏。配音段只静音原声人声，伴奏继续。",
        "project",
    ),
    ("transcribe", "转录", "按静音切段逐段识别，段落边界即字幕时间码。", "project"),
    ("synth", "配音", "按角色逐段合成，只重跑改动过的段。", "row"),
    (
        "align",
        "贴齐与混音",
        "按时间码补静音、加速贴齐，超出原句时隙的按槽位截断；再与人声按电平混合。",
        "project",
    ),
    ("export", "导出", "封装成片并写出字幕。", "project"),
)
STEP_LABELS = {key: label for key, label, _note, _unit in STEP_MANIFEST}
STEP_NOTES = {key: note for key, _label, note, _unit in STEP_MANIFEST}

#: 一条命令行装得下的静音区间数。Windows 的命令行上限是 32767 字符，
#: 每个区间在滤镜表达式里占约 30 字符，留一倍余量后按 300 个一段分窗。
_MASK_RANGES_PER_CALL = 300


from app.producers.builtin.dubbing.mix_stage import _MixMixin
from app.producers.builtin.dubbing.synth_stage import _SynthMixin
from app.producers.builtin.dubbing.transcribe_stage import _TranscribeMixin


class DubbingService(_TranscribeMixin, _SynthMixin, _MixMixin):
    def __init__(
        self,
        root: Path,
        settings: Callable[[], DubbingSettings] | DubbingSettings | None = None,
    ) -> None:
        self.root = root
        self.store = DubbingStore(root)
        #: 插件传 getter（设置页改完立即生效），测试可以直接传一份模型。
        self._settings = settings or DubbingSettings
        #: 贴齐与导出会写同一批产物：同一项目同一时刻只允许一个操作。
        #: 用可重入锁是因为导出内部还要再走一次贴齐。
        self._project_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        #: 运行中的分离模型：详情每几秒拉一遍，按短 TTL 缓存。
        self._stem_models: tuple[float, list[dict[str, Any]]] | None = None
        self._stem_models_guard = threading.Lock()
        self._backfill_subtitles()

    def _backfill_subtitles(self) -> None:
        """旧项目升级：转录过但还没有字幕成品的，补一份。

        已经被贴齐写过的字幕不覆盖：那是按成片时间轴对齐过的，拿源时间码
        重写会让下载的字幕与成片错位。
        """
        try:
            projects = self.store.list_projects()
        except Exception:  # noqa: BLE001 - 启动补写失败不该拖死插件
            return
        for project in projects:
            project_id = str(project["id"])
            try:
                if not self.store.cues(project_id):
                    continue
                # 已有生成的字幕只改名，不重写：可能已经按成片时间轴对齐过。
                if not self._normalize_subtitle_names(project_id):
                    self.sync_subtitles(project_id)
            except Exception as exc:  # noqa: BLE001 - 单个项目失败不影响其他项目
                CHANNEL.warning(
                    f"旧项目补写字幕失败：{exc}",
                    source="dubbing",
                    project=project_id,
                )

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

    def _stem_meta(self, project: dict[str, Any]) -> dict[str, Any]:
        meta = (project.get("options") or {}).get("stems")
        return meta if isinstance(meta, dict) else {}

    def _stem_paths(self, project: dict[str, Any]) -> tuple[Path, Path] | None:
        """当前可用的分轨文件 ``(人声, 伴奏)``。

        源音轨换过（重新抽取 / 重新指定）后旧分轨不再代表这一版声音：
        键对不上就当作没有，混音退回整条原声，详情提示重跑。
        """
        meta = self._stem_meta(project)
        if not meta:
            return None
        original = self.original(project)
        if not original.is_file() or str(meta.get("key") or "") != _stamp(original):
            return None
        folder = self.folder(project["id"]) / "audio"
        vocals = folder / "vocals.wav"
        accompaniment = folder / "accompaniment.wav"
        if not vocals.is_file() or not accompaniment.is_file():
            return None
        return vocals, accompaniment

    def _stem_state(self, project: dict[str, Any]) -> dict[str, Any]:
        """分轨在界面上的三档：没有、有但过期、当前可用。"""
        meta = self._stem_meta(project)
        folder = self.folder(project["id"]) / "audio"
        files = (folder / "vocals.wav").is_file() and (
            folder / "accompaniment.wav"
        ).is_file()
        current = self._stem_paths(project) is not None
        return {
            "files": files,
            "ready": current,
            "stale": bool(files and not current),
            "model": str(meta.get("model") or ""),
            "label": str(meta.get("label") or ""),
        }

    def _running_stem_models(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        """运行中的分轨模型：读现成的 audio.cpp 服务，不为读状态去拉起进程。"""
        now = time.monotonic()
        if not fresh:
            with self._stem_models_guard:
                cached = self._stem_models
                if cached is not None and now - cached[0] < _STEM_MODELS_TTL:
                    return [dict(row) for row in cached[1]]
        rows: list[dict[str, Any]] = []
        try:
            from app.usecase.producers import started_service

            service = started_service("audiocpp")
            if service is not None:
                rows = stem_models.options(list(service.running_models()))
        except Exception:  # noqa: BLE001 - 读不到运行时就是没有可选模型
            rows = []
        with self._stem_models_guard:
            self._stem_models = (now, rows)
        return [dict(row) for row in rows]

    def _assemble_key(self, project: dict[str, Any]) -> str:
        """贴齐实际使用的设置指纹：影响混音或随贴齐写出的字幕，全部进键。

        并发数不进：它只决定请求怎么排队，不改变成片。时间戳与文件 mtime
        回答"段落有没有改"，这里回答"设置有没有改"，两者缺一不可。
        """
        settings = self.settings
        options = project["options"]
        stems = self._stem_paths(project)
        payload = {
            "fit": bool(settings.fit),
            "max_speed": round(float(settings.max_speed), 3),
            "loudness": str(settings.loudness),
            "original_gain": round(float(options.get("original_gain", 0.0)), 2),
            "dub_gain": round(float(options.get("dub_gain", 0.0)), 2),
            "accompaniment_gain": round(
                float(options.get("accompaniment_gain", 0.0)), 2
            ),
            # 分轨换了一版或干脆没有，混音结果就是另一条：让旧混音失效。
            "stems": str(self._stem_meta(project).get("key") or "") if stems else "",
            "subtitle_formats": [
                item.strip().lower()
                for item in settings.subtitle_formats.split(",")
                if item.strip()
            ],
            "subtitle_max_chars": int(settings.subtitle_max_chars),
            "subtitle_encoding": str(settings.subtitle_encoding),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _subtitle_formats(self) -> list[str]:
        raw = str(self.settings.subtitle_formats or "")
        return [part.strip().lower() for part in raw.split(",") if part.strip()] or ["srt"]

    @staticmethod
    def _export_stems(out: Path) -> set[str]:
        """out 里历次成片的基础名；字幕跟随它们，清理时也只认这些。"""
        return {
            path.stem
            for path in out.iterdir()
            if path.is_file()
            and path.name not in _MIX_INTERMEDIATE
            and path.suffix.lower() not in _SUBTITLE_SUFFIXES
        }

    def _subtitle_stem(self, project_id: str) -> str:
        """字幕基础名跟当前成片走：有成品用成片名，没有就用项目名。

        播放器把 ``xxx.mp4`` 与 ``xxx.srt`` 当作一套；固定叫 ``subtitles``
        下载回去还得手动改名，字幕就不会自动挂到画面上。
        """
        out = self._out(project_id)
        exports = [
            path
            for path in out.iterdir()
            if path.is_file()
            and path.name not in _MIX_INTERMEDIATE
            and path.suffix.lower() not in _SUBTITLE_SUFFIXES
        ]
        if exports:
            latest = max(exports, key=_newest_first)
            return latest.stem
        project = self._project(project_id)
        return safe_segment(project["name"], "字幕")

    def _write_subtitles_for(
        self,
        project_id: str,
        entries: Sequence[tuple[float, float, str]],
        *,
        stem: str | None = None,
    ) -> dict[str, Path]:
        """把字幕写进 ``out/``，文件名与成片一致；旧的字幕文件一并清掉。

        字幕是用户能单独下载的成品，不在贴齐时才生成：转录或导入一完成就要
        出现在成品区。贴齐后会以同一组文件名重写，覆盖成对齐后的时间码。
        """
        out = self._out(project_id)
        settings = self.settings
        base = stem or self._subtitle_stem(project_id)
        written = subtitle_uc.write_subtitles(
            entries,
            out,
            base,
            formats=self._subtitle_formats(),
            encoding=normalize_encoding(settings.subtitle_encoding),
            max_chars=int(settings.subtitle_max_chars),
        )
        keep = {path.name for path in written.values()}
        if keep or not entries:
            # 没有段落时也清掉：删光段落之后，成品区不该还挂着一份旧字幕。
            # 只清应用自己生成的字幕（固定名、项目名、当前名、历次成片名），
            # 不碰 out 里用户自己放的 `.txt` 一类文件。项目名那一份是还没导出
            # 时的字幕名，导出后要让位给与成片同名的这一份。
            project_stem = safe_segment(self._project(project_id)["name"], "字幕")
            known = self._export_stems(out) | {"subtitles", base, project_stem}
            for path in out.iterdir():
                if (
                    path.is_file()
                    and path.suffix.lower() in _SUBTITLE_SUFFIXES
                    and path.stem in known
                    and path.name not in keep
                ):
                    path.unlink(missing_ok=True)
        return written

    def _normalize_subtitle_names(self, project_id: str) -> bool:
        """旧项目里固定叫 ``subtitles.*`` 的字幕改到当前成片名下。

        只改文件名，不重写内容：已经贴齐过的字幕是对齐过的，拿源时间码重写
        会让下载的字幕与成片错位。返回是否找到了应用生成的字幕。
        """
        out = self._out(project_id)
        stem = self._subtitle_stem(project_id)
        project_stem = safe_segment(self._project(project_id)["name"], "字幕")
        known = self._export_stems(out) | {"subtitles", stem, project_stem}
        subs = [
            path
            for path in out.iterdir()
            if path.is_file()
            and path.suffix.lower() in _SUBTITLE_SUFFIXES
            and path.stem in known
        ]
        if not subs:
            return False
        newest: dict[str, Path] = {}
        for path in sorted(subs, key=lambda item: item.stat().st_mtime):
            newest[path.suffix.lower()] = path
        keep_names: set[str] = set()
        for suffix, source in newest.items():
            target = out / f"{stem}{suffix}"
            keep_names.add(target.name)
            if source != target:
                target.unlink(missing_ok=True)
                source.rename(target)
        for path in subs:
            if path.name not in keep_names and path.exists():
                path.unlink(missing_ok=True)
        return True

    def sync_subtitles(self, project_id: str, *, announce: bool = False) -> dict[str, Path]:
        """按当前段落重写字幕成品；写失败只记 warning，不连累段落与音频落库。"""
        cues = self.store.cues(project_id)
        entries = [
            (
                max(0, int(cue["start_ms"])) / 1000.0,
                max(0, int(cue["end_ms"])) / 1000.0,
                " ".join(str(cue["text"] or "").split()),
            )
            for cue in cues
            if str(cue["text"] or "").strip()
            and int(cue["end_ms"]) > int(cue["start_ms"])
        ]
        try:
            written = self._write_subtitles_for(project_id, entries)
        except Exception as exc:  # noqa: BLE001 - 字幕写不出去，主操作仍然有效
            CHANNEL.warning(
                f"字幕成品没写出来：{exc}",
                source="dubbing",
                project=project_id,
            )
            return {}
        if announce and written:
            CHANNEL.info(
                f"字幕已写出：{'、'.join(path.name for path in written.values())}",
                source="dubbing",
                project=project_id,
                cues=len(entries),
                formats=sorted(written),
            )
        return written

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.project(project_id)
        if project is None:
            raise ProducerError("找不到这个配音项目：它可能已被删除。")
        return project

    def _abs(self, project: dict[str, Any], rel: str) -> Path:
        return self.folder(project["id"]) / rel

    def source_file(self, project: dict[str, Any]) -> Path:
        """源文件路径：引用导入是项目外的绝对路径，上传导入在项目目录里。"""
        if project.get("source_linked"):
            return Path(str(project["source_path"] or ""))
        return self._abs(project, project["source_path"])

    def source_ok(self, project: dict[str, Any]) -> bool:
        try:
            return self.source_file(project).is_file()
        except OSError:
            return False

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
            # 原声默认满电平：没被配音覆盖的段落就是原样的原声，
            # 这一项只用来压低它，不是"要不要保留"的开关。
            options={"original_gain": 0.0, "dub_gain": 0.0, "accompaniment_gain": 0.0},
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
        CHANNEL.info(
            f"导入项目：{title} · {'视频' if project['kind'] == 'video' else '音频'}"
            f" · {info.duration:.1f}s · {chosen.codec or '未知编码'}"
            f" · 第 {chosen.index} 条音轨",
            source="dubbing",
            project=project["id"],
            file=source_name,
            duration=round(info.duration, 1),
            kind=project["kind"],
            stream=chosen.index,
            codec=chosen.codec,
            channels=chosen.channels,
            size=source.stat().st_size if source.is_file() else 0,
        )
        return self.detail(project["id"])

    def create_linked(
        self,
        *,
        name: str,
        path: str | Path,
        stream: int = 0,
    ) -> dict[str, Any]:
        """引用本机文件建项目：源文件留在原处，项目只存转出的音轨与产物。

        与上传导入的区别只有一处——不复制源文件。预览、抽音轨、导出仍读原路径；
        原片被移动或删除时，项目会明确说"源文件不在了"，并允许重新指定。
        """
        source = self._linked_source(path)
        info = probe_media(source)
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

        title = (name or source.stem or "未命名项目").strip()
        project = self.store.create_project(
            name=title,
            source_name=source.name,
            source_path=str(source),
            source_linked=True,
            kind="video" if info.has_video else "audio",
            duration_ms=_now_ms(info.duration),
            stream_index=chosen.index,
            audio_path="",
            options={"original_gain": 0.0, "dub_gain": 0.0, "accompaniment_gain": 0.0},
        )
        self.folder(project["id"]).mkdir(parents=True, exist_ok=True)
        self.store.add_role(project["id"], name="旁白")
        try:
            self.ensure_audio(project["id"])
        except ProducerError as exc:
            CHANNEL.warning(f"抽音轨未完成：{exc}", source="dubbing")
        CHANNEL.info(
            f"引用项目：{title} · {'视频' if project['kind'] == 'video' else '音频'}"
            f" · {info.duration:.1f}s · {chosen.codec or '未知编码'}"
            f" · 第 {chosen.index} 条音轨 · 源文件留在原处",
            source="dubbing",
            project=project["id"],
            file=source.name,
            path=str(source),
            duration=round(info.duration, 1),
            kind=project["kind"],
            stream=chosen.index,
            codec=chosen.codec,
            channels=chosen.channels,
        )
        return self.detail(project["id"])

    def relink(self, project_id: str, *, path: str | Path) -> dict[str, Any]:
        """重新指定引用的源文件：文件被移动、改名后用它恢复项目。

        只接受时长与项目时间轴一致的文件——段落时间码按原片排，换一条片子
        会让整条字幕错位。音轨重新抽一份，旧的混音随之标成待重贴齐。
        """
        project = self._project(project_id)
        self._ensure_idle(project_id)
        if not project.get("source_linked"):
            raise ProducerError("这个项目是上传导入的，源文件已经在项目目录里。")
        source = self._linked_source(path)
        info = probe_media(source)
        if info.duration <= 0:
            raise ProducerError("读不出这个文件的时长，无法确认它和当前项目对得上。")
        if not info.audio:
            raise ProducerError("这个文件里没有音轨。")
        stream = int(project.get("stream_index") or 0)
        chosen = next((item for item in info.audio if item.index == stream), None)
        if chosen is None:
            raise ProducerError(
                f"这个文件没有第 {stream + 1} 条音轨；项目时间轴用的是那一条，"
                "换成另一条会错位。"
            )
        expected = int(project["duration_ms"]) / 1000.0
        if abs(info.duration - expected) > _RELINK_TOLERANCE_S:
            raise ProducerError(
                f"这个文件的时长是 {info.duration:.2f}s，项目时间轴是 {expected:.2f}s："
                "不是同一条片子，不能用来恢复这个项目。"
            )

        folder = self.folder(project_id)
        target = folder / "audio" / "original.wav"
        scratch = folder / "audio" / f"original.relink-{uuid.uuid4().hex[:8]}.wav"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        try:
            ffmpeg_tools.extract_audio(source, scratch, stream=stream)
        except ffmpeg_tools.FfmpegError as exc:
            scratch.unlink(missing_ok=True)
            raise ProducerError(str(exc)) from exc
        try:
            _publish_media(scratch, target)
        except OSError as exc:
            scratch.unlink(missing_ok=True)
            raise ProducerError(
                "新的音轨写不进去：可能有播放器正占用它，先停止试听再试。"
            ) from exc
        self.store.update_project(
            project_id,
            source_name=source.name,
            source_path=str(source),
            kind="video" if info.has_video else "audio",
            audio_path="audio/original.wav",
        )
        # 换了一份文件（哪怕时长相同）就不能继续拿旧混音当当前版本。
        self.store.touch(project_id)
        CHANNEL.info(
            f"重新指定源文件：{project['name']} → {source.name}",
            source="dubbing",
            project=project_id,
            path=str(source),
            duration=round(info.duration, 1),
        )
        return self.detail(project_id)

    @staticmethod
    def _linked_source(path: str | Path) -> Path:
        """本机引用路径：必须是绝对路径、真实文件、认识的容器。"""
        raw = str(path or "").strip()
        if not raw:
            raise ProducerError("没有给出要引用的文件路径。")
        source = Path(raw).expanduser()
        if not source.is_absolute():
            raise ProducerError("引用导入需要绝对路径。")
        try:
            source = source.resolve(strict=True)
        except OSError:
            raise ProducerError(f"找不到这个文件：{source}") from None
        if not source.is_file():
            raise ProducerError(f"这个路径不是文件：{source}")
        if source.suffix.lower() not in _SOURCE_SUFFIXES:
            raise ProducerError(
                f"不支持 .{source.suffix.lstrip('.') or '无后缀'}："
                "可选 mp4 / mov / mkv / webm / avi / wav / mp3 / m4a / aac / flac / ogg / opus。"
            )
        return source

    def delete(self, project_id: str) -> None:
        project = self._project(project_id)
        self._ensure_idle(project_id)
        self.store.delete_project(project_id)
        folder = self.folder(project_id)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        CHANNEL.info(
            f"删除项目：{project['name']}",
            source="dubbing",
            project=project_id,
        )

    def ensure_audio(self, project_id: str) -> Path:
        """确保有一条统一格式的原音轨；已有就复用（文件名固定，不带指纹）。"""
        project = self._project(project_id)
        self._ensure_idle(project_id)
        target = self.folder(project_id) / "audio" / "original.wav"
        if target.is_file() and target.stat().st_size > 44:
            if project["audio_path"] != "audio/original.wav":
                self.store.update_project(project_id, audio_path="audio/original.wav")
            return target
        source = self.source_file(project)
        if not source.is_file():
            if project.get("source_linked"):
                raise ProducerError(
                    f"引用的源文件不在了：{source}。在项目页重新指定路径后再抽音轨。"
                )
            raise ProducerError("源文件不在了：项目目录被移动或删除过。")
        stream = int(project.get("stream_index") or 0)
        try:
            ffmpeg_tools.extract_audio(source, target, stream=stream)
        except ffmpeg_tools.FfmpegError as exc:
            raise ProducerError(str(exc)) from exc
        self.store.update_project(project_id, audio_path="audio/original.wav")
        self.store.touch(project_id)
        CHANNEL.info(
            f"抽音轨完成：{source.name}（第 {stream} 条）"
            f"{' · 引用' if project.get('source_linked') else ''}"
            f" · {probe_duration(target):.1f}s",
            source="dubbing",
            project=project_id,
            stream=stream,
            size=target.stat().st_size if target.is_file() else 0,
        )
        return target

    def detail(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        roles = self.store.roles(project_id)
        cues = self._cues_out(project_id, roles)
        stats = self._stats(cues, roles)
        job = self.store.latest_job(project_id)
        out = self._out(project_id)
        duration = project["duration_ms"] / 1000.0
        has_audio = self.original(project).is_file()
        source_present = self.source_ok(project)
        vocal = out / "vocal.wav"
        mix = out / "mix.wav"
        # 成品只认"导出那一步写出来的文件"：vocal / mix / bed 与字幕是中间产物，
        # 把它们算进来会让「导出」在没导出时也显示完成。
        skip = _MIX_INTERMEDIATE
        subtitle_suffixes = _SUBTITLE_SUFFIXES
        files = [item for item in out.iterdir() if item.is_file()] if out.is_dir() else []
        reason = asr_uc.unavailable_reason()
        # 该配却配不了（角色没选音色）时在配音那一步说清，不然按钮点了只会报错。
        unvoiced_note = f" · {stats['unvoiced']} 段缺音色" if stats["unvoiced"] else ""
        stem_state = self._stem_state(project)
        stem_rows = [
            {"id": row["id"], "label": row["label"], "family": row["family"], "note": row["note"]}
            for row in self._running_stem_models()
        ]
        stem_note = (
            "没有正在运行的分离模型：到 audio.cpp 页加载一个分轨模型后再回来。"
            if not stem_rows
            else ""
        )
        export_files = sorted(
            (
                item
                for item in files
                if item.name not in skip and item.suffix.lower() not in subtitle_suffixes
            ),
            key=_newest_first,
            reverse=True,
        )
        export_meta = [
            {"name": item.name, "size": item.stat().st_size, "at": _file_time(item)}
            for item in export_files
        ]
        exports = [item["name"] for item in export_meta]
        subtitles = sorted(
            (item.name for item in files if item.suffix.lower() in subtitle_suffixes),
            key=str.lower,
        )
        newest_cue = max(
            [str(cue["updated_at"] or "") for cue in cues]
            + [str(project.get("content_at") or "")]
        )
        mix_time = _file_time(mix, precise=True)
        export_time = (
            _file_time(out / export_meta[0]["name"], precise=True) if export_meta else ""
        )
        # 混音/成片是否还代表当前段落与当前设置：文本改了要重写字幕，
        # 响度/贴齐/字幕设置改了也要重跑一遍。段落时间戳带微秒，同一秒里的
        # 改动也分得清；旧项目还没有设置指纹时先按段落判，贴齐一次后补上。
        stored_assemble = str(project.get("assemble_key") or "")
        assemble_ok = not stored_assemble or stored_assemble == self._assemble_key(project)
        mix_current = (
            bool(mix_time)
            and assemble_ok
            and (not newest_cue or mix_time >= newest_cue)
        )
        # 成片晚于混音还不够：混音本身可能早于最后一次改段。只有混音是当前的、
        # 成片又不早于它，才代表这份成片装的是现在的段落。
        export_current = bool(export_time) and mix_current and export_time >= mix_time
        blockers = {
            "stale": stats["stale"],
            "failed": stats["failed"],
            "unvoiced": stats["unvoiced"],
            "pending": stats["pending"],
        }
        # 只有“该配却没声音”的段会让成片与字幕错位；已经有旧音频的 stale/failed
        # 段是另一档风险，导出时单独提示。
        block_count = stats["stale"] + stats["failed"] + stats["pending"]

        # 角色不单列一步：分配角色在角色条完成，配音这一步直接带上它的统计。
        steps = [
            {
                "key": "extract",
                "label": STEP_LABELS["extract"],
                "note": STEP_NOTES["extract"],
                "state": "done" if has_audio else "wait",
                "summary": (
                    "源文件不在了，先重新指定路径"
                    if not source_present
                    else (
                        f"{project['kind'] == 'video' and '视频' or '音频'} · "
                        f"{duration:.1f}s · 第 {int(project['stream_index']) + 1} 条音轨"
                        f"{' · 引用' if project.get('source_linked') else ''}"
                        if has_audio
                        else "等待 ffmpeg"
                    )
                ),
            },
            {
                "key": "separate",
                "label": STEP_LABELS["separate"],
                "note": STEP_NOTES["separate"],
                "state": (
                    "run"
                    if job and job["kind"] == "separate" and job["state"] == "running"
                    else ("done" if stem_state["ready"] else "off")
                ),
                "summary": (
                    f"人声 + 伴奏 · {stem_state['label'] or '已分轨'}"
                    if stem_state["ready"]
                    else (
                        "分轨早于当前音轨，需重跑"
                        if stem_state["stale"]
                        else stem_note or "未分轨（可选）"
                    )
                ),
            },
            {
                "key": "transcribe",
                "label": STEP_LABELS["transcribe"],
                "note": STEP_NOTES["transcribe"],
                "state": (
                    "run" if job and job["kind"] == "transcribe" and job["state"] == "running"
                    else ("done" if stats["total"] else "wait")
                ),
                "summary": (
                    f"{stats['total']} 段" if stats["total"] else (reason or "未开始")
                ),
            },
            {
                "key": "synth",
                "label": STEP_LABELS["synth"],
                "note": STEP_NOTES["synth"],
                "state": (
                    "run" if job and job["kind"] == "synth" and job["state"] == "running"
                    else (
                        "done"
                        if stats["total"]
                        and (not stats["dubbed"] or stats["ready"] == stats["dubbed"])
                        else "wait"
                    )
                ),
                "summary": (
                    f"{stats['ready']} / {stats['dubbed']} 段已配音 · "
                    f"{stats['keep']} 段原声{unvoiced_note}"
                    if stats["dubbed"]
                    else ("没有需要配音的段落 · 全部保留原声" if stats["total"] else "未开始")
                ),
            },
            {
                "key": "align",
                "label": STEP_LABELS["align"],
                "note": STEP_NOTES["align"],
                "state": "done" if mix.is_file() and mix_current else "wait",
                "summary": (
                    f"替换 {stats['voiced']} 段 · 保留 {stats['total'] - stats['voiced']} 段"
                    if mix_current
                    else (
                        "有改动，需重新贴齐与混音"
                        if mix.is_file()
                        else "还没贴齐"
                    )
                ),
            },
            {
                "key": "export",
                "label": STEP_LABELS["export"],
                "note": STEP_NOTES["export"],
                "state": "done" if export_current else "wait",
                "summary": (
                    f"{exports[0]}（最新）"
                    if export_current
                    else ("成品早于当前混音，重新导出" if exports else "未导出")
                ),
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
            # 引用导入的源文件在项目外：详情要说清它现在还在不在。
            "source_ok": source_present,
            "source_linked": bool(project.get("source_linked")),
            "paths": {
                "source": project["source_path"],
                "original": project["audio_path"],
                "vocal": "out/vocal.wav" if vocal.is_file() else "",
                "mix": "out/mix.wav" if mix.is_file() else "",
                # 整片试听要带版本：贴齐会覆盖同名 mix.wav，不带版本会播缓存里的旧混音。
                "mix_version": _file_version(mix) if mix.is_file() else "",
                "mix_current": mix_current,
                "export_current": export_current,
                "exports": exports,
                "export_meta": export_meta,
                "subtitles": subtitles,
                # 列成品时显示体积：几 MB 与几百 KB 一眼分得清导出的是哪一版。
                "sizes": {item.name: item.stat().st_size for item in files},
            },
            "separation": {
                "ready": stem_state["ready"],
                "stale": stem_state["stale"],
                "files": stem_state["files"],
                "model": stem_state["model"],
                "label": stem_state["label"],
                "models": stem_rows,
                "note": stem_note,
            },
            "export": {
                "ready": block_count == 0 and bool(stats["voiced"]),
                "blockers": block_count,
                "stats": blockers,
            },
            "asr": {"ready": not reason, "note": reason},
            "settings": self.settings.model_dump(),
        }

    def _cues_out(
        self, project_id: str, roles: Sequence[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """段落 + ``dub``：这一段是重新配音还是保留原声。界面按它决定行上的说法。

        ``audio_version`` 是产物的版本号（mtime_ns + 字节数）：重配会覆盖同名文件，
        播放地址不带版本时浏览器会拿缓存里的旧音——表现就是"改了字、重配了，
        听着还是上一版"。
        """
        owned = roles if roles is not None else self.store.roles(project_id)
        folder = self.folder(project_id)
        # 同一台引擎的身份只取一次：详情每几秒拉一遍，不能逐段枚举插件。
        identities: dict[str, str] = {}
        out: list[dict[str, Any]] = []
        for cue in self.store.cues(project_id):
            role = role_of(cue, owned)
            item = {**cue, "dub": is_dubbed(cue, owned)}
            path = folder / cue["audio"] if cue["audio"] else None
            item["audio_version"] = _file_version(path) if path is not None else ""
            # 库里有路径不等于文件还在：清理、手动删目录都会让它变成一条
            # "显示已配音、点了却读不出来"的行。界面据此把播放键灰掉。
            item["audio_ok"] = bool(path is not None and path.is_file())
            # 引擎升级、换模型或读音表变了：旧产物不再是当前参数的产物。
            # 有键才判；早期手工写入、没有键的样例保持原状态。
            if (
                cue["audio_key"]
                and item["audio_ok"]
                and item["dub"]
                and cue["state"] in {"ready", "stale"}
                and role is not None
            ):
                engine_key = str(role.get("engine") or "")
                if engine_key not in identities:
                    identities[engine_key] = self._identity_for(engine_key)
                item["state"] = (
                    "ready"
                    if self._key_matches(cue, role, identity=identities[engine_key])
                    else "stale"
                )
            out.append(item)
        return out

    @staticmethod
    def _stats(
        cues: Sequence[dict[str, Any]], roles: Sequence[dict[str, Any]]
    ) -> dict[str, int]:
        """段落统计。``total`` 是全部段落，配音进度只看 ``dubbed`` 那部分：
        保留原声的段永远不会有产物，算进分母会让进度条永远差一截。

        ``voiced`` 是"成片里真的换成了配音"的段数——改过文本但还没重合成的段
        也算，它放的是旧产物，原声照样被它顶掉。``unvoiced`` 是角色还没选音色的
        配音段：该配、配不了，界面要能说清卡在哪。
        """
        dubbed = [cue for cue in cues if cue["dub"] and str(cue["text"] or "").strip()]

        def has_voice(cue: dict[str, Any]) -> bool:
            role = role_of(cue, roles)
            return bool(role and role["engine"] and role["voice_id"])

        # 路径在库里不等于文件还在：audio_ok 是详情接口已经算好的答案。
        def usable(cue: dict[str, Any]) -> bool:
            return bool(cue["audio"]) and bool(cue.get("audio_ok", True))

        voiced = [cue for cue in dubbed if usable(cue)]
        ready = [
            cue for cue in dubbed if usable(cue) and cue["state"] == "ready"
        ]
        # 失败是独立一档：没有音频的失败段不再重复算进 pending，否则导出提示
        # 会把同一段写成"失败 + 未配音"两条。
        failed = [cue for cue in dubbed if cue["state"] == "failed"]
        stale = [
            cue for cue in dubbed if usable(cue) and cue["state"] == "stale"
        ]
        pending = [
            cue
            for cue in dubbed
            if not usable(cue) and cue["state"] != "failed"
        ]
        return {
            "total": len(cues),
            "dubbed": len(dubbed),
            "keep": len(cues) - len(dubbed),
            "voiced": len(voiced),
            "ready": len(ready),
            "stale": len(stale),
            "failed": len(failed),
            "pending": len(pending),
            "unvoiced": sum(1 for cue in dubbed if not has_voice(cue)),
        }

    # ---- 段落编辑 ---------------------------------------------------------

    def _remove_cue_audio(self, project_id: str, cue: dict[str, Any]) -> None:
        """删段/重转录时顺带清掉它自己的产物：文件按 cue id 命名，没有别处再用。"""
        rel = str(cue.get("audio") or "")
        if not rel:
            return
        folder = self.folder(project_id).resolve()
        try:
            path = (folder / rel).resolve()
        except OSError:
            return
        if not path.is_relative_to(folder):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def rename(self, project_id: str, name: str) -> dict[str, Any]:
        project = self._project(project_id)
        value = str(name or "").strip()
        if not value:
            raise ProducerError("项目名不能为空。")
        if value == project["name"]:
            return self.detail(project_id)
        self.store.update_project(project_id, name=value[:120])
        CHANNEL.info(
            f"项目改名：{project['name']} → {value[:120]}",
            source="dubbing",
            project=project_id,
        )
        return self.detail(project_id)

    def batch_cues(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """成批改角色或删除：字幕多选后的唯一写入口。"""
        self._project(project_id)
        action = str(payload.get("action") or "").strip()
        wanted = [str(item) for item in (payload.get("cue_ids") or []) if str(item)]
        if not wanted:
            raise ProducerError("没有选中段落。")
        roles = self.store.roles(project_id)
        if action == "role":
            role_id = str(payload.get("role_id") or "")
            if role_id and not any(role["id"] == role_id for role in roles):
                raise ProducerError("找不到这个角色。")
            target = next((role for role in roles if role["id"] == role_id), roles[0] if roles else None)
            identity = self._identity_for(str((target or {}).get("engine") or ""))
            touched = 0
            for cue_id in dict.fromkeys(wanted):
                cue = self.store.cue(cue_id)
                if cue is None or cue["project_id"] != project_id:
                    continue
                fields: dict[str, Any] = {"role_id": role_id}
                if cue["audio"]:
                    owner = role_of({**cue, **fields}, roles)
                    same = bool(
                        owner
                        and cue["audio_key"]
                        and self._key_matches(
                            {**cue, **fields}, owner, identity=identity
                        )
                        and (self.folder(project_id) / cue["audio"]).is_file()
                    )
                    fields["state"] = "ready" if same else "stale"
                self.store.update_cue(cue_id, **fields)
                touched += 1
        elif action == "delete":
            touched = 0
            for cue_id in dict.fromkeys(wanted):
                cue = self.store.cue(cue_id)
                if cue is None or cue["project_id"] != project_id:
                    continue
                self._remove_cue_audio(project_id, cue)
                self.store.delete_cue(cue_id)
                touched += 1
            self.store.renumber(project_id)
            self.store.touch(project_id)
            self.sync_subtitles(project_id)
        else:
            raise ProducerError("不认识的批量操作。")
        cues = self._cues_out(project_id, roles)
        CHANNEL.info(
            f"批量修改段落：{action} · {touched} / {len(wanted)} 段",
            source="dubbing",
            project=project_id,
            action=action,
        )
        return {
            "object": "dubbing_cues",
            "data": cues,
            "stats": self._stats(cues, roles),
        }

    def import_subtitles(self, project_id: str, data: bytes, filename: str = "") -> dict[str, Any]:
        """导入 SRT / VTT 当作段落表：源片已有字幕时不必再识别一遍。

        只取时间码与文本；角色按旧段落的重叠关系继承，初次导入一律归旁白。
        """
        project = self._project(project_id)
        self._ensure_idle(project_id)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise ProducerError("字幕文件不是 UTF-8 或 GB18030 编码。") from exc
        items = parse_subtitles(text)
        if not items:
            raise ProducerError("没有读到字幕：只支持带时间码的 SRT / VTT。")
        duration = int(project["duration_ms"])
        old = self.store.cues(project_id)
        narrator = next((role["id"] for role in self.store.roles(project_id)), "")
        rows: list[dict[str, Any]] = []
        for start, end, content in items:
            start_ms = max(0, round(start * 1000))
            end_ms = max(start_ms + 1, round(end * 1000))
            if duration:
                start_ms = min(duration, start_ms)
                end_ms = min(duration, end_ms)
            if end_ms <= start_ms:
                continue
            rows.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": " ".join(content.split()),
                    "role_id": self._nearest_role(old, start_ms / 1000, end_ms / 1000)
                    or narrator,
                }
            )
        rows.sort(key=lambda item: item["start_ms"])
        # 重叠的字幕（滚动字幕常见）不能把后一条压成起止倒挂：起点推到前一条
        # 结束处，终点至少留 1 ms；推到时长之外的那条直接丢弃。
        clean: list[dict[str, Any]] = []
        for row in rows:
            if clean:
                row["start_ms"] = max(row["start_ms"], clean[-1]["end_ms"])
            if duration and row["start_ms"] >= duration:
                continue
            row["end_ms"] = max(row["end_ms"], row["start_ms"] + 1)
            if duration:
                row["end_ms"] = min(duration, row["end_ms"])
            if row["end_ms"] <= row["start_ms"]:
                continue
            clean.append(row)
        if not clean:
            raise ProducerError("字幕的时间码都落在源文件时长之外。")
        rows = clean
        for cue in old:
            self._remove_cue_audio(project_id, cue)
        self.store.replace_cues(project_id, rows)
        self.store.update_project(project_id, status="transcribed")
        self.sync_subtitles(project_id, announce=True)
        CHANNEL.info(
            f"导入字幕：{filename or '字幕文件'} · {len(rows)} 段",
            source="dubbing",
            project=project_id,
            cue_count=len(rows),
        )
        return self.detail(project_id)

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
            # 用户改回去时还能听回原来那条（重合成会覆盖）。改回原句时键值对得上，
            # 仍算已配音——一次误改不该让人白等一轮合成。
            updated_probe = {**cue, **fields}
            role = role_of(updated_probe, self.store.roles(project_id))
            same = bool(
                role
                and self._key_matches(updated_probe, role)
                and (self.folder(project_id) / cue["audio"]).is_file()
            )
            fields["state"] = "ready" if same else "stale"
        if fields:
            self.store.update_cue(cue_id, **fields)
        # 改字或改时间码会让下载的字幕对不上画面；角色变化不影响字幕文本。
        if any(key in fields for key in ("text", "start_ms", "end_ms")):
            self.sync_subtitles(project_id)
        updated = self.store.cue(cue_id)
        assert updated is not None
        folder = self.folder(project_id)
        path = folder / updated["audio"] if updated["audio"] else None
        return {
            **updated,
            "dub": is_dubbed(updated, self.store.roles(project_id)),
            "audio_version": _file_version(path) if path is not None else "",
            "audio_ok": bool(path is not None and path.is_file()),
        }

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
            role_id=str(cue["role_id"] or ""),
        )
        self.sync_subtitles(project_id)
        return self._cues_out(project_id)

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
        self._remove_cue_audio(project_id, nxt)
        self.store.update_cue(
            cue_id,
            end_ms=int(nxt["end_ms"]),
            text=text,
            state="stale" if this["audio"] else "empty",
        )
        self.store.delete_cue(nxt["id"])
        # 删除后序号会出现空洞：重排一次，让「第 N 段」与界面上的序号一致。
        self.store.renumber(project_id)
        self.sync_subtitles(project_id)
        return self._cues_out(project_id)

    def delete_cue(self, project_id: str, cue_id: str) -> list[dict[str, Any]]:
        self._project(project_id)
        cue = self.store.cue(cue_id)
        if cue is not None and cue["project_id"] == project_id:
            self._remove_cue_audio(project_id, cue)
        self.store.delete_cue(cue_id)
        self.store.renumber(project_id)
        self.store.touch(project_id)
        self.sync_subtitles(project_id)
        return self._cues_out(project_id)

    # ---- 角色 -------------------------------------------------------------

    @staticmethod
    def _auto_engine_for(
        voice_id: str, library: LibraryService | None, preferred: str = ""
    ) -> str:
        """「自动」此刻落到哪台；没有可用返回空串。

        给了音色库就按这条音色能落的引擎筛（平台音色不能被派给不认它的引擎）；
        库不可用（测试、后台任务）时只看当前在跑的本地引擎，由合成时再校正。
        ``preferred`` 是上一次那台：它还有实例在跑就继续用——一本书不该因为
        后来多起了一台引擎就整轨换嗓；它不在了才换。
        """
        voice = None
        if library is not None and voice_id:
            try:
                voice = library.get_voice(voice_id)
            except LibraryError:
                voice = None
        keys = auto_engine_keys(voice)
        if preferred and preferred in keys:
            return preferred
        return keys[0] if keys else ""

    def _refresh_auto_roles(
        self, project_id: str, library: LibraryService | None
    ) -> None:
        """开始合成前重新选一次「自动」角色的引擎。

        角色上存的是上一次选中的实际引擎：实例换了、插件升级了，只要再开始
        一次合成就会跟着换，调用方不必改配置。选不到时保留上一次那台——它可能
        只是暂时没起，合成会在那一台上给出「不可用」的明确说法。
        """
        for role in self.store.roles(project_id):
            if not role.get("engine_auto"):
                continue
            previous = str(role.get("engine") or "")
            key = self._auto_engine_for(
                str(role.get("voice_id") or ""), library, preferred=previous
            )
            if not key or key == previous:
                continue
            self.store.update_role(role["id"], engine=key, engine_auto=True)
            CHANNEL.info(
                f"自动引擎：{role['name']} 改用 {_engine_name(key)}"
                + (f"（原 {_engine_name(previous)}）" if previous else ""),
                source="dubbing",
                project=project_id,
                role=role["id"],
                engine=key,
                previous=previous,
            )

    def add_role(
        self,
        project_id: str,
        payload: dict[str, Any],
        library: LibraryService | None = None,
    ) -> dict[str, Any]:
        self._project(project_id)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ProducerError("角色要有名字。")
        raw_engine = str(payload.get("engine") or "").strip()
        engine_auto = raw_engine == ENGINE_AUTO
        engine = (
            self._auto_engine_for(str(payload.get("voice_id") or ""), library)
            if engine_auto
            else raw_engine
        )
        role = self.store.add_role(
            project_id,
            name=name,
            mode=str(payload.get("mode") or "tts"),
            engine=engine,
            engine_auto=engine_auto,
            voice_id=str(payload.get("voice_id") or ""),
            voice_name=str(payload.get("voice_name") or ""),
            sample_mode=str(payload.get("sample_mode") or ""),
            sample_file=str(payload.get("sample_file") or ""),
            params=payload.get("params") or {},
        )
        if engine_auto and not engine:
            CHANNEL.warning(
                f"自动引擎暂无可用：{role['name']} 只有云端或没有实例在跑的引擎，"
                "开始合成时会再选一次。",
                source="dubbing",
                project=project_id,
                role=role["id"],
            )
        CHANNEL.info(
            f"新建角色：{role['name']}",
            source="dubbing",
            project=project_id,
            role=role["id"],
            engine=role["engine"],
            auto=engine_auto,
            voice=role["voice_id"],
        )
        return role

    def update_role(
        self,
        project_id: str,
        role_id: str,
        payload: dict[str, Any],
        library: LibraryService | None = None,
    ) -> dict[str, Any]:
        self._project(project_id)
        role = self.store.role(role_id)
        if role is None or role["project_id"] != project_id:
            raise ProducerError("找不到这个角色。")
        fields = {
            key: payload[key]
            for key in (
                "name",
                "mode",
                "engine",
                "voice_id",
                "voice_name",
                "sample_mode",
                "sample_file",
                "params",
            )
            if key in payload
        }
        if "engine" in fields:
            raw_engine = str(fields["engine"] or "").strip()
            if raw_engine == ENGINE_AUTO:
                fields["engine_auto"] = True
                fields["engine"] = self._auto_engine_for(
                    str(fields.get("voice_id", role.get("voice_id")) or ""),
                    library,
                    preferred=str(role.get("engine") or ""),
                )
            else:
                fields["engine_auto"] = False
                fields["engine"] = raw_engine
        elif role.get("engine_auto") and "voice_id" in fields:
            # 自动角色换了音色：候选引擎跟着音色变，当场再选一次（原来那台还在就继续用）。
            fields["engine"] = self._auto_engine_for(
                str(fields["voice_id"] or ""),
                library,
                preferred=str(role.get("engine") or ""),
            )
        if "sample_mode" in fields:
            mode = str(fields["sample_mode"] or "")
            if mode not in {"", "emotion", "fixed"}:
                raise ProducerError("样例策略只能是按情绪匹配或固定样例。")
            fields["sample_mode"] = mode
        next_mode = str(fields.get("sample_mode", role.get("sample_mode")) or "")
        if "sample_file" in fields:
            fields["sample_file"] = str(fields["sample_file"] or "")
        if next_mode != "fixed":
            # 不用固定样例时留着旧文件名只会让下一次换成固定样例时误用它。
            fields["sample_file"] = ""
        elif "sample_file" in fields and not fields["sample_file"]:
            raise ProducerError("选择固定样例时，要指定用哪一条样例。")
        self.store.update_role(role_id, **fields)
        updated = self.store.role(role_id)
        assert updated is not None
        if {"mode", "engine", "voice_id", "params", "sample_mode", "sample_file"} & set(
            fields
        ):
            # 处理方式从配音改回原声（或反过来）也会改变成片，但不需要逐段改状态；
            # 用一个项目级时间把混音与成片标成过期。
            self.store.touch(project_id)
        if {"engine", "voice_id", "params", "sample_mode", "sample_file"} & set(fields):
            # 换了音色或样例：这个角色名下的段落都要重配。但**换回来**时旧产物仍然
            # 算数——合成键逐段对，且文件还在，才算「已配音」，与创作页同一条判据。
            roles = self.store.roles(project_id)
            folder = self.folder(project_id)
            identity = self._identity_for(str(updated.get("engine") or ""))
            restored = 0
            for cue in self.store.cues(project_id):
                if not cue["audio"]:
                    continue
                owner = role_of(cue, roles)
                if owner is None or owner["id"] != role_id:
                    continue
                same = (
                    self._key_matches(cue, updated, identity=identity)
                    and (folder / cue["audio"]).is_file()
                )
                self.store.update_cue(cue["id"], state="ready" if same else "stale")
                restored += 1 if same else 0
            if restored:
                CHANNEL.info(
                    f"恢复旧配音：{_role_text(updated)} 的 {restored} 段与新音色重新对上，"
                    "不需要重配。",
                    source="dubbing",
                    project=project_id,
                    role=role_id,
                )
        CHANNEL.info(
            f"更新角色：{updated['name']} · {updated['mode']}"
            + (f" · {_role_text(updated)}" if updated["voice_id"] else "")
            + (
                f" · 样例 {_sample_text(updated)}"
                if updated["voice_id"] and updated.get("sample_mode")
                else ""
            ),
            source="dubbing",
            project=project_id,
            role=role_id,
            mode=updated["mode"],
            engine=updated["engine"],
            voice=updated["voice_id"],
            sample_mode=updated.get("sample_mode", ""),
            sample_file=updated.get("sample_file", ""),
            fields=sorted(fields),
        )
        return updated

    def delete_role(self, project_id: str, role_id: str) -> None:
        self._project(project_id)
        role = self.store.role(role_id)
        if role is None or role["project_id"] != project_id:
            raise ProducerError("找不到这个角色。")
        affected = self.store.delete_role(role_id)
        roles = self.store.roles(project_id)
        folder = self.folder(project_id)
        identities: dict[str, str] = {}
        restored = 0
        for cue_id in affected:
            cue = self.store.cue(cue_id)
            if cue is None or not cue["audio"]:
                continue
            owner = role_of(cue, roles)
            engine_key = str((owner or {}).get("engine") or "")
            if engine_key not in identities:
                identities[engine_key] = self._identity_for(engine_key)
            same = bool(
                owner
                and self._key_matches(cue, owner, identity=identities[engine_key])
                and (folder / cue["audio"]).is_file()
            )
            self.store.update_cue(cue["id"], state="ready" if same else "stale")
            restored += 1 if same else 0
        CHANNEL.info(
            f"删除角色：{role['name']} · {len(affected)} 段退回旁白"
            + (f" · {restored} 段沿用旧配音" if restored else ""),
            source="dubbing",
            project=project_id,
            role=role_id,
            cues=len(affected),
            restored=restored,
        )

    # ---- 分轨 -------------------------------------------------------------





    # ---- 任务 -------------------------------------------------------------

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.job(job_id)
        return bool(job and job["cancel_requested"])

    def _cancel_check(self, job_id: str) -> Callable[[], None]:
        """给拼装算法的检查点：取消就走 ``_JobCancelled``，不会被当成失败。"""

        def check() -> None:
            if self._cancelled(job_id):
                raise _JobCancelled

        return check

    def _stage(self, job_id: str, done: int, message: str) -> None:
        self.store.update_job(job_id, done=done, message=message)

    def _project_lock(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._project_locks[project_id] = lock
            return lock

    def _lock_project(self, project_id: str) -> threading.RLock:
        """抢项目的操作位；忙就直说，不让第二个请求排到超时。"""
        lock = self._project_lock(project_id)
        if not lock.acquire(blocking=False):
            raise ProducerError("这个项目正在贴齐、导出或分轨，等它结束再试。")
        return lock

    def _ensure_idle(self, project_id: str, *, allow_job_id: str = "") -> None:
        """拼装与导出会读整批段落：有任务在写同一批段时不能开始。

        ``allow_job_id`` 是当前执行线程自己的任务：导出任务内部要再走一遍贴齐，
        不能把它自己当成"另一个任务"挡掉。
        """
        job = self.store.latest_job(project_id)
        if job and job["state"] == "running" and job["id"] != allow_job_id:
            raise ProducerError(
                f"「{job['kind']}」任务还在跑，等它结束再操作这个项目。"
            )

    def _spawn(
        self, project_id: str, kind: str, total: int, work: Callable[[str], str]
    ) -> dict[str, Any]:
        """起一个后台任务；同一项目同一时间只允许一个任务在跑。

        串行是刻意的：识别与合成共用一张显卡时，两个任务同时压模型只会都变慢；
        而且转录会整批替换段落，合成若同时在写这些段落，结果没有一致可言。
        """
        current = self.store.latest_job(project_id)
        if current and current["state"] == "running":
            raise ProducerError(
                f"已经有一个「{current['kind']}」任务在跑，等它结束或先取消。"
            )
        job = self.store.start_job(project_id, kind, total)
        threading.Thread(
            target=self._run_job, args=(job["id"], kind, work), daemon=True
        ).start()
        CHANNEL.info(
            f"任务开始：{kind} · {total} 段",
            source="dubbing",
            project=project_id,
            job=job["id"],
            kind=kind,
            total=total,
        )
        return job

    def _run_job(self, job_id: str, kind: str, work: Callable[[str], str]) -> None:
        started = time.monotonic()
        try:
            message = work(job_id)
        except _JobCancelled:
            self.store.update_job(job_id, state="cancelled", message="已取消")
            CHANNEL.info(
                f"任务取消：{kind} · {round(time.monotonic() - started, 1)}s",
                source="dubbing",
                job=job_id,
                kind=kind,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落到任务上，不能让线程静默死掉
            self.store.update_job(job_id, state="failed", message=str(exc))
            CHANNEL.error(
                f"配音任务失败（{kind}）：{exc}",
                source="dubbing",
                job=job_id,
                kind=kind,
                elapsed=round(time.monotonic() - started, 1),
            )
            return
        elapsed = time.monotonic() - started
        # 贴齐/导出在最后一个耗时阶段之后可能才收到取消：产物已经写完，
        # 不能把一份已经落盘的成片记成"已取消"。取消只在检查点生效。
        if self._cancelled(job_id) and kind not in {"assemble", "export"}:
            self.store.update_job(job_id, state="cancelled", message="已取消")
            CHANNEL.info(
                f"任务取消：{kind} · {elapsed:.1f}s",
                source="dubbing",
                job=job_id,
                kind=kind,
                elapsed=round(elapsed, 1),
            )
            return
        self.store.update_job(job_id, state="done", message=message or "完成")
        CHANNEL.info(
            f"任务结束：{kind} · {message or '完成'} · {elapsed:.1f}s",
            source="dubbing",
            job=job_id,
            kind=kind,
            elapsed=round(elapsed, 1),
        )

    # ---- 转录 -------------------------------------------------------------





    # ---- 合成 -------------------------------------------------------------











    # ---- 拼装 / 混音 / 导出 ----------------------------------------------








    # ---- 媒体 -------------------------------------------------------------

    def media_path(self, project_id: str, kind: str, name: str = "") -> Path:
        """取一个可下载/可播放的产物路径；越界与不存在都在这里挡掉。"""
        project = self._project(project_id)
        folder = self.folder(project_id).resolve()
        if kind == "source":
            path = self.source_file(project)
            # 引用导入的源文件在项目外：只放行这一个已知路径，不放开整个目录。
            if project.get("source_linked"):
                resolved = path.resolve()
                if not resolved.is_file():
                    raise ProducerError(
                        "引用的源文件不在了：在项目页重新指定路径后再播放或导出。"
                    )
                return resolved
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
