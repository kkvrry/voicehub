"""向量模型用例：状态、后台下载、删除、推理。

下载是用户点击后才启动的，不在应用启动或打开 AI 弹窗时自动发生。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from app.embedding.base import EmbeddingError, EmbeddingProvider
from app.embedding.registry import detect_provider, list_providers
from app.infra.loghub import CHANNEL

__all__ = [
    "embed_texts",
    "list_status",
    "ready_provider",
    "remove",
    "start_download",
    "status",
]


@dataclass
class _Task:
    state: str = ""
    progress: float = 0.0
    note: str = ""
    current: str = ""
    thread: threading.Thread | None = None


_TASKS: dict[str, _Task] = {}
_LOCK = threading.Lock()


def _provider(key: str) -> EmbeddingProvider:
    provider = detect_provider(key)
    if provider is None:
        raise EmbeddingError(f"没有这个向量模型：{key}。", code="embedding_not_found")
    return provider


def _status(provider: EmbeddingProvider) -> dict[str, Any]:
    task = _TASKS.get(provider.key)
    installed = provider.installed()
    if task and task.state == "downloading":
        state, progress, note = "downloading", round(task.progress, 1), task.current
    elif task and task.state == "error":
        state, progress, note = "error", round(task.progress, 1), task.note
    elif installed:
        state, progress, note = "ready", 100.0, ""
    else:
        state, progress, note = "not_downloaded", 0.0, "模型还没下载。"
    return {
        "object": "embedding_provider",
        **provider.describe(),
        "installed": installed,
        "state": state,
        "progress": progress,
        "note": note,
    }


def list_status() -> list[dict[str, Any]]:
    return [_status(provider) for provider in list_providers().values()]


def status(key: str) -> dict[str, Any]:
    return _status(_provider(key))


def start_download(key: str) -> dict[str, Any]:
    provider = _provider(key)
    with _LOCK:
        task = _TASKS.get(key)
        if provider.installed():
            return _status(provider)
        if task and task.state == "downloading":
            return _status(provider)
        task = _Task(state="downloading", progress=0.0, note="准备下载……")
        _TASKS[key] = task

    def on_progress(done: int, total: int, current: str) -> None:
        ratio = (done / total * 100.0) if total > 0 else 0.0
        with _LOCK:
            task.progress = ratio
            task.current = current

    def run() -> None:
        try:
            provider.download(on_progress)
        except Exception as exc:  # noqa: BLE001 - 下载失败是业务结果，状态端点负责说
            with _LOCK:
                task.state = "error"
                task.note = getattr(exc, "reason", None) or str(exc)
            CHANNEL.warning(f"向量模型下载失败：{provider.label} · {task.note}", source="embedding")
            return
        with _LOCK:
            task.state = "ready"
            task.progress = 100.0
            task.note = ""
        CHANNEL.info(f"向量模型已下载：{provider.label}", source="embedding")

    thread = threading.Thread(target=run, name=f"embedding-download-{key}", daemon=True)
    task.thread = thread
    thread.start()
    return _status(provider)


def remove(key: str) -> dict[str, Any]:
    provider = _provider(key)
    with _LOCK:
        task = _TASKS.get(key)
        if task and task.state == "downloading":
            raise EmbeddingError("模型正在下载，等下载结束再删除。", code="embedding_busy")
    try:
        provider.remove()
    except OSError as exc:
        raise EmbeddingError(f"删除模型失败：{exc}") from exc
    with _LOCK:
        _TASKS.pop(key, None)
    CHANNEL.info(f"向量模型已删除：{provider.label}", source="embedding")
    return _status(provider)


def embed_texts(key: str, texts: list[str], *, query: bool = False) -> list[list[float]]:
    provider = _provider(key)
    if not provider.installed():
        raise EmbeddingError("模型还没下载：先在设置里下载并启用。", code="embedding_missing")
    return provider.embed(texts, query=query)


def ready_provider() -> EmbeddingProvider | None:
    """已下载的向量模型；有多个时取 key 排序第一个。"""
    for provider in list_providers().values():
        if provider.installed():
            return provider
    return None
