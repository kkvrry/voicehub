"""分析阶段：让对话模型给对白行认人、标情绪（可选再写引导指令）。

区间与开销的约定：

- 发送粒度只决定「一次请求装多少行」，**不跨章**——一章的对白按批切开也只是多几个
  请求，正文永不带入别章；
- 接口无状态：每批都把上下文正文重新发一次，模型不保留上一批的记忆；
- 多个请求按 ``analysis_concurrency`` 并发，缓存命中不发请求——重跑不重复计费。

回复是紧凑格式 ``{"rows": [[行号, 说话人, …内容…, 回显]]}``：要回哪几格由提示词自己决定
（生效预设按身份定，自定义提示词按正文写没写「引导指令」「情绪」推断，见
``ai_prompts.analysis_fields``）；数组里的值按内容认——词表里的算情绪，其余算引导指令，
所以情绪写在指令前面也不串格。行号是批内下标，串位时行号照样"合法"，所以用回显
（原文开头）兜底；说话人直接给姓名，不另立名单（同人异名留给用户自己合并）。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiobook import ai_prompts
from app.producers.builtin.audiobook.fingerprint import analysis_key
from app.producers.builtin.audiobook.llmio import (
    MIN_SPLIT_ROWS,
    chat_with_retry,
    echo_matches,
    output_limit,
    output_tokens,
    parse_json,
    rows_of,
    rows_per_request,
)
from app.producers.builtin.audiobook.prompts import compose_prompt
from app.producers.builtin.audiobook.rows import emotion_words, missing_text, row_params
from app.producers.builtin.audiobook.rules import DIALOGUE, NARRATOR

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

__all__ = ["AnalyzeStage"]

#: 候选角色的上限：提示词里列到 40 个已经够长，再多只是占 token。
_MAX_KNOWN_ROLES = 40
#: 随请求提示的候选角色条数：只给最近的这些，避免提示词随书变长。
_HINT_ROLES = 20
#: 引导指令的落地长度上限：提示词要的是 20 字以内，模型偶尔会写成一段话。
_MAX_INSTRUCTION = 60


def clean_instruction(raw: Any) -> str:
    """模型的引导指令 → 一行话：压掉换行与多余空白，去掉外层引号，截到上限。

    长的直接截断而不是丢弃：一句话的前半句仍然可用，丢整行等于这一行白问一遍。
    """
    text = " ".join(str(raw or "").split())
    if len(text) >= 2 and text[0] in "“\"「『" and text[-1] in "”\"」』":
        text = text[1:-1].strip()
    return text[:_MAX_INSTRUCTION]


class AnalyzeStage:
    """分析的全部实现；门面（``AudiobookService``）只负责转调。"""

    def __init__(self, service: AudiobookService) -> None:
        self.svc = service

    @property
    def store(self) -> Any:
        return self.svc.store

    @property
    def settings(self) -> Any:
        return self.svc.settings

    @staticmethod
    def needs_analysis(row: dict[str, Any]) -> bool:
        """要模型认人的行：对白行（占位「对话」或已有人工/分析给的角色名）。

        旁白是拆句阶段定的，模型不参与——既不送、也不改（原版口径）。
        """
        return str(row["speaker"] or "").strip() not in ("", NARRATOR)

    @staticmethod
    def is_pending(row: dict[str, Any]) -> bool:
        """还没标注过的对白行：说话人仍是空或占位「对话」。"""
        return str(row["speaker"] or "").strip() in ("", DIALOGUE)

    @staticmethod
    def context_text(
        chapter: dict[str, Any],
        rows: list[dict[str, Any]],
        batch: list[dict[str, Any]],
        mode: str,
        lines: int,
    ) -> str:
        """这一批要带上的正文：整章，或本批对白前后各 ``lines`` 行。

        窗口**含本批对白自身**（人称与语序要靠它定位），所以对白会同时出现在
        「正文上下文」与「对白行」两处：前者给位置，后者给要回填的文本。
        接口无状态——每批都把这段正文重新发一次，模型不保留上一批的记忆。
        """
        if mode != "window":
            return str(chapter["text"])
        positions = {row["id"]: index for index, row in enumerate(rows)}
        indexes = [positions[row["id"]] for row in batch if row["id"] in positions]
        if not indexes:
            return str(chapter["text"])
        first = max(0, min(indexes) - lines)
        last = min(len(rows), max(indexes) + lines + 1)
        return "\n".join(str(row["text"]) for row in rows[first:last])

    def apply_items(
        self,
        batch: list[dict[str, Any]],
        items: list[Any],
        words: set[str],
        fields: tuple[str, ...],
    ) -> tuple[int, int, list[str]]:
        """把一批模型输出写回行，返回 ``(改动行数, 回显不符跳过数, 这一批出现的说话人)``。

        接受两种形状：提示词要的 ``[行号, 说话人, …内容…, 原文开头]``，以及旧回复/
        不听话的模型给的 ``{"i":…, "speaker":…, "emotion":…, "instruction":…}``。

        ``fields`` 只决定**收哪几格**（由提示词推出，见 ``prompts.fields_for_prompt``）：
        数组里除了说话人以外的值不按位置认，而是"词表里的算情绪、其余的算引导指令"，
        所以模型把情绪写在指令前面也不会串格。多出来的一格是对稿回显。

        :param words: 情绪候选的生效值（用户词表）。模型回了表外的词时保留这一行
            原来的情绪——不猜、不硬塞。
        """
        changed = 0
        skipped = 0
        names: list[str] = []
        pending: list[tuple[str, str, str, str]] = []

        def given_of(values: list[Any]) -> dict[str, str]:
            """说话人 + 其余字段：顺序无关，按"是不是词表里的情绪"分。"""
            given: dict[str, str] = {}
            rest = [str(value if value is not None else "").strip() for value in values]
            if rest:
                given["speaker"] = rest.pop(0)
            for value in rest:
                text = clean_instruction(value)
                if "emotion" in fields and text in words and "emotion" not in given:
                    given["emotion"] = text
                elif "instruction" in fields and "instruction" not in given:
                    given["instruction"] = text
                elif "emotion" in fields and "emotion" not in given:
                    given["emotion"] = text
            return given

        for item in items:
            if isinstance(item, dict):
                index = item.get("i")
                if not isinstance(index, int) or not 0 <= index < len(batch):
                    continue
                row = batch[index]
                given = {name: clean_instruction(item.get(name)) for name in fields}
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                index = item[0]
                if not isinstance(index, int) or not 0 <= index < len(batch):
                    continue
                row = batch[index]
                values = list(item[1:])
                # 比字段多一格 = 末尾是对稿回显；对不上说明串位，整条跳过。
                if len(values) > len(fields):
                    head = str(values.pop() or "").strip()
                    if head and not echo_matches(str(row["text"]), head):
                        skipped += 1
                        continue
                given = given_of(values)
            else:
                continue
            speaker = given.get("speaker", "")
            if speaker and speaker != "未知":
                names.append(speaker)
            new_speaker = speaker or row["speaker"]
            emotion = given.get("emotion", "")
            new_emotion = emotion if emotion in words else row["emotion"]
            # 行里已有引导指令就不覆盖：那是用户自己写的，模型只补空位。
            current = str(row_params(row).get("instruction") or "").strip()
            new_instruction = "" if current else given.get("instruction", "")
            if (
                new_speaker == row["speaker"]
                and new_emotion == row["emotion"]
                and not new_instruction
            ):
                continue
            # 先攒着：一批一次写库。逐行写是一行两次连接，分析一本书要多花三成时间。
            pending.append((str(row["id"]), new_speaker, new_emotion, new_instruction))
        if pending:
            changed = self.store.set_row_annotations(pending)
        return changed, skipped, names

    def start(self, project_id: str) -> dict[str, Any]:
        """建分析任务：先校验有没有可分析的句子，再把慢活放进后台。"""
        self.svc._ensure_idle(project_id)
        rows = self.store.all_rows(project_id)
        missing = missing_text(rows)
        if missing:
            raise ProducerError(missing)
        # 只有对白行需要模型：旁白不送、不改。范围决定要不要复核已标注的行。
        pending_only = str(self.settings.analysis_scope or "all") == "pending"
        total = sum(
            1
            for row in rows
            if self.needs_analysis(row) and (not pending_only or self.is_pending(row))
        )
        if not rows:
            raise ProducerError("还没有可分析的句子：先完成拆句。")
        if not total:
            if pending_only:
                raise ProducerError("没有待标注的行：对白都已经有说话人了。")
            raise ProducerError("没有需要对白的行：全篇都是旁白，不需要分析。")
        return self.svc._start_job(
            project_id,
            "analysis",
            total,
            lambda job_id: self.run(project_id, job_id),
        )

    async def run(self, project_id: str, job_id: str = "") -> dict[str, Any]:
        """按章分析：只让模型给对白行认人 + 标情绪。

        回复上限由通道自己的「回复上限」决定（:func:`output_limit`），单批行数按它
        切好；万一仍被截断或解析不出，就对半分再发，不整批作废。
        """
        from app.usecase import llm as llm_uc

        project = self.svc._require_project(project_id)
        book = str(project["name"])
        settings = self.settings
        channel = str(settings.analysis_channel or "").strip()
        model = str(settings.analysis_model or "").strip()
        custom_prompt = str(settings.analysis_prompt or "").strip()
        preset_key = str(getattr(settings, "analysis_preset", "") or "").strip()
        # 本项目的自定义提示词优先；没填过就用设置页「词表」里那条预设的生效正文。
        # 预设被删掉时回落到内置那份（compose_prompt 的默认）。
        if custom_prompt:
            prompt_text = custom_prompt
        else:
            preset = ai_prompts.analysis_preset(preset_key or "default")
            if preset is None:
                # 选中的预设可能已被删除：回落到内置默认那份，别把空提示词发出去。
                preset = ai_prompts.analysis_preset("default")
            prompt_text = str(preset["text"]) if preset else ""
        granularity = str(settings.analysis_granularity or "batch")
        context_mode = str(settings.analysis_context or "chapter")
        context_lines = max(1, int(settings.analysis_context_lines))
        batch_size = max(10, int(settings.analysis_batch))
        concurrency = max(1, min(32, int(settings.analysis_concurrency)))
        pending_only = str(settings.analysis_scope or "all") == "pending"
        limit = output_limit(channel)
        per_request = rows_per_request(limit)
        #: 情绪候选的生效值（设置页「词表」）：提示词列给它、回写认它、缓存键带它。
        #:    从前写死 EMOTIONS：用户加了自定义词，模型按新词表标的词会在回写那一步
        #:    被当成"不在表里"丢掉——标了也白标。
        words = emotion_words()
        vocabulary = set(words)
        #: 这次要模型回什么：由生效预设的身份或自定义提示词的正文决定。
        #:    它只影响回写收哪几格，不影响发出去的那段正文。
        fields = ai_prompts.analysis_fields(prompt_text, preset_key)

        #: 已出现过的说话人：本项目现有的名字 + 这次运行里模型回的名字，逐批往后传。
        known_roles: list[str] = []
        for item in self.store.speakers(project_id):
            name = str(item.get("speaker") or "").strip()
            if name and name not in (NARRATOR, DIALOGUE) and name not in known_roles:
                known_roles.append(name)

        chapters = self.store.chapters(project_id)
        # 全项目的行取一次再按章分组：逐章 ``rows()`` 是章数次连接，一百章就是近一秒。
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.all_rows(project_id):
            grouped.setdefault(str(row["chapter_id"]), []).append(row)
        # 先把所有请求排好：粒度决定一批几行，上下文范围决定随请求带哪段正文。
        plans: list[tuple[str, list[dict[str, Any]], str]] = []
        for chapter in chapters:
            rows = grouped.get(str(chapter["id"]), [])
            if not rows:
                continue
            targets = [
                row
                for row in rows
                if self.needs_analysis(row) and (not pending_only or self.is_pending(row))
            ]
            if not targets:
                continue
            step = len(targets) if granularity == "chapter" else batch_size
            step = max(1, min(step, per_request))
            for start in range(0, len(targets), step):
                batch = targets[start : start + step]
                plans.append(
                    (
                        str(chapter["id"]),
                        batch,
                        self.context_text(chapter, rows, batch, context_mode, context_lines),
                    )
                )

        changed_chapters: set[str] = set()
        counters = {"rows": 0, "cached": 0, "requests": 0, "split": 0, "echo": 0}
        cancelled = False
        semaphore = asyncio.Semaphore(concurrency)

        def apply(chapter_id: str, batch: list[dict[str, Any]], items: list[Any]) -> None:
            """写回一批；行数只在这里记一次（分段后的子批各记各的）。"""
            changed, skipped, names = self.apply_items(batch, items, vocabulary, fields)
            for name in names:
                if name not in known_roles and len(known_roles) < _MAX_KNOWN_ROLES:
                    known_roles.append(name)
            counters["echo"] += skipped
            if changed:
                changed_chapters.add(chapter_id)
                counters["rows"] += changed
            if job_id:
                self.store.bump_job(job_id, done=len(batch))

        async def request(chapter_id: str, batch: list[dict[str, Any]], context: str) -> None:
            nonlocal cancelled
            if cancelled:
                return
            hint_roles = known_roles[:_HINT_ROLES]
            key = analysis_key(
                channel=channel,
                model=model,
                prompt=prompt_text,
                book=book,
                text=context,
                rows=[row["text"] for row in batch],
                granularity=granularity,
                context=context_mode,
                context_lines=context_lines,
                emotions=words,
            )
            cached = self.store.cache_get(key)
            if cached is not None:
                # 并发只罩住真正要发出去的请求；缓存命中不排队。
                counters["cached"] += 1
                data = parse_json(cached)
                apply(chapter_id, batch, rows_of(data))
                return
            payload = json.dumps(
                [{"i": index, "text": row["text"]} for index, row in enumerate(batch)],
                ensure_ascii=False,
            )
            prompt = compose_prompt(
                prompt_text,
                book=book,
                text=context,
                rows=payload,
                roles=hint_roles,
                emotions=words,
            )
            body = {
                "messages": [
                    {"role": "system", "content": "你是有声书配音脚本标注器，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "json_object": True,
                "temperature": 0.2,
                "max_tokens": output_tokens(len(batch), limit),
            }
            if channel:
                body["provider"] = channel
            if model:
                body["model"] = model
            async with semaphore:
                if job_id and self.store.job_cancelled(job_id):
                    cancelled = True
                    return
                counters["requests"] += 1
                result = await chat_with_retry(llm_uc, body)
            data = parse_json(str(result.get("text") or ""))
            items = rows_of(data)
            truncated = str(result.get("finish_reason") or "") == "length"
            if (truncated or not items) and len(batch) > MIN_SPLIT_ROWS:
                # 截断的回复不能用：对半分两次发，别把整批标注丢掉。
                counters["split"] += 1
                middle = len(batch) // 2
                await request(chapter_id, batch[:middle], context)
                await request(chapter_id, batch[middle:], context)
                return
            if items:
                self.store.cache_put(key, json.dumps({"rows": items}, ensure_ascii=False))
            apply(chapter_id, batch, items)

        failure: BaseException | None = None
        if plans:
            outcomes = await asyncio.gather(
                *(request(*plan) for plan in plans), return_exceptions=True
            )
            for outcome in outcomes:
                # 一个请求失败就整步失败：吞掉异常会让"分析完成"变成假话。
                if isinstance(outcome, BaseException) and failure is None:
                    failure = outcome

        # 有改动：本章成品失效；没改动就别把已合并的章节退回分析阶段。
        # 失败时也先走这一步：已经写进去的标注同样要让旧成品失效。
        for chapter in chapters:
            # 「这一章有没有行」章聚合里就有，不必为此把整章的行读回来。
            if not int(chapter["rows"] or 0):
                continue
            if chapter["id"] in changed_chapters:
                self.store.invalidate_chapter(chapter["id"], "analysis")
            elif chapter["stage"] not in ("audio", "merged"):
                self.store.set_chapter_stage(chapter["id"], "analysis")

        if failure is not None:
            raise failure

        note = f"标注 {counters['rows']} 行"
        if counters["cached"]:
            note += f"（缓存命中 {counters['cached']} 批）"
        if counters["split"]:
            note += f"（{counters['split']} 批超长，已自动分段）"
        if counters["echo"]:
            note += f"（{counters['echo']} 行回显不符，已跳过）"
        if cancelled:
            note += "，已取消"
        if job_id:
            self.store.update_job(job_id, message=note)
        CHANNEL.info(f"分析完成：{note}", source="audiobook", project=project_id)
        return self.svc.detail(project_id)
