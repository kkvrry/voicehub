"""VoiceHub 桌面版入口：单实例 + 托盘 + 开机自启，服务跑在后台线程。

两种运行态共用这一份代码：

- **发布态**：PyInstaller 以本文件为入口，``console=False``，自启指向 ``VoiceHub.exe``。
- **源码态**：``pythonw.exe packaging/voicehub_launcher.py``，自启指向
  ``pythonw.exe <本文件>``。无控制台窗口，改完代码重启托盘即生效。

行为：

- **单实例**：按数据目录命名互斥量。第二次双击只把已在运行的服务页打开，
  不会再有第二个进程去碰同一个 SQLite 库。
- **托盘**：打开界面 / 开机自启 / 退出。服务线程退出时托盘自动消失。
- **开机自启**：写 ``HKCU\\...\\Run``，命令带 ``--no-browser``，登录时不抢浏览器；
  源码态额外带 ``--data-dir``，否则登录后落到 ``%LOCALAPPDATA%`` 的另一份库。
- **窗口化无控制台**：``stdout`` / ``stderr`` 为 ``None``，启动即改道到
  ``<数据目录>/logs/launcher.log``；崩溃由 ``faulthandler`` 兜底。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "VoiceHub"
_MUTEX_PREFIX = "Local\\VoiceHub-"
_ERROR_ALREADY_EXISTS = 183
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "VoiceHub"

if not getattr(sys, "frozen", False):
    # 源码直跑时脚本在 packaging/ 下：仓库根给 `import app.*`，本目录给 `voicehub_tray`。
    # 冻结包两者都在 PYZ 里，不需要这一句。
    _HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HERE.parent))
    sys.path.insert(0, str(_HERE))

#: 本次启动显式指定的数据目录；开启自启时一并写进命令，登录后仍用同一份数据。
_DATA_DIR_ARG = ""

#: 启动前是否没有控制台。窗口化进程才需要弹窗，开发命令行直接打印。
_NO_CONSOLE = False

_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 进程外的基础设施
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="VoiceHub 桌面版")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口（默认读环境变量 / config.json）",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="监听地址（默认读环境变量 / config.json，均未设则 127.0.0.1）",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘（调试用）")
    parser.add_argument("--data-dir", default="", help="数据目录（默认 LOCALAPPDATA\\voicehub）")
    return parser.parse_args(argv)


def _redirect_output() -> None:
    """窗口化进程没有控制台：把两个流接到启动器日志，异常才有地方落。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from app.infra.config import log_base

        path = log_base() / "launcher.log"
        handle = path.open("a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001 - 日志都开不了时只能放弃，不能拦住启动
        return
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def _enable_faulthandler() -> None:
    try:
        import faulthandler

        faulthandler.enable(file=sys.stderr)
    except Exception:  # noqa: BLE001, S110 - 纯诊断设施，失败不影响运行
        pass


def _message(text: str, *, error: bool = False) -> None:
    """无控制台时的用户提示；窗口化进程同时把同一句话写进 launcher.log。"""
    stream = sys.stderr or sys.__stderr__
    if sys.platform == "win32" and _NO_CONSOLE:
        if stream is not None:
            print(f"[{APP_NAME}] {text}", file=stream, flush=True)
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)
            return
        except Exception:  # noqa: BLE001 - 退回日志
            _LOG.debug("弹窗失败，改走日志。", exc_info=True)
    if stream is not None:
        print(f"[{APP_NAME}] {text}", file=stream, flush=True)


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((probe_host, port)) == 0


class _Instance:
    """按数据目录命名的互斥量；句柄要留到进程退出，否则命名对象会提前消失。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handle: int | None = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        self._handle = self._kernel32.CreateMutexW(None, False, self._name)
        if not self._handle:
            return False
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _mutex_name(data_root: Path) -> str:
    digest = hashlib.sha1(str(data_root).lower().encode("utf-8")).hexdigest()[:16]
    return f"{_MUTEX_PREFIX}{digest}"


def _runtime_file(data_root: Path) -> Path:
    return data_root / "runtime.json"


def _publish_runtime(data_root: Path, port: int, host: str) -> str:
    # 绑通配地址时本机走回环；绑具体地址时只能用那个地址（回环上没有监听）。
    local_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    url = f"http://{local_host}:{port}/"
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": url,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    target = _runtime_file(data_root)
    try:
        scratch = target.with_suffix(".tmp")
        scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        scratch.replace(target)
    except OSError:
        _LOG.warning("运行态文件写入失败：%s", target, exc_info=True)
    return url


def _clear_runtime(data_root: Path) -> None:
    target = _runtime_file(data_root)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if int(payload.get("pid", 0)) == os.getpid():
            target.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def _existing_url(data_root: Path, fallback_port: int) -> str:
    try:
        payload = json.loads(_runtime_file(data_root).read_text(encoding="utf-8"))
        url = str(payload.get("url") or "").strip()
        pid = int(payload.get("pid", 0))
        if url and (not pid or _pid_alive(pid)):
            return url
    except (OSError, ValueError, TypeError):
        pass
    return f"http://127.0.0.1:{fallback_port}/"


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use("127.0.0.1", port):
            return True
        time.sleep(0.1)
    return False


def _open_when_ready(url: str, fallback_port: int) -> None:
    port = urlparse(url).port or fallback_port
    _wait_for_port(port)
    webbrowser.open(url)


# --------------------------------------------------------------------------- #
# 开机自启（HKCU Run）
# --------------------------------------------------------------------------- #


def _autostart_command() -> str:
    """自启命令行。发布态指向 exe；源码态指向 pythonw + 本脚本，登录不弹控制台。"""
    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}" --no-browser'
    else:
        exe = Path(sys.executable).resolve()
        # 用 python.exe 手工跑过时也要转成 pythonw：自启不该在登录时亮一个黑窗口。
        windowless = exe.with_name("pythonw.exe")
        interpreter = windowless if windowless.is_file() else exe
        command = f'"{interpreter}" "{Path(__file__).resolve()}" --no-browser'
    if _DATA_DIR_ARG:
        command += f' --data-dir "{_DATA_DIR_ARG}"'
    return command


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
        return bool(str(value).strip())
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        _LOG.warning("开机自启设置失败：%s", exc)
        return False


def _refresh_autostart_path() -> None:
    """入口搬过家时把自启项改指当前命令；没启用过就不主动创建。"""
    if not autostart_enabled():
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            current, _ = winreg.QueryValueEx(key, _RUN_VALUE)
    except OSError:
        return
    if str(current).strip() != _autostart_command():
        set_autostart(True)


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #


def _icon_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    packaged = base / "assets" / "voicehub.ico"
    if packaged.is_file():
        return packaged
    return Path(__file__).with_name("voicehub.ico")


def _run_tray(server_thread: threading.Thread, url: str) -> None:
    from voicehub_tray import TrayIcon

    from app.usecase import capture as capture_uc

    tray = TrayIcon(
        title=f"{APP_NAME} · 音色库与有声书",
        icon_path=_icon_path(),
        on_open=lambda: webbrowser.open(url),
        on_toggle_autostart=_toggle_autostart,
        is_autostart_enabled=autostart_enabled,
        on_quit=lambda: _quit(tray),
        on_toggle_capture=lambda: _toggle_capture(tray),
        capture_state=capture_uc.service.peek,
        hotkey=capture_uc.HOTKEY,
        # 热键只有在托盘里才注册得成：结果写回应用层，设置页据此显示可用性。
        on_hotkey_result=lambda ok, reason: capture_uc.service.note_hotkey(ok, reason),
    )

    def watch() -> None:
        server_thread.join()
        tray.stop()

    threading.Thread(target=watch, name="voicehub-watch", daemon=True).start()
    tray.run()


def _toggle_capture(tray) -> None:
    """热键与托盘菜单共用的起停：结果只通过气泡与日志说明。"""
    from app.infra.loghub import CHANNEL
    from app.usecase import capture as capture_uc

    service = capture_uc.service
    try:
        if service.peek()["recording"]:
            result = service.stop()
            seconds = int(result["seconds"])
            silent = bool(result.get("silent"))
            text = f"已保存 {Path(result['file']).name}（{seconds} 秒）"
            if silent:
                text += "，未检测到声音。"
            tray.notify("内录已停止", text, warning=silent)
            CHANNEL.info(
                f"内录已停止：{text}",
                source="capture",
                file=result["file"],
                seconds=seconds,
                peak=result.get("peak"),
                silent=silent,
            )
        else:
            service.start()
            tray.notify("内录已开始", "再按一次停止。")
            CHANNEL.info("内录已开始。", source="capture")
    except capture_uc.CaptureError as exc:
        tray.notify("内录失败", str(exc), warning=True)
        CHANNEL.warning(f"内录失败：{exc}", source="capture")


def _toggle_autostart() -> None:
    if not set_autostart(not autostart_enabled()):
        _message("开机自启设置失败，请查看 logs/launcher.log。", error=True)


def _quit(tray) -> None:
    _LOG.info("托盘退出，正在停止服务")
    state["server"].should_exit = True

    def wait_and_stop() -> None:
        # 先收内录：进程退出前不关闭文件，wav 头里的长度就停在占位值上。
        from app.usecase import capture as capture_uc

        try:
            if capture_uc.service.peek()["recording"]:
                capture_uc.service.stop()
        except Exception:  # noqa: BLE001 - 收尾失败不改变退出流程
            _LOG.warning("退出前停止内录失败", exc_info=True)
        state["server_thread"].join(timeout=15)
        tray.stop()

    threading.Thread(target=wait_and_stop, name="voicehub-quit", daemon=True).start()


#: 服务与线程从这一处取；托盘的菜单回调只收自身参数。
state: dict[str, object] = {}


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def _wait_started(server, thread: threading.Thread, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.05)
    return False


def main(argv: list[str] | None = None) -> int:
    global _DATA_DIR_ARG, _NO_CONSOLE

    args = _parse_args(argv)
    _NO_CONSOLE = sys.stderr is None
    if args.data_dir:
        _DATA_DIR_ARG = str(Path(args.data_dir).expanduser().resolve())
        os.environ["VOICEHUB_DATA_DIR"] = _DATA_DIR_ARG
    _redirect_output()
    _enable_faulthandler()

    # 开机自启的注册表值必须指向**启动器**：核心不猜 exe 路径，命令由这里交给
    # 服务进程（`app/infra/autostart.py` 读这个变量）。源码态没有启动器，
    # 变量为空，服务据此回答「不支持」，界面就不给假开关。
    from app.infra.autostart import ENV_COMMAND

    os.environ[ENV_COMMAND] = _autostart_command()

    from app.infra.config import data_dir, log_base, resolve_host, resolve_port

    data_root = data_dir()
    port = resolve_port(args.port)
    host = resolve_host(args.host)
    fallback_url = f"http://127.0.0.1:{port}/"

    instance = _Instance(_mutex_name(data_root))
    if not instance.acquire():
        instance.close()
        _LOG.info("已有实例在运行，打开 %s", fallback_url)
        if not args.no_browser:
            _open_when_ready(_existing_url(data_root, port), port)
        return 0

    try:
        if _port_in_use(host, port):
            text = (
                f"端口 {port} 已被占用，VoiceHub 无法启动。\n\n"
                f"多半是另一个 VoiceHub 进程（不是本程序启动的）还在运行。\n"
                f"先结束它，或在命令行加 --port {port + 1}。"
            )
            _message(text, error=True)
            return 1

        import uvicorn

        from app.infra.loghub import CHANNEL
        from app.main import app

        _refresh_autostart_path()

        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        server_thread = threading.Thread(target=server.run, name="voicehub-server", daemon=True)
        state.update(server=server, server_thread=server_thread)
        server_thread.start()
        if not _wait_started(server, server_thread):
            text = (
                "VoiceHub 服务启动失败。\n\n"
                f"日志：{log_base() / 'voicehub.log'}"
            )
            CHANNEL.error("桌面模式启动失败，详见日志。", source="launcher")
            _message(text, error=True)
            return 1

        url = _publish_runtime(data_root, port, host)
        CHANNEL.info(f"桌面模式启动：{url}", source="launcher", port=port)
        if not args.no_browser:
            webbrowser.open(url)

        if args.no_tray:
            server_thread.join()
            return 0

        try:
            _run_tray(server_thread, url)
        except Exception as exc:  # noqa: BLE001 - 托盘起不来不该连带把服务杀了
            CHANNEL.warning(f"托盘不可用，转为无界面常驻：{exc}", source="launcher")
            server_thread.join()
        return 0
    finally:
        server = state.get("server")
        if server is not None:
            server.should_exit = True
        thread = state.get("server_thread")
        if isinstance(thread, threading.Thread):
            thread.join(timeout=10)
        _clear_runtime(data_root)
        instance.close()
        try:
            from app.infra.loghub import CHANNEL

            CHANNEL.info("桌面模式退出。", source="launcher")
        except Exception:  # noqa: BLE001, S110 - 收尾日志失败不改变退出码
            pass


if __name__ == "__main__":
    raise SystemExit(main())
