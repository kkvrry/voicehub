"""本地向量模型域：给语义检索提供 ``文本 → 向量`` 能力。

与 TTS 引擎分开：引擎契约是"文本 → 音频"，这里是"文本 → 向量"，两者没有
重合能力。插件目录布局、配置/状态/下载体验沿用引擎那套习惯，但不继承
:class:`~app.engines.base.EnginePlugin`，也不出现在引擎列表里。
"""

from __future__ import annotations
