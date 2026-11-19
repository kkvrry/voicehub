"""内录的应用层：一个进程只有一路，热键与菜单都落到这里。

为什么要单例
------------
采集设备是独占资源，两路同时录没有意义，还会让"停止"不知道停哪一路。托盘热键、
托盘菜单与 HTTP 接口共用同一个实例，所以"是否已经在录"只有一个判定点。

热键状态为什么由外部写入
------------------------
全局热键只能由桌面启动器注册（浏览器与页面都做不到），注册成败也只有它知道。
所以这里不猜：:meth:`CaptureService.note_hotkey` 由托盘在注册后写入，未写入时
状态为"未知"，界面据此说"托盘启动后可用"，而不是假装可用或假装不可用。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.infra import loopback

__all__ = [
    "HINT",
    "HOTKEY",
    "LABEL",
    "CaptureError",
    "CaptureService",
    "service",
]

#: 托盘注册的全局热键。结构给托盘解析，文字给界面显示，两者同源。
HOTKEY: dict[str, Any] = {"text": "Ctrl+Alt+R", "modifiers": ["ctrl", "alt"], "key": "R"}

#: 设置页「快捷键」里的行标题。
LABEL = "启停内录"

#: 一行说明：用途与必须提前知道的落盘规则。
HINT = "录制系统正在播放的声音；输出 WAV 写入录音目录，服务重启后清空。"

#: 静音判据。低于这一峰值按"没录到声音"提示，但不删除文件——用户可能就是要静音段。
_SILENT_PEAK = 0.001


class CaptureError(RuntimeError):
    """内录业务错误；``code`` 进错误码表，``message`` 可直接展示。"""

    def __init__(self, message: str, *, code: str = "capture_failed", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CaptureService:
    """一路内录的状态机。所有公开方法都可在任意线程调用。"""

    def __init__(
        self, factory: Callable[..., loopback.Recorder] = loopback.Recorder
    ) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._recorder: loopback.Recorder | None = None
        #: 正在打开设备：这段时间里 `_recorder` 还没挂上，必须另有一个占用标记。
        self._starting = False
        self._hotkey_ok: bool | None = None
        self._hotkey_reason = ""

    # -- 热键状态 --------------------------------------------------------- #

    def note_hotkey(self, ok: bool, reason: str = "") -> None:
        """由托盘写入热键注册结果；``reason`` 为不可用的原因（被占用、系统拒绝）。"""
        with self._lock:
            self._hotkey_ok = ok
            self._hotkey_reason = "" if ok else reason

    # -- 录制 ------------------------------------------------------------- #

    def start(self) -> dict[str, Any]:
        """开始内录，返回当前状态。设备不可用或已在录时报错。"""
        usable, reason = loopback.available()
        if not usable:
            raise CaptureError(reason, code="capture_unavailable")
        with self._lock:
            if self._starting:
                raise CaptureError("内录已在进行中。", code="capture_in_progress", status=409)
            current = self._recorder
            if current is not None and current.status()["recording"]:
                raise CaptureError("内录已在进行中。", code="capture_in_progress", status=409)
            self._starting = True
        # 打开设备最坏要等几秒，不能持锁：托盘的悬停提示每秒读一次状态。
        try:
            recorder = self._factory()
            recorder.start()
        except loopback.LoopbackError as exc:
            raise CaptureError(str(exc), code="capture_unavailable") from exc
        finally:
            with self._lock:
                self._starting = False
        with self._lock:
            self._recorder = recorder
        return self.status()

    def stop(self) -> dict[str, Any]:
        """停止内录并返回结果；没有在录时报错。"""
        with self._lock:
            recorder = self._recorder
            self._recorder = None
        if recorder is None:
            raise CaptureError("当前没有内录。", code="capture_idle", status=409)
        # 收尾要等文件回填，同样不持锁。
        try:
            result = recorder.stop()
        except loopback.LoopbackError as exc:
            raise CaptureError(str(exc)) from exc
        return {
            "object": "capture_result",
            **result,
            # 峰值独立给出：文件非空但全程静音是内录最常见的故障（系统静音、独占播放）。
            "silent": float(result.get("peak") or 0.0) < _SILENT_PEAK,
        }

    def status(self) -> dict[str, Any]:
        """当前状态。未在录时给出最后一次的错误与零值统计。"""
        with self._lock:
            recorder = self._recorder
            hotkey_ok = self._hotkey_ok
            hotkey_reason = self._hotkey_reason
        usable, reason = loopback.available()
        live = recorder.status() if recorder is not None else {}
        return {
            "object": "capture_status",
            "label": LABEL,
            "hint": HINT,
            "available": usable,
            "reason": reason,
            "recording": bool(live.get("recording")),
            "seconds": live.get("seconds", 0.0),
            "peak": live.get("peak", 0.0),
            "file": live.get("file", ""),
            "error": live.get("error", ""),
            "hotkey": dict(HOTKEY),
            "hotkey_ok": hotkey_ok,
            "hotkey_reason": hotkey_reason,
            "max_minutes": round(loopback.MAX_SECONDS / 60),
        }

    def peek(self) -> dict[str, Any]:
        """只读录制进度，不探测设备。

        托盘的悬停提示每秒刷新一次，而 :meth:`status` 会枚举输出设备——把设备
        枚举放进每秒一次的路径会在驱动异常时拖住消息循环。
        """
        with self._lock:
            recorder = self._recorder
        live = recorder.status() if recorder is not None else {}
        return {
            "recording": bool(live.get("recording")),
            "seconds": live.get("seconds", 0.0),
        }


#: 进程内唯一实例：托盘与 HTTP 共用。
service = CaptureService()
