"""环境代理的归一与本机地址枚举。

前者：把 ``NO_PROXY`` 里 httpx 解析不了的 IPv6 写法改成它的。

Windows 常见 ``NO_PROXY=...,[::1],::1``。带方括号是合法写法，但 httpx 0.28
只把不带括号的 ``::1`` 认成 IPv6：它会拼出 ``all://*[::1]``，把 ``:1]`` 当成
端口，于是任何读环境代理（``trust_env=True``，默认）的客户端都在构造时抛
``InvalidURL``。这类客户端在创建前调用 :func:`normalize_no_proxy_env`。

后者：自签证书的 SAN 与启动日志里的局域网地址同取 :func:`lan_addresses`，
两处不能各列一份。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket

__all__ = ["lan_addresses", "normalize_no_proxy_env"]

_BRACKETED_IPV6 = re.compile(r"^\[([0-9A-Fa-f:.]+)\]$")


def _normalize(value: str) -> str:
    tokens = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        match = _BRACKETED_IPV6.fullmatch(token)
        if match:
            try:
                ipaddress.IPv6Address(match.group(1))
            except ValueError:
                pass
            else:
                token = match.group(1)
        tokens.append(token)
    return ",".join(tokens)


def normalize_no_proxy_env() -> None:
    """把 ``NO_PROXY`` / ``no_proxy`` 里的 ``[::1]`` 规范成 httpx 认的 ``::1``。"""
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name)
        if not value:
            continue
        normalized = _normalize(value)
        if normalized != value:
            os.environ[name] = normalized


def lan_addresses() -> list[str]:
    """本机的局域网 IPv4（去重、跳过回环）。取不到给空表——地址只用于提示与证书 SAN，不是启动条件。"""
    found: list[str] = []
    try:
        resolved = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        resolved = []
    for _family, _type, _proto, _canon, sockaddr in resolved:
        address = sockaddr[0]
        if not address.startswith("127.") and address not in found:
            found.append(address)
    try:
        # 不产生流量：connect 一个 UDP 套接字只用于让系统选出默认出口网卡的地址。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            primary = probe.getsockname()[0]
        if not primary.startswith("127.") and primary not in found:
            found.insert(0, primary)
    except OSError:
        pass
    return found
