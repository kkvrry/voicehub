"""文本阶段：分章后的所有文字工作——规则替换、拆句、行编辑、说话人合并。

这一段只读写「章节正文 + 行」，不碰音频与成品。修改一行文本、调整拆句选项、
合并角色都走这里；音频是否过期由生成阶段按指纹判断，本模块只负责把行改对。
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from app.domain.request_spec import GROUPS, SPEC_FIELDS
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.patterns import (
    count_chars,
    preprocess,
)
from app.producers.builtin.audiobook.rows import (
    ROW_PARAM_FIELDS,
    ROW_PARAM_GROUPS,
    clean_row_params,
    merge_split_reports,
    plan_auto_merge,
    rewrite_row_texts,
    row_params,
    split_point,
)
from app.producers.builtin.audiobook.rules import (
    DEFAULT_QUOTE,
    NARRATOR,
    QUOTE_STYLES,
    SENTENCE_MARKS,
    SPLIT_MODE_NAMES,
    SPLIT_MODES,
    SplitOptions,
    check_split,
    split_rows,
)

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

__all__ = ["TextStage"]


class TextStage:
    """文本阶段的实现；门面（``AudiobookService``）只负责转调。"""

    def __init__(self, service: AudiobookService) -> None:
        self.svc = service

    @property
    def store(self) -> Any:
        return self.svc.store

    @property
    def settings(self) -> Any:
        return self.svc.settings

    @staticmethod
    def _text_rules(body: dict[str, Any]) -> dict[str, Any]:
        custom = body.get("custom_dialog") or {}
        pattern = str(custom.get("pattern") or "").strip()
        return {
            "dialog": [str(name) for name in body.get("dialog") or []],
            "bracket": [str(name) for name in body.get("bracket") or []],
            "custom_dialog": (pattern, str(custom.get("replacement") or "")) if pattern else None,
            "custom_bracket": str(body.get("custom_bracket") or "").strip(),
        }

    def _text_scopes(
        self, project_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        """文本处理改谁：有章节改各章**当前显示的对象**。

        已拆句的章逐行改（行边界与角色都保留，不要求重新拆句）；没拆句的章
        改正文；没章节时改源文。
        """
        chapters = self.svc.store.chapters(project_id)
        if chapters:
            # 一次取全项目行再分组：逐章查是章数次连接，替换一遍正文要等上近一秒。
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in self.svc.store.all_rows(project_id):
                grouped.setdefault(str(row["chapter_id"]), []).append(row)
            scopes: list[dict[str, Any]] = []
            for item in chapters:
                rows = grouped.get(str(item["id"]), []) if int(item["rows"] or 0) else []
                scopes.append(
                    {
                        "id": item["id"],
                        "label": item["title"],
                        "text": item["text"],
                        "rows": [
                            {
                                "id": row["id"],
                                "ordinal": int(row["ordinal"]),
                                "text": row["text"],
                            }
                            for row in rows
                        ],
                    }
                )
            return scopes, "chapters"
        project = self.svc._require_project(project_id)
        return (
            [
                {
                    "id": "",
                    "label": "全文",
                    "text": str(project.get("source") or ""),
                    "rows": [],
                }
            ],
            "source",
        )

    @staticmethod
    def _text_units(scope: dict[str, Any]) -> list[dict[str, Any]]:
        """一章的处理单位：拆过句就是每一行，没拆过就是整章正文。"""
        rows = scope.get("rows") or []
        if rows:
            return [
                {
                    "prefix": f"{scope['id']}|{row['id']}|",
                    "text": row["text"],
                    "line": int(row["ordinal"]) + 1,
                    "row_id": row["id"],
                }
                for row in rows
            ]
        prefix = f"{scope['id']}|" if scope["id"] else ""
        return [{"prefix": prefix, "text": scope["text"], "line": 0, "row_id": ""}]

    def preview_text(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """试跑文本处理：给出每一处改动与它在段落里的位置，不落库。

        逐章跑；已拆句的章逐行跑（key 带章节 + 行作用域）。界面拿
        ``context_at`` / ``context_len`` 画高亮；取消勾选的 ``exclude`` 原样传回。
        """
        scopes, mode = self._text_scopes(project_id)
        exclude = {str(item) for item in body.get("exclude") or []}
        rules = self._text_rules(body)
        hits: list[dict[str, Any]] = []
        before_chars = 0
        after_chars = 0
        changed = False
        for scope in scopes:
            remaining = 50
            for unit in self._text_units(scope):
                prefix = unit["prefix"]
                local = {
                    key.removeprefix(prefix) for key in exclude if key.startswith(prefix)
                }
                result = preprocess(
                    unit["text"], **rules, exclude=local, limit=max(0, remaining)
                )
                remaining -= len(result.hits)
                before_chars += count_chars(unit["text"])
                after_chars += count_chars(result.text)
                changed = changed or result.text != unit["text"]
                for hit in result.hits:
                    hits.append(
                        {
                            "key": prefix + hit.key,
                            "chapter": scope["label"] if scope["id"] else "",
                            "line": unit["line"],
                            "kind": hit.kind,
                            "rule": hit.rule,
                            "before": hit.before,
                            "after": hit.after,
                            "context": hit.context,
                            "context_at": hit.context_at,
                            "context_len": hit.context_len,
                        }
                    )
        return {
            "hits": hits,
            "changed": changed,
            "scope": mode,
            "chars_before": before_chars,
            "chars_after": after_chars,
            "applied": sum(1 for hit in hits if hit["key"] not in exclude),
        }

    def apply_text(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """应用文本处理：作用于每章当前显示的对象，不强制重新拆句。

        已拆句的章逐行改文本并写回章节正文，行、角色、未受影响行的音频都保留；
        只作废改动行的旧音频与本章成品。没拆句的章仍改正文。
        """
        self.svc._ensure_idle(project_id)
        scopes, mode = self._text_scopes(project_id)
        exclude = {str(item) for item in body.get("exclude") or []}
        rules = self._text_rules(body)
        changed: list[tuple[dict[str, Any], str, int]] = []
        updated_rows = 0
        for scope in scopes:
            if scope.get("rows"):
                before = [row["text"] for row in scope["rows"]]
                after = list(before)
                applied = 0
                for index, unit in enumerate(self._text_units(scope)):
                    prefix = unit["prefix"]
                    local = {
                        key.removeprefix(prefix)
                        for key in exclude
                        if key.startswith(prefix)
                    }
                    result = preprocess(unit["text"], **rules, exclude=local)
                    if result.text == unit["text"]:
                        continue
                    after[index] = result.text
                    applied += sum(1 for hit in result.hits if hit.key not in local)
                if after == before:
                    continue
                rewritten = rewrite_row_texts(scope["text"], before, after)
                if rewritten is None:
                    CHANNEL.warning(
                        f"文本处理写回正文时定位失败（{scope['label']}），正文未改",
                        source="audiobook",
                        project=project_id,
                        chapter=scope["id"],
                    )
                elif rewritten != scope["text"]:
                    self.svc.store.update_chapter_text(scope["id"], rewritten)
                updates = [
                    (row["id"], after[index])
                    for index, row in enumerate(scope["rows"])
                    if after[index] != row["text"]
                ]
                updated_rows += self.svc.store.apply_row_texts(scope["id"], updates)
                changed.append(
                    (scope, scope["text"] if rewritten is None else rewritten, applied)
                )
                continue

            prefix = f"{scope['id']}|" if scope["id"] else ""
            local = {key.removeprefix(prefix) for key in exclude if key.startswith(prefix)}
            result = preprocess(scope["text"], **rules, exclude=local)
            if result.text == scope["text"]:
                continue
            applied = sum(1 for hit in result.hits if hit.key not in local)
            self.svc.store.set_chapter_text(scope["id"], result.text)
            self.svc.store.replace_rows(scope["id"], [])
            audio = self.svc.project_folder(project_id) / "audio" / scope["id"]
            if audio.is_dir():
                shutil.rmtree(audio, ignore_errors=True)
            changed.append((scope, result.text, applied))

        if not changed:
            raise ProducerError("这些规则没有改动任何文字。")

        total = sum(item[2] for item in changed)
        if mode == "source":
            project = self.svc._require_project(project_id)
            self.svc.store.set_project_source(project_id, changed[0][1])
            self.svc.store.replace_chapters(project_id, [])
            self.svc._clear_derived(project_id)
            self.svc._write_source_file(project_id, str(project["name"]), changed[0][1])
            note = "章节已作废，请重新分章"
        else:
            self.svc._rewrite_chapter_files(project_id)
            note = (
                f"更新 {updated_rows} 行，行结构保持不变"
                if updated_rows
                else f"{len(changed)} 章正文已更新"
            )
        CHANNEL.info(
            f"文本处理完成：改动 {total} 处，{note}",
            source="audiobook",
            project=project_id,
        )
        return {
            **self.svc.detail(project_id),
            "changed_hits": total,
            "scope": mode,
            "updated_rows": updated_rows,
            "updated_chapters": len(changed),
        }

    def clear_chapter_rows(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        """清掉一章的行与音频、保留正文：想整章改字时先走这一步（行会作废）。"""
        self.svc._ensure_idle(project_id)
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        count = len(self.svc.store.rows(chapter_id))
        self.svc.store.replace_rows(chapter_id, [])
        self.svc.store.set_chapter_stage(chapter_id, "chapters")
        audio = self.svc.project_folder(project_id) / "audio" / chapter_id
        if audio.is_dir():
            shutil.rmtree(audio, ignore_errors=True)
        CHANNEL.info(
            f"清空章节行：{chapter['title']} · {count} 行，正文保留",
            source="audiobook",
            project=project_id,
            chapter=chapter_id,
        )
        return self.svc.detail(project_id)

    def save_chapter_text(
        self, project_id: str, chapter_id: str, text: str
    ) -> dict[str, Any]:
        """手改一章正文：落库、作废该章的行与音频；内容没变就当无事发生。"""
        self.svc._ensure_idle(project_id)
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        incoming = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        current = str(chapter["text"] or "").replace("\r\n", "\n").replace("\r", "\n")
        if incoming == current:
            return {**self.svc.detail(project_id), "changed": False}
        self.svc.store.set_chapter_text(chapter_id, incoming)
        self.svc.store.replace_rows(chapter_id, [])
        audio = self.svc.project_folder(project_id) / "audio" / chapter_id
        if audio.is_dir():
            shutil.rmtree(audio, ignore_errors=True)
        live = self.svc.store.chapter(project_id, chapter_id)
        if live:
            self.svc._write_chapter_file(project_id, live)
        CHANNEL.info(
            f"章节正文已手改：{chapter['title']} · {len(incoming)} 字，需要重新拆句",
            source="audiobook",
            project=project_id,
            chapter=chapter_id,
        )
        return {**self.svc.detail(project_id), "changed": True}

    def edit_row(
        self,
        project_id: str,
        row_id: str,
        *,
        speaker: str | None = None,
        emotion: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """手改一行；改文本时按顺序写回章节正文（正文是台词的产地）。"""
        row = self.svc.store.row(row_id)
        if not row or not self.svc.store.chapter(project_id, row["chapter_id"]):
            raise ProducerError("这一行不属于该项目。")
        if text is not None and text != row["text"]:
            chapter = self.svc.store.chapter(project_id, row["chapter_id"])
            rows = self.svc.store.rows(row["chapter_id"])
            before = [item["text"] for item in rows]
            after = list(before)
            after[next(i for i, item in enumerate(rows) if item["id"] == row_id)] = text
            rewritten = rewrite_row_texts(str(chapter["text"] or ""), before, after)
            if rewritten is None:
                CHANNEL.warning(
                    f"行文本写回正文时定位失败（{chapter['title']}），正文未改",
                    source="audiobook",
                    project=project_id,
                    chapter=row["chapter_id"],
                )
            else:
                self.svc.store.set_chapter_text(row["chapter_id"], rewritten)
                live = self.svc.store.chapter(project_id, row["chapter_id"])
                if live:
                    self.svc._write_chapter_file(project_id, live)
        self.svc.store.update_row(row_id, speaker=speaker, emotion=emotion, text=text)
        # 改回去的行当场复活：接口这次就把「已生成」还给前端，不必等下一次读。
        self.svc._settle_stale(project_id, str(row["chapter_id"]))
        return self.svc._row_view(project_id, self.svc.store.row(row_id) or {})

    def row_params(self, project_id: str, row_id: str) -> dict[str, Any]:
        """这一行能调哪些参数：规范里「合成参数 / 静默」两组全列，标出这台引擎支不支持。"""
        from app.usecase import engines as engines_uc

        row, _ = self._row_of(project_id, row_id)
        speaker = row["speaker"] or NARRATOR
        mapping = self.svc.store.voice_map(project_id).get(speaker) or {}
        engine_key = str(mapping.get("engine") or "")
        # 按**插件**取，不按已启用：这一行已经绑在这台引擎上，这里回答的是"它能调
        # 什么"，不是"现在能不能跑"。停用后仍要看得见它的参数面——换成已启用清单，
        # 停用那台的行会被当成"还没分配引擎"，把它不支持的参数全画成支持。
        plugin = engines_uc.list_engines().get(engine_key) if engine_key else None
        sources: set[str] = set()
        unsupported: dict[str, str] = {}
        if plugin is not None:
            sources = {item.source for item in plugin.mapping().params}
            sources.update(plugin.dynamic_mappings())
            unsupported = plugin.unsupported()
        values = row_params(row)
        fields = []
        for field in SPEC_FIELDS:
            if field.key not in ROW_PARAM_FIELDS:
                continue
            # 还没分配音色时不知道是哪台引擎：先照全量给，别把设置入口堵死。
            supported = plugin is None or (
                field.key in sources and field.key not in unsupported
            )
            fields.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "group": field.group,
                    "widget": field.widget,
                    "unit": field.unit,
                    "min": field.min,
                    "max": field.max,
                    "step": field.step,
                    "options": [
                        {"value": value, "label": label} for value, label in field.options
                    ],
                    "default": field.default,
                    "supported": supported,
                    "note": "" if supported else unsupported.get(field.key, "本引擎无对应参数。"),
                    "value": values.get(field.key),
                }
            )
        return {
            "engine": engine_key,
            "speaker": speaker,
            "voice_missing": not mapping.get("voice_id"),
            "groups": [
                {"key": group.key, "label": group.label}
                for group in GROUPS
                if group.key in ROW_PARAM_GROUPS
            ],
            "fields": fields,
        }

    def save_row_params(
        self, project_id: str, row_id: str, params: Any
    ) -> dict[str, Any]:
        """写入行级参数：指纹里含它，这一行的音频随之过期、等重生成。

        只是过期，不删：参数改回去时 ``_settle_stale`` 会把旧音频认回来。
        """
        row, _ = self._row_of(project_id, row_id)
        clean = clean_row_params(params)
        # 没改就不动：把一次没改值的保存当成"改过"，会让这一行白白重做一遍。
        if row_params(row) != clean:
            self.svc.store.set_row_params(row_id, clean)
            self.svc._settle_stale(project_id, str(row["chapter_id"]))
        return self.svc._row_view(project_id, self.svc.store.row(row_id) or row)

    @staticmethod
    def _split_options(project: dict[str, Any]) -> SplitOptions:
        mode = str(project.get("split_mode") or "rule")
        quote = str(project.get("split_quote") or DEFAULT_QUOTE)
        return SplitOptions(
            mode=mode if mode in SPLIT_MODES else "rule",
            dialogue=bool(int(project.get("split_dialogue", 1) or 0)),
            quote=quote if quote in {item.key for item in QUOTE_STYLES} else DEFAULT_QUOTE,
            max_chars=int(project.get("split_max_chars") or 200),
        )

    def _split_options_payload(self, project: dict[str, Any]) -> dict[str, Any]:
        options = self._split_options(project)
        return {
            "mode": options.mode,
            "dialogue": options.dialogue,
            "quote": options.quote,
            "max_chars": options.max_chars,
        }

    def split_options_meta(self) -> dict[str, Any]:
        """拆句选项的可选值：模式、引号写法与句末标点都由后端给。"""
        return {
            "modes": [{"key": key, "label": SPLIT_MODE_NAMES[key]} for key in SPLIT_MODES],
            "quotes": [
                {"key": item.key, "label": item.label, "open": item.open, "close": item.close}
                for item in QUOTE_STYLES
            ],
            "sentence_marks": SENTENCE_MARKS,
        }

    def save_split_options(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.svc._require_project(project_id)
        mode = str(body.get("mode") or "rule")
        if mode not in SPLIT_MODES:
            raise ProducerError(f"拆句模式不认识：{mode}。")
        quote = str(body.get("quote") or DEFAULT_QUOTE)
        if quote not in {item.key for item in QUOTE_STYLES}:
            raise ProducerError(f"对白引号写法不认识：{quote}。")
        try:
            max_chars = int(body.get("max_chars") or 200)
        except (TypeError, ValueError):
            raise ProducerError("单行字数上限必须是整数。") from None
        if not 50 <= max_chars <= 2000:
            raise ProducerError("单行字数上限要在 50–2000 之间。")
        self.svc.store.set_split_options(
            project_id,
            mode=mode,
            dialogue=bool(body.get("dialogue", True)),
            quote=quote,
            max_chars=max_chars,
        )
        return self.svc.detail(project_id)

    def split_sentences(self, project_id: str, scope: str = "all") -> dict[str, Any]:
        """拆句：默认重切**所有**章节；``scope="pending"`` 只处理还没有行的章。

        逐章走与「重拆本章」完全相同的一条路（:meth:`_carry_over`）：文本没变的行
        连同角色、情绪与音频一起保留。所以同一章从哪颗按钮进来，结果都一样；
        两个入口的差别只有范围，不是规则。
        """
        chapters = self.svc.store.chapters(project_id)
        if not chapters:
            raise ProducerError("还没有章节：先在「分章」里切出章节。")
        project = self.svc._require_project(project_id)
        options = self._split_options(project)
        pending_only = scope == "pending"
        done = 0
        total_rows = 0
        kept = 0
        reports: list[dict[str, Any]] = []
        for chapter in chapters:
            has_rows = int(chapter["rows"] or 0) > 0
            if has_rows and pending_only:
                continue
            chunks = split_rows(chapter["text"], options=options)
            planned, same = self._carry_over(chapter["id"], chunks)
            self.svc.store.replace_rows(chapter["id"], planned)
            if not same:
                # 行变了：旧成品对不上这些行，连音频记录一起作废（文件留着，下次覆盖）。
                self.svc.store.invalidate_chapter(chapter["id"], "sentences")
            report = check_split(chapter["text"], planned, options)
            report["chapter"] = chapter["title"]
            report["kept"] = sum(1 for row in planned if row.get("audio"))
            reports.append(report)
            done += 1
            total_rows += len(planned)
            kept += report["kept"]
        if not done:
            raise ProducerError("没有需要拆句的章节。")
        note = f"{done} 章 · {total_rows} 行" + (f" · 沿用 {kept} 行音频" if kept else "")
        CHANNEL.info(f"拆句完成：{note}", source="audiobook", project=project_id)
        return {**self.svc.detail(project_id), "split_report": merge_split_reports(reports)}

    def _carry_over(
        self, chapter_id: str, chunks: list[dict[str, str]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """按文本认亲：新行与旧行逐字相同时，角色、情绪、音频一起搬过来。

        两个拆句入口共用这一条，保证同一章得到同样的行；返回 ``(计划行, 行文本是否未变)``。
        """
        donors: dict[str, list[dict[str, Any]]] = {}
        old_rows = self.svc.store.rows(chapter_id)
        for old in old_rows:
            donors.setdefault(str(old["text"]), []).append(old)

        planned: list[dict[str, Any]] = []
        for row in chunks:
            queue = donors.get(row["text"])
            if not queue:
                planned.append(dict(row))
                continue
            old = queue.pop(0)
            audio = str(old["audio"] or "")
            status = str(old["status"] or "text")
            # 没有音频的失败只是上一次尝试的残影（引擎/音色早就换过了），
            # 认亲只认真正的产物；失败标记留到这次生成时重新判定。
            if not audio:
                if status == "failed":
                    status = "text"
                error = ""
            else:
                error = str(old["error"] or "")
            planned.append(
                {
                    **row,
                    "speaker": old["speaker"] or row["speaker"],
                    "emotion": old["emotion"],
                    "speaker_edited": old["speaker_edited"],
                    "emotion_edited": old["emotion_edited"],
                    "audio": audio,
                    "audio_key": old["audio_key"],
                    "error": error,
                    # 行参数跟着这一行走：文本没变就不该丢掉用户设过的语速/音高。
                    "params": old.get("params", "{}"),
                    "status": status,
                }
            )
        same = [item["text"] for item in old_rows] == [item["text"] for item in planned]
        return planned, same

    def resplit_chapter(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        """重新拆一章：与「拆全部章节」同一套规则，范围只限本章。"""
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        project = self.svc._require_project(project_id)
        options = self._split_options(project)
        chunks = split_rows(chapter["text"], options=options)
        if not chunks:
            raise ProducerError("本章正文为空。")
        planned, same = self._carry_over(chapter_id, chunks)
        self.svc.store.replace_rows(chapter_id, planned)
        if not same:
            self.svc.store.invalidate_chapter(chapter_id, "sentences")
        kept = sum(1 for row in planned if row.get("audio"))
        CHANNEL.info(
            f"重新拆句：{chapter['title']} · {len(planned)} 行"
            f"{'（行未变，音频保留）' if same else ''}",
            source="audiobook",
            project=project_id,
            chapter=chapter_id,
        )
        report = check_split(chapter["text"], planned, options)
        report["chapter"] = chapter["title"]
        report["kept"] = kept
        return {**self.svc.detail(project_id), "split_report": merge_split_reports([report])}

    def _row_of(self, project_id: str, row_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self.svc.store.row(row_id)
        chapter = self.svc.store.chapter(project_id, row["chapter_id"]) if row else None
        if not row or not chapter:
            raise ProducerError("这一行不属于该项目。")
        return row, chapter

    def delete_row(self, project_id: str, row_id: str) -> dict[str, Any]:
        self._row_of(project_id, row_id)
        self.svc.store.delete_row(row_id)
        return self.svc.detail(project_id)

    def split_row(self, project_id: str, row_id: str, at: int | None = None) -> dict[str, Any]:
        row, chapter = self._row_of(project_id, row_id)
        text = str(row["text"] or "")
        if at is None:
            point = split_point(text)
        else:
            point = int(at)
            if not 0 < point < len(text):
                raise ProducerError("光标位置无效：请把光标放到要拆开的地方。")
        left, right = text[:point].strip(), text[point:].strip()
        if not left or not right:
            raise ProducerError("这一行太短，无法拆分。")
        self.svc.store.update_row(row_id, text=left)
        self.svc.store.insert_row_after(
            chapter["id"],
            row_id,
            right,
            speaker=row["speaker"],
            emotion=row["emotion"],
            kind=row["kind"],
            speaker_edited=int(row["speaker_edited"]),
            emotion_edited=int(row["emotion_edited"]),
        )
        return self.svc.detail(project_id)

    def merge_rows(self, project_id: str, row_ids: list[str]) -> dict[str, Any]:
        rows = [self._row_of(project_id, row_id)[0] for row_id in row_ids]
        if len(rows) < 2:
            raise ProducerError("至少选中两行才能合并。")
        if len({row["chapter_id"] for row in rows}) > 1:
            raise ProducerError("只能合并同一章里的行。")
        ordinals = sorted(int(row["ordinal"]) for row in rows)
        if ordinals[-1] - ordinals[0] + 1 != len(ordinals):
            raise ProducerError("只能合并相邻的行。")
        self.svc.store.merge_rows([row["id"] for row in rows])
        return self.svc.detail(project_id)

    def auto_merge_rows(
        self, project_id: str, chapter_id: str, max_chars: int | None
    ) -> dict[str, Any]:
        """把短旁白并进后面的旁白：一行只念半句就断，听感是"卡"。

        ``max_chars`` 是合并后一行的上限（与拆句同一口径）；对白行一律不并。
        只动本章——整本走 :meth:`auto_merge_all`。
        """
        self.svc._ensure_idle(project_id)
        chapter = self.svc.store.chapter(project_id, chapter_id)
        if not chapter:
            raise ProducerError("章节不存在。")
        cap = self._merge_cap(max_chars)
        if not self._merge_chapter(project_id, chapter, cap):
            raise ProducerError(
                f"没有可合并的旁白行：本章旁白都不短于 {cap} 字，或后面紧跟对白。"
            )
        return self.svc.detail(project_id)

    def auto_merge_all(self, project_id: str, max_chars: int | None) -> dict[str, Any]:
        """整本自动合并：逐章走一遍，没有可合并旁白的章跳过。

        一章一个事务（不合成一个大事务）：中途出错时前面几章已经是合并后的样子，
        比"整本回滚、白跑一遍"更好交代。
        """
        self.svc._ensure_idle(project_id)
        cap = self._merge_cap(max_chars)
        chapters = self.svc.store.chapters(project_id)
        if not chapters:
            raise ProducerError("这本书还没有章节。")
        touched = removed = 0
        for chapter in chapters:
            got = self._merge_chapter(project_id, chapter, cap)
            if got:
                touched += 1
                removed += got
        if not touched:
            raise ProducerError(
                f"没有可合并的旁白行：全书旁白都不短于 {cap} 字，或后面紧跟对白。"
            )
        CHANNEL.info(
            f"自动合并（全书）：{touched} 章，并掉 {removed} 行，上限 {cap} 字",
            source="audiobook",
            project=project_id,
        )
        return self.svc.detail(project_id)

    @staticmethod
    def _merge_cap(max_chars: int | None) -> int:
        try:
            cap = int(max_chars)
        except (TypeError, ValueError):
            raise ProducerError("字数上限必须是整数。") from None
        if not 2 <= cap <= 1000:
            raise ProducerError("字数上限要在 2–1000 之间。")
        return cap

    def _merge_chapter(self, project_id: str, chapter: dict[str, Any], cap: int) -> int:
        """一章之内按组并旁白：没有可合并的组返回 0。"""
        groups = plan_auto_merge(self.svc.store.rows(chapter["id"]), max_chars=cap)
        if not groups:
            return 0
        removed = self.svc.store.auto_merge_rows(chapter["id"], groups)
        CHANNEL.info(
            f"自动合并：{chapter['title']} · {len(groups)} 组，并掉 {removed} 行，上限 {cap} 字",
            source="audiobook",
            project=project_id,
        )
        return removed

    def batch_update_rows(
        self,
        project_id: str,
        row_ids: list[str],
        *,
        speaker: str | None = None,
        emotion: str | None = None,
    ) -> dict[str, Any]:
        if speaker is None and emotion is None:
            raise ProducerError("没有要修改的字段。")
        for row_id in row_ids:
            self._row_of(project_id, row_id)
        self.svc.store.batch_update(row_ids, speaker=speaker, emotion=emotion)
        return self.svc.detail(project_id)

    def merge_speakers(self, project_id: str, names: list[str], target: str) -> dict[str, Any]:
        """把几个说话人并成一个名字：只改行上的 ``speaker``。

        旧音频随之作废（音色跟着说话人走），改名后的行记 ``speaker_edited``，
        后续分析不再覆盖；``voices`` 里的旧映射不动——没有行引用它，界面上自然不显示。
        """
        self.svc._ensure_idle(project_id)
        target = str(target or "").strip()
        if not target:
            raise ProducerError("合并后的名字不能为空。")
        if target == NARRATOR:
            raise ProducerError("不能把角色合并成「旁白」。")
        checked = {str(name).strip() for name in names if str(name).strip()}
        checked.add(target)
        if NARRATOR in checked:
            raise ProducerError("「旁白」不能合并进角色。")
        sources = sorted(checked - {target})
        if not sources:
            raise ProducerError("至少选两个不同的角色。")
        existing = {str(item["speaker"]) for item in self.svc.store.speakers(project_id)}
        missing = [name for name in sources if name not in existing]
        if missing:
            raise ProducerError(f"这些角色不在本书里：{'、'.join(missing)}。")
        changed = self.svc.store.merge_speakers(project_id, sources, target)
        if not changed:
            raise ProducerError("这些角色没有可合并的行。")
        CHANNEL.info(
            f"角色合并：{'、'.join(sources)} → {target} · {changed} 行",
            source="audiobook",
            project=project_id,
        )
        return self.svc.detail(project_id)
