"""GitHub 正式发布检查：只回答「有没有新发布」，不下载、不替换任何文件。

两个来源分开看：

- **桌面版**（PyInstaller）：当前版本就是构建时的版本号，直接与最新发布比较；
- **源码版**：本地可能已经领先最后一个 tag（改了很多提交但还没发版），
  所以比较时取「构建版本号」与 ``git describe`` 里那个 tag 的较大者——
  源码领先一个已发布的版本时不该被提示「有新版本」。

网络失败、限流与仓库还没有发布都是**可预期的结果**，不抛接口错误；
响应里的 ``error`` 交给界面原样显示。
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app.infra.netenv import normalize_no_proxy_env

__all__ = ["REPO", "REPO_URL", "check"]

REPO = "kkvrry/voicehub"
REPO_URL = f"https://github.com/{REPO}"
_API = f"https://api.github.com/repos/{REPO}/releases/latest"

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "VoiceHub-release-check",
}

#: GitHub 未认证接口每小时 60 次；同一分钟内连点几次不必次次打网络。
_CACHE_TTL = 60.0
_CACHE: tuple[float, dict[str, Any]] | None = None
_LOCK = threading.Lock()

_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_DESCRIBE = re.compile(r"^v?(\d+\.\d+\.\d+)-(\d+)-g([0-9a-f]+)(-dirty)?$")


class ReleaseError(Exception):
    """一次检查失败：``code`` 给程序判断，``message`` 原样给界面。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    """``v0.2.0`` / ``0.2.0-beta.1`` → (0, 2, 0)；认不出返回 None。"""
    match = _TAG.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _repo_root() -> Path:
    # app/infra/releases.py → 仓库根。冻结包里不该走 git，调用方先看 sys.frozen。
    return Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _source_info() -> dict[str, Any]:
    """源码态的本机身份；拿不到 git 也不影响版本比较。"""
    if getattr(sys, "frozen", False):
        return {"mode": "desktop", "commit": "", "ahead": 0, "dirty": False, "base_version": ""}

    root = _repo_root()
    if not (root / ".git").exists():
        return {"mode": "source", "commit": "", "ahead": 0, "dirty": False, "base_version": ""}

    described = _git("describe", "--tags", "--long", "--dirty", "--always")
    commit = _git("rev-parse", "--short", "HEAD")
    match = _DESCRIBE.match(described)
    if match:
        return {
            "mode": "source",
            "commit": match.group(3),
            "ahead": int(match.group(2)),
            "dirty": bool(match.group(4)),
            "base_version": match.group(1),
        }
    return {
        "mode": "source",
        "commit": commit or described,
        "ahead": 0,
        "dirty": described.endswith("-dirty"),
        "base_version": "",
    }


def _fetch_latest() -> dict[str, Any]:
    """问 GitHub 要最新正式发布。失败一律转成 :class:`ReleaseError`。"""
    normalize_no_proxy_env()
    with httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:
        response = client.get(_API)

    if response.status_code == 404:
        raise ReleaseError("no_release", "仓库还没有正式发布。")
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        raise ReleaseError("rate_limited", "GitHub 接口已限流，请稍后再试。")
    if response.status_code >= 400:
        raise ReleaseError("github_error", f"GitHub 返回 HTTP {response.status_code}。")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseError("bad_response", "GitHub 返回的内容不是 JSON。") from exc
    if not isinstance(payload, dict):
        raise ReleaseError("bad_response", "GitHub 返回的不是一条发布记录。")

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise ReleaseError("bad_response", "GitHub 的发布条目缺少版本号。")
    version = _version_tuple(tag)
    return {
        "tag": tag,
        "version": ".".join(str(part) for part in version) if version else "",
        "name": str(payload.get("name") or tag).strip(),
        "published_at": str(payload.get("published_at") or "").strip(),
        "url": str(payload.get("html_url") or f"{REPO_URL}/releases/tag/{tag}").strip(),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _payload(
    current_version: str,
    source: dict[str, Any],
    *,
    latest: dict[str, Any] | None,
    update_available: bool,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "object": "release_check",
        "repo": REPO,
        "repo_url": REPO_URL,
        "checked_at": _now(),
        "current": {
            "version": current_version,
            "mode": source["mode"],
            "commit": source["commit"],
            "ahead": source["ahead"],
            "dirty": source["dirty"],
            "base_tag": f"v{source['base_version']}" if source["base_version"] else "",
        },
        "latest": latest,
        "update_available": update_available,
        "error": error,
    }


def check(current_version: str, *, refresh: bool = False) -> dict[str, Any]:
    """查一次最新正式发布；``refresh`` 跳过一分钟缓存。

    只读操作：不写文件、不碰当前进程。失败时返回带 ``error`` 的正常响应，
    由界面决定怎么展示——检查更新失败不是服务故障。
    """
    global _CACHE

    now = time.monotonic()
    with _LOCK:
        if not refresh and _CACHE is not None and now - _CACHE[0] < _CACHE_TTL:
            return _CACHE[1]

    source = _source_info()
    try:
        latest = _fetch_latest()
    except ReleaseError as exc:
        return _payload(
            current_version,
            source,
            latest=None,
            update_available=False,
            error={"code": exc.code, "message": exc.message},
        )
    except httpx.HTTPError as exc:
        return _payload(
            current_version,
            source,
            latest=None,
            update_available=False,
            error={"code": "unreachable", "message": f"无法连接 GitHub：{exc}"},
        )
    except Exception as exc:  # noqa: BLE001 - 检查更新永远不该把接口打成 500
        return _payload(
            current_version,
            source,
            latest=None,
            update_available=False,
            error={"code": "check_failed", "message": f"检查更新失败：{exc}"},
        )

    latest_version = _version_tuple(str(latest.get("version") or latest.get("tag") or ""))
    current_base = _version_tuple(current_version)
    source_base = _version_tuple(str(source.get("base_version") or ""))
    known = [value for value in (current_base, source_base) if value is not None]
    if latest_version is None or not known:
        return _payload(
            current_version,
            source,
            latest=latest,
            update_available=False,
            error={"code": "bad_version", "message": "版本号无法识别，无法比较新旧。"},
        )

    result = _payload(
        current_version,
        source,
        latest=latest,
        update_available=latest_version > max(known),
        error=None,
    )
    with _LOCK:
        _CACHE = (now, result)
    return result
