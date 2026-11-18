"""网关用例：收到规范化请求后做四件事——解析音色、准备参考音频、按引擎契约校验、路由到插件。

第 2 步由 VoiceHub 统一做（不在插件里）：依据来自每台引擎自己的声明，插件不必为此多写一行。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.domain.credential import DESIGN, PLATFORM, REFERENCE, credential_of
from app.domain.emotions import canonical_emotion, emotion_matches
from app.domain.models import Sample, platform_voice_id
from app.engines import ratelimit
from app.engines.base import (
    CODE_NOT_ENABLED,
    CODE_NOT_FOUND,
    CODE_RATE_LIMITED,
    KIND_LOCAL,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    language_names,
)
from app.engines.registry import detect_engine
from app.engines.traits import detect_language
from app.infra.loghub import CHANNEL
from app.infra.requestctx import current_request_id
from app.usecase import pronunciation, reference
from app.usecase.engines import active_engines, settings_for
from app.usecase.library import LibraryError, LibraryService

__all__ = ["normalize_voice", "synthesize", "validate_against_spec"]

#: 平台明确回限流后的重试间隔（秒）。限流按分钟计，等满一分钟会拖住整批；
#: 完全不等则是空转——取中间值，且每次失败都让同一引擎一起降温。
_RATE_BACKOFF = (2.0, 6.0)


def _short(value: Any, limit: int = 120) -> Any:
    """日志里的值：长文本截断，其它原样。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _settle_extra(
    engine: EnginePlugin, request: SynthesisRequest
) -> tuple[SynthesisRequest, tuple[tuple[str, str], ...]]:
    """只把引擎声明接受的 ``extra`` 键交给插件；其余剔除并回报。

    白名单来自 :meth:`EnginePlugin.accepted_extra_keys`（映射表 + 动态映射），
    与 ``describe()`` 下发的是同一份。拼错的键、换引擎后残留的私有键在这里出局，
    不再落到插件里静默消失；回报进 ``prep_notes``，HTTP 响应会带出去。
    """
    allowed = engine.accepted_extra_keys()
    dropped = sorted(key for key in request.extra if key not in allowed)
    if not dropped:
        return request, ()
    kept = {key: value for key, value in request.extra.items() if key in allowed}
    names = "、".join(dropped)
    hint = f"可用参数：{'、'.join(sorted(allowed))}。" if allowed else "这台引擎没有额外的参数。"
    CHANNEL.warning(
        f"忽略参数：{engine.key} 不接受 {names}。",
        source="gateway",
        engine=engine.key,
        request_id=current_request_id(),
    )
    return replace(request, extra=kept), (
        ("parameter_ignored", f"{engine.label} 不接受参数 {names}，已忽略；{hint}"),
    )


def _settle_language(
    engine: EnginePlugin, request: SynthesisRequest
) -> tuple[SynthesisRequest, tuple[tuple[str, str], ...]]:
    """语言不在引擎声明里时剔掉那一格，按「不指定」发送。

    落点存在不等于支持每一种取值：OmniVoice 收 ``language``，但服务端只认它
    语言表里的代码，``cmn-xa`` 这类统一词表里的方言点会直接 500。与其让整批
    合成撞在服务端，不如按声明先剔掉——引擎自己会按正文判断，少一格提示比
    整段失败更接近用户想要的结果。

    只有统一词表认得出的代码才判：``th`` 这类词表外代码交给插件，600+ 语言的
    引擎可能认得它，核心不替它关门。
    """
    notes: list[tuple[str, str]] = []
    extra = dict(request.extra)
    for key, feature in (("language", "正文语言"), ("reference_language", "参考语言")):
        raw = str(extra.get(key) or "").strip()
        if not raw:
            continue
        code = detect_language(raw)
        if not code or engine.supports_language(code):
            continue
        extra.pop(key, None)
        label = language_names((code,))[0]
        notes.append(
            (
                "language_ignored",
                f"{engine.label} 不支持{feature}「{label}」，按不指定发送。",
            )
        )
        CHANNEL.debug(
            f"{feature}未发送：{engine.key} 不支持「{label}」。",
            source="gateway",
            engine=engine.key,
            request_id=current_request_id(),
        )
    if not notes:
        return request, ()
    return replace(request, extra=extra), tuple(notes)


#: 引导指令的旧写法 → 规范名。``speaker`` 是旧版字段名，``instruct`` 是映射表里的
#: 落点名。归一在网关做一次，插件只认 ``instruction``：从前这份别名表在各插件里各写
#: 一份，认三个 / 认两个 / 一个都不认都有——漏认的那台就是"给了指令却静默收不到"。
_INSTRUCTION_ALIASES = ("speaker", "instruct")


def _canonical_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """旧写法并到 ``instruction`` 上：正名有值就以正名为准，空值不顶掉有值的。"""
    values = dict(extra or {})
    for alias in _INSTRUCTION_ALIASES:
        raw = values.pop(alias, None)
        given = str(values.get("instruction") or "").strip()
        if isinstance(raw, str) and raw.strip() and not given:
            values["instruction"] = raw
    return values


def _instruction_text(request: SynthesisRequest) -> str:
    """这一请求带的引导指令（已归一成 ``instruction``）；没带返回空串。"""
    value = request.extra.get("instruction")
    return value.strip() if isinstance(value, str) else ""


def _settle_expression(engine: EnginePlugin, request: SynthesisRequest) -> SynthesisRequest:
    """情绪与引导指令都认的引擎，一次只说一路：带引导指令就不发情绪。

    两路风格同时下给引擎，引擎各让一半，得到的是两者折中的读法——听感上"说不上
    哪里不对，就是不自然"。引导指令更具体、信息量更大（怎么说、快慢、停顿），
    所以有指令就以指令为准，情绪这一格撤掉；没指令时情绪照发，两格并不冲突。

    撤掉比发空串干净：空串在有的平台上是"无效枚举"，会被判成参数错。
    """
    if not request.emotion or not engine.dual_expression():
        return request
    if not _instruction_text(request):
        return request
    CHANNEL.info(
        "情绪与引导指令同时给了：本引擎两路各有落点，只发引导指令，情绪不发送。",
        source="gateway",
        engine=engine.key,
        request_id=current_request_id(),
    )
    return replace(request, emotion="")


def _pick_sample(
    voice: Any,
    *,
    emotion: str,
    sample_file: str = "",
    match_emotion: bool = False,
) -> Sample | None:
    """这一条请求用哪个样例：指定样例 → 情绪匹配 → 主样例。

    - ``sample_file``：用户在这个角色上钉死的那条；文件不在了就往下走，不报错。
    - ``match_emotion``：按这一行的情绪找样例，同义写法先归一（兴奋 = 开心）。
      认不出的情绪、没有对应样例，一律回落主样例——"没匹配"不是失败。
    """
    samples = list(voice.samples or [])
    if sample_file:
        hit = next((s for s in samples if s.file == sample_file), None)
        if hit is not None:
            return hit
    target = canonical_emotion(emotion) if match_emotion else ""
    if target:
        for sample in samples:
            # 一条样例可以标几种情绪（「严肃、轻蔑」）：命中任意一种都算。
            if emotion_matches(sample.emotion, target):
                return sample
    # 没有主样例（或主样例不在列表里）时取第一条：有音频总比"有样例却说没有"好。
    fallback = samples[0] if samples else None
    return next((s for s in samples if s.file == voice.main_sample), fallback)


def _library_request(
    voice: Any,
    *,
    emotion: str,
    engine_key: str = "",
    sample_file: str = "",
    match_emotion: bool = False,
) -> SynthesisRequest:
    """库里那条音色 → 规范化请求。

    带什么由 :func:`app.domain.credential.credential_of` 判，这里只负责把那种凭据
    取出来：设计取描述，参考取一段音频，平台取音色 id。落不到这台引擎时当场说清，
    不把别人的音色 id 发出去换一句平台侧的"音色不存在"。
    """
    cred = credential_of(voice, engine_key=engine_key)
    if cred == DESIGN:
        return SynthesisRequest(
            text="",
            voice_id=voice.shown_name,
            emotion=emotion,
            extra={"voice_design": str((voice.payload or {}).get("design_prompt") or "").strip()},
        )
    if cred == REFERENCE:
        chosen = _pick_sample(
            voice, emotion=emotion, sample_file=sample_file, match_emotion=match_emotion
        )
        if chosen is not None:
            return SynthesisRequest(
                text="",
                voice_id=voice.shown_name,
                reference_audio=str(Path(voice.folder) / chosen.file),
                reference_text=chosen.text,
                emotion=emotion,
            )
        # 复刻凭据：平台没保存这条音色，样本就是它（库里没有样例目录时走这里）。
        ref = str((voice.payload or {}).get("ref_audio") or "").strip()
        if ref:
            return SynthesisRequest(
                text="", voice_id=voice.shown_name, reference_audio=ref, emotion=emotion
            )
        raise EngineError(
            f"「{voice.shown_name}」没有可当参考音频的样例，这次没法复刻。",
            code="voice_not_usable",
        )
    if cred == PLATFORM:
        # 平台音色把命名空间前缀留给引擎剥：发 ``voice.name``（登记时存的原值）。
        return SynthesisRequest(text="", voice_id=voice.name, emotion=emotion)

    # ``NONE``：这条音色落不到这台引擎上，逐种情况说清该怎么改。
    if str((voice.payload or {}).get("design_prompt") or "").strip():
        raise EngineError(
            f"「{voice.shown_name}」是按描述设计的音色，换一台支持「音色设计」的引擎再合成。",
            code="voice_not_usable",
        )
    if voice.is_local:
        raise EngineError(
            f"本地音色「{voice.shown_name}」还没有样例，复刻需要一段参考音频。",
            code="voice_not_usable",
        )
    raise EngineError(
        f"「{voice.shown_name}」是 {voice.source} 的音色，{engine_key} 不认识它的音色 id，"
        "而它没有可当参考音频的样例。换一条带样例的音色，或改用 "
        f"{voice.source} 合成。",
        code="voice_not_usable",
    )


async def _fallback_request(
    service: LibraryService,
    engine: EnginePlugin,
    voice_id: str,
    *,
    emotion: str,
    sample_file: str = "",
    match_emotion: bool = False,
) -> SynthesisRequest | None:
    """库里没有这条音色时再认两次；都认不出返回 ``None``。

    上游常传裸音色名（不带 ``引擎键:`` 前缀）：先按 ``引擎键:名字`` 查库，
    再问一次引擎的实时清单（仅限有清单端点的引擎）。本地音色不参与回落。
    """
    bare = voice_id.split(":", 1)[1].strip() if ":" in voice_id else voice_id.strip()
    if bare:
        try:
            # 同 :func:`_route`：`get_voice` 是同步 IO，别占事件循环。
            voice = await asyncio.to_thread(service.get_voice, platform_voice_id(engine.key, bare))
        except LibraryError:
            voice = None
        if voice is not None and not voice.is_local:
            return _library_request(
                voice,
                emotion=emotion,
                engine_key=engine.key,
                sample_file=sample_file,
                match_emotion=match_emotion,
            )
        # 只有带清单端点的引擎才走"问实时清单"这条路（按能力判断，与通道无关）。
        if engine.capabilities().voice_listing:
            result = await engine.list_voices(settings_for(engine))
            if any(meta.id == bare for meta in result.items):
                return SynthesisRequest(text="", voice_id=bare, emotion=emotion)
    return None


def normalize_voice(
    service: LibraryService,
    voice_id: str,
    *,
    emotion: str = "",
    engine_key: str = "",
    sample_file: str = "",
    match_emotion: bool = False,
) -> SynthesisRequest:
    """音色 id → 规范化请求的音色部分（text 由调用方填）。

    本地音色：参考音频 = 主样例绝对路径，参考文本 = 那条样例的**说话内容**。
    平台音色：只带发给平台的 id。

    **情绪只认调用方给的那一格**：不拿样例标签补默认。参考音频本身已经带着
    原声的语气与节奏，再加一层情绪标签是替调用方提了他没提的要求，认情绪的引擎
    会照做，效果反而更差（生成页与有声书都按这条走）。
    """
    voice = service.get_voice(voice_id)  # 不存在就在这里说，不带脏数据往下走
    return _library_request(
        voice,
        emotion=emotion,
        engine_key=engine_key,
        sample_file=sample_file,
        match_emotion=match_emotion,
    )


def validate_against_spec(
    engine: EnginePlugin, request: SynthesisRequest, cfg: Any = None
) -> tuple[str, ...]:
    """照引擎声明的契约挑缺料（**返回问题表，空表 = 能发**）。

    :param cfg: 这台引擎当前的配置。有些约束随服务端模型变（GPT-SoVITS 的 v3/v4
        必须有参考文本、v2Pro 可以没有），传了才按实际那一版算。
    """
    spec = engine.synthesis_spec_for(cfg) if cfg is not None else engine.synthesis_spec()
    problems: list[str] = []
    labels = {
        "voice_id": "音色 id",
        "reference_audio": "参考音频",
        "reference_text": "参考文本（样例的说话内容）",
        "emotion": "情绪",
    }
    for name in spec.requires:
        if not getattr(request, name, ""):
            label = labels.get(name, name)
            problems.append(f"{engine.label} 要求提供{label}，本次请求为空。")
    if request.format and spec.formats and request.format not in spec.formats:
        problems.append(
            f"{engine.label} 不支持格式 {request.format}。"
            f"可用的格式：{'、'.join(spec.formats)}。"
        )
    # 字数上限是引擎声明的事，判据只有这一处：单发、试听、生产方都走这里。
    # 生产方另有计划期的一次预检（一次列出所有超长行），这里兜住其余的入口。
    limit = engine.capabilities().max_chars
    if limit and len(request.text) > limit:
        problems.append(
            f"{engine.label} 单次最多 {limit} 字，这一段 {len(request.text)} 字，先拆分再发。"
        )
    return tuple(problems)


async def synthesize(
    service: LibraryService,
    *,
    engine_key: str,
    voice_id: str,
    text: str,
    emotion: str = "",
    format: str = "",
    extra: Mapping[str, Any] | None = None,
    engines: Mapping[str, EnginePlugin] | None = None,
    sample_file: str = "",
    match_emotion: bool = False,
    log_failure: bool = True,
) -> SynthesisResult:
    """规范化 → 校验 → 路由，并统一记录成败（出口只有一处，避免新失败路径漏记）。

    :param log_failure: 失败时是否在这里记一条 warning。批量合成自己带行号记，
        同一件事再记一遍就是两份；单发（试听、单条接口）没有第二层，保持 ``True``。
    """
    started = time.perf_counter()
    try:
        result = await _route(
            service,
            engine_key=engine_key,
            voice_id=voice_id,
            text=text,
            emotion=emotion,
            format=format,
            extra=extra,
            engines=engines,
            sample_file=sample_file,
            match_emotion=match_emotion,
        )
    except EngineError as exc:
        # warning 而非 error：明确拒绝（参数错、音色无）是正常业务，记下供事后排查。
        # 明细始终留一条（批量时降到 debug）：调用方记的那条带行号，但不一定带引擎/音色。
        line = f"合成未完成：{exc}"
        fields = {
            "source": "gateway",
            "engine": engine_key,
            "voice": voice_id,
            "request_id": current_request_id(),
        }
        if log_failure:
            CHANNEL.warning(line, **fields)
        else:
            CHANNEL.debug(line, **fields)
        raise
    CHANNEL.info(
        f"合成完成：{engine_key} / {voice_id} / {len(text)} 字 / "
        f"{time.perf_counter() - started:.2f}s",
        source="gateway",
        engine=engine_key,
        voice=voice_id,
        chars=len(text),
        request_id=current_request_id(),
    )
    return result


async def _route(
    service: LibraryService,
    *,
    engine_key: str,
    voice_id: str,
    text: str,
    emotion: str = "",
    format: str = "",
    extra: Mapping[str, Any] | None = None,
    engines: Mapping[str, EnginePlugin] | None = None,
    sample_file: str = "",
    match_emotion: bool = False,
) -> SynthesisResult:
    """规范化 → 校验 → 路由；成败由 :func:`synthesize` 记，这里只记一条入参形状的 debug。"""
    CHANNEL.debug(
        f"合成请求：{engine_key} / {voice_id} / {len(text)} 字 / 附加参数 {sorted(extra or {})}",
        source="gateway",
        engine=engine_key,
        chars=len(text),
        extra_keys=sorted(extra or {}),
        request_id=current_request_id(),
    )
    text = text.strip()
    if not text:
        raise EngineError("正文不能为空。", code="empty_text")
    text, replaced = await asyncio.to_thread(pronunciation.apply, text)
    if replaced:
        CHANNEL.info(
            f"读音替换：{'、'.join(replaced)}",
            source="gateway",
            engine=engine_key,
            voice=voice_id,
        )
    # 默认只从**已启用**的引擎里取：停用之后不该还能被调用。测试可以直接注入
    # ``engines`` 覆盖这一层（见 tests/test_gateway.py）。
    available = engines if engines is not None else active_engines()
    engine = available.get(engine_key.strip())
    if engine is None:
        # 适配了但没启用，与"根本没有这台"要分开说——前者一步就能解决。
        known = detect_engine(engine_key) if engines is None else None
        if known is not None:
            raise EngineError(
                f"引擎 {known.label} 未启用。在「引擎」页把它打开后再用。",
                code=CODE_NOT_ENABLED,
            )
        raise EngineError(
            f"引擎 {engine_key} 不存在。可用引擎：{'、'.join(sorted(available)) or '（无）'}。",
            code=CODE_NOT_FOUND,
        )
    # 语气词标签（``[[倒吸气]]`` / ``[[停顿:0.4]]``）：认的换成这一家的写法，不认的从
    # 正文里删掉——留着必被当成字念出来。闸口只此一处：交给插件各自记得调，忘一次
    # 就是标签被朗读（IndexTTS2 实测），而新加的插件根本不会知道有这一步。
    text = engine.prepare_text(text)
    if not text.strip():
        raise EngineError(
            f"正文里只有语气词标签，{engine.label} 不认这些标签，删掉之后没有可念的字。",
            code="empty_text",
        )
    try:
        # `get_voice` 会读 SQLite、扫引擎保存目录、缺时长时还会起 ffprobe——全是同步活。
        # 直接在事件循环里做，一个慢盘 / 一个 ffprobe 就能把所有并发请求（含日志 SSE）拖住。
        request = await asyncio.to_thread(
            normalize_voice,
            service,
            voice_id,
            emotion=emotion,
            engine_key=engine.key,
            sample_file=sample_file,
            match_emotion=match_emotion,
        )
    except LibraryError as exc:
        request = await _fallback_request(
            service,
            engine,
            voice_id,
            emotion=emotion,
            sample_file=sample_file,
            match_emotion=match_emotion,
        )
        if request is None:
            raise EngineError(str(exc)) from exc
    request = SynthesisRequest(
        text=text,
        voice_id=request.voice_id,
        reference_audio=request.reference_audio,
        reference_text=request.reference_text,
        emotion=request.emotion,
        # 原样传调用方说的格式，空就是空："没指定"与"指定了 wav"是两回事，
        # 补默认值会让引擎配置里的默认格式永远轮不上。
        format=format,
        # 引擎特有参数原样下走，怎么落进请求体是那台引擎映射表的事。
        # 音色解析带回来的凭据（如设计音色的描述）先放，调用方显式给的优先。
        # 引导指令的旧写法在这里并成规范名：插件只认 ``instruction``。
        extra=_canonical_extra({**dict(request.extra or {}), **dict(extra or {})}),
    )
    # 用这台引擎真实的配置（密钥等），不用空模型。**提前取**：参考音频的准备
    # 也要看配置——比如 gpt_sovits 换到 v2Pro 后就不再要求参考文本。
    cfg = settings_for(engine)
    # 两路风格只说一路：这一步要在下面那行「发送参数」之前，日志才是真发出去的样子。
    request = _settle_expression(engine, request)
    # 不支持的正文/参考语言按「不指定」发：先剔掉，插件就不会把它翻进请求体。
    request, language_notes = _settle_language(engine, request)
    # 准备参考音频（转格式 / 截取 / 补逐字稿）是慢操作，丢进线程跑，避免卡住事件循环。
    request, prep_notes = await asyncio.to_thread(
        reference.prepare, service, engine, request, library_voice_id=voice_id, cfg=cfg
    )
    # 引擎只收到它声明过的扩展参数：白名单与 describe() 下发的是同一份。
    request, extra_notes = _settle_extra(engine, request)
    prep_notes = (*language_notes, *prep_notes, *extra_notes)
    # 实际发出去的参数（统一形状）：排查"我调了它没生效"时，第一个要看的就是这一行。
    CHANNEL.info(
        "发送参数："
        + json.dumps(
            {
                "engine": engine_key,
                "voice": request.voice_id,
                "text": _short(text),
                "reference": Path(request.reference_audio).name if request.reference_audio else "",
                "reference_text": _short(request.reference_text),
                "emotion": request.emotion,
                "extra": {key: _short(value) for key, value in request.extra.items()},
            },
            ensure_ascii=False,
        ),
        source="gateway",
        engine=engine_key,
    )
    problems = validate_against_spec(engine, request, cfg)
    if problems:
        raise EngineError(" ".join(problems))
    # 用这台引擎真实的配置（密钥等），不用空模型。
    result = await _call_engine(engine, cfg, request)
    # 准备步骤的改动（转码、补逐字稿）作为 prep_notes 说给用户，与引擎自己的 notes 分开。
    if prep_notes:
        result = replace(result, prep_notes=prep_notes)
    return result


async def _call_engine(
    engine: EnginePlugin, cfg: Any, request: SynthesisRequest
) -> SynthesisResult:
    """发一次合成：先过限速闸，限流或本机引擎暂时忙时退避重试。

    闸口按引擎设置里的 ``rpm`` 与 ``concurrency`` 建，跨任务、跨项目共享——
    生产方的信号量只管一个任务内的行，两个项目并行时各自放行，加起来就超了平台额度。
    被限流时不立刻把这一行判死：让同一引擎降温后再试，仍失败才交给调用方记错。

    ``retryable`` 只对**本机**引擎消费：本地服务说"忙"是会过去的状态；
    云端 503 不自动重发，避免边界情况下重复计费。
    """
    for delay in (*_RATE_BACKOFF, None):
        try:
            # 额度取「设置值」与「引擎声明的硬上限」的较小者：本机单模型服务
            # 声明 1 就得真的只跑一条，试听与第二个项目也不该绕过去。
            async with ratelimit.permit(
                engine.key, cfg, concurrency=ratelimit.limit_for(engine, cfg)
            ):
                return await engine.synthesize(cfg, request)
        except EngineError as exc:
            if delay is None:
                raise
            if exc.code == CODE_RATE_LIMITED:
                what = "平台限流"
            elif exc.retryable and engine.capabilities().kind == KIND_LOCAL:
                what = "本机引擎暂时忙"
            else:
                raise
        ratelimit.penalize(engine.key, cfg, delay)
        CHANNEL.warning(
            f"{what}：{engine.key} 暂停 {delay:g}s 后重试。",
            source="gateway",
            engine=engine.key,
            request_id=current_request_id(),
        )
    raise AssertionError("unreachable")
