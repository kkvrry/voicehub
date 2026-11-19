"""audio.cpp 服务的薄客户端：跑一次分离或增强任务。

端点与 TTS 不同：走通用 ``POST /v1/tasks/run``，请求体是
``{"model": id, "request": {"audio": path, "options": {...}}}``，音频只能给
**服务端本机路径**或 base64，不支持 multipart。同机部署传路径，省一次编码传输。

上游没有把响应结构写进公开文档，所以音频取回按"已知的几种形状"依次试：``audio`` /
``result.audio`` / ``outputs[]`` / ``artifacts[]``（分离的多路输出走最后一种，取人声那一路）。
一种都对不上时，把响应里的键名原话带出来——那才是修下一版的依据。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from pathlib import Path
from typing import Any

import httpx

from app.producers.base import ProducerError

__all__ = ["run_task"]

#: 分离任务的产物里，这几个词表示"人声那一路"。
_VOCAL_HINTS = ("vocal", "voice", "speech", "target", "speaker")


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


def _post(host: str, path: str, body: dict[str, Any], timeout: float) -> Any:
    url = f"{host}{path}"
    try:
        with _client(timeout) as client:
            response = client.post(url, json=body)
    except httpx.TimeoutException:
        raise ProducerError(f"{url} 超时（{timeout:.0f} 秒）。") from None
    except httpx.HTTPError:
        raise ProducerError(f"无法连接 audio.cpp（{host}）：服务已退出或地址变了。") from None
    if response.status_code >= 400:
        raise ProducerError(f"{url} 返回 {response.status_code}：{_error(response)}")
    with contextlib.suppress(ValueError):
        return response.json()
    return {}


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
