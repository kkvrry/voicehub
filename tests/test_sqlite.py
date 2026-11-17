"""连接生命期：``with conn:`` 结束就关句柄，而不是等 GC。"""

from __future__ import annotations

import sqlite3

import pytest


def test_the_connection_is_closed_at_the_end_of_the_with_block(tmp_path) -> None:
    from app.infra.sqlite import connect

    with connect(tmp_path / "x.db") as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_both_stores_use_the_closing_connection(tmp_path) -> None:
    from app.infra.db import HubDB

    db = HubDB(tmp_path / "hub.db")
    with db._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
