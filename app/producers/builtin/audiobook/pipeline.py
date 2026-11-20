"""有声书流水线：分章 → 拆句 → 分析 → 生成 → 合并。

每一步都只读写本插件的数据目录；分析走宿主对话模型，合成走宿主引擎网关，
日志走宿主统一通道（``app.infra.loghub``）。插件不持有任何外部服务配置。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.analyze_stage import AnalyzeStage
from app.producers.builtin.audiobook.audio_stage import (
    AudioPlan,
    AudioStage,
    row_key,
    voice_identity,
)
from app.producers.builtin.audiobook.loudness_stage import LoudnessStage
from app.producers.builtin.audiobook.merge_stage import MergeStage
from app.producers.builtin.audiobook.naming import folder_name, safe_name
from app.producers.builtin.audiobook.paths import DirListing, abs_path
from app.producers.builtin.audiobook.patterns import (
    check_conservation,
    count_chars,
    split_by_pattern,
    text_payload,
)
from app.producers.builtin.audiobook.rows import (
    emotion_groups,
    emotion_words,
    normalize_newlines,
    row_out,
)
from app.producers.builtin.audiobook.rules import (
    DIALOGUE,
    NARRATOR,
)
from app.producers.builtin.audiobook.settings import AudiobookSettings
from app.producers.builtin.audiobook.store import AudiobookStore
from app.producers.builtin.audiobook.text_stage import TextStage
from app.usecase import pronunciation

__all__ = ["NARRATOR", "AudiobookService"]

#: 任务心跳间隔。另一个实例（或重启后的自己）靠它判断"这个任务还有人在推进"。
_HEARTBEAT_S = 5.0


class AudiobookService:
    def __init__(
        self,
        root: Path,
        settings: Callable[[], AudiobookSettings] | None = None,
    ) -> None:
        self.root = root
        self.store = AudiobookStore(root)
        #: 每次现读：设置页改完立即生效，不必重启。测试直接构造时用默认配置。
        self._settings = settings or AudiobookSettings
        #: 运行中的后台任务：事件循环只弱引用 task，这里必须自己持有。
        self._tasks: dict[str, asyncio.Task] = {}
        #: 每个任务的心跳协程；任务收尾时一起收掉。
        self._beats: dict[str, asyncio.Task] = {}
        #: 各阶段的实现：门面只做转发，阶段之间不互相持有。
        self.analysis = AnalyzeStage(self)
        self.text = TextStage(self)
        self.audio = AudioStage(self)
        self.output = MergeStage(self)
        self.loudness = LoudnessStage(self)
        #: 旧项目补目录（一次查询；迁移过的项目不会重复写）。
        self._migrate_layout()

    @property
    def settings(self) -> AudiobookSettings:
        return self._settings()

    @property
    def projects_root(self) -> Path:
        folder = self.settings.project_dir.strip()
        root = Path(folder).expanduser() if folder else self.root / "projects"
        root.mkdir(parents=True, exist_ok=True)
        return root

    # ---- 读取 -------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        return {
            "storage": str(self.projects_root),
            "projects": self.store.list_projects(),
            # 情绪候选由后端给：前端不写第二份词表。取值随用户词表走
            # （设置页「词表」：两组默认词 + 自定义、去隐藏）；`emotion_groups`
            # 是同一份词的分组摆法，下拉直接按它分组。
            "emotions": list(emotion_words()),
            "emotion_groups": emotion_groups(),
        }

    def _settle_stale(self, project_id: str, chapter_id: str = "") -> list[str]:
        """过期音频的复活：参数改回去了，就把「已生成」还给它。

        过期时既不删文件、也不清 ``audio_key``，所以这里只要按当前参数重算一次指纹：
        对得上（文件也还在）就是同一次合成的产物，直接恢复；对不上就继续当旧音频留着。
        改回原角色 / 情绪 / 文本 / 参数 / 音色都走这一条，不用重新生成。

        返回被恢复的章 id（调用方据此把章的阶段往前推一格）。
        """
        rows = self.store.stale_rows(project_id, chapter_id)
        if not rows:
            return []
        from app.usecase import engines as engines_uc

        voices = self.store.voice_map(project_id)
        chapter_voices = self.chapter_voice_rows(project_id)
        words = set(emotion_words())
        revision = pronunciation.revision()
        identities: dict[str, str] = {}
        # 项目根取一次；目录也只列一遍（见 :class:`_DirListing`）。
        root = self.project_folder(project_id)
        files = DirListing(root)
        good: list[str] = []
        chapters: set[str] = set()
        for row in rows:
            mapping = self.mapping_for(row, voices, chapter_voices)
            if not mapping or not mapping.get("voice_id"):
                continue
            engine_key = str(mapping["engine"])
            identity = identities.get(engine_key)
            if identity is None:
                engine = engines_uc.detect_engine(engine_key)
                identity = voice_identity(engine) if engine is not None else ""
                identities[engine_key] = identity
            # 按这一行当初的指纹口径重算：老行用 v1，新行用 v2。
            key = row_key(
                row,
                mapping,
                words=words,
                identity=identity,
                version=int(row.get("audio_key_v") or 1),
                revision=revision,
            )
            if key != row["audio_key"]:
                continue
            if not files.has(row["audio"]):
                continue
            good.append(str(row["id"]))
            chapters.add(str(row["chapter_id"]))
        self.store.mark_rows_audio(good)
        if not chapters:
            return []
        # 章阶段只认行统计：``chapters()`` 已经带着 ``rows`` / ``done`` 两列，
        # 不必为「这一章齐了没」把每章的行再读一遍（旧写法每章两次查询 + 整章对象）。
        stages = {str(item["id"]): item for item in self.store.chapters(project_id)}
        ready: list[str] = []
        for cid in sorted(chapters):
            chapter = stages.get(str(cid))
            if not chapter or chapter["stage"] == "merged":
                continue
            total = int(chapter["rows"] or 0)
            if total and int(chapter["done"] or 0) == total and chapter["stage"] != "audio":
                ready.append(str(cid))
        self.store.set_chapters_stage(ready, "audio")
        return sorted(chapters)

    def chapter_voice_rows(self, project_id: str) -> dict[str, dict[str, str]]:
        """按章指定的旁白音色；这本书已有具名角色时不用它（分配表那时按角色走）。

        这样一张分配表只有一种读法：有角色就按角色，没有就按章；不会出现
        「改的是角色，发的是别的章」这种看不见的覆盖。
        """
        if self.store.has_named_speakers(project_id):
            return {}
        return self.store.chapter_voice_map(project_id)

    def mapping_for(
        self,
        row: dict[str, Any],
        voices: dict[str, dict[str, str]],
        chapter_voices: dict[str, dict[str, str]],
    ) -> dict[str, str] | None:
        """这一行实际使用的音色：章级音色生效时按章，否则按说话人。

        章级音色只在书里没有具名角色时生效（``chapter_voice_rows`` 已把关），
        所以此时整章的行都归这一颗，包括拆句留下的「对话」占位行。
        """
        if chapter_voices:
            override = chapter_voices.get(str(row.get("chapter_id") or ""))
            if override and override.get("voice_id"):
                return override
        return voices.get(row["speaker"] or NARRATOR)

    def detail(self, project_id: str) -> dict[str, Any]:
        # 先结算过期音频：改回去的行在这里就恢复成「已生成」，统计不会虚报待生成。
        self._settle_stale(project_id)
        # 一次性读完：每条新连接第一次查询都要重读 schema，六条查询各开各的连接
        # 在 19 MB 的库上要多花三十多毫秒。
        with self.store.session() as conn:
            project = self.store.project(project_id, conn)
            if not project:
                raise ProducerError("项目不存在。")
            chapters = self.store.chapters(project_id, conn)
            # 「需校对」不在章节聚合的 SQL 里（它要看正文），只在详情这一处补进章节；
            # 章节栏的灯按它亮，但拆句 / 合并那些路径不必付这次扫描。
            broken = self.store.broken_counts(project_id, conn)
            chapter_roles = self.store.chapter_speakers(project_id, conn)
            speakers = self.store.speakers(project_id, conn)
            voices = self.store.voice_map(project_id, conn)
            chapter_voices = self.store.chapter_voice_map(project_id, conn)
            job = self.store.latest_job(project_id, conn)
        for chapter in chapters:
            chapter["broken"] = broken.get(str(chapter["id"]), 0)
            chapter["speakers"] = chapter_roles.get(str(chapter["id"]), [])
        done = sum(1 for chapter in chapters if chapter["stage"] == "merged")
        source_text = str(project.get("source") or "")
        return {
            # `source` 是整本书原文、`text` 是整章正文：界面都用不上，
            # 放进响应会把首屏撑到十几 MB（45k 行的书实测如此）。
            **{
                key: value
                for key, value in project.items()
                if key not in ("source", "book_audio", "book_key")
            },
            # 原文只给统计；正文本身由「分章」面板单独取一次。
            "source": {
                "chars": count_chars(source_text),
                "paragraphs": sum(1 for line in source_text.splitlines() if line.strip()),
            },
            "chapters": [
                {key: value for key, value in chapter.items() if key != "text"}
                for chapter in chapters
            ],
            "speakers": speakers,
            "voices": voices,
            "chapter_voices": chapter_voices,
            "split_options": self._split_options_payload(project),
            # 最近一次任务：刷新页面后据此继续显示进度。
            "job": job,
            "stats": {
                "chapters": len(chapters),
                "merged": done,
                "rows": sum(int(chapter["rows"] or 0) for chapter in chapters),
                "analyzed": sum(int(chapter["analyzed"] or 0) for chapter in chapters),
                "dialogue": sum(int(chapter["dialogue"] or 0) for chapter in chapters),
                "audio": sum(int(chapter["done"] or 0) for chapter in chapters),
                # 四个"要处理"的口径，章节灯与行表筛选共用。前三个互斥；
                # 「需校对」是另一维，可能与它们重叠，不能相加当总数。
                "unassigned": sum(int(chapter["unassigned"] or 0) for chapter in chapters),
                "failed": sum(int(chapter["failed"] or 0) for chapter in chapters),
                "pending": sum(int(chapter["pending"] or 0) for chapter in chapters),
                "broken": sum(int(chapter["broken"] or 0) for chapter in chapters),
                # 待生成里"有旧音频、只是不算数"的那部分：改回去能自己恢复。
                "stale": sum(int(chapter["stale"] or 0) for chapter in chapters),
            },
        }

    def running_job(self, project_id: str) -> dict[str, Any] | None:
        # 上一个进程崩溃时来不及写终态：先把"确实没人推进"的那批收掉，再回答。
        # 另一个实例正在跑时心跳新鲜、进程也在，这里不会动它。
        self.store.reclaim_jobs()
        job = self.store.latest_job(project_id)
        if job and job["state"] in ("running", "cancelling"):
            return job
        return None

    def _ensure_idle(self, project_id: str) -> None:
        job = self.running_job(project_id)
        if job:
            raise ProducerError(f"已有任务在运行（{job['kind']}），等它结束或取消后再试。")

    def marks(self, project_id: str) -> dict[str, Any]:
        """本项目的语气词分组：通用标签、各引擎多出来的，以及角色→引擎。"""
        from app.usecase import engines as engines_uc

        payload = engines_uc.marks_payload()
        payload["engine_marks"] = {
            key: sorted(engine.mark_map())
            for key, engine in engines_uc.active_engines().items()
        }
        voices = self.store.voice_map(project_id)
        payload["speaker_engine"] = {
            speaker: item["engine"] for speaker, item in voices.items() if item.get("voice_id")
        }
        return payload

    def chapter_rows(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        # 与 detail 同一件事，范围收到本章：改回去的行在返回前就该是「已生成」。
        self._settle_stale(project_id, chapter_id)
        # 查一次就够：以前先查一次「这章在不在」，返回时再查一次拿最新阶段。
        chapter = self.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        # 项目根只取一次：每行都去查一次库，一百行就是大半秒的空白。
        root = self.project_folder(project_id)
        loudness_plan = self.loudness.plan(root) if self.settings.row_balance_enabled else {}
        # 单章正文跟着走：没拆句之前界面要能看「这一章是什么」，拆完以行表为准。
        return {
            "chapter": chapter,
            "rows": [
                self._row_view(project_id, row, root, loudness_plan)
                for row in self.store.rows(chapter_id)
            ],
        }

    # ---- 分章 -------------------------------------------------------------

    def create(self, name: str, text: str) -> dict[str, Any]:
        """粘贴/新建：只落原文，不替用户决定怎么切——分章是下一步的事。"""
        title = (name or "").strip() or "未命名项目"
        if not (text or "").strip():
            raise ProducerError("源文本为空。")
        return self._create(title, text, [])

    def import_book(self, filename: str, data: bytes, title: str = "") -> dict[str, Any]:
        """上传的书稿或字幕：按扩展名解析后建项目。

        - 书稿（txt / md / epub / mobi / azw3）：带章节的（epub / mobi / KF8）直接成章，
          其余只落原文，等用户在「分章」里选模式切；
        - 字幕（.srt）：**单章项目**，一条字幕一行、原时间码保留（``kind='subtitle'``）。
          时间码是硬约束，所以不分章也不拆句——合并时按时间轴拼，见 :meth:`merge`。
        """
        from app.producers.builtin.audiobook.ebooks import read_book
        from app.producers.builtin.audiobook.subtitles import SUPPORTED as SUBTITLES
        from app.producers.builtin.audiobook.subtitles import read_subtitle

        if Path(filename.lower()).suffix in SUBTITLES:
            guessed, cues, skipped = read_subtitle(filename, data)
            return self._create_subtitle(
                (title or "").strip() or guessed, filename, data, cues, skipped
            )
        guessed, source, chapters = read_book(filename, data)
        return self._create((title or "").strip() or guessed, source, chapters)

    def _create_subtitle(
        self,
        title: str,
        filename: str,
        data: bytes,
        cues: list[Any],
        skipped: int,
    ) -> dict[str, Any]:
        """字幕项目：一章 + 带时间码的行（说话人按标记填，没有就留「对话」给分析认）。"""
        text = "\n".join(str(cue.text) for cue in cues)
        project_id = self.store.create_project(title, text, [("全片", text)], kind="subtitle")
        folder = self._unique_project_folder(title)
        folder.mkdir(parents=True, exist_ok=True)
        self.store.set_project_folder(project_id, str(folder))
        self._write_source_file(project_id, title, text)
        self._write_raw_source(project_id, title, filename, data)
        self._rewrite_chapter_files(project_id)
        self.store.set_layout_version(project_id, 1)
        chapter = self.store.chapters(project_id)[0]
        self.store.replace_rows(
            chapter["id"],
            [
                {
                    "text": str(cue.text),
                    "kind": "dialogue",
                    # 有标记就用标记；没有就留「对话」——分析那一步会认人
                    "speaker": str(cue.speaker) or DIALOGUE,
                    "start_ms": int(cue.start_ms),
                    "end_ms": int(cue.end_ms),
                }
                for cue in cues
            ],
        )
        # 行已经在库里了：这一步等于「拆句完成」，下一步是分析/配声
        self.store.set_chapter_stage(chapter["id"], "sentences")
        CHANNEL.info(
            f"字幕项目已导入：{title} · {len(cues)} 条"
            f"{f' · 跳过 {skipped} 条坏块' if skipped else ''} · {folder}",
            source="audiobook",
            project=project_id,
        )
        return self.detail(project_id)

    def _write_raw_source(self, project_id: str, title: str, filename: str, data: bytes) -> None:
        """原始文件另存一份（字幕的标签/时间码原样保留，导入时剥掉的那些都还在）。"""
        suffix = Path(filename).suffix.lower() or ".srt"
        target = self.project_folder(project_id) / "source" / f"{safe_name(title, '字幕')}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _create(
        self, title: str, source: str, chapters: list[tuple[str, str]]
    ) -> dict[str, Any]:
        project_id = self.store.create_project(title, source, chapters)
        folder = self._unique_project_folder(title)
        folder.mkdir(parents=True, exist_ok=True)
        self.store.set_project_folder(project_id, str(folder))
        self._write_source_file(project_id, title, source)
        self._rewrite_chapter_files(project_id)
        self.store.set_layout_version(project_id, 1)
        CHANNEL.info(
            f"有声书项目已创建：{title} · {len(chapters)} 章 · {folder}",
            source="audiobook",
            project=project_id,
        )
        return self.detail(project_id)

    # ---- 落盘（库是权威，盘上的 txt 是给人看的副本） ----------------------

    def _write_source_file(self, project_id: str, title: str, source: str) -> None:
        """源稿副本：`source/<书名>.txt`。空原文（不该出现）就跳过。"""
        text = str(source or "")
        if not text.strip():
            return
        folder = self.project_folder(project_id) / "source"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{safe_name(title, '书稿')}.txt"
        target.write_text(normalize_newlines(text), encoding="utf-8")

    def _chapter_path(self, project_id: str, chapter: dict[str, Any]) -> Path:
        name = f"{int(chapter['ordinal']) + 1:03d}_{safe_name(chapter['title'], '章节')}.txt"
        return self.project_folder(project_id) / "chapters" / name

    def _write_chapter_file(self, project_id: str, chapter: dict[str, Any]) -> None:
        target = self._chapter_path(project_id, chapter)
        target.parent.mkdir(parents=True, exist_ok=True)
        title = str(chapter["title"] or "").strip()
        body = normalize_newlines(str(chapter["text"] or "")).strip()
        content = f"{title}\n\n{body}\n" if body else f"{title}\n"
        target.write_text(content, encoding="utf-8")

    def _rewrite_chapter_files(self, project_id: str) -> None:
        """整目录重写：删章、改标题会动序号与文件名，一次重写最稳。"""
        folder = self.project_folder(project_id) / "chapters"
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob("*.txt"):
            stale.unlink(missing_ok=True)
        for chapter in self.store.chapters(project_id):
            self._write_chapter_file(project_id, chapter)

    def _migrate_layout(self) -> None:
        """旧项目一次性补齐目录：source/ 与 chapters/*.txt，目录名改成书名。"""
        for project in self.store.projects_pending_layout():
            project_id = str(project["id"])
            try:
                self._layout_project(project)
            except OSError as exc:
                CHANNEL.warning(
                    f"项目目录迁移失败：{project['name']} · {exc}",
                    source="audiobook",
                    project=project_id,
                )
                continue

    def _layout_project(self, project: dict[str, Any]) -> None:
        project_id = str(project["id"])
        folder = Path(str(project["folder"] or "")) if project["folder"] else None
        if folder is None or not folder.is_dir():
            folder = self._unique_project_folder(str(project["name"]))
            self.store.set_project_folder(project_id, str(folder))
        if folder.name == project_id:
            # 老目录是项目号：先折相对路径，再换成书名目录。
            self.store.relativize_audio(project_id, folder)
            target = self._unique_project_folder(str(project["name"]))
            folder.rename(target)
            self.store.set_project_folder(project_id, str(target))
            folder = target
        folder.mkdir(parents=True, exist_ok=True)
        self._write_source_file(project_id, str(project["name"]), str(project["source"] or ""))
        self._rewrite_chapter_files(project_id)
        self.store.set_layout_version(project_id, 1)
        CHANNEL.info(
            f"项目目录已迁移：{project['name']} · {folder}",
            source="audiobook",
            project=project_id,
        )

    def _unique_project_folder(self, title: str) -> Path:
        """项目目录用书名，不用户号：用户要在资源管理器里一眼认出来。"""
        base = self.projects_root
        stem = folder_name(title)
        candidate = base / stem
        for number in range(2, 500):
            if not candidate.exists():
                return candidate
            candidate = base / f"{stem} ({number})"
        raise ProducerError(f"「{stem}」在 {base} 下同名目录过多，先清理一下。")

    # ---- 原文与分章 -------------------------------------------------------

    def patterns(self) -> dict[str, Any]:
        """分章模式与文本处理预设：与界面同一份正则表。"""
        return text_payload()

    def _require_project(self, project_id: str) -> dict[str, Any]:
        project = self.store.project(project_id)
        if not project:
            raise ProducerError("项目不存在。")
        return project

    def source_payload(self, project_id: str) -> dict[str, Any]:
        """原文正文 + 统计。3 MB 的正文只在打开分章面板时取这一次。"""
        project = self._require_project(project_id)
        text = str(project.get("source") or "")
        return {
            "text": text,
            "chars": count_chars(text),
            "paragraphs": sum(1 for line in text.splitlines() if line.strip()),
            "pattern_key": str(project.get("pattern_key") or ""),
        }

    def preview_split(self, project_id: str, pattern_key: str) -> dict[str, Any]:
        """按模式试切一遍：只报结果，不改库。"""
        project = self._require_project(project_id)
        source = str(project.get("source") or "")
        if not source.strip():
            raise ProducerError("这本书没有原文，无法分章。")
        result = split_by_pattern(source, pattern_key)
        return {
            "pattern_key": result.pattern_key,
            "pattern_name": result.pattern_name,
            "matched": result.matched,
            "count": result.count,
            "warnings": list(result.warnings),
            "chapters": [
                {"title": title, "chars": count_chars(body)}
                for title, body in result.chapters[:30]
            ],
            "conservation": check_conservation(source, list(result.chapters)),
        }

    def split_project(self, project_id: str, pattern_key: str) -> dict[str, Any]:
        """真分章：整本换掉。旧行、旧音频、旧成品全部作废。"""
        self._ensure_idle(project_id)
        project = self._require_project(project_id)
        source = str(project.get("source") or "")
        if not source.strip():
            raise ProducerError("这本书没有原文，无法分章。")
        result = split_by_pattern(source, pattern_key)
        if not result.chapters:
            raise ProducerError("没有切出章节：正文是空的。")
        self.store.replace_chapters(project_id, list(result.chapters))
        self.store.set_project_pattern(project_id, result.pattern_key)
        self._clear_derived(project_id)
        self._rewrite_chapter_files(project_id)
        CHANNEL.info(
            f"分章完成：{result.count} 章 · {result.pattern_name}",
            source="audiobook",
            project=project_id,
        )
        return self.detail(project_id)

    def _clear_derived(self, project_id: str) -> None:
        """原文或章节结构变了：逐行音频与成品都是派生物，整目录清掉。"""
        folder = self.project_folder(project_id)
        for name in ("audio", "merged"):
            target = folder / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)

    # ---- 章节生命周期 -----------------------------------------------------

    def delete_chapter(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        """删一章：行、音频目录一起清掉，后面的章序号补齐。"""
        self._ensure_idle(project_id)
        chapter = self.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        rows = len(self.store.rows(chapter_id))
        self.store.delete_chapter(chapter_id)
        self.store.renumber_chapters(project_id)
        audio = self.project_folder(project_id) / "audio" / chapter_id
        if audio.is_dir():
            shutil.rmtree(audio, ignore_errors=True)
        self._rewrite_chapter_files(project_id)
        CHANNEL.info(
            f"删除章节：{chapter['title']} · 连同 {rows} 行",
            source="audiobook",
            project=project_id,
        )
        return self.detail(project_id)

    def update_project(
        self,
        project_id: str,
        name: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        """改书名与作者；只传要改的那一项，另一项保持原样。"""
        title = None if name is None else (name or "").strip()
        if title is not None and not title:
            raise ProducerError("项目名不能为空。")
        if not self.store.project_meta(project_id):
            raise ProducerError("项目不存在。")
        self.store.update_project_profile(
            project_id,
            name=title,
            author=None if author is None else (author or "").strip(),
        )
        return self.detail(project_id)

    def rename_project(self, project_id: str, name: str) -> dict[str, Any]:
        return self.update_project(project_id, name=name)

    def rename_chapter(self, project_id: str, chapter_id: str, title: str) -> dict[str, Any]:
        name = (title or "").strip()
        if not name:
            raise ProducerError("章节标题不能为空。")
        if not self.store.chapter(project_id, chapter_id):
            raise ProducerError("章节不存在。")
        self.store.rename_chapter(project_id, chapter_id, name)
        self._rewrite_chapter_files(project_id)
        return self.detail(project_id)


    # ---- 文本（实现见 text_stage）----------------------------------------

    def preview_text(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.text.preview_text(project_id=project_id, body=body)

    def apply_text(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.text.apply_text(project_id=project_id, body=body)

    def clear_chapter_rows(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        return self.text.clear_chapter_rows(project_id=project_id, chapter_id=chapter_id)

    def save_chapter_text(
        self, project_id: str, chapter_id: str, text: str
    ) -> dict[str, Any]:
        return self.text.save_chapter_text(project_id=project_id, chapter_id=chapter_id, text=text)

    def edit_row(
        self,
        project_id: str,
        row_id: str,
        *,
        speaker: str | None = None,
        emotion: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        return self.text.edit_row(project_id=project_id, row_id=row_id, speaker=speaker, emotion=emotion, text=text)

    def row_params(self, project_id: str, row_id: str) -> dict[str, Any]:
        return self.text.row_params(project_id=project_id, row_id=row_id)

    def save_row_params(
        self, project_id: str, row_id: str, params: Any
    ) -> dict[str, Any]:
        return self.text.save_row_params(project_id=project_id, row_id=row_id, params=params)

    def _split_options_payload(self, project: dict[str, Any]) -> dict[str, Any]:
        return self.text._split_options_payload(project=project)

    def split_options_meta(self) -> dict[str, Any]:
        return self.text.split_options_meta()

    def save_split_options(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.text.save_split_options(project_id=project_id, body=body)

    def split_sentences(self, project_id: str, scope: str = "all") -> dict[str, Any]:
        return self.text.split_sentences(project_id=project_id, scope=scope)

    def resplit_chapter(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        return self.text.resplit_chapter(project_id=project_id, chapter_id=chapter_id)

    def delete_row(self, project_id: str, row_id: str) -> dict[str, Any]:
        return self.text.delete_row(project_id=project_id, row_id=row_id)

    def split_row(self, project_id: str, row_id: str, at: int | None = None) -> dict[str, Any]:
        return self.text.split_row(project_id=project_id, row_id=row_id, at=at)

    def merge_rows(self, project_id: str, row_ids: list[str]) -> dict[str, Any]:
        return self.text.merge_rows(project_id=project_id, row_ids=row_ids)

    def auto_merge_rows(
        self, project_id: str, chapter_id: str, max_chars: int | None
    ) -> dict[str, Any]:
        return self.text.auto_merge_rows(
            project_id=project_id, chapter_id=chapter_id, max_chars=max_chars
        )

    def auto_merge_all(self, project_id: str, max_chars: int | None) -> dict[str, Any]:
        return self.text.auto_merge_all(project_id=project_id, max_chars=max_chars)

    def batch_update_rows(
        self,
        project_id: str,
        row_ids: list[str],
        *,
        speaker: str | None = None,
        emotion: str | None = None,
    ) -> dict[str, Any]:
        return self.text.batch_update_rows(project_id=project_id, row_ids=row_ids, speaker=speaker, emotion=emotion)

    def merge_speakers(self, project_id: str, names: list[str], target: str) -> dict[str, Any]:
        return self.text.merge_speakers(project_id=project_id, names=names, target=target)

    # ---- 生成 -------------------------------------------------------------

    def project_folder(self, project_id: str) -> Path:
        """项目根目录：建项目时记下的那一个；老库首次访问时补记到当前根。"""
        project = self.store.project_meta(project_id) or {}
        folder = str(project.get("folder") or "").strip()
        if folder:
            return Path(folder)
        path = self.projects_root / project_id
        self.store.set_project_folder(project_id, str(path))
        return path

    def resolve_audio(self, project_id: str, value: str) -> Path:
        """音频路径：新记录是相对项目根，老记录是绝对路径，两种都认。

        只查单条时用它；**遍历一批行**时先取一次项目根再走 :func:`_abs_path`，
        否则每行多一次 SQLite 查询。
        """
        return abs_path(self.project_folder(project_id), value)

    def _row_view(
        self, project_id: str, row: dict[str, Any], root: Path | None = None,
        loudness_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """给前端的行：参数摊平，再加上音频文件的写入时刻。

        文件名与 ``audio_key`` 都不随"重新生成"变（同名覆盖、同一套参数），
        播放地址若只认指纹，重新生成后点播放还在放旧的那一版。带上文件时刻：
        没重生成时地址稳定（同一段能暂停/继续），重生成后必变（装进新音频）。

        ``root`` 给整章 / 整批调用方传：省掉每行一次项目根查询。
        """
        view = row_out(row)
        view["audio_version"] = self._audio_version(project_id, row, root)
        view["loudness_note"] = "原始音频"
        if row.get("audio") and self.settings.row_balance_enabled:
            folder = root or self.project_folder(project_id)
            path, note = self.loudness.row_audio(folder, row, loudness_plan)
            view["loudness_note"] = note
            try:
                view["audio_version"] = path.stat().st_mtime_ns
            except OSError:
                view["audio_version"] = 0
        return view

    def _audio_version(
        self, project_id: str, row: dict[str, Any], root: Path | None = None
    ) -> int:
        """这一行音频的写入时刻（纳秒）；没有音频时为 0。"""
        if not row.get("audio"):
            return 0
        try:
            folder = root or self.project_folder(project_id)
            return int(abs_path(folder, row["audio"]).stat().st_mtime_ns)
        except OSError:
            return 0

    def _project_dir(self, project_id: str) -> Path:
        path = self.project_folder(project_id)
        path.mkdir(parents=True, exist_ok=True)
        return path







    async def _heartbeat(self, job_id: str) -> None:
        """任务活着就每 :data:`_HEARTBEAT_S` 秒写一次时间戳。

        没有它，另一个实例无法区分"这个任务还在跑"与"上一个进程崩了留下的"，
        只能靠启动时一律清空——那样第二个实例一开就把第一个的任务判死。
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            try:
                self.store.touch_job(job_id)
            except Exception:  # noqa: BLE001 - 心跳写不进去就算了，不拖垮任务
                return

    def _start_job(
        self,
        project_id: str,
        kind: str,
        total: int,
        runner: Callable[[str], Any],
    ) -> dict[str, Any]:
        job = self.store.create_job(project_id, kind, total)
        job_id = job["id"]

        async def guard() -> None:
            # 先让出一次：响应要先发出去，任务里的同步准备工作不该占着首字节。
            await asyncio.sleep(0.05)
            try:
                await runner(job_id)
            except asyncio.CancelledError:
                self.store.update_job(job_id, state="cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 任务失败要落到状态里
                self.store.update_job(job_id, state="failed", message=str(exc))
                CHANNEL.warning(
                    f"{kind} 任务失败：{exc}",
                    source="audiobook",
                    project=project_id,
                )
                return
            except BaseException as exc:  # 兜底，别把 job 留在 running
                self.store.update_job(job_id, state="failed", message=str(exc))
                raise
            cancelled = self.store.job_cancelled(job_id)
            self.store.update_job(job_id, state="cancelled" if cancelled else "done")

        def on_done(task: asyncio.Task) -> None:
            # 取一次异常：CancelledError / BaseException 两个分支会重新抛，
            # 没人检索时 asyncio 在 GC 阶段才打一条 "never retrieved"，真实故障会
            # 被埋在日志末尾；异常明细已写进 job 状态，这里补齐可见性。
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    CHANNEL.warning(
                        f"{kind} 任务异常终止：{error}",
                        source="audiobook",
                        project=project_id,
                    )
            self._job_done(job_id)

        task = asyncio.create_task(guard())
        self._tasks[job_id] = task
        task.add_done_callback(on_done)
        self._beats[job_id] = asyncio.create_task(self._heartbeat(job_id))
        return job

    def _job_done(self, job_id: str) -> None:
        """无论任务怎么结束都留下终态：卡在 running 会挡住该项目的全部新任务。"""
        self._tasks.pop(job_id, None)
        beat = self._beats.pop(job_id, None)
        if beat is not None:
            beat.cancel()
        try:
            job = self.store.job(job_id)
            if job and job["state"] in ("running", "cancelling"):
                self.store.update_job(job_id, state="failed", message="任务意外终止。")
        except Exception:  # noqa: BLE001 - 兜底路径不能再把异常抛回事件循环
            CHANNEL.warning("任务收尾失败", source="audiobook", project=job_id)

    # ---- 生成（实现见 audio_stage）---------------------------------------

    async def synthesize(
        self,
        project_id: str,
        library: Any,
        job_id: str = "",
        plan: tuple[
            list[dict[str, Any]],
            list[tuple[dict[str, Any], dict[str, Any], dict[str, str], str]],
            int,
            dict[str, int],
        ]
        | None = None,
    ) -> dict[str, Any]:
        return await self.audio.synthesize(project_id=project_id, library=library, job_id=job_id, plan=plan)

    async def retry_row(self, project_id: str, row_id: str, library: Any) -> dict[str, Any]:
        return await self.audio.retry_row(project_id=project_id, row_id=row_id, library=library)

    def prepare_audio(self, project_id: str) -> AudioPlan:
        return self.audio.prepare_audio(project_id=project_id)

    def start_audio(self, project_id: str, library: Any) -> dict[str, Any]:
        return self.audio.start_audio(project_id=project_id, library=library)

    def start_audio_planned(
        self, project_id: str, library: Any, plan: AudioPlan
    ) -> dict[str, Any]:
        return self.audio.start_audio_planned(project_id=project_id, library=library, plan=plan)

    # ---- 分析 -------------------------------------------------------------

    def start_analysis(self, project_id: str) -> dict[str, Any]:
        """按章分析：建任务前先校验有没有可分析的对白行。"""
        return self.analysis.start(project_id)

    async def analyze(self, project_id: str, job_id: str = "") -> dict[str, Any]:
        """执行一次分析（后台任务里跑的就是它）。"""
        return await self.analysis.run(project_id, job_id)

    def _apply_analysis(
        self,
        batch: list[dict[str, Any]],
        items: list[Any],
        words: set[str],
        fields: tuple[str, ...] = ("speaker", "emotion"),
    ) -> tuple[int, int, list[str]]:
        """写回一批标注：保留这个方法名，测试与阶段内部都用它。"""
        return self.analysis.apply_items(batch, items, words, fields)

    # ---- 成品（实现见 merge_stage）----------------------------------------

    def merge(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        return self.output.merge(project_id=project_id, chapter_id=chapter_id)

    def chapter_audio(self, project_id: str, chapter_id: str) -> Path:
        return self.output.chapter_audio(project_id=project_id, chapter_id=chapter_id)

    def chapter_subtitle(self, project_id: str, chapter_id: str, fmt: str = "srt") -> Path:
        return self.output.chapter_subtitle(project_id=project_id, chapter_id=chapter_id, fmt=fmt)

    def export_chapters(self, project_id: str) -> Path:
        return self.output.export_chapters(project_id=project_id)

    def export_m4b(self, project_id: str) -> Path:
        return self.output.export_m4b(project_id=project_id)
