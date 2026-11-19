"""HTTP 层入口：建立路由表，各域的分区实现在 :mod:`app.api.sections`。

约定（与 ``docs/request-spec.md`` 一致）：

- 路径前缀由 ``main.py`` 注入：只有 ``/api/v1`` 一个前缀。
- 集合响应统一为 ``{"object": "list", "data": [...], "has_more": false}``；
  单个资源带 ``object`` 判别字段；删除返回 ``{"id", "object", "deleted": true}``。
- 错误一律 ``raise ApiError(...)``（或 :mod:`app.api.errors` 的便捷函数），
  由全局处理器生成错误体。**不再使用 ``HTTPException``**：它会绕开错误码表。
- 合成与克隆支持 ``Idempotency-Key``：同一键在有效期内重复提交返回首次结果。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.sections import audio, capture, engines, library, producers, studio, system
from app.usecase.library import LibraryService

__all__ = ["build_router"]


def build_router(service: LibraryService, *, version: str = "0.1.0") -> APIRouter:
    """建表顺序即分区顺序；同域内的路径顺序由各分区自己保证。"""
    router = APIRouter(tags=["system"])
    system.register(router, service, version)
    library.register(router, service, version)
    engines.register(router, service, version)
    studio.register(router, service, version)
    capture.register(router, service, version)
    audio.register(router, service, version)
    producers.register(router, service, version)
    return router
