"""PyInstaller 运行前钩子：把随包的 ffmpeg / ffprobe 目录放到 PATH 最前。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BUNDLE = Path(getattr(sys, "_MEIPASS", ""))
_FFMPEG = _BUNDLE / "vendor" / "ffmpeg"
if _FFMPEG.is_dir():
    os.environ["PATH"] = str(_FFMPEG) + os.pathsep + os.environ.get("PATH", "")
os.environ["VOICEHUB_PACKAGED"] = "1"
