"""对话模型用例：配置读写（密钥掩码）、模型清单、一次补全。

密钥口径与引擎一致：存 = Windows DPAPI（当前用户）加密后写入本机 ``config.json``；
读 = 解密后用，``GET`` 掩码为空串并另给 ``secrets_set``；写 = 掩码字段提交空串表示不改。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from app.infra import secrets as secret_store
from app.infra.config import load_config, save_config
from app.infra.loghub import CHANNEL
from app.llm.base import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    LlmError,
    LlmProvider,
    ModelList,
    describe_abilities,
)
from app.llm.registry import detect_provider, list_providers

__all__ = [
    "LlmConfigError",
    "chat",
    "default_provider",
    "models_of",
    "output_limit",
    "providers_payload",
    "research",
    "save_default_provider",
    "save_provider_settings",
    "settings_payload",
    "test_provider",
]


class LlmConfigError(Exception):
    """配置/调用相关的用例错误——api 层翻成 400/404，``str(exc)`` 是可读说明。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def _tolerant(schema: type[BaseModel], stored: Mapping[str, Any]) -> BaseModel:
    """把存下的字典读成模型：坏字段丢掉、其余保留，不让一家模型读不出配置。"""
    known = {k: v for k, v in (stored or {}).items() if k in schema.model_fields}
    try:
        return schema(**known)
    except ValidationError:
        good: dict[str, Any] = {}
        for name, value in known.items():
            try:
                schema.model_validate({name: value})
            except ValidationError:
                continue
            good[name] = value
        return schema(**good)


def require_provider(key: str) -> LlmProvider:
    provider = detect_provider(key)
    if provider is None:
        available = "、".join(sorted(list_providers())) or "（无）"
        raise LlmConfigError(f"模型 {key} 不存在。可用：{available}。")
    return provider


def _stored(provider: LlmProvider) -> dict[str, Any]:
    """配置段 → 可交给插件的字典：机密字段先解密，旧明文原样通过。"""
    raw = load_config().llm.get(provider.key) or {}
    return secret_store.reveal_fields(raw, provider.secret_fields())


def settings_for(provider: LlmProvider) -> BaseModel:
    """这家插件的当前配置。认实例不认 key：便于测试注入自定义插件。"""
    return _tolerant(provider.settings_schema(), _stored(provider))


def output_limit(provider_key: str = "") -> int:
    """这家通道配置里的单次回复上限（插件设置里的「回复上限」）；读不到给 0。

    调用方按它规划一次请求装多少行——**上限是通道自己的事**，不在别处写死：
    换一家模型、或在面板里调大「回复上限」，这边就跟着变。
    """
    key = str(provider_key or "").strip() or default_provider()
    if not key:
        return 0
    try:
        provider = require_provider(key)
    except LlmConfigError:
        return 0
    return max(0, int(getattr(settings_for(provider), "max_tokens", 0) or 0))


def _mask(provider: LlmProvider, model: BaseModel) -> dict[str, Any]:
    data = model.model_dump()
    for name in provider.secret_fields():
        if name in data:
            data[name] = ""
    return data


def _defaults(provider: LlmProvider) -> dict[str, Any]:
    properties = provider.settings_schema().model_json_schema().get("properties", {})
    return {name: spec["default"] for name, spec in properties.items() if "default" in spec}


def default_provider() -> str:
    """当前默认模型：配置里那家 → 第一家就绪的 → 第一家。空表返回空串。"""
    providers = list_providers()
    configured = (load_config().llm_default or "").strip()
    if configured in providers:
        return configured
    for key, provider in providers.items():
        if not provider.readiness(settings_for(provider)):
            return key
    return next(iter(providers), "")


def settings_payload(key: str) -> dict[str, Any]:
    """给界面的整包：掩码后的当前值 + 渲染表单要的一切。"""
    provider = require_provider(key)
    cfg = settings_for(provider)
    note = provider.readiness(cfg)
    return {
        **provider.describe(cfg),
        "values": _mask(provider, cfg),
        "secrets_set": [name for name in sorted(provider.secret_fields()) if getattr(cfg, name, "")],
        "ready": not note,
        "note": note,
        "defaults": _defaults(provider),
        "is_default": default_provider() == key,
    }


def providers_payload() -> list[dict[str, Any]]:
    """全部模型，按 key 排序；每家一包（与 ``/engines`` 同形）。"""
    return [settings_payload(key) for key in list_providers()]


def save_provider_settings(key: str, patch: Mapping[str, Any]) -> BaseModel:
    """保存配置。掩码字段提交空串 = 不改。

    框架级指令：``"_clear": ["api_key"]`` 显式清空机密（空串已被解释为"不改"）；
    ``"_reset": true`` 恢复默认（非机密回落插件默认值，机密保留）。
    """
    provider = require_provider(key)
    schema = provider.settings_schema()
    secrets = provider.secret_fields()

    body = dict(patch or {})
    reset = bool(body.pop("_reset", False))
    cleared = {name for name in (body.pop("_clear", None) or []) if name in secrets}

    stored = _stored(provider)
    if reset:
        stored = {name: value for name, value in stored.items() if name in secrets}
    clean: dict[str, Any] = {}
    for name, value in body.items():
        if name not in schema.model_fields:
            continue  # 不认识的键直接丢
        if name in secrets and name not in cleared and isinstance(value, str) and not value.strip():
            continue
        clean[name] = value
    for name in sorted(cleared):
        clean[name] = ""

    model = _tolerant(schema, {**stored, **clean})
    config = load_config()
    config.llm[key] = secret_store.seal_fields(model.model_dump(), secrets)
    save_config(config)
    return model


def save_default_provider(key: str) -> str:
    """选默认模型。空串 = 回到"按就绪状态自动挑"。"""
    cleaned = (key or "").strip()
    if cleaned:
        require_provider(cleaned)
    config = load_config()
    config.llm_default = cleaned
    save_config(config)
    return default_provider()


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #


def _content(raw: Any) -> str | list[dict[str, Any]]:
    """一条消息的内容：纯文本，或 OpenAI 同形的内容块数组（图片走这一路）。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [
            dict(part)
            for part in raw
            if isinstance(part, Mapping) and str(part.get("type") or "").strip()
        ]
        return parts
    return ""


def _tool_calls(raw: Any) -> tuple[dict[str, Any], ...]:
    """工具调用数组：只收形状对的项，其余丢弃（平台的回复也可能带脏数据）。"""
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping) or not str(function.get("name") or "").strip():
            continue
        out.append(dict(item))
    return tuple(out)


def _message(item: Any) -> ChatMessage | None:
    """一条消息：``ChatMessage`` 或同形的 dict 都收。"""

    if isinstance(item, ChatMessage):
        return item
    if not isinstance(item, Mapping):
        return None
    return ChatMessage(
        role=str(item.get("role") or ""),
        content=_content(item.get("content")),
        tool_calls=_tool_calls(item.get("tool_calls")),
        tool_call_id=str(item.get("tool_call_id") or ""),
    )


def _request(body: Mapping[str, Any]) -> ChatRequest:
    """外部请求体 → 统一请求。只做形状校验，不替调用方补提示词。

    ``messages`` 收 list 或 tuple，元素收 ``ChatMessage`` 或 dict：用例层两种写法
    都在用（``_messages_payload`` 转 dict，插件与用例直接拼对象），这里统一认。
    """
    raw = body.get("messages")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise LlmError("请求里没有 messages。")
    messages = tuple(
        message for message in (_message(item) for item in raw) if message is not None
    )
    if not messages:
        raise LlmError("messages 里没有一条合法消息（需要 role 与 content）。")

    temperature = body.get("temperature")
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            raise LlmError("temperature 只接受数字。") from None
    try:
        max_tokens = int(body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        raise LlmError("max_tokens 只接受整数。") from None

    schema = body.get("json_schema")
    thinking = str(body.get("thinking") or "").strip()
    if thinking and thinking not in {"off", "low", "high", "max"}:
        raise LlmError("thinking 只接受 off / low / high / max。")
    tools = body.get("tools")
    tool_choice = str(body.get("tool_choice") or "").strip()
    if tool_choice and tool_choice not in {"auto", "required", "none"}:
        raise LlmError("tool_choice 只接受 auto / required / none。")
    return ChatRequest(
        messages=messages,
        model=str(body.get("model") or "").strip(),
        temperature=temperature,
        max_tokens=max(0, max_tokens),
        json_object=bool(body.get("json_object")),
        json_schema=schema if isinstance(schema, Mapping) else None,
        stop=tuple(str(x) for x in (body.get("stop") or []) if str(x)),
        thinking=thinking,
        tools=tuple(
            dict(item) for item in tools if isinstance(item, Mapping)
        )
        if isinstance(tools, (list, tuple))
        else (),
        tool_choice=tool_choice,
        web_search=bool(body.get("web_search")),
    )


async def chat(body: Mapping[str, Any]) -> dict[str, Any]:
    """发一次补全。``provider`` 留空就用默认那家。"""
    wanted = str(body.get("provider") or "").strip() or default_provider()
    if not wanted:
        raise LlmConfigError("没有可用的对话模型：先装一家插件。")
    provider = require_provider(wanted)
    cfg = settings_for(provider)
    note = provider.readiness(cfg)
    if note:
        raise LlmError(f"{provider.label} 还没就绪：{note}", code="llm_not_configured")

    request = _request(body)
    if request.web_search and not provider.capabilities().web_search:
        raise LlmError(
            f"{provider.label} 不支持联网搜索。", code="llm_search_unsupported"
        )

    result: ChatResult = await provider.chat(cfg, request)
    searches = _server_searches(result.usage)
    CHANNEL.info(
        f"对话完成：{provider.label} · {result.model or '—'} · {_usage_text(result)} · "
        f"{result.latency_ms} ms" + (f" · 联网 {searches} 次" if searches else ""),
        source="llm",
        provider=provider.key,
        model=result.model,
        tokens=dict(result.usage or {}),
        searches=searches,
    )
    return {
        "object": "chat",
        "provider": provider.key,
        "provider_label": provider.label,
        "model": result.model,
        "text": result.text,
        "finish_reason": result.finish_reason,
        "usage": dict(result.usage or {}),
        "notes": list(result.notes),
        "latency_ms": result.latency_ms,
        "tool_calls": [dict(item) for item in result.tool_calls],
        "citations": [
            {"url": citation.url, "title": citation.title}
            for citation in result.citations
        ],
    }


def _usage_text(result: ChatResult) -> str:
    usage = result.usage or {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return "token 未提供"
    return f"输入 {prompt if prompt is not None else '—'} / 输出 {completion if completion is not None else '—'} token"


async def research(body: Mapping[str, Any]) -> dict[str, Any]:
    """一次自带联网的对话：模型自己决定搜不搜，搜索与整合都在平台侧完成。

    服务端搜索只返回纯文本，所以这里不接 JSON 约束：要结构化结果，调用方拿
    回答正文再发一次 :func:`chat`。这是「先查后抽取」两段式，宿主不执行工具循环。
    """
    payload = dict(body)
    if payload.get("json_object") or payload.get("json_schema"):
        raise LlmError("联网搜索只返回纯文本，不能同时要求 JSON 输出。")
    payload["web_search"] = True
    return await chat(payload)


def _server_searches(usage: Mapping[str, Any]) -> int:
    """平台给的计费搜索次数（``usage.server_tool_use.tool_search``）；没有给就是 0。"""
    counter = usage.get("server_tool_use")
    if not isinstance(counter, Mapping):
        return 0
    try:
        return max(0, int(counter.get("tool_search") or 0))
    except (TypeError, ValueError):
        return 0


async def test_provider(key: str) -> dict[str, Any]:
    """连通性自检：发一句极短的话，确认密钥、地址与模型都能用。

    **失败也是业务结果**（200 + ``ok:false``）：与拉清单同口径，界面照原话显示。
    用 ``max_tokens=8`` 压成本——这一步只为验证，不为取内容。
    """
    provider = require_provider(key)
    cfg = settings_for(provider)
    note = provider.readiness(cfg)
    if note:
        return {"ok": False, "note": note, "latency_ms": 0, "text": ""}
    started = time.perf_counter()
    try:
        result = await provider.chat(
            cfg,
            ChatRequest(
                messages=(ChatMessage("user", "只回复两个字：可用"),),
                max_tokens=8,
                temperature=0.0,
            ),
        )
    except LlmError as exc:
        return {"ok": False, "note": exc.reason, "latency_ms": 0, "text": ""}
    return {
        "ok": True,
        "note": "",
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model": result.model,
        "text": result.text,
    }


async def models_of(key: str) -> dict[str, Any]:
    """平台模型清单（只读，不落盘）。拉不到返回 ``ok:false`` + 理由。"""
    provider = require_provider(key)
    cfg = settings_for(provider)
    if not provider.capabilities().model_listing:
        return {
            "ok": False,
            "key": key,
            "items": [],
            "reason": f"{provider.label} 不提供模型清单。",
        }
    note = provider.readiness(cfg)
    if note:
        # 没配好就别发请求：缺密钥时带出去的 ``Bearer `` 是非法头，报出来的是
        # "Illegal header value"，与真正的"没填密钥"隔了一层。
        return {"ok": False, "key": key, "items": [], "reason": note}
    result: ModelList = await provider.list_models(cfg)
    items = [
        {
            "id": meta.id,
            "label": meta.label or meta.id,
            "context": meta.context,
            "max_output": meta.max_output,
            "abilities": describe_abilities(meta.abilities),
        }
        for meta in result.items
    ]
    return {
        "ok": bool(items),
        "key": key,
        "items": items,
        "reason": "" if items else (result.why or "本次未获取到模型清单。"),
    }
