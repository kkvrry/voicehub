"""限速闸：并发与 RPM 各管一维（见 ``app/engines/ratelimit.py``）。"""

from __future__ import annotations

import asyncio
import time

from app.engines import ratelimit
from app.engines.base import (
    CODE_RATE_LIMITED,
    KIND_CLOUD,
    KIND_LOCAL,
    Capabilities,
    EngineError,
)
from app.usecase import gateway


class _Cfg:
    """最小的配置替身：只带闸口要读的四个字段。"""

    def __init__(
        self,
        *,
        rpm: int = 0,
        concurrency: int = 0,
        host: str = "https://api.example.com",
        base_url: str = "",
    ) -> None:
        self.rpm = rpm
        self.concurrency = concurrency
        self.host = host
        self.base_url = base_url


def test_rpm_zero_means_no_rate_gate() -> None:
    async def run() -> float:
        cfg = _Cfg(rpm=0)
        started = time.perf_counter()
        for _ in range(30):
            async with ratelimit.permit("fake", cfg, window=5.0):
                pass
        return time.perf_counter() - started

    try:
        assert asyncio.run(run()) < 0.2
    finally:
        ratelimit.reset()


def test_rpm_window_holds_the_burst() -> None:
    """窗口内只放 rpm 条，其余等到最早那条滑出窗口。"""

    async def run() -> float:
        cfg = _Cfg(rpm=2)
        started = time.perf_counter()
        for _ in range(4):
            async with ratelimit.permit("fake", cfg, window=0.4):
                pass
        return time.perf_counter() - started

    try:
        # 4 条 / 每窗 2 条 → 第二批要等满一个窗口。
        assert asyncio.run(run()) >= 0.35
    finally:
        ratelimit.reset()


def test_concurrency_caps_in_flight() -> None:
    """并发位与速率分开：rpm 不限时，同时在跑的不超过 concurrency。"""

    async def run() -> int:
        cfg = _Cfg(concurrency=2)
        peak = 0
        live = 0

        async def one() -> None:
            nonlocal peak, live
            async with ratelimit.permit("fake", cfg):
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.02)
                live -= 1

        await asyncio.gather(*(one() for _ in range(8)))
        return peak

    try:
        assert asyncio.run(run()) == 2
    finally:
        ratelimit.reset()


class _LocalEngine:
    """本机单模型服务：设置里没有 concurrency，只声明硬上限 1。"""

    key = "local"
    label = "本机"

    def configured_concurrency(self, cfg: object) -> int:
        return int(getattr(cfg, "concurrency", 0) or 0)

    def capabilities(self) -> Capabilities:
        return Capabilities(kind=KIND_LOCAL, max_concurrency=1)


def test_limit_for_takes_the_smaller_of_setting_and_hard_cap() -> None:
    engine = _LocalEngine()
    assert ratelimit.limit_for(engine, _Cfg(concurrency=0)) == 1
    assert ratelimit.limit_for(engine, _Cfg(concurrency=4)) == 1


def test_an_explicit_limit_serializes_without_a_setting() -> None:
    async def run() -> int:
        cfg = _Cfg(concurrency=0)
        limit = ratelimit.limit_for(_LocalEngine(), cfg)
        live = peak = 0

        async def one() -> None:
            nonlocal live, peak
            async with ratelimit.permit("local", cfg, concurrency=limit):
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.02)
                live -= 1

        await asyncio.gather(*(one() for _ in range(4)))
        return peak

    try:
        assert asyncio.run(run()) == 1
    finally:
        ratelimit.reset()


def test_gateway_serializes_a_local_engine_without_a_concurrency_setting() -> None:
    """单发合成也要吃硬上限：以前本机引擎的 max_concurrency 只在批内生效。"""

    class _Busy(_LocalEngine):
        key = "local-gateway"

        def __init__(self) -> None:
            self.live = 0
            self.peak = 0

        async def synthesize(self, cfg: object, request: object) -> str:
            self.live += 1
            self.peak = max(self.peak, self.live)
            await asyncio.sleep(0.02)
            self.live -= 1
            return "audio"

    engine = _Busy()
    cfg = _Cfg(concurrency=0)

    async def run() -> None:
        await asyncio.gather(
            *(gateway._call_engine(engine, cfg, None) for _ in range(4))
        )

    try:
        asyncio.run(run())
        assert engine.peak == 1
    finally:
        ratelimit.reset()


def test_penalize_cools_the_whole_bucket() -> None:
    async def run() -> float:
        cfg = _Cfg()
        ratelimit.penalize("fake", cfg, 0.3)
        started = time.perf_counter()
        async with ratelimit.permit("fake", cfg):
            pass
        return time.perf_counter() - started

    try:
        assert asyncio.run(run()) >= 0.25
    finally:
        ratelimit.reset()


def test_buckets_are_split_by_host() -> None:
    """直连与中转是两份配额，互不占用。"""

    async def run() -> float:
        direct = _Cfg(rpm=1, host="https://a.example.com")
        relay = _Cfg(rpm=1, host="https://b.example.com")
        async with ratelimit.permit("fake", direct, window=10.0):
            pass
        started = time.perf_counter()
        async with ratelimit.permit("fake", relay, window=10.0):
            pass
        return time.perf_counter() - started

    try:
        assert asyncio.run(run()) < 0.1
    finally:
        ratelimit.reset()


class _Engine:
    """合成签名与插件一致；按次数决定这一次是限流还是成功。"""

    key = "fake"

    def __init__(self, failures: int, code: str = CODE_RATE_LIMITED) -> None:
        self.failures = failures
        self.code = code
        self.calls = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(kind=KIND_CLOUD)

    async def synthesize(self, cfg: object, request: object) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise EngineError("平台限流", retryable=True, code=self.code)
        return "audio"


def test_gateway_retries_after_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "_RATE_BACKOFF", (0.02, 0.02))
    engine = _Engine(failures=1)
    try:
        assert asyncio.run(gateway._call_engine(engine, _Cfg(), None)) == "audio"
        assert engine.calls == 2
    finally:
        ratelimit.reset()


def test_gateway_gives_up_after_the_backoff_list(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "_RATE_BACKOFF", (0.01, 0.01))
    engine = _Engine(failures=99)
    try:
        try:
            asyncio.run(gateway._call_engine(engine, _Cfg(), None))
        except EngineError as exc:
            assert exc.code == CODE_RATE_LIMITED
        else:  # pragma: no cover - 没有抛出就是错的
            raise AssertionError("限流用尽后应当抛出")
        assert engine.calls == 3
    finally:
        ratelimit.reset()


def test_gateway_does_not_retry_other_errors(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "_RATE_BACKOFF", (0.01, 0.01))
    engine = _Engine(failures=99, code="engine_failed")
    try:
        try:
            asyncio.run(gateway._call_engine(engine, _Cfg(), None))
        except EngineError as exc:
            assert exc.code == "engine_failed"
        else:  # pragma: no cover
            raise AssertionError("非限流错误应当直接抛出")
        assert engine.calls == 1
    finally:
        ratelimit.reset()
