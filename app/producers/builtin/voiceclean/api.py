"""音色处理接口：挂在 ``/api/v1/producers/voiceclean``。"""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.infra.uploads import UploadTooLarge, read_limited
from app.producers.base import ProducerError
from app.producers.builtin.voiceclean.service import MAX_SOURCE_BYTES, VoiceCleanService

__all__ = ["build_router"]


class SegmentIn(BaseModel):
    """一段要保留的区间（秒）。"""

    start: float = 0.0
    end: float = 0.0


class TrimIn(BaseModel):
    """一条样例要保留的区间。

    ``segments`` 是删除若干区间后剩下的段；旧的 ``start`` / ``end`` 当成一段
    （``end = 0`` 表示到结尾）。两者都没给表示整段。
    """

    start: float = 0.0
    end: float = 0.0
    segments: list[SegmentIn] = Field(default_factory=list)


class ScopeIn(BaseModel):
    """处理目标：一条音色（可再限定 ``files`` / ``file``）或一份上传的电脑文件。

    ``kind='file'`` 时只认 ``token``：文件由 ``POST /source`` 上传，处理结果下载，
    不写回音色库。
    """

    kind: str = "voice"
    token: str = ""
    voice_id: str = ""
    file: str = ""
    files: list[str] = Field(default_factory=list)
    trims: dict[str, TrimIn] = Field(default_factory=dict)


class StepIn(BaseModel):
    key: str
    enabled: bool = True
    model: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class RunIn(BaseModel):
    scope: ScopeIn = Field(default_factory=ScopeIn)
    steps: list[StepIn] = Field(default_factory=list)


class ApplyIn(BaseModel):
    job_id: str
    indexes: list[int] = Field(default_factory=list)


def build_router(service: VoiceCleanService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/state", summary="服务状态、步骤与可选模型")
    def state() -> dict[str, Any]:
        return service.state()

    @router.post("/source", summary="上传电脑里的音频作为处理目标")
    async def upload_source(
        file: UploadFile | None = File(None, description="要处理的音频"),  # noqa: B008
    ) -> dict[str, Any]:
        """收一份不属于音色库的音频；只处理与下载，不写回任何样例。"""
        if file is None:
            raise ProducerError("没收到音频文件。")
        try:
            raw = await read_limited(file, MAX_SOURCE_BYTES)
        except UploadTooLarge as exc:
            raise ProducerError(f"音频超过 {exc.label}。") from exc
        # 写盘与 ffprobe 都是阻塞的：放进线程池，别拖住事件循环。
        payload = await run_in_threadpool(service.save_source, raw, file.filename or "")
        return {"object": "voiceclean_source", **payload}

    @router.get("/source/audio", summary="试听上传的电脑文件")
    def source_audio(token: str) -> FileResponse:
        path = service.source_file(token)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # 不带 filename：这是给 <audio> 内联播放的，不能触发下载。
        return FileResponse(path, media_type=media)

    @router.post("/run", summary="按范围开始处理")
    def run(body: RunIn) -> dict[str, Any]:
        return service.run(body.scope.model_dump(), [row.model_dump() for row in body.steps])

    @router.get("/job", summary="当前任务与逐条结果")
    def job() -> dict[str, Any]:
        return service.job_view() or {}

    @router.post("/job/cancel", summary="取消当前任务")
    def cancel() -> dict[str, Any]:
        return service.cancel()

    @router.post("/job/clear", summary="清掉已完成的任务，回到可编辑状态")
    def clear() -> dict[str, Any]:
        return service.clear()

    @router.get("/job/audio", summary="试听：原声或处理结果")
    def audio(job_id: str, index: int, side: str = "result") -> FileResponse:
        path = service.audio_path(job_id, index, side)
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    @router.get("/job/download", summary="下载电脑文件的处理结果")
    def download(job_id: str, index: int) -> FileResponse:
        path = service.audio_path(job_id, index, "result")
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=service.download_name(job_id, index),
        )

    @router.post("/job/apply", summary="按确认的条目替换样例音频")
    def apply(body: ApplyIn) -> dict[str, Any]:
        return service.apply(body.job_id, body.indexes)

    return router
