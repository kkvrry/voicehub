"""配音生产方接口：挂在 ``/api/v1/producers/dubbing``。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.infra.media import audio_mime
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.service import DubbingService

__all__ = ["build_router"]

#: 单次导入的上限：两小时的 1080p 素材约 3–6 GB，超过就不是"一条视频"了。
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024

_VIDEO_MIME = {"mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
               "webm": "video/webm", "avi": "video/x-msvideo"}


def _mime(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return _VIDEO_MIME.get(suffix) or audio_mime(suffix)


def build_router(service: DubbingService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/overview", summary="项目列表与识别可用性")
    def overview() -> dict[str, Any]:
        return service.overview()

    @router.get("/projects/{project_id}", summary="项目详情")
    def project_detail(project_id: str) -> dict[str, Any]:
        return service.detail(project_id)

    @router.post("/projects", summary="导入音视频建立项目")
    async def create_project(
        request: Request,
        file: UploadFile = File(..., description="音频或视频文件"),  # noqa: B008
        name: str = Form("", description="项目名；留空用文件名"),
        stream: int = Form(0, description="第几条音轨（多语言容器用）"),
    ) -> dict[str, Any]:
        """先落盘到临时文件再探时长：探不出就拒收，不留半个项目。

        上传按块写盘，不整份读进内存——一小时的 1080p 素材有几 GB。
        """
        suffix = Path(file.filename or "").suffix.lower()
        descriptor, temp_name = tempfile.mkstemp(suffix=suffix, prefix="dubbing-")
        temp = Path(temp_name)
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while chunk := await file.read(_CHUNK):
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise ProducerError("文件超过 8 GB：先剪出需要配音的片段再导入。")
                    handle.write(chunk)
        finally:
            if size == 0:
                temp.unlink(missing_ok=True)
        if size == 0:
            raise ProducerError("没有收到文件内容。")
        try:
            return service.create(
                name=name or Path(file.filename or "").stem,
                upload=temp,
                suffix=suffix,
                stream=max(0, stream),
                source_name=Path(file.filename or "").name,
            )
        finally:
            temp.unlink(missing_ok=True)

    @router.delete("/projects/{project_id}", summary="删除项目")
    def delete_project(project_id: str) -> dict[str, Any]:
        service.delete(project_id)
        return {"id": project_id, "object": "dubbing_project", "deleted": True}

    @router.post("/projects/{project_id}/extract", summary="重新抽音轨")
    def extract(project_id: str) -> dict[str, Any]:
        service.ensure_audio(project_id)
        return service.detail(project_id)

    @router.post("/projects/{project_id}/options", summary="保存混音与导出选项")
    def save_options(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        project = service.store.project(project_id)
        if project is None:
            raise ProducerError("找不到这个配音项目。")
        options = dict(project["options"])
        if "original_gain" in body:
            gain = float(body["original_gain"])
            options["original_gain"] = max(-60.0, min(12.0, gain))
        service.store.update_project(project_id, options=options)
        return service.detail(project_id)

    # ---- 转录 -------------------------------------------------------------

    @router.post("/projects/{project_id}/transcribe", summary="开始转录")
    def transcribe(project_id: str) -> dict[str, Any]:
        return service.start_transcribe(project_id)

    @router.post("/projects/{project_id}/transcribe/cancel", summary="取消转录")
    def cancel_transcribe(project_id: str) -> dict[str, Any]:
        job = service.store.latest_job(project_id)
        if job is None or job["kind"] != "transcribe" or job["state"] != "running":
            raise ProducerError("当前没有在跑的转录任务。")
        service.store.cancel_job(job["id"])
        return {"object": "dubbing_job", **service.store.job(job["id"])}

    @router.get("/projects/{project_id}/jobs/{job_id}", summary="任务进度")
    def job_of(project_id: str, job_id: str) -> dict[str, Any]:
        job = service.store.job(job_id)
        if job is None or job["project_id"] != project_id:
            raise ProducerError("找不到这个任务。")
        return {"object": "dubbing_job", **job}

    # ---- 段落 -------------------------------------------------------------

    @router.put("/projects/{project_id}/cues/{cue_id}", summary="改一段")
    def update_cue(project_id: str, cue_id: str, body: dict[str, Any]) -> dict[str, Any]:
        cue = service.update_cue(project_id, cue_id, body or {})
        return {"object": "dubbing_cue", **cue}

    @router.post("/projects/{project_id}/cues/{cue_id}/split", summary="按时间点拆分")
    def split_cue(project_id: str, cue_id: str, body: dict[str, Any]) -> dict[str, Any]:
        at = int((body or {}).get("at_ms") or 0)
        cues = service.split_cue(project_id, cue_id, at)
        return {"object": "list", "data": cues, "has_more": False}

    @router.post("/projects/{project_id}/cues/{cue_id}/merge", summary="与下一段合并")
    def merge_cue(project_id: str, cue_id: str) -> dict[str, Any]:
        cues = service.merge_cues(project_id, cue_id)
        return {"object": "list", "data": cues, "has_more": False}

    @router.delete("/projects/{project_id}/cues/{cue_id}", summary="删除一段")
    def delete_cue(project_id: str, cue_id: str) -> dict[str, Any]:
        cues = service.delete_cue(project_id, cue_id)
        return {"object": "list", "data": cues, "has_more": False}

    # ---- 角色 -------------------------------------------------------------

    @router.post("/projects/{project_id}/roles", summary="新建角色")
    def add_role(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"object": "dubbing_role", **service.add_role(project_id, body or {})}

    @router.put("/projects/{project_id}/roles/{role_id}", summary="修改角色")
    def update_role(project_id: str, role_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "object": "dubbing_role",
            **service.update_role(project_id, role_id, body or {}),
        }

    @router.delete("/projects/{project_id}/roles/{role_id}", summary="删除角色")
    def delete_role(project_id: str, role_id: str) -> dict[str, Any]:
        service.delete_role(project_id, role_id)
        return {"id": role_id, "object": "dubbing_role", "deleted": True}

    # ---- 合成与导出 -------------------------------------------------------

    @router.post("/projects/{project_id}/synth", summary="开始逐段合成")
    def synth(project_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cue_ids = list((body or {}).get("cue_ids") or [])
        return service.start_synth(project_id, request.app.state.library, cue_ids)

    @router.post("/projects/{project_id}/synth/cancel", summary="取消合成")
    def cancel_synth(project_id: str) -> dict[str, Any]:
        job = service.store.latest_job(project_id)
        if job is None or job["kind"] != "synth" or job["state"] != "running":
            raise ProducerError("当前没有在跑的合成任务。")
        service.store.cancel_job(job["id"])
        return {"object": "dubbing_job", **service.store.job(job["id"])}

    @router.post("/projects/{project_id}/assemble", summary="贴齐并混音")
    def assemble(project_id: str) -> dict[str, Any]:
        return service.assemble(project_id)

    @router.post("/projects/{project_id}/export", summary="导出成品")
    def export(project_id: str) -> dict[str, Any]:
        return service.export(project_id)

    # ---- 媒体 -------------------------------------------------------------

    @router.get("/projects/{project_id}/media/{kind}", summary="播放 / 下载产物")
    def media(project_id: str, kind: str, name: str = "") -> FileResponse:
        """``kind=source|original|vocal|mix|cue|subtitle|export``。

        只有成品带 ``filename``（下载）；其余走内联，播放器要按字节范围取，
        加了附件头浏览器会先下载而不是播放。
        """
        path = service.media_path(project_id, kind, name)
        if kind == "export":
            return FileResponse(path, media_type=_mime(path), filename=path.name)
        return FileResponse(path, media_type=_mime(path))

    return router
