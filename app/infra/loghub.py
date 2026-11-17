"""日志的去处之一：进程内唯一出口——序号、环形快照、SSE 广播。

与 :class:`~app.domain.log.MemorySink` 的分工：后者无序号、只被读；这里每条配
进程内自增 ``seq``，既按游标补帧也实时推给界面。序号在落地时补（形状里不占位）。

线程与事件循环：生产者是任意工作线程，入队必须 ``call_soon_threadsafe``，
跨线程直接塞 ``asyncio.Queue`` 是未定义行为。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

from app.domain.log import ChannelLogHandler, LogChannel, LogRecord, MemorySink
from app.infra.config import log_base
from app.infra.logfile import FileSink

__all__ = ["CHANNEL", "HUB", "MEMORY", "LogHub", "install_logging"]

#: 一个订阅者最多允许积压的帧数；撑满丢最老的——用户想看的是最新发生的事。
_QUEUE = 500

#: 给界面补帧用的环形深度，与前端 1000 行一致（浅了断线重连会缺段）。
_RING = 1000


def _offer(queue: asyncio.Queue, item: tuple[int, dict[str, Any]]) -> None:
    """往订阅者队列放一帧；满则丢最老的。由 ``call_soon_threadsafe`` 调度，运行在事件循环线程。"""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # 腾出最老的那一格
            queue.put_nowait(item)
        except Exception:  # noqa: BLE001, S110 - 队列别的毛病也不该把循环带崩
            pass


class LogHub:
    """序号 + 环形快照 + 异步广播。实现 :class:`~app.domain.log.LogSink`。

    不做 IO（写文件是 :class:`~app.infra.logfile.FileSink` 的事）。
    :param limit: 环形深度（供补帧），见 :data:`_RING`。
    """

    def __init__(self, limit: int = _RING) -> None:
        self._lock = threading.Lock()
        self._limit = max(1, int(limit))
        self._seq = 0
        self._ring: deque[tuple[int, dict[str, Any]]] = deque(maxlen=self._limit)
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 落地（LogSink 协议）--------------------------------------------

    def emit(self, record: LogRecord) -> None:
        """配号、入环、推给订阅者；永不抛。"""
        with self._lock:
            self._seq += 1
            item = (self._seq, record.as_dict())
            self._ring.append(item)
            subs = list(self._subs)
            loop = self._loop
        if loop is None or not subs:
            return
        for queue in subs:
            try:
                loop.call_soon_threadsafe(_offer, queue, item)
            except RuntimeError:
                # 事件循环已关（进程收尾），丢掉这一路。
                continue

    # ---- 读 ---------------------------------------------------------------

    @property
    def cursor(self) -> int:
        """当前最大序号（前端把它当"我读到哪了"）。"""
        with self._lock:
            return self._seq

    @property
    def subscribers(self) -> int:
        """当前 SSE 连接数——生成器漏了自我摘除时，唯一看得出来的地方。"""
        with self._lock:
            return len(self._subs)

    def snapshot(self, since: int = 0, limit: int = 200) -> dict[str, Any]:
        """``since`` 之后的那一段（正序）。

        :param since: 客户端上次收到的最大 ``seq``（0 = 从头）。
        :param limit: 最多给多少条（从最新的往回数，首屏要"最近发生了什么"）。
        :return: ``{"lines", "cursor", "dropped"}``；``dropped`` = 环形转过一圈，
            ``since`` 之前缺了一段，界面据此提示而非假装连续。
        """
        start = max(0, int(since))
        with self._lock:
            items = [item for item in self._ring if item[0] > start]
            cursor = self._seq
            oldest = self._ring[0][0] if self._ring else 0
        # 只在"客户端起点早于环里最老一条、且环确实转过"时才算缺。
        dropped = bool(oldest and oldest > start + 1)
        if limit and len(items) > limit:
            items = items[-int(limit) :]
        return {
            "lines": [{"seq": seq, **data} for seq, data in items],
            "cursor": cursor,
            "dropped": dropped,
        }

    # ---- 订阅（SSE）------------------------------------------------------

    async def stream(self, since: int = 0):
        """异步生成器：先补 ``since`` 之后的一段，然后持续推新。

        先进订阅集合再取补帧，否则两步之间的行会永远丢失；重叠由前端按 ``seq``
        去重。``finally`` 里自我摘除，否则断开的连接会留下无人读的队列。
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._loop = loop
            backlog = [item for item in self._ring if item[0] > max(0, int(since))]
            self._subs.add(queue)
        try:
            for seq, data in backlog:
                yield {"seq": seq, **data}
            while True:
                seq, data = await queue.get()
                yield {"seq": seq, **data}
        finally:
            with self._lock:
                self._subs.discard(queue)


# --------------------------------------------------------------------------- #
# 进程级的那一份装配
# --------------------------------------------------------------------------- #

#: 生产者认识的那一个（`from app.infra.loghub import CHANNEL`）。模块级单例，
#: 与 `usecase/asr.py` 里那张 `_TASKS` 同一类东西：都是"这个进程里只有一份"的状态。
CHANNEL = LogChannel()

#: 给界面那一份（SSE / 快照）。
HUB = LogHub()

#: 没有别的去处时也攒着的那一份（进程刚开始、文件还没配好时）。
MEMORY = MemorySink()

_FILE_SINK: FileSink | None = None


def install_logging(*, level: int = logging.WARNING, log_path: Path | None = None) -> LogChannel:
    """把这几个去处分给通道，并给 stdlib 挂上适配器。**重复调用是安全的**。

    :param level: stdlib 那条路的最低级别，见 :class:`~app.domain.log.ChannelLogHandler`。
    :param log_path: 文件落点；不传就是日志目录（默认数据目录下的 ``logs``）的 ``voicehub.log``。

    文件落点每次按当前数据目录重算：测试中每个用例一套数据目录，写死会串目录。
    """
    global _FILE_SINK
    CHANNEL.add_sink(MEMORY)
    CHANNEL.add_sink(HUB)

    target = Path(log_path) if log_path is not None else log_base() / "voicehub.log"
    if _FILE_SINK is None or _FILE_SINK.path != target:
        if _FILE_SINK is not None:
            CHANNEL.remove_sink(_FILE_SINK)
        _FILE_SINK = FileSink(target)
    CHANNEL.add_sink(_FILE_SINK)

    root = logging.getLogger()
    if not any(
        isinstance(handler, ChannelLogHandler) and handler.channel is CHANNEL
        for handler in root.handlers
    ):
        root.addHandler(ChannelLogHandler(CHANNEL, level=level))
    return CHANNEL
