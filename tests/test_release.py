"""发布检查：只读、可缓存，网络失败与限流都不该变成接口故障。"""

from __future__ import annotations

import httpx
import pytest

from app.infra import releases


@pytest.fixture(autouse=True)
def _clear_cache():
    releases._CACHE = None
    yield
    releases._CACHE = None


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        releases.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


def _release(tag: str = "v0.2.0") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "tag_name": tag,
            "name": f"VoiceHub {tag.lstrip('v')}",
            "published_at": "2026-05-01T00:00:00Z",
            "html_url": f"https://github.com/kkvrry/voicehub/releases/tag/{tag}",
        },
    )


def _source(**overrides):
    info = {
        "mode": "source",
        "commit": "abc1234",
        "ahead": 3,
        "dirty": False,
        "base_version": "0.1.0",
    }
    info.update(overrides)
    return info


def test_version_tuple_accepts_v_prefix_and_suffix():
    assert releases._version_tuple("v0.2.0-beta.1") == (0, 2, 0)
    assert releases._version_tuple("0.1.0") == (0, 1, 0)
    assert releases._version_tuple("dev") is None


def test_new_release_is_reported(monkeypatch: pytest.MonkeyPatch):
    _mock_http(monkeypatch, lambda request: _release())
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    result = releases.check("0.1.0", refresh=True)

    assert result["update_available"] is True
    assert result["latest"]["tag"] == "v0.2.0"
    assert result["error"] is None


def test_a_source_checkout_leading_the_release_is_not_prompted(monkeypatch: pytest.MonkeyPatch):
    _mock_http(monkeypatch, lambda request: _release("v0.1.0"))
    monkeypatch.setattr(releases, "_source_info", lambda: _source(ahead=15))

    result = releases.check("0.1.0", refresh=True)

    assert result["update_available"] is False
    assert result["current"]["ahead"] == 15


def test_no_release_is_a_normal_result(monkeypatch: pytest.MonkeyPatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(404))
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    result = releases.check("0.1.0", refresh=True)

    assert result["update_available"] is False
    assert result["latest"] is None
    assert result["error"]["code"] == "no_release"


def test_rate_limit_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch):
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
    )
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    result = releases.check("0.1.0", refresh=True)

    assert result["error"]["code"] == "rate_limited"


def test_network_failure_is_reported(monkeypatch: pytest.MonkeyPatch):
    def boom(request):
        raise httpx.ConnectError("offline", request=request)

    _mock_http(monkeypatch, boom)
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    result = releases.check("0.1.0", refresh=True)

    assert result["error"]["code"] == "unreachable"
    assert "GitHub" in result["error"]["message"]


def test_success_is_cached(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def handler(request):
        calls.append(request)
        return _release()

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    releases.check("0.1.0", refresh=True)
    releases.check("0.1.0")

    assert len(calls) == 1


def test_route_returns_the_check(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        releases,
        "_fetch_latest",
        lambda: {
            "tag": "v0.2.0",
            "version": "0.2.0",
            "name": "VoiceHub 0.2.0",
            "published_at": "2026-05-01T00:00:00Z",
            "url": "https://example.test/release",
        },
    )
    monkeypatch.setattr(releases, "_source_info", lambda: _source())

    body = client.get("/api/v1/release", params={"refresh": 1}).json()

    assert body["object"] == "release_check"
    assert body["latest"]["tag"] == "v0.2.0"
    assert body["current"]["mode"] == "source"
