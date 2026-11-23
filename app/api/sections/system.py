"""路由分区「system」：只注册本域的接口；路径前缀与错误约定见 :mod:`app.api.routes`。"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.auth import resolve_token
from app.api.clientaddr import is_local_host
from app.api.errors import ApiError
from app.api.localonly import require_local
from app.api.schemas import HealthOut, LogSnapshotOut
from app.api.views import (
    _STARTED,
    ClientLogIn,
    FoldersIn,
    ServiceIn,
    _engine_states,
    _vocab,
    bind_host,
    bind_port,
    bind_scheme,
)
from app.domain.request_spec import spec_payload
from app.infra import autostart, releases
from app.infra import secrets as secret_store
from app.infra.config import DEFAULT_HOST, WILDCARD_HOSTS, load_config, save_config
from app.infra.loghub import CHANNEL, HUB
from app.usecase import vocab as vocab_uc
from app.usecase.engines import marks_payload
from app.usecase.library import LibraryService

__all__ = ["register"]


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.get("/health", response_model=HealthOut, summary="健康检查", tags=["system"])
    def health() -> dict[str, Any]:
        """存活探针。带版本号与各引擎就绪状态，供上游做能力协商。

        ``lan_open`` = 监听在非回环地址且没设访问令牌。设置页的「网络访问」
        据此标出「同网段都能操作」，不把这件事藏进启动日志里。
        """
        host = bind_host()
        return {
            "status": "ok",
            "service": "voicehub",
            "version": version,
            "uptime_s": int(time.monotonic() - _STARTED),
            "engines": _engine_states(),
            "lan_open": bool(host) and not is_local_host(host) and not resolve_token(),
        }

    @router.get("/service", summary="服务监听、https 与令牌", tags=["system"])
    def service_state() -> dict[str, Any]:
        """当前**生效**的监听地址、协议与令牌状态。

        地址与协议取自实际 bind 值而非配置：``--host`` / ``--https`` 会盖掉
        ``config.json``，证书签不出来时还会回落 http，读配置答不准。
        改这三项要重启进程，所以这里只回答"现在是什么"，不回答"配置里写了什么"。
        """
        host = bind_host() or DEFAULT_HOST
        config = load_config()
        return {
            "object": "service",
            "host": host,
            "port": bind_port(),
            "scheme": bind_scheme(),
            # `lan` / `scheme` 是现在生效的；`config_lan` / `https` 是配置里写的
            # （重启后才兑现）。两者不同时界面要如实说"改了但还没重启"，不能只报一个数。
            "lan": not is_local_host(host),
            "config_lan": config.host in WILDCARD_HOSTS,
            "https": config.https,
            "token_set": bool(resolve_token()),
            # 开机自启只由桌面启动器提供；源码态没有可执行的自启命令。
            "autostart_supported": autostart.supported(),
            "autostart_enabled": autostart.enabled(),
        }

    @router.put("/service", summary="保存监听范围、https、令牌与自启", tags=["system"])
    def save_service(request: Request, body: ServiceIn) -> dict[str, Any]:
        """按提交的字段落盘：没给的字段不动。

        监听地址、https 与令牌要**重启进程**才兑现；开机自启是注册表状态，立刻生效。
        所以 `restart_required` 只跟着前三项走，界面据此提示。
        """
        require_local(request)
        config = load_config()
        restart = False
        if body.lan is not None:
            config.host = "0.0.0.0" if body.lan else ""
            restart = True
        if body.https is not None:
            config.https = body.https
            restart = True
        if body.access_token is not None:
            config.access_token = secret_store.seal(body.access_token.strip())
            restart = True
        if restart:
            save_config(config)

        # 自启不进 config.json：它是这台机器上的注册表状态，随启动器路径走。
        if body.autostart is not None:
            if not autostart.supported():
                raise ApiError(
                    status=400,
                    code="autostart_unsupported",
                    message="开机自启只在桌面版可用：当前进程不是由启动器拉起的。",
                )
            if not autostart.set_enabled(body.autostart):
                raise ApiError(
                    status=500,
                    code="autostart_failed",
                    message="写注册表失败，详见日志。",
                )

        if restart:
            CHANNEL.info(
                "监听范围、https 与令牌已保存，重启服务后生效。",
                source="config",
                lan=config.host in WILDCARD_HOSTS,
                https=config.https,
                token_set=bool(config.access_token),
            )
        return {
            "object": "service",
            "lan": config.host in WILDCARD_HOSTS,
            "https": config.https,
            "token_set": bool(config.access_token),
            "autostart_enabled": autostart.enabled(),
            "restart_required": restart,
        }

    @router.get("/release", summary="检查最新正式发布", tags=["system"])
    def release_check(refresh: bool = False) -> dict[str, Any]:
        """只读查询 GitHub 上的最新**正式发布**。

        只回答「有没有新发布」，不下载、不替换文件。``refresh`` 跳过一分钟缓存；
        网络不通、限流与仓库还没有发布都算正常结果，放在响应的 ``error`` 里。
        """
        return releases.check(version, refresh=refresh)

    # ---- 日志 ------------------------------------------------------------
    #
    # 两个入口，职责不同：
    # - 快照：首屏与断线重连（带上次的 seq）；
    # - SSE：仅在界面打开时维持实时。两者行形状一致，前端按 seq 去重。

    @router.get("/logs", response_model=LogSnapshotOut, summary="日志快照", tags=["system"])
    def logs(since: int = 0, limit: int = 200) -> dict[str, Any]:
        """``since`` 之后的日志（正序），附当前游标。

        ``dropped`` 表示环形缓冲已越过 ``since``，中间缺了一段——必须如实告知，
        不能让调用方误以为日志连续。
        """
        return {"object": "log_snapshot", **HUB.snapshot(since=since, limit=limit)}

    @router.post("/logs", summary="前端提示（写入日志）", tags=["system"])
    def write_log(body: ClientLogIn) -> dict[str, Any]:
        """界面上的 toast 也进日志链路：用户看得见一次，日志里留得住一次。"""
        message = (body.message or "").strip()[:500]
        if message:
            level = body.level if body.level in {"debug", "info", "warning", "error"} else "info"
            getattr(CHANNEL, level)(message, source=(body.source or "ui").strip()[:32] or "ui")
        return {"ok": True}

    @router.get("/logs/stream", summary="日志实时流（SSE）", tags=["system"])
    async def logs_stream(since: int = 0) -> StreamingResponse:
        """SSE 实时流。浏览器自动重连，重连时带 ``since`` 补帧。"""

        async def frames():
            # 先发注释帧以立刻送出响应头；否则要等到第一条日志才建立连接，
            # 而"刚打开日志栏"往往正是最安静的时刻。
            yield ": connected\n\n"
            async for line in HUB.stream(since=since):
                yield f"event: log\ndata: {json.dumps(line, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # 关闭中间层缓冲：否则日志会积批出现，看起来像卡住。
                "X-Accel-Buffering": "no",
            },
        )


    # ---- 元数据（自描述载荷）--------------------------------------------
    #
    # /tree、/spec、/marks、/engines 的 schema 是运行时数据：由插件声明或由规范表
    # 推导。此处不为它们建模（写死一份等于抄第二份，且必然漂移）。

    @router.get("/tree", summary="左栏结构", tags=["library"])
    def tree() -> dict[str, Any]:
        """根目录 → 分类树 → 音色数，附引擎段与标签词表。"""
        return {"object": "tree", **service.tree(), "vocab": _vocab()}

    @router.get("/spec", summary="合成参数规范", tags=["synthesis"])
    def spec() -> dict[str, Any]:
        """参数总表（分组、单位、区间、控件类型）。界面据此渲染，不硬编码字段。

        情绪候选随用户词表走（设置页「词表」可改）：默认 18 词（基础 + 复合）+ 用户自定义、
        减去被藏起的。词表外的值照旧原样发——候选不是白名单。
        """
        return {"object": "spec", **spec_payload(vocab_uc.emotions())}

    @router.get("/marks", summary="语气词分组", tags=["synthesis"])
    def marks() -> dict[str, Any]:
        """语气词标记的分组（通用 / 引擎专有），由跨引擎计算得出。"""
        return {"object": "marks", **marks_payload()}


    # ---- 扫描根 ----------------------------------------------------------

    @router.get("/folders", summary="读取扫描根", tags=["library"])
    def folders() -> dict[str, Any]:
        return {"object": "folder_list", "folders": service.folders()}

    @router.put("/folders", summary="保存扫描根并重扫", tags=["library"])
    def save_folders(body: FoldersIn) -> dict[str, Any]:
        return {"object": "scan_result", **service.save_folders(body.folders)}

    @router.post("/scan", summary="重新扫描（后台执行）", tags=["library"])
    def scan() -> dict[str, Any]:
        """起一次后台扫描并立刻返回进度。

        以前这里同步扫完才返回：大库要几十秒，请求线程被占着、界面只能干等。
        现在返回任务快照（``state: running``），用 ``GET /scan/status`` 轮询，
        扫完那一次的状态里带 ``result``（与旧的扫描结果同形）。
        """
        return {"object": "scan_job", **service.start_scan()}

    @router.get("/scan/status", summary="扫描进度", tags=["library"])
    def scan_status() -> dict[str, Any]:
        """最近一次后台扫描的进度；没扫过 → ``state: idle``。"""
        return {"object": "scan_job", **service.scan_status()}

