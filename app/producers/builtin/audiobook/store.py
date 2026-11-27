"""有声书项目存储：一个 SQLite 文件，四张表。

项目 → 章节 → 行 → 角色音色。行是合成的最小单位，章节是合并的最小单位。
所有写入走短连接 + WAL；并发合成时读多写少，够用。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.infra.process import alive
from app.infra.sqlite import connect as sqlite_connect

__all__ = ["AudiobookStore"]

#: 任务心跳超过这么久没更新，且进程也确实不在了，才认为它中断。
#: 心跳每 5 秒一次，120 秒是"事件循环被卡住"也能容忍的余量。
_JOB_STALE_S = 120

#: 行级占位的有效期：一次单行合成不可能超过它。进程崩了，占位自己过期。
_ROW_CLAIM_S = 900


def _stamp() -> int:
    """秒级时间戳（心跳与占位用它，ISO 字符串只用于展示）。"""
    return int(time.time())


def _owner_alive(owner: str) -> bool:
    """``owner`` 形如 ``<pid>-<随机后缀>``；进程还在就算活着。"""
    pid = owner.split("-", 1)[0].strip()
    return pid.isdigit() and alive(int(pid))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'book',
    pattern_key TEXT NOT NULL DEFAULT '',
    split_mode TEXT NOT NULL DEFAULT 'rule',
    split_dialogue INTEGER NOT NULL DEFAULT 1,
    split_quote TEXT NOT NULL DEFAULT 'cn_double',
    split_max_chars INTEGER NOT NULL DEFAULT 200,
    layout_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'chapters',
    audio TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_project ON chapters(project_id, ordinal);
CREATE TABLE IF NOT EXISTS rows (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'narration',
    speaker TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    emotion TEXT NOT NULL DEFAULT '',
    speaker_edited INTEGER NOT NULL DEFAULT 0,
    emotion_edited INTEGER NOT NULL DEFAULT 0,
    audio TEXT NOT NULL DEFAULT '',
    audio_key TEXT NOT NULL DEFAULT '',
    -- 指纹口径：1 = 历史（整张读音表版本），2 = 逐行读音命中 + 引擎身份。
    -- 老行保持 1，第一次生成计划时"不重做、只换键"地升到 2（见 audio_stage）。
    audio_key_v INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS idx_rows_chapter ON rows(chapter_id, ordinal);
-- 章统计（chapters 的聚合）只读这三列：走覆盖索引后不必回表，实测 12k 行 29.6 → 11.7 ms。
CREATE INDEX IF NOT EXISTS idx_rows_stats ON rows(chapter_id, status, speaker);
CREATE TABLE IF NOT EXISTS voices (
    project_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT '',
    voice_id TEXT NOT NULL DEFAULT '',
    voice_name TEXT NOT NULL DEFAULT '',
    sample_mode TEXT NOT NULL DEFAULT 'emotion',
    sample_file TEXT NOT NULL DEFAULT '',
    -- AI 分配时给出的依据：只说这颗音色为什么合适，换音色即作废。
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, speaker)
);
-- 按章指定的旁白音色：还没识别出角色时，分配表按章列行。
-- 只存旁白这一层；具名角色仍走 voices。
CREATE TABLE IF NOT EXISTS chapter_voices (
    project_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT '',
    voice_id TEXT NOT NULL DEFAULT '',
    voice_name TEXT NOT NULL DEFAULT '',
    sample_mode TEXT NOT NULL DEFAULT 'emotion',
    sample_file TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, chapter_id)
);
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
    -- 谁在推进这个任务：``<pid>-<随机后缀>``。启动清理靠它区分"上一个进程
    -- 崩溃留下的"与"另一个实例正在跑的"。
    owner TEXT NOT NULL DEFAULT '',
    -- 心跳（秒级时间戳）。任务活着就每几秒写一次，停了会被判成中断。
    heartbeat_at INTEGER NOT NULL DEFAULT 0,
    -- 0 = 旁路任务（当前只有预检）：只读行音频、只写自己的结果表，
    -- 不参与「同一项目同时只跑一个任务」的判定，合并与生成不必等它。
    exclusive INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at);
-- 行级占位：一次合成只让一个 worker 写这一行。单独一张表，行表的形状与
-- 接口响应不受影响；进程崩了就靠 ``claimed_at`` 过期自动放开。
CREATE TABLE IF NOT EXISTS row_claims (
    row_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    claimed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 回读预检结果：一行一条。``audio_key`` 是检查时那一版音频的指纹，读的时候
-- 对不上就当没检过——换引擎或重生成后旧结论自动失效，不需要谁去清。
CREATE TABLE IF NOT EXISTS row_qc (
    row_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    audio_key TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    engine TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_row_qc_project ON row_qc(project_id, verdict);
"""



#: ``projects`` 里除 ``source`` 之外的全部列：详情与路径解析只需要这些。
_PROJECT_META = (
    "id, name, author, folder, kind, pattern_key, split_mode, split_dialogue,"
    " split_quote, split_max_chars, layout_version, created_at, updated_at"
)



from app.producers.builtin.audiobook.store_helpers import (
    _broken_flag,
    _new_id,
    _now,
)
from app.producers.builtin.audiobook.store_rows import _RowsStoreMixin
from app.producers.builtin.audiobook.store_voices import _VoicesStoreMixin


class AudiobookStore(_RowsStoreMixin, _VoicesStoreMixin):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "projects.db"
        #: 这一份库的写入者身份：任务心跳与行级占位都记它。
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            # 只在这里设一次：每条连接都设 journal_mode 会去抢写锁，
            # 任务运行时别的请求会被拖住。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "DELETE FROM row_claims WHERE claimed_at < ?", (_stamp() - _ROW_CLAIM_S,)
            )
        # 只回收"确实没人推进"的任务。以前这里一律把 running 标成中断：
        # 第二个实例一启动就会把另一个进程正在跑的任务判死，闸门随之放开，
        # 两批同时写同一行是这么来的。
        self.reclaim_jobs()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老库补列：表是 ``CREATE IF NOT EXISTS``，新增字段不会自己出现。"""
        row_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rows)")}
        if "speaker_edited" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN speaker_edited INTEGER NOT NULL DEFAULT 0")
        if "emotion_edited" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN emotion_edited INTEGER NOT NULL DEFAULT 0")
        if "audio_key" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN audio_key TEXT NOT NULL DEFAULT ''")
        if "audio_key_v" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN audio_key_v INTEGER NOT NULL DEFAULT 1")
        if "error" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN error TEXT NOT NULL DEFAULT ''")
        if "params" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN params TEXT NOT NULL DEFAULT '{}'")
        # 字幕项目的行带着原字幕时间码（毫秒；0 = 不是字幕行）。书稿项目一律 0，
        # 合并照旧走「顺序拼接」——这两列只是增项，不改老路径。
        if "start_ms" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN start_ms INTEGER NOT NULL DEFAULT 0")
        if "end_ms" not in row_columns:
            conn.execute("ALTER TABLE rows ADD COLUMN end_ms INTEGER NOT NULL DEFAULT 0")
        # 历史数据：批量改角色时曾把行状态写成非法的 'analysis'（正确是 'analyzed'），
        # 那些行永远不算"已标注"，每次开库顺手订正。
        conn.execute("UPDATE rows SET status = 'analyzed' WHERE status = 'analysis'")
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "exclusive" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN exclusive INTEGER NOT NULL DEFAULT 1")
        project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        if "author" not in project_columns:
            # 作者只进书目信息与成品标签，不参与任何路径。
            conn.execute("ALTER TABLE projects ADD COLUMN author TEXT NOT NULL DEFAULT ''")
        if "kind" not in project_columns:
            # 'book' 书稿（默认，老库全是它）/ 'subtitle' 字幕配音（单章 + 时间轴）
            conn.execute("ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'book'")
        if "folder" not in project_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        if "pattern_key" not in project_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN pattern_key TEXT NOT NULL DEFAULT ''")
        if "layout_version" not in project_columns:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN layout_version INTEGER NOT NULL DEFAULT 0"
            )
        split_columns = {
            "split_mode": "TEXT NOT NULL DEFAULT 'rule'",
            "split_dialogue": "INTEGER NOT NULL DEFAULT 1",
            "split_max_chars": "INTEGER NOT NULL DEFAULT 200",
        }
        for name, definition in split_columns.items():
            if name not in project_columns:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
        if "split_quote" not in project_columns:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN split_quote TEXT NOT NULL DEFAULT 'cn_double'"
            )
            # 旧版没有「引号写法」：显式选过一对的按对迁移，其余保持原来的自动识别，
            # 不让升级悄悄改变已有项目的切行结果。
            if "split_open" in project_columns and "split_close" in project_columns:
                conn.execute(
                    "UPDATE projects SET split_quote = CASE split_open || split_close"
                    " WHEN '“”' THEN 'cn_double'"
                    " WHEN '「」' THEN 'corner'"
                    " WHEN '『』' THEN 'double_corner'"
                    " WHEN '\"\"' THEN 'en_double'"
                    " WHEN '‘’' THEN 'cn_single'"
                    " ELSE 'auto' END"
                )
            else:
                conn.execute("UPDATE projects SET split_quote = 'auto'")
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "owner" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        if "heartbeat_at" not in job_columns:
            # 老任务没有心跳：0 会让它们在首次启动时被回收，这正是升级想要的。
            conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at INTEGER NOT NULL DEFAULT 0")
        voice_columns = {row["name"] for row in conn.execute("PRAGMA table_info(voices)")}
        if "sample_mode" not in voice_columns:
            conn.execute(
                "ALTER TABLE voices ADD COLUMN sample_mode TEXT NOT NULL DEFAULT 'emotion'"
            )
        if "sample_file" not in voice_columns:
            conn.execute("ALTER TABLE voices ADD COLUMN sample_file TEXT NOT NULL DEFAULT ''")
        if "reason" not in voice_columns:
            conn.execute("ALTER TABLE voices ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        # 「local」是音色来源，不是引擎：旧版把它存进 voices.engine，合成必然失败。
        conn.execute("UPDATE voices SET engine = '' WHERE engine = 'local'")
        # 旧分析路径只写 speaker、不写 kind：这些行正文里明明有人说话，却仍标着
        # narration，界面于是既不给对白高亮、也不算进「对白」统计。补一次（幂等）：
        # 有名字的说话人 = 对白行，与 `update_row` / `set_row_annotation` 同一条规则。
        conn.execute(
            "UPDATE rows SET kind = 'dialogue'"
            " WHERE kind = 'narration' AND speaker NOT IN ('', '旁白')"
        )
        # 两类不再可操作的假失败：错误早已被换音色清空，或指向不存在的 local 引擎。
        # 留着它们只会让行永远显示红色，却没有可用的原因。
        conn.execute(
            "UPDATE rows SET error = '',"
            " status = CASE WHEN audio != '' THEN 'stale' ELSE 'text' END"
            " WHERE status = 'failed' AND (error = '' OR error LIKE '引擎 local 不存在%')"
        )

    def _conn(self) -> sqlite3.Connection:
        # ``with`` 结束即关闭：SQLite 的连接上下文只提交/回滚，不关句柄。
        return sqlite_connect(self.path, timeout=30, busy_ms=30000)

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """一次请求里连跑多条读查询：共用一条连接。

        每条新连接第一次查询都要重读 schema（19 MB 的库实测 5–10 ms），
        详情一次要跑六条查询——各开各的连接，光这一项就三十多毫秒。
        会话内的查询必须把 ``conn`` 传进去；会话本身只读，退出时提交（无事务即空转）。
        """
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _query(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        conn: sqlite3.Connection | None = None,
    ) -> list[sqlite3.Row]:
        """跑一条查询：给了 ``conn`` 就用它（会话内），否则自己开一条。"""
        if conn is not None:
            return conn.execute(sql, params).fetchall()
        with self._conn() as own:
            return own.execute(sql, params).fetchall()

    # ---- 项目 -------------------------------------------------------------

    def create_project(
        self,
        name: str,
        source: str,
        chapters: list[tuple[str, str]],
        *,
        kind: str = "book",
    ) -> str:
        """建项目。``kind``：``book`` 书稿（默认）/ ``subtitle`` 字幕配音（单章 + 时间轴）。"""
        project_id = _new_id()
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO projects(id, name, source, kind, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (project_id, name, source, kind, now, now),
            )
            for ordinal, (title, text) in enumerate(chapters):
                conn.execute(
                    "INSERT INTO chapters(id, project_id, ordinal, title, text, updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (_new_id(), project_id, ordinal, title, text, now),
                )
        return project_id

    def set_project_folder(self, project_id: str, folder: str) -> None:
        """记下项目根目录：项目移动后仍能解析相对音频路径。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET folder = ?, updated_at = ? WHERE id = ?",
                (folder, _now(), project_id),
            )

    def update_project_profile(
        self, project_id: str, *, name: str | None = None, author: str | None = None
    ) -> None:
        """书名与作者一起写：都是书目信息，分两条 UPDATE 只会多拿一次写锁。"""
        fields: dict[str, str] = {}
        if name is not None:
            fields["name"] = name
        if author is not None:
            fields["author"] = author
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), project_id),
            )

    def rename_project(self, project_id: str, name: str) -> None:
        self.update_project_profile(project_id, name=name)

    def source_text(self, project_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT source FROM projects WHERE id = ?", (project_id,)).fetchone()
        return str(row["source"]) if row else ""

    def set_project_source(self, project_id: str, source: str) -> None:
        """文本处理改的是整本书的原文；章节是它的派生物，由调用方负责作废。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET source = ?, updated_at = ? WHERE id = ?",
                (source, _now(), project_id),
            )

    def set_project_pattern(self, project_id: str, pattern_key: str) -> None:
        """记住这本书上次用的分章模式：下次打开面板时还是它。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET pattern_key = ?, updated_at = ? WHERE id = ?",
                (pattern_key, _now(), project_id),
            )

    def projects_pending_layout(self) -> list[dict[str, Any]]:
        """还没按书落盘的项目（迁移用；一次查询，常态为空）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, folder, source FROM projects WHERE layout_version < 1"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_layout_version(self, project_id: str, version: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET layout_version = ? WHERE id = ?", (version, project_id)
            )

    def relativize_audio(self, project_id: str, root: Path) -> int:
        """老库里可能是绝对路径：项目目录要改名时先把它们折成相对路径。"""
        changed = 0
        prefix = str(root.resolve())
        with self._conn() as conn:
            chapters = conn.execute(
                "SELECT id, audio FROM chapters WHERE project_id = ? AND audio <> ''",
                (project_id,),
            ).fetchall()
            for chapter in chapters:
                value = str(chapter["audio"])
                path = Path(value)
                if not path.is_absolute():
                    continue
                try:
                    relative = path.resolve().relative_to(prefix)
                except ValueError:
                    continue
                conn.execute(
                    "UPDATE chapters SET audio = ? WHERE id = ?",
                    (relative.as_posix(), chapter["id"]),
                )
                changed += 1
            rows = conn.execute(
                "SELECT r.id, r.audio FROM rows r JOIN chapters c ON c.id = r.chapter_id"
                " WHERE c.project_id = ? AND r.audio <> ''",
                (project_id,),
            ).fetchall()
            for row in rows:
                value = str(row["audio"])
                path = Path(value)
                if not path.is_absolute():
                    continue
                try:
                    relative = path.resolve().relative_to(prefix)
                except ValueError:
                    continue
                conn.execute(
                    "UPDATE rows SET audio = ? WHERE id = ?",
                    (relative.as_posix(), row["id"]),
                )
                changed += 1
        return changed

    def list_projects(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.author, p.kind, p.created_at, p.updated_at,
                       COUNT(DISTINCT c.id) AS chapters,
                       COUNT(r.id) AS rows
                FROM projects p
                LEFT JOIN chapters c ON c.project_id = p.id
                LEFT JOIN rows r ON r.chapter_id = c.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def project(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM projects WHERE id = ?", (project_id,), conn)
        return dict(rows[0]) if rows else None

    def project_meta(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        """项目元数据：**不含原文**。

        ``source`` 是一整本书（实测 3 MB），而详情、路径解析、存在性判断都不看它。
        用 ``SELECT *`` 取一次就要复制几 MB，详情接口在每次改动后都要调一遍——
        这些路径一律改用它；需要原文的地方（``source_payload``、分章）仍走
        :meth:`project`。
        """
        rows = self._query(
            f"SELECT {_PROJECT_META} FROM projects WHERE id = ?", (project_id,), conn
        )
        return dict(rows[0]) if rows else None

    def delete_project(self, project_id: str) -> None:
        with self._conn() as conn:
            ids = [
                item["id"]
                for item in conn.execute(
                    "SELECT id FROM chapters WHERE project_id = ?", (project_id,)
                ).fetchall()
            ]
            for chapter_id in ids:
                conn.execute("DELETE FROM rows WHERE chapter_id = ?", (chapter_id,))
            conn.execute("DELETE FROM chapters WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM voices WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM chapter_voices WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ---- 章节 -------------------------------------------------------------

    def chapters(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT c.*, COALESCE(s.total, 0) AS rows,
                   COALESCE(s.done, 0) AS done,
                   COALESCE(s.analyzed, 0) AS analyzed,
                   COALESCE(s.dialogue, 0) AS dialogue,
                   COALESCE(s.unassigned, 0) AS unassigned,
                   COALESCE(s.failed, 0) AS failed,
                   COALESCE(s.pending, 0) AS pending,
                   COALESCE(s.stale, 0) AS stale,
                   COALESCE(q2.suspect, 0) AS suspect
            FROM chapters c
            LEFT JOIN (
                SELECT chapter_id, COUNT(*) AS total,
                       SUM(CASE WHEN status = 'audio' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN status IN ('analyzed', 'audio') THEN 1 ELSE 0 END)
                           AS analyzed,
                       SUM(CASE WHEN speaker NOT IN ('', '旁白') THEN 1 ELSE 0 END)
                           AS dialogue,
                       -- 「未分配」与行表的判据必须同一句：空、未知都算没定人。
                       SUM(CASE WHEN COALESCE(speaker, '') IN ('', '未知') THEN 1 ELSE 0 END)
                           AS unassigned,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       -- 「待生成」：定了人、没失败、但当前没有算数的音频。含两种——
                       -- 从没生成过（text / analyzed）与改过参数后过期（stale）。
                       SUM(CASE WHEN COALESCE(speaker, '') NOT IN ('', '未知')
                                 AND status NOT IN ('audio', 'failed')
                                THEN 1 ELSE 0 END) AS pending,
                       -- 其中「有过旧音频、只是不算数」的那部分：改回去能自己复活。
                       SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale
                FROM rows
                GROUP BY chapter_id
            ) s ON s.chapter_id = c.id
            -- 预检结论跟着那一版音频走：指纹对不上的行按没检过算。
            LEFT JOIN (
                SELECT r.chapter_id, COUNT(*) AS suspect
                FROM row_qc q JOIN rows r ON r.id = q.row_id
                WHERE q.verdict = 'suspect' AND q.audio_key = COALESCE(r.audio_key, '')
                GROUP BY r.chapter_id
            ) q2 ON q2.chapter_id = c.id
            WHERE c.project_id = ?
            ORDER BY c.ordinal
            """,
            (project_id,),
            conn,
        )
        return [dict(row) for row in rows]

    def chapter_speakers(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, list[str]]:
        """每章出现过的角色；空值与「旁白」并成同一项，与行表的旁白判据一致。"""
        rows = self._query(
            """
            SELECT r.chapter_id,
                   CASE WHEN COALESCE(r.speaker, '') IN ('', '旁白') THEN '旁白'
                        ELSE r.speaker END AS role
            FROM rows r
            JOIN chapters c ON c.id = r.chapter_id
            WHERE c.project_id = ?
            GROUP BY r.chapter_id, role
            """,
            (project_id,),
            conn,
        )
        found: dict[str, list[str]] = {}
        for row in rows:
            found.setdefault(str(row["chapter_id"]), []).append(str(row["role"]))
        return found

    def broken_counts(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """每章「需校对」的行数。

        只给项目详情用（章节栏的缺字灯）：这个判据是正文扫描，不进``chapters()``
        的常用查询——合章、拆句、生成那些路径不需要它。

        判据交给 SQL（注册 ``is_broken``）：一万多行的书如果先把正文全取回再逐行
        建 ``Row`` 对象，光这一步就比扫描本身还贵。
        """
        if conn is not None:
            conn.create_function("is_broken", 1, _broken_flag, deterministic=True)
            rows = conn.execute(
                """
                SELECT r.chapter_id AS chapter_id, COUNT(*) AS broken
                FROM rows r JOIN chapters c ON c.id = r.chapter_id
                WHERE c.project_id = ? AND is_broken(r.text)
                GROUP BY r.chapter_id
                """,
                (project_id,),
            ).fetchall()
            return {str(row["chapter_id"]): int(row["broken"]) for row in rows}
        with self._conn() as own:
            return self.broken_counts(project_id, own)

    def chapter(self, project_id: str, chapter_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
                (chapter_id, project_id),
            ).fetchone()
        return dict(row) if row else None

    def set_chapter_stage(self, chapter_id: str, stage: str, audio: str = "") -> None:
        with self._conn() as conn:
            if audio:
                conn.execute(
                    "UPDATE chapters SET stage = ?, audio = ?, updated_at = ? WHERE id = ?",
                    (stage, audio, _now(), chapter_id),
                )
            else:
                conn.execute(
                    "UPDATE chapters SET stage = ?, updated_at = ? WHERE id = ?",
                    (stage, _now(), chapter_id),
                )

    def rename_chapter(self, project_id: str, chapter_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE chapters SET title = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                (title, _now(), chapter_id, project_id),
            )

    def invalidate_chapter(self, chapter_id: str, stage: str) -> None:
        """章内内容变了：成品失效，阶段退回。

        不删成品文件：下次合并用同一个文件名覆盖，留着也不会被误播。
        """
        self.set_chapters_stage([chapter_id], stage, clear_audio=True)

    def set_chapters_stage(
        self, chapter_ids: list[str], stage: str, *, clear_audio: bool = False
    ) -> int:
        """一批章一起改阶段。

        逐章一条 UPDATE 是逐章一次提交：106 章实测要半秒（Windows 上每次 fsync
        都不便宜）。同一次操作里改的章通常是一整批，合成一条语句就是一次提交。
        """
        ids = [str(item) for item in chapter_ids if str(item).strip()]
        if not ids:
            return 0
        total = 0
        sets = "stage = ?, updated_at = ?" + (", audio = ''" if clear_audio else "")
        with self._conn() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                total += conn.execute(
                    f"UPDATE chapters SET {sets} WHERE id IN ({marks})",
                    [stage, _now(), *chunk],
                ).rowcount
        return int(total or 0)

    def set_chapter_text(self, chapter_id: str, text: str) -> None:
        """改一章正文：内容变了，这一章退回「分章」（行与音频由调用方清理）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chapters SET text = ?, stage = 'chapters', audio = '',"
                " updated_at = ? WHERE id = ?",
                (text, _now(), chapter_id),
            )

    def update_chapter_text(self, chapter_id: str, text: str) -> None:
        """只替换正文：行还在，阶段不动，章成品失效（调用方随后维护行文本）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chapters SET text = ?, audio = '', updated_at = ? WHERE id = ?",
                (text, _now(), chapter_id),
            )

    def apply_row_texts(
        self, chapter_id: str, updates: list[tuple[str, str]]
    ) -> int:
        """文本处理改行：只改文本，说话人 / 情绪与人工修正标记都不动。

        文本变了的行旧音频失效；改空的行直接删掉并补齐序号。
        返回实际改动（含删除）的行数。
        """
        changed = 0
        with self._conn() as conn:
            for row_id, text in updates:
                current = conn.execute(
                    "SELECT text FROM rows WHERE id = ? AND chapter_id = ?",
                    (row_id, chapter_id),
                ).fetchone()
                if not current:
                    continue
                if not text.strip():
                    conn.execute(
                        "DELETE FROM rows WHERE id = ? AND chapter_id = ?",
                        (row_id, chapter_id),
                    )
                    changed += 1
                    continue
                if current["text"] == text:
                    continue
                conn.execute(
                    "UPDATE rows SET text = ?, error = '',"
                    " status = CASE WHEN audio <> '' THEN 'stale' ELSE 'text' END"
                    " WHERE id = ?",
                    (text, row_id),
                )
                changed += 1
            if not changed:
                return 0
            remaining = conn.execute(
                "SELECT id FROM rows WHERE chapter_id = ? ORDER BY ordinal", (chapter_id,)
            ).fetchall()
            for ordinal, item in enumerate(remaining):
                conn.execute(
                    "UPDATE rows SET ordinal = ? WHERE id = ?", (ordinal, item["id"])
                )
            conn.execute(
                "UPDATE chapters SET stage = ?, audio = '', updated_at = ? WHERE id = ?",
                ("analysis" if remaining else "chapters", _now(), chapter_id),
            )
        return changed

    # ---- 行 ---------------------------------------------------------------







    # ---- 章节结构 ---------------------------------------------------------

    def replace_chapters(self, project_id: str, chapters: list[tuple[str, str]]) -> None:
        """重分章：整本换掉。旧行随章节一起走（新章节没有行）。"""
        with self._conn() as conn:
            old = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM chapters WHERE project_id = ?", (project_id,)
                ).fetchall()
            ]
            for chapter_id in old:
                conn.execute("DELETE FROM rows WHERE chapter_id = ?", (chapter_id,))
            conn.execute("DELETE FROM chapters WHERE project_id = ?", (project_id,))
            # 章号换了，按章音色跟着旧章一起作废；新章由用户在分配表里重挑。
            conn.execute("DELETE FROM chapter_voices WHERE project_id = ?", (project_id,))
            now = _now()
            conn.executemany(
                "INSERT INTO chapters(id, project_id, ordinal, title, text, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (_new_id(), project_id, ordinal, title, text, now)
                    for ordinal, (title, text) in enumerate(chapters)
                ],
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )

    def delete_chapter(self, chapter_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM rows WHERE chapter_id = ?", (chapter_id,))
            conn.execute("DELETE FROM chapter_voices WHERE chapter_id = ?", (chapter_id,))
            conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))

    def renumber_chapters(self, project_id: str) -> None:
        """删章之后把序号补齐：界面上的 1..N 不能带洞。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM chapters WHERE project_id = ? ORDER BY ordinal", (project_id,)
            ).fetchall()
            now = _now()
            for ordinal, row in enumerate(rows):
                conn.execute(
                    "UPDATE chapters SET ordinal = ?, updated_at = ? WHERE id = ?",
                    (ordinal, now, row["id"]),
                )










    # ---- 回读预检 ---------------------------------------------------------











    # ---- 角色音色 ---------------------------------------------------------


















    # ---- 分析缓存 ---------------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM analysis_cache WHERE key = ?", (key,)
            ).fetchone()
        return str(row["payload"]) if row else None

    def cache_put(self, key: str, payload: str, *, keep: int = 500) -> None:
        """写入一份分析结果；只留最近 ``keep`` 份，缓存不是账本。"""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO analysis_cache(key, payload, created_at) VALUES(?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload,"
                " created_at = excluded.created_at",
                (key, payload, _now()),
            )
            conn.execute(
                "DELETE FROM analysis_cache WHERE key NOT IN"
                " (SELECT key FROM analysis_cache ORDER BY created_at DESC LIMIT ?)",
                (keep,),
            )

    def cache_size(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM analysis_cache").fetchone()
        return int(row["total"]) if row else 0

    # ---- 任务 -------------------------------------------------------------

    def create_job(
        self, project_id: str, kind: str, total: int = 0, exclusive: bool = True
    ) -> dict[str, Any]:
        job_id = _new_id()
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs(id, project_id, kind, state, total, owner, heartbeat_at,"
                " exclusive, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, kind, "running", total, self.owner, _stamp(),
                 1 if exclusive else 0, now, now),
            )
        return self.job(job_id) or {}

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def touch_job(self, job_id: str) -> None:
        """心跳：只要任务还在跑就每几秒写一次。"""
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (_stamp(), job_id))

    def reclaim_jobs(self) -> int:
        """把"确实没人推进"的任务标成中断，返回条数。

        判据只有两条，任一条成立就算中断：

        - 记在 ``owner`` 里的进程已经不在了（崩溃、被杀、另一个实例已退出）；
        - 心跳超过 :data:`_JOB_STALE_S` 没更新（进程还在但任务丢了）。

        另一个实例正在跑时两条都不成立——这是"同一行被两批同时合成"的闸门。
        """
        cutoff = _stamp() - _JOB_STALE_S
        interrupted: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, owner, heartbeat_at FROM jobs"
                " WHERE state IN ('running', 'cancelling')"
            ).fetchall()
            for row in rows:
                fresh = int(row["heartbeat_at"] or 0) >= cutoff
                if not fresh or not _owner_alive(str(row["owner"] or "")):
                    interrupted.append(str(row["id"]))
            for job_id in interrupted:
                conn.execute(
                    "UPDATE jobs SET state = 'interrupted', updated_at = ? WHERE id = ?",
                    (_now(), job_id),
                )
        return len(interrupted)

    def latest_job(
        self,
        project_id: str,
        conn: sqlite3.Connection | None = None,
        exclusive_only: bool = False,
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM jobs WHERE project_id = ?"
        if exclusive_only:
            sql += " AND exclusive = 1"
        rows = self._query(f"{sql} ORDER BY rowid DESC LIMIT 1", (project_id,), conn)
        return dict(rows[0]) if rows else None

    def update_job(
        self,
        job_id: str,
        *,
        state: str | None = None,
        message: str | None = None,
        total: int | None = None,
        done: int | None = None,
        failed: int | None = None,
    ) -> None:
        fields = {
            "state": state,
            "message": message,
            "total": total,
            "done": done,
            "failed": failed,
        }
        pairs = [(name, value) for name, value in fields.items() if value is not None]
        if not pairs:
            return
        sets = ", ".join(f"{name} = ?" for name, _ in pairs)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                [value for _, value in pairs] + [_now(), job_id],
            )

    def bump_job(self, job_id: str, *, done: int = 0, failed: int = 0) -> None:
        if not done and not failed:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET done = done + ?, failed = failed + ?, updated_at = ? WHERE id = ?",
                (done, failed, _now(), job_id),
            )

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1,"
                " state = CASE WHEN state = 'running' THEN 'cancelling' ELSE state END,"
                " updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )
        return self.job(job_id)

    def job_cancelled(self, job_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])
