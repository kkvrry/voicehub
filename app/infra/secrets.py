"""本机密钥的落盘保护：Windows DPAPI（当前用户）加密后再写进 ``config.json``。

DPAPI 的密文只在这台机器、这个用户下可解——拷走 ``config.json`` 也拿不到密钥，
而这正是"个人工具"需要的那一档保护：不引入主密码，不改变用户的使用方式。
非 Windows / DPAPI 不可用时原样落盘（其它平台本来也没有这把伞，不能因此起不来）。

落盘格式带前缀与版本：``enc:dpapi:v1:<base64>``。读到没有前缀的值按旧的明文处理；
再次保存时自然升级成密文。解密失败不抛（配置坏了不该拦住服务），当作空值。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

__all__ = [
    "PREFIX",
    "available",
    "reveal",
    "reveal_fields",
    "seal",
    "seal_fields",
    "sealed",
]

PREFIX = "enc:dpapi:v1:"
#: 附加熵：同一用户下别的程序即使拿到密文，也不能直接解开。
_ENTROPY = b"voicehub:config:v1"
_UI_FORBIDDEN = 0x1

_LOG = logging.getLogger(__name__)


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


@lru_cache(maxsize=1)
def _api() -> tuple[Any, Any] | None:
    """返回 (crypt32, kernel32)；非 Windows 或加载失败时为 None。"""
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - 只会在异常系统上走到
        return None

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_Blob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_Blob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    # 调用方必须持有 buffer：blob 只存指针，buffer 被回收就成了野指针。
    return blob, buffer


def _crypt(data: bytes, *, protect: bool) -> bytes:
    pair = _api()
    if pair is None:
        raise OSError("这台机器上没有 DPAPI")
    crypt32, kernel32 = pair
    # 下划线前缀是给 lint 看的：这两个对象**必须**活到这个函数结束（见 _blob）。
    source, _keep_source = _blob(data)
    entropy, _keep_entropy = _blob(_ENTROPY)
    out = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "VoiceHub 配置",
            ctypes.byref(entropy),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(out),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            _UI_FORBIDDEN,
            ctypes.byref(out),
        )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def available() -> bool:
    """这台机器能不能加密。不能时读写都按原文处理，功能不因此缺失。"""
    return _api() is not None


def sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value: str) -> str:
    """明文 → 带前缀的密文；空值 / 已是密文 / 平台不支持时原样返回。"""
    text = str(value or "")
    if not text or sealed(text) or not available():
        return text
    try:
        payload = base64.b64encode(_crypt(text.encode("utf-8"), protect=True))
    except OSError as exc:  # pragma: no cover - DPAPI 调用失败（权限/系统异常）
        _LOG.warning("密钥加密失败，按明文保存：%s", exc)
        return text
    return PREFIX + payload.decode("ascii")


def reveal(value: str) -> str:
    """密文 → 明文；旧的无前缀值原样返回（配置可手改、可迁移）。"""
    text = str(value or "")
    if not sealed(text):
        return text
    try:
        raw = base64.b64decode(text[len(PREFIX) :], validate=True)
        return _crypt(raw, protect=False).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _LOG.warning("密钥解密失败（换了机器或用户？），按未填写处理：%s", exc)
        return ""


def seal_fields(stored: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """写盘前把指定字段加密；其余字段不动。"""
    out = dict(stored)
    for name in fields:
        value = out.get(name)
        if isinstance(value, str) and value:
            out[name] = seal(value)
    return out


def reveal_fields(stored: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """读配置后把指定字段解密；没有前缀的旧值原样保留。"""
    out = dict(stored)
    for name in fields:
        if name in out:
            out[name] = reveal(out[name])
    return out
