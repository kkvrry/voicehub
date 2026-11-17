"""IndexTTS2 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/breeze_tts/`` 同一形状，便于对照）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

**它接的是 audio.cpp 那一层**（OpenAI 兼容端点 ``/v1/audio/speech``），
与 BreezeTTS 2 是同一套推理框架、不同的模型。注册表扫到本目录的
:class:`IndexTts2Engine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.indextts2.plugin import IndexTts2Engine

__all__ = ["IndexTts2Engine"]
