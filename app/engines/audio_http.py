"""引擎响应的公共部分：连接、请求体日志、错误解析、响应校验。

各插件仍然负责自己的文案（报错时挂哪个引擎名、4xx 时补哪一句），这里只统一形状：
连接池怎么建、错误信封怎么拆、响应算不算音频、多长才算音频。

:func:`speak` 只服务同一套 ``POST /v1/audio/speech`` 的三台本机引擎
（CosyVoice3 / OmniVoice / VoxCPM2）；其余引擎的合成流程各有分岔，只用这里的
若干小工具，不套这个函数。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engines.base import CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.http_pool import borrow, post_within
from app.infra.loghub import CHANNEL

__all__ = [
    "AUDIO_SPEECH_PATH",
    "MIN_AUDIO_BYTES",
    "SpeechEndpoint",
    "audio_bytes",
    "ensure_audio_size",
    "error_detail",
    "error_text",
    "loggable_body",
    "net_error",
    "open_client",
    "speak",
]

#: audio.cpp 与 OpenAI 兼容实现的音频端点。
AUDIO_SPEECH_PATH = "/v1/audio/speech"

#: 低于这个字节数当"只有文件头"（wav 头 44 字节）。
MIN_AUDIO_BYTES = 200


@dataclass(frozen=True, slots=True)
class SpeechEndpoint:
    """一台引擎在 ``/v1/audio/speech`` 上的差异：只有名字。

    :param label: 日志与报错里的引擎名（``CosyVoice3``）。
    :param engine: 日志里的引擎键（``cosyvoice3``）。
    :param unknown_model_hint: 4xx 原话里出现 ``unknown model`` 时，补一句模型 id
        要对齐（这一句由端点自己说，因为只有它知道 ``cfg.model`` 的来历）。
    """

    label: str
    engine: str
    unknown_model_hint: bool = True


def open_client(cfg: Any, **kw: Any):
    """按设置里的地址与超时借一个连接。``trust_env=False``：本机请求不走系统代理。"""
    return borrow(base_url=cfg.host, timeout=float(cfg.timeout), trust_env=False, **kw)


def error_text(payload: Any) -> str:
    """**已解析**的错误体 → 那句原话；认不出错误信封时返回空串（= 这里没有错误）。

    三家信封的形状都在这里认：

    - OpenAI 风格：``{"error": {"message": ...}}`` / ``{"error": "..."}``；
    - GPT-SoVITS 的 ``tts_handle``：``{"message": "tts failed", "Exception": ...}``
      （只说 ``message`` 等于没说，真正的原因在 ``Exception``）；
    - FastAPI 自己的校验失败：``{"detail": [...]}``——原样带上，它写了缺哪个字段。

    "有没有错误"这个判断要留给调用方（有的平台 200 里也藏错误，见 MiMo），所以
    **认不出来就是空串**，不替它编一句。响应还没解析时用 :func:`error_detail`。
    """
    if not isinstance(payload, dict):
        return str(payload)[:200]
    envelope = payload.get("error")
    if isinstance(envelope, dict):
        message = str(envelope.get("message") or "").strip()
        if message:
            return message
    elif isinstance(envelope, str) and envelope.strip():
        return envelope.strip()
    message = str(payload.get("message") or "").strip()
    detail = str(payload.get("Exception") or "").strip()
    if message and detail:
        return f"{message}：{detail}"
    if message or detail:
        return message or detail
    if payload.get("detail"):
        return str(payload["detail"])[:300]
    return ""


def error_detail(response: Any) -> str:
    """4xx/5xx 的响应体 → 一句可读说明；认不出信封就退回整个响应体截断。"""
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] if text else "（响应体是空的）"
    return error_text(payload) or str(payload)[:200] or "（响应体是空的）"


def ensure_audio_size(audio: bytes, label: str, *, hint: str = "") -> bytes:
    """音频字节太短时拦下；``hint`` 是"可能是什么原因"的短句。

    只抓"只有文件头"那种空音频：wav 头就有 44 字节，正常句子以 KB 计。
    从 JSON 里解出音频的引擎（base64 / hex）也走这一道。
    """
    if len(audio) >= MIN_AUDIO_BYTES:
        return audio
    text = f"{label} 返回的音频仅 {len(audio)} 字节，低于合理的最小长度"
    raise EngineError(f"{text}（{hint}）。" if hint else f"{text}。")


def audio_bytes(
    response: Any, label: str, *, hint: str = "可能是正文为空或参考音频未读取"
) -> bytes:
    """成功响应 → 音频字节；非 ``audio/*`` 或过短都在这里拦下。

    只服务"响应体本身就是音频"的引擎。JSON 里带 base64 / hex 的引擎先解出字节，
    再调 :func:`ensure_audio_size`。
    """
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    audio = response.content
    if not content_type.startswith("audio/"):
        raise EngineError(
            f"{label} 返回 {response.status_code} 但响应不是音频"
            f"（content-type={content_type or '未提供'}，"
            f"开头 {audio[:60].decode('utf-8', 'replace')!r}）。"
        )
    return ensure_audio_size(audio, label, hint=hint)


def loggable_body(
    body: dict[str, Any],
    *,
    path_keys: Iterable[str] = ("voice_ref",),
    text_keys: Iterable[str] = ("input", "reference_text"),
    limit: int = 160,
) -> dict[str, Any]:
    """日志里那份请求体：参考音频只留文件名或长度，长文本截断。"""
    compact = dict(body)
    for key in path_keys:
        value = compact.get(key)
        if isinstance(value, dict):
            data = value.get("data")
            if data:
                compact[key] = {**value, "data": f"<{len(str(data))} chars>"}
        elif value:
            compact[key] = Path(str(value)).name
    for key in text_keys:
        value = compact.get(key)
        if isinstance(value, str) and len(value) > limit:
            compact[key] = value[:limit] + "…"
    return compact


def net_error(
    exc: Exception,
    cfg: Any,
    *,
    label: str,
    code: str = "engine_failed",
    step: str = "",
    timeout_hint: str = "",
) -> EngineError:
    """网络层失败 → 一句可读说明：哪个引擎、哪个地址、怎么失败。

    **只说事实，不推断原因。** 连接被拒可能是服务没起、端口改过、或者刚崩掉，
    三者在本端看起来一模一样；用户装的也不一定是这一台引擎（同一个人可能刚换过），
    替他说"去启动那个 exe"多半指向错的东西。服务端自己回的错误才原样带出来。

    :param label: 引擎显示名，接在地址前面（``BreezeTTS 2（http://…）无法连接``）。
    :param step: 失败的**动作**（``合成`` / ``拉音色清单``）。给了就顶替地址——
        一台引擎有多个动作时，说清"哪一步"比说地址有用（地址只有一个）。
    :param timeout_hint: 超时那一支追加的一句；推理型引擎要说明"等得久是正常的"。
    """
    import httpx

    where = step or cfg.host
    if isinstance(exc, httpx.ConnectError):
        return EngineError(f"{label}（{where}）无法连接", code=code)
    if isinstance(exc, httpx.TimeoutException):
        seconds = float(getattr(cfg, "timeout", 0) or 0)
        text = f"{label}（{where}）响应超时"
        if seconds:
            text += f"（{seconds:.0f} 秒）"
        return EngineError(f"{text}。{timeout_hint}" if timeout_hint else text)
    return EngineError(f"{label}（{where}）请求失败：{exc}")


async def speak(
    cfg: Any, body: dict[str, Any], endpoint: SpeechEndpoint
) -> SynthesisResult:
    """``POST /v1/audio/speech``：503 可重试、4xx 报原话、非音频与过短音频拦下。"""
    import httpx

    label = endpoint.label
    # 把**真正发出去的那一份**写进日志：参数没生效时，第一个要看的就是它。
    CHANNEL.info(
        f"{label} 请求：" + json.dumps(loggable_body(body), ensure_ascii=False),
        source="engine",
        engine=endpoint.engine,
    )
    try:
        async with open_client(cfg) as client:
            response = await post_within(
                client, AUDIO_SPEECH_PATH, seconds=float(cfg.timeout), json=body
            )
    except httpx.HTTPError as exc:
        raise net_error(exc, cfg, label=label, code=CODE_UNREACHABLE) from exc

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    audio = response.content
    CHANNEL.info(
        f"{label} 响应：{response.status_code} / {len(audio)} 字节"
        f" / {content_type or '未提供类型'}",
        source="engine",
        engine=endpoint.engine,
    )

    if response.status_code == 503:
        # server_busy / insufficient_memory：过一会儿再发就行，不是这次参数的问题。
        raise EngineError(f"{label} 暂时出不了声：{error_detail(response)}", retryable=True)
    if response.status_code >= 500:
        # 5xx 多数是环境或模型问题（缺权重、显存不够），重发一次不会变好。
        raise EngineError(f"{label} 出不了声：{error_detail(response)}")
    if response.status_code >= 400:
        detail = error_detail(response)
        message = f"{label} 拒绝了这次合成：{detail}"
        if endpoint.unknown_model_hint and "unknown model" in detail.lower():
            message += (
                f"「模型」需与服务端注册的 id 一致（当前 {cfg.model}），"
                "可用 GET /v1/models 查看。"
            )
        raise EngineError(message)
    audio = audio_bytes(response, label)

    # 本机推理没有计费，也没有别的格式：这一路回的就是 wav。
    return SynthesisResult(audio=audio, format="wav")
