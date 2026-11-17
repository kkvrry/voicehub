"""MiMo 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

端点与字段照官方文档（``mimo.mi.com/docs``，OpenAI 兼容）：

==============  ====================================  ============================
用途            端点                                    形状
==============  ====================================  ============================
合成            ``POST /v1/chat/completions``          JSON；音频在
                                                        ``choices[0].message.audio.data``
                                                        （**base64 串**）
==============  ====================================  ============================

**四个不显然的地方**，都体现在代码里：

1. **请求头两份一起发**：官方 Python 示例走 ``openai`` SDK，发的是
   ``Authorization: Bearer``；而文档页与社区文章写的是 ``api-key:``。两处不一致，
   多发一个头不花钱，少发一个可能 401——所以 :func:`headers` 两个都带上。

2. **要念的台词在 assistant 消息里**，风格指令在 user 消息里（官方规定）。
   位置反了的症状是"用户听见自己的指令被念出来"，而请求照样 200——装配在
   :mod:`plugin` 里，这里只负责发。

3. **错误是 OpenAI 那套信封**（``{"error": {"message": ...}}``），且 4xx 与
   "200 里藏错误"两种都出现过。每一次响应都先拆信封再挖音频，顺序反了的话，
   拒绝时 ``audio.data`` 是空的，能挖到的只有一句"没返回音频"。

4. **pcm16 是裸样，没有文件头**。下游要的是能落盘的文件，所以 :func:`synthesize`
   会把裸样包进 wav 壳（24kHz / 16bit / 单声道，官方文档原话）再交出去。
   内容一个字节不改，只是让它能存能播。
"""

from __future__ import annotations

import base64
import binascii
import io
import wave
from pathlib import Path
from typing import Any

from app.engines.audio_http import MIN_AUDIO_BYTES, ensure_audio_size, error_text, net_error
from app.engines.base import CODE_RATE_LIMITED, CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.builtin.mimo.settings import MimoSettings
from app.engines.http_pool import borrow, post_within
from app.infra.ffmpeg import container_of_bytes
from app.infra.media import audio_mime

__all__ = [
    "MAX_SAMPLE_BASE64",
    "MIN_AUDIO_BYTES",
    "SYNTH_PATH",
    "headers",
    "synthesize",
    "voice_sample_data_uri",
]

SYNTH_PATH = "/v1/chat/completions"

#: 复刻样本的上限（官方原话：base64 后的字符串不超过 10 MB）。
#: base64 比原文件大约 1/3，所以原文件约 7.5 MB 往上就会撞线。
MAX_SAMPLE_BASE64 = 10 * 1024 * 1024

#: 样本能用的容器（官方：仅支持 mp3 与 wav）。
_SAMPLE_FORMATS = ("wav", "mp3")

#: pcm16 的采样参数（官方文档：24kHz / 16bit / 单声道）。
_PCM_RATE = 24000


def headers(cfg: MimoSettings) -> dict[str, str]:
    """认证头**两份一起发**（理由见模块顶部第 1 条）。"""
    key = cfg.api_key.strip()
    return {"Authorization": f"Bearer {key}", "api-key": key}


def _timeout(cfg: MimoSettings) -> float:
    return float(cfg.timeout)


#: 限流文案里的关键词（这一家的 429 在 OpenRouter 风格信封里，措辞不固定）。
_RATE_WORDS = ("rate limit", "ratelimit", "too many requests", "请求过于频繁", "限流")


def _is_rate_limit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _RATE_WORDS)


async def _client(cfg: MimoSettings, **kw: Any):  # 返回类型不外露：httpx 是可选细节
    # ``trust_env=True``：与修复前一致——云端引擎在用户配了 HTTP(S)_PROXY 的机器上
    # 要照常走代理；本机引擎才需要 ``trust_env=False``（见那些模块顶部的说明）。
    return borrow(
        base_url=cfg.host,
        headers=headers(cfg),
        timeout=_timeout(cfg),
        trust_env=True,
        follow_redirects=True,
        **kw,
    )


def _net_error(exc: Exception, cfg: MimoSettings, label: str) -> EngineError:
    """网络层失败 → 可读说明。超时那一支要说清"等得久是正常的"。"""
    return net_error(
        exc,
        cfg,
        label=label,
        code=CODE_UNREACHABLE,
        timeout_hint="整段合成要等推理跑完才返回，长文本放慢——要么把超时调大，要么把文本切短。",
    )


def _payload_of(response: Any, what: str, label: str) -> dict[str, Any]:
    """响应 → JSON 对象；不像 JSON 就直说，不让它掉到"没返回音频"那一步。"""
    try:
        payload = response.json()
    except ValueError as exc:
        preview = (response.text or "")[:200].strip()
        raise EngineError(
            f"{label} {what}的响应不是 JSON（{exc}）：{preview or '（响应体是空的）'}"
        ) from exc
    if not isinstance(payload, dict):
        raise EngineError(
            f"{label} {what}的响应是个 {type(payload).__name__}，不是 JSON 对象"
        )
    return payload


def _audio_of(payload: dict[str, Any], what: str, label: str) -> str:
    """``choices[0].message.audio.data`` → base64 串。

    **先看 ``finish_reason``**：内容被过滤时这一格整个不存在，而那句话要说的
    是"这段文本平台不给念"，不是"平台坏了"。
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise EngineError(f"{label} {what}的响应里没有 choices——原样返回：{str(payload)[:200]}")
    choice = choices[0]
    reason = str(choice.get("finish_reason") or "").strip()
    if reason == "content_filter":
        raise EngineError(
            f"{label} {what}被内容过滤拦下了（finish_reason=content_filter）——"
            "这段文本平台不给念，换一段或改个说法。"
        )
    message = choice.get("message")
    audio = message.get("audio") if isinstance(message, dict) else None
    data = str(audio.get("data") or "").strip() if isinstance(audio, dict) else ""
    if not data:
        raise EngineError(
            f"{label} {what}没有返回音频（choices[0].message.audio.data 是空的）"
            + (f"，finish_reason={reason}" if reason else "")
        )
    return data


def _wrap_pcm(raw: bytes) -> bytes:
    """裸 PCM16 → wav 壳（24kHz / 单声道 / 16bit）。**内容一个字节不改。**"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_PCM_RATE)
        handle.writeframes(raw)
    return buffer.getvalue()


def voice_sample_data_uri(path: str, *, label: str) -> str:
    """复刻样本 → ``data:{mime};base64,...``（官方要的那个形状）。

    **这一家不收路径**（``audio.voice`` 要么是预置音色 id，要么是音频样本的
    base64），所以参考音频只在本机被读一次，发出去的是音频本身。

    :raises EngineError: 读不到 / 不是 mp3 或 wav / 编码后超过 10 MB——
        **都在发出去之前**报出来，而且每一句都带上"那该怎么改"。
    """
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise EngineError(
            f"无法读取参考音频：{path}（{exc.strerror or exc}）。"
            "该字段需要本机文件路径，请确认文件存在且未被移动。"
        ) from exc
    if not raw:
        raise EngineError(f"参考音频为空文件：{path}。")

    container = container_of_bytes(raw[:16])
    if container not in _SAMPLE_FORMATS:
        raise EngineError(
            f"{label} 的复刻样本仅支持 mp3 与 wav，而「{source.name}」为"
            f"{container or '无法识别的格式'}（文件头是 {raw[:4]!r}）。"
        )
    encoded = base64.b64encode(raw).decode("ascii")
    if len(encoded) > MAX_SAMPLE_BASE64:
        raise EngineError(
            f"复刻样本过大：{len(raw) / 1024 / 1024:.1f} MB（编码后 "
            f"{len(encoded) / 1024 / 1024:.1f} MB），超出平台上限 "
            f"{MAX_SAMPLE_BASE64 / 1024 / 1024:.0f} MB。"
            "复刻只需要一小段干净人声，请裁剪到十几秒。"
        )
    return f"data:{audio_mime(container)};base64,{encoded}"


async def synthesize(
    cfg: MimoSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成（``POST /v1/chat/completions``）。``body`` 由 :mod:`plugin` 装配好。"""
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")
    wanted = str((body.get("audio") or {}).get("format") or "wav").strip().lower()
    try:
        async with await _client(cfg) as client:
            response = await post_within(client, SYNTH_PATH, seconds=_timeout(cfg), json=body)
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, label) from exc

    if response.status_code >= 400:
        # 4xx/5xx：错误在信封里，先拆再说——原样带平台那句话，不接自己的解释。
        try:
            detail = error_text(response.json())
        except ValueError:
            detail = (response.text or "").strip()[:200]
        limited = response.status_code == 429 or _is_rate_limit(detail)
        raise EngineError(
            f"{label} 合成失败：HTTP {response.status_code}。{detail or '（响应体是空的）'}",
            retryable=limited,
            code=CODE_RATE_LIMITED if limited else "engine_failed",
        )

    payload = _payload_of(response, "合成", label)
    error = error_text(payload)
    if error:
        # 200 里藏错误（这一家的拒绝不在状态码里）。
        limited = _is_rate_limit(error)
        raise EngineError(
            f"{label} 拒绝了这次合成：{error}",
            retryable=limited,
            code=CODE_RATE_LIMITED if limited else "engine_failed",
        )

    data = _audio_of(payload, "合成", label)
    try:
        # ``validate=True``：非 base64 字符当场报错，而不是被静默跳过——
        # 跳过的话解出来的是一段缺字节的音频，症状是"声音缺了一块"，查无从查。
        audio = base64.b64decode(data, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise EngineError(
            f"{label} 返回的音频不是合法的 base64（{exc}）——开头 40 个字符是 {data[:40]!r}"
        ) from exc

    if wanted in ("pcm", "pcm16"):
        # 平台给的是 24kHz 裸样，没有文件头——包一层 wav 壳再交出去
        # （内容不变，只是能落盘；见模块顶部第 4 条）。
        audio = _wrap_pcm(audio)
        wanted = "wav"
    audio = ensure_audio_size(audio, label, hint="可能是文本为空或被过滤")
    # 计费换算不出音频时长（响应只回 token 用量），所以不报 cost_note——空着，不编。
    return SynthesisResult(audio=audio, format=wanted or "wav")
