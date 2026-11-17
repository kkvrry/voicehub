"""插件发现：内置子包 + 第三方 entry points，各插件域共用一套规则。

一个插件 = 一个目录（内置）或一条 entry point（第三方），发现它不需要改这里。
各域共用的部分只有「怎么找」：

1. 扫内置包下的子目录，导入模块，取基类子类，按 ``key`` 建实例；
2. 读 entry points，第三方同名覆盖内置；
3. 按 ``key``（或调用方给的次序）排序；
4. 缓存到进程结束——装插件本来就要重启，而每次请求都重扫 ``site-packages``
   里所有 ``dist-info`` 太贵（本机实测 ``entry_points()`` 单次约 20 ms）。

各域不同的是**契约**（基类），由调用方传入；这里不 import 任何具体插件，
也不认识任何前缀之外的东西。装坏的插件跳过并记一条 warning，不拖死整库——
左栏少一行，不是整个面板打不开。
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

__all__ = ["clear_cache", "discover"]

_LOG = logging.getLogger(__name__)

#: 发现结果缓存：`(基类, 组名, 内置根, 模块文件)` → ``key → 实例``。
#:
#: 键要覆盖全部输入：合成引擎与识别引擎扫同一个内置包、各取一遍，靠基类分开；
#: 测试换内置根时也得自然重扫，不能命中上一次的结果。
#: 插件实例不带状态，插件集合在进程内固定，因此整个结果可以缓存；配置项仍每次现读，
#: 改设置不需要重启。
_CACHE: dict[tuple[type, str, str, str], dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """丢掉发现结果（测试注入假插件、或运行期装了新插件时用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _classes_in(module: object, base: type) -> list[type]:
    """模块里所有 ``base`` 的子类（跳过基类自己与没写 ``key`` 的）。"""
    found: list[type] = []
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, base)
            and value is not base
            and getattr(value, "key", "")
        ):
            found.append(value)
    return found


def _instantiate(candidate: Any, base: type) -> Any | None:
    """把「类」或「返回实例的工厂」统一成实例；不合契约返回 ``None``。

    entry point 两种写法都在用：多数域指向类（宿主自己 ``cls()``），
    生产方域指向工厂（由插件决定怎么构造）。两种都收，调用方不必按域分支。
    """
    if isinstance(candidate, type):
        if issubclass(candidate, base) and candidate is not base and getattr(candidate, "key", ""):
            return candidate()
        return None
    if callable(candidate):
        instance = candidate()
        if isinstance(instance, base) and getattr(instance, "key", ""):
            return instance
    return None


def _register(found: dict[str, Any], candidate: Any, base: type, source: str) -> None:
    """登记一个候选；构造失败或契约不符就记一条 warning 后跳过。"""
    try:
        plugin = _instantiate(candidate, base)
    except Exception as exc:  # noqa: BLE001 - 一个坏插件不该拖死整库，理由见模块顶部
        _LOG.warning("%s 构造失败：%s: %s", source, type(exc).__name__, exc)
        return
    if plugin is None:
        _LOG.warning("%s 不是 %s 的有效实现（缺 key 或基类不符）", source, base.__name__)
        return
    found[plugin.key] = plugin


def _builtin_plugins(
    base: type, package: str, root: Path | None, module_file: str
) -> dict[str, Any]:
    """扫内置根下的子目录，每类插件各取一遍（只认子目录）。

    ``root`` 为空时取 ``package`` 包自身的目录；给了就以它为准（测试换目录、
    或某个域的内置根不在包内时用）。模块名始终按 ``package`` 拼。

    ``module_file`` 非空时只认含该文件的子目录，并导入该文件而不是子包本身
    （内置插件写在 ``plugin.py`` 里的域用；其余域插件就是子包的 ``__init__``）。
    """
    if root is None:
        try:
            pkg = importlib.import_module(package)
        except Exception as exc:  # noqa: BLE001 - 包位置随宿主发布，导不进来说明装配坏了
            _LOG.warning("内置插件包 %s 导入失败：%s: %s", package, type(exc).__name__, exc)
            return {}
        root = Path(pkg.__file__).resolve().parent

    found: dict[str, Any] = {}
    for folder in sorted(root.iterdir() if root.is_dir() else ()):
        if not folder.is_dir():
            continue
        # 两种内置形态的准入判据不同，各留各的：插件写在 ``__init__`` 的域要求是包，
        # 写在 ``plugin.py`` 的域只要求那个文件在（目录没 ``__init__`` 也算，导入失败会记日志）。
        if module_file:
            if not (folder / module_file).is_file():
                continue
        elif not (folder / "__init__.py").is_file():
            continue
        name = folder.name
        module_name = f"{package}.{name}"
        if module_file:
            module_name = f"{module_name}.{Path(module_file).stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 一个坏插件不该拖死整库
            _LOG.warning("插件 %s 导入失败：%s: %s", name, type(exc).__name__, exc)
            continue
        for cls in _classes_in(module, base):
            _register(found, cls, base, f"插件 {name}")
    return found


def _entry_point_plugins(group: str, base: type) -> dict[str, Any]:
    """第三方 entry points（同名覆盖内置）。"""
    try:
        found_entries = entry_points(group=group)
    except Exception as exc:  # noqa: BLE001 - 解释器层面的兜底，不能因此空掉整表
        _LOG.warning("entry point 组 %s 读取失败：%s: %s", group, type(exc).__name__, exc)
        found_entries = ()

    found: dict[str, Any] = {}
    for entry in found_entries:
        try:
            candidate = entry.load()
        except Exception as exc:  # noqa: BLE001 - 同上：装坏的包不该让整表空掉
            _LOG.warning("插件 %s 加载失败：%s: %s", entry.name, type(exc).__name__, exc)
            continue
        _register(found, candidate, base, f"插件 {entry.name}")
    return found


def discover(
    group: str,
    base: type,
    *,
    builtin_pkg: str,
    builtin_root: Path | None = None,
    builtin_file: str = "",
    sort_key: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """内置 + 第三方合并后的插件（``key → 实例``），结果缓存。

    返回副本：调用方往里塞东西不该污染注册表。``sort_key`` 收插件实例，
    默认按 ``key``；需要按插件自报次序排的域（生产方）传自己的。
    ``builtin_root`` 见 :func:`_builtin_plugins`。
    """
    key = (base, group, str(builtin_root or ""), builtin_file)
    cached = _CACHE.get(key)
    if cached is not None:
        return dict(cached)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return dict(cached)
        found = _builtin_plugins(base, builtin_pkg, builtin_root, builtin_file)
        found.update(_entry_point_plugins(group, base))
        order = sort_key or (lambda plugin: plugin.key)
        result = dict(sorted(found.items(), key=lambda item: order(item[1])))
        _CACHE[key] = result
        return dict(result)
