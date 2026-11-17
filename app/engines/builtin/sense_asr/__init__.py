"""SenseVoice-Small 识别插件包。

目录分工与 Qwen3-ASR 一致：``plugin.py``（入口 + 能力）、``settings.py``（配置）、
``languages.py``（语言清单）。HTTP 调用与 Qwen3-ASR 共用
:mod:`app.engines.asr_http`——两家在 audio.cpp 上是同一套线协议。
"""

from app.engines.builtin.sense_asr.plugin import SenseAsrEngine

__all__ = ["SenseAsrEngine"]
