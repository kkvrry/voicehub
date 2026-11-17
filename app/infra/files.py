"""文件指纹：路径 + 大小 + mtime（纳秒）。

判"这份派生物还对得上源文件吗"的**唯一**判据：只比大小会漏原地改写，只比 mtime
会被 ``touch`` 全量打穿；两个一起才够。路径也在里面——同一份内容换个位置就是另一份。

Windows 上路径不分大小写，``normcase`` 把两种写法折成同一个键。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["file_stamp"]


def file_stamp(path: Path) -> tuple[str, int, int] | None:
    """``(规范路径, 大小, mtime_ns)``；读不到（不存在 / 无权限）返回 ``None``。

    返回 ``None`` 而不是 ``(路径, 0, 0)``：调用方必须区分"文件没了"与"文件是空的"，
    前者该报错或跳过，后者是合法输入。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (os.path.normcase(str(path)), stat.st_size, stat.st_mtime_ns)
