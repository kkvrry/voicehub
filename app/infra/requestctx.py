"""请求上下文：把当前请求的标识带给不持有 ``Request`` 的层（中间件写，下游读）。

用 :class:`contextvars.ContextVar` 而非参数透传：避免让合成链上每个调用点
都背一个与业务无关的日志字段。``asyncio.to_thread`` 会复制上下文，
worker 线程里仍读得到同一个值。
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["REQUEST_ID", "current_request_id"]

#: 空串 = 不在一次 HTTP 请求里（启动期、后台任务）。
REQUEST_ID: ContextVar[str] = ContextVar("voicehub_request_id", default="")


def current_request_id() -> str:
    return REQUEST_ID.get()
