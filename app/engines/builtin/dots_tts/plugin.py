"""DotTTS 引擎插件——本机 audio.cpp 里的多语言零样本 TTS（SOAR / MeanFlow / Edit）。

**参数怎么落**（audio.cpp 的 ``/v1/audio/speech``，与 BreezeTTS 2 / IndexTTS2
同一层，但这一家的选项更碎）：

- **正文 → ``input``，参考音频 → ``voice_ref``**（收**本机绝对路径**，与
  IndexTTS2 一致；中文路径可用）。参考文本 ``reference_text`` 同时发——
  两者都在**顶层**（服务器文档：请求级选项才进 ``options``）。
- **正文语言 → ``options.language``**：统一那一格空着就是不指定（引擎默认
  ``none``，即不贴语言标签）。
- **引导指令 → ``options.instruct``**：这一家的 ``instruct`` 是**结构化指令**，
  只有 ``template_name=instruction_tts`` / ``edit`` 两个模板消费它。没显式指定
  模板时，插件见指令就把模板切到 ``instruction_tts``；显式选了不消费它的模板，
  就照发并说清（不静默）。
- **情绪 → 引导指令**：没有情绪参数，但情绪会翻成一句引导语（与 BreezeTTS 2
  共用 :mod:`app.domain.emotion_instruction` 那张表），再按上面的规则落进
  ``options.instruct``。有明确指令时指令优先。
- **种子**：``-1`` = 随机，**插件抽一个** 0 ~ 2³²−1 发出去并报回来；
  ``0`` 及以上 = 固定。引擎不收负数，且**省略 seed 时引擎用定值 42**——
  所以这一家的"随机"和 BreezeTTS 2 一样必须由插件抽，不能靠不传。
  配置默认就是 ``-1``（随机）：要复现同一段音频，再去配置里写固定值。
- **引擎特有参数**（``template_name`` / ``speaker_scale`` / ``sampler_mode`` /
  推理步数 / 分块 / ``max_tokens`` / ``use_xvector`` / ``vocoder_merge_steps``
  / Edit 的 ``source_audio``）走 ``extra_schema``：前端照它渲染，随 ``extra``
  进来，由映射表落进 ``options``。

没接的项（指令强度 / 语速 / 音量 / 音高 / 前后静默）在结果说明里逐条说清
"这次没发出去"，不静默丢。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.domain.emotion_instruction import emotion_instruction
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
from app.engines.builtin.dots_tts import client
from app.engines.builtin.dots_tts.settings import DotsTtsSettings
from app.engines.mapping import EngineMapping, ParamMapping, apply_mapping
from app.engines.seed import resolve_seed as _resolve_seed
from app.infra.loghub import CHANNEL

__all__ = ["DotsTtsEngine", "resolve_seed"]

#: 统一参数 → DotTTS 请求体。落点名字来自 audio.cpp 的 ``model_specs/dots_tts.json``。
#:
#: **模型参数必须放进 ``options``**（与 IndexTTS2 同一条实测结论：摆在顶层时
#: 服务端 200 但静默忽略）。'``seed`` 是例外，它在顶层——audio.cpp 的网页
#: 界面就是把 input / voice_ref / reference_text / seed 四样摆在外面。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", required=True, note="要合成的正文。"),
        # 本引擎的主体：参考音频（零样本克隆的音色来源）。**不给也能走**
        # （引擎自己挑一个音色），所以不写 required——有声书场景音色必带。
        ParamMapping(
            "reference_audio", "voice_ref", note="参考音频；本机绝对路径，中文路径可用。"
        ),
        ParamMapping("reference_text", "reference_text", note="参考音频的逐字稿。"),
        # 正文语言：统一那一格空着 = 不指定（skip_empty 默认 True，整个不发）
        ParamMapping("language", "options.language", note="正文语言；留空不指定。"),
        ParamMapping(
            "instruction",
            "options.instruct",
            note="结构化指令；仅 instruction_tts / edit 模板消费。",
            warning=True,
        ),
        # ---- 引擎特有参数（前端照 extra_schema 渲染，随 extra 进来）----
        ParamMapping("template_name", "options.template_name", note="生成模板。"),
        ParamMapping(
            "speaker_scale", "options.speaker_scale", transform="number", note="音色缩放。"
        ),
        ParamMapping("sampler_mode", "options.sampler_mode", note="采样器。"),
        ParamMapping(
            "num_inference_steps",
            "options.num_inference_steps",
            transform="int",
            note="推理步数。",
        ),
        ParamMapping("max_tokens", "options.max_tokens", transform="int", note="帧数上限。"),
        ParamMapping(
            "text_chunk_size", "options.text_chunk_size", transform="int", note="分块长度。"
        ),
        ParamMapping("text_chunk_mode", "options.text_chunk_mode", note="分块方式。"),
        ParamMapping(
            "vocoder_merge_steps",
            "options.vocoder_merge_steps",
            transform="int",
            note="流式合并步数。",
        ),
        ParamMapping("use_xvector", "options.use_xvector", note="说话人引导。"),
        # Edit 模板的源音频（服务端机器上的 WAV 路径）
        ParamMapping("source_audio", "options.source_audio", note="Edit 模板的源音频路径。"),
    ),
    constants={},
    cautions=(
        "引导指令仅在 template_name 为 instruction_tts 或 edit 时生效；其他模板会照发但不消费。",
        "种子 -1 表示随机，由核心抽样后发送；0 及以上为固定种子。",
        "本机模型参数必须位于 options 内；写入顶层会被服务端静默忽略。",
    ),
)

#: 上游给了、这一家没有落点的项 → 原因（面板映射表的「说明」列）。
_UNSUPPORTED: dict[str, str] = {
    "guidance_scale": "本引擎的 CFG 刻度不同，未接入",
    "speed": "本引擎无对应参数",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
    "reference_language": "本引擎无对应参数",
}

#: 只在面板映射表里出现的说明（不随请求报：都是这台引擎的恒定量）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频代表音色，没有音色 ID。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出参数固定，不可指定。",
    "channels": "输出参数固定，不可指定。",
}

#: 消费 ``instruct`` 的两个模板（其余模板发了也不起作用）。
_INSTRUCT_TEMPLATES = ("instruction_tts", "edit")


def resolve_seed(value: Any) -> tuple[int, str]:
    """定这一次真正发出去的种子（负数 = 随机，抽到的数报回来）。"""
    seed, note = _resolve_seed(value, engine="dots_tts")
    if seed is None:
        raise EngineError("种子缺失。")
    return seed, note


def _provided(value: Any) -> bool:
    return not (value is None or (isinstance(value, str) and not value.strip()))


class DotsTtsEngine(EnginePlugin):
    """DotTTS（audio.cpp，OpenAI 兼容端点）。"""

    key = "dots_tts"
    label = "DotTTS"
    version = "1.0.0"
    homepage = "https://github.com/0xShug0/audio.cpp"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return DotsTtsSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            voice_listing=False,
            voice_cloning=False,
            reference_audio=True,
            # 指令只在 instruction_tts / edit 模板生效，但确实是原生能力。
            instruction_control=True,
            # 没有情绪枚举——情绪由插件翻成一句 instruct；不是原生情绪参数。
            emotion_control=False,
            # 长文本由服务端自己分块（``text_chunk_size`` 默认 320 字符、``tag_aware``
            # 保留段首的风格/语言标签），所以没有"发不出去"的上限；0 = 不拦。
            max_chars=0,
            max_concurrency=1,
            # 语言：官方 README 没有"支持 N 种语言"的清单，覆盖是从 24 语言的
            # MiniMax 多语言评测表来的（中文、粤语、英语、日语、韩语……全在表里），
            # 统一词表刚好都能填。跨语种克隆有 en→zh / zh→en 的实测 SIM（CV3-Eval）。
            languages=(
                "zh",
                "yue",
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
                "uk",
                "id",
                "vi",
            ),
            cross_lingual=True,
            dialects=("粤语",),
            language_note="官方以 24 语言评测集给覆盖；language 是强制标签，留空则不指定",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # 不给参考音频也能合成（引擎自己挑音色）；有声书场景音色必带。
            requires=(),
            formats=("wav",),
            reference_formats=("wav",),
            # 要逐字稿，但只在给了参考音频时——中转站照它补逐字稿并写回样例。
            needs_transcript=True,
            # 引擎特有参数：前端照这个 Schema 渲染，中转站照它校验。
            extra_schema={
                "type": "object",
                "properties": {
                    "template_name": {
                        "type": "string",
                        "title": "模板",
                        "enum": ["tts", "instruction_tts", "text_to_audio", "tts_interleave", "edit"],
                        "default": "tts",
                    },
                    "num_inference_steps": {
                        "type": "integer",
                        "title": "推理步数",
                        "default": 10,
                        "minimum": 1,
                    },
                    "speaker_scale": {
                        "type": "number",
                        "title": "音色缩放",
                        "default": 1.5,
                        "minimum": 0,
                    },
                    "sampler_mode": {
                        "type": "string",
                        "title": "采样器",
                        "enum": ["euler", "midpoint", "rk4"],
                        "default": "euler",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "title": "帧数上限",
                        "default": 500,
                        "minimum": 1,
                    },
                    "text_chunk_size": {
                        "type": "integer",
                        "title": "分块字数",
                        "default": 320,
                        "minimum": 1,
                    },
                    "text_chunk_mode": {
                        "type": "string",
                        "title": "分块方式",
                        "enum": ["default", "tag_aware", "japanese", "endline"],
                        "default": "tag_aware",
                    },
                    "vocoder_merge_steps": {
                        "type": "integer",
                        "title": "流式合并步数",
                        "default": 4,
                        "minimum": 1,
                    },
                    "use_xvector": {
                        "type": "string",
                        "title": "说话人引导",
                        "enum": ["auto", "on", "off"],
                        "default": "auto",
                    },
                    "source_audio": {
                        "type": "string",
                        "title": "编辑源音频",
                        "description": "服务端机器上的 WAV 路径（template_name=edit 时必填）",
                    },
                },
            },
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """面板映射表里的「无落点」原因；``_UNSUPPORTED`` 另管随请求报的那几句。"""
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """种子的落点在装配时决定（负数在插件里抽样），静态映射表表达不了。"""
        return {
            "seed": ("seed", "固定种子；-1 表示由核心抽样。"),
            "emotion": (
                "options.instruct",
                "翻成一句引导语；有明确指令时以指令为准。",
            ),
        }

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: DotsTtsSettings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: DotsTtsSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model
        body["input"] = str(values.get("text") or "")

        options: dict[str, Any] = body.setdefault("options", {})

        # ---- 指令与情绪：明确指令优先；没有指令才把情绪翻成一句 ----
        explicit = str(options.get("instruct") or "").strip()
        emotion = str(values.get("emotion") or "").strip()
        instruct = explicit
        if not instruct and emotion:
            instruct = emotion_instruction(emotion, str(values.get("text") or ""))
            if instruct:
                CHANNEL.info(
                    f"情绪「{emotion}」按引导指令发送：{instruct}",
                    source="engine",
                    engine="dots_tts",
                )
        if instruct:
            options["instruct"] = instruct

        # ---- 模板：显式指定优先；否则有指令就用 instruction_tts ----
        # 默认 ``tts`` 模板不消费 instruct；不自动切的话，指令发了也等于没发。
        template = str(options.get("template_name") or "").strip()
        if not template and instruct:
            options["template_name"] = "instruction_tts"
            template = "instruction_tts"
        template = template or "tts"

        # ---- 参考音频：这一家收本机绝对路径（给了就必须读得到） ----
        reference = str(body.get("voice_ref") or "").strip()
        if reference:
            source = Path(reference)
            if not source.is_file():
                raise EngineError(
                    f"DotTTS 读不到参考音频：{reference}（文件不存在或已被移动）。"
                )
            body["voice_ref"] = str(source)

        # ---- 种子：上游没提（或给空）就用配置里那一格（默认 -1 = 随机） ----
        raw_seed = values.get("seed")
        if not _provided(raw_seed):
            raw_seed = cfg.seed
        seed, seed_note = resolve_seed(raw_seed)
        body["seed"] = seed

        # ---- 没落点的那几项 + 引导指令的模板不匹配：给了就说，不静默丢 ----
        notes: list[str] = []
        if seed_note:
            notes.append(seed_note)
        if instruct and template not in _INSTRUCT_TEMPLATES:
            notes.append(
                "引导指令已随请求发出，但当前模板不消费它；"
                "要生效请把「模板」换成 instruction_tts 或 edit。"
            )
        ignored = [
            param_label(key) for key in _UNSUPPORTED if _provided(values.get(key))
        ]
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        if not options:
            body.pop("options", None)
        return body, tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        assert isinstance(cfg, DotsTtsSettings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
