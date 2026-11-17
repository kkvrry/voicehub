"""VoxCPM2 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/dots_tts/`` 同一形状）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

接的是 audio.cpp 的 ``voxcpm2`` 家族（``POST /v1/audio/speech``）：2B 参数、30 种语言、
零样本克隆与"一句话描述音色"。它最特别的一处是**没有独立指令字段**——风格与音色描述
写在正文最前面的括号里，插件在装配阶段拼好再发（见 :mod:`plugin`）。
注册表扫到本目录的 :class:`VoxCpm2Engine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.voxcpm2.plugin import VoxCpm2Engine

__all__ = ["VoxCpm2Engine"]
