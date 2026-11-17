"""DeepSeek 对话模型插件（OpenAI 兼容接口，要密钥、要联网、按量计费）。

只做三件事：把统一请求装成 ``chat/completions`` 的请求体、发出去、把回复收窄成
:class:`~app.llm.base.ChatResult`。提示词、裁剪上下文、重试策略都不在这里——
那些是用例与上游的事。

三处**必须按模型分开**的行为：

1. 思考模式（2026-10 起平台默认开启，**本插件默认关闭**）会忽略 ``temperature``，且回复带
   ``reasoning_content``。这里把「跟随平台 / 关闭 / 三档力度」翻成 ``thinking``
   与 ``reasoning_effort`` 两个参数——把开关交给界面，别让温度设置静默失效。
2. Flash 认图（``content`` 用内容块数组），V4 Pro 只读文本；能力标签照这个声明，
   免得给 Pro 也标上「视觉」。
3. 服务端联网搜索（``deepseek_web_search``）只由 Flash 提供：请求了搜索但选的是
   别的模型时改用 Flash，并在 ``notes`` 里说明。搜索与整合都在平台侧完成，
   与自定义工具、JSON 输出互斥；引用从 ``message.annotations`` 收。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from app.llm.base import (
    ROLES,
    ChatRequest,
    ChatResult,
    Citation,
    LlmCapabilities,
    LlmError,
    LlmProvider,
    ModelList,
)
from app.llm.builtin.deepseek import client
from app.llm.builtin.deepseek.settings import (
    DEFAULT_MODEL,
    WEB_SEARCH_MODEL,
    DeepseekSettings,
)

__all__ = ["DeepseekProvider"]


def _as_settings(cfg: BaseModel) -> DeepseekSettings:
    """把框架递来的配置收窄成本插件模型；类型不对即实现错误，立即炸掉。"""
    assert isinstance(cfg, DeepseekSettings)
    return cfg


class DeepseekProvider(LlmProvider):
    """DeepSeek（V4.1 Flash 与 V4 Pro 两个模型）。"""

    key = "deepseek"
    label = "DeepSeek"
    version = "1.2.0"
    homepage = "https://platform.deepseek.com"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return DeepseekSettings

    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(
            json_object=True,
            # 平台只认 ``json_object``，不认 ``json_schema``（字段级约束由上游提示词负责）。
            json_schema=False,
            model_listing=True,
            web_search=True,
            max_context=1_000_000,  # 官方 1M；这一格只用于界面提示，不拦请求
            abilities=("text", "reasoning", "vision"),
        )

    def abilities(self, cfg: BaseModel | None = None) -> tuple[str, ...]:
        """Flash 认图，Pro 只读文本——能力跟着**选中的模型**走，不是整家一个说法。"""
        model = str(getattr(cfg, "model", "") or DEFAULT_MODEL).strip().lower()
        if "flash" in model:
            return ("text", "reasoning", "vision")
        return ("text", "reasoning")

    def readiness(self, cfg: BaseModel) -> str:
        if not getattr(cfg, "configured", False):
            return "还没填 API Key。"
        return ""

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: DeepseekSettings, req: ChatRequest) -> tuple[dict[str, Any], list[str]]:
        """统一请求 → 请求体，并带回降级说明。"""
        notes: list[str] = []
        model = (req.model or cfg.model or "").strip()
        if not model:
            raise LlmError("没有指定模型，也没有配默认模型。")

        body: dict[str, Any] = {
            "model": model,
            "messages": self._messages(req),
            "stream": False,
            "max_tokens": int(req.max_tokens or cfg.max_tokens),
            "temperature": float(cfg.temperature if req.temperature is None else req.temperature),
        }
        if req.stop:
            body["stop"] = list(req.stop)

        # 思考模式：请求里的 thinking 优先（这一次），否则跟配置。
        # auto 不发（跟随平台），off 明确关掉，三档力度打开并带上 effort。
        thinking = (req.thinking or "").strip() or cfg.thinking
        if thinking == "off":
            body["thinking"] = {"type": "disabled"}
        elif thinking in {"low", "high", "max"}:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = thinking

        wants_json = bool(req.json_object or req.json_schema)
        if req.web_search:
            # 服务端联网工具与自定义工具、结构化输出都不能同时声明（平台 400）。
            if req.tools:
                raise LlmError("联网搜索不能与自定义工具同时使用。")
            if wants_json:
                raise LlmError("联网搜索只返回纯文本，不能同时要求 JSON 输出。")
            if "flash" not in model.lower():
                notes.append(
                    f"联网搜索固定使用 {WEB_SEARCH_MODEL}；这次从 {model} 切换。"
                )
                model = WEB_SEARCH_MODEL
                body["model"] = model
            # 不设 tool_choice：平台不允许强制服务端工具。
            body["tools"] = [{"type": "deepseek_web_search"}]
        if wants_json:
            body["response_format"] = {"type": "json_object"}
            if req.json_schema:
                notes.append("DeepSeek 只认「必须是合法 JSON 对象」，不认字段级 Schema；约束由提示词负责。")
        if req.tools:
            body["tools"] = [dict(tool) for tool in req.tools]
            body["tool_choice"] = req.tool_choice or "auto"
        return body, notes

    @staticmethod
    def _messages(req: ChatRequest) -> list[dict[str, Any]]:
        if not req.messages:
            raise LlmError("对话请求里没有任何消息。")
        out: list[dict[str, Any]] = []
        for message in req.messages:
            role = (message.role or "").strip()
            if role not in ROLES:
                raise LlmError(f"不认识的角色「{role}」；只接受 {' / '.join(ROLES)}。")
            calls = [dict(call) for call in message.tool_calls]
            content = message.content
            if isinstance(content, str):
                # 工具调用那一轮的内容本来就是空的：有 tool_calls 就不能当成空消息丢掉。
                if not content.strip() and not calls:
                    continue
            elif isinstance(content, list):
                if not content:
                    continue
                # 平台只允许 user 消息带图：在这儿拦，报出来的话比 400 原话短。
                if role != "user" and any(part.get("type") != "text" for part in content):
                    raise LlmError("图片只能放在 user 消息里。")
            else:
                continue
            row: dict[str, Any] = {"role": role, "content": content}
            if calls:
                row["tool_calls"] = calls
            if message.tool_call_id:
                row["tool_call_id"] = message.tool_call_id
            out.append(row)
        if not out:
            raise LlmError("对话请求里的消息内容全是空的。")
        return out

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def chat(self, cfg: BaseModel, req: ChatRequest) -> ChatResult:
        settings = _as_settings(cfg)
        body, notes = self.build_body(settings, req)

        started = time.perf_counter()
        data = await client.chat(settings, body)
        latency = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError(f"DeepSeek 的响应里没有 choices：{str(data)[:200]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = str(message.get("content") or "")
        finish = str(first.get("finish_reason") or "")
        # 工具调用那一轮没有正文：``finish_reason`` 是 tool_calls，content 为空。
        calls = tuple(
            dict(item)
            for item in (message.get("tool_calls") or [])
            if isinstance(item, dict)
            and isinstance(item.get("function"), dict)
            and str(item["function"].get("name") or "").strip()
        )

        if finish == "content_filter":
            raise LlmError("DeepSeek 的内容审核拒答了这次请求。")
        if not text.strip() and not calls:
            raise LlmError("DeepSeek 返回了空回复。")
        if finish == "length":
            notes.append(
                f"回复被 max_tokens={body['max_tokens']} 截断——需要完整结果就调大「回复上限」。"
            )

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            text=text,
            model=str(data.get("model") or body["model"]),
            finish_reason=finish,
            usage=usage,
            notes=tuple(notes),
            latency_ms=latency,
            tool_calls=calls,
            citations=_citations(message),
        )

    async def list_models(self, cfg: BaseModel) -> ModelList:
        return await client.list_models(_as_settings(cfg))


def _citations(message: dict[str, Any]) -> tuple[Citation, ...]:
    """``message.annotations`` → 引用表。同一 URL 只留第一次出现。"""
    rows = message.get("annotations")
    if not isinstance(rows, list):
        return ()
    out: list[Citation] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "url_citation":
            continue
        payload = row.get("url_citation")
        if not isinstance(payload, dict):
            continue
        url = str(payload.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(Citation(url=url, title=str(payload.get("title") or "")))
    return tuple(out)
