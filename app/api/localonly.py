"""破坏性操作只认本机来源。

与 :func:`app.api.clientaddr.is_local_client` 同一份判据：它把"回环地址"当作本机，
不看 ``X-Forwarded-For``——本服务默认不信任任何代理头。

用在"改一条配置就可能动到用户文件"的接口上：改存储/输出目录（含 ``migrate``
搬迁）、在宿主机弹资源管理器、指定并执行本机可执行文件。普通读写、合成、播放
不受影响——局域网里的平板照常听书、照常触发合成。
"""

from __future__ import annotations

from fastapi import Request

from app.api.clientaddr import is_local_client
from app.api.errors import ApiError

__all__ = ["require_local"]


def require_local(
    request: Request,
    *,
    code: str = "local_only",
    message: str = "这个操作只能在本机使用。",
) -> None:
    if is_local_client(request):
        return
    raise ApiError(status=403, code=code, message=message)
