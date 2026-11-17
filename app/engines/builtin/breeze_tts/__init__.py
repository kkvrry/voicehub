"""BreezeTTS 2 引擎插件包——**一台引擎一个目录**。

目录里各文件的职责（与 ``builtin/gpt_sovits/`` 同一形状，便于对照）：

- :mod:`plugin`   元信息、能力声明、映射装配、CFG 规则——**入口**
- :mod:`settings` 配置模型（前端照它的 JSON Schema 渲染表单）
- :mod:`client`   HTTP 调用与错误翻译（"怎么发"留在这家的目录里）
- :mod:`mapping`  统一参数 → 这家请求体 的映射表与说明
- ``icon.svg``    平台图标（界面按目录取，换图标不用改代码）

**这里没有 ``voices.py``**（MiniMax 那个目录里有一个），因为这一家**没有音色清单
可言**：它的"音色"是一段参考音频，没有可枚举的端点。少一个空文件比多一个
"看着像有清单、其实没有"的模块诚实。

**它接的是 audio.cpp 那一层，不是整合包外层的加密网关**——理由见 :mod:`plugin`
的模块 docstring。这一条决定了"自部署要起什么"：只需要 ``audiocpp_server.exe``。

注册表扫到本目录的 :class:`BreezeTtsEngine` 即完成接入，**VoiceHub 主代码零改动**。
"""

from app.engines.builtin.breeze_tts.plugin import BreezeTtsEngine

__all__ = ["BreezeTtsEngine"]
