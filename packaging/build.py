"""一键构建 Windows 便携包：前端 → ffmpeg/图标 → PyInstaller onedir → zip。

用法（在仓库根目录）::

    uv sync --group package
    uv run --group package python packaging/build.py

``--ffmpeg`` 指向 ffmpeg 所在目录或 ffmpeg.exe；不传则从 PATH 上找。构建只写
``web/dist``、``packaging/vendor``、``packaging/voicehub.ico``、``dist`` 与
``build``，不碰源码，日常 ``start.bat`` / ``uv run python -m app.main`` 照旧。
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
VENDOR = PACKAGING / "vendor" / "ffmpeg"
DIST = ROOT / "dist"
APP_DIR = DIST / "VoiceHub"
WORK = ROOT / "build" / "pyinstaller"

# 控制台编码不保证装得下中文：CI 的 Windows runner 是 cp1252，`print("准备 ffmpeg")`
# 直接 UnicodeEncodeError 把构建打断（真实撞到过，还是第一步）。这里只放宽**错误策略**、
# 不换编码——中文 Windows 的 GBK 控制台照常显示，真装不下的字符退化成问号而不是崩。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def _banner(text: str) -> None:
    print(f"\n[build] {text}", flush=True)


def _run(command: list[str], *, cwd: Path = ROOT) -> int:
    print(f"[build] $ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=str(cwd), check=False).returncode


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _folder_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def ensure_web() -> None:
    _banner("构建前端（源码比 dist 新时才会真正重建）")
    code = _run([sys.executable, str(ROOT / "scripts" / "ensure_web.py")])
    if code != 0:
        raise SystemExit("前端构建失败，打包中止。")
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        raise SystemExit("web/dist/index.html 不存在，打包中止。")


def _ffmpeg_dir(explicit: str) -> Path:
    if explicit:
        source = Path(explicit).expanduser().resolve()
        return source.parent if source.is_file() else source
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit(
            "没有找到 ffmpeg。请先安装，或用 --ffmpeg 指向 ffmpeg 所在目录 / ffmpeg.exe。"
        )
    return Path(found).resolve().parent


def stage_ffmpeg(explicit: str) -> None:
    _banner("准备 ffmpeg / ffprobe")
    source = _ffmpeg_dir(explicit)
    suffix = ".exe" if sys.platform == "win32" else ""
    pairs = [("ffmpeg", source / f"ffmpeg{suffix}"), ("ffprobe", source / f"ffprobe{suffix}")]
    missing = [name for name, path in pairs if not path.is_file()]
    if missing:
        raise SystemExit(f"{source} 下缺少：{', '.join(missing)}（两者必须放在同一目录）。")

    VENDOR.mkdir(parents=True, exist_ok=True)
    # 换源时先清掉上一次留下的动态库：static 包没有 DLL，残留会让产物混着两套运行时。
    for stale in VENDOR.glob("*.dll"):
        stale.unlink()

    # 共享库构建的 ffmpeg.exe 只有几百 KB，真正的解码器在旁边的 DLL 里。**必须先放 DLL
    # 再验可执行**：少了它们 `-version` 当场就失败（CI 上真实撞到过——DLL 后拷、
    # 验证在前，于是共享库版一律被自己的验证拦下）。static 构建没有 DLL，自然是空集。
    dlls = sorted(source.glob("*.dll"))
    for dll in dlls:
        shutil.copy2(dll, VENDOR / dll.name)
    if dlls:
        size = sum(d.stat().st_size for d in dlls) / 1024 / 1024
        print(f"[build] 动态库 {len(dlls)} 个：{size:.0f} MB", flush=True)

    for _name, path in pairs:
        target = VENDOR / path.name
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        check = subprocess.run(
            [str(target), "-version"], capture_output=True, text=True, timeout=30, check=False
        )
        if check.returncode != 0:
            detail = " ".join((check.stderr or check.stdout or "").split())[-300:]
            raise SystemExit(f"{target.name} 无法运行（多半是缺同目录的 DLL）：{detail}")

    copied: set[str] = set()
    for candidate in (
        source / "LICENSE",
        source / "LICENSE.txt",
        source / "COPYING",
        source / "COPYING.GPLv3",
        source / "README.txt",
        source.parent / "LICENSE",
        source.parent / "LICENSE.txt",
        source.parent / "README.txt",
    ):
        if candidate.is_file() and candidate.name not in copied:
            shutil.copy2(candidate, VENDOR / candidate.name)
            copied.add(candidate.name)
    print(f"[build] ffmpeg 来源：{source}", flush=True)


def ensure_icon(force: bool) -> None:
    _banner("准备应用图标")
    sys.path.insert(0, str(PACKAGING))
    from make_icon import generate

    path = generate(force=force)
    print(f"[build] 图标：{path}", flush=True)


def run_pyinstaller() -> None:
    _banner("PyInstaller 打包")
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "缺少 PyInstaller。先执行 `uv sync --group package`，"
            "再运行 `uv run --group package python packaging/build.py`。"
        )
    code = _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--log-level",
            "WARN",
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK),
            str(PACKAGING / "voicehub.spec"),
        ]
    )
    if code != 0:
        raise SystemExit("PyInstaller 失败，打包中止。")


def verify() -> None:
    _banner("检查产物")
    required = (
        APP_DIR / "VoiceHub.exe",
        APP_DIR / "_internal" / "web" / "dist" / "index.html",
        APP_DIR / "_internal" / "app" / "infra" / "texts_default.json",
        APP_DIR / "_internal" / "app" / "producers" / "builtin" / "audiobook" / "plugin.py",
        APP_DIR / "_internal" / "app" / "producers" / "builtin" / "audiobook" / "patterns.json",
        APP_DIR / "_internal" / "app" / "engines" / "builtin" / "breeze_tts" / "icon.svg",
        APP_DIR / "_internal" / "vendor" / "ffmpeg" / "ffmpeg.exe",
        APP_DIR / "_internal" / "vendor" / "ffmpeg" / "ffprobe.exe",
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise SystemExit("产物缺文件：\n" + "\n".join(str(path) for path in missing))
    # 在产物目录里真跑一次：动态库漏收时只有这里能发现，否则要等用户首次转码才炸。
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        exe = APP_DIR / "_internal" / "vendor" / "ffmpeg" / name
        check = subprocess.run(
            [str(exe), "-version"], capture_output=True, text=True, timeout=30, check=False
        )
        if check.returncode != 0:
            raise SystemExit(
                f"{name} 在产物目录里无法运行（多半是缺同目录的 DLL）："
                f"{' '.join((check.stderr or '').split())[-300:]}"
            )
    print(f"[build] 程序目录：{APP_DIR}（{_human(_folder_size(APP_DIR))}）", flush=True)


def copy_readme() -> None:
    source = PACKAGING / "README.txt"
    if not source.is_file():
        return
    target = APP_DIR / "使用说明.txt"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8-sig")


def make_zip() -> Path | None:
    _banner("生成便携 zip")
    target = DIST / "VoiceHub-portable.zip"
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, Path("VoiceHub") / path.relative_to(APP_DIR))
    print(f"[build] {target}（{_human(target.stat().st_size)}）", flush=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 VoiceHub Windows 便携包")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg 所在目录或 ffmpeg.exe")
    parser.add_argument("--skip-web", action="store_true", help="跳过前端构建")
    parser.add_argument("--no-zip", action="store_true", help="不生成便携 zip")
    parser.add_argument("--force-icon", action="store_true", help="重新生成图标")
    args = parser.parse_args()

    if not args.skip_web:
        ensure_web()
    stage_ffmpeg(args.ffmpeg)
    ensure_icon(args.force_icon)
    run_pyinstaller()
    verify()
    copy_readme()
    zip_path = None if args.no_zip else make_zip()
    _banner("完成")
    print(f"[build] 双击：{APP_DIR / 'VoiceHub.exe'}", flush=True)
    if zip_path:
        print(f"[build] 分发：{zip_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
