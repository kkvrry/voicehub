"""Windows SAPI 5 桥接配置与接口测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.infra import sapi_bridge


def _lan(client: TestClient) -> TestClient:
    return TestClient(client.app, client=("192.168.1.60", 50000))


def test_config_roundtrip_and_preserves_extra_fields(monkeypatch, tmp_path: Path) -> None:
    cfg_file = tmp_path / "bridge.json"
    monkeypatch.setenv("VOICEHUB_SAPI_CONFIG", str(cfg_file))

    default_cfg = sapi_bridge.read_config()
    assert default_cfg["engine"] == "auto"
    assert default_cfg["voice_id"] == ""

    saved = sapi_bridge.save_config(
        voice_id="voice-123",
        endpoint="http://127.0.0.1:17865/",
        previous_default_voice=r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\Demo",
    )
    assert saved["voice_id"] == "voice-123"
    assert saved["engine"] == "auto"
    assert saved["endpoint"] == "http://127.0.0.1:17865"
    assert saved["previous_default_voice"].endswith(r"\Demo")

    # 再次仅更新 voice_id 时保留 previous_default_voice 与 endpoint
    updated = sapi_bridge.save_config(voice_id="voice-456")
    assert updated["voice_id"] == "voice-456"
    assert updated["endpoint"] == "http://127.0.0.1:17865"
    assert updated["previous_default_voice"].endswith(r"\Demo")
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["voice_id"] == "voice-456"


def test_get_and_put_sapi_voice_via_api(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    cfg_file = tmp_path / "bridge.json"
    monkeypatch.setenv("VOICEHUB_SAPI_CONFIG", str(cfg_file))
    monkeypatch.setattr(sapi_bridge, "supported", lambda: True)
    monkeypatch.setattr(sapi_bridge, "is_installed", lambda: True)
    monkeypatch.setattr(sapi_bridge, "is_default", lambda: False)

    resp = client.get("/api/v1/sapi")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "sapi_bridge"
    assert body["engine"] == "auto"
    assert body["installed"] is True

    put_resp = client.put("/api/v1/sapi", json={"voice_id": "v_demo"})
    assert put_resp.status_code == 200
    put_body = put_resp.json()
    assert put_body["voice_id"] == "v_demo"
    assert put_body["engine"] == "auto"


def test_set_default_requires_installed(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOICEHUB_SAPI_CONFIG", str(tmp_path / "bridge.json"))
    monkeypatch.setattr(sapi_bridge, "supported", lambda: True)
    monkeypatch.setattr(sapi_bridge, "is_installed", lambda: False)

    resp = client.put("/api/v1/sapi", json={"is_default": True})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "sapi_not_installed"


def test_sapi_mutations_refused_from_lan(client: TestClient) -> None:
    lan = _lan(client)
    assert lan.get("/api/v1/sapi").status_code == 200

    for method, path, payload in (
        ("put", "/api/v1/sapi", {"voice_id": "v1"}),
        ("post", "/api/v1/sapi/install", {"voice_id": "v1", "set_default": True}),
        ("post", "/api/v1/sapi/uninstall", None),
    ):
        resp = getattr(lan, method)(path, json=payload) if payload is not None else getattr(lan, method)(path)
        assert resp.status_code == 403, path
        assert resp.json()["error"]["code"] == "local_only", path


def test_install_and_uninstall_routes_delegate_to_bridge(
    client: TestClient, monkeypatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_install(*, voice_id: str = "", endpoint: str = "", set_as_default: bool = True):
        captured.update(voice_id=voice_id, endpoint=endpoint, set_as_default=set_as_default)
        return {
            "object": "sapi_bridge",
            "supported": True,
            "installed": True,
            "is_default": set_as_default,
            "can_install": True,
            "voice_id": voice_id,
            "engine": "auto",
            "endpoint": endpoint,
            "fallback_voice": "Microsoft Huihui Desktop",
        }

    def _fake_uninstall():
        return {
            "object": "sapi_bridge",
            "supported": True,
            "installed": False,
            "is_default": False,
            "can_install": True,
            "voice_id": "",
            "engine": "auto",
            "endpoint": "http://127.0.0.1:17865",
            "fallback_voice": "Microsoft Huihui Desktop",
        }

    monkeypatch.setattr(sapi_bridge, "install", _fake_install)
    monkeypatch.setattr(sapi_bridge, "uninstall", _fake_uninstall)

    ins = client.post("/api/v1/sapi/install", json={"voice_id": "v_abc", "set_default": True})
    assert ins.status_code == 200
    assert ins.json()["installed"] is True
    assert captured["voice_id"] == "v_abc"
    assert captured["set_as_default"] is True

    unins = client.post("/api/v1/sapi/uninstall")
    assert unins.status_code == 200
    assert unins.json()["installed"] is False
