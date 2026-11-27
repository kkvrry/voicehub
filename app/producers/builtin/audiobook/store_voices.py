"""有声书项目库：角色统计、音色映射与失效标记操作。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.producers.builtin.audiobook.store_helpers import (
    _now,
    _voice_tuple,
)


class _VoicesStoreMixin:
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

