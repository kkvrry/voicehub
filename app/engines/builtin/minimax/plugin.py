"""MiniMax 引擎插件——云端语音合成服务（要密钥、要联网、按量计费）。

契约来自官方文档：合成 ``POST /v1/t2a_v2``（上限 10,000 字符）；音色清单
``POST /v1/get_voice``（POST + body）；克隆经 ``/v1/files/upload`` → ``/v1/voice_clone``，
克隆音色 7 天内不用会被删除；情绪是枚举（见 ``mapping.EMOTION_ENUM``）。

不做的事：不内置音色清单（拉不到就说为什么）、不猜错误原因（只报平台原话）、
不替上游决定参数转换（转换器在 :mod:`app.engines.mapping`）。
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.engines.base import (
    KIND_CLOUD,
    Capabilities,
    CloneRequest,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
    VoiceList,
    VoiceMeta,
)
from app.engines.builtin.minimax import client
from app.engines.builtin.minimax.mapping import (
    AUDIO_FORMATS,
    EMOTION_MAP,
    INTERJECTION_MODELS,
    INTERJECTIONS,
    MAPPING,
    MARK_MAP,
    NUMERIC_RANGES,
    emotion_choices,
)
from app.engines.builtin.minimax.settings import MinimaxSettings
from app.engines.mapping import EngineMapping, apply_mapping

__all__ = ["MinimaxEngine", "normalize_voice_id"]

#: ``voice_id`` 的规则（官方）：8–256 字符、字母开头、只含字母/数字/``-``/``_``、
#: 不能以 ``-`` 或 ``_`` 结尾。不满足会被平台拒——**在这里先修好**，
#: 而不是让用户拿到一句"voice_id 不合法"再自己猜规则。
_VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,255}$")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def normalize_voice_id(raw: str, name: str = "") -> str:
    """把用户给的名字收拾成平台一定接受的 ``voice_id``（官方规则见上）。

    给了合法值原样用；不合法值报出来（不静默改，否则用户在平台后台找不到）；
    没给则由音色名生成——生成必须带随机后缀：中文名 ASCII 过滤后常剩不下字符
    （「胡桃」「钟离」会撞同一个 id），且同名克隆第二次是常事。
    """
    candidate = (raw or "").strip()
    if candidate:
        if not _VOICE_ID_RE.match(candidate):
            raise EngineError(
                f"「{candidate}」不能当 MiniMax 的音色 id：要求 8–256 个字符、"
                "以英文字母开头、只含字母/数字/-/_，且不能以 - 或 _ 结尾。"
                "留空则由音色名自动生成一个。"
            )
        return candidate

    stem = _ID_SAFE_RE.sub("", (name or "").strip())[:40].strip("-_") or "voice"
    if not stem[0].isalpha():  # 名字以数字或横线开头时补一个字母
        stem = f"v{stem}"
    candidate = f"vh_{stem}_{secrets.token_hex(3)}"
    return candidate if _VOICE_ID_RE.match(candidate) else f"vh_voice_{secrets.token_hex(3)}"


def _as_settings(cfg: BaseModel) -> MinimaxSettings:
    """把框架递来的配置收窄成本引擎模型；类型不对即实现错误，立即炸掉。"""
    assert isinstance(cfg, MinimaxSettings)
    return cfg


class MinimaxEngine(EnginePlugin):
    """MiniMax 语音合成（国内站 / 国际站，两站密钥不通用）。"""

    key = "minimax"
    label = "MiniMax"
    version = "1.0.0"
    homepage = "https://platform.minimaxi.com"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return MinimaxSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_CLOUD,
            voice_listing=True,
            voice_cloning=True,  # /v1/files/upload → /v1/voice_clone
            reference_audio=False,  # t2a_v2 不吃参考音频（克隆是另一套接口）
            emotion_control=True,
            max_chars=10_000,  # 官方：text 必须小于 10000 字符
            # 语言：平台枚举共 40 种，统一词表能填的是下面这 17 个。
            # ``language_boost`` 官方口径是"增强指定小语种和方言的识别"——提示而不是
            # 限定，所以音色（含复刻音色）可以念另一种语言的正文。方言只有粤语有自己
            # 的枚举值（``Chinese,Yue``），其余方言没有单独开关。
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
            language_note="平台枚举 40 种，统一词表只覆盖这 17 种；language_boost 是增强提示，不是语言开关",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            requires=("voice_id",),
            formats=AUDIO_FORMATS,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """规范里有、这一家没有落点的项：面板逐条写原因，不静默丢。"""
        return {
            "reference_audio": "合成接口不接受参考音频；克隆请使用「音色克隆」。",
            "reference_text": "合成接口不接受参考文本；克隆请使用「音色克隆」。",
            "reference_language": "只有正文语言增强，没有参考语言字段。",
            "guidance_scale": "本引擎无对应参数。",
            "lead_silence": "本引擎无对应参数；停顿只能写在正文中，且不能位于首尾。",
            "lag_silence": "本引擎无对应参数；停顿只能写在正文中，且不能位于首尾。",
            "seed": "本引擎无对应参数。",
        }

    def value_choices(self) -> dict[str, tuple[str, ...]]:
        """枚举参数的取值——目前只有 ``emotion``，发规范词 + 同义词的并集，
        供上游逐行比对打标（用户自由填的词也可能被平台认得）。"""
        return {"emotion": emotion_choices()}

    def mark_map(self) -> dict[str, str]:
        """语气词标签：:data:`~app.engines.builtin.minimax.mapping.MARK_MAP` 里那 21 条。"""
        return MARK_MAP

    def readiness(self, cfg: BaseModel) -> str:
        if not getattr(cfg, "configured", False):
            return "还没填 API Key。"
        return ""

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: MinimaxSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``t2a_v2`` 请求体（只要请求体的场合用这个）。
        被剔掉的那几项要用 :meth:`build_body_with_notes` 才拿得到。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: MinimaxSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """装配请求体，并带回被剔掉的那几项。

        先按 :data:`MAPPING` 落上游给的值，再补配置默认值（顺序不能反）。
        正文里的语气词标签已由网关换成平台写法（见 ``EnginePlugin.prepare_text``）；
        这里只剩一道校验：模型不认语气词时拦下，不让它被当成字念出来。
        """
        values = req.as_values()
        self._guard_interjections(str(values.get("text") or ""), cfg.model)
        dropped: list[str] = []
        body = apply_mapping(MAPPING, values, dropped)
        self._guard_ranges(body)

        body.setdefault("model", cfg.model)
        audio = body.get("audio_setting")
        if not isinstance(audio, dict):
            audio = {}
            body["audio_setting"] = audio
        audio.setdefault("sample_rate", cfg.sample_rate)
        audio.setdefault("bitrate", cfg.bitrate)
        audio.setdefault("channel", cfg.channel)
        audio.setdefault("format", req.format or cfg.audio_format)

        # 字幕是顶层开关（不在 ``audio_setting`` 里）；关着时连 ``subtitle_type``
        # 一起不带——省略才是"不要"的表示法。
        if cfg.subtitle_enable:
            body["subtitle_enable"] = True
            body.setdefault("subtitle_type", cfg.subtitle_type)

        if cfg.language_boost:
            body.setdefault("language_boost", cfg.language_boost)
        if cfg.text_normalization_mode:
            body.setdefault("text_normalization_mode", cfg.text_normalization_mode)
        if cfg.sound_effect:
            body.setdefault("voice_modify", {})["sound_effects"] = cfg.sound_effect
        return body, self._notes_for(dropped, values)

    @staticmethod
    def _notes_for(dropped: list[str], values: Mapping[str, Any]) -> tuple[str, ...]:
        """被剔掉的那几项 → 逐条说明（只剔不说是最坏的：声音照出、要求无声丢失）。"""
        notes = []
        for name in dict.fromkeys(dropped):  # 去重且保序
            raw = str(values.get(name) or "").strip()
            if name == "emotion":
                notes.append(
                    f"MiniMax 认不出情绪「{raw}」——这一句按无情绪合成"
                    f"（它认得：{'、'.join(emotion_choices()[: len(EMOTION_MAP)])} 等"
                    f" {len(emotion_choices())} 个说法）。"
                )
            else:
                notes.append(f"MiniMax 用不了「{name}」（收到 {raw or '空值'}），这一项已忽略。")
        return tuple(notes)

    @staticmethod
    def _guard_interjections(text: str, model: str) -> None:
        """语气词只有 2.8 那两个模型认，别的模型发出去会被当成字念出来。"""
        if model in INTERJECTION_MODELS:
            return
        # ``INTERJECTIONS`` 里存的就是**带括号的那一整串**（``(laughs)``），
        # 所以这里直接找它，不再自己给它加一层括号。
        hit = [name for name in INTERJECTIONS if name in text]
        if hit:
            raise EngineError(
                "语气词（"
                + "、".join(hit)
                + "）只有 "
                + " / ".join(INTERJECTION_MODELS)
                + " 认，当前配的是 "
                + model
                + "——换模型，或把正文里这几条指令删掉。没有发出去。"
            )

    @staticmethod
    def _guard_ranges(body: dict[str, Any]) -> None:
        """发之前拦下超区间的值（区间是官方写死的，拦截是确定的）。"""
        def _at(path: str) -> Any:
            node: Any = body
            for part in path.split("."):
                if not isinstance(node, dict):
                    return None
                node = node.get(part)
            return node

        for path, (low, high) in NUMERIC_RANGES.items():
            value = _at(path)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise EngineError(
                    f"{path} 只接受数字，收到的是 {value!r}——请改成一个数值。"
                ) from None
            if not (low <= number <= high):
                raise EngineError(
                    f"MiniMax 的 {path} 只接受 {low}–{high} 之间的值（收到 {number}），"
                    "没有发出去。"
                )

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def list_voices(self, cfg: BaseModel) -> VoiceList:
        return await client.fetch_voices(_as_settings(cfg), label=self.label)

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        body, notes = self.build_body_with_notes(settings, req)
        result = await client.synthesize(settings, body, label=self.label)
        if not notes:
            return result
        # 引擎那边也说了话（计费之类）时**两条都留**，别用后面这句盖掉前面那句。
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=(*result.notes, *notes),
            subtitle_url=result.subtitle_url,
        )

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        """上传音源 → 克隆 → 回一个可长期使用的音色。

        所有"看参数就能判"的检查都排在第一次上传之前：上传已算一次调用，
        先传再报"试听文本超 1000 字"就白花了。
        """
        cfg = _as_settings(cfg)
        if not (req.audio_path or "").strip():
            raise EngineError("音色克隆需要本地音源音频（10 秒至 5 分钟，mp3/m4a/wav，不超过 20MB）。")

        voice_id = normalize_voice_id(req.voice_id, req.name)

        # ---- 本地能判的，在这里判完 ----
        prompt_audio = (req.prompt_audio or "").strip()
        prompt_text = (req.prompt_text or "").strip()
        # 官方要求 ``clone_prompt`` 的两个子字段成对出现，缺一个就不发这一项。
        if bool(prompt_audio) != bool(prompt_text):
            raise EngineError(
                "提示音频（prompt_audio）与提示文本（prompt_text）必须成对提供——"
                "只给其中一个，平台会拒绝这次克隆。"
            )
        validation = (req.transcript or "").strip()
        if len(validation) > 200:
            raise EngineError(
                f"音源文本（text_validation）最多 200 字，现在 {len(validation)} 字。"
            )
        preview = (req.preview_text or "").strip()
        if len(preview) > 1000:
            raise EngineError(f"试听文本最多 1000 字，当前 {len(preview)} 字。")

        # ---- 从这里开始才真正向平台发请求 ----
        file_id = await client.upload_audio(
            cfg, req.audio_path, client.PURPOSE_SOURCE, label=self.label
        )
        body: dict[str, Any] = {"file_id": file_id, "voice_id": voice_id}
        if prompt_audio:
            prompt_file_id = await client.upload_audio(
                cfg, prompt_audio, client.PURPOSE_PROMPT, label=self.label
            )
            body["clone_prompt"] = {"prompt_audio": prompt_file_id, "prompt_text": prompt_text}
        if validation:
            body["text_validation"] = validation
        if preview:
            body["text"] = preview
            body["model"] = req.model or cfg.model

        result = await client.clone_voice(cfg, body, label=self.label)
        note = ""
        if isinstance(result.get("extra_info"), dict) and result["extra_info"].get("usage_characters"):
            note = f"（试听按 {result['extra_info']['usage_characters']} 字符计费）"
        demo = str(result.get("demo_audio") or "").strip()
        return VoiceMeta(
            id=voice_id,
            name=(req.name or voice_id).strip(),
            description=(
                f"克隆自 {Path(req.audio_path).name}{note}"
                + (f"，试听：{demo}" if demo else "")
                + "。7 天内至少要使用一次，否则平台会删除它。"
            ),
            category="克隆音色",
            tags=("克隆",),
        )
