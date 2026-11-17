"""访问令牌：默认不启用；配了之后**本机照旧、其它来源要带令牌**。

这一层钉的是"开放到局域网 ≠ 谁都能操作"：没有令牌时行为与以前一模一样（本机个人
工具），配了之后界面一打开就是登录页、接口 401，本机（回环）不受影响。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "s3cret-token"


def _write_token(data_dir: Path, token: str = TOKEN) -> None:
    from app.infra.config import load_config, save_config

    config = load_config()
    config.access_token = token
    save_config(config)


def _app():
    from app.main import create_app

    return create_app()


@pytest.fixture()
def guarded(data_dir: Path):
    """配了令牌的 app + 一台"局域网来的"客户端。"""
    _write_token(data_dir)
    return TestClient(_app(), client=("192.168.1.50", 51000))


@pytest.fixture()
def unguarded(data_dir: Path):
    """没配令牌（默认）——行为与以前一致。"""
    return TestClient(_app(), client=("192.168.1.50", 51000))


def test_without_a_token_everything_stays_open(unguarded) -> None:
    """默认值不变：本机个人工具，绑通配就是"可信网络里谁都能用"。"""
    assert unguarded.get("/api/v1/health").status_code == 200
    assert unguarded.get("/").status_code in {200, 404}


def test_the_local_machine_never_needs_the_token(data_dir: Path) -> None:
    """托盘与本机浏览器不该被自己的登录页挡住。"""
    _write_token(data_dir)
    local = TestClient(_app())  # 默认来源是 testclient，算本机
    assert local.get("/api/v1/health").status_code == 200
    assert local.get("/").status_code == 200


def test_a_lan_client_is_refused_and_told_why(guarded) -> None:
    resp = guarded.get("/api/v1/health")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "token_required"
    assert body["error"]["message"]
    # 被挡住这件事也要能追：响应头与错误体里的 id 是同一个。
    assert body["error"]["request_id"]
    assert resp.headers["X-Request-Id"] == body["error"]["request_id"]
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_the_header_token_opens_the_api(guarded) -> None:
    ok = guarded.get("/api/v1/health", headers={"X-VoiceHub-Token": TOKEN})
    assert ok.status_code == 200


def test_a_bearer_token_works_too(guarded) -> None:
    ok = guarded.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_a_wrong_token_is_refused(guarded) -> None:
    assert guarded.get("/api/v1/health", headers={"X-VoiceHub-Token": "nope"}).status_code == 401


def test_the_browser_is_walked_to_the_login_page(guarded) -> None:
    resp = guarded.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


def test_login_sets_a_cookie_and_comes_back(guarded) -> None:
    entered = guarded.post(
        "/login", data={"token": TOKEN, "next": "/"}, follow_redirects=False
    )
    assert entered.status_code == 303
    assert entered.headers["location"] == "/"
    cookie = entered.cookies.get("voicehub_token")
    assert cookie and cookie != TOKEN  # cookie 里是派生值，不是口令本身

    # 带上 cookie 之后：界面与接口都通了。
    guarded.cookies.set("voicehub_token", cookie)
    assert guarded.get("/api/v1/health").status_code == 200


def test_the_raw_token_is_not_a_valid_cookie(guarded) -> None:
    """把口令本身塞进 cookie 不该被当成已登录（也说明派生值确实参与了判定）。"""
    guarded.cookies.set("voicehub_token", TOKEN)
    assert guarded.get("/api/v1/health").status_code == 401


def test_a_token_with_awkward_characters_still_works(data_dir: Path) -> None:
    """口令可能带 ``;`` / 空格：cookie 存派生值，所以登录不会被字符集搞坏。"""
    awkward = "a b;c=你好"
    _write_token(data_dir, awkward)
    client = TestClient(_app(), client=("192.168.1.50", 51000))

    entered = client.post(
        "/login", data={"token": awkward, "next": "/"}, follow_redirects=False
    )
    assert entered.status_code == 303
    client.cookies.set("voicehub_token", entered.cookies.get("voicehub_token"))
    assert client.get("/api/v1/health").status_code == 200


def test_a_bad_login_stays_on_the_page(guarded) -> None:
    resp = guarded.post("/login", data={"token": "wrong", "next": "/"})
    assert resp.status_code == 401
    assert "令牌不对" in resp.text


def test_login_never_redirects_off_site(guarded) -> None:
    """``next`` 只认本站路径：不然登录页会变成开放重定向。"""
    for evil in ("https://example.com/", "//example.com/", ""):
        resp = guarded.post(
            "/login", data={"token": TOKEN, "next": evil}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


def test_the_login_page_itself_is_reachable(guarded) -> None:
    page = guarded.get("/login", follow_redirects=False)
    assert page.status_code == 200
    assert "访问令牌" in page.text
    # 自带样式、无外部依赖：没有前端包也能登录。
    assert "<style>" in page.text


def test_the_auth_state_endpoint_does_not_leak_the_token(guarded) -> None:
    body = guarded.get("/auth").json()
    assert body["required"] is True
    assert body["ok"] is False
    assert TOKEN not in str(body)

    body = guarded.get("/auth", headers={"X-VoiceHub-Token": TOKEN}).json()
    assert body["ok"] is True


def test_the_environment_wins_over_the_file(data_dir: Path, monkeypatch) -> None:
    """``VOICEHUB_TOKEN`` 优先——临时加锁不必改文件。"""
    from app.api.auth import resolve_token

    _write_token(data_dir)
    assert resolve_token() == TOKEN
    monkeypatch.setenv("VOICEHUB_TOKEN", "from-env")
    assert resolve_token() == "from-env"
