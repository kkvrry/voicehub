"""日志的统一形状与进程内唯一的汇聚点。

- :class:`LogRecord` 是唯一形状；:class:`LogSink` 是落地端协议（写文件 / 环形缓冲 / SSE 各自实现）；
- :class:`LogChannel` 是生产者唯一认识的对象，只分发不做 IO 决策；
- 放在 domain 层：形状是纯逻辑，落地端在各层实现协议，加去处不用改这里；
- ``seq`` 由 :meth:`LogSink.emit` 落地时补齐，前端游标去重逻辑因此照旧可用。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "DEFAULT_SOURCE",
    "LEVELS",
    "LEVEL_BY_NO",
    "ChannelLogHandler",
    "LogChannel",
    "LogRecord",
    "LogSink",
    "MemorySink",
    "now",
    "source_of",
]

#: 四档级别，与有声书那一侧逐字相同（两仓日志栏需一致）。不加第五档。
LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error")

#: 来源缺失时的兜底。意思是"这条日志没说自己从哪来"，前端照字面显示。
DEFAULT_SOURCE = "unknown"


def now() -> datetime:
    """一条日志的时刻，带时区。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LogRecord:
    """一条日志。

    :param level: 见 :data:`LEVELS`。通道不校验级别——不为一个词丢掉一条日志。
    :param message: 给人看的一句话。
    :param source: 点分命名空间（``asr.install`` / ``gateway`` / ``engines`` 等）。
        新增来源直接写新字符串，无登记手续。
    :param at: 时刻。
    :param ctx: 开放的结构化字段（``task`` / ``engine`` / ``voice``……），
        值必须 JSON 可序列化。
    """

    level: str
    message: str
    source: str = DEFAULT_SOURCE
    at: datetime = field(default_factory=now)
    ctx: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """摊平成事件帧那一半（``seq`` 由落地端补）。"""
        return {
            "level": self.level,
            "source": self.source or DEFAULT_SOURCE,
            "message": self.message,
            "at": self.at.isoformat(),
            "ctx": dict(self.ctx),
        }


class LogSink(Protocol):
    """一个落地端。协议而非基类：sink 分属不同层，只需长得像。"""

    def emit(self, record: LogRecord) -> None:
        """落地一条。不许抛——落地失败不得带崩任务；如需上报须防重入。"""
        ...


class LogChannel:
    """进程内唯一的汇聚点，随进程存活，生产者只认识它。

    只分发，不做 IO 决策；一个 sink 失败不影响其他 sink。
    线程安全：加锁保护 sink 列表；发布时先快照再逐个发，避免持锁重入死锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sinks: list[LogSink] = []

    # ---- 注册 -------------------------------------------------------------

    def add_sink(self, sink: LogSink) -> None:
        """挂上一个去处。重复挂同一个会被忽略（避免每条日志写两遍）。"""
        with self._lock:
            if any(existing is sink for existing in self._sinks):
                return
            self._sinks.append(sink)

    def remove_sink(self, sink: LogSink) -> None:
        """摘掉一个去处（任务结束时用）。没挂过则忽略。"""
        with self._lock:
            self._sinks = [item for item in self._sinks if item is not sink]

    @property
    def sinks(self) -> tuple[LogSink, ...]:
        with self._lock:
            return tuple(self._sinks)

    # ---- 发布 -------------------------------------------------------------

    def publish(self, record: LogRecord) -> None:
        """分发出去，永不抛。失败的 sink 直接跳过——上报本身也走通道，会无限递归。"""
        for sink in self.sinks:  # 快照：分发途中改列表也不会出岔子
            try:
                sink.emit(record)
            except Exception:  # noqa: BLE001, S112 - 一个去处坏了不该连累别的
                continue

    def log(self, level: str, message: str, *, source: str = DEFAULT_SOURCE, **ctx: Any) -> None:
        """便捷入口——绝大多数调用点只想要"记一句话"。"""
        self.publish(LogRecord(level=level, message=message, source=source, ctx=ctx))

    def debug(self, message: str, *, source: str = DEFAULT_SOURCE, **ctx: Any) -> None:
        self.log("debug", message, source=source, **ctx)

    def info(self, message: str, *, source: str = DEFAULT_SOURCE, **ctx: Any) -> None:
        self.log("info", message, source=source, **ctx)

    def warning(self, message: str, *, source: str = DEFAULT_SOURCE, **ctx: Any) -> None:
        self.log("warning", message, source=source, **ctx)

    def error(self, message: str, *, source: str = DEFAULT_SOURCE, **ctx: Any) -> None:
        self.log("error", message, source=source, **ctx)


class MemorySink:
    """进程级环形缓冲，随进程存活，补上"任务结束后无日志可看"的缺口。

    不推 SSE，只被快照读：推送的每一行都要有 ``seq``，而这里没有序号来源。

    :param limit: 保留条数，几百条足够排查；全量历史在文件里。
    """

    def __init__(self, limit: int = 300) -> None:
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._records: deque[LogRecord] = deque(maxlen=self._limit)

    def emit(self, record: LogRecord) -> None:
        with self._lock:
            self._records.append(record)

    def recent(self, limit: int = 100) -> list[LogRecord]:
        """最近若干条（**正序**，老的在先）。"""
        with self._lock:
            items = list(self._records)
        return items[-max(0, int(limit)) :]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


# --------------------------------------------------------------------------- #
# stdlib logging → 通道
# --------------------------------------------------------------------------- #

#: 放在 domain：挂载点在进程入口，翻译器本身不做 IO，与通道同居一层。

#: stdlib 级别号 → 通道级别。CRITICAL 并入 error。
LEVEL_BY_NO: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

#: 剥掉的包前缀。它在每一行里都一模一样，留着只是占地方。
_PACKAGE = "app."

#: 转发的重入标记（**按线程**：日志可能从任何一个工作线程里写出来）。
_BUSY = threading.local()


def source_of(name: str) -> str:
    """``logging`` 的 logger 名 → 来源：剥掉 ``app.`` 前缀，其余原样保留。

    不做翻译——翻译表会变成新的登记负担，漏登记的日志会被归错来源。
    """
    text = str(name or "").strip()
    text = text.removeprefix(_PACKAGE)
    return text or "system"


class ChannelLogHandler(logging.Handler):
    """stdlib 记录 → 统一通道，让插件与第三方库的 warning 到得了界面。

    :param channel: 汇聚点，只分发不落地。
    :param level: 转发的最低级别。默认 ``WARNING``：DEBUG/INFO 太碎，需诊断细节时调低即可。
    """

    def __init__(self, channel: LogChannel, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._channel = channel

    @property
    def channel(self) -> LogChannel:
        """转发目标；同一通道挂两遍会导致每条日志出现两次。"""
        return self._channel

    def emit(self, record: logging.LogRecord) -> None:
        """翻译并转发，永不抛。"""
        if getattr(_BUSY, "on", False):
            return  # 通道里某个 sink 又在写 stdlib 日志——掐断，别转下去
        _BUSY.on = True
        try:
            self._channel.publish(
                LogRecord(
                    level=LEVEL_BY_NO.get(record.levelno, "info"),
                    message=record.getMessage(),
                    source=source_of(record.name),
                    ctx=_ctx_of(record),
                )
            )
        except Exception:  # noqa: BLE001, S110 - 转发失败绝不能影响正在跑的那件事
            pass
        finally:
            _BUSY.on = False


def _ctx_of(record: logging.LogRecord) -> dict[str, Any]:
    """能带上的结构化字段：有 ``exc_info`` 时带异常类型，没有则不给空键。"""
    ctx: dict[str, Any] = {}
    if record.exc_info:
        ctx["exc_type"] = getattr(record.exc_info[0], "__name__", "")
    return ctx
