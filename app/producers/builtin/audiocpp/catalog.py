"""官方模型清单：快照、刷新与本地已装扫描。

清单的产地是上游仓库的 ``model_specs/*.json``（``scripts/sync_audiocpp_catalog.py``
生成 ``catalog.snapshot.json`` 随插件分发）。插件不再手写第二份：增删模型只重跑
脚本，改文案只改上游。

运行时刷新落到数据目录的 ``catalog.remote.json``，快照坏了或被删都能退回内置版本。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

__all__ = [
    "CATEGORY_LABELS",
    "STATUS_LABELS",
    "categories",
    "families",
    "family",
    "installed_families",
    "installed_packages",
    "package",
    "package_dir",
    "package_files",
    "refresh",
    "source_note",
]

_SNAPSHOT = Path(__file__).with_name("catalog.snapshot.json")

#: 分类与状态的界面说法只在这里改一次；前端不写第二份词表。
CATEGORY_LABELS = {
    "tts": "语音合成",
    "asr": "语音识别",
    "speech_analysis": "语音分析",
    "audio_generation": "音频生成",
    "audio_tools": "音频工具",
    "voice_conversion": "声音转换",
    "community": "社区模型",
    "other": "其他",
}
STATUS_LABELS = {"supported": "支持", "experimental": "实验", "community": "社区"}

#: raw.githubusercontent 直连在国内基本不通（实测 000），镜像排前面。
_RAW_MIRRORS = (
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "",
)
_RAW = "https://raw.githubusercontent.com/0xShug0/audio.cpp/main/model_specs"
_TIMEOUT = 20.0


#: 同一份清单在一次界面轮询里会被问十几次；按路径 + mtime/大小缓存。
_READ_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


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


def _pick(root: Path | None) -> dict[str, Any]:
    """刷新过的清单优先；没有或读坏了退回随插件分发的快照。"""
    if root is not None:
        remote = _read(root / "catalog.remote.json")
        if remote.get("families"):
            return remote
    return _read(_SNAPSHOT)


def families(root: Path | None = None) -> list[dict[str, Any]]:
    rows = _pick(root).get("families")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def family(name: str, root: Path | None = None) -> dict[str, Any]:
    for row in families(root):
        if row.get("family") == name:
            return row
    return {}


def package(package_id: str, root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """按包 id 找 (family, package)；找不到返回两个空字典。"""
    for row in families(root):
        for item in row.get("packages") or []:
            if isinstance(item, dict) and item.get("id") == package_id:
                return row, item
    return {}, {}


def categories(root: Path | None = None) -> list[dict[str, Any]]:
    order = list(CATEGORY_LABELS)
    counts: dict[str, int] = {}
    for row in families(root):
        key = str(row.get("category") or "other")
        counts[key] = counts.get(key, 0) + 1
    return [
        {"key": key, "label": CATEGORY_LABELS.get(key, key), "count": counts[key]}
        for key in order
        if counts.get(key)
    ]


def source_note(root: Path | None = None) -> dict[str, Any]:
    payload = _pick(root)
    refreshed = (root / "catalog.remote.json").is_file() if root is not None else False
    return {
        "source": str(payload.get("source") or "0xShug0/audio.cpp@main/model_specs"),
        "count": len(families(root)),
        "refreshed": refreshed,
    }


def package_dir(models_root: Path | None, item: dict[str, Any]) -> Path | None:
    """包解压后的目录：``模型根 / target_directory``；没有声明时为空。"""
    if models_root is None:
        return None
    target = str(item.get("target_directory") or "").strip("/")
    return Path(models_root) / target if target else None


def package_files(models_root: Path | None, item: dict[str, Any]) -> list[Path]:
    """包在磁盘上的落点：目标目录 + 声明文件（去掉 strip_prefix）；没有文件表时按目录。"""
    if models_root is None:
        return []
    base = package_dir(models_root, item)
    if base is None:
        return []
    prefix = str(item.get("strip_prefix") or "").strip("/")
    files: list[Path] = []
    for path in item.get("files") or []:
        relative = str(path).lstrip("/")
        if prefix and relative.startswith(prefix + "/"):
            relative = relative[len(prefix) + 1 :]
        files.append(base / relative)
    return files or [base]


def installed_packages(models_root: Path | None, root: Path | None = None) -> set[str]:
    """按官方声明的落点扫本地目录：存在即已装。

    官方管理接口只报下载任务、不报已装清单（0.9.4 实测），所以这一层自己扫。
    文件级判断不做哈希：官方 ``.audiocpp-package-.json`` 才是完整性凭据，
    这里只回答"界面上该不该显示已安装"。
    """
    if models_root is None:
        return set()
    folder = Path(models_root)
    if not folder.is_dir():
        return set()
    found: set[str] = set()
    for row in families(root):
        for item in row.get("packages") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            files = package_files(folder, item)
            if files and all(os.path.exists(path) for path in files):
                found.add(str(item["id"]))
    return found


def installed_families(models_root: Path | None, root: Path | None = None) -> set[str]:
    done = installed_packages(models_root, root)
    return {
        str(row["family"])
        for row in families(root)
        if any(isinstance(item, dict) and item.get("id") in done for item in row.get("packages") or [])
    }


def refresh(root: Path) -> dict[str, Any]:
    """从上游重拉 model_specs；失败抛 ``OSError``/``ValueError``，调用方报原话。"""
    listing = _fetch_json("https://api.github.com/repos/0xShug0/audio.cpp/contents/model_specs?ref=main")
    names = sorted(
        str(row.get("name"))
        for row in listing
        if isinstance(row, dict) and row.get("type") == "file" and str(row.get("name")).endswith(".json")
    )
    if not names:
        raise OSError("上游清单为空。")
    with ThreadPoolExecutor(max_workers=8) as pool:
        specs = list(pool.map(lambda name: (name, _fetch_json(f"{_RAW}/{name}")), names))
    rows = [row for name, spec in specs if (row := _trim(name, spec)) is not None]
    rows.sort(key=lambda row: (str(row.get("category")), str(row.get("family"))))
    payload = {
        "source": "0xShug0/audio.cpp@main/model_specs",
        "specs": len(names),
        "families": rows,
    }
    target = root / "catalog.remote.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"count": len(rows), "specs": len(names)}


def _fetch_json(url: str) -> Any:
    last: Exception | None = None
    for prefix in _RAW_MIRRORS:
        try:
            request = urllib.request.Request(
                f"{prefix}{url}", headers={"User-Agent": "VoiceHub/audiocpp"}
            )
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
    raise OSError(f"无法获取上游清单：{last}")


def _trim(name: str, spec: Any) -> dict[str, Any] | None:
    """与 ``scripts/sync_audiocpp_catalog.py`` 同一套裁剪规则，两边必须一致。"""
    if not isinstance(spec, dict):
        return None
    key = str(spec.get("family") or "")
    if not key:
        return None
    caps = spec.get("capabilities") if isinstance(spec.get("capabilities"), dict) else {}
    ui = spec.get("ui") if isinstance(spec.get("ui"), dict) else {}
    packages = []
    for item in spec.get("packages") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        row: dict[str, Any] = {
            field: item[field]
            for field in ("id", "display_name", "description", "default", "format", "precision")
            if item.get(field) not in (None, "")
        }
        if item.get("target_directory"):
            row["target_directory"] = str(item["target_directory"])
        if item.get("strip_prefix"):
            row["strip_prefix"] = str(item["strip_prefix"])
        if isinstance(item.get("files"), list):
            row["files"] = [str(path) for path in item["files"]]
        download = item.get("download") if isinstance(item.get("download"), dict) else {}
        if download.get("repo"):
            row["download"] = {
                "kind": str(download.get("kind") or ""),
                "repo": str(download["repo"]),
                "revision": str(download.get("revision") or "main"),
                "gated": bool(download.get("gated")),
            }
        packages.append(row)
    if not packages:
        return None
    return {
        "family": key,
        "name": str(spec.get("display_name") or key),
        "description": str(spec.get("description") or ""),
        "category": str(spec.get("category") or "other"),
        "status": str(spec.get("status") or "supported"),
        "tasks": [str(item) for item in spec.get("tasks") or []],
        "modes": [str(item) for item in spec.get("modes") or []],
        "languages": [str(item) for item in spec.get("languages") or []],
        "capabilities": sorted(str(item) for item in caps),
        "summary": str(ui.get("summary") or ""),
        "tags": [str(item) for item in ui.get("tags") or []],
        "docs": [str(item) for item in ui.get("docs") or []],
        "packages": packages,
    }
