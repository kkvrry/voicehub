"""请求来自哪台机器——"本机"的唯一判据。

两处用同一份判断，必须同源：``/reveal`` 只允许本机触发（否则等于把"在宿主机弹
资源管理器"这个动作交给网段里的任何人）；访问令牌对本机免检（托盘与本机浏览器
不该被自己的登录页挡住）。
"""

from __future__ import annotations

from fastapi import Request

__all__ = ["is_local_client", "is_local_host"]

#: 视为"本机"的来源。``testclient`` 是 Starlette 测试客户端的地址（它不经过网络）。
_LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def is_local_host(host: str) -> bool:
    """这个来源地址是不是本机（回环 IPv4 / IPv6 / 测试客户端）。

    接地址而不是 ``Request``：ASGI 层的中间件（如压缩）拿不到 ``Request``，
    但它**必须**和接口层用同一份判断，否则"谁算本机"迟早会分叉。
    """
    host = (host or "").strip().lower()
    return host in _LOCAL_CLIENTS or host.startswith(("127.", "::ffff:127."))


def is_local_client(request: Request) -> bool:
    """这个请求是不是从本机发起的（回环 IPv4 / IPv6 / 测试客户端）。"""
    return is_local_host(request.client.host if request.client else "")
