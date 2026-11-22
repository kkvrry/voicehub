"""Qwen3-TTS 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/voxcpm2/`` 同一形状）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

接的是 audio.cpp 的 ``qwen3_tts`` 家族（``POST /v1/audio/speech``）的 **Base 权重**：
12 Hz 语音 token、10 种语言、3 秒参考音频克隆。它只有克隆一条路——没有内置音色、
没有音色设计、没有指令与情绪，说话方式全部来自参考音频（见 :mod:`plugin`）。
注册表扫到本目录的 :class:`Qwen3TtsEngine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.qwen3_tts.plugin import Qwen3TtsEngine

__all__ = ["Qwen3TtsEngine"]
