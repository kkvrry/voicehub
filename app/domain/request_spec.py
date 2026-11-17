"""合成请求规范的机内一半（文档那一半是 ``docs/request-spec.md``，两边必须是同一张表）。

中文名、单位、区间的**唯一产地是这里**，经 ``GET /api/v1/spec`` 原样下发，
加参数不用改前端。本表只说"有什么、叫什么、怎么画、什么区间"，某家引擎认不认
由它自己的映射表回答；未提升为 ``SynthesisRequest`` 字段的暂走 ``extra``。

界面文案纪律：只下发名字、单位、区间、候选，不下发解释文字。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.models import EMOTION_CHOICES, LANGUAGE_CHOICES

__all__ = ["GROUPS", "PARAM_LABELS", "SPEC_FIELDS", "Group", "SpecField", "spec_payload"]


@dataclass(frozen=True, slots=True)
class Group:
    """界面上的一组参数（一个小标题）。分组只影响排版，不参与取值与校验。

    :param key: 组名（英文；界面拿它认组）。
    :param label: 小标题的中文（唯一产地）。
    :param folded: 界面默认收起（"通常不用动"的组）；展开后值照旧生效。
    """

    key: str
    label: str
    folded: bool = False


#: 分组与界面上先后的顺序；常用组在前，静默 / 语言 / 输出默认折叠。
GROUPS: tuple[Group, ...] = (
    Group("text", "输入文本"),
    Group("voice", "音色"),
    Group("expression", "合成参数"),
    # 静默单独一组：它是"前后留多少空"的排版属性，与语速/音量/音高不同类，需单独折叠。
    Group("silence", "静默", folded=True),
    Group("language", "语言", folded=True),
    Group("output", "输出", folded=True),
)


@dataclass(frozen=True, slots=True)
class SpecField:
    """规范里的一项。

    :param key: payload 键名（英文，与文档总表同名）。
    :param label: 界面上的中文名（唯一产地）。
    :param group: 见 :data:`GROUPS`。
    :param widget: 画法：``textarea`` / ``voice`` / ``reference`` / ``number`` /
        ``select`` / ``combo``（候选 + 手输）/ ``seed``（整数 + 随机键，非滑块）/ ``text``。
    :param unit: 单位；没有就空着。
    :param min/max: 数值格上下界，也是滑块两端。
    :param step: 滑块步长。
    :param options: ``select`` 的候选（值 → 说法）；空串项 = "由引擎决定"。
    :param default: 数值格的滑块起点，是这一项自己的默认值（语速 1.0 = 原速），
        不是"没填时替你填的值"。必须落在 ``min + n × step`` 网格上，否则滑块会挪值。
    :param required: 必填。
    """

    key: str
    label: str
    group: str
    widget: str = "text"
    unit: str = ""
    min: float | None = None
    max: float | None = None
    step: float = 0.1
    options: tuple[tuple[str, str], ...] = ()
    default: float | None = None
    required: bool = False


#: 数值格的 ``default`` 只是滑块起点，不是"空着时替你填的值"。
#:
#: 规范口径"空着 = 没要求"：未改动的格子仍然不发送，滑块只停在自己默认位置
#: （界面用"默认"角标标示，见 ``ParamFields.vue`` 的 ``isDefault``）。
#: 各格起点因此不在同一位置，不要"拉齐"。
_LANGUAGE_OPTIONS: tuple[tuple[str, str], ...] = (("", "不指定"), *LANGUAGE_CHOICES)
#: 情绪用 ``combo``（候选 + 手输）：那一串词是候选不是白名单，引擎侧对认不出的词
#: 是"剔掉并说明"而非报错；同义词多（兴奋/高兴…归一到「开心」），不宜当门禁。
_EMOTION_OPTIONS: tuple[tuple[str, str], ...] = (("", "不指定"), *EMOTION_CHOICES)

SPEC_FIELDS: tuple[SpecField, ...] = (
    SpecField(
        key="text",
        label="正文",
        group="text",
        widget="textarea",
        required=True,
        # 可带句内标记（[[笑]] / [[停顿:0.4]]）；原样交给引擎，认不认由那家决定。
    ),
    # ---- 音色 ----
    SpecField(
        key="voice_id",
        label="音色",
        group="voice",
        widget="voice",
        required=True,
        # 从库里挑；这一份请求会扇出到勾选的每一台引擎。
    ),
    SpecField(
        key="reference_audio",
        label="参考音频",
        group="voice",
        widget="reference",
        # 挑哪条样例当参考 = 用哪段声音（一个音色可挂多条样例，按情绪换参考）。
        #
        # 空着 = 用该音色默认那条：gateway.normalize_voice 会用主样例补齐
        # reference_audio / reference_text 两样，空值含义即"默认"。
        # 样例上的情绪标签**不**跟着补——参考音频已带着原声的语气，那一格留空即"没要求"。
        #
        # 预置音色用不到这一格：它有自己的平台音色 id，合成一律发 id，不拿样例复刻。
    ),
    SpecField(
        key="emotion",
        label="情绪",
        group="voice",
        widget="combo",
        options=_EMOTION_OPTIONS,
        # 各家枚举对照在各映射表里，本层不认识任何一家的写法；候选不是白名单。
        #
        # 与「参考音频」不是二选一：样例自带情绪，这一格是用文字再说一次；
        # 两样都发，引擎自己决定听谁的。
    ),
    # ---- 合成参数 ----
    SpecField(
        key="instruction",
        label="引导指令",
        group="expression",
        widget="textarea",
        # 自然语言描述（BreezeTTS 2 的官方 ``instruction`` 字段）。空 = 用引擎默认引导语；
        # 有它就该配 CFG（见下一格）。不沿用旧 ``speaker`` 名：该字段在有无参考音频
        # 时分别是微调语气与直接描述音色两种用法，``speaker`` 管不住后半句。
    ),
    SpecField(
        key="guidance_scale",
        label="指令强度",
        group="expression",
        widget="number",
        min=0.0,
        max=1.0,
        step=0.05,
        default=0.5,
        # 0–1 的统一刻度：0.5 = 各家的推荐默认（BreezeTTS 的 CFG 4、
        # IndexTTS2 的 emotion_alpha 0.5）。引擎侧按自己的刻度换算，见各插件映射表。
    ),
    SpecField(
        key="speed",
        label="语速",
        group="expression",
        widget="number",
        unit="倍率",
        min=0.5,
        max=2.0,
        step=0.1,
        default=1.0,
        # 1.0 = 原速，这一项自己的默认值。
    ),
    SpecField(
        key="volume",
        label="音量",
        group="expression",
        widget="number",
        unit="倍率",
        min=0.2,
        max=2.0,
        step=0.1,
        default=1.0,
    ),
    SpecField(
        key="pitch",
        label="音高",
        group="expression",
        widget="number",
        unit="倍率",
        min=0.5,
        max=2.0,
        step=0.1,
        default=1.0,
        # 用倍率而非半音：边界正好对齐（0.5↔−12、2.0↔+12），换算是有定义的那一步。
    ),
    SpecField(
        key="seed",
        label="种子",
        group="expression",
        # 种子是整数标识不是区间位置，不适合滑块，故单独一种 widget。
        widget="seed",
        min=-1,
        max=2147483647,
        step=1,
        # -1 = 随机：下游不收负数，须由中间层抽无符号数发出并回报抽到的值，
        # 否则满意结果无法复现（见 BreezeTTS plugin.resolve_seed）。
        #
        # 上限 2³¹−1，与抽取区间一致。``default`` 留空：种子没有统一默认，
        # 各家配置里那一格才是；空 = 没要求，填 -1 表示每次随机。
    ),
    SpecField(
        key="lead_silence",
        label="前静默",
        group="silence",
        widget="number",
        unit="秒",
        min=0.0,
        max=10.0,
        step=0.1,
        default=0.0,
        # 填 0 = 明确要求"不留空"，与空着不是一回事（见 default 的注）。
    ),
    SpecField(
        key="lag_silence",
        label="后静默",
        group="silence",
        widget="number",
        unit="秒",
        min=0.0,
        max=10.0,
        step=0.1,
        default=0.0,
        # 有的引擎把它写进正文，那是那边映射表的事。
    ),
    # ---- 语言 ----
    SpecField(
        key="language",
        label="正文语言",
        group="language",
        widget="select",
        options=_LANGUAGE_OPTIONS,
        # 这段字是什么语言。不指定就用那台引擎配置里那一格。
    ),
    SpecField(
        key="reference_language",
        label="参考语言",
        group="language",
        widget="select",
        options=_LANGUAGE_OPTIONS,
        # 参考音频里那句话是什么语言（本地音色 = 主样例说的那种话）。
        # 有的引擎分开问，有的只有一格。
    ),
    # ---- 输出 ----
    SpecField(
        key="format",
        label="格式",
        group="output",
        widget="select",
        options=(
            ("", "由引擎决定"),
            ("wav", "wav"),
            ("mp3", "mp3"),
            ("flac", "flac"),
            ("pcm", "pcm"),
            ("ogg", "ogg"),
            ("aac", "aac"),
        ),
        # 空 = 未指定，不是"要 wav"：编一个默认值，引擎就分不出这两者。
    ),
    # 两个「由引擎决定」的下拉排在一起（界面上它们并排一行），数值格跟在后面。
    # 声道那一格去掉了：TTS 成品几乎全是单声道，摆了也只是噪音。
    SpecField(
        key="sample_rate",
        label="采样率",
        group="output",
        widget="select",
        unit="Hz",
        options=(
            ("", "由引擎决定"),
            ("16000", "16000"),
            ("24000", "24000"),
            ("32000", "32000"),
            ("44100", "44100"),
        ),
    ),
    SpecField(
        key="bitrate",
        label="码率",
        group="output",
        widget="number",
        unit="bps",
        min=32000,
        max=320000,
        step=8000,
        default=128000,
        # 只对有损格式有意义。
    ),
)


#: 参数键 → 界面中文名（唯一产地仍是这份规范）；样例卡片上的参数回显也查它。
PARAM_LABELS: dict[str, str] = {f.key: f.label for f in SPEC_FIELDS}


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _value_type(field: SpecField) -> str:
    """这一格的值类型，由规范决定（随规范一起下发），前端不按字段名猜。"""
    if field.widget == "number":
        return "number"
    # 种子也是数字；单独一种 widget 是画法原因，与类型无关。
    if field.widget == "seed":
        return "number"
    if field.widget in ("select", "combo"):
        values = [value for value, _label in field.options if value]
        if values and all(_looks_numeric(v) for v in values):
            return "number"
    return "string"


def _emotion_options(emotions: Sequence[str] | None) -> tuple[tuple[str, str], ...]:
    """情绪那一格的候选。调用方给的是**用户词表的生效值**（设置页「词表」可改），
    缺省 = 仓库默认那 8 个（见 §7.6：它们正好对上各家枚举的底）。"""
    values = EMOTION_CHOICES if emotions is None else tuple((word, word) for word in emotions)
    return (("", "不指定"), *values)


def _options_of(
    field: SpecField, emotions: Sequence[str] | None
) -> tuple[tuple[str, str], ...]:
    """情绪候选随用户词表走；其余格用规范里写死的选项。"""
    if field.key == "emotion":
        return _emotion_options(emotions)
    return field.options


def spec_payload(emotions: Sequence[str] | None = None) -> dict[str, Any]:
    """发给前端的那一整包。

    无 ``note``（界面不摆解释文字）；``folded`` 随组下发，前端不自己攒名单。
    语气词标签不在此包——那是引擎之间的事，走 ``GET /api/v1/marks``。

    :param emotions: 情绪候选的生效值（用户词表，见 :mod:`app.usecase.vocab`）；
        缺省 = 仓库默认的两组词（基础 + 复合）。
    """
    return {
        "groups": [
            {"key": g.key, "label": g.label, "folded": g.folded} for g in GROUPS
        ],
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "group": f.group,
                "widget": f.widget,
                "type": _value_type(f),
                "unit": f.unit,
                "min": f.min,
                "max": f.max,
                "step": f.step,
                "options": [list(pair) for pair in _options_of(f, emotions)],
                "default": f.default,
                "required": f.required,
            }
            for f in SPEC_FIELDS
        ],
    }
