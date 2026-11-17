"""停子进程：先礼后兵（terminate → 等 → kill）。

Windows 上 terminate 就是 ``TerminateProcess``，子进程不会自己收尾；等一会儿是为了
让它把管道关掉——读 stderr 的那一端还在等，句柄不关就一直挂着。等不到才 kill，
kill 之后仍再等一次，否则句柄留在进程表里。

三处用它：有声书的响度批处理与合并、audio.cpp 实例的启停。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import sys
import threading

__all__ = ["CREATE_NO_WINDOW", "DEFAULT_GRACE", "ChildJob", "alive", "guard_child", "stop"]

#: 等它自己退的秒数。ffmpeg 收到 terminate 后通常立刻退出，3 秒足够。
DEFAULT_GRACE = 3.0

#: 不给控制台子程序分配窗口。无控制台的父进程（pythonw / 打包 exe）默认会给
#: ffmpeg、ffprobe 各新开一个控制台窗口，界面上就是一连串黑窗闪烁；创建子进程
#: 一律带上它。非 Windows 平台为 0，传给 subprocess 不产生作用。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


#: ``GetExitCodeProcess`` 的"仍在运行"返回值（STILL_ACTIVE）。
_STILL_ACTIVE = 259

#: Job Object 的 ``JobObjectExtendedLimitInformation`` 信息类。
_JOB_EXTENDED_LIMIT = 9
#: 最后一个 Job 句柄关闭时杀掉组内所有进程。
_JOB_KILL_ON_CLOSE = 0x2000


class _JobBasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> object:
    lib = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    lib.CreateJobObjectW.restype = ctypes.c_void_p
    lib.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    lib.SetInformationJobObject.restype = ctypes.c_int
    lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.AssignProcessToJobObject.restype = ctypes.c_int
    lib.CloseHandle.argtypes = [ctypes.c_void_p]
    lib.CloseHandle.restype = ctypes.c_int
    return lib


class ChildJob:
    """Windows Job Object：句柄一关，组内子进程全部结束。

    ``atexit`` 只在正常退出时跑；托盘被强杀、进程崩溃、关机时不会。把子进程放进
    这个 Job 后，VoiceHub 怎么死都由系统替它收尾，不会留下占着显存的孤儿进程。
    """

    def __init__(self, handle: int) -> None:
        self._handle = int(handle)

    @classmethod
    def create(cls) -> ChildJob | None:
        """建一个 kill-on-close 的 Job；非 Windows 返回 ``None``。"""
        if sys.platform != "win32":
            return None
        lib = _kernel32()
        handle = lib.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JobExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
        ok = lib.SetInformationJobObject(
            handle, _JOB_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            lib.CloseHandle(handle)
            return None
        return cls(handle)

    def assign(self, process: subprocess.Popen) -> bool:
        """把一个刚创建的子进程放进 Job；失败返回 ``False``，不影响它继续跑。"""
        if sys.platform != "win32" or not self._handle:
            return False
        handle = getattr(process, "_handle", None)
        if not handle:
            return False
        lib = _kernel32()
        return bool(lib.AssignProcessToJobObject(self._handle, int(handle)))

    def close(self) -> None:
        """主动关闭 Job（测试用）；关闭即杀掉组内所有进程。"""
        if sys.platform != "win32" or not self._handle:
            return
        with contextlib.suppress(Exception):
            _kernel32().CloseHandle(self._handle)
        self._handle = 0


_JOB_LOCK = threading.Lock()
_JOB: ChildJob | None = None
_JOB_READY = False


def guard_child(process: subprocess.Popen) -> bool:
    """把子进程挂到进程级 Job：VoiceHub 退出（含强杀）时一起结束。"""
    global _JOB, _JOB_READY
    if sys.platform != "win32":
        return False
    with _JOB_LOCK:
        if not _JOB_READY:
            _JOB = ChildJob.create()
            _JOB_READY = True
        job = _JOB
    return bool(job and job.assign(process))


def alive(pid: int) -> bool:
    """这个进程号还在吗。

    只用来判断"上一个实例是不是已经退出"，所以**宁可回答活着**：pid 不存在时
    返回 ``False``；查询失败时返回 ``True``，让调用方走心跳超时那条更慢但安全的路径。

    Windows 上不能用 ``os.kill(pid, 0)``：它对任何非 CTRL_* 的信号都会直接
    调 ``TerminateProcess``，那是"探测"变成"杀人"。
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def stop(process: subprocess.Popen, *, grace: float = DEFAULT_GRACE) -> None:
    """已经退出的直接返回；否则 terminate，超时再 kill。

    ``kill`` 也杀不掉时**放弃等待**，不把调用线程挂死——那种进程只能重启机器清掉。
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
