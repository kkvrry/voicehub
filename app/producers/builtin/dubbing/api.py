"""配音生产方接口：挂在 ``/api/v1/producers/dubbing``。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.localonly import require_local
from app.infra.media import audio_mime
from app.infra.shell import open_folder, pick_media_file
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.service import DubbingService

__all__ = ["build_router"]

#: 单次导入的上限：两小时的 1080p 素材约 3–6 GB，超过就不是"一条视频"了。
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024

_VIDEO_MIME = {"mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
               "webm": "video/webm", "avi": "video/x-msvideo"}
_SUBTITLE_MIME = {"srt": "application/x-subrip", "vtt": "text/vtt",
                  "ass": "text/x-ssa", "lrc": "text/plain", "txt": "text/plain"}

#: 会被覆盖重写的产物：播放时必须回源，不能吃浏览器缓存。
_VOLATILE = {"cue", "vocal", "mix", "subtitle", "original"}


def _mime(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return _VIDEO_MIME.get(suffix) or _SUBTITLE_MIME.get(suffix) or audio_mime(suffix)


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
        # 临时文件必须无条件清掉：超限、读流中断、建项目失败都会走到这里；
        # 只在 size==0 时删，会让一次超限上传在临时目录留下接近 8 GB 的半截文件。
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while chunk := await file.read(_CHUNK):
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise ProducerError("文件超过 8 GB：先剪出需要配音的片段再导入。")
                    handle.write(chunk)
            if size == 0:
                raise ProducerError("没有收到文件内容。")
            return service.create(
                name=name or Path(file.filename or "").stem,
                upload=temp,
                suffix=suffix,
                stream=max(0, stream),
                source_name=Path(file.filename or "").name,
            )
        finally:
            temp.unlink(missing_ok=True)

    @router.post("/pick-source", summary="选择要引用的本机文件")
    def pick_source(request: Request) -> dict[str, Any]:
        """弹宿主机文件选择框；取消时返回空路径。

        浏览器拿不到真实路径，引用导入必须由服务端问出路径。这个入口只限本机：
        别的设备让服务端在用户桌面上弹框，既不礼貌也分不清是谁在操作。
        """
        require_local(
            request,
            code="link_local_only",
            message="引用本机文件只能在运行服务的这台电脑上使用。",
        )
        try:
            chosen = pick_media_file()
        except OSError as exc:
            raise ProducerError(str(exc)) from exc
        return {"object": "picked_file", "path": str(chosen) if chosen else ""}

    @router.post("/projects/link", summary="引用本机文件建立项目")
    def create_linked_project(
        request: Request, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        require_local(
            request,
            code="link_local_only",
            message="引用本机文件只能在运行服务的这台电脑上使用。",
        )
        payload = body or {}
        return service.create_linked(
            name=str(payload.get("name") or ""),
            path=str(payload.get("path") or ""),
            stream=max(0, int(payload.get("stream") or 0)),
        )

    @router.post("/projects/{project_id}/source", summary="重新指定引用的源文件")
    def relink_project(
        project_id: str,
        request: Request,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_local(
            request,
            code="link_local_only",
            message="重新指定源文件只能在本机使用。",
        )
        return service.relink(project_id, path=str((body or {}).get("path") or ""))

    @router.delete("/projects/{project_id}", summary="删除项目")
    def delete_project(project_id: str) -> dict[str, Any]:
        service.delete(project_id)
        return {"id": project_id, "object": "dubbing_project", "deleted": True}

    @router.patch("/projects/{project_id}", summary="修改项目")
    def update_project(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if "name" in (body or {}):
            return service.rename(project_id, str(body.get("name") or ""))
        return service.detail(project_id)

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
        # 三条轨各自一个电平；−60 dB 当静音，界面上就是滑块最左端。
        for key in ("original_gain", "dub_gain", "accompaniment_gain"):
            if key in body:
                gain = float(body[key])
                options[key] = max(-60.0, min(12.0, gain))
        service.store.update_project(project_id, options=options)
        service.store.touch(project_id)
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

    @router.post("/projects/{project_id}/subtitles", summary="导入 SRT / VTT 字幕")
    async def import_subtitles(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
        raw = await file.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ProducerError("字幕文件超过 8 MB：这不是一份正常的字幕。")
        if not raw:
            raise ProducerError("没有收到字幕文件内容。")
        return service.import_subtitles(
            project_id, raw, filename=Path(file.filename or "").name
        )

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

    @router.post("/projects/{project_id}/cues/batch", summary="批量修改或删除段落")
    def batch_cues(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return service.batch_cues(project_id, body or {})

    # ---- 角色 -------------------------------------------------------------

    @router.post("/projects/{project_id}/roles", summary="新建角色")
    def add_role(
        project_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        """``engine`` 传 ``auto`` 时由宿主按当前在跑的引擎选，存下实际选中那台。"""
        return {
            "object": "dubbing_role",
            **service.add_role(project_id, body or {}, library=request.app.state.library),
        }

    @router.put("/projects/{project_id}/roles/{role_id}", summary="修改角色")
    def update_role(
        project_id: str, role_id: str, request: Request, body: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "object": "dubbing_role",
            **service.update_role(
                project_id, role_id, body or {}, library=request.app.state.library
            ),
        }

    @router.delete("/projects/{project_id}/roles/{role_id}", summary="删除角色")
    def delete_role(project_id: str, role_id: str) -> dict[str, Any]:
        service.delete_role(project_id, role_id)
        return {"id": role_id, "object": "dubbing_role", "deleted": True}

    # ---- 合成与导出 -------------------------------------------------------

    @router.post("/projects/{project_id}/synth", summary="开始逐段合成")
    def synth(project_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
        cue_ids = list((body or {}).get("cue_ids") or [])
        # 用户点名要重配的段不走缓存：同一个键也重新请求引擎，这是「重配」的含义。
        force = bool((body or {}).get("force")) and bool(cue_ids)
        return service.start_synth(project_id, request.app.state.library, cue_ids, force=force)

    @router.post("/projects/{project_id}/synth/cancel", summary="取消合成")
    def cancel_synth(project_id: str) -> dict[str, Any]:
        job = service.store.latest_job(project_id)
        if job is None or job["kind"] != "synth" or job["state"] != "running":
            raise ProducerError("当前没有在跑的合成任务。")
        service.store.cancel_job(job["id"])
        return {"object": "dubbing_job", **service.store.job(job["id"])}

    # ---- 分轨 -------------------------------------------------------------

    @router.post("/projects/{project_id}/separate", summary="分离人声与伴奏")
    def separate(
        project_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return service.start_separate(
            project_id, str((body or {}).get("model") or "")
        )

    @router.post("/projects/{project_id}/separation/remove", summary="移除分轨")
    def remove_separation(project_id: str) -> dict[str, Any]:
        return service.remove_stems(project_id)

    @router.post("/projects/{project_id}/assemble", summary="开始贴齐并混音")
    def assemble(project_id: str) -> dict[str, Any]:
        return service.start_assemble(project_id)

    @router.post("/projects/{project_id}/export", summary="开始导出成品")
    def export(project_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return service.start_export(
            project_id,
            allow_incomplete=bool((body or {}).get("allow_incomplete")),
        )

    @router.post("/projects/{project_id}/jobs/cancel", summary="取消当前任务")
    def cancel_job(project_id: str) -> dict[str, Any]:
        job = service.store.latest_job(project_id)
        if job is None or job["state"] != "running":
            raise ProducerError("当前没有在跑的任务。")
        service.store.cancel_job(job["id"])
        return {"object": "dubbing_job", **service.store.job(job["id"])}

    # ---- 媒体 -------------------------------------------------------------

    @router.get("/projects/{project_id}/media/{kind}", summary="播放 / 下载产物")
    def media(project_id: str, kind: str, name: str = "") -> FileResponse:
        """``kind=source|original|vocal|mix|cue|subtitle|export``。

        成品与字幕带 ``filename``（下载）；其余走内联，播放器要按字节范围取，
        加了附件头浏览器会先下载而不是播放。

        会变的产物（逐段音频、混音、字幕）一律 ``no-cache``：重配会覆盖同名文件，
        浏览器若不回源，点播放听到的还是上一版。前端还会带版本号，双保险。
        """
        path = service.media_path(project_id, kind, name)
        fresh = {"Cache-Control": "no-cache"} if kind in _VOLATILE else None
        if kind in {"export", "subtitle"}:
            return FileResponse(path, media_type=_mime(path), filename=path.name, headers=fresh)
        return FileResponse(path, media_type=_mime(path), headers=fresh)

    @router.post("/projects/{project_id}/reveal", summary="打开成品所在文件夹")
    def reveal(project_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """在宿主机资源管理器里打开成品目录；带 ``name`` 时校验这个产物还在。

        窗口弹在本机桌面上，和音色目录的「在资源管理器中打开」同一条边界。
        """
        require_local(
            request, code="reveal_local_only", message="「打开文件夹」只能在本机使用。"
        )
        name = str((body or {}).get("name") or "").strip()
        path = service.media_path(project_id, "export", name) if name else service.folder(project_id) / "out"
        folder = path.parent if name else path
        if not folder.is_dir():
            raise ProducerError("成品目录还不存在：先导出一次。")
        try:
            open_folder(folder)
        except OSError as exc:
            raise ProducerError(f"打不开这个文件夹：{exc}") from exc
        return {"object": "reveal", "folder": str(folder)}

    return router
