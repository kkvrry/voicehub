"""有声书项目库：行表、批量编辑与回读质检操作。"""

from __future__ import annotations

import json
from typing import Any

from app.producers.builtin.audiobook.store_helpers import (
    _new_id,
    _now,
    _params_dict,
    _params_json,
    _stamp,
)

#: 行级占位的有效期：一次单行合成不可能超过它。进程崩了，占位自己过期。
_ROW_CLAIM_S = 900


class _RowsStoreMixin:
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
        """单行失败：保留原因与旧音频，只写错误与 failed，任务继续跑其余行。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE rows SET error = ?, status = 'failed' WHERE id = ?",
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


    def qc_map(self, project_id: str) -> dict[str, dict[str, Any]]:
        rows = self._query("SELECT * FROM row_qc WHERE project_id = ?", (project_id,))
        return {row["row_id"]: dict(row) for row in rows}


    def qc_put(self, project_id: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        now = _now()
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO row_qc(row_id, project_id, audio_key, verdict, score, detail,"
                " engine, checked_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(row_id) DO UPDATE SET project_id = excluded.project_id,"
                " audio_key = excluded.audio_key, verdict = excluded.verdict,"
                " score = excluded.score, detail = excluded.detail,"
                " engine = excluded.engine, checked_at = excluded.checked_at",
                [
                    (
                        item["row_id"], project_id, item.get("audio_key", ""),
                        item["verdict"], float(item.get("score", 0.0)),
                        json.dumps(item.get("detail") or {}, ensure_ascii=False),
                        item.get("engine", ""), now,
                    )
                    for item in items
                ],
            )


    def qc_prune(self, project_id: str, alive: set[str]) -> None:
        """删掉已经不存在的行留下的结论：重新分章之后旧结论没有宿主。"""
        rows = self._query("SELECT row_id FROM row_qc WHERE project_id = ?", (project_id,))
        dead = [(row["row_id"],) for row in rows if row["row_id"] not in alive]
        if dead:
            with self._conn() as conn:
                conn.executemany("DELETE FROM row_qc WHERE row_id = ?", dead)


    def qc_clear(self, project_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM row_qc WHERE project_id = ?", (project_id,))


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

