"""``scripts/ensure_web.py`` 的两条判据：依赖装没装全、产物够不够新。

依赖那条踩过：``node_modules`` 存在，只是 ``package-lock.json`` 之后又加了 ``pinia``，
启动脚本按"没有才装"跳过安装，构建在 rollup 里报 ``failed to resolve import "pinia"`` ——
看着像代码错，其实是没装。所以判据要按 mtime 比，不能只看目录在不在。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ensure_web.py"

_EARLY = 1_000_000.0
_LATE = 2_000_000.0


def _load() -> object:
    spec = importlib.util.spec_from_file_location("ensure_web_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(path: Path, when: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    os.utime(path, (when, when))


@pytest.fixture()
def web(tmp_path: Path):
    """（模块，web 目录，依赖目录，安装戳）。"""
    module = _load()
    root = tmp_path / "web"
    modules = root / "node_modules"
    stamp = modules / ".package-lock.json"
    modules.mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        _stamp(root / name, _EARLY)
    module.WEB, module.NODE_MODULES, module.DEPS_STAMP = root, modules, stamp
    module.DIST, module.INDEX = root / "dist", root / "dist" / "index.html"
    return module, root, modules, stamp


def test_declaration_newer_than_install_requires_reinstall(web) -> None:
    module, root, _, stamp = web
    _stamp(stamp, _EARLY)
    _stamp(root / "package-lock.json", _LATE)
    stale, _ = module.deps_stale()
    assert stale


def test_install_newer_than_declaration_is_fine(web) -> None:
    module, _, _, stamp = web
    _stamp(stamp, _LATE)
    stale, reason = module.deps_stale()
    assert not stale, reason


def test_missing_stamp_falls_back_to_directory_time(web) -> None:
    """缺安装戳 ≠ 需要重装：退回依赖目录 mtime，否则每次启动都白装 50 秒。"""
    module, _, modules, _ = web
    os.utime(modules, (_LATE, _LATE))
    stale, reason = module.deps_stale()
    assert not stale, reason


def test_missing_node_modules_requires_install(web) -> None:
    module, _, modules, _ = web
    modules.rmdir()
    stale, _ = module.deps_stale()
    assert stale


def test_vite_config_counts_as_source(web) -> None:
    """改了 ``vite.config.ts`` 也要重建——列表里写錯扩展名时会静默漏掉。"""
    module, root, _, _ = web
    _stamp(root / "src" / "main.ts", _EARLY)
    _stamp(root / "dist" / "index.html", _EARLY)
    assert not module.is_stale()[0]
    _stamp(root / "vite.config.ts", _LATE)
    stale, reason = module.is_stale()
    assert stale, reason
