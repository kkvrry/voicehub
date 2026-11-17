"""系统代理环境：``[::1]`` 换成 httpx 认的 ``::1``，其余 token 原样保留。"""

from __future__ import annotations

import os

import pytest

from app.infra.netenv import normalize_no_proxy_env


def test_bracketed_ipv6_tokens_are_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    value = " localhost, .localhost ,127.0.0.1,127.0.0.0/8,[::1],[2001:DB8::1],::1, "
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    normalize_no_proxy_env()

    expected = "localhost,.localhost,127.0.0.1,127.0.0.0/8,::1,2001:DB8::1,::1"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected


def test_brackets_that_are_not_ipv6_stay_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "example.com,[not-an-ip],*.local,[::1]"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    normalize_no_proxy_env()

    expected = "example.com,[not-an-ip],*.local,::1"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected
