"""OpenAI 兼容通道的共用 HTTP 传输。

只收「怎么发 HTTP」：连接复用、路径拼接、状态码与 JSON 解析。各家仍保留
自己的请求体、错误文案与鉴权头——那是插件的事实，不进公共层。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from app.infra.netenv import normalize_no_proxy_env
from app.llm.base import LlmError

__all__ = ["client", "endpoint", "request_json"]

_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None


def client() -> httpx.AsyncClient:
    """复用连接；按事件循环分开缓存（测试每次 ``asyncio.run`` 都是新循环）。"""
    global _CLIENT, _CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or _CLIENT_LOOP is not loop:
        normalize_no_proxy_env()
        _CLIENT = httpx.AsyncClient()
        _CLIENT_LOOP = loop
    return _CLIENT


def endpoint(root: str, path: str) -> str:
    """站点根 + 路径。已经写到具体端点的不再重复拼。"""
    base = (root or "").strip().rstrip("/")
    if base.endswith(path):
        return base
    return f"{base}{path}"


async def request_json(
    method: str,
    root: str,
    path: str,
    *,
    label: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    payload: dict[str, Any] | None = None,
    error_reader: Callable[[int, str], LlmError],
    transport_hint: str = "",
) -> dict[str, Any]:
    """发一次请求并解析 JSON；HTTP 错误交给调用方的 ``error_reader``。"""
    url = endpoint(root, path)
    try:
        response = await client().request(
            method, url, json=payload, headers=headers, timeout=timeout
        )
    except httpx.HTTPError as exc:
        hint = f"{transport_hint}。" if transport_hint else ""
        raise LlmError(
            f"连不上 {label}（{root}）：{exc}。{hint}", retryable=True
        ) from exc
    if response.status_code >= 400:
        raise error_reader(response.status_code, response.text)
    try:
        data = response.json()
    except ValueError as exc:
        raise LlmError(f"{label} 的响应不是 JSON：{response.text[:200]}") from exc
    if not isinstance(data, dict):
        raise LlmError(f"{label} 的响应不是 JSON 对象。")
    return data
