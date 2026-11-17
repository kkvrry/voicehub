"""DotTTS 的 HTTP 调用——打 audio.cpp 推理服务的 ``POST /v1/audio/speech``。

与 IndexTTS2 是同一台服务、同一套端点，只是标签与模型 id 不同。错误解析、响应
校验与连接池的建法在 :mod:`app.engines.audio_http`。
"""

from __future__ import annotations

import json
from typing import Any

from app.engines.audio_http import (
    MIN_AUDIO_BYTES,
    error_detail,
    loggable_body,
    net_error,
    open_client,
)
from app.engines.base import CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.builtin.dots_tts.settings import DotsTtsSettings
from app.engines.http_pool import post_within
from app.infra.loghub import CHANNEL

__all__ = ["MIN_AUDIO_BYTES", "SYNTH_PATH", "synthesize"]

SYNTH_PATH = "/v1/audio/speech"


async def synthesize(cfg: DotsTtsSettings, body: dict[str, Any]) -> SynthesisResult:
    """合成。``body`` 由 :meth:`DotsTtsEngine.build_body` 装配好。"""
    import httpx

    # 把**真正发出去的那一份**写进日志：参数没生效时，第一个要看的就是它。
    CHANNEL.info(
        "DotTTS 请求：" + json.dumps(loggable_body(body), ensure_ascii=False),
        source="engine",
        engine="dots_tts",
    )
    try:
        async with open_client(cfg) as client:
            response = await post_within(
                client, SYNTH_PATH, seconds=float(cfg.timeout), json=body
            )
    except httpx.HTTPError as exc:
        raise net_error(exc, cfg, label="DotTTS", code=CODE_UNREACHABLE) from exc

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    audio = response.content
    CHANNEL.info(
        f"DotTTS 响应：{response.status_code} / {len(audio)} 字节 / {content_type or '未提供类型'}",
        source="engine",
        engine="dots_tts",
    )

    if response.status_code >= 400:
        raise EngineError(f"DotTTS 拒绝了这次合成：{error_detail(response)}")
    if not content_type.startswith("audio/"):
        raise EngineError(
            f"DotTTS 返回 {response.status_code} 但响应不是音频"
            f"（content-type={content_type or '未提供'}，"
            f"开头 {audio[:60].decode('utf-8', 'replace')!r}）。"
        )
    if len(audio) < MIN_AUDIO_BYTES:
        raise EngineError(
            f"DotTTS 返回的音频仅 {len(audio)} 字节，低于合理的最小长度"
            "（可能是文本为空或参考音频未读取）。"
        )

    # 本机推理没有计费，也没有别的格式：audio.cpp 这一路回的就是 wav。
    return SynthesisResult(audio=audio, format="wav")
