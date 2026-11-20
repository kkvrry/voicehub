"""有声书项目存储：一个 SQLite 文件，四张表。

项目 → 章节 → 行 → 角色音色。行是合成的最小单位，章节是合并的最小单位。
所有写入走短连接 + WAL；并发合成时读多写少，够用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra.process import alive
from app.infra.sqlite import connect as sqlite_connect

from .rules import is_broken

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
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _voice_tuple(value: dict[str, Any] | None) -> tuple[str, ...] | None:
    """一条音色映射的比较键；没指定（或没有音色 id）就是空。"""
    if not value or not value.get("voice_id"):
        return None
    return (
        str(value.get("engine") or ""),
        str(value.get("voice_id") or ""),
        str(value.get("voice_name") or ""),
        str(value.get("sample_mode") or "emotion"),
        str(value.get("sample_file") or ""),
    )


def _params_json(params: dict[str, Any]) -> str:
    """一行参数落库的形状：键排序，空表就是 ``{}``（指纹据此判定"没改过"）。"""
    return json.dumps(params or {}, ensure_ascii=False, sort_keys=True)


def _params_dict(raw: Any) -> dict[str, Any]:
    """库里那串参数 → 字典；坏 JSON 当没设过（不让一条脏数据废掉整行）。"""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


#: ``projects`` 里除 ``source`` 之外的全部列：详情与路径解析只需要这些。
_PROJECT_META = (
    "id, name, author, folder, kind, pattern_key, split_mode, split_dialogue,"
    " split_quote, split_max_chars, layout_version, created_at, updated_at"
)


def _broken_flag(text: str | None) -> int:
    """SQL 里的「需校对」判据：与行表筛选共用 :func:`is_broken` 同一句正则。"""
    return 1 if is_broken(text or "") else 0


class AudiobookStore:
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
            "UPDATE rows SET error = '', status = 'text'"
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
                   COALESCE(s.stale, 0) AS stale
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

    def rows(self, chapter_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM rows WHERE chapter_id = ? ORDER BY ordinal", (chapter_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def all_rows(self, project_id: str) -> list[dict[str, Any]]:
        """整个项目的行，按章序 / 行序排好。

        逐章 ``rows()`` 是「章数」次连接：106 章的书实测 888 ms，同样的数据一次
        join 只要 167 ms。凡是「要看全书的行」的地方都走这里。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT r.* FROM rows r JOIN chapters c ON c.id = r.chapter_id"
                " WHERE c.project_id = ? ORDER BY c.ordinal, r.ordinal",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stale_rows(self, project_id: str, chapter_id: str = "") -> list[dict[str, Any]]:
        """标成过期的行（参数变了、旧音频还留着）——等待按指纹判定能不能复活。"""
        sql = (
            "SELECT r.* FROM rows r JOIN chapters c ON c.id = r.chapter_id"
            " WHERE c.project_id = ? AND r.status = 'stale'"
        )
        args: list[Any] = [project_id]
        if chapter_id:
            sql += " AND r.chapter_id = ?"
            args.append(chapter_id)
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def mark_rows_audio(self, row_ids: list[str]) -> int:
        """把一批行恢复成「已生成」：指纹又对上了，旧音频重新算数。"""
        if not row_ids:
            return 0
        total = 0
        with self._conn() as conn:
            # SQLite 的变量上限按版本不同（999 / 32766），分批发最稳。
            for start in range(0, len(row_ids), 500):
                chunk = row_ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                total += conn.execute(
                    f"UPDATE rows SET status = 'audio', error = '' WHERE id IN ({marks})",
                    chunk,
                ).rowcount
        return int(total or 0)

    def set_split_options(
        self,
        project_id: str,
        *,
        mode: str,
        dialogue: bool,
        quote: str,
        max_chars: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET split_mode = ?, split_dialogue = ?, split_quote = ?,"
                " split_max_chars = ?, updated_at = ? WHERE id = ?",
                (
                    mode,
                    1 if dialogue else 0,
                    quote,
                    max_chars,
                    _now(),
                    project_id,
                ),
            )

    def replace_rows(self, chapter_id: str, rows: list[dict[str, Any]]) -> None:
        """整章换行。``rows`` 可带音频/人工标记：重拆时按文本认亲后原样搬过来。

        字幕项目还带 ``start_ms`` / ``end_ms``（原字幕时间码）；书稿项目没有这两个键，
        落库就是 0——与老行为一致。
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM rows WHERE chapter_id = ?", (chapter_id,))
            conn.executemany(
                "INSERT INTO rows(id, chapter_id, ordinal, kind, speaker, text, emotion,"
                " speaker_edited, emotion_edited, audio, audio_key, error, params,"
                " start_ms, end_ms, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        _new_id(),
                        chapter_id,
                        index,
                        row.get("kind", "narration"),
                        row.get("speaker", ""),
                        row["text"],
                        row.get("emotion", ""),
                        int(row.get("speaker_edited", 0)),
                        int(row.get("emotion_edited", 0)),
                        row.get("audio", ""),
                        row.get("audio_key", ""),
                        row.get("error", ""),
                        row.get("params", "{}"),
                        int(row.get("start_ms") or 0),
                        int(row.get("end_ms") or 0),
                        row.get("status", "text"),
                    )
                    for index, row in enumerate(rows)
                ],
            )

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

    def set_row_annotation(self, row_id: str, speaker: str, emotion: str) -> bool:
        """写入模型推断值；人工改过的字段不动。返回是否有字段真的变了。

        变了的行，旧音频**不删也不清引用**，只标成 ``stale``：参数改回去时靠指纹
        自动复活（见 pipeline ``_settle_stale``），改了别的就等重新生成覆盖。
        """
        current = self.row(row_id)
        if not current:
            return False
        new_speaker = current["speaker"] if current["speaker_edited"] else speaker
        new_emotion = current["emotion"] if current["emotion_edited"] else emotion
        changed = new_speaker != current["speaker"] or new_emotion != current["emotion"]
        audio = current["audio"]
        status = ("stale" if audio else "analyzed") if changed else current["status"]
        # 说话人与「这一行是不是对白」是同一件事的两面：界面按 `kind` 决定高亮、
        # 筛选与统计。只写 speaker 会让"有人说话的行"继续长着旁白的样子
        # （旧分析路径就是这么漏的，见 `_migrate` 里那条修复）。
        new_kind = "narration" if new_speaker in ("", "旁白") else "dialogue"
        with self._conn() as conn:
            # `audio` / `audio_key` 一律不动：过期的音频留着（能播、也能在改回去时复活），
            # 由 `status` 说明它现在算不算数。
            conn.execute(
                "UPDATE rows SET speaker = ?, emotion = ?, kind = ?, status = ? WHERE id = ?",
                (new_speaker, new_emotion, new_kind, status, row_id),
            )
        return changed

    def update_row(
        self,
        row_id: str,
        *,
        speaker: str | None = None,
        emotion: str | None = None,
        text: str | None = None,
    ) -> bool:
        """人工修正说话人 / 情绪 / 文本；只改传进来的那几项。

        改过的字段记 ``*_edited``，后续分析不再覆盖；这一行的旧音频标成过期
        （``stale``）但留着——改回原样时按指纹自动恢复，不用重新生成。
        """
        current = self.row(row_id)
        if not current:
            return False
        new_speaker = current["speaker"] if speaker is None else str(speaker).strip()
        new_emotion = current["emotion"] if emotion is None else emotion
        new_text = current["text"] if text is None else text
        # 占位「旁白」不是角色名；其余非空说话人就是对白。
        new_kind = (
            current["kind"]
            if speaker is None
            else ("narration" if new_speaker in ("", "旁白") else "dialogue")
        )
        changed = (
            new_speaker != current["speaker"]
            or new_emotion != current["emotion"]
            or new_text != current["text"]
            or new_kind != current["kind"]
        )
        with self._conn() as conn:
            # 旧音频留着（`audio` / `audio_key` 不动），改回去时能自动恢复成「已生成」；
            # 代价只是状态先标成 `stale`，统计里算「待生成」。
            conn.execute(
                "UPDATE rows SET speaker = ?, emotion = ?, text = ?, kind = ?,"
                " speaker_edited = ?, emotion_edited = ?,"
                " error = CASE WHEN ? THEN '' ELSE error END, status = ? WHERE id = ?",
                (
                    new_speaker,
                    new_emotion,
                    new_text,
                    new_kind,
                    1 if speaker is not None else current["speaker_edited"],
                    1 if emotion is not None else current["emotion_edited"],
                    1 if changed else 0,
                    ("stale" if current["audio"] else "text") if changed else current["status"],
                    row_id,
                ),
            )
        if changed:
            self.invalidate_chapter(current["chapter_id"], "analysis")
        return changed

    def set_row_audio(
        self, row_id: str, audio: str, key: str = "", key_version: int = 1
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET audio = ?, audio_key = ?, audio_key_v = ?,"
                " error = '', status = 'audio' WHERE id = ?",
                (audio, key, int(key_version), row_id),
            )
            conn.execute("DELETE FROM row_claims WHERE row_id = ?", (row_id,))

    def set_row_audio_keys(self, items: list[tuple[str, str]]) -> None:
        """把老行的指纹升到 v2，**不动音频与状态**。

        参数是 ``(新指纹, 行 id)``。这批行的旧指纹刚刚比对成功，说明磁盘上的音频
        就是当前输入的结果；换键只是把"以后按什么判失效"换成新口径，不产生任何
        重做。升级时的唯一假设：引擎身份自那次合成以来没变——变了的话，用户下次
        生成该行时自然会重做（新指纹对不上）。
        """
        if not items:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE rows SET audio_key = ?, audio_key_v = 2 WHERE id = ?", items
            )

    def set_row_annotations(self, items: list[tuple[str, str, str, str]]) -> int:
        """一批行写模型推断值，返回真的变了的行数。

        每项是 ``(行, 说话人, 情绪, 引导指令)``；引导指令为空 = 这一批没要求它
        （只标情绪的提示词），非空才写进行参数。行里已经有一条引导指令时不覆盖：
        那是用户自己写的，模型只补空位。

        规则与 :meth:`set_row_annotation` 相同（人工改过的字段不动、旧音频标过期），
        但读一遍、写完一批：分析一本书一万五千行，逐行两次连接就是三成时间。
        """
        if not items:
            return 0
        ids = [str(item[0]) for item in items]
        updates: list[tuple[Any, ...]] = []
        changed = 0
        with self._conn() as conn:
            current: dict[str, dict[str, Any]] = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    "SELECT id, speaker, emotion, speaker_edited, emotion_edited,"
                    f" audio, params FROM rows WHERE id IN ({marks})",
                    chunk,
                ).fetchall():
                    current[str(row["id"])] = dict(row)
            for row_id, speaker, emotion, instruction in items:
                item = current.get(str(row_id))
                if not item:
                    continue
                new_speaker = item["speaker"] if item["speaker_edited"] else speaker
                new_emotion = item["emotion"] if item["emotion_edited"] else emotion
                params = _params_dict(item["params"])
                written = ""
                if instruction and not str(params.get("instruction") or "").strip():
                    params["instruction"] = instruction
                    written = instruction
                if (
                    new_speaker == item["speaker"]
                    and new_emotion == item["emotion"]
                    and not written
                ):
                    continue
                status = "stale" if item["audio"] else "analyzed"
                # 与单行版同一条规则：说话人决定这一行算不算对白。
                kind = "narration" if new_speaker in ("", "旁白") else "dialogue"
                updates.append(
                    (
                        new_speaker,
                        new_emotion,
                        kind,
                        status,
                        _params_json(params),
                        str(row_id),
                    )
                )
                changed += 1
            if updates:
                conn.executemany(
                    "UPDATE rows SET speaker = ?, emotion = ?, kind = ?, status = ?,"
                    " params = ? WHERE id = ?",
                    updates,
                )
        return changed

    def set_row_params(self, row_id: str, params: dict[str, Any]) -> None:
        """行级合成参数：指纹里含它，改了就作废这一行的音频。

        与改角色/情绪同一套：音频留着、标过期，参数改回去时自动恢复。
        """
        row = self.row(row_id)
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET params = ?, error = '', status = ? WHERE id = ?",
                (_params_json(params), "stale" if row and row["audio"] else "text", row_id),
            )
        if row:
            self.invalidate_chapter(str(row["chapter_id"]), "analysis")

    def set_row_error(self, row_id: str, message: str) -> None:
        """单行失败：保留原因，音频置空，任务继续跑其余行。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET audio = '', audio_key = '', error = ?, status = 'failed'"
                " WHERE id = ?",
                (message, row_id),
            )
            conn.execute("DELETE FROM row_claims WHERE row_id = ?", (row_id,))

    def claim_row(self, row_id: str) -> bool:
        """抢下这一行的合成权；返回 ``False`` = 另一个 worker 正在做它。

        批量与单行重试都从这里过：没有占位时，两个实例的计划会把同一行各排一遍，
        最后写同一个文件（``write_bytes`` 会截断），留下两份字节拼起来的音频。
        """
        now = _stamp()
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO row_claims(row_id, owner, claimed_at) VALUES(?,?,?)"
                " ON CONFLICT(row_id) DO UPDATE SET owner = excluded.owner,"
                " claimed_at = excluded.claimed_at"
                " WHERE row_claims.claimed_at < ?",
                (row_id, self.owner, now, now - _ROW_CLAIM_S),
            )
        return cursor.rowcount == 1

    def release_row(self, row_id: str) -> None:
        """放开自己占的行；别人的占位不动。成功 / 失败两条路都会调它。"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM row_claims WHERE row_id = ? AND owner = ?", (row_id, self.owner)
            )

    def row(self, row_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM rows WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def insert_row_after(
        self,
        chapter_id: str,
        after_id: str,
        text: str,
        *,
        speaker: str = "",
        emotion: str = "",
        kind: str = "narration",
        speaker_edited: int = 0,
        emotion_edited: int = 0,
    ) -> dict[str, Any]:
        rows = self.rows(chapter_id)
        after = next((row for row in rows if row["id"] == after_id), None)
        ordinal = int(after["ordinal"]) + 1 if after else len(rows)
        row_id = _new_id()
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET ordinal = ordinal + 1 WHERE chapter_id = ? AND ordinal >= ?",
                (chapter_id, ordinal),
            )
            conn.execute(
                "INSERT INTO rows(id, chapter_id, ordinal, kind, speaker, text, emotion,"
                " speaker_edited, emotion_edited) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    chapter_id,
                    ordinal,
                    kind,
                    speaker,
                    text,
                    emotion,
                    speaker_edited,
                    emotion_edited,
                ),
            )
        self.invalidate_chapter(chapter_id, "sentences")
        return self.row(row_id) or {}

    def delete_row(self, row_id: str) -> dict[str, Any] | None:
        row = self.row(row_id)
        if not row:
            return None
        with self._conn() as conn:
            conn.execute("DELETE FROM rows WHERE id = ?", (row_id,))
            conn.execute(
                "UPDATE rows SET ordinal = ordinal - 1 WHERE chapter_id = ? AND ordinal > ?",
                (row["chapter_id"], row["ordinal"]),
            )
        self.invalidate_chapter(row["chapter_id"], "sentences")
        return row

    def merge_rows(self, row_ids: list[str]) -> dict[str, Any] | None:
        """把选中的多行并成一行：按原顺序接文本，角色取先有的那个。

        行 id 先去重：同一个 id 传两次，文本会被接上两遍，而删除时只删得掉一份。
        """
        rows = [
            row
            for row in (self.row(row_id) for row_id in dict.fromkeys(row_ids))
            if row
        ]
        if len(rows) < 2:
            return rows[0] if rows else None
        chapter_id = rows[0]["chapter_id"]
        if any(row["chapter_id"] != chapter_id for row in rows):
            return None
        rows.sort(key=lambda row: int(row["ordinal"]))
        first = rows[0]
        text = "".join(str(row["text"] or "") for row in rows)
        kind = "dialogue" if any(row["kind"] == "dialogue" for row in rows) else "narration"
        speaker = next((row["speaker"] for row in rows if row["speaker"]), "")
        emotion = next((row["emotion"] for row in rows if row["emotion"]), "")
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET text = ?, kind = ?, speaker = ?, emotion = ?,"
                " speaker_edited = ?, emotion_edited = ?, audio = '', audio_key = '',"
                " error = '', status = 'text' WHERE id = ?",
                (
                    text,
                    kind,
                    speaker,
                    emotion,
                    1 if any(row["speaker_edited"] for row in rows) else 0,
                    1 if any(row["emotion_edited"] for row in rows) else 0,
                    first["id"],
                ),
            )
            for row in rows[1:]:
                conn.execute("DELETE FROM rows WHERE id = ?", (row["id"],))
            remaining = conn.execute(
                "SELECT id FROM rows WHERE chapter_id = ? ORDER BY ordinal", (chapter_id,)
            ).fetchall()
            for index, item in enumerate(remaining):
                conn.execute("UPDATE rows SET ordinal = ? WHERE id = ?", (index, item["id"]))
        self.invalidate_chapter(chapter_id, "sentences")
        return self.row(first["id"])

    def auto_merge_rows(self, chapter_id: str, groups: list[list[str]]) -> int:
        """按组把旁白行并成一行：组内文本按原顺序相接，其余字段取组内第一行。

        与 :meth:`merge_rows` 同一套结果（旧音频作废、序号补齐、阶段退回拆句），
        区别是一次事务写完所有组：一章几百行逐组提交要拖上几秒。
        返回被并入的行数。
        """
        if not groups:
            return 0
        removed = 0
        with self._conn() as conn:
            current = {
                str(item["id"]): dict(item)
                for item in conn.execute(
                    "SELECT * FROM rows WHERE chapter_id = ? ORDER BY ordinal", (chapter_id,)
                ).fetchall()
            }
            for ids in groups:
                rows = [current[str(row_id)] for row_id in ids if str(row_id) in current]
                if len(rows) < 2:
                    continue
                text = "".join(str(row["text"] or "") for row in rows)
                conn.execute(
                    "UPDATE rows SET text = ?, kind = 'narration', audio = '',"
                    " audio_key = '', error = '', status = 'text' WHERE id = ?",
                    (text, rows[0]["id"]),
                )
                for row in rows[1:]:
                    conn.execute("DELETE FROM rows WHERE id = ?", (row["id"],))
                removed += len(rows) - 1
            remaining = conn.execute(
                "SELECT id FROM rows WHERE chapter_id = ? ORDER BY ordinal", (chapter_id,)
            ).fetchall()
            for index, item in enumerate(remaining):
                conn.execute("UPDATE rows SET ordinal = ? WHERE id = ?", (index, item["id"]))
        self.invalidate_chapter(chapter_id, "sentences")
        return removed

    def batch_update(
        self, row_ids: list[str], *, speaker: str | None = None, emotion: str | None = None
    ) -> int:
        """批量改角色 / 情绪；只写传进来的字段，并记成人工修正。

        读一遍、写完一批：以前每条都 ``row()`` 一次再 UPDATE 一次，框选五百行
        就是一千次连接 + 一千次提交，界面要转好几秒。
        """
        ids = [str(item) for item in row_ids if str(item).strip()]
        if not ids:
            return 0
        chapters: set[str] = set()
        updates: list[tuple[Any, ...]] = []
        with self._conn() as conn:
            current: dict[str, dict[str, Any]] = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT * FROM rows WHERE id IN ({marks})", chunk
                ).fetchall():
                    current[str(row["id"])] = dict(row)
            for row_id in ids:
                item = current.get(row_id)
                if not item:
                    continue
                new_speaker = item["speaker"] if speaker is None else str(speaker).strip()
                new_emotion = item["emotion"] if emotion is None else emotion
                new_kind = (
                    item["kind"]
                    if speaker is None
                    else ("narration" if new_speaker in ("", "旁白") else "dialogue")
                )
                if (
                    new_speaker == item["speaker"]
                    and new_emotion == item["emotion"]
                    and new_kind == item["kind"]
                ):
                    continue
                updates.append(
                    (
                        new_speaker,
                        new_emotion,
                        new_kind,
                        1 if speaker is not None else item["speaker_edited"],
                        1 if emotion is not None else item["emotion_edited"],
                        row_id,
                    )
                )
                chapters.add(str(item["chapter_id"]))
            if not updates:
                return 0
            for start in range(0, len(updates), 500):
                conn.executemany(
                    "UPDATE rows SET speaker = ?, emotion = ?, kind = ?, speaker_edited = ?,"
                    " emotion_edited = ?, error = '',"
                    # 'analysis' 不是合法行状态：界面统计"已标注"只认 'analyzed'，
                    # 写成 'analysis' 的行永远不算分析完成，还会被原样搬进下一版。
                    " status = CASE WHEN audio <> '' THEN 'stale' ELSE 'analyzed' END"
                    " WHERE id = ?",
                    updates[start : start + 500],
                )
        self.set_chapters_stage(sorted(chapters), "analysis", clear_audio=True)
        return len(updates)

    # ---- 角色音色 ---------------------------------------------------------

    def speakers(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT r.speaker, COUNT(r.id) AS rows,
                   COALESCE(v.engine, '') AS engine,
                   COALESCE(v.voice_id, '') AS voice_id,
                   COALESCE(v.voice_name, '') AS voice_name
            FROM rows r
            JOIN chapters c ON c.id = r.chapter_id
            LEFT JOIN voices v ON v.project_id = c.project_id AND v.speaker = r.speaker
            WHERE c.project_id = ? AND r.speaker <> '' AND r.speaker <> '旁白'
            GROUP BY r.speaker
            ORDER BY rows DESC, r.speaker
            """,
            (project_id,),
            conn,
        )
        return [dict(row) for row in rows]

    def narration_rows(self, project_id: str) -> int:
        """旁白行数：说话人为空或写着「旁白」的行，与行表同一判据。"""
        row = self._query(
            "SELECT COUNT(*) AS n FROM rows r"
            " JOIN chapters c ON c.id = r.chapter_id"
            " WHERE c.project_id = ? AND COALESCE(r.speaker, '') IN ('', '旁白')",
            (project_id,),
        )
        return int(row[0]["n"]) if row else 0

    def speaker_lines(
        self,
        project_id: str,
        speakers: list[str],
        *,
        limit: int = 2,
        chars: int = 80,
    ) -> dict[str, list[str]]:
        """每个角色前几条对白：给 AI 判断语气与年龄感用，截断到 ``chars`` 字。"""
        names = [str(name).strip() for name in speakers if str(name).strip()]
        if not names:
            return {}
        marks = ",".join("?" for _ in names)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT speaker, text FROM ("
                f"  SELECT r.speaker, r.text,"
                f"         ROW_NUMBER() OVER ("
                f"           PARTITION BY r.speaker ORDER BY c.ordinal, r.ordinal"
                f"         ) AS n"
                f"  FROM rows r JOIN chapters c ON c.id = r.chapter_id"
                f"  WHERE c.project_id = ? AND r.speaker IN ({marks}) AND r.text <> ''"
                f") WHERE n <= ?",
                (project_id, *names, max(1, int(limit))),
            ).fetchall()
        out: dict[str, list[str]] = {name: [] for name in names}
        for row in rows:
            text = str(row["text"] or "").strip()
            if len(text) > chars:
                text = text[:chars] + "…"
            if text:
                out[str(row["speaker"])].append(text)
        return out

    def merge_speakers(self, project_id: str, names: list[str], target: str) -> int:
        """把若干说话人并成一个名字，返回改动的行数。

        只动 `rows`：说话人换成 ``target`` 并记 ``speaker_edited``（以后分析不覆盖）；
        这些行的旧音频作废——音色跟着说话人走，旧音频不能再念。`voices` 表不动：
        旧角色的音色映射留在原处，没有行引用它自然不显示。
        """
        sources = [str(name).strip() for name in names if str(name).strip()]
        if not sources:
            return 0
        marks = ",".join("?" for _ in sources)
        with self._conn() as conn:
            chapters = [
                str(row["chapter_id"])
                for row in conn.execute(
                    f"SELECT DISTINCT r.chapter_id FROM rows r"
                    f" JOIN chapters c ON c.id = r.chapter_id"
                    f" WHERE c.project_id = ? AND r.speaker IN ({marks})",
                    (project_id, *sources),
                ).fetchall()
            ]
            changed = conn.execute(
                f"UPDATE rows SET speaker = ?, speaker_edited = 1, error = '',"
                f" status = CASE WHEN audio <> '' THEN 'stale' ELSE 'text' END"
                f" WHERE speaker IN ({marks})"
                f" AND chapter_id IN (SELECT id FROM chapters WHERE project_id = ?)",
                (target, *sources, project_id),
            ).rowcount
        self.set_chapters_stage(chapters, "analysis", clear_audio=True)
        return int(changed or 0)

    def set_voice(
        self,
        project_id: str,
        speaker: str,
        engine: str,
        voice_id: str,
        voice_name: str,
        *,
        sample_mode: str = "emotion",
        sample_file: str = "",
    ) -> None:
        """指定角色音色。``sample_mode``：``emotion`` 按行情绪匹配样例 / ``fixed`` 钉住一条。

        **不动 ``reason``**：那是 AI 给这个角色写下的音色描述，属于角色而不是某颗音色，
        换引擎 / 换音色 / 换样例都该留着。
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO voices(project_id, speaker, engine, voice_id, voice_name,"
                " sample_mode, sample_file) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id, speaker) DO UPDATE SET"
                " engine = excluded.engine, voice_id = excluded.voice_id,"
                " voice_name = excluded.voice_name, sample_mode = excluded.sample_mode,"
                " sample_file = excluded.sample_file",
                (project_id, speaker, engine, voice_id, voice_name, sample_mode, sample_file),
            )

    def set_voices(
        self, project_id: str, assignments: list[dict[str, Any]]
    ) -> list[str]:
        """批量指定角色音色，返回真正发生变化的角色名。

        一次事务写完；调用方只对返回的角色作废旧音频，没变的不动。
        """
        if not assignments:
            return []
        with self._conn() as conn:
            existing = {
                str(row["speaker"]): row
                for row in conn.execute(
                    "SELECT speaker, engine, voice_id, voice_name, sample_mode, sample_file"
                    " FROM voices WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
            changed: list[str] = []
            for item in assignments:
                speaker = str(item.get("speaker") or "").strip()
                if not speaker:
                    continue
                engine = str(item.get("engine") or "")
                voice_id = str(item.get("voice_id") or "")
                voice_name = str(item.get("voice_name") or "")
                sample_mode = str(item.get("sample_mode") or "emotion")
                sample_file = str(item.get("sample_file") or "")
                reason = str(item.get("reason") or "").strip()
                conn.execute(
                    "INSERT INTO voices(project_id, speaker, engine, voice_id, voice_name,"
                    " sample_mode, sample_file, reason) VALUES(?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(project_id, speaker) DO UPDATE SET"
                    " engine = excluded.engine, voice_id = excluded.voice_id,"
                    " voice_name = excluded.voice_name, sample_mode = excluded.sample_mode,"
                    " sample_file = excluded.sample_file,"
                    # 带了新的依据就替换；没带就留着——描述属于角色，不随音色作废。
                    " reason = CASE"
                    "   WHEN excluded.reason <> '' THEN excluded.reason"
                    "   ELSE voices.reason END",
                    (
                        project_id,
                        speaker,
                        engine,
                        voice_id,
                        voice_name,
                        sample_mode,
                        sample_file,
                        reason,
                    ),
                )
                old = existing.get(speaker)
                if old is None or (
                    str(old["engine"]),
                    str(old["voice_id"]),
                    str(old["voice_name"]),
                    str(old["sample_mode"]),
                    str(old["sample_file"]),
                ) != (engine, voice_id, voice_name, sample_mode, sample_file):
                    changed.append(speaker)
            return changed

    def voice_map(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, dict[str, str]]:
        rows = self._query("SELECT * FROM voices WHERE project_id = ?", (project_id,), conn)
        return {
            row["speaker"]: {
                "engine": row["engine"],
                "voice_id": row["voice_id"],
                "voice_name": row["voice_name"],
                "sample_mode": row["sample_mode"] or "emotion",
                "sample_file": row["sample_file"] or "",
                # AI 给这个角色写的音色描述；空 = 还没写过。换音色不清它。
                "reason": row["reason"] or "",
            }
            for row in rows
        }

    def chapter_voice_map(
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, dict[str, str]]:
        """按章指定的旁白音色：章 id → 映射（没有单独指定过的章不在里面）。"""
        rows = self._query(
            "SELECT chapter_id, engine, voice_id, voice_name, sample_mode, sample_file"
            " FROM chapter_voices WHERE project_id = ?",
            (project_id,),
            conn,
        )
        return {
            str(row["chapter_id"]): {
                "engine": row["engine"],
                "voice_id": row["voice_id"],
                "voice_name": row["voice_name"],
                "sample_mode": row["sample_mode"] or "emotion",
                "sample_file": row["sample_file"] or "",
            }
            for row in rows
        }

    def has_named_speakers(self, project_id: str) -> bool:
        """这本书有没有识别出具名角色；没有时分配表按章列行。"""
        return bool(
            self._query(
                "SELECT 1 AS found FROM rows r JOIN chapters c ON c.id = r.chapter_id"
                " WHERE c.project_id = ?"
                " AND COALESCE(r.speaker, '') NOT IN ('', '旁白', '对话', '未知')"
                " LIMIT 1",
                (project_id,),
            )
        )

    def set_chapter_voices(
        self, project_id: str, assignments: list[dict[str, Any]]
    ) -> list[str]:
        """按章写旁白音色，返回真正变化的章 id。

        第一次只为某一章指定时，其余各章沿用这颗音色——用户只挑一次就够；
        书里已有章级音色后再改一章，只改这一章，其余章保持当前的生效音色，
        改一章不会悄悄把别的章一起换掉。
        """
        if not assignments:
            return []
        with self._conn() as conn:
            chapters = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM chapters WHERE project_id = ? ORDER BY ordinal",
                    (project_id,),
                ).fetchall()
            ]
            known = set(chapters)
            existing = {
                str(row["chapter_id"]): {
                    "engine": row["engine"],
                    "voice_id": row["voice_id"],
                    "voice_name": row["voice_name"],
                    "sample_mode": row["sample_mode"] or "emotion",
                    "sample_file": row["sample_file"] or "",
                }
                for row in conn.execute(
                    "SELECT * FROM chapter_voices WHERE project_id = ?", (project_id,)
                ).fetchall()
            }
            incoming: dict[str, dict[str, str] | None] = {}
            for item in assignments:
                chapter_id = str(item.get("chapter_id") or "").strip()
                if chapter_id not in known:
                    continue
                voice_id = str(item.get("voice_id") or "")
                if not voice_id:
                    incoming[chapter_id] = None
                    continue
                incoming[chapter_id] = {
                    "engine": str(item.get("engine") or ""),
                    "voice_id": voice_id,
                    "voice_name": str(item.get("voice_name") or ""),
                    "sample_mode": str(item.get("sample_mode") or "emotion"),
                    "sample_file": str(item.get("sample_file") or ""),
                }
            default = next(
                (existing[cid] for cid in chapters if existing.get(cid, {}).get("voice_id")),
                None,
            )
            first = next((incoming[cid] for cid in chapters if incoming.get(cid)), None)
            changed: list[str] = []
            for chapter_id in chapters:
                before = existing.get(chapter_id)
                if chapter_id in incoming:
                    after = incoming[chapter_id]
                elif before and before.get("voice_id"):
                    after = before
                elif default is not None:
                    after = default
                else:
                    after = first
                if _voice_tuple(before) != _voice_tuple(after):
                    changed.append(chapter_id)
                if after is None:
                    conn.execute(
                        "DELETE FROM chapter_voices WHERE project_id = ? AND chapter_id = ?",
                        (project_id, chapter_id),
                    )
                    continue
                conn.execute(
                    "INSERT INTO chapter_voices(project_id, chapter_id, engine, voice_id,"
                    " voice_name, sample_mode, sample_file) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(project_id, chapter_id) DO UPDATE SET"
                    " engine = excluded.engine, voice_id = excluded.voice_id,"
                    " voice_name = excluded.voice_name, sample_mode = excluded.sample_mode,"
                    " sample_file = excluded.sample_file",
                    (
                        project_id,
                        chapter_id,
                        after["engine"],
                        after["voice_id"],
                        after["voice_name"],
                        after["sample_mode"],
                        after["sample_file"],
                    ),
                )
            return changed

    def chapter_voice_assignments(self) -> list[dict[str, str]]:
        """全项目的章级音色映射：扫描自愈一次取回。"""
        rows = self._query(
            "SELECT project_id, chapter_id, engine, voice_id, voice_name FROM chapter_voices"
        )
        return [dict(row) for row in rows]

    def rebind_chapter_voice_ids(
        self, items: list[tuple[str, str, str, str]]
    ) -> int:
        """按 (项目, 章) 改写章级音色的 id 与显示名；只给扫描自愈用。"""
        if not items:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "UPDATE chapter_voices SET voice_id = ?, voice_name = ?"
                " WHERE project_id = ? AND chapter_id = ?",
                [
                    (voice_id, voice_name, project_id, chapter_id)
                    for project_id, chapter_id, voice_id, voice_name in items
                ],
            )
        return len(items)

    def invalidate_chapters(self, project_id: str, chapter_ids: list[str]) -> None:
        """换章级音色：只作废这些章里的行与章成品，别的章不动。

        没有具名角色时章级音色管整章；有具名角色时只影响占位说话人的行。
        音频留着不删：换回原音色时指纹又能对上，行自动恢复成「已生成」。
        """
        ids = [str(item) for item in chapter_ids if str(item).strip()]
        if not ids:
            return
        # 没有具名角色时，章级音色对整章的行都生效；有具名角色时只动占位旁白行。
        named = self.has_named_speakers(project_id)
        with self._conn() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                conn.execute(
                    "UPDATE rows SET error = '',"
                    " status = CASE"
                    "   WHEN audio <> '' THEN 'stale'"
                    "   WHEN status IN ('audio', 'failed') THEN 'analyzed'"
                    "   ELSE status END"
                    f" WHERE chapter_id IN ({marks})"
                    + (
                        ""
                        if not named
                        else " AND COALESCE(speaker, '') IN ('', '旁白', '对话', '未知')"
                    ),
                    chunk,
                )
                conn.execute(
                    "UPDATE chapters SET audio = '',"
                    " stage = CASE WHEN stage IN ('merged', 'audio') THEN 'analysis'"
                    "   ELSE stage END,"
                    f" updated_at = ? WHERE project_id = ? AND id IN ({marks})",
                    (_now(), project_id, *chunk),
                )

    def voice_assignments(self) -> list[dict[str, str]]:
        """全项目的角色→音色映射：扫描自愈一次取回，不逐个音色回查。"""
        rows = self._query(
            "SELECT project_id, speaker, engine, voice_id, voice_name FROM voices"
        )
        return [dict(row) for row in rows]

    def rebind_voice_ids(
        self, items: list[tuple[str, str, str, str]]
    ) -> int:
        """按 (项目, 角色) 改写音色 id 与显示名；样例策略与描述原样保留。

        只给扫描自愈用：本地音色的 id 跟着目录路径走，目录移动后新 id 对着
        同一套文件，旧音频仍然有效。
        """
        if not items:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "UPDATE voices SET voice_id = ?, voice_name = ?"
                " WHERE project_id = ? AND speaker = ?",
                [
                    (voice_id, voice_name, project_id, speaker)
                    for project_id, speaker, voice_id, voice_name in items
                ],
            )
        return len(items)

    def invalidate_speaker(self, project_id: str, speaker: str) -> None:
        """换音色：这个角色的旧音频全部标过期，章成品失效。"""
        self.invalidate_speakers(project_id, [speaker])

    def invalidate_speakers(self, project_id: str, speakers: list[str]) -> None:
        """批量换音色：一次性作废这些角色的旧音频与相关章成品。

        音频留着不删：换回原音色时指纹又能对上，行自动恢复成「已生成」。
        """
        names = [str(name).strip() for name in speakers if str(name).strip()]
        if not names:
            return
        # 旁白没有名字：行上的 speaker 可能是空串，也可能写着「旁白」。
        # 两者都取 ``row["speaker"] or 旁白`` 那一颗音色，换音色就得一起作废。
        clauses = [f"speaker IN ({','.join('?' for _ in names)})"]
        if "旁白" in names:
            clauses.append("speaker = ''")
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET error = '',"
                " status = CASE"
                "   WHEN audio <> '' THEN 'stale'"
                "   WHEN status IN ('audio', 'failed') THEN 'analyzed'"
                "   ELSE status END"
                f" WHERE ({' OR '.join(clauses)}) AND chapter_id IN"
                " (SELECT id FROM chapters WHERE project_id = ?)",
                (*names, project_id),
            )
            conn.execute(
                "UPDATE chapters SET audio = '',"
                " stage = CASE WHEN stage IN ('merged', 'audio') THEN 'analysis' ELSE stage END,"
                " updated_at = ? WHERE project_id = ?",
                (_now(), project_id),
            )

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

    def create_job(self, project_id: str, kind: str, total: int = 0) -> dict[str, Any]:
        job_id = _new_id()
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs(id, project_id, kind, state, total, owner, heartbeat_at,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, kind, "running", total, self.owner, _stamp(), now, now),
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
        self, project_id: str, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY rowid DESC LIMIT 1",
            (project_id,),
            conn,
        )
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
