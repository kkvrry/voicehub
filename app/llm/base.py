"""对话模型插件协议——**一家模型 = 一个目录**，与 TTS 引擎同构、不同契约。

为什么单开一域
--------------
引擎那边的契约是"给文本出声"：音色清单、音色克隆、参考音频、语气词映射。
对话模型的契约是"给消息回消息"：消息数组、温度、JSON 输出、上下文长度。
两者没有一项能力重合——硬塞进 :class:`~app.engines.base.EnginePlugin`，音色库、
通道、试听这些概念对它全部失效，界面只能靠分支绕开。

同构的部分照旧：``app/llm/builtin/<key>/`` 一台一个目录、自声明配置模型、
密钥只写本机配置且读时掩码、``describe()`` 一包给全界面。加一家模型 = 加一个目录。

不做的事
--------
- **不拼提示词**：提示词是用例的事，插件只负责把消息发出去、把回复收回来。
- **不猜模型能力**：能做什么由插件声明（:class:`LlmCapabilities`），调用方照它降级
  并在 ``notes`` 里留一句——静默降级比报错更难查。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# 形态词表与界面提示只在 engines 那一处定义：这里借用，不复制第二份。
from app.engines.base import KIND_CLOUD, KIND_LABELS, ui_hints

__all__ = [
    "ABILITY_LABELS",
    "CAPABILITY_LABELS",
    "ROLES",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "Citation",
    "LlmCapabilities",
    "LlmError",
    "LlmProvider",
    "ModelList",
    "ModelMeta",
    "describe_abilities",
    "describe_chat",
    "ui_hints",
]

#: 允许的角色。``tool`` 是工具调用的回执：模型要求调工具后，结果以这个角色回传。
#: 图片多模态仍不收：收了就要在每家插件里各写一遍映射。
ROLES: tuple[str, ...] = ("system", "user", "assistant", "tool")

#: **模型本身会什么**（文本 / 推理 / 视觉 / 音频）。界面按这组画彩色标签。
#: 与 :data:`CAPABILITY_LABELS` 分开：后者是协议能力（JSON、清单），不是模型能力。
ABILITY_LABELS: dict[str, tuple[str, str]] = {
    "text": ("文本", "读写文字"),
    "reasoning": ("推理", "先想后答：思考模式可开"),
    "vision": ("视觉", "能读图片：截图、图表、封面"),
    "audio": ("音频", "能听音频"),
}

#: 能力的界面说法（中文只在这一处写，前端不认识能力键）。
CAPABILITY_LABELS: dict[str, tuple[str, str]] = {
    "streaming": ("流式输出", "边生成边返回；关着时等整段回来"),
    "json_object": ("JSON 输出", "可以要求回复是合法 JSON 对象"),
    "json_schema": ("结构化输出", "可以给 JSON Schema 约束字段与类型"),
    "model_listing": ("模型清单", "能向平台查询可用模型"),
    "web_search": (
        "联网搜索",
        "模型可在对话里自行搜索网页并整合作答；搜索在平台侧执行，按搜索次数计费",
    ),
}


class LlmError(Exception):
    """对话调用失败。``reason`` 是中文说明，HTTP 层原样放进错误体。

    :param retryable: 原样重试是否有意义（限流、超时可重试；密钥错、参数错不可）。
    :param code: 错误码；缺省 ``llm_failed``。
    """

    def __init__(self, reason: str, *, retryable: bool = False, code: str = "llm_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code


# --------------------------------------------------------------------------- #
# 能力与消息
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LlmCapabilities:
    """一家模型能干什么——调用方照它决定要不要降级，界面照它讲能力。

    ``abilities`` 说的是**模型本身会什么**（推理 / 视觉 / 音频 / 文本），
    界面按 :data:`ABILITY_LABELS` 画彩色标签；其余字段是协议能力，调用方用。
    ``max_context`` 是**单次请求的上下文上限**（token）；0 = 插件没声明 = 不拦。
    """

    kind: str = KIND_CLOUD
    streaming: bool = False
    json_object: bool = False
    json_schema: bool = False
    model_listing: bool = False
    web_search: bool = False
    max_context: int = 0
    abilities: tuple[str, ...] = ("text",)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    #: 纯文本，或 OpenAI 同形的内容块（``{"type": "text"}`` / ``{"type": "image_url"}``）。
    content: str | list[dict[str, Any]]
    #: 模型要求调用的工具（assistant 消息才有）。形状照平台原样：
    #: ``{"id": ..., "type": "function", "function": {"name": ..., "arguments": "..."}}``。
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    #: 工具回执对应的调用 id（role=tool 才有）。
    tool_call_id: str = ""


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """一次补全请求的统一形状。

    :param model: 空串 = 用配置里的默认模型；给了就覆盖这一格（不改配置）。
    :param temperature: ``None`` = 用配置值；0 是合法取值，不能拿 0 当"没给"。
    :param max_tokens: 0 = 用配置值。
    :param json_object: 要求回复是 JSON 对象（``response_format``）。
    :param json_schema: 结构化输出约束。**给了它以它为准**；插件不支持时降级成
        ``json_object`` 并在 ``notes`` 里说一句。
    :param thinking: 这一次的思考模式：``""`` = 用插件配置，``off`` = 明确关掉，
        ``low`` / ``high`` / ``max`` = 打开并给力度。插件不认这个字段就忽略；
        关掉它温度才生效——结构化抽取这类任务不需要先想一遍。
    :param tools: 可调用的工具（OpenAI 同形 ``[{"type":"function","function":{...}}]``）。
        给了就允许模型先调工具、拿到结果再回话；空 = 不给工具。
    :param tool_choice: ``""`` / ``auto`` = 交给模型；``required`` = 必须调一次。
        只有给了 ``tools`` 才有意义。
    :param web_search: 声明平台的服务端联网搜索：是否搜索由模型判断，搜索与整合
        都在平台侧完成，宿主不执行任何工具。与 ``tools`` / JSON 输出是否可共存
        由插件按平台限制处理。
    """

    messages: tuple[ChatMessage, ...]
    model: str = ""
    temperature: float | None = None
    max_tokens: int = 0
    json_object: bool = False
    json_schema: Mapping[str, Any] | None = None
    stop: tuple[str, ...] = ()
    thinking: str = ""
    tools: tuple[Mapping[str, Any], ...] = ()
    tool_choice: str = ""
    web_search: bool = False


@dataclass(frozen=True, slots=True)
class Citation:
    """联网回答引用的一条来源。``title`` 未知时可能为空。"""

    url: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str = ""
    model: str = ""
    finish_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    #: 这次没能按原样生效的地方（降级、剔除）。空 = 完全照请求执行。
    notes: tuple[str, ...] = ()
    latency_ms: int = 0
    #: 模型这次要求调用的工具；空 = 它直接给了回答。``text`` 此时可能为空。
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    #: 联网回答的引用（按出现顺序去重）；没有联网时为空。
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelMeta:
    """平台清单里的一条。能力 / 上下文都照平台原话收窄，界面不另算。"""

    id: str
    label: str = ""
    context: int = 0
    max_output: int = 0
    abilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelList:
    """平台模型清单。拉不到不抛异常：``why`` 说清理由（空串 = 没事）。"""

    items: tuple[ModelMeta, ...] = ()
    why: str = ""


# --------------------------------------------------------------------------- #
# 插件基类
# --------------------------------------------------------------------------- #

#: 没有自带图标的插件用这个（一个中性的"对话"方块）。
_DEFAULT_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#8a8fa8"/>'
    '<path d="M9 11h14v9H14l-5 4z" fill="#fff" opacity=".9"/>'
    "</svg>"
)


class LlmProvider(ABC):
    """对话模型插件基类。

    子类至少要给 :attr:`key` / :attr:`label`，并实现 :meth:`chat`；
    配置、能力、模型清单都有可用缺省。
    """

    key: str = ""
    label: str = ""
    version: str = "0.1.0"
    #: 去哪查（界面"关于"里显示，空则不显示）。
    homepage: str = ""
    #: 额外当作机密的字段名（配置模型里标了 ``secret`` 的会自动进来）。
    secrets: tuple[str, ...] = ()

    # ---- 元信息 ---------------------------------------------------------- #

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        """要配什么——pydantic 模型即答案（空模型 = 不用配）。"""

        class EmptySettings(BaseModel):
            pass

        return EmptySettings

    @classmethod
    def secret_fields(cls) -> frozenset[str]:
        marked = set()
        for name, field_info in cls.settings_schema().model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("secret"):
                marked.add(name)
        return frozenset(marked | set(cls.secrets))

    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities()

    def abilities(self, cfg: BaseModel | None = None) -> tuple[str, ...]:
        """这一家（含**当前选中的模型**）会什么。默认照 :meth:`capabilities`。

        给了 ``cfg`` 才能区分同一家的不同模型（Flash 认图、Pro 不认）——
        静态能力表说不清这件事，界面就会给 Pro 也标上「视觉」。
        """
        return self.capabilities().abilities

    def readiness(self, cfg: BaseModel) -> str:
        """现在能不能用：空串 = 能，否则一句可读说明缺什么。"""
        return ""

    def icon_svg(self) -> str:
        """图标（SVG 文本）。默认读插件目录下的 ``icon.svg``，没有则回落中性图标。"""
        module = inspect.getmodule(type(self))
        module_file = getattr(module, "__file__", "") if module else ""
        if module_file:
            path = Path(module_file).resolve().parent / "icon.svg"
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - 读不动就回落，不让它拖死列表
                pass
        return _DEFAULT_ICON

    # ---- 能力 ------------------------------------------------------------ #

    @abstractmethod
    async def chat(self, cfg: BaseModel, req: ChatRequest) -> ChatResult:
        """发一次补全。失败抛 :class:`LlmError`（中文说明 + 是否可重试）。"""

    async def list_models(self, cfg: BaseModel) -> ModelList:
        return ModelList(why=f"{self.label} 不提供模型清单。")

    # ---- 界面用的自描述 -------------------------------------------------- #

    def describe(self, cfg: BaseModel | None = None) -> dict[str, Any]:
        """把插件讲清楚——``/api/v1/llm/providers`` 直接返回它，前端不写分支。

        ``cfg`` 有值时才说得出"当前选中的模型会什么"；没给就只讲这家通用的那几条。
        """
        caps = self.capabilities()
        kind_label, kind_note = KIND_LABELS.get(caps.kind, (caps.kind, ""))
        return {
            "key": self.key,
            "label": self.label,
            "version": self.version,
            "homepage": self.homepage,
            "kind": caps.kind,
            "kind_label": kind_label,
            "kind_note": kind_note,
            "icon_url": f"/api/v1/llm/providers/{self.key}/icon",
            "capabilities": {
                "kind": caps.kind,
                "streaming": caps.streaming,
                "json_object": caps.json_object,
                "json_schema": caps.json_schema,
                "model_listing": caps.model_listing,
                "web_search": caps.web_search,
                "max_context": caps.max_context,
                "abilities": list(self.abilities(cfg)),
            },
            "abilities": describe_abilities(self.abilities(cfg)),
            "capability_notes": describe_chat(caps),
            "settings_schema": self.settings_schema().model_json_schema(),
            "secret_fields": sorted(self.secret_fields()),
        }


def describe_abilities(keys: tuple[str, ...] | list[str]) -> list[dict[str, str]]:
    """能力键 → 中文标签（顺序照 :data:`ABILITY_LABELS`，稳定输出）。不认识的键丢掉。"""
    picked = set(keys or ())
    return [
        {"key": key, "label": label, "note": note}
        for key, (label, note) in ABILITY_LABELS.items()
        if key in picked
    ]


def describe_chat(caps: LlmCapabilities) -> list[dict[str, str]]:
    """能力 → 逐条说明（键序照 :data:`CAPABILITY_LABELS`，稳定输出）。"""
    notes = [
        {"key": key, "label": label, "note": note}
        for key, (label, note) in CAPABILITY_LABELS.items()
        if getattr(caps, key, False)
    ]
    if caps.max_context:
        notes.append(
            {
                "key": "max_context",
                "label": "上下文",
                "note": f"单次请求最多约 {caps.max_context} token，超了由上游负责裁剪",
            }
        )
    return notes
