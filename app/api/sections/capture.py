"""路由分区「capture」：系统输出内录的起停与状态。

热键由桌面启动器注册，本分区只提供同一件事的 HTTP 入口与状态：
设置页读状态显示热键，托盘读状态显示录音时长。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.errors import ApiError
from app.usecase import capture as capture_uc
from app.usecase.library import LibraryService

__all__ = ["register"]


def _as_api_error(exc: capture_uc.CaptureError) -> ApiError:
    return ApiError(status=exc.status, code=exc.code, message=str(exc))


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.post("/capture/start", summary="开始内录", tags=["capture"])
    def capture_start() -> dict[str, Any]:
        """录制默认输出设备上正在播放的声音。

        设备不可用或已在录制时报错；成功返回与状态接口相同的形状。
        """
        try:
            return capture_uc.service.start()
        except capture_uc.CaptureError as exc:
            raise _as_api_error(exc) from exc

    @router.post("/capture/stop", summary="停止内录", tags=["capture"])
    def capture_stop() -> dict[str, Any]:
        """停止并返回落盘结果。全程静音时 ``silent`` 为真，文件仍保留。"""
        try:
            return capture_uc.service.stop()
        except capture_uc.CaptureError as exc:
            raise _as_api_error(exc) from exc

    @router.get("/capture/status", summary="内录状态", tags=["capture"])
    def capture_status() -> dict[str, Any]:
        """可用性、是否在录、已录时长、热键及其注册状态。"""
        return capture_uc.service.status()
