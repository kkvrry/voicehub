"""Qwen3-TTS 引擎插件——本机 audio.cpp 里的 12 Hz 克隆 TTS（Base 权重）。

**这一家只有一条路：克隆。** Base 权重没有内置音色、没有音色设计、没有指令与情绪
字段，说话方式全部来自参考音频——所以请求里必须同时有 ``voice_ref``（本机 WAV 路径）
与 ``reference_text``（那段音频的逐字稿）。给不出逐字稿时服务端会退回说话人向量模式
（``x_vector_only_mode``），音色明显更粗；本插件不替用户做这个降级，缺逐字稿直接报。

**语言是一格提示，不是门禁**：官方 10 种语言，``options.language`` 收代码（``zh`` /
``en`` …）。不在这 10 种里的语种由网关按声明剔掉、按不指定发送；正文照发，模型按
正文自己判断。

**采样与分块都在 ``options``**：``temperature`` / ``top_k`` / ``top_p`` /
``repetition_penalty`` / ``max_tokens`` / ``text_chunk_size``。这些是 audio.cpp 的
TTS 通用选项，落点与 CosyVoice3 / BreezeTTS 2 / DotTTS 同一层。

**种子省略时引擎随机**（`--seed` 的默认值是 random，不是定值）。插件仍按统一约定
抽一个发出去，把抽到的数报回结果说明——同样的正文与参数可以复现，排查"这次怎么念
得不一样"时才有抓手。

没有的能力如实不声明：没有内置音色清单、不注册音色 id、没有音色设计、没有情绪枚举、
没有语速 / 音量 / 音高 / 静音。给了这些值会在结果说明里逐条报"未发送"，不静默丢。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from pydantic import BaseModel

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
from app.engines.builtin.qwen3_tts import client
from app.engines.builtin.qwen3_tts.settings import Qwen3TtsSettings
from app.engines.mapping import EngineMapping, ParamMapping, apply_mapping
from app.infra.loghub import CHANNEL

__all__ = ["MAPPING", "RANDOM_SEED", "SUPPORTED_LANGUAGES", "Qwen3TtsEngine", "resolve_seed"]

#: 种子写成负数（约定 ``-1``）= 随机：由插件抽一个再发出去。
RANDOM_SEED = -1

#: 种子上限（audio.cpp 的 seed 是 uint32）。
SEED_CEILING = 2**32 - 1

#: 官方声明的 10 种语言。与 ``LANGUAGE_CHOICES`` 同代码，界面说法在那里。
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "ru",
    "pt",
    "es",
    "it",
)

#: 界面给的可能是中文说法：先归一成代码。
_LANGUAGE_BY_LABEL: dict[str, str] = {label: code for code, label in LANGUAGE_CHOICES}
_LABEL_BY_LANGUAGE: dict[str, str] = {code: label for code, label in LANGUAGE_CHOICES}

#: 统一参数 → Qwen3-TTS 请求体。落点名字取自 ``docs/models/qwen3_tts.md`` 与
#: audio.cpp 的 TTS 通用选项。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", required=True, note="要合成的正文。"),
        ParamMapping(
            "reference_audio",
            "voice_ref",
            note="参考音频；本机绝对路径，WAV。",
            warning=True,
        ),
        ParamMapping(
            "reference_text",
            "reference_text",
            note="参考音频的逐字稿；Base 按 in-context 模式使用，缺了音色更粗。",
            warning=True,
        ),
        ParamMapping(
            "temperature", "options.temperature", transform="number", note="采样温度。"
        ),
        ParamMapping("top_k", "options.top_k", transform="int", note="采样候选数。"),
        ParamMapping("top_p", "options.top_p", transform="number", note="累积概率截断。"),
        ParamMapping(
            "repetition_penalty",
            "options.repetition_penalty",
            transform="number",
            note="重复惩罚。",
        ),
        ParamMapping(
            "max_tokens", "options.max_tokens", transform="int", note="每块生成上限。"
        ),
        ParamMapping(
            "text_chunk_size",
            "options.text_chunk_size",
            transform="int",
            note="长文本分块字数。",
        ),
    ),
    cautions=(
        "Base 权重必须同时给参考音频与逐字稿；只有参考音频时音色会明显变粗。",
        "种子省略时引擎随机；插件仍抽一个发出去，抽到的数随结果返回。",
        "语言只发官方 10 种代码；其它语种由正文自行判断。",
    ),
)

#: 上游给了、这一家没有落点、且值得随请求报出来的项 → 原因。
_UNSUPPORTED: dict[str, str] = {
    "reference_language": "本引擎无对应参数",
    "guidance_scale": "本引擎无引导强度参数",
    "speed": "本引擎无对应参数",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
    "instruction": "Base 权重不接收引导指令",
    "emotion": "本引擎无情绪参数",
}

#: 只在面板映射表里出现的说明（都是这台引擎的恒定量）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频代表音色，没有音色 ID。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出固定为 WAV，无码率参数。",
    "channels": "输出参数固定，不可指定。",
}


def resolve_seed(value: Any) -> tuple[int, str]:
    """定这一次真正发出去的种子——返回 ``（值, 对用户说的一句话）``。

    负数（约定写作 ``-1``）= 随机：抽一个、发出去、把抽到的数报回来。这一家的
    规格里种子省略就是随机，抽出来只是为了让这一次能复现；不抽也能跑。
    """
    try:
        seed = int(value)
    except (TypeError, ValueError):
        raise EngineError(f"种子「{value}」不是整数。") from None
    if seed >= 0:
        return min(seed, SEED_CEILING), f"seed={seed}"
    drawn = random.randrange(0, SEED_CEILING + 1)
    CHANNEL.info(
        f"种子写的是 {seed}（= 随机），抽取结果 {drawn}（可用它复现）",
        source="engine",
        engine="qwen3_tts",
    )
    return drawn, f"seed={drawn}"


def _provided(value: Any) -> bool:
    return not (value is None or (isinstance(value, str) and not value.strip()))


class Qwen3TtsEngine(EnginePlugin):
    """Qwen3-TTS（audio.cpp，Base 克隆权重）。"""

    key = "qwen3_tts"
    label = "Qwen3-TTS"
    version = "0.1.0"
    homepage = "https://github.com/QwenLM/Qwen3-TTS"

    # ------------------------------------------------------------------ #
    # 配置与能力
    # ------------------------------------------------------------------ #

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return Qwen3TtsSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 本机服务不提供音色清单，也不登记音色 id：音色 = 一段参考音频。
            voice_listing=False,
            voice_cloning=False,
            voice_design=False,
            reference_audio=True,
            instruction_control=False,
            emotion_control=False,
            # 服务端自己按 text_chunk_size 分块，长文本不会失败，不设硬上限。
            max_chars=0,
            # 本机单模型服务：并发只会让第二条排队。
            max_concurrency=1,
            languages=SUPPORTED_LANGUAGES,
            language_note=(
                "官方 10 种语言，按正文与 language 提示决定；清单外的语种不发送。"
            ),
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # Base 权重只走克隆：没有参考音频就没有音色。
            requires=("reference_audio",),
            formats=("wav",),
            reference_formats=("wav",),
            # 逐字稿是 in-context 克隆的前提：缺了音色会明显变粗，所以不降级。
            needs_transcript=True,
            # 参考音频取 3–30 秒是这一家的常见区间，过长会挤占生成预算；
            # 下限不设，短样例照收。核心按这个区间截取并重听逐字稿。
            reference_duration=(0.0, 30.0),
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """落点随请求变的那几格：静态映射表表达不了。"""
        return {
            "seed": ("seed", "固定种子；-1 由插件抽样，抽到的数随结果返回。"),
            "language": (
                "options.language",
                "只发官方 10 种代码；中文说法先归一，其它语种剔除并说明。",
            ),
        }

    def mark_map(self) -> dict[str, str]:
        """这一家没有正文内嵌标签的语法。"""
        return {}

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: Qwen3TtsSettings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: Qwen3TtsSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model
        notes: list[str] = []

        # ---- 参考音频：本机绝对路径，给了就必须读得到 ----
        reference = str(body.get("voice_ref") or "").strip()
        if not reference:
            raise EngineError(
                "Qwen3-TTS 只能克隆：请先选一条带本地样例的音色（参考音频是必填）。"
            )
        source = Path(reference)
        if not source.is_file():
            raise EngineError(
                f"Qwen3-TTS 读不到参考音频：{reference}（文件不存在或已被移动）。"
            )
        body["voice_ref"] = str(source)

        # ---- 逐字稿：in-context 克隆的前提，不给就直接报，不替用户降级 ----
        transcript = str(body.get("reference_text") or "").strip()
        if not transcript:
            raise EngineError(
                "Qwen3-TTS 需要参考音频的逐字稿：请给这条音色的样例补上说话内容。"
            )
        body["reference_text"] = transcript

        # ---- 采样与分块：上游没给才补配置里的 ----
        options: dict[str, Any] = body.setdefault("options", {})
        options.setdefault("temperature", float(cfg.temperature))
        options.setdefault("top_k", int(cfg.top_k))
        options.setdefault("top_p", float(cfg.top_p))
        options.setdefault("repetition_penalty", float(cfg.repetition_penalty))
        if int(cfg.max_tokens or 0) > 0:
            options["max_tokens"] = int(cfg.max_tokens)
        if int(cfg.text_chunk_size or 0) > 0:
            options["text_chunk_size"] = int(cfg.text_chunk_size)

        # ---- 语言：代码原样发；不在这 10 种里的剔除并说明 ----
        raw_language = str(values.get("language") or "").strip()
        if raw_language:
            code = _LANGUAGE_BY_LABEL.get(raw_language, raw_language)
            if code in SUPPORTED_LANGUAGES:
                options["language"] = code
            else:
                label = _LABEL_BY_LANGUAGE.get(code, raw_language)
                notes.append(f"未发送：正文语言（Qwen3-TTS 不支持{label}）。")

        # ---- 种子：-1 = 随机，仍抽一个发出去，抽到的数报回来 ----
        raw_seed = values.get("seed")
        seed, seed_note = resolve_seed(raw_seed if _provided(raw_seed) else cfg.seed)
        body["seed"] = seed
        notes.append(seed_note)

        ignored = [param_label(key) for key in _UNSUPPORTED if _provided(values.get(key))]
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        return body, tuple(notes)

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        assert isinstance(cfg, Qwen3TtsSettings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
