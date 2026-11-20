"""有声书生产方接口：挂在 ``/api/v1/producers/audiobook``。"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.domain.models import platform_voice_id
from app.engines.base import CloneRequest
from app.infra.loghub import CHANNEL
from app.infra.media import audio_mime
from app.infra.uploads import UploadTooLarge, read_limited
from app.producers.base import ProducerError
from app.producers.builtin.audiobook import ai_prompts
from app.producers.builtin.audiobook.assist import voice_assign as voice_assign_task
from app.producers.builtin.audiobook.assist.registry import TASKS
from app.producers.builtin.audiobook.pipeline import AudiobookService
from app.producers.builtin.audiobook.rules import NARRATOR
from app.usecase import assist as assist_uc
from app.usecase import engines as engines_uc
from app.usecase.library import LibraryError

__all__ = ["build_router"]

#: 书稿导入的上限；再大就不是一本小说了。
_MAX_BOOK_BYTES = 64 * 1024 * 1024


def build_router(service: AudiobookService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/overview", summary="项目列表")
    def overview() -> dict[str, Any]:
        return service.overview()

    @router.get("/patterns", summary="分章模式与文本处理预设")
    def patterns() -> dict[str, Any]:
        return service.patterns()

    @router.get("/split/options", summary="拆句选项的可选值")
    def split_options_meta() -> dict[str, Any]:
        return service.split_options_meta()

    @router.get("/assist/status", summary="AI 辅助状态")
    def assist_status() -> dict[str, Any]:
        """AI 按钮能不能点：通道没配好时给一句可直接展示的原因。"""
        return assist_uc.status(service.settings)

    @router.get("/prompts", summary="AI 提示词")
    def prompts() -> dict[str, Any]:
        """设置页「词表」里那四组 AI 提示词的生效值。"""
        return ai_prompts.state()

    @router.put("/prompts", summary="保存 AI 提示词")
    def save_prompts(body: dict[str, Any]) -> dict[str, Any]:
        """按块保存；下一轮分析 / 匹配 / 画像 / 正则直接用新正文。"""
        try:
            return ai_prompts.save(body)
        except ai_prompts.PromptError as exc:
            raise ProducerError(str(exc)) from exc

    @router.get("/assist/voice-options", summary="分配可选的引擎与方式")
    def assist_voice_options(request: Request) -> dict[str, Any]:
        """弹窗里的引擎、方式与音色清单；默认引擎与默认方式也在这一处定。"""
        return voice_assign_task.voice_options(request.app.state.library)

    @router.post("/assist/{task_key}", summary="AI 辅助提案")
    async def assist_task(
        task_key: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        """一次结构化提案。模型只提案，不写库；应用仍走原有写接口。"""
        task = TASKS.get(task_key)
        if task is None:
            raise ProducerError(f"没有这个 AI 任务：{task_key}。")
        project_id = str(body.get("project_id") or "").strip()
        if not project_id:
            raise ProducerError("缺少项目 id。")
        if not service.store.project_meta(project_id):
            raise ProducerError("项目不存在。")
        ctx = assist_uc.AssistContext(
            service=service,
            library=request.app.state.library,
            settings=service.settings,
            project_id=project_id,
            body=body,
        )
        try:
            return await assist_uc.run_task(task, ctx)
        except assist_uc.AssistError as exc:
            raise ProducerError(str(exc)) from None

    @router.post("/projects", summary="新建项目")
    def create(body: dict[str, Any]) -> dict[str, Any]:
        return service.create(str(body.get("name") or ""), str(body.get("text") or ""))

    @router.post("/projects/import", summary="导入书稿")
    async def import_book(
        file: UploadFile = File(...),  # noqa: B008 - FastAPI 的声明式写法
        name: str = Form(""),
    ) -> dict[str, Any]:
        try:
            data = await read_limited(file, _MAX_BOOK_BYTES)
        except UploadTooLarge as exc:
            raise ProducerError(f"文件超过 {exc.label}。") from None
        # 解析 EPUB / 写项目库是同步活，交给线程池，别占着事件循环。
        return await asyncio.to_thread(service.import_book, file.filename or "", data, name)

    @router.get("/projects/{project_id}", summary="项目详情")
    def detail(project_id: str) -> dict[str, Any]:
        return service.detail(project_id)

    @router.put("/projects/{project_id}", summary="改书名与作者")
    def update(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.update_project(
            project_id,
            name=None if "name" not in body else str(body.get("name") or ""),
            author=None if "author" not in body else str(body.get("author") or ""),
        )

    @router.delete("/projects/{project_id}", summary="删除项目")
    def remove(project_id: str) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再删项目。")
        folder = service.project_folder(project_id)
        service.store.delete_project(project_id)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return {"deleted": project_id}

    @router.get("/projects/{project_id}/marks", summary="语气词标记")
    def marks(project_id: str) -> dict[str, Any]:
        return service.marks(project_id)

    @router.get("/projects/{project_id}/source", summary="书稿原文")
    def source(project_id: str) -> dict[str, Any]:
        return service.source_payload(project_id)

    @router.post("/projects/{project_id}/split/preview", summary="预览分章")
    def preview_split(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.preview_split(project_id, str(body.get("pattern_key") or ""))

    @router.post("/projects/{project_id}/split", summary="分章")
    def split(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.split_project(project_id, str(body.get("pattern_key") or ""))

    @router.put("/projects/{project_id}/split-options", summary="保存拆句选项")
    def save_split_options(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.save_split_options(project_id, body)

    @router.post("/projects/{project_id}/text/preview", summary="预览文本处理")
    def preview_text(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.preview_text(project_id, body)

    @router.post("/projects/{project_id}/text/apply", summary="应用文本处理")
    def apply_text(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.apply_text(project_id, body)

    @router.get("/projects/{project_id}/chapters/{chapter_id}", summary="章节与行")
    def chapter(project_id: str, chapter_id: str) -> dict[str, Any]:
        return service.chapter_rows(project_id, chapter_id)

    @router.put("/projects/{project_id}/chapters/{chapter_id}", summary="重命名章节")
    def rename_chapter(project_id: str, chapter_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.rename_chapter(project_id, chapter_id, str(body.get("title") or ""))

    @router.put("/projects/{project_id}/chapters/{chapter_id}/text", summary="改章节正文")
    def save_chapter_text(
        project_id: str, chapter_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        text = body.get("text")
        if not isinstance(text, str):
            raise ProducerError("参数 text 必须是字符串。")
        return service.save_chapter_text(project_id, chapter_id, text)

    @router.delete("/projects/{project_id}/chapters/{chapter_id}/rows", summary="清空本章行")
    def clear_chapter_rows(project_id: str, chapter_id: str) -> dict[str, Any]:
        return service.clear_chapter_rows(project_id, chapter_id)

    @router.post(
        "/projects/{project_id}/chapters/{chapter_id}/rows/auto-merge",
        summary="自动合并旁白行",
    )
    def auto_merge_rows(
        project_id: str, chapter_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        max_chars = (body or {}).get("max_chars")
        return service.auto_merge_rows(project_id, chapter_id, max_chars)

    @router.post("/projects/{project_id}/rows/auto-merge", summary="自动合并旁白行（全书）")
    def auto_merge_all_rows(
        project_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        max_chars = (body or {}).get("max_chars")
        return service.auto_merge_all(project_id, max_chars)

    @router.delete("/projects/{project_id}/chapters/{chapter_id}", summary="删除章节")
    def delete_chapter(project_id: str, chapter_id: str) -> dict[str, Any]:
        return service.delete_chapter(project_id, chapter_id)

    @router.post("/projects/{project_id}/chapters/{chapter_id}/resplit", summary="重新拆句")
    def resplit(project_id: str, chapter_id: str) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再重新拆句。")
        return service.resplit_chapter(project_id, chapter_id)

    @router.post("/projects/{project_id}/steps/sentences", summary="拆句")
    def sentences(project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = str((body or {}).get("scope") or "all")
        return service.split_sentences(project_id, scope)

    @router.post(
        "/projects/{project_id}/steps/analysis",
        summary="分析说话人与情绪",
        status_code=202,
    )
    async def analysis(project_id: str) -> dict[str, Any]:
        return service.start_analysis(project_id)

    @router.post(
        "/projects/{project_id}/steps/audio",
        summary="生成逐行音频",
        status_code=202,
    )
    async def audio(project_id: str, request: Request) -> dict[str, Any]:
        # 慢的是只读准备（扫全表、列音频目录）：扔进线程，别占着事件循环——
        # 否则这几百毫秒到几十秒里连进度轮询都排队，界面看着就是"点了没反应"。
        # 建任务这一步很快，且必须在当前循环里建（create_task 认当前循环）。
        plan = await asyncio.to_thread(service.prepare_audio, project_id)
        return service.start_audio_planned(
            project_id, request.app.state.library, plan
        )

    @router.post("/projects/{project_id}/steps/loudness", summary="处理全书行音频响度", status_code=202)
    async def loudness(project_id: str) -> dict[str, Any]:
        return service.loudness.start(project_id)

    @router.get("/projects/{project_id}/merge-progress", summary="合并处理进度")
    def merge_progress(project_id: str) -> dict[str, Any]:
        if not service.store.project_meta(project_id):
            raise ProducerError("项目不存在。")
        return service.output.progress(project_id)

    @router.post("/projects/{project_id}/merge/cancel", summary="停止合并")
    def cancel_merge(project_id: str) -> dict[str, Any]:
        if not service.store.project_meta(project_id):
            raise ProducerError("项目不存在。")
        return {"cancelled": service.output.cancel(project_id)}

    @router.get("/jobs/{job_id}", summary="任务进度")
    def job(job_id: str) -> dict[str, Any]:
        found = service.store.job(job_id)
        if not found:
            raise ProducerError("任务不存在。")
        return found

    @router.post("/jobs/{job_id}/cancel", summary="取消任务")
    def cancel(job_id: str) -> dict[str, Any]:
        found = service.store.request_cancel(job_id)
        if not found:
            raise ProducerError("任务不存在。")
        return found

    @router.post("/projects/{project_id}/chapters/{chapter_id}/merge", summary="合并章节")
    def merge(project_id: str, chapter_id: str) -> dict[str, Any]:
        return service.merge(project_id, chapter_id)

    @router.get("/projects/{project_id}/export", summary="分章导出")
    def export(project_id: str) -> FileResponse:
        path = service.export_chapters(project_id)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @router.get("/projects/{project_id}/export/m4b", summary="整本 M4B 导出")
    def export_m4b(project_id: str) -> FileResponse:
        path = service.export_m4b(project_id)
        return FileResponse(path, media_type="audio/mp4", filename=path.name)

    @router.put("/projects/{project_id}/voices", summary="分配角色音色")
    def set_voice(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        speaker = str(body.get("speaker") or "").strip()
        if not speaker:
            raise ProducerError("缺少角色名。")
        if service.running_job(project_id):
            raise ProducerError("任务运行中，等它结束或取消后再换音色。")
        mode = str(body.get("sample_mode") or "emotion")
        if mode not in {"emotion", "fixed"}:
            raise ProducerError("样例策略只能是「按情绪匹配」或「固定样例」。")
        sample_file = str(body.get("sample_file") or "").strip()
        if mode == "fixed" and not sample_file:
            raise ProducerError("固定样例模式下要指定一条样例。")
        service.store.set_voice(
            project_id,
            speaker,
            str(body.get("engine") or ""),
            str(body.get("voice_id") or ""),
            str(body.get("voice_name") or ""),
            sample_mode=mode,
            sample_file=sample_file if mode == "fixed" else "",
        )
        service.store.invalidate_speaker(project_id, speaker)
        return service.detail(project_id)

    @router.put("/projects/{project_id}/voices/batch", summary="批量分配角色音色")
    async def set_voices(
        project_id: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        """应用 AI 建议或其它批量分配：只写选中项，只作废真正变化的角色。

        应用前重新核对角色仍在、音色仍在库里且能落到这台引擎上；
        预览到应用之间被改过的项跳过，不覆盖用户后来的选择。
        ``overwrite`` 为真时允许改写已指定音色的角色（弹窗里的「调整」）。
        ``mode=design`` 的项先按描述创建设计音色，再走同一条分配路径。
        """
        if service.running_job(project_id):
            raise ProducerError("任务运行中，等它结束或取消后再换音色。")
        items = body.get("assignments")
        if not isinstance(items, list) or not items:
            raise ProducerError("没有要应用的角色。")
        if len(items) > 200:
            raise ProducerError("一次最多应用 200 个角色。")
        overwrite = bool(body.get("overwrite"))
        speakers = service.store.speakers(project_id)
        known = {str(item["speaker"]) for item in speakers}
        unassigned = {
            str(item["speaker"])
            for item in speakers
            if not str(item.get("voice_id") or "").strip()
        }
        # 旁白不在 ``speakers`` 里（它没有名字，是 narration 行），但合成时按
        # ``row["speaker"] or 旁白`` 取音色，所以它同样是一条可指定的映射。
        if service.store.narration_rows(project_id):
            known.add(NARRATOR)
            narrator = service.store.voice_map(project_id).get(NARRATOR) or {}
            if not str(narrator.get("voice_id") or "").strip():
                unassigned.add(NARRATOR)
        info = voice_assign_task.engine_caps()
        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                skipped.append({"speaker": "", "reason": "格式不对。"})
                continue
            speaker = str(item.get("speaker") or "").strip()
            engine = str(item.get("engine") or "").strip()
            voice_id = str(item.get("voice_id") or "").strip()
            mode = str(item.get("mode") or "").strip()
            design_prompt = str(item.get("design_prompt") or "").strip()
            sample_mode = str(item.get("sample_mode") or "emotion").strip()
            sample_file = str(item.get("sample_file") or "").strip()
            if sample_mode not in {"emotion", "fixed"}:
                skipped.append({"speaker": speaker, "reason": "样例策略不合法。"})
                continue
            if sample_mode == "fixed" and not sample_file:
                skipped.append({"speaker": speaker, "reason": "固定样例缺少文件。"})
                continue
            if not speaker or not engine:
                skipped.append({"speaker": speaker, "reason": "缺少角色或引擎。"})
                continue
            if speaker not in known:
                skipped.append({"speaker": speaker, "reason": "角色不在这本书里。"})
                continue
            if speaker not in unassigned and not overwrite:
                skipped.append({"speaker": speaker, "reason": "角色已被指定，未覆盖。"})
                continue
            if mode == "design":
                if not design_prompt:
                    skipped.append({"speaker": speaker, "reason": "音色设计缺少描述。"})
                    continue
                try:
                    meta = await engines_uc.clone_voice(
                        request.app.state.library,
                        engine,
                        CloneRequest(name=f"{speaker} 设计", design_prompt=design_prompt),
                    )
                except Exception as exc:  # noqa: BLE001 - 单个角色失败不拖垮整批
                    skipped.append({"speaker": speaker, "reason": f"音色设计失败：{exc}"})
                    continue
                voice_id = platform_voice_id(engine, meta.id)
            elif not voice_id:
                skipped.append({"speaker": speaker, "reason": "缺少音色。"})
                continue
            try:
                voice = voice_assign_task.resolve_voice(
                    request.app.state.library, voice_id, engine, info=info
                )
            except (LibraryError, assist_uc.AssistError) as exc:
                skipped.append({"speaker": speaker, "reason": str(exc)})
                continue
            # 设计音色没有样可选：描述本身就是凭据，固定按情绪走。
            pinned = mode != "design" and sample_mode == "fixed"
            rows.append(
                {
                    "speaker": speaker,
                    "engine": engine,
                    "voice_id": voice.id,
                    "voice_name": voice.shown_name,
                    "sample_mode": "fixed" if pinned else "emotion",
                    "sample_file": sample_file if pinned else "",
                    # AI 的判断依据随分配一起存：下次打开分配表还看得到。
                    "reason": str(item.get("reason") or "").strip()[:300],
                }
            )
        if not rows:
            raise ProducerError("没有可应用的分配：角色可能已被指定，或音色已不在库中。")
        changed = service.store.set_voices(project_id, rows)
        service.store.invalidate_speakers(project_id, changed)
        CHANNEL.info(
            f"AI 音色分配已应用：{len(rows)} 个角色，{len(changed)} 个变化",
            source="audiobook",
            project=project_id,
        )
        return {
            "object": "voice_assign",
            "detail": service.detail(project_id),
            "applied": len(rows),
            "changed": len(changed),
            "skipped": skipped,
        }

    @router.put("/projects/{project_id}/chapter-voices", summary="按章分配旁白音色")
    async def set_chapter_voices(
        project_id: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        """没识别出角色时，分配表按章列行：一颗音色只落在指定章。

        第一次只为某一章指定时，其余章由存储层沿用这颗音色；之后改一章只改这一章。
        应用前与角色分配走同一条复核：音色还在库里、确实能落到这台引擎上。
        """
        if service.running_job(project_id):
            raise ProducerError("任务运行中，等它结束或取消后再换音色。")
        items = body.get("assignments")
        if not isinstance(items, list) or not items:
            raise ProducerError("没有要应用的章节。")
        if len(items) > 500:
            raise ProducerError("一次最多应用 500 章。")
        chapters = {
            str(item["id"]): item for item in service.store.chapters(project_id)
        }
        info = voice_assign_task.engine_caps()
        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                skipped.append({"chapter_id": "", "reason": "格式不对。"})
                continue
            chapter_id = str(item.get("chapter_id") or "").strip()
            engine = str(item.get("engine") or "").strip()
            voice_id = str(item.get("voice_id") or "").strip()
            mode = str(item.get("mode") or "").strip()
            design_prompt = str(item.get("design_prompt") or "").strip()
            sample_mode = str(item.get("sample_mode") or "emotion").strip()
            sample_file = str(item.get("sample_file") or "").strip()
            chapter = chapters.get(chapter_id)
            if chapter is None:
                skipped.append({"chapter_id": chapter_id, "reason": "章节不在这本书里。"})
                continue
            if not engine:
                skipped.append({"chapter_id": chapter_id, "reason": "缺少引擎。"})
                continue
            if sample_mode not in {"emotion", "fixed"}:
                skipped.append({"chapter_id": chapter_id, "reason": "样例策略不合法。"})
                continue
            if sample_mode == "fixed" and not sample_file:
                skipped.append({"chapter_id": chapter_id, "reason": "固定样例缺少文件。"})
                continue
            if mode == "design":
                if not design_prompt:
                    skipped.append({"chapter_id": chapter_id, "reason": "音色设计缺少描述。"})
                    continue
                try:
                    meta = await engines_uc.clone_voice(
                        request.app.state.library,
                        engine,
                        CloneRequest(
                            name=f"{chapter['title']} 设计", design_prompt=design_prompt
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - 单章失败不拖垮整批
                    skipped.append(
                        {"chapter_id": chapter_id, "reason": f"音色设计失败：{exc}"}
                    )
                    continue
                voice_id = platform_voice_id(engine, meta.id)
            elif not voice_id:
                skipped.append({"chapter_id": chapter_id, "reason": "缺少音色。"})
                continue
            try:
                voice = voice_assign_task.resolve_voice(
                    request.app.state.library, voice_id, engine, info=info
                )
            except (LibraryError, assist_uc.AssistError) as exc:
                skipped.append({"chapter_id": chapter_id, "reason": str(exc)})
                continue
            pinned = mode != "design" and sample_mode == "fixed"
            rows.append(
                {
                    "chapter_id": chapter_id,
                    "engine": engine,
                    "voice_id": voice.id,
                    "voice_name": voice.shown_name,
                    "sample_mode": "fixed" if pinned else "emotion",
                    "sample_file": sample_file if pinned else "",
                }
            )
        if not rows:
            raise ProducerError("没有可应用的分配：章节可能已不存在，或音色已不在库中。")
        changed = service.store.set_chapter_voices(project_id, rows)
        service.store.invalidate_chapters(project_id, changed)
        CHANNEL.info(
            f"按章音色已应用：{len(rows)} 章，{len(changed)} 章变化",
            source="audiobook",
            project=project_id,
        )
        return {
            "object": "chapter_voice_assign",
            "detail": service.detail(project_id),
            "applied": len(rows),
            "changed": len(changed),
            "skipped": skipped,
        }

    @router.post("/projects/{project_id}/speakers/merge", summary="合并角色")
    def merge_speakers(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        names = [str(item).strip() for item in body.get("names") or [] if str(item).strip()]
        return service.merge_speakers(project_id, names, str(body.get("target") or ""))

    @router.put("/projects/{project_id}/rows/batch", summary="批量修正")
    def batch_update(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        ids = [str(item) for item in body.get("ids") or [] if str(item).strip()]
        if not ids:
            raise ProducerError("没有选中行。")
        return service.batch_update_rows(
            project_id,
            ids,
            speaker=body.get("speaker") if "speaker" in body else None,
            emotion=body.get("emotion") if "emotion" in body else None,
        )

    @router.put("/projects/{project_id}/rows/merge", summary="合并选中的行")
    def merge_rows(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        ids = [str(item) for item in body.get("ids") or [] if str(item).strip()]
        if len(ids) < 2:
            raise ProducerError("至少选中两行才能合并。")
        return service.merge_rows(project_id, ids)

    @router.put("/projects/{project_id}/rows/{row_id}", summary="修正一行")
    def edit_row(project_id: str, row_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        return service.edit_row(
            project_id,
            row_id,
            speaker=body.get("speaker"),
            emotion=body.get("emotion"),
            text=body.get("text"),
        )

    @router.post("/projects/{project_id}/rows/{row_id}/split", summary="拆分一行")
    def split_row(
        project_id: str, row_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        at = (body or {}).get("at")
        return service.split_row(project_id, row_id, int(at) if at is not None else None)

    @router.delete("/projects/{project_id}/rows/{row_id}", summary="删除一行")
    def delete_row(project_id: str, row_id: str) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，先取消再改行。")
        return service.delete_row(project_id, row_id)

    @router.post("/projects/{project_id}/rows/{row_id}/audio", summary="重试一行")
    async def retry_row(project_id: str, row_id: str, request: Request) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，等它结束再重试单行。")
        return await service.retry_row(project_id, row_id, request.app.state.library)

    @router.get("/projects/{project_id}/rows/{row_id}/params", summary="行级参数")
    def row_params(project_id: str, row_id: str) -> dict[str, Any]:
        """这一行可调的参数（合成参数 / 静默）与当前值。"""
        return service.row_params(project_id, row_id)

    @router.put("/projects/{project_id}/rows/{row_id}/params", summary="保存行级参数")
    def save_row_params(
        project_id: str, row_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if service.running_job(project_id):
            raise ProducerError("任务运行中，等它结束再改参数。")
        return service.save_row_params(project_id, row_id, body.get("params"))

    @router.get("/projects/{project_id}/chapters/{chapter_id}/audio", summary="章节成品")
    def chapter_audio(project_id: str, chapter_id: str) -> FileResponse:
        path = service.chapter_audio(project_id, chapter_id)
        return FileResponse(path, media_type=audio_mime(path.suffix), filename=path.name)

    @router.get(
        "/projects/{project_id}/chapters/{chapter_id}/subtitle", summary="章节字幕"
    )
    def chapter_subtitle(
        project_id: str, chapter_id: str, format: str = "srt"
    ) -> FileResponse:
        path = service.chapter_subtitle(project_id, chapter_id, format)
        media = "application/x-subrip" if path.suffix == ".srt" else "text/plain"
        return FileResponse(path, media_type=media, filename=path.name)

    @router.get("/projects/{project_id}/rows/{row_id}/audio", summary="逐行音频")
    def row_audio(project_id: str, row_id: str) -> FileResponse:
        row = service.store.row(row_id)
        if not row or not row.get("audio"):
            raise ProducerError("这一行还没有音频。")
        chapter = service.store.chapter(project_id, row["chapter_id"])
        if not chapter:
            raise ProducerError("这一行不属于该项目。")
        path, _ = service.loudness.row_audio(service.project_folder(project_id), row)
        if not path.is_file():
            raise ProducerError("音频文件已不存在。")
        return FileResponse(path)

    return router
