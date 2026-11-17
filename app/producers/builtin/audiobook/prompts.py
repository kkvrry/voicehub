"""对话分析的提示词：内置模板、预设与拼装。

模板认 ``{book}`` / ``{text}`` / ``{rows}`` / ``{roles}`` / ``{emotions}`` 五个占位，
用 ``str.replace`` 而不是 ``str.format``——示例 JSON 里的花括号不该要求调用方写成双份。

``{roles}`` 是「候选角色」那一段（由前面的批次与本项目已有的角色累积而来），
留空时整段消失；自定义提示词没写这个占位时，它会被补在正文最前面。
``{emotions}`` 是情绪候选那一串（设置页「词表」的生效值）——用户自己加了情绪词时，
标注器也该看见，否则自定义的词只能手动逐行设。自定义提示词没写这个占位时原样不动。

``ANALYSIS_PRESETS`` 是仓库默认；设置页「词表」里改过的生效预设由
:mod:`app.producers.builtin.audiobook.ai_prompts` 合并后下发，弹窗顶部那颗下拉
直接照生效值列。提示词正文只有后端这一处产地，前端不抄。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.models import EMOTIONS

__all__ = [
    "ANALYSIS_PRESETS",
    "BUILTIN_PROMPT",
    "DEFAULT_OUTPUT",
    "OUTPUT_FIELDS",
    "PROMPT_VERSION",
    "PromptPreset",
    "compose_prompt",
    "fields_for_prompt",
    "infer_fields",
]

#: 提示词或拼装规则的版本：进缓存键，改了就让旧缓存失效。
#: 5 = 情绪候选改为 ``{emotions}`` 占位（随用户词表走）。
#: 6 = 预设改了名、加了引导指令两版；要回哪几格改由提示词正文推出。
#: 7 = 引导指令改为「情绪基调 + 念法」合成一句（有引擎只认这一路）。
PROMPT_VERSION = 7

BUILTIN_PROMPT = """本次要分析的书是《{book}》。

{roles}下面给出正文上下文，以及其中需要标注的对白行。请结合上下文判断每句对白是谁说的，并标注情绪。
规则：
1. 只处理给出的对白行：不新增、不修改原文；旁白（未列出的行）一律不动。
2. 说话人只填姓名或称谓；上下文里没有线索时填「未知」。
3. 情绪取值：{emotions}。
4. 只输出 JSON：{"rows":[[0,"老人","平静","你来了"]]}
   每行四项：行号、说话人、情绪、原文开头 6 个字（照抄，用来核对行号）。

正文上下文：
{text}

对白行：
{rows}"""

#: 只写引导指令的那一版：不标情绪，行表的情绪列改显引导指令。
#: 指令要把**情绪基调与念法合成一句**：只认这一路的引擎上，情绪没有第二次机会。
INSTRUCTION_PROMPT = """本次要分析的书是《{book}》。

{roles}下面给出正文上下文，以及其中需要标注的对白行。请结合上下文判断每句对白是谁说的，并给这一句写一条引导指令。
引导指令：一句话写这句该怎么念，把情绪基调与念法合成一句。先写情绪或态度，再写音色、语速、音量、
停顿、重音，例如「压抑的怒气，压低嗓子一字一顿地说」「强作轻松，语速偏快，句尾带笑」。
不超过 20 字；用陈述句或短语，用与原文相同的语言；不加引号、不写「语气：」「语调：」这类标签、
不写「他低声说」这种叙述，也不照抄原文。
规则：
1. 只处理给出的对白行：不新增、不修改原文；旁白（未列出的行）一律不动。
2. 说话人只填姓名或称谓；上下文里没有线索时填「未知」。
3. 引导指令按这一句的处境写具体：同一情绪在不同处境下也要有区别，不要整章都用同一句。
4. 只输出 JSON：{"rows":[[0,"老人","压抑的怒气，压低嗓子一字一顿","你来了"]]}
   每行四项：行号、说话人、引导指令、原文开头 6 个字（照抄，用来核对行号）。

正文上下文：
{text}

对白行：
{rows}"""

#: 引导指令与情绪一起给的那一版：两者都在，界面优先显示引导指令。
COMBINED_PROMPT = """本次要分析的书是《{book}》。

{roles}下面给出正文上下文，以及其中需要标注的对白行。请结合上下文判断每句对白是谁说的，
并同时给出情绪与引导指令。
情绪取一个词，取值：{emotions}。
引导指令：一句话写这句该怎么念，把上面这个情绪落到念法上（音色、语速、音量、停顿、重音），
不是把情绪词重写一遍——情绪取「生气」时可以写「压抑的怒气，压低嗓子一字一顿地说」。
不超过 20 字；用陈述句或短语，用与原文相同的语言；不加引号、不写标签、不写叙述、不照抄原文。
同一情绪在不同处境下也要写得有区别，不要整章都用同一句。
规则：
1. 只处理给出的对白行：不新增、不修改原文；旁白（未列出的行）一律不动。
2. 说话人只填姓名或称谓；上下文里没有线索时填「未知」。
3. 只输出 JSON：{"rows":[[0,"老人","压抑的怒气，压低嗓子一字一顿","生气","你来了"]]}
   每行五项：行号、说话人、引导指令、情绪、原文开头 6 个字（照抄，用来核对行号）。

正文上下文：
{text}

对白行：
{rows}"""

#: 回复里除行号与回显之外可能出现的字段组合。字段顺序不是解析依据（见
#: :func:`app.producers.builtin.audiobook.analyze_stage.AnalyzeStage.apply_items`），
#: 它只回答「这次要不要这一格」。
OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "speaker_emotion": ("speaker", "emotion"),
    "speaker_instruction": ("speaker", "instruction"),
    "speaker_instruction_emotion": ("speaker", "instruction", "emotion"),
}
DEFAULT_OUTPUT = "speaker_emotion"


def infer_fields(text: str) -> tuple[str, ...]:
    """按提示词正文里提到的内容判断要模型回哪几格。"""
    has_instruction = "引导指令" in text
    has_emotion = "情绪" in text
    if has_instruction and has_emotion:
        return OUTPUT_FIELDS["speaker_instruction_emotion"]
    if has_instruction:
        return OUTPUT_FIELDS["speaker_instruction"]
    return OUTPUT_FIELDS[DEFAULT_OUTPUT]


def fields_for_prompt(prompt: str) -> tuple[str, ...]:
    """这段提示词要模型回什么——**提示词自己说了算**，不另设开关。

    先按正文认预设（一字不差），认不出就按它提到的内容判：写了「引导指令」就
    解析引导指令，同时写了「情绪」就两个都解析。自定义提示词因此只需把要模型
    回的东西写进去，不必再回来勾一个下拉。

    设置页改过的生效预设用 :func:`app.producers.builtin.audiobook.ai_prompts.analysis_fields`：
    它先认生效预设（预设身份决定要回哪几格），再退回这里的正文推断。
    """
    text = str(prompt or "").strip()
    if not text:
        return OUTPUT_FIELDS[DEFAULT_OUTPUT]
    for preset in ANALYSIS_PRESETS:
        if preset.text.strip() == text:
            return preset.fields
    return infer_fields(text)


@dataclass(frozen=True, slots=True)
class PromptPreset:
    """一条可切换的分析提示词。

    ``fields`` 是这段提示词要求模型输出的内容；``note`` 只说它和「说话人+情绪」那条的差别。
    """

    key: str
    label: str
    note: str
    text: str
    fields: tuple[str, ...] = OUTPUT_FIELDS[DEFAULT_OUTPUT]


ANALYSIS_PRESETS: tuple[PromptPreset, ...] = (
    PromptPreset(
        key="default",
        label="说话人+情绪",
        note="结合正文上下文判断对白角色与情绪。",
        text=BUILTIN_PROMPT,
    ),
    PromptPreset(
        key="instruction",
        label="说话人+引导指令",
        note="只写角色与引导指令，不标情绪；情绪列改显引导指令。",
        text=INSTRUCTION_PROMPT,
        fields=OUTPUT_FIELDS["speaker_instruction"],
    ),
    PromptPreset(
        key="instruction_emotion",
        label="说话人+引导指令+情绪",
        note="引导指令与情绪都标；行表优先显示引导指令。",
        text=COMBINED_PROMPT,
        fields=OUTPUT_FIELDS["speaker_instruction_emotion"],
    ),
    PromptPreset(
        key="terse",
        label="精简",
        note="规则与「说话人+情绪」一致，提示词更短。",
        text="""本次要分析的书是《{book}》。

{roles}正文上下文：
{text}

对白行：
{rows}

判断每句对白的说话人与情绪，每行输出 [行号, 说话人, 情绪, 原文开头 6 个字]；
没有线索时说话人填「未知」；情绪取{emotions}。
只处理给出的对白行，不改原文，只输出 JSON：{"rows":[[0,"老人","平静","你来了"]]}。""",
    ),
    PromptPreset(
        key="conservative",
        label="保守认人",
        note="没有明确依据时填「未知」，不按语气猜测。",
        text="""本次要分析的书是《{book}》。

{roles}下面给出正文上下文，以及需要标注的对白行。只有在上下文中有明确依据时才填写说话人；
仅凭语气、称呼习惯或常识推测的一律填「未知」。不要改动正文，不要处理未列出的旁白。

情绪取值：{emotions}；无法判断时填「中性」。
只输出 JSON：{"rows":[[0,"老人","平静","你来了"]]}
每行四项：行号、说话人、情绪、原文开头 6 个字（照抄）。

正文上下文：
{text}

对白行：
{rows}""",
    ),
)


def compose_prompt(
    custom: str,
    *,
    book: str,
    text: str,
    rows: str,
    roles: Iterable[str] = (),
    emotions: Iterable[str] = (),
) -> str:
    """把占位换成这一次的正文、对白行、候选角色与情绪候选。

    自定义提示词只认这几个占位；留空或没写占位时，仍保证上下文与对白行被带上。
    候选角色没写占位就补在最前面——它是"该从哪些名字里选"的约束，必须看得见。
    情绪候选没写占位就不补：提示词是用户的，默认那批词由用户自己决定要不要列出。

    :param emotions: 情绪候选的生效值（用户词表）；空 = 回落仓库默认的两组词。
    """
    template = str(custom or "").strip() or BUILTIN_PROMPT
    names = [str(name).strip() for name in roles if str(name).strip()]
    hint = f"已出现过的角色（优先沿用这些叫法）：{'、'.join(names)}。\n\n" if names else ""
    words = [str(word).strip() for word in emotions if str(word).strip()]
    listing = "、".join(words) if words else "、".join(EMOTIONS)
    value = (
        template.replace("{book}", book)
        .replace("{text}", text)
        .replace("{rows}", rows)
        .replace("{roles}", hint)
        .replace("{emotions}", listing)
    )
    if hint and "{roles}" not in template:
        value = hint + value
    if "{text}" not in template and "{rows}" not in template:
        value = f"{value.rstrip()}\n\n正文上下文：\n{text}\n\n对白行：\n{rows}"
    return value
