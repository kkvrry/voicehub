# -*- mode: python ; coding: utf-8 -*-
"""VoiceHub 的 PyInstaller 配置：Windows onedir 桌面版。

入口是 ``packaging/voicehub_launcher.py``（托盘 + 单实例），不是 ``app.main``。
内置插件是运行期按目录发现的，静态分析看不到，因此：

- 插件目录连同 ``.py`` / ``.json`` / ``.svg`` 一起作为数据带上——注册表靠
  ``pkgutil`` / ``plugin.py`` 判断“这个插件在不在”，文件必须真的在磁盘上；
- 同时用 ``collect_submodules`` 把这些模块收进 PYZ，避免运行期再从源码路径导入。

ffmpeg / ffprobe 由 ``packaging/build.py`` 预先放进 ``packaging/vendor/ffmpeg``。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
PACKAGING = ROOT / "packaging"
VENDOR = PACKAGING / "vendor" / "ffmpeg"

_PLUGIN_ROOTS = (
    "app/engines/builtin",
    "app/llm/builtin",
    "app/embedding/builtin",
    "app/producers/builtin",
)
_PLUGIN_SUFFIXES = {".py", ".json", ".svg"}


def _plugin_datas() -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for relative in _PLUGIN_ROOTS:
        for path in (ROOT / relative).rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _PLUGIN_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            items.append((str(path), str(path.parent.relative_to(ROOT))))
    return items


datas: list[tuple[str, str]] = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(ROOT / "app" / "infra" / "texts_default.json"), "app/infra"),
    (str(PACKAGING / "voicehub.ico"), "assets"),
]
datas += _plugin_datas()

binaries: list[tuple[str, str]] = []
for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"):
    path = VENDOR / name
    if path.is_file():
        if name.lower().endswith(".exe"):
            binaries.append((str(path), "vendor/ffmpeg"))
        else:
            datas.append((str(path), "vendor/ffmpeg"))

# 共享库构建的 ffmpeg 把解码器放在 DLL 里，必须与 exe 同目录（Windows 加载器只搜
# exe 所在目录）。static 构建没有这些文件，循环自然为空。
for path in sorted(VENDOR.glob("*.dll")):
    binaries.append((str(path), "vendor/ffmpeg"))

# soundcard 走延迟导入（内录），显式列出，避免静态分析漏掉平台后端；
# 它自带 PyInstaller hook（hook-dirs），后端模块与 cffi 由 hook 负责。
hiddenimports = ["app.main", "voicehub_tray", "soundcard", "multipart", "multipart.multipart"]
for package in (
    "app.engines.builtin",
    "app.llm.builtin",
    "app.embedding.builtin",
    "app.producers.builtin",
):
    hiddenimports += collect_submodules(package)
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

a = Analysis(  # noqa: F821 - SPECPATH / Analysis 由 PyInstaller 注入
    [str(PACKAGING / "voicehub_launcher.py")],
    pathex=[str(ROOT), str(PACKAGING)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PACKAGING / "rthook_bundle.py")],
    excludes=[
        "pytest",
        "ruff",
        "tkinter",
        "PySide6",
        "PyQt5",
        "PyQt6",
        # numpy 是 onnxruntime 的运行时依赖（向量模型），不能再排除。
        "pandas",
        "matplotlib",
        "scipy",
        "torch",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PACKAGING / "voicehub.ico"),
    version=str(PACKAGING / "version_info.txt"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VoiceHub",
)
