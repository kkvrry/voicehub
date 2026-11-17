"""头像编排：**找图、裁成方形头像、落盘、重扫**，显示交给现有头像设置。

图片来自公开的角色 Wiki、萌娘百科、AniList 与 Bing 图片搜索；不再调用生图模型。
来源只返回候选 URL，下载与裁剪在这一层，写出的永远是宿主自己的 ``avatar.png``；
下载到的整张原图一并留在音色目录里（``avatar-source.*``），方便回看或手动重裁。

单条音色走「先看候选再落盘」：:func:`candidates` 把各来源裁好的图连同出处交给界面，
用户挑了以后 :func:`apply_candidate` 才写盘——自动匹配认错人时不用覆盖掉现有头像。
一键配头像（:func:`generate`）自动取最优的一张，但**只补空缺**：目录里已经有图就跳过，
批量补齐不会盖掉现有的封面。

查找顺序按分类决定（见 :func:`app.infra.avatar_sources.plan`）：三国杀/原神/明日方舟
先查对应 Wiki，真人 / 主播先走 Bing，其它角色先查萌娘百科与 AniList。任何一个
来源被站点拦截都只影响它自己，后面的来源照常跑。
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import cv2
import httpx
import numpy as np

from app.engines.base import EngineError
from app.infra import avatar_sources
from app.infra.avatar_sources import Candidate, SourceError
from app.infra.loghub import CHANNEL
from app.infra.netenv import normalize_no_proxy_env
from app.infra.walker import COVER_NAMES
from app.usecase.library import LibraryError, LibraryService

__all__ = ["AvatarError", "apply_candidate", "candidates", "generate", "status"]

#: 宿主写出的文件名（扫描器优先认它，其次才是 cover.*）。
_AVATAR_NAMES = ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp")
#: 下载到的原图另存为这个前缀；扫描器不认它，不会顶掉 cover.*。
_SOURCE_NAMES = tuple(
    f"avatar-source{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")
)
#: 覆盖前留一张上一版：认错人时把 avatar-prev.* 改回 avatar.png 就回退了。
_PREV_NAMES = tuple(f"avatar-prev{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp"))
_AVATAR_SIZE = 768
_MIN_SOURCE = 96
_MAX_DOWNLOAD = 12 * 1024 * 1024
_MAX_CANDIDATES = 6
#: 一张图找完就收工：分辨率够高又检出了脸的，不值得再下下一张。
_GOOD_SIDE = 600
#: 整次查找（含所有来源）的墙钟预算。源站慢的时候宁可拿手里最好的，
#: 也不让详情面板的按钮转上好几分钟。
_FIND_BUDGET = 30.0
#: 选图弹层一次最多摆几张、每个来源最多几张（来源多，摆多了反而挑花眼）。
_MAX_PICK = 8
_PER_SOURCE_PICK = 3
#: 候选只在内存里活十分钟：用户看完选一张就走，不值得落盘。
_PICK_TTL = 600.0

#: 正面 + 侧面两张级联：正脸用 alt2（比 default 少误报），侧脸再镜像跑一遍。
_CASCADES: tuple[tuple[cv2.CascadeClassifier, float], ...] = (
    (cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"), 1.0),
    (cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml"), 0.85),
)


class AvatarError(EngineError):
    """头像查找/下载/裁剪失败。"""

    def __init__(self, reason: str, *, code: str = "avatar_failed", retryable: bool = False) -> None:
        super().__init__(reason, retryable=retryable, code=code)


@dataclass(frozen=True, slots=True)
class _Found:
    """一张裁好的候选。``original`` 只有自动落盘时带在手上；
    候选列表不保留原图（一张可能十几 MB），选中后再按 URL 下一次。"""

    rendered: _Rendered
    candidate: Candidate
    ext: str
    original: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Pending:
    expires: float
    voice_id: str
    name: str
    found: _Found


_PENDING: dict[str, _Pending] = {}


def status() -> dict[str, object]:
    """头像来源清单。**不探网络**，设置页每次打开都问得起。"""
    return {
        "ready": True,
        "note": "",
        "sources": avatar_sources.source_rows(),
    }


# --------------------------------------------------------------------------- #
# 找图
# --------------------------------------------------------------------------- #


def _ext_for(data: bytes, content_type: str = "") -> str:
    """原图的落盘扩展名：先看文件头，再退回响应类型。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    kind = (content_type or "").split(";")[0].strip().lower()
    known = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    }
    return known.get(kind, ".jpg")


async def _download(
    client: httpx.AsyncClient, candidate: Candidate
) -> tuple[bytes, str] | None:
    """下载一张候选图。返回 ``(原图字节, 扩展名)``，裁切与保存都交给调用方。"""
    headers = {"Accept": "image/avif,image/webp,image/png,image/jpeg,*/*"}
    if candidate.referer:
        headers["Referer"] = candidate.referer
    try:
        async with client.stream("GET", candidate.url, headers=headers) as response:
            if response.status_code >= 400:
                return None
            kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if kind and not (
                kind.startswith("image/") or kind in ("application/octet-stream", "binary/octet-stream")
            ):
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_DOWNLOAD:
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
            return (data, _ext_for(data, kind)) if data else None
    except (httpx.HTTPError, ValueError):
        return None


def _square(image: np.ndarray, left: int, top: int, side: int) -> np.ndarray:
    height, width = image.shape[:2]
    side = max(32, min(side, width, height))
    left = max(0, min(left, width - side))
    top = max(0, min(top, height - side))
    crop = image[top : top + side, left : left + side]
    if side == _AVATAR_SIZE:
        return crop
    interpolation = cv2.INTER_AREA if side > _AVATAR_SIZE else cv2.INTER_CUBIC
    return cv2.resize(crop, (_AVATAR_SIZE, _AVATAR_SIZE), interpolation=interpolation)


def _skin_mask(image: np.ndarray) -> np.ndarray:
    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    return cv2.inRange(ycrcb, np.array([0, 133, 77]), np.array([255, 173, 127]))


def _face_box(
    image: np.ndarray, mask: np.ndarray, *, min_skin: float = 0.35
) -> tuple[int, int, int, int] | None:
    """在图片上半部找一张可信的人脸。

    Haar 在立绘上误报不少（腿甲、灯笼都能被当成脸），所以同时要求：位于上半部、
    框内肤色占比够高、框的大小占短边至少一成；多个候选按肤色、靠上程度和大小综合打分。
    只框住眼睛的小框（<10% 短边）一律不认——放大后就是「只有一双眼睛」。
    实拍照片的肤色常被调色压淡，``min_skin`` 由调用方放宽。
    """
    height, width = image.shape[:2]
    short = min(width, height)
    gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    mirrored = cv2.flip(gray, 1)
    min_side = max(24, short // 16)
    min_face = max(28, int(short * 0.10))
    best: tuple[float, tuple[int, int, int, int]] | None = None
    for cascade, weight in _CASCADES:
        if cascade.empty():
            continue
        # 侧面级联只认一个朝向，镜像再跑一遍才覆盖另一边脸。
        passes = ((gray, weight), (mirrored, weight * 0.9)) if weight < 1 else ((gray, weight),)
        for canvas, pass_weight in passes:
            for x, y, box_w, box_h in cascade.detectMultiScale(
                canvas, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side)
            ):
                if canvas is mirrored:
                    x = width - int(x) - int(box_w)
                if min(box_w, box_h) < min_face or max(box_w, box_h) > short * 0.95:
                    continue
                relative_y = (y + box_h / 2) / height
                if relative_y > 0.62:
                    continue
                skin = mask[y : y + box_h, x : x + box_w]
                ratio = float(skin.mean()) / 255 if skin.size else 0.0
                if ratio < min_skin:
                    continue
                score = ratio * (1 - relative_y) * (box_w * box_h) ** 0.5 * pass_weight
                if best is None or score > best[0]:
                    best = (score, (int(x), int(y), int(box_w), int(box_h)))
    return best[1] if best else None


class _Rendered(NamedTuple):
    """一张候选图裁完之后的样子，附带挑图用的画质分。"""

    png: bytes
    has_face: bool
    short: int
    score: float
    ready: bool


def _quality(width: int, height: int, side: int, has_face: bool) -> float:
    """候选图打分：分辨率、能裁多大、有没有脸；离谱长宽比与超小图扣分。"""
    short = min(width, height)
    score = (min(short, 1024) / 1024) * (min(side, 1024) / 1024)
    if has_face:
        score *= 1.6
    if short < 240:
        score *= 0.6
    aspect = width / height if height else 1.0
    if aspect < 0.55 or aspect > 1.8:
        score *= 0.8
    return score


def _render(data: bytes, *, photo: bool = False) -> _Rendered | None:
    """解码、裁成方形、缩到 768，并给这张图打一个画质分。"""
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    if image.dtype == np.uint16:
        image = (image >> 8).astype(np.uint8)
    elif image.dtype != np.uint8:
        return None
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.shape[2] == 2:
        image = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    elif image.shape[2] == 4:
        # 透明立绘铺白底，避免在浅色界面里变成一块黑。
        alpha = image[:, :, 3:4].astype(np.float32) / 255
        image = (image[:, :, :3].astype(np.float32) * alpha + 255 * (1 - alpha)).astype(np.uint8)
    elif image.shape[2] != 3:
        return None
    height, width = image.shape[:2]
    short = min(height, width)
    if short < _MIN_SOURCE:
        return None

    # Wiki 的头像图标本身已经是方形头肩照，再裁只会切掉半张脸。
    if short <= 256 and 0.75 <= width / height <= 1.34:
        square = _square(image, 0, 0, short)
        ok, buffer = cv2.imencode(".png", square, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
        if not ok:
            return None
        return _Rendered(buffer.tobytes(), False, short, _quality(width, height, short, False), True)

    face = _face_box(image, _skin_mask(image), min_skin=0.2 if photo else 0.35)
    if face is not None:
        x, y, box_w, box_h = face
        # 至少半个短边：立绘上常只框到眉眼，2.2 倍放大后就是一张「眼睛特写」。
        side = max(int(max(box_w, box_h) * 2.2), int(short * 0.5))
        side = min(side, width, height)
        left = int(x + box_w / 2 - side / 2)
        top = int(y - side * 0.2)
        square = _square(image, left, top, side)
        has_face = True
    else:
        # 没有可信人脸（动漫特写很常见）。横图取整幅高度（取 55% 高度会只剩一双眼睛）；
        # 竖图取六成高度，保住头和肩，不至于把整条腿都塞进头像。
        side = height if width >= height else min(width, int(height * 0.6))
        square = _square(image, (width - side) // 2, 0, side)
        has_face = False

    ok, buffer = cv2.imencode(".png", square, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
    if not ok:
        return None
    return _Rendered(
        buffer.tobytes(), has_face, short, _quality(width, height, side, has_face), False
    )


def _looks_done(rendered: _Rendered, candidate: Candidate) -> bool:
    """百科的现成方形头像，或高分辨率且确实有脸：这一张已经够好，不用再试。
    照片来源的小方图多半只是缩略图，继续往下找大图。"""
    if rendered.ready and not candidate.photo:
        return True
    return rendered.has_face and rendered.short >= _GOOD_SIDE


async def _source_found(
    client: httpx.AsyncClient,
    voice,
    key: str,
    name: str,
    category: str,
    deadline: float,
    *,
    folder: str = "",
    keep_original: bool = False,
    keep: int = _PER_SOURCE_PICK,
    eager: bool = True,
) -> list[_Found]:
    """跑一个来源，下载并裁好最多 ``_MAX_CANDIDATES`` 张，按画质分取前 ``keep`` 张。

    ``folder`` 只参与作品判定（见 :func:`avatar_sources.series`），不参与检索逻辑。
    """
    try:
        candidates = await avatar_sources.search(key, client, name, category, folder)
    except SourceError as exc:
        CHANNEL.info(f"头像来源 {key} 跳过：{exc}", source="avatar", voice=voice.id)
        raise
    found: list[_Found] = []
    for candidate in candidates[:_MAX_CANDIDATES]:
        if time.monotonic() > deadline:
            break
        downloaded = await _download(client, candidate)
        if not downloaded:
            continue
        data, ext = downloaded
        rendered = await asyncio.to_thread(_render, data, photo=candidate.photo)
        if rendered is None:
            continue
        found.append(_Found(rendered, candidate, ext, data if keep_original else None))
        # 选图模式不提前收工：第一张够好也继续摆几张，用户要的是「是不是本人」。
        if eager and _looks_done(rendered, candidate):
            break
    found.sort(key=lambda item: item.rendered.score, reverse=True)
    return found[:keep]


def _not_found(name: str, failures: list[str]) -> AvatarError:
    detail = f"（{failures[0]}）" if failures else ""
    return AvatarError(
        f"没有找到「{name}」的可用头像{detail}。可以补充分类重试，或在该音色目录放一张 cover.png。",
        code="avatar_not_found",
        retryable=bool(failures),
    )


async def _find_match(client: httpx.AsyncClient, voice) -> _Found:
    """自动模式：按来源顺序，第一个有结果的来源里挑画质最高的一张。"""
    name = (voice.shown_name or voice.name or "").strip()
    category = voice.category or ""
    # 平台音色没有目录；认作品要用到它（分类名可能是「普通话」这种内容分组）。
    folder = getattr(voice, "folder", "") or ""
    failures: list[str] = []
    deadline = time.monotonic() + _FIND_BUDGET

    for key in avatar_sources.plan(category, folder):
        if time.monotonic() > deadline:
            failures.append("找图超时，先用已经拿到的结果")
            break
        try:
            found = await _source_found(
                client, voice, key, name, category, deadline,
                folder=folder, keep_original=True, keep=1,
            )
        except SourceError as exc:
            failures.append(str(exc))
            continue
        except httpx.HTTPError as exc:
            failures.append(f"{key} 网络请求失败：{exc}")
            CHANNEL.info(f"头像来源 {key} 网络失败：{exc}", source="avatar", voice=voice.id)
            continue

        # 自动模式取当前来源里最好的一张；原图已经在手上，落盘不用再下一次。
        if found:
            return found[0]

    raise _not_found(name, failures)


async def _collect(client: httpx.AsyncClient, voice) -> list[_Found]:
    """选图模式：把计划里每个来源的前几张都收集起来，跨来源去重后交给界面。"""
    name = (voice.shown_name or voice.name or "").strip()
    category = voice.category or ""
    folder = getattr(voice, "folder", "") or ""
    deadline = time.monotonic() + _FIND_BUDGET
    collected: list[_Found] = []
    seen: set[str] = set()

    for key in avatar_sources.plan(category, folder):
        if len(collected) >= _MAX_PICK or time.monotonic() > deadline:
            break
        try:
            found = await _source_found(
                client, voice, key, name, category, deadline, folder=folder, eager=False
            )
        except SourceError as exc:
            CHANNEL.info(f"头像来源 {key} 跳过：{exc}", source="avatar", voice=voice.id)
            continue
        except httpx.HTTPError as exc:
            CHANNEL.info(f"头像来源 {key} 网络失败：{exc}", source="avatar", voice=voice.id)
            continue
        for item in found:
            if item.candidate.url in seen:
                continue
            seen.add(item.candidate.url)
            collected.append(item)
            if len(collected) >= _MAX_PICK:
                break

    if not collected:
        raise _not_found(name, [])
    return collected


def _preview(png: bytes) -> str:
    """候选缩略图（data URL）：界面只用来挑图，不落盘。"""
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return ""
    thumb = cv2.resize(image, (176, 176), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()


def _prune_pending() -> None:
    now = time.monotonic()
    for token, pending in list(_PENDING.items()):
        if pending.expires <= now:
            _PENDING.pop(token, None)


def _remember(voice_id: str, name: str, found: list[_Found]) -> list[dict[str, object]]:
    """把这一批候选存进内存，返回界面用的行（缩略图 + 出处 + token）。"""
    _prune_pending()
    for token, pending in list(_PENDING.items()):
        if pending.voice_id == voice_id:
            _PENDING.pop(token, None)
    rows: list[dict[str, object]] = []
    for item in found:
        token = uuid.uuid4().hex
        _PENDING[token] = _Pending(time.monotonic() + _PICK_TTL, voice_id, name, item)
        rows.append(
            {
                "token": token,
                "source": item.candidate.source,
                "page": item.candidate.page,
                "preview": _preview(item.rendered.png),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 落盘 / 重扫
# --------------------------------------------------------------------------- #


def _write_bytes(folder: Path, name: str, data: bytes) -> Path:
    target = folder / name
    temp = folder / f".{target.stem}-{uuid.uuid4().hex}.tmp"
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def _write_avatar(folder: Path, data: bytes) -> Path:
    if not data:
        raise AvatarError("没有拿到图片数据。", code="avatar_empty")
    # 覆盖前留一张上一版：认错人时把 avatar-prev.* 改回 avatar.png 就是回退。
    for name in _AVATAR_NAMES:
        previous = folder / name
        if previous.is_file():
            kept = _write_bytes(folder, f"avatar-prev{previous.suffix}", previous.read_bytes())
            for other in _PREV_NAMES:
                if other != kept.name:
                    (folder / other).unlink(missing_ok=True)
            break
    target = _write_bytes(folder, "avatar.png", data)

    # 其他扩展名的旧头像不再生效，顺手清掉；目标文件在 replace 之后才动旧的。
    for name in _AVATAR_NAMES:
        path = folder / name
        if path != target:
            path.unlink(missing_ok=True)
    return target


def _write_source(folder: Path, data: bytes, ext: str) -> Path:
    """把下载到的整张原图留在音色目录；换图时清掉旧扩展名的那份。"""
    target = _write_bytes(folder, f"avatar-source{ext}", data)
    for name in _SOURCE_NAMES:
        path = folder / name
        if path != target:
            path.unlink(missing_ok=True)
    return target


async def _rescan(service: LibraryService) -> None:
    """写盘后重扫一次，让列表与详情看到新的封面。

    同时给两张音色找头像时扫描是互斥的：撞上就等一会儿再重试，
    否则第二张的新头像要等下一次扫描才看得到。文件都已落盘，失败也不判查找失败。
    """
    for attempt in range(4):
        try:
            await asyncio.to_thread(service.scan)
            return
        except LibraryError as exc:
            if attempt == 3:
                CHANNEL.warning(f"头像已写入，但重扫未完成：{exc}", source="avatar")
                return
            await asyncio.sleep(0.7)
        except Exception as exc:  # noqa: BLE001 - 重扫失败不该吞掉已经找好的头像
            CHANNEL.warning(f"头像已写入，但重扫失败：{exc}", source="avatar")
            return


def _local_voice(service: LibraryService, voice_id: str):
    voice = service.get_voice(voice_id)
    if not voice.folder:
        raise AvatarError("平台音色没有本地目录，暂不支持查找头像。", code="avatar_unsupported")
    folder = Path(voice.folder)
    if not folder.is_dir():
        raise AvatarError("这条音色的目录不存在，可能已被移动或删除。", code="avatar_folder_missing")
    return voice, folder


def _client() -> httpx.AsyncClient:
    normalize_no_proxy_env()
    timeout = httpx.Timeout(12.0, connect=8.0)
    return httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=avatar_sources.HEADERS
    )


async def _save(
    service: LibraryService,
    voice_id: str,
    folder: Path,
    found: _Found,
    name: str,
) -> dict[str, object]:
    target = _write_avatar(folder, found.rendered.png)
    original = _write_source(folder, found.original, found.ext) if found.original else None
    await _rescan(service)
    updated = service.get_voice(voice_id)
    where = f" · 原图 {original.name}" if original else ""
    CHANNEL.info(
        f"头像已保存：{target.name}{where} · 来源 {found.candidate.source} · {found.candidate.page}",
        source="avatar",
        voice=voice_id,
    )
    return {
        "voice": updated,
        "source": found.candidate.source,
        "source_url": found.candidate.page,
        "matched_name": name,
    }


def _existing_picture(folder: Path) -> str:
    """目录里已经有一张图就返回它的文件名。

    判据与扫描器同一份（``walker.COVER_NAMES``：``avatar.*`` 优先，其次 ``cover.*``）：
    批量只补空缺，眼前已经有图的不再上网、也不覆盖。
    """
    for name in COVER_NAMES:
        if (folder / name).is_file():
            return name
    return ""


async def generate(service: LibraryService, voice_id: str) -> dict[str, object]:
    """一键配头像：**只补空缺**。目录里已有图（``avatar.*`` 或手工 ``cover.*``）就跳过，
    不上网、不动文件；要换头像走 :func:`candidates` 挑一张再 :func:`apply_candidate`。"""
    voice, folder = _local_voice(service, voice_id)
    existing = _existing_picture(folder)
    if existing:
        # 图在目录里、库里却还没登记（外部放进去的）：顺手重扫，界面下次就不会把它算进空缺。
        await _rescan(service)
        return {
            "voice": service.get_voice(voice_id),
            "source": "",
            "source_url": "",
            "matched_name": "",
            "skipped": existing,
        }
    name = (voice.shown_name or voice.name or "").strip()
    async with _client() as client:
        found = await _find_match(client, voice)
    return await _save(service, voice_id, folder, found, name)


async def candidates(service: LibraryService, voice_id: str) -> dict[str, object]:
    """列出裁好的候选（不落盘），由用户挑一张再 :func:`apply_candidate`。"""
    voice, _ = _local_voice(service, voice_id)
    name = (voice.shown_name or voice.name or "").strip()
    async with _client() as client:
        found = await _collect(client, voice)
    return {"voice": voice, "items": _remember(voice_id, name, found)}


async def apply_candidate(service: LibraryService, voice_id: str, token: str) -> dict[str, object]:
    """把用户选中的那张写盘；原图按来源 URL 重新下一次，失败就只留头像。"""
    _prune_pending()
    pending = _PENDING.pop(token, None)
    if pending is None:
        raise AvatarError(
            "这张候选已经过期，请重新查找。", code="avatar_candidate_expired", retryable=True
        )
    if pending.voice_id != voice_id:
        raise AvatarError("候选与音色不匹配，请重新查找。", code="avatar_candidate_mismatch")
    _, folder = _local_voice(service, voice_id)
    found = pending.found
    if found.original is None:
        async with _client() as client:
            downloaded = await _download(client, found.candidate)
        if downloaded:
            found = _Found(found.rendered, found.candidate, downloaded[1], downloaded[0])
        else:
            # 拿不到原图时别把上一张的原图当成这张的。
            for name in _SOURCE_NAMES:
                (folder / name).unlink(missing_ok=True)
    return await _save(service, voice_id, folder, found, pending.name)
