"""VoiceHub 应用入口。起服务：``python -m app.main``（默认 ``127.0.0.1:17865``）。

端口解析优先级 ``--port`` > ``VOICEHUB_PORT`` > ``config.json``，监听地址同构：
``--host`` > ``VOICEHUB_HOST`` > ``config.json`` 的 ``host``；两处都只在
:mod:`app.infra.config` 解析，默认只绑回环。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import uuid
import webbrowser
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.api.clientaddr import is_local_host
from app.api.errors import install_error_handlers
from app.api.routes import build_router
from app.infra import recordings
from app.infra.config import (
    DEFAULT_PORT,
    WILDCARD_HOSTS,
    data_dir,
    load_config,
    resolve_host,
    resolve_port,
)
from app.infra.db import HubDB
from app.infra.instancelock import InstanceLock
from app.infra.loghub import CHANNEL, install_logging
from app.infra.requestctx import REQUEST_ID
from app.infra.runtime import data_path
from app.usecase import asr as asr_usecase
from app.usecase.library import LibraryError, LibraryService

_STATIC = data_path("web/dist")

#: 服务版本。同时出现在 ``/health`` 与 ``/openapi.json``，供上游做能力协商。
VERSION = "0.1.0"

#: 开放局域网时额外允许的源：同网段的私有地址（本机 UI 与 API 同源，不需要 CORS；
#: 这一条是给"另一台机器上的前端直接调这台 hub"用的）。
_LAN_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost|127\.0\.0\.1|\[::1\]|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(?::\d+)?$"
)


#: 只给**非本机**来源压缩的中间件。
#:
#: 本机请求走回环，几百 KB 的前端包也是毫秒级，压一遍纯属白烧 CPU；真正需要压缩的
#: 是局域网 / Wi-Fi 上那台机器——同一份包 gzip 后约剩四分之一。``/login`` 与静态资源
#: 都走这条路，和令牌无关。
class _LanGzip:
    """"本机不压、外网压"的 gzip 闸门（纯 ASGI，不经过 ``Request``）。"""

    #: 太小压缩不划算（握手与分块的开销比省下的字节还多）。
    MIN_SIZE = 1024

    def __init__(self, app: Any) -> None:
        self._app = app
        # 6 档：JS 的体积收益与 9 档几乎一样，耗时少一半。
        self._gzip = GZipMiddleware(app, minimum_size=self.MIN_SIZE, compresslevel=6)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and _needs_compression(scope):
            await self._gzip(scope, receive, send)
        else:
            await self._app(scope, receive, send)


def _needs_compression(scope: dict) -> bool:
    """这次请求要不要压：只看来源——本机不压，其它都压。

    流式响应（``text/event-stream``、``audio/*``、图片）由 ``GZipMiddleware`` 的
    默认排除表挡住，不必按路径再列一遍：那条日志流一旦被 gzip 攒块，就会卡住不动，
    而它全部的价值就在"立刻看到"。
    """
    client = scope.get("client")
    return not is_local_host(client[0] if client else "")


def _quiet_proactor_noise() -> None:
    """把 Windows Proactor 的"连接被对端关掉"降成 debug——它**不是**请求失败。

    症状是一条 ``[ERROR] asyncio Exception in callback
    _ProactorBasePipeTransport._call_connection_lost(None)``，通常比某个请求晚十几秒
    出现（浏览器刷新 / 关标签 / 长连接被重置时，asyncio 收尾那条 socket 报的）。
    同一时刻的请求早已返回，日志里能对得上：合成完成在前，这条在后。

    留着它当 ERROR 的坏处是**真问题会被淹掉**：用户会照着这条去查合成为什么慢。
    换成 debug 之后它仍在（排查底层连接时看得到），只是不再冒充故障。
    其余异常原样交给默认处理器与上一个处理器，一条都不吞。
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    logger = logging.getLogger("asyncio")

    def handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        message = str(context.get("message") or "")
        if "_ProactorBasePipeTransport._call_connection_lost" in message:
            logger.debug("连接收尾（对端已关闭，非请求失败）：%s", message)
            return
        if previous is not None:
            previous(_loop, context)
        else:
            _loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动时做两件进程级的事：清临时录音、给事件循环装噪音过滤。"""
    _quiet_proactor_noise()
    yield


def _cors_origins() -> list[str]:
    """跨源来源只取 ``config.json`` 的 ``cors_origins``，去重保序。

    本机界面由本进程托管、同源，不需要 CORS；新产品接入改配置，不改代码。
    """
    extra = [origin for origin in load_config().cors_origins if origin.strip()]
    return list(dict.fromkeys(extra))


#: 只对本机可达的监听地址：这些值下不放开私有网段的 CORS。
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK


def _lan_addresses() -> list[str]:
    """本机的局域网 IPv4（去重、跳过回环）。取不到给空表——地址只用于提示，不是启动条件。"""
    found: list[str] = []
    try:
        resolved = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        resolved = []
    for _family, _type, _proto, _canon, sockaddr in resolved:
        address = sockaddr[0]
        if not address.startswith("127.") and address not in found:
            found.append(address)
    try:
        # 不产生流量：connect 一个 UDP 套接字只用于让系统选出默认出口网卡的地址。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            primary = probe.getsockname()[0]
        if not primary.startswith("127.") and primary not in found:
            found.insert(0, primary)
    except OSError:
        pass
    return found


def create_app(*, host: str | None = None) -> FastAPI:
    # 必须先装日志：启动阶段的 warning 才能进入界面。
    install_logging()

    if _LOCKED_OUT:
        CHANNEL.warning(
            f"数据目录已被另一个实例占用（{data_dir()}）：本次不启动生产方服务。",
            source="app",
        )

    # 旧配置里的明文密钥在服务起来时换成 DPAPI 密文；失败不拦住启动。
    from app.usecase.migrate_secrets import migrate as migrate_secrets

    try:
        migrate_secrets()
    except Exception as exc:  # noqa: BLE001 - 配置迁移是尽力而为
        CHANNEL.warning(f"明文密钥迁移失败，配置保持原样：{exc}", source="config")

    app = FastAPI(
        lifespan=_lifespan,
        title="VoiceHub",
        version=VERSION,
        description=(
            "本地音色库与语音合成中转服务。接口契约见 docs/request-spec.md；"
            "错误体统一为 {\"error\": {message, type, code, param, request_id}}。"
        ),
        openapi_tags=[
            {"name": "system", "description": "健康检查与日志"},
            {"name": "library", "description": "音色、样例与平台音色索引"},
            {"name": "engines", "description": "引擎插件、配置与音色通道"},
            {"name": "llm", "description": "对话模型插件、配置与补全（与合成引擎同构、不同契约）"},
            {"name": "producers", "description": "生产方插件：流水线工作区与其接口"},
            {"name": "synthesis", "description": "合成、产物与目录配置"},
            {"name": "asr", "description": "语音识别档位、转写与麦克风语音输入"},
            {"name": "texts", "description": "文案库：生成样例时挑一句现成的（分组，可在设置页改）"},
        ],
    )
    install_error_handlers(app)

    # 令牌闸门要在**最内层**（先注册 = 内层）：它得跑在请求上下文之后，
    # 401 才带得上 X-Request-Id；CORS 在外层，预检请求因此不经过鉴权。
    from app.api.auth import install_token_guard, login_routes

    install_token_guard(app)
    login_routes(app)

    @app.middleware("http")
    async def _request_context(request, call_next):
        """为每次请求建立标识并回显到响应头。

        客户端自带 ``X-Request-Id`` 时沿用，否则生成；同时写入错误体与日志。
        """
        incoming = request.headers.get("X-Request-Id", "").strip()
        request_id = incoming[:64] if incoming else f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        token = REQUEST_ID.set(request_id)
        try:
            response = await call_next(request)
        finally:
            REQUEST_ID.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.middleware("http")
    async def _cache_headers(request, call_next):
        """静态资源缓存：首页 no-cache，``/assets/`` 长缓存 immutable。

        ``index.html`` 一旦被浏览器缓存，会钉住旧的带哈希 JS 包名，表现为更新后界面无变化。
        """
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    bind_host = host or resolve_host(None)
    # `/health` 的「局域网开放」按实际监听地址判：`--host` 会盖掉配置，现读配置不准。
    from app.api.views import set_bind_host

    set_bind_host(bind_host)
    # 压缩装在 CORS 内侧：预检请求不必压，正常响应压完再过 CORS 加头。
    app.add_middleware(_LanGzip)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        # 只绑回环时不放开私有网段：那会让局域网里的一页读到本机接口的响应。
        allow_origin_regex=(None if _is_loopback(bind_host) else _LAN_ORIGIN_REGEX),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    # 录音是"这一次用哪段声音"的临时产物：上一次跑留下的那些已经没有请求会指向它们，
    # 而界面上没有入口能删（不在扫描范围内），所以**启动时清一次**。
    recordings.clear()

    db = HubDB(data_dir() / "hub.db")
    service = LibraryService(db)
    app.state.library = service
    router = build_router(service, version=VERSION)
    app.include_router(router, prefix="/api/v1")

    # 「连接时自动识别」：启动后等扫描把样例写进库，再补一批逐字稿。
    # 开关关着时这里直接返回，不起线程。
    asr_usecase.maybe_fill(service, delay=6)

    # 生产方插件自带路由：挂在自己的前缀下，宿主不接管其内部契约。
    from app.producers.registry import discover as discover_producers
    from app.usecase.producers import is_enabled, reset_runtimes, runtime_for

    # 建 app = 新一轮：旧实例（可能是上一个测试数据目录的）一律收掉。
    reset_runtimes()
    plugins = list(discover_producers())
    for plugin in plugins:
        runtime = runtime_for(plugin)
        app.include_router(
            plugin.router(runtime),
            prefix=f"/api/v1/producers/{plugin.key}",
        )
        # 停用的插件到这里为止：构造函数里可能有自动启动（audio.cpp 会拉起实例），
        # 先构造再拦等于没停。路由挂着只为运行期启用；停用期间访问由代理回一句话。
        # 没拿到数据目录锁（已有实例在跑）时也不起：第二个进程只该退出，不该抢占
        # 同一份库、拉起第二套本机服务。
        if not _LOCKED_OUT and is_enabled(plugin.key):
            runtime.get()
    # 扫描后置任务：有声书把目录移动后失效的本地音色引用按唯一同名重绑回项目。
    for plugin in plugins:
        after_scan = getattr(plugin, "after_library_scan", None)
        if not callable(after_scan):
            continue
        service.add_scan_hook(
            lambda plugin=plugin, after_scan=after_scan: after_scan(
                runtime_for(plugin), service
            )
        )
    if db.rebuild_needed:
        # 刚补上「平台音色归属」那一列：老行得重读一次 sidecar 才有值，
        # 否则平台音色要等到用户哪天手动重扫才"有可以播的样例"。后台跑，不挡启动。
        with contextlib.suppress(LibraryError):  # 已经在扫（409）就让它扫完
            service.start_scan()
    if _STATIC.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="web")
    else:
        # 缺少 web/dist 时接口仍全部可用，仅界面为空白页。
        print(
            "[VoiceHub] 缺少 web/dist，界面不可用（接口不受影响）。"
            "请运行 scripts/ensure_web.py 构建一次。",
            flush=True,
        )
    return app


#: 同一份数据目录只允许一个服务进程。锁在导入时就要拿：``create_app()`` 会构造
#: 生产方服务（有声书打开 ``projects.db``、audio.cpp 可能拉起实例），等端口检查
#: 到再来已经晚了。锁由操作系统持有，进程退出即释放。
_LOCK = InstanceLock(data_dir() / "service.lock")
_HAVE_LOCK = _LOCK.acquire()
#: 只有"锁被别人拿着"才是第二个实例。数据目录不可写导致的拿锁失败不拦启动。
_LOCKED_OUT = not _HAVE_LOCK and _LOCK.busy

app = create_app()


def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被监听，把绑定失败变成可读说明而非 traceback。"""

    probe_host = "127.0.0.1" if host in WILDCARD_HOSTS else host
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((probe_host, port)) == 0


def main() -> None:
    import uvicorn

    if _LOCKED_OUT:
        owner = _LOCK.owner_pid()
        print(
            f"[VoiceHub] 数据目录已被另一个实例占用：{data_dir()}"
            + (f"（进程 {owner}）" if owner else ""),
            flush=True,
        )
        print("[VoiceHub] 同一份数据只能开一个服务；先结束旧进程再启动。", flush=True)
        return

    parser = argparse.ArgumentParser(description="VoiceHub 音色库服务")
    # 默认值必须是 None：否则环境变量与配置文件都失去生效机会（顺序见 resolve_port）。
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（默认 {DEFAULT_PORT}，也可用环境变量 VOICEHUB_PORT）",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "监听地址（默认 127.0.0.1，仅本机）。"
            "0.0.0.0 = 开放局域网，也可用环境变量 VOICEHUB_HOST 或 config.json 的 host"
        ),
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = resolve_port(args.port)
    host = resolve_host(args.host)
    # `/service` 回答"现在生效什么"：端口与地址一样，`--port` 会盖掉配置，
    # 必须在绑定之前把实际值记进 HTTP 层，现读配置会答错。
    from app.api.views import set_bind_port

    set_bind_port(port)
    wildcard = host in WILDCARD_HOSTS
    browser_host = "127.0.0.1" if wildcard else ("[::1]" if host == "::1" else host)
    url = f"http://{browser_host}:{port}/"
    if _port_in_use(host, port):
        CHANNEL.warning(
            f"端口 {port} 已被占用——多半是服务已经在跑：{url}", source="app", port=port
        )
        print(f"[VoiceHub] 端口 {port} 已被占用——服务可能已在运行：{url}", flush=True)
        print("[VoiceHub] 重启需先结束旧进程；换端口可任选其一：", flush=True)
        print(f"[VoiceHub]   · 命令行加 --port {port + 1}", flush=True)
        print(f"[VoiceHub]   · 设环境变量 VOICEHUB_PORT={port + 1}", flush=True)
        print("[VoiceHub]   · 改 config.json 里的 \"port\"（在服务数据目录下）", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return

    CHANNEL.info(
        f"服务启动：{url}",
        source="app",
        port=port,
        host=host,
        data_dir=str(data_dir()),
    )
    print(f"[VoiceHub] 运行中：{url}", flush=True)
    from app.api.auth import resolve_token

    token_on = bool(resolve_token())
    if wildcard:
        for address in _lan_addresses():
            print(f"[VoiceHub] 局域网：http://{address}:{port}/", flush=True)
        if token_on:
            print("[VoiceHub] 已启用访问令牌：其它设备首次打开要在 /login 输入。", flush=True)
        else:
            print(
                "[VoiceHub] 注意：接口无身份校验，仅限可信网络使用；"
                "要加锁可在 config.json 里设 access_token。",
                flush=True,
            )
    elif not _is_loopback(host):
        print(f"[VoiceHub] 监听：{url}", flush=True)
        if token_on:
            print("[VoiceHub] 已启用访问令牌：其它设备首次打开要在 /login 输入。", flush=True)
        else:
            print(
                "[VoiceHub] 注意：接口无身份校验，仅限可信网络使用；"
                "要加锁可在 config.json 里设 access_token。",
                flush=True,
            )
    if not args.no_browser:
        webbrowser.open(url)

    # 只在"回环与否"跟模块级 app 不一致时重建：CORS 的放行范围由它决定。
    server = (
        app
        if _is_loopback(host) == _is_loopback(resolve_host(None))
        else create_app(host=host)
    )
    try:
        uvicorn.run(server, host=host, port=port, log_level="warning")
    finally:
        CHANNEL.info("服务退出。", source="app")


if __name__ == "__main__":
    main()
