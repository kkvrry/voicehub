"""OmniVoice 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/dots_tts/`` 同一形状）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

接的是 audio.cpp 的 ``omnivoice`` 家族（``POST /v1/audio/speech``）：600+ 语言、
零样本克隆、一句话描述音色，正文里还能写 ``[laughter]`` 这类非语言标签（插件把
``[[笑]]`` 翻成它的写法）。
注册表扫到本目录的 :class:`OmniVoiceEngine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.omnivoice.plugin import OmniVoiceEngine

__all__ = ["OmniVoiceEngine"]
