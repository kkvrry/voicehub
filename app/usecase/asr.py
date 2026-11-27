"""识别的编排：**选哪台**、**用它听**、**报状态**。

识别引擎是一个插件类别（见 :mod:`app.engines.asr_base`）：
换模型 = 换一个插件目录，这里一行不用改。本模块只做三件事——

1. 定"现在用哪台"（手动指定优先；未指定时按 key 顺序取当前开着的识别服务）；
2. 把一段音频交给它，并记一条日志（谁、多长、多少字、几秒）；
3. 拼出界面要的状态（每台的说明、当前值、够不够用）。

**这里不再装任何东西**：没有 pip、没有权重下载、没有几 GB 的账本。
"识别模型从哪来"是插件自己的事（HTTP 服务 / 本地推理库 / 云端），
核心不该知道。曾经的进程内 funasr 档位表与安装器已删——理由见
:mod:`app.engines.builtin.qwen3_asr.plugin` 顶部。

**应用方的选择**（选哪台、自动识别、是否写回）与引擎自身的配置分开：
前者是这台服务怎么用识别，存在 ``config.asr_options``；后者属于插件
（``config.engines[key]``），随插件走。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.engines.asr_base import AsrEngine, AsrError, Transcript
from app.engines.registry import list_asr_engines
from app.engines.traits import detect_language
from app.infra.config import load_config, update_config
from app.infra.files import file_stamp
from app.infra.loghub import CHANNEL
from app.usecase.engines import (
    AsrConfigError,
    asr_payload,
    asr_settings_for,
    save_asr_settings_for,
)
from app.usecase.library import LibraryError, LibraryService
from app.usecase.live_models import live_engine_keys

__all__ = [
    "AsrError",
    "AsrOptions",
    "available",
    "cancel_fill",
    "current_key",
    "engine_of",
    "fill_all",
    "fill_missing",
    "fill_state",
    "maybe_fill",
    "options",
    "probe",
    "reachable",
    "save_options",
    "save_settings",
    "set_current",
    "status",
    "transcribe",
    "transcribe_file",
    "transcribe_for_reference",
    "unavailable_reason",
    "write_language",
]

#: 「连接时自动识别」一轮补多少条。识别是串行的（一台服务通常一次只跑一条），
#: 几千条一次跑完要几小时；按批来，下一轮连接接着补，界面上不会长时间挂着任务。
_AUTO_FILL_BATCH = 200

#: 补逐字稿的互斥锁：同一时刻只跑一轮（启动、保存设置、切引擎可能连着触发）。
_fill_lock = threading.Lock()

#: 停止信号：置位后当前这条收尾、不再开始下一条。
_fill_cancel = threading.Event()

#: 进行中那一轮的计数（给界面 toast 轮询）；`:func:`fill_state` 负责读出。
#: ``filling`` 单独记：起了线程不等于有活干（还要等服务起来、还要库里真缺文本）。
_fill_state: dict[str, Any] = {"done": 0, "failed": 0, "total": 0, "filling": False}


class AsrOptions(BaseModel):
    """这台服务怎么用识别——与"用哪台引擎"分开的几件应用层的事。

    :param auto_transcript: 缺逐字稿时自动识别（合成前补文本、生成页主动补样例）。
    :param auto_on_connect: 识别服务连上后，自动把库里还没有逐字稿的样例补一批。
        一轮有上限（:data:`_AUTO_FILL_BATCH`），下次连接继续；已写回的不会重复识别。
    :param writeback: 自动识别的结果写回样例的「说话内容」。
    :param transcribe_recordings: 麦克风录音落盘后顺手转写。
    :param language_writeback: 识别出的语种写回音色（仍守「主样例 + 只填空 + 不覆盖」）。
        只在当前引擎声明 ``returns_language`` 时才有意义；关闭后结果只在识别结果里回显。
    """

    auto_transcript: bool = True
    auto_on_connect: bool = False
    writeback: bool = True
    transcribe_recordings: bool = True
    language_writeback: bool = True


def options() -> AsrOptions:
    """当前应用层选项；坏字段忽略，缺的用默认值（不让一条旧配置卡住识别）。"""
    raw = load_config().asr_options
    known = {name: value for name, value in raw.items() if name in AsrOptions.model_fields}
    try:
        return AsrOptions(**known)
    except ValidationError:
        return AsrOptions()


def save_options(patch: dict[str, Any]) -> dict[str, Any]:
    """改应用层选项（只认识 :class:`AsrOptions` 的字段，其余丢弃）；返回最新状态。"""
    known = {name: value for name, value in (patch or {}).items() if name in AsrOptions.model_fields}
    try:
        model = AsrOptions.model_validate({**options().model_dump(), **known})
    except ValidationError as exc:
        raise AsrError("识别选项不合法。", code="invalid_value") from exc
    dumped = model.model_dump()
    update_config(lambda cfg: setattr(cfg, "asr_options", dumped))
    return status()


# --------------------------------------------------------------------------- #
# 选哪台
# --------------------------------------------------------------------------- #


def current_key() -> str:
    """现在实际会用的那台：手动指定优先，没指定就按 key 顺序选开着的。

    "开着"的判据来自 audio.cpp 的运行实例（一处探测、短缓存）。读不到这份状态
    （插件未启用、服务在外部或别的机器上）就退回配置里那台 / 第一台，不让外部
    服务因此判死。所有识别入口都经这里，不会出现闸与实际识别选中不同一台。
    """
    engines = list_asr_engines()
    if not engines:
        return ""
    configured = load_config().asr_engine.strip()
    if configured:
        return configured if configured in engines else next(iter(engines))
    live = live_engine_keys(engines.keys())
    for key in engines:
        if key in live:
            return key
    return next(iter(engines))


def set_current(key: str) -> dict[str, Any]:
    """选一台。空串 = 回到自动（按 key 顺序选当前开着的识别服务）。"""
    wanted = (key or "").strip()
    if wanted and wanted not in list_asr_engines():
        raise AsrError(
            f"识别引擎 {wanted} 不存在。可用引擎：{'、'.join(sorted(list_asr_engines())) or '（无）'}。",
            code="engine_not_found",
        )
    update_config(lambda cfg: setattr(cfg, "asr_engine", wanted))
    return status()


def save_settings(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """保存某台的配置（掩码 / 恢复默认的语义与合成引擎一致）。"""
    try:
        save_asr_settings_for(engine_of(key), patch)
    except AsrConfigError as exc:
        raise AsrError(str(exc), code="engine_not_found") from exc
    return status()


def probe(key: str) -> dict[str, Any]:
    """测一下这台现在能不能连上（界面上的「测试连接」）。"""
    engine = engine_of(key)
    cfg = asr_settings_for(engine)
    ok, message = engine.probe(cfg)
    return {"key": engine.key, "ok": ok, "message": message}


def engine_of(key: str) -> AsrEngine:
    """按 key 取一台识别引擎；不存在就说清有哪些（不抛 KeyError）。"""
    engines = list_asr_engines()
    wanted = (key or "").strip()
    if not wanted:
        raise AsrError("尚未选择识别引擎。", code="asr_not_installed")
    engine = engines.get(wanted)
    if engine is None:
        raise AsrError(
            f"识别引擎 {wanted} 不存在。可用引擎：{'、'.join(sorted(engines)) or '（无）'}。",
            code="engine_not_found",
        )
    return engine


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #


def status() -> dict[str, Any]:
    """界面要的全部：每台识别引擎的说明与当前值、哪台在用、能不能用。

    不逐台发 ``probe``（那是「测试连接」按钮的事）；自动模式要读一份带短缓存的
    运行实例状态，成本与一次界面刷新相当。
    """
    engines = list_asr_engines()
    rows = [asr_payload(engine) for engine in engines.values()]
    current = current_key()
    active = next((row for row in rows if row["key"] == current), None)
    return {
        "engines": rows,
        "current": current,
        # 空配置 = 自动：界面据此显示"自动"，并说明实际选中了哪台。
        "auto": not bool(load_config().asr_engine.strip()),
        "ready": bool(active and active["ready"]),
        "note": (active or {}).get("note", "还没有可用的识别引擎。"),
        "options": options().model_dump(),
        # 补逐字稿那一轮的进度：界面用 toast 轮询 /asr/fill，这里顺带带一份。
        "fill": fill_state(),
    }


def unavailable_reason() -> str:
    """现在不能识别的原因；空串 = 能用。

    与设置页「引擎状态」同一判据（``readiness``，只看配置、不发网络请求）。
    自动识别一律先过这一关：没就绪就别起任务，也别让每次合成各自报一遍。
    """
    engines = list_asr_engines()
    key = current_key()
    if not key or key not in engines:
        return "暂无可用识别引擎，请在「引擎 → 识别」中选择。"
    engine = engines[key]
    missing = engine.readiness(asr_settings_for(engine))
    return f"{engine.label} 尚未就绪：{missing}" if missing else ""


def available() -> bool:
    """这台机器上现在就能识别吗（有引擎且配置够用）。界面据此摆不摆「识别」按钮。"""
    return not unavailable_reason()


def is_local() -> bool:
    """当前识别引擎是否本地 / 局域网推理。

    云端识别按整段计费：流式通道的「部分结果」每次都重听整段，不能替用户
    做这个决定。读不到能力时按本地处理——那只是少一条优化，不会多计费。
    """
    key = current_key()
    if not key:
        return True
    try:
        return bool(engine_of(key).capabilities().local)
    except AsrError:
        return True


def reachable() -> tuple[bool, str]:
    """问一次当前引擎的服务在不在（与设置里的「测试连接」同一个调用）。

    配置就绪不代表识别服务已经启动：本地服务常常是分开起的。这一层给
    长时间的后台补全做闸——一次探测换掉整批注定失败的请求。
    """
    engine, cfg = _prepared()
    return engine.probe(cfg)


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #


def _prepared() -> tuple[AsrEngine, BaseModel]:
    """取当前引擎与其配置；缺哪一步就分别说清（"去哪儿配"才是用户要听的那句）。"""
    reason = unavailable_reason()
    if reason:
        raise AsrError(reason, code="asr_not_installed")
    engine = engine_of(current_key())
    return engine, asr_settings_for(engine)


def _listen(path: Path, engine: AsrEngine, cfg: BaseModel) -> dict[str, Any]:
    """真正跑一遍，返回给调用方的扁平形状（``text`` / ``language`` / ``model`` / ``seconds``）。"""
    result: Transcript = engine.transcribe(cfg, path)
    text = _clean(result.text)
    converted = str(result.extra.get("transcoded_from") or "")
    CHANNEL.info(
        f"识别完成（{engine.label}）：{len(text)} 字 / {result.seconds:g}s"
        + (f" / {result.language}" if result.language else "")
        + (f" / 由 {converted} 转为 WAV" if converted else ""),
        source="asr",
        model=engine.key,
        seconds=result.seconds,
        chars=len(text),
        language=result.language,
    )
    return {
        "text": text,
        "language": result.language,
        # 归一后的语种代码（Chinese → zh）；认不出为空串，不猜。
        "language_code": detect_language(result.language),
        # 情绪：当今没有识别模型给（见 asr_base.AsrCapabilities），空串是"它没说"。
        "emotion": result.emotion,
        "model": engine.key,
        "seconds": result.seconds,
    }


def write_language(
    service: LibraryService, voice_id: str, sample_file: str, language: str
) -> str:
    """按「主样例 + 只填空 + 不覆盖」把识别语种写回音色。

    识别是**按样例**的，音色的 ``language`` 却是**单值**。拿任意一条样例的识别
    结果去改整个音色，会在多样例、多语种时把字段写成一个片面的答案；所以只认
    主样例（它是这条音色的代表），且只在音色语种为空时写。已有值（手填或平台
    认出的）永不覆盖。

    :return: 实际写入的语种代码（``zh`` / ``ja`` …）；空串 = 没有写。
    """
    code = detect_language(language)
    if not code or not voice_id or not sample_file:
        return ""
    try:
        voice = service.get_voice(voice_id)
    except LibraryError:
        return ""
    if (voice.language or "").strip():
        return ""
    if not voice.main_sample or sample_file != voice.main_sample:
        return ""
    service.update_voice(voice.id, {"language": code})
    CHANNEL.info(
        f"识别语种已写入音色（主样例）：{code}",
        source="asr",
        voice=voice.id,
        sample=sample_file,
    )
    return code


def _clean(text: str) -> str:
    """出文本后的一道清洗：压空白、统一全角空格。

    曾经这里还负责剥 SenseVoice 的 ``<|...|>`` 富文本标签；那档模型随进程内
    识别一起删了，**兜底正则保留**——任何识别模型都可能吐出这类控制标记，
    而它会被引擎一字不差地念出来。
    """
    value = re.sub(r"<\|[^|]*\|>", "", str(text or ""))
    return " ".join(value.replace("　", " ").split())


def transcribe_file(path: Path) -> dict[str, Any]:
    """把磁盘上某个音频文件听成文字（补逐字稿那条路用）。

    与 :func:`transcribe` 的差别只在"谁定文件"：这里收已拼好的路径，不做路径校验，
    调用方自行保证路径来源可信。
    """
    engine, cfg = _prepared()
    target = Path(path)
    if not target.is_file():
        raise AsrError(f"待识别音频不存在：{target}")
    return _listen(target, engine, cfg)


#: 进程内备忘：同一份音频、同一台引擎、同一套引擎配置，只**真听**一次。
#:
#: 补逐字稿是**按行**发生的（一行一次合成），而"样例的说话内容为空"这个判断每行都
#: 成立：自动写回关闭、或音色不在库中（写不回去）时，一本上千行的书会把同一段录音
#: 听上千遍，每次几秒。键里带文件指纹（大小 + mtime）与引擎配置摘要——样例换了录音、
#: 或者换了模型，都算另一件事，不会拿旧文本顶上去。
_HEARD: dict[tuple[str, int, int, str, str], dict[str, Any]] = {}
#: 已经说过"这次是沿用"的键：同一份音频只在**第一次**命中时记 info，之后降到 debug。
#: 一本书上千行，每行记一条等于把日志面板刷屏。
_HEARD_REPORTED: set[tuple[str, int, int, str, str]] = set()
_HEARD_LOCK = threading.Lock()
_HEARD_MAX = 64


def _heard_key(
    source: Path, engine: AsrEngine, cfg: BaseModel
) -> tuple[str, int, int, str, str] | None:
    """缓存键；文件读不到就不缓存，交给调用方照常去听。"""
    file = file_stamp(source)
    if file is None:
        return None
    try:
        stamp = hashlib.md5(
            cfg.model_dump_json().encode(), usedforsecurity=False
        ).hexdigest()[:12]
    except (AttributeError, TypeError, ValueError):
        stamp = type(cfg).__name__
    return (*file, engine.key, stamp)


def transcribe_for_reference(path: Path, *, reason: str = "") -> dict[str, Any]:
    """补逐字稿专用：同一次运行里对同一份音频只真听一次，结果多带 ``cached``。

    与 :func:`transcribe_file` 的分工是**谁在乎重复**：这里服务的是"每一行都要问一次
    那段参考音频的文本"的场景，所以按文件 + 引擎做中转；``/asr/*`` 那些手动入口走的
    仍是 :func:`transcribe_file`——用户点了"识别"就该真听一遍，不能被缓存糊过去。

    :param reason: 为什么要听（如"该引擎参考音频需要逐字稿，而该样例的说话内容为空"），
        只在**真要听**时写进日志；复用时不打这句，避免一次批量里刷上千条"正在识别"。
    """
    engine, cfg = _prepared()
    target = Path(path)
    if not target.is_file():
        raise AsrError(f"待识别音频不存在：{target}")
    key = _heard_key(target, engine, cfg)
    if key is not None:
        with _HEARD_LOCK:
            hit = _HEARD.get(key)
        if hit is not None:
            with _HEARD_LOCK:
                first = key not in _HEARD_REPORTED
                _HEARD_REPORTED.add(key)
            line = f"沿用本次已识别的逐字稿（{engine.label}），未重新识别：{target.name}"
            if first:
                # 每份音频说一次：这是"少了上千次识别"的证据，再说就是噪音。
                CHANNEL.info(line, source="asr", model=engine.key, file=target.name)
            else:
                CHANNEL.debug(line, source="asr", model=engine.key, file=target.name)
            return {**hit, "cached": True}
    # 先说"正在识别"：首次识别要加载模型，几秒到几十秒，界面须能区分"卡住"与"在干活"。
    CHANNEL.info(
        f"{reason + '；' if reason else ''}正在识别「{target.name}」（{engine.label}）。",
        source="asr",
        model=engine.key,
        file=target.name,
    )
    out = _listen(target, engine, cfg)
    if key is not None and str(out.get("text") or "").strip():
        with _HEARD_LOCK:
            if len(_HEARD) >= _HEARD_MAX:
                _HEARD.clear()  # 上限只防"一进程里听过几万份"这一种情况
                _HEARD_REPORTED.clear()
            _HEARD[key] = dict(out)
    return {**out, "cached": False}


def transcribe(service: LibraryService, voice_id: str, file: str) -> dict[str, Any]:
    """把库里某条样例听成文字，只接受库内样例（路径由服务端拼，防路径穿越）。"""
    # "能不能识别"排在"音色在不在"前面：都没配引擎时，"去哪儿配"才是用户要听的那句。
    engine, cfg = _prepared()
    voice = service.get_voice(voice_id)  # 找不到会抛 LibraryError，原样往上走
    # 判据是有没有本地音频，不是"是不是本地音色"：平台音色生成过之后也有音频。
    if not voice.folder or not voice.samples:
        raise AsrError("该音色尚无音频。请先在详情页生成一段。")
    sample = next((item for item in voice.samples if item.file == file), None)
    if sample is None:
        raise AsrError(f"该音色不包含文件 {file}。")
    # 失败也记一笔（界面上红字一刷新就没了）；只在这一处记，两个入口不说两遍。
    try:
        out = _listen(Path(voice.folder) / sample.file, engine, cfg)
    except AsrError as exc:
        CHANNEL.warning(f"识别失败：{exc}", source="asr", voice=voice_id, file=file)
        raise
    written = (
        write_language(service, voice.id, sample.file, out.get("language", ""))
        if options().language_writeback
        else ""
    )
    out["language_written"] = bool(written)
    out["voice_language"] = written or voice.language
    return out


def fill_missing(
    service: LibraryService,
    *,
    limit: int = _AUTO_FILL_BATCH,
    stop: threading.Event | None = None,
    on_step: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """把库里还没有逐字稿的样例听一批，写回「说话内容」。

    串行跑：一台识别服务通常一次只处理一条，并发只会让每条都更慢。一条失败不
    影响后面的——日志里留下 voice / file，下一轮还会再试。写回成功的样例文字非空，
    下一轮查询自然不再选中它，不会重复识别。

    :param limit: 这一批最多补多少条（``0`` = 全部）。
    :param stop: 停止信号；置位后当前这条收尾、不再开始下一条。
    :param on_step: 每走一条回调 ``"listening"``（真的要听这一条了）/ ``"written"`` /
        ``"failed"``，给进度用。
    """
    engine, cfg = _prepared()
    rows = service.missing_transcripts(limit)
    heard = written = failed = 0
    cached_id = ""
    cached_folder = ""
    for voice_id, file in rows:
        if stop is not None and stop.is_set():
            break
        # 同一音色的连续多条只查一次库：get_voice 要读 sidecar 与生成目录索引。
        if voice_id != cached_id:
            try:
                cached_folder = service.get_voice(voice_id).folder or ""
            except LibraryError:
                cached_folder = ""
            cached_id = voice_id
        if not cached_folder:
            continue
        path = Path(cached_folder) / file
        if not path.is_file():
            continue
        _step(on_step, "listening")
        try:
            out = _listen(path, engine, cfg)
        except AsrError as exc:
            failed += 1
            _step(on_step, "failed")
            CHANNEL.warning(
                f"自动识别跳过一条：{exc}", source="asr", voice=voice_id, file=file
            )
            continue
        heard += 1
        text = str(out.get("text") or "").strip()
        if not text:
            continue
        try:
            service.update_sample(voice_id, file, {"text": text})
        except LibraryError as exc:
            failed += 1
            _step(on_step, "failed")
            CHANNEL.warning(
                f"自动识别写入失败：{exc}", source="asr", voice=voice_id, file=file
            )
            continue
        written += 1
        _step(on_step, "written")
        if options().language_writeback and out.get("language"):
            write_language(service, voice_id, file, str(out["language"]))
    return {"total": len(rows), "heard": heard, "written": written, "failed": failed}


def _step(on_step: Callable[[str], None] | None, kind: str) -> None:
    if on_step is not None:
        on_step(kind)


def _bump(kind: str) -> None:
    """一条走了一步：写回算 done，其它算 failed（进度条只看这两个数）。

    ``listening`` 不是进度，是"这一条真的要听"——界面靠它决定亮不亮 toast：
    起了线程却一条都不用听（服务没起、库里有文字、文件已不在）时不该有 toast。
    """
    if kind == "listening":
        _fill_state["filling"] = True
    elif kind == "written":
        _fill_state["done"] = int(_fill_state["done"]) + 1
    else:
        _fill_state["failed"] = int(_fill_state["failed"]) + 1


def fill_all(service: LibraryService) -> dict[str, int]:
    """一直补到没有可补的（或被停止）。

    每批重新查库：补的过程里音色库可能新增/删除；也避免一次把几万条读进内存。
    一条都没写回就收手——再查还是同一批，空转没有意义（失败原因已经进日志）。
    """
    _fill_state.update(done=0, failed=0, total=len(service.missing_transcripts()), filling=False)
    totals = {"total": 0, "heard": 0, "written": 0, "failed": 0}
    try:
        while not _fill_cancel.is_set():
            rows = service.missing_transcripts(_AUTO_FILL_BATCH)
            if not rows:
                break
            stats = fill_missing(service, stop=_fill_cancel, on_step=_bump)
            for key in totals:
                totals[key] += stats[key]
            if _fill_cancel.is_set() or stats["written"] == 0:
                break
    finally:
        # 这一轮结束了就立刻收起 toast：别等到锁释放（此刻还可能有一小段收尾）。
        _fill_state["filling"] = False
    stopped = _fill_cancel.is_set()
    if totals["total"]:
        CHANNEL.info(
            ("连接时自动识别已停止：" if stopped else "连接时自动识别：")
            + f"补上 {totals['written']} 条逐字稿"
            + (f"，{totals['failed']} 条失败" if totals["failed"] else ""),
            source="asr",
            written=totals["written"],
            failed=totals["failed"],
            stopped=stopped,
        )
    return totals


def fill_state() -> dict[str, Any]:
    """这一轮补逐字稿的进度：界面用 toast 轮询它。

    ``running`` 只在**真的有一条音频在听**时为真。线程起了不等于有活：启动后要等
    扫描写完库、还要等服务连得上、库里也真的缺文本——这些时候 ``running`` 都是假，
    界面据此不亮 toast（否则每次启动都会挂一条空任务）。

    ``starting`` 是"线程起了、还没确定有没有活"：前端据此继续轮询，但不显示任何东西。
    """
    held = _fill_lock.locked()
    filling = bool(_fill_state["filling"])
    return {
        "running": held and filling,
        "starting": held and not filling,
        "done": int(_fill_state["done"]),
        "failed": int(_fill_state["failed"]),
        "total": int(_fill_state["total"]),
    }


def cancel_fill() -> dict[str, Any]:
    """请求停止：当前这一条收尾后不再开始下一条（与「一键配头像」同一语义）。"""
    _fill_cancel.set()
    return fill_state()


def maybe_fill(
    service: LibraryService, *, delay: float = 0.0, verified: bool = False
) -> bool:
    """「连接时自动识别」：连上识别服务后，在后台补逐字稿，一直补到底。

    调用点都发生在请求里（保存设置 / 切换引擎 / 探测成功 / 服务启动），
    识别不能拖住它们，所以整轮放到守护线程。返回是否真的启动了一轮——
    开关关着、引擎未就绪、服务连不上、已经有一轮在跑，都不启动。

    :param delay: 启动时先等一会儿，让扫描把样例写进库之后再查。
    :param verified: 调用方刚探测成功（「测试连接」那一路），不必再探一次。
    """
    if not options().auto_on_connect:
        return False
    reason = unavailable_reason()
    if reason:
        CHANNEL.debug(f"连接时自动识别未开始：{reason}", source="asr")
        return False
    if not _fill_lock.acquire(blocking=False):
        return False
    _fill_cancel.clear()
    _fill_state.update(done=0, failed=0, total=0, filling=False)

    def run() -> None:
        try:
            if delay > 0:
                time.sleep(delay)
            if not verified:
                ok, message = reachable()
                if not ok:
                    CHANNEL.warning(
                        f"连接时自动识别未开始：识别服务未启动（{message}）", source="asr"
                    )
                    return
            fill_all(service)
        except AsrError as exc:
            # 没引擎 / 没配好：这条连接本来就用不了识别，留一行日志就够。
            CHANNEL.warning(f"连接时自动识别未开始：{exc}", source="asr")
        except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛回请求
            CHANNEL.error(f"连接时自动识别出错：{exc}", source="asr")
        finally:
            _fill_lock.release()

    threading.Thread(target=run, daemon=True, name="asr-fill").start()
    return True
