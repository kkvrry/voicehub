"""音色处理接口：挂在 ``/api/v1/producers/voiceclean``。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.producers.builtin.voiceclean.service import VoiceCleanService

__all__ = ["build_router"]


class ScopeIn(BaseModel):
    """处理目标：一条音色的全部样例，或它的一条样例（``file`` 非空）。"""

    voice_id: str = ""
    file: str = ""


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


class InstallIn(BaseModel):
    id: str


def build_router(service: VoiceCleanService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/state", summary="服务状态、步骤与可选模型")
    def state() -> dict[str, Any]:
        return service.state()

    @router.post("/run", summary="按范围开始处理")
    def run(body: RunIn) -> dict[str, Any]:
        return service.run(body.scope.model_dump(), [row.model_dump() for row in body.steps])

    @router.get("/job", summary="当前任务与逐条结果")
    def job() -> dict[str, Any]:
        return service.job_view() or {}

    @router.post("/job/cancel", summary="取消当前任务")
    def cancel() -> dict[str, Any]:
        return service.cancel()

    @router.get("/job/audio", summary="试听：原声或处理结果")
    def audio(job_id: str, index: int, side: str = "result") -> FileResponse:
        path = service.audio_path(job_id, index, side)
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    @router.post("/job/apply", summary="按确认的条目替换样例音频")
    def apply(body: ApplyIn) -> dict[str, Any]:
        return service.apply(body.job_id, body.indexes)

    @router.post("/models/install", summary="安装模型包")
    def install(body: InstallIn) -> dict[str, Any]:
        return service.install(body.id)

    return router
