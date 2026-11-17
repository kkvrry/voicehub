"""目录遍历（IO 层）——"哪些文件夹是音色"的判定交给 domain 的纯规则。

一个根目录的产出是一组 ``FoundVoice``。两处省的是同一件事：**每个文件只 stat 一次**。

- 三层缓存共用同一份 ``os.scandir`` 结果（文件名 / 大小 / mtime），不再各自 ``exists``；
- 时长两阶段：缓存命中的当场填，没命中的走完目录后用 :func:`probe_many` 并行量完回填；
- sidecar 每层只读一次：时长备份在这里用掉，元信息随 :attr:`FoundVoice.sidecar`
  一起交给上层合并（不在两处各读一遍同一个文件）；
- 每个音色带一枚**目录指纹**（:attr:`FoundVoice.stamp`）：这一层每个文件的大小与
  mtime 的摘要。上层据此判断"这个文件夹自上次扫描以来有没有动过"，没动就整条复用
  库里的旧行（见 ``LibraryService.scan``），不必重读 sidecar、清单与文本。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.scanner import audio_files_of, emotion_of, relative_category
from app.infra.audioprobe import probe_duration, probe_many
from app.infra.duration_cache import DurationCache
from app.infra.sidecar import (
    SIDECAR_NAME,
    Sidecar,
    read_sidecar,
    sample_bytes,
    sample_duration,
)

__all__ = ["FoundVoice", "walk_root"]

#: 样例文本的清单文件名（``音频名|文本`` 每行一条，GPT-SoVITS 通行格式）。
_LIST_FILES: tuple[str, ...] = ("list.txt", "esd.list", "metadata.txt")
#: 可选封面文件名（按序挑第一张）。``avatar.*`` 是宿主生成的头像，优先于手工封面；
#: 删掉它即回退到 ``cover.*``。
COVER_NAMES: tuple[str, ...] = (
    "avatar.png",
    "avatar.jpg",
    "avatar.jpeg",
    "avatar.webp",
    "cover.png",
    "cover.jpg",
    "cover.jpeg",
    "cover.webp",
)

#: 一次 ``os.scandir`` 拿到的文件事实：``文件名 → (大小, mtime, mtime_ns)``。
#: 前两项与 ``probe_cache`` 的指纹对得上；``mtime_ns`` 只用于目录指纹（更细，防抖）。
FileFacts = dict[str, tuple[int, float, int]]


@dataclass(slots=True)
class FoundVoice:
    """扫描发现的一个音色（sidecar 还没合并——那是 usecase 的事）。"""

    folder: str = ""
    name: str = ""  # 文件夹名（人物名）
    category: str = ""  # 相对扫描根的目录路径（``/`` 连接）；直接在根下 = 空
    root: str = ""  # 属于哪个扫描根目录（界面标注来源用）
    cover: str = ""
    #: 平铺布局标记（根目录直接散放的音频）：此时 folder 是根目录本身，
    #: 身份必须带上文件名，否则同目录几个平铺音色共用一个 id。
    flat: bool = False
    samples: list[dict] = field(default_factory=list)  # file/text/emotion/duration
    #: 目录指纹：这一层的文件集合（名 / 大小 / mtime_ns）的 sha1。
    #: 与库里上次存的一致 = 这个音色自上次扫描以来没动过，可以整条复用。
    stamp: str = ""
    #: 这个文件夹的 sidecar（**每层只读一次**）：时长备份在这里取，元信息由上层合并。
    #: 平铺布局下同目录的几条音色共用这一份——它只读，谁也别在这里改。
    sidecar: Sidecar = field(default_factory=Sidecar)


def _list_dir(folder: Path) -> tuple[FileFacts, list[str]]:
    """列一层目录：文件事实 + 子目录名（**一次 scandir，两边共用**）。

    读不动（权限 / 设备拔了）给空表——与"这里没有音色"同一个结果，不报错。
    """
    files: FileFacts = {}
    subdirs: list[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        subdirs.append(entry.name)
                    elif entry.is_file():
                        stat = entry.stat()
                        files[entry.name] = (stat.st_size, stat.st_mtime, stat.st_mtime_ns)
                except OSError:
                    continue
    except OSError:
        return {}, []
    subdirs.sort()
    return files, subdirs


def _stamp(files: FileFacts, names: Iterable[str]) -> str:
    """这些文件的指纹。名字也进摘要：改名 / 换文件同样算"动过"。"""
    digest = hashlib.sha1()
    for name in names:
        facts = files.get(name)
        if facts is None:
            continue
        digest.update(
            f"{name}\x00{facts[0]}\x00{facts[2]}\n".encode("utf-8", "surrogateescape")
        )
    return digest.hexdigest()


def _read_list_file(folder: Path, files: FileFacts) -> dict[str, str]:
    """读清单文件：``音频名|文本``（有些语料用 ``音频名|文本|说话人``，只取前两段）。"""
    for name in _LIST_FILES:
        if name not in files:
            continue
        path = folder / name
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        table: dict[str, str] = {}
        for line in lines:
            parts = line.split("|")
            if len(parts) >= 2 and parts[0].strip():
                # 键原样收（「开心.wav」「开心」「/abs/path/开心.wav」都有），匹配见 _sample_text。
                table[parts[0].strip()] = parts[1].strip()
        if table:
            return table
    return {}


def _sample_text(folder: Path, audio: str, listed: dict[str, str], files: FileFacts) -> str:
    """样例文本的文件来源：同名 .txt → list 清单（sidecar 覆盖在更上层，不在这里）。"""
    stem = audio.rsplit(".", 1)[0]
    if f"{stem}.txt" in files:
        try:
            text = (folder / f"{stem}.txt").read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        except OSError:
            pass
    for key in (audio, stem, f"{folder}/{audio}"):
        if listed.get(key):
            return listed[key]
    return ""


def _backup_durations(side: Sidecar) -> dict[str, tuple[float, int]]:
    """已经读进来的那份 sidecar 里的时长备份：``文件名 → (秒, 写入时的字节数)``。

    字节数是**校验**：同一个文件（改名、搬家、重建库）大小不变 → 直接采用，不必再起
    ffprobe；被换过内容的同名文件大小会变 → 不认，照常去量。没记字节数的老 sidecar
    只能当参考，由调用方决定信不信（见 :func:`walk_root`）。
    """
    out: dict[str, tuple[float, int]] = {}
    for name, entry in side.samples.items():
        seconds = sample_duration(entry)
        if seconds > 0:
            out[str(name)] = (seconds, sample_bytes(entry))
    return out


def _scan_voice_folder(
    folder: Path,
    *,
    root: Path,
    duration_of,
    files: FileFacts,
    side: Sidecar,
) -> FoundVoice:
    """一个音色文件夹 → ``FoundVoice``。``files`` 来自本层唯一那次 ``scandir``。"""
    audios = audio_files_of(files)
    backups = _backup_durations(side)
    listed = _read_list_file(folder, files)
    cover = next((name for name in COVER_NAMES if name in files), "")
    samples = []
    for name in audios:
        # 时长先给 0.0，未命中的等走完目录并行量完再回填（键必须先存在）。
        sample = {
            "file": name,
            "text": _sample_text(folder, name, listed, files),
            "emotion": emotion_of(name, voice_name=folder.name),
            "duration": 0.0,
        }
        duration_of(sample, folder, name, files[name], backups.get(name))
        samples.append(sample)
    return FoundVoice(
        folder=str(folder),
        name=folder.name,
        category=relative_category(folder, root),
        root=str(root),
        cover=cover,
        samples=samples,
        # 整个文件夹都算：音频、文本、封面、sidecar 任何一样变了都要重新合并。
        stamp=_stamp(files, sorted(files)),
        sidecar=side,
    )


def _flat_stamp(files: FileFacts, name: str) -> str:
    """平铺布局下单独一条音色的指纹：它自己的音频 + 会改它样子的那几份公共文件。

    不含同目录的**其它**音频：往根里丢一条新音频，不该让已有的每一条都重新合并。
    """
    stem = name.rsplit(".", 1)[0]
    names = {name, SIDECAR_NAME, *(n for n in _LIST_FILES if n in files)}
    if f"{stem}.txt" in files:
        names.add(f"{stem}.txt")
    return _stamp(files, sorted(names))


def walk_root(
    root: str,
    *,
    ffprobe_path: str = "",
    cache: DurationCache | None = None,
    on_voice: Callable[[int], None] | None = None,
) -> list[FoundVoice]:
    """扫一个根目录。根目录不存在 / 不是目录 → 空表（不报错，目录可能在移动盘上）。

    规则（判定在 domain）：某层直接含音频即一个音色，不再深入；
    根目录平铺的音频 → 每个文件一个音色，名 = 文件名去后缀。

    :param cache: 时长缓存；``None`` 就每条现量（测试与单文件调用走这条路）。
    :param on_voice: 每发现一个音色回调一次，参数是**本根累计**发现的条数（给进度条用）。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[FoundVoice] = []
    # 没缓存命中的那些：``(样例 dict, 文件路径)``——量完按同一顺序回填。
    pending: list[tuple[dict, Path]] = []

    def duration_of(
        sample: dict,
        folder: Path,
        name: str,
        facts: tuple[int, float, int],
        backup: tuple[float, int] | None = None,
    ) -> None:
        path = folder / name
        if cache is None:
            sample["duration"] = probe_duration(path, ffprobe_path=ffprobe_path) or (
                backup[0] if backup else 0.0
            )
            return
        known = cache.lookup_known(path, facts[0], facts[1])
        if known is not None:
            sample["duration"] = known
            return
        if backup is not None:
            seconds, size = backup
            # 字节数对得上（或压根没记过、缓存又是空的）才认这份备份：
            # 改名/搬家大小不变，直接用；换了内容的同名文件大小变了，去量真的。
            if size == facts[0] or (size <= 0 and cache.is_empty):
                sample["duration"] = seconds
                cache.remember(path, seconds)
                return
        pending.append((sample, path))

    stack = [base]
    while stack:
        current = stack.pop()
        files, subdirs = _list_dir(current)
        audios = audio_files_of(files)
        if not audios:
            # 这一层不算音色；子目录里可能还有，继续往下（后进先出，按名字倒序压）。
            stack.extend(current / name for name in reversed(subdirs))
            continue
        if current == base:
            # 平铺布局：每个音频文件一个音色；清单在这一层只读一次（勿放循环内重读）。
            # 根下的子目录仍是分类，照样往下走。
            stack.extend(current / name for name in reversed(subdirs))
            listed = _read_list_file(current, files)
            side = read_sidecar(current)
            backups = _backup_durations(side)
            for name in audios:
                stem = name.rsplit(".", 1)[0]
                sample = {
                    "file": name,
                    "text": _sample_text(current, name, listed, files),
                    "emotion": "",
                    "duration": 0.0,
                }
                duration_of(sample, current, name, files[name], backups.get(name))
                found.append(
                    FoundVoice(
                        folder=str(current),
                        name=stem,
                        category="",
                        root=str(base),
                        flat=True,
                        samples=[sample],
                        stamp=_flat_stamp(files, name),
                        # 同一层几个平铺音色共用这一份 sidecar（只读）。
                        sidecar=side,
                    )
                )
                if on_voice is not None:
                    on_voice(len(found))
            continue
        found.append(
            _scan_voice_folder(
                current,
                root=base,
                duration_of=duration_of,
                files=files,
                side=read_sidecar(current),
            )
        )
        if on_voice is not None:
            on_voice(len(found))
        # 音色文件夹不再深入（子目录里的是素材，不是另一个音色）。

    if pending:
        # 未命中的批量并行量；probe_many 保序，回填按 pending 顺序对齐。
        durations = probe_many([path for _sample, path in pending], ffprobe_path=ffprobe_path)
        for (sample, path), duration in zip(pending, durations):
            sample["duration"] = duration
            if cache is not None:
                cache.remember(path, duration)
    return found
