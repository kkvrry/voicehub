"""VoxCPM2 引擎插件——本机 audio.cpp 里的 2B 多语言 TTS（零样本克隆 + 描述设计音色）。

**它和别家最不一样的一条**：这一家**没有独立的风格/指令字段**。音色设计与语气控制
都写成正文最前面的一句括号描述（``(温柔年轻女性)正文``）——所以本插件把「引导指令」
「音色描述」「情绪」「方言」四样都拼进 ``input`` 的前缀，而不是塞进某个参数格。
服务端的文档写得很直白：``clon`` / ``vdes`` 只是同一个实现的别名，路由不改变可用的输入。

**方言也走这句前缀**：官方 9 种中文方言（粤语、四川话、吴语、东北话、河南话、
陕西话、山东话、天津话、闽南话）没有独立参数，控制指令里写方言名即可；正文仍要
尽量用方言写法，否则只是普通话的方言口音。非方言语言由正文自动判断，不单独发送。

**其余落点**：

- **正文 → ``input``**；**参考音频 → ``voice_ref``**（本机绝对路径，WAV）；
  **逐字稿 → ``reference_text``**。
- **参考音频 + 逐字稿同时给 → ``options.ultimate_clone=true``**（官方称高保真克隆，
  保真度最高）；只给音频时按普通克隆走，此时不需要逐字稿。
  **有风格/方言/情绪时不走高保真克隆**：官方文档写明 Hi-Fi 模式会忽略控制指令，
  此时退成可控克隆——参考音频照发，逐字稿不发，风格写在正文前缀。
- **指令强度 → ``options.guidance_scale``**：我们的 0–1 刻度换算成这一家的 1–5
  （0.5 → 2，即它的默认）。换算器见 :func:`unit_to_guidance`。
- **推理步数 → ``options.num_inference_steps``**（默认 10）。
- **种子 → 顶层 ``seed``**：**省略时引擎用定值 1234**，所以"每次不一样"必须由插件
  抽一个发出去并报回抽到的数（与 BreezeTTS 2 / DotTTS 同一条道理）。
- **``retry_badcase`` 只在离线模式可用**（流式必须 false）。插件只走离线整段合成，
  因此保持引擎默认（开），不发这个字段。

没有的能力如实不声明：没有内置音色、不注册音色 id（克隆是"每次带样本"）、
没有情绪枚举、没有语速/音量/音高/静音参数——快慢与情绪都靠那句括号描述。
给了这些值会在结果说明里逐条报"未发送"，不静默丢。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.emotions import canonical_emotion
from app.domain.models import LANGUAGE_CHOICES
from app.engines.base import (
    KIND_LOCAL,
    Capabilities,
    CloneRequest,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
    VoiceMeta,
    param_label,
)
from app.engines.builtin.voxcpm2 import client
from app.engines.builtin.voxcpm2.settings import VoxCpm2Settings
from app.engines.mapping import EngineMapping, ParamMapping, apply_mapping, register_transform
from app.engines.seed import resolve_seed as _resolve_seed
from app.infra.loghub import CHANNEL

__all__ = [
    "EMOTION_STYLES",
    "RANDOM_SEED",
    "VoxCpm2Engine",
    "emotion_style",
    "resolve_seed",
    "unit_to_guidance",
]

#: 种子写成负数（约定 ``-1``）= 随机：由插件抽一个再发出去。
#:
#: 这一家的规格里 ``seed`` 默认写死 **1234**——省略种子不会"每次不一样"，
#: 所以随机只能由插件做（与 BreezeTTS 2 / DotTTS 同一条结论）。
RANDOM_SEED = -1

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_LATIN = re.compile(r"[A-Za-z]")


def unit_to_guidance(value: Any) -> float | None:
    """规范里的 0–1 刻度 → 这一家的 1–5 刻度。

    取值点对齐两边的"推荐默认"：0 → 1（最松）、**0.5 → 2（它的默认）**、1 → 5（最紧）。
    两段线性：前段 1+2x，后段 2+6(x−0.5)。压到 1–5 之外没有意义，直接钳住。
    """
    try:
        unit = float(value)
    except (TypeError, ValueError):
        return None
    unit = min(max(unit, 0.0), 1.0)
    guidance = 1 + 2 * unit if unit <= 0.5 else 2 + 6 * (unit - 0.5)
    return round(guidance, 3)


register_transform("unit_to_guidance", unit_to_guidance, label="0–1 刻度 → 引导强度（1 / 2 / 5）")

#: 统一参数 → VoxCPM2 请求体。落点名字取自 ``docs/tts.md`` 的 request options。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", required=True, note="要合成的正文；风格描述写在它最前面。"),
        ParamMapping(
            "reference_audio", "voice_ref", note="参考音频；本机绝对路径，WAV。"
        ),
        ParamMapping(
            "reference_text",
            "reference_text",
            note="参考音频的逐字稿；没有风格/方言时按高保真克隆发送。",
        ),
        ParamMapping(
            "guidance_scale",
            "options.guidance_scale",
            transform="unit_to_guidance",
            note="0–1 刻度换算为 1–5 的引导强度。",
        ),
    ),
    cautions=(
        "音色与语气写在正文最前面、括号内：(温柔年轻女性)正文；这一家没有独立的指令字段。",
        (
            "参考音频与逐字稿同时给时按 ultimate_clone 发送，保真度最高；"
            "有风格/方言时改走可控克隆，逐字稿不发送。"
        ),
        "种子省略时引擎用定值 1234；要每次不同必须由插件抽样发送。",
    ),
)

#: 上游给了、这一家没有落点、且值得随请求报出来的项 → 原因。
_UNSUPPORTED: dict[str, str] = {
    "speed": "只能用括号描述控制，不是精确倍速",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
    "reference_language": "本引擎无对应参数",
}

#: 方言代码/界面说法 → 控制指令里的写法。
#:
#: 官方列出 9 种中文方言。代码来自统一词表；界面说法也收，因为引擎声明的
#: ``dialects`` 会以原词作为候选值（东北话、天津话没有独立代码，只能走说法）。
#: 方言只对中文正文生效，英文正文不发送。
_DIALECT_PROMPTS: dict[str, str] = {
    "yue": "粤语",
    "粤语": "粤语",
    "cantonese": "粤语",
    "cmn-cd": "四川话",
    "成都话": "四川话",
    "四川话": "四川话",
    "wuu-sz": "吴语",
    "wuu-hz": "吴语",
    "苏州话": "吴语",
    "杭州话": "吴语",
    "吴语": "吴语",
    "cmn-zz": "河南话",
    "郑州话": "河南话",
    "河南话": "河南话",
    "cmn-xa": "陕西话",
    "西安话": "陕西话",
    "陕西话": "陕西话",
    "cmn-jn": "山东话",
    "济南话": "山东话",
    "山东话": "山东话",
    "nan-qz": "闽南话",
    "泉州话": "闽南话",
    "闽南语": "闽南话",
    "闽南话": "闽南话",
    "cmn-ne": "东北话",
    "东北话": "东北话",
    "cmn-tj": "天津话",
    "天津话": "天津话",
}

#: 统一语言词表里所有「方言点」代码：不在 :data:`_DIALECT_PROMPTS` 里的就是
#: 这家不支持的方言（武汉、南京、长沙），要如实报"未发送"。
_DIALECT_CODES: frozenset[str] = frozenset(
    code for code, _ in LANGUAGE_CHOICES if code == "yue" or "-" in code
)
_LANGUAGE_BY_LABEL: dict[str, str] = {label: code for code, label in LANGUAGE_CHOICES}
_LANGUAGE_LABEL: dict[str, str] = {code: label for code, label in LANGUAGE_CHOICES}

#: 只在面板映射表里出现的说明。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频或音色描述代表音色，没有内置音色 id。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出固定为 WAV，无码率参数。",
}

#: 情绪 → 括号里那句风格描述（中文写法, 英文写法）。
#:
#: 这一家没有情绪参数，语气只能写成描述；认不出的自定义情绪走模板句。
EMOTION_STYLES: dict[str, tuple[str, str]] = {
    # 基础情绪
    "开心": ("轻快愉悦的语气", "a happy, cheerful tone"),
    "悲伤": ("低沉克制的语气，语速稍慢", "a sad, subdued tone, a little slower"),
    "愤怒": ("强硬急促的语气", "an angry, forceful tone"),
    "恐惧": ("发颤、放轻的语气", "a frightened, trembling voice"),
    "惊讶": ("上扬、带惊讶的语气", "a surprised, rising tone"),
    "兴奋": ("上扬、语速偏快的语气", "an excited, quickened tone"),
    "委屈": ("低回、忍着情绪的语气", "a wronged, held-back tone"),
    "平静": ("舒缓、从容的语气", "a calm, unhurried tone"),
    "冷漠": ("平直、不带起伏的语气", "a flat, indifferent tone"),
    # 复合情绪
    "怅然": ("怅然若失、克制的语气", "a wistful, restrained tone"),
    "欣慰": ("温和、带笑意的语气", "a warm, gratified tone"),
    "无奈": ("放缓、认命似的语气", "a resigned, unhurried tone"),
    "愧疚": ("放低、带歉意的语气", "a guilty, apologetic tone"),
    "释然": ("放松、如释重负的语气", "a relieved, relaxed tone"),
    "嫉妒": ("发紧、压着火的语气", "a jealous, tight tone"),
    "厌倦": ("提不起劲的语气", "a weary, listless tone"),
    "忐忑": ("迟疑、气息不稳的语气", "an uneasy, hesitant tone"),
    "动情": ("带哽咽、气息起伏的语气", "a moved, slightly choked tone"),
    "害羞": ("音量收着、气息偏弱的语气", "a shy, quiet tone with a soft, uneven breath"),
    "疑惑": ("上扬而迟疑的语气", "a puzzled, rising, hesitant tone"),
    "失望": ("尾音下坠、收短的语气", "a disappointed tone, endings cut short"),
    "担忧": ("压低声音、放不下的语气", "a worried, lowered voice"),
    # 态度
    "严肃": ("收住、不放笑意的语气", "a serious, contained tone, without warmth"),
    "轻蔑": ("带冷笑、居高临下的语气", "a contemptuous, sneering tone"),
    "恳求": ("带着求人的急切的语气", "a pleading, urgent tone"),
    "得意": ("放松而上扬的语气", "a smug, relaxed, rising tone"),
    "撒娇": ("拖长尾音、偏软的语气", "a coquettish tone, drawing out the endings"),
    # 旧词直通：候选里没有它们了，老项目的行还写着，别掉到模板句。
    "中性": ("平稳自然的语气", "a neutral, natural delivery"),
    "厌恶": ("冷淡、嫌弃的语气", "a disgusted, cold tone"),
}


def _english_text(text: str) -> bool:
    """正文是不是英文——描述要与正文同语言（官方要求）。"""
    raw = text or ""
    return len(_LATIN.findall(raw)) > len(_CJK.findall(raw))


def emotion_style(emotion: str, text: str) -> str:
    """情绪 → 一句风格描述；空情绪返回空串。认不出的词走模板，不当作"没有情绪"。"""
    raw = (emotion or "").strip()
    if not raw:
        return ""
    key = canonical_emotion(raw) or raw
    pair = EMOTION_STYLES.get(key)
    english = _english_text(text)
    if pair is not None:
        return pair[1] if english else pair[0]
    return f'a "{key}" tone' if english else f"「{key}」的语气"


def resolve_seed(value: Any) -> tuple[int, str]:
    """这一次发出去的种子（负数 = 随机，抽到的数报回来）。"""
    seed, note = _resolve_seed(value, engine="voxcpm2", ceiling=2**31 - 1)
    if seed is None:
        raise EngineError("种子缺失。")
    return seed, note


def _provided(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


class VoxCpm2Engine(EnginePlugin):
    """VoxCPM2（audio.cpp，OpenAI 兼容端点）。"""

    key = "voxcpm2"
    label = "VoxCPM2"
    version = "1.0.0"
    homepage = "https://github.com/OpenBMB/VoxCPM"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return VoxCpm2Settings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 没有内置音色，也没有清单端点。
            voice_listing=False,
            # 零样本：参考音频本身就是音色，没有"注册一个音色 id"这一步。
            voice_cloning=False,
            # 一句话描述出一个音色（写进括号），这是它的看家本事之一。
            voice_design=True,
            # 引导指令写进正文开头的括号描述（动态映射）。
            instruction_control=True,
            reference_audio=True,
            # 没有情绪枚举——语气写成描述，那是"翻译"，不是原生支持。
            emotion_control=False,
            # 长文本由服务端自己分块（``text_chunk_size`` 默认 **2048 字符**，
            # 仅 offline 模式——我们只走 offline），所以没有硬上限；0 = 不拦。
            max_chars=0,
            # 本机单模型服务：并发只会让第二条排队。
            max_concurrency=1,
            # 语言：官方 README 写的是 30 种语言、**按正文自动判断、不需要语言标签**
            # （所以「正文语言」那一格在映射表里不是原样发送，而是把方言翻成前缀）。
            # 统一词表对得上的是下面 15 个；30 种里的缅甸语、高棉语、老挝语等没有
            # 对应代码可填，正文照发也念。
            languages=(
                "zh",
                "en",
                "ja",
                "ko",
                "es",
                "fr",
                "de",
                "ru",
                "pt",
                "it",
                "ar",
                "nl",
                "tr",
                "id",
                "vi",
            ),
            # 参考音频与正文分开传（官方称 Isolated Reference & Continuation），但
            # 官方没有声明"一把嗓子读另一种语言"——所以跨语言这一格留空（不画）。
            # 方言有 9 种，走正文开头的括号描述（见 _DIALECT_PROMPTS）。
            dialects=(
                "粤语",
                "四川话",
                "吴语",
                "东北话",
                "河南话",
                "陕西话",
                "山东话",
                "天津话",
                "闽南话",
            ),
            language_note=(
                "官方 30 种语言按正文自动判断；方言写进正文开头的括号描述，"
                "正文尽量用方言写法。"
            ),
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # 三条路都通：只给正文（引擎自己挑声音）、给参考音频（克隆）、
            # 给一句描述（设计）。所以不写 requires，缺料由装配时的判断兜住。
            requires=(),
            formats=("wav",),
            reference_formats=("wav",),
            # 逐字稿是"最高保真"的加分项，不是门槛：缺了照样能克隆。
            needs_transcript=False,
            # 上限来自 AudioVAE 编码器的固定容量：240000 采样点 / 16000Hz = 15 秒，
            # 超了服务端直接回 "encoder sample capacity exceeded"。交给核心
            # （usecase.reference._fit_duration）按句读停顿截到上限以内，与 GPT-SoVITS
            # 同一条路。这一家服务端没有最短判据，下限取 0：短样例照收，不无故拒。
            reference_duration=(0.0, 15.0),
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """落点随请求变的那几格：静态映射表表达不了。"""
        return {
            "seed": ("seed", "固定种子；-1 表示由插件抽样（引擎省略时用定值 1234）。"),
            "instruction": (
                "input 前缀（括号内）",
                "写成 ``(描述)正文``；有风格/方言时自动退成可控克隆，逐字稿不发送。",
            ),
            "emotion": (
                "input 前缀（括号内）",
                "没有引导指令时，情绪写成一句风格描述；有引导指令时以指令为准。",
            ),
            "voice_design": (
                "input 前缀（括号内）",
                "设计描述写进前缀；使用设计描述时不发送参考音频。",
            ),
            "language": (
                "input 前缀（括号内）",
                "中文方言翻成一句方言描述；其它语言按正文自动判断，不单独发送。",
            ),
        }

    def mark_map(self) -> dict[str, str]:
        """正文内嵌的发声事件这一家不支持——括号前缀是它的语法，别混用。"""
        return {}

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        """音色设计：不上传音频，也不换回平台音色 id——描述本身就是这条音色。"""
        if not req.design_prompt.strip():
            raise EngineError("VoxCPM2 音色设计需要音色描述。")
        raw = req.voice_id.strip()
        voice_id = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", raw).strip("_") or f"design-{uuid.uuid4().hex}"
        return VoiceMeta(
            id=voice_id,
            name=(req.name or voice_id).strip(),
            description=req.design_prompt.strip(),
            category="设计音色",
            tags=("设计",),
        )

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: VoxCpm2Settings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: VoxCpm2Settings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model

        text = str(values.get("text") or "")
        notes: list[str] = []

        # ---- 括号前缀：设计描述 > 引导指令 > 情绪；方言是另一维，总拼在最前 ----
        design = str(values.get("voice_design") or "").strip()
        # 引导指令的旧写法（instruct）已由网关并成 ``instruction``。
        explicit = str(values.get("instruction") or "").strip()
        mood = (
            ""
            if design or explicit
            else emotion_style(str(values.get("emotion") or ""), text)
        )
        parts = [part for part in (design or explicit, mood) if part]

        raw_language = str(values.get("language") or "").strip()
        language = _LANGUAGE_BY_LABEL.get(raw_language, raw_language)
        dialect = _DIALECT_PROMPTS.get(language, "")
        if dialect:
            if _english_text(text):
                notes.append(f"未发送：正文语言（VoxCPM2 的{dialect}只对中文正文生效）。")
            else:
                parts.insert(0, dialect)
        elif language in _DIALECT_CODES:
            label = _LANGUAGE_LABEL.get(language, raw_language)
            notes.append(f"未发送：正文语言（VoxCPM2 不支持{label}）。")
        style = "，".join(parts)

        # ---- 设计音色不吃参考音频：两样都给时以描述为准，说出来 ----
        if design and body.get("voice_ref"):
            body.pop("voice_ref", None)
            body.pop("reference_text", None)
            notes.append("按音色描述生成，参考音频未发送。")

        # ---- 参考音频：本机绝对路径，给了就必须读得到 ----
        reference = str(body.get("voice_ref") or "").strip()
        if reference:
            source = Path(reference)
            if not source.is_file():
                raise EngineError(
                    f"VoxCPM2 读不到参考音频：{reference}（文件不存在或已被移动）。"
                )
            body["voice_ref"] = str(source)

        # ---- 生成参数：上游没给才补配置里的 ----
        options: dict[str, Any] = body.setdefault("options", {})
        options.setdefault("guidance_scale", float(cfg.guidance_scale))
        options["num_inference_steps"] = int(cfg.num_inference_steps)
        if int(cfg.text_chunk_size or 0) > 0:
            options["text_chunk_size"] = int(cfg.text_chunk_size)
        # 参考音频 + 逐字稿同时在场 = 终极克隆（保真度最高）；缺一个就按普通克隆走。
        # **有风格/方言时不走高保真克隆**：官方文档写明 Hi-Fi 模式会忽略控制指令，
        # 把方言写在逐字稿句首也不会生效。此时退成可控克隆（参考音频 + 正文前缀），
        # 逐字稿不发送，避免它被当成续写提示或把前缀带进正文。
        transcript = str(body.get("reference_text") or "").strip()
        ultimate = bool(body.get("voice_ref")) and bool(transcript)
        if ultimate and style:
            body.pop("reference_text", None)
            ultimate = False
            notes.append(
                "风格/方言与高保真克隆二选一：本次按可控克隆发送，逐字稿未发送。"
            )
        if ultimate:
            options["ultimate_clone"] = True

        body["input"] = f"({style}){text}" if style else text
        if style:
            CHANNEL.info(
                f"风格描述写进正文前缀：({style})",
                source="engine",
                engine="voxcpm2",
            )

        # ---- 种子：-1 = 随机（引擎的默认是定值 1234，必须由插件抽） ----
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
        assert isinstance(cfg, VoxCpm2Settings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
