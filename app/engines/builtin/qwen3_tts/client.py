"""Qwen3-TTS 的 HTTP 调用——打 audio.cpp 的 ``POST /v1/audio/speech``。

与其余几台本机引擎同一层接口，只是模型家族不同。发请求、认错误、认"回的不是
音频"、认"服务正忙"都在 :mod:`app.engines.audio_http`，这里只声明本引擎的名字。
"""

from __future__ import annotations

from typing import Any

from app.engines.audio_http import SpeechEndpoint, speak
from app.engines.base import SynthesisResult
from app.engines.builtin.qwen3_tts.settings import Qwen3TtsSettings

__all__ = ["synthesize"]

_ENDPOINT = SpeechEndpoint(label="Qwen3-TTS", engine="qwen3_tts")


async def synthesize(cfg: Qwen3TtsSettings, body: dict[str, Any]) -> SynthesisResult:
    """合成。``body`` 由 :meth:`Qwen3TtsEngine.build_body` 装配好。"""
    return await speak(cfg, body, _ENDPOINT)
