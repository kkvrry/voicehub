"""开机自启：Windows 当前用户 ``Run`` 项。

命令由桌面启动器经环境变量 ``VOICEHUB_AUTOSTART`` 交给本进程——注册表值必须指向
启动器，核心不猜 exe 路径。源码态（``start.bat``）没有启动器，这个变量为空，
于是「不支持」：写进去的命令没人执行，给一个假开关比不给更坏。

只写 ``HKCU``：登录时随当前用户启动、不需要管理员，用户也能在任务管理器一并关掉。
"""

from __future__ import annotations

import logging
import os
import sys

__all__ = ["ENV_COMMAND", "command", "enabled", "set_enabled", "supported"]

ENV_COMMAND = "VOICEHUB_AUTOSTART"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "VoiceHub"

_LOG = logging.getLogger(__name__)


def command() -> str:
    """启动器交给本进程的自启命令行；没有就是空串。"""
    return os.environ.get(ENV_COMMAND, "").strip()


def supported() -> bool:
    """能不能由本进程设置：要 Windows，且确实有启动器在跑。"""
    return sys.platform == "win32" and bool(command())


def enabled() -> bool:
    """注册表里那条值是否指向当前启动器。路径变了（换了目录）就算没启用。"""
    if not supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
    except OSError:
        return False
    return str(value).strip() == command()


def set_enabled(on: bool) -> bool:
    """写或删那条值。返回是否成功；失败只记日志，不抛给界面。"""
    if not supported():
        return False
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if on:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        _LOG.warning("开机自启设置失败：%s", exc)
        return False
