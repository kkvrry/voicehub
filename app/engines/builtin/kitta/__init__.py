"""Kitta Audio 引擎插件包——**一台引擎一个目录**。

- :mod:`plugin`   元信息、能力声明、装配与克隆编排——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用、错误翻译、被忽略参数的转述
- :mod:`voices`   音色清单的响应解析
- :mod:`mapping`  统一参数 → v3 请求体 的映射表与换算器
- ``instructions.json``  引导指令的现成说法
- ``icon.svg``    平台图标

注册表扫到本目录的 :class:`KittaEngine` 即完成接入，**VoiceHub 主代码零改动**。
"""

from app.engines.builtin.kitta.plugin import KittaEngine

__all__ = ["KittaEngine"]
