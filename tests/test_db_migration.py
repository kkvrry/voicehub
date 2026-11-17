"""老库补列——``CREATE TABLE IF NOT EXISTS`` 对**已经建好**的表一个字都不会动。

所以后加的列必须有 ``ALTER TABLE`` 兜着（``db._MIGRATIONS``）：新装一台机器是对的，
本机那份老库却会从此拿不到这一列，症状是写库时才报 ``no such column``，
而"刷新一下"看起来是成功的。这两条钉的就是那两处补列。
"""

import sqlite3
from pathlib import Path

from app.infra.db import HubDB

_OLD_SCHEMA = """
CREATE TABLE voices (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    display TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    gender TEXT NOT NULL DEFAULT '',
    age TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    main_sample TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE samples (
    voice_id TEXT NOT NULL,
    file TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    emotion TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (voice_id, file)
);
"""


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class TestMigrations:
    def test_voices_gets_language(self, tmp_path: Path):
        path = tmp_path / "hub.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(_OLD_SCHEMA)
        conn.commit()
        conn.close()

        HubDB(path)
        assert "language" in _columns(path, "voices")

    def test_samples_gets_tags(self, tmp_path: Path):
        """样例自己的标签是后加的——**两张表都要补**，只查 voices 的话这张永远补不上。"""
        path = tmp_path / "hub.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(_OLD_SCHEMA)
        conn.commit()
        conn.close()

        HubDB(path)
        assert "tags" in _columns(path, "samples")

    def test_samples_gets_params(self, tmp_path: Path):
        """"生成这一段时用的参数"也是后加的（同一条纪律）。"""
        path = tmp_path / "hub.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(_OLD_SCHEMA)
        conn.commit()
        conn.close()

        HubDB(path)
        assert "params" in _columns(path, "samples")

    def test_opening_twice_is_fine(self, tmp_path: Path):
        """补列要**幂等**：第二次开不能报 "duplicate column name"。"""
        path = tmp_path / "hub.db"
        HubDB(path)
        HubDB(path)
        assert {"language"} <= _columns(path, "voices")
        assert {"tags", "params"} <= _columns(path, "samples")

    def test_voices_gets_origin_id(self, tmp_path: Path):
        """平台归属也是后加的列：老库不补，平台音色永远挂不上扫描根里的样例。"""
        path = tmp_path / "hub.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(_OLD_SCHEMA)
        conn.commit()
        conn.close()

        HubDB(path)
        assert "origin_id" in _columns(path, "voices")

    def test_origin_backfill_asks_for_one_rescan(self, tmp_path: Path):
        """补列时把本地行的扫描指纹清掉：不清就整条复用旧行，sidecar 里的归属读不回来。

        只清一次——列加上了，第二次开库不该再让用户重扫一遍。
        """
        path = tmp_path / "hub.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(_OLD_SCHEMA)
        conn.execute("ALTER TABLE voices ADD COLUMN scan_stamp TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "INSERT INTO voices (id, source, name, scan_stamp) VALUES ('v1', 'local', '胡桃', 'abc')"
        )
        conn.commit()
        conn.close()

        assert HubDB(path).rebuild_needed is True
        conn = sqlite3.connect(str(path))
        try:
            stamp = conn.execute("SELECT scan_stamp FROM voices WHERE id = 'v1'").fetchone()[0]
        finally:
            conn.close()
        assert stamp == ""

        assert HubDB(path).rebuild_needed is False

