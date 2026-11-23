"""实时对话接口：挂在 ``/api/v1/producers/realtime_chat``。

只做状态、背景与回复语音三件事。对话、合成与识别都在宿主的通用接口上：
本插件不保存 endpoint、模型清单或音色凭据，换哪家模型都不会多出一份配置。
"""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.infra.uploads import UploadTooLarge, read_limited
from app.producers.base import ProducerError
from app.producers.builtin.realtime_chat.service import (
    MAX_AUDIO_BYTES,
    MAX_BACKGROUND_BYTES,
    RealtimeChatService,
)

__all__ = ["build_router"]

_BASE = "/api/v1/producers/realtime_chat"


def build_router(service: RealtimeChatService) -> APIRouter:
    router = APIRouter(tags=["producers"])

    @router.get("/state", summary="角色、背景与聊天记录")
    def state() -> dict[str, Any]:
        return {"object": "realtime_chat_state", **service.state()}

    @router.put("/state", summary="整份保存角色、背景与聊天记录")
    def save_state(body: dict[str, Any]) -> dict[str, Any]:
        return {"object": "realtime_chat_state", **service.save(body)}

    @router.post("/backgrounds", summary="上传一张自定义背景")
    async def upload_background(
        file: UploadFile | None = File(None, description="PNG / JPG / WebP 背景图"),  # noqa: B008
    ) -> dict[str, Any]:
        if file is None:
            raise ProducerError("没收到背景图片。")
        try:
            raw = await read_limited(file, MAX_BACKGROUND_BYTES)
        except UploadTooLarge as exc:
            raise ProducerError(
                f"背景图片超过 {MAX_BACKGROUND_BYTES // (1024 * 1024)} MB，先压缩一下。"
            ) from exc
        payload = await run_in_threadpool(
            service.save_background, raw, file.filename or ""
        )
        return {
            "object": "realtime_chat_background",
            **payload,
            "url": f"{_BASE}/backgrounds/{payload['name']}",
        }

    @router.get("/backgrounds/{name}", summary="读取一张自定义背景")
    def background(name: str) -> FileResponse:
        path = service.background_file(name)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # 文件名带随机串，换一张就是新地址，可以放心缓存。
        return FileResponse(
            path, media_type=media, headers={"Cache-Control": "public, max-age=86400"}
        )

    @router.post("/audio", summary="保存一条回复的语音")
    async def upload_audio(
        file: UploadFile | None = File(None, description="合成好的语音"),  # noqa: B008
    ) -> dict[str, Any]:
        if file is None:
            raise ProducerError("没收到语音。")
        try:
            raw = await read_limited(file, MAX_AUDIO_BYTES)
        except UploadTooLarge as exc:
            raise ProducerError(
                f"合成的语音超过 {MAX_AUDIO_BYTES // (1024 * 1024)} MB，不能保存。"
            ) from exc
        payload = await run_in_threadpool(service.save_audio, raw, file.filename or "")
        return {"object": "realtime_chat_audio", **payload}

    @router.get("/audio/{name}", summary="读取一条回复的语音")
    def audio(name: str) -> FileResponse:
        path = service.audio_file(name)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # 文件名带随机串，换一条就是新地址，可以放心缓存。
        return FileResponse(
            path, media_type=media, headers={"Cache-Control": "public, max-age=86400"}
        )

    @router.delete("/audio/{name}", summary="删除一条回复的语音")
    def delete_audio(name: str) -> dict[str, Any]:
        deleted = service.delete_audio(name)
        return {"object": "realtime_chat_audio", "name": name, "deleted": deleted}

    return router
