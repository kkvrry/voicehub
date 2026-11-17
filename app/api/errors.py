"""统一错误契约：一个错误体、一张错误码表、一处出口。

对外形状（所有非 2xx 一致）::

    {
      "error": {"message", "type", "code", "param", "request_id"},
      "detail": "<与 message 同值，过渡字段，v1 稳定后移除>"
    }

- ``message``：中文，严格、简洁、专业且易懂；``type`` / ``code`` 供程序分支，
  ``code`` 登记在 :data:`CODES`（唯一产地，改名即破坏契约）；
- ``request_id``：与响应头 ``X-Request-Id`` 同值，用于关联日志行；
- 状态码判定只依赖异常自带的 ``code`` / ``status``，不依赖消息文案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.infra.loghub import CHANNEL

__all__ = [
    "ERROR_TYPES",
    "ApiError",
    "engine_failed",
    "install_error_handlers",
    "internal",
    "invalid",
    "not_found",
    "token_denied",
]

#: 错误大类。只有这四个——细分交给 ``code``。
#: 增删取值会改变调用方的分支逻辑，属于契约变更。
ERROR_TYPES: tuple[str, ...] = (
    "invalid_request_error",  # 请求本身有问题（参数、取值、状态冲突）
    "not_found_error",  # 资源不存在
    "engine_error",  # 引擎在，但这台做不到，或上游平台拒绝
    "internal_error",  # 本服务的内部故障
)

#: 错误码表。**这里是唯一产地**：调用方按这些字符串分支，改名等于破坏契约。
CODES: dict[str, str] = {
    # 请求形状
    "invalid_request": "请求体不符合接口约定",
    "invalid_value": "字段取值不合法",
    "not_found": "接口或资源不存在",
    "method_not_allowed": "请求方法不被允许",
    # 音色库
    "voice_not_found": "音色不存在",
    "sample_not_found": "样例不存在",
    "cover_not_found": "封面不存在",
    "platform_voice_not_found": "平台音色条目不存在",
    "unsafe_path": "路径不安全，拒绝操作",
    "invalid_operation": "该操作不适用于这个对象",
    "folder_missing": "音色文件夹不存在",
    "reveal_failed": "打开文件夹失败",
    "reveal_local_only": "只能打开本机音色的文件夹",
    "autostart_unsupported": "开机自启只在桌面版可用",
    "autostart_failed": "写注册表失败",
    "scan_in_progress": "扫描已在进行中",
    "audio_empty": "音频为空",
    "audio_too_large": "音频超出大小上限",
    "clone_source_required": "克隆需要音源音频",
    "clone_source_missing": "克隆音源不存在",
    # 引擎
    "engine_not_found": "引擎不存在",
    "engine_not_enabled": "引擎未启用",
    "engine_failed": "引擎执行失败",
    "engine_unreachable": "引擎服务连不上",
    "engine_unsupported": "引擎不支持该操作",
    "engine_no_audio": "引擎未返回音频",
    "engine_required": "缺少目标引擎",
    "invalid_settings": "引擎设置值不合法",
    "rate_limited": "平台限流，请求过快",
    "voice_not_usable": "音色无法用于该引擎",
    # 识别
    "asr_error": "语音识别失败",
    "asr_not_installed": "识别引擎未就绪",
    "dictation_required": "没有可转写的音频",
    "recording_required": "没有收到录音",
    # 对话模型
    "llm_failed": "对话模型调用失败",
    "llm_not_found": "对话模型不存在",
    "llm_not_configured": "对话模型未配置",
    "llm_search_unsupported": "对话模型不支持联网搜索",
    # 向量模型
    "embedding_failed": "向量模型执行失败",
    "embedding_not_found": "向量模型不存在",
    "embedding_missing": "向量模型未下载",
    "embedding_busy": "向量模型正在下载",
    # 头像
    "avatar_failed": "头像处理失败",
    "avatar_not_found": "头像不存在",
    "avatar_empty": "没有拿到图片数据",
    "avatar_unsupported": "该音色不支持头像处理",
    "avatar_folder_missing": "音色目录不存在",
    "avatar_candidate_expired": "头像候选已过期",
    "avatar_candidate_mismatch": "头像候选与音色不匹配",
    # 生产方与 TTS 直连
    "producer_error": "生产方业务错误",
    "trash_failed": "移入回收站失败",
    "storage_missing": "存储目录不存在",
    "storage_open_failed": "打开存储目录失败",
    "tts_body_encoding": "TTS 请求体编码不支持",
    "tts_body_invalid": "TTS 请求体格式不合法",
    "tts_text_required": "缺少合成文本",
    "tts_target_required": "缺少合成目标",
    "tts_speed_invalid": "语速不合法",
    "tts_guidance_invalid": "引导指令不合法",
    "tts_guidance_range": "引导强度超出范围",
    "tts_voice_not_found": "合成音色不存在",
    "tts_voice_ambiguous": "音色匹配到多个结果",
    # 合成与产物
    "empty_text": "正文为空",
    "archive_failed": "生成结果落盘失败",
    "idempotency_key_conflict": "幂等键与既有请求不一致",
    "idempotency_in_progress": "同一幂等键的请求正在处理中",
    # 设置与存储
    "cache_setting_out_of_range": "缓存策略超出允许范围",
    "storage_dir_relative": "存储目录必须是绝对路径",
    # 访问令牌（见 app.api.auth）
    "token_required": "缺少访问令牌",
    "token_invalid": "访问令牌不对",
    # 兜底
    "internal_error": "服务内部错误",
}


@dataclass
class ApiError(Exception):
    """HTTP 层唯一主动抛出的错误类型。

    :param status: HTTP 状态码。
    :param code: :data:`CODES` 里的键。
    :param message: 中文说明，严格、简洁、专业。
    :param type: :data:`ERROR_TYPES` 之一；缺省按 ``status`` 推导。
    :param param: 出错的字段名（可选）。
    """

    status: int
    code: str
    message: str
    type: str = ""
    param: str | None = None

    def __post_init__(self) -> None:
        if not self.type:
            self.type = {404: "not_found_error", 500: "internal_error"}.get(
                self.status, "invalid_request_error"
            )
        super().__init__(self.message)


def invalid(
    code: str, message: str, *, param: str | None = None, status: int = 400
) -> ApiError:
    """请求侧错误（默认 400；形状校验用 422 时显式传）。"""
    return ApiError(status=status, code=code, message=message, param=param)


def not_found(code: str, message: str, *, param: str | None = None) -> ApiError:
    """资源不存在（404）。"""
    return ApiError(status=404, code=code, message=message, param=param)


def engine_failed(message: str, *, code: str = "engine_failed", status: int = 400) -> ApiError:
    """引擎侧错误（默认 400）。"""
    return ApiError(status=status, code=code, message=message, type="engine_error")


def internal(message: str = "服务内部错误。") -> ApiError:
    """内部故障（500）。"""
    return ApiError(status=500, code="internal_error", message=message, type="internal_error")


def token_denied(request: Request) -> JSONResponse:
    """401：这个来源没有带对的访问令牌。

    中间件里直接用（它跑在异常处理器之外），所以这里自己拼错误体——但用的仍是
    同一份 :func:`_body`，形状与其他错误一致，调用方不必为"被挡住"写第二套解析。
    """
    return JSONResponse(
        status_code=401,
        content=_body(
            "需要访问令牌：在 /login 输入，或用 X-VoiceHub-Token 头带上。",
            type="invalid_request_error",
            code="token_required",
            param=None,
            request_id=_request_id(request),
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _body(message: str, *, type: str, code: str, param: str | None, request_id: str) -> dict:
    return {
        "error": {
            "message": message,
            "type": type,
            "code": code,
            "param": param,
            "request_id": request_id,
        },
        # 过渡字段：旧读取方仍按 detail 取消息。移除时机见模块文档。
        "detail": message,
    }


#: pydantic 校验错误的**常用类型 → 中文说法**。
#:
#: 只翻常见的几类；没有对应说法时保留 pydantic 原文（那是技术细节，不是口语），
#: 不猜——猜错比英文原文更难排查。
_VALIDATION_TEXT: dict[str, str] = {
    "missing": "缺少必填项",
    "string_type": "应为字符串",
    "int_type": "应为整数",
    "int_parsing": "应为整数",
    "bool_type": "应为布尔值",
    "bool_parsing": "应为布尔值",
    "float_type": "应为数值",
    "float_parsing": "应为数值",
    "dict_type": "应为对象",
    "list_type": "应为数组",
    "literal_error": "取值不在允许范围内",
    "enum": "取值不在允许范围内",
    "greater_than_equal": "低于允许下限",
    "less_than_equal": "高于允许上限",
}


def _validation_message(exc: RequestValidationError) -> tuple[str, str | None]:
    """第一条校验错误 → （中文说明, 字段路径）。"""
    errors = exc.errors()
    if not errors:
        return "请求参数不合法。", None
    first = errors[0]
    loc = [str(part) for part in first.get("loc", ()) if part not in ("body", "query", "path")]
    param = ".".join(loc) or None
    text = _VALIDATION_TEXT.get(str(first.get("type", "")), str(first.get("msg", "")).strip())
    # 自定义校验器（raise ValueError）的 msg 带 pydantic 前缀，去掉后才是人能读的那句。
    text = text.removeprefix("Value error, ")
    where = f"参数 {param} " if param else "请求体"
    return f"{where}不合法：{text}。", param


def install_error_handlers(app: FastAPI) -> None:
    """挂上四个处理器：主动错误、校验错误、HTTP 错误、未捕获异常。

    只有这一处生成错误响应体；新代码一律 ``raise ApiError(...)``，
    散在各路由的 ``raise HTTPException`` 会绕开错误码表。
    """
    # 域异常映射放函数内导入：依赖方向是单向的（api → usecase），避免模块级互相引用。
    from app.engines.base import EngineError
    from app.infra.archive import ArchiveError
    from app.infra.migrate import MoveError
    from app.infra.trash import TrashError
    from app.llm.base import LlmError
    from app.producers.base import ProducerError
    from app.usecase.asr import AsrError
    from app.usecase.engines import EngineConfigError, EngineSettingsError
    from app.usecase.library import LibraryError
    from app.usecase.llm import LlmConfigError

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=_body(
                exc.message,
                type=exc.type,
                code=exc.code,
                param=exc.param,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        message, param = _validation_message(exc)
        return JSONResponse(
            status_code=422,
            content=_body(
                message,
                type="invalid_request_error",
                code="invalid_request",
                param=param,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else ""
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        message = detail or {404: "接口不存在。", 405: "请求方法不被允许。"}.get(
            exc.status_code, f"请求失败（HTTP {exc.status_code}）。"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(
                message,
                type="not_found_error" if exc.status_code == 404 else "invalid_request_error",
                code=code,
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(LibraryError)
    async def _library_error(request: Request, exc: LibraryError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=_body(
                str(exc),
                type="not_found_error" if exc.status == 404 else "invalid_request_error",
                code=exc.code,
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(EngineConfigError)
    async def _engine_config_error(
        request: Request, exc: EngineConfigError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_body(
                str(exc),
                type="not_found_error",
                code="engine_not_found",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(EngineSettingsError)
    async def _engine_settings_error(
        request: Request, exc: EngineSettingsError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                str(exc),
                type="invalid_request_error",
                code="invalid_settings",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(EngineError)
    async def _engine_error(request: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                exc.reason,
                type="engine_error",
                code=getattr(exc, "code", "engine_failed"),
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(LlmConfigError)
    async def _llm_config_error(request: Request, exc: LlmConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_body(
                str(exc),
                type="not_found_error",
                code="llm_not_found",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(LlmError)
    async def _llm_error(request: Request, exc: LlmError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                exc.reason,
                type="engine_error",
                code=getattr(exc, "code", "llm_failed"),
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(AsrError)
    async def _asr_error(request: Request, exc: AsrError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                str(exc),
                type="engine_error",
                code=getattr(exc, "code", "asr_error"),
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(ProducerError)
    async def _producer_error(request: Request, exc: ProducerError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                str(exc),
                type="engine_error",
                code=getattr(exc, "code", "producer_error"),
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(MoveError)
    async def _move_error(request: Request, exc: MoveError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                str(exc),
                type="invalid_request_error",
                code="invalid_value",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(TrashError)
    async def _trash_error(request: Request, exc: TrashError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_body(
                str(exc),
                type="invalid_request_error",
                code="invalid_operation",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(ArchiveError)
    async def _archive_error(request: Request, exc: ArchiveError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_body(
                str(exc),
                type="internal_error",
                code="archive_failed",
                param=None,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 未捕获异常只对外说一句；细节进日志，用 request_id 关联。
        request_id = _request_id(request)
        CHANNEL.error(
            f"未处理的异常：{type(exc).__name__}: {exc}",
            source="api",
            request_id=request_id,
            path=str(request.url.path),
        )
        return JSONResponse(
            status_code=500,
            content=_body(
                "服务内部错误。",
                type="internal_error",
                code="internal_error",
                param=None,
                request_id=request_id,
            ),
        )


def error_types() -> dict[str, Any]:
    """给单元测试与文档用的错误码表快照。"""
    return dict(CODES)
