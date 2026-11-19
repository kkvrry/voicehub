"""音频库用例：录音、内录、生成物三类音频的索引，以及「入库 / 删除」两个写动作。

**索引只读盘，不建第二份状态。** 三类音频各自有家：

- 生成物：``<引擎保存目录>/<音色>/<文件>`` 与 ``<缓存根>/<引擎>/<音色>/<文件>``；
- 录音与内录：``<数据目录>/recordings/<文件>``，按文件名前缀区分两类。

条目的 id 就是定位串（``gen/<引擎 key>/<音色目录>/<文件>``、``rec/<文件>``），
入库与删除都按它反查路径。尺寸、时长、文本、情绪都在各自目录的 sidecar 里，
与音色库看到的是同一份东西。

**入库 = 复制一份进音色目录**，源文件不动。样例必须落在音色目录里才活得过缓存清理，
所以这里不做「引用缓存」那种省磁盘的做法。入库后两处 sidecar 各记一笔：目标音色记
``source``（这条样例从音频库哪一条来），源目录记 ``absorbed``（这条音频进了哪几条音色）。
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.domain.models import voice_id_for
from app.engines.registry import list_engines
from app.infra import archive, recordings, sidecar
from app.infra.audioprobe import probe_duration_cached
from app.infra.config import load_config
from app.infra.sidecar import Sidecar, read_sidecar, write_sidecar
from app.infra.trash import move_to_trash

__all__ = ["AudioError", "absorb", "discard", "index"]

#: 文件名开头的时间戳（``20261111-144814-01.wav``）。取不到就用 mtime。
_STAMP = re.compile(r"^(\d{8})-(\d{6})")

#: 索引缓存。读盘成本随产物增长，而同一秒里界面会问两三次（列表、计数、引擎分组）。
#: 只认「时间 + sidecar 写入次数」：写入动作自己会 :func:`forget`。
_TTL = 5.0
_CACHE: dict[str, Any] = {"at": 0.0, "revision": -1, "items": []}


class AudioError(RuntimeError):
    """音频库的业务错误；``code`` 进错误码表，``message`` 可直接展示。"""

    def __init__(self, message: str, *, code: str = "audio_failed", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def forget() -> None:
    """丢掉索引缓存：写动作之后调一次，下一读立刻看到结果。"""
    _CACHE["at"] = 0.0
    _CACHE["revision"] = -1


# -- 索引 --------------------------------------------------------------- #


def _created(path: Path) -> int:
    match = _STAMP.match(path.name)
    if match:
        try:
            return int(datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp())
        except ValueError:
            pass
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _files(folder: Path) -> list[Path]:
    """一个目录里的音频，新的在前（文件名带时间戳）。读不动当空。"""
    try:
        return sorted(
            (
                path
                for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in archive.AUDIO_SUFFIXES
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return []


def _voice_name(folder: Path, side: Sidecar) -> str:
    return str(side.display or "").strip() or folder.name


def _audio_url(kind: str, key: str, label: str, voice: str, name: str) -> str:
    rel = archive.display_rel(key, label, voice, name, as_sample=kind == "gen")
    return f"/api/v1/generated/{quote(rel, safe='/')}?engine={quote(key)}"


#: 这两项描述的是"生成这段音频时用了什么参考"，不是这条音频自己的属性：
#: 列在行里是一条指向别处的长路径，写进目标音色更是两行无意义的参数。
_REFERENCE_KEYS = ("reference_audio", "reference_text")


def _params(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    # 长值是引导指令一类的东西（一句话几十个字），列在行里只会把文本挤没；
    # 参数摘要只回答"这段用了什么"，不铺开原文。
    return [
        f"{key} {value}".strip()
        for key, value in raw.items()
        if str(value).strip() and key not in _REFERENCE_KEYS and len(str(value)) <= 24
    ]


def _sample_params(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (row.get("params_map") or {}).items()
        if key not in _REFERENCE_KEYS
    }


def _engine_rows() -> list[tuple[str, str, str, Path]]:
    """(kind, key, label, root)：每台引擎的样例目录与缓存目录各一条。"""
    rows: list[tuple[str, str, str, Path]] = []
    for key, engine in list_engines().items():
        label = engine.label
        rows.append(("gen", key, label, archive.engine_root(key, label, create=False)))
        rows.append(("cache", key, label, archive.cache_root(key, label, create=False)))
    return rows


def _generated_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for kind, key, label, root in _engine_rows():
        if not root.is_dir():
            continue
        try:
            folders = sorted(path for path in root.iterdir() if path.is_dir())
        except OSError:
            continue
        for folder in folders:
            side = read_sidecar(folder)
            for path in _files(folder):
                sample = side.samples.get(path.name) or {}
                items.append(
                    {
                        "id": f"{kind}/{key}/{folder.name}/{path.name}",
                        "source": "engine",
                        "file": path.name,
                        "path": str(path),
                        "folder": str(folder),
                        "url": _audio_url(kind, key, label, folder.name, path.name),
                        "engine": label,
                        "engine_key": key,
                        "voice_name": _voice_name(folder, side),
                        "voice_id": str(side.origin.get("voice_id") or ""),
                        "text": str(sample.get("text") or ""),
                        "emotion": str(sample.get("emotion") or ""),
                        "params": _params(sample.get("params")),
                        "params_map": dict(sample.get("params") or {}),
                        "duration": float(sample.get("duration") or 0.0)
                        or probe_duration_cached(path, ffprobe_path=load_config().ffprobe_path),
                        "created": _created(path),
                        "absorbed": list(sample.get("absorbed") or []),
                    }
                )
    return items


def _recording_items() -> list[dict[str, Any]]:
    root = recordings.root(create=False)
    if not root.is_dir():
        return []
    side = read_sidecar(root)
    items: list[dict[str, Any]] = []
    for path in _files(root):
        sample = side.samples.get(path.name) or {}
        # 内录的文件名由 loopback 决定（`内录-…`）；其余都是麦克风录音。
        source = "loopback" if path.name.startswith("内录") else "record"
        items.append(
            {
                "id": f"rec/{path.name}",
                "source": source,
                "file": path.name,
                "path": str(path),
                "folder": str(root),
                "url": f"/api/v1/recordings/{quote(path.name)}",
                "engine": "",
                "engine_key": "",
                "voice_name": "",
                "voice_id": "",
                "text": str(sample.get("text") or ""),
                "emotion": str(sample.get("emotion") or ""),
                "params": [],
                "params_map": {},
                "duration": float(sample.get("duration") or 0.0)
                or probe_duration_cached(path, ffprobe_path=load_config().ffprobe_path),
                "created": _created(path),
                "absorbed": list(sample.get("absorbed") or []),
            }
        )
    return items


def _scan() -> list[dict[str, Any]]:
    items = [*_generated_items(), *_recording_items()]
    items.sort(key=lambda item: item["created"], reverse=True)
    return items


def scan() -> list[dict[str, Any]]:
    """全量条目（带缓存）。写动作之后调 :func:`forget`。"""
    now = time.time()
    if now - _CACHE["at"] < _TTL and _CACHE["revision"] == sidecar.revision():
        return _CACHE["items"]
    items = _scan()
    _CACHE.update({"at": now, "revision": sidecar.revision(), "items": items})
    return items


def _since(span: str) -> int:
    """时间范围的起点（本地零点）：今天 / 近 7 天 / 近 30 天。"""
    days = {"today": 0, "7d": 6, "30d": 29}.get(span)
    if days is None:
        return 0
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()) - days * 86400


def index(
    *,
    source: str = "",
    engine: str = "",
    q: str = "",
    status: str = "",
    span: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """按来源 / 引擎 / 入库状态 / 关键词筛出列表，连同左栏的计数一起给。"""
    items = scan()
    counts = {"all": len(items), "record": 0, "loopback": 0, "engine": 0}
    engines: dict[str, int] = {}
    for item in items:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
        if item["source"] == "engine" and item["engine"]:
            engines[item["engine"]] = engines.get(item["engine"], 0) + 1

    rows = items
    since = _since(span)
    if since:
        rows = [item for item in rows if item["created"] >= since]
    if source:
        rows = [item for item in rows if item["source"] == source]
    if engine:
        rows = [item for item in rows if item["engine"] == engine]
    if status == "yes":
        rows = [item for item in rows if item["absorbed"]]
    elif status == "no":
        rows = [item for item in rows if not item["absorbed"]]
    query = q.strip().lower()
    if query:
        rows = [
            item
            for item in rows
            if query
            in f"{item['text']} {item['file']} {item['voice_name']} {item['engine']}".lower()
        ]

    return {
        "object": "list",
        "data": rows[: max(1, limit)],
        "has_more": len(rows) > max(1, limit),
        "total": len(rows),
        "unabsorbed": sum(1 for item in rows if not item["absorbed"]),
        "counts": counts,
        "engines": [{"label": label, "count": count} for label, count in engines.items()],
    }


# -- 入库 --------------------------------------------------------------- #


def _voice_folder(service: Any, voice: Any) -> Path:
    """一条音色的样例该落在哪个目录：它自己的家；平台音色没有家时退回引擎保存目录。"""
    folder = str(getattr(voice, "folder", "") or "").strip()
    if folder:
        return Path(folder)
    engine = list_engines().get(str(voice.source or ""))
    label = engine.label if engine else str(voice.source or "未命名")
    return archive.voice_dir(str(voice.source or ""), label, voice.name or voice.id, create=False)


def _check_folder(folder: Path) -> Path:
    """新建音色的目标目录：必须在用户配的扫描根之内，否则它会「建了但看不见」。"""
    roots = [Path(item) for item in load_config().folders if str(item).strip()]
    target = folder.resolve()
    for root in roots:
        try:
            if target == root.resolve() or target.is_relative_to(root.resolve()):
                return target
        except OSError:
            continue
    raise AudioError(
        "归属文件夹不在音色库的扫描目录内，入库后不会出现在音色库里。",
        code="folder_outside_scan",
    )


def _create_voice(target: dict[str, Any]) -> tuple[Path, str]:
    """新建一条本地音色：建目录 + 写 sidecar。返回 (目录, 名称)。"""
    name = archive.safe_segment(str(target.get("name") or "").strip(), fallback="")
    if not name:
        raise AudioError("新建音色需要名称。", code="name_required")
    base = str(target.get("folder") or "").strip()
    if not base:
        folders = [str(item) for item in load_config().folders if str(item).strip()]
        if not folders:
            raise AudioError("音色库还没有扫描目录，先到设置里加一个。", code="no_scan_root")
        base = folders[0]
    folder = _check_folder(Path(base) / name)
    if folder.exists():
        raise AudioError(f"「{name}」已存在。", code="voice_exists", status=409)
    folder.mkdir(parents=True)
    write_sidecar(
        folder,
        Sidecar(
            display=name,
            gender=str(target.get("gender") or ""),
            language=str(target.get("language") or ""),
        ),
    )
    return folder, name


def _voice_target(service: Any, target: dict[str, Any], row: dict[str, Any]) -> tuple[Path, str, str]:
    """解析这一条的落点：(目录, voice_id, 显示名)。"""
    kind = str(target.get("kind") or "")
    if kind == "origin":
        voice_id = str(row.get("voice_id") or "")
        if not voice_id:
            raise AudioError("这条音频不属于任何音色，需要指定目标。", code="target_required")
        voice = service.get_voice(voice_id)
        return _voice_folder(service, voice), voice.id, voice.shown_name
    if kind == "voice":
        voice_id = str(target.get("voice_id") or "")
        if not voice_id:
            raise AudioError("没有选择目标音色。", code="target_required")
        voice = service.get_voice(voice_id)
        return _voice_folder(service, voice), voice.id, voice.shown_name
    raise AudioError("入库目标不明确。", code="target_required")


def _copy_into(folder: Path, source: Path) -> Path:
    """把音频复制进音色目录，返回落点。

    同名同大小 = 已经入过这一条，直接复用（幂等）；同名不同大小 = 另一段音频撞了名字，
    加序号而不是覆盖。
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    try:
        if target.exists() and target.stat().st_size == source.stat().st_size:
            return target
    except OSError:
        pass
    if target.exists():
        for n in range(2, 100):
            candidate = folder / f"{source.stem}-{n}{source.suffix}"
            if not candidate.exists():
                target = candidate
                break
    shutil.copy2(source, target)
    return target


def _set_main(folder: Path, name: str) -> None:
    side = read_sidecar(folder)
    if side.main_sample == name:
        return
    side.main_sample = name
    write_sidecar(folder, side)


def absorb(service: Any, ids: list[str], target: dict[str, Any]) -> list[dict[str, Any]]:
    """把这几条音频登记为某条音色的样例。返回每条的去处。"""
    items = {item["id"]: item for item in scan()}
    rows = [items[item_id] for item_id in ids if item_id in items]
    if not rows:
        raise AudioError("音频不存在，可能已被清理。", code="audio_not_found", status=404)

    emotion = str(target.get("emotion") or "")
    main = bool(target.get("main_sample"))
    folder: Path | None = None
    voice_id = ""
    voice_name = ""

    if str(target.get("kind") or "") == "new":
        folder, voice_name = _create_voice(target)
        voice_id = voice_id_for(str(folder))
    elif str(target.get("kind") or "") == "origin":
        # 回原音色只在这一批同源时成立；前端也是按这个条件给默认项的。
        same = {str(item.get("voice_id") or "") for item in rows}
        if len(same) != 1 or "" in same:
            raise AudioError("这批音频不属于同一条音色，需要指定目标。", code="target_required")

    done: list[dict[str, Any]] = []
    for row in rows:
        if folder is not None:
            target_folder, target_voice, name = folder, voice_id, voice_name
        else:
            target_folder, target_voice, name = _voice_target(service, target, row)
        source = Path(row["path"])
        dest = _copy_into(target_folder, source)
        archive.record_sample(
            target_folder,
            dest.name,
            text=row["text"],
            emotion=emotion or row["emotion"],
            params=_sample_params(row),
            origin={"voice_id": target_voice, "engine": row["engine_key"]},
            source=row["id"],
        )
        archive.note_absorbed(
            Path(row["folder"]), row["file"], voice_id=target_voice, name=name
        )
        if main and dest.name != "":
            _set_main(target_folder, dest.name)
        done.append({"voice_id": target_voice, "name": name, "file": dest.name})

    forget()
    service.invalidate_generated()
    if target.get("kind") == "new":
        # 新目录要经过一次扫描才进库；不阻塞这次请求，库自己起后台任务。
        service.start_scan()
    return done


# -- 删除 --------------------------------------------------------------- #


def _resolve(item_id: str) -> Path:
    """定位串 → 绝对路径。越界、不存在一律报错（不返回"路径不合法"，那会泄露目录）。"""
    kind, _, rest = item_id.partition("/")
    if kind == "rec":
        root = recordings.root(create=False)
        target = archive.resolve_within(root, rest)
    elif kind in {"gen", "cache"}:
        key, _, tail = rest.partition("/")
        engine = list_engines().get(key)
        if engine is None:
            target = None
        else:
            root = (
                archive.engine_root(key, engine.label, create=False)
                if kind == "gen"
                else archive.cache_root(key, engine.label, create=False)
            )
            target = archive.resolve_within(root, tail)
    else:
        target = None
    if target is None or not target.is_file():
        raise AudioError("音频不存在，可能已被清理。", code="audio_not_found", status=404)
    return target


def discard(ids: list[str]) -> int:
    """把这几条音频移进回收站。生成物连 sidecar 记录一起摘；录音只有文件。"""
    removed = 0
    for item_id in ids:
        target = _resolve(item_id)
        if item_id.startswith("rec/"):
            move_to_trash(target)
        else:
            archive.discard_audio(target)
        removed += 1
    forget()
    return removed
