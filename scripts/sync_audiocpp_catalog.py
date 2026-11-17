"""把 audio.cpp 官方的 ``model_specs/*.json`` 收敛成插件用的清单快照。

上游仓库的 ``model_specs/`` 是模型清单的唯一产地：family、说明、语言、能力、
包与下载地址都在里面。插件不再手写一份，只按本脚本的字段裁剪后落成
``catalog.snapshot.json`` 随插件分发；运行时若允许联网则从同一地址刷新。

用法：

    ./.venv/Scripts/python.exe scripts/sync_audiocpp_catalog.py          # 只看差异
    ./.venv/Scripts/python.exe scripts/sync_audiocpp_catalog.py --write  # 写回快照
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app" / "producers" / "builtin" / "audiocpp" / "catalog.snapshot.json"
REPO = "0xShug0/audio.cpp"
REF = "main"

#: raw.githubusercontent 直连在国内基本不通（实测 000），镜像排在前面；
#: 规则与插件下载运行包一致，换成别的镜像只改这里。
RAW_MIRRORS = (
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "",
)

#: 只留界面用得到的字段，别把 options / sources / tensors 一起搬进来。
PACKAGE_KEYS = ("id", "display_name", "description", "default", "format", "precision")


def _get(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceHub/audiocpp-catalog"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch(path: str) -> Any:
    url = f"https://raw.githubusercontent.com/{REPO}/{REF}/{path}"
    last: Exception | None = None
    for prefix in RAW_MIRRORS:
        try:
            return json.loads(_get(f"{prefix}{url}").decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
    raise SystemExit(f"拉取 {path} 失败：{last}")


def _names() -> list[str]:
    payload = _get(f"https://api.github.com/repos/{REPO}/contents/model_specs?ref={REF}")
    rows = json.loads(payload.decode("utf-8"))
    return sorted(row["name"] for row in rows if row.get("type") == "file" and row["name"].endswith(".json"))


def _package(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row[key] for key in PACKAGE_KEYS if row.get(key) not in (None, "")}
    for key in ("target_directory", "strip_prefix"):
        if row.get(key):
            out[key] = str(row[key])
    if isinstance(row.get("files"), list):
        out["files"] = [str(item) for item in row["files"]]
    download = row.get("download") if isinstance(row.get("download"), dict) else {}
    if download.get("repo"):
        out["download"] = {
            "kind": str(download.get("kind") or ""),
            "repo": str(download["repo"]),
            "revision": str(download.get("revision") or "main"),
            "gated": bool(download.get("gated")),
        }
    return out


def _family(name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    family = str(spec.get("family") or "")
    if not family:
        return None
    caps = spec.get("capabilities") if isinstance(spec.get("capabilities"), dict) else {}
    ui = spec.get("ui") if isinstance(spec.get("ui"), dict) else {}
    row: dict[str, Any] = {
        "family": family,
        "name": str(spec.get("display_name") or family),
        "description": str(spec.get("description") or ""),
        "category": str(spec.get("category") or "other"),
        "status": str(spec.get("status") or "supported"),
        "tasks": [str(item) for item in spec.get("tasks") or []],
        "modes": [str(item) for item in spec.get("modes") or []],
        "languages": [str(item) for item in spec.get("languages") or []],
        "capabilities": sorted(str(key) for key in caps),
        "summary": str(ui.get("summary") or ""),
        "tags": [str(item) for item in ui.get("tags") or []],
        "docs": [str(item) for item in ui.get("docs") or []],
        "packages": [
            _package(item) for item in spec.get("packages") or [] if isinstance(item, dict)
        ],
    }
    if not row["packages"]:
        return None
    return row


def collect() -> dict[str, Any]:
    names = _names()
    with ThreadPoolExecutor(max_workers=8) as pool:
        specs = list(pool.map(lambda name: (name, _fetch(f"model_specs/{name}")), names))
    families = [row for name, spec in specs if (row := _family(name, spec)) is not None]
    families.sort(key=lambda row: (row["category"], row["family"]))
    return {
        "source": f"{REPO}@{REF}/model_specs",
        "specs": len(names),
        "families": families,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写回 catalog.snapshot.json")
    args = parser.parse_args(argv)
    fresh = collect()
    before = json.loads(TARGET.read_text(encoding="utf-8")) if TARGET.is_file() else {}
    old = {row["family"]: row for row in before.get("families", [])}
    new = {row["family"]: row for row in fresh["families"]}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
    print(f"上游 {fresh['specs']} 个规格文件 → {len(new)} 个 family")
    print(f"新增 {len(added)}{'：' + ', '.join(added[:8]) if added else ''}")
    print(f"删除 {len(removed)}{'：' + ', '.join(removed[:8]) if removed else ''}")
    print(f"变更 {len(changed)}{'：' + ', '.join(changed[:8]) if changed else ''}")
    if not args.write:
        print("（未写入；加 --write 落盘）")
        return 0
    TARGET.write_text(
        json.dumps(fresh, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
