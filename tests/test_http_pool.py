"""引擎客户端池：同循环复用、跨循环隔离、借用不关连接。"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from app.engines.base import EngineError
from app.engines.http_pool import borrow, post_within


def _call(**overrides):
    options = {"base_url": "http://127.0.0.1:19999", "timeout": 5.0}
    options.update(overrides)
    return borrow(**options)


def test_the_same_target_reuses_one_client_inside_a_loop():
    """逐句合成走的是同一个客户端——复用连接，而不是每句一次握手。"""

    async def run():
        first = _call()
        second = _call()
        async with first as a, second as b:
            assert a is b
            return a

    client = asyncio.run(run())
    assert client.is_closed is False  # 退出 async with 不关连接：它还在池里


def test_a_changed_setting_gets_a_new_client():
    """设置页改了超时 / 地址，就该换一条连接，而不是继续用旧的。"""

    async def run():
        async with _call() as a, _call(timeout=9.0) as b:
            return a, b

    first, second = asyncio.run(run())
    assert first is not second


def test_proxy_behaviour_is_part_of_the_key():
    """本机引擎不走系统代理、云端引擎走——两种策略不能共用一条连接。"""

    async def run():
        async with _call() as direct, _call(trust_env=True) as proxied:
            return direct, proxied

    first, second = asyncio.run(run())
    assert first is not second


def test_bracketed_ipv6_no_proxy_does_not_break_client_creation(monkeypatch: pytest.MonkeyPatch):
    """真实的 Windows 环境会给出 ``[::1]``；httpx 0.28 会把它拼成非法模式而崩。
    引擎客户端创建前应把这种合法写法规范成 httpx 能认的 ``::1``。"""
    value = "localhost,.localhost,127.0.0.1,127.0.0.0/8,[::1],::1"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    async def run():
        async with _call(trust_env=True) as client:
            return client

    client = asyncio.run(run())
    assert client.is_closed is False
    assert os.environ["NO_PROXY"] == "localhost,.localhost,127.0.0.1,127.0.0.0/8,::1,::1"
    assert os.environ["no_proxy"] == "localhost,.localhost,127.0.0.1,127.0.0.0/8,::1,::1"


def test_invalid_client_url_is_reported_as_an_engine_error():
    """地址/代理解析失败不能在界面上落成含糊的“服务内部错误”。"""

    async def run():
        with pytest.raises(EngineError, match=r"HTTP 客户端配置无效.*目标地址"):
            borrow(base_url="http://[::1", timeout=5.0)

    asyncio.run(run())


def test_event_loops_do_not_share_clients():
    """``asyncio.run`` 每次都换循环；跨循环复用会报 attached to a different loop。"""

    async def run():
        async with _call() as client:
            return client

    first = asyncio.run(run())
    second = asyncio.run(run())
    assert first is not second


def test_calls_with_extra_options_are_not_cached():
    """测试注入的 ``transport`` 这类值不可哈希，也不该被留下——原样交给调用方关闭。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    async def run():
        async with _call(transport=transport) as a, _call(transport=transport) as b:
            return a, b

    first, second = asyncio.run(run())
    assert first is not second


class _SlowClient:
    """服务端一直在吐数据、但整次请求拖过时限的替身。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path: str, **kw: object) -> httpx.Response:
        self.calls.append((path, kw))
        await asyncio.sleep(self.delay)
        return httpx.Response(200, json={"ok": True})


def test_post_within_caps_the_whole_request():
    """分块慢吐时，空闲超时会一直被重置；总时长上限必须能掐断。"""
    client = _SlowClient(0.5)

    async def run():
        return await post_within(client, "/tts", seconds=0.05, json={"text": "你好"})

    with pytest.raises(httpx.TimeoutException):
        asyncio.run(run())
    assert client.calls  # 请求确实发出去了，不是被提前拦下


def test_post_within_returns_a_fast_response():
    client = _SlowClient(0.0)
    response = asyncio.run(post_within(client, "/tts", seconds=5.0, json={}))
    assert response.status_code == 200


def test_post_within_keeps_httpx_read_timeout():
    """httpx 自己的读超时要原样冒泡，不能被总时长那层吞掉或改口。"""

    class _NoBytes:
        async def post(self, *_args: object, **_kw: object) -> httpx.Response:
            raise httpx.ReadTimeout("no bytes")

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(post_within(_NoBytes(), "/tts", seconds=5.0))


def test_mimo_reports_the_total_timeout(monkeypatch: pytest.MonkeyPatch):
    """总时长超时要落到引擎那句可读报错上，而不是冒成 500。"""
    from app.engines.base import EngineError
    from app.engines.builtin.mimo import client as mimo_client
    from app.engines.builtin.mimo.settings import MimoSettings

    class _Ctx:
        async def __aenter__(self) -> _SlowClient:
            return _SlowClient(0.5)

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    async def fake_client(_cfg: object, **_kw: object) -> _Ctx:
        return _Ctx()

    monkeypatch.setattr(mimo_client, "_client", fake_client)
    cfg = MimoSettings(api_key="sk-test", timeout=0.05)
    with pytest.raises(EngineError, match="超时"):
        asyncio.run(mimo_client.synthesize(cfg, {"audio": {"format": "wav"}}, label="MiMo"))
