"""对外契约：错误体、信封、版本前缀、幂等与提示结构。

这一份钉的是**调用方依赖的形状**，不是内部实现：

- 错误体必须有 ``type`` / ``code`` / ``param`` / ``request_id`` 四个字段——
  调用方按它们分支，改名字等于破坏契约；
- 集合端点必须是 ``{object: "list", data: [...]}``，单个资源带 ``object``；
- 接口只挂在 ``/api/v1`` 一个前缀下，``/api`` 不再提供 JSON 端点；
- 同一 ``Idempotency-Key`` 重复提交只调用一次引擎；内容不同则 409。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from app.engines.base import SynthesisResult

# --------------------------------------------------------------------------- #
# 错误体
# --------------------------------------------------------------------------- #


def test_a_missing_resource_returns_a_structured_error(client) -> None:
    resp = client.get("/api/v1/voices/ghost")
    assert resp.status_code == 404
    body = resp.json()
    error = body["error"]
    assert error["type"] == "not_found_error"
    assert error["code"] == "voice_not_found"
    assert error["param"] is None
    assert error["message"]
    # 过渡字段：旧读取方仍按 detail 取消息。
    assert body["detail"] == error["message"]


def test_request_id_is_echoed_and_carried_in_the_error_body(client) -> None:
    resp = client.get("/api/v1/voices/ghost", headers={"X-Request-Id": "req-contract-1"})
    assert resp.headers["X-Request-Id"] == "req-contract-1"
    assert resp.json()["error"]["request_id"] == "req-contract-1"


def test_a_generated_request_id_is_present_when_the_client_sends_none(client) -> None:
    resp = client.get("/api/v1/voices/ghost")
    assert resp.headers["X-Request-Id"].startswith("req-")
    assert resp.json()["error"]["request_id"] == resp.headers["X-Request-Id"]


def test_an_unknown_route_uses_the_same_error_shape(client) -> None:
    resp = client.get("/api/v1/unknown-endpoint")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_a_validation_failure_names_the_field(client) -> None:
    resp = client.post("/api/v1/synthesize", json={"engine": "minimax"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["param"] == "voice_id"
    assert "参数 voice_id" in error["message"]


def test_extra_cannot_overwrite_the_explicit_fields(client) -> None:
    """``extra`` 是映射扩展位，不是第二份请求体。

    装配顺序是核心字段在前、``extra`` 最后覆盖（参考音频要靠这一条覆盖样例），
    所以 ``extra.text`` 会让校验与日志看顶层、发出去的是另一句。入口直接拒绝。
    """
    resp = client.post(
        "/api/v1/synthesize",
        json={
            "engine": "minimax",
            "voice_id": "x",
            "text": "顶层这句。",
            "extra": {"text": "extra 里那句。", "speed": 1.2},
        },
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["param"] == "extra"
    assert "text" in error["message"]
    assert "Value error" not in error["message"]


def test_reference_audio_may_still_override_through_extra(client) -> None:
    """参考音频/逐字稿是 ``extra`` 的**合法**用途（生成页就是这样覆盖已存样例的）。"""
    from app.api.views import SynthesizeIn

    body = SynthesizeIn(
        engine="minimax",
        voice_id="x",
        text="你好。",
        extra={"reference_audio": "/tmp/a.wav", "reference_text": "你好"},
    )
    assert body.extra["reference_audio"] == "/tmp/a.wav"


# --------------------------------------------------------------------------- #
# 信封与前缀
# --------------------------------------------------------------------------- #


def test_collection_endpoints_use_the_list_envelope(client) -> None:
    for path in ("/api/v1/voices", "/api/v1/engines", "/api/v1/tags"):
        payload = client.get(path).json()
        assert payload["object"] == "list"
        assert isinstance(payload["data"], list)
        assert payload["has_more"] is False


def test_the_bare_api_prefix_serves_no_endpoint(client) -> None:
    """只有一个前缀：``/api`` 下不再挂路由。"""
    assert client.get("/api/voices").status_code == 404


def test_a_delete_returns_an_id_and_a_deleted_flag(client, seed_platform_voice) -> None:
    voice = seed_platform_voice(voice_id="MX_DEL", name="待删除")
    resp = client.delete(f"/api/v1/platform-voices/{voice.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "id": voice.id,
        "object": "platform_voice",
        "deleted": True,
        "trashed": [],
    }


def test_health_reports_version_and_engine_states(client) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["version"]
    assert isinstance(body["uptime_s"], int)
    assert set(body["engines"]) >= {"minimax"}
    assert set(body["engines"].values()) <= {"ready", "not_configured", "error"}
    # 默认只绑回环：界面不该显示「局域网开放」。
    assert body["lan_open"] is False


def test_health_flags_an_open_lan(client, monkeypatch) -> None:
    """绑到通配且没有令牌：设置页据此标出"同网段都能操作"。"""
    from app.api import views

    monkeypatch.delenv("VOICEHUB_TOKEN", raising=False)
    monkeypatch.setattr(views, "_BIND_HOST", "0.0.0.0")
    assert client.get("/api/v1/health").json()["lan_open"] is True


def test_service_reports_the_effective_listen_scope(client) -> None:
    """`/service` 答的是"现在生效什么"，不是"配置里写了什么"。"""
    body = client.get("/api/v1/service").json()
    assert body["object"] == "service"
    assert body["lan"] is False
    assert body["token_set"] is False
    assert body["port"] > 0


def test_autostart_is_unsupported_without_the_launcher(client) -> None:
    """源码态没有启动器：不给假开关，写注册表直接拒绝。"""
    body = client.get("/api/v1/service").json()
    assert body["autostart_supported"] is False
    assert body["autostart_enabled"] is False

    resp = client.put("/api/v1/service", json={"autostart": True})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "autostart_unsupported"


def test_saving_the_listen_scope_writes_config_and_asks_for_a_restart(client) -> None:
    """地址在进程启动时绑定：保存只落盘，兑现要重启。"""
    from app.infra import secrets as secret_store
    from app.infra.config import load_config

    resp = client.put("/api/v1/service", json={"lan": True, "access_token": "s3cret"})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True

    config = load_config()
    assert config.host == "0.0.0.0"
    assert secret_store.reveal(config.access_token) == "s3cret"


def test_https_is_a_restart_setting(client) -> None:
    """https 与地址、令牌同一套语义：保存只落盘，`/service` 报现在**生效**的 scheme。"""
    from app.infra.config import load_config

    body = client.get("/api/v1/service").json()
    assert body["scheme"] == "http"
    assert body["https"] == "auto"

    resp = client.put("/api/v1/service", json={"https": "off"})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True
    assert resp.json()["https"] == "off"
    assert load_config().https == "off"

    # 只认三态：界面把值拼错时不能静默落成 auto。
    assert client.put("/api/v1/service", json={"https": "maybe"}).status_code == 422


# --------------------------------------------------------------------------- #
# 幂等与提示
# --------------------------------------------------------------------------- #


def _fake_synthesis(monkeypatch, *, notes: tuple[str, ...] = ()) -> dict[str, int]:
    """把网关与归档替换成内存实现；返回调用计数。"""
    calls = {"n": 0}

    async def fake_synthesize(*args: Any, **kwargs: Any) -> SynthesisResult:
        calls["n"] += 1
        return SynthesisResult(
            audio=b"RIFF" + b"\x00" * 64,
            format="wav",
            notes=notes,
            prep_notes=(("audio_transcoded", "参考音频已转换。"),),
        )

    def fake_write(*args: Any, **kwargs: Any) -> tuple[str, Path]:
        return "相对/文件.wav", Path("C:/tmp/文件.wav")

    monkeypatch.setattr("app.api.sections.studio.gateway.synthesize", fake_synthesize)
    monkeypatch.setattr("app.api.sections.studio.archive.write_audio", fake_write)
    return calls


def test_the_same_idempotency_key_replays_the_first_result(client, monkeypatch) -> None:
    calls = _fake_synthesis(monkeypatch)
    body = {"engine": "minimax", "voice_id": "ghost", "text": "第一行。", "persist": "cache"}
    first = client.post(
        "/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-1"}
    )
    second = client.post(
        "/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-1"}
    )
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["object"] == "synthesis"


def test_an_in_flight_key_is_held_until_the_result_lands(library) -> None:
    """同一键的并发请求只能有一个去调引擎：其余的是"处理中"，不是重放也不是再打一次。"""
    from app.usecase.library import LibraryError

    assert library.idempotent_begin("k-flight", "fp", ttl=3600) == ("new", None)
    assert library.idempotent_begin("k-flight", "fp", ttl=3600) == ("pending", None)
    with pytest.raises(LibraryError) as conflict:
        library.idempotent_begin("k-flight", "other", ttl=3600)
    assert conflict.value.code == "idempotency_key_conflict"
    library.idempotent_store("k-flight", "fp", {"id": "first"}, ttl=3600)
    assert library.idempotent_begin("k-flight", "fp", ttl=3600) == ("replay", {"id": "first"})


def test_a_failed_synthesis_releases_the_key(client, monkeypatch) -> None:
    """引擎报错后键要放开：否则用户重试会一直撞"处理中"，或等 24 小时。"""
    from app.api.errors import ApiError

    calls = {"n": 0}

    async def boom(*args: Any, **kwargs: Any):
        calls["n"] += 1
        raise ApiError(status=502, code="engine_failed", message="上游挂了。")

    monkeypatch.setattr("app.api.sections.studio.gateway.synthesize", boom)
    body = {"engine": "minimax", "voice_id": "ghost", "text": "重试。", "persist": "cache"}
    first = client.post("/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-retry"})
    second = client.post("/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-retry"})
    assert first.status_code == second.status_code == 502
    assert calls["n"] == 2


def test_a_reused_key_with_different_content_is_a_conflict(client, monkeypatch) -> None:
    _fake_synthesis(monkeypatch)
    client.post(
        "/api/v1/synthesize",
        json={"engine": "minimax", "voice_id": "ghost", "text": "第一行。", "persist": "cache"},
        headers={"Idempotency-Key": "k-2"},
    )
    resp = client.post(
        "/api/v1/synthesize",
        json={"engine": "minimax", "voice_id": "ghost", "text": "第二行。", "persist": "cache"},
        headers={"Idempotency-Key": "k-2"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_key_conflict"


def test_warnings_carry_source_and_code(client, monkeypatch) -> None:
    _fake_synthesis(monkeypatch, notes=("情绪词未识别，已忽略。",))
    resp = client.post(
        "/api/v1/synthesize",
        json={"engine": "minimax", "voice_id": "ghost", "text": "一行。", "persist": "cache"},
    )
    warnings = resp.json()["warnings"]
    assert [w["source"] for w in warnings] == ["engine", "hub"]
    assert warnings[0]["code"] == "engine_note"
    assert warnings[1]["code"] == "audio_transcoded"
    # notes 保留为消息列表（兼容旧读取方）。
    assert resp.json()["notes"] == ["情绪词未识别，已忽略。", "参考音频已转换。"]


# --------------------------------------------------------------------------- #
# 三种生成各归各
# --------------------------------------------------------------------------- #


def test_default_persist_streams_bytes_and_writes_nothing(client, monkeypatch) -> None:
    """第三方那条路：不落盘、直接回音频字节，元数据走响应头。"""
    _fake_synthesis(monkeypatch)

    def explode(*args: Any, **kwargs: Any) -> tuple[str, Path]:
        raise AssertionError("persist=none 不应写盘")

    monkeypatch.setattr("app.api.sections.studio.archive.write_audio", explode)
    resp = client.post(
        "/api/v1/synthesize",
        json={"engine": "minimax", "voice_id": "ghost", "text": "你好。"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content.startswith(b"RIFF")
    meta = json.loads(unquote(resp.headers["x-voicehub-meta"]))
    assert meta["persist"] == "none"
    assert meta["bytes"] == len(resp.content)
    assert "audio_url" not in meta


def test_persist_cache_returns_a_url(client, monkeypatch) -> None:
    """VoiceHub 生成页那条路：落缓存目录，回 JSON + 可播放地址。"""
    _fake_synthesis(monkeypatch)
    resp = client.post(
        "/api/v1/synthesize",
        json={"engine": "minimax", "voice_id": "ghost", "text": "你好。", "persist": "cache"},
    )
    body = resp.json()
    assert body["persist"] == "cache"
    assert body["saved_rel"] == "相对/文件.wav"
    assert body["audio_url"].startswith("/api/v1/generated/")


def test_delete_generated_moves_the_cache_file_to_trash(client, monkeypatch) -> None:
    """删除生成产物：文件进回收站，sidecar 里那条记录跟着摘掉；再删一次就是 404。"""
    from app.infra import archive
    from app.infra.sidecar import read_sidecar

    rel, path = archive.write_audio(
        "minimax",
        "MiniMax",
        "胡桃",
        b"RIFF-fake",
        fmt="wav",
        voice_id="v1",
        text="你好。",
        as_sample=False,
    )
    assert path.is_file() and path.name in read_sidecar(path.parent).samples
    # 真往回收站里扔会污染测试机，换成"删掉"
    monkeypatch.setattr(archive, "move_to_trash", lambda target: Path(target).unlink())

    resp = client.delete(f"/api/v1/generated/{rel}", params={"engine": "minimax"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True and body["object"] == "generated"
    assert not path.exists()
    assert path.name not in read_sidecar(path.parent).samples

    again = client.delete(f"/api/v1/generated/{rel}", params={"engine": "minimax"})
    assert again.status_code == 404


def test_delete_generated_refuses_a_file_outside_that_engines_cache(client) -> None:
    """相对路径的第一段就是引擎的目录：拿别的引擎来删一律 404，缺 engine 一律 400。

    不校验这一段的话，删某台引擎的产物就能顺着 ``..`` 之外的路子删到隔壁那条。
    """
    from app.infra import archive

    rel, path = archive.write_audio(
        "minimax", "MiniMax", "胡桃", b"RIFF-fake", fmt="wav", as_sample=False
    )
    assert client.delete(f"/api/v1/generated/{rel}", params={"engine": "mimo"}).status_code == 404
    assert client.delete(f"/api/v1/generated/{rel}").status_code == 400
    assert path.is_file()


def test_persist_none_replays_the_same_bytes_for_an_idempotency_key(
    client, monkeypatch
) -> None:
    """不落盘也要保住幂等：重放的是第一次的音频，不是再调一次引擎。"""
    calls = _fake_synthesis(monkeypatch)
    body = {"engine": "minimax", "voice_id": "ghost", "text": "第一行。"}
    first = client.post(
        "/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-none"}
    )
    second = client.post(
        "/api/v1/synthesize", json=body, headers={"Idempotency-Key": "k-none"}
    )
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1
    assert first.content == second.content


# --------------------------------------------------------------------------- #
# 存储位置
# --------------------------------------------------------------------------- #


def test_storage_lists_the_four_places(client) -> None:
    """数据 / 生成结果 / 缓存 / 日志。

    "识别权重目录"那一行随内置识别一起删了：识别现在是一类插件，权重在哪由
    插件自己说（``settings_schema`` 里那几格），核心不该再摆一个它不认识的目录。
    """
    body = client.get("/api/v1/storage").json()
    assert [entry["key"] for entry in body["entries"]] == [
        "data",
        "output",
        "cache",
        "log",
    ]
    data = body["entries"][0]
    assert data["editable"] is False
    # 生效路径与配置原值分开给：界面显示前者、输入框绑后者。
    assert data["path"]
    assert data["value"] == ""


def test_storage_reports_the_cache_policy(client) -> None:
    """回收线随存储一起下发：界面那两格显示的必须是**后端生效值**。"""
    body = client.get("/api/v1/storage").json()
    assert body["cache"] == {"limit_mb": 512, "keep_days": 30}


def test_storage_saves_the_cache_policy(client) -> None:
    body = client.put("/api/v1/storage", json={"cache_limit_mb": 0, "cache_keep_days": 7}).json()
    assert body["cache"] == {"limit_mb": 0, "keep_days": 7}
    assert client.get("/api/v1/storage").json()["cache"] == {"limit_mb": 0, "keep_days": 7}


def test_storage_rejects_an_out_of_range_cache_policy(client) -> None:
    """越界不夹到边界：界面写 99999 却按 3650 生效是更难查的错。"""
    resp = client.put("/api/v1/storage", json={"cache_keep_days": -1})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "cache_setting_out_of_range"
    resp = client.put("/api/v1/storage", json={"cache_limit_mb": 999999})
    assert resp.status_code == 400


def test_storage_rejects_a_relative_path(client) -> None:
    resp = client.put("/api/v1/storage", json={"cache_dir": "relative\\cache"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "storage_dir_relative"


def test_storage_roundtrips_a_custom_cache_dir(client, tmp_path) -> None:
    target = tmp_path / "cache-custom"
    body = client.put("/api/v1/storage", json={"cache_dir": str(target)}).json()
    entry = next(item for item in body["entries"] if item["key"] == "cache")
    assert entry["path"] == str(target)
    assert entry["value"] == str(target)

    # 空串 = 回默认：生效路径回到数据目录下，原值清空。
    body = client.put("/api/v1/storage", json={"cache_dir": ""}).json()
    entry = next(item for item in body["entries"] if item["key"] == "cache")
    assert entry["value"] == ""
    assert entry["path"].endswith("cache")


# --------------------------------------------------------------------------- #
# 错误码表
# --------------------------------------------------------------------------- #


def test_every_error_code_is_registered() -> None:
    """``CODES`` 是唯一产地：代码里抛出的字符串错误码必须能在表里查到。

    扫描范围限定在错误构造上（``EngineError`` / ``ApiError`` / ``invalid`` 等），
    普通字典里的 ``code=`` 不算错误码。``code=常量`` 由常量本身保证，这里只收集
    字符串字面量。
    """
    from app.api.errors import CODES

    constructors = {
        "ApiError",
        "EngineError",
        "AsrError",
        "LlmError",
        "LibraryError",
        "AvatarError",
        "ProducerError",
        "TrashError",
        "invalid",
        "not_found",
        "engine_failed",
        "internal",
    }
    root = Path(__file__).resolve().parents[1] / "app"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name not in constructors:
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    found.add(keyword.value.value)

    missing = sorted(found - set(CODES))
    assert not missing, "这些错误码没有登记在 app/api/v1/errors.py::CODES：" + "、".join(missing)
