"""Qwen3-ASR 识别插件包。

目录分工与合成引擎一致：``plugin.py``（入口 + 能力）、``settings.py``（配置模型）、
``client.py``（怎么调）、``icon.svg``（图标）。**没有** ``voices.py`` / ``mapping.py``
——识别没有音色，也没有"统一参数落到各家"这件事（它只收一段音频）。
"""

from app.engines.builtin.qwen3_asr.plugin import Qwen3AsrEngine

__all__ = ["Qwen3AsrEngine"]
