"""破坏性操作只认本机；只读与合成不受影响（局域网照常用）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _lan(client: TestClient) -> TestClient:
    """同一个 app，来源地址换成局域网里的一台机器。"""
    return TestClient(client.app, client=("192.168.1.50", 50000))


def test_changing_storage_dirs_from_the_lan_is_refused(client: TestClient) -> None:
    lan = _lan(client)
    resp = lan.put("/api/v1/storage", json={"cache_dir": ""})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "storage_local_only"


def test_cache_policy_can_still_be_changed_from_the_lan(client: TestClient) -> None:
    """只有目录字段受限；回收线是两个数字，不碰文件。"""
    lan = _lan(client)
    resp = lan.put("/api/v1/storage", json={"cache_limit_mb": 64})
    assert resp.status_code == 200


def test_changing_the_listen_scope_from_the_lan_is_refused(client: TestClient) -> None:
    """把服务开到局域网是改这台机器的暴露面，只能在本机做。"""
    lan = _lan(client)
    resp = lan.put("/api/v1/service", json={"lan": True, "access_token": ""})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "local_only"


def test_changing_the_output_dir_from_the_lan_is_refused(
    client: TestClient, tmp_path
) -> None:
    lan = _lan(client)
    resp = lan.put("/api/v1/output", json={"dir": str(tmp_path / "generated")})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "output_local_only"


def test_opening_a_folder_from_the_lan_is_refused(client: TestClient) -> None:
    lan = _lan(client)
    resp = lan.post("/api/v1/storage/open", json={"key": "cache"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "reveal_local_only"


def test_opening_a_producer_field_from_the_lan_is_refused(client: TestClient) -> None:
    lan = _lan(client)
    resp = lan.post("/api/v1/producers/audiobook/settings/open", json={"field": "project_dir"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "reveal_local_only"


def test_audiocpp_settings_from_the_lan_are_refused(client: TestClient) -> None:
    """它的配置里能指定本机可执行文件：这个保存只认本机。"""
    lan = _lan(client)
    resp = lan.put("/api/v1/producers/audiocpp/settings", json={})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "producer_settings_local_only"


def test_audiocpp_runtime_mutations_from_the_lan_are_refused(client: TestClient) -> None:
    """运行包下载、启停、模型安装与卸载都只能在本机执行；状态查询保持开放。"""
    lan = _lan(client)
    assert lan.get("/api/v1/producers/audiocpp/state").status_code == 200
    for path, payload in (
        ("/api/v1/producers/audiocpp/settings", {"exe": "cmd.exe"}),
        ("/api/v1/producers/audiocpp/download", {}),
        ("/api/v1/producers/audiocpp/start", {}),
        ("/api/v1/producers/audiocpp/stop", {}),
        ("/api/v1/producers/audiocpp/models/install", {"id": "demo"}),
        ("/api/v1/producers/audiocpp/uninstall", {"confirm": True}),
    ):
        resp = lan.post(path, json=payload)
        assert resp.status_code == 403, path
        assert resp.json()["error"]["code"] == "audiocpp_local_only", path


def test_deleting_voices_or_samples_from_the_lan_is_refused(client: TestClient) -> None:
    """删除本地音色/样例会把宿主机文件移进回收站，只能在本机发起。"""
    lan = _lan(client)
    resp = lan.delete("/api/v1/voices/nonexistent")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "delete_local_only"
    resp2 = lan.delete("/api/v1/voices/nonexistent/samples/a.wav")
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "delete_local_only"


def test_reading_and_synthesis_stay_open_on_the_lan(client: TestClient) -> None:
    lan = _lan(client)
    assert lan.get("/api/v1/health").status_code == 200
    assert lan.get("/api/v1/voices").status_code == 200
