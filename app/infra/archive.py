"""生成出来的音频放哪：两个根目录、两种东西。

- **样例**：``<引擎的音色保存目录>/<音色名>/<文件>``，入口是音色卡片的「生成样例」，
  平台音色生成一次才有得听，落在它自己的音色文件夹里挂回去；
- **内容**：``<缓存根>/<引擎名>/<音色名>/<文件>``（:func:`cache_root`），
  生成页扇出一次的结果，是一次生成的内容、不是谁的样例。

两者分根：本机引擎拿库里的音色生成的仍是那条音色的声音，不能变成引擎自己的"音色"。
缓存根可配（默认数据目录下的 ``cache``）、永不扫描——生成页随手跑不会污染用户配的生成结果目录。

没配引擎自己的目录时，保存目录回退到 ``<output_dir 或 数据目录>/<引擎 label>/<音色名>/<文件>``，
与老路径一致。路径规则只有这一份，api / usecase / 界面都问它。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.infra.atomic import write_bytes
from app.infra.audioprobe import probe_duration
from app.infra.config import cache_base, data_dir, load_config
from app.infra.sidecar import Sidecar, read_sidecar, sample_duration, write_sidecar
from app.infra.trash import move_to_trash

__all__ = [
    "AUDIO_SUFFIXES",
    "ArchiveError",
    "cache_dir",
    "cache_root",
    "candidate_roots",
    "configured_dirs",
    "default_root",
    "discard_audio",
    "display_rel",
    "engine_root",
    "generated_cache_file",
    "note_absorbed",
    "record_sample",
    "resolve_within",
    "safe_segment",
    "voice_dir",
    "write_audio",
]


class ArchiveError(Exception):
    """归档时的意外（比如同一秒里生成的文件多到排不下）——api 层翻成 500。"""

#: Windows 不允许出现在路径里的字符（``\\ / : * ? " < > |``）与控制字符。
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 认得出的音频后缀。认样例时用它——生成结果只会是各家平台支持的格式。
AUDIO_SUFFIXES = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".pcm", ".aac")


def safe_segment(name: str, fallback: str = "未命名") -> str:
    """把引擎名 / 音色名洗成合法目录名。

    去掉结尾的点与空格（Windows 会悄悄吃掉，否则「茉莉.」与「茉莉」写成同一目录）。
    """
    cleaned = _UNSAFE.sub("_", str(name or "")).strip().strip(".").strip()
    return cleaned or fallback


def resolve_within(root: Path, name: str) -> Path | None:
    """把请求给的 ``name`` 解析成 ``root`` 内的绝对路径，越界返回 ``None``。

    绝对路径会被 pathlib 用来整体替换 root，``..`` 与指向目录外的符号链接由
    ``resolve()`` 还原——三种都在 ``is_relative_to`` 这一关被挡下。
    """
    try:
        base = root.resolve()
        target = (base / name).resolve()
    except (OSError, RuntimeError):
        return None
    return target if target.is_relative_to(base) else None


def default_root() -> Path:
    """全局那一层（设置里「生成结果目录」，空 = 服务数据目录下的 ``generated``）。"""
    custom = load_config().output_dir.strip()
    return Path(custom) if custom else data_dir() / "generated"


def configured_dirs() -> dict[str, str]:
    """引擎 → 用户指定的保存目录（没指的引擎不在里面）。"""
    return {
        str(key): str(value).strip()
        for key, value in load_config().engine_outputs.items()
        if str(value).strip()
    }


def _ensure(path: Path) -> Path:
    """目录在就返回，不在才建。

    ``mkdir(parents=True, exist_ok=True)`` 在 Windows 上每次几百微秒，而这几条路径
    在列表请求里每台引擎都要走一遍（见 ``LibraryService._generated_index``）。
    """
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    return path


def engine_root(engine_key: str, label: str, *, create: bool = True) -> Path:
    """这台引擎的音色保存目录：配了用配的（只在写时创建），没配 = 全局根下按引擎 label。"""
    custom = configured_dirs().get(engine_key, "")
    path = Path(custom) if custom else default_root() / safe_segment(label)
    return _ensure(path) if create else path


def voice_dir(engine_key: str, label: str, voice_name: str, *, create: bool = True) -> Path:
    """一条音色在磁盘上的家：``<引擎保存目录>/<音色名>``。"""
    path = engine_root(engine_key, label, create=create) / safe_segment(voice_name)
    return _ensure(path) if create else path


def cache_root(engine_key: str, label: str, *, create: bool = True) -> Path:
    """这台引擎的缓存目录：生成页跑出的音频落在这。

    与"音色保存目录"（:func:`engine_root`）是两件事：前者是某条音色的样例，
    后者是一次生成的内容——本机引擎拿库里音色生成的仍是那条音色的声音，不是
    这台引擎的"音色"。固定住缓存根（不跟用户的「生成结果目录」走），
    永不被扫描、不出现在左栏；界面上给绝对路径。
    """
    path = cache_base() / safe_segment(label)
    return _ensure(path) if create else path


def cache_dir(engine_key: str, label: str, voice_name: str, *, create: bool = True) -> Path:
    """一次生成的内容在缓存里的家：``<缓存目录>/<音色名>``。"""
    path = cache_root(engine_key, label, create=create) / safe_segment(voice_name)
    return _ensure(path) if create else path


def generated_cache_file(name: str, label: str) -> Path | None:
    """``/api/v1/generated`` 给的那段相对路径，在缓存里指的是哪个文件。

    缓存内容的相对路径**一律带引擎 label 前缀**（见 :func:`display_rel`），
    所以第一段必须对上：不校验的话，删某台引擎的产物就能顺着相对路径
    删到隔壁那条引擎的缓存里。越界、前缀不符、文件不在，都返回 ``None``。
    """
    base = cache_base().resolve()
    target = resolve_within(base, name)
    if target is None:
        return None
    parts = target.relative_to(base).parts
    return target if parts and parts[0] == safe_segment(label) else None


def discard_audio(path: Path) -> None:
    """把一条生成出来的音频移进回收站，并摘掉 sidecar 里对应的那条记录。

    **两半必须一起动**（与「移除样例」同一条纪律）：只移文件，sidecar 里就留下一个
    指向空文件的名字；只摘记录，文件还在原位的话下次扫描又把它认回来。顺序是
    **先磁盘后记录**——文件移不走的时候，记录不该已经消失。
    """
    move_to_trash(path)
    folder = path.parent
    side = read_sidecar(folder)
    if path.name not in side.samples and side.main_sample != path.name:
        return
    side.samples = {key: entry for key, entry in side.samples.items() if key != path.name}
    if side.main_sample == path.name:
        side.main_sample = ""
    write_sidecar(folder, side)


def candidate_roots() -> list[Path]:
    """``/api/v1/generated/{相对路径}`` 该去哪几个地方找。

    顺序：全局生成结果目录、各引擎自配保存目录（长路径在前，同名时先命中更具体的）、
    最后是缓存目录——同名时先给"留得下来的那一份"（样例）。
    """
    roots = [default_root(), *(Path(p) for p in configured_dirs().values())]
    found = sorted(roots, key=lambda path: len(str(path)), reverse=True)
    return [*found, cache_base()]



def display_rel(
    engine_key: str, label: str, voice_name: str, filename: str, *, as_sample: bool = True
) -> str:
    """界面上显示的那一段（``引擎/音色/文件`` 或 ``音色/文件``）。

    配了引擎自己的保存目录时不套引擎名（那层目录本身就叫"XX 音色保存目录"）；
    完整绝对路径另给。缓存内容（``as_sample=False``）无论如何都套引擎名，
    ``/api/v1/generated`` 靠它找回。
    """
    voice = safe_segment(voice_name)
    if as_sample and configured_dirs().get(engine_key):
        return f"{voice}/{filename}"
    return f"{safe_segment(label)}/{voice}/{filename}"


def _param_text(value: Any) -> str:
    """参数值 → 一行里能显示的样子（整数值浮点去掉小数点），不做单位换算、不加中文。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def record_sample(
    folder: Path,
    filename: str,
    *,
    text: str = "",
    emotion: str = "",
    params: Mapping[str, Any] | None = None,
    origin: Mapping[str, str] | None = None,
    source: str = "",
) -> None:
    """把"这一段念的是什么、拿什么参数生成的"写进该音色文件夹的 sidecar。

    写的是同一 sidecar 格式（``voice.json`` 的 ``samples`` 键，键为文件名），
    平台音色与引擎目录被当作扫描根的几家看到同一份东西，无需第二套解析。

    :param origin: 音频属于库里哪条音色（``{"voice_id": …, "engine": …}``），
        与音频一起写——文件夹名会变，认得回归属靠这个。
    :param source: 这条样例从历史里的哪一条来（历史条目的 id）。历史据此回答
        "这条入库了没有"，不必反查所有音色目录。
    """
    text = str(text or "").strip()
    emotion = str(emotion or "").strip()
    extra: dict[str, str] = {}
    for key, value in (params or {}).items():
        shown = _param_text(value)
        if shown:
            extra[str(key)] = shown
    recorded_origin = {str(k): str(v) for k, v in (origin or {}).items() if str(v).strip()}
    recorded_source = str(source or "").strip()
    if not (text or emotion or extra or recorded_origin or recorded_source):
        return  # 没东西可记就不动文件
    side = read_sidecar(folder)
    entries = dict(side.samples)
    known = side.samples.get(filename, {})
    target = folder / filename
    # 这一段的字节数：时长的校验值，和 duration 一起存（扫描器靠它跳过 ffprobe）。
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    # 时长**落盘这一刻量一次**：不管这段音频从哪来（生成、试听存样例、平台音色挂目录），
    # 记录它的时候就把 (时长, 字节数) 写进 sidecar——扫描器以后不必再为它起 ffprobe。
    # 已经记过（同一文件名重写记录）就不再量：那次量的是同一个文件，值仍然算数。
    duration = sample_duration(known) or probe_duration(
        target, ffprobe_path=load_config().ffprobe_path
    )
    # 用户给这一段加过的标签要留住，这里只补来路两栏。
    entries[filename] = {
        "text": text,
        "emotion": emotion,
        "tags": list(known.get("tags") or []),
        "params": extra,
    }
    # 历史回写的那两栏是这条样例的事实，重写记录时不能丢：丢了之后「它从哪来、
    # 已经入库到哪几条音色」就说不出来，再入一次库会重复记一条。
    if recorded_source:
        entries[filename]["source"] = recorded_source
    if known.get("absorbed"):
        entries[filename]["absorbed"] = list(known["absorbed"])
    if duration > 0 and size > 0:
        # 时长与它的校验值成对写：光留一个字节数没人能拿来做什么（读的一侧也把它丢掉）。
        entries[filename]["duration"] = duration
        entries[filename]["bytes"] = size
    write_sidecar(
        folder,
        Sidecar(
            gender=side.gender,
            age=side.age,
            language=side.language,
            tags=side.tags,
            description=side.description,
            display=side.display,
            main_sample=side.main_sample,
            favorite=side.favorite,
            hidden=side.hidden,
            samples=entries,
            # 归属已记过的不再覆盖——它是文件的事实，后来的调用不该改写。
            origin=side.origin or recorded_origin,
        ),
    )


def note_absorbed(folder: Path, filename: str, *, voice_id: str, name: str = "") -> None:
    """在源音频的 sidecar 里记一笔「已入库到哪条音色」。

    历史据此回答「这条入库了没有」，不必反查所有音色目录。记录键与样例同形
    （``samples[<文件名>].absorbed``）；重复入库到同一目标只留一条。

    录音目录不是音色目录，但 sidecar 格式通用：它没有条目时这里补一条，只写这一栏。
    """
    voice = str(voice_id or "").strip()
    if not voice:
        return
    side = read_sidecar(folder)
    entry = dict(side.samples.get(filename) or {})
    rows = [
        row
        for row in (entry.get("absorbed") or [])
        if isinstance(row, dict) and str(row.get("voice_id") or "") != voice
    ]
    rows.append({"voice_id": voice, "name": str(name or "")})
    entry["absorbed"] = rows
    side.samples[filename] = entry
    write_sidecar(folder, side)


def write_audio(
    engine_key: str,
    label: str,
    voice_name: str,
    data: bytes,
    *,
    fmt: str,
    voice_id: str = "",
    text: str = "",
    emotion: str = "",
    params: Mapping[str, Any] | None = None,
    as_sample: bool = True,
    folder: Path | None = None,
) -> tuple[str, Path]:
    """把一段生成出来的音频落盘，返回 ``(显示用的相对路径, 绝对路径)``。

    文件名 = 时间戳 + 序号，同一秒内连生成几次往后排号、不覆盖上一份；
    同时把来路登记进该目录的 sidecar（见 :func:`record_sample`）。

    :param as_sample: ``True``（默认）落进音色保存目录、平台音色当场挂为样例；
        ``False``（生成页）落进缓存目录，不扫进库、不挂给音色。
    :param voice_name: 目录名，调用方须给稳定标识（显示名会变，旧音频会失散）。
    :param voice_id: 库内 id，记进 sidecar，读回按它认归属。
    :param folder: 显式指定落盘目录——平台音色挂着本地样例目录时，样例就写回那里，
        而不是另起一个不会被扫描的引擎目录。给了它就不再按 ``as_sample`` 推路径。
    """
    suffix = f".{fmt.strip().lower()}" if fmt else ".bin"
    target_dir = folder or (
        voice_dir(engine_key, label, voice_name, create=True)
        if as_sample
        else cache_dir(engine_key, label, voice_name, create=True)
    )
    _ensure(target_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        target = target_dir / f"{stamp}-{n:02d}{suffix}"
        if not target.exists():
            # 先落临时文件再改名：生成页重复点击/重试时，读端不会读到半段音频。
            write_bytes(target, data)
            record_sample(
                target_dir,
                target.name,
                text=text,
                emotion=emotion,
                params=params,
                origin={"voice_id": voice_id, "engine": engine_key} if voice_id else None,
            )
            rel = (
                f"{target_dir.name}/{target.name}"
                if folder is not None
                else display_rel(engine_key, label, voice_name, target.name, as_sample=as_sample)
            )
            return rel, target
    raise ArchiveError(f"写入 {target_dir} 的文件过多，请稍后重试。")
