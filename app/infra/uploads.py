"""上传流的分块限额读取：累计超限立刻停，不把整份内容吃进内存。

``UploadFile`` 的请求体已经落在 SpooledTemporaryFile 里，但不带参数 ``read()``
仍会把整份内容读进进程内存。录音、克隆音源、书稿三个入口共用这一条限额；
异常由各层翻成自己的错误码。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["CHUNK_BYTES", "UploadTooLarge", "read_limited"]

CHUNK_BYTES = 64 * 1024


class UploadTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        #: 界面说法只有这一处产地；调用方不再各自换算单位。
        self.label = (
            f"{limit // 1024 // 1024} MB"
            if limit >= 1024 * 1024
            else f"{limit // 1024} KB"
        )
        super().__init__(f"上传超过 {self.label}")


class _AsyncReader(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


async def read_limited(upload: _AsyncReader, limit: int) -> bytes:
    """按块读完；累计超过 ``limit`` 时立即抛 :class:`UploadTooLarge`。"""
    parts: list[bytes] = []
    size = 0
    while chunk := await upload.read(CHUNK_BYTES):
        size += len(chunk)
        if size > limit:
            raise UploadTooLarge(limit)
        parts.append(chunk)
    return b"".join(parts)
