"""响应模型：**接口契约的机器可读部分**（`/openapi.json` 由这些模型生成）。

设计原则：

1. **只有稳定形状才建模**。插件的自描述载荷（``/engines``、``/spec``、``/marks``、
   ``/tree``、``/asr``）的 schema 是**运行时数据**——它们由插件声明或由规范表推导，
   在这里写死一份等于抄第二份，加一台引擎就要改一处，且迟早对不上。那些端点靠
   ``summary`` 与 ``description`` 说明，并在文档里登记为例外。
2. **信封统一**：纯集合用 :class:`ListResponse`，单个资源用该资源模型（带 ``object``
   判别字段）。删除用 :class:`DeleteOut`。
3. 字段名与既有 JSON **逐字一致**：这里不重命名任何字段，只补类型与判别字段。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

__all__ = [
    "AudioAbsorbIn",
    "AudioAbsorbTargetIn",
    "AudioDeleteIn",
    "ChatIn",
    "ChatMessageIn",
    "ChatOut",
    "DeleteOut",
    "EngineChoiceOut",
    "HealthOut",
    "ListResponse",
    "LogSnapshotOut",
    "OutputOut",
    "SampleOut",
    "SynthesisOut",
    "TagOut",
    "VoiceDetailOut",
    "VoiceOut",
    "WarningOut",
]

T = TypeVar("T")


class ListResponse(BaseModel, Generic[T]):  # noqa: UP046 - pydantic 泛型沿用 Generic
    """集合信封。

    ``has_more`` 只在调用方显式传了 ``limit`` 时才可能为真：默认 ``limit=0``
    表示"一次给全"，此时它恒为假（老界面靠一次拿全量做本地筛选）。
    """

    object: Literal["list"] = "list"
    data: list[T] = Field(default_factory=list)
    has_more: bool = False


class SampleOut(BaseModel):
    object: Literal["sample"] = "sample"
    file: str
    text: str
    emotion: str
    tags: list[str] = Field(default_factory=list)
    params: list[dict[str, str]] = Field(default_factory=list)
    duration: float
    is_main: bool
    audio_url: str
    path: str


class VoiceOut(BaseModel):
    object: Literal["voice"] = "voice"
    id: str
    source: str
    is_local: bool
    engine: dict[str, Any] | None = None
    name: str
    shown_name: str
    category: str
    gender: str
    age: str
    language: str
    tags: list[str] = Field(default_factory=list)
    description: str
    #: 是不是设计音色（音色本体就是 ``payload.design_prompt`` 里那句描述）。
    #: 界面据此挂「设计」徽标、左栏据它分出一组；不吃 tag/分类这些可改的字段。
    is_design: bool = False
    hidden: bool
    favorite: bool
    main_sample: str
    main_audio_url: str
    sample_count: int
    #: 这条音色能落到哪些引擎（引擎键）。列表就要带上：指定音色的引擎下拉、
    #: 「能不能用这台」的提示都读它，缺了这一项前端只能猜。
    engines: list[str] = Field(default_factory=list)
    cover_url: str
    #: 当前封面是不是宿主生成的 ``avatar.*``（手工 cover.* 为 False）。
    avatar_generated: bool = False
    #: 目录里有没有图（``avatar.*`` 或手工 ``cover.*``）。「一键配头像」只补没有图
    #: 的那些：判据是"眼前有没有一张图"，不是"这张图是谁写的"。
    has_cover: bool = False


class VoiceDetailOut(VoiceOut):
    folder: str
    samples: list[SampleOut] = Field(default_factory=list)


class TagOut(BaseModel):
    object: Literal["tag"] = "tag"
    tag: str
    count: int


class EngineChoiceOut(BaseModel):
    """引擎下拉里的一项：键、显示名、形态与就绪状态。

    上游据此灰掉不可用的条目，不必再发一次请求。
    """

    object: Literal["engine_choice"] = "engine_choice"
    key: str
    label: str
    kind: str = ""
    kind_label: str = ""
    ready: bool = True
    #: audio.cpp 此刻有没有对应模型在跑；与 ready 不同维。
    running: bool = False
    #: 此刻能不能合成：云端看就绪、本地还要实例在跑。上游据此灰条目，不再自算。
    available: bool = False
    note: str = ""
    can_list: bool = False


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "voicehub"
    version: str
    uptime_s: int
    engines: dict[str, str] = Field(default_factory=dict)
    #: 监听在非回环地址且没设访问令牌：同网段任何设备都能操作，界面据此提示。
    lan_open: bool = False


class LogSnapshotOut(BaseModel):
    object: Literal["log_snapshot"] = "log_snapshot"
    lines: list[dict[str, Any]] = Field(default_factory=list)
    cursor: int
    dropped: bool


class WarningOut(BaseModel):
    """一条“未按预期生效”或“平台代为处理”的说明。

    ``source``：``engine`` = 请求里有、但该引擎未接受；``hub`` = VoiceHub 为完成
    请求所做的处理（转换、补全）。
    """

    code: str
    source: Literal["engine", "hub"]
    message: str


class SynthesisOut(BaseModel):
    object: Literal["synthesis"] = "synthesis"
    id: str
    created: int
    #: 实际使用的引擎键（请求写 ``auto`` 时由网关选定）。
    engine: str = ""
    format: str
    cost_note: str
    notes: list[str] = Field(default_factory=list)
    warnings: list[WarningOut] = Field(default_factory=list)
    subtitle_url: str
    bytes: int
    audio_url: str
    saved_path: str
    saved_rel: str
    persist: Literal["none", "cache", "sample"]


class DeleteOut(BaseModel):
    """删除结果。``trashed`` 只对"本地音色移除"有意义（进回收站的路径清单）。"""

    id: str
    object: str
    deleted: bool = True
    trashed: list[str] = Field(default_factory=list)


class OutputOut(BaseModel):
    object: Literal["output_config"] = "output_config"
    dir: str
    custom: bool
    engines: dict[str, str] = Field(default_factory=dict)
    #: 这次保存换目录前的旧目录（没换 = 空串）。
    previous_dir: str = ""
    #: 这次保存搬了什么（``migrate=true`` 且旧目录有东西时才有）。
    moved: dict[str, Any] | None = None


class StorageEntryOut(BaseModel):
    """一个存储位置：给界面显示路径、占用与是否可改。

    :param editable: 能否在设置页改。数据目录由环境变量决定，这里恒为 ``false``。
    :param locked_by: 不可改时是谁锁住的（如 ``VOICEHUB_ASR_DIR``），可空。
    """

    key: str
    label: str
    path: str
    value: str = ""
    editable: bool = True
    locked_by: str = ""
    exists: bool = True
    size: int = 0
    files: int = 0
    note: str = ""


class CachePolicyOut(BaseModel):
    """转换产物的回收线：上限（MB）与保留天数；``0`` = 那条规则不启用。"""

    limit_mb: int = 0
    keep_days: int = 0


class StorageOut(BaseModel):
    object: Literal["storage"] = "storage"
    entries: list[StorageEntryOut] = Field(default_factory=list)
    #: 转换产物的自动回收设置（设置页「数据与存储」里那两格）。
    cache: CachePolicyOut | None = None
    #: 改生成结果目录并选择迁移时，搬了什么。
    moved: dict[str, Any] | None = None


class AudioAbsorbTargetIn(BaseModel):
    """入库目标。

    ``origin`` 回这条音频原本的音色（生成物才有）、``voice`` 是选定的音色、
    ``new`` 是就地新建一条本地音色——三者都要经过同一套样例写入。
    """

    kind: Literal["origin", "voice", "new"] = "voice"
    voice_id: str = ""
    name: str = ""
    gender: str = ""
    language: str = ""
    #: 新建音色的归属文件夹（扫描根或它下面的一级子目录）。
    folder: str = ""
    #: 只作用于这一批里的第一条落点，不改变其它样例的主样例。
    main_sample: bool = False
    emotion: str = ""


class AudioAbsorbIn(BaseModel):
    """把历史里的条目登记为某条音色的样例。"""

    ids: list[str] = Field(default_factory=list)
    target: AudioAbsorbTargetIn = Field(default_factory=AudioAbsorbTargetIn)


class AudioDeleteIn(BaseModel):
    """移入回收站。生成物连 sidecar 记录一起摘。"""

    ids: list[str] = Field(default_factory=list)


class ChatMessageIn(BaseModel):
    """一条对话消息。角色只收 system / user / assistant（插件侧还校一遍）。

    ``content`` 可以是纯文本，也可以是 OpenAI 同形的内容块数组
    （``{"type": "text"}`` / ``{"type": "image_url"}``）——认图的那家模型走这一路。
    """

    role: str
    content: str | list[dict[str, Any]]


class ChatIn(BaseModel):
    """一次补全请求。

    ``provider`` 留空 = 用默认那家（``config.json`` 的 ``llm_default``，或第一家就绪的）。
    ``json_schema`` 给了就以它为准；不支持的插件降级并在响应 ``notes`` 里说明。
    """

    provider: str = ""
    model: str = ""
    messages: list[ChatMessageIn]
    temperature: float | None = None
    max_tokens: int = 0
    json_object: bool = False
    json_schema: dict[str, Any] | None = None
    stop: list[str] = Field(default_factory=list)
    #: 声明平台的服务端联网搜索：模型自己决定搜不搜、搜完直接整合作答。
    web_search: bool = False


class ChatOut(BaseModel):
    object: Literal["chat"] = "chat"
    provider: str
    provider_label: str = ""
    model: str = ""
    text: str
    finish_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    #: 这次没能按原样生效的地方（降级、截断）。
    notes: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    #: 联网回答引用的来源（按出现顺序去重）。
    citations: list[dict[str, str]] = Field(default_factory=list)
