"""给源码模式生成桌面快捷方式：双击起托盘，不留控制台窗口。

为什么不再用 ``start.bat``
-------------------------
``start.bat`` 是**开发启动器**：开控制台、跑 ``uv sync``、把失败原因打在窗口里、
用 ``Ctrl+C`` 结束。调试时这些都对，日常自用则每次都亮一个黑窗口，退出还得回窗口里按。

源码态要的其实是发布态的体验：双击、没窗口、右下角有图标、能勾开机自启。
所以快捷方式的目标是 ``.venv\\Scripts\\pythonw.exe``（Windows 无控制台子系统），
参数是 ``packaging/voicehub_launcher.py --data-dir <仓库>/data``。``pythonw`` 下
``sys.stdout`` / ``sys.stderr`` 为 ``None``，launcher 据此把输出改道到
``<数据目录>/logs/launcher.log``——正是为打包版写的那条分支，无需另加代码。

``--data-dir`` 必须显式给：launcher 的默认值是 ``%LOCALAPPDATA%\\voicehub``，
而 ``start.bat`` 一直用的是仓库 ``data``。不给就是"换个启动方式，音色库空了"。
这个参数还会被写进开机自启的命令，登录后仍指向同一份库。

用法（仓库根）::

    uv run python scripts/make_shortcut.py                  # 生成到桌面
    uv run python scripts/make_shortcut.py --dir .          # 生成到指定目录
    uv run python scripts/make_shortcut.py --no-data-dir    # 改用 %LOCALAPPDATA% 的数据
    uv run python scripts/make_shortcut.py --name "VoiceHub" --force
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "packaging" / "voicehub_launcher.py"
ICON = ROOT / "packaging" / "voicehub.ico"
DEFAULT_DATA = ROOT / "data"

#: 桌面可能被 OneDrive 重定向；``~/Desktop`` 在那种机器上是个空壳目录。
_SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"


def _desktop() -> Path:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        if str(value).strip():
            return Path(value)
    except OSError:
        pass
    return Path.home() / "Desktop"


def _ps(value: str) -> str:
    """PowerShell 单引号字符串：内部单引号写两遍。路径含空格也不需要额外处理。"""
    return "'" + value.replace("'", "''") + "'"


def _pythonw() -> Path:
    """venv 里的 pythonw；退回当前解释器（会带控制台，提示但不拦）。"""
    windowless = Path(sys.executable).resolve().with_name("pythonw.exe")
    if windowless.is_file():
        return windowless
    print(f"[shortcut] 未找到 {windowless}，退回 {sys.executable}（会有控制台窗口）。")
    return Path(sys.executable).resolve()


def create_shortcut(target: Path, arguments: str, link: Path) -> None:
    lines = [
        "$shell = New-Object -ComObject WScript.Shell",
        f"$item = $shell.CreateShortcut({_ps(str(link))})",
        f"$item.TargetPath = {_ps(str(target))}",
        f"$item.Arguments = {_ps(arguments)}",
        f"$item.WorkingDirectory = {_ps(str(ROOT))}",
        f"$item.IconLocation = {_ps(f'{ICON},0')}",
        "$item.Description = 'VoiceHub 开发版'",
        "$item.Save()",
    ]
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"创建快捷方式失败：{result.stderr.strip() or result.stdout.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 VoiceHub 源码模式快捷方式")
    parser.add_argument("--dir", default="", help="快捷方式放置目录（默认桌面）")
    parser.add_argument("--name", default="VoiceHub", help="快捷方式名称（不含 .lnk）")
    parser.add_argument("--data-dir", default="", help="数据目录（默认仓库 data）")
    parser.add_argument("--no-data-dir", action="store_true", help="不指定数据目录，用 %LOCALAPPDATA%")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的快捷方式")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("只支持 Windows：自启写注册表 Run 项，快捷方式是 .lnk。")
    if not LAUNCHER.is_file():
        raise SystemExit(f"缺少入口脚本：{LAUNCHER}")
    if not (ROOT / ".venv" / "Scripts" / "pythonw.exe").is_file():
        print("[shortcut] 缺少 .venv。先执行：uv sync --group package")
        return 1

    folder = Path(args.dir).expanduser().resolve() if args.dir else _desktop()
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / f"{args.name}.lnk"
    if link.exists() and not args.force:
        raise SystemExit(f"已存在：{link}（加 --force 覆盖）")

    arguments = f'"{LAUNCHER}"'
    if not args.no_data_dir:
        data = Path(args.data_dir).expanduser().resolve() if args.data_dir else DEFAULT_DATA
        data.mkdir(parents=True, exist_ok=True)
        arguments += f' --data-dir "{data}"'

    create_shortcut(_pythonw(), arguments, link)
    print(f"[shortcut] 已生成：{link}")
    print(f"[shortcut] 目标：{_pythonw()} {arguments}")
    print("[shortcut] 双击启动；开机自启在托盘菜单里勾选。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
