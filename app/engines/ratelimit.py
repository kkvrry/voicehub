"""按引擎限速：RPM 与并发是两件事，闸口要各管一维。

平台限的是 **RPM（每分钟接受的请求数）**，不是并发数。并发只决定"同时在跑几条"，
管不住"一分钟一共发了几条"：一句话的合成几百毫秒就返回，并发 3 也能打出每分钟
数百次的节奏，短句越多越快。反过来，限流被拒时平台在 0.1 秒内回错，信号量立刻
放行下一行——**拒绝得越快，空转越快**，直到这一分钟配额耗尽。

所以这里有两道闸，都是「每个引擎 + 每个主机」一份，跨任务、跨项目共享：

- **并发闸**：许可数取引擎设置里的 ``concurrency``，多项目同时合成也不会叠加。
- **速率闸**：滑动窗口，``rpm`` 次/60 秒；窗口满了就等到最早那次过期再发。
- **冷却**：平台已明确回限流时调用 :func:`penalize`，同一个键上的请求一起停，
  而不是各自立刻重试。

取值都来自引擎自己的设置模型（``rpm`` / ``concurrency``）；填 0 表示不启用该维，
没有这两个字段的引擎（本机推理）自然不受速率闸约束。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

__all__ = [
    "DEFAULT_WINDOW",
    "bucket_key",
    "limit_for",
    "limit_of",
    "penalize",
    "permit",
    "reset",
]

#: 平台配额的统计窗口。文档写的是 RPM，即 60 秒。
DEFAULT_WINDOW = 60.0

#: 单次等待上限：配置成 1 RPM 时一次要等一分钟，但等的过程要能被取消（任务中止）。
_MAX_WAIT = 30.0

#: 窗口末尾的余量。平台按它自己的窗口计数，我们的窗口与它不会逐毫秒对齐；
#: 不留余量时"刚好滑出窗口"的那批请求会撞在平台的边界上被拒。
_GUARD = 1.0


@dataclass
class _Bucket:
    """一个「引擎 + 主机」的闸口状态。"""

    stamps: deque[float] = field(default_factory=deque)
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    slots: asyncio.Semaphore | None = None
    slot_limit: int = 0


#: 按事件循环隔离：测试每个用例一个新循环，共用一份状态会串味。
_state: WeakKeyDictionary[Any, dict[str, _Bucket]] = WeakKeyDictionary()


def _bucket(key: str) -> _Bucket:
    loop = asyncio.get_running_loop()
    pool = _state.setdefault(loop, {})
    bucket = pool.get(key)
    if bucket is None:
        bucket = pool[key] = _Bucket()
    return bucket


def bucket_key(engine_key: str, cfg: Any) -> str:
    """闸口身份：同上一个站点的两个引擎各算一份，换站点不加宽额度。

    自定义地址参与键：走中转与直连是两份独立的平台配额。
    """
    host = str(getattr(cfg, "host", "") or getattr(cfg, "base_url", "") or "")
    host = host.strip().rstrip("/")
    return f"{engine_key}|{host}" if host else engine_key


def limit_of(cfg: Any, name: str) -> int:
    """设置项 → 闸口额度；缺字段、空值、非法值都算「不启用」。"""
    try:
        return max(0, int(getattr(cfg, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def limit_for(engine: Any, cfg: Any) -> int:
    """这台引擎的有效并发额度：设置值与本机硬上限取小；都没配置 = 0（不启用）。

    单发合成与克隆以前只读设置里的 ``concurrency``，而本机引擎的设置模型没有
    这个字段——于是 ``max_concurrency=1`` 只在批内生效，试听、两个项目并行、
    拉清单全都绕过了闸口，单个模型服务被同时打进来几条。
    """
    try:
        configured = int(engine.configured_concurrency(cfg) or 0)
    except (AttributeError, TypeError, ValueError):
        configured = limit_of(cfg, "concurrency")
    try:
        hard = int(engine.capabilities().max_concurrency or 0)
    except (AttributeError, TypeError, ValueError):
        hard = 0
    if configured and hard:
        return min(configured, hard)
    return configured or hard


@asynccontextmanager
async def permit(
    engine_key: str,
    cfg: Any,
    *,
    window: float = DEFAULT_WINDOW,
    concurrency: int | None = None,
) -> AsyncIterator[None]:
    """取一个并发位与一个速率配额，用完释放并发位。

    顺序固定：**先占并发位再取配额**。反过来会让一堆请求先耗掉配额再排队，
    窗口统计与实际发出时间错位。``window`` 只在测试里收窄。

    ``concurrency`` 显式给出时优先用它（调用方用 :func:`limit_for` 把引擎的硬上限
    算进来）；``None`` 保持旧行为——只读设置里的字段。
    """
    bucket = _bucket(bucket_key(engine_key, cfg))
    concurrency = (
        limit_of(cfg, "concurrency") if concurrency is None else max(0, int(concurrency))
    )
    rpm = limit_of(cfg, "rpm")
    async with _slot(bucket, concurrency):
        await _take(bucket, rpm, window)
        yield


@asynccontextmanager
async def _slot(bucket: _Bucket, limit: int) -> AsyncIterator[None]:
    if limit <= 0:
        yield
        return
    if bucket.slots is None or bucket.slot_limit != limit:
        # 设置改了就换一只：旧信号量继续服务已持有的请求，新请求按新上限排队。
        bucket.slots = asyncio.Semaphore(limit)
        bucket.slot_limit = limit
    async with bucket.slots:
        yield


async def _take(bucket: _Bucket, rpm: int, window: float = DEFAULT_WINDOW) -> None:
    """滑动窗口：窗口内已经发满 ``rpm`` 次，就等最早那次滑出窗口。

    ``rpm <= 0`` 表示不设速率上限，但**冷却照样生效**——那一轮是平台自己说的"太密"，
    与本地配了多大额度无关。
    """
    guard = min(_GUARD, window * 0.05)
    while True:
        async with bucket.lock:
            now = time.monotonic()
            while bucket.stamps and now - bucket.stamps[0] >= window:
                bucket.stamps.popleft()
            if bucket.cooldown_until > now:
                wait = bucket.cooldown_until - now
            elif rpm > 0 and len(bucket.stamps) >= rpm:
                wait = bucket.stamps[0] + window + guard - now
            else:
                if rpm > 0:
                    bucket.stamps.append(now)
                return
        await asyncio.sleep(min(max(wait, 0.01), _MAX_WAIT))


def penalize(engine_key: str, cfg: Any, seconds: float) -> None:
    """平台已明确限流：这个键上的请求统一停 ``seconds`` 秒。

    只让被拒的那一条重试是不够的——同一窗口里其余的请求会紧接着打上去，
    平台仍然拒绝，于是又变成一次空转。
    """
    if seconds <= 0:
        return
    bucket = _bucket(bucket_key(engine_key, cfg))
    until = time.monotonic() + seconds
    bucket.cooldown_until = max(bucket.cooldown_until, until)


def reset() -> None:
    """清空当前事件循环的窗口状态（测试用）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _state.pop(loop, None)
