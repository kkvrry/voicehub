"""DotTTS 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/indextts2/`` 同一形状，便于对照）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

**它接的也是 audio.cpp 那一层**（OpenAI 兼容端点 ``/v1/audio/speech``）——
与 BreezeTTS 2 / IndexTTS2 是同一套推理框架、不同的模型。DotTTS 是小红书
dots 团队联合上海交大 X-LANCE 开源的多语言零样本 TTS（SOAR / MeanFlow /
Edit 三个检查点，audio.cpp 侧家族名 ``dots_tts``）。注册表扫到本目录的
:class:`DotsTtsEngine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.dots_tts.plugin import DotsTtsEngine

__all__ = ["DotsTtsEngine"]
