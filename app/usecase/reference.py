"""参考音频准备：发出去之前，把录音弄成这台引擎读得进去的样子。

四件事，且只在引擎声明时才做（依据 ``synthesis_spec``，不写在代码里的引擎名单）：

1. **转换**——引擎只读 wav 而素材是 mp3 时转一道（依据声明的 ``reference_formats``）；
2. **截取**——引擎对长度有硬区间而这段超了，截到上限以内（依据 ``reference_duration``）；
3. **补逐字稿**——样例「说话内容」空着时识别一次，并把结果写回样例；
4. **换路径**——引擎读不了非 ASCII 路径时，复制一份 ASCII 名的副本再发
   （依据 ``reference_path_ascii``）。

前三件都要说：每一件都往 ``SynthesisResult.notes`` 留一句，否则用户无法解释
"我挑了 mp3 它却成功了"。第 4 件**不上界面**——音色与内容都没变，只是发出去的文件名
换了一个，用户没有需要知道的东西；万一复制失败才报错。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from app.engines.base import EngineError, EnginePlugin, SynthesisRequest, SynthesisSpec
from app.infra import audioprobe, ffmpeg
from app.infra.loghub import CHANNEL
from app.usecase import asr as asr_use
from app.usecase.library import LibraryError, LibraryService

__all__ = ["prepare"]

#: 截取时给上限留的余量（秒）。服务端把秒数换算成采样点数再比较，
#: 卡在边界上会因取整被判成"超了"；留半秒既够余量又不明显影响音色。
_DURATION_MARGIN = 0.5


def _probe_detail(message: str) -> str:
    """探测结果 → 只留"地址 + 原因"，去掉与外面那句话重复的部分。

    探测那一路自带一句「连不上」，而调用处外面写的已经是「识别服务未启动」；
    两句叠起来就是"未启动（连不上 …）"，同一个意思说两遍。末尾的句号也一并去掉：
    括起来的那一段接在整句里，再留一个"。"就成了"）。"。
    """
    detail = message.removeprefix("连不上").lstrip(" ：:，,").strip().rstrip("。")
    return detail or message


def _reference_pair(request: SynthesisRequest) -> tuple[str, str]:
    """这次真正会用的那段音频与它自己的逐字稿。

    逐字稿必须跟着音频走，不能跨段借：请求格子里与 ``extra`` 里可能是不同录音，
    借了会让提示文本与实际音频对不上且不报错。规则：``extra`` 给了音频则文本只认
    ``extra`` 那份；没给则用主样例（文本可用 ``extra`` 更正过的）。

    :return: ``(音频路径, 逐字稿)``，两者要么都空、要么来自同一次选择。
    """
    picked = request.extra.get("reference_audio")
    picked = picked.strip() if isinstance(picked, str) else ""
    given_text = request.extra.get("reference_text")
    given_text = given_text.strip() if isinstance(given_text, str) else ""
    if picked:
        return picked, given_text
    return str(request.reference_audio or "").strip(), given_text or str(
        request.reference_text or ""
    ).strip()


def _remember(
    service: LibraryService, library_voice_id: str, source: Path, text: str
) -> tuple[bool, str]:
    """把刚识别出的文本写回那条样例的「说话内容」，返回 ``(是否写回, 样例文件名)``。

    认样例靠绝对路径（不靠显示名，显示名改一次就对不上）。写不回去不算失败——
    本次合成照发，只是下次还得再识别；返回结果供调用方说明，不悄悄吞掉。
    文件名是给语种写回用的：语种只能按主样例写，得知道这次听的是哪一条。
    """
    if not library_voice_id:
        return False, ""
    try:
        voice = service.get_voice(library_voice_id)
    except LibraryError:
        return False, ""
    if not voice.folder:
        return False, ""
    target = source.resolve() if source.exists() else source
    for sample in voice.samples:
        path = Path(voice.folder) / sample.file
        if path == source or (path.exists() and path.resolve() == target):
            service.update_sample(voice.id, sample.file, {"text": text})
            return True, sample.file
    return False, ""


def _fit_duration(
    engine: EnginePlugin,
    spec: SynthesisSpec,
    source: Path,
    working: Path,
    raw_text: str,
) -> tuple[Path, str, tuple[str, str] | None]:
    """引擎对参考音频长度有硬区间时量一次：超了截到上限以内，短了直接报。

    **截过之后逐字稿一律作废**：它对应的是整段录音，拿去对齐截出来的那半句话
    不会报错，只会念得怪（音素与语义 token 错位）。作废后由下一步按引擎声明
    决定要不要重听——需要逐字稿的引擎会重新识别；不需要的就空着发。

    :return: ``(音频, 逐字稿, note)``；没动过时原样返回、note 为 ``None``。
    """
    assert spec.reference_duration is not None  # 调用方已判
    low, high = spec.reference_duration
    seconds = audioprobe.probe_duration(working)
    if seconds <= 0:
        # 量不出来就不拦：ffprobe 缺席时猜一个长度去截，比让服务端报错更糟。
        return working, raw_text, None
    if seconds < low:
        raise EngineError(
            f"参考音频「{source.name}」只有 {seconds:.1f} 秒，{engine.label} 要求 "
            f"{low:g}–{high:g} 秒；短了补不回来，请换一条更长的样例。"
        )
    if seconds <= high:
        return working, raw_text, None

    ceiling = max(low + 0.1, high - _DURATION_MARGIN)
    # 在允许范围内找一句说完的地方：从上限往回退，退到下限为止（也就是 3 秒），
    # 找一个停顿总比硬切在字中间强。截得越少音色越接近原样。
    point, at_pause = ffmpeg.cut_point(working, low=low, high=ceiling)
    if not at_pause:
        # 句子尾巴常常正好落在余量里：上限内再放宽一点找一次（服务端判据是 10 秒，
        # 9.9 秒仍在里面），比直接硬切在 9.5 更可能保住句子。
        wider = min(high - 0.1, ceiling + _DURATION_MARGIN - 0.1)
        if wider > ceiling:
            point, at_pause = ffmpeg.cut_point(working, low=low, high=wider)
    try:
        trimmed = ffmpeg.to_wav(working, limit=point)
    except ffmpeg.FfmpegError as exc:
        raise EngineError(
            f"参考音频「{source.name}」长 {seconds:.1f} 秒，{engine.label} 要求 "
            f"{low:g}–{high:g} 秒；截取前 {point:.1f} 秒失败：{exc.reason}"
        ) from exc
    where = (
        f"前 {point:.1f} 秒（该处为句读停顿）"
        if at_pause
        else f"前 {point:.1f} 秒（未找到停顿，按上限截断）"
    )
    CHANNEL.info(
        f"参考音频「{source.name}」长 {seconds:.1f} 秒，超出 {engine.label} 的 "
        f"{low:g}–{high:g} 秒；已截取{where}：{trimmed.name}",
        source="reference",
        engine=engine.key,
        seconds=round(seconds, 1),
        cut_at=point,
        at_pause=at_pause,
    )
    tail = (
        "，逐字稿按截取后的音频重新听写。" if raw_text and spec.needs_transcript else "。"
    )
    return trimmed, "", (
        "reference_trimmed",
        (
            f"参考音频「{source.name}」长 {seconds:.1f} 秒，{engine.label} 要求 "
            f"{low:g}–{high:g} 秒；已截取{where}后发送{tail}"
        ),
    )


def prepare(
    service: LibraryService,
    engine: EnginePlugin,
    request: SynthesisRequest,
    *,
    library_voice_id: str = "",
    cfg: Any = None,
) -> tuple[SynthesisRequest, tuple[tuple[str, str], ...]]:
    """照引擎声明的契约准备参考音频，返回 ``(新请求, ((code, message), ...))``。

    ``code`` 机器可读（``audio_transcoded`` / ``reference_trimmed`` / ``transcript_inferred``），
    ``message`` 中文说明；HTTP 层合成进 ``warnings``。
    没给参考音频时原封不动返回；做不成抛 :class:`EngineError`，说明比引擎原生报错更细。

    :param library_voice_id: 库内音色 id（非请求里的显示名），仅供写回样例用。
    :param cfg: 这台引擎当前的配置。有些约束随服务端模型变（GPT-SoVITS 的 v3/v4 要
        参考文本、v2Pro 可以没有），传了才按实际那一版算。
    """
    if engine.capabilities().voice_design and str(request.extra.get("voice_design") or "").strip():
        return request, ()
    spec = engine.synthesis_spec_for(cfg) if cfg is not None else engine.synthesis_spec()
    raw_audio, raw_text = _reference_pair(request)
    if not raw_audio:
        return request, ()

    source = Path(raw_audio)
    working = source
    notes: list[tuple[str, str]] = []

    # ---- 1) 格式：引擎只读 wav 而这段不是，就转一道 ----
    if spec.reference_formats:
        wanted = "、".join(spec.reference_formats)
        # 嗅探优先、后缀兜底：嗅探认得出后缀骗人的（如挂着错后缀的下载音频）。
        actual = ffmpeg.container_of(source) or source.suffix.lstrip(".").lower()
        if actual not in spec.reference_formats:
            try:
                working = ffmpeg.to_wav(source)
            except ffmpeg.FfmpegError as exc:
                raise EngineError(
                    f"{engine.label} 的参考音频需要 {wanted}，而「{source.name}」"
                    f"为 {actual or '无法识别的格式'}；转换失败：{exc.reason}"
                ) from exc
            notes.append(
                (
                    "audio_transcoded",
                    (
                        f"参考音频「{source.name}」为 {actual or '无法识别的格式'}，"
                        f"{engine.label} 的参考音频需要 {wanted}；"
                        f"已通过 ffmpeg 转换为 WAV（{working.name}）后发送。"
                    ),
                )
            )
            # 转码耗时（几百毫秒到几秒），记下来供"这次为什么慢"排查。
            CHANNEL.info(
                f"参考音频「{source.name}」为 {actual or '无法识别的格式'}，"
                f"{engine.label} 需要 {wanted}；已转换为 WAV：{working.name}",
                source="reference",
                engine=engine.key,
                from_format=actual or "unknown",
            )
        else:
            # debug：与上面 info 成对——"这次快"是因为没转，直接用了原件。
            CHANNEL.debug(
                f"参考音频「{source.name}」已是 {actual}，无需转换。",
                source="reference",
                engine=engine.key,
            )

    # ---- 2) 时长：引擎有硬区间时先量，超了截、短了报 ----
    # 截之前先记下"用户到底给没给逐字稿"：截断会把逐字稿作废，而给了逐字稿的
    # （如 VoxCPM2 终极克隆）截后应当重听一句保住对应关系，不该悄悄退成普通克隆。
    had_transcript = bool(raw_text.strip())
    trimmed = False
    if spec.reference_duration:
        working, raw_text, trim_note = _fit_duration(engine, spec, source, working, raw_text)
        if trim_note:
            trimmed = True
            notes.append(trim_note)

    # ---- 3) 逐字稿：引擎要它而这条样例没有，就听一遍 ----
    # 两种情形要听：① 引擎强制要逐字稿（needs_transcript）；② 用户本来给了逐字稿、
    # 音频被截断后作废了——重听一句让发出去的逐字稿仍配得上截出来的那段音频。
    prefs = asr_use.options()
    if (spec.needs_transcript or (trimmed and had_transcript)) and not raw_text:
        # "没有逐字稿"有两种来路，说法必须分开：
        #   ① 样例本来就没填——用户去填就行；
        #   ② 音频超长被截断、原逐字稿随之作废——样例里明明填着字，还按①去说
        #      "说话内容为空"，用户只会困惑（他能填什么？整段的字配不上截出的半句）。
        if trimmed and spec.reference_duration:
            low, high = spec.reference_duration
            missing = (
                f"{engine.label} 参考音频要求 {low:g}–{high:g} 秒，"
                "截取后需重新识别逐字稿"
            )
            # 截断是这一次的临时行为，不该让用户去样例里填"半句话"的字——
            # 能走的路只有两条：识别服务起来，或换一条本来就在区间内的样例。
            # 两条都不写进报错：用户要知道的是"现在卡在哪"，不是选项清单。
        else:
            missing = f"{engine.label} 参考音频需要逐字稿，而该样例的说话内容为空"
        if not prefs.auto_transcript:
            raise EngineError(
                f"{missing}；自动识别已关闭（设置 → 语音识别 → 缺逐字稿时自动识别）。"
            )
        # 先看识别引擎在不在（与设置页「引擎状态」同一判据），再看服务起没起
        # （与「测试连接」同一个探测）：两样都过了才去听。
        # 否则报出来的是"自动识别失败"，用户要查的其实是"引擎没启动"。
        unavailable = asr_use.unavailable_reason()
        if not unavailable:
            ok, message = asr_use.reachable()
            if not ok:
                unavailable = f"识别服务未启动（{_probe_detail(message)}）"
        if unavailable:
            raise EngineError(f"{missing}，{unavailable}。")
        # "正在识别"那句由 asr 层打（它才知道是真听还是复用本次已听过的）。
        try:
            heard = asr_use.transcribe_for_reference(working, reason=missing)
        except asr_use.AsrError as exc:
            # 上游原话原样带出（可能自带句号），不再接自己的解释。
            reason = str(exc).strip().rstrip("。")
            raise EngineError(f"{missing}；自动识别失败：{reason}。") from exc
        heard_text = str(heard.get("text") or "").strip()
        if not heard_text:
            raise EngineError(
                f"{missing}；未从「{working.name}」中识别出文字（可能不含人声）。"
            )
        raw_text = heard_text
        # 复用本次已听过的结果时，**写回与说明都不再做**：同一段录音在第一次就已经按
        # 当时的设置处理过，逐行重复写一遍没有新信息，只会把同一句警告刷上千条。
        if heard.get("cached"):
            CHANNEL.debug(
                "逐字稿沿用本次已识别的结果，跳过写回与说明。",
                source="reference",
                engine=engine.key,
                file=working.name,
            )
        else:
            # 截取过的**不写回**：样例仍是整段录音，把半句话的转写写进去，
            # 下次用整段时逐字稿就对不上了（而且那条记录看起来像是"已经填好了"）。
            kept, sample_file = (
                _remember(service, library_voice_id, source, heard_text)
                if prefs.writeback and not trimmed
                else (False, "")
            )
            language_code = (
                asr_use.write_language(
                    service, library_voice_id, sample_file, str(heard.get("language") or "")
                )
                if sample_file and prefs.language_writeback
                else ""
            )
            if kept:
                tail = "，并写回该样例。"
            elif trimmed and prefs.writeback:
                tail = "，未写回样例（与整段录音不符）。"
            elif prefs.writeback:
                tail = "，未能写回（音色不在库中）。"
            else:
                tail = "，未写回（自动写回已关闭）。"
            if language_code:
                tail += f"识别语种已写入音色（{language_code}）。"
            notes.append(
                (
                    "transcript_inferred",
                    (
                        f"{missing}；已用识别引擎（{heard.get('model')}）"
                        f"转写：「{heard_text}」{tail}"
                    ),
                )
            )
            # 这行说的是写回结果（usecase.asr 自己那条"听完了"不知道这件事），各记各的。
            if kept:
                CHANNEL.info("识别结果已写回样例。", source="reference", engine=engine.key)
            elif trimmed and prefs.writeback:
                CHANNEL.debug(
                    "参考音频已截取，识别结果只用于本次合成，未写回样例。",
                    source="reference",
                    engine=engine.key,
                )
            elif prefs.writeback:
                # warning：结果没存下来，用户须知道"重启之后还要再等一次"。
                # 同一次运行内不会再重复识别（见 asr.transcribe_for_reference）。
                CHANNEL.warning(
                    "音色不在库中，识别结果未能写回样例；服务重启后仍需重新识别。",
                    source="reference",
                    engine=engine.key,
                    voice=library_voice_id or "(未提供)",
                )
            else:
                CHANNEL.debug(
                    "自动写回已关闭，识别结果只用于本次合成。",
                    source="reference",
                    engine=engine.key,
                )

    # ---- 4) 路径：引擎读不了非 ASCII 路径时，落一份 ASCII 名的副本 ----
    # gsv.cpp 这类 Windows 原生后端把请求里的路径转本地代码页，中文名一律 500
    # （``No mapping for the Unicode character exists …``）。复制一份改名是最小代价：
    # 不动用户的素材库，也不改音色本身。
    if spec.reference_path_ascii and not str(working).isascii():
        try:
            staged = ffmpeg.stage_ascii_path(working)
        except OSError as exc:
            raise EngineError(
                f"参考音频「{source.name}」的路径含非 ASCII 字符，{engine.label} 读不了这种路径；"
                f"复制到纯 ASCII 的暂存目录时失败：{exc.strerror or exc}。"
            ) from exc
        # 这条不发到界面：音色与内容都没变，只是换了个文件名发出去。
        CHANNEL.debug(
            f"参考音频路径含非 ASCII，已复制为 {staged.name} 后发送"
            f"（{engine.label} 只认 ASCII 路径）。",
            source="reference",
            engine=engine.key,
            file=staged.name,
        )
        working = staged

    # ---- 收尾：把生效值归一到请求自己的格子上 ----
    # 否则"实际发哪一个"要看两处，而映射表读的是合并后的值，可能判 A 发 B。
    extra = {
        key: value
        for key, value in request.extra.items()
        if key not in ("reference_audio", "reference_text")
    }
    return (
        replace(
            request,
            reference_audio=str(working),
            reference_text=raw_text,
            extra=extra,
        ),
        tuple(notes),
    )
