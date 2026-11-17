"""OmniVoice 引擎插件——本机 audio.cpp 里的多语言零样本 TTS（克隆 + 自动音色）。

**它的路靠"这次给了什么"分开**（``clon`` 只是同一个实现的别名）：

- 只给正文 → 引擎自己挑一把声音；
- 给参考音频（可带逐字稿）→ 克隆。

**为什么不接 ``options.instruct``**：那一格是**封闭的属性词表**（性别/年龄/音调/
耳语/口音/方言，逗号分隔，同类互斥），既不是自由"引导指令"，也收不下自由"音色描述"
（``年轻女声，清亮`` 这种写法服务端直接 500）。按本项目"先对齐参数语义、接不进来就
舍弃"的原则，这台引擎**不对接 instruct**：引导指令与音色描述都如实报"未发送"，
音色只由参考音频决定。

**其余落点**（audio.cpp 的 ``/v1/audio/speech``）：

- 正文 → ``input``；参考音频 → ``voice_ref``（本机绝对路径）；逐字稿 →
  ``reference_text``；正文语言 → ``options.language``。
  **语言只是给正文的提示**：模型不会把普通话正文翻译成粤语；有参考音频时，
  参考样例的口音还会主导输出。粤语要同时用粤语正文与粤语参考样例。
- 语速 → ``options.speed``（1.0 为原速）；解码 → ``options.num_inference_steps``
  （默认 32）与 ``options.guidance_scale``（默认 2.0）。
- **种子 → 顶层 ``seed``**：省略时沿用会话的随机状态，即"不传就是随机"，所以
  ``-1`` 就不发这一格；给了具体值才发。

**语气词**：这一家的非语言标签直接写在正文里（``[laughter]``、``[sigh]``），所以
本插件登记 :meth:`mark_map`；``[[笑]]`` / ``[[叹气]]`` 由网关在发出前按
:meth:`~app.engines.base.EnginePlugin.prepare_text` 翻成它的写法，表外的标签删掉。

没有的能力如实不声明：没有内置音色、不注册音色 id、没有情绪枚举、没有音量/音高。
"""

from __future__ import annotations

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
from app.engines.builtin.omnivoice import client
from app.engines.builtin.omnivoice.settings import OmniVoiceSettings
from app.engines.mapping import EngineMapping, ParamMapping, apply_mapping
from app.infra.loghub import CHANNEL

__all__ = ["MAPPING", "MARKS", "OmniVoiceEngine"]

#: 统一参数 → OmniVoice 请求体。落点名字取自 ``model_specs/omnivoice.json``。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", required=True, note="要合成的正文。"),
        ParamMapping(
            "reference_audio", "voice_ref", note="参考音频；本机绝对路径，WAV。"
        ),
        ParamMapping("reference_text", "reference_text", note="参考音频的逐字稿。"),
        ParamMapping("language", "options.language", note="正文语言；留空由引擎自动判断。"),
        ParamMapping("speed", "options.speed", note="语速倍率，1.0 为原速。"),
    ),
    cautions=(
        "模板由输入决定：给参考音频是克隆、都不给由引擎挑声音；这一家不对接 instruct。",
        "种子省略时沿用会话的随机状态；要复现就固定一个值。",
    ),
)

#: 上游给了、这一家没有落点的项 → 原因。
_UNSUPPORTED: dict[str, str] = {
    "instruction": "本引擎没有引导指令参数",
    "voice_design": "音色设计只认固定属性词表，自由描述接不进来",
    "emotion": "语气由参考音频决定，这一家没有情绪参数",
    "guidance_scale": "本引擎无指令强度参数（CFG 在设置里配）",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
    "reference_language": "本引擎无对应参数",
}

#: 只在面板映射表里出现的说明（都是这台引擎的恒定量）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频代表音色，没有内置音色 id。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出固定为 WAV，无码率参数。",
    "channels": "输出参数固定，不可指定。",
}

#: 语气词：统一写法 → 这一家的方括号标签。官方列出的标签里只有这两个与本项目的
#: 词表同义；``[question-*]`` / ``[surprise-*]`` 那一类不属本项目的标签集，不登记。
MARKS: dict[str, str] = {
    "笑": "[laughter]",
    "叹气": "[sigh]",
}

#: 界面说法 → 代码：语言那一格可能收到 ``yue`` 或 ``粤语``，两者都认。
_LANGUAGE_BY_LABEL: dict[str, str] = {label: code for code, label in LANGUAGE_CHOICES}


def _provided(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


class OmniVoiceEngine(EnginePlugin):
    """OmniVoice（audio.cpp，OpenAI 兼容端点）。"""

    key = "omnivoice"
    label = "OmniVoice"
    version = "1.0.0"
    homepage = "https://github.com/k2-fsa/OmniVoice"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return OmniVoiceSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 没有内置音色，也没有清单端点。
            voice_listing=False,
            # 零样本：参考音频本身就是音色，没有"注册一个音色 id"这一步。
            voice_cloning=False,
            # 它的 instruct 是封闭属性词表，收不下本项目的自由音色描述——不对接，
            # 于是这台引擎没有"描述设计音色"这条路，音色只由参考音频决定。
            voice_design=False,
            reference_audio=True,
            # 没有情绪枚举，也不收自由引导指令。
            emotion_control=False,
            # 长文本会分段：框架分块默认关，但模型侧按估计音频时长自动切
            # （``audio_chunk_threshold_sec`` 30s 触发、每块 15s），所以没有硬上限。
            max_chars=0,
            # 本机单模型服务：并发只会让第二条排队。
            max_concurrency=1,
            # 语言：官方称 600+ 种；这里按统一词表列出可填的代码。
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
            language_note=(
                "官方 600+ 种；语言只是正文提示，不会翻译正文。"
                "参考音频的口音会主导输出，粤语请用粤语正文和粤语参考样例。"
            ),
            # 粤语按本项目的口径算方言（与 CosyVoice3 同一处理），语言行不再重复它。
            dialects=("粤语",),
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # 两条路都通：只给正文（引擎挑声音）、给参考音频（克隆）。缺料由装配兜住。
            requires=(),
            formats=("wav",),
            reference_formats=("wav",),
            # 逐字稿是加分项，不是门槛。
            needs_transcript=False,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """落点随请求变的那一格：静态映射表表达不了。"""
        return {
            "seed": ("seed", "固定种子；-1 不发这一格，沿用会话的随机状态。"),
        }

    def mark_map(self) -> dict[str, str]:
        return MARKS

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: OmniVoiceSettings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: OmniVoiceSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model
        body["input"] = str(values.get("text") or "")

        notes: list[str] = []
        options: dict[str, Any] = body.setdefault("options", {})

        # ---- 参考音频：本机绝对路径，给了就必须读得到 ----
        # 这一家不对接 instruct：音色只由参考音频决定，引导指令/音色描述在下方报"未发送"。
        reference = str(body.get("voice_ref") or "").strip()
        if reference:
            source = Path(reference)
            if not source.is_file():
                raise EngineError(
                    f"OmniVoice 读不到参考音频：{reference}（文件不存在或已被移动）。"
                )
            body["voice_ref"] = str(source)

        # 语言：界面说法归一成代码；这一格只是正文提示，不翻译、也不改参考口音。
        raw_language = str(values.get("language") or "").strip()
        language = _LANGUAGE_BY_LABEL.get(raw_language, raw_language)
        if language:
            options["language"] = language
        else:
            options.pop("language", None)
        if reference and language == "yue":
            notes.append(
                "粤语要正文与参考样例都用粤语；模型不会把普通话正文翻译成粤语，"
                "参考音频也会主导口音。"
            )

        # ---- 解码参数：设置里的默认值随请求落进 options ----
        options.setdefault("guidance_scale", float(cfg.guidance_scale))
        options["num_inference_steps"] = int(cfg.num_inference_steps)
        if int(cfg.text_chunk_size or 0) > 0:
            options["text_chunk_size"] = int(cfg.text_chunk_size)

        # ---- 种子：省略 = 沿用会话随机状态，所以 -1 就不发这一格 ----
        raw_seed = values.get("seed")
        seed = raw_seed if _provided(raw_seed) else cfg.seed
        try:
            value = int(seed)
        except (TypeError, ValueError):
            raise EngineError(f"种子「{seed}」不是整数。") from None
        if value >= 0:
            body["seed"] = value
            notes.append(f"seed={value}")
        else:
            body.pop("seed", None)
            CHANNEL.info(
                "种子写的是随机：不发 seed，沿用会话的随机状态",
                source="engine",
                engine="omnivoice",
            )

        # voice_design 不是规范参数，param_label 认不得；报"未发送"时给个界面名。
        ignored = [
            "音色描述" if key == "voice_design" else param_label(key)
            for key in _UNSUPPORTED
            if _provided(values.get(key))
        ]
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        return body, tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        assert isinstance(cfg, OmniVoiceSettings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
