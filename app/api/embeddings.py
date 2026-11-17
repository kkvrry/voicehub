"""向量模型 HTTP 入口：状态、按需下载、删除与试算。

下载是后台线程：接口立刻返回 ``downloading``，前端轮询状态。这里不自动
触发下载，只有用户点按钮才会调用 ``POST .../download``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.errors import ApiError
from app.embedding import service as embedding_uc
from app.embedding.base import EmbeddingError

__all__ = ["build_embedding_router"]


def _fail(exc: EmbeddingError) -> ApiError:
    status = 404 if exc.code == "embedding_not_found" else 400
    return ApiError(status=status, code=exc.code, message=exc.reason)


def build_embedding_router() -> APIRouter:
    router = APIRouter(tags=["embeddings"])

    @router.get(
        "/embeddings/providers",
        summary="向量模型列表",
        tags=["embeddings"],
    )
    def providers() -> dict[str, Any]:
        rows = embedding_uc.list_status()
        return {"object": "list", "data": rows, "has_more": False}

    @router.get(
        "/embeddings/providers/{key}",
        summary="向量模型状态",
        tags=["embeddings"],
    )
    def provider(key: str) -> dict[str, Any]:
        try:
            return embedding_uc.status(key)
        except EmbeddingError as exc:
            raise _fail(exc) from None

    @router.post(
        "/embeddings/providers/{key}/download",
        summary="下载并启用向量模型",
        tags=["embeddings"],
    )
    def download(key: str) -> dict[str, Any]:
        try:
            return embedding_uc.start_download(key)
        except EmbeddingError as exc:
            raise _fail(exc) from None

    @router.post(
        "/embeddings/providers/{key}/remove",
        summary="删除向量模型",
        tags=["embeddings"],
    )
    def remove(key: str) -> dict[str, Any]:
        try:
            return embedding_uc.remove(key)
        except EmbeddingError as exc:
            raise _fail(exc) from None

    @router.post(
        "/embeddings/providers/{key}/embed",
        summary="试算文本向量",
        tags=["embeddings"],
    )
    def embed(key: str, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("texts")
        if not isinstance(raw, list) or not raw:
            raise ApiError(
                status=400,
                code="invalid_value",
                message="texts 必须是非空数组。",
            )
        texts = [str(item) for item in raw[:64]]
        try:
            vectors = embedding_uc.embed_texts(key, texts, query=bool(body.get("query")))
        except EmbeddingError as exc:
            raise _fail(exc) from None
        return {
            "object": "embedding",
            "key": key,
            "dimension": len(vectors[0]) if vectors else 0,
            "vectors": vectors,
        }

    return router
