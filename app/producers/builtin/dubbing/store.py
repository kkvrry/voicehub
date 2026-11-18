"""配音项目存储：一个 SQLite 文件，四张表。

项目 → 段落 → 角色 → 任务。段落是合成的单位，时间码是它与源音频唯一的连接；
角色是"这一段谁来说"的对象，段落只记角色 id——换音色不必逐段改。

所有写入走短连接 + WAL：合成与识别是后台线程，读多写少，够用。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra.sqlite import connect as sqlite_connect

__all__ = ["ROLE_COLORS", "DubbingStore"]

#: 角色配色：按建角色的顺序取，前端只用这一份，不在界面里另配一套色。
ROLE_COLORS: tuple[str, ...] = (
    "#7aa2f7",
    "#f0b162",
    "#9ece6a",
    "#bb9af7",
    "#7dcfff",
    "#ff9e64",
    "#e46876",
    "#73daca",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'audio',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    stream_index INTEGER NOT NULL DEFAULT 0,
    audio_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'tts',
    engine TEXT NOT NULL DEFAULT '',
    voice_id TEXT NOT NULL DEFAULT '',
    voice_name TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roles_project ON roles(project_id);
CREATE TABLE IF NOT EXISTS cues (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    role_id TEXT NOT NULL DEFAULT '',
    audio TEXT NOT NULL DEFAULT '',
    audio_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'empty',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cues_project ON cues(project_id, start_ms);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class DubbingStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "dubbing.db"
        root.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        self.reclaim_jobs()

    def _conn(self):
        return sqlite_connect(self.path)

    # ---- 项目 -------------------------------------------------------------

    @staticmethod
    def _project_out(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["options"] = _loads(item.get("options") or "{}")
        return item

    def list_projects(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
            out = []
            for row in rows:
                item = self._project_out(row)
                stats = conn.execute(
                    "SELECT COUNT(*) AS cues,"
                    " SUM(CASE WHEN state = 'ready' THEN 1 ELSE 0 END) AS ready"
                    " FROM cues WHERE project_id = ?",
                    (item["id"],),
                ).fetchone()
                item["cue_count"] = int(stats["cues"] or 0)
                item["ready_count"] = int(stats["ready"] or 0)
                out.append(item)
            return out

    def project(self, project_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._project_out(row) if row else None

    def create_project(
        self,
        *,
        name: str,
        source_name: str,
        source_path: str,
        kind: str,
        duration_ms: int,
        stream_index: int = 0,
        audio_path: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = _id("prj")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, source_name, source_path, kind,"
                " duration_ms, stream_index, audio_path, status, options,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (
                    project_id,
                    name,
                    source_name,
                    source_path,
                    kind,
                    int(duration_ms),
                    int(stream_index),
                    audio_path,
                    json.dumps(options or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        created = self.project(project_id)
        assert created is not None
        return created

    def update_project(self, project_id: str, **fields: Any) -> None:
        allowed = {
            "name",
            "source_name",
            "source_path",
            "kind",
            "duration_ms",
            "stream_index",
            "audio_path",
            "status",
            "options",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(
                json.dumps(value, ensure_ascii=False) if key == "options" else value
            )
        if not sets:
            return
        sets.append("updated_at = ?")
        values.extend([_now(), project_id])
        with self._conn() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", values)

    def delete_project(self, project_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cues WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM roles WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ---- 角色 -------------------------------------------------------------

    def roles(self, project_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM roles WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["params"] = _loads(item.get("params") or "{}")
            out.append(item)
        return out

    def role(self, role_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["params"] = _loads(item.get("params") or "{}")
        return item

    def add_role(
        self,
        project_id: str,
        *,
        name: str,
        mode: str = "tts",
        engine: str = "",
        voice_id: str = "",
        voice_name: str = "",
        params: dict[str, Any] | None = None,
        color: str = "",
    ) -> dict[str, Any]:
        role_id = _id("rol")
        existing = self.roles(project_id)
        color = color or ROLE_COLORS[len(existing) % len(ROLE_COLORS)]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO roles (id, project_id, name, color, mode, engine,"
                " voice_id, voice_name, params, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    role_id,
                    project_id,
                    name,
                    color,
                    mode,
                    engine,
                    voice_id,
                    voice_name,
                    json.dumps(params or {}, ensure_ascii=False),
                    _now(),
                ),
            )
        created = self.role(role_id)
        assert created is not None
        return created

    def update_role(self, role_id: str, **fields: Any) -> None:
        allowed = {"name", "color", "mode", "engine", "voice_id", "voice_name", "params"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(
                json.dumps(value, ensure_ascii=False) if key == "params" else value
            )
        if not sets:
            return
        values.append(role_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE roles SET {', '.join(sets)} WHERE id = ?", values)

    def stale_role_cues(self, role_id: str) -> None:
        """换了音色：这个角色名下已有产物的段落标成过期（不删文件，改回去还能听）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE cues SET state = 'stale' WHERE role_id = ? AND audio != ''",
                (role_id,),
            )

    def delete_role(self, role_id: str) -> None:
        """删除角色：它名下的段落退回旁白（``role_id`` 清空），不跟着删。"""
        with self._conn() as conn:
            conn.execute("UPDATE cues SET role_id = '' WHERE role_id = ?", (role_id,))
            conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))

    # ---- 段落 -------------------------------------------------------------

    def cues(self, project_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cues WHERE project_id = ? ORDER BY ordinal",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cue(self, cue_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM cues WHERE id = ?", (cue_id,)).fetchone()
        return dict(row) if row else None

    def replace_cues(
        self, project_id: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """整批替换段落（重新识别用）：旧产物指针对应不上新段落，一并清掉。"""
        now = _now()
        with self._conn() as conn:
            conn.execute("DELETE FROM cues WHERE project_id = ?", (project_id,))
            for index, item in enumerate(items, start=1):
                conn.execute(
                    "INSERT INTO cues (id, project_id, ordinal, start_ms, end_ms, text,"
                    " role_id, audio, audio_key, state, error, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 'empty', '', ?)",
                    (
                        _id("cue"),
                        project_id,
                        index,
                        int(item.get("start_ms") or 0),
                        int(item.get("end_ms") or 0),
                        str(item.get("text") or ""),
                        str(item.get("role_id") or ""),
                        now,
                    ),
                )
        return self.cues(project_id)

    def update_cue(self, cue_id: str, **fields: Any) -> None:
        allowed = {
            "ordinal",
            "start_ms",
            "end_ms",
            "text",
            "role_id",
            "audio",
            "audio_key",
            "state",
            "error",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        values.extend([_now(), cue_id])
        with self._conn() as conn:
            conn.execute(f"UPDATE cues SET {', '.join(sets)} WHERE id = ?", values)

    def delete_cue(self, cue_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cues WHERE id = ?", (cue_id,))

    def renumber(self, project_id: str) -> None:
        """按时间顺序重排序号：删段、并段后会留出空洞，界面上的「第 N 段」必须连续。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM cues WHERE project_id = ? ORDER BY start_ms, ordinal",
                (project_id,),
            ).fetchall()
            for position, row in enumerate(rows, start=1):
                conn.execute(
                    "UPDATE cues SET ordinal = ? WHERE id = ?", (position, row["id"])
                )

    def insert_cue(
        self, project_id: str, *, after_ordinal: int, start_ms: int, end_ms: int, text: str
    ) -> dict[str, Any]:
        """在指定序号后插入一段（拆分用）：后面的序号整体后移一格。"""
        cue_id = _id("cue")
        with self._conn() as conn:
            conn.execute(
                "UPDATE cues SET ordinal = ordinal + 1"
                " WHERE project_id = ? AND ordinal > ?",
                (project_id, after_ordinal),
            )
            conn.execute(
                "INSERT INTO cues (id, project_id, ordinal, start_ms, end_ms, text,"
                " role_id, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', 'empty', ?)",
                (
                    cue_id,
                    project_id,
                    after_ordinal + 1,
                    int(start_ms),
                    int(end_ms),
                    text,
                    _now(),
                ),
            )
        created = self.cue(cue_id)
        assert created is not None
        return created

    def stats(self, project_id: str) -> dict[str, int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN state = 'ready' THEN 1 ELSE 0 END) AS ready,"
                " SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN state = 'stale' THEN 1 ELSE 0 END) AS stale"
                " FROM cues WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "ready": int(row["ready"] or 0),
            "failed": int(row["failed"] or 0),
            "stale": int(row["stale"] or 0),
        }

    # ---- 任务 -------------------------------------------------------------

    def start_job(self, project_id: str, kind: str, total: int = 0) -> dict[str, Any]:
        job_id = _id("job")
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, project_id, kind, state, total, created_at,"
                " updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (job_id, project_id, kind, int(total), now, now),
            )
        job = self.job(job_id)
        assert job is not None
        return job

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"state", "total", "done", "failed", "message", "cancel_requested"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        values.extend([_now(), job_id])
        with self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", values)

    def latest_job(self, project_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def cancel_job(self, job_id: str) -> None:
        self.update_job(job_id, cancel_requested=1)

    def reclaim_jobs(self) -> None:
        """进程重启后，把上次留下的 running 任务标成中断：没有线程会再推进它们。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'interrupted', message = '服务重启，"
                "任务已中断；已完成的段落保留。', updated_at = ? WHERE state = 'running'",
                (_now(),),
            )
