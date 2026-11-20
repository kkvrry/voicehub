"""扫描后的音色引用自愈：本地音色 id 跟着目录路径走，目录一移动旧 id 就查不到了。

只重绑**唯一同名、且原引擎仍能用**的本地音色；同名多条或找不到的不动，留给用户
在分配弹框里手选。重绑成功时同时按新 id 重算受影响行的 ``audio_key``——旧音频
文件没变，只是路径换了，不该因此被判成"参数变了"而全部重做。
"""

from __future__ import annotations

from typing import Any

from app.domain.models import SOURCE_LOCAL
from app.infra.loghub import CHANNEL
from app.producers.builtin.audiobook.assist.voice_assign import (
    allowed_engines,
    engine_caps,
)
from app.producers.builtin.audiobook.audio_stage import row_key, voice_identity
from app.producers.builtin.audiobook.rows import emotion_words
from app.producers.builtin.audiobook.rules import NARRATOR
from app.usecase import engines as engines_uc
from app.usecase import pronunciation

__all__ = ["rebind_stale_voices"]


def rebind_stale_voices(store: Any, library: Any) -> dict[str, Any]:
    """按名字自愈失效的本地音色引用，返回 ``{rebound, keys, ambiguous, missing}``。

    :param store: ``AudiobookStore``（读写项目库）。
    :param library: ``LibraryService``（读当前音色库）。
    """
    assignments = store.voice_assignments()
    if not assignments:
        return {"rebound": 0, "keys": 0, "ambiguous": 0, "missing": 0}

    voices = library.list_voices(source=SOURCE_LOCAL)
    by_id = {str(voice.id): voice for voice in voices}
    local_by_name: dict[str, list[Any]] = {}
    for voice in voices:
        name = str(voice.shown_name).strip()
        if not name or voice.hidden:
            continue
        local_by_name.setdefault(name, []).append(voice)

    caps = engine_caps()
    ambiguous = 0
    missing = 0
    rebinds: dict[tuple[str, str], Any] = {}
    for row in assignments:
        voice_id = str(row.get("voice_id") or "")
        # 平台音色的 id 是「平台:音色id」，不走路径；这里只管本地音色。
        if not voice_id or ":" in voice_id or voice_id in by_id:
            continue
        name = str(row.get("voice_name") or "").strip()
        candidates = [
            voice
            for voice in local_by_name.get(name, [])
            if str(row.get("engine") or "") in allowed_engines(voice, caps)
        ]
        if len(candidates) == 1:
            key = (str(row["project_id"]), str(row["speaker"]))
            rebinds[key] = candidates[0]
        elif candidates:
            ambiguous += 1
        else:
            missing += 1
    # 章级旁白音色同样按名字自愈；旧指纹不在这里重算，下一次生成会换成新 id。
    chapter_rebinds: dict[tuple[str, str], Any] = {}
    for row in getattr(store, "chapter_voice_assignments", list)():
        voice_id = str(row.get("voice_id") or "")
        if not voice_id or ":" in voice_id or voice_id in by_id:
            continue
        name = str(row.get("voice_name") or "").strip()
        candidates = [
            voice
            for voice in local_by_name.get(name, [])
            if str(row.get("engine") or "") in allowed_engines(voice, caps)
        ]
        if len(candidates) == 1:
            chapter_rebinds[(str(row["project_id"]), str(row["chapter_id"]))] = candidates[0]
        elif candidates:
            ambiguous += 1
        else:
            missing += 1
    if not rebinds and not chapter_rebinds:
        _log(0, 0, ambiguous, missing)
        return {"rebound": 0, "keys": 0, "ambiguous": ambiguous, "missing": missing}

    active = engines_uc.active_engines()
    words = set(emotion_words())
    revision = pronunciation.revision()
    updates: list[tuple[str, str, str, str]] = []
    keys: list[tuple[str, str]] = []

    for project_id in sorted({pid for pid, _ in rebinds}):
        old_map = store.voice_map(project_id)
        rows = store.all_rows(project_id)
        identities: dict[str, str] = {}
        for (pid, speaker), voice in rebinds.items():
            if pid != project_id:
                continue
            old = old_map.get(speaker)
            if not old or not old.get("voice_id"):
                missing += 1
                continue
            engine = str(old.get("engine") or "")
            engine_plugin = active.get(engine)
            if engine_plugin is None:
                # 引擎已停用：映射先不动，等它回来再重绑，避免把音频键算坏。
                missing += 1
                continue
            identity = identities.get(engine)
            if identity is None:
                identity = voice_identity(engine_plugin)
                identities[engine] = identity
            new_map = {
                **old,
                "voice_id": str(voice.id),
                "voice_name": str(voice.shown_name),
            }
            updates.append(
                (project_id, speaker, str(voice.id), str(voice.shown_name))
            )
            for row in rows:
                if not row.get("audio"):
                    continue
                effective = str(row.get("speaker") or NARRATOR)
                if effective != speaker:
                    continue
                version = int(row.get("audio_key_v") or 1)
                if str(row.get("audio_key") or "") != row_key(
                    row, old, words=words, identity=identity, version=version,
                    revision=revision,
                ):
                    # 旧指纹本来就不对：音频已经过期，交给正常生成流程处理。
                    continue
                keys.append(
                    (
                        row_key(
                            row, new_map, words=words, identity=identity,
                            version=2, revision=revision,
                        ),
                        str(row["id"]),
                    )
                )

    changed = store.rebind_voice_ids(updates)
    chapter_updates = [
        (project_id, chapter_id, str(voice.id), str(voice.shown_name))
        for (project_id, chapter_id), voice in chapter_rebinds.items()
    ]
    if chapter_updates:
        changed += store.rebind_chapter_voice_ids(chapter_updates)
    if keys:
        store.set_row_audio_keys(keys)
    _log(changed, len(keys), ambiguous, missing)
    return {
        "rebound": changed,
        "keys": len(keys),
        "ambiguous": ambiguous,
        "missing": missing,
    }


def _log(rebound: int, keys: int, ambiguous: int, missing: int) -> None:
    if rebound:
        CHANNEL.info(
            f"音色引用自动重绑 {rebound} 条（目录移动；唯一同名且引擎可用）"
            + (f"，同步更新 {keys} 行音频指纹" if keys else ""),
            source="audiobook",
            rebound=rebound,
            keys=keys,
        )
    if ambiguous or missing:
        CHANNEL.warning(
            f"还有 {ambiguous + missing} 条音色引用无法自动重绑"
            f"（同名歧义 {ambiguous}、找不到 {missing}）：到音色分配里手动重选。",
            source="audiobook",
            ambiguous=ambiguous,
            missing=missing,
        )
