"""路由分区「audio」：历史的列表、入库与删除。

列表在三处根上读盘（引擎保存目录、缓存目录、录音目录），筛选与计数由用例层一次算完；
入库与删除各自是一个写动作，错误按业务码返回（``audio_not_found`` / ``target_required`` 等）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.errors import ApiError
from app.api.schemas import AudioAbsorbIn, AudioDeleteIn
from app.usecase import audio as audio_uc
from app.usecase.library import LibraryService

__all__ = ["register"]


def _as_api_error(exc: audio_uc.AudioError) -> ApiError:
    return ApiError(status=exc.status, code=exc.code, message=str(exc))


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.get("/audio", summary="音频列表", tags=["audio"])
    def list_audio(
        source: str = Query("", description="record / loopback / engine；空 = 全部"),
        engine: str = Query("", description="生成物按引擎目录再筛一层"),
        q: str = Query("", description="文本、文件名、音色名、引擎名"),
        status: str = Query("", description="yes / no：已入库 / 未入库"),
        span: str = Query("", description="today / 7d / 30d；空 = 不限时间"),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        """三类音频的同一张表：录音、内录、生成物。

        每条的 ``id`` 是定位串，入库与删除都按它反查路径；``absorbed`` 是这条音频
        已经入库到哪几条音色（写在各目录的 sidecar 里，不是另建的状态）。
        """
        return audio_uc.index(
            source=source, engine=engine, q=q, status=status, span=span, limit=limit
        )

    @router.post("/audio/absorb", summary="入库到音色", tags=["audio"])
    def absorb_audio(body: AudioAbsorbIn) -> dict[str, Any]:
        """把这几条音频复制进目标音色的目录并登记为样例。

        源文件保留；入库后目标音色的 sidecar 记 ``source``，源目录记 ``absorbed``。
        同一目标重复入库是幂等的（同名同大小即视为已入过）。
        """
        try:
            rows = audio_uc.absorb(service, body.ids, body.target.model_dump())
        except audio_uc.AudioError as exc:
            raise _as_api_error(exc) from exc
        return {"object": "absorb", "data": rows}

    @router.delete("/audio", summary="移入回收站", tags=["audio"])
    def delete_audio(body: AudioDeleteIn) -> dict[str, Any]:
        """生成物连 sidecar 记录一起摘；录音只有文件。均可从回收站还原。"""
        try:
            removed = audio_uc.discard(body.ids)
        except audio_uc.AudioError as exc:
            raise _as_api_error(exc) from exc
        return {"object": "delete", "removed": removed}
