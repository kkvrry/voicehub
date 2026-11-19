"""SQLite 索引（``hub.db``）——可随时重建的派生物。

- 本地音色的元数据源头是 sidecar，这里只是索引，重扫时本地行增量重写
  （``scan_stamp`` 对得上的整条跳过，见 ``LibraryService.scan``）；
- ``hidden`` / ``favorite`` 是服务级偏好（不进 sidecar），重扫时按 id 保留；
- 平台音色没有文件夹，重扫不动；
- ``probe_cache`` 只服务时长（路径 + 指纹 → 时长），删掉无数据损失，只是重扫全量重量；
- ``idempotency`` 是合成与克隆的幂等键登记：有效期内重复提交直接返回首次结果，
  防止按量计费引擎因网络重试重复扣费。过期记录在写入时清理。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from app.domain.models import SOURCE_LOCAL, Sample, Voice
from app.infra.sqlite import connect as sqlite_connect

__all__ = ["UNCATEGORIZED", "HubDB", "path_key"]

#: 「未分类」是旧界面给"直接在扫描根下的音色"取的名字。左栏已不造该节点，
#: 但旧前端包可能仍传它上来，折回空串查询比"点进去 0 条"好。
UNCATEGORIZED = "（未分类）"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    display TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    gender TEXT NOT NULL DEFAULT '',
    age TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    cover TEXT NOT NULL DEFAULT '',
    main_sample TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    origin_id TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    favorite INTEGER NOT NULL DEFAULT 0,
    scan_stamp TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS samples (
    voice_id TEXT NOT NULL,
    file TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    emotion TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    params TEXT NOT NULL DEFAULT '[]',
    duration REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (voice_id, file)
);
CREATE INDEX IF NOT EXISTS idx_voices_source ON voices(source, category);
-- 按扫描根筛音色时用范围比较（见 ROOT_MATCH），这条表达式索引才用得上：
-- LOWER+REPLACE 与查询里写的一模一样，规划器才认得出"是同一列"。
CREATE INDEX IF NOT EXISTS idx_voices_folder_key ON voices(LOWER(REPLACE(folder, '/', '\\')));
CREATE TABLE IF NOT EXISTS recent (
    voice_id TEXT PRIMARY KEY,
    used_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_cache (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    -- 1 = 已抢占、结果还没写回。并发同键只能有一个真正去打引擎。
    pending INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""


#: 幂等占位的存活时间（秒）：超过它还没写回结果，就认为那次请求已经死了。
#: 单条合成正常远短于 5 分钟；批次合成不走幂等。
_PENDING_TTL = 300.0

#: 旧库缺列时的补列（``language`` 等）。
#:
#: 不能只改 ``_SCHEMA``：``CREATE TABLE IF NOT EXISTS`` 对已建好的表不生效，
#: 老库会拿不到新列。这是加列不是迁移数据，默认值与"从没认出来"同形状；
#: 谁该有值由下一次导入重算。第二格是表名——两张表都会长，不能只查 ``voices``。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("voices", "language", "ALTER TABLE voices ADD COLUMN language TEXT NOT NULL DEFAULT ''"),
    ("voices", "payload", "ALTER TABLE voices ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'"),
    #: 扫描指纹：目录里文件（名 / 大小 / mtime）的摘要。空 = 老行，下次扫描重算。
    ("voices", "scan_stamp", "ALTER TABLE voices ADD COLUMN scan_stamp TEXT NOT NULL DEFAULT ''"),
    #: 平台归属（sidecar 的 ``origin.voice_id``）：本地 16 条 = 某条平台音色的样例目录。
    ("voices", "origin_id", "ALTER TABLE voices ADD COLUMN origin_id TEXT NOT NULL DEFAULT ''"),
    ("samples", "tags", "ALTER TABLE samples ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"),
    ("samples", "params", "ALTER TABLE samples ADD COLUMN params TEXT NOT NULL DEFAULT '[]'"),
    #: 幂等占位：老的记录都是"已完成"，所以默认 0。
    ("idempotency", "pending", "ALTER TABLE idempotency ADD COLUMN pending INTEGER NOT NULL DEFAULT 0"),
)


#: 「这条本地行不是一条独立的本地音色，而是某条平台音色的样例目录」。
#:
#: 归属写在目录的 ``voice.json``（``origin.voice_id``），扫描时落进 ``origin_id``。
#: 这样的行仍留在库里（它的样例、时长、指纹都还有用），但**不再单独列出来**：
#: 界面按那条平台音色显示（见 ``LibraryService._with_generated``）。平台行已经不在库里
#: （引擎卸载 / 音色删了）时它照旧当本地音色出现——那句 ``IN (SELECT id FROM voices)``
#: 就是这条退路。
_LINKED_LOCAL = (
    "NOT (source = 'local' AND origin_id != '' AND origin_id IN (SELECT id FROM voices))"
)


def _params_from_json(raw: Any) -> tuple[tuple[str, str], ...]:
    """``samples.params`` 列 → ``Sample.params`` 形状（保序键值对）；坏了当没有。"""
    try:
        pairs = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(pairs, list):
        return ()
    return tuple(
        (str(pair[0]), str(pair[1]))
        for pair in pairs
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    )


def _params_to_json(params: tuple[tuple[str, str], ...]) -> str:
    return json.dumps([[key, value] for key, value in params], ensure_ascii=False)


def _payload_from_json(raw: Any) -> dict:
    """``voices.payload`` 列 → 引擎私有凭据；坏了当没有（不让一条脏记录拖垮整页）。"""
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sample_from_row(row: sqlite3.Row) -> Sample:
    """``samples`` 一行 → :class:`Sample`。"""
    return Sample(
        file=row["file"],
        text=row["text"],
        emotion=row["emotion"],
        duration=row["duration"],
        tags=tuple(json.loads(row["tags"] or "[]")),
        params=_params_from_json(row["params"]),
    )


def _row_to_voice(row: sqlite3.Row, samples: tuple[Sample, ...] = ()) -> Voice:
    keys = row.keys()
    return Voice(
        id=row["id"],
        source=row["source"],
        name=row["name"],
        display=row["display"],
        category=row["category"],
        gender=row["gender"],
        age=row["age"],
        language=row["language"] if "language" in keys else "",
        tags=tuple(json.loads(row["tags"] or "[]")),
        description=row["description"],
        folder=row["folder"],
        cover=row["cover"],
        payload=_payload_from_json(row["payload"]) if "payload" in keys else {},
        origin_id=row["origin_id"] if "origin_id" in keys else "",
        hidden=bool(row["hidden"]),
        favorite=bool(row["favorite"]),
        main_sample=row["main_sample"],
        samples=samples,
        sample_count=(
            len(samples)
            if samples
            else (row["sample_count"] if "sample_count" in keys else 0)
        ),
    )


def path_key(value: str) -> str:
    """一条路径 → 可比较形状：分隔符统一 ``\\``、字母折小写。

    手打、粘贴、脚本写入的路径写法各异，不折形则 LIKE 匹配不上且无报错
    （左栏有数、点进去 0 条）。折形规则两边共用：SQL 见 :data:`ROOT_MATCH`，
    Python 见 :func:`app.usecase.library.LibraryService.tree`。
    只折分隔符与大小写，不解析 ``..``。
    """
    return str(value).replace("/", "\\").lower()


def _like_prefix(value: str) -> str:
    """一条分类路径 → "以它为前缀"的 LIKE 模式。

    category 用 ``/`` 连接（见 ``domain.scanner.relative_category``），不能先走
    :func:`path_key`：那会把 ``/`` 折成 ``\\``，而库里的分类仍用 ``/``，前缀永远
    匹配不上，只剩等值命中。这里只转义 LIKE 的元字符与反斜杠本身。
    """
    escaped = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return escaped + "%"


#: 折形后的目录列。查询与索引必须**逐字一致**，规划器才认得出是同一个表达式。
FOLDER_KEY = "LOWER(REPLACE(folder, '/', '\\'))"

#: 按根目录筛的匹配式：``folder_key`` 等于根，或落在「根 + 分隔符」之下。
#:
#: 用范围比较而不是 ``LIKE '根%'``：LIKE 默认大小写不敏感，SQLite 不敢用索引，
#: 只能全表扫；而且 ``%`` / ``_`` 得逐个转义，漏一个就静默多匹配。范围写法
#: 既走索引，也不再需要转义（比较是逐字节的，没有通配符概念）。
ROOT_MATCH = f"({FOLDER_KEY} = ? OR ({FOLDER_KEY} >= ? AND {FOLDER_KEY} < ?))"

#: 范围比较的上界尾巴：比任何字符都大的那个码位（UTF-8 里也排最后）。
#: 「根\\」→「根\\\U0010ffff」正好圈住这个根下的全部路径。
_RANGE_TAIL = "\U0010ffff"


def _root_range(path: str) -> tuple[str, str, str]:
    """一条扫描根 → :data:`ROOT_MATCH` 的三个参数（根本身 / 下界 / 上界）。"""
    base = path_key(path).rstrip("\\")
    lower = base + "\\"
    return base, lower, lower + _RANGE_TAIL


#: 「这条是不是设计音色」的判据：``payload`` 里有一句非空的 ``design_prompt``。
#:
#: 认 payload 而不是 ``category = '设计音色'``：分类是用户能在详情里改掉的一格，
#: 改完它就掉出这一组；``source`` 也不可靠——设计音色现在哪台引擎都能用，
#: 哪台建的只是历史，不是身份。
DESIGN_MATCH = "TRIM(COALESCE(json_extract(payload, '$.design_prompt'), '')) != ''"


def _filters(
    *,
    source: str = "",
    category: str = "",
    root: str = "",
    gender: str = "",
    age: str = "",
    language: str = "",
    tag: str = "",
    q: str = "",
    design: bool = False,
) -> tuple[str, list[Any]]:
    """筛选条件 → ``(SQL 片段, 参数)``。列表与标签云共用，避免两边 SQL 漂移。"""
    sql = ""
    args: list[Any] = []
    if design:
        sql += f" AND {DESIGN_MATCH}"
    if source:
        sql += " AND source = ?"
        args.append(source)
        # 设计音色只归「设计音色」那一组：它不绑引擎，`source` 只是"哪台建的"。
        # 不排掉的话，点 MiMo 会连它建的设计音色一起列出来，左栏计数也跟着虚高。
        if source != SOURCE_LOCAL and not design:
            sql += f" AND NOT ({DESIGN_MATCH})"
    if category:
        if category == UNCATEGORIZED:
            # 旧前端的「（未分类）」= 库里的空串（见 UNCATEGORIZED）。
            sql += " AND category = ''"
        else:
            # 前缀匹配：category 是相对根的整条路径，尾缀带 ``/`` 避免误吞同名前缀目录。
            sql += " AND (category = ? OR category LIKE ? ESCAPE '\\')"
            args.append(category)
            args.append(_like_prefix(category + "/"))
    if root:
        # 前缀匹配而非另记一张归属表；两边都折形（见 path_key）。
        sql += f" AND {ROOT_MATCH}"
        args.extend(_root_range(root))
    if gender:
        sql += " AND gender = ?"
        args.append(gender)
    if age:
        sql += " AND age = ?"
        args.append(age)
    if language:
        sql += " AND language = ?"
        args.append(language)
    if tag:
        sql += " AND tags LIKE ?"
        args.append(f'%"{tag}"%')
    if q:
        sql += " AND (name LIKE ? OR display LIKE ? OR description LIKE ?)"
        like = f"%{q}%"
        args.extend([like, like, like])
    # 平台音色的样例目录不单独列：它按那条平台音色出现（列表与标签云同一口径）。
    sql += f" AND {_LINKED_LOCAL}"
    return sql, args


class HubDB:
    """``hub.db`` 的唯一入口。线程安全靠**每操作一连接**（SQLite 的 WAL 够本机用）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connect() as conn:
            # WAL 是**库级**的持久设置：建库时设一次就够。每条连接都执行它是写型
            # pragma，频繁调用会在多线程下多一次无谓的锁竞争。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            #: 刚补上「平台归属」那一列：老行的值只有重读 sidecar 才填得上，
            #: 调用方据此在启动后跑一次重扫（见 ``create_app``）。
            self.rebuild_needed = self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> bool:
        """给已经存在的表补上后来才加的列（见 :data:`_MIGRATIONS`）。

        返回"索引需要重建一次"。``origin_id`` 是新加的那一列：它的值只能从
        每个文件夹的 sidecar 读出来，而增量扫描会整条复用指纹对得上的旧行——
        不清掉指纹，老库永远拿不到归属，那 16 个平台音色就一直"没有可以播的"。
        """
        added: set[str] = set()
        for table, column, ddl in _MIGRATIONS:
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(ddl)
                added.add(column)
        if "origin_id" not in added:
            return False
        conn.execute("UPDATE voices SET scan_stamp = '' WHERE source = ?", (SOURCE_LOCAL,))
        return True

    def _connect(self) -> sqlite3.Connection:
        # 每个操作一条连接、各自读写：撞锁时等一会儿，别立刻抛 "database is locked"。
        # ``with`` 结束即关闭（见 app.infra.sqlite）。
        return sqlite_connect(self._path, busy_ms=3000)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def replace_local(self, voices: list[Voice]) -> None:
        """重扫后整批重写本地行（**平台行不动**；偏好按 id 留下来）。

        保留给"不知道哪些变过"的调用方；扫描走 :meth:`sync_local`——它跳过没动过的行，
        重复扫描只碰变了的那些（目录里几百条音色、几千个样例时差别是几十倍写入量）。
        """
        self.sync_local(voices)

    def sync_local(
        self,
        voices: list[Voice],
        *,
        unchanged: frozenset[str] | set[str] = frozenset(),
        stamps: Mapping[str, str] | None = None,
    ) -> None:
        """增量重写本地行：变了的 upsert、没了的删掉、没动过的**一个字节都不写**。

        :param unchanged: 指纹对得上、整条复用的音色 id（它们的行原样留着）。
        :param stamps: 本次扫出的指纹（``id → stamp``），落库供下次比对。
        """
        stamps = stamps or {}
        keep = {voice.id for voice in voices}
        with self._connect() as conn:
            gone = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM voices WHERE source = ?", (SOURCE_LOCAL,)
                )
                if row["id"] not in keep
            ]
            for voice_id in gone:
                conn.execute("DELETE FROM samples WHERE voice_id = ?", (voice_id,))
                conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
            for voice in voices:
                if voice.id in unchanged:
                    continue
                self._write_voice(conn, voice, stamp=stamps.get(voice.id, ""))

    def _write_voice(self, conn: sqlite3.Connection, voice: Voice, *, stamp: str) -> None:
        """写一条本地音色：行 upsert（``hidden``/``favorite`` 不在 SET 里，偏好得以保留）、
        样例整条重写、指纹一起落盘。"""
        conn.execute(
            "INSERT INTO voices (id, source, name, display, category, gender, age,"
            " language, tags, description, folder, cover, main_sample, payload, origin_id,"
            " hidden, favorite, scan_stamp, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET source=excluded.source, name=excluded.name,"
            " display=excluded.display, category=excluded.category, gender=excluded.gender,"
            " age=excluded.age, language=excluded.language, tags=excluded.tags,"
            " description=excluded.description, folder=excluded.folder, cover=excluded.cover,"
            " main_sample=excluded.main_sample, payload=excluded.payload,"
            " origin_id=excluded.origin_id, scan_stamp=excluded.scan_stamp,"
            " updated_at=excluded.updated_at",
            (
                voice.id,
                voice.source,
                voice.name,
                voice.display,
                voice.category,
                voice.gender,
                voice.age,
                voice.language,
                json.dumps(list(voice.tags), ensure_ascii=False),
                voice.description,
                voice.folder,
                voice.cover,
                voice.main_sample,
                json.dumps(dict(voice.payload or {}), ensure_ascii=False),
                voice.origin_id,
                int(voice.hidden),
                int(voice.favorite),
                stamp,
                time.time(),
            ),
        )
        conn.execute("DELETE FROM samples WHERE voice_id = ?", (voice.id,))
        conn.executemany(
            "INSERT INTO samples (voice_id, file, text, emotion, tags, params, duration)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (
                    voice.id,
                    s.file,
                    s.text,
                    s.emotion,
                    json.dumps(list(s.tags), ensure_ascii=False),
                    _params_to_json(s.params),
                    s.duration,
                )
                for s in voice.samples
            ],
        )

    def local_stamps(self) -> dict[str, str]:
        """本机音色上次扫描留下的目录指纹（``id → stamp``）。"""
        with self._connect() as conn:
            return {
                row["id"]: row["scan_stamp"] or ""
                for row in conn.execute(
                    "SELECT id, scan_stamp FROM voices WHERE source = ?", (SOURCE_LOCAL,)
                )
            }

    def local_voices(self) -> dict[str, Voice]:
        """本机音色连同样例（``id → Voice``）——扫描时比对指纹、命中就整条复用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM voices WHERE source = ?", (SOURCE_LOCAL,)
            ).fetchall()
            samples: dict[str, list[Sample]] = {}
            for row in conn.execute(
                "SELECT s.* FROM samples s JOIN voices v ON v.id = s.voice_id"
                " WHERE v.source = ? ORDER BY s.file",
                (SOURCE_LOCAL,),
            ):
                samples.setdefault(row["voice_id"], []).append(_sample_from_row(row))
        return {
            row["id"]: _row_to_voice(row, tuple(samples.get(row["id"], ())))
            for row in rows
        }

    def remember_durations(self, entries: Iterable[tuple[str, int, float, float]]) -> None:
        """把这次新量出来的时长写回缓存（``(路径, 大小, mtime, 时长)``），一次事务写完。"""
        rows = list(entries)
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO probe_cache (path, size, mtime, duration) VALUES (?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
                " duration=excluded.duration",
                rows,
            )

    def upsert_voice(self, voice: Voice, *, with_samples: bool = True) -> None:
        """插/更一条（编辑元数据、平台音色登记都走它）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO voices (id, source, name, display, category, gender, age, language,"
                " tags, description, folder, cover, main_sample, payload, origin_id, hidden,"
                " favorite, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET source=excluded.source, name=excluded.name,"
                " display=excluded.display, category=excluded.category, gender=excluded.gender,"
                " age=excluded.age, language=excluded.language, tags=excluded.tags,"
                " description=excluded.description, folder=excluded.folder,"
                " cover=excluded.cover, main_sample=excluded.main_sample,"
                " payload=excluded.payload, origin_id=excluded.origin_id,"
                " updated_at=excluded.updated_at",
                (
                    voice.id,
                    voice.source,
                    voice.name,
                    voice.display,
                    voice.category,
                    voice.gender,
                    voice.age,
                    voice.language,
                    json.dumps(list(voice.tags), ensure_ascii=False),
                    voice.description,
                    voice.folder,
                    voice.cover,
                    voice.main_sample,
                    json.dumps(dict(voice.payload or {}), ensure_ascii=False),
                    voice.origin_id,
                    int(voice.hidden),
                    int(voice.favorite),
                    time.time(),
                ),
            )
            if with_samples:
                conn.execute("DELETE FROM samples WHERE voice_id = ?", (voice.id,))
                conn.executemany(
                    "INSERT INTO samples (voice_id, file, text, emotion, tags, params, duration)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            voice.id,
                            s.file,
                            s.text,
                            s.emotion,
                            json.dumps(list(s.tags), ensure_ascii=False),
                            _params_to_json(s.params),
                            s.duration,
                        )
                        for s in voice.samples
                    ],
                )

    def set_prefs(self, voice_id: str, *, hidden: bool | None, favorite: bool | None) -> None:
        """改服务级偏好，只动传入的两个字段。"""
        sets, args = [], []
        if hidden is not None:
            sets.append("hidden = ?")
            args.append(int(hidden))
        if favorite is not None:
            sets.append("favorite = ?")
            args.append(int(favorite))
        if not sets:
            return
        args.append(voice_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE voices SET {', '.join(sets)} WHERE id = ?", args)

    def delete_voice(self, voice_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM samples WHERE voice_id = ?", (voice_id,))
            conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))

    def touch_recent(self, voice_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO recent (voice_id, used_at) VALUES (?, ?)"
                " ON CONFLICT(voice_id) DO UPDATE SET used_at = excluded.used_at",
                (voice_id, time.time()),
            )

    # ------------------------------------------------------------------ #
    # 幂等键
    # ------------------------------------------------------------------ #

    def idempotent_begin(self, key: str, fingerprint: str, *, ttl: float) -> tuple[str, dict[str, Any] | None]:
        """抢占一个幂等键，返回 ``(状态, 已存结果)``。

        状态取值：

        - ``"new"``：抢到了，调用方去真正干活，干完用 :meth:`idempotent_put` 写回；
        - ``"replay"``：已有完成的结果，直接重放（不再次调用引擎）；
        - ``"pending"``：同一个键的另一次请求正在处理中——**不要**再打一次引擎，
          否则按量计费的引擎会双倍扣费；
        - ``"conflict"``：键相同但请求内容不同，调用方翻译成 409。

        整个判定在一条写事务里完成：``INSERT ... ON CONFLICT DO NOTHING`` 只有一个
        赢家，SQLite 的写锁把并发的第二个请求挡在外面。
        """
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM idempotency WHERE created_at < ?", (now - ttl,))
            # 占位不能用整整 24 小时的 TTL：进程硬崩时没人来 abandon，
            # 这个键会被"处理中"锁到过期。超过 _PENDING_TTL 视为那次请求已经死了。
            conn.execute(
                "DELETE FROM idempotency WHERE pending = 1 AND created_at < ?",
                (now - _PENDING_TTL,),
            )
            inserted = conn.execute(
                "INSERT INTO idempotency(key, fingerprint, response, pending, created_at)"
                " VALUES(?, ?, '', 1, ?) ON CONFLICT(key) DO NOTHING",
                (key, fingerprint, now),
            )
            if inserted.rowcount == 1:
                return "new", None
            row = conn.execute(
                "SELECT fingerprint, response, pending FROM idempotency WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                # 刚好被另一条连接的过期清理删掉：让调用方重试一次即可。
                return "new", None
            if str(row["fingerprint"]) != fingerprint:
                return "conflict", None
            if int(row["pending"] or 0):
                return "pending", None
            return "replay", json.loads(row["response"])

    def idempotent_abandon(self, key: str, fingerprint: str) -> None:
        """放弃占位（引擎报错、归档失败、请求被取消）：键可以立刻重试。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE key = ? AND fingerprint = ? AND pending = 1",
                (key, fingerprint),
            )

    def idempotent_put(
        self, key: str, fingerprint: str, response: dict[str, Any], *, ttl: float
    ) -> None:
        """登记一次成功结果（占位转正），并清理已过期的记录。"""
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM idempotency WHERE created_at < ?", (now - ttl,))
            conn.execute(
                "INSERT OR REPLACE INTO idempotency"
                " (key, fingerprint, response, pending, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (key, fingerprint, json.dumps(response, ensure_ascii=False), now),
            )

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def duration_cache(self) -> dict[str, tuple[int, float, float]]:
        """整张「路径 → (大小, mtime, 时长)」（扫描开始前读一次）。

        读全表而非按根筛：行小、一次读完更省，同一文件被两个根覆盖时一并命中。
        ``size = 0`` 的行不返回——那是没有指纹的历史行，留着只会比不中。
        """
        with self._connect() as conn:
            return {
                row["path"]: (row["size"], row["mtime"], row["duration"])
                for row in conn.execute(
                    "SELECT path, size, mtime, duration FROM probe_cache WHERE size > 0"
                )
            }

    def get_voice(self, voice_id: str, *, with_samples: bool = True) -> Voice | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM voices WHERE id = ?", (voice_id,)).fetchone()
            if row is None:
                return None
            samples: tuple[Sample, ...] = ()
            if with_samples:
                samples = tuple(
                    Sample(
                        file=r["file"],
                        text=r["text"],
                        emotion=r["emotion"],
                        duration=r["duration"],
                        tags=tuple(json.loads(r["tags"] or "[]")),
                        params=_params_from_json(r["params"]),
                    )
                    for r in conn.execute(
                        "SELECT * FROM samples WHERE voice_id = ? ORDER BY file", (voice_id,)
                    )
                )
            return _row_to_voice(row, samples)

    def list_voices(
        self,
        *,
        source: str = "",
        category: str = "",
        root: str = "",
        gender: str = "",
        age: str = "",
        language: str = "",
        tag: str = "",
        q: str = "",
        design: bool = False,
        limit: int = 0,
        offset: int = 0,
    ) -> list[Voice]:
        """列表查询（不带 samples，详情接口才给）。

        :param limit: 取几条；``0`` = 不分页（一次全给，界面首屏就是这个用法）。
        :param offset: 跳过几条，仅 ``limit > 0`` 时有意义。

        样例数走 ``LEFT JOIN`` 一次聚合：原来是每行跑一次相关子查询
        （``SELECT COUNT(*) ... WHERE voice_id = voices.id``），行数越多越亏。
        """
        where, args = _filters(
            source=source, category=category, root=root, gender=gender,
            age=age, language=language, tag=tag, q=q, design=design,
        )
        sql = (
            "SELECT voices.*, COALESCE(agg.n, 0) AS sample_count FROM voices"
            " LEFT JOIN (SELECT voice_id, COUNT(*) AS n FROM samples GROUP BY voice_id) AS agg"
            " ON agg.voice_id = voices.id WHERE 1=1" + where
            + " ORDER BY favorite DESC, source, category, name"
        )
        if limit > 0:
            sql += " LIMIT ? OFFSET ?"
            args = [*args, limit, max(offset, 0)]
        with self._connect() as conn:
            return [_row_to_voice(row) for row in conn.execute(sql, args)]

    def samples_without_text(self, limit: int = 0) -> list[tuple[str, str, str]]:
        """还没有逐字稿的样例：``(voice_id, file, folder)``，按音色、文件名排。

        识别「连接时自动补逐字稿」用。只取有文件夹的行——文件在哪由服务层按
        ``folder/file`` 拼，拼出来不存在的那些在那边剔掉（库里的记录可能比磁盘旧）。

        :param limit: >0 时最多取这么多（一轮补一批，不必一次扫完整库）。
        """
        sql = (
            "SELECT s.voice_id, s.file, v.folder FROM samples s"
            " JOIN voices v ON v.id = s.voice_id"
            " WHERE TRIM(COALESCE(s.text, '')) = '' AND COALESCE(v.folder, '') != ''"
            " ORDER BY v.source, v.name, s.file"
        )
        args: list[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._connect() as conn:
            return [
                (row["voice_id"], row["file"], row["folder"])
                for row in conn.execute(sql, args)
            ]

    def local_folders(self) -> list[tuple[str, str]]:
        """本地音色的 ``(folder, category)``——左栏按根分组的原料。

        不取整条 ``Voice``：左栏只要这两个值，而构造 Voice 要解两段 JSON、
        算样例数，是这条链上最贵的一段。列表查询仍走 :meth:`list_voices`。

        平台音色的样例目录不算（见 :data:`_LINKED_LOCAL`）：它在左栏由那条平台音色
        代表，否则同一个文件夹会在「扫描目录」里再出现一次，名字还是平台音色 id。
        """
        with self._connect() as conn:
            return [
                (row["folder"], row["category"])
                for row in conn.execute(
                    f"SELECT folder, category FROM voices WHERE source = ? AND {_LINKED_LOCAL}",
                    (SOURCE_LOCAL,),
                )
            ]

    def linked_locals(self) -> dict[str, Voice]:
        """``origin_id → Voice``：扫描根里那些"某条平台音色的样例目录"。

        只给 :meth:`LibraryService._with_generated` 用：平台音色的样例不止在引擎
        保存目录里，也可能就放在用户的音色库里（生成时落进去的）。平台行已经不在
        库里的不算——那时它就是一条普通本地音色，继续留在扫描目录。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM voices WHERE source = ? AND origin_id != ''"
                " AND origin_id IN (SELECT id FROM voices)",
                (SOURCE_LOCAL,),
            ).fetchall()
            if not rows:
                return {}
            samples: dict[str, list[Sample]] = {}
            marks = ",".join("?" * len(rows))
            for row in conn.execute(
                f"SELECT * FROM samples WHERE voice_id IN ({marks}) ORDER BY file",
                tuple(row["id"] for row in rows),
            ):
                samples.setdefault(row["voice_id"], []).append(_sample_from_row(row))
            return {
                row["origin_id"]: _row_to_voice(row, tuple(samples.get(row["id"], ())))
                for row in rows
            }

    def voice_ids(self) -> set[str]:
        """库里全部音色 id。一次查完，供「这条边栏音色还在不在」批量判断。"""
        with self._connect() as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM voices")}

    def tag_cloud(
        self,
        *,
        source: str = "",
        category: str = "",
        root: str = "",
        gender: str = "",
        age: str = "",
        language: str = "",
        q: str = "",
        design: bool = False,
    ) -> list[tuple[str, int]]:
        """当前筛选条件下各标签还剩几条（``tag`` 自身不参与筛选，便于用户改选标签）。

        与 :meth:`list_voices` 唯一不同是不带 ``tag`` 条件；计数在 SQL 一次 ``GROUP BY``。
        """
        where, args = _filters(
            source=source, category=category, root=root, gender=gender,
            age=age, language=language, q=q, design=design,
        )
        sql = (
            "SELECT j.value AS tag, COUNT(*) AS n FROM voices, json_each(voices.tags) AS j"
            " WHERE 1=1" + where + " GROUP BY j.value ORDER BY n DESC, j.value"
        )
        with self._connect() as conn:
            return [(row["tag"], row["n"]) for row in conn.execute(sql, args)]

    def tree(self) -> dict[str, Any]:
        """左栏数据：本地按「根目录 → 分类」聚合，平台按平台名聚合（**只给计数**）。"""
        with self._connect() as conn:
            local_rows = conn.execute(
                f"SELECT folder, category, COUNT(*) AS n FROM voices WHERE source = ?"
                f" AND {_LINKED_LOCAL} GROUP BY category, folder",
                (SOURCE_LOCAL,),
            ).fetchall()
            platform_rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM voices WHERE source != ?"
                f" AND NOT ({DESIGN_MATCH}) GROUP BY source",
                (SOURCE_LOCAL,),
            ).fetchall()
            # 设计音色：跨引擎的一组，不能只按 source 分——它们哪台建的都有。
            designed = conn.execute(
                f"SELECT COUNT(*) AS n FROM voices WHERE source != ? AND {DESIGN_MATCH}",
                (SOURCE_LOCAL,),
            ).fetchone()
        # 根目录从 folder 反推：根归属由调用方按 config.folders 匹配，这里只给 category 计数。
        categories: dict[str, int] = {}
        for row in local_rows:
            key = row["category"] or ""
            categories[key] = categories.get(key, 0) + row["n"]
        return {
            "local_categories": dict(sorted(categories.items())),
            "platforms": dict(sorted((row["source"], row["n"]) for row in platform_rows)),
            "design": int(designed["n"]) if designed else 0,
        }
