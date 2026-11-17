"""实例进程：拉起、停止、端口探测与日志。

只做进程层的事，不认识模型与清单；配置内容由 ``runtime`` 拼好再交进来。
端口上已有服务就不再起第二个：那不是我们的进程，界面显示为外部实例。
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.infra.loghub import CHANNEL
from app.infra.process import guard_child
from app.infra.process import stop as stop_process
from app.producers.builtin.audiocpp import manager

__all__ = ["Instance", "ProcessPool", "url_of"]

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 端口探测结果的复用窗口（秒）：几处请求同时轮询不必重复打端口。
_PROBE_TTL = 1.5


@dataclass(frozen=True, slots=True)
class Instance:
    """一台实例：``model`` 是引擎请求里用的 id，``family`` 是官方族名。

    ``engine`` 是这一行的出处——引擎设置里的服务地址；空 = 用户手动添加的实例。
    端口与模型 id 都从引擎那边来，插件不另存一份，否则两边一改就各说各话。
    """

    port: int
    model: str
    label: str
    family: str = ""
    engine: str = ""
    #: 由模型扫描补出来的行：端口存在 ``auto_ports``，改端口也写那张表。
    auto: bool = False

    def dump(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "model": self.model,
            "label": self.label,
            "family": self.family,
            "engine": self.engine,
            "auto": self.auto,
        }


def url_of(port: int) -> str:
    return f"http://127.0.0.1:{port}"


class ProcessPool:
    """一台插件实例的进程表；子进程、pid 标记与日志都归它。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.log_dir = root / "logs"
        self.pid_dir = root / "pids"
        self.config_dir = root / "config"
        for folder in (self.log_dir, self.pid_dir, self.config_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._procs: dict[int, subprocess.Popen] = {}
        self._handles: dict[int, Any] = {}
        self._probe_cache: dict[int, tuple[float, tuple[bool, int]]] = {}
        self._job_warned = False

    # ------------------------------------------------------------------ #
    # 派生与回收
    # ------------------------------------------------------------------ #

    def spawn(
        self, exe: Path, config: Path, env: dict[str, str] | None = None
    ) -> tuple[subprocess.Popen, Any]:
        port = int(json.loads(config.read_text(encoding="utf-8"))["port"])
        handle = (self.log_dir / f"{port}.log").open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(exe), "--config", str(config)],
                cwd=str(exe.parent),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError:
            handle.close()
            raise
        guarded = guard_child(process)
        if (
            sys.platform == "win32"
            and not guarded
            and process.poll() is None
            and not self._job_warned
        ):
            self._job_warned = True
            CHANNEL.warning(
                "无法把 audio.cpp 实例放进 Job Object：强杀 VoiceHub 时它们可能残留。",
                source="audiocpp",
            )
        with self._lock:
            self._procs[port] = process
            self._handles[port] = handle
        (self.pid_dir / f"{port}.pid").write_text(str(process.pid), encoding="utf-8")
        self.forget(port)
        return process, handle

    def owned(self, port: int) -> bool:
        return (self.pid_dir / f"{port}.pid").exists()

    def alive(self, port: int) -> bool:
        with self._lock:
            process = self._procs.get(port)
        return process is not None and process.poll() is None

    def stop(self, port: int) -> None:
        with self._lock:
            process = self._procs.pop(port, None)
            handle = self._handles.pop(port, None)
        if process is not None:
            stop_process(process, grace=4.0)
        if handle is not None:
            handle.close()
        (self.pid_dir / f"{port}.pid").unlink(missing_ok=True)
        self.forget(port)

    def stop_all(self) -> None:
        ports = {int(marker.stem) for marker in self.pid_dir.glob("*.pid") if marker.stem.isdigit()}
        with self._lock:
            ports.update(self._procs)
        for port in sorted(ports):
            with contextlib.suppress(Exception):
                self.stop(port)

    def reap(self) -> list[int]:
        """进程自己退了：回收标记与句柄，返回退出的端口。"""
        with self._lock:
            dead = [port for port, proc in self._procs.items() if proc.poll() is not None]
        for port in dead:
            self.stop(port)
        return dead

    def boot_markers(self) -> None:
        """上一次会话的进程无法安全接管：清掉标记，端口探测会认作外部服务。"""
        for marker in self.pid_dir.glob("*.pid"):
            marker.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # 探测与日志
    # ------------------------------------------------------------------ #

    def probe(self, port: int) -> tuple[bool, int]:
        """(服务是否就绪, ``/health`` 报的模型数)；结果缓存一个轮询周期。"""
        now = time.monotonic()
        with self._lock:
            hit = self._probe_cache.get(port)
        if hit and now - hit[0] < _PROBE_TTL:
            return hit[1]
        info = manager.health(url_of(port), timeout=0.25)
        alive = info.get("status") == "ok"
        try:
            count = int(info.get("models") or 0)
        except (TypeError, ValueError):
            count = 0
        result = (alive, count)
        with self._lock:
            self._probe_cache[port] = (now, result)
        return result

    def probe_many(self, ports: Iterable[int]) -> dict[int, tuple[bool, int]]:
        """一次探完所有端口：死端口各要等一个超时，串行会拖成好几秒。"""
        wanted = list(dict.fromkeys(int(port) for port in ports))
        if not wanted:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
            results = list(pool.map(self.probe, wanted))
        return dict(zip(wanted, results))

    @staticmethod
    def port_open(port: int, timeout: float = 0.15) -> bool:
        """端口上有没有东西在 listen：健康接口还没就绪时也不能再起第二份。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def forget(self, port: int) -> None:
        with self._lock:
            self._probe_cache.pop(port, None)

    def wait_ready(self, port: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._procs.get(port) is not None and self._procs[port].poll() is not None:
                return False
            if manager.health(url_of(port), timeout=0.4):
                return True
            time.sleep(0.4)
        return False

    def tail(self, port: int, limit: int = 1600) -> str:
        path = self.log_dir / f"{port}.log"
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - limit * 2))
                return handle.read().decode("utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def config_path(self, port: int) -> Path:
        return self.config_dir / f"{port}.json"


def port_of(url: str) -> int:
    try:
        return int(urlparse(url).port or 0)
    except ValueError:
        return 0
