"""audio.cpp 服务的薄客户端：列模型、查模型目录、装模型、跑一次任务。

模型步骤与 TTS 引擎同构（填地址、发请求、收音频），但端点不同：分离与增强走通用
``POST /v1/tasks/run``，请求体是 ``{"model": id, "request": {...}}``，音频只能给
**服务端本机路径**或 base64，不支持 multipart。同机部署传路径，省一次编码传输。

上游没有把响应结构写进公开文档，所以音频取回按"已知的几种形状"依次试：``audio`` /
``result.audio`` / ``outputs[]`` / ``artifacts[]``（分离的多路输出走最后一种，取人声那一路）。
一种都对不上时，把响应里的键名原话带出来——那才是修下一版的依据。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.producers.base import ProducerError

__all__ = [
    "install",
    "loaded_models",
    "models_root",
    "normalize_host",
    "reachable",
    "run_task",
]

#: 分离任务的产物里，这几个词表示"人声那一路"。
_VOCAL_HINTS = ("vocal", "voice", "speech", "target", "speaker")


def normalize_host(raw: str) -> str:
    """用户填的地址：补协议、去尾斜杠、只留 ``scheme://host:port``。"""
    text = str(raw or "").strip().strip('"').rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ProducerError("服务地址须为 http://主机:端口。")
    return f"{parsed.scheme}://{parsed.netloc}"


def reachable(host: str, timeout: float = 0.4) -> bool:
    """端口是否有人听。

    先做这一层再发 HTTP：本机端口没人听时，Windows 会静默丢 SYN，连接要等满超时
    （实测 httpx 一次要 2.5 秒），界面为了一个"服务没起"白等三秒不值得。
    """
    parsed = urlparse(host)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def _client(timeout: float) -> httpx.Client:
    # trust_env=False：本机请求不走系统代理，也不读 http_proxy。
    return httpx.Client(trust_env=False, follow_redirects=True, timeout=timeout)


def _error(response: httpx.Response) -> str:
    with contextlib.suppress(ValueError):
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                if message:
                    return message
            if isinstance(error, str) and error.strip():
                return error.strip()
            detail = str(payload.get("detail") or payload.get("message") or "").strip()
            if detail:
                return detail
    text = (response.text or "").strip()
    return text[:200] or f"HTTP {response.status_code}"


def _get(host: str, path: str, timeout: float = 5.0) -> Any:
    url = f"{host}{path}"
    try:
        with _client(timeout) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise ProducerError(f"无法连接 audio.cpp（{host}）：服务未启动或地址不对。") from None
    if response.status_code >= 400:
        raise ProducerError(f"{url} 返回 {response.status_code}：{_error(response)}")
    with contextlib.suppress(ValueError):
        return response.json()
    return {}


def _post(host: str, path: str, body: dict[str, Any], timeout: float) -> Any:
    url = f"{host}{path}"
    try:
        with _client(timeout) as client:
            response = client.post(url, json=body)
    except httpx.TimeoutException:
        raise ProducerError(f"{url} 超时（{timeout:.0f} 秒）。") from None
    except httpx.HTTPError:
        raise ProducerError(f"无法连接 audio.cpp（{host}）：服务未启动或地址不对。") from None
    if response.status_code >= 400:
        raise ProducerError(f"{url} 返回 {response.status_code}：{_error(response)}")
    with contextlib.suppress(ValueError):
        return response.json()
    return {}


def loaded_models(host: str, timeout: float = 5.0) -> list[str]:
    """服务端注册的模型 id（``GET /v1/models``）。"""
    payload = _get(host, "/v1/models", timeout=timeout)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]


def models_root(host: str, timeout: float = 3.0) -> Path | None:
    """模型目录（``GET /v1/ui/models-root``）；未开动态管理时为空。"""
    try:
        payload = _get(host, "/v1/ui/models-root", timeout=timeout)
    except ProducerError:
        return None
    raw = str((payload or {}).get("models_root") or "").strip()
    return Path(raw) if raw else None


def install(host: str, package_id: str) -> dict[str, Any]:
    """按包 id 安装；进度由服务端任务表回，插件不自己下。"""
    payload = _post(host, "/v1/ui/models/install", {"id": package_id}, timeout=30.0)
    return payload if isinstance(payload, dict) else {}


def _decode(raw: str) -> bytes | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _audio_in(payload: Any, *, depth: int = 0) -> bytes | None:
    """在响应里找音频：先认显式字段，再认列表，最后认分离的多路输出。"""
    if depth > 3 or payload is None:
        return None
    if isinstance(payload, str):
        return _decode(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("audio", "audio_base64", "wav", "data"):
        value = payload.get(key)
        if isinstance(value, str):
            found = _decode(value)
            if found:
                return found
    for key in ("result", "response", "output"):
        found = _audio_in(payload.get(key), depth=depth + 1)
        if found:
            return found
    for key in ("outputs", "files", "audio_outputs"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            chosen = _pick_vocal(rows) or rows[0]
            found = _audio_in(chosen, depth=depth + 1)
            if found:
                return found
    rows = payload.get("artifacts")
    if isinstance(rows, list) and rows:
        chosen = _pick_vocal(rows) or rows[0]
        found = _audio_in(chosen, depth=depth + 1)
        if found:
            return found
    return None


def _pick_vocal(rows: list[Any]) -> Any | None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        tag = " ".join(
            str(row.get(key) or "") for key in ("kind", "name", "id", "label", "stem", "role")
        ).lower()
        if any(hint in tag for hint in _VOCAL_HINTS):
            return row
    return None


def run_task(
    host: str,
    *,
    model: str,
    audio: Path,
    options: dict[str, Any] | None = None,
    timeout: float = 600.0,
) -> bytes:
    """跑一次模型任务，返回音频字节。

    :param model: 服务端注册的模型 id（``/v1/models`` 里那一个）。
    :param audio: 服务端本机可读的 wav 路径。
    :param options: 模型自己的请求选项（``request.options``）。
    """
    body = {
        "model": model,
        "request": {"audio": str(audio), "options": dict(options or {})},
    }
    payload = _post(host, "/v1/tasks/run", body, timeout=timeout)
    audio_bytes = _audio_in(payload)
    if not audio_bytes:
        keys = "、".join(sorted(payload.keys())) if isinstance(payload, dict) else type(payload).__name__
        raise ProducerError(
            f"模型「{model}」返回里没有音频（响应字段：{keys}）；"
            "请核对 audio.cpp 的 /v1/tasks/run 响应结构。"
        )
    if len(audio_bytes) < 200:
        raise ProducerError(f"模型「{model}」返回的音频仅 {len(audio_bytes)} 字节。")
    return audio_bytes


def describe_response(payload: Any) -> str:
    """调试用：响应字段一览（不写进界面文案）。"""
    return json.dumps(sorted(payload.keys()), ensure_ascii=False) if isinstance(payload, dict) else ""
