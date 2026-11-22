"""LM Studio 对话模型插件（本机推理，OpenAI 兼容接口，默认不要密钥）。

LM Studio 把本机已下载的模型开成一个 OpenAI 兼容服务（默认
``http://127.0.0.1:1234/v1``）；这一家与云端那家的差别只有三处：

1. **模型默认留空**：请求前先问 ``/v1/models``，优先用当前已加载的模型——
   本机模型随用户自己的下载变化，写死一个默认名只会得到 404。
2. **不强制密钥**：默认不校验；只有用户在 LM Studio 打开「需要 API Token」时，
   才把设置里的令牌装进 ``Authorization``。
3. **形态是本机**：能力标签与说明按 ``KIND_LOCAL`` 下发，界面不说「按量计费」。

JSON 对象与字段级 Schema 都走 ``response_format``（LM Studio 两个都支持）；
工具按 OpenAI 形状原样下发。联网搜索与思考力度这一家没有，调用方要求时由
上游用例拒绝或忽略。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.engines.base import KIND_LOCAL
from app.llm.base import (
    ROLES,
    ChatRequest,
    ChatResult,
    LlmCapabilities,
    LlmError,
    LlmProvider,
    ModelList,
)
from app.llm.builtin.lmstudio import client
from app.llm.builtin.lmstudio.settings import LmStudioSettings

__all__ = ["LmStudioProvider"]

#: 自动挑选留空模型时给结构化输出用的名字。
_SCHEMA_NAME = "voicehub_response"

#: 可空采样参数（LM Studio 的 ``/v1/chat/completions`` 认这些键）。空着不发。
_SAMPLING_KEYS: tuple[str, ...] = (
    "top_p",
    "top_k",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)


def _as_settings(cfg: BaseModel) -> LmStudioSettings:
    """把框架递来的配置收窄成本插件模型；类型不对即实现错误，立即炸掉。"""
    assert isinstance(cfg, LmStudioSettings)
    return cfg


class LmStudioProvider(LlmProvider):
    """LM Studio（本机 OpenAI 兼容服务，模型清单随本机变化）。"""

    key = "lmstudio"
    label = "LM Studio"
    version = "0.1.0"
    homepage = "https://lmstudio.ai"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return LmStudioSettings

    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(
            kind=KIND_LOCAL,
            json_object=True,
            json_schema=True,
            model_listing=True,
            # 上下文随所加载的模型变化，不在插件级写死；清单里逐条给。
            max_context=0,
            abilities=("text",),
        )

    def readiness(self, cfg: BaseModel) -> str:
        # 本机服务不靠密钥；服务没起来会在调用时给出"连不上"的原话。
        return ""

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(
        self, cfg: LmStudioSettings, req: ChatRequest, model: str
    ) -> tuple[dict[str, Any], list[str]]:
        """统一请求 → 请求体，并带回降级说明。``model`` 由 :func:`client.resolve_model` 定。"""
        notes: list[str] = []
        body: dict[str, Any] = {
            "model": model,
            "messages": self._messages(req),
            "stream": False,
            "max_tokens": int(req.max_tokens or cfg.max_tokens),
            "temperature": float(cfg.temperature if req.temperature is None else req.temperature),
        }
        if req.stop:
            body["stop"] = list(req.stop)

        # 采样参数只在填了的时候发：空着交给 LM Studio 自己的模型设置，
        # 不拿插件里的默认值覆盖用户在那一侧调好的东西。
        for key in _SAMPLING_KEYS:
            value = getattr(cfg, key)
            if value is not None:
                body[key] = value

        # 字段级 Schema 优先于「只要是合法 JSON」；两个都给时以 Schema 为准。
        if req.json_schema:
            body["response_format"] = self._structured(req.json_schema)
        elif req.json_object:
            body["response_format"] = {"type": "json_object"}

        if req.tools:
            body["tools"] = [dict(tool) for tool in req.tools]
            body["tool_choice"] = req.tool_choice or "auto"
        return body, notes

    @staticmethod
    def _structured(schema: Mapping[str, Any]) -> dict[str, Any]:
        """字段级约束 → ``response_format``。

        ``json_schema`` 收两种写法：OpenAI 同形的整包（``type`` + ``json_schema``），
        或直接一份 JSON Schema——后者包成整包，免得调用方必须记住外层形状。
        """
        if schema.get("type") == "json_schema" or "json_schema" in schema:
            return dict(schema)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _SCHEMA_NAME,
                "strict": True,
                "schema": dict(schema),
            },
        }

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
                # OpenAI 同形服务只允许 user 消息带图：在这儿拦，比 400 原话好读。
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
        model = await client.resolve_model(settings, req.model)
        body, notes = self.build_body(settings, req, model)

        started = time.perf_counter()
        data = await client.chat(settings, body)
        latency = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError(f"LM Studio 的响应里没有 choices：{str(data)[:200]}")
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

        if not text.strip() and not calls and finish != "length":
            raise LlmError("LM Studio 返回了空回复。")
        if finish == "length":
            # 思考型模型可能把额度全用在思考上，正文为空；连通性是好的，别报成"空回复"。
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
        )

    async def list_models(self, cfg: BaseModel) -> ModelList:
        return await client.list_models(_as_settings(cfg))
