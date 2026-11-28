"""Windows SAPI 5 桥接管理：读写 ``bridge.json``、查询注册状态、切换系统默认音色与调用安装脚本。

本模块只管宿主机上的 SAPI 5 状态：
- 读/写 ``%LOCALAPPDATA%\\voicehub\\sapi\\bridge.json`` 无需管理员权限，下次朗读立刻生效；
- 切换系统默认音色写 ``HKCU\\SOFTWARE\\Microsoft\\Speech\\Voices\\DefaultTokenId``，同样无需提权；
- 仅「安装 / 卸载」写 ``HKLM`` 令牌与 COM 类时会通过 PowerShell 脚本弹 UAC 提权。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json

__all__ = [
    "config_path",
    "default_voice_id",
    "install",
    "install_dir",
    "is_default",
    "is_installed",
    "read_config",
    "save_config",
    "scripts_dir",
    "set_default",
    "status",
    "supported",
    "uninstall",
]

_LOG = logging.getLogger(__name__)

_TOKEN_ID = "VoiceHub_TTS"
_TOKEN_FULL = rf"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\{_TOKEN_ID}"
_HKLM_TOKENS_ROOT = r"SOFTWARE\Microsoft\Speech\Voices\Tokens"
_HKLM_TOKEN_KEY = rf"{_HKLM_TOKENS_ROOT}\{_TOKEN_ID}"
_HKCU_VOICES_KEY = r"SOFTWARE\Microsoft\Speech\Voices"
_HUIHUI_TOKEN = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_ZH-CN_HUIHUI_11.0"

_DEFAULTS: dict[str, Any] = {
    "endpoint": "https://127.0.0.1:17865",
    "engine": "auto",
    "voice_id": "",
    "format": "wav",
    "timeout_seconds": 30,
    "fallback_voice": "Microsoft Huihui Desktop",
    "voice_name": "VoiceHub TTS",
    "language": "804",
}


def supported() -> bool:
    """SAPI 5 仅 Windows 存在。"""
    return sys.platform == "win32"


def install_dir() -> Path:
    """桥接程序集与配置的默认目录（``%LOCALAPPDATA%\\voicehub\\sapi``）。"""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "voicehub" / "sapi"
    return Path.home() / "AppData" / "Local" / "voicehub" / "sapi"


def config_path() -> Path:
    """桥接配置文件路径：``VOICEHUB_SAPI_CONFIG`` 优先。"""
    override = os.environ.get("VOICEHUB_SAPI_CONFIG", "").strip()
    if override:
        return Path(override)
    return install_dir() / "bridge.json"


def scripts_dir() -> Path | None:
    """定位仓库或打包目录中的 ``packaging/sapi_bridge``；找不到返回 ``None``。"""
    root = Path(__file__).resolve().parents[2]
    candidate = root / "packaging" / "sapi_bridge"
    if (candidate / "install.ps1").is_file():
        return candidate
    return None


def read_config() -> dict[str, Any]:
    """读取 ``bridge.json`` 并补齐默认值；文件不存在或损坏时回默认。"""
    cfg = dict(_DEFAULTS)
    path = config_path()
    if not path.is_file():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except (OSError, ValueError) as exc:
        _LOG.warning("读取 SAPI 配置失败，使用默认：%s", exc)
    return cfg


def save_config(
    *,
    voice_id: str | None = None,
    endpoint: str | None = None,
    engine: str | None = None,
    previous_default_voice: str | None = None,
) -> dict[str, Any]:
    """原子更新 ``bridge.json``；未传入的字段保持原值。"""
    cfg = read_config()
    if voice_id is not None:
        cfg["voice_id"] = voice_id.strip()
    if endpoint is not None and endpoint.strip():
        cfg["endpoint"] = endpoint.strip().rstrip("/")
    if engine is not None and engine.strip():
        cfg["engine"] = engine.strip()
    elif not cfg.get("engine"):
        cfg["engine"] = "auto"
    if previous_default_voice is not None:
        if previous_default_voice.strip():
            cfg["previous_default_voice"] = previous_default_voice.strip()
        else:
            cfg.pop("previous_default_voice", None)
    write_json(config_path(), cfg)
    return cfg


def is_installed() -> bool:
    """检测 SAPI 语音令牌与程序集是否已安装到位。"""
    if not supported():
        return False
    dll = install_dir() / "VoiceHub.Sapi.dll"
    if not dll.is_file():
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _HKLM_TOKEN_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ):
            return True
    except OSError:
        return False


def default_voice_id() -> str:
    """读取当前 SAPI 默认音色令牌 ID；取不到返回空串。"""
    if not supported():
        return ""
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, _HKCU_VOICES_KEY) as key:
                    value, _ = winreg.QueryValueEx(key, "DefaultTokenId")
                    text = str(value).strip()
                    if text:
                        return text
            except OSError:
                continue
    except ImportError:
        pass
    return ""


def _is_self_token(token_id: str) -> bool:
    return token_id.strip().lower().endswith(f"\\{_TOKEN_ID.lower()}")


def is_default() -> bool:
    """当前系统默认 SAPI 音色是否为 VoiceHub。"""
    return _is_self_token(default_voice_id())


def _pick_restore_token() -> str:
    """取消默认时挑一个要换回的系统音色：优先先前保存的，再退回 Huihui 或其它系统音色。"""
    cfg = read_config()
    prev = str(cfg.get("previous_default_voice") or "").strip()
    if prev and not _is_self_token(prev):
        return prev
    if not supported():
        return ""
    try:
        import winreg

        candidates: list[str] = []
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _HKLM_TOKENS_ROOT,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if sub.lower() == _TOKEN_ID.lower():
                    continue
                candidates.append(rf"HKEY_LOCAL_MACHINE\{_HKLM_TOKENS_ROOT}\{sub}")
        for item in candidates:
            if item.lower() == _HUIHUI_TOKEN.lower():
                return item
        if candidates:
            return candidates[0]
    except OSError:
        pass
    return ""


def set_default(on: bool) -> bool:
    """设置或取消 VoiceHub 为当前用户的系统默认 SAPI 音色（写 HKCU，无需管理员权限）。"""
    if not supported():
        return False
    try:
        import winreg

        current = default_voice_id()
        if on:
            if current and not _is_self_token(current):
                save_config(previous_default_voice=current)
            target = _TOKEN_FULL
        else:
            if not _is_self_token(current):
                return True
            target = _pick_restore_token()
            if not target:
                _LOG.warning("找不到可恢复的系统默认 SAPI 音色。")
                return False

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _HKCU_VOICES_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "DefaultTokenId", 0, winreg.REG_SZ, target)
        return True
    except OSError as exc:
        _LOG.warning("切换 SAPI 默认音色失败：%s", exc)
        return False


def _artifacts_fresh(src_dir: Path) -> bool:
    """已编译的 DLL 与双架构原生桩都在且不旧于源码时可跳过重编译。"""
    out_dir = install_dir()
    dll = out_dir / "VoiceHub.Sapi.dll"
    x64 = out_dir / "VoiceHubSapiStub.x64.dll"
    x86 = out_dir / "VoiceHubSapiStub.x86.dll"
    cs = src_dir / "VoiceHub.Sapi.cs"
    rs = src_dir / "sapi_stub.rs"
    if not (dll.is_file() and x64.is_file() and x86.is_file()):
        return False
    try:
        if cs.is_file() and dll.stat().st_mtime < cs.stat().st_mtime:
            return False
        if rs.is_file() and (
            x64.stat().st_mtime < rs.stat().st_mtime
            or x86.stat().st_mtime < rs.stat().st_mtime
        ):
            return False
    except OSError:
        return False
    return True


def status(*, default_endpoint: str = "") -> dict[str, Any]:
    """返回前端展示用的 SAPI 桥接完整状态（纯只读）。"""
    cfg = read_config()
    endpoint = str(cfg.get("endpoint") or default_endpoint or _DEFAULTS["endpoint"]).strip()
    return {
        "object": "sapi_bridge",
        "supported": supported(),
        "installed": is_installed(),
        "is_default": is_default(),
        "can_install": supported() and scripts_dir() is not None,
        "voice_id": str(cfg.get("voice_id") or ""),
        "engine": str(cfg.get("engine") or "auto"),
        "endpoint": endpoint,
        "fallback_voice": str(cfg.get("fallback_voice") or _DEFAULTS["fallback_voice"]),
    }


def _run_ps(argv: list[str], *, timeout: int = 180) -> None:
    """在子进程中执行 PowerShell 脚本；非零退出时抛出带摘要的 RuntimeError。"""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        last_line = detail.splitlines()[-1].strip() if detail else f"退出码 {proc.returncode}"
        raise RuntimeError(last_line)


def install(
    *,
    voice_id: str = "",
    endpoint: str = "",
    set_as_default: bool = True,
) -> dict[str, Any]:
    """执行 ``install.ps1`` 编译并注册 SAPI 桥接（写 HKLM 时会弹 UAC）。"""
    if not supported():
        raise RuntimeError("系统朗读桥接仅支持 Windows。")
    src_dir = scripts_dir()
    if src_dir is None:
        raise RuntimeError("找不到 SAPI 桥接安装脚本（packaging/sapi_bridge/install.ps1）。")

    ep = endpoint.strip().rstrip("/") or str(read_config().get("endpoint") or _DEFAULTS["endpoint"])
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(src_dir / "install.ps1"),
        "-Endpoint",
        ep,
        "-Engine",
        "auto",
    ]
    if voice_id.strip():
        argv.extend(["-VoiceId", voice_id.strip()])
    if set_as_default:
        argv.append("-SetDefault")
    if _artifacts_fresh(src_dir):
        argv.append("-NoBuild")

    _run_ps(argv)
    return status(default_endpoint=ep)


def uninstall() -> dict[str, Any]:
    """执行 ``uninstall.ps1`` 移除 SAPI 令牌与 COM 类并恢复默认音色。"""
    if not supported():
        raise RuntimeError("系统朗读桥接仅支持 Windows。")
    src_dir = scripts_dir()
    if src_dir is None:
        raise RuntimeError("找不到 SAPI 桥接卸载脚本（packaging/sapi_bridge/uninstall.ps1）。")

    argv = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(src_dir / "uninstall.ps1"),
    ]
    _run_ps(argv)
    return status()
