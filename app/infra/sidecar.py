"""sidecar ``voice.json`` 的读写——本地音色元数据的源头（跟随音库本身）。

**扫描只读**：没有该文件不创建、字段缺省不补写；只在用户首次编辑时落盘，
且只写与扫描推导不同的字段。

samples 按键增量合并：音频文件删除后，对应键在下次保存时清掉；新文件无键时
回落到同名 .txt / list 清单的来源（见 :mod:`app.usecase.library`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json

__all__ = [
    "SIDECAR_NAME",
    "Sidecar",
    "read_sidecar",
    "revision",
    "sample_bytes",
    "sample_duration",
    "sample_params",
    "write_sidecar",
]

SIDECAR_NAME = "voice.json"

#: 本进程写过几次 sidecar。**读的一侧据此判断缓存是不是已经过期**。
#:
#: 用例层按目录缓存了"引擎保存目录里的产物索引"（含各目录的 sidecar），列表接口
#: 因此不必每次读盘。但用户刚在界面上改的文字就在 sidecar 里——不认这个计数器，
#: 改完再看还是旧文字（缓存要等 TTL 才过期）。写一次 +1，读的比一下数字就知道。
_REVISION = [0]


def revision() -> int:
    """sidecar 的写入次数（进程级单调递增）。"""
    return _REVISION[0]


@dataclass(slots=True)
class Sidecar:
    """``voice.json`` 的内容。**全部字段可缺省**——缺省 = 用扫描推导值。"""

    gender: str = ""
    age: str = ""
    language: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    display: str = ""
    main_sample: str = ""
    #: 收藏 / 隐藏。``None`` = 这座库里没设过（此时以数据库里的为准）。
    #: 存进 sidecar 是为了**跟着目录走**：文件夹改名或搬家后 ``voice_id`` 会变，
    #: 数据库里那两栏跟着旧行一起没了，这里的副本能把它找回来。
    favorite: bool | None = None
    hidden: bool | None = None
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 这个文件夹里的音频属于库里哪条音色（键是 ``voice_id`` / ``engine``）。
    #: 这是"归属"的正本——文件夹名曾取显示名，用户一改显示名音频就散进两个文件夹，
    #: 键才是可靠的。名字只留给没有此键的老文件夹兜底。
    origin: dict[str, str] = field(default_factory=dict)

    # ``samples`` 每条的键：``text`` / ``emotion`` / ``tags`` / ``duration`` / ``bytes`` /
    # ``params``。``params`` 只有生成出来的样例才有（见 domain/models.Sample）。
    # ``duration`` + ``bytes`` 是**跟着目录走的时长备份**：字节数对得上就直接用，
    # 省掉一次 ffprobe（文件夹改名、库重建时尤其值）；对不上说明音频换过，去量真的。


def _sidecar_path(folder: Path) -> Path:
    return folder / SIDECAR_NAME


def _seconds(value: Any) -> float:
    """时长写读都用这一处折形：保留 3 位小数，坏值当 0（= 没量过）。"""
    try:
        seconds = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(seconds, 3) if seconds > 0 else 0.0


def _bytes(value: Any) -> int:
    """字节数折形：正整数才算数，其余当 0（= 没记过）。"""
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, size)


def _flag(raw: dict[str, Any], key: str) -> bool | None:
    """读一个"设过没有"的三态开关：键不在 → ``None``；在 → ``True``/``False``。"""
    if key not in raw:
        return None
    value = raw[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def read_sidecar(folder: Path) -> Sidecar:
    """读 sidecar。文件不在 / 坏了 → 全缺省（扫描推导值顶上，不报错）。

    坏掉的**要留一行日志**：多一个逗号就足以让整份 sidecar 被当成不存在——文字、标签
    和 ``origin`` 一起消失，症状是"这条平台音色怎么又变回本地音色了"。静默丢弃太贵。
    """
    path = _sidecar_path(folder)
    if not path.exists():
        return Sidecar()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from app.infra.loghub import CHANNEL  # 延迟导入：这条只在出问题时走

        CHANNEL.warning(
            f"voice.json 读不了，已按空处理：{path}（{exc}）",
            source="library",
            path=str(path),
        )
        return Sidecar()
    samples: dict[str, dict[str, Any]] = {}
    for key, value in (raw.get("samples") or {}).items():
        if isinstance(value, dict):
            entry: dict[str, Any] = {
                "text": str(value.get("text", "") or ""),
                "emotion": str(value.get("emotion", "") or ""),
            }
            # 样例自己的标签：没有键就不写（空列表与"没设过"在文件里无法区分）。
            tags = [str(t).strip() for t in (value.get("tags") or []) if str(t).strip()]
            if tags:
                entry["tags"] = tags
            # 时长备份：0 / 缺失 都算没量过，不留键（免得读的人以为 0 秒是事实）。
            seconds = _seconds(value.get("duration"))
            if seconds > 0:
                entry["duration"] = seconds
                # 写入时的字节数：读回时用它判"还是不是同一段音频"。没有它就只能当参考，
                # 不能当真（见 app.infra.walker._backup_durations）。
                size = _bytes(value.get("bytes"))
                if size > 0:
                    entry["bytes"] = size
            # 生成参数只认一层平表：它是一条音频的来路，不是可回喂的请求体。
            raw_params = value.get("params")
            params = (
                {
                    str(key): str(item)
                    for key, item in raw_params.items()
                    if str(item).strip()
                }
                if isinstance(raw_params, dict)
                else {}
            )
            if params:
                entry["params"] = params
            # 历史回写的两栏：这条样例从哪一条音频来、已经入库到哪些音色。
            # 它们是"这条文件的事实"，与 text / duration 同级；丢了之后历史既说不出来路，
            # 也会把同一条音频在列表里重新标成"未入库"。
            source = str(value.get("source") or "").strip()
            if source:
                entry["source"] = source
            absorbed = [
                {"voice_id": str(row.get("voice_id") or ""), "name": str(row.get("name") or "")}
                for row in (value.get("absorbed") or [])
                if isinstance(row, dict) and str(row.get("voice_id") or "").strip()
            ]
            if absorbed:
                entry["absorbed"] = absorbed
            samples[str(key)] = entry
    # 归属：只认一层平表（键值均为字符串），值只有一个答案：音色 id。
    raw_origin = raw.get("origin")
    origin = (
        {
            str(key): str(value).strip()
            for key, value in raw_origin.items()
            if str(value).strip()
        }
        if isinstance(raw_origin, dict)
        else {}
    )
    return Sidecar(
        gender=str(raw.get("gender", "") or "").strip(),
        age=str(raw.get("age", "") or "").strip(),
        language=str(raw.get("language", "") or "").strip(),
        tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
        description=str(raw.get("description", "") or "").strip(),
        display=str(raw.get("display", "") or "").strip(),
        main_sample=str(raw.get("main_sample", "") or "").strip(),
        favorite=_flag(raw, "favorite"),
        hidden=_flag(raw, "hidden"),
        samples=samples,
        origin=origin,
    )


def sample_duration(entry: dict[str, Any] | None) -> float:
    """sidecar 里一条样例的时长**备份**（秒）。没有 / 坏值 → ``0.0``。

    这不是判据：文件大小对得上（见 :func:`sample_bytes`）才用它顶替 ffprobe，
    对不上还是要去量（见 ``app.infra.walker``）。
    """
    return _seconds(entry.get("duration")) if isinstance(entry, dict) else 0.0


def sample_bytes(entry: dict[str, Any] | None) -> int:
    """sidecar 里这条时长是对着多大一个文件记的（0 = 没记过，只能当参考）。"""
    return _bytes(entry.get("bytes")) if isinstance(entry, dict) else 0


def sample_params(entry: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """sidecar 里的一条样例记录 → ``Sample.params`` 形状。

    保序（参数顺序由规范定）；返回元组以保持 ``Sample`` 可哈希。
    """
    if not isinstance(entry, dict):
        return ()
    raw = entry.get("params")
    if not isinstance(raw, dict):
        return ()
    return tuple((str(key), str(value)) for key, value in raw.items())


def write_sidecar(folder: Path, sidecar: Sidecar) -> None:
    """写 sidecar（原子写）。只写有内容的字段；全缺省时删掉文件（与"设过又清空"区分）。"""
    _REVISION[0] += 1
    path = _sidecar_path(folder)
    payload: dict[str, Any] = {"version": 1}
    for key in ("gender", "age", "language", "description", "display", "main_sample"):
        value = getattr(sidecar, key)
        if value:
            payload[key] = value
    if sidecar.tags:
        payload["tags"] = sidecar.tags
    # 三态：None = 没设过，连键都不写（不然读的人会以为"取消收藏过"）。
    if sidecar.favorite is not None:
        payload["favorite"] = bool(sidecar.favorite)
    if sidecar.hidden is not None:
        payload["hidden"] = bool(sidecar.hidden)
    if sidecar.origin:
        payload["origin"] = sidecar.origin
    samples = {
        name: {k: v for k, v in entry.items() if v}
        for name, entry in sidecar.samples.items()
    }
    # 时长统一折形：界面编辑带过来的浮点可能带一串尾数，文件里不该有。
    for entry in samples.values():
        if "duration" in entry:
            seconds = _seconds(entry["duration"])
            if seconds > 0:
                entry["duration"] = seconds
            else:
                entry.pop("duration")
    samples = {name: entry for name, entry in samples.items() if entry}
    if samples:
        payload["samples"] = samples

    if len(payload) == 1:  # 只剩 version = 全缺省
        path.unlink(missing_ok=True)
        return
    write_json(path, payload)
