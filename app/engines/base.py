"""引擎插件协议——**一台引擎 = 一个目录**，自描述、可扩展。

设计口径（docs/design.md §4–5）：

- **发现**走 ``app/engines/builtin/<key>/`` 子目录扫描（内置）与
  ``importlib.metadata`` entry points（第三方包）。装上一个包即被发现，
  不用改 VoiceHub 一行代码；同名 entry point 覆盖内置。
- **一台引擎一个目录**：请求装配、配置模型、音色获取、参数映射、图标各就各位。
  单文件的写法（audiobook 那边）省事，但加第三项能力时那个文件就会开始互相牵扯。
- **形态由插件自己声明**（:data:`KIND_CLOUD` / :data:`KIND_LOCAL`）：
  云端服务要密钥、要联网、按量计费；本机推理音色在磁盘上、请求不出机器。
  界面照它分组与提示，而不是靠硬编码名字。
- **拉不到清单不抛异常**——返回 ``VoiceList(why=...)`` 说清理由
  （空表与"这个平台没有音色"长得一模一样，那句话必须有人说）。

新增一台引擎要写的，就是那一个目录：
``plugin.py``（元信息 + 能力 + 契约）、``settings.py``（pydantic 配置）、
``client.py``（怎么调）、``voices.py``（响应怎么解析）、``mapping.py``（参数怎么落）、
``icon.svg``（图标）。
"""

from __future__ import annotations

import inspect
from abc import ABC
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.marks import engine_marks, render_marks
from app.domain.models import LANGUAGE_CHOICES
from app.domain.request_spec import SPEC_FIELDS
from app.domain.uischema import ui_hints
from app.engines.mapping import EngineMapping, transform_label
from app.engines.traits import detect_language

__all__ = [
    "CAPABILITY_LABELS",
    "CODES_ENGINE_UNAVAILABLE",
    "CODE_NOT_ENABLED",
    "CODE_NOT_FOUND",
    "CODE_RATE_LIMITED",
    "CODE_UNREACHABLE",
    "DUAL_EXPRESSION_NOTE",
    "KINDS",
    "KIND_CLOUD",
    "KIND_LABELS",
    "KIND_LOCAL",
    "ORPHAN_LABEL",
    "SECTION_LABELS",
    "Capabilities",
    "CloneRequest",
    "EngineError",
    "EnginePlugin",
    "SynthesisRequest",
    "SynthesisResult",
    "SynthesisSpec",
    "UiSection",
    "VoiceList",
    "VoiceMeta",
    "language_items",
    "language_names",
    "param_label",
    "supported_language_codes",
    "ui_hints",
]

#: 规范参数的中文名（唯一产地是 ``domain.request_spec``）。插件报"这次哪几项没落点"
#: 时用它，免得每个插件再抄一份中文名。
_SPEC_LABELS: dict[str, str] = {item.key: item.label for item in SPEC_FIELDS}
_SPEC_FIELDS = {item.key: item for item in SPEC_FIELDS}

#: 情绪与引导指令各有原生落点时的那条约束（见 :meth:`EnginePlugin.dual_expression`）。
#: 装配侧按它真的不发情绪（``usecase.gateway``），面板按它写进「能力与参数」页，
#: 两处同源，改口径只改这一句。
DUAL_EXPRESSION_NOTE = (
    "情绪与引导指令都有原生落点：一次请求只发一路，带引导指令时不发送情绪——"
    "两路风格叠在一起，引擎各让一半，读法反而失真。"
)


def param_label(key: str) -> str:
    """规范参数的界面名；不属于规范的键原样返回。"""
    return _SPEC_LABELS.get(key, key)


#: 语言代码 → 界面说法。唯一产地在 ``domain.models.LANGUAGE_CHOICES``；
#: 插件在 :attr:`Capabilities.languages` 里填的就是那批代码。
_LANGUAGE_NAMES: dict[str, str] = {value: label for value, label in LANGUAGE_CHOICES}


def language_names(codes: tuple[str, ...]) -> list[str]:
    """语言代码 → 界面说法；词表外的代码原样返回（标签不是白名单）。"""
    return [_LANGUAGE_NAMES.get(code, code) for code in codes]


def language_items(caps: Capabilities) -> list[dict[str, str]]:
    """「支持语言」那一栏要摆的语言：``{code, name}``，**去掉同时算方言的那几个**。

    粤语既是语言代码（``yue``，筛选栏在用）又是方言。清单里两处各摆一遍就是重复：
    方言已经单独一行，语言这一行不再重复它。代码表 ``Capabilities.languages`` 不动，
    去重只发生在展示层。
    """
    dialects = set(caps.dialects)
    items: list[dict[str, str]] = []
    for code in caps.languages:
        name = _LANGUAGE_NAMES.get(code, code)
        if name in dialects:
            continue
        items.append({"code": code, "name": name})
    return items


def supported_language_codes(caps: Capabilities) -> frozenset[str]:
    """这台引擎真正能念的语言代码：``languages`` 加方言说法归一成的代码。

    方言语种（``dialects``）登记的是界面说法（``四川话``），用户选的是代码
    （``cmn-cd``）；两边要对上，靠 ``traits`` 那张写法表。归一不出代码的方言
    （如 ``东北话``）不进集合——统一词表里也没有对应的可选项。
    """
    codes = set(caps.languages)
    for label in caps.dialects:
        code = detect_language(label)
        if code:
            codes.add(code)
    return frozenset(codes)


#: 平台限流（RPM / 配额）的错误码。网关据此退避重试并给同一引擎降温，
#: 不再当成"这一次的参数有问题"。插件把平台的限流回复翻成这个码。
CODE_RATE_LIMITED = "rate_limited"

#: 服务压根不在线（连接被拒 / 端口没人听）的错误码。批量合成据此**停掉这台引擎**：
#: 服务没起时，后面每一行都只是白撞一次连接，把行状态刷成失败、进度刷成满屏错误。
#: 与 ``engine_failed`` 的区别：后者是"服务在，但它说这次不行"。
CODE_UNREACHABLE = "engine_unreachable"

#: 这台引擎当前不在用：已停用，或插件已卸载。计划期就该摘掉这些行，但计划算完到
#: 跑完之间开关还是可能被关——那时每一行都会撞同一个错误，原因也只有一个。
CODE_NOT_ENABLED = "engine_not_enabled"
CODE_NOT_FOUND = "engine_not_found"

#: **整台做不了**的错误码：换哪一行都是同一个结果。批量合成据此停掉这台引擎，
#: 剩余行不发、不标失败，只记一条原因（见 ``audio_stage`` 的 ``halt_engine``）。
CODES_ENGINE_UNAVAILABLE: frozenset[str] = frozenset(
    {CODE_UNREACHABLE, CODE_NOT_ENABLED, CODE_NOT_FOUND}
)


class EngineError(Exception):
    """引擎执行失败。``reason`` 是中文说明，HTTP 层原样放进错误体。

    :param reason: 面向用户的说明（严格、简洁、专业）。
    :param retryable: 调用方是否可以原样重试（网关只对**本机**引擎消费这一位）。
    :param code: 错误码（见 ``app.api.errors.CODES``）；缺省 ``engine_failed``。
    """

    def __init__(self, reason: str, *, retryable: bool = False, code: str = "engine_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code


# --------------------------------------------------------------------------- #
# 部署形态
# --------------------------------------------------------------------------- #

#: 云端服务：要 API 密钥、要联网、**按量计费**。界面上要说清"这一步会花钱"。
KIND_CLOUD = "cloud"
#: 本机推理：音色是磁盘目录，请求不出这台机器。
KIND_LOCAL = "local"

KINDS: tuple[str, ...] = (KIND_CLOUD, KIND_LOCAL)

#: 形态的显示名与一句话说明（界面直接用它，不在前端再写一份中文）。
KIND_LABELS: dict[str, tuple[str, str]] = {
    KIND_CLOUD: ("云端", "云端服务，需要密钥，按量计费"),
    KIND_LOCAL: ("本地", "本地推理，音色与推理均在本机"),
}

#: **插件已经不在了**的来源（库里还留着它的音色）在界面上怎么念。
#: 它不是第三种 ``KIND``：单开常量与标志位（``orphan``），界面照它分支。
ORPHAN_LABEL: tuple[str, str] = (
    "已卸载",
    "这台引擎的插件已经不在了，但库里还有它的音色——点一下只在音色库里看这些音色。",
)

#: 能力的界面说法（中文只在这一处写，前端不认识能力键）。
#:
#: 「音色克隆」不按路由字段一一列出：``voice_cloning`` / ``sample_cloning`` /
#: ``reference_audio`` 是三种实现，页面上只显示一条，说明由 :meth:`_capability_notes`
#: 按实现挑选。``reference_audio`` 因此不在这张表里——它只在「引擎要求」里出现。
CAPABILITY_LABELS: dict[str, tuple[str, str]] = {
    "voice_listing": ("内置音色", "平台自带或内置的音色，可直接选用"),
    "voice_cloning": ("音色克隆", "上传音频并注册为可复用的音色"),
    "voice_design": ("音色设计", "用文字描述生成音色并入库"),
    "instruction_control": ("引导指令", "支持用自然语言描述语气、风格与演法"),
    "emotion_control": ("情绪控制", "支持情绪参数"),
}

#: ``describe()["capabilities"]`` 不逐项下发的字段：语言面在顶层 ``languages`` 里，
#: 样本容器只给核心校验用。其余字段**自动**随能力包下发——加字段不用再改第二处。
_CAPABILITY_FIELDS_HIDDEN = frozenset(
    {"sample_formats", "languages", "dialects", "cross_lingual", "language_note", "language_hint"}
)

#: 引擎页面分区的说法与说明（页签照它画，中文只在这一处写）。
#: 「音色」的说明由生效通道决定，在 ``page_sections`` 里覆盖后下发。
SECTION_LABELS: dict[str, tuple[str, str]] = {
    "settings": ("设置", "引擎参数，表单由插件的配置模型生成"),
    "voices": ("音色", ""),
    "clone": ("音色克隆", "以已有音频注册可长期使用的音色"),
    "design": ("音色设计", "以文字描述注册可长期使用的音色"),
    "about": ("能力与参数", "能力清单、参数映射与引擎要求"),
}


@dataclass(frozen=True, slots=True)
class UiSection:
    """引擎页面的一块（一个页签）。页面结构由插件声明，前端不认识任何引擎与能力键。

    ``meta`` 是这一块要显示给用户的**事实**（如克隆/设计各用哪个模型），由插件
    声明、前端只渲染——模型 id 不该在 Vue 里写死。
    """

    key: str
    label: str
    note: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """这台引擎能干什么——界面按它渲染按钮，**不能试听的音色不出现点了才报错的 ▶**。"""

    kind: str = KIND_LOCAL  # 部署形态，见 KINDS
    voice_listing: bool = False  # 能不能拉平台音色清单
    voice_cloning: bool = False  # 支不支持音色克隆（上传本地音频换一个音色 id）
    #: 克隆没有“平台音色 id”可登记，音色 = 一段参考样本（MiMo 的 voiceclone）：
    #: 克隆成功时把样本存进它自己的样例目录，以后每次合成都带着这段样本去。
    sample_cloning: bool = False
    #: ``sample_cloning`` 收哪些样本容器（小写、不带点）。空 = 用核心的保守默认
    #: ``("mp3", "wav")``；只有确实不收这两种的引擎才需要声明得更窄或更宽。
    sample_formats: tuple[str, ...] = ()
    #: 用一句文字描述生成音色（不上传音频，也不换回平台音色 id）。描述随音色入库，
    #: 以后每次合成都带着它重新生成——平台侧不保存音色，见各插件的说明。
    voice_design: bool = False
    reference_audio: bool = False  # 接不接受参考音频（本地音色 → 引擎）
    #: 支持自由文本引导指令（语气、风格、演法）。与情绪分开：情绪是枚举，
    #: 引导是指令；两者都有原生落点时网关只发指令（见 ``_settle_expression``）。
    instruction_control: bool = False
    emotion_control: bool = False
    #: 单次文本上限（**字符**，按要发出去的正文算，语气词标签不计）。
    #:
    #: ``0`` 不等于「没查过」这一个意思，别拿它当待办清单：本机那几台都会自己
    #: 分块（audio.cpp 的 ``text_chunk_size``，各家默认 320–2048 字；GPT-SoVITS
    #: 按 ``text_split_method`` 断句），长文本不会失败，只是被切成几段拼接——
    #: 那是**质量**取舍，不是请求能不能发的问题，所以不填数字去拦它。
    #:
    #: 填数字的是**真的有硬上限**的那几家（云端按 token 计上下文的那种），
    #: 判据写在插件里并注明出处，不猜。
    max_chars: int = 0
    #: 这台引擎**同时在跑几条**的硬上限（0 = 没声明）。
    #:
    #: 本机单模型服务（audio.cpp 那几个）必须声明 1：实测两条并发时，第二条只是
    #: 在服务端排队——单条 7s，两条并发是 7s + 14s，总时长与串行完全一样，而
    #: 每一行的等待都变长了。云端账号的并发写在插件配置里（见
    #: :meth:`EnginePlugin.configured_concurrency`），不占这一格。
    max_concurrency: int = 0
    #: 这台引擎念得了哪些语言。填规范代码（取值同 ``LANGUAGE_CHOICES``）；
    #: 空 = 插件没有声明。「支持语言」那一栏照它摆，空则不画那一栏。
    languages: tuple[str, ...] = ()
    #: 音色与正文可以是两种语言。只是**是/否**：``True`` 时「能力」卡里出一枚
    #: 「跨语言合成」标签（不写解释），``False`` / ``None`` 都不出。
    cross_lingual: bool | None = None
    #: 能念的方言（中文说法，如「粤语」「四川话」）。普通话不算方言，不进这一格。
    #: 与 ``languages`` 同名的（粤语）只在这里出现一次，语言那一栏会跳过它。
    dialects: tuple[str, ...] = ()
    #: 「支持语言」那一栏的说明（卡片副标题），一句话：清单比官方窄在哪、方言走哪条路。
    language_note: str = ""
    #: 选了未逐项登记的语言/方言时给生成页的一句话：说明这一格为什么没发、
    #: 结果实际由什么决定。空 = 按「不支持」处理并列出引擎名。
    language_hint: str = ""

    @property
    def is_cloud(self) -> bool:
        return self.kind == KIND_CLOUD


# --------------------------------------------------------------------------- #
# 音色
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VoiceMeta:
    """一个平台音色的元数据（``id`` 是发给平台的值，``name`` 只给界面）。

    ``gender`` / ``age`` / ``language`` / ``tags`` 含平台没给、由
    :mod:`app.engines.traits` 从命名与说明里认出的信息，认不出为空；
    认出的不许覆盖用户手改过的值。``tags`` 是可多选的一串词，其余各占一格。
    """

    id: str
    name: str = ""
    description: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    gender: str = ""
    age: str = ""
    language: str = ""


@dataclass(frozen=True, slots=True)
class VoiceList:
    """一台引擎的音色清单。

    拉不到清单不抛异常：``why`` 说明理由（空串 = 没事），界面据此区分
    "平台没有音色"与"这次没拉到"。``cost_note`` 计费提示由插件给。
    """

    items: tuple[VoiceMeta, ...] = ()
    why: str = ""
    cost_note: str = ""


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #


#: 请求自身的四个顶层字段：不经 ``extra`` 传递，API 入口也会拒绝它们。
_CORE_REQUEST_KEYS = frozenset({"text", "voice_id", "emotion", "format"})


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """合成请求的统一形状——上游只认这一种。

    ``reference_text`` 即样例的说话内容，参考音频引擎必须连同它一起给。

    :param format: 输出格式。空串 = 调用方未指定，由引擎按配置决定；
        不在此补默认值，否则引擎分不出"没指定"与"指定了 wav"。
    :param extra: 上游多给的参数（语速、音量、音高……），统一格式的扩展位；
        引擎在映射表里按名字取（如 ``ParamMapping("speed", "voice_setting.speed")``）。
    """

    text: str
    voice_id: str
    reference_audio: str = ""  # 样例音频路径
    reference_text: str = ""  # 样例的说话内容
    emotion: str = ""
    format: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_values(self) -> dict[str, Any]:
        """摊平成一张平表——映射表按名字取值的入口。"""
        values: dict[str, Any] = {
            "text": self.text,
            "voice_id": self.voice_id,
            "reference_audio": self.reference_audio,
            "reference_text": self.reference_text,
            "emotion": self.emotion,
            "format": self.format,
        }
        values.update(self.extra)
        return values


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    audio: bytes = b""
    format: str = "wav"
    cost_note: str = ""
    #: 这次没能全量生效的地方（如情绪词不认被剔掉）——剔了就要说，音频上听不出来。
    notes: tuple[str, ...] = ()
    #: VoiceHub 为完成请求所做的处理（``(code, message)``），与 ``notes`` 分开
    #: （"引擎没接受" vs "我们代做了"），响应里合成 ``warnings`` 并用 ``source`` 区分。
    prep_notes: tuple[tuple[str, str], ...] = ()
    #: 平台顺带生成的字幕下载链接（有时效，要留须先落盘）。空 = 没给或未开。
    subtitle_url: str = ""


@dataclass(frozen=True, slots=True)
class SynthesisSpec:
    """引擎自己声明的合成入参契约，中转站照它在发请求前拦下缺料。

    :param requires: :class:`SynthesisRequest` 里必须非空的字段名（无条件要求）。
    :param formats: 接受的输出格式（第一个 = 默认）。
    :param reference_formats: 参考音频能读的容器格式（如 ``("wav",)``）；空 = 不挑。
        中转站照它决定要不要用 ffmpeg 转一道（见 ``usecase.reference``）；
        不声明就不动。
    :param needs_transcript: 拿参考音频克隆时必须有逐字稿。与 ``requires`` 的分工：
        这条只在真给了参考音频时成立（有的引擎还有"只给一句描述做音色设计"的路）。
        中转站照它决定要不要用本机识别模型补逐字稿并写回样例。
    :param reference_duration: 参考音频可用的秒数区间（闭区间，如 ``(3.0, 10.0)``）；
        ``None`` = 不限。超上限时中转站截取到上限以内并重听逐字稿，低于下限直接报错
        （短了补不回来，补静音只会骗过判据）。
    :param reference_path_ascii: 参考音频的**路径本身**必须是纯 ASCII。默认 ``False``。
        Windows 原生后端（如 gsv.cpp 的 ``gsv_server.exe``）把路径转成本地代码页，
        文件名带中文时直接回 500 ``No mapping for the Unicode character exists in the
        target multi-byte code page.``；声明为真后中转站会先把它落到纯 ASCII 的暂存
        目录再发。官方 Python 版不需要（它按 UTF-8 读路径）。
    :param extra_schema: 引擎特有参数的 JSON Schema（前端照它渲染，中转站照它校验）。
    """

    requires: tuple[str, ...] = ()
    formats: tuple[str, ...] = ("wav",)
    extra_schema: dict = field(default_factory=dict)
    reference_formats: tuple[str, ...] = ()
    needs_transcript: bool = False
    reference_duration: tuple[float, float] | None = None
    reference_path_ascii: bool = False


# --------------------------------------------------------------------------- #
# 音色克隆
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CloneRequest:
    """克隆一个音色：用本地音频换一个能长期使用的音色 id。

    与合成不是一回事（合成是"给文本出声"，克隆是"先注册一个音色"），
    收费、耗时、对音频的要求各家不同，故单独建模。

    :param name: 克隆出的音色显示名。
    :param voice_id: 想要的音色 id（各家规则不同）；留空由引擎生成。
    :param audio_path: 音源音频的本地绝对路径。
    :param transcript: 音源念的内容（部分平台用于相似度校验）。
    :param prompt_audio / prompt_text: 可选提示音频与对应文字。
    :param preview_text: 克隆后试听文本（留空不试听，省一次计费）。
    :param model: 预览/合成用的模型。
    :param design_prompt: 音色设计描述。非空时不需音源，走 ``voice_design`` 那一路。
    """

    name: str
    voice_id: str = ""
    audio_path: str = ""
    transcript: str = ""
    prompt_audio: str = ""
    prompt_text: str = ""
    preview_text: str = ""
    model: str = ""
    design_prompt: str = ""


# --------------------------------------------------------------------------- #
# 插件基类
# --------------------------------------------------------------------------- #

#: 没有自带图标的引擎用这个（一个中性的"盒子"）。
_DEFAULT_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#8a8fa8"/>'
    '<path d="M10 12h12v8H10z" fill="#fff" opacity=".9"/>'
    "</svg>"
)


class EnginePlugin(ABC):
    """引擎插件基类。

    子类至少要给 :attr:`key` / :attr:`label`，其余都有可用缺省——写一个"什么都
    不声明"的引擎只需要两行，而写一个真能用的引擎该重写的是
    :meth:`settings_schema` / :meth:`capabilities` / :meth:`mapping` /
    :meth:`synthesize`。
    """

    key: str = ""
    label: str = ""
    version: str = "0.1.0"
    #: 这台引擎是谁做的 / 去哪查（界面"关于"里显示，空则不显示）。
    homepage: str = ""
    #: 配置里**要当作机密**的字段名——读配置时掩码，日志里不打。
    secrets: tuple[str, ...] = ()

    # ---- 元信息 ---------------------------------------------------------- #

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        """这台引擎要配什么——pydantic 模型即答案（空模型 = 不用配）。"""

        class EmptySettings(BaseModel):
            pass

        return EmptySettings

    @classmethod
    def secret_fields(cls) -> frozenset[str]:
        """要掩码的配置字段：显式声明的 + 模型里标了 ``secret`` 的。"""
        marked = set()
        for name, field_info in cls.settings_schema().model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("secret"):
                marked.add(name)
        return frozenset(marked | set(cls.secrets))

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def supports_language(self, code: str) -> bool:
        """声明里能不能念这个语言代码（方言说法已归一成代码再比）。

        只认 ``capabilities`` 这一个产地：网关据此剔掉不支持的 ``language``，
        前端据此在「语言」栏列出这台引擎。词表外的代码不在这里拦——600+ 语言
        的引擎可能认得统一词表没收的代码，交给插件自己判断。
        """
        return code in supported_language_codes(self.capabilities())

    def configured_concurrency(self, cfg: BaseModel) -> int:
        """这台引擎当前配置允许同时跑几条；0 = 没配，由调用方决定。

        默认读配置模型里的 ``concurrency`` 字段：云端引擎把账号额度摆在设置页，
        用户可改。它与 :attr:`Capabilities.max_concurrency` 分工不同——那个是
        引擎能力的硬上限，这个是当前账号/配置下的额度；两者都有时取较小者。
        """
        return max(0, int(getattr(cfg, "concurrency", 0) or 0))

    def readiness(self, cfg: BaseModel) -> str:
        """这台引擎现在能不能用：空串 = 能，否则一句可读说明缺什么。默认永远就绪。"""
        return ""

    def icon_svg(self) -> str:
        """平台图标（SVG 文本）。默认读插件目录下的 ``icon.svg``，没有则回落中性图标。"""
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

    # ---- 契约 ------------------------------------------------------------ #

    def synthesis_spec(self) -> SynthesisSpec:
        """合成入参契约（默认：只要文本 + 音色 id，wav 输出）。"""
        return SynthesisSpec()

    def synthesis_spec_for(self, cfg: BaseModel) -> SynthesisSpec:
        """**这份配置下**的入参契约。

        大部分引擎的约束是固定的，默认就退回 :meth:`synthesis_spec`。需要它的是
        "同一台服务换模型就换规则"的那类——写死在 ``synthesis_spec`` 里就只能按
        最严的那一档算（例如参考音频的可用时长随服务端换模型而变）。
        """
        return self.synthesis_spec()

    def reconcile_settings(
        self, incoming: dict[str, Any], stored: dict[str, Any]
    ) -> dict[str, Any]:
        """保存前的一道整理：插件可以按自己的规则把某些字段带过去。

        ``incoming`` 是这次提交的值，``stored`` 是配置里原有的值（都在字段名下）。
        默认原样返回。需要它的场景是"换了一个选项，另一组字段的合理取值跟着变"——
        例如换了服务端模型版本，采样参数该按那一版的推荐值走。
        """
        return incoming

    def mapping(self) -> EngineMapping:
        """统一参数 → 这台引擎请求体 的映射表（见 :mod:`app.engines.mapping`）。"""
        return EngineMapping()

    def value_choices(self) -> dict[str, tuple[str, ...]]:
        """枚举型参数这台引擎认得的取值（``{"emotion": ("中性", …)}``），
        供上游发请求前逐行打标；默认空 = 无枚举约束。"""
        return {}

    def unsupported(self) -> dict[str, str]:
        """规范里有、这台引擎没有落点的参数 → 原因（键是规范参数名）。

        面板的映射表按它写「说明」，装配时也按它报"这次哪几项没发出去"。
        没写原因的项按「本引擎无对应参数」处理；确实有落点的项不要放进来。
        """
        return {}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """映射表表达不了、但确实有落点的参数：``{参数: (落点说法, 说明)}``。

        形状随请求变的引擎（数组下标、一个键两种含义）用它声明；面板的映射表照它
        写「映射位置」。装配仍在插件自己的代码里，这里只是把事实说出来，不参与装配。
        """
        return {}

    def accepted_extra_keys(self) -> frozenset[str]:
        """这台引擎真正接受的 ``extra`` 键——声明与校验的唯一产地。

        来源只有映射表的 ``source`` 与 :meth:`dynamic_mappings`：前者由装配表直接
        落进请求体，后者说明"装配在插件里"的私有键。四个顶层字段
        （``text`` / ``voice_id`` / ``emotion`` / ``format``）不走 ``extra``，排除。

        网关照它过滤请求，``describe()`` 下发同一份；插件只有把私有键登记进
        ``dynamic_mappings()``，它才会被放行。
        """
        keys = {item.source for item in self.mapping().params}
        keys.update(self.dynamic_mappings())
        keys.difference_update(_CORE_REQUEST_KEYS)
        return frozenset(keys)

    def dual_expression(self) -> bool:
        """情绪与引导指令是否**各有独立的原生落点**。

        两种情况都算"没有独立落点"，不适用「只发一路」：情绪没有落点（只能靠参考
        音频）、情绪本身就是翻译成引导指令发出去的（落点相同，那一路已经按指令优先
        处理过了）。判据取映射表与动态映射里的落点，不按引擎名分支。
        """
        placed = {item.source: item.target for item in self.mapping().params}
        for key, (target, _note) in self.dynamic_mappings().items():
            placed.setdefault(key, target)
        emotion = placed.get("emotion", "")
        instruction = placed.get("instruction", "")
        return bool(emotion) and bool(instruction) and emotion != instruction

    def mark_map(self) -> dict[str, str]:
        """正文里的语气词标签：这台引擎认哪几个、认的话怎么写（``{统一名: 原生写法}``，
        带参数的留 ``{sec}``）。不在表里的标签会从正文删掉，绝不硬发
        （由 :meth:`prepare_text` 执行）。

        键一律用 :data:`~app.domain.marks.MARKS` 里的中文名：原生写法常是另一词
        （``(gasp)`` = ``喘气``），先翻成中文名再登记才能进「通用」组；确为该引擎
        独有概念才原样登记（自动落到它自己那一组）。默认空表 = 一条都不认，
        该引擎不参与「通用」运算，正文里的标签全部删掉。
        """
        return {}

    def mark_passthrough(self) -> str:
        """认**任意**标签时的原生写法模板（``"({name})"``），空 = 只认 :meth:`mark_map` 那几条。

        与 :meth:`mark_map` 的区别是"认哪些"与"怎么写"：这家支持不在列表里的标签
        （MiMo 官方原话："支持使用不在列表中的风格"），表里登记的只是几个常见项的
        写法。声明了这一档之后：

        - 装配时表外的标签按模板原样转过去，不删（见 :func:`~app.domain.marks.render_marks`）；
        - 跨引擎算「通用」时它的支持面是全集：它确实发得出去，也不因为只登记了几条
          就把别人的通用项挤掉（见 :func:`~app.domain.marks.mark_groups`）；
        - 界面上给它那一组补一格自定义输入。
        """
        return ""

    def prepare_text(self, text: str) -> str:
        """正文过一遍语气词：统一写法 → 这台引擎的写法，它不认的从正文里删掉。

        **网关在发出前统一调一次**（:mod:`app.usecase.gateway`）：试听、生成页、
        有声书三条路都过那一处，插件不必各自记得调，也不会漏掉后来的新路径。
        漏掉的后果不是"少一条标签"，而是标签被当成字念出来（"倒吸气"三个字）。

        默认按 :meth:`mark_map` / :meth:`mark_passthrough` 翻译；写法随正文变的引擎
        重写这个方法（BreezeTTS 按中英换括号，见 ``breeze_tts``）。
        """
        return render_marks(text, self.mark_map(), passthrough=self.mark_passthrough())

    def field_presets(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """某一格参数的现成说法：``{"instruction": ((短名, 完整句子), ...)}``。

        与 :meth:`value_choices` 不同：那个说"认哪些取值"（认不出会被剔），
        这个说"可以照抄哪些句子"（自由文本，仅省手打）。哪些话该引擎听得懂只有
        它自己知道，故由插件声明；键是规范里那一格的键名，前端照它挂候选。
        默认空表 = 该格就是普通输入框。
        """
        return {}

    # ---- 界面结构 -------------------------------------------------------- #

    def ui_sections(self) -> tuple[UiSection, ...]:
        """这台引擎的页面有哪几块（顺序即页签顺序）。

        默认从能力推导：``settings`` / ``voices`` / ``about`` 永远有，
        ``clone`` 需声明 ``voice_cloning`` 或 ``sample_cloning``。插件可重写改顺序或增删，
        但键须在 :data:`SECTION_LABELS` 里，画不出的键会被丢掉。
        """
        caps = self.capabilities()
        sections = [
            UiSection("settings", *SECTION_LABELS["settings"]),
            UiSection("voices", *SECTION_LABELS["voices"]),
        ]
        if caps.voice_cloning or caps.sample_cloning:
            sections.append(UiSection("clone", *SECTION_LABELS["clone"]))
        if caps.voice_design:
            sections.append(UiSection("design", *SECTION_LABELS["design"]))
        sections.append(UiSection("about", *SECTION_LABELS["about"]))
        return tuple(sections)

    # ---- 能力 ------------------------------------------------------------ #

    async def list_voices(self, cfg: BaseModel) -> VoiceList:
        return VoiceList(why=f"{self.label} 没有提供音色清单。")

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        raise EngineError(f"{self.label} 尚未接入合成。", code="engine_unsupported")

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        """克隆一个音色；不支持就如实说明（默认实现）。"""
        raise EngineError(f"{self.label} 不支持音色克隆。")

    # ---- 界面用的自描述 -------------------------------------------------- #

    def describe(self, cfg: BaseModel | None = None) -> dict[str, Any]:
        """把插件讲清楚——``/api/v1/engines`` 直接返回它，前端不再为某一家写分支。

        :param cfg: 这台引擎当前的配置。给了就按它算"这一版要不要逐字稿"这类
            随服务端模型变化的项；不给则按静态声明（最严的那一档），
            用于只取 ``kind_label`` 之类与配置无关的调用方。
        """
        caps = self.capabilities()
        spec = self.synthesis_spec_for(cfg) if cfg is not None else self.synthesis_spec()
        mapping = self.mapping()
        kind_label, kind_note = KIND_LABELS.get(caps.kind, (caps.kind, ""))
        raw = {
            item.name: getattr(caps, item.name)
            for item in fields(caps)
            if item.name not in _CAPABILITY_FIELDS_HIDDEN
        }
        return {
            "key": self.key,
            "label": self.label,
            "version": self.version,
            "homepage": self.homepage,
            "kind": caps.kind,
            "kind_label": kind_label,
            "kind_note": kind_note,
            "icon_url": f"/api/v1/engines/{self.key}/icon",
            "capabilities": raw,
            # 只列这台引擎真有的能力，连中文说法一起发（见 CAPABILITY_LABELS）。
            "capability_notes": self._capability_notes(caps),
            # 语言面的机器可读那一份（界面「支持语言」那一栏照它画）：
            # items 已去掉同时算方言的那几个，两处不重复摆同一个词。
            "languages": {
                "codes": list(caps.languages),
                # 界面判「所选语言这台认不认」用这一份：方言说法已归一成代码。
                "supported_codes": sorted(supported_language_codes(caps)),
                "items": language_items(caps),
                "dialects": list(caps.dialects),
                "cross_lingual": caps.cross_lingual,
                "note": caps.language_note,
                "hint": caps.language_hint,
            },
            "settings_schema": self.settings_schema().model_json_schema(),
            "secret_fields": sorted(self.secret_fields()),
            "synthesis_spec": {
                "requires": list(spec.requires),
                "formats": list(spec.formats),
                # 接受哪些扩展参数与实际过滤用的是同一份（见 accepted_extra_keys）。
                "accepted_extra": sorted(self.accepted_extra_keys()),
                "extra_schema": spec.extra_schema,
                # 参考音频侧的两条要求，界面上要看得见（解释转码/补逐字稿的来由）。
                "reference_formats": list(spec.reference_formats),
                "needs_transcript": spec.needs_transcript,
                # 有硬性时长要求的引擎（如 GPT-SoVITS 的 3–10 秒）：界面直接给区间。
                "reference_duration": (
                    list(spec.reference_duration) if spec.reference_duration else None
                ),
            },
            # 按项目规范逐项列全：有落点写落点，没有落点写原因。
            "mapping": self._mapping_rows(mapping, spec),
            "mapping_cautions": self._cautions(mapping),
            # 枚举取值随接口下发，词表唯一产地在插件。
            "value_choices": {k: list(v) for k, v in self.value_choices().items()},
            # 本台引擎的语气词映射；「通用」分组由核心在 GET /api/v1/marks 里算，
            # 不放在插件（否则同一条规则抄 N 份），前端不认识任何标签名。
            "marks": engine_marks(self.mark_map()),
            # 空串 = 只认上面那几条；非空 = 表外标签按这个模板原样转过去（MiMo 那类）。
            "marks_passthrough": self.mark_passthrough(),
            # 某格参数的现成说法（键 = 规范键名，值 = 短名 + 完整句子）。
            "field_presets": {
                key: [{"label": label, "text": text} for label, text in items]
                for key, items in self.field_presets().items()
            },
            # 每次请求原样带上的固定量，界面显示它以便核对"到底发了什么"。
            "mapping_constants": dict(mapping.constants),
        }

    def _cautions(self, mapping: EngineMapping) -> list[str]:
        """参数映射页列出的重点：插件自己声明的那几条，加上核心统一的跨参数约束。

        「只发一路」这条由核心在装配时执行（见 ``usecase.gateway``），所以文案也由
        核心追加：让每个双落点引擎各写一份，迟早有一家写成另一种说法。
        """
        cautions = list(mapping.cautions)
        if self.dual_expression():
            cautions.append(DUAL_EXPRESSION_NOTE)
        return cautions

    def _mapping_rows(
        self, mapping: EngineMapping, spec: SynthesisSpec
    ) -> list[dict[str, Any]]:
        """映射表按**项目规范**逐项列全：规范里的每一项都有一行，没落点就写原因。

        顺序取规范的分组顺序（输入 → 音色 → 合成参数 → 静默 → 语言 → 输出），
        各引擎因此可以逐行对照。规范里没有的引擎专有参数排在后面；与已有落点
        重复的别名（同一条路换个名字）不再单列——那是实现细节，不是规范参数。
        """
        properties = (spec.extra_schema or {}).get("properties", {}) or {}
        unsupported = self.unsupported()
        dynamic = self.dynamic_mappings()
        placed = {item.source: item for item in mapping.params}

        def label_of(key: str) -> str:
            field = _SPEC_FIELDS.get(key)
            if field is not None:
                return field.label
            meta = properties.get(key)
            if isinstance(meta, dict) and meta.get("title"):
                return str(meta["title"])
            return key

        def spec_note(key: str) -> str:
            field = _SPEC_FIELDS.get(key)
            if field is None:
                meta = properties.get(key)
                if isinstance(meta, dict) and meta.get("description"):
                    return str(meta["description"])
                return ""
            parts: list[str] = []
            if field.unit:
                parts.append(f"单位：{field.unit}")
            if field.min is not None and field.max is not None:
                parts.append(f"区间：{field.min}–{field.max}")
            return "；".join(parts)

        def row(key: str) -> dict[str, Any]:
            item = placed.get(key)
            if item is None and key in dynamic:
                target, note = dynamic[key]
                return {
                    "source": key,
                    "label": label_of(key),
                    "target": target,
                    "transform": "",
                    "transform_label": "",
                    "required": False,
                    "note": note,
                    "warning": False,
                }
            if item is None:
                return {
                    "source": key,
                    "label": label_of(key),
                    "target": "",
                    "transform": "",
                    "transform_label": "",
                    "required": False,
                    "note": unsupported.get(key) or "本引擎无对应参数。",
                    "warning": False,
                }
            return {
                "source": key,
                "label": item.label or label_of(key),
                "target": item.target,
                "transform": item.transform,
                "transform_label": transform_label(item.transform),
                "required": item.required,
                "note": item.note or spec_note(key),
                "warning": item.warning,
            }

        rows = [row(item.key) for item in SPEC_FIELDS]
        seen = {item["target"] for item in rows if item["target"]}
        for item in mapping.params:
            if item.source in _SPEC_FIELDS or item.target in seen:
                continue
            rows.append(row(item.source))
            seen.add(item.target)
        return rows

    @staticmethod
    def _capability_notes(caps: Capabilities) -> list[dict[str, str]]:
        """能力 → 逐条说明。三种音色克隆实现在这里合并成一条。

        顺序是固定的：内置音色 → 音色克隆 → 音色设计 → 引导指令 → 情绪控制。
        文本上限不是能力，放在「引擎要求」里由界面渲染。
        """
        notes: list[dict[str, str]] = []

        def add(key: str) -> None:
            label, note = CAPABILITY_LABELS[key]
            notes.append({"key": key, "label": label, "note": note})

        if caps.voice_listing:
            add("voice_listing")
        if caps.sample_cloning:
            notes.append(
                {
                    "key": "voice_cloning",
                    "label": CAPABILITY_LABELS["voice_cloning"][0],
                    "note": "上传音频作为复刻样本；平台不保存音色 id，每次合成随样本发送",
                }
            )
        elif caps.voice_cloning:
            add("voice_cloning")
        elif caps.reference_audio:
            notes.append(
                {
                    "key": "voice_cloning",
                    "label": CAPABILITY_LABELS["voice_cloning"][0],
                    "note": "用参考音频复刻音色，每次合成随请求提供参考音频",
                }
            )
        if caps.voice_design:
            add("voice_design")
        if caps.instruction_control:
            add("instruction_control")
        if caps.emotion_control:
            add("emotion_control")
        if caps.cross_lingual:
            # 只给标签、不写解释：能力名本身就是结论。语言与方言不在这里——
            # 它们是清单，占「支持语言」那一栏（见 :func:`language_items`）。
            notes.append({"key": "cross_lingual", "label": "跨语言合成", "note": ""})
        return notes
