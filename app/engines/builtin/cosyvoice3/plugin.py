"""CosyVoice3 引擎插件——本机 audio.cpp 里的零样本克隆 / 跨语种 / 指令控制 TTS。

**这一家的形状由 ``template_name`` 决定**，它是请求里最先要定的一件事：

- ``zero_shot``（默认）：参考音频 + 逐字稿，克隆同一语言；
- ``cross_lingual``：只给参考音频，用这把嗓子读另一种语言——**跨语种是它明写的模板**；
- ``instruct``：参考音频 + 一句指令，语气与风格跟着指令走。

所以插件按"这次给了什么"选模板：有方言/明确指令/情绪 → ``instruct``；否则看设置里的
「跨语种」开关 → ``cross_lingual``；都不是 → ``zero_shot``。模板定下来才谈其余参数。

**instruct 就是语言控制**：官方示例是「用四川话说这句话」「请用广东话表达」，
不是单独的语言参数。统一语言那一格选到方言时，插件把它翻成一句 instruct。
方言与"怎么念"正交，可以和明确指令同时发；明确指令与情绪则只留指令。

**正文里的发声事件走原生方括号标签**：``[laughter]`` / ``[sigh]`` / ``[breath]`` /
``[cough]`` 等（见 :data:`_MARK_MAP`）；统一写法 ``[[笑]]`` 由网关在发出前译过来。
**包裹式情绪/方向**（官方 demo 的 ``<surprised>…</surprised>``、``<fast>…</fast>``、
``<peppa>…</peppa>`` 等）不被引擎识别，插件会把标签翻成一句 instruct 并从正文移除；
``<laughter>`` / ``<strong>`` 是原生包裹标签，原样保留。

**不要发 ``<|endofprompt|>``**：那是 Python 库的分隔符；audio.cpp 这层收指令原文，
带上它模型会把指令当正文念出来（实测）。插件遇到时会去掉。

**中文指令按 20 字截断**：instruct 模板把指令放在 prompt 一侧，但模型对长指令的
边界并不稳——超过约 20 字后，后半段会被当成正文念在目标文本前面（实测：36 字的
「用西安话说这句话。 语气发紧…」原样念出；19 字的「用西安话说这句话，语气发紧，
语速偏快。」不念）。插件先把中间的句号/分号并成逗号，再在标点处截到 20 字以内，
并在结果说明里写明未发送的部分；宁可少一句风格描述，也不让指令进入音频。

**其余落点**（audio.cpp 的 ``/v1/audio/speech``）：

- 正文 → ``input``；参考音频 → ``voice_ref``（本机绝对路径）；逐字稿 →
  ``reference_text``。
- 采样与解码 → ``options``：``top_k``、``num_inference_steps``、``text_chunk_size``。
- **种子 → 顶层 ``seed``**：省略时引擎用定值 **1986**，所以"每次不一样"必须由插件
  抽一个发出去并报回抽到的数（与 BreezeTTS 2 / DotTTS / VoxCPM2 同一条）。

**情绪走指令**：这一家没有情绪枚举，但 instruct 模板收自由文本，所以情绪值由插件
翻成一句引导语（与 BreezeTTS 2 共用 :mod:`app.domain.emotion_instruction` 那张表），
同时把模板切到 ``instruct``。方言与情绪可以同时发；有明确指令时指令优先，情绪不再
重复发送。

没有的能力如实不声明：没有内置音色、不注册音色 id、没有原生情绪枚举、没有音量 /
音高——语气由指令与参考音频决定。语速自 audio.cpp 0.9.6 起原生支持（
``options.speed``，HiFT 前重采样 mel）。没有落点的项给了就在结果说明里逐条报
"未发送"；情绪有落点（翻成 instruct），不在其中。
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.emotion_instruction import EMOTION_INSTRUCTIONS, emotion_instruction
from app.domain.models import LANGUAGE_CHOICES
from app.engines.base import (
    KIND_LOCAL,
    Capabilities,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
    param_label,
)
from app.engines.builtin.cosyvoice3 import client
from app.engines.builtin.cosyvoice3.settings import CosyVoice3Settings
from app.engines.mapping import EngineMapping, ParamMapping, apply_mapping
from app.infra.loghub import CHANNEL

__all__ = ["MAPPING", "RANDOM_SEED", "TEMPLATES", "CosyVoice3Engine", "resolve_seed"]

#: 种子写成负数（约定 ``-1``）= 随机：由插件抽一个再发出去。
#:
#: 这一家的规格里 ``seed`` 默认写死 **1986**——省略种子不会"每次不一样"，
#: 所以随机只能由插件做（与 BreezeTTS 2 / DotTTS / VoxCPM2 同一条结论）。
RANDOM_SEED = -1

#: ``template_name`` 的三个取值：克隆 / 跨语种 / 指令。
TEMPLATES = ("zero_shot", "cross_lingual", "instruct")

#: 统一参数 → CosyVoice3 请求体。落点名字取自 ``model_specs/cosyvoice3.json``。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", required=True, note="要合成的正文。"),
        ParamMapping(
            "reference_audio",
            "voice_ref",
            # 必填由 synthesis_spec 声明并在装配时报（这里标 required 会先抛 ValueError，
            # 文案不如"请先在左栏选一条带本地样例的音色"具体）。
            note="参考音频；本机绝对路径，WAV。",
            warning=True,
        ),
        ParamMapping("reference_text", "reference_text", note="参考音频的逐字稿。"),
        ParamMapping(
            "speed",
            "options.speed",
            note="语速倍率，>1 加快、<1 放慢（audio.cpp 0.9.6 起支持）。",
        ),
    ),
    cautions=(
        "模板由插件按输入选：有引导指令走 instruct、开了跨语种走 cross_lingual、否则 zero_shot。",
        "种子省略时引擎用定值 1986；要每次不同必须由插件抽样发送。",
    ),
)

#: 上游给了、这一家没有落点、且值得随请求报出来的项 → 原因。
_UNSUPPORTED: dict[str, str] = {
    "reference_language": "本引擎无对应参数",
    "guidance_scale": "本引擎无指令强度参数",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
}

#: 只在面板映射表里出现的说明（都是这台引擎的恒定量）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频代表音色，没有音色 ID。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出固定为 WAV，无码率参数。",
    "channels": "输出参数固定，不可指定。",
}

#: 情绪说法按这一家的 instruct 模板调强：短而温和的句子它动得很轻。
#:
#: 实测（同一段中文、同一参考音频、固定 seed）：共用表里的「用愤怒、强硬的语气
#: 朗读。」只让能量 +5%（0.0242 → 0.0255）、时长几乎不变；换成下面「生气地质问，
#: 声音提高，语速加快，句尾往上顶。」能量到 0.0606（约 2.5 倍）、音高下降约 8%。
#: 所以同一档情绪在这里要写成"声音怎么变、快慢怎么变、句尾往哪走"，而不是只报
#: 一个情绪词。没覆盖到的情绪仍走共用表。
_EMOTION_OVERRIDES: dict[str, tuple[str, str]] = {
    "开心": (
        "用明亮、上扬的语气说话，语速轻快，句尾带笑意。",
        "Speak brightly and cheerfully, a little faster, with a smile in the voice.",
    ),
    "悲伤": (
        "用低沉、压抑的语气说话，语速放慢，句尾往下坠。",
        "Speak in a low, heavy tone, slower, letting the endings fall.",
    ),
    "愤怒": (
        "生气地质问，声音提高，语速加快，句尾往上顶。",
        "Speak angrily and loudly, faster, with sharp, rising endings.",
    ),
    "恐惧": (
        "用发抖的气声说话，音量压低，气息不稳。",
        "Speak in a shaky, breathy voice, quietly and unevenly.",
    ),
    "惊讶": (
        "用吃惊、上扬的语气说话，句首提高，像刚反应过来。",
        "Speak in a surprised, rising tone, starting high as if caught off guard.",
    ),
    "兴奋": (
        "用兴奋、上扬的语气说话，语速偏快，音量提高。",
        "Speak excitedly, faster and louder, with rising energy.",
    ),
    "平静": (
        "用平稳、舒缓的语气说话，音量适中，句尾放松。",
        "Speak calmly and evenly, at a moderate volume, with relaxed endings.",
    ),
    "冷漠": (
        "用冷淡、平面的语气说话，几乎没有起伏，句尾收短。",
        "Speak in a cold, flat tone, almost no inflection, clipping the endings.",
    ),
    "委屈": (
        "用委屈、压着的语气说话，声音偏低，句尾发虚。",
        "Speak in a wronged, restrained tone, lower and softer, with fading endings.",
    ),
    "撒娇": (
        "用撒娇的语气说话，尾音拖长、发软，带一点鼻音。",
        "Speak in a coquettish tone, drawing out soft endings with a little nasality.",
    ),
}

#: 方言/语言 → 一句 instruct。**这一家的 instruct 就是语言控制**：官方示例写
#: 「用四川话说这句话」「请用广东话表达」，不是单独的语言参数。
#:
#: 中文（zh）与非汉语语种不在这里：正文本身已经说明语言，额外发一句只会让模型
#: 分心。``yue`` 用官方原句；``cmn-cd`` 用官方 CosyVoice2 示例原句（CosyVoice3
#: 实测同样有效，音高会明显下降）；其余方言点按同一句式写。
_LANGUAGE_INSTRUCTIONS: dict[str, str] = {
    "yue": "请用广东话表达。",
    "cmn-cd": "用四川话说这句话。",
    "cmn-wh": "用武汉话说这句话。",
    "cmn-xa": "用西安话说这句话。",
    "cmn-zz": "用郑州话说这句话。",
    "cmn-jn": "用济南话说这句话。",
    "cmn-nj": "用南京话说这句话。",
    "wuu-sz": "用苏州话说这句话。",
    "wuu-hz": "用杭州话说这句话。",
    "nan-qz": "用泉州话说这句话。",
    "hsn-cs": "用长沙话说这句话。",
}

#: 语言那一格可能收到代码（``cmn-cd``）或界面说法（``成都话``）：两者都认。
_LANGUAGE_BY_LABEL: dict[str, str] = {label: code for code, label in LANGUAGE_CHOICES}

#: 方言代码 → 界面说法（能力表与 ``dialects`` 用）。
_DIALECT_LABELS: tuple[str, ...] = tuple(
    label for code, label in LANGUAGE_CHOICES if code in _LANGUAGE_INSTRUCTIONS
)

#: 正文内嵌的发声事件 → audio.cpp 的 CosyVoice3 tokenizer 原生写法。
#:
#: 这张表只收**运行时确实认的标签**（``audiocpp_server.exe`` 里的 tokenizer 字符串：
#: ``[breath]`` / ``[quick_breath]`` / ``[laughter]`` / ``[cough]`` / ``[sigh]`` /
#: ``[hissing]`` / ``[lipsmack]`` / ``[mn]`` 等）。没有 ``[gasp]``——第三方教程里
#: 写的那条在这个运行时里不存在，登记了只会被当正文念出来。
_MARK_MAP: dict[str, str] = {
    "笑": "[laughter]",
    "轻笑": "[laughter]",
    "叹气": "[sigh]",
    "换气": "[breath]",
    "喘气": "[quick_breath]",
    "咳嗽": "[cough]",
    "咂嘴": "[lipsmack]",
    "嗯": "[mn]",
    "嘶嘶声": "[hissing]",
}

#: 包裹式情绪标签 → 规范情绪词。官方 demo 的 "Instruct ability transfer" 用
#: ``<sad>`` / ``<surprised>`` / ``<angry>`` / ``<fearful>`` 这类写法；它们是
#: **demo 层的简写**，不是 tokenizer 原生 token——引擎原生只认 ``<laughter>`` /
#: ``<strong>``。裸发 ``<surprised>`` 会被当正文念出来（实测 ASR 听到
#: "surprised, 他停下脚步…"），所以插件先把它拆成一句 instruct，再把标签移除。
_EMOTION_TAGS: dict[str, str] = {
    "happy": "开心",
    "sad": "悲伤",
    "angry": "愤怒",
    "surprised": "惊讶",
    "surprise": "惊讶",
    "fearful": "恐惧",
    "afraid": "恐惧",
    "scared": "恐惧",
    "disgusted": "厌恶",
    "excited": "兴奋",
    "calm": "平静",
    "neutral": "中性",
    "shy": "害羞",
    "worried": "担忧",
    "nervous": "忐忑",
    "jealous": "嫉妒",
    "guilty": "愧疚",
    "relieved": "释然",
    "disappointed": "失望",
    "confused": "疑惑",
    "puzzled": "疑惑",
    "pleading": "恳求",
    "smug": "得意",
    "coquettish": "撒娇",
    "serious": "严肃",
    "contemptuous": "轻蔑",
    "weary": "厌倦",
    "moved": "动情",
    "grateful": "欣慰",
    "resigned": "无奈",
    "wistful": "怅然",
    **{name: name for name in EMOTION_INSTRUCTIONS},
}

#: 官方 demo 里除情绪外还演示了这几条包裹标签（Instruct ability transfer）：
#: ``<fast>`` / ``<slow>`` / ``<peppa>`` / ``<robot>``。它们直接对应官方
#: ``instruct_list`` 里的句子，原样翻过去即可。
_WRAPPER_DIRECTIONS: dict[str, str] = {
    "fast": "请用尽可能快地语速说一句话。",
    "slow": "请用尽可能慢地语速说一句话。",
    "peppa": "我想体验一下小猪佩奇风格，可以吗？",
    "robot": "你可以尝试用机器人的方式解答吗？",
}

#: 包裹式标签：``<tag>正文</tag>``。只处理**成对且同名**的标签；未知标签原样
#: 留着，让引擎自己决定（``<laughter>`` / ``<strong>`` 是它原生认识的）。
_WRAPPER_RE = re.compile(r"<([^<>\s]+)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_LAUGHING_RE = re.compile(r"<laughing>(.*?)</laughing>", re.IGNORECASE | re.DOTALL)

#: 中文 instruct 的字符上限（标点算一个字符）。
#:
#: 这一家的 instruct 把指令放在 prompt 一侧；短句能稳住，长句的后半段会被模型
#: 当正文念出来。实测 36 字的「用西安话说这句话。 语气发紧，语速偏快，句子之间
#: 几乎不停，像有件急事压着。」会把「语气发紧…」原样念在目标文本前；19 字的
#: 「用西安话说这句话，语气发紧，语速偏快。」在多个种子下都不念。截在标点上，
#: 保留前半段最要紧的风格，其余在 ``notes`` 里报出来。
_INSTRUCT_MAX_CHARS = 20

#: 只用来判断"要不要按中文字数截断"；英文指令的字符密度不同，不套这个上限。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _fit_instruction(text: str) -> tuple[str, bool]:
    """把中文指令收成一句话，并截到 :data:`_INSTRUCT_MAX_CHARS` 以内。

    返回 ``（发出去的指令, 是否有省略）``。中间的句号/分号改逗号（这一家的长句与
    多句会被念成正文）；整条都不超限时原样返回；第一段就超限时硬截（否则整条都
    发不出去）；英文指令不截（没有实测依据，不拿中文字数硬套）。
    """
    text = (text or "").strip()
    if not text:
        return "", False
    if _CJK_RE.search(text):
        # 中间的句号/分号会把后半段变成"正文"（实测「用西安话说这句话。 语气发紧…」
        # 会念出后半句）。统一改成逗号，让整条指令保持一句话；句尾的标点保留。
        text = re.sub(r"[。；！？]+(?=\s|[\u3400-\u4dbf\u4e00-\u9fff])", "，", text)
        text = re.sub(r"[ \t]*([，、])[ \t]*", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= _INSTRUCT_MAX_CHARS or not _CJK_RE.search(text):
        return text, False

    kept = ""
    for segment in re.split(r"(?<=[，。；！？、])", text):
        if not segment.strip():
            continue
        # 保留原文的分隔（中文常没有空格；方言 + 引导指令之间有一个空格）。
        candidate = (kept + segment).strip()
        if len(candidate) > _INSTRUCT_MAX_CHARS:
            if kept:
                return kept.rstrip("，。；！？、 ") + "。", True
            return (
                segment.strip()[:_INSTRUCT_MAX_CHARS].rstrip("，。；！？、 ") + "。",
                True,
            )
        kept = candidate
    return kept, False


def _unwrap_directions(text: str) -> tuple[str, str, str, tuple[str, ...]]:
    """拆掉正文里的包裹标签——返回 ``(干净正文, 情绪, 直接指令, 其余指令)``。

    官方 demo 的 ``<surprised>`` / ``<fast>`` 这类写法不是 tokenizer 原生 token；
    demo 层会把标签翻成一句 instruct，再把标签从正文移除。这里做同一件事。

    这一家的 instruct 是**整条请求**的，不能按句子中途换情绪；所以多个不同包裹
    只采用第一个，其余标签一并移除（不留给引擎念）。``<laughter>`` / ``<strong>``
    不在两张表里，原样保留。
    """
    emotions: list[str] = []
    directions: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tag = match.group(1).strip().lower()
        emotion = _EMOTION_TAGS.get(tag)
        if emotion is not None:
            emotions.append(emotion)
            return match.group(2)
        direction = _WRAPPER_DIRECTIONS.get(tag)
        if direction is not None:
            directions.append(direction)
            return match.group(2)
        return match.group(0)

    cleaned = _WRAPPER_RE.sub(replace, text or "")
    # 官方 tokenizer 只有 <laughter>；第三方常用的 <laughing> 归一到它。
    cleaned = _LAUGHING_RE.sub(r"<laughter>\1</laughter>", cleaned)
    return (
        cleaned,
        emotions[0] if emotions else "",
        directions[0] if directions else "",
        tuple(emotions[1:] + directions[1:]),
    )


def _provided(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def resolve_seed(value: Any) -> tuple[int, str]:
    """这一次发出去的种子——返回 ``（值, 对用户说的一句话）``。

    负数（约定 ``-1``）= 随机：抽一个、发出去、把抽到的数报回来。这一家省略种子时
    用定值 1986，所以"随机"只能显式抽。
    """
    try:
        seed = int(value)
    except (TypeError, ValueError):
        raise EngineError(f"种子「{value}」不是整数。") from None
    if seed >= 0:
        return seed, f"seed={seed}"
    drawn = random.randrange(0, 2**31)
    CHANNEL.info(
        f"种子写的是 {seed}（= 随机），抽取结果 {drawn}（可用它复现）",
        source="engine",
        engine="cosyvoice3",
    )
    return drawn, f"seed={drawn}"


class CosyVoice3Engine(EnginePlugin):
    """CosyVoice3（audio.cpp，OpenAI 兼容端点）。"""

    key = "cosyvoice3"
    label = "CosyVoice3"
    version = "1.0.0"
    homepage = "https://github.com/FunAudioLLM/CosyVoice"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return CosyVoice3Settings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 没有内置音色，也没有清单端点：音色就是你的参考音频。
            voice_listing=False,
            # 零样本：不注册音色 id，克隆是"每次带样本"。
            voice_cloning=False,
            # 指令只能调语气，不能凭一句描述造出一个音色（参考音频必填）。
            voice_design=False,
            # 语气由自由文本指令控制（动态映射到它的 instruct 落点）。
            instruction_control=True,
            reference_audio=True,
            # 没有情绪枚举——情绪由插件翻成一句 instruct，那是"翻译"，
            # 不是原生支持。
            emotion_control=False,
            # 长文本由服务端自己分块（``text_chunk_size`` 默认 600 字符，超出按句切、
            # 逐段合成再拼），所以没有"发不出去"的上限；0 = 不拦。
            max_chars=0,
            # 本机单模型服务：并发只会让第二条排队。
            max_concurrency=1,
            # 语言：model_specs 的 9 种语言，加上官方列的 18+ 中文方言里统一词表
            # 覆盖到的方言点（方言代码 → instruct 句子，见 _LANGUAGE_INSTRUCTIONS）。
            languages=(
                "zh",
                *_LANGUAGE_INSTRUCTIONS,
                "en",
                "ja",
                "ko",
                "de",
                "es",
                "fr",
                "it",
                "ru",
            ),
            # 跨语种是它明写的模板（cross_lingual）：同一把嗓子读另一种语言。
            cross_lingual=True,
            dialects=_DIALECT_LABELS,
            language_note="方言走 instruct：选方言时自动发一句「用四川话说这句话」。",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # 三条模板都以参考音频为前提（instruct 也是"带参考音频的命令式克隆"）。
            requires=("reference_audio",),
            formats=("wav",),
            reference_formats=("wav",),
            # zero_shot / instruct 都要逐字稿；跨语种模板不用，但补上只多一次识别。
            needs_transcript=True,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """落点随请求变的那几格：静态映射表表达不了。"""
        return {
            "instruction": (
                "options.instruct",
                "写进指令；同时把模板切成 instruct。超过 20 字会截断，后半段会被念出来。",
            ),
            "emotion": (
                "options.instruct",
                "翻成一句引导语；有明确指令时以指令为准。",
            ),
            "language": (
                "options.instruct",
                "方言翻成「用四川话说这句话」；与明确指令可以同时发。",
            ),
            "seed": ("seed", "固定种子；-1 表示由插件抽样（引擎省略时用定值 1986）。"),
        }

    def mark_map(self) -> dict[str, str]:
        """正文内嵌的发声事件：audio.cpp 的 CosyVoice3 tokenizer 认方括号标签。"""
        return _MARK_MAP

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: CosyVoice3Settings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: CosyVoice3Settings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model

        # 包裹式标签（<surprised>…</surprised>、<fast>…</fast>）先拆掉：这一家
        # 不认这种标签，留着会被当英文单词念出来。拆出来的情绪和情绪那一格同权。
        raw_text = str(values.get("text") or "")
        text, wrapper_emotion, wrapper_direction, extra_directions = _unwrap_directions(
            raw_text
        )
        if not text.strip():
            raise EngineError("正文里只有包裹标签，去掉标签后没有可念的字。")
        body["input"] = text

        notes: list[str] = []
        options: dict[str, Any] = body.setdefault("options", {})

        # ---- 模板：方言/明确指令/情绪 > 跨语种 > 克隆 ----
        # instruct 是这一家唯一的控制入口：方言、情绪都翻成一句自然的祈使句。
        # 方言是"用哪种话"，与"怎么念"正交；明确指令与方言可以同时存在，
        # 但与情绪只能留一个——两路都是"怎么念"，同时下发只会互相稀释。
        explicit = str(values.get("instruction") or "").strip()
        # 官方示例里的 <|endofprompt|> 是 Python 库的分隔符；audio.cpp 这层收的是
        # 指令原文。实测把它一起发出去，模型会把前半句当正文念出来。
        if "<|endofprompt|>" in explicit:
            explicit = explicit.replace("<|endofprompt|>", "").strip()
            CHANNEL.info(
                "引导指令里的 <|endofprompt|> 已去掉：这一层收指令原文，带上会被念出来。",
                source="engine",
                engine="cosyvoice3",
            )
        field_emotion = str(values.get("emotion") or "").strip()
        emotion = wrapper_emotion or field_emotion
        if extra_directions:
            CHANNEL.info(
                "正文有多个同类包裹标签，只采用第一个；其余已从正文移除。",
                source="engine",
                engine="cosyvoice3",
            )
        if wrapper_emotion and field_emotion and field_emotion != wrapper_emotion:
            CHANNEL.info(
                f"情绪包裹「{wrapper_emotion}」覆盖了情绪那一格「{field_emotion}」。",
                source="engine",
                engine="cosyvoice3",
            )
        raw_language = str(values.get("language") or "").strip()
        language = _LANGUAGE_BY_LABEL.get(raw_language, raw_language)
        parts: list[str] = []
        dialect = _LANGUAGE_INSTRUCTIONS.get(language, "")
        if dialect:
            parts.append(dialect)
        if explicit:
            parts.append(explicit)
            if emotion or wrapper_direction:
                CHANNEL.info(
                    "明确指令优先：包裹式情绪/方向不再另发。",
                    source="engine",
                    engine="cosyvoice3",
                )
        else:
            if wrapper_direction:
                parts.append(wrapper_direction)
            if emotion:
                mood = emotion_instruction(
                    emotion,
                    text,
                    overrides=_EMOTION_OVERRIDES,
                )
                if mood:
                    parts.append(mood)
        instruction = " ".join(parts)
        if instruction:
            original_length = len(instruction)
            instruction, clipped = _fit_instruction(instruction)
            if clipped:
                notes.append(
                    f"引导指令 {original_length} 字，CosyVoice3 会把长指令的后半段当正文念出来；"
                    f"本次发送「{instruction}」，其余未发送。"
                )
                CHANNEL.info(
                    f"指令过长（{original_length} 字），已截到「{instruction}」；"
                    "这一家的长指令会被念进音频。",
                    source="engine",
                    engine="cosyvoice3",
                )
            CHANNEL.info(
                "按引导指令发送：" + instruction,
                source="engine",
                engine="cosyvoice3",
            )
        if instruction:
            options["template_name"] = "instruct"
            options["instruct"] = instruction
        elif cfg.cross_lingual:
            options["template_name"] = "cross_lingual"
        else:
            options["template_name"] = "zero_shot"

        # ---- 参考音频：本机绝对路径，给了就必须读得到 ----
        reference = str(body.get("voice_ref") or "").strip()
        if not reference:
            raise EngineError(
                "CosyVoice3 需要参考音频（音色样例）：它没有内置音色，"
                "请先在左栏选一条带本地样例的音色。"
            )
        source = Path(reference)
        if not source.is_file():
            raise EngineError(
                f"CosyVoice3 读不到参考音频：{reference}（文件不存在或已被移动）。"
            )
        body["voice_ref"] = str(source)

        # ---- 解码参数：设置里的默认值随请求落进 options ----
        options["top_k"] = int(cfg.top_k)
        options["num_inference_steps"] = int(cfg.num_inference_steps)
        if int(cfg.text_chunk_size or 0) > 0:
            options["text_chunk_size"] = int(cfg.text_chunk_size)

        # ---- 种子：-1 = 随机（引擎的默认是定值 1986，必须由插件抽） ----
        raw_seed = values.get("seed")
        seed, seed_note = resolve_seed(raw_seed if _provided(raw_seed) else cfg.seed)
        body["seed"] = seed
        notes.append(seed_note)

        ignored = [param_label(key) for key in _UNSUPPORTED if _provided(values.get(key))]
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        return body, tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        assert isinstance(cfg, CosyVoice3Settings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
