"""Kitta Audio 引擎插件——云端语音合成（供应商无关的统一协议，要密钥、按量计费）。

契约来自官方文档：同步合成 ``POST /api/open/v3/speech/tts``（响应体即音频）；
音色清单 ``GET /api/open/v3/voices``；克隆 ``POST /api/open/v1/voices``（multipart）。
平台负责把公共参数路由到具体供应商模型，客户端只用 ``voiceId`` / ``modelId`` 与通用控制参数。

**情绪走引导指令**：平台的 ``emotion`` 是"全局情绪 ID"，词表由账号侧定义、文档没有公开，
发自造值会 400；而 ``instruction`` 明确接受情绪描述。所以情绪写成「情绪：X。」并到指令前面
——两者在同一格，不构成"两路风格"。

**不声明语气词标签**：标签语法取决于底层供应商模型，平台文档没有约定；声明了就等于
替所有路由目标担保。正文里的 ``[[笑]]`` 在本引擎上会被删掉，演法请写在引导指令里。

不做的事：不内置音色清单（拉不到就说为什么）、不猜错误原因（只报平台原话与 requestId）、
不把被平台忽略的参数当成"已生效"（它写在响应头里，转成 notes 回给用户）。
"""

from __future__ import annotations

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
    UiSection,
    VoiceList,
    VoiceMeta,
)
from app.engines.builtin.kitta import client
from app.engines.builtin.kitta.mapping import (
    AUDIO_FORMATS,
    INSTRUCTION_LIMIT,
    MAPPING,
    NUMERIC_RANGES,
    with_emotion,
)
from app.engines.builtin.kitta.settings import KittaSettings
from app.engines.mapping import EngineMapping, apply_mapping
from app.engines.presets import load_presets

__all__ = ["KittaEngine"]

PRESETS_PATH = Path(__file__).resolve().parent / "instructions.json"

#: 克隆音源的要求（官方：单个文件 ≤10MB，支持 wav / mp3 / m4a / flac / ogg / opus 等）。
SAMPLE_NOTE = "参考音频单个不超过 10 MB，格式见平台列表；多个文件可一次上传。"

#: 正文上限（官方字段说明；个别模型可能更窄，以能力目录的 maxTextLength 为准）。
MAX_CHARS = 10_000


def _as_settings(cfg: BaseModel) -> KittaSettings:
    """把框架递来的配置收窄成本引擎模型；类型不对即实现错误，立即炸掉。"""
    assert isinstance(cfg, KittaSettings)
    return cfg


class KittaEngine(EnginePlugin):
    """Kitta Audio 语音合成（``fishaudio-s21pro-flash`` 等由平台路由的模型）。"""

    key = "kitta"
    label = "Kitta Audio"
    version = "1.0.0"
    homepage = "https://kittaaudio.com"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return KittaSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_CLOUD,
            voice_listing=True,
            voice_cloning=True,  # POST /api/open/v1/voices
            reference_audio=False,  # 合成只认 voiceId，参考音频走克隆
            instruction_control=True,
            # 没有情绪参数：emotion 被写成一句引导指令，属于翻译而不是原生情绪枚举
            # （与 BreezeTTS 同一口径）。
            emotion_control=False,
            max_chars=MAX_CHARS,
            # 平台按音色与模型路由，语言覆盖取决于底层供应商；只列统一词表对得上的。
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
            language_note="语言由音色与底层模型决定；此处只列统一词表对得上的 17 种",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=AUDIO_FORMATS)

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """规范里有、这一家没有落点的项：面板逐条写原因，不静默丢。"""
        return {
            "reference_audio": "合成只认平台音色 id；参考音频请用「音色克隆」建一个音色。",
            "reference_text": "不接内联参考音频，因此也不需要逐字稿。",
            "reference_language": "平台没有参考语言字段，语言由音色决定。",
            "guidance_scale": "平台没有强度参数；风格与演法写在引导指令里。",
            "seed": "平台没有种子参数。",
            "sample_rate": "平台不接受采样率参数，由模型决定。",
            "bitrate": "平台不接受码率参数，由模型决定。",
            "lead_silence": "平台没有前静默参数。",
            "lag_silence": "平台没有后静默参数。",
        }

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """有落点但映射表表达不了的项：情绪并进引导指令，没有独立字段。"""
        return {
            "emotion": ("instruction", "写成「情绪：X。」并到引导指令前面。"),
        }

    def field_presets(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {"instruction": load_presets(PRESETS_PATH)}

    def readiness(self, cfg: BaseModel) -> str:
        if not getattr(cfg, "configured", False):
            return "还没填 API Key。"
        return ""

    def ui_sections(self) -> tuple[UiSection, ...]:
        """克隆页补一句音源要求——那是本地判不了、发出去才知道的事。"""
        return tuple(
            UiSection(section.key, section.label, SAMPLE_NOTE, dict(section.meta))
            if section.key == "clone"
            else section
            for section in super().ui_sections()
        )

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: KittaSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``/v3/speech/tts`` 请求体。

        先落上游给的值（情绪并进指令），再补配置默认值；``modelId`` 只在配置里填了
        才发——留空是"由音色所属引擎决定"，补一个默认值等于替平台做了选择。
        """
        values = req.as_values()
        instruction = with_emotion(
            str(values.get("instruction") or ""), str(values.get("emotion") or "")
        )
        if instruction:
            values["instruction"] = instruction
        self._guard_instruction(instruction)

        body = apply_mapping(MAPPING, values)
        self._guard_ranges(body)

        body.setdefault("format", req.format or cfg.audio_format)
        body.setdefault("textNormalization", cfg.text_normalization)
        model = (cfg.model_id or "").strip()
        if model:
            body.setdefault("modelId", model)
        return body

    @staticmethod
    def _guard_instruction(instruction: str) -> None:
        if len(instruction) > INSTRUCTION_LIMIT:
            raise EngineError(
                f"引导指令上限 {INSTRUCTION_LIMIT} 字，现在 {len(instruction)} 字。"
                "情绪与指令写在同一个字段里，两条合起来算。"
            )

    @staticmethod
    def _guard_ranges(body: dict[str, Any]) -> None:
        """发之前拦下超区间的值（区间是官方写死的，拦截是确定的）。"""
        labels = {"speed": "语速", "volume": "音量", "pitch": "音高"}
        for key, (low, high) in NUMERIC_RANGES.items():
            value = body.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise EngineError(f"{key} 只接受数字，收到的是 {value!r}。") from None
            if not (low <= number <= high):
                raise EngineError(
                    f"{labels[key]}只接受 {low}–{high}（收到 {number}），没有发出去。"
                )

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def list_voices(self, cfg: BaseModel) -> VoiceList:
        return await client.fetch_voices(_as_settings(cfg), label=self.label)

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        body = self.build_body(settings, req)
        return await client.synthesize(settings, body, label=self.label)

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        """上传参考音频 → 换一个 ``voiceId``。

        平台没有试听参数：``preview_text`` 填了也不会生成试听，这一点写进描述。
        音源大小与容器在本地先判（见 ``client.create_voice``），那次上传不白花。
        """
        settings = _as_settings(cfg)
        path = (req.audio_path or "").strip()
        if not path:
            raise EngineError(f"音色克隆需要参考音频（{SAMPLE_NOTE}）。")
        if (req.voice_id or "").strip():
            raise EngineError(
                f"{self.label} 的音色 id 由平台生成，不能指定——把「音色 id」留空即可。"
            )

        name = (req.name or "").strip() or Path(path).stem or "未命名音色"
        description = f"克隆自 {Path(path).name}"
        if (req.preview_text or "").strip():
            description += "；平台克隆接口不生成试听，试听请在音色页发起"

        result = await client.create_voice(
            settings,
            name=name,
            audio_path=path,
            transcript=(req.transcript or "").strip(),
            description=description,
            label=self.label,
        )
        return VoiceMeta(
            id=str(result.get("voiceId") or "").strip(),
            name=str(result.get("title") or name).strip(),
            description=str(result.get("description") or description).strip(),
            category="",
            tags=("克隆",),
        )
