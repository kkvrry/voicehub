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
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.engines.base import CODES_ENGINE_UNAVAILABLE, EngineError
from app.infra import ffmpeg as ffmpeg_tools
from app.infra.archive import safe_segment
from app.infra.audioprobe import probe_duration, probe_media
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import separation as stem_models
from app.producers.builtin.audiocpp import tasks as audiocpp_tasks
from app.producers.builtin.dubbing import diar as speaker_models
from app.producers.builtin.dubbing.settings import LOUDNESS_TARGETS, DubbingSettings
from app.producers.builtin.dubbing.store import DubbingStore
from app.usecase import asr as asr_uc
from app.usecase import assembly
from app.usecase import subtitles as subtitle_uc
from app.usecase.subtitles import normalize_encoding, parse_subtitles

if TYPE_CHECKING:
    from app.usecase.library import LibraryService

__all__ = ["STEP_MANIFEST", "DubbingService"]

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


class _JobCancelled(Exception):
    """任务被用户取消：由检查点抛出，``_run_job`` 记成 cancelled 而不是 failed。"""


def _assembly_message(result: dict[str, Any]) -> str:
    """贴齐完成后给任务条的一句话；与前端原先的完成提示同一组口径。"""
    parts = [f"替换 {result['dubbed']} 段"]
    if result.get("kept"):
        parts.append(f"保留原声 {result['kept']} 段")
    if result.get("delayed"):
        parts.append(f"顺延 {len(result['delayed'])} 段")
    if result.get("fitted"):
        parts.append(f"变速 {len(result['fitted'])} 段")
    if result.get("truncated"):
        parts.append(f"截断 {len(result['truncated'])} 段")
    if result.get("skipped"):
        parts.append(f"{result['skipped']} 段未配音")
    return " · ".join(parts)


def _export_message(result: dict[str, Any]) -> str:
    size = int(result.get("size") or 0)
    return f"已导出 {result['file']}（{size / 1024 / 1024:.1f} MB）"


#: 太短的段不单独成句：那是呼吸或杂音，识别出来也只有一个字。
_MIN_SEGMENT_S = 0.35
#: 切段时两端各留一点余量，避免把首尾的辅音切掉。
_SEGMENT_PAD_S = 0.06

#: out/ 里的中间产物：不算成品，也不用来给字幕起名。
_MIX_INTERMEDIATE = {"vocal.wav", "mix.wav", "bed.wav", "vocals-masked.wav"}

#: 引用导入接受的容器；与上传同一组，不在这里的路径不交给 ffmpeg 猜。
_SOURCE_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
}
#: 重新指定源文件时允许的时长偏差：段落时间码按原时长排，换一条片子会整片错位。
_RELINK_TOLERANCE_S = 0.2

#: 分轨按块跑：audio.cpp 把整段音频编成 base64 放在响应里，两小时的片一次几十 GB
#: 内存会直接打穿。十分钟一块把峰值压到几百 MB，也顺带给了任务进度。
_STEM_CHUNK_S = 600.0
#: 相邻块重叠一点再切掉：模型在边界缺上下文，直接拼会听到一道接缝。
_STEM_OVERLAP_S = 0.5
#: 模型输出与输入时长的容许偏差：对不上就说明返回的不是这一块。
_STEM_TOLERANCE_S = 0.25
#: 运行中分离模型的缓存时长（秒）：详情会被反复拉取，不必每次去连实例。
_STEM_MODELS_TTL = 5.0

#: 引擎中文名只在这里查一次：日志里写 key 等于让人自己翻译。
_ENGINE_NAMES: dict[str, str] = {}


def _engine_name(key: str) -> str:
    """引擎 key → 界面上的名字；插件卸载后照 key 记，不抛。"""
    key = str(key or "")
    if not key:
        return "—"
    if key not in _ENGINE_NAMES:
        try:
            from app.engines.registry import detect_engine

            engine = detect_engine(key)
            _ENGINE_NAMES[key] = engine.label if engine is not None else key
        except Exception:  # noqa: BLE001 - 日志不能因为插件坏掉再抛一次
            _ENGINE_NAMES[key] = key
    return _ENGINE_NAMES[key]


def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _tc_text(ms: int) -> str:
    total = max(0, int(ms))
    return f"{total // 60000:02d}:{total % 60000 // 1000:02d}.{total % 1000:03d}"


def _params_text(params: dict[str, Any]) -> str:
    """角色参数压成一行：空值与内部分解出来的情绪不重复写。"""
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if key == "emotion" or value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = text.strip()
        if not text:
            continue
        parts.append(f"{key}={text[:28]}{'…' if len(text) > 28 else ''}")
    return " · ".join(parts)


def _sample_text(role: dict[str, Any]) -> str:
    mode = str(role.get("sample_mode") or "")
    if mode == "fixed":
        return f"固定样例 {role.get('sample_file') or '（未指定）'}"
    if mode == "emotion":
        return "按情绪匹配样例"
    return "主样例"


def _role_text(role: dict[str, Any]) -> str:
    """一段话里说清"谁、用什么声音、哪台引擎"。"""
    voice = str(role.get("voice_name") or role.get("voice_id") or "未选音色")
    return f"{role.get('name') or '旁白'} / {voice} / {_engine_name(str(role.get('engine') or ''))}"


def _cue_text(cue: dict[str, Any]) -> str:
    text = " ".join(str(cue.get("text") or "").split())
    short = f"{text[:18]}{'…' if len(text) > 18 else ''}"
    return (
        f"第 {cue.get('ordinal')} 段（{_tc_text(int(cue.get('start_ms') or 0))}–"
        f"{_tc_text(int(cue.get('end_ms') or 0))}）「{short}」"
    )
#: 一条命令行装得下的静音区间数。Windows 的命令行上限是 32767 字符，
#: 每个区间在滤镜表达式里占约 30 字符，留一倍余量后按 300 个一段分窗。
_MASK_RANGES_PER_CALL = 300
#: 字幕产物的扩展名。成品区据此把字幕与成片分开；重写字幕时也只清这些，
#: 不碰 out 里别的文件。
_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt", ".lrc", ".txt", ".ass"})


def role_of(cue: dict[str, Any], roles: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """这一段归谁：段落没指定角色时归旁白（第一条角色）。

    空 ``role_id`` 的段落一律算旁白，**不是**"随便找一条会配音的角色"。
    旁白选原声时必须真的不合成——按后一种理解，这些段会被别人的音色念一遍。
    """
    if not roles:
        return None
    wanted = str(cue.get("role_id") or "")
    for role in roles:
        if role["id"] == wanted:
            return role
    return roles[0]


def is_dubbed(cue: dict[str, Any], roles: Sequence[dict[str, Any]]) -> bool:
    """这一段要不要重新配音：角色是配音模式即要。原声角色只保留源音频。"""
    role = role_of(cue, roles)
    return bool(role and role.get("mode") == "tts")


def _file_version(path: Path) -> str:
    """文件的版本号：重配写的是同一个路径，只能靠 mtime + 大小区分"这是新的一版"。"""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def _file_time(path: Path, *, precise: bool = False) -> str:
    """文件时间。``precise`` 用微秒：与段落时间戳比先后时，同一秒也要分得清。"""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
        return stamp.isoformat(timespec="microseconds" if precise else "seconds")
    except OSError:
        return ""


def _stamp(path: Path) -> str:
    """源文件的指纹：切出来的试听片段按它命名，换源或改切分后不复用旧片段。"""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_size:x}-{stat.st_mtime_ns:x}"


def _publish_media(scratch: Path, target: Path, *, attempts: int = 6) -> None:
    """媒体文件改名成正式名字；播放器占着目标时退避重试。"""
    for attempt in range(attempts):
        try:
            os.replace(scratch, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


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


# ---- 说话人分离（audio.cpp 的 diar 模型） -----------------------------------

#: 转录的段与说话轮次的重叠低于这个秒数不算命中：分离模型在段边界会多铺几帧。
_TURN_MIN_OVERLAP_S = 0.05


def _running_speaker_models() -> list[dict[str, Any]]:
    """运行中的说话人分离模型：模型白名单在 ``dubbing.diar``，这里只读状态。"""
    try:
        from app.usecase.producers import started_service

        service = started_service("audiocpp")
        if service is not None:
            return speaker_models.options(list(service.running_models()))
    except Exception:  # noqa: BLE001 - 读不到运行时就是没有可选模型
        return []
    return []


def _speaker_labels_from_turns(
    segments: Sequence[tuple[float, float]], turns: Sequence[dict[str, Any]]
) -> dict[int, int]:
    """把说话轮次折到切段上：每段取重叠时间最长的轮次的说话人。

    簇号按 ``speaker_id`` 首次出现的顺序排成 0,1,2…，界面从「说话人 1」数起。
    分离模型覆盖全部语音区，正常每段都有命中；边界抖动靠重叠时长最大值吸收。
    """
    order: dict[str, int] = {}
    labels: dict[int, int] = {}
    for index, (start, end) in enumerate(segments):
        best_speaker, best_overlap = "", 0.0
        for turn in turns:
            overlap = min(end, float(turn["end"])) - max(start, float(turn["start"]))
            if overlap > best_overlap:
                best_speaker, best_overlap = str(turn["speaker"]), overlap
        if best_overlap < _TURN_MIN_OVERLAP_S:
            continue
        if best_speaker not in order:
            order[best_speaker] = len(order)
        labels[index] = order[best_speaker]
    return labels


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
            latest = max(exports, key=lambda path: (path.stat().st_mtime, path.name))
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
        export_meta = sorted(
            (
                {
                    "name": item.name,
                    "size": item.stat().st_size,
                    "at": _file_time(item),
                }
                for item in files
                if item.name not in skip and item.suffix.lower() not in subtitle_suffixes
            ),
            # 同一秒导出的两份按名字兜底排序：-2 在原名之后，仍是最新的一份在前。
            key=lambda item: (item["at"], item["name"]),
            reverse=True,
        )
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
            sample_mode=str(payload.get("sample_mode") or ""),
            sample_file=str(payload.get("sample_file") or ""),
            params=payload.get("params") or {},
        )
        CHANNEL.info(
            f"新建角色：{role['name']}",
            source="dubbing",
            project=project_id,
            role=role["id"],
            engine=role["engine"],
            voice=role["voice_id"],
        )
        return role

    def update_role(self, project_id: str, role_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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

    # ---- 合成 -------------------------------------------------------------

    def start_synth(
        self,
        project_id: str,
        library: LibraryService,
        cue_ids: Sequence[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        roles = self.store.roles(project_id)
        cues = self._cues_out(project_id, roles)
        stats = self._stats(cues, roles)
        targets = self._synth_targets(project_id, cue_ids)
        if not targets:
            detail = "先转录，或检查角色的处理方式是否为「配音」且已选音色。"
            if cue_ids:
                detail = "选中的段落里没有可合成的：它们可能保留原声，或角色还没有音色。"
            raise ProducerError(f"没有需要合成的段落：{detail}")
        missing = 0 if cue_ids else stats["unvoiced"]
        job = self._spawn(
            project_id,
            "synth",
            len(targets),
            lambda job_id: self._synth(job_id, library, targets, force=force),
        )
        engines = sorted({str(item["_role"]["engine"]) for item in targets})
        voices = sorted(
            {f"{item['_role']['engine']}/{item['_role']['voice_id']}" for item in targets}
        )
        # 谁、用什么声音、哪台引擎、各多少段：开始前一次说清，日志里不必猜。
        groups: dict[tuple[str, str, str], int] = {}
        for item in targets:
            role = item["_role"]
            group = (
                str(role.get("name") or "旁白"),
                str(role.get("voice_name") or role.get("voice_id") or "未选音色"),
                _engine_name(str(role.get("engine") or "")),
            )
            groups[group] = groups.get(group, 0) + 1
        summary = "；".join(f"{a} / {b} / {c} ×{count}" for (a, b, c), count in groups.items())
        note = f" · 跳过 {missing} 段缺音色" if missing else ""
        self.store.update_job(
            job["id"],
            message=f"准备合成 {len(targets)} 段{note}",
        )
        CHANNEL.info(
            f"开始配音：{len(targets)} 段{note} · {summary}"
            f" · 并发 {self.settings.concurrency}{' · 强制重配' if force else ''}",
            source="dubbing",
            project=project["id"],
            job=job["id"],
            engines=engines,
            voices=voices,
            targets=len(targets),
            skipped=missing,
            concurrency=self.settings.concurrency,
            force=force,
        )
        return job

    def _synth_targets(
        self, project_id: str, cue_ids: Sequence[str] | None
    ) -> list[dict[str, Any]]:
        """要合成的段落：配音角色名下、有音色、文本非空；指定了就只做这些。

        保留原声的角色（旁白常是这种）名下的一段都不进这个清单——它们本身
        就是成片里的声音，重念一遍只会把原声盖掉。
        """
        roles = self.store.roles(project_id)
        wanted = set(cue_ids or [])
        out: list[dict[str, Any]] = []
        for cue in self.store.cues(project_id):
            if wanted and cue["id"] not in wanted:
                continue
            if not str(cue["text"] or "").strip():
                continue
            role = role_of(cue, roles)
            if role is None or role["mode"] != "tts" or not (role["engine"] and role["voice_id"]):
                continue
            out.append({**cue, "_role": role})
        return out

    def _identity_for(self, engine_key: str) -> str:
        """本机当前引擎身份；引擎未安装时返回空串。停用不影响身份。"""
        from app.usecase import engines as engines_uc
        from app.usecase.engines import engine_identity

        try:
            # 停用的引擎仍要按它的版本/模型算身份：旧音频不该因为开关一关
            # 就整批变“待重配”，重新启用后又自己好了。
            engine = engines_uc.list_engines().get(engine_key)
        except Exception:  # noqa: BLE001 - 身份读不出来不该挡住详情
            engine = None
        return engine_identity(engine)

    def _audio_key(
        self,
        cue: dict[str, Any],
        role: dict[str, Any],
        *,
        identity: str = "",
        with_sample: bool = True,
    ) -> str:
        """当前口径的合成键：文本 / 引擎身份 / 音色 / 参数 / 本段命中的读音。

        ``identity`` 由调用方按引擎缓存后传入；这里不查注册表，避免每段都
        重新枚举引擎。空的 identity 仍可算键（测试与旧数据）。
        """
        from app.usecase import pronunciation

        key_fields: dict[str, Any] = {
            "v": 2,
            "text": cue["text"],
            "engine": role["engine"],
            "identity": identity,
            "voice": role["voice_id"],
            "params": role["params"],
            "pron": pronunciation.matches(str(cue["text"] or "")),
        }
        if with_sample and role.get("sample_mode"):
            key_fields["sample"] = f"{role['sample_mode']}:{role.get('sample_file') or ''}"
        payload = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _legacy_audio_key(
        self, cue: dict[str, Any], role: dict[str, Any], *, with_sample: bool = True
    ) -> str:
        """历史口径：整张读音表版本、没有引擎身份。只用于认旧产物。"""
        from app.usecase import pronunciation

        key_fields: dict[str, Any] = {
            "text": cue["text"],
            "engine": role["engine"],
            "voice": role["voice_id"],
            "params": role["params"],
            "pron": pronunciation.revision(),
        }
        if with_sample and role.get("sample_mode"):
            key_fields["sample"] = f"{role['sample_mode']}:{role.get('sample_file') or ''}"
        payload = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _key_matches(
        self,
        cue: dict[str, Any],
        role: dict[str, Any],
        *,
        identity: str | None = None,
    ) -> bool:
        """这条旧音频是不是当前角色参数的产物。

        新键带引擎身份与本段读音命中；旧键只带整张读音表版本。两种都认，
        旧项目不会因为升级判据整批作废；没有样例策略的角色再认一次不带
        样例格的旧键。
        """
        stored = str(cue.get("audio_key") or "")
        if not stored:
            return False
        if identity is None:
            identity = self._identity_for(str(role.get("engine") or ""))
        if stored == self._audio_key(cue, role, identity=identity):
            return True
        if stored == self._legacy_audio_key(cue, role):
            return True
        # 没有样例策略的角色才认不带样例格的旧键：用户一旦明确选了
        # 「按情绪 / 固定样例」，这一段就必须按新策略重配。
        return not role.get("sample_mode") and stored == self._legacy_audio_key(
            cue, role, with_sample=False
        )

    def _synth(
        self,
        job_id: str,
        library: LibraryService,
        targets: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> str:
        job = self.store.job(job_id)
        assert job is not None
        project_id = job["project_id"]
        self._project(project_id)
        work = self._work(project_id, "tts")
        concurrency = max(1, int(self.settings.concurrency))
        started = time.monotonic()

        async def run() -> tuple[int, int, int, int, int, int, str, dict[str, dict[str, Any]]]:
            semaphore = asyncio.Semaphore(concurrency)
            done = 0
            failed = 0
            blocked = 0
            produced = 0
            reused = 0
            written_bytes = 0
            first_error = ""
            lock = asyncio.Lock()
            # 整台做不了就停这一台：服务没起、引擎被停用、插件已卸载。
            # 后面每一段都只是白撞一次，还会把段落状态刷成满屏失败。
            halted: dict[str, dict[str, Any]] = {}
            identities: dict[str, str] = {}

            async def one(cue: dict[str, Any]) -> None:
                nonlocal done, failed, blocked, produced, reused, written_bytes
                nonlocal first_error
                if self._cancelled(job_id):
                    return
                role = cue["_role"]
                engine_key = str(role["engine"])
                engine_name = _engine_name(engine_key)
                sample_mode = str(role.get("sample_mode") or "")
                sample_file = (
                    str(role.get("sample_file") or "") if sample_mode == "fixed" else ""
                )
                cue_pos = _cue_text(cue)
                role_text = _role_text(role)
                if engine_key in halted:
                    async with lock:
                        blocked += 1
                    return
                if engine_key not in identities:
                    identities[engine_key] = self._identity_for(engine_key)
                identity = identities[engine_key]
                key = self._audio_key(cue, role, identity=identity)
                if not force and cue["audio"] and self._key_matches(
                    cue, role, identity=identity
                ):
                    existing = self.folder(project_id) / cue["audio"]
                    if existing.is_file():
                        # 键没变、文件还在：这一段本来就是好的。上一轮失败留下的
                        # failed 标记要在这里擦掉，否则它永远停在"失败"上。
                        was_failed = cue["state"] == "failed" or bool(cue["error"])
                        if cue["state"] != "ready" or cue["error"]:
                            self.store.update_cue(cue["id"], state="ready", error="")
                        if was_failed:
                            CHANNEL.info(
                                f"{cue_pos} 复用已有配音：{role_text} · "
                                f"{_size_text(existing.stat().st_size)}；"
                                "上一轮的失败标记已清掉。",
                                source="dubbing",
                                project=project_id,
                                job=job_id,
                                cue=cue["id"],
                                engine=engine_key,
                                voice=role["voice_id"],
                            )
                        else:
                            CHANNEL.debug(
                                f"{cue_pos} 跳过合成：文本、音色与样例都没变，"
                                f"复用 {_size_text(existing.stat().st_size)} 的旧产物。",
                                source="dubbing",
                                project=project_id,
                                job=job_id,
                                cue=cue["id"],
                                engine=engine_key,
                                voice=role["voice_id"],
                            )
                        async with lock:
                            done += 1
                            reused += 1
                            self.store.update_job(job_id, done=done, failed=failed)
                        return
                    CHANNEL.debug(
                        f"{cue_pos} 的旧音频文件已不在磁盘上（{cue['audio']}），"
                        "按新配音重新合成。",
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                    )
                else:
                    CHANNEL.debug(
                        f"{cue_pos} 需要重配："
                        + ("用户指定了强制重配。" if force else "文本、角色、音色或样例已变。"),
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                        voice=role["voice_id"],
                    )
                async with semaphore:
                    # 排队期间可能已有别的段撞上「连不上」或用户点了取消。
                    if self._cancelled(job_id):
                        return
                    if engine_key in halted:
                        async with lock:
                            blocked += 1
                        return
                    params = dict(role.get("params") or {})
                    emotion = str(params.pop("emotion", "") or "")
                    started_cue = time.monotonic()
                    try:
                        from app.usecase import gateway

                        result = await gateway.synthesize(
                            library,
                            engine_key=engine_key,
                            voice_id=role["voice_id"],
                            text=cue["text"],
                            emotion=emotion,
                            extra=params or None,
                            sample_file=sample_file,
                            match_emotion=sample_mode == "emotion",
                            log_failure=False,
                        )
                        suffix = (result.format or "wav").lower()
                        raw = work / f"{cue['id']}.{suffix}"
                        raw.write_bytes(result.audio)
                        # 中间件统一 44.1k 单声道 PCM：各家引擎的采样率不同，
                        # 不统一就没法按时间轴拼。
                        wav = self._to_middle(raw, cue["id"])
                        rel = wav.relative_to(self.folder(project_id)).as_posix()
                    except EngineError as exc:
                        if exc.code in CODES_ENGINE_UNAVAILABLE:
                            async with lock:
                                entry = halted.get(engine_key)
                                if entry is None:
                                    entry = halted[engine_key] = {
                                        "reason": exc.reason,
                                        "label": engine_name,
                                        "rows": 0,
                                    }
                                    CHANNEL.warning(
                                        f"{cue_pos} 撞上引擎不可用：{role_text} · "
                                        f"{engine_name}（{engine_key}）· {exc.reason}；"
                                        "这台引擎的剩余段落停下，其他引擎继续。",
                                        source="dubbing",
                                        project=project_id,
                                        job=job_id,
                                        engine=engine_key,
                                        voice=role["voice_id"],
                                        cue=cue["id"],
                                    )
                                entry["rows"] += 1
                                blocked += 1
                            return
                        kept = "；旧音频保留，仍可试听。" if cue["audio"] else "；这一段没有旧音频。"
                        async with lock:
                            failed += 1
                            if not first_error:
                                first_error = exc.reason
                            self.store.update_cue(
                                cue["id"], state="failed", error=exc.reason
                            )
                            self.store.update_job(job_id, done=done, failed=failed)
                        CHANNEL.warning(
                            f"{cue_pos} 合成失败：{role_text} · "
                            f"{len(cue['text'])} 字 · "
                            f"引擎返回 {exc.code or '错误'}：{exc.reason}{kept}",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            engine=engine_key,
                            voice=role["voice_id"],
                            cue=cue["id"],
                            code=exc.code,
                        )
                        return
                    except Exception as exc:  # noqa: BLE001
                        # 这一步失败 = 真的没合成出来：标 failed，旧产物仍在也能放。
                        async with lock:
                            failed += 1
                            if not first_error:
                                first_error = str(exc)
                            self.store.update_cue(
                                cue["id"], state="failed", error=str(exc)
                            )
                            self.store.update_job(job_id, done=done, failed=failed)
                        kept = (
                            "；旧音频保留，仍可试听。"
                            if cue["audio"]
                            else "；这一段没有旧音频。"
                        )
                        CHANNEL.warning(
                            f"{cue_pos} 合成失败：{role_text} · "
                            f"{len(cue['text'])} 字 · {exc}{kept}",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            engine=engine_key,
                            voice=role["voice_id"],
                            cue=cue["id"],
                        )
                        return
                    # 音频已经落盘，只有"记账"可能失败：写不进去是库的问题，不是合成
                    # 的问题。以前把它算成 failed，界面就出现"显示失败、播放却是对的"。
                    # 合成期间用户改过字或角色时，产物已经不是当前参数的产物：
                    # 照写音频，但状态记成过期；改回去时键值仍对得上。
                    latest = self.store.cue(cue["id"])
                    if latest is None:
                        wav.unlink(missing_ok=True)
                        return
                    role_now = role_of(latest, self.store.roles(project_id))
                    current = bool(
                        role_now
                        and self._audio_key(
                            latest,
                            role_now,
                            identity=self._identity_for(
                                str(role_now.get("engine") or "")
                            ),
                        )
                        == key
                    )
                    try:
                        self.store.update_cue(
                            cue["id"],
                            audio=rel,
                            audio_key=key,
                            state="ready" if current else "stale",
                            error="",
                        )
                    except Exception as exc:  # noqa: BLE001
                        CHANNEL.warning(
                            f"{cue_pos} 的配音产物已写出（{_size_text(wav.stat().st_size)}），"
                            f"但没记进库：{exc}；界面重进后会按文件重新认。",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            cue=cue["id"],
                        )
                    elapsed_cue = time.monotonic() - started_cue
                    size = wav.stat().st_size
                    hints: list[str] = []
                    if not current:
                        hints.append("合成期间段落已改动，产物标为过期")
                    if emotion:
                        hints.append(f"情绪 {emotion}")
                    extra_note = _params_text(params)
                    if extra_note:
                        hints.append(extra_note)
                    hints.append(_sample_text(role))
                    if result.cost_note:
                        hints.append(result.cost_note)
                    for note in result.notes:
                        hints.append(f"引擎提示：{note}")
                    for _code, text in result.prep_notes:
                        hints.append(f"已自动处理：{text}")
                    CHANNEL.info(
                        f"{cue_pos} 合成完成：{role_text} · {len(cue['text'])} 字 · "
                        f"{elapsed_cue:.2f}s · {_size_text(size)}"
                        + (f" · {' · '.join(hints)}" if hints else ""),
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                        voice=role["voice_id"],
                        sample_mode=sample_mode,
                        sample_file=sample_file,
                        elapsed=round(elapsed_cue, 2),
                        bytes=size,
                    )
                    async with lock:
                        done += 1
                        produced += 1
                        written_bytes += size
                        self.store.update_job(job_id, done=done, failed=failed)

            tasks = [asyncio.create_task(one(cue)) for cue in targets]
            for index, task in enumerate(asyncio.as_completed(tasks), start=1):
                await task
                if self._cancelled(job_id):
                    for pending in tasks:
                        pending.cancel()
                    break
                self.store.update_job(
                    job_id,
                    done=done,
                    failed=failed,
                    message=f"合成 {index} / {len(targets)}",
                )
            return done, failed, blocked, produced, reused, written_bytes, first_error, halted

        (
            done,
            failed,
            blocked,
            produced,
            reused,
            written_bytes,
            first_error,
            halted,
        ) = asyncio.run(run())
        elapsed = time.monotonic() - started
        self.store.update_project(project_id, status="synthesized")
        parts = [f"新配 {produced} 段"]
        if reused:
            parts.append(f"复用 {reused} 段")
        if failed:
            parts.append(f"失败 {failed} 段")
        if blocked:
            for entry in halted.values():
                parts.append(f"{entry['label']} 跳过 {entry['rows']} 段：{entry['reason']}")
        if written_bytes:
            parts.append(f"产物 {_size_text(written_bytes)}")
        message = f"{' · '.join(parts)} · {elapsed:.1f}s"
        if done == 0 and (failed or blocked):
            reason = first_error or next(
                (entry["reason"] for entry in halted.values()), "全部段落合成失败。"
            )
            CHANNEL.error(
                f"配音没有完成：{message}",
                source="dubbing",
                project=project_id,
                job=job_id,
                done=done,
                failed=failed,
                blocked=blocked,
                elapsed=round(elapsed, 1),
            )
            raise ProducerError(reason)
        CHANNEL.info(
            f"配音完成：{message}",
            source="dubbing",
            project=project_id,
            job=job_id,
            done=done,
            failed=failed,
            blocked=blocked,
            produced=produced,
            reused=reused,
            bytes=written_bytes,
            elapsed=round(elapsed, 1),
        )
        self.store.update_job(
            job_id,
            done=done,
            failed=failed,
            message=message,
        )
        return message

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

    # ---- 拼装 / 混音 / 导出 ----------------------------------------------

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
        windows = max(1, math.ceil(len(ranges) / _MASK_RANGES_PER_CALL))
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
