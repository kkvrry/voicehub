"""BreezeTTS 2 的「引导指令」预设——**数据在同目录的 ``instructions.json``，加载器只有一份**。

加载器与"为什么是数据文件"的完整说明在 :mod:`app.engines.presets`（两台引擎共用，
不在这边再抄一遍）。本模块只剩两件事：这一份数据的位置，与一个保持旧导入路径
不变的薄壳（``from ...breeze_tts import presets; presets.load_presets()``）。
"""

from __future__ import annotations

from pathlib import Path

from app.engines.presets import load_presets as _load_presets

__all__ = ["PRESETS_PATH", "load_presets"]

#: 这份数据就在插件自己目录下（与 ``icon.svg`` 同一个位置、同一种口径：
#: "这一台引擎自己带的东西"都放在它自己的目录里）。
PRESETS_PATH = Path(__file__).resolve().parent / "instructions.json"


def load_presets() -> tuple[tuple[str, str], ...]:
    """读 BreezeTTS 2 那份预设（顺序即界面上候选的顺序）。"""
    return _load_presets(PRESETS_PATH)
