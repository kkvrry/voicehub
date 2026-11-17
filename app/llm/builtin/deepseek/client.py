"""DeepSeek 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

端点照官方 OpenAI 兼容接口：

==================  ==========================================  =====================
用途                端点                                        形状
==================  ==========================================  =====================
对话                ``POST {root}/chat/completions``            OpenAI 同形
模型清单            ``GET  {root}/models``                       ``data[].id``
==================  ==========================================  =====================

``root`` 既可以是 ``https://api.deepseek.com``，也可以是带 ``/v1`` 的写法，
也可以是中转站——补路径只在 :func:`endpoint` 一处做，别在插件里再拼一遍。

错误一律翻成 :class:`~app.llm.base.LlmError`，**只报平台原话 + 一句定位**：
401 与 402 的处置完全不同（换密钥 vs 充值），把它们合并成"请求失败"等于把
真正有用的那句埋掉。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.infra.netenv import normalize_no_proxy_env
from app.llm.base import LlmError, ModelList, ModelMeta
from app.llm.builtin.deepseek.settings import DeepseekSettings

__all__ = [
    "chat",
    "endpoint",
    "list_models",
    "read_error",
]

CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"

#: 复用连接：在 Windows 上新建 AsyncClient 要两秒多，每条消息建一个等于把分析拖慢十倍。
#: 按事件循环分开缓存——测试每次 ``asyncio.run`` 都是新循环，不能拿到上一个循环的客户端。
_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None


def _client() -> httpx.AsyncClient:
    global _CLIENT, _CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or _CLIENT_LOOP is not loop:
        normalize_no_proxy_env()
        _CLIENT = httpx.AsyncClient()
        _CLIENT_LOOP = loop
    return _CLIENT


def endpoint(root: str, path: str) -> str:
    """站点根 + 路径。已经写到 ``/chat/completions`` 的不再重复拼。"""
    base = (root or "").strip().rstrip("/")
    if base.endswith(path):
        return base
    return f"{base}{path}"


def _timeout(cfg: DeepseekSettings) -> httpx.Timeout:
    # 连接单独给 10 秒：连不上要快死；读给足——长文本分析本来就慢。
    return httpx.Timeout(float(cfg.timeout), connect=10.0)


def _headers(cfg: DeepseekSettings) -> dict[str, str]:
    if not (cfg.api_key or "").strip():
        # 空 key 拼出来的 ``Bearer `` 是非法头，报错会变成 "Illegal header value"，
        # 与真正的"没填密钥"隔了一层。在这里先拦。
        raise LlmError("还没填 API Key。", code="llm_not_configured")
    return {
        "Authorization": f"Bearer {cfg.api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def read_error(status: int, body: str) -> LlmError:
    """HTTP 错误 → 可读说明。``retryable`` 只给真的值得重试的那几档。"""
    detail = (body or "").strip()[:300] or "（空响应）"
    if status == 401:
        return LlmError(f"DeepSeek 拒绝了这次请求：API Key 无效或已撤销（401）。{detail}")
    if status == 402:
        return LlmError(f"DeepSeek 账户余额不足（402）。{detail}")
    if status == 404:
        return LlmError(f"DeepSeek 没有这个接口或模型（404）。检查接口地址与模型名。{detail}")
    if status == 422:
        return LlmError(f"DeepSeek 认为请求参数不合法（422）。{detail}")
    if status == 429:
        return LlmError(f"DeepSeek 限流了（429），稍后重试。{detail}", retryable=True)
    if status >= 500:
        return LlmError(f"DeepSeek 服务端错误（{status}），可稍后重试。{detail}", retryable=True)
    return LlmError(f"DeepSeek 返回 {status}。{detail}")


def _transport_reason(error: Exception, root: str) -> LlmError:
    return LlmError(f"连不上 DeepSeek（{root}）：{error}。检查网络或接口地址。", retryable=True)


async def chat(cfg: DeepseekSettings, payload: dict[str, Any]) -> dict[str, Any]:
    """发一次 ``chat/completions``，返回原始 JSON（解析在插件里做）。"""
    url = endpoint(cfg.root, CHAT_PATH)
    try:
        response = await _client().post(
            url, json=payload, headers=_headers(cfg), timeout=_timeout(cfg)
        )
    except httpx.HTTPError as exc:
        raise _transport_reason(exc, cfg.root) from exc
    if response.status_code >= 400:
        raise read_error(response.status_code, response.text)
    try:
        data = response.json()
    except ValueError as exc:
        raise LlmError(f"DeepSeek 的响应不是 JSON：{response.text[:200]}") from exc
    if not isinstance(data, dict):
        raise LlmError("DeepSeek 的响应不是 JSON 对象。")
    return data


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _abilities(row: dict[str, Any]) -> tuple[str, ...]:
    """平台的模型元信息 → 能力键。**照它自己声明的收**，不按模型名猜。"""
    inputs = row.get("input_modalities")
    inputs = inputs if isinstance(inputs, list) else []
    effort = row.get("effort") if isinstance(row.get("effort"), dict) else {}
    found: list[str] = []
    if "text" in inputs or not inputs:
        found.append("text")
    if "image" in inputs:
        found.append("vision")
    if "audio" in inputs:
        found.append("audio")
    if effort.get("supported_levels"):
        found.append("reasoning")
    return tuple(found)


async def list_models(cfg: DeepseekSettings) -> ModelList:
    """平台模型清单。拉不到不抛：``why`` 带回理由（与音色清单同口径）。"""
    url = endpoint(cfg.root, MODELS_PATH)
    try:
        headers = _headers(cfg)
    except LlmError as exc:
        return ModelList(why=exc.reason)
    try:
        response = await _client().get(url, headers=headers, timeout=_timeout(cfg))
    except httpx.HTTPError as exc:
        return ModelList(why=f"连不上 DeepSeek：{exc}")
    if response.status_code >= 400:
        return ModelList(why=read_error(response.status_code, response.text).reason)
    try:
        data = response.json()
    except ValueError:
        return ModelList(why="DeepSeek 的模型清单不是 JSON。")
    rows = data.get("data") if isinstance(data, dict) else None
    items = tuple(
        ModelMeta(
            id=str(row.get("id") or ""),
            label=str(row.get("name") or row.get("id") or ""),
            context=_int(row.get("context_window")),
            max_output=_int(row.get("max_output_tokens")),
            abilities=_abilities(row),
        )
        for row in (rows or [])
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    )
    return ModelList(items=items, why="" if items else "平台没有返回任何模型。")
