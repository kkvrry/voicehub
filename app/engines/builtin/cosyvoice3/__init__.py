"""CosyVoice3 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/dots_tts/`` 同一形状）：

- :mod:`plugin`   元信息、能力声明、参数装配——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译
- ``icon.svg``    平台图标

接的是 audio.cpp 的 ``cosyvoice3`` 家族（``POST /v1/audio/speech``）：零样本克隆、
跨语种克隆与指令控制三种模板，语言覆盖中英日韩德西法意俄与粤语。参考音频是硬前提——
它没有内置音色，也没有情绪枚举。
注册表扫到本目录的 :class:`CosyVoice3Engine` 即完成接入，VoiceHub 主代码零改动。
"""

from app.engines.builtin.cosyvoice3.plugin import CosyVoice3Engine

__all__ = ["CosyVoice3Engine"]
