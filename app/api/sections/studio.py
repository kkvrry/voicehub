"""路由分区「studio」：只注册本域的接口；路径前缀与错误约定见 :mod:`app.api.routes`。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Header, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.api.errors import ApiError, not_found
from app.api.views import (
    _IDEMPOTENCY_TTL,
    AvatarApplyIn,
    PronunciationsIn,
    SynthesizeIn,
    TextsIn,
    TranscribeIn,
    VocabIn,
    _binary_audio,
    _fingerprint,
    _read_audio,
    _synthesis_replay,
    _temp_audio,
    _voice_out,
)
from app.engines.registry import list_engines
from app.infra import archive, recordings, texts
from app.infra.loghub import CHANNEL
from app.infra.vocab import VocabError
from app.usecase import (
    asr,
    asr_stream,
    avatar,
    gateway,
)
from app.usecase import (
    pronunciation as pronunciation_uc,
)
from app.usecase import (
    vocab as vocab_uc,
)
from app.usecase.library import LibraryError, LibraryService

__all__ = ["register"]


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.get("/avatar/status", summary="头像来源", tags=["avatar"])
    def avatar_status() -> dict[str, Any]:
        """可用的图片来源与说明。**不探网络**。"""
        return {"object": "avatar_status", **avatar.status()}

    @router.post("/avatar/voices/{voice_id}", summary="查找音色头像", tags=["avatar"])
    async def avatar_generate(voice_id: str) -> dict[str, Any]:
        """按分类与名字找图，裁成方形头像并写盘。**只补空缺**：目录里已有图就跳过
        （``skipped`` 带回那个文件名），要换头像走候选接口。一键配头像走这里。"""
        result = await avatar.generate(service, voice_id)
        return {
            "object": "voice_avatar",
            "source": result["source"],
            "source_url": result["source_url"],
            "matched_name": result["matched_name"],
            "skipped": result.get("skipped", ""),
            "voice": _voice_out(result["voice"], with_samples=True),
        }

    @router.post("/avatar/voices/{voice_id}/candidates", summary="头像候选", tags=["avatar"])
    async def avatar_candidates(voice_id: str) -> dict[str, Any]:
        """各来源裁好的候选图（缩略图 + 出处），**不落盘**；挑好后走 apply。"""
        result = await avatar.candidates(service, voice_id)
        return {
            "object": "avatar_candidates",
            "voice": _voice_out(result["voice"], with_samples=True),
            "data": result["items"],
        }

    @router.post("/avatar/voices/{voice_id}/apply", summary="应用头像候选", tags=["avatar"])
    async def avatar_apply(voice_id: str, body: AvatarApplyIn) -> dict[str, Any]:
        """把用户选中的那张候选写盘。认错人可以换一张，旧头像留在 avatar-prev.*。"""
        result = await avatar.apply_candidate(service, voice_id, body.token)
        return {
            "object": "voice_avatar",
            "source": result["source"],
            "source_url": result["source_url"],
            "matched_name": result["matched_name"],
            "voice": _voice_out(result["voice"], with_samples=True),
        }

    @router.post("/asr/transcribe", summary="识别样例", tags=["asr"])
    def asr_transcribe(body: TranscribeIn) -> dict[str, Any]:
        """将库中某条样例转为文字。"""
        return {"object": "transcript", **asr.transcribe(service, body.voice_id, body.file)}

    @router.post("/asr/dictation", summary="麦克风语音输入", tags=["asr"])
    async def asr_dictation(
        file: UploadFile | None = File(None, description="录好的音频（wav）"),  # noqa: B008
    ) -> dict[str, Any]:
        """把一段**不属于任何音色**的录音听成文字（生成页正文那一格的语音输入）。

        与 ``/asr/transcribe`` 的差别只是"音频从哪来"：这里是麦克风录的，库里没有
        它，所以由调用方上传；听完即删（这段录音的唯一用途就是这一句话）。
        """
        if file is None:
            raise ApiError(status=400, code="dictation_required", message="没收到音频。")
        raw = await _read_audio(file)
        path = _temp_audio(raw, Path(file.filename or "").suffix)
        try:
            return {"object": "transcript", **asr.transcribe_file(path)}
        finally:
            path.unlink(missing_ok=True)

    @router.websocket("/asr/stream")
    async def asr_stream_ws(ws: WebSocket) -> None:
        """麦克风音频流：**服务端 VAD 分段**，说完一段推一段文字。

        协议（``/api/v1/asr/stream``）：

        - 客户端 → 服务端：第一条文本 ``{"type":"start","sample_rate":48000}``
          （浏览器 AudioContext 的实际采样率，服务端统一重采样到 16k）；之后是
          二进制帧（int16 单声道 PCM，建议 100ms 一帧）；收尾发文本
          ``{"type":"stop"}``，把没说完的话冲出来。
        - 服务端 → 客户端：``ready``（会话就绪）、``speech``（开始说话）、
          ``partial``（说话中的部分识别，文字随说话推进变化，仅作实时展示）、
          ``segment``（一段话听完了，带 ``text`` 等识别结果）、``error``、
          ``done``。

        VAD 在事件循环里收帧、推理放工作线程；切出来的段进一条队列，由单独的
        识别协程按序听，收帧不被识别卡住。部分识别同一时刻只跑一条，间隔随
        说话时长翻倍；一旦切出整段，在途的部分结果即作废。发送全部经一个发送
        协程，保证事件顺序。
        """
        await ws.accept()
        # 插件发现与 torch / silero 的 import 都可能有几秒，放线程里做：
        # 这条连接还没 ready，但别的请求不该陪着等。
        try:
            reason = await asyncio.to_thread(
                lambda: asr.unavailable_reason() or asr_stream.vad_unavailable_reason()
            )
        except Exception as exc:  # noqa: BLE001 - 依赖装坏（如 torch DLL）也要回一句话
            reason = f"流式识别依赖检查失败：{exc}"
        if reason:
            await ws.send_json({"type": "error", "message": reason})
            await ws.close()
            return

        # 云端识别按整段计费：部分结果每次都要重听整段，不能替用户决定花这笔钱。
        partial_ok = True
        try:
            partial_ok = await asyncio.to_thread(asr.is_local)
        except Exception:  # noqa: BLE001 - 能力读不到就按本地处理，不额外计费
            partial_ok = True

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        # 切出来的段进这条队列，由单独的识别协程按序听：收帧循环只做 VAD，
        # 不等识别。识别慢的时候帧会压在 WebSocket 里，但顺序与内容都不丢。
        segment_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: dict[str, Any] | None) -> None:
            # VAD 回调在工作线程里跑；队列只能从事件循环线程碰。
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def sender() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    break
                try:
                    await ws.send_json(event)
                except Exception:  # noqa: BLE001 - 连接已断，发送方自行收摊
                    return

        async def recognizer() -> None:
            while True:
                pcm = await segment_queue.get()
                if pcm is None:
                    break
                result = await asyncio.to_thread(_listen_segment, pcm)
                emit({"type": "segment", **result})

        sender_task = asyncio.create_task(sender())
        recognizer_task = asyncio.create_task(recognizer())

        def enqueue_segment(pcm: bytes) -> None:
            """切出一段：作废在途的部分结果，交给识别协程排队。"""
            nonlocal segment_gen, partial_seq
            segment_gen += 1
            partial_seq += 1
            segment_queue.put_nowait(pcm)

        session = None
        rate = asr_stream.TARGET_RATE
        # 部分识别（边说边出字）的闸门：
        # - segment_gen 每切出一段就 +1，在途的部分结果立即作废；
        # - partial_seq 每起一次部分识别 +1，后发先至的旧结果不会盖掉新的；
        # - partial_task 只留一个，上一轮没跑完就不再起下一轮（识别服务一次
        #   只跑一条，排队只会把收尾段挡在后面）。
        segment_gen = 0
        partial_seq = 0
        partial_mark = 0
        partial_gap = asr_stream.PARTIAL_MS
        partial_task: asyncio.Task[None] | None = None

        def _on_speech() -> None:
            nonlocal partial_mark, partial_gap
            partial_mark = 0
            partial_gap = asr_stream.PARTIAL_MS
            emit({"type": "speech"})

        async def _emit_partial(pcm: bytes, seq: int, gen: int) -> None:
            try:
                result = await asyncio.to_thread(_listen_segment, pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 部分结果失败不该断流
                CHANNEL.warning(f"流式识别的部分结果失败：{exc}", source="asr")
                return
            if seq == partial_seq and gen == segment_gen and result.get("text"):
                emit({"type": "partial", **result})

        try:
            while True:
                message = await ws.receive()
                kind = message.get("type")
                if kind == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is not None:
                    payload = _stream_command(text)
                    if payload is None:
                        emit({"type": "error", "message": "指令不认识，应为 start / stop。"})
                        continue
                    command = payload.get("type")
                    if command == "start":
                        if session is not None:
                            emit(
                                {
                                    "type": "error",
                                    "message": "已经在听，先 stop 再重新 start。",
                                }
                            )
                            continue
                        wanted = payload.get("sample_rate") or asr_stream.TARGET_RATE
                        try:
                            wanted_rate = int(wanted)
                        except (TypeError, ValueError):
                            wanted_rate = 0
                        if not asr_stream.MIN_RATE <= wanted_rate <= asr_stream.MAX_RATE:
                            emit(
                                {
                                    "type": "error",
                                    "message": (
                                        f"采样率不合法：{wanted}。"
                                        f"应在 {asr_stream.MIN_RATE}–{asr_stream.MAX_RATE} Hz 之间。"
                                    ),
                                }
                            )
                            continue
                        rate = wanted_rate
                        if partial_task is not None:
                            partial_task.cancel()
                            partial_task = None
                        partial_seq += 1
                        segment_gen += 1
                        # speech 事件在"转入说话"的瞬间发（VAD 状态机回调），
                        # 好让界面立刻点亮"在听"，而不是等一段说完才知道。
                        try:
                            session = await asyncio.to_thread(
                                asr_stream.new_session, _on_speech
                            )
                        except Exception as exc:  # noqa: BLE001 - 模型装不上也要回一句话
                            session = None
                            CHANNEL.warning(f"流式识别启动失败：{exc}", source="asr")
                            emit(
                                {
                                    "type": "error",
                                    "message": f"流式识别启动失败：{exc}",
                                }
                            )
                            continue
                        emit({"type": "ready"})
                    elif command == "stop":
                        # 在途的部分结果注定被收尾段取代：作废并直接丢掉，不再
                        # 等它跑完再推给界面（那只会让 stop 变慢）。
                        if partial_task is not None:
                            partial_seq += 1
                            partial_task.cancel()
                            partial_task = None
                        if session is not None:
                            tail = session.flush()
                            session = None
                            if tail:
                                enqueue_segment(tail)
                        # 收尾段排在已切出的段后面；识别协程排干再报 done。
                        segment_queue.put_nowait(None)
                        with contextlib.suppress(Exception):
                            await recognizer_task
                        emit({"type": "done"})
                        break
                    continue
                data = message.get("bytes")
                if data is None or session is None:
                    continue
                try:
                    segments = await asyncio.to_thread(session.feed, data, rate)
                except Exception as exc:  # noqa: BLE001 - 分段器坏了就明说，别静默断流
                    session = None
                    CHANNEL.warning(f"流式识别中断：{exc}", source="asr")
                    emit({"type": "error", "message": f"流式识别中断：{exc}"})
                    continue
                for segment in segments:
                    enqueue_segment(segment)
                if (
                    partial_ok
                    and session is not None
                    and session.speaking
                    and session.speech_ms - partial_mark >= partial_gap
                    and (partial_task is None or partial_task.done())
                ):
                    snapshot = session.partial()
                    if snapshot is not None:
                        partial_mark = session.speech_ms
                        partial_seq += 1
                        partial_task = asyncio.create_task(
                            _emit_partial(snapshot, partial_seq, segment_gen)
                        )
                        partial_gap = min(partial_gap * 2, asr_stream.PARTIAL_MAX_MS)
        except WebSocketDisconnect:
            pass
        finally:
            background = [task for task in (partial_task, recognizer_task) if task is not None]
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            emit(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sender_task, timeout=5)
            with contextlib.suppress(Exception):
                await ws.close()

    def _stream_command(text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("type") not in ("start", "stop"):
            return None
        return payload

    def _listen_segment(pcm: bytes) -> dict[str, Any]:
        """一段 PCM → 识别结果；失败给一条可读的事件而不是断流。"""
        try:
            return asr_stream.transcribe_pcm(pcm)
        except asr.AsrError as exc:
            return {"text": "", "error": exc.reason}
        except Exception as exc:  # noqa: BLE001 - 识别端点的最后一道边界
            CHANNEL.warning(f"流式识别出错：{exc}", source="asr")
            return {"text": "", "error": f"识别出错：{exc}"}

    @router.post("/recordings", summary="保存一段录音当参考音频", tags=["asr"])
    async def save_recording(
        file: UploadFile | None = File(None, description="录好的音频（wav）"),  # noqa: B008
        transcribe: bool | None = Form(
            None, description="顺手听出它念的是什么；不传按设置里的「录音后自动转写」"
        ),
    ) -> dict[str, Any]:
        """麦克风录一段，落盘后把路径交回界面（填进「参考音频」那一格）。

        **只留在临时目录**（服务重启即清）：它是"这一次用哪段声音"，不是谁的样例，
        留在磁盘上用户既看不见也删不掉。
        """
        if file is None:
            raise ApiError(status=400, code="recording_required", message="没收到音频。")
        raw = await _read_audio(file)
        path = recordings.save(raw, Path(file.filename or "").suffix or ".wav")
        text = ""
        if transcribe is None:
            transcribe = asr.options().transcribe_recordings
            # 自动转写以识别引擎就绪为前置（与设置页「引擎状态」同一判据）：
            # 没就绪就不试、不告警，录音本身照常可用。
            if transcribe and not asr.available():
                transcribe = False
        if transcribe:
            try:
                text = str(asr.transcribe_file(path).get("text", "") or "")
            except Exception as exc:  # noqa: BLE001 - 转不出来不该拦住"录音已经能用"
                # 录音落盘了就是可用的参考音频；逐字稿只是顺手补的一栏。
                CHANNEL.warning(f"录音转写失败：{exc}", source="asr", file=str(path))
        return {"object": "recording", "path": str(path), "name": path.name, "text": text}

    @router.get("/recordings/{name}", summary="临时录音流", tags=["asr"])
    def recording_audio(name: str) -> FileResponse:
        """临时录音的试听地址。只在临时目录里找，不接受路径分隔符。"""
        clean = Path(name).name
        if clean != name:
            raise not_found("recording_not_found", "录音不存在。")
        target = recordings.root(create=False) / clean
        if not target.is_file():
            raise not_found("recording_not_found", "录音不存在（服务重启后临时目录会清空）。")
        return FileResponse(str(target))

    @router.post("/recordings/{name}/transcribe", summary="补听一段临时录音", tags=["asr"])
    def transcribe_recording(name: str) -> dict[str, Any]:
        """把一段已落盘的临时录音听成文字（「录音后自动转写」关闭时的手动入口）。

        只认临时目录里的文件名，不接受路径分隔符——与试听端点同一条边界。
        """
        clean = Path(name).name
        if clean != name:
            raise not_found("recording_not_found", "录音不存在。")
        target = recordings.root(create=False) / clean
        if not target.is_file():
            raise not_found("recording_not_found", "录音不存在（服务重启后临时目录会清空）。")
        return {"object": "transcript", **asr.transcribe_file(target)}


    # ---- 文案库 ----------------------------------------------------------

    @router.get("/texts", summary="读取文案库", tags=["texts"])
    def get_texts() -> dict[str, Any]:
        """分组的现成句子。**文件不在 = 默认那份**（不写盘）。"""
        return {"object": "texts", "groups": texts.load()}

    @router.put("/texts", summary="保存文案库", tags=["texts"])
    def put_texts(body: TextsIn) -> dict[str, Any]:
        """整份替换。坏形状与越界一律 400（不悄悄改用户敲的字）。"""
        try:
            saved = texts.save(body.groups)
        except texts.TextsError as exc:
            raise ApiError(status=400, code=exc.code, message=str(exc)) from exc
        return {"object": "texts", "groups": saved}

    @router.post("/texts/reset", summary="恢复默认文案", tags=["texts"])
    def reset_texts() -> dict[str, Any]:
        """改乱了回默认（会写盘，从此这份就是你改过的那一份）。"""
        return {"object": "texts", "groups": texts.reset()}


    # ---- 用户词表 ------------------------------------------------------

    @router.get("/vocab", summary="读取用户词表", tags=["vocab"])
    def get_vocab() -> dict[str, Any]:
        """情绪候选与引导指令预设：默认 / 删除 / 改名 / 自定义 / 生效。

        两节的默认都在仓库里（情绪词表与各引擎的 ``instructions.json``），
        用户差别在数据目录的两个文件上；``effective`` 就是各处实际看到的那一份。
        """
        return {"object": "vocab", **vocab_uc.state()}

    @router.put("/vocab", summary="保存用户词表", tags=["vocab"])
    def put_vocab(body: VocabIn) -> dict[str, Any]:
        """按节局部保存。坏形状与越界一律 400（不悄悄改用户敲的字）。"""
        try:
            saved = vocab_uc.save(
                emotions=body.emotions.model_dump() if body.emotions else None,
                instructions=body.instructions.model_dump() if body.instructions else None,
            )
        except VocabError as exc:
            raise ApiError(status=400, code=exc.code, message=str(exc)) from exc
        return {"object": "vocab", **saved}


    # ---- 读音替换 ------------------------------------------------------

    @router.get("/pronunciations", summary="读取读音替换", tags=["vocab"])
    def get_pronunciations() -> dict[str, Any]:
        """用户维护的「词 → 同音词」表；合成前按最长匹配作用到正文。"""
        return {"object": "pronunciations", **pronunciation_uc.state()}

    @router.put("/pronunciations", summary="保存读音替换", tags=["vocab"])
    def put_pronunciations(body: PronunciationsIn) -> dict[str, Any]:
        """整份替换。空词、重复、方括号与换行一律 400。"""
        try:
            saved = pronunciation_uc.save([entry.model_dump() for entry in body.entries])
        except pronunciation_uc.PronunciationError as exc:
            raise ApiError(status=400, code=exc.code, message=str(exc)) from exc
        return {"object": "pronunciations", **saved}


    # ---- 合成与产物 ------------------------------------------------------

    @router.post("/synthesize", summary="合成", tags=["synthesis"])
    async def synthesize(
        body: SynthesizeIn,
        idempotency_key: str = Header("", alias="Idempotency-Key", description="幂等键"),
    ) -> Any:
        """规范化 → 按引擎声明的契约校验 → 路由到引擎 → 按 ``persist`` 决定归属。

        ``persist="none"``（默认）：**不落盘**，响应体是音频字节，元数据在
        ``X-VoiceHub-Meta`` 头里（URL 编码的 JSON）。给第三方调用方。

        ``persist="cache"`` / ``"sample"``：落盘并返回 JSON（``SynthesisOut``），
        ``audio_url`` 可直接播放。前者进缓存目录（生成页），后者进音色保存目录
        并登记为该音色的样例（卡片上的「生成样例」）。

        ``Idempotency-Key``：同一键在 24 小时内重复提交时直接返回首次结果，
        不再次调用引擎（按量计费的引擎因此不会因网络重试重复计费）。
        ``persist="none"`` 的首次结果把音频一并记进幂等记录，重放时原样回字节。
        """
        if idempotency_key:
            payload = body.model_dump()
            # 读音表变了，同一段原文的发音就变了：指纹里带上版本，不能让旧结果
            # 冒充这次请求；没有替换条目时不加这一维，老键照旧命中。
            revision = pronunciation_uc.revision()
            if revision:
                payload["_pronunciation"] = revision
            fingerprint = _fingerprint(payload)
            # 先抢占再干活：同一键的两个并发请求（浏览器重试、双击、代理重发）
            # 从前会双双 miss，然后各打一次引擎——按量计费就是双倍。
            state, cached = await asyncio.to_thread(
                service.idempotent_begin, idempotency_key, fingerprint, ttl=_IDEMPOTENCY_TTL
            )
            if state == "replay":
                return _synthesis_replay(cached, body.persist)
            if state == "pending":
                raise ApiError(
                    status=409,
                    code="idempotency_in_progress",
                    message="同一个请求正在处理中，等它完成后再试。",
                )
        try:
            result = await gateway.synthesize(
                service,
                engine_key=body.engine,
                voice_id=body.voice_id,
                text=body.text,
                emotion=body.emotion,
                format=body.format,
                extra=body.extra,
            )
            if not result.audio:
                raise ApiError(
                    status=502, code="engine_no_audio", message="引擎未返回音频。", type="engine_error"
                )
            now = int(time.time())
            meta: dict[str, Any] = {
                "object": "synthesis",
                "id": f"syn-{now}-{uuid.uuid4().hex[:8]}",
                "created": now,
                "format": result.format,
                "cost_note": result.cost_note,
                # notes 保留为消息列表（兼容旧读取方）；warnings 带 code/source，供程序分支。
                "notes": [*result.notes, *(message for _, message in result.prep_notes)],
                "warnings": [
                    {"code": "engine_note", "source": "engine", "message": note}
                    for note in result.notes
                ]
                + [
                    {"code": code, "source": "hub", "message": message}
                    for code, message in result.prep_notes
                ],
                "subtitle_url": result.subtitle_url,
                "bytes": len(result.audio),
                "persist": body.persist,
            }
            if body.persist == "none":
                # 第三方那一路：产物归调用方，VoiceHub 不留下文件。
                if idempotency_key:
                    await asyncio.to_thread(
                        service.idempotent_store,
                        idempotency_key,
                        fingerprint,
                        {**meta, "audio_b64": base64.b64encode(result.audio).decode("ascii")},
                        ttl=_IDEMPOTENCY_TTL,
                    )
                return _binary_audio(meta, result.audio)

            # 归档名取自库中的音色记录，而不是请求体：调用方给出的是库内 id。
            # 查不到（上游传裸音色名且清单未入库）时用传入名归档——已合成成功的音频
            # 不应因归档名解析失败而丢弃。
            try:
                voice = await asyncio.to_thread(service.get_voice, body.voice_id)
            except LibraryError:
                voice = None
            # 取的是插件名，不是可用性：能走到这里说明这台引擎刚合成成功（网关只放
            # 已启用的进来），这里只要它目录名里那个中文标签。
            engine = list_engines().get(body.engine.strip())
            key = body.engine.strip()
            rel, absolute = await asyncio.to_thread(
                archive.write_audio,
                key,
                engine.label if engine else key,
                # 目录名使用稳定标识（音色 id / 目录名），不用显示名——显示名可改，
                # 改了之后同一音色的音频会落到另一个目录。
                # 只凭录音合成时库里没有音色，落进「录音」目录。
                voice.name if voice else (body.voice_id or "录音"),
                result.audio,
                fmt=result.format,
                voice_id=voice.id if voice else "",
                text=body.text,
                emotion=body.emotion,
                params=body.extra,
                as_sample=body.persist == "sample",
                # 平台音色在音色库里已经有家：样例就写回那个文件夹，别另起一个
                # 不会被扫描的引擎目录（下一批生成物否则又"不在库里"了）。
                folder=(
                    service.sample_folder(voice)
                    if voice is not None and body.persist == "sample"
                    else None
                ),
            )
            # 刚写进去的产物要立刻能列出来：产物索引按目录 mtime 缓存，这里主动作废这台引擎的。
            service.invalidate_generated(key)
            payload = {
                **meta,
                # 相对路径的归属由 engine 参数确定（每台引擎可配置各自的保存目录）。
                "audio_url": f"/api/v1/generated/{quote(rel)}?engine={quote(key)}",
                "saved_path": str(absolute),
                "saved_rel": rel,
            }
            if idempotency_key:
                await asyncio.to_thread(
                    service.idempotent_store,
                    idempotency_key,
                    fingerprint,
                    payload,
                    ttl=_IDEMPOTENCY_TTL,
                )
            return payload
        except BaseException:
            # 引擎报错 / 归档失败 / 请求被取消：占位必须放开，否则这个键会锁到过期。
            if idempotency_key:
                await asyncio.to_thread(
                    service.idempotent_abandon, idempotency_key, fingerprint
                )
            raise

