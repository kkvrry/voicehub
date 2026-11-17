"""封面缩略图。

列表页的卡片头像只有 58px，详情 76px，而头像原图是 0.5–1MB 的 PNG：几百个音色
一起进列表时，光是解码就是几百 MB 的内存与主线程时间。这里在**首次被请求**时把
封面缩成 160px 的 WebP 缓存进数据目录，之后的请求直接回缓存文件。

失效判断用 mtime 当指纹：生成时把源文件的 ``mtime_ns`` 写成缩略图的 ``mtime_ns``，
两边相等 = 有效。换了头像（``avatar.png`` 被覆盖）mtime 变，缩略图自动重生成——
列表 URL 里的 ``?v=`` 本来就会变，这条只是兜底。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

from PIL import Image

__all__ = ["SIDE", "ThumbError", "thumbnail"]

#: 缩略图边长：卡片 58px、详情 76px，2x 屏最大 152px——160 留一点余量。
SIDE: Final[int] = 160


class ThumbError(RuntimeError):
    """原图打不开（损坏 / 不是图片）：调用方回退到原文件。"""


def thumbnail(source: Path, cache_dir: Path) -> Path:
    """返回 ``source`` 的方形缩略图路径；缓存不在或过期就当场生成。

    缩略图本身也算"生成的产物"，落在缓存目录里：清缓存无损，最多重生成一次。
    """
    src_stamp = source.stat().st_mtime_ns
    key = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:20]
    cache = cache_dir / f"{key}.webp"
    if cache.is_file() and int(cache.stat().st_mtime_ns) == src_stamp:
        return cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            frame = image.convert("RGBA")
            side = min(frame.width, frame.height)
            left = (frame.width - side) // 2
            top = (frame.height - side) // 2
            frame = frame.crop((left, top, left + side, top + side))
            frame = frame.resize((SIDE, SIDE), Image.Resampling.LANCZOS)
            frame.save(cache, "WEBP", quality=88)
    except (OSError, ValueError) as exc:  # PIL 的打开 / 解码失败都归这两类
        raise ThumbError(str(exc)) from exc
    os.utime(cache, ns=(src_stamp, src_stamp))
    return cache
