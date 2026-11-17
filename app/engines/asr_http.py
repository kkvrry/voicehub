"""OpenAI 兼容识别端点（``/v1/audio/transcriptions``）的公共实现。

Qwen3-ASR 与 SenseVoice 在 audio.cpp 上的线协议相同：multipart 上传 WAV，
``/v1/audio/transcriptions`` 回 ``text`` + ``timing``，``/details`` 多一个
``language``。两家插件只保留各自的配置、能力声明与错误口径；"怎么发"在这里。

**音频总是上传，不给"本机路径 / 上传"二选一。** 服务端路径只有服务与本机
共享文件系统时才成立；上传在本地、局域网、云端都成立。这两种发送方式不是用户
该做的决定——音频本来就在项目手里，识别什么也由项目决定。

**只发 WAV。** 服务端核心音频路由接收 WAV；MP3 / FLAC 需要额外的解码前端，
是否启用取决于部署。为了让同一份引擎在"开了"和"没开"的服务上都工作，非 WAV
先用 ffmpeg 转成 16 kHz 单声道 PCM16（临时文件，识别完即删）。

**本机请求必须绕开系统代理**（``ProxyHandler({})``）。这条是踩过的：环境里配了
``HTTP_PROXY`` 时，发给 ``127.0.0.1`` 的请求会被送到代理，拿回来的是
``upstream connect failed: 由于目标计算机积极拒绝``——那句话在说代理的 upstream，
而真正的问题是"本机这个端口没人监听"。查错方向会被带偏。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Protocol

from app.engines.asr_base import AsrError, Transcript
from app.engines.audio_http import error_text
from app.infra import ffmpeg
from app.infra.loghub import CHANNEL

__all__ = [
    "ASR_RATE",
    "DETAIL_PATH",
    "MODELS_PATH",
    "PLAIN_PATH",
    "AsrServiceConfig",
    "parse_transcript",
    "probe",
    "transcribe",
]

PLAIN_PATH = "/v1/audio/transcriptions"
DETAIL_PATH = "/v1/audio/transcriptions/details"
MODELS_PATH = "/v1/models"

#: 这些服务的输入采样率。上传前统一到单声道 16 kHz PCM16。
ASR_RATE = 16000

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AsrServiceConfig(Protocol):
    """两家识别插件配置里参与 HTTP 调用的那几项。"""

    base_url: str
    model: str
    details: bool
    language: str
    timeout: float


def _base(cfg: AsrServiceConfig, label: str) -> str:
    """服务地址：去掉尾部斜杠，空则报清楚（而不是拼出 ``/v1/...``）。"""
    base = (cfg.base_url or "").strip().rstrip("/")
    if not base:
        raise AsrError(f"识别服务地址为空，请在「引擎 → 识别」里填 {label} 的服务地址。")
    return base


def _upstream_message(raw: bytes) -> str:
    """上游错误体里的那句话。**只报它的原话，不追加自己编的解释。**"""
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    # 信封的形状归 ``error_text`` 管（OpenAI 那套 / FastAPI 的 detail / message）。
    return error_text(payload) or text[:300]


def _post(url: str, body: bytes, content_type: str, timeout: float, *, label: str) -> bytes:
    """发一次请求，把"连不上"与"它拒绝了"分开说。"""
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": content_type}
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        message = _upstream_message(exc.read())
        raise AsrError(
            f"识别服务拒绝了这次请求（HTTP {exc.code}）：{message or exc.reason}",
            retryable=exc.code >= 500,
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        # 系统的连接错误自带句号（``…，无法连接。``），直接再拼一个就是"。。"。
        detail = str(reason).strip().rstrip("。")
        raise AsrError(
            f"连不上识别服务（{url}）：{detail}。"
            f"{label} 服务没起、或端口与「服务地址」不一致时都是这条。",
            retryable=True,
        ) from exc
    except TimeoutError as exc:
        raise AsrError(f"识别超时（{timeout:g}s）：音频可能太长，可调大超时。", retryable=True) from exc


def _as_wav(path: Path, *, label: str, engine: str) -> tuple[Path, str, bool]:
    """把待识别音频变成 WAV。返回 ``(文件, 原格式, 是否是临时文件)``。

    本来就是 WAV 时原格式为空串、不是临时文件（那是调用方的文件，不能删）。
    转出来的那份是**一次性**的：识别完就删，不留在缓存里（``ffmpeg.to_wav(transient=True)``）。
    """
    actual = ffmpeg.container_of(path) or path.suffix.lstrip(".").lower()
    if actual == "wav":
        return path, "", False
    try:
        converted = ffmpeg.to_wav(path, rate=ASR_RATE, transient=True)
    except ffmpeg.FfmpegError as exc:
        raise AsrError(
            f"{label} 只接收 WAV，而「{path.name}」为 {actual or '无法识别的格式'}；"
            f"转换失败：{exc.reason}"
        ) from exc
    CHANNEL.info(
        f"识别音频「{path.name}」为 {actual or '无法识别的格式'}，"
        f"已转换为 16 kHz 单声道 WAV：{converted.name}",
        source="asr",
        engine=engine,
        from_format=actual or "unknown",
    )
    return converted, actual or "unknown", True


def _multipart_body(cfg: AsrServiceConfig, path: Path) -> tuple[bytes, str]:
    """multipart：文件名固定 ASCII——服务端解的是字节，名字带中文只多一层麻烦。"""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )

    field("model", cfg.model)
    field("response_format", "verbose_json")
    if cfg.language.strip():
        field("language", cfg.language.strip())
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def parse_transcript(payload: Any, *, seconds: float = 0.0, model: str = "") -> Transcript:
    """响应体 → :class:`Transcript`。缺字段就是空串，**不猜**。

    兼容两种形状：``{"text": …}`` 与 OpenAI 的 ``{"text": …, "language": …}``；
    多出来的字段原样留在 ``extra``（排查"它到底给了什么"时有用）。
    """
    if not isinstance(payload, dict):
        raise AsrError("识别服务的响应不是 JSON 对象。")
    text = str(payload.get("text") or "").strip()
    known = {"text", "language", "emotion", "timing"}
    extra = {key: value for key, value in payload.items() if key not in known}
    return Transcript(
        text=text,
        language=str(payload.get("language") or "").strip(),
        emotion=str(payload.get("emotion") or "").strip(),
        seconds=round(float(seconds), 2),
        model=model,
        extra=extra,
    )


def transcribe(
    cfg: AsrServiceConfig, path: Path, *, engine: str, label: str
) -> Transcript:
    """识别一个文件：非 WAV 先转 WAV，再 multipart 上传。"""
    base = _base(cfg, label)
    target = path.resolve() if not path.is_absolute() else path
    if not target.is_file():
        raise AsrError(f"待识别音频不存在：{path}")
    working, from_format, temporary = _as_wav(target, label=label, engine=engine)
    try:
        url = base + (DETAIL_PATH if cfg.details else PLAIN_PATH)
        body, content_type = _multipart_body(cfg, working)
        started = time.perf_counter()
        raw = _post(url, body, content_type, float(cfg.timeout), label=label)
        seconds = time.perf_counter() - started
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise AsrError(f"识别服务返回的不是 JSON：{raw[:120]!r}") from exc
        result = parse_transcript(payload, seconds=seconds, model=cfg.model)
        if from_format:
            result.extra["transcoded_from"] = from_format
        return result
    finally:
        # 一次性转码：上传完就没用了（失败也一样），不留在盘上。
        if temporary:
            working.unlink(missing_ok=True)


def probe(cfg: AsrServiceConfig, *, label: str) -> tuple[bool, str]:
    """``GET /v1/models``：能连上就顺便报出它有哪些模型（"我是不是指错端口"）。"""
    base = _base(cfg, label)
    try:
        with _OPENER.open(base + MODELS_PATH, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return False, f"服务在，但 {MODELS_PATH} 回了 HTTP {exc.code}。"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"连不上 {base}：{reason}"
    except json.JSONDecodeError:
        return False, f"{base}{MODELS_PATH} 回的不是 JSON。"
    models = payload.get("data") if isinstance(payload, dict) else None
    names = [str(item.get("id", "")) for item in models or [] if isinstance(item, dict)]
    if not names:
        return False, f"连上了，但它没报出任何模型（{base}{MODELS_PATH}）。"
    if cfg.model and cfg.model not in names:
        return False, f"连上了，但没有「{cfg.model}」这个模型。它有的是：{'、'.join(names)}。"
    return True, f"可用模型：{'、'.join(names)}。"
