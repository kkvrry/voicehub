"""对话模型插件域：注册、配置掩码、请求装配、错误映射、API 形状。

**不联网**：HTTP 一律走 ``httpx.MockTransport``，把发出去的请求体与回来的错误
逐条断言掉。真实 API Key 不进测试。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest

from app.llm.base import ChatMessage, ChatRequest, LlmError
from app.llm.builtin.deepseek.client import endpoint, read_error
from app.llm.builtin.deepseek.plugin import DeepseekProvider
from app.llm.builtin.deepseek.settings import DeepseekSettings
from app.llm.builtin.lmstudio.client import read_error as lmstudio_read_error
from app.llm.builtin.lmstudio.plugin import LmStudioProvider
from app.llm.builtin.lmstudio.settings import FOLLOW_LM_STUDIO, LmStudioSettings
from app.llm.registry import detect_provider, list_providers
from app.usecase.llm import LlmConfigError, _request, require_provider

# --------------------------------------------------------------------------- #
# 注册与自描述
# --------------------------------------------------------------------------- #


def test_builtin_deepseek_is_discovered() -> None:
    providers = list_providers()
    assert "deepseek" in providers
    assert providers["deepseek"].label == "DeepSeek"


def test_unknown_provider_is_explained() -> None:
    assert detect_provider("nope") is None
    with pytest.raises(LlmConfigError) as exc:
        require_provider("nope")
    assert "deepseek" in str(exc.value)


def test_client_creation_survives_bracketed_ipv6_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 客户端读系统代理；``[::1]`` 不能把分析打成 500。"""
    from app.llm.builtin.deepseek import client as deepseek_client

    value = "localhost,127.0.0.1,[::1],::1"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    async def run() -> bool:
        client = deepseek_client._client()
        try:
            return client.is_closed
        finally:
            await client.aclose()

    try:
        assert asyncio.run(run()) is False
    finally:
        deepseek_client._CLIENT = None
        deepseek_client._CLIENT_LOOP = None
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1,::1"


def test_describe_carries_schema_and_capabilities() -> None:
    data = DeepseekProvider().describe(DeepseekSettings())
    assert data["key"] == "deepseek"
    assert data["kind"] == "cloud"
    # 只认 JSON 对象，不认字段级 Schema——这一条决定了上游敢不敢要结构化输出。
    assert data["capabilities"]["json_object"] is True
    assert data["capabilities"]["json_schema"] is False
    assert data["capabilities"]["web_search"] is True
    assert "api_key" in data["settings_schema"]["properties"]
    assert data["secret_fields"] == ["api_key"]
    # 能力的中文说法只在后端一处，前端不写第二份。
    keys = {row["key"] for row in data["capability_notes"]}
    assert keys == {"json_object", "model_listing", "max_context", "web_search"}
    # 模型能力是另一套词：文本 / 推理 / 视觉（Flash 认图，Pro 不认）。
    assert [row["key"] for row in data["abilities"]] == ["text", "reasoning", "vision"]
    pro = DeepseekProvider().describe(DeepseekSettings(model="deepseek-v4-pro"))
    assert [row["key"] for row in pro["abilities"]] == ["text", "reasoning"]


# --------------------------------------------------------------------------- #
# 请求装配
# --------------------------------------------------------------------------- #


def _req(**kwargs: Any) -> ChatRequest:
    defaults = {"messages": (ChatMessage("user", "你好"),)}
    return ChatRequest(**{**defaults, **kwargs})


def test_request_assembly_keeps_tools_and_tool_messages() -> None:
    """调用方传 dict 请求时，工具与工具回执也要穿过统一请求。"""
    req = _request(
        {
            "messages": [
                {"role": "user", "content": "写画像"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "search_web", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "搜到一条", "tool_call_id": "c1"},
            ],
            "tools": [{"type": "function", "function": {"name": "search_web"}}],
            "tool_choice": "auto",
        }
    )
    assert req.messages[1].tool_calls[0]["id"] == "c1"
    assert req.messages[2].tool_call_id == "c1"
    assert req.tools[0]["function"]["name"] == "search_web"
    assert req.tool_choice == "auto"
    with pytest.raises(LlmError):
        _request({"messages": [{"role": "user", "content": "x"}], "tool_choice": "always"})


def test_request_accepts_message_objects_and_tuples() -> None:
    """用例层两种写法都在用：``ChatMessage`` 对象 + tuple，与 dict 列表等价。"""
    req = _request({"messages": (ChatMessage("user", "你好"),)})
    assert req.messages[0].role == "user"
    assert req.messages[0].content == "你好"
    assert req.web_search is False


def test_request_reads_server_web_search() -> None:
    req = _request({"messages": [{"role": "user", "content": "查一下"}], "web_search": True})
    assert req.web_search is True


def test_research_refuses_json_output() -> None:
    """核心只承诺纯文本：要结构化结果，调用方拿正文再发一次普通对话。"""
    from app.usecase.llm import research

    with pytest.raises(LlmError, match="纯文本"):
        asyncio.run(
            research(
                {
                    "messages": [{"role": "user", "content": "查一下"}],
                    "json_object": True,
                }
            )
        )


def test_json_object_goes_into_response_format() -> None:
    body, notes = DeepseekProvider().build_body(DeepseekSettings(), _req(json_object=True))
    assert body["response_format"] == {"type": "json_object"}
    assert notes == []


def test_reasoning_models_still_take_json_object() -> None:
    """2026-10 起两个模型都支持 JSON 输出：不再按模型名摘掉 ``response_format``。"""
    settings = DeepseekSettings(model="deepseek-v4-pro")
    body, notes = DeepseekProvider().build_body(settings, _req(json_object=True))
    assert body["response_format"] == {"type": "json_object"}
    assert notes == []


def test_thinking_switch_is_translated() -> None:
    """默认关闭；``auto`` 不发参数（跟随平台）；三档力度同时带 ``reasoning_effort``。"""
    default, _ = DeepseekProvider().build_body(DeepseekSettings(), _req())
    assert default["thinking"] == {"type": "disabled"}

    auto, _ = DeepseekProvider().build_body(DeepseekSettings(thinking="auto"), _req())
    assert "thinking" not in auto

    off, _ = DeepseekProvider().build_body(DeepseekSettings(thinking="off"), _req())
    assert off["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in off

    high, _ = DeepseekProvider().build_body(DeepseekSettings(thinking="high"), _req())
    assert high["thinking"] == {"type": "enabled"}
    assert high["reasoning_effort"] == "high"


def test_request_thinking_overrides_the_config() -> None:
    """请求里的 thinking 只影响这一次：分析关掉它，配置里的 auto/high 不动。"""
    off, _ = DeepseekProvider().build_body(DeepseekSettings(), _req(thinking="off"))
    assert off["thinking"] == {"type": "disabled"}

    high, _ = DeepseekProvider().build_body(
        DeepseekSettings(thinking="off"), _req(thinking="high")
    )
    assert high["thinking"] == {"type": "enabled"}
    assert high["reasoning_effort"] == "high"

    # 没给就用配置：配置 high 时仍然打开。
    inherited, _ = DeepseekProvider().build_body(DeepseekSettings(thinking="high"), _req())
    assert inherited["thinking"] == {"type": "enabled"}


def test_image_parts_pass_through() -> None:
    """认图的模型走内容块数组：插件不拆不改，原样交给平台。"""
    parts = [
        {"type": "text", "text": "这张图里是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    body, _ = DeepseekProvider().build_body(
        DeepseekSettings(), ChatRequest(messages=(ChatMessage("user", parts),))
    )
    assert body["messages"] == [{"role": "user", "content": parts}]


def test_images_outside_user_messages_are_refused() -> None:
    parts = [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]
    with pytest.raises(LlmError):
        DeepseekProvider().build_body(
            DeepseekSettings(), ChatRequest(messages=(ChatMessage("system", parts),))
        )


def test_json_schema_is_downgraded_with_note() -> None:
    body, notes = DeepseekProvider().build_body(DeepseekSettings(), _req(json_schema={"type": "object"}))
    assert body["response_format"] == {"type": "json_object"}
    assert any("Schema" in note for note in notes)


def test_temperature_zero_is_respected() -> None:
    """0 是合法温度，不能当成"没给"回落成默认值。"""
    body, _ = DeepseekProvider().build_body(DeepseekSettings(temperature=1.0), _req(temperature=0.0))
    assert body["temperature"] == 0.0


def test_bad_role_is_rejected_before_sending() -> None:
    with pytest.raises(LlmError):
        DeepseekProvider().build_body(
            DeepseekSettings(), ChatRequest(messages=(ChatMessage("narrator", "x"),))
        )


def test_tools_go_into_the_request_body() -> None:
    """工具按 OpenAI 形状原样下发；没写 tool_choice 时交给模型自己决定。"""
    tool = {
        "type": "function",
        "function": {
            "name": "search_web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
    body, _ = DeepseekProvider().build_body(DeepseekSettings(), _req(tools=(tool,)))
    assert body["tools"] == [tool]
    assert body["tool_choice"] == "auto"

    forced, _ = DeepseekProvider().build_body(
        DeepseekSettings(), _req(tools=(tool,), tool_choice="required")
    )
    assert forced["tool_choice"] == "required"


def test_server_web_search_is_declared_as_a_platform_tool() -> None:
    """联网搜索是平台服务端工具：只声明，不给 tool_choice，也不需要正文工具回执。"""
    body, notes = DeepseekProvider().build_body(DeepseekSettings(), _req(web_search=True))
    assert body["tools"] == [{"type": "deepseek_web_search"}]
    assert "tool_choice" not in body
    assert "response_format" not in body
    assert notes == []


def test_server_web_search_switches_to_flash() -> None:
    """服务端搜索只由 Flash 提供：选了 Pro 就这一次改用 Flash，并留下说明。"""
    body, notes = DeepseekProvider().build_body(
        DeepseekSettings(model="deepseek-v4-pro"), _req(web_search=True)
    )
    assert body["model"] == "deepseek-flash"
    assert any("deepseek-flash" in note for note in notes)


def test_server_web_search_refuses_json_and_custom_tools() -> None:
    """平台限制：服务端联网工具不能与 JSON 输出或自定义工具同时使用。"""
    with pytest.raises(LlmError, match="纯文本"):
        DeepseekProvider().build_body(
            DeepseekSettings(), _req(web_search=True, json_object=True)
        )
    tool = {"type": "function", "function": {"name": "search_web"}}
    with pytest.raises(LlmError, match="工具"):
        DeepseekProvider().build_body(
            DeepseekSettings(), _req(web_search=True, tools=(tool,))
        )


def test_tool_call_messages_keep_id_and_empty_content() -> None:
    """工具调用那一轮正文是空的，不能被当成空消息丢掉。"""
    calls = (
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "search_web", "arguments": '{"query":"x"}'},
        },
    )
    req = ChatRequest(
        messages=(
            ChatMessage("user", "写画像"),
            ChatMessage("assistant", "", tool_calls=calls),
            ChatMessage("tool", "搜到一条", tool_call_id="call-1"),
        )
    )
    body, _ = DeepseekProvider().build_body(DeepseekSettings(), req)
    assert body["messages"][1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": list(calls),
    }
    assert body["messages"][2] == {
        "role": "tool",
        "content": "搜到一条",
        "tool_call_id": "call-1",
    }


def test_endpoint_accepts_root_or_v1() -> None:
    assert endpoint("https://api.deepseek.com", "/chat/completions") == (
        "https://api.deepseek.com/chat/completions"
    )
    assert endpoint("https://api.deepseek.com/v1/", "/chat/completions") == (
        "https://api.deepseek.com/v1/chat/completions"
    )


def test_error_mapping_separates_auth_from_quota() -> None:
    """401 与 402 的处置不同（换密钥 vs 充值），不能被合并成一句"请求失败"。"""
    assert "401" in read_error(401, "bad key").reason
    assert "余额" in read_error(402, "").reason
    assert read_error(429, "").retryable is True
    assert read_error(401, "").retryable is False


# --------------------------------------------------------------------------- #
# LM Studio（本机，OpenAI 兼容，模型默认留空）
# --------------------------------------------------------------------------- #


def test_builtin_lmstudio_is_discovered() -> None:
    providers = list_providers()
    assert "lmstudio" in providers
    assert providers["lmstudio"].label == "LM Studio"


def test_lmstudio_defaults_to_the_loaded_model() -> None:
    """``model`` 默认不填：本机模型随用户下载变化，写死一个名字只会得到 404。"""
    settings = LmStudioSettings()
    assert settings.model == ""
    data = LmStudioProvider().describe(settings)
    assert data["kind"] == "local"
    assert data["capabilities"]["model_listing"] is True
    assert data["capabilities"]["json_schema"] is True
    assert data["settings_schema"]["properties"]["model"]["default"] == ""
    assert data["settings_schema"]["properties"]["model"]["placeholder"] == (
        "留空 = 当前已加载的模型"
    )


def test_lmstudio_root_accepts_site_root_or_v1() -> None:
    assert LmStudioSettings().root == "http://127.0.0.1:1234/v1"
    assert LmStudioSettings(base_url="http://127.0.0.1:1234").root == (
        "http://127.0.0.1:1234/v1"
    )
    assert LmStudioSettings(base_url="http://127.0.0.1:1234/v1/").root == (
        "http://127.0.0.1:1234/v1"
    )
    assert LmStudioSettings(base_url="127.0.0.1:1234").root == "http://127.0.0.1:1234/v1"


def test_lmstudio_body_keeps_schema_and_tools() -> None:
    """字段级 Schema 包成 OpenAI 整包；工具按原样下发，没写 tool_choice 时交给模型。"""
    tool = {"type": "function", "function": {"name": "search_web"}}
    req = _req(
        tools=(tool,),
        json_schema={"type": "object", "properties": {"rows": {"type": "array"}}},
    )
    body, notes = LmStudioProvider().build_body(LmStudioSettings(), req, "qwen3-8b")
    assert body["model"] == "qwen3-8b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["type"] == "object"
    assert body["tools"] == [tool]
    assert body["tool_choice"] == "auto"
    assert notes == []


def test_lmstudio_accepts_a_complete_response_format() -> None:
    """已经是 OpenAI 同形的整包时不再套一层。"""
    wrapper = {
        "type": "json_schema",
        "json_schema": {"name": "rows", "strict": True, "schema": {"type": "object"}},
    }
    body, _ = LmStudioProvider().build_body(
        LmStudioSettings(), _req(json_schema=wrapper), "qwen3-8b"
    )
    assert body["response_format"] == wrapper


def test_lmstudio_temperature_zero_is_respected() -> None:
    body, _ = LmStudioProvider().build_body(
        LmStudioSettings(temperature=0.7), _req(temperature=0.0), "qwen3-8b"
    )
    assert body["temperature"] == 0.0


def test_lmstudio_sampling_params_are_opted_in() -> None:
    """采样参数留空**不发这个键**：跟着 LM Studio 给该模型调好的那套走。"""
    provider = LmStudioProvider()
    body, _ = provider.build_body(LmStudioSettings(), _req(), "qwen3-8b")
    assert not {
        "top_p",
        "top_k",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "seed",
    } & set(body)

    tuned = LmStudioSettings(
        top_p=0.9,
        top_k=40,
        repeat_penalty=1.05,
        presence_penalty=0.2,
        frequency_penalty=-0.1,
        seed=7,
    )
    body, _ = provider.build_body(tuned, _req(), "qwen3-8b")
    assert body["top_p"] == 0.9
    assert body["top_k"] == 40
    assert body["repeat_penalty"] == 1.05
    assert body["presence_penalty"] == 0.2
    assert body["frequency_penalty"] == -0.1
    assert body["seed"] == 7


def test_lmstudio_sampling_fields_say_what_empty_means() -> None:
    """空白的采样格要说清自己代表什么；默认必须是"不发"。"""
    props = LmStudioProvider().describe(LmStudioSettings())["settings_schema"]["properties"]
    for key in ("top_p", "top_k", "repeat_penalty", "presence_penalty", "frequency_penalty"):
        assert props[key]["default"] is None
        assert props[key]["placeholder"] == FOLLOW_LM_STUDIO
        assert props[key]["group"] == "采样"
    assert props["seed"]["default"] is None
    assert props["seed"]["group"] == "采样"


def test_lmstudio_error_mapping() -> None:
    assert "API Token" in lmstudio_read_error(401, "").reason
    assert "模型" in lmstudio_read_error(404, "").reason
    assert lmstudio_read_error(429, "").retryable is True
    assert lmstudio_read_error(503, "").retryable is True
    assert lmstudio_read_error(400, "").retryable is False


# --------------------------------------------------------------------------- #
# 走 HTTP：MockTransport
# --------------------------------------------------------------------------- #


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """把 ``httpx.AsyncClient`` 换成带 MockTransport 的版本，返回收到的请求表。"""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)
    return seen


def _reply(text: str = "可用", *, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chat-1",
            "model": "deepseek-flash",
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def test_chat_sends_bearer_and_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_transport(monkeypatch, lambda request: _reply())
    out = asyncio.run(DeepseekProvider().chat(DeepseekSettings(api_key="sk-test"), _req(temperature=0.2)))
    assert out.text == "可用"
    assert out.usage["total_tokens"] == 5
    assert seen[0].headers["Authorization"] == "Bearer sk-test"
    sent = json.loads(seen[0].content)
    assert sent["stream"] is False
    assert sent["model"] == "deepseek-flash"


def test_truncated_reply_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda request: _reply(finish="length"))
    out = asyncio.run(DeepseekProvider().chat(DeepseekSettings(api_key="k"), _req()))
    assert any("max_tokens" in note for note in out.notes)


def test_tool_call_reply_is_parsed_not_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具调用那一轮没有正文：要原样带回 tool_calls，不能报「空回复」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_web",
                                        "arguments": '{"query":"《红楼梦》 王熙凤"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 9},
            },
        )

    _patch_transport(monkeypatch, handler)
    out = asyncio.run(DeepseekProvider().chat(DeepseekSettings(api_key="k"), _req()))

    assert out.text == ""
    assert out.tool_calls[0]["id"] == "call-1"
    assert out.tool_calls[0]["function"]["name"] == "search_web"
    assert out.finish_reason == "tool_calls"


def test_web_search_reply_carries_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端搜索的引用在 ``message.annotations``：按 URL 去重后原样带回。"""

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["tools"] == [{"type": "deepseek_web_search"}]
        assert "tool_choice" not in sent
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "冰岛人口约 39.4 万。",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://a.example/pop",
                                        "title": "冰岛人口",
                                    },
                                },
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://a.example/pop",
                                        "title": "重复来源",
                                    },
                                },
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://b.example/news",
                                        "title": "快讯",
                                    },
                                },
                            ],
                            "server_tool_calls": [
                                {
                                    "index": 0,
                                    "calls": [
                                        {
                                            "index": 0,
                                            "id": "s1",
                                            "type": "deepseek_web_search",
                                            "kind": "search",
                                            "status": "completed",
                                            "action": {
                                                "type": "search",
                                                "queries": ["Iceland population"],
                                            },
                                            "sources": [],
                                        }
                                    ],
                                }
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "total_tokens": 11,
                    "server_tool_use": {"tool_search": 1, "tool_fetch": 0},
                },
            },
        )

    _patch_transport(monkeypatch, handler)
    out = asyncio.run(
        DeepseekProvider().chat(
            DeepseekSettings(api_key="sk-test"), _req(web_search=True)
        )
    )
    assert out.text == "冰岛人口约 39.4 万。"
    assert [(item.url, item.title) for item in out.citations] == [
        ("https://a.example/pop", "冰岛人口"),
        ("https://b.example/news", "快讯"),
    ]
    assert out.usage["server_tool_use"]["tool_search"] == 1


def _lm_reply(text: str = "可用", *, model: str = "qwen3-8b", finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def test_lmstudio_blank_model_uses_the_loaded_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """没填模型时不发空 ``model``：先问清单，跳过向量模型，优先已加载的那条。"""
    sent_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "nomic-embed-text-v1.5", "type": "embedding"},
                        {
                            "key": "qwen3-8b",
                            "type": "llm",
                            "loaded_instances": [{"id": "qwen3-8b"}],
                        },
                    ]
                },
            )
        if request.url.path.endswith("/models"):
            raise AssertionError(f"不该再走 OpenAI 清单：{request.url.path}")
        sent_models.append(json.loads(request.content)["model"])
        return _lm_reply()

    _patch_transport(monkeypatch, handler)
    out = asyncio.run(LmStudioProvider().chat(LmStudioSettings(), _req()))

    assert sent_models == ["qwen3-8b"]
    assert out.text == "可用"
    assert out.model == "qwen3-8b"
    assert out.usage["total_tokens"] == 5


def test_lmstudio_without_a_model_explains_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(LlmError, match="加载"):
        asyncio.run(LmStudioProvider().chat(LmStudioSettings(), _req()))


def test_lmstudio_token_is_optional_and_sent_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认不带 ``Authorization``；填了令牌才带上——空 key 拼非法头的问题在云端那边。"""
    seen = _patch_transport(monkeypatch, lambda request: _lm_reply())

    asyncio.run(LmStudioProvider().chat(LmStudioSettings(model="qwen3-8b"), _req()))
    assert "authorization" not in seen[0].headers

    asyncio.run(
        LmStudioProvider().chat(
            LmStudioSettings(model="qwen3-8b", api_key="lm-token"), _req()
        )
    )
    assert seen[1].headers["Authorization"] == "Bearer lm-token"


def test_lmstudio_truncated_empty_reply_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """思考型模型可能把额度全用在思考上：正文为空但连通正常，不报"空回复"。"""
    _patch_transport(monkeypatch, lambda request: _lm_reply("", finish="length"))
    out = asyncio.run(
        LmStudioProvider().chat(LmStudioSettings(model="qwen3-8b"), _req())
    )
    assert out.text == ""
    assert any("max_tokens" in note for note in out.notes)


def test_lmstudio_model_list_skips_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """REST 清单里有向量模型与上下文：向量不列，上下文照它给。"""
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "models": [
                    {
                        "key": "qwen3-8b",
                        "type": "llm",
                        "display_name": "Qwen3 8B",
                        "max_context_length": 32768,
                        "capabilities": {"vision": False, "reasoning": {"default": "on"}},
                    },
                    {"key": "nomic-embed-text-v1.5", "type": "embedding"},
                ]
            },
        ),
    )
    listing = asyncio.run(LmStudioProvider().list_models(LmStudioSettings()))
    assert [item.id for item in listing.items] == ["qwen3-8b"]
    assert listing.items[0].label == "Qwen3 8B"
    assert listing.items[0].context == 32768
    assert listing.items[0].abilities == ("text", "reasoning")


def test_lmstudio_falls_back_to_the_openai_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧版本没有 REST 清单时不报错：/v1/models 的 id 也够解析与列出。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})

    _patch_transport(monkeypatch, handler)
    listing = asyncio.run(LmStudioProvider().list_models(LmStudioSettings()))
    assert [item.id for item in listing.items] == ["qwen3-8b"]


def test_lmstudio_unreachable_service_says_where_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(LlmError, match="本地服务"):
        asyncio.run(LmStudioProvider().chat(LmStudioSettings(model="qwen3-8b"), _req()))


# --------------------------------------------------------------------------- #
# API 形状
# --------------------------------------------------------------------------- #


def test_provider_list_masks_secret(client) -> None:
    saved = client.put(
        "/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live", "model": "deepseek-flash"}
    )
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["values"]["api_key"] == ""  # 读接口只给掩码
    assert payload["secrets_set"] == ["api_key"]
    assert payload["ready"] is True

    listing = client.get("/api/v1/llm/providers").json()["data"]
    row = next(item for item in listing if item["key"] == "deepseek")
    assert row["values"]["api_key"] == ""


def test_blank_secret_keeps_the_stored_one(client) -> None:
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live"})
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "", "max_tokens": 2048})
    payload = client.get("/api/v1/llm/providers/deepseek").json()
    assert payload["secrets_set"] == ["api_key"]
    assert payload["values"]["max_tokens"] == 2048


def test_clear_removes_the_secret(client) -> None:
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live"})
    payload = client.put(
        "/api/v1/llm/providers/deepseek/settings", json={"_clear": ["api_key"]}
    ).json()
    assert payload["secrets_set"] == []
    assert payload["ready"] is False


def test_chat_without_key_is_refused_with_reason(client) -> None:
    # 指名 DeepSeek：LM Studio 不需要密钥，会接管"没指定 provider"的默认位。
    resp = client.post(
        "/api/v1/llm/chat",
        json={"provider": "deepseek", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "llm_not_configured"
    assert "API Key" in error["message"]


def test_chat_endpoint_returns_notes_and_usage(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda request: _reply("已经可用"))
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live"})

    resp = client.post(
        "/api/v1/llm/chat",
        json={
            "provider": "deepseek",
            "messages": [{"role": "user", "content": "只回复四个字"}],
            "json_schema": {"type": "object"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat"
    assert body["text"] == "已经可用"
    assert body["provider"] == "deepseek"
    assert body["usage"]["total_tokens"] == 5
    assert any("Schema" in note for note in body["notes"])


def test_chat_endpoint_accepts_image_blocks(client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_transport(monkeypatch, lambda request: _reply("图里是鲸鱼"))
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live"})
    parts = [
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "https://example.com/whale.png"}},
    ]
    resp = client.post(
        "/api/v1/llm/chat",
        json={"provider": "deepseek", "messages": [{"role": "user", "content": parts}]},
    )
    assert resp.status_code == 200
    sent = json.loads(seen[0].content)
    assert sent["messages"][0]["content"] == parts


def test_models_endpoint_reports_platform_list(client, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-live"})
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-flash",
                        "name": "DeepSeek V4.1 Flash",
                        "context_window": 1_000_000,
                        "max_output_tokens": 393_216,
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
                    },
                    {
                        "id": "deepseek-v4-pro",
                        "name": "DeepSeek V4 Pro",
                        "context_window": 1_000_000,
                        "input_modalities": ["text"],
                    },
                ]
            },
        ),
    )
    body = client.get("/api/v1/llm/providers/deepseek/models").json()
    assert body["ok"] is True
    ids = [item["id"] for item in body["items"]]
    assert ids == ["deepseek-flash", "deepseek-v4-pro"]
    flash = body["items"][0]
    assert flash["context"] == 1_000_000
    assert [row["key"] for row in flash["abilities"]] == ["text", "reasoning", "vision"]
    pro = body["items"][1]
    assert [row["key"] for row in pro["abilities"]] == ["text"]


def test_test_endpoint_reports_failure_as_business_result(client, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put("/api/v1/llm/providers/deepseek/settings", json={"api_key": "sk-bad"})
    _patch_transport(monkeypatch, lambda request: httpx.Response(401, text="invalid key"))
    body = client.post("/api/v1/llm/providers/deepseek/test").json()
    assert body["ok"] is False
    assert "401" in body["note"]


def test_default_provider_round_trip(client) -> None:
    body = client.put("/api/v1/llm/default", json={"key": "deepseek"}).json()
    assert body["key"] == "deepseek"


def test_lmstudio_settings_keep_the_model_blank(client) -> None:
    payload = client.get("/api/v1/llm/providers/lmstudio").json()
    assert payload["values"]["model"] == ""
    assert payload["defaults"]["model"] == ""
    assert payload["ready"] is True


def test_lmstudio_chat_resolves_the_loaded_model(client, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"key": "qwen3-8b", "type": "llm", "loaded_instances": []}
                    ]
                },
            )
        sent.append(json.loads(request.content))
        return _lm_reply("已经可用")

    _patch_transport(monkeypatch, handler)
    resp = client.post(
        "/api/v1/llm/chat",
        json={"provider": "lmstudio", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "qwen3-8b"
    assert body["text"] == "已经可用"
    assert sent[0]["model"] == "qwen3-8b"


class TestReplyParsing:
    """回复解析是宿主与有声书插件共用的，两边的容错口径必须一致。"""

    def test_code_fence_is_stripped(self) -> None:
        from app.llm.json_text import parse_json

        assert parse_json('```json\n{"rows": []}\n```') == {"rows": []}

    def test_text_around_the_object_is_ignored(self) -> None:
        from app.llm.json_text import parse_json

        assert parse_json('好的，结果如下：\n{"rows": [1]}\n如需调整请告诉我。') == {"rows": [1]}

    def test_reply_without_an_object_is_empty(self) -> None:
        from app.llm.json_text import parse_json

        assert parse_json("我无法完成这个请求。") == {}
        assert parse_json("") == {}
        # 数组也是合法 JSON，但这一层只承诺对象。
        assert parse_json("[1, 2]") == {}
