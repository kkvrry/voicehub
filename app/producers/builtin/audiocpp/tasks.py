"""audio.cpp 的通用任务客户端：一次请求可能返回多路音频。

端点 ``POST /v1/tasks/run``，请求体是
``{"model": id, "request": {"audio": path, "options": {...}}}``，音频只能给
**服务端本机路径**或 base64，不支持 multipart。同机部署传路径，省一次编码传输。

上游没有把响应结构写进公开文档，所以按已知的几种形状依次收：``named_audio_outputs``
（分离任务的多路输出，``[{id, audio}]``）、``audio``（单路）、``artifacts`` /
``outputs`` / ``files``（旧形状或包装）、``result`` / ``response`` / ``output``
（再套一层）。返回 ``{名字: wav 字节}``；名字来自响应的 ``id`` / ``kind`` / ``name``，
单路没有名字时叫 ``output``。取哪一路由调用方决定。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from pathlib import Path
from typing import Any

import httpx

from app.producers.base import ProducerError

__all__ = ["DIAR_RATE", "pick_vocal", "run_diar", "run_task", "vocal_name"]

#: 分离任务的产物里，这几个词表示"人声那一路"。
_VOCAL_HINTS = ("vocal", "voice", "speech", "target", "speaker")
#: 单路输出没有名字时用的键。
_MAIN = "output"


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


def _label(row: dict[str, Any], index: int) -> str:
    for key in ("id", "kind", "name", "label", "stem", "role"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return f"stem-{index}"


def _add(found: dict[str, bytes], name: str, data: bytes | None) -> None:
    if not data:
        return
    key = name.strip() or _MAIN
    # 同一段音频可能在 ``audio`` 与 ``named_audio_outputs`` 里各出现一次：
    # 按字节去重，避免把"同一路"当成两路。
    if any(existing == data for existing in found.values()):
        return
    # 同名输出只留第一份。
    found.setdefault(key, data)


def _collect(payload: Any, found: dict[str, bytes], depth: int = 0) -> None:
    """从响应里收音频；先认显式多路输出，再认单路，最后认包装层。"""
    if depth > 4 or payload is None:
        return
    if isinstance(payload, str):
        _add(found, _MAIN, _decode(payload))
        return
    if not isinstance(payload, dict):
        return

    rows = payload.get("named_audio_outputs")
    named_outputs = 0
    if isinstance(rows, list) and rows:
        before = len(found)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            _add(found, _label(row, index), _decode(str(row.get("audio") or "")))
        named_outputs = len(found) - before

    named = _label(payload, 0) if any(
        key in payload for key in ("kind", "name", "label", "stem", "role")
    ) else _MAIN
    for key in ("audio", "audio_base64", "wav", "data"):
        value = payload.get(key)
        # 多路命名输出已经收齐时，顶层 ``audio`` 多半是其中一路的重复（或整段合成）；
        # 不再把它当作另一路，否则伴奏会被多算一遍。
        if named_outputs and key == "audio":
            continue
        if isinstance(value, str):
            _add(found, named, _decode(value))

    for key in ("outputs", "files", "audio_outputs", "artifacts"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if isinstance(row, dict):
                    _collect_output_row(row, found, index)
                elif isinstance(row, str):
                    _add(found, _MAIN, _decode(row))

    for key in ("result", "response", "output"):
        if key in payload:
            _collect(payload.get(key), found, depth + 1)


def _collect_output_row(row: dict[str, Any], found: dict[str, bytes], index: int) -> None:
    label = _label(row, index)
    for key in ("audio", "audio_base64", "wav", "data"):
        value = row.get(key)
        if isinstance(value, str):
            _add(found, label, _decode(value))
            return
    _collect(row, found, depth=1)


def outputs_of(payload: Any) -> dict[str, bytes]:
    """响应 → ``{名字: wav 字节}``；没有音频时返回空表。"""
    found: dict[str, bytes] = {}
    _collect(payload, found)
    return found


def _audio_in(payload: Any) -> bytes | None:
    """旧调用方的入口：从响应里取人声；没有音频时返回 ``None``。"""
    found = outputs_of(payload)
    return pick_vocal(found) if found else None


def vocal_name(found: dict[str, bytes]) -> str:
    """多路输出里人声那一路的名字：按名字先认，再认单路，最后取第一路。"""
    for name in found:
        if any(hint in name.lower() for hint in _VOCAL_HINTS):
            return name
    if _MAIN in found:
        return _MAIN
    return next(iter(found), "")


def pick_vocal(found: dict[str, bytes]) -> bytes:
    """从多路输出里取人声。"""
    name = vocal_name(found)
    if not name:
        raise ProducerError("模型返回里没有音频。")
    return found[name]


def run_task(
    host: str,
    *,
    model: str,
    audio: Path,
    options: dict[str, Any] | None = None,
    timeout: float = 600.0,
) -> dict[str, bytes]:
    """跑一次模型任务，返回全部音频输出。

    :param model: 服务端注册的模型 id（``/v1/models`` 里那一个）。
    :param audio: 服务端本机可读的 wav 路径。
    :param options: 模型自己的请求选项（``request.options``）。
    """
    body = {
        "model": model,
        "request": {"audio": str(audio), "options": dict(options or {})},
    }
    payload = _post(host, "/v1/tasks/run", body, timeout=timeout)
    found = outputs_of(payload)
    if not found:
        keys = "、".join(sorted(payload.keys())) if isinstance(payload, dict) else type(payload).__name__
        raise ProducerError(
            f"模型「{model}」返回里没有音频（响应字段：{keys}）；"
            "请核对 audio.cpp 的 /v1/tasks/run 响应结构。"
        )
    if all(len(data) < 200 for data in found.values()):
        sizes = "、".join(f"{name} {len(data)} B" for name, data in found.items())
        raise ProducerError(f"模型「{model}」返回的音频过小（{sizes}）。")
    return found


#: 说话人分离的采样率：diar 模型只吃 16 kHz，turns 里的样本序号按它换算成秒。
DIAR_RATE = 16000


def run_diar(
    host: str,
    *,
    model: str,
    audio: Path,
    timeout: float = 3600.0,
) -> list[dict[str, Any]]:
    """跑一次说话人分离，返回 ``[{start, end, speaker, confidence}]``（秒）。

    分离不产音频：响应取 ``speaker_turns``（样本序号 + 说话人 + 置信度），
    与 :func:`run_task` 的音频解析路径不同。
    """
    body = {
        "model": model,
        "request": {"audio": str(audio), "options": {}},
    }
    payload = _post(host, "/v1/tasks/run", body, timeout=timeout)
    turns = payload.get("speaker_turns") if isinstance(payload, dict) else None
    if turns is None:
        # 没有说话轮次：无人声时模型只回 timing。任务确实跑过就当作
        # "没有说话人"，不算响应结构错误。
        if isinstance(payload, dict) and "timing" in payload:
            return []
        keys = "、".join(sorted(payload.keys())) if isinstance(payload, dict) else type(payload).__name__
        raise ProducerError(
            f"模型「{model}」返回里没有 speaker_turns（响应字段：{keys}）；"
            "请核对 audio.cpp 的 /v1/tasks/run 响应结构。"
        )
    if not isinstance(turns, list):
        raise ProducerError(
            f"模型「{model}」的 speaker_turns 不是列表（{type(turns).__name__}）。"
        )
    out: list[dict[str, Any]] = []
    for row in turns:
        if not isinstance(row, dict):
            continue
        start = int(row.get("start_sample") or 0) / DIAR_RATE
        end = int(row.get("end_sample") or 0) / DIAR_RATE
        speaker = str(row.get("speaker_id") or "")
        if end <= start or not speaker:
            continue
        out.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "speaker": speaker,
                "confidence": float(row.get("confidence") or 0.0),
            }
        )
    return out
