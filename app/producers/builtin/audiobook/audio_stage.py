"""生成阶段：把行送进引擎、落成逐行音频，并维护章节的生成状态。

一次生成的三段：算计划（哪些行要重做）→ 按引擎额度排队合成 → 刷新章阶段。
计划的判据是指纹（见 :mod:`fingerprint`），引擎与并发由宿主的网关与设置决定；
本模块不写死任何一台引擎。

并发两条线：单引擎额度按「引擎配置 → 声明上限 → 项目设置」取，
整批另有一把总闸，避免总并发按引擎数翻倍。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from app.engines.base import CODES_ENGINE_UNAVAILABLE, EngineError
from app.infra.atomic import write_bytes
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.fingerprint import (
    audio_key,
    audio_key_v2,
    sample_key,
)
from app.producers.builtin.audiobook.paths import DirListing
from app.producers.builtin.audiobook.rows import emotion_words, missing_text, row_params
from app.producers.builtin.audiobook.rules import NARRATOR
from app.usecase import gateway, pronunciation

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

__all__ = ["AudioPlan", "AudioStage"]


class AudioPlan(NamedTuple):
    """一次生成计划：要合成的、跳过的、以及两类"跳过了但要让用户知道"的。

    以前是个四元组，靠位置拆包；加第三类跳过原因时序号就跟着挪，调用点全是猜。
    字段名让"哪个数字是什么"写在读的地方。
    """

    chapters: list[dict[str, Any]]
    #: 真正要合成的行：``(章, 行, 音色映射, 指纹)``。
    tasks: list[tuple[dict[str, Any], dict[str, Any], dict[str, str], str]]
    #: 文件已是最新、这次不必重做的行数。
    skipped: int
    #: 没配音色的说话人 → 行数。
    unassigned: dict[str, int]
    #: 音色指向的引擎已停用：引擎名 → 行数。整批都做不了，要在开始前就说清。
    disabled: dict[str, int]

#: 合成进度的落库间隔（秒）。界面轮询本来就看不出更细的差别，而每行一次 UPDATE
#: 会把 SQLite 的写锁一直攥在手里——进度条越"勤快"，合成越慢。
_PROGRESS_FLUSH_S = 0.4

#: 取消标志的重查间隔（秒）。取消是人工动作，半秒内生效与逐行查库没有区别。
_CANCEL_POLL_S = 0.2


def voice_identity(engine: Any) -> str:
    """引擎身份的唯一实现在 :func:`app.usecase.engines.engine_identity`。

    创作页与配音共用同一判据：插件版本 + 模型 / 后端，拿不到就只按版本算。
    """
    from app.usecase.engines import engine_identity

    return engine_identity(engine)


def row_key(
    row: dict[str, Any],
    mapping: dict[str, str],
    *,
    words: set[str],
    identity: str,
    version: int,
    revision: str = "",
) -> str:
    """一行的合成指纹；``version`` 由行上存的 ``audio_key_v`` 决定。

    v1 是历史口径（整张读音表版本 + 固定格式位），只为读老行；
    v2 只算"这一行真正命中的读音替换"和"引擎身份"。
    """
    text = str(row["text"])
    params = row_params(row)
    emotion = row["emotion"] if row["emotion"] in words else ""
    if version >= 2:
        return audio_key_v2(
            text=text,
            engine=mapping["engine"],
            voice_id=mapping["voice_id"],
            emotion=emotion,
            identity=identity,
            params=params,
            sample=sample_key(mapping),
            pron=pronunciation.matches(text),
        )
    return audio_key(
        text=text,
        engine=mapping["engine"],
        voice_id=mapping["voice_id"],
        emotion=emotion,
        params=params,
        sample=sample_key(mapping),
        revision=revision,
    )


def _engine_limits(
    tasks: list[tuple[dict[str, Any], dict[str, Any], dict[str, str], str]],
    engines: dict[str, Any],
    settings_limit: int,
    settings_of: Callable[[Any], Any] | None = None,
) -> dict[str, int]:
    """每个引擎这一轮最多同时跑几条。

    优先级：**引擎配置里的并发** → 引擎声明的硬上限 → 项目设置。云端引擎把账号
    额度写在自己的设置里（用户可改），它直接生效，不再被项目「合成并发」盖住；
    本机引擎声明硬上限 1，配置再大也突破不了，两者同时存在时取较小者。

    **为什么不能一刀切**：本机单模型服务（audio.cpp 那几个 GGUF 实例）一次只跑一条。
    实测给它开两条：单条 7.0s，两条并发是 7.0s + 14.3s——总时长与串行一样，
    而**每一行的等待都变长了**，用户看到的就是"从界面点比在 WebUI 点慢一倍"。

    引擎没配置并发、没声明上限，或这一行指的引擎已经不在了，都按项目设置——
    "不知道该限多少"时听用户的，而不是替他把并发压成 1。
    """
    limits: dict[str, int] = {}
    for _, _, mapping, _ in tasks:
        key_of = str(mapping.get("engine") or "")
        if key_of in limits:
            continue
        engine = engines.get(key_of)
        cap = int(engine.capabilities().max_concurrency or 0) if engine else 0
        own = 0
        if engine is not None and settings_of is not None:
            own = engine.configured_concurrency(settings_of(engine))
        if own > 0:
            limits[key_of] = min(own, cap) if cap > 0 else own
        else:
            limits[key_of] = min(settings_limit, cap) if cap > 0 else settings_limit
    return limits

def _global_limit(limits: dict[str, int], settings_limit: int) -> int:
    """整批的总闸：不让总并发按引擎数翻倍，也不盖住单引擎自己的额度。

    一台引擎时这把闸不小于它的额度，等于不存在；多台引擎同批时，把总量收敛到
    「项目设置」与「最大单引擎额度」中较大的那个。
    """
    return max([settings_limit, *limits.values()])

def _voice_group(task: tuple[dict[str, Any], dict[str, Any], dict[str, str], str]) -> tuple[str, str]:
    """同一颗音色的行排在一起：模型换音色就要重载参考音频，按角色成块最省。"""
    _, _, mapping, _ = task
    return (str(mapping.get("engine") or ""), str(mapping.get("voice_id") or ""))


def _unassigned_note(unassigned: dict[str, int]) -> str:
    """缺音色的行与角色压成一句：数量要有，名字也要有——用户才知道去补谁。"""
    if not unassigned:
        return ""
    rows = sum(unassigned.values())
    names = list(unassigned)
    shown = "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
    return f"{rows} 行没有分配音色，已跳过（{shown}）"


def _disabled_note(disabled: dict[str, int]) -> str:
    """音色指向的引擎已停用：一条就够，别让每一行都长成一个错误。"""
    if not disabled:
        return ""
    rows = sum(disabled.values())
    names = list(disabled)
    shown = "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
    return f"{rows} 行所属引擎已停用，已跳过（{shown}）：在「引擎」页启用后再生成"


def _plan_note(plan: AudioPlan) -> str:
    """计划里两类"跳过但用户要知道"的原因，合成一句。"""
    parts = (_unassigned_note(plan.unassigned), _disabled_note(plan.disabled))
    return "；".join(part for part in parts if part)



class AudioStage:
    """生成阶段的实现；门面（``AudiobookService``）只负责转调。"""

    def __init__(self, service: AudiobookService) -> None:
        self.svc = service

    @property
    def store(self) -> Any:
        return self.svc.store

    @property
    def settings(self) -> Any:
        return self.svc.settings

    def _overwritten_rows(self, rows: list[dict[str, Any]]) -> set[str]:
        """找出音频文件被别的行顶掉的行（旧版按章内序号存文件名留下的坑）。

        老文件叫 ``0072.wav``，序号 72 的行生成时写它；一旦前面删过行，原来 72 号
        的行变成 71 号，可它的 ``audio`` 还指着 ``0072.wav``。此后 72 号那行再生成，
        同一路径被写第二遍，71 号就播出了别人的声音——而它的 ``audio_key`` 仍然
        对得上，生成计划会把它当成「已生成」永远跳过。

        能判定归属的就一条：文件名序号等于自己当前 ordinal 的那行是最后写入者，
        同组的其余行都算被顶掉；没有这样一行（序号挪过不止一位）就整组重生成。
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["audio"]:
                groups.setdefault(row["audio"], []).append(row)
        stale: set[str] = set()
        for path, sharers in groups.items():
            if len(sharers) < 2:
                continue
            stem = Path(path).stem
            owner = next(
                (r for r in sharers if stem.isdigit() and stem == f"{int(r['ordinal']):04d}"),
                None,
            )
            for row in sharers:
                if owner is None or row["id"] != owner["id"]:
                    stale.add(row["id"])
        return stale

    def _audio_plan(
        self, project_id: str, rows: list[dict[str, Any]] | None = None
    ) -> AudioPlan:
        """列出要合成的行；已是最新的跳过。超长在这里就报，缺音色的行记下、跳过。

        ``rows`` 是调用方已经取好的全项目行（一次 join）。给进来就不再查第二遍：
        这本书一万五千行，多读一次是一秒。

        **只认已启用的引擎**：停用的那台做不了，它的行在计划里就摘掉并计数——
        不摘的话每一行都要撞一次网关的"引擎未启用"，一万人书就是一万条行错误，
        而原因只有一个。
        """
        from app.usecase import engines as engines_uc

        voices = self.svc.store.voice_map(project_id)
        chapter_voices = self.svc.chapter_voice_rows(project_id)
        chapters = self.svc.store.chapters(project_id)
        engines = engines_uc.active_engines()
        # 这一行的情绪发不发：过用户词表的生效值（默认两组 + 自定义、去隐藏）。
        words = set(emotion_words())
        tasks: list[tuple[dict[str, Any], dict[str, Any], dict[str, str], str]] = []
        over_limit: list[tuple[dict[str, Any], int, str, int]] = []
        #: 没配音色的说话人 → 行数。跳过它们，其余照常生成；提示由调用方带出去。
        unassigned: dict[str, int] = {}
        #: 音色指向的引擎已停用：引擎名 → 行数。与上面同理，只是原因不同。
        disabled: dict[str, int] = {}
        #: 停用引擎的显示名，只在提示里用；同一个 key 只查一次。
        labels: dict[str, str] = {}
        skipped = 0
        # 读音表版本只在比老指纹时用；新指纹按行算命中，不再用整表版本。
        revision = pronunciation.revision()
        # 引擎身份按 key 查一次：settings_for 每次都要验一遍配置，逐行问太贵。
        identities: dict[str, str] = {}
        # 老行指纹升级：只换键与口径，不动音频（见 store.set_row_audio_keys）。
        blessed: list[tuple[str, str]] = []
        # 引擎的单次上限按 key 查一次就够：capabilities() 是每行都要问的东西。
        limits: dict[str, int] = {}
        # 「这个文件还在不在」别逐行问：项目根取一次，目录也各列一遍（_DirListing）。
        # 这本书一万五千行，逐行 ``is_file()`` + 每行查库拿根目录，实测几十秒。
        root = self.svc.project_folder(project_id)
        files = DirListing(root)

        if rows is None:
            rows = self.svc.store.all_rows(project_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["chapter_id"]), []).append(row)

        for chapter in chapters:
            chapter_rows = grouped.get(str(chapter["id"]), [])
            overwritten = self._overwritten_rows(chapter_rows)
            for row in chapter_rows:
                speaker = row["speaker"] or NARRATOR
                mapping = self.svc.mapping_for(row, voices, chapter_voices)
                if not mapping or not mapping.get("voice_id"):
                    # 缺音色只挡住这几行：整本书等着配一个配角，不如先把能做的做完。
                    # 按章分配时缺的是「这一章」，提示里就写章名，不写旁白。
                    label = str(chapter.get("title") or speaker) if chapter_voices else speaker
                    unassigned[label] = unassigned.get(label, 0) + 1
                    continue
                engine_key = str(mapping["engine"])
                if engine_key not in engines:
                    # 已停用（或插件已卸载）：这台发不出声。按引擎记一次，别逐行报错。
                    if engine_key not in labels:
                        known = engines_uc.detect_engine(engine_key)
                        labels[engine_key] = known.label if known else engine_key
                    name = labels[engine_key]
                    disabled[name] = disabled.get(name, 0) + 1
                    continue
                engine = engines[engine_key]
                if engine_key not in limits:
                    limits[engine_key] = int(engine.capabilities().max_chars or 0)
                limit = limits[engine_key]
                if limit:
                    # 算的是**要发出去的字**：语气词标签会被删掉，不该占上限。
                    sent = engine.prepare_text(str(row["text"]))
                    if len(sent) > limit:
                        over_limit.append((row, limit, engine.label, len(sent)))
                        continue
                identity = identities.get(engine_key)
                if identity is None:
                    identity = voice_identity(engine)
                    identities[engine_key] = identity
                key = row_key(row, mapping, words=words, identity=identity, version=2)
                # 被顶掉的行旧指纹照样对得上，但文件里已经是别人的声音。
                if (
                    row["audio"]
                    and files.has(row["audio"])
                    and row["id"] not in overwritten
                ):
                    if int(row.get("audio_key_v") or 1) >= 2:
                        if row["audio_key"] == key:
                            skipped += 1
                            continue
                    elif row["audio_key"] == row_key(
                        row,
                        mapping,
                        words=words,
                        identity=identity,
                        version=1,
                        revision=revision,
                    ):
                        # 老指纹对得上 = 磁盘上就是当前输入的结果。只把口径换成
                        # v2，不重做、不花钱；以后改读音表只影响真正命中的行。
                        blessed.append((key, str(row["id"])))
                        skipped += 1
                        continue
                tasks.append((chapter, row, mapping, key))
        if blessed:
            self.svc.store.set_row_audio_keys(blessed)
        if over_limit:
            _, limit, label, longest = max(over_limit, key=lambda item: item[3])
            raise ProducerError(
                f"{len(over_limit)} 行超过「{label}」单次上限 {limit} 字"
                f"（最长 {longest} 字）：先在行表里拆分再生成。"
            )
        return AudioPlan(
            chapters=chapters,
            tasks=tasks,
            skipped=skipped,
            unassigned=unassigned,
            disabled=disabled,
        )

    async def _synth_row(
        self,
        project_id: str,
        root: Path,
        folder: Path,
        chapter: dict[str, Any],
        row: dict[str, Any],
        mapping: dict[str, str],
        key: str,
        library: Any,
        words: set[str],
    ) -> bool:
        """``root`` / ``folder`` 由调用方准备好：每行查一次项目根、建一次目录都是白花。

        返回 ``False`` = 这一行正被另一个 worker 合成，本次跳过（不写文件、不改状态）。
        """
        store = self.svc.store
        if not store.claim_row(row["id"]):
            # 另一个批次（另一个实例 / 另一个入口）已经在做这一行。没有这道闸，
            # 两边会把同一段音频写进同一个文件名，先写的那份被后来者截断。
            return False
        try:
            emotion = row["emotion"] if row["emotion"] in words else ""
            mode = str(mapping.get("sample_mode") or "emotion")
            result = await gateway.synthesize(
                library,
                engine_key=mapping["engine"],
                voice_id=mapping["voice_id"],
                text=row["text"],
                emotion=emotion,
                # 行级参数（语速、音高……）：没设过就是空表，引擎按自己的配置走。
                extra=row_params(row),
                # 样例：固定那条，或按这一行的情绪匹配（认不出就回主样例）。
                sample_file=str(mapping.get("sample_file") or "") if mode == "fixed" else "",
                match_emotion=mode == "emotion",
                # 失败由这里记（带行号）；网关再记一条就是同一件事写两遍。
                log_failure=False,
            )
            # 容器由引擎决定（请求里不再指定格式），文件名照实说它是什么：从前固定按设置
            # 里的后缀命名，IndexTTS2 这类只出 WAV 的引擎写出了一批 .mp3 的 WAV。
            suffix = (result.format or "wav").lower()
            # 序号打头方便在文件夹里认，唯一性靠后面的行 id：删行 / 并行的会重排序号，
            # 只按序号命名的话，旧文件就成了「前任的音频」，重生成还会把别人那份覆盖掉。
            path = folder / f"{int(row['ordinal']):04d}-{row['id']}.{suffix}"
            # 先写临时文件再改名：中途失败或被取消，目标位置不会留下半段音频。
            write_bytes(path, result.audio)
            store.set_row_audio(
                row["id"], path.relative_to(root).as_posix(), key, key_version=2
            )
            return True
        finally:
            store.release_row(row["id"])

    def _refresh_audio_stages(self, project_id: str) -> None:
        """按行状态回写章阶段：全齐才算生成完，部分失败退回分析。

        ``chapters()`` 的聚合里已经有 ``rows`` / ``done`` 两列——它就是行统计本身。
        以前为了判断「这一章是不是全生成了」，把每章的行再全读一遍（一万五千行、
        近一秒），然后才拿 ``all(status == 'audio')``，白读。
        """
        done: list[str] = []
        partial: list[str] = []
        for chapter in self.svc.store.chapters(project_id):
            total = int(chapter["rows"] or 0)
            if not total:
                continue
            if int(chapter["done"] or 0) == total:
                if chapter["stage"] not in ("merged", "audio"):
                    done.append(str(chapter["id"]))
            # 已经是 analysis 且没有成品文件时，这一条 UPDATE 只是白白改一次库。
            elif chapter["stage"] != "analysis" or chapter["audio"]:
                partial.append(str(chapter["id"]))
        # 一次写明：逐章各来一条 UPDATE 是逐章一次提交，一百章就要半秒。
        self.svc.store.set_chapters_stage(done, "audio")
        self.svc.store.set_chapters_stage(partial, "analysis", clear_audio=True)

    async def synthesize(
        self,
        project_id: str,
        library: Any,
        job_id: str = "",
        plan: AudioPlan | None = None,
    ) -> dict[str, Any]:
        """``plan`` 是 ``start_audio`` 刚算好的那份：任务里不再重算一遍。

        全量扫描一次要读几万行、列几十个目录，刚算完就扔、再算一次，
        等于让用户多点几十秒「0/N」。
        """
        # 先把能复活的过期行认回来，否则它们会白排一遍队（甚至报"没有需要生成的"）。
        # 带着 ``plan`` 来时就跳过：那份计划是在 settle **之后**算的，再查一次纯属重复。
        if plan is None:
            self.svc._settle_stale(project_id)
            plan = self._audio_plan(project_id)
        chapters, tasks, skipped = plan.chapters, plan.tasks, plan.skipped
        note = _plan_note(plan)
        if not tasks:
            if job_id:
                message = f"{skipped} 行已是最新，无需生成。" if skipped else note
                if skipped and note:
                    message = f"{skipped} 行已是最新，无需生成；{note}"
                self.svc.store.update_job(job_id, message=message)
            return self.svc.detail(project_id)
        # 按音色分块排队：sort 是稳定的，块内仍是章序 / 行序。
        tasks.sort(key=_voice_group)
        if job_id:
            message = f"待生成 {len(tasks)} 行，跳过 {skipped} 行"
            self.svc.store.update_job(
                job_id, total=len(tasks), message=f"{message}；{note}" if note else message
            )

        # 并发**按引擎**算，不是一刀切（规则见 :func:`_engine_limits`）。
        from app.usecase import engines as engines_uc

        settings_limit = max(1, int(self.svc.settings.concurrency))
        limits = _engine_limits(
            tasks,
            engines_uc.active_engines(),
            settings_limit,
            engines_uc.settings_for,
        )
        semaphores = {key_of: asyncio.Semaphore(value) for key_of, value in limits.items()}
        # 引擎各按自己的额度；总闸只压住多引擎叠加，不盖住单引擎额度。
        global_sem = asyncio.Semaphore(_global_limit(limits, settings_limit))
        # 兜底那个只建一次：每行新建一个就等于没限制。
        fallback = asyncio.Semaphore(settings_limit)
        # 行的情绪发不发：与 ``_audio_plan`` 同一把尺子（用户词表的生效值）。
        words = set(emotion_words())
        root = self.svc.project_folder(project_id)
        # 章目录只建一次：每行 ``mkdir(exist_ok=True)`` 也是一次系统调用，一万行白花。
        folders: dict[str, Path] = {}
        for chapter, *_rest in tasks:
            chapter_id = str(chapter["id"])
            if chapter_id in folders:
                continue
            folder = root / "audio" / chapter_id
            folder.mkdir(parents=True, exist_ok=True)
            folders[chapter_id] = folder
        # 进度别每行写一次库：一万五千行就是三万个 UPDATE，SQLite 的写锁全被它占着。
        # 攒在内存里定期刷，收尾必刷——测试与界面看到的最终数仍是一分不差。
        pending = {"done": 0, "failed": 0}
        flushed = [0.0]

        def flush_progress(force: bool = False) -> None:
            if not job_id or not (pending["done"] or pending["failed"]):
                return
            now = time.monotonic()
            if not force and now - flushed[0] < _PROGRESS_FLUSH_S:
                return
            done, failed = pending["done"], pending["failed"]
            pending["done"] = pending["failed"] = 0
            flushed[0] = now
            self.svc.store.bump_job(job_id, done=done, failed=failed)

        # 取消也别每行问两次库：0.2 秒问一次够用，一旦问出「已取消」就记住，
        # 后面的行直接停，不必再查。
        cancel = {"hit": False, "next": 0.0, "seen": 0}

        def cancelled() -> bool:
            if not job_id:
                return False
            if cancel["hit"]:
                return True
            now = time.monotonic()
            cancel["seen"] += 1
            if cancel["seen"] > 1 and now < cancel["next"]:
                return False
            cancel["next"] = now + _CANCEL_POLL_S
            cancel["hit"] = self.svc.store.job_cancelled(job_id)
            return cancel["hit"]

        # 整台做不了就停这一台：服务没起、引擎被停用、插件已卸载——后面每一行都只是
        # 白撞一次，还会把行状态刷成失败、进度刷成满屏错误。值是没生成的行数与原因。
        halted: dict[str, dict[str, Any]] = {}

        def note_failure(chapter: dict[str, Any], row: dict[str, Any], exc: Exception) -> None:
            self.svc.store.set_row_error(row["id"], str(exc))
            pending["failed"] += 1
            flush_progress()
            CHANNEL.warning(
                f"合成失败：{row['id']} · {exc}",
                source="audiobook",
                project=project_id,
                chapter=chapter["id"],
            )

        def halt_engine(chapter: dict[str, Any], engine_key: str, exc: EngineError) -> None:
            """整台做不了时停掉这台引擎，日志只记首行那一条。

            「连不上」与「未启用」同一处置：换哪一行都是同一个结果，逐行报错只是把
            一条原因复制成一万条。这一行也不写 ``row error``：不是这一行的问题。
            剩余行逐个标成失败，用户既找不到原因，重试前还得先把这些错清一遍。
            """
            entry = halted.get(engine_key)
            if entry is None:
                entry = halted[engine_key] = {"reason": exc.reason, "rows": 0}
                CHANNEL.warning(
                    f"引擎不可用，已停止该引擎的剩余行：{exc.reason}",
                    source="audiobook",
                    project=project_id,
                    chapter=chapter["id"],
                    engine=engine_key,
                )
            entry["rows"] += 1

        async def run(
            chapter: dict[str, Any],
            row: dict[str, Any],
            mapping: dict[str, str],
            key: str,
        ) -> None:
            engine_key = str(mapping.get("engine") or "")
            # 这台引擎已判定不在线：连名额都不必等。
            if engine_key in halted:
                halted[engine_key]["rows"] += 1
                return
            # 先按引擎额度排队，再受总闸限制：反过来会让等引擎名额的任务占着总闸。
            async with semaphores.get(engine_key) or fallback, global_sem:
                # 拿到名额后再问一次：排队这段时间里点的「取消」也要算数。
                if cancelled():
                    return
                # 排队期间可能已有别的行撞上「连不上」。
                if engine_key in halted:
                    halted[engine_key]["rows"] += 1
                    return
                try:
                    synthesized = await self._synth_row(
                        project_id,
                        root,
                        folders[str(chapter["id"])],
                        chapter,
                        row,
                        mapping,
                        key,
                        library,
                        words,
                    )
                except EngineError as exc:
                    if exc.code in CODES_ENGINE_UNAVAILABLE:
                        halt_engine(chapter, engine_key, exc)
                    else:
                        note_failure(chapter, row, exc)
                except Exception as exc:  # noqa: BLE001 - 单行失败不拖垮整批
                    note_failure(chapter, row, exc)
                else:
                    # 没抢到这一行（另一个 worker 在做）不算本批的进度。
                    if synthesized:
                        pending["done"] += 1
                        flush_progress()

        await asyncio.gather(*(run(*task) for task in tasks))
        flush_progress(force=True)
        self._refresh_audio_stages(project_id)
        stopped = sum(entry["rows"] for entry in halted.values())
        # 停掉的行留在「待生成」：服务起来后点一次生成就能续跑，不必先清错。
        if stopped and job_id and not cancelled():
            self.svc.store.update_job(
                job_id,
                message="；".join(
                    f"{entry['rows']} 行未生成：{entry['reason']}" for entry in halted.values()
                ),
            )
        done = len(tasks)
        CHANNEL.info(
            f"合成结束：{done} 行 · {len(chapters)} 章 · 跳过 {skipped} 行"
            + (f" · 中止 {stopped} 行" if stopped else ""),
            source="audiobook",
            project=project_id,
        )
        return self.svc.detail(project_id)

    async def retry_row(self, project_id: str, row_id: str, library: Any) -> dict[str, Any]:
        """单行重试：音色缺失或引擎拒绝时把原因留在这一行上。"""
        row = self.svc.store.row(row_id)
        chapter = self.svc.store.chapter(project_id, row["chapter_id"]) if row else None
        if not row or not chapter:
            raise ProducerError("这一行不属于该项目。")
        speaker = row["speaker"] or NARRATOR
        chapter_voices = self.svc.chapter_voice_rows(project_id)
        mapping = self.svc.mapping_for(
            row, self.svc.store.voice_map(project_id), chapter_voices
        )
        if not mapping or not mapping.get("voice_id"):
            label = str(chapter.get("title") or speaker) if chapter_voices else speaker
            raise ProducerError(f"「{label}」尚未分配音色。")
        words = set(emotion_words())
        from app.usecase import engines as engines_uc

        engine = engines_uc.detect_engine(str(mapping["engine"]))
        identity = voice_identity(engine) if engine is not None else ""
        key = row_key(row, mapping, words=words, identity=identity, version=2)
        root = self.svc.project_folder(project_id)
        folder = root / "audio" / chapter["id"]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            synthesized = await self._synth_row(
                project_id,
                root,
                folder,
                chapter,
                row,
                mapping,
                key,
                library,
                words,
            )
        except Exception as exc:  # 失败原因写回行上
            self.svc.store.set_row_error(row_id, str(exc))
            raise ProducerError(f"这一行仍未生成：{exc}") from exc
        if not synthesized:
            raise ProducerError("这一行正在被另一个任务生成，等它完成后再试。")
        self._refresh_audio_stages(project_id)
        row = self.svc.store.row(row_id) or {}
        return self.svc._row_view(project_id, row)

    def prepare_audio(self, project_id: str) -> AudioPlan:
        """生成前的只读准备：校验 + 算计划。**慢的都在这里，可以在工作线程里跑。**

        全表扫描、按章列音频目录都是同步阻塞的事。放在事件循环里，这几百毫秒
        到几十秒里所有请求（包括进度轮询）都得排队，界面看着就是"点了没反应"。
        """
        self.svc._ensure_idle(project_id)
        # 计划在 settle 之后算：否则「本可以复活的行」会被当成待生成，白排一遍。
        self.svc._settle_stale(project_id)
        # 全项目的行只取一次：校验空文本与算计划用的是同一份，别读两遍（一万五千行
        # 读一次约 0.17 秒，逐章读是 0.9 秒，读两遍就是白扔一秒多）。
        rows = self.svc.store.all_rows(project_id)
        missing = missing_text(rows)
        if missing:
            raise ProducerError(missing)
        return self._audio_plan(project_id, rows)

    def start_audio(self, project_id: str, library: Any) -> dict[str, Any]:
        """同步入口：准备 + 建任务。API 那边拆成两步，好把准备放进线程。"""
        return self.start_audio_planned(project_id, library, self.prepare_audio(project_id))

    def start_audio_planned(
        self,
        project_id: str,
        library: Any,
        plan: AudioPlan,
    ) -> dict[str, Any]:
        """建任务并开跑。**必须在事件循环线程里调用**：``create_task`` 认当前循环。"""
        # 准备到建任务之间可能被另一个请求抢先，这里再挡一次（只查一次库）。
        self.svc._ensure_idle(project_id)
        skipped = plan.skipped
        note = _plan_note(plan)
        if not plan.tasks:
            if not skipped and not note:
                raise ProducerError("没有可合成的行。")
            job = self.svc.store.create_job(project_id, "audio", 0)
            latest = f"{skipped} 行已是最新。" if skipped else ""
            self.svc.store.update_job(job["id"], state="done", message=f"{latest}{note}")
            return self.svc.store.job(job["id"]) or job
        job = self.svc._start_job(
            project_id,
            "audio",
            len(plan.tasks),
            # 计划直接带进任务：同一份东西算两遍，用户就多等一遍。
            lambda job_id: self.synthesize(project_id, library, job_id, plan),
        )
        if note:
            # 启动时就说清楚跳过了谁：任务跑起来后，这句话会被进度说明顶掉。
            self.svc.store.update_job(job["id"], message=note)
            job = self.svc.store.job(job["id"]) or job
        return job
