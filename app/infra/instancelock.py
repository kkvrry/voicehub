"""同一个数据目录只允许一个服务进程。

两个进程打开同一本 ``projects.db`` 时，库里的任务状态会被互相踩：后起的那个在
构造时看到 ``running`` 的行，会以为上一个进程已经死了。锁落在数据目录的
``service.lock`` 上，由操作系统持有——进程退出（包括崩溃）立刻释放，不需要
自己清理残留文件。

锁是"建议性"的：它拦的是本程序自己的第二个实例，不是防外部程序。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["InstanceLock"]


class InstanceLock:
    """一个数据目录一把锁：``acquire()`` 拿不到就是"已有实例在跑"。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._busy = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def busy(self) -> bool:
        """上一次 ``acquire()`` 失败是不是因为"别人正拿着"。

        ``False`` 表示拿不到锁的原因不是并发（目录不可写、锁文件建不出来）。
        那种情况该照常启动：锁只是并发保护，不是运行前提。
        """
        return self._busy

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """非阻塞地拿锁；返回 ``False`` 表示另一个进程正持有它。"""
        if self._fd is not None:
            return True
        self._busy = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR)
        except OSError:
            # 数据目录不可写时不拦启动：锁只是并发保护，不是运行前提。
            return False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\n")
            _lock(fd)
        except OSError:
            # 锁被占 = 另一个实例在跑；这是唯一该拒绝启动的原因。
            self._busy = True
            os.close(fd)
            return False
        self._fd = fd
        self._stamp()
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    def owner_pid(self) -> int:
        """锁文件里记的进程号；读不到返回 0（只用于提示文案）。

        只看第一行：上一次的残留内容可能比这次写进去的长，留在后面的行里。
        持有锁时走自己的句柄——Windows 的字节锁会拒绝别的句柄读那一段。
        """
        try:
            if self._fd is not None:
                os.lseek(self._fd, 0, os.SEEK_SET)
                raw = os.read(self._fd, 64)
                os.lseek(self._fd, 0, os.SEEK_SET)
                text = raw.decode("ascii", "ignore")
            else:
                text = self._path.read_text(encoding="utf-8")
        except OSError:
            return 0
        lines = text.splitlines()
        first = lines[0].strip() if lines else ""
        return int(first) if first.isdigit() else 0

    def _stamp(self) -> None:
        """把自己写进锁文件：出问题时能看出是哪个进程占着。"""
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, f"{os.getpid()}\n".encode("ascii")[:32])
            os.lseek(self._fd, 0, os.SEEK_SET)
        except OSError:
            pass


def _lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
