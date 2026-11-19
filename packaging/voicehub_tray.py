"""Windows 通知区域图标：直接调 ``Shell_NotifyIcon``，不依赖 pystray。

为什么不用 pystray
-----------------
pystray 0.19.5 在本机（CPython 3.12 / 64 位）注册后图标不出现在通知区域，
也不抛异常——``icon.visible`` 始终为 ``False``。同一进程里直接 ctypes 调
``Shell_NotifyIconW`` 返回 1 且图标正常显示，可见问题出在 pystray 的 64 位
句柄处理（``LoadCursorW`` 一类未声明 ``restype``，HANDLE 被截断成 ``c_int``，
``RegisterClassEx`` 因此失败，而它不检查返回值）。给第三方库的 DLL 签名打补丁
比这 200 行更脆，所以自己实现，顺带去掉一个运行期依赖与 pillow 的图像转换环节。

实现约定
--------
- 所有 Win32 函数**显式声明 argtypes / restype**：64 位下任何遗漏都会截断句柄。
- ``Shell_NotifyIcon(NIM_ADD)`` 返回 0 时**抛异常**，不静默降级——上一次静默
  失败的代价是排查了半天"进程活着但没图标"。
- 监听 ``TaskbarCreated``：explorer 重启后系统会丢掉所有托盘图标，收到这条注册
  消息就重新 ``NIM_ADD``，比让用户重启程序合理。
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_INFO = 0x0010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002

MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
MF_DEFAULT = 0x1000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

ID_OPEN = 1001
ID_AUTOSTART = 1002
ID_QUIT = 1003
ID_CAPTURE = 1004

_HOTKEY_ID = 1
_TIMER_ID = 1

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

#: 模块句柄不声明 restype 会被截断成 c_int，64 位下 CreateWindowEx 直接失败。
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.WORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_size_t,
    wintypes.LPCWSTR,
]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

_CLASS_NAME = "VoiceHubTrayWindow"

_MODIFIER_CODES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
}


def _virtual_key(key: str) -> int:
    """组合键的主键 → 虚拟键码。只支持字母数字与 F1–F12。

    热键字符串由后端下发（``usecase.capture.HOTKEY``），这里只翻译，不定义组合。
    认不出的键返回 0，由调用方报"热键不可用"，不静默改成别的键。
    """
    text = (key or "").strip().upper()
    if len(text) == 1 and text.isalnum():
        return ord(text)
    if text.startswith("F") and text[1:].isdigit():
        number = int(text[1:])
        if 1 <= number <= 12:
            return 0x6F + number  # VK_F1 = 0x70
    return 0


def _clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class TrayIcon:
    """托盘图标 + 菜单。``run()`` 阻塞在主线程，``stop()`` 可从任意线程调用。

    热键由这里注册（浏览器做不到全局热键）：组合键来自后端下发的
    ``usecase.capture.HOTKEY``，注册结果写回 ``hotkey_ok`` / ``hotkey_error``，
    供启动器转告应用层，设置页据此显示可用性。
    """

    def __init__(
        self,
        *,
        title: str,
        icon_path: Path,
        on_open: Callable[[], None],
        on_toggle_autostart: Callable[[], None],
        is_autostart_enabled: Callable[[], bool],
        on_quit: Callable[[], None],
        on_toggle_capture: Callable[[], None] | None = None,
        capture_state: Callable[[], dict[str, Any]] | None = None,
        hotkey: dict[str, Any] | None = None,
        on_hotkey_result: Callable[[bool, str], None] | None = None,
    ) -> None:
        self._title = title
        self._icon_path = icon_path
        self._on_open = on_open
        self._on_toggle_autostart = on_toggle_autostart
        self._is_autostart_enabled = is_autostart_enabled
        self._on_quit = on_quit
        self._on_toggle_capture = on_toggle_capture or (lambda: None)
        self._capture_state = capture_state or dict
        self._hotkey = hotkey or {}
        self._on_hotkey_result = on_hotkey_result or (lambda ok, reason: None)

        #: 热键注册结果。``None`` 表示未尝试（无窗口/未调用 run）。
        self.hotkey_ok: bool | None = None
        self.hotkey_error = ""
        self._current_tip = title

        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._added = False
        #: 窗口过程必须活到消息循环结束，否则回调指针被回收会直接崩进程。
        self._wndproc = WNDPROC(self._handle)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # -- 生命周期 --------------------------------------------------------- #

    def run(self) -> None:
        user32.SetProcessDPIAware()
        self._register_class()
        self._create_window()
        self._add_icon()
        self._register_hotkey()
        user32.SetTimer(self._hwnd, _TIMER_ID, 1000, None)
        try:
            self._loop()
        finally:
            if self._hwnd:
                user32.KillTimer(self._hwnd, _TIMER_ID)
                if self.hotkey_ok:
                    user32.UnregisterHotKey(self._hwnd, _HOTKEY_ID)
            self._remove_icon()

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def notify(self, title: str, text: str, *, warning: bool = False) -> None:
        """气泡提示。热键在别的程序里按下时，这是唯一能说明结果的地方。"""
        if not (self._added and self._hwnd):
            return
        nid = self._data()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = (title or self._title)[:63]
        nid.szInfo = (text or "")[:255]
        nid.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        # 气泡不改变悬停提示；下一次计时器刷新会把 tip 写回常态。
        self._current_tip = ""

    # -- 内部 ------------------------------------------------------------- #

    def _register_class(self) -> None:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS：同进程重复注册，无害
                raise OSError(f"RegisterClassExW 失败：{err}")

    def _create_window(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, self._title, 0, 0, 0, 0, 0, None, None, hinstance, None
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW 失败：{ctypes.get_last_error()}")
        self._hwnd = hwnd

    def _load_icon(self) -> int:
        if self._icon_path.is_file():
            hicon = user32.LoadImageW(
                None,
                str(self._icon_path),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if hicon:
                return hicon
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _data(self, tip: str | None = None) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = (tip or self._title)[:127]
        return nid

    def _set_tip(self, tip: str) -> None:
        if not (self._added and self._hwnd):
            return
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(tip)))
        self._current_tip = tip

    def _hotkey_set(self, ok: bool, reason: str = "") -> None:
        self.hotkey_ok = ok
        self.hotkey_error = "" if ok else reason
        try:
            self._on_hotkey_result(ok, self.hotkey_error)
        except Exception:  # noqa: BLE001, S110 - 结果通知失败不该影响托盘
            pass

    def _register_hotkey(self) -> None:
        """注册全局热键。失败不抛：托盘仍可用，设置页会如实显示不可用原因。"""
        modifiers = 0
        for name in self._hotkey.get("modifiers") or []:
            modifiers |= _MODIFIER_CODES.get(str(name).lower(), 0)
        vk = _virtual_key(str(self._hotkey.get("key") or ""))
        if not self._hwnd or not modifiers or not vk:
            self._hotkey_set(False, "热键配置无效。")
            return
        if not user32.RegisterHotKey(
            self._hwnd, _HOTKEY_ID, modifiers | MOD_NOREPEAT, vk
        ):
            error = ctypes.get_last_error()
            self._hotkey_set(
                False,
                "热键被其他程序占用。"
                if error == _ERROR_HOTKEY_ALREADY_REGISTERED
                else f"热键注册失败（错误 {error}）。",
            )
            return
        self._hotkey_set(True)

    def _capture(self) -> dict[str, Any]:
        try:
            return self._capture_state() or {}
        except Exception:  # noqa: BLE001 - 托盘不能因状态读取失败而卡住
            return {}

    def _refresh_tip(self) -> None:
        """每秒刷新悬停提示：录音时显示时长，其余时间回到标题。"""
        state = self._capture()
        tip = (
            f"{self._title} · 内录 {_clock(float(state.get('seconds') or 0))}"
            if state.get("recording")
            else self._title
        )
        if tip != self._current_tip:
            self._set_tip(tip)

    def _add_icon(self) -> None:
        if self._hicon is None:
            self._hicon = self._load_icon()
        nid = self._data()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError(f"Shell_NotifyIconW(NIM_ADD) 失败：{ctypes.get_last_error()}")
        self._added = True

    def _remove_icon(self) -> None:
        if self._added and self._hwnd:
            nid = self._data()
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._added = False

    def _loop(self) -> None:
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:
                return
            if ret == -1:
                raise OSError(f"GetMessageW 失败：{ctypes.get_last_error()}")
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle(self, hwnd, msg, wparam, lparam) -> int:
        if self._taskbar_created and msg == self._taskbar_created:
            # explorer 重启后系统会清空托盘，重新注册一次。
            try:
                self._added = False
                self._add_icon()
            except OSError:
                pass
            return 0

        if msg == WM_HOTKEY and int(wparam) == _HOTKEY_ID:
            # 起停要打开设备、收尾要等文件回填，不能在窗口线程里做：
            # 那期间托盘菜单会整块卡住。
            threading.Thread(
                target=self._on_toggle_capture, name="voicehub-hotkey", daemon=True
            ).start()
            return 0

        if msg == WM_TIMER and int(wparam) == _TIMER_ID:
            self._refresh_tip()
            return 0

        if msg == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._on_open()
            elif event == WM_RBUTTONUP:
                self._popup_menu()
            return 0

        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0

        if msg == WM_DESTROY:
            self._remove_icon()
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING | MF_DEFAULT, ID_OPEN, "打开界面")
            state = self._capture()
            if state.get("recording"):
                capture_label = f"停止内录  {_clock(float(state.get('seconds') or 0))}"
            elif self.hotkey_ok is False:
                # 菜单仍可点（手动起停不依赖热键），但不能把用不了的组合键印在这里。
                capture_label = "内录（热键不可用）"
            else:
                text = str(self._hotkey.get("text") or "")
                capture_label = f"内录  {text}" if text else "内录"
            user32.AppendMenuW(hmenu, MF_STRING, ID_CAPTURE, capture_label)
            flags = MF_STRING | (MF_CHECKED if self._is_autostart_enabled() else 0)
            user32.AppendMenuW(hmenu, flags, ID_AUTOSTART, "开机自启")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "退出")

            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # 不先抢前台，菜单在点击别处时不会关闭。
            user32.SetForegroundWindow(self._hwnd)
            chosen = user32.TrackPopupMenu(
                hmenu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(hmenu)

        if chosen == ID_OPEN:
            self._on_open()
        elif chosen == ID_CAPTURE:
            threading.Thread(
                target=self._on_toggle_capture, name="voicehub-capture", daemon=True
            ).start()
        elif chosen == ID_AUTOSTART:
            self._on_toggle_autostart()
        elif chosen == ID_QUIT:
            self._on_quit()
