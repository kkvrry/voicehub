"""引擎 HTTP 客户端的进程内复用。

逐句合成会把"一条请求一个客户端"放大：每次新建 ``httpx.AsyncClient`` 就要重来一次
TCP 握手（云端再叠 TLS），而这份开销只与句数有关，与文本长度无关。

缓存键 = 事件循环 + 目标地址 + 超时 + 请求头指纹 + 重定向策略，于是：

- 设置页改了地址 / 超时 / 密钥，自然落到新键上，旧连接退役；
- 测试与 ``asyncio.run`` 每次新建事件循环，不会跨循环复用（跨循环会报
  ``attached to a different loop``）。

生命周期：命中缓存时给一个"借用"包装（退出 ``async with`` 不关连接）；池满时淘汰
最旧的一个并关闭它，其余随进程退出回收。带额外参数的调用（测试里的 ``transport``）
不缓存，仍由调用方 ``async with`` 关闭——那也是修复前的行为。
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections.abc import Mapping
from typing import Any

from app.engines.base import EngineError
from app.infra.netenv import normalize_no_proxy_env

__all__ = ["borrow", "post_within"]

#: 每个事件循环最多留几个客户端。真实场景是"每台引擎一个"，8 足够覆盖设置页来回改。
_MAX_PER_LOOP = 8

#: 事件循环 → {缓存键: 客户端}。弱引用：循环被回收时这一桶随之消失。
_BUCKETS: weakref.WeakKeyDictionary[Any, dict[tuple[Any, ...], Any]] = weakref.WeakKeyDictionary()

#: 淘汰时派生的关闭任务要留引用，否则可能在执行前被回收。
_RETIRING: set[Any] = set()


class _Borrowed:
    """借用一次池里的客户端：``async with`` 退出时**不**关闭它。"""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _headers_key(headers: Mapping[str, str] | None) -> str:
    """请求头指纹。密钥在头里（Authorization），所以只留摘要，不在键里留明文副本。"""
    if not headers:
        return ""
    material = "\n".join(f"{key}\x00{value}" for key, value in sorted(headers.items()))
    return hashlib.sha1(material.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _new_client(
    *,
    base_url: str,
    timeout: float,
    headers: Mapping[str, str] | None,
    trust_env: bool,
    follow_redirects: bool,
    extra: Mapping[str, Any],
) -> Any:
    import httpx

    if trust_env:
        normalize_no_proxy_env()
    try:
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            trust_env=trust_env,
            follow_redirects=follow_redirects,
            **extra,
        )
    except httpx.InvalidURL as exc:
        raise EngineError(
            f"HTTP 客户端配置无效：{exc}。目标地址：{base_url}。"
            "如果系统设置了 NO_PROXY，IPv6 请写成 ::1，不要写成 [::1]。",
        ) from exc


def _retire(client: Any) -> None:
    """把被淘汰的客户端关掉；没有运行中的循环就交给 GC。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(client.aclose())
    _RETIRING.add(task)
    task.add_done_callback(_RETIRING.discard)


def borrow(
    *,
    base_url: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    trust_env: bool = False,
    follow_redirects: bool = False,
    **extra: Any,
) -> Any:
    """取一个客户端（或借用包装）。``extra`` 非空时不缓存，直接交给调用方关闭。"""
    if extra:
        return _new_client(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            trust_env=trust_env,
            follow_redirects=follow_redirects,
            extra=extra,
        )
    loop = asyncio.get_running_loop()
    bucket = _BUCKETS.get(loop)
    if bucket is None:
        bucket = {}
        _BUCKETS[loop] = bucket
    key = (base_url, float(timeout), _headers_key(headers), trust_env, follow_redirects)
    client = bucket.get(key)
    if client is not None and not client.is_closed:
        return _Borrowed(client)
    if len(bucket) >= _MAX_PER_LOOP:
        _, stale = bucket.pop(next(iter(bucket)))
        _retire(stale)
    client = _new_client(
        base_url=base_url,
        timeout=timeout,
        headers=headers,
        trust_env=trust_env,
        follow_redirects=follow_redirects,
        extra={},
    )
    bucket[key] = client
    return _Borrowed(client)


async def post_within(client: Any, path: str, *, seconds: float, **kw: Any) -> Any:
    """发一次 POST，给「从发出到读完响应体」加总时长上限。

    httpx 自带的 timeout 管的是两段数据之间的空闲时间：服务端分块慢慢吐时，
    每收一块就重新计时，整次请求可以远超设置值。这里补上总时长上限；
    超时转成 ``httpx.TimeoutException``，复用各引擎已有的报错文案。
    """
    import httpx

    try:
        async with asyncio.timeout(seconds):
            return await client.post(path, **kw)
    except TimeoutError as exc:
        raise httpx.TimeoutException(f"整次请求超过 {seconds:g} 秒") from exc
