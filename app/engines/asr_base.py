"""识别插件协议——**音频 → 文字**，与合成引擎方向相反，故单独一套契约。

为什么不套 :class:`app.engines.base.EnginePlugin`：那套契约的骨架是「音色」——
``list_voices`` / ``clone_voice`` / ``voice_id`` / 参数映射表，识别一条都不占。
硬塞进去会让每个识别插件实现一堆空方法，而界面又要为"这台引擎没有音色页"打补丁。
所以识别只共享**发现机制**（``app/engines/builtin/<key>/`` 与 entry points）与
**配置方式**（pydantic 模型 → 表单），能力声明与调用入口各走各的。

**核心只认这一份契约**：换成哪家的识别模型（本地推理服务、云端 HTTP、将来某个
本地推理库）都是换一个插件目录，``usecase/asr.py`` 一行不用改。
"""

from __future__ import annotations

import inspect
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

__all__ = [
    "ASR_KIND_LABELS",
    "RETURN_FIELD_LABELS",
    "AsrCapabilities",
    "AsrEngine",
    "AsrError",
    "AsrLanguage",
    "AsrLanguageGroup",
    "Transcript",
]


#: 识别引擎的形态说法（与合成的 :data:`~app.engines.base.KIND_LABELS` 分开：
#: 识别问的是"音频去哪儿"，合成问的是"推理在哪儿"）。
ASR_KIND_LABELS: dict[str, tuple[str, str]] = {
    "local": ("本地服务", "音频只发给你配置的识别服务（本机或局域网），不经过第三方云端"),
    "cloud": ("云端服务", "音频要发给云端服务，按量计费"),
}

#: 一次识别**可能**带回的字段（契约 :class:`Transcript` 的那些）与界面说法。
#: **没有的也要列出来**："不回报情绪"正是用户要核对的那一条——只列有的，
#: 界面就分不清"这台没有"和"这家没声明"。``text`` 是契约字段，恒有。
RETURN_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "text": ("文字转写", "识别出的文字"),
    "language": ("语种检测", "识别到的语种，如 Chinese"),
    "emotion": ("情感识别", "说话情绪，需情感分类模型"),
    "events": ("声音事件检测", "笑声、掌声、音乐等非语音事件"),
    "timestamps": ("时间轴", "逐词或逐段时间戳"),
}


@dataclass(frozen=True, slots=True)
class AsrLanguage:
    """语言清单里的一项。

    ``code`` 是模型/接口认的语言 id（可填进「语种提示」）；方言没有独立代码时为空。
    """

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class AsrLanguageGroup:
    """一组语言覆盖（如「通用语言」「中文方言」）。

    界面照着分组画表：组名、说明、逐项代码与名称都来自插件，前端不写死任何一份清单。
    """

    key: str
    label: str
    note: str = ""
    items: tuple[AsrLanguage, ...] = ()


class AsrError(Exception):
    """识别失败。``reason`` 是中文说明，``code`` 供调用方分支。

    :param reason: 面向用户的说明（严格、简洁、专业）。
    :param code: 错误码（见 ``app.api.errors.CODES``）；缺省 ``asr_error``。
    :param retryable: 同一段音频再试一次是否有意义（"连不上"算，"模型不认识"不算）。
    """

    def __init__(self, reason: str, *, code: str = "asr_error", retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AsrCapabilities:
    """这台识别引擎能给出什么——界面按它排徽标，**没有的不假装有**。

    :param languages: 语言覆盖的短说法，界面直接显示（如「30 种语言 · 22 种中文方言」）。
    :param language_groups: 语言覆盖的逐项清单（组名 + 代码 + 名称），界面照它画表；
        空 = 插件没有更细的清单，不替它编。
    :param returns_language: 会不会回报识别到的语种。
    :param returns_emotion: 会不会回报说话情绪。
        **当今的识别模型都不带情感分类**（Qwen3-ASR 只出文本，``/details``
        多给一个 ``language``）；字段留着是为了将来接 SenseVoice 这类带情感
        标签的模型——那时界面多一格，核心不用改。
    :param returns_events: 会不会回报声音事件（笑声、掌声、音乐等）。
        Qwen3-ASR 只做语音转写，这一项是 ``False``；**没有的不假装有**。
    :param timestamps: 会不会给逐词 / 逐段时间轴。
        注意区分服务端的推理计时（``timing`` 里的 ``wall_ms``）——那不是时间轴。
    :param local: 是不是本地 / 局域网推理（决定界面上要不要说"会联网/计费"）。
    :param formats: 引擎**直接接收**的音频容器（其余格式由插件自己转换或拒绝）。
    :param format_note: 输入格式的处理方式，界面原样显示。
    :param streaming: 这个插件的识别接口是否能边流边返回。
        核心的连续麦克风（``/asr/stream``）**不走这一项**：它由核心 VAD 分段后
        仍按整段调用插件，部分结果由核心重识别得到（见 ``usecase/asr_stream``）。
        这里只描述插件自己的协议事实，界面原样显示。
    :param streaming_note: 服务端与插件在流式上的差异，界面原样显示。
    """

    languages: str = ""
    language_groups: tuple[AsrLanguageGroup, ...] = ()
    returns_language: bool = False
    returns_emotion: bool = False
    returns_events: bool = False
    timestamps: bool = False
    local: bool = True
    formats: tuple[str, ...] = ()
    format_note: str = ""
    streaming: bool = False
    streaming_note: str = ""


@dataclass(frozen=True, slots=True)
class Transcript:
    """一次识别的结果。

    ``text`` 之外的字段都是**引擎真给了才填**：没给就是空串，不是"猜一个"。
    猜出来的语种进了库，下一句就会照它选处理路径，错了没人发现。
    """

    text: str
    language: str = ""
    emotion: str = ""
    seconds: float = 0.0
    model: str = ""
    extra: dict = field(default_factory=dict)


class AsrEngine(ABC):
    """识别插件基类。子类至少要给 :attr:`key` / :attr:`label`。

    该重写的是 :meth:`settings_schema`（配什么）、:meth:`capabilities`（能什么）、
    :meth:`transcribe`（怎么识别）。``readiness`` 只报**配置层面**缺什么，
    不做网络请求——它在每次打开设置页时都会被调用。
    """

    key: str = ""
    label: str = ""
    vendor: str = ""
    version: str = "0.1.0"
    homepage: str = ""
    #: 配置里要当作机密的字段名（读取时掩码、日志里不打）。
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

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities()

    @classmethod
    def api_format(cls) -> dict[str, str]:
        """请求与返回格式的声明——界面「能力与参数」页里的那张卡照它画。

        换一家服务（别的端口 / 别的协议）只改插件里这一句，核心与界面都不动。
        两项都可以留空（界面画「—」），但**协议写在插件里**这件事不变：
        从前端写死端点名，等于每接一家都要改一次 Vue。
        """
        return {"request": "", "response": ""}

    def readiness(self, cfg: BaseModel) -> str:
        """现在能不能用：空串 = 能，否则一句可读说明缺什么。**不发网络请求。**"""
        return ""

    def icon_svg(self) -> str:
        """图标（SVG 文本）。默认读插件目录下的 ``icon.svg``，没有则用中性图标。"""
        module = inspect.getmodule(type(self))
        module_file = getattr(module, "__file__", "") if module else ""
        if module_file:
            path = Path(module_file).resolve().parent / "icon.svg"
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - 读不动就回落，别拖死列表
                pass
        return _DEFAULT_ICON

    # ---- 契约 ------------------------------------------------------------ #

    def transcribe(self, cfg: BaseModel, path: Path) -> Transcript:
        """把磁盘上的一个音频文件听成文字。**必须**实现（默认实现只用来报错）。

        同步：识别是"一次一段、等它出结果"的活，没有可并行的东西；调用方
        （``usecase.asr``）本身就跑在请求线程里，包一层 async 只是多一个壳。
        真要长时间推理，插件自己在内部起线程即可，别把这件事推给每个调用方。
        """
        raise AsrError(f"{self.label} 尚未接入识别。", code="engine_unsupported")

    def probe(self, cfg: BaseModel) -> tuple[bool, str]:
        """测一下现在能不能连上（设置页的「测试连接」按钮）。默认：不问就当能。"""
        return True, ""

    # ---- 界面用的自描述 -------------------------------------------------- #

    def describe(self) -> dict[str, Any]:
        """``/api/v1/asr`` 直接返回它：前端不认识任何一家识别引擎。"""
        caps = self.capabilities()
        kind_label, kind_note = ASR_KIND_LABELS.get(
            "local" if caps.local else "cloud", ("", "")
        )
        fmt = self.api_format()
        return {
            "key": self.key,
            "label": self.label,
            "vendor": self.vendor,
            "version": self.version,
            "homepage": self.homepage,
            "kind": "local" if caps.local else "cloud",
            "kind_label": kind_label,
            "kind_note": kind_note,
            "icon_url": f"/api/v1/asr/engines/{self.key}/icon",
            "capabilities": {
                "languages": caps.languages,
                "returns_language": caps.returns_language,
                "returns_emotion": caps.returns_emotion,
                "returns_events": caps.returns_events,
                "timestamps": caps.timestamps,
                "local": caps.local,
            },
            # 能力全表：有的打勾，没有的也列出来（见 RETURN_FIELD_LABELS）。
            "returns": _return_rows(caps),
            # 语言覆盖的逐项清单：插件登记多少就画多少，界面不写死任何一份。
            "language_groups": [
                {
                    "key": group.key,
                    "label": group.label,
                    "note": group.note,
                    "items": [{"code": item.code, "name": item.name} for item in group.items],
                }
                for group in caps.language_groups
            ],
            # 输入格式与流式：插件声明，界面原样显示（不替它猜）。
            "input": {"formats": list(caps.formats), "note": caps.format_note},
            "streaming": {"supported": caps.streaming, "note": caps.streaming_note},
            # 请求与返回格式：插件自己的声明，界面原样显示。
            "api_format": {"request": fmt.get("request", ""), "response": fmt.get("response", "")},
            "settings_schema": self.settings_schema().model_json_schema(),
            "secret_fields": sorted(self.secret_fields()),
        }


#: 没有自带图标的识别引擎用这个（一个中性的"耳朵"）。
_DEFAULT_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#8a8fa8"/>'
    '<path d="M11 13a5 5 0 0 1 10 0v6a5 5 0 0 1-10 0z" fill="#fff" opacity=".9"/>'
    "</svg>"
)


def _return_rows(caps: AsrCapabilities) -> list[dict[str, Any]]:
    """能力全表 → 界面行（``has`` = 这家真给这一项吗）。

    ``text`` 恒真（契约字段）；其余照能力标志位。**顺序固定**：
    与 :data:`RETURN_FIELD_LABELS` 一致，界面不必自己排。
    """
    flags = {
        "text": True,
        "language": caps.returns_language,
        "emotion": caps.returns_emotion,
        "events": caps.returns_events,
        "timestamps": caps.timestamps,
    }
    return [
        {"key": key, "label": label, "note": note, "has": flags[key]}
        for key, (label, note) in RETURN_FIELD_LABELS.items()
    ]
