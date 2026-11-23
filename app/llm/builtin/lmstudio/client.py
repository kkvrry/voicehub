"""LM Studio 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

走本地服务的 OpenAI 兼容端点（默认 ``http://127.0.0.1:1234/v1``）：

==================  ==========================================  =====================
用途                端点                                        形状
==================  ==========================================  =====================
对话                ``POST {root}/chat/completions``            OpenAI 同形
模型清单            ``GET  {站点根}/api/v1/models``              ``models[]``（v0 为 ``data[]``）
清单回落            ``GET  {root}/models``                       ``data[].id``
==================  ==========================================  =====================

``root`` 由配置模型补全：站点根与带 ``/v1`` 的写法都认，补路径只在 :func:`endpoint`
一处做。默认按本机服务对待，不走系统代理（:func:`normalize_no_proxy_env`）。

模型清单先问 LM Studio 自己的 REST 接口（``/api/v1/models``，旧版本
``/api/v0/models``）：那里有「已加载 / 未加载」、上下文与向量模型之分，
OpenAI 兼容的 ``/v1/models`` 只有 id。REST 不可用时回落到 ``/v1/models``。

模型解析也在这一层：``model`` 配置留空时先问清单，优先已加载的模型，
其次第一个非向量模型——用户不必先抄模型 id。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm.base import LlmError, ModelList, ModelMeta
from app.llm.builtin.lmstudio.settings import LmStudioSettings
from app.llm.http import client, endpoint, request_json

__all__ = [
    "chat",
    "endpoint",
    "list_models",
    "read_error",
    "resolve_model",
]

CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"

#: LM Studio 自己的模型清单（新版本在前）。它们给「已加载 / 未加载」与上下文，
#: OpenAI 兼容的 ``/v1/models`` 只有 id；两个都不可用时才回落。
REST_MODELS_PATHS = ("/api/v1/models", "/api/v0/models")

#: 非对话模型：清单里不列，自动挑选时也跳过。REST v0 写 ``embeddings``，v1 写 ``embedding``。
_SKIP_TYPES = {"embedding", "embeddings", "decision"}

def _timeout(cfg: LmStudioSettings) -> httpx.Timeout:
    # 连接单独给 10 秒：本机服务没起来要快死；读给足——生成本来就慢。
    return httpx.Timeout(float(cfg.timeout), connect=10.0)


def _list_timeout() -> httpx.Timeout:
    # 清单只是一次本机查询：服务活着立刻回来，别陪着聊天的超时一起等。
    return httpx.Timeout(30.0, connect=10.0)


def _headers(cfg: LmStudioSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = (cfg.api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _detail(body: str) -> str:
    """平台原话：JSON 里的 ``error`` 通常比整段响应短得多。"""
    text = (body or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:300] or "（空响应）"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return message[:300]
        if isinstance(error, str) and error.strip():
            return error.strip()[:300]
    return text[:300] or "（空响应）"


def read_error(status: int, body: str) -> LlmError:
    """HTTP 错误 → 可读说明。``retryable`` 只给真的值得重试的那几档。"""
    detail = _detail(body)
    if status == 401:
        return LlmError(
            f"LM Studio 拒绝了这次请求：API Token 无效或没有填（401）。{detail}"
        )
    if status == 404:
        return LlmError(
            f"LM Studio 没有这个接口或模型（404）。检查服务地址与模型 id。{detail}"
        )
    if status == 400:
        return LlmError(f"LM Studio 认为请求参数不合法（400）。{detail}")
    if status == 429:
        return LlmError(f"LM Studio 正忙（429），稍后重试。{detail}", retryable=True)
    if status >= 500:
        return LlmError(
            f"LM Studio 服务端错误（{status}），可稍后重试。{detail}", retryable=True
        )
    return LlmError(f"LM Studio 返回 {status}。{detail}")


def _transport_reason(error: Exception, root: str) -> LlmError:
    return LlmError(
        f"连不上 LM Studio（{root}）：{error}。"
        "确认 LM Studio 已启动本地服务（开发者 → 本地服务），地址与端口一致。",
        retryable=True,
    )


async def chat(cfg: LmStudioSettings, payload: dict[str, Any]) -> dict[str, Any]:
    """发一次 ``chat/completions``，返回原始 JSON（解析在插件里做）。"""
    return await request_json(
        "POST",
        cfg.root,
        CHAT_PATH,
        label="LM Studio",
        headers=_headers(cfg),
        timeout=_timeout(cfg),
        payload=payload,
        error_reader=read_error,
        transport_hint="确认 LM Studio 已启动本地服务（开发者 → 本地服务），地址与端口一致",
    )


def _host_root(cfg: LmStudioSettings) -> str:
    """OpenAI 兼容根的上一级：``/api/v1/models`` 挂在站点根上，不在 ``/v1`` 下。"""
    return cfg.root.removesuffix("/v1")


async def _rows_at(cfg: LmStudioSettings, url: str) -> list[dict[str, Any]] | None:
    """拉一次清单；这一条路径不可用（404 / 非 JSON）时返回 ``None``，交给下一条。"""
    try:
        response = await client().get(url, headers=_headers(cfg), timeout=_list_timeout())
    except httpx.HTTPError as exc:
        # 连不上是定论：换一条路径也是同一个端口，别把"服务没起来"埋成"清单读不出来"。
        raise _transport_reason(exc, cfg.root) from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        # 401 / 5xx 不是"换一条路径"能解决的：直接按平台原话报出去。
        raise read_error(response.status_code, response.text)
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("models")
    if not isinstance(rows, list):
        rows = data.get("data")
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


async def _model_rows(cfg: LmStudioSettings) -> list[dict[str, Any]]:
    """本机模型清单的原始条目。失败抛 :class:`LlmError`（调用方决定报错还是降级）。

    先试 LM Studio 自己的 REST 清单（带加载状态与上下文），都不可用时回落
    OpenAI 兼容的 ``/v1/models``——那个只有 id，也够解析与列出。
    """
    for path in REST_MODELS_PATHS:
        rows = await _rows_at(cfg, f"{_host_root(cfg)}{path}")
        if rows is not None:
            return rows
    rows = await _rows_at(cfg, endpoint(cfg.root, MODELS_PATH))
    if rows is None:
        raise LlmError(
            f"LM Studio 的模型清单读不出来（{cfg.root}）：检查服务地址与版本。"
        )
    return rows


def _model_id(row: dict[str, Any]) -> str:
    # REST v1 写 ``key``，v0 与 OpenAI 兼容写 ``id``。
    return str(row.get("id") or row.get("key") or "").strip()


def _model_type(row: dict[str, Any]) -> str:
    return str(row.get("type") or "").strip().lower()


async def resolve_model(cfg: LmStudioSettings, requested: str) -> str:
    """这次要发给服务的模型 id：请求里给的 → 配置里的 → 本机清单里的第一个。

    自动挑选优先已加载的模型（``state`` / ``loaded_instances``），其次跳过向量
    与决策模型。一个都没有时报出可操作的说明，而不是让服务回一句 400。
    """
    wanted = (requested or "").strip() or (cfg.model or "").strip()
    if wanted:
        return wanted

    rows = [row for row in await _model_rows(cfg) if _model_id(row)]
    chat_rows = [row for row in rows if _model_type(row) not in _SKIP_TYPES]
    for row in chat_rows:
        if str(row.get("state") or "").strip().lower() == "loaded":
            return _model_id(row)
        if row.get("loaded_instances"):
            return _model_id(row)
    for row in chat_rows:
        if "embed" not in _model_id(row).lower():
            return _model_id(row)
    if chat_rows:
        return _model_id(chat_rows[0])
    raise LlmError(
        "LM Studio 里没有可用模型：先在 LM Studio 加载一个模型，或在设置里填模型 id。",
        code="llm_not_configured",
    )


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _capabilities(row: dict[str, Any]) -> tuple[str, ...]:
    """平台声明的能力（有就照它，没有就按文本模型算）。"""
    found = ["text"]
    caps = row.get("capabilities")
    if isinstance(caps, dict):
        if caps.get("vision") is True:
            found.append("vision")
        if caps.get("reasoning"):
            found.append("reasoning")
    return tuple(found)


def _meta(row: dict[str, Any]) -> ModelMeta | None:
    model_id = _model_id(row)
    if not model_id or _model_type(row) in _SKIP_TYPES:
        return None
    return ModelMeta(
        id=model_id,
        label=str(row.get("display_name") or row.get("name") or model_id),
        context=_int(row.get("max_context_length") or row.get("context_length")),
        abilities=_capabilities(row),
    )


async def list_models(cfg: LmStudioSettings) -> ModelList:
    """本机模型清单。拉不到不抛：``why`` 带回理由（与音色清单同口径）。"""
    try:
        rows = await _model_rows(cfg)
    except LlmError as exc:
        return ModelList(why=exc.reason)
    items = tuple(meta for meta in (_meta(row) for row in rows) if meta is not None)
    return ModelList(
        items=items,
        why="" if items else "LM Studio 没有返回任何模型：先在「开发者」页加载一个模型。",
    )
