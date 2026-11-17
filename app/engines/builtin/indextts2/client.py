"""IndexTTS2 的 HTTP 调用——打 audio.cpp hub 的 ``POST /v1/audio/speech``。

与 DotTTS 是同一套端点，只是服务实例与模型 id 不同。错误解析、响应校验与连接池
的建法在 :mod:`app.engines.audio_http`。
"""

from __future__ import annotations

import json
from typing import Any

from app.engines.audio_http import (
    audio_bytes,
    error_detail,
    loggable_body,
    net_error,
    open_client,
)
from app.engines.base import CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.builtin.indextts2.settings import IndexTts2Settings
from app.engines.http_pool import post_within
from app.infra.loghub import CHANNEL

__all__ = ["SYNTH_PATH", "synthesize"]

SYNTH_PATH = "/v1/audio/speech"


async def synthesize(
    cfg: IndexTts2Settings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成。``body`` 由 :meth:`IndexTts2Engine.build_body` 装配好。"""
    import httpx

    # 把**真正发出去的那一份**写进日志：参数没生效时，第一个要看的就是它。
    CHANNEL.info(
        f"{label} 请求：" + json.dumps(loggable_body(body), ensure_ascii=False),
        source="engine",
        engine="indextts2",
    )
    try:
        async with open_client(cfg) as client:
            response = await post_within(
                client, SYNTH_PATH, seconds=float(cfg.timeout), json=body
            )
    except httpx.HTTPError as exc:
        raise net_error(exc, cfg, label=label, code=CODE_UNREACHABLE) from exc

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    audio = response.content
    CHANNEL.info(
        f"{label} 响应：{response.status_code} / {len(audio)} 字节 / {content_type or '未提供类型'}",
        source="engine",
        engine="indextts2",
    )

    if response.status_code >= 400:
        raise EngineError(f"{label} 拒绝了这次合成：{error_detail(response)}")
    audio = audio_bytes(response, label, hint="可能是文本为空或参考音频未读取")

    # 本机推理没有计费，也没有别的格式：audio.cpp 这一路回的就是 wav。
    return SynthesisResult(audio=audio, format="wav")
