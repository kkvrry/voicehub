"""ffmpeg 层：识别音频容器（:func:`container_of`）、找句读断点（:func:`cut_point`）
与转成 wav（:func:`to_wav`，``transient=True`` 时一次性用完即弃）。

容器识别只看魔数、不需要 ffmpeg——说得出来才能告诉用户要装什么。
ffmpeg 查找先配置、再随包、后 PATH（与 :mod:`app.infra.audioprobe` 一致）。

参考音频的转换结果落在缓存根下的 ``refaudio/`` 并复用：文件名带源文件指纹
（路径 + 大小 + mtime），源文件一变就会重转；用不到的那些按设置里的上限与保留期
自动回收（见 :func:`trim_cache`）。识别用的 16k 音频**不缓存**（``transient=True``，落系统临时目录）——那种转码是一次性的。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.infra.archive import safe_segment
from app.infra.config import cache_base, load_config
from app.infra.files import file_stamp
from app.infra.loghub import CHANNEL
from app.infra.process import CREATE_NO_WINDOW
from app.infra.runtime import bundled_binary

__all__ = [
    "REF_RATE",
    "FfmpegError",
    "container_of",
    "container_of_bytes",
    "cut_point",
    "encode_audio",
    "extract_audio",
    "find_ffmpeg",
    "mux_media",
    "normalize",
    "silence_spans",
    "slice_audio",
    "stage_ascii_path",
    "to_wav",
    "trim_cache",
]


class FfmpegError(Exception):
    """转换没做成——``reason`` 是可读说明，调用方原样摆给用户，不能吞掉。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: 转换的统一目标：单声道 44.1kHz 16bit PCM（BreezeTTS 2 官方给出的参数，
#: 也是各家克隆都吃得下的形状：人声克隆不需要立体声）。
REF_RATE = 44100

#: PATH 上找 ffmpeg 只做一次（结果不会在一个进程里变），与 audioprobe 同一条。
_PATH_FFMPEG: str | None = ""  # 空串 = 还没查过


def _sibling_of_ffprobe(configured: str) -> str:
    """``ffprobe_path`` 旁边那个 ffmpeg；没配或不存在返回空串（线索不成立，非出错）。"""
    if not configured:
        return ""
    probe = Path(configured)
    name = "ffmpeg.exe" if probe.suffix.lower() == ".exe" else "ffmpeg"
    candidate = probe.with_name(name)
    return str(candidate) if candidate.is_file() else ""


def find_ffmpeg(configured: str | None = None) -> str:
    """ffmpeg 在哪：**先配置、再随包、后 PATH**；都找不到返回空串（调用方决定怎么报）。

    :param configured: ``ffprobe_path``；``None`` = 读配置，显式空串 = 不翻配置（测试用）。
    """
    global _PATH_FFMPEG
    if configured is None:
        configured = load_config().ffprobe_path
    sibling = _sibling_of_ffprobe(configured)
    if sibling:
        return sibling
    bundled = bundled_binary("ffmpeg")
    if bundled:
        return str(bundled)
    if _PATH_FFMPEG == "":
        _PATH_FFMPEG = shutil.which("ffmpeg") or ""
    return _PATH_FFMPEG


def container_of_bytes(head: bytes) -> str:
    """文件开头几十个字节 → 容器名；认不出返回空串。

    空串 ≠ 不是音频：裸 PCM 本无文件头。调用方照旧拿去转，转坏了 ffmpeg 报得更准。
    """
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        # RIFF 是一族容器（wav/avi/webp），第二段的 WAVE 才是 wav。
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        # MP4 家族（m4a/mp4/mov 同形），对"要不要转"是同一回事。
        return "m4a"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        # 无 ID3 标签的 mp3：帧同步字打头（FFEx）。
        return "mp3"
    if head[:4] == b"FORM" and head[8:12] == b"AIFF":
        return "aiff"
    return ""


def container_of(path: Path) -> str:
    """一个文件是什么壳（读不到就返回空串——由调用方去说"读不到"那句话）。"""
    try:
        head = path.open("rb").read(16)
    except OSError:
        return ""
    return container_of_bytes(head)


def _cache_dir(folder: str = "refaudio", *, create: bool = True) -> Path:
    """转换结果的缓存位置 ``<缓存根>/<folder>``：中间产物不进用户的素材库，
    也不在任何扫描根里，永远不会变成库里的一条音色。"""
    path = cache_base() / folder
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _source_digest(source: Path) -> str:
    """源文件摘要（路径 + 大小 + mtime 的 md5 短串），用来做**纯 ASCII** 的文件名。

    与 :func:`_fingerprint` 不同：那个返回元组、给 ``cut_point`` 当缓存键用；
    这个的产物要直接进文件名，所以必须是短的 ASCII 十六进制。
    """
    key = file_stamp(source) or (os.path.normcase(str(source)), 0, 0)
    return hashlib.md5(
        f"{key[0]}|{key[1]}:{key[2]}".encode(), usedforsecurity=False
    ).hexdigest()[:12]


def _target_for(
    source: Path, *, rate: int, channels: int, folder: str, limit: float | None = None
) -> Path:
    """这次写到哪个文件：源文件指纹 + 参数决定名字（源一变名字就变，不会读到旧缓存）。"""
    key = file_stamp(source) or (os.path.normcase(str(source)), 0, 0)
    digest = hashlib.md5(
        f"{key[0]}|{key[1]}:{key[2]}|{rate}|{channels}|{limit or ''}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return _cache_dir(folder) / f"{safe_segment(source.stem)}-{digest}.wav"


def _ascii_staging_dir() -> Path:
    """能装 ASCII 名副本的目录：优先缓存根，根不含 ASCII 就退到系统临时目录。

    缓存根默认是 ``<数据目录>/cache``；数据目录放在中文路径下是可能的，那时整条
    路径仍然是非 ASCII，复制过去等于没解决问题。
    """
    for candidate in (cache_base() / "refaudio-ascii", Path(tempfile.gettempdir()) / "voicehub-ref"):
        if str(candidate).isascii():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    # 两处都不是 ASCII（极罕见）：只能用临时目录，交给上层走报错那条路。
    fallback = Path(tempfile.gettempdir()) / "voicehub-ref"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _ready(path: Path) -> bool:
    """这份产物已经可用：文件在、且不只是一个 wav 头（44 字节）。"""
    try:
        return path.is_file() and path.stat().st_size > 44
    except OSError:
        return False


#: 每个目标文件一把锁（键是规范化后的路径）：同一个目标只让一个线程去转。
_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _serialized(target: Path) -> Iterator[None]:
    """按目标路径串行化。**用完就摘**：计数归零才删条目，正在等的那个不算漏。

    并发来自"同一段参考音频被很多行同时用"：批量合成的每一行、多引擎对比的每一次
    请求，都会拿着**同一个源文件**走进 :func:`to_wav`，而缓存还没生成时它们都判成
    "要转"。串行之后后来的那些在锁里重判一次，直接拿缓存，不多起一个 ffmpeg。
    """
    key = os.path.normcase(str(target))
    with _LOCKS_GUARD:
        lock, waiting = _LOCKS.get(key, (threading.Lock(), 0))
        _LOCKS[key] = (lock, waiting + 1)
    try:
        with lock:
            yield
    finally:
        with _LOCKS_GUARD:
            lock, waiting = _LOCKS.get(key, (lock, 1))
            if waiting <= 1:
                _LOCKS.pop(key, None)
            else:
                _LOCKS[key] = (lock, waiting - 1)


def _scratch_for(target: Path) -> Path:
    """中间文件名**每次都不同**。

    同名是这一类错的老来源：几个并发转换都写 ``X.part``，后起的 ``-y`` 会把先起的
    那份截掉（内容互相覆盖），改名时也只剩一个赢家——谁先改谁把别人的中间文件搬走，
    剩下的去改名只能失败。各写各的之后，"改名"才真的是原子提交。
    """
    tag = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:6]}"
    return target.with_name(f"{target.name}.{tag}.part")


def _publish(scratch: Path, target: Path, *, attempts: int = 6) -> None:
    """把中间文件改名成正式名字；撞上"文件被占"就退避重试。

    Windows 上改名要求源和目标都不被别的句柄攥着（引擎正在上传这段 wav、识别正在
    读它、杀软正在扫刚写完的新文件，都算）。占用通常是毫秒级的，退避几轮比让整次
    合成失败划算；真放不下才把错抛给调用方。
    """
    for attempt in range(attempts):
        try:
            os.replace(scratch, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


# --------------------------------------------------------------------------- #
# 缓存回收
# --------------------------------------------------------------------------- #

#: 派生产物所在的子目录（``refaudio-ascii`` 是 gsv.cpp 那份 ASCII 副本）。
#: 识别用的 16k wav 不在这里：那种转码是**一次性**的（``to_wav(transient=True)``）。
_CACHE_FOLDERS = ("refaudio", "refaudio-ascii")
#: 旧版把识别音频也缓存在这，现在不再写；清扫时整目录清掉，别让几百 MB 留成化石。
_LEGACY_FOLDERS = ("asr",)
#: 两次清扫之间至少隔这么久：清扫要逐个 stat，不该跟着每次转换走。
_SWEEP_INTERVAL = 600.0

_SWEEPS: dict[str, float] = {}
_SWEEP_LOCK = threading.Lock()


def _cache_files() -> list[tuple[float, int, Path]]:
    """可回收的缓存文件：``(mtime, 大小, 路径)``，跳过正在写的 ``.part``。

    用 ``scandir`` 而不是 ``iterdir`` + ``stat``：Windows 上目录枚举本身就带回了
    大小与时间，几千个文件能省掉几千次系统调用（实测 3700 份从 285ms 降到十几毫秒）。
    """
    found: list[tuple[float, int, Path]] = []
    for folder in _CACHE_FOLDERS:
        try:
            entries = list(os.scandir(_cache_dir(folder, create=False)))
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(".part"):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, Path(entry.path)))
    return found


def trim_cache(
    *, limit: int | None = None, max_age: float | None = None
) -> dict[str, int]:
    """按"最久没碰过"清缓存，返回 ``{"removed": 份数, "freed": 字节}``。

    :param limit: 体积上限（字节）；``None`` = 读设置里的「缓存上限」，``<=0`` = 不按体积清。
    :param max_age: 保留秒数；``None`` = 读设置里的「保留天数」，``<=0`` = 不按时间清。

    两条规则独立：超过 ``max_age`` 的一律删，其余按 mtime 从旧到新删到总量不超
    ``limit``。删不掉的不算错——正被引擎读、正被 ffmpeg 写的文件会报占用，跳过就是。
    """
    config = load_config()
    if limit is None:
        limit = config.cache_limit_mb * 1024 * 1024
    if max_age is None:
        max_age = config.cache_keep_days * 86400.0
    size_cap = limit if limit > 0 else None
    age_cap = max_age if max_age > 0 else None

    now = time.time()
    files = sorted(_cache_files())  # 按 mtime 升序：最旧的在前面
    total = sum(size for _, size, _ in files)
    removed = freed = 0
    for mtime, size, path in files:
        stale = age_cap is not None and now - mtime > age_cap
        over = size_cap is not None and total > size_cap
        if not stale and not over:
            break  # 后面只会更新、总量只会更小，不会再有该删的
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed += 1
        freed += size
        total -= size
    legacy = _purge_legacy()
    return {"removed": removed + legacy["removed"], "freed": freed + legacy["freed"]}


def _purge_legacy() -> dict[str, int]:
    """清掉不再写入的旧目录（识别音频缓存）：里面的东西永远不会再被复用。"""
    removed = freed = 0
    for folder in _LEGACY_FOLDERS:
        base = _cache_dir(folder, create=False)
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(".part"):
                continue
            try:
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
            freed += size
    return {"removed": removed, "freed": freed}


def _maybe_trim(folder: str) -> None:
    """写完一份新产物之后顺手清扫，按目录节流（每个目录十分钟最多一次）。

    清扫失败不影响这次转换：缓存是"能重建的东西"，清不掉只是白占一块盘。
    """
    now = time.monotonic()
    with _SWEEP_LOCK:
        last = _SWEEPS.get(folder)
        if last is not None and now - last < _SWEEP_INTERVAL:
            return
        _SWEEPS[folder] = now
    try:
        result = trim_cache()
    except OSError:
        return
    if result["removed"]:
        CHANNEL.info(
            f"缓存回收：清掉 {result['removed']} 份过期转换产物"
            f"（{result['freed'] / 1024 / 1024:.0f} MB）。",
            source="cache",
            folder=folder,
        )


def stage_ascii_path(source: Path) -> Path:
    """把一段音频放到**纯 ASCII 路径**下，返回新路径（内容不动，只复制）。

    有些 Windows 原生后端把请求里的路径转成本地代码页：gsv.cpp 的
    ``gsv_server.exe`` 拿到 ``…\\战场原中文#zh#….wav`` 会回 500
    ``No mapping for the Unicode character exists in the target multi-byte code page.``，
    同一个文件换成 ASCII 名就正常。官方 Python 版没有这条约束（按 UTF-8 读路径），
    所以这一步只在引擎显式声明 :attr:`SynthesisSpec.reference_path_ascii` 时才做。

    名字取源文件指纹：源一变名字就变，不会读到旧副本。**不转码**——需要转换时
    调用方已经先转过一道了，这里再转会把两件事混在一起。
    """
    suffix = source.suffix or ".wav"
    target = _ascii_staging_dir() / f"ref-{_source_digest(source)}{suffix}"
    try:
        same = target.is_file() and target.stat().st_size == source.stat().st_size
    except OSError:
        same = False
    if same:
        return target
    # 临时文件 + 改名：并发下不会读到半份文件（与 to_wav 同一套写法）。
    scratch = _scratch_for(target)
    try:
        shutil.copy2(source, scratch)
        _publish(scratch, target)
    finally:
        scratch.unlink(missing_ok=True)
    _maybe_trim("refaudio-ascii")
    return target


def _require_convertible(source: Path, configured: str | None) -> str:
    """转码前的两道门：源文件在、ffmpeg 在。返回 ffmpeg 路径，否则抛 :class:`FfmpegError`。"""
    # 先确认文件在：ffmpeg 对不存在的输入回 "Invalid argument"，会误导排查方向。
    if not source.is_file():
        raise FfmpegError(
            f"无法读取参考音频：{source}。文件可能已被移动、重命名或删除；"
            "该字段要求本机路径，盘符与目录必须一致。"
        )
    ffmpeg = find_ffmpeg(configured)
    if not ffmpeg:
        raise FfmpegError(
            f"该参考音频需要转换为 WAV（当前为 {source.suffix or '无后缀'}），"
            "但本机未找到 ffmpeg。请安装 ffmpeg 并加入 PATH；"
            "若设置中的「ffprobe 路径」已指向其安装目录，也可直接填写该路径。"
        )
    return ffmpeg


def _once_target(source: Path) -> Path:
    """一次性转码的落点：系统临时目录下**唯一**的文件名（不会被复用，也不参与回收）。

    临时目录而不是缓存目录：这种产物用完就该消失，混进缓存只会让回收逻辑多背一种
    寿命。文件名仍带源文件主干，排查日志时认得出是哪一段录音。
    """
    folder = Path(tempfile.gettempdir()) / "voicehub-tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FfmpegError(f"临时目录建不出来：{folder}（{exc}）。") from exc
    return folder / f"{safe_segment(source.stem)}-{uuid.uuid4().hex[:8]}.wav"


def to_wav(
    source: Path,
    *,
    configured: str | None = None,
    rate: int = REF_RATE,
    channels: int = 1,
    timeout: float = 300.0,
    folder: str = "refaudio",
    limit: float | None = None,
    transient: bool = False,
) -> Path:
    """把一段音频转成 wav（单声道 / 44.1kHz / pcm_s16le），返回新文件路径。

    默认**缓存复用**：已转过的直接返回；写入走"临时文件 + 改名"，并发下不会读到
    半份文件，同一个目标按路径串行化（见 :func:`_serialized`）。

    :param folder: 缓存子目录（参考音频用 ``refaudio``，ASCII 副本另走
        :func:`stage_ascii_path`）。``transient=True`` 时无意义。
    :param limit: 只保留开头这么多秒（``None`` = 整段）。裁与转一步做完，避免
        先转一遍再截一遍；秒数进缓存名，换个上限就是另一份文件。
    :param transient: 这次转出来的东西**只给这一次用**：落系统临时目录、不查缓存
        （每次都是一个新文件）、不参与回收。识别上传那一路用它——同一段音频不会再被
        第二次用到，留在缓存里只是白占盘。**删由调用方做**：函数返回路径时文件还要
        被读（上传），这里不能替调用方决定什么时候删。
    :raises FfmpegError: 没装 ffmpeg / 它报错——两种都带"该怎么改"。
    """
    ffmpeg = _require_convertible(source, configured)
    if transient:
        target = _once_target(source)
    else:
        target = _target_for(source, rate=rate, channels=channels, folder=folder, limit=limit)
        if _ready(target):
            return target

    # 同一个目标只让一个线程真去转：批量合成的每一行、多引擎对比的每一次请求，
    # 都会拿着同一个源文件同时走到这里（缓存还没生成时它们都判成"要转"）。
    wrote = False
    with _serialized(target):
        if not _ready(target):  # 排在前面的那个可能刚转完，那就不用再转
            _convert(
                source,
                target,
                ffmpeg=ffmpeg,
                rate=rate,
                channels=channels,
                limit=limit,
                timeout=timeout,
            )
            wrote = True
    if wrote and not transient:
        # 放在锁外：清扫是顺手的事，不该拖住等这把锁的其它请求。
        _maybe_trim(folder)
    return target


def _convert(
    source: Path,
    target: Path,
    *,
    ffmpeg: str,
    rate: int,
    channels: int,
    limit: float | None,
    timeout: float,
) -> None:
    """真起一次 ffmpeg 写 ``target``：中间文件各写各的，写完改名。

    调用方已确认 ``target`` 还不存在、并且拿着 :func:`_serialized` 那把锁。
    """
    scratch = _scratch_for(target)
    command = [
        ffmpeg,
        "-hide_banner",
        # 不碰标准输入：Windows 上会卡住等待按键，请求随之挂起。
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        # 排除视频流：带封面的 mp3 有一个"视频"流，不排除时 ffmpeg 会拿它当输出候选。
        "-vn",
    ]
    if limit and limit > 0:
        # ``-t`` 放在输入之后 = 只输出这么多秒（放前面会变成"读多久"，
        # 对有些容器会连同时间基一起截断）。
        command += ["-t", f"{limit:.3f}"]
    command += [
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        # 输出走 .part 临时文件，ffmpeg 靠后缀猜不出封装，必须明写 -f wav。
        "-f",
        "wav",
        str(scratch),
    ]
    try:
        try:
            done = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise FfmpegError(
                f"无法启动 ffmpeg（{exc}）：文件可能已删除或缺少执行权限（{ffmpeg}）。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(
                f"ffmpeg 转换超时（{timeout:.0f} 秒），已终止：{source.name}。"
                "录音可能过长或已损坏。"
            ) from exc

        if done.returncode != 0 or not _ready(scratch):
            detail = " ".join((done.stderr or "").split())[-400:] or "原因未知"
            raise FfmpegError(
                f"ffmpeg 无法转换参考音频：{source.name}。错误信息：{detail}"
            )
        try:
            _publish(scratch, target)
        except PermissionError as exc:
            raise FfmpegError(
                f"参考音频已转好，但写入缓存失败：{target.name} 正被其他程序占用"
                f"（{exc.strerror or exc}）。关掉正在读它的程序后重试。"
            ) from exc
    finally:
        # 失败时别留下垃圾：中间文件名每次都是新的，留一份就永远不会被复用。
        scratch.unlink(missing_ok=True)



# --------------------------------------------------------------------------- #
# 句读断点
# --------------------------------------------------------------------------- #

#: 句读判据：比这更静就算"停顿"。语音底噪一般在 -50dB 以下，-35dB 能把
#: 句与句之间的换气同字与字之间的过渡分开，又不至于把轻声的句尾当成静音。
_CUT_NOISE_DB = -35
#: 短于这个长度的静音不算停顿（那是辅音闭合，切在那里会切断字）。
_CUT_MIN_SILENCE = 0.15

#: 进程内备忘：``(路径, 大小, mtime, low, high) → (切点, 是否落在停顿上)``。
#: 同一条样例会被同一音色的每一行反复用到，不该每行都解码一遍整段录音。
_CUT_MEMO: dict[tuple[str, int, int, float, float], tuple[float, bool]] = {}
_CUT_MEMO_MAX = 512


def _fingerprint(source: Path) -> tuple[str, int, int]:
    """源文件一变就是另一个键，缓存不会张冠李戴（判据只有 :func:`file_stamp` 一份）。"""
    return file_stamp(source) or (os.path.normcase(str(source)), 0, 0)


def _spans_with(
    source: Path, ffmpeg: str, *, noise_db: float, min_silence: float, timeout: float
) -> list[tuple[float, float]]:
    """扫一遍音量，返回静音区间 ``[(起, 止), ...]``（秒）。

    ``silencedetect`` 只有人看的日志、没有机器可读的输出，所以只能解析 stderr；
    解析不出来就当"没有停顿"，由调用方退回硬切——**不猜**。
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(source),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        # 只分析不解码成文件：null 复用器丢掉全部输出。
        "-f",
        "null",
        "-",
    ]
    try:
        done = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    log = done.stderr or ""
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*(-?[\d.]+)", log)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*(-?[\d.]+)", log)]
    # 末尾还在静音时只有 start 没有 end，配不上就丢掉——那不是"一句话说完"。
    return list(zip(starts, ends, strict=False))


def silence_spans(
    source: Path,
    *,
    noise_db: float = _CUT_NOISE_DB,
    min_silence: float = _CUT_MIN_SILENCE,
    configured: str | None = None,
    timeout: float = 1800.0,
) -> list[tuple[float, float]]:
    """整段音频的静音区间（秒）。切段识别用它，阈值比句读断点宽松：

    台词与台词之间的停顿通常 0.3 秒以上，而句子内部的换气只有 0.1–0.2 秒。
    阈值走参数而不是复用常量——切得太碎会让字幕一行只有两三个字。
    找不到 ffmpeg 或文件不可读时返回空表，调用方退回整段识别。
    """
    if not source.is_file():
        return []
    ffmpeg = find_ffmpeg(configured)
    if not ffmpeg:
        return []
    return _spans_with(
        source, ffmpeg, noise_db=noise_db, min_silence=min_silence, timeout=timeout
    )


def _silence_spans(
    source: Path, ffmpeg: str, *, timeout: float
) -> list[tuple[float, float]]:
    """句读断点用的窄阈值静音区间（见 :func:`silence_spans`）。"""
    return _spans_with(
        source,
        ffmpeg,
        noise_db=_CUT_NOISE_DB,
        min_silence=_CUT_MIN_SILENCE,
        timeout=timeout,
    )


def cut_point(
    source: Path,
    *,
    low: float,
    high: float,
    configured: str | None = None,
    timeout: float = 120.0,
) -> tuple[float, bool]:
    """在 ``(low, high]`` 里挑一个"一句话说完"的位置，返回 ``(秒, 是否落在停顿上)``。

    从上限往**回退**着找：先看最靠后的停顿，没有就往前，退到下限（通常是 3 秒）为止。
    截得越少越接近整段录音，但保住句子完整优先于保住长度。
    找不到任何停顿就返回 ``(high, False)``——宁可硬切，也不为一条样例把整段合成拒掉。

    :param low: 下限（秒）。切点不会早于它，保证截出来的音频够长。
    :param high: 上限（秒）。通常由引擎的时长上限减一点余量得到。
    """
    key = (*_fingerprint(source), float(low), float(high))
    hit = _CUT_MEMO.get(key)
    if hit is not None:
        return hit

    fallback = (float(high), False)
    ffmpeg = find_ffmpeg(configured)
    if not ffmpeg:
        return fallback

    best = 0.0
    for start, end in _silence_spans(source, ffmpeg, timeout=timeout):
        if end <= low or start >= high:
            continue
        # 把静音段裁进 [low, high] 再取中点：跨界的静音段也能给出一个合法切点。
        middle = (max(start, low) + min(end, high)) / 2.0
        if low < middle <= high:
            best = max(best, middle)

    result = (round(best, 2), True) if best > 0 else fallback
    if len(_CUT_MEMO) >= _CUT_MEMO_MAX:
        _CUT_MEMO.clear()  # 只防"扫过几千条样例"这一种情况
    _CUT_MEMO[key] = result
    return result


# --------------------------------------------------------------------------- #
# 媒体搬运：抽音轨、按时间段切、封装成片
#
# 三件事都不经过模型：解复用与复用是确定性操作，失败信息要让用户能自己修
# （装了 ffmpeg 没有、源文件还在不在、目标文件是不是被播放器占着）。
# --------------------------------------------------------------------------- #

#: 配音中间件统一采样率：人声与混音都用这一档，避免一个项目里转码五六次。
MEDIA_RATE = 44100
#: 识别副本的采样率：16 kHz 单声道是各家识别模型的标准输入。
ASR_RATE = 16000


def _run_ffmpeg(
    command: list[str], *, timeout: float, what: str
) -> None:
    """跑一条 ffmpeg 命令；返回码非零时把 stderr 末尾一句带出来。

    不做"临时文件 + 改名"：调用方给的目标就是临时路径或独占产物，
    写坏了下一次重跑即可（产物名带指纹，不会读到半份文件）。
    """
    try:
        done = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise FfmpegError(f"无法启动 ffmpeg（{exc}）：{what}。") from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg 超时（{timeout:.0f} 秒）：{what}。") from exc
    if done.returncode != 0:
        detail = " ".join((done.stderr or "").split())[-400:] or "原因未知"
        raise FfmpegError(f"{what}失败：{detail}")


def _require_source(source: Path, configured: str | None, what: str) -> str:
    if not source.is_file():
        raise FfmpegError(f"{what}：源文件不存在（{source}）。")
    ffmpeg = find_ffmpeg(configured)
    if not ffmpeg:
        raise FfmpegError(
            f"{what}需要 ffmpeg：本机未找到。请安装并加入 PATH，"
            "或在设置里把「ffprobe 路径」指向它的安装目录。"
        )
    return ffmpeg


def extract_audio(
    source: Path,
    target: Path,
    *,
    stream: int = 0,
    rate: int = MEDIA_RATE,
    channels: int = 2,
    configured: str | None = None,
    timeout: float = 3600.0,
) -> Path:
    """把容器里的第 ``stream`` 条音频流取出来写成 wav（解复用，不分离声源）。

    视频文件、多音轨容器都走这一条：``-map 0:a:<stream>`` 选流，``-vn`` 丢画面。
    重编码而不 ``-c copy``：源可能是 aac / opus / ac3，中间件要统一成 PCM，
    否则后续按样本切段的边界会落在解码器的填充上。
    """
    ffmpeg = _require_source(source, configured, "抽音轨")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        f"0:a:{max(0, int(stream))}",
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(target),
    ]
    _run_ffmpeg(command, timeout=timeout, what=f"抽音轨（{source.name}）")
    return target


def slice_audio(
    source: Path,
    target: Path,
    *,
    start: float,
    end: float | None = None,
    rate: int = ASR_RATE,
    channels: int = 1,
    configured: str | None = None,
    timeout: float = 600.0,
) -> Path:
    """截取 ``[start, end)`` 写成 wav。

    ``-ss`` 放在 ``-i`` 之前：解码从关键位置开始，长音频上不必逐帧丢掉前半小时。
    输出重编码，因此时间点仍是精确的（不是按帧对齐的复制）。
    """
    ffmpeg = _require_source(source, configured, "切段")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, float(start)):.3f}",
        "-i",
        str(source),
    ]
    if end is not None and end > start:
        command += ["-t", f"{float(end) - float(start):.3f}"]
    command += [
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(target),
    ]
    _run_ffmpeg(command, timeout=timeout, what=f"切段（{source.name}）")
    return target


def mux_media(
    video: Path,
    audio: Path,
    target: Path,
    *,
    subtitles: Path | None = None,
    subtitle_codec: str = "mov_text",
    copy_video: bool = True,
    configured: str | None = None,
    timeout: float = 3600.0,
) -> Path:
    """把新音轨（可选字幕）封回原视频，画面不重编码。

    **不加 ``-shortest``**：配音轨通常比画面短（尾部没有台词就没有声音），
    按最短流截断会把后面的画面一起丢掉。音轨先结束、画面继续，这是正常成片。
    """
    ffmpeg = _require_source(video, configured, "封装成片")
    if not audio.is_file():
        raise FfmpegError(f"封装成片：新音轨不存在（{audio}）。")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio),
    ]
    if subtitles is not None:
        if not subtitles.is_file():
            raise FfmpegError(f"封装成片：字幕文件不存在（{subtitles}）。")
        command += ["-i", str(subtitles)]
    command += ["-map", "0:v:0", "-map", "1:a:0"]
    if subtitles is not None:
        command += ["-map", "2:0"]
    command += ["-c:v", "copy" if copy_video else "libx264"]
    command += ["-c:a", "aac", "-b:a", "192k"]
    if subtitles is not None:
        command += ["-c:s", subtitle_codec]
        if subtitle_codec == "mov_text":
            # mp4 的字幕轨要声明语言，否则播放器列表里是一行空白。
            command += ["-metadata:s:s:0", "language=chi"]
    command.append(str(target))
    _run_ffmpeg(command, timeout=timeout, what=f"封装成片（{video.name}）")
    return target


#: 成品容器 → 编码器；码率只对有损容器生效。
_ENCODERS = {
    "mp3": "libmp3lame",
    "m4a": "aac",
    "wav": "pcm_s16le",
    "flac": "flac",
    "aac": "aac",
    "ogg": "libvorbis",
    "opus": "libopus",
}


def encode_audio(
    source: Path,
    target: Path,
    *,
    rate: int = MEDIA_RATE,
    channels: int = 2,
    bitrate: str = "192k",
    configured: str | None = None,
    timeout: float = 3600.0,
) -> Path:
    """把混好的 wav 编成成品容器（后缀决定编码器）。

    容器由目标文件名决定，调用方不必先问"这个格式用什么编码器"——
    可写容器之外的直接报错，而不是悄悄出个坏文件。``rate`` / ``channels`` 传 ``0``
    表示沿用源流的参数（替换样例时用，不为导出规格改声道数）。
    """
    ffmpeg = _require_source(source, configured, "导出音频")
    suffix = target.suffix.lower().lstrip(".")
    encoder = _ENCODERS.get(suffix)
    if encoder is None:
        raise FfmpegError(
            f"不支持导出成 .{suffix}：可选 mp3 / m4a / wav / flac / aac / ogg / opus。"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",
    ]
    if channels:
        command += ["-ac", str(channels)]
    if rate:
        command += ["-ar", str(rate)]
    command += ["-c:a", encoder]
    if encoder != "pcm_s16le" and bitrate:
        command += ["-b:a", bitrate]
    command.append(str(target))
    _run_ffmpeg(command, timeout=timeout, what=f"导出音频（{target.name}）")
    return target


def normalize(
    source: Path,
    target: Path,
    *,
    rate: int = 0,
    channels: int = 1,
    highpass: int = 0,
    peak: float = 0.891,
    start: float = 0.0,
    duration: float = 0.0,
    segments: list[tuple[float, float]] | None = None,
    configured: str | None = None,
    timeout: float = 900.0,
) -> Path:
    """把一段音频折成"可比较、可再喂给模型"的 WAV。

    只做确定性的事：声道、采样率、低频切除、限峰、可选的区间截取。**不做响度归一**——
    把电平拉平会抹掉两条素材之间的音量差，试听原声与结果时听到的就不再是处理本身。

    :param rate: 目标采样率；``0`` = 保持源采样率（升采样只会让文件变大）。
    :param peak: 限峰阈值（线性满刻度）；``0`` = 不限。默认 0.891 ≈ −1 dBFS。
    :param highpass: 高通截止频率（Hz）；``0`` = 不切。口播素材 60 Hz 足够。
    :param start: 从源的第几秒开始；输出定位放在 ``-i`` 之后，按解码时间精确切。
    :param duration: 截取多长（秒）；``0`` = 到结尾。
    :param segments: 要保留的若干区间 ``[(start, end), ...]``；给两段以上时按序拼接。
        单段等价于 ``start`` / ``duration``，空表与 ``None`` 表示整段。
    """
    keeps = [(float(a), float(b)) for a, b in (segments or []) if b > a]
    if len(keeps) == 1:
        start, duration = keeps[0][0], keeps[0][1] - keeps[0][0]
    if len(keeps) > 1:
        return _normalize_segments(
            source,
            target,
            keeps,
            rate=rate,
            channels=channels,
            highpass=highpass,
            peak=peak,
            configured=configured,
            timeout=timeout,
        )
    ffmpeg = _require_source(source, configured, "处理音频")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
    ]
    # 输出定位（-ss 放在 -i 之后）按解码时间切，不受关键帧影响；素材都很短，
    # 多解一段的代价可以忽略。裁剪与收尾是同一次解码，不生成中间文件。
    if start > 0:
        command += ["-ss", f"{float(start):.3f}"]
    if duration > 0:
        command += ["-t", f"{float(duration):.3f}"]
    command += [
        "-vn",
        "-ac",
        str(channels),
    ]
    if rate:
        command += ["-ar", str(rate)]
    filters: list[str] = []
    if highpass:
        filters.append(f"highpass=f={int(highpass)}")
    if peak:
        filters.append(f"alimiter=limit={peak:.3f}:level=false")
    if filters:
        command += ["-af", ",".join(filters)]
    command += ["-c:a", "pcm_s16le", str(target)]
    _run_ffmpeg(command, timeout=timeout, what=f"处理音频（{target.name}）")
    return target


def _normalize_segments(
    source: Path,
    target: Path,
    keeps: list[tuple[float, float]],
    *,
    rate: int,
    channels: int,
    highpass: int,
    peak: float,
    configured: str | None,
    timeout: float,
) -> Path:
    """多段保留：每段先切出来，按序接上，再过与单段相同的收尾滤镜。

    段与段之间不补静音、不做交叉淡化：这是用户明确删掉中间内容的结果，
    补任何东西都会改变他指定的保留范围。
    """
    ffmpeg = _require_source(source, configured, "处理音频")
    target.parent.mkdir(parents=True, exist_ok=True)
    chains: list[str] = []
    pieces: list[str] = []
    for index, (begin, end) in enumerate(keeps):
        chains.append(
            f"[0:a]atrim=start={begin:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS[seg{index}]"
        )
        pieces.append(f"[seg{index}]")
    chains.append(f"{''.join(pieces)}concat=n={len(pieces)}:v=0:a=1[joined]")
    filters: list[str] = []
    if highpass:
        filters.append(f"highpass=f={int(highpass)}")
    if peak:
        filters.append(f"alimiter=limit={peak:.3f}:level=false")
    chains.append(f"[joined]{','.join(filters) if filters else 'anull'}[out]")
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[out]",
        "-vn",
        "-ac",
        str(channels),
    ]
    if rate:
        command += ["-ar", str(rate)]
    command += ["-c:a", "pcm_s16le", str(target)]
    _run_ffmpeg(command, timeout=timeout, what=f"处理音频（{target.name}）")
    return target
