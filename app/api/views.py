"""HTTP 层的公共件：请求/响应模型，以及各分区共用的载荷拼装。

路由本身按域分在 :mod:`app.api.sections`；这里只放「多个分区都要用」的东西，
避免分区之间互相导入。
"""


from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.api.errors import ApiError
from app.domain.models import (
    AGE_CHOICES,
    GENDER_CHOICES,
    LANGUAGE_CHOICES,
    TIMBRE_CHOICES,
    TONE_CHOICES,
    Voice,
)
from app.domain.request_spec import PARAM_LABELS
from app.infra import archive, recordings
from app.infra.config import (
    cache_base,
    data_dir,
    load_config,
    log_base,
    resolve_host,
    resolve_port,
)
from app.infra.media import audio_mime
from app.infra.uploads import UploadTooLarge, read_limited
from app.usecase.engines import (
    active_engines,
    engines_for_voice,
    marks_payload,
    settings_payload,
)

__all__ = ["AsrModelIn", "AsrOptionsIn", "AvatarApplyIn", "AvatarSourceIn", "ClientLogIn", "DesignPromptIn", "FoldersIn", "MainSampleIn", "OutputIn", "PrefsIn", "PronunciationEntryIn", "PronunciationsIn", "SamplePatch", "StorageIn", "StorageOpenIn", "SynthesizeIn", "TextsIn", "TranscribeIn", "VocabEmotionsIn", "VocabIn", "VocabInstructionsIn", "VoicePatch", "bind_host", "bind_scheme", "set_bind_host", "set_bind_scheme"]

#: 幂等记录的有效期（秒）。24 小时覆盖"当天整章重跑"这种最常见的重试场景。
_IDEMPOTENCY_TTL = 24 * 3600

#: 进程启动时刻，用于 ``/health`` 的 ``uptime_s``。
_STARTED = time.monotonic()

#: 实际监听地址。``--host`` 可以盖掉配置里的值，所以不能现算 ``resolve_host``。
#: 只给 ``/health`` 判断「这台是不是开在局域网上」用。
_BIND_HOST = ""
_BIND_PORT = 0
#: 实际协议（``http`` / ``https``）。命令行与证书是否签成都可能盖掉配置，同样不能现算。
_BIND_SCHEME = ""


def set_bind_host(host: str) -> None:
    global _BIND_HOST
    _BIND_HOST = str(host or "").strip()


def set_bind_port(port: int) -> None:
    global _BIND_PORT
    _BIND_PORT = int(port or 0)


def set_bind_scheme(scheme: str) -> None:
    global _BIND_SCHEME
    _BIND_SCHEME = "https" if str(scheme or "").strip().lower() == "https" else "http"


def bind_host() -> str:
    """本次进程的监听地址；宿主还没设置时按配置解析。"""
    return _BIND_HOST or resolve_host(None)


def bind_port() -> int:
    """本次进程的监听端口；``--port`` 会盖掉配置，所以只认宿主设进来的值。"""
    return _BIND_PORT or resolve_port(None)


def bind_scheme() -> str:
    """本次进程对外提供的协议；宿主没设过（外部 uvicorn 起的、测试客户端）即 ``http``。"""
    return _BIND_SCHEME or "http"


# --------------------------------------------------------------------- #
# 进出参
# --------------------------------------------------------------------- #


class FoldersIn(BaseModel):
    folders: list[str] = Field(default_factory=list)


class OutputIn(BaseModel):
    """生成结果目录。**空串 = 回到默认**（服务数据目录下的 ``generated``）。"""

    dir: str = ""
    #: 改了引擎保存目录时，是否把旧目录中的文件一并迁移。
    #: 默认不迁移——改一项配置不应移动用户文件；界面会先询问。
    migrate: bool = False


class VoicePatch(BaseModel):
    display: str | None = None
    gender: str | None = None
    age: str | None = None
    language: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    category: str | None = None
    main_sample: str | None = None


class SamplePatch(BaseModel):
    text: str | None = None
    emotion: str | None = None
    tags: list[str] | None = None


class MainSampleIn(BaseModel):
    file: str = ""


class DesignPromptIn(BaseModel):
    """让 AI 写一句设计描述：给音色名，可带一句用户已写的草稿。"""

    name: str = ""
    brief: str = ""


class PrefsIn(BaseModel):
    hidden: bool | None = None
    favorite: bool | None = None


class AsrModelIn(BaseModel):
    """当前使用的识别引擎。**空串 = 使用第一台**。"""

    key: str = ""


class AsrOptionsIn(BaseModel):
    """应用层识别选项。缺省字段不改（都是可选的布尔）。"""

    auto_transcript: bool | None = None
    auto_on_connect: bool | None = None
    writeback: bool | None = None
    transcribe_recordings: bool | None = None
    language_writeback: bool | None = None


class TranscribeIn(BaseModel):
    """待识别的样例：**音色 id + 文件名**，不是路径。

    与服务端拼路径的其它入口一致：调用方给出库中的两个名字，路径由服务端解析，
    因此 ``..`` 之类的路径穿越在形状上不可能成立。
    """

    voice_id: str
    file: str


class TextsIn(BaseModel):
    """文案库（生成样例时"挑一句现成的"）。一组 = 一个名字 + 一串句子。"""

    groups: list[dict[str, Any]] = Field(default_factory=list)


class VocabEmotionsIn(BaseModel):
    """情绪候选的差别：删掉哪些默认词、把哪个默认词改叫什么、自己加了哪些。"""

    hidden: list[str] = Field(default_factory=list)
    renamed: dict[str, str] = Field(default_factory=dict)
    custom: list[str] = Field(default_factory=list)


class VocabInstructionsIn(BaseModel):
    """引导指令预设的差别：删掉哪些默认条、改哪个短名、自己加了哪些。"""

    hidden: list[str] = Field(default_factory=list)
    renamed: dict[str, str] = Field(default_factory=dict)
    custom: list[dict[str, str]] = Field(default_factory=list)


class VocabIn(BaseModel):
    """用户词表：两节都可选，只传要改的那一节，另一节原样保留。"""

    emotions: VocabEmotionsIn | None = None
    instructions: VocabInstructionsIn | None = None


class PronunciationEntryIn(BaseModel):
    """一条读音替换：把正文里的 word 换成 replacement（同音词）。"""

    word: str = ""
    replacement: str = ""


class PronunciationsIn(BaseModel):
    """整份读音替换表。"""

    entries: list[PronunciationEntryIn] = Field(default_factory=list)


class ClientLogIn(BaseModel):
    """前端提示写进日志链路：界面说一句，日志里也留一条。"""

    message: str
    level: str = "info"
    source: str = "ui"


class AvatarApplyIn(BaseModel):
    """选中的头像候选。token 由候选列表接口发，只活在内存里十分钟。"""

    token: str


class AvatarSourceIn(BaseModel):
    """头像来源：取 ``/avatar/status`` 下发的 key，一次只查一个。"""

    source: str = ""


class SynthesizeIn(BaseModel):
    """合成请求。四个显式字段定位路由与内容，其余规范项走 ``extra``。

    :param extra: 统一参数的扩展位。VoiceHub 不解释这些键，由目标引擎的映射表
        决定落点（见 ``docs/request-spec.md`` §一 与 §九）。
    :param persist: 这一次的音频归谁：

        - ``none``（默认）：**不落盘**，直接回音频字节。给第三方 API 调用方——
          产物归调用方，VoiceHub 不替它保存；
        - ``cache``：落进缓存目录，给 VoiceHub 自己的生成页；
        - ``sample``：落进音色保存目录并登记为该音色的样例，给卡片上的「生成样例」。
    """

    engine: str
    voice_id: str
    text: str
    emotion: str = ""
    format: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
    persist: Literal["none", "cache", "sample"] = "none"
    #: 本地音色用哪一条样例：``sample_file`` 钉死一条；``match_emotion`` 按情绪匹配。
    #: 平台音色没有样例，两项都不生效；固定样例文件不在时网关会回落主样例。
    sample_file: str = ""
    match_emotion: bool = False

    @field_validator("extra")
    @classmethod
    def _no_reserved_keys(cls, extra: dict[str, Any]) -> dict[str, Any]:
        """``extra`` 只放规范参数，不能覆盖四个显式字段。

        ``SynthesisRequest.as_values()`` 最后 ``update(extra)``（参考音频要靠它覆盖已存的
        样例），于是 ``extra.text`` 能悄悄换掉正文：空文本校验、读音表、日志看的都是顶层
        的那个，发出去的却是另一个。这里在入口拦下，比在装配层猜来源可靠。
        """
        reserved = sorted({"text", "voice_id", "emotion", "format"} & extra.keys())
        if reserved:
            raise ValueError(
                f"extra 里不能再写 {' / '.join(reserved)}"
                "：这四个字段请放顶层，否则会覆盖实际发出的正文、音色或格式"
            )
        return extra


def _params_out(params: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    """生成参数 → 界面可渲染的形状。中文名取自规范表，界面不自造词。"""
    return [
        {"key": key, "label": PARAM_LABELS.get(key, key), "value": value}
        for key, value in params
    ]


def _audio_url(voice_id: str, file: str) -> str:
    """样例播放地址。两段都必须 percent-encode。

    文件名由用户命名，常含 ``#``、``?``、``%``——它们在 URL 中有特殊含义，
    不编码会导致"点了没有声音也不报错"（服务端收到的是被截断的路径）。
    """
    return f"/api/v1/audio/{quote(voice_id, safe='')}/{quote(file, safe='')}"


#: ``persist="none"`` 时元数据的去向：URL 编码的 JSON 头（HTTP 头只能是 ASCII）。
_META_HEADER = "X-VoiceHub-Meta"


def _binary_audio(meta: dict[str, Any], audio: bytes) -> Response:
    """不落盘的合成结果：音频字节 + 元数据头。

    元数据（id / format / cost_note / notes / warnings）放 ``X-VoiceHub-Meta``，
    URL 编码的 JSON；响应体就是音频本身，调用方直接写盘或转发。
    """
    fmt = str(meta.get("format") or "")
    headers = {
        _META_HEADER: quote(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")), safe=""
        ),
        "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="{meta.get("id", "voicehub")}.{fmt or "bin"}"',
    }
    return Response(content=audio, media_type=audio_mime(fmt), headers=headers)


def _synthesis_replay(cached: dict[str, Any], persist: str) -> Any:
    """幂等重放：``none`` 的记录里存着音频，按字节回；其余原样回 JSON。"""
    if persist == "none" and "audio_b64" in cached:
        audio = base64.b64decode(cached["audio_b64"])
        meta = {key: value for key, value in cached.items() if key != "audio_b64"}
        return _binary_audio(meta, audio)
    return cached


#: 目录统计缓存：``路径 → (统计时刻, (字节, 文件数))``。
#:
#: 递归遍历在缓存目录很大时要几百毫秒到几秒，而设置页那一行只是"大概多大"。
#: 同一个路径 :data:`_USAGE_TTL` 秒内只真算一次；迁目录 / 清缓存最多晚这么久反映。
_USAGE_TTL = 20.0
_USAGE_CACHE: dict[str, tuple[float, tuple[int, int]]] = {}


def _dir_usage(path: Path) -> tuple[int, int]:
    """目录占用（字节、文件数）。读不到的子项跳过——统计不是权限检查。"""
    key = str(path)
    now = time.monotonic()
    hit = _USAGE_CACHE.get(key)
    if hit is not None and now - hit[0] < _USAGE_TTL:
        return hit[1]
    total = 0
    files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    result = (total, files)
    _USAGE_CACHE[key] = (now, result)
    return result


def _storage_entries() -> list[dict[str, Any]]:
    """设置页「存储」栏的四行：数据 / 生成结果 / 缓存 / 日志。

    ``path`` 是**生效值**（默认值也算），``value`` 是配置里的原值（空 = 用默认）：
    界面显示 ``path``，输入框绑 ``value``——把生效值当配置保存会把"默认"变成"写死"。
    """
    config = load_config()
    rows: list[tuple[str, str, Path, bool, str, str, str]] = [
        (
            "data",
            "服务数据目录",
            data_dir(),
            False,
            "VOICEHUB_DATA_DIR",
            "config.json 与 hub.db 所在；改它要重启服务。",
            "",
        ),
        (
            "output",
            "样例保存目录",
            archive.default_root(),
            True,
            "",
            "平台音色「生成样例」的落点；本地音色的样例在它自己的目录里。",
            config.output_dir,
        ),
        (
            "cache",
            "缓存目录",
            cache_base(create=False),
            True,
            "",
            "生成页的结果与参考音频转换；可随时清空。",
            config.cache_dir,
        ),
        (
            "log",
            "日志目录",
            log_base(create=False),
            True,
            "",
            "voicehub.log 所在。",
            config.log_dir,
        ),
    ]
    entries: list[dict[str, Any]] = []
    for key, label, path, editable, locked_by, note, value in rows:
        exists = path.exists()
        size, files = _dir_usage(path) if exists else (0, 0)
        entries.append(
            {
                "key": key,
                "label": label,
                "path": str(path),
                "value": value,
                "editable": editable,
                "locked_by": locked_by,
                "exists": exists,
                "size": size,
                "files": files,
                "note": note,
            }
        )
    return entries


def _cache_policy() -> dict[str, int]:
    """转换产物的回收线（设置页「数据与存储」里那两格）：上限 MB 与保留天数。"""
    config = load_config()
    return {"limit_mb": config.cache_limit_mb, "keep_days": config.cache_keep_days}


def _cache_number(value: int, label: str, ceiling: int) -> int:
    """回收线的一格：``0`` = 那条规则不启用；负数与离谱的大数一律 400。

    不在这里悄悄夹到边界：界面写了 99999 却按 3650 生效，是更难查的那种错。
    """
    if value < 0 or value > ceiling:
        raise ApiError(
            status=400,
            code="cache_setting_out_of_range",
            message=f"{label}要填 0–{ceiling} 之间的整数（0 = 不启用）。",
        )
    return value


#: 不该被当成"存作品的目录"的系统位置（Windows 取盘符下的一级目录名）。
_SYSTEM_TOP_DIRS = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
}

#: POSIX 上的系统位置：整棵子树都不许。
_SYSTEM_ROOTS = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
)


def _is_system_dir(path: Path) -> bool:
    """盘根 / 系统目录 / 用户主目录本身：这些位置不能用来存产物或缓存。

    不做"白名单根"：用户把书放在 ``D:\\有声书``、缓存放在移动盘都是正常用法，
    拦这些只会逼人改目录。这里只挡那些"选错了就是把文件撒进系统里"的位置。
    """
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        # 解析失败（网络盘不可达等）交给后续的建目录逻辑报错，不在这里拦。
        return False
    if resolved.parent == resolved:  # 盘符根 / "/"
        return True
    if os.name == "nt":
        drive = Path(resolved.drive + "\\") if resolved.drive else None
        if drive is None:
            return False
        try:
            top = resolved.relative_to(drive).parts[0].casefold()
        except (ValueError, IndexError):
            return False
        if top in _SYSTEM_TOP_DIRS:
            return True
    else:
        if any(str(resolved) == root or str(resolved).startswith(root + "/") for root in _SYSTEM_ROOTS):
            return True
    try:
        if resolved == Path.home().resolve():
            return True
    except OSError:
        pass
    return False


def _absolute_dir(value: str, label: str) -> str:
    """存储目录的输入：空串 = 回默认；否则必须是绝对路径，且不能是系统目录。"""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ApiError(
            status=400,
            code="storage_dir_relative",
            message=f"{label}要填绝对路径（例如 D:\\voicehub）。",
        )
    if _is_system_dir(path):
        raise ApiError(
            status=400,
            code="unsafe_path",
            message=f"{label}不能选盘符根、系统目录或用户主目录：换一个专用文件夹。",
        )
    return str(path)


class StorageIn(BaseModel):
    """设置页「存储」栏：只发改过的项；``None`` = 不动，空串 = 回到默认。"""

    output_dir: str | None = None
    cache_dir: str | None = None
    log_dir: str | None = None
    #: 转换产物的回收线：上限（MB）与保留天数，``0`` = 那条规则不启用。
    cache_limit_mb: int | None = None
    cache_keep_days: int | None = None
    #: 改生成结果目录时，把旧目录里的东西一并搬过去（默认不搬）。
    #: 界面在保存前用 ``POST /output/preview`` 问过用户。
    migrate_output: bool = False


class StorageOpenIn(BaseModel):
    key: str


class ServiceIn(BaseModel):
    """监听范围、https、访问令牌与开机自启。前三项重启后生效，自启立刻生效。"""

    #: 没给的字段不动：四项各自独立，前端只提交改过的那几个。
    lan: bool | None = None
    #: https 启动方式：``auto``（跟随监听地址）/ ``on`` / ``off``。
    https: Literal["auto", "on", "off"] | None = None
    #: 空串 = 清掉令牌（回到"不校验"，仅本机使用时的默认）。
    access_token: str | None = None
    #: 开机自启；``None`` = 这次不改。只在桌面版可设（注册表项要指向启动器）。
    autostart: bool | None = None


class SapiConfigIn(BaseModel):
    """Windows SAPI 桥接设置：选音色与是否设为系统默认，引擎固定默认 ``auto``。"""

    voice_id: str | None = None
    is_default: bool | None = None


class SapiInstallIn(BaseModel):
    """安装 SAPI 桥接时的初始选项。"""

    voice_id: str = ""
    set_default: bool = True


def _cover_url(voice: Voice) -> str:
    """封面地址带上文件时间：重新生成/换图后 URL 一定变，浏览器不会拿旧图。

    封面只按 id 缓存的话，生成完头像详情里还是上一张，得手动刷新才看得到。
    列表与详情都用缩略图（``s=thumb``）：头像原图是 0.5–1MB 的 PNG，
    几百张卡一起进来时解码就是卡顿本身。
    """
    if not voice.cover or not voice.folder:
        return ""
    try:
        stamp = (Path(voice.folder) / voice.cover).stat().st_mtime_ns
    except OSError:
        stamp = 0
    return f"/api/v1/cover/{voice.id}?v={stamp}&s=thumb"


def _voice_description(voice: Voice) -> str:
    """这条音色的"描述"。设计音色没有别的东西可描述：它的描述就是那段设计描述。

    取 ``payload["design_prompt"]`` 而不是库里那一列：凭据在 payload 里（改描述会
    同步过去，见 ``LibraryService._update_platform_voice``），早先建的音色那一列还
    写着引擎的说明文字，读 payload 才能显示用户真正写下的那段。
    """
    design = str((voice.payload or {}).get("design_prompt") or "").strip()
    return design or voice.description


def _voice_out(
    voice: Voice, *, with_samples: bool = False, engine: dict[str, Any] | None = None
) -> dict[str, Any]:
    """序列化。显示名回落由 domain 的 ``shown_name`` 负责，这里不重复判断。"""
    out: dict[str, Any] = {
        "object": "voice",
        "id": voice.id,
        "source": voice.source,
        "is_local": voice.is_local,
        "engine": engine,
        "name": voice.name,
        "shown_name": voice.shown_name,
        "category": voice.category,
        "gender": voice.gender,
        "age": voice.age,
        "language": voice.language,
        "tags": list(voice.tags),
        "description": _voice_description(voice),
        # 设计音色的判据只看 payload 那句描述（与列表筛选、左栏计数同一口径），
        # 不看 tag 或 category——那两格用户能改，改完它就从这一组里掉出去了。
        "is_design": bool(str((voice.payload or {}).get("design_prompt") or "").strip()),
        "hidden": voice.hidden,
        "favorite": voice.favorite,
        "main_sample": voice.main_sample,
        # 列表不返回 samples（代价高），主样例地址单独给出。
        "main_audio_url": (
            _audio_url(voice.id, voice.main_sample)
            if voice.folder and voice.main_sample
            else ""
        ),
        "sample_count": voice.sample_count or len(voice.samples),
        # 这条音色能落到哪些引擎：前端照它摆选项，不自己按能力推一遍。
        "engines": engines_for_voice(voice),
        "cover_url": _cover_url(voice),
        "avatar_generated": voice.cover.startswith("avatar."),
        "has_cover": bool(voice.cover),
    }
    if with_samples:
        out["folder"] = voice.folder
        out["samples"] = [
            {
                "object": "sample",
                "file": s.file,
                "text": s.text,
                "emotion": s.emotion,
                "tags": list(s.tags),
                "params": _params_out(s.params),
                "duration": s.duration,
                "is_main": s.file == voice.main_sample,
                "audio_url": _audio_url(voice.id, s.file) if voice.folder else "",
                # 绝对路径：本地推理引擎的 reference_audio 需要服务端文件路径。
                "path": str(Path(voice.folder) / s.file) if voice.folder else "",
            }
            for s in voice.samples
        ]
    return out


def _vocab() -> dict[str, list[list[str]]]:
    """标签候选词表（值 → 说法），随 ``/tree`` 下发。"""
    return {
        "genders": [[value, label] for value, label in GENDER_CHOICES],
        "ages": [[value, label] for value, label in AGE_CHOICES],
        "languages": [[value, label] for value, label in LANGUAGE_CHOICES],
        # 角色卡（音色详情）的两组固定标签：语调 / 音色。
        "tones": [[value, label] for value, label in TONE_CHOICES],
        "timbres": [[value, label] for value, label in TIMBRE_CHOICES],
    }


def _engine_states() -> dict[str, str]:
    """**已启用**引擎的就绪状态：``ready`` / ``not_configured`` / ``error``。

    健康检查回答的是"现在在用的那几台能不能用"。停用的报 ``not_configured``
    只会让人以为配坏了——它本来就不该出现在工作集里。
    单台引擎读取失败不影响其余条目——健康检查本身不应因一台坏插件而失败。
    """
    states: dict[str, str] = {}
    # 这一包要的是就绪状态，顺带被算进去的"通用标签"却要读全部引擎的配置：
    # 算一次传进去，别让健康检查（界面每隔几秒问一次）按引擎数平方地读配置。
    common = {row["name"] for row in marks_payload()["common"]}
    for key in active_engines():
        try:
            payload = settings_payload(key, common=common)
            states[key] = "ready" if payload.get("ready") else "not_configured"
        except Exception:  # noqa: BLE001 - 坏插件只降级它自己这一项
            states[key] = "error"
    return states


def _fingerprint(payload: dict[str, Any]) -> str:
    """幂等指纹：请求内容规范化后的 SHA-256。

    同键不同内容必须被识别为冲突，而不是返回一份与本次请求无关的旧结果。
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 克隆音源可以比录音长（一两分钟的 wav 有十几 MB），单独给一档。
_MAX_CLONE_BYTES = 20 * 1024 * 1024


async def _read_audio(upload: UploadFile) -> bytes:
    """读一次上传的音频。**上限在这里拦**：录音是浏览器给的，别等写满磁盘。"""
    try:
        raw = await read_limited(upload, recordings.MAX_BYTES)
    except UploadTooLarge as exc:
        raise ApiError(
            status=400,
            code="audio_too_large",
            message=f"录音超过 {exc.label}。",
        ) from exc
    if not raw:
        raise ApiError(status=400, code="audio_empty", message="录音为空。")
    return raw


def _temp_audio(raw: bytes, suffix: str) -> Path:
    """把上传的内容放到一个临时文件里（识别完就删，不进任何归档）。"""
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - 路径需在退出后仍存在
        prefix="voicehub-dictation-", suffix=suffix or ".wav", delete=False
    )
    handle.write(raw)
    handle.close()
    return Path(handle.name)


# --------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------- #


