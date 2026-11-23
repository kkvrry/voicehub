"""服务配置（``config.json``）：这台机器上这个服务的状态（扫描根目录等）。

数据目录解析顺序（测试与演示依赖）：``VOICEHUB_DATA_DIR`` → ``%LOCALAPPDATA%\\voicehub``
→ ``~/.voicehub``。选 json 而非 yaml 是为了少一个依赖。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.infra.atomic import write_json
from app.infra.files import file_stamp

__all__ = [
    "DEFAULT_CACHE_KEEP_DAYS",
    "DEFAULT_CACHE_LIMIT_MB",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HTTPS_AUTO",
    "HTTPS_OFF",
    "HTTPS_ON",
    "WILDCARD_HOSTS",
    "Config",
    "cache_base",
    "data_dir",
    "is_loopback_host",
    "load_config",
    "log_base",
    "resolve_host",
    "resolve_https",
    "resolve_port",
    "save_config",
]

#: 服务默认端口（``127.0.0.1:17865``）。不用常用默认端口，避免与本地工具相撞且难排查。
#:
#: 端口三层的优先级只写在 :func:`resolve_port` 一处：
#: ``--port`` > ``VOICEHUB_PORT`` > ``config.json``。
DEFAULT_PORT = 17865

#: 默认只监听回环。局域网访问要显式打开（``--host 0.0.0.0`` / ``VOICEHUB_HOST`` /
#: ``config.json`` 的 ``host``）——这个服务没有身份校验，默认暴露给整个网段不合适。
DEFAULT_HOST = "127.0.0.1"

#: 通配地址：绑它们等于"这台机器的所有网卡"。
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})

#: https 三态。``auto`` = 跟随监听地址（非回环即开），另两个是用户的明确选择。
HTTPS_AUTO = "auto"
HTTPS_ON = "on"
HTTPS_OFF = "off"

#: 转换产物的默认回收线。512MB 大约是"五百份 12 秒参考音频"的量级，
#: 超过这个数说明积的多半是用不到的旧样例；30 天没被重新生成过的直接清掉。
DEFAULT_CACHE_LIMIT_MB = 512
DEFAULT_CACHE_KEEP_DAYS = 30

#: ``data_dir()`` 的解析结果：``环境变量给出的值 → 目录``。进程内不会变。
_DATA_DIRS: dict[str, Path] = {}

#: ``load_config()`` 的解析结果：``(路径, mtime_ns, 大小) → Config``。
_CONFIG_CACHE: tuple[tuple[str, int, int], Config] | None = None


def _ensure(path: Path) -> Path:
    """目录在就返回，不在才建。

    ``Path.mkdir(parents=True, exist_ok=True)`` 在 Windows 上是一次几百微秒的调用
    （实测 407 µs，``is_dir`` 只有 7 µs），而这两个函数在每次请求里都会被问到。
    """
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """服务数据目录，不存在则创建（只建自己的目录，不碰用户文件）。

    解析结果按"环境变量给出的那个值"缓存：同一进程里它不会变，而这条路径在
    每次请求里要问十几次（``config.json``、``hub.db``、缓存、日志都挂在它下面）。
    """
    override = os.environ.get("VOICEHUB_DATA_DIR", "").strip()
    if override:
        key, root = override, Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA", "").strip()
        key = base or str(Path.home())
        root = Path(base) / "voicehub" if base else Path.home() / ".voicehub"
    cached = _DATA_DIRS.get(key)
    if cached is not None:
        # 运行期被删 / 被挪走时补建——缓存的是"解析结果"，不是"它一直存在"。
        return _ensure(cached)
    _DATA_DIRS[key] = _ensure(root)
    return _DATA_DIRS[key]


def cache_base(*, create: bool = True) -> Path:
    """缓存根目录：配置了 ``cache_dir`` 用它，否则 ``<数据目录>/cache``。

    这里只给"根"；按引擎分的子目录由 :mod:`app.infra.archive` 拼。
    ``create=False`` 给"只看不建"的场合（设置页列目录），坏路径不在这里抛。
    """
    custom = load_config().cache_dir.strip()
    root = Path(custom) if custom else data_dir() / "cache"
    return _ensure(root) if create else root


def log_base(*, create: bool = True) -> Path:
    """日志目录：配置了 ``log_dir`` 用它，否则 ``<数据目录>/logs``。"""
    custom = load_config().log_dir.strip()
    root = Path(custom) if custom else data_dir() / "logs"
    return _ensure(root) if create else root


@dataclass(slots=True)
class Config:
    """``config.json`` 的内容。

    :param folders: 扫描根目录列表。不去重、不排序、不校验存在（目录可能在移动盘上）。
    :param output_dir: 生成结果目录（全局），按 ``<引擎 label>/<音色名>/<文件>`` 归档；
        空 = 服务数据目录下的 ``generated``。
    :param cache_dir: 缓存根目录（参考音频转换、生成页结果）；空 = 数据目录下的 ``cache``。
    :param log_dir: 日志目录；空 = 数据目录下的 ``logs``。
    :param cache_limit_mb: 转换产物（``refaudio`` / ``refaudio-ascii``）合计上限，单位 MB。
        超过就按"最久没碰过"从旧到新删；``0`` = 不按体积回收。
    :param cache_keep_days: 转换产物的保留天数，超过就删（不论总量）；``0`` = 不按时间回收。
    :param engine_outputs: 每台引擎自己的保存目录（引擎 key → 路径），配了则覆盖全局层。
        不硬编码引擎名：装了的引擎都会在自己的设置页里得到这一格。
    :param asr_engine: 指定使用的识别引擎（``app.engines.asr_base`` 插件的 key）；
        空 = 自动，按 key 顺序选当前开着的识别服务，都没有则取第一台。识别不再
        下载权重、不再有"装没装"这回事——引擎自己的 ``settings`` 段说明它从哪来
        （HTTP 服务 / 本地推理），配置照常存在 ``engines`` 里。
    :param asr_options: 应用层识别选项（自动识别、写回样例、录音转写、语种写回），
        与 ``engines`` 分开：保存引擎设置会整段重写 ``engines[key]``，放一起会被抹掉。
    :param engines: 每台引擎的配置，键是引擎 key。放服务端：密钥属于这台机器，不随音色库拷贝。
    :param engines_enabled: 已启用的引擎 key。发现到的插件不必都出现在界面上——未启用的只在
        「添加引擎」的清单里。``None`` = 这份配置还没有这个字段（升级前写的），此时按"用过的"
        推导（见 ``usecase.engines.enabled_keys``），推导结果不落盘；一旦用户启停过一台，
        这里就是一份明确的列表，空列表表示一台也没启用。
    :param llm: 每台对话模型（LLM）的配置，键是插件 key。与 ``engines`` 分开：
        合成引擎与对话模型是两套插件、两套契约，混在一段里保存会互相覆盖。
    :param llm_default: 默认对话模型 key；空 = 按就绪状态自动挑（见 usecase.llm）。
    :param producers: 每个生产方插件的配置，键是插件 key。生产方自声明配置模型，
        宿主只负责存取与掩码。
    :param producers_disabled: 已停用的生产方插件 key。停用是**真的不起**：
        启动时不构造它的服务、不跑自动启动，接口一律拒绝；界面上的入口只是结果。
    :param channels: 每台引擎的音色通道配置（见 :mod:`app.domain.channel`）。
        独立于 ``engines`` 段：保存引擎设置会整段重写 ``engines[key]``，放一起会被抹掉。
    :param port: 服务端口。0 = 用 :data:`DEFAULT_PORT`，"没配"与"配成同一个数"同义。
    :param host: 监听地址。空 = 用 :data:`DEFAULT_HOST`（仅回环）；``0.0.0.0`` = 全网卡。
    :param https: https 启动方式：:data:`HTTPS_AUTO`（跟随监听地址）/ ``on`` / ``off``。
        证书是自己签的那张，落在数据目录 ``certs/``；与 host/port 一样，改完要重启。
    :param access_token: 访问令牌。空 = 不校验（默认，个人本机工具）；
        配了之后非回环请求要带令牌（见 :mod:`app.api.auth`）。与 host/port 一样，
        改完要重启。
    :param cors_origins: 额外允许的 CORS 来源，改配置即可接入新产品。
    """

    folders: list[str] = field(default_factory=list)
    ffprobe_path: str = ""  # 配了就支持全格式时长；空 = 只读 wav
    output_dir: str = ""  # 生成结果目录（全局；空 = 数据目录下的 generated）
    cache_dir: str = ""  # 缓存根（空 = 数据目录下的 cache）
    log_dir: str = ""  # 日志目录（空 = 数据目录下的 logs）
    cache_limit_mb: int = DEFAULT_CACHE_LIMIT_MB  # 转换产物上限（0 = 不按体积回收）
    cache_keep_days: int = DEFAULT_CACHE_KEEP_DAYS  # 转换产物保留天数（0 = 不按时间回收）
    engine_outputs: dict[str, str] = field(default_factory=dict)  # 引擎 key → 它自己的保存目录
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)  # 引擎 key → 音色通道配置
    asr_engine: str = ""  # 当前使用的识别引擎（空 = 第一台）
    asr_options: dict[str, Any] = field(default_factory=dict)  # 应用层识别选项
    port: int = 0  # 服务端口（0 = 用 DEFAULT_PORT；改完要重启才生效）
    host: str = ""  # 监听地址（空 = DEFAULT_HOST；0.0.0.0 = 开放局域网）
    https: str = HTTPS_AUTO  # https 启动方式（auto / on / off；改完要重启才生效）
    access_token: str = ""  # 访问令牌（空 = 不校验；配了则非回环请求要带）
    cors_origins: list[str] = field(default_factory=list)  # 额外允许的 CORS 来源
    engines: dict[str, dict[str, Any]] = field(default_factory=dict)
    engines_enabled: list[str] | None = None
    llm: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_default: str = ""  # 默认对话模型（空 = 按就绪状态自动挑）
    producers: dict[str, dict[str, Any]] = field(default_factory=dict)
    producers_disabled: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _config_file() -> Path:
    return data_dir() / "config.json"


def _as_port(value: Any) -> int:
    """端口：认不出 / 越界一律回 0（= 用默认值），不在启动时炸——配置可手改，不能拦住服务。"""
    text = str(value).strip() if value is not None else ""
    try:
        number = float(text)
    except ValueError:
        return 0
    if not number.is_integer():  # nan / inf / 1.5（空串在 float 那一步就抛了）
        return 0
    port = int(number)
    return port if 0 < port < 65536 else 0


def _as_count(value: Any, default: int) -> int:
    """非负整数；认不出（空 / 非数字 / 负数）回默认值。

    ``0`` 是**有意义**的（= 这条回收规则不启用），所以不能把 0 当"没填"。
    """
    if value is None or value == "":
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def resolve_port(cli_port: int | None = None) -> int:
    """这次启动监听哪个端口：``--port`` > ``VOICEHUB_PORT`` > ``config.json`` > 默认。

    顺序只写这一处；CLI 默认值必须是 ``None``，否则配置与环境变量失去生效机会。
    """
    if cli_port:
        return cli_port
    return _as_port(os.environ.get("VOICEHUB_PORT", "")) or _as_port(load_config().port) or DEFAULT_PORT


def _as_host(value: Any) -> str:
    """监听地址：去掉空白；认不出（含空格 / 分隔符）当"没说"。

    不在这里校验是不是本机网卡——那张表在运行时会变（插拔网卡、VPN），
    绑不上由 uvicorn 报，比启动前猜一遍更准。
    """
    text = str(value or "").strip()
    if not text or any(ch in text for ch in " \t/\\"):
        return ""
    return text


def resolve_host(cli_host: str | None = None) -> str:
    """这次启动监听哪个地址：``--host`` > ``VOICEHUB_HOST`` > ``config.json`` > 默认。

    与端口同一套优先级，理由也相同：三层散成三份就没人说得清"为什么还是连不上"。
    """
    if cli_host is not None and str(cli_host).strip():
        return _as_host(cli_host) or DEFAULT_HOST
    return _as_host(os.environ.get("VOICEHUB_HOST", "")) or _as_host(load_config().host) or DEFAULT_HOST


def is_loopback_host(host: str) -> bool:
    """这个监听地址是否只在本机可达。

    两处共用：https 的 ``auto`` 判据与 CORS 放开范围。绑通配地址与绑某张网卡的地址
    都算对外开放，绑 ``127.x`` / ``::1`` / ``localhost`` 算本机。
    """
    text = str(host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text in {"127.0.0.1", "localhost", "::1"} or text.startswith("127.")


def _as_https(value: Any) -> str:
    """https 三态。认不出的一律当 ``auto``：配置可手改，写坏了不该把服务拦在门外。

    布尔值与 ``1`` / ``0`` 一并认——早先只有一个环境变量开关，手写文件里出现得出来。
    """
    if isinstance(value, bool):
        return HTTPS_ON if value else HTTPS_OFF
    text = str(value or "").strip().lower()
    if text in {"on", "1", "true", "yes"}:
        return HTTPS_ON
    if text in {"off", "0", "false", "no"}:
        return HTTPS_OFF
    return HTTPS_AUTO


def resolve_https(cli: str | None = None, host: str = "") -> bool:
    """这次启动要不要 https：``--https`` / ``--no-https`` > ``VOICEHUB_HTTPS`` > ``config.json`` > 自动。

    与端口、监听地址同一套顺序，判据只写这一处。``auto`` 看监听地址：开放局域网就开
    ——浏览器只在 https 或 localhost 下把麦克风交给页面，局域网上不开等于关掉平板的
    录音入口；只监听回环则相反，给自己添一次证书告警没有好处。
    """
    mode = _as_https(cli) if cli else HTTPS_AUTO
    if mode == HTTPS_AUTO:
        mode = _as_https(os.environ.get("VOICEHUB_HTTPS", ""))
    if mode == HTTPS_AUTO:
        mode = _as_https(load_config().https)
    if mode == HTTPS_AUTO:
        return not is_loopback_host(host)
    return mode == HTTPS_ON


def load_config() -> Config:
    """读配置。文件不在 / 坏了 → 空配置（服务照样起得来，界面上再配目录）。

    按 ``mtime_ns + 大小`` 缓存解析结果：这份文件在每次请求里被问十几次，每次
    读盘 + 解析约 0.8 ms（实测），而它只在设置页保存时变化。

    命中缓存也返回**副本**：调用方会就地改字段再 ``save_config``（改扫描根、存引擎
    设置都是这个形状），共用同一个对象会让"A 还没保存的改动"出现在 B 的读里。
    """
    global _CONFIG_CACHE
    path = _config_file()
    stamp = file_stamp(path)
    if stamp is None:
        return Config()
    cached = _CONFIG_CACHE
    if cached is not None and cached[0] == stamp:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Config()
    # 旧版头像生成配置，已由公开图片来源替代。
    for obsolete in ("avatar_engine", "avatar_options"):
        raw.pop(obsolete, None)
    known = {
        "folders",
        "ffprobe_path",
        "output_dir",
        "cache_dir",
        "log_dir",
        "cache_limit_mb",
        "cache_keep_days",
        "engine_outputs",
        "channels",
        "asr_engine",
        "asr_options",
        "port",
        "host",
        "https",
        "access_token",
        "cors_origins",
        "engines",
        "engines_enabled",
        "llm",
        "llm_default",
        "producers",
        "producers_disabled",
    }
    raw_engines = raw.get("engines")
    engines = (
        {str(key): dict(value) for key, value in raw_engines.items() if isinstance(value, dict)}
        if isinstance(raw_engines, dict)
        else {}
    )
    # 对话模型同理：只收 dict，坏值留给 usecase 那一层的容错。
    raw_llm = raw.get("llm")
    llm = (
        {str(key): dict(value) for key, value in raw_llm.items() if isinstance(value, dict)}
        if isinstance(raw_llm, dict)
        else {}
    )
    raw_producers = raw.get("producers")
    producers = (
        {str(key): dict(value) for key, value in raw_producers.items() if isinstance(value, dict)}
        if isinstance(raw_producers, dict)
        else {}
    )
    raw_outputs = raw.get("engine_outputs")
    outputs = (
        {str(key): str(value).strip() for key, value in raw_outputs.items() if str(value).strip()}
        if isinstance(raw_outputs, dict)
        else {}
    )
    # 通道段只收 dict：坏值在 resolve_channel 那一层还有一道容错，这里先粗筛。
    raw_channels = raw.get("channels")
    channels = (
        {str(key): dict(value) for key, value in raw_channels.items() if isinstance(value, dict)}
        if isinstance(raw_channels, dict)
        else {}
    )
    # 启用清单：**键不在**与"空列表"不是一回事——前者是升级前的配置（按用过的推导），
    # 后者是用户真的把每一台都停用了。故先看键在不在，再收值。
    enabled: list[str] | None = None
    if "engines_enabled" in raw:
        raw_enabled = raw.get("engines_enabled")
        enabled = (
            [str(key).strip() for key in raw_enabled if str(key).strip()]
            if isinstance(raw_enabled, list)
            else []
        )
    raw_asr_options = raw.get("asr_options")
    asr_options = dict(raw_asr_options) if isinstance(raw_asr_options, dict) else {}
    config = Config(
        folders=[str(x).strip() for x in raw.get("folders", []) if str(x).strip()],
        ffprobe_path=str(raw.get("ffprobe_path", "") or "").strip(),
        output_dir=str(raw.get("output_dir", "") or "").strip(),
        cache_dir=str(raw.get("cache_dir", "") or "").strip(),
        log_dir=str(raw.get("log_dir", "") or "").strip(),
        cache_limit_mb=_as_count(raw.get("cache_limit_mb"), DEFAULT_CACHE_LIMIT_MB),
        cache_keep_days=_as_count(raw.get("cache_keep_days"), DEFAULT_CACHE_KEEP_DAYS),
        engine_outputs=outputs,
        channels=channels,
        asr_engine=str(raw.get("asr_engine", "") or "").strip(),
        asr_options=asr_options,
        port=_as_port(raw.get("port")),
        host=_as_host(raw.get("host")),
        https=_as_https(raw.get("https")),
        access_token=str(raw.get("access_token", "") or "").strip(),
        cors_origins=[
            str(origin).strip()
            for origin in raw.get("cors_origins", [])
            if str(origin).strip()
        ]
        if isinstance(raw.get("cors_origins"), list)
        else [],
        engines=engines,
        engines_enabled=enabled,
        llm=llm,
        llm_default=str(raw.get("llm_default", "") or "").strip(),
        producers=producers,
        producers_disabled=[
            str(key).strip()
            for key in raw.get("producers_disabled", [])
            if str(key).strip()
        ]
        if isinstance(raw.get("producers_disabled"), list)
        else [],
        extra={k: v for k, v in raw.items() if k not in known},
    )
    _CONFIG_CACHE = (stamp, config)
    return copy.deepcopy(config)


def save_config(config: Config) -> None:
    """写配置（原子写：临时文件 + rename，写一半断电不会留下半个 JSON）。"""
    global _CONFIG_CACHE
    path = _config_file()
    payload = {
        "folders": config.folders,
        "ffprobe_path": config.ffprobe_path,
        "output_dir": config.output_dir,
        "cache_dir": config.cache_dir,
        "log_dir": config.log_dir,
        "cache_limit_mb": config.cache_limit_mb,
        "cache_keep_days": config.cache_keep_days,
        "engine_outputs": config.engine_outputs,
        "channels": config.channels,
        "asr_engine": config.asr_engine,
        "asr_options": config.asr_options,
        "port": config.port,
        "host": config.host,
        "https": config.https,
        # 只有配了才写进文件：默认配置里不该出现一个空令牌键，看着像"设过又清空"。
        **({"access_token": config.access_token} if config.access_token else {}),
        "cors_origins": config.cors_origins,
        "engines": config.engines,
        # 没写过就不写：留着这个键缺失，升级后的推导才有机会生效。
        **({"engines_enabled": config.engines_enabled} if config.engines_enabled is not None else {}),
        "llm": config.llm,
        "llm_default": config.llm_default,
        "producers": config.producers,
        "producers_disabled": config.producers_disabled,
        **config.extra,
    }
    write_json(path, payload)
    # 文件时间戳理论上已经变了；显式失效一次，不赌文件系统的时钟精度。
    _CONFIG_CACHE = None
