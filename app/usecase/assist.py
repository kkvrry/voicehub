"""微型 AI 任务：一次结构化提案，不直接写数据。

任务由生产方定义（上下文、提示词、校验器、预览），这里只负责发一次
JSON 补全、容错解析、失败回喂一次、写日志与回答「现在能不能用」。

模型只拿提案权：校验、试跑与应用前复核都在调用方；应用仍走原有写接口。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.infra.loghub import CHANNEL
from app.llm.base import ChatMessage
from app.llm.json_text import parse_json
from app.usecase import llm as llm_uc

__all__ = [
    "AssistContext",
    "AssistError",
    "AssistInvalid",
    "AssistTask",
    "run_task",
    "status",
]

#: 一次任务最多发几次：第一次 + 校验失败后的唯一一次修复。
_MAX_ATTEMPTS = 2


class AssistError(RuntimeError):
    """面向用户的失败：没有拿到可用的提案，或这次任务不能做。

    由 API 层翻成 400；消息本身要能直接展示。
    """


class AssistInvalid(AssistError):
    """模型回复没通过校验：错误会回喂给模型再试一次。"""


@dataclass(frozen=True, slots=True)
class AssistContext:
    """一次任务需要的上下文。``service`` / ``library`` 由生产方传入，宿主不认类型。"""

    service: Any
    library: Any
    settings: Any
    project_id: str
    body: Mapping[str, Any]


class AssistTask(Protocol):
    """一个可复用的 AI 动作。

    ``build`` 把请求与项目数据收窄成提示词能用的形状；``validate`` 只做
    确定性校验；``preview`` 做服务端试跑并返回界面要画的那一包。

    可选异步钩子：``enrich(ctx, prepared)`` 在发提示词之前补上下文。
    """

    key: str
    label: str
    max_tokens: int

    def build(self, ctx: AssistContext) -> Any: ...

    def messages(self, prepared: Any) -> tuple[ChatMessage, ...]: ...

    def validate(self, prepared: Any, data: Mapping[str, Any]) -> dict[str, Any]: ...

    def preview(self, prepared: Any, proposal: Mapping[str, Any]) -> dict[str, Any]: ...


def _messages_payload(messages: tuple[ChatMessage, ...] | list[ChatMessage]) -> list[dict[str, Any]]:
    """消息 → 请求体。工具调用与回执一并带上，免得回喂时把上一轮的动作丢掉。"""
    out: list[dict[str, Any]] = []
    for message in messages:
        row: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            row["tool_calls"] = [dict(call) for call in message.tool_calls]
        if message.tool_call_id:
            row["tool_call_id"] = message.tool_call_id
        out.append(row)
    return out


async def run_task(
    task: AssistTask,
    ctx: AssistContext,
    *,
    chat: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """跑一次任务：结构化提案 → 校验/试跑 → 失败回喂一次 → 返回提案。

    ``chat`` 只给测试注入假 provider；正常路径走 :func:`app.usecase.llm.chat`。
    """
    prepared = task.build(ctx)
    enrich = getattr(task, "enrich", None)
    if enrich is not None:
        prepared = await enrich(ctx, prepared)
    messages = list(task.messages(prepared))
    body: dict[str, Any] = {
        "messages": _messages_payload(messages),
        "json_object": True,
        "temperature": 0,
        "thinking": "off",
        "max_tokens": int(getattr(task, "max_tokens", 0) or 0),
    }
    provider = str(getattr(ctx.settings, "analysis_channel", "") or "").strip()
    model = str(getattr(ctx.settings, "analysis_model", "") or "").strip()
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model

    send = chat or llm_uc.chat
    started = time.perf_counter()
    result: Mapping[str, Any] = {}
    proposal: dict[str, Any] = {}
    preview: dict[str, Any] = {}
    last_error = ""

    for attempt in range(_MAX_ATTEMPTS):
        result = await send(body)
        raw = str(result.get("text") or "")
        data = parse_json(raw)
        try:
            if not data:
                raise AssistInvalid("回复不是合法的 JSON 对象。")
            proposal = task.validate(prepared, data)
            preview = task.preview(prepared, proposal)
            break
        except AssistInvalid as exc:
            last_error = str(exc)
            if attempt + 1 >= _MAX_ATTEMPTS:
                raise AssistError(f"模型没有给出可用的结果：{last_error}") from None
            messages.append(ChatMessage("assistant", raw or "（空回复）"))
            messages.append(
                ChatMessage(
                    "user",
                    f"上一次回复没有通过校验：{last_error}\n"
                    "只输出修正后的 JSON，不要解释，不要加代码块。",
                )
            )
            body["messages"] = _messages_payload(messages)

    latency_ms = int((time.perf_counter() - started) * 1000)
    CHANNEL.info(
        f"AI 任务完成：{task.label} · {result.get('provider_label') or result.get('provider') or '—'} · "
        f"{result.get('model') or '—'} · {latency_ms} ms",
        source="assist",
        project=ctx.project_id,
        task=task.key,
        tokens=dict(result.get("usage") or {}),
    )
    return {
        "object": "assist_proposal",
        "task": task.key,
        "task_label": task.label,
        "provider": str(result.get("provider") or ""),
        "provider_label": str(result.get("provider_label") or ""),
        "model": str(result.get("model") or ""),
        "proposal": proposal,
        "preview": preview,
        "notes": list(result.get("notes") or []),
        "usage": dict(result.get("usage") or {}),
        "latency_ms": latency_ms,
    }


def status(settings: Any) -> dict[str, Any]:
    """AI 按钮的可用状态：没配好时给一句能直接展示的原因。"""
    wanted = str(getattr(settings, "analysis_channel", "") or "").strip()
    try:
        key = wanted or llm_uc.default_provider()
        if not key:
            return {
                "object": "assist_status",
                "ready": False,
                "provider": "",
                "provider_label": "",
                "model": "",
                "note": "还没有可用的对话模型：先在设置里配置一家。",
            }
        payload = llm_uc.settings_payload(key)
        values = payload.get("values") or {}
        model = str(values.get("model") or getattr(settings, "analysis_model", "") or "").strip()
        capabilities = payload.get("capabilities") or {}
        search = bool(capabilities.get("web_search"))
        return {
            "object": "assist_status",
            "ready": bool(payload.get("ready")),
            "provider": key,
            "provider_label": str(payload.get("label") or key),
            "model": model,
            "note": str(payload.get("note") or ""),
            "search": search,
            "search_note": "" if search else "当前通道不支持联网搜索。",
        }
    except Exception as exc:  # noqa: BLE001 - 状态端点只回答，不该让整页打不开
        return {
            "object": "assist_status",
            "ready": False,
            "provider": wanted,
            "provider_label": "",
            "model": "",
            "note": str(exc),
            "search": False,
            "search_note": "当前通道不支持联网搜索。",
        }
