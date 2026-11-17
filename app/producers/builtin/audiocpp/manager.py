"""audio.cpp 官方模型管理接口的薄客户端。

安装、进度、停止、删除都走实例自己的 ``/v1/ui/models/*`` 与 ``/v1/models/*``：
清单与文件布局由官方定义，插件只发命令、读结果，不重写下载器。

这些接口只在服务端以 ``--ui-management`` 启动时开放；外部实例没开时返回
``403 dynamic model management is disabled``，界面据此把模型区置为只读。
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any
from urllib.parse import urlparse

import httpx

from app.producers.base import ProducerError

__all__ = ["Connection", "client", "health", "normalize_url"]

_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None


def client() -> httpx.Client:
    """回环调用共用一个连接池：httpx 每次都重建 SSL 上下文，界面会白等两秒。"""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                trust_env=False,
                follow_redirects=True,
                timeout=httpx.Timeout(8.0, connect=1.0),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return _CLIENT


def normalize_url(raw: str) -> str:
    """用户填的地址：补协议、去尾斜杠、只留 scheme://host:port。"""
    text = str(raw or "").strip().strip('"').rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ProducerError("服务地址须为 http://主机:端口。")
    return f"{parsed.scheme}://{parsed.netloc}"


def _json(response: httpx.Response, url: str) -> Any:
    if response.status_code >= 400:
        detail = ""
        with contextlib.suppress(ValueError):
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                detail = str(error.get("message") if isinstance(error, dict) else error or "")
        raise ProducerError(detail or f"{url} 返回 {response.status_code}")
    try:
        return response.json()
    except ValueError:
        return {}


def _request(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
    try:
        response = client().request(method, url, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ProducerError(f"无法连接 audio.cpp：{exc}") from exc
    return _json(response, url)


def health(url: str, timeout: float = 1.0) -> dict[str, Any]:
    """服务是否活着；死端口返回空字典，不抛异常（探测是常态）。"""
    try:
        response = client().get(f"{url}/health", timeout=timeout)
        if response.status_code != 200:
            return {}
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class Connection:
    """一台已确认是 audio.cpp 的服务。"""

    __slots__ = ("info", "url")

    def __init__(self, url: str, info: dict[str, Any]) -> None:
        self.url = url
        self.info = info

    @classmethod
    def open(cls, url: str, timeout: float = 1.0) -> Connection | None:
        info = health(url, timeout)
        return cls(url, info) if info.get("status") == "ok" else None

    @property
    def version(self) -> str:
        return str(self.info.get("version") or "")

    @property
    def backend(self) -> str:
        return str(self.info.get("backend") or "")

    @property
    def management(self) -> bool:
        return bool(self.info.get("ui_management"))

    @property
    def loaded(self) -> int:
        try:
            return int(self.info.get("models") or 0)
        except (TypeError, ValueError):
            return 0

    def models(self) -> list[dict[str, Any]]:
        payload = _request("GET", f"{self.url}/v1/models")
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def models_root(self) -> str:
        if not self.management:
            return ""
        payload = _request("GET", f"{self.url}/v1/ui/models-root")
        return str(payload.get("models_root") or "") if isinstance(payload, dict) else ""

    def set_models_root(self, path: str) -> str:
        payload = _request("POST", f"{self.url}/v1/ui/models-root", {"path": path})
        return str(payload.get("models_root") or "") if isinstance(payload, dict) else ""

    def install(self, package_id: str) -> dict[str, Any]:
        return _request("POST", f"{self.url}/v1/ui/models/install", {"id": package_id})

    def install_status(self) -> list[dict[str, Any]]:
        payload = _request("GET", f"{self.url}/v1/ui/models/install-status")
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def stop_install(self, package_id: str) -> dict[str, Any]:
        return _request("POST", f"{self.url}/v1/ui/models/install/stop", {"id": package_id})

    def delete_package(self, package_id: str) -> dict[str, Any]:
        return _request("POST", f"{self.url}/v1/ui/models/delete", {"id": package_id})

    def clean_partial(self, package_id: str) -> dict[str, Any]:
        return _request("POST", f"{self.url}/v1/ui/models/clean-partial", {"id": package_id})

    def load(self, package_id: str, family: str, task: str) -> dict[str, Any]:
        body = {"id": family, "package_id": package_id}
        if task:
            body["task"] = task
        return _request("POST", f"{self.url}/v1/models/load", body)

    def unload(self, model_id: str) -> dict[str, Any]:
        return _request("POST", f"{self.url}/v1/models/unload", {"id": model_id})
