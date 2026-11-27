"""路由分区「producers」：只注册本域的接口；路径前缀与错误约定见 :mod:`app.api.routes`。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.api.embeddings import build_embedding_router
from app.api.errors import ApiError, not_found
from app.api.localonly import require_local
from app.api.schemas import DeleteOut, OutputOut, StorageOut
from app.api.views import (
    OutputIn,
    StorageIn,
    StorageOpenIn,
    _absolute_dir,
    _cache_number,
    _cache_policy,
    _storage_entries,
)
from app.infra import archive
from app.infra import migrate as migrate_mod
from app.infra.config import data_dir, load_config, update_config
from app.infra.loghub import install_logging
from app.infra.media import audio_mime
from app.infra.shell import open_folder
from app.infra.trash import TrashError
from app.usecase import gateway
from app.usecase import producers as producer_uc
from app.usecase.engines import preview_engine_output_dir, save_engine_output_dir
from app.usecase.library import LibraryError, LibraryService

__all__ = ["register"]


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    # ---- 朗读引擎（第三方阅读 App）-------------------------------------
    #
    # 「阅读」这类 App 的 HTTP 朗读引擎只会「给一段字、要一段音频」，注入方式是
    # URL 模板或表单，发不了本项目的 JSON 请求体。这一层只做形状转换：合成仍走
    # gateway，产物不落盘，响应体就是音频字节（与 persist="none" 同一条路）。

    async def _tts_voice(raw: str) -> str:
        """``voice_id`` 也认音色名：本地音色的 id 是路径散列，搬目录或改扫描根就会变，
        名字通常不变。名字必须精确匹配且唯一，否则让调用方继续用 id。

        平台音色带 ``:``（本地文件名里不可能有），照旧交给网关切。
        """
        try:
            await asyncio.to_thread(service.get_voice, raw)
            return raw
        except LibraryError:
            if ":" in raw:
                return raw
        found = await asyncio.to_thread(service.list_voices, q=raw)
        exact = [voice for voice in found if raw in {voice.name, voice.shown_name}]
        if len(exact) == 1:
            return exact[0].id
        if not exact:
            raise ApiError(
                status=400,
                code="tts_voice_not_found",
                message=f"找不到音色「{raw}」。可以填 id 或音色名，名单见 /api/v1/voices。",
            )
        raise ApiError(
            status=400,
            code="tts_voice_ambiguous",
            message=f"有 {len(exact)} 条音色都叫「{raw}」，请改用 voice_id。",
        )

    async def _tts_params(request: Request) -> dict[str, str]:
        """GET 取 query；POST 兼容表单、JSON 与裸 ``a=b&c=d``。

        有的 App 发 POST 却不带 Content-Type，按表单解析比直接 400 有用。
        """
        params = {key: value for key, value in request.query_params.items()}
        if request.method != "POST":
            return params
        raw = await request.body()
        if not raw:
            return params
        if "json" in request.headers.get("content-type", "").lower():
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(
                    status=400, code="tts_body_invalid", message="请求体不是合法 JSON。"
                ) from exc
            if isinstance(payload, dict):
                params.update({str(k): str(v) for k, v in payload.items() if v is not None})
            return params
        try:
            body = raw.decode("utf-8")
            pairs = parse_qs(
                body, keep_blank_values=True, encoding="utf-8", errors="strict"
            )
        except UnicodeDecodeError as exc:
            raise ApiError(
                status=400,
                code="tts_body_encoding",
                message="请求体不是 UTF-8 文本。",
            ) from exc
        params.update({key: values[0] for key, values in pairs.items()})
        return params

    @router.get("/tts", summary="朗读引擎接口（GET）", tags=["synthesis"])
    @router.post("/tts", summary="朗读引擎接口（POST）", tags=["synthesis"])
    async def tts(request: Request) -> Response:
        """给第三方阅读 App 直接朗读的一层薄适配。

        参数：``text`` / ``engine`` / ``voice_id`` 必填；``emotion`` / ``format``
        可选；``speak_speed`` 按「阅读」的刻度收（语速 × 10，10 = 原速）；
        ``instruction``（引导指令）与 ``guidance_scale``（指令强度 0–1）原样
        走 ``extra``，认不认由引擎的映射表决定（BreezeTTS 两格都认）。
        """
        params = await _tts_params(request)
        text = (params.get("text") or "").strip()
        engine = (params.get("engine") or "").strip()
        voice_id = (params.get("voice_id") or "").strip()
        if not text:
            raise ApiError(status=400, code="tts_text_required", message="要念的正文是空的。")
        if not engine or not voice_id:
            raise ApiError(
                status=400,
                code="tts_target_required",
                message="engine 与 voice_id 都要给。",
            )
        extra: dict[str, Any] = {}
        raw_speed = (params.get("speak_speed") or "").strip()
        if raw_speed:
            try:
                # App 的滑杆能到 0 或 30 以上：夹进规范区间，别让越界值打断朗读。
                extra["speed"] = min(2.0, max(0.5, float(raw_speed) / 10.0))
            except ValueError as exc:
                raise ApiError(
                    status=400, code="tts_speed_invalid", message="speak_speed 要是数字。"
                ) from exc
        instruction = (params.get("instruction") or "").strip()
        if instruction:
            extra["instruction"] = instruction
        raw_guidance = (params.get("guidance_scale") or "").strip()
        if raw_guidance:
            try:
                guidance = float(raw_guidance)
            except ValueError as exc:
                raise ApiError(
                    status=400,
                    code="tts_guidance_invalid",
                    message="guidance_scale 要是 0–1 的数字。",
                ) from exc
            if not 0.0 <= guidance <= 1.0:
                raise ApiError(
                    status=400,
                    code="tts_guidance_range",
                    message="guidance_scale 的范围是 0–1（0.5 相当于 BreezeTTS 的 CFG 4）。",
                )
            extra["guidance_scale"] = guidance
        # 音色解析要读库（同步 IO）：放在纯参数校验之后，形状错了不必先查一遍库。
        voice_id = await _tts_voice(voice_id)
        result = await gateway.synthesize(
            service,
            engine_key=engine,
            voice_id=voice_id,
            text=text,
            emotion=(params.get("emotion") or "").strip(),
            format=(params.get("format") or "").strip(),
            extra=extra,
        )
        if not result.audio:
            raise ApiError(
                status=502, code="engine_no_audio", message="引擎未返回音频。", type="engine_error"
            )
        return Response(
            content=result.audio,
            media_type=audio_mime(result.format),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/generated/{name:path}", summary="取回生成音频", tags=["synthesis"])
    def generated(name: str, engine: str = "") -> FileResponse:
        """按相对路径取回音频。

        路径穿越一律 404，不返回"路径不合法"——后者会泄露目录结构。
        带 ``engine`` 时先查该引擎的保存目录与缓存目录，否则按候选根依次尝试。
        """
        from app.usecase.engines import require_engine

        roots = []
        if engine.strip():
            try:
                plugin = require_engine(engine.strip())
                roots = [
                    archive.engine_root(engine.strip(), plugin.label, create=False),
                    archive.cache_root(engine.strip(), plugin.label, create=False),
                ]
            except Exception:  # noqa: BLE001 - 引擎不存在时退回候选根
                roots = []
        for root in [*roots, *archive.candidate_roots()]:
            resolved_root = root.resolve()
            target = (resolved_root / name).resolve()
            if target.is_relative_to(resolved_root) and target.is_file():
                return FileResponse(str(target))
        raise not_found("not_found", "音频文件不存在，可能已被清理。")

    @router.delete(
        "/generated/{name:path}",
        response_model=DeleteOut,
        summary="删除生成音频",
        tags=["synthesis"],
    )
    def delete_generated(name: str, engine: str = "") -> dict[str, Any]:
        """把生成页跑出来的一条音频移进回收站（可还原）。

        ``name`` 与取回音频时是同一段相对路径（``引擎/音色/文件``）。删的是**缓存里
        那一份**：音色保存目录里的样例不归这里，那条要走「移除样例」（它还要动库里的
        记录）。``engine`` 必填——缓存路径的第一段按它核对，缺了就没法确认删的是谁家的。
        """
        from app.usecase.engines import EngineConfigError, require_engine

        key = engine.strip()
        if not key:
            raise ApiError(
                status=400, code="engine_required", message="删除生成音频需要指明引擎。"
            )
        try:
            plugin = require_engine(key)
        except EngineConfigError:
            raise not_found("not_found", "音频文件不存在，可能已被清理。") from None
        target = archive.generated_cache_file(name, plugin.label)
        if target is None or not target.is_file():
            raise not_found("not_found", "音频文件不存在，可能已被清理。")
        try:
            archive.discard_audio(target)
        except TrashError as exc:
            # 与「移除样例」同一条兜底：有的环境文件已经进了回收站却回一个错误码。
            if target.exists():
                raise ApiError(status=500, code="trash_failed", message=str(exc)) from exc
        return {"id": name, "object": "generated", "deleted": True, "trashed": [str(target)]}

    @router.get("/output", response_model=OutputOut, summary="读取生成目录", tags=["synthesis"])
    def output() -> dict[str, Any]:
        """全局生成目录与各引擎的覆盖目录。"""
        config = load_config()
        return {
            "object": "output_config",
            "dir": str(archive.default_root()),
            "custom": bool(config.output_dir.strip()),
            "engines": archive.configured_dirs(),
        }

    @router.post("/output/preview", summary="预览全局目录迁移", tags=["synthesis"])
    def preview_output(body: OutputIn) -> dict[str, Any]:
        """改全局生成目录前看一眼：旧目录里有没有东西要一起搬（不碰文件）。"""
        previous = str(archive.default_root())
        target = body.dir.strip() or str(data_dir() / "generated")
        if previous == target:
            return {"object": "output_preview", "needed": False, "reason": ""}
        plan = migrate_mod.plan_move(Path(previous), Path(target))
        return {
            "object": "output_preview",
            "needed": not plan.empty,
            "reason": plan.note,
            "source": plan.source,
            "target": plan.target,
            "entries": plan.entries,
            "audio": plan.audio,
            "bytes": plan.bytes,
        }

    @router.put("/output", response_model=OutputOut, summary="保存生成目录", tags=["synthesis"])
    def save_output(body: OutputIn, request: Request) -> dict[str, Any]:
        """修改全局生成目录。**不校验存在**：目录可能位于未挂载的移动盘。

        该设置只决定写入位置，不触发重扫。

        ``migrate=true`` 时把旧目录里的东西搬到新目录（界面先用
        ``POST /output/preview`` 问过用户）；默认不搬——改一项配置不该挪用户文件。

        只认本机来源：``migrate`` 会移动并删除源目录，是数据破坏面。
        """
        require_local(
            request,
            code="output_local_only",
            message="生成目录只能在本机修改：改目录并迁移会移动、删除旧目录里的文件。",
        )
        previous = str(archive.default_root())
        # 与 /storage 同口径：绝对路径、且不是盘根/系统目录。
        new_output = _absolute_dir(body.dir, "生成结果目录")
        update_config(lambda cfg: setattr(cfg, "output_dir", new_output))
        target = str(archive.default_root())
        moved: dict[str, Any] | None = None
        if body.migrate and previous and previous != target:
            plan = migrate_mod.plan_move(Path(previous), Path(target))
            if plan.entries:
                moved = asdict(migrate_mod.apply_move(plan))
        payload = output()
        payload["previous_dir"] = previous if previous != target else ""
        payload["moved"] = moved
        return payload

    @router.post("/engines/{key}/output/preview", summary="预览目录迁移", tags=["synthesis"])
    def preview_engine_output(key: str, body: OutputIn) -> dict[str, Any]:
        """保存前预览：本次修改是否需要迁移旧目录内容（不触碰文件）。"""
        return {
            "object": "output_preview",
            **preview_engine_output_dir(key, body.dir),
        }

    @router.put("/engines/{key}/output", summary="保存引擎生成目录", tags=["synthesis"])
    def save_engine_output(key: str, body: OutputIn, request: Request) -> dict[str, Any]:
        """修改该引擎的音色保存目录；``migrate=true`` 时迁移旧目录内容。

        空串表示恢复默认（全局根下的引擎子目录）。只认本机来源，理由同 ``/output``。
        """
        require_local(
            request,
            code="output_local_only",
            message="引擎保存目录只能在本机修改：迁移会移动、删除旧目录里的文件。",
        )
        return {
            "object": "engine_output",
            **save_engine_output_dir(
                key, _absolute_dir(body.dir, "引擎保存目录"), migrate=body.migrate
            ),
        }

    # ---- 存储位置 --------------------------------------------------------

    @router.get("/storage", response_model=StorageOut, summary="存储位置与占用", tags=["synthesis"])
    def storage() -> dict[str, Any]:
        """四处落盘位置 + 转换产物的回收线。"""
        return {"object": "storage", "entries": _storage_entries(), "cache": _cache_policy()}

    @router.put("/storage", response_model=StorageOut, summary="保存存储位置", tags=["synthesis"])
    def save_storage(body: StorageIn, request: Request) -> dict[str, Any]:
        """只改传了的项；空串 = 回默认；相对路径一律 400。

        缓存 / 日志要当场建得出来——建不出来（非法字符、盘不在）回 400
        并把系统那句话带上，而不是等第一次写日志时才 500。
        生成结果目录不校验存在：它可能指在未挂载的移动盘上，这条与 ``/output`` 一致。
        日志目录改完立即生效（文件落点跟着换），其余目录下一次读写就生效。
        改目录**不搬旧文件**：旧目录里的东西原地保留，界面会说明。
        回收线（``cache_limit_mb`` / ``cache_keep_days``）下一次转换写盘时生效；
        ``0`` = 那条规则不启用。

        改目录只认本机来源：路径是自由的，指到别处等于把后续产物写到任意位置；
        只改回收线（两个数字）不受此限。
        """
        if body.output_dir is not None or body.cache_dir is not None or body.log_dir is not None:
            require_local(
                request,
                code="storage_local_only",
                message="存储目录只能在本机修改；回收线可以在任意设备调整。",
            )
        previous_output = str(archive.default_root())
        pending: list[tuple[str, str, str]] = []
        new_output_dir = (
            _absolute_dir(body.output_dir, "生成结果目录")
            if body.output_dir is not None
            else None
        )
        new_cache_dir = (
            _absolute_dir(body.cache_dir, "缓存目录") if body.cache_dir is not None else None
        )
        if new_cache_dir is not None:
            pending.append((new_cache_dir, "缓存目录", "storage_dir_unusable"))
        new_log_dir = (
            _absolute_dir(body.log_dir, "日志目录") if body.log_dir is not None else None
        )
        if new_log_dir is not None:
            pending.append((new_log_dir, "日志目录", "storage_dir_unusable"))
        new_cache_limit = (
            _cache_number(body.cache_limit_mb, "缓存上限", 102400)
            if body.cache_limit_mb is not None
            else None
        )
        new_cache_days = (
            _cache_number(body.cache_keep_days, "保留天数", 3650)
            if body.cache_keep_days is not None
            else None
        )
        for value, label, code in pending:
            if not value:
                continue
            try:
                Path(value).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ApiError(
                    status=400,
                    code=code,
                    message=f"{label}建不出来：{exc}。换一个本机可写的目录。",
                ) from exc

        def _apply_storage(cfg) -> None:
            if new_output_dir is not None:
                cfg.output_dir = new_output_dir
            if new_cache_dir is not None:
                cfg.cache_dir = new_cache_dir
            if new_log_dir is not None:
                cfg.log_dir = new_log_dir
            if new_cache_limit is not None:
                cfg.cache_limit_mb = new_cache_limit
            if new_cache_days is not None:
                cfg.cache_keep_days = new_cache_days

        update_config(_apply_storage)
        moved: dict[str, Any] | None = None
        target_output = str(archive.default_root())
        if body.output_dir is not None and body.migrate_output and previous_output != target_output:
            plan = migrate_mod.plan_move(Path(previous_output), Path(target_output))
            if plan.entries:
                moved = asdict(migrate_mod.apply_move(plan))
        if body.log_dir is not None:
            install_logging()  # 文件落点换到新目录（重复调用是安全的）
        return {
            "object": "storage",
            "entries": _storage_entries(),
            "cache": _cache_policy(),
            "moved": moved,
        }

    @router.post("/storage/open", summary="打开存储目录", tags=["synthesis"])
    def open_storage(body: StorageOpenIn, request: Request) -> dict[str, Any]:
        """在资源管理器里打开这个目录（服务在本机运行才做得到）。

        只认本机来源：窗口弹在**宿主机**桌面上，不是请求方那台机器。
        """
        require_local(
            request, code="reveal_local_only", message="「打开目录」只能在本机使用。"
        )
        entry = next((item for item in _storage_entries() if item["key"] == body.key), None)
        if entry is None:
            raise not_found("storage_not_found", "没有这个存储位置。")
        path = Path(entry["path"])
        if not path.exists():
            raise ApiError(
                status=400,
                code="storage_missing",
                message="目录还不存在——先保存一次设置或生成一次。",
            )
        try:
            open_folder(path)
        except OSError as exc:
            raise ApiError(
                status=400, code="storage_open_failed", message=f"打不开这个目录：{exc}"
            ) from exc
        return {"object": "storage_open", "key": body.key, "path": str(path)}

    # ---- 生产方插件 ------------------------------------------------------
    # 生产方自带工作区与路由；宿主只列清单、存配置、发图标。
    # 具体接口在 ``/producers/{key}/...`` 下，由各插件自己的 router 提供。

    @router.get("/producers", tags=["producers"], summary="已安装生产方")
    def producers_of() -> dict[str, Any]:
        return {"object": "list", "data": producer_uc.list_payload()}

    @router.get("/producers/{key}", tags=["producers"], summary="生产方详情")
    def producer_of(key: str) -> dict[str, Any]:
        return producer_uc.settings_payload(key)

    @router.put("/producers/{key}/enabled", tags=["producers"], summary="启用 / 停用生产方")
    def producer_enabled(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """停用会真的把它的服务收掉；启用会立刻构造（含插件的自动启动）。"""
        return producer_uc.set_enabled(key, bool((body or {}).get("enabled")))

    @router.put("/producers/{key}/settings", tags=["producers"], summary="保存生产方配置")
    def producer_settings(key: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        """插件可以声明"配置里含本机可执行文件/路径"（``settings_local_only``）。"""
        plugin = producer_uc.find(key)
        if plugin.settings_local_only:
            require_local(
                request,
                code="producer_settings_local_only",
                message=f"「{plugin.label}」的设置只能在本机修改：它会指定并执行本机程序。",
            )
        return producer_uc.save_settings(key, body or {})

    @router.post("/producers/{key}/settings/open", tags=["producers"], summary="打开配置指向的目录")
    def producer_open_field(key: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        require_local(
            request, code="reveal_local_only", message="「打开目录」只能在本机使用。"
        )
        field = str(body.get("field") or "").strip()
        if not field:
            raise not_found("field_required", "没有说要打开哪个字段。")
        return producer_uc.open_field(key, field)

    @router.get("/producers/{key}/icon", tags=["producers"], summary="生产方图标")
    def producer_icon(key: str) -> Response:
        return Response(
            content=producer_uc.find(key).icon_svg(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    router.include_router(build_embedding_router())
    return router
