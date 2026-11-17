"""运行包：清单、下载解压、可执行文件与后端探测。

清单里是 audio.cpp 官方 Release 的资产（URL + sha256），随插件分发；
版本与最低要求的比对也在这里，界面只读结果。
"""

from __future__ import annotations

import hashlib
import json
import platform as sys_platform
import shutil
import subprocess
import tarfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.producers.base import ProducerError
from app.producers.builtin.audiocpp.manager import client

__all__ = [
    "BACKENDS",
    "Installer",
    "catalog",
    "minimum",
    "pick",
    "platform_key",
    "rows",
    "version",
    "version_tuple",
]

_HERE = Path(__file__).resolve().parent
BACKENDS = ("cpu", "cuda", "vulkan", "metal", "hip")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: GitHub Release 直连不通时依次换的镜像前缀（空串 = 直连）。
_MIRRORS = ("", "https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")
_HINT = {"prefix": ""}
_LOCK = threading.Lock()


def catalog() -> dict[str, Any]:
    try:
        payload = json.loads((_HERE / "runtimes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def version() -> str:
    return str(catalog().get("version") or "")


def minimum() -> str:
    return str(catalog().get("minimum") or "")


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


def rows(backend: str = "") -> list[dict[str, Any]]:
    return [
        row
        for row in catalog().get("packages", [])
        if isinstance(row, dict)
        and row.get("platform") == platform_key()
        and (not backend or row.get("backend") == backend)
    ]


def pick(runtime_id: str = "", backend: str = "") -> dict[str, Any]:
    found = rows(backend)
    for row in found:
        if runtime_id and row.get("id") == runtime_id:
            return row
    for row in found:
        if not row.get("experimental"):
            return row
    return found[0] if found else {}


def _mirror_order() -> tuple[str, ...]:
    with _LOCK:
        first = _HINT["prefix"]
    return (first, *[item for item in _MIRRORS if item != first])


def _remember(prefix: str) -> None:
    with _LOCK:
        _HINT["prefix"] = prefix


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
        last: Exception | None = None
        for prefix in _mirror_order():
            try:
                self._stream(part, f"{prefix}{url}", checksum, index, count, prefix)
                _remember(prefix)
                return
            except (httpx.HTTPError, OSError, ProducerError) as exc:
                last = exc
                part.unlink(missing_ok=True)
        raise ProducerError(f"运行包下载失败：{last}") from last

    def _stream(
        self, part: Path, url: str, checksum: str, index: int, count: int, prefix: str
    ) -> None:
        digest = hashlib.sha256()
        mirror = prefix.split("//")[-1].split("/")[0] if prefix else ""
        with (
            client().stream(
                "GET",
                url,
                timeout=httpx.Timeout(60.0, connect=15.0),
                headers={"User-Agent": "VoiceHub/audiocpp-runtime"},
            ) as response,
            part.open("wb") as handle,
        ):
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            written = 0
            for chunk in response.iter_bytes(512 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                ratio = (index + (written / total if total else 0)) / count * 100
                note = f"{written / 1048576:.1f} MB"
                self._sink("downloading", ratio, f"{note} · 镜像 {mirror}" if mirror else note)
        if digest.hexdigest().lower() != checksum.strip().lower():
            raise ProducerError("校验和不匹配，文件已丢弃。")

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
