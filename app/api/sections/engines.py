"""路由分区「engines」：只注册本域的接口；路径前缀与错误约定见 :mod:`app.api.routes`。"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, UploadFile
from fastapi.responses import Response

from app.api.errors import ApiError
from app.api.schemas import ChatIn, ChatOut, EngineChoiceOut, ListResponse
from app.api.views import (
    _IDEMPOTENCY_TTL,
    _MAX_CLONE_BYTES,
    AsrModelIn,
    AsrOptionsIn,
    _fingerprint,
)
from app.domain.models import platform_voice_id
from app.engines.base import CloneRequest
from app.infra import archive
from app.infra.uploads import UploadTooLarge, read_limited
from app.usecase import asr
from app.usecase import llm as llm_uc
from app.usecase.engines import (
    active_engines,
    catalog_payload,
    clear_engine_voices,
    clone_voice,
    engine_choices,
    engine_voices,
    marks_payload,
    refresh_voices,
    save_engine_channel,
    save_engine_settings,
    set_engine_enabled,
    settings_payload,
)
from app.usecase.library import LibraryService

__all__ = ["register"]


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.get("/engines", response_model=ListResponse[dict[str, Any]], summary="引擎列表", tags=["engines"])
    def engines() -> dict[str, Any]:
        """**已启用**引擎的自描述：形态、能力、配置 schema、合成契约、参数映射、就绪状态。

        未启用的一律不在这里——它们只出现在 ``/engines/catalog``，添加后才算在用。
        密钥一律以掩码返回。
        """
        # 「通用」标签是对已启用那几台求的交集：这里算一次，九台共用。
        common = {row["name"] for row in marks_payload()["common"]}
        return {
            "object": "list",
            "data": [
                {"object": "engine", **settings_payload(key, common=common)}
                for key in active_engines()
            ],
            "has_more": False,
        }

    @router.get(
        "/engines/catalog",
        response_model=ListResponse[dict[str, Any]],
        summary="适配清单",
        tags=["engines"],
    )
    def engines_catalog() -> dict[str, Any]:
        """发现到的全部引擎（含未启用）。「添加引擎」照它画，前端不认识任何一家。"""
        return {
            "object": "list",
            "data": [{"object": "engine_catalog_item", **row} for row in catalog_payload()],
            "has_more": False,
        }

    @router.put("/engines/{key}/enabled", summary="启用 / 停用引擎", tags=["engines"])
    def put_engine_enabled(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """``{"enabled": true|false}``。停用不动配置与库内音色，只是不再出现。"""
        enabled = bool((body or {}).get("enabled", True))
        return {"object": "engine_enabled", **set_engine_enabled(key, enabled)}

    @router.get("/engines/{key}/icon", summary="引擎图标", tags=["engines"])
    def engine_icon(key: str) -> Response:
        """插件目录下的 ``icon.svg``。"""
        from app.usecase.engines import require_engine

        engine = require_engine(key)
        return Response(
            content=engine.icon_svg(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get(
        "/engines/choices",
        tags=["engines"],
        response_model=ListResponse[EngineChoiceOut],
        summary="引擎选项",
    )
    def engine_choices_of() -> dict[str, Any]:
        """**已启用**引擎的键与显示名——上游据此填充"交给哪一台"。"""
        return {
            "object": "list",
            "data": [
                {"object": "engine_choice", **item} for item in engine_choices()
            ],
            "has_more": False,
        }

    @router.get("/engines/{key}/voices", summary="引擎音色清单", tags=["engines"])
    async def voices_of_engine(key: str) -> dict[str, Any]:
        """这台引擎当前可用的音色，只读（不写入本地索引）。

        与 ``POST /engines/{key}/refresh`` 的区别：本接口不持久化，只回答"现在有什么"。
        响应同时给出该引擎接受的情绪词表，供上游逐行比对。
        """
        return {"object": "engine_voices", **await engine_voices(key, service)}

    @router.get("/engines/{key}/settings", summary="读取引擎配置", tags=["engines"])
    def get_engine_settings(key: str) -> dict[str, Any]:
        return {"object": "engine", **settings_payload(key)}

    @router.put("/engines/{key}/settings", summary="保存引擎配置", tags=["engines"])
    def put_engine_settings(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """保存配置。**密钥字段提交空串表示保持不变**。

        另有两个框架级指令（以下划线开头，不会与插件字段冲突）：
        ``_clear: [...]`` 清空指定机密项；``_reset: true`` 恢复插件默认值
        （非机密字段回落，机密保留）。
        """
        save_engine_settings(key, body or {})
        return {"object": "engine", **settings_payload(key)}

    @router.put("/engines/{key}/channel", summary="保存音色通道", tags=["engines"])
    def put_engine_channel(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """音色通道配置。``{"mode": ""}`` 或空请求体表示恢复默认。

        越界的模式回落到默认值而不是报错：能力是上限，配置越界只应回落。
        """
        return {"object": "engine_channel", **save_engine_channel(key, body or {})}

    @router.post("/engines/{key}/refresh", summary="拉取引擎音色清单", tags=["engines"])
    async def refresh_engine(key: str) -> dict[str, Any]:
        """拉取并入库。**拉取失败返回 200 + ``ok: false`` + 原因**。

        "没有拉到"是业务结果，不是协议错误——界面应显示为说明而非报错。
        """
        return {"object": "voice_refresh", **await refresh_voices(service, key)}

    @router.delete("/engines/{key}/voices", summary="清除引擎音色", tags=["engines"])
    def clear_voices_of_engine(key: str) -> dict[str, Any]:
        """清除库里属于这台引擎的全部音色条目。

        只删库：条目本身是音色 id 与元数据；已生成的样例在引擎保存目录里，不动磁盘。
        重新「拉取音色清单」会再次写入这些条目。
        """
        return {"object": "voice_clear", **clear_engine_voices(service, key)}

    @router.post(
        "/engines/{key}/clone",
        tags=["engines"],
        summary="音色克隆",
    )
    async def clone_engine(
        key: str,
        file: UploadFile | None = File(None, description="音源音频；也可用 source_voice_id/source_file 从音色库取"),  # noqa: B008
        source_voice_id: str = Form("", description="音源来自库里的哪条音色"),
        source_file: str = Form("", description="源样例文件名（与 source_voice_id 成对）"),
        name: str = Form("", description="克隆出的音色名称"),
        voice_id: str = Form("", description="指定的音色 id；留空自动生成"),
        transcript: str = Form("", description="音源文本（可选，用于相似度校验）"),
        design_prompt: str = Form("", description="音色设计描述；非空时不需音源"),
        preview_text: str = Form("", description="克隆后的试听文本（可选，按字符计费）"),
        model: str = Form("", description="试听使用的模型；留空使用引擎默认"),
        idempotency_key: str = Header("", alias="Idempotency-Key", description="幂等键"),
    ) -> dict[str, Any]:
        """换回一个可长期使用的平台音色。

        音源二选一：**从音色库选一条样例**（``source_voice_id`` + ``source_file``，
        界面就是这个入口），或旧的上传文件。库里的音源直接在磁盘上，不用再上传一遍。
        音色设计那一路不收音源，只收 ``design_prompt``。
        """
        temp_path: Path | None = None
        if design_prompt.strip():
            audio_path = ""
            source_key = "design:" + hashlib.sha256(design_prompt.strip().encode()).hexdigest()
        elif source_voice_id.strip() and source_file.strip():
            voice = service.get_voice(source_voice_id.strip())
            name_of_source = source_file.strip()
            # 只认库里登记过的样例名；解析后再限定在音色目录内，挡住 ../ 与绝对路径。
            target: Path | None = None
            if voice.folder and name_of_source in {s.file for s in voice.samples}:
                inside = archive.resolve_within(Path(voice.folder), name_of_source)
                if inside is not None and inside.is_file():
                    target = inside
            if target is None:
                generated = service.generated_sample_path(voice.id, name_of_source)
                if generated is not None and generated.is_file():
                    target = generated
            if target is None:
                raise ApiError(
                    status=400,
                    code="clone_source_missing",
                    message="这条样例的文件不在磁盘上，换一条音源。",
                )
            audio_path = str(target)
            source_key = f"{voice.id}/{name_of_source}"
        else:
            if file is None:
                raise ApiError(
                    status=400,
                    code="clone_source_required",
                    message="给一段音源：从音色库选一条样例，或上传一个文件。",
                )
            try:
                raw = await read_limited(file, _MAX_CLONE_BYTES)
            except UploadTooLarge as exc:
                raise ApiError(
                    status=400,
                    code="audio_too_large",
                    message=f"音源超过 {exc.label}。",
                ) from exc
            if not raw:
                raise ApiError(status=400, code="audio_empty", message="音源为空。")
            source_key = hashlib.sha256(raw).hexdigest()
            suffix = Path(Path(file.filename or "").name).suffix or ".audio"
            handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - 路径需在退出后仍存在
                prefix="voicehub-clone-", suffix=suffix, delete=False
            )
            handle.write(raw)
            handle.close()
            temp_path = Path(handle.name)
            audio_path = str(temp_path)

        fingerprint = _fingerprint(
            {
                "key": key,
                "name": name,
                "voice_id": voice_id,
                "transcript": transcript,
                "design_prompt": design_prompt,
                "preview_text": preview_text,
                "model": model,
                "source": source_key,
            }
        )
        if idempotency_key:
            # 与合成同一条规则：先抢占再克隆。克隆同样按量计费，同键并发不能各打一次。
            state, cached = await asyncio.to_thread(
                service.idempotent_begin, idempotency_key, fingerprint, ttl=_IDEMPOTENCY_TTL
            )
            if state in ("replay", "pending"):
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                if state == "replay":
                    return cached
                raise ApiError(
                    status=409,
                    code="idempotency_in_progress",
                    message="同一个请求正在处理中，等它完成后再试。",
                )
        try:
            try:
                meta = await clone_voice(
                    service,
                    key,
                    CloneRequest(
                        name=name.strip(),
                        voice_id=voice_id.strip(),
                        audio_path=audio_path,
                        transcript=transcript.strip(),
                        design_prompt=design_prompt.strip(),
                        preview_text=preview_text.strip(),
                        model=model.strip(),
                    ),
                )
            finally:
                if temp_path is not None:
                    try:  # 临时文件尽力清理；清理失败不影响本次结果
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            payload = {
                "object": "voice_clone",
                "id": meta.id,
                "voice_id": platform_voice_id(key, meta.id),
                "name": meta.name,
                "description": meta.description,
            }
            if idempotency_key:
                await asyncio.to_thread(
                    service.idempotent_store,
                    idempotency_key,
                    fingerprint,
                    payload,
                    ttl=_IDEMPOTENCY_TTL,
                )
            return payload
        except BaseException:
            # 克隆失败 / 被取消：放开占位，别让这个键锁到过期。
            if idempotency_key:
                await asyncio.to_thread(
                    service.idempotent_abandon, idempotency_key, fingerprint
                )
            raise


    # ---- 对话模型（LLM 插件）---------------------------------------------
    #
    # 与引擎同构、不同契约：引擎"给文本出声"，这里"给消息回消息"。
    # 密钥一律掩码；"没配好"与"平台拒了"都带可读说明。

    @router.get(
        "/llm/providers",
        response_model=ListResponse[dict[str, Any]],
        summary="对话模型列表",
        tags=["llm"],
    )
    def llm_providers() -> dict[str, Any]:
        """已装对话模型的自描述：形态、能力、配置 schema、就绪状态，密钥掩码。"""
        return {
            "object": "list",
            "data": [{"object": "llm_provider", **item} for item in llm_uc.providers_payload()],
            "has_more": False,
        }

    @router.get("/llm/providers/{key}", summary="读取对话模型", tags=["llm"])
    def llm_provider(key: str) -> dict[str, Any]:
        return {"object": "llm_provider", **llm_uc.settings_payload(key)}

    @router.get("/llm/providers/{key}/icon", summary="对话模型图标", tags=["llm"])
    def llm_icon(key: str) -> Response:
        return Response(
            content=llm_uc.require_provider(key).icon_svg(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.put("/llm/providers/{key}/settings", summary="保存对话模型配置", tags=["llm"])
    def llm_save_settings(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """保存配置。**密钥字段提交空串表示保持不变。**

        框架级指令同引擎：``_clear: [...]`` 清空机密，``_reset: true`` 恢复默认。
        """
        llm_uc.save_provider_settings(key, body or {})
        return {"object": "llm_provider", **llm_uc.settings_payload(key)}

    @router.put("/llm/default", summary="选择默认对话模型", tags=["llm"])
    def llm_set_default(body: dict[str, Any]) -> dict[str, Any]:
        """``{"key": "deepseek"}``；空串 = 回到"按就绪状态自动挑"。"""
        key = llm_uc.save_default_provider(str((body or {}).get("key") or ""))
        return {"object": "llm_default", "key": key}

    @router.get("/llm/providers/{key}/models", summary="平台模型清单", tags=["llm"])
    async def llm_models(key: str) -> dict[str, Any]:
        """只读；拉不到返回 200 + ``ok:false`` + 理由。"""
        return {"object": "llm_models", **await llm_uc.models_of(key)}

    @router.post("/llm/providers/{key}/test", summary="测试连通", tags=["llm"])
    async def llm_test(key: str) -> dict[str, Any]:
        """发一句极短的话验证密钥、地址与模型。失败也是业务结果（200 + ``ok:false``）。"""
        return {"object": "llm_test", **await llm_uc.test_provider(key)}

    @router.post("/llm/chat", response_model=ChatOut, summary="对话补全", tags=["llm"])
    async def llm_chat(body: ChatIn) -> dict[str, Any]:
        """一次补全。``provider`` 留空用默认那家；降级与截断写在 ``notes`` 里。"""
        return await llm_uc.chat(body.model_dump())


    # ---- 语音识别 --------------------------------------------------------
    #
    # 识别是一类插件（``app.engines.asr_base``）：与合成引擎共用发现机制与配置
    # 存取，能力与调用各走各的。接口以"用哪台、它配什么"为中心——没有"安装/下载"，
    # 模型从哪来是插件自己的事（HTTP 服务 / 本地推理库 / 云端）。

    @router.get("/asr", summary="识别引擎状态", tags=["asr"])
    def asr_status() -> dict[str, Any]:
        """每台识别引擎的说明、当前值与就绪情况；**不探网络**，开销很低。"""
        return {"object": "asr_status", **asr.status()}

    @router.get("/asr/engines/{key}/icon", summary="识别引擎图标", tags=["asr"])
    def asr_icon(key: str) -> Response:
        """插件目录里的 ``icon.svg``（没有则中性图标）。与合成引擎同一口径。"""
        engine = asr.engine_of(key)
        return Response(content=engine.icon_svg(), media_type="image/svg+xml")

    @router.put("/asr/engines/{key}/settings", summary="保存识别引擎设置", tags=["asr"])
    def asr_settings(key: str, body: dict[str, Any]) -> dict[str, Any]:
        """保存一台识别引擎的配置；``_clear`` / ``_reset`` 的语义与合成引擎一致。"""
        out = asr.save_settings(key, body)
        # 刚填好地址 / 模型，就等于"连上了"：开关开着就顺手补一批逐字稿。
        asr.maybe_fill(service, delay=0.5)
        return {"object": "asr_status", **out}

    @router.post("/asr/engines/{key}/probe", summary="测试识别引擎", tags=["asr"])
    def asr_probe(key: str) -> dict[str, Any]:
        """连一下这台引擎（或其服务）：能不能用、它有哪些模型。"""
        out = asr.probe(key)
        if out.get("ok"):
            # 刚探过这一台就是当前引擎，后台补全不必再探一次。
            asr.maybe_fill(service, delay=0.5, verified=key == asr.current_key())
        return {"object": "asr_probe", **out}

    @router.put("/asr/current", summary="选择识别引擎", tags=["asr"])
    def asr_current(body: AsrModelIn) -> dict[str, Any]:
        """空串表示用第一台。"""
        out = asr.set_current(body.key)
        asr.maybe_fill(service, delay=0.5)
        return {"object": "asr_status", **out}

    @router.put("/asr/options", summary="识别应用选项", tags=["asr"])
    def asr_options(body: AsrOptionsIn) -> dict[str, Any]:
        """自动识别、写回样例、录音转写——与"用哪台引擎"分开的应用层选择。"""
        patch = {name: value for name, value in body.model_dump().items() if value is not None}
        out = asr.save_options(patch)
        # 刚打开这个开关不必等下一次连接：现在就先补一批。
        if patch.get("auto_on_connect") and asr.maybe_fill(service, delay=0.5):
            # 重新取一次状态：界面据此显示「正在补逐字稿」。
            out = asr.status()
        return {"object": "asr_status", **out}

    @router.get("/asr/fill", summary="补逐字稿进度", tags=["asr"])
    def asr_fill() -> dict[str, Any]:
        """「连接时自动识别」那一轮的进度：toast 轮询它。

        ``running`` 为假就收起 toast；``starting`` 是"线程起了、还没确定有没有活"，
        只用来决定还要不要接着轮询（启动时等服务起来那几秒就落在这里）。
        """
        return {"object": "asr_fill", **asr.fill_state()}

    @router.post("/asr/fill/cancel", summary="停止补逐字稿", tags=["asr"])
    def asr_fill_cancel() -> dict[str, Any]:
        """停止：当前这一条收尾，不再开始下一条。"""
        return {"object": "asr_fill", **asr.cancel_fill()}

