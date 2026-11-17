"""AI 分配音色：补未指定的角色，也支持只调整某几个已有音色的角色。

两段式：先让对话模型只凭书名（带作者）与角色名写一份**声音画像**
（``assist/voice_profile``，不发对白、不发候选；认不准的角色可以联网查证），
再用这份画像去全库做向量检索，取出几十条最接近的候选交回模型裁决。所以模型看到的候选表是按角色筛过的
短名单，不是全库前 200 条。

引擎不交给模型：候选先按「这台引擎能不能用这条音色」筛好，模型只选
音色编号；落哪台引擎由服务端按项目已有分配的主引擎推断，再退回第一台
可用引擎。应用前会重新校验角色仍在、音色仍在库里。

``body["speakers"]`` 点了名就只处理这几个角色（可以含已指定音色的），
用于弹窗里的「调整」与单角色匹配；不点名时保持老行为——只补未指定的。

``choices`` 里 ``mode=design`` 的角色不需要从候选里挑：音色就是一句描述。
用户填了描述就照用户的；没填就让模型在同一次回复里直接写最终描述
（``assignments[].design``），清洗后原样落库，不再另发一次改写请求。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.channel import VoiceChannel
from app.domain.credential import DESIGN, PLATFORM, REFERENCE, credential_of
from app.embedding import service as embedding_uc
from app.llm.base import ChatMessage
from app.producers.builtin.audiobook import ai_prompts
from app.producers.builtin.audiobook.assist import voice_profile, voice_recall
from app.usecase import engines as engines_uc
from app.usecase import voice_design
from app.usecase.assist import AssistContext, AssistError, AssistInvalid

__all__ = [
    "VoiceAssignTask",
    "allowed_engines",
    "engine_caps",
    "resolve_voice",
    "voice_options",
]

#: 画像 + 向量召回之后，提示词里的候选上限。每一行都带模型能读的标签，
#: 40 条够覆盖每个角色若干个合理选项，又不会让结论被无关音色稀释。
_MAX_CANDIDATES = 40
#: 没有向量模型时只能按名字顺序取：收回 40 条会直接丢掉大部分库，所以保持原上限。
_MAX_CANDIDATES_PLAIN = 200


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    voice: Any
    engines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Prepared:
    project_id: str
    project_name: str
    roles: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    candidate_map: dict[str, Candidate]
    voice_by_id: dict[str, Any]
    role_map: dict[str, dict[str, Any]]
    unassigned: tuple[str, ...]
    existing: dict[str, dict[str, str]]
    dominant_engine: str
    engine_labels: dict[str, str]
    warnings: tuple[str, ...]
    #: 过了引擎过滤的完整候选池（音色, 可用引擎）；提示词只装前 N 条。
    pool: tuple[tuple[Any, tuple[str, ...]], ...] = ()
    #: 书目信息里的作者：同名书很多，画像这一步要靠它认人。
    project_author: str = ""
    #: 角色名 → 声音画像（见 ``voice_profile``）；空表示模型没认出来。
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic: bool = False
    #: 本次参与分配的角色：默认是未指定的，点名时可能是已指定音色的（调整）。
    scope: tuple[str, ...] = ()
    #: 每个角色指定的引擎与方式（``speaker -> {engine, mode, design_prompt}``）。
    #: 空表 = 老请求，仍按「主引擎 + 全库候选」推断。
    prefs: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 角色名 → 该角色可选音色的 id；不在表里 = 不限。召回换了一批候选后，
    #: 提示词编号按这张表重算，保证每个角色只在它自己的范围内挑。
    role_allowed: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _pack(
    entries: Sequence[tuple[Any, tuple[str, ...]]], limit: int = _MAX_CANDIDATES
) -> tuple[tuple[dict[str, Any], ...], dict[str, Candidate], dict[str, Any]]:
    """候选池 → 提示词里的编号表。编号每次重新生成，不跨请求复用。"""
    candidate_map: dict[str, Candidate] = {}
    voice_by_id: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    for voice, engines in entries[:limit]:
        key = f"v{len(candidates) + 1}"
        candidate_map[key] = Candidate(key=key, voice=voice, engines=tuple(engines))
        voice_by_id[voice.id] = voice
        candidates.append(
            {
                "key": key,
                "name": voice.shown_name,
                "gender": voice.gender,
                "age": voice.age,
                "language": voice.language,
                "tags": list(voice.tags)[:6],
                "samples": int(voice.sample_count or 0),
            }
        )
    return tuple(candidates), candidate_map, voice_by_id


def _finalize(
    prepared: Prepared,
    entries: Sequence[tuple[Any, tuple[str, ...]]],
    *,
    limit: int,
    semantic: bool = False,
) -> Prepared:
    """候选池 → 提示词编号表，并按音色 id 重算每个角色的可选编号。

    编号是位置排出来的：召回换了一批候选后，旧编号就指向别的音色了。所以
    角色的可选范围只认 ``role_allowed`` 里的 id，每次打包时重新对一遍。
    """
    candidates, candidate_map, voice_by_id = _pack(entries, limit)
    key_by_voice = {cand.voice.id: cand.key for cand in candidate_map.values()}
    roles: list[dict[str, Any]] = []
    for role in prepared.roles:
        allowed = prepared.role_allowed.get(str(role.get("speaker") or ""))
        if allowed is None or "candidates" not in role:
            roles.append(role)
            continue
        roles.append(
            {
                **role,
                "candidates": [key_by_voice[vid] for vid in allowed if vid in key_by_voice],
            }
        )
    return replace(
        prepared,
        candidates=candidates,
        candidate_map=candidate_map,
        voice_by_id=voice_by_id,
        roles=tuple(roles),
        role_map={str(item.get("speaker") or ""): item for item in roles},
        semantic=prepared.semantic or semantic,
    )


def engine_caps() -> dict[str, dict[str, Any]]:
    """**已启用**引擎能不能用：只看就绪与能力，不在这里读具体配置。

    停用的不进这张表：分配弹窗、``allowed_engines`` 与默认引擎都照它算，
    留在表里就会给出"选了就失败"的候选。

    ``default_mode`` 由通道推出来（``own`` → 预置、``library`` → 克隆），不在这里
    按 kind 再写一段同构的分支。
    """
    out: dict[str, dict[str, Any]] = {}
    for key, engine in engines_uc.active_engines().items():
        try:
            cfg = engines_uc.settings_for(engine)
            caps = engine.capabilities()
            channel = engines_uc.channel_of(engine)
            modes: list[str] = []
            if caps.voice_listing:
                modes.append("preset")
            if caps.reference_audio:
                modes.append("clone")
            if caps.voice_design:
                modes.append("design")
            out[key] = {
                "label": engine.label,
                "ready": not engine.readiness(cfg),
                "reference": bool(caps.reference_audio),
                "kind": caps.kind,
                "listing": bool(caps.voice_listing),
                "design": bool(caps.voice_design),
                "modes": modes,
                "default_mode": _default_of(channel, modes),
            }
        except Exception:  # noqa: BLE001, S112 - 一台坏插件不该让整个分配不可用
            continue
    return out


def _default_of(channel: VoiceChannel, modes: Sequence[str]) -> str:
    """未指定方式时的默认：自有音色通道给预置、库供给给克隆，其余取第一个可用方式。"""
    for mode, on in (("preset", channel.is_own), ("clone", channel.is_library)):
        if on and mode in modes:
            return mode
    return modes[0] if modes else ""


def _modes_of(item: Mapping[str, Any]) -> tuple[str, ...]:
    """这台引擎可用的方式；旧配置只有能力布尔值时按同一套规则推。"""
    modes = item.get("modes")
    if isinstance(modes, (list, tuple)) and modes:
        return tuple(str(mode) for mode in modes)
    out: list[str] = []
    if item.get("listing"):
        out.append("preset")
    if item.get("reference"):
        out.append("clone")
    if item.get("design"):
        out.append("design")
    return tuple(out)


def _default_engine(
    info: Mapping[str, Mapping[str, Any]], mode: str = ""
) -> str:
    """未指定引擎时：第一台就绪的本地引擎；没有本地才用云端。

    ``mode`` 指定时只在支持该方式的引擎里挑——"自动引擎 + 音色设计"应落到
    支持设计的那台，而不是先选本地再把方式悄悄改掉。
    """
    for key, item in info.items():
        if (
            item.get("ready")
            and item.get("kind") == "local"
            and (not mode or mode in _modes_of(item))
        ):
            return key
    for key, item in info.items():
        if item.get("ready") and (not mode or mode in _modes_of(item)):
            return key
    for key, item in info.items():
        if item.get("ready"):
            return key
    return ""


def _default_mode(engine: str, info: Mapping[str, Mapping[str, Any]]) -> str:
    """未指定方式时取 ``engine_caps`` 算好的那个，不再按 kind / 清单重推一遍。"""
    item = info.get(engine) or {}
    declared = str(item.get("default_mode") or "").strip()
    if declared:
        return declared
    modes = _modes_of(item)
    return modes[0] if modes else "clone"


def _voice_rows(
    library: Any, info: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """手动挑选用的音色清单：每条带上「能落到哪些引擎」。

    前端只按这份清单筛，不在界面里重新推一遍「哪条音色能配哪台引擎」：
    同一个判断错在两个地方，界面上就会出现点了就失败的选项。
    """
    rows: list[dict[str, Any]] = []
    for voice in library.list_voices():
        if voice.hidden or not voice.shown_name:
            continue
        engines = allowed_engines(voice, info)
        if not engines:
            continue
        design_prompt = str((voice.payload or {}).get("design_prompt") or "").strip()
        rows.append(
            {
                "id": voice.id,
                "name": voice.shown_name,
                "source": str(voice.source or ""),
                "local": bool(voice.is_local),
                "samples": int(voice.sample_count or 0),
                "gender": voice.gender,
                "age": voice.age,
                "language": voice.language,
                "tags": list(voice.tags)[:4],
                "design": bool(design_prompt),
                # 设计音色的描述就是它的凭据：带上原话，调整时能原样填回输入框。
                "design_prompt": design_prompt,
                "engines": engines,
            }
        )
    rows.sort(key=lambda row: (row["design"], row["samples"] == 0, row["name"]))
    return rows


def voice_options(library: Any = None) -> dict[str, Any]:
    """分配弹窗可选的引擎、方式与音色；默认值在这里定，前端不写规则。

    ``library`` 给得出来时顺带下发音色清单（手动改音色用），没给就只有
    引擎与方式——老调用不受影响。
    """
    info = engine_caps()
    engines: list[dict[str, Any]] = []
    for key, item in info.items():
        if not item.get("ready"):
            continue
        engines.append(
            {
                "key": key,
                "label": str(item.get("label") or key),
                "kind": str(item.get("kind") or ""),
                "modes": list(_modes_of(item)),
                "default_mode": _default_mode(key, info),
            }
        )
    default_engine = _default_engine(info)
    payload: dict[str, Any] = {
        "engines": engines,
        "default_engine": default_engine,
        "default_mode": _default_mode(default_engine, info) if default_engine else "",
    }
    if library is not None:
        payload["voices"] = _voice_rows(library, info)
    return payload


def allowed_engines(
    voice: Any, info: Mapping[str, Mapping[str, Any]] | None = None
) -> list[str]:
    """这条音色能被哪些**就绪**引擎使用。

    凭据由 :func:`app.domain.credential.credential_of` 判（与网关同一个函数），
    这里只把凭据映射到能力上，再滤掉没配好的引擎——不重写规则：两边各推一遍，
    界面就会给出"选了就失败"的选项。
    """
    caps = info or engine_caps()
    out: list[str] = []
    for key, item in caps.items():
        if not item.get("ready"):
            continue
        credential = credential_of(voice, engine_key=key)
        if credential == REFERENCE:
            usable = bool(item.get("reference"))
        elif credential == DESIGN:
            usable = bool(item.get("design"))
        elif credential == PLATFORM:
            usable = key == str(voice.source or "")  # 预置音色只回自己的平台
        else:
            usable = False
        if usable:
            out.append(key)
    return out


def resolve_voice(
    library: Any,
    voice_id: str,
    engine: str,
    *,
    info: Mapping[str, Mapping[str, Any]] | None = None,
) -> Any:
    """应用前复核：音色还在，且确实能落到这台引擎上。"""
    voice = library.get_voice(voice_id)
    if engine not in allowed_engines(voice, info):
        raise AssistError(f"音色「{voice.shown_name}」不能用在引擎 {engine} 上。")
    return voice


def _scope_of(
    raw: Any, by_name: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """``speakers`` 点名了哪些角色。

    没点名（缺省 / 空表 / 不是数组）返回 ``None``，调用方沿用「只补空的」；
    点了名但一个都对不上返回空表，让调用方报「角色不在这本书里」而不是
    悄悄退回旧行为。
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    names = [str(item).strip() for item in raw if str(item).strip()]
    if not names:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        item = by_name.get(name)
        if item is None or name in seen:
            continue
        seen.add(name)
        out.append(item)
    return out


def _resolve_scope(
    service: Any, project_id: str, body: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """项目、未指定音色的角色，以及这一次真正要处理的角色。

    点名 = 调整：点到的角色可以是已经有音色的。不点名就仍只补空的；
    点了名却一个都对不上要报错，不能悄悄退回旧行为。
    """
    project = service.store.project(project_id)
    if not project:
        raise AssistError("项目不存在。")
    speakers = service.store.speakers(project_id)
    if not speakers:
        raise AssistError("这本书还没有角色：先完成对白分析。")
    by_name = {str(item["speaker"]): item for item in speakers}
    unassigned = [
        item for item in speakers if not str(item.get("voice_id") or "").strip()
    ]
    picked = _scope_of(body.get("speakers"), by_name)
    if picked is None:
        if not unassigned:
            raise AssistError("所有角色都已指定音色。")
        return project, unassigned, unassigned
    if not picked:
        raise AssistError("指定的角色不在这本书里：先完成对白分析。")
    return project, unassigned, picked


def _dominant_engine(
    existing: Mapping[str, Mapping[str, Any]], info: Mapping[str, Mapping[str, Any]]
) -> str:
    """项目里用得最多的那台引擎；它不可用时留空（下游按「没有主引擎」处理）。"""
    counts = Counter(
        str(item.get("engine") or "") for item in existing.values() if item.get("engine")
    )
    if not counts:
        return ""
    top = counts.most_common(1)[0][0]
    return top if info.get(top, {}).get("ready") else ""


def _candidate_voices(library: Any) -> list[Any]:
    """全库候选：有样例、收藏优先——向量召回不可用时的默认顺序。"""
    voices = [
        voice for voice in library.list_voices() if not voice.hidden and voice.shown_name
    ]
    voices.sort(
        key=lambda voice: (
            int(voice.sample_count or 0) == 0,
            not bool(voice.favorite),
            voice.shown_name,
        )
    )
    return voices


def _normalize_pref(
    speaker: str,
    raw: Mapping[str, Any],
    info: Mapping[str, Mapping[str, Any]],
    warnings: list[str],
) -> dict[str, str]:
    """一个角色指定的引擎与方式；不可用的退回默认，并留一句警告说明改了什么。"""
    engine = str(raw.get("engine") or "").strip()
    requested_mode = str(raw.get("mode") or "").strip()
    if engine not in info or not info[engine].get("ready"):
        if engine:
            warnings.append(f"「{speaker}」指定的引擎不可用，已改用默认引擎。")
        engine = _default_engine(info, requested_mode)
    allowed = _modes_of(info[engine])
    mode = requested_mode
    if mode not in allowed:
        if mode:
            label = str(info[engine].get("label") or engine)
            warnings.append(f"「{speaker}」选的方式在 {label} 上不可用，已改用默认方式。")
        mode = _default_mode(engine, info)
        if mode not in allowed:
            mode = allowed[0] if allowed else "clone"
    return {
        "engine": engine,
        "mode": mode,
        "design_prompt": str(raw.get("design_prompt") or "").strip(),
    }


def _role_voices(
    speaker: str,
    pref: Mapping[str, str],
    voices: Sequence[Any],
    info: Mapping[str, Mapping[str, Any]],
    warnings: list[str],
) -> list[tuple[Any, tuple[str, ...]]]:
    """这个角色在它指定的引擎与方式下能挑哪些音色（空表 + 一句为什么）。"""
    if pref["mode"] == "design":
        return []
    picked: list[tuple[Any, tuple[str, ...]]] = []
    for voice in voices:
        # 设计音色每次按描述重设计，不能当预置或克隆候选
        if (voice.payload or {}).get("design_prompt"):
            continue
        allowed = tuple(allowed_engines(voice, info))
        if pref["engine"] not in allowed:
            continue
        if pref["mode"] == "preset":
            # 预置音色只取这台平台自己的音色，不能拿别家的当预置
            if voice.is_local or voice.source != pref["engine"]:
                continue
        elif int(voice.sample_count or 0) == 0:
            continue
        picked.append((voice, allowed))
    if not picked:
        label = str(info[pref["engine"]].get("label") or pref["engine"])
        warnings.append(
            f"「{speaker}」在 {label} 上没有可用的"
            f"{'预置音色' if pref['mode'] == 'preset' else '参考音频音色'}，"
            "换个引擎或方式。"
        )
    return picked


def _role_prefs(
    scope: Sequence[Mapping[str, Any]],
    choices: Mapping[str, Any],
    info: Mapping[str, Mapping[str, Any]],
    voices: Sequence[Any],
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, list[tuple[Any, tuple[str, ...]]]],
    list[tuple[Any, tuple[str, ...]]],
    list[str],
]:
    """按角色指定的引擎与方式收窄候选，合成一张共用的候选池。

    池是各角色候选的**并集**：同一个编号在不同角色下含义相同，模型才能跨角色复用。
    """
    if not _default_engine(info):
        raise AssistError("还没有可用的 TTS 引擎：先在设置里配置一台。")
    warnings: list[str] = []
    prefs: dict[str, dict[str, str]] = {}
    for item in scope:
        speaker = str(item["speaker"])
        raw = choices.get(speaker)
        prefs[speaker] = _normalize_pref(
            speaker, raw if isinstance(raw, Mapping) else {}, info, warnings
        )

    per_role: dict[str, list[tuple[Any, tuple[str, ...]]]] = {}
    for speaker, pref in prefs.items():
        per_role[speaker] = _role_voices(speaker, pref, voices, info, warnings)

    pool: list[tuple[Any, tuple[str, ...]]] = []
    seen_ids: set[str] = set()
    for speaker in prefs:
        for voice, allowed in per_role[speaker]:
            if voice.id in seen_ids:
                continue
            seen_ids.add(voice.id)
            pool.append((voice, allowed))
    if not pool and any(pref["mode"] != "design" for pref in prefs.values()):
        raise AssistError(
            "；".join(warnings)
            or "按当前选择没有可用音色：换引擎或方式，或改用音色设计。"
        )
    return prefs, per_role, pool, warnings


def _role_entries(
    scope: Sequence[Mapping[str, Any]],
    prefs: Mapping[str, Mapping[str, str]],
    role_candidates: Mapping[str, list[str]],
    existing: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """提示词里的角色条目：名字、行数、可选编号、现有音色。

    对白不进来：模型判断音色靠的是它对这个角色的了解（画像），几句截断的台词
    只能说明说了什么，还会把「该配什么声音」的注意力拉走。

    键的先后会被 ``json.dumps`` 原样写进提示词，别在这里重排。
    """
    roles: list[dict[str, Any]] = []
    for item in scope:
        speaker = str(item["speaker"])
        role: dict[str, Any] = {
            "speaker": speaker,
            "rows": int(item.get("rows") or 0),
        }
        if prefs:
            role["engine"] = prefs[speaker]["engine"]
            role["mode"] = prefs[speaker]["mode"]
            # 设计角色没有编号可挑，别下发空数组：模型看到「candidates: []」
            # 会以为无从下手，转手把角色写进 unmatched。
            if prefs[speaker]["mode"] != "design":
                role["candidates"] = role_candidates.get(speaker, [])
        # 已有音色的角色要告诉模型「现在是什么」，调整时才有参照。
        if speaker in existing:
            role["current"] = dict(existing[speaker])
        roles.append(role)
    return tuple(roles)


def _proposed_by_speaker(raw: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    """模型对每个角色只认第一条（它偶尔会多写），设计描述也从这里取。"""
    proposed: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if isinstance(item, Mapping):
            proposed.setdefault(str(item.get("speaker") or "").strip(), item)
    return proposed


def _design_rows(
    prefs: Mapping[str, Mapping[str, str]],
    allowed: set[str],
    proposed: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """设计音色不需要模型选编号：描述就是凭据。

    **用户填了就用用户的**；没填就用模型在同一次回复里写的那段——所以
    「音色设计」也能一键 AI 分配，不必先手打一段话，也不再补发改写请求。
    模型给的文字只做清洗与截断；什么都没给的不记驳回，留给 unmatched
    统一说明。
    """
    rows: list[dict[str, Any]] = []
    designed: set[str] = set()
    for speaker, pref in prefs.items():
        if pref.get("mode") != "design" or speaker not in allowed:
            continue
        own = str(pref.get("design_prompt") or "").strip()
        item = proposed.get(speaker) or {}
        prompt_text = own or voice_design.clean(str(item.get("design") or ""))
        if not prompt_text:
            continue
        rows.append(
            {
                "speaker": speaker,
                "engine": pref["engine"],
                "voice_id": "",
                "voice_name": "（按描述新建）",
                "mode": "design",
                "design_prompt": prompt_text,
                "reason": (
                    "用户描述"
                    if own
                    else str(item.get("reason") or "按角色设计").strip()[:60]
                ),
                "shared": False,
            }
        )
        designed.add(speaker)
    return rows, designed


def _engine_for(candidate: Candidate, prepared: Prepared, speaker: str) -> str:
    """这一条落哪台引擎：指定过的按指定的，不在候选里的退回第一个可用的。"""
    engines = list(candidate.engines)
    if prepared.prefs:
        engine = str((prepared.prefs.get(speaker) or {}).get("engine") or "")
        return engine if engine in engines else engines[0]
    return (
        prepared.dominant_engine
        if prepared.dominant_engine in engines
        else engines[0]
    )


def _is_shared(
    voice: Any,
    speaker: str,
    existing: Mapping[str, Mapping[str, Any]],
    used: Counter[str],
) -> bool:
    """这颗音色已被本批的别人用上，或书里本来就指定给了别人。"""
    if used[voice.id] > 0:
        return True
    return any(
        name != speaker and str(item.get("voice_id") or "") == voice.id
        for name, item in existing.items()
    )


def _design_role(prepared: Prepared, speaker: str) -> bool:
    """这个角色这次按「音色设计」处理：没有编号可挑，凭据是描述。"""
    return bool(prepared.prefs) and (
        (prepared.prefs.get(speaker) or {}).get("mode") == "design"
    )


def _unmatched_rows(
    prepared: Prepared, data: Mapping[str, Any], allowed: set[str], seen: set[str]
) -> list[dict[str, str]]:
    """没分到音色的角色：模型自己说的原因优先，其余按「没给建议」补一句。"""
    unmatched: list[dict[str, str]] = []
    reported: set[str] = set()
    raw_unmatched = data.get("unmatched")
    if isinstance(raw_unmatched, list):
        for item in raw_unmatched:
            if not isinstance(item, Mapping):
                continue
            speaker = str(item.get("speaker") or "").strip()
            if speaker not in allowed or speaker in seen or speaker in reported:
                continue
            if _design_role(prepared, speaker):
                # 设计角色没有候选表，模型给的往往是它自己的困惑（「候选表为空」），
                # 对用户没有用；交给下面的兜底说清该做什么。
                continue
            unmatched.append(
                {
                    "speaker": speaker,
                    "reason": str(item.get("reason") or "").strip()[:60]
                    or "没有合适的音色",
                }
            )
            reported.add(speaker)
    for speaker in prepared.scope or prepared.unassigned:
        if speaker in seen or speaker in reported:
            continue
        design = _design_role(prepared, speaker)
        unmatched.append(
            {
                "speaker": speaker,
                "reason": (
                    "音色设计缺少描述（可先自己填一句）"
                    if design
                    else "模型没有给出建议"
                ),
            }
        )
        reported.add(speaker)
    return unmatched


class VoiceAssignTask:
    key = "voice_assign"
    label = "AI 分配音色"
    #: 设计角色的最终描述也在这一条回复里；按每个角色最多 300 字留足余量。
    max_tokens = 6000

    def build(self, ctx: AssistContext) -> Prepared:
        service = ctx.service
        project, unassigned, scope = _resolve_scope(
            service, ctx.project_id, ctx.body
        )
        existing = service.store.voice_map(ctx.project_id)
        info = engine_caps()
        dominant_engine = _dominant_engine(existing, info)

        names = [str(item["speaker"]) for item in scope]
        if not any(item.get("ready") for item in info.values()):
            raise AssistError("还没有可用的 TTS 引擎：先在设置里配置一台。")

        voices = _candidate_voices(ctx.library)
        raw_choices = ctx.body.get("choices")
        choices = raw_choices if isinstance(raw_choices, Mapping) else None
        prefs: dict[str, dict[str, str]] = {}
        per_role: dict[str, list[tuple[Any, tuple[str, ...]]]] = {}
        warnings: list[str] = []

        if choices is not None:
            prefs, per_role, pool, warnings = _role_prefs(scope, choices, info, voices)
        else:
            pool = [
                (voice, engines)
                for voice in voices
                if (engines := tuple(allowed_engines(voice, info)))
            ]
            if not pool:
                raise AssistError("音色库里没有可用于当前引擎的音色。")

        role_allowed = (
            {speaker: tuple(voice.id for voice, _ in per_role[speaker]) for speaker in prefs}
            if choices is not None
            else {}
        )
        roles = _role_entries(scope, prefs, {}, existing)
        engines_used = {pref["engine"] for pref in prefs.values()}
        single_engine = next(iter(engines_used)) if len(engines_used) == 1 else ""
        prepared = Prepared(
            project_id=ctx.project_id,
            project_name=str(project.get("name") or "本书"),
            roles=roles,
            candidates=(),
            candidate_map={},
            voice_by_id={},
            role_map={str(item["speaker"]): item for item in roles},
            unassigned=tuple(str(item["speaker"]) for item in unassigned),
            existing=existing,
            dominant_engine=single_engine if prefs else dominant_engine,
            engine_labels={key: str(item.get("label") or key) for key, item in info.items()},
            warnings=tuple(warnings),
            pool=tuple(pool),
            project_author=str(project.get("author") or ""),
            role_allowed=role_allowed,
            scope=tuple(names),
            prefs=prefs,
        )
        return _finalize(prepared, pool, limit=_MAX_CANDIDATES_PLAIN)

    async def enrich(self, ctx: AssistContext, prepared: Prepared) -> Prepared:
        """两段：先取角色画像，再按画像召回候选。两段都能独立退让。"""
        prepared = await self._profile(ctx, prepared)
        return await self._recall(ctx, prepared)

    async def _profile(self, ctx: AssistContext, prepared: Prepared) -> Prepared:
        """让模型只凭书名与角色名写声音画像。

        这一遍不发候选、不发对白：候选要等画像出来才筛得准，对白只会把模型往
        「这句话怎么念」上带。拿不到画像不算失败——退回按角色名匹配。
        """
        if not prepared.roles:
            return prepared
        try:
            profiles, note = await voice_profile.ensure(
                prepared.project_name,
                [
                    (str(role.get("speaker") or ""), int(role.get("rows") or 0))
                    for role in prepared.roles
                ],
                author=prepared.project_author,
                provider=str(getattr(ctx.settings, "analysis_channel", "") or ""),
                model=str(getattr(ctx.settings, "analysis_model", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 画像只是增强，不能拖垮分配
            reason = getattr(exc, "reason", None) or str(exc)
            return replace(
                prepared,
                warnings=(*prepared.warnings, f"角色画像没拿到（{reason}），按角色名匹配。"),
            )

        roles: list[dict[str, Any]] = []
        for role in prepared.roles:
            speaker = str(role.get("speaker") or "")
            line = voice_profile.summary(profiles.get(speaker))
            roles.append({**role, "profile": line} if line else dict(role))
        warnings = (*prepared.warnings, note) if note else prepared.warnings
        return replace(
            prepared,
            roles=tuple(roles),
            role_map={str(item.get("speaker") or ""): item for item in roles},
            profiles=profiles,
            warnings=warnings,
        )

    async def _recall(self, ctx: AssistContext, prepared: Prepared) -> Prepared:
        """向量模型已下载时，把全库按画像收窄成每个角色的一小组候选。

        模型没装/推理失败都退回原来的「有样例、收藏优先」前 200 条：这时候
        只剩名字可比，收回几十条会直接丢掉大部分库。
        """
        if not prepared.pool:
            return prepared
        try:
            provider = embedding_uc.ready_provider()
        except Exception:  # noqa: BLE001 - 向量模型不可用不该影响分配
            provider = None
        if provider is None:
            if len(prepared.pool) <= _MAX_CANDIDATES_PLAIN:
                return prepared
            return replace(
                prepared,
                warnings=(
                    *prepared.warnings,
                    (
                        f"候选音色较多，只取前 {_MAX_CANDIDATES_PLAIN} 条；在「设置 → 语义检索」"
                        "下载本地向量模型后可按角色相似度召回。"
                    ),
                ),
            )

        order = {voice.id: index for index, (voice, _) in enumerate(prepared.pool)}
        queries = [
            voice_recall.role_text(
                str(role.get("speaker") or ""),
                voice_profile.text(prepared.profiles.get(str(role.get("speaker") or ""))),
            )
            for role in prepared.roles
        ]
        # 每个角色只在自己的可选范围内排：指定了引擎的角色不能拿到另一台引擎的音色。
        masks = []
        for role in prepared.roles:
            allowed = prepared.role_allowed.get(str(role.get("speaker") or ""))
            if allowed is None:
                masks.append(None)
                continue
            mask = [False] * len(prepared.pool)
            for voice_id in allowed:
                if voice_id in order:
                    mask[order[voice_id]] = True
            masks.append(mask)

        try:
            entries = await asyncio.to_thread(
                voice_recall.recall,
                provider,
                prepared.pool,
                queries,
                masks=masks,
                limit=_MAX_CANDIDATES,
            )
        except Exception as exc:  # noqa: BLE001 - 同上，失败只影响召回质量
            reason = getattr(exc, "reason", None) or str(exc)
            return replace(
                prepared,
                warnings=(
                    *prepared.warnings,
                    f"语义召回失败（{reason}），按名字顺序取候选。",
                ),
            )
        if not entries:
            return prepared
        return _finalize(prepared, entries, limit=_MAX_CANDIDATES, semantic=True)

    def messages(self, prepared: Prepared) -> tuple[ChatMessage, ...]:
        roles = json.dumps(prepared.roles, ensure_ascii=False)
        # 全是设计角色时候选表本来就是空的：整段省掉，别让模型看到一个空表
        # 就断定「无从下手」。
        candidates = (
            f"\n候选音色：\n{json.dumps(prepared.candidates, ensure_ascii=False)}"
            if prepared.candidates
            else ""
        )
        rules = [
            (
                "只输出 JSON："
                '{"assignments":[{"speaker":"角色","voice":"v12","design":"","reason":"..."}],'
                '"unmatched":[{"speaker":"角色","reason":"..."}]}'
            ),
            "voice 只能填候选音色表第一列的编号（v1、v2…），不能编造编号或音色名。",
        ]
        if prepared.prefs:
            # 放在第 3 条：设计角色没有编号可挑，先讲清楚，否则模型会当成
            # 「没有可选项」把角色丢进 unmatched。
            rules.append(
                "mode=design 的是音色设计，本来就没有候选编号，这是正常的：voice 留空，"
                "在 design 里直接写最终落库的音色描述（不会再改写），不要写进 unmatched。"
                f"写法：{voice_design.BRIEF_RULES}"
            )
        rules.extend(
            [
                "每个角色最多一条；同一颗音色尽量只给一个角色，候选不足时才复用。",
                (
                    "profile 是这份角色画像（性别、年龄、音色质感、语气、语速）——按它挑，"
                    "候选里的 gender / age / tags / name 就是现有的同类标记。"
                ),
                (
                    "current 是该角色当前的音色（可能没有）：只作参照，不是限制——"
                    "现有音色不合适就换一条，合适也可以再选它（前提是它在候选表里）。"
                ),
                "候选里确实没有合适的音色才写进 unmatched，并给一句原因。",
                "reason 不超过 20 字，写给用户看。",
            ]
        )
        numbered = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1))
        system, template = ai_prompts.assist_messages("voice_assign")
        prompt = (
            template.replace("{book}", prepared.project_name)
            .replace("{rules}", numbered)
            .replace("{roles}", roles)
            .replace("{candidates}", candidates)
        )
        return (ChatMessage("system", system), ChatMessage("user", prompt))

    def validate(self, prepared: Prepared, data: Mapping[str, Any]) -> dict[str, Any]:
        raw = data.get("assignments")
        if not isinstance(raw, list):
            raise AssistInvalid("assignments 必须是数组。")
        allowed = set(prepared.scope or prepared.unassigned)
        prefs = prepared.prefs
        used: Counter[str] = Counter()
        rejected: list[dict[str, str]] = []
        proposed = _proposed_by_speaker(raw)

        # 设计音色先落：它不需要模型选编号，也不会与下面的编号校验冲突。
        assignments, design_done = _design_rows(prefs, allowed, proposed) if prefs else ([], set())
        seen: set[str] = set(design_done)

        for item in raw:
            if not isinstance(item, Mapping):
                rejected.append({"speaker": "", "reason": "建议格式不对"})
                continue
            speaker = str(item.get("speaker") or "").strip()
            key = str(item.get("voice") or "").strip()
            if speaker not in allowed:
                rejected.append({"speaker": speaker, "reason": "角色不在未指定列表里"})
                continue
            if speaker in seen:
                # 设计音色已经成条，模型再给编号也当没看见（不记驳回）。
                if speaker not in design_done:
                    rejected.append(
                        {"speaker": speaker, "reason": "同一个角色出现了多条建议"}
                    )
                continue
            candidate = prepared.candidate_map.get(key)
            if candidate is None:
                rejected.append({"speaker": speaker, "reason": "音色编号不在候选表里"})
                continue
            if prefs:
                role = prepared.role_map.get(speaker) or {}
                if role.get("mode") == "design":
                    continue
                if key not in set(role.get("candidates") or []):
                    rejected.append(
                        {"speaker": speaker, "reason": "音色不在该角色可选范围内"}
                    )
                    continue
            voice = candidate.voice
            shared = _is_shared(voice, speaker, prepared.existing, used)
            used[voice.id] += 1
            assignments.append(
                {
                    "speaker": speaker,
                    "engine": _engine_for(candidate, prepared, speaker),
                    "voice_id": voice.id,
                    "voice_name": voice.shown_name,
                    "mode": str((prefs.get(speaker) or {}).get("mode") or "") if prefs else "",
                    "design_prompt": "",
                    "reason": str(item.get("reason") or "").strip()[:60],
                    "shared": shared,
                }
            )
            seen.add(speaker)

        design_missing = [
            str(item["speaker"])
            for item in prepared.roles
            if _design_role(prepared, str(item["speaker"]))
            and str(item["speaker"]) not in seen
        ]
        if design_missing and not assignments:
            # 设计角色必须有描述：模型一条都没给，等于这次没有提案。回喂一次让它
            # 补上，而不是把「候选表为空」这种自言自语当成结果发给用户。
            raise AssistInvalid(
                "音色设计角色缺少描述："
                + "、".join(design_missing)
                + "。设计音色必须有一句描述，可在描述框里填一句再重试。"
            )

        unmatched = _unmatched_rows(prepared, data, allowed, seen)

        warnings = list(prepared.warnings)
        if any(item["shared"] for item in assignments):
            warnings.append("有角色复用了同一颗音色，应用前确认是否合适。")
        if rejected:
            warnings.append(f"{len(rejected)} 条建议被丢弃（角色或音色编号不在候选内）。")
        return {
            "assignments": assignments,
            "unmatched": unmatched,
            "rejected": rejected,
            "warnings": warnings,
        }

    def preview(self, prepared: Prepared, proposal: Mapping[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for item in proposal.get("assignments") or []:
            voice = prepared.voice_by_id.get(str(item.get("voice_id") or ""))
            speaker = str(item.get("speaker") or "")
            role = prepared.role_map.get(speaker, {})
            rows.append(
                {
                    **item,
                    "rows": int(role.get("rows") or 0),
                    "profile": str(role.get("profile") or ""),
                    "note": str((prepared.profiles.get(speaker) or {}).get("note") or ""),
                    "current": prepared.existing.get(speaker) or None,
                    "engine_label": prepared.engine_labels.get(
                        str(item.get("engine") or ""), str(item.get("engine") or "")
                    ),
                    "voice": {
                        "id": voice.id if voice else "",
                        "name": (
                            voice.shown_name
                            if voice
                            else str(item.get("voice_name") or "（按描述新建）")
                        ),
                        "gender": voice.gender if voice else "",
                        "age": voice.age if voice else "",
                        "language": voice.language if voice else "",
                        "tags": list(voice.tags) if voice else [],
                        "is_local": bool(voice.is_local) if voice else False,
                        "sample_count": int(voice.sample_count or 0) if voice else 0,
                        "favorite": bool(voice.favorite) if voice else False,
                    },
                }
            )
        unmatched = [
            {
                **item,
                "rows": int(
                    prepared.role_map.get(str(item.get("speaker") or ""), {}).get("rows")
                    or 0
                ),
            }
            for item in proposal.get("unmatched") or []
        ]
        return {
            "assignments": rows,
            "unmatched": unmatched,
            "rejected": proposal.get("rejected") or [],
            "warnings": proposal.get("warnings") or [],
            "engine": prepared.dominant_engine,
            "engine_label": prepared.engine_labels.get(prepared.dominant_engine, ""),
            "role_count": len(prepared.roles),
            "candidate_count": len(prepared.candidates),
            "candidate_pool": len(prepared.pool) or len(prepared.candidates),
            "semantic": prepared.semantic,
            "profile_count": sum(1 for role in prepared.roles if role.get("profile")),
        }
