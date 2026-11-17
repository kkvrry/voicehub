"""MiniMax 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（新加一台引擎就照这个形状复制一份）：

- :mod:`plugin`   元信息、能力声明、映射装配、克隆编排——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与信封拆解（"怎么发"留在这家的目录里）
- :mod:`voices`   音色清单的响应解析
- :mod:`mapping`  统一参数 → 这家请求体 的映射表与说明
- ``icon.svg``    平台图标（界面按目录取，换图标不用改代码）

注册表扫到本目录的 :class:`MinimaxEngine` 即完成接入，**VoiceHub 主代码零改动**。
"""

from app.engines.builtin.minimax.plugin import MinimaxEngine

__all__ = ["MinimaxEngine"]
