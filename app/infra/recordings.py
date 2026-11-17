"""麦克风录下来的那段音频放哪：**临时目录**，服务重启即清。

它跟"归档"是两回事：:mod:`app.infra.archive` 管的是生成产物（样例 / 缓存），
录音是"这一次要用的参考音频"，用完不必留——留在磁盘上反而是垃圾：
用户看不见它（不在扫描范围内）、删不掉它（界面上没有入口）。

所以这里只有一个目录、两条规则：**写进去**（文件名带时间，好认）、**启动时清空**。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from app.infra.config import data_dir

__all__ = ["MAX_BYTES", "clear", "root", "save"]

#: 一次录音的上限（16 kHz / 16 bit 单声道 ≈ 32 KB/s，10 MB ≈ 5 分钟）。
#: 越界在 api 层拦，别等写满磁盘。
MAX_BYTES = 10 * 1024 * 1024


def root(*, create: bool = True) -> Path:
    """录音目录：``<数据目录>/recordings``。**永不扫描**（与缓存根同一条纪律）。"""
    path = data_dir() / "recordings"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def save(raw: bytes, suffix: str = ".wav") -> Path:
    """写一份录音，返回它的路径。文件名带时刻：同一分钟录两段不会互相盖掉。"""
    folder = root()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    clean = suffix if suffix.startswith(".") else f".{suffix}"
    target = folder / f"录音-{stamp}{clean}"
    # 同秒里录了两段：加序号而不是盖掉前一个（盖掉会让前一条请求指向新音频）。
    n = 1
    while target.exists():
        n += 1
        target = folder / f"录音-{stamp}-{n}{clean}"
    target.write_bytes(raw)
    return target


def clear(*, keep_recent: float = 0) -> int:
    """清空录音目录（启动时调一次），返回删掉几个文件。

    只删这个目录里的东西：它可能被人手塞进别的文件，但这是**服务自己的**目录，
    与"不动用户的文件"不冲突——用户从没被引导往这里放东西。

    :param keep_recent: 保留最近 N 秒的（测试里留一条刚录的看效果）。
    """
    folder = root(create=False)
    if not folder.is_dir():
        return 0
    now = time.time()
    gone = 0
    for item in folder.iterdir():
        if keep_recent and item.is_file() and now - item.stat().st_mtime < keep_recent:
            continue
        if item.is_file():
            item.unlink(missing_ok=True)
            gone += 1
    return gone
