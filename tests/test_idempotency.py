"""幂等键的占位语义：并发只放一个过去，失败或崩溃之后还能重试。"""

from __future__ import annotations


def _db(tmp_path):
    from app.infra.db import HubDB

    return HubDB(tmp_path / "hub.db")


def test_a_second_request_is_held_while_the_first_runs(tmp_path) -> None:
    db = _db(tmp_path)
    assert db.idempotent_begin("k", "fp", ttl=3600) == ("new", None)
    # 第二个同键请求不许再打一次引擎：这是重复计费的入口。
    assert db.idempotent_begin("k", "fp", ttl=3600) == ("pending", None)
    db.idempotent_put("k", "fp", {"id": "first"}, ttl=3600)
    assert db.idempotent_begin("k", "fp", ttl=3600) == ("replay", {"id": "first"})


def test_abandon_frees_the_key_for_a_retry(tmp_path) -> None:
    db = _db(tmp_path)
    db.idempotent_begin("k", "fp", ttl=3600)
    db.idempotent_abandon("k", "fp")
    assert db.idempotent_begin("k", "fp", ttl=3600)[0] == "new"


def test_a_placeholder_left_by_a_crash_expires(tmp_path) -> None:
    """进程被杀时没人来 abandon：占位要按自己的短 TTL 过期，而不是锁满 24 小时。"""
    db = _db(tmp_path)
    db.idempotent_begin("k", "fp", ttl=3600)
    with db._connect() as conn:
        conn.execute("UPDATE idempotency SET created_at = created_at - 400")
    assert db.idempotent_begin("k", "fp", ttl=3600)[0] == "new"


def test_the_same_key_with_other_content_is_a_conflict(tmp_path) -> None:
    db = _db(tmp_path)
    db.idempotent_begin("k", "fp", ttl=3600)
    assert db.idempotent_begin("k", "other", ttl=3600)[0] == "conflict"
