"""Qwen3-ASR 的调用口径；HTTP 实现在 :mod:`app.engines.asr_http`。

端点、multipart 上传、WAV 转换与错误信封与 SenseVoice 完全共用，这里只钉住
Qwen3-ASR 在日志与报错里的名字，并保留插件和脚本沿用的函数入口。

端点（OpenAI 兼容）：

=============================  ====================================================
用途                           端点
=============================  ====================================================
识别（只要文字）               ``POST /v1/audio/transcriptions``
识别（含语种等富字段）         ``POST /v1/audio/transcriptions/details``
服务上加载了什么（"测试连接"用） ``GET  /v1/models``
=============================  ====================================================
"""

from __future__ import annotations

from pathlib import Path

from app.engines import asr_http
from app.engines.asr_base import Transcript
from app.engines.builtin.qwen3_asr.settings import Qwen3AsrSettings

__all__ = [
    "ASR_RATE",
    "DETAIL_PATH",
    "MODELS_PATH",
    "PLAIN_PATH",
    "parse_transcript",
    "probe",
    "transcribe",
]

ASR_RATE = asr_http.ASR_RATE
PLAIN_PATH = asr_http.PLAIN_PATH
DETAIL_PATH = asr_http.DETAIL_PATH
MODELS_PATH = asr_http.MODELS_PATH
parse_transcript = asr_http.parse_transcript


def transcribe(cfg: Qwen3AsrSettings, path: Path) -> Transcript:
    return asr_http.transcribe(cfg, path, engine="qwen3_asr", label="Qwen3-ASR")


def probe(cfg: Qwen3AsrSettings) -> tuple[bool, str]:
    return asr_http.probe(cfg, label="Qwen3-ASR")
