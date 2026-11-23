"""运行包：清单、下载解压、可执行文件与后端探测。

默认用插件分发的官方 Release 资产（``runtimes.json``）；显式同步后改用数据目录的
``runtimes.remote.json``，坏掉或删掉都能退回内置版本。最低要求始终取内置门槛，
不随上游版本放宽。界面只读这里的结果。
"""

from __future__ import annotations

import json
import platform as sys_platform
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.infra import download
from app.infra.atomic import write_json
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp.manager import client

__all__ = [
    "BACKENDS",
    "Installer",
    "catalog",
    "minimum",
    "mirror_order",
    "pick",
    "platform_key",
    "refresh",
    "rows",
    "synced",
    "version",
    "version_tuple",
]

_HERE = Path(__file__).resolve().parent
_SNAPSHOT = _HERE / "runtimes.json"
_REMOTE = "runtimes.remote.json"
BACKENDS = ("cpu", "cuda", "vulkan", "metal", "hip")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: GitHub Release 直连不通时依次换的镜像前缀（空串 = 直连）。
_MIRRORS = ("", "https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")
_API_MIRRORS = ("", "https://ghfast.top/", "https://ghproxy.net/", "https://gh-proxy.com/")
_API = "https://api.github.com/repos/0xShug0/audio.cpp/releases/latest"
_TIMEOUT = 20.0
_HINT = {"prefix": ""}
_LOCK = threading.Lock()

#: 同一份清单在一次界面轮询里会被问十几次；按路径 + mtime/大小缓存。
_READ_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

_ASSET_RE = re.compile(
    r"^audio-v(?P<version>[^-]+)-(?P<kind>bin|cudart)-(?P<rest>.+?)(?:\.(?P<ext>tar\.gz|zip))$"
)
_PLATFORMS = (
    ("windows-x64", "windows-x64", "Windows x64"),
    ("ubuntu-x64", "linux-x64", "Linux x64"),
    ("macos-arm64", "macos-arm64", "macOS arm64"),
    ("macos-x64", "macos-x64", "macOS x64"),
)
_VARIANTS = ("portable", "colab")


def _read(path: Path) -> dict[str, Any]:
    key = str(path)
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _READ_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload if isinstance(payload, dict) else {}
        _READ_CACHE[key] = (stamp, value)
        return value
    except (OSError, ValueError):
        return {}


def catalog(root: Path | None = None) -> dict[str, Any]:
    """当前生效的清单：同步过的优先，没有或读坏了退回内置快照。"""
    if root is not None:
        remote = _read(root / _REMOTE)
        if remote.get("version") and remote.get("packages"):
            return remote
    return _read(_SNAPSHOT)


def version(root: Path | None = None) -> str:
    return str(catalog(root).get("version") or "")


def minimum() -> str:
    """最低要求取内置门槛，不随上游版本放宽。"""
    return str(_read(_SNAPSHOT).get("minimum") or "")


def synced(root: Path | None) -> bool:
    if root is None:
        return False
    payload = _read(Path(root) / _REMOTE)
    return bool(payload.get("version") and payload.get("packages"))


def version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text or "").lstrip("v").split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def platform_key() -> str:
    system = sys_platform.system().lower()
    machine = sys_platform.machine().lower()
    if system == "windows":
        return "windows-x64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    if system == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    if system == "linux":
        return "linux-x64" if machine in ("amd64", "x86_64") else f"linux-{machine}"
    return f"{system}-{machine}"


def rows(backend: str = "", root: Path | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in catalog(root).get("packages", [])
        if isinstance(row, dict)
        and row.get("platform") == platform_key()
        and (not backend or row.get("backend") == backend)
    ]


def pick(runtime_id: str = "", backend: str = "", root: Path | None = None) -> dict[str, Any]:
    found = rows(backend, root)
    for row in found:
        if runtime_id and row.get("id") == runtime_id:
            return row
    for row in found:
        if not row.get("experimental"):
            return row
    return found[0] if found else {}


# ---------------------------------------------------------------------- #
# 上游同步
# ---------------------------------------------------------------------- #


def _asset_info(name: str, asset: dict[str, Any]) -> dict[str, Any] | None:
    """把一个 Release 资产解析成清单行；不认识的文件名返回空。"""
    match = _ASSET_RE.match(str(name or ""))
    if match is None:
        return None
    rest = match.group("rest")
    place: tuple[str, str, str] | None = None
    for prefix, key, label in _PLATFORMS:
        if rest == prefix or rest.startswith(prefix + "-"):
            place = (key, label, rest[len(prefix) :].lstrip("-"))
            break
    if place is None:
        return None
    platform, platform_label, tail = place
    parts = tail.split("-")
    if parts and parts[-1] in _VARIANTS:
        parts = parts[:-1]
    token = "-".join(parts)
    backend = ""
    backend_label = ""
    cuda_version = ""
    if token == "cpu":
        backend, backend_label = "cpu", "CPU"
    elif token == "vulkan":
        backend, backend_label = "vulkan", "Vulkan"
    elif token == "metal":
        backend, backend_label = "metal", "Metal"
    elif re.fullmatch(r"rocm[\d.]+(?:-test)?", token):
        backend, backend_label = "hip", "ROCm（实验）"
    elif found := re.fullmatch(r"cuda(\d+(?:\.\d+)?)", token):
        cuda_version = found.group(1)
        backend, backend_label = "cuda", f"CUDA {cuda_version}"
    if not backend:
        return None
    suffix = f"cuda{cuda_version}" if backend == "cuda" else backend
    digest = str(asset.get("digest") or "").strip()
    if ":" in digest:
        digest = digest.split(":", 1)[1]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        digest = ""
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "version": match.group("version"),
        "kind": match.group("kind"),
        "platform": platform,
        "backend": backend,
        "cuda_version": cuda_version,
        "archive": match.group("ext"),
        "id": f"{platform}-{suffix}",
        "label": f"{platform_label} · {backend_label}",
        "experimental": backend == "hip" or "test" in token,
        "url": str(asset.get("browser_download_url") or ""),
        "sha256": digest,
        "size": size,
    }


def _build_packages(assets: Any, version: str = "") -> list[dict[str, Any]]:
    """主包缺校验和的一律丢弃；cudart 按同版本、同平台、同容器配对。"""
    found: dict[str, dict[str, Any]] = {}
    extras: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        info = _asset_info(str(asset.get("name") or ""), asset)
        if info is None or not info["url"]:
            continue
        if version and info["version"] != version.lstrip("v"):
            continue
        if info["kind"] == "cudart":
            if info["sha256"]:
                key = (info["platform"], info["cuda_version"], info["archive"])
                extras.setdefault(key, []).append(
                    {"url": info["url"], "sha256": info["sha256"], "size": info["size"]}
                )
            continue
        if not info["sha256"] or info["id"] in found:
            continue
        found[info["id"]] = {
            "id": info["id"],
            "platform": info["platform"],
            "backend": info["backend"],
            "label": info["label"],
            "archive": info["archive"],
            "url": info["url"],
            "sha256": info["sha256"],
            "size": info["size"],
            "extras": [],
            "experimental": info["experimental"],
        }
    for (platform, cuda_version, archive), items in extras.items():
        row = found.get(f"{platform}-cuda{cuda_version}")
        if row is not None and row["archive"] == archive:
            row["extras"] = items
    return [found[key] for key in sorted(found)]


def refresh(root: Path) -> dict[str, Any]:
    """从上游最新 Release 重建运行包清单；失败抛 ``OSError``，旧清单不动。"""
    payload = _fetch_json(_API)
    if not isinstance(payload, dict):
        raise OSError("上游 Release 返回格式异常。")
    tag = str(payload.get("tag_name") or "")
    release_version = tag.lstrip("v")
    if not release_version:
        raise OSError("上游 Release 缺少版本号。")
    packages = _build_packages(payload.get("assets"), release_version)
    if not packages:
        raise OSError("上游 Release 没有可识别的运行包。")
    document = {
        "note": "上游 Release assets；extras 为 CUDA 运行时依赖，解压到同一目录。",
        "version": release_version,
        "minimum": minimum() or release_version,
        "source": str(
            payload.get("html_url")
            or f"https://github.com/0xShug0/audio.cpp/releases/tag/{tag}"
        ),
        "packages": packages,
    }
    target = Path(root) / _REMOTE
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, document)
    return {"version": release_version, "count": len(packages)}


def _fetch_json(url: str) -> Any:
    last: Exception | None = None
    for prefix in _API_MIRRORS:
        try:
            request = urllib.request.Request(
                f"{prefix}{url}",
                headers={
                    "User-Agent": "VoiceHub/audiocpp-runtime",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
    raise OSError(f"无法获取上游运行包清单：{last}")


def mirror_order() -> tuple[str, ...]:
    """直连与镜像的尝试顺序；上一次成功的排最前。"""
    with _LOCK:
        first = _HINT["prefix"]
    return (first, *[item for item in _MIRRORS if item != first])


def _remember(prefix: str) -> None:
    with _LOCK:
        _HINT["prefix"] = prefix


def remember_mirror(prefix: str) -> None:
    """记下这次成功的镜像；给同进程里别的下载器复用。"""
    _remember(prefix)


class Installer:
    """把一个运行包下载、校验、解压进 ``bin/``；进度通过回调上报。"""

    def __init__(self, root: Path, sink: Callable[[str, float, str], None]) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self._sink = sink
        #: 按可执行文件记后端：内置包与用户指定的外部 exe 能力不同，不能共用一份。
        self._backends: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ #
    # 可执行文件
    # ------------------------------------------------------------------ #

    def exe(self) -> Path | None:
        for name in ("audiocpp_server.exe", "audiocpp_server"):
            found = sorted(self.bin_dir.rglob(name))
            if found:
                return found[0]
        return None

    def backends(self, exe: Path | None = None) -> list[str]:
        """可执行文件实际编译进来的后端；CPU 便携包里选 cuda 会起不来。

        ``exe`` 为空时用内置运行包；用户指定的外部可执行文件同样靠 ``--version``
        自报后端，不按包名猜。
        """
        exe = exe if exe is not None else self.exe()
        if exe is None:
            return list(BACKENDS)
        cached = self._backends.get(str(exe))
        if cached:
            return list(cached)
        try:
            result = subprocess.run(
                [str(exe), "--version"],
                cwd=str(exe.parent),
                capture_output=True,
                text=True,
                # 与 audioprobe 同因：中文 Windows 的 locale 是 GBK，不显式指定
                # 会让 --version 输出在中文路径下解码失败。
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
            for line in (result.stdout or "").splitlines():
                if not line.startswith("backends:"):
                    continue
                found = [part.strip() for part in line.split(":", 1)[1].split(",") if part.strip()]
                if found:
                    self._backends[str(exe)] = found
                    return list(found)
        except (OSError, subprocess.SubprocessError):
            pass
        return list(BACKENDS)

    def forget_backends(self) -> None:
        self._backends.clear()

    # ------------------------------------------------------------------ #
    # 下载与解压
    # ------------------------------------------------------------------ #

    def install(self, row: dict[str, Any]) -> Path:
        jobs = [(str(row.get("url") or ""), str(row.get("sha256") or ""))]
        jobs += [
            (str(item.get("url") or ""), str(item.get("sha256") or ""))
            for item in row.get("extras") or []
            if isinstance(item, dict)
        ]
        jobs = [(link, digest) for link, digest in jobs if link]
        if not jobs:
            raise ProducerError("运行包缺少下载地址。")
        # 运行包会被直接执行：没写校验和就等于"从镜像拿到什么就跑什么"。
        # 清单由插件自己分发（runtimes.json），缺 sha256 是清单错误，不该放行。
        missing = [link for link, digest in jobs if not digest.strip()]
        if missing:
            raise ProducerError(
                f"运行包清单缺少 sha256，已拒绝安装：{Path(missing[0]).name or missing[0]}"
            )
        suffix = ".tar.gz" if any(link.endswith(".tar.gz") for link, _ in jobs) else ".zip"
        parts: list[Path] = []
        try:
            for index, (link, digest) in enumerate(jobs):
                part = self.root / f"runtime-{index}{suffix}.part"
                parts.append(part)
                self._fetch(part, link, digest, index, len(jobs))
            self._sink("downloading", 100.0, "解压中")
            if self.bin_dir.exists():
                shutil.rmtree(self.bin_dir)
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            for part in parts:
                self._unpack(part, "tar.gz" if part.name.endswith(".tar.gz.part") else "zip")
        finally:
            for part in parts:
                part.unlink(missing_ok=True)
        exe = self.exe()
        if exe is None:
            raise ProducerError("包内没有 audiocpp_server.exe。")
        self.forget_backends()
        return exe

    def _fetch(self, part: Path, url: str, checksum: str, index: int, count: int) -> None:
        def progress(written: int, total: int) -> None:
            ratio = (index + (written / total if total else 0)) / count * 100
            self._sink("downloading", ratio, f"{written / 1048576:.1f} MB")

        try:
            download.fetch_with_mirrors(
                url,
                part,
                mirrors=mirror_order(),
                sha256=checksum,
                timeout=httpx.Timeout(60.0, connect=15.0),
                headers={"User-Agent": "VoiceHub/audiocpp-runtime"},
                on_progress=progress,
                on_mirror=_remember,
                client=client(),
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            part.unlink(missing_ok=True)
            raise ProducerError(f"运行包下载失败：{exc}") from exc

    def _unpack(self, part: Path, archive: str = "zip") -> None:
        if archive == "tar.gz":
            with tarfile.open(part, "r:gz") as bundle:
                for info in bundle.getmembers():
                    name = Path(info.name)
                    if name.is_absolute() or ".." in name.parts or not (info.isfile() or info.isdir()):
                        continue
                    dest = self.bin_dir / name
                    if info.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    source = bundle.extractfile(info)
                    if source is None:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with source, dest.open("wb") as target:
                        shutil.copyfileobj(source, target)
            return
        with zipfile.ZipFile(part) as bundle:
            for info in bundle.infolist():
                name = Path(info.filename)
                if name.is_absolute() or ".." in name.parts:
                    continue
                dest = self.bin_dir / name
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, dest.open("wb") as target:
                    shutil.copyfileobj(source, target)
