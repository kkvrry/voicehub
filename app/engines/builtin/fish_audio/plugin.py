"""Fish Audio 引擎插件——云端语音合成服务（要密钥、要联网、按量计费）。

契约来自官方文档：合成 ``POST /v1/tts``（响应体即音频，模型走请求头 ``model``）；
音色清单 ``GET /model``；克隆 ``POST /model``（multipart，``train_mode=fast`` 建完即可用）。
情绪与引导指令没有独立字段，写成正文开头的 ``[标签]``——这是 S2 的文档语法。

不做的事：不内置音色清单（拉不到就说为什么）、不猜错误原因（只报平台原话）、
不替平台把秒数或音高塞进一个它没有的字段。
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
from app.engines.builtin.fish_audio import client
from app.engines.builtin.fish_audio.mapping import (
    AUDIO_FORMATS,
    MAPPING,
    MARK_MAP,
    MARK_PASSTHROUGH,
    NUMERIC_RANGES,
    emotion_tag,
    instruction_tag,
)
from app.engines.builtin.fish_audio.settings import FishAudioSettings
from app.engines.mapping import EngineMapping, apply_mapping
from app.engines.presets import load_presets

__all__ = ["FishAudioEngine"]

PRESETS_PATH = Path(__file__).resolve().parent / "instructions.json"

#: 克隆音源的要求（官方：samples 支持 wav / mp3 / m4a / opus，建议 10 秒以上）。
SAMPLE_NOTE = "音源 10 秒以上，单人、无背景音乐；wav / mp3 / m4a / opus。"


def _as_settings(cfg: BaseModel) -> FishAudioSettings:
    """把框架递来的配置收窄成本引擎模型；类型不对即实现错误，立即炸掉。"""
    assert isinstance(cfg, FishAudioSettings)
    return cfg


def _with_tags(text: str, emotion: str, instruction: str) -> str:
    """把情绪与引导指令写成正文开头的 ``[标签]``。

    平台没有这两个字段：S2 的表达控制就是正文里的方括号标签。顺序固定为
    情绪在前、指令在后，便于对照日志。
    """
    tags: list[str] = []
    emo = emotion_tag(emotion) if emotion else ""
    if emo:
        tags.append(f"[{emo}]")
    ins = instruction_tag(instruction) if instruction else ""
    if ins:
        tags.append(f"[{ins}]")
    return f"{' '.join(tags)} {text}" if tags else text


class FishAudioEngine(EnginePlugin):
    """Fish Audio 语音合成（``s2.1-pro`` / ``s2.1-pro-free`` / ``s2-pro``）。"""

    key = "fish_audio"
    label = "Fish Audio"
    version = "1.0.0"
    homepage = "https://fish.audio"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return FishAudioSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_CLOUD,
            voice_listing=True,
            voice_cloning=True,  # POST /model，train_mode=fast
            reference_audio=False,  # 内联参考音频要 msgpack 请求体，本插件不接
            instruction_control=True,  # 写成 [自由文本] 标签
            emotion_control=True,  # 写成 [情绪] 标签
            # 平台按 chunk_length 切段处理长文本，没有按字数的硬上限。
            max_chars=0,
            # 平台标称 80+ 语言，语言由正文自动判定；这里只列统一词表覆盖的那些。
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
            language_note="平台标称支持 80+ 语言、按正文自动判定；此处只列统一词表对得上的 17 种",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=AUDIO_FORMATS)

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """规范里有、这一家没有落点的项：面板逐条写原因，不静默丢。"""
        return {
            "reference_audio": (
                "合成只认平台音色 id；内联参考音频需要 msgpack 请求体，"
                "本地音色请先用「音色克隆」注册。"
            ),
            "reference_text": "不接内联参考音频，因此也不需要逐字稿。",
            "reference_language": "平台按音源自动判定，没有参考语言字段。",
            "guidance_scale": "平台没有强度参数；演法只能写在 [标签] 里。",
            "pitch": "平台没有音高参数。",
            "seed": "平台没有种子参数。",
            "language": "平台按正文自动判定语言，没有语言开关。",
            "lead_silence": "平台没有前静默参数；停顿写成正文里的 [[停顿]]（落 [pause]）。",
            "lag_silence": "平台没有后静默参数；停顿写成正文里的 [[停顿]]（落 [pause]）。",
        }

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """有落点但映射表表达不了的项：两者都拼进正文，没有独立字段。"""
        return {
            "emotion": ("text", "作为 [情绪标签] 写在正文开头。"),
            "instruction": ("text", "作为 [自由文本标签] 写在正文开头。"),
        }

    def mark_map(self) -> dict[str, str]:
        return MARK_MAP

    def mark_passthrough(self) -> str:
        """官方接受自由文本标签，表外的标签原样带方括号转过去。"""
        return MARK_PASSTHROUGH

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

    def build_body(self, cfg: FishAudioSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``/v1/tts`` 请求体。

        先落上游给的值，再补配置默认值（顺序不能反）；情绪与引导指令在这一层
        拼进正文——网关已把语气词标签换成 ``[laughing]`` 这类写法。
        """
        values = req.as_values()
        text = str(values.get("text") or "")
        if text:
            values["text"] = _with_tags(
                text,
                str(values.get("emotion") or ""),
                str(values.get("instruction") or ""),
            )
        body = apply_mapping(MAPPING, values)
        self._guard_ranges(body)

        body.setdefault("format", req.format or cfg.audio_format)
        body.setdefault("sample_rate", cfg.sample_rate)
        body.setdefault("latency", cfg.latency)
        body.setdefault("chunk_length", cfg.chunk_length)
        body.setdefault("normalize", cfg.normalize)
        body.setdefault("temperature", cfg.temperature)
        body.setdefault("top_p", cfg.top_p)
        return body

    @staticmethod
    def _guard_ranges(body: dict[str, Any]) -> None:
        """发之前拦下超区间的值（区间是官方写死的，拦截是确定的）。"""
        prosody = body.get("prosody")
        speed = prosody.get("speed") if isinstance(prosody, dict) else None
        if speed is None:
            return
        low, high = NUMERIC_RANGES["prosody.speed"]
        try:
            number = float(speed)
        except (TypeError, ValueError):
            raise EngineError(f"prosody.speed 只接受数字，收到的是 {speed!r}。") from None
        if not (low <= number <= high):
            raise EngineError(
                f"Fish Audio 的语速只接受 {low}–{high} 倍（收到 {number}），没有发出去。"
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
        """上传音源 → 建一个可复用的音色 id。

        平台没有试听参数：``preview_text`` 填了也不会生成试听，这一点写进描述，
        不静默丢掉。本地能判的（有没有音源）排在第一次上传之前。
        """
        settings = _as_settings(cfg)
        path = (req.audio_path or "").strip()
        if not path:
            raise EngineError(f"音色克隆需要本地音源音频（{SAMPLE_NOTE}）。")
        if (req.voice_id or "").strip():
            raise EngineError(
                f"{self.label} 的音色 id 由平台生成，不能指定——把「音色 id」留空即可。"
            )

        title = (req.name or "").strip() or Path(path).stem or "未命名音色"
        description = f"克隆自 {Path(path).name}"
        if (req.preview_text or "").strip():
            description += "；平台克隆接口不生成试听，试听请在音色页发起"

        result = await client.create_voice(
            settings,
            title=title,
            audio_path=path,
            transcript=(req.transcript or "").strip(),
            description=description,
            label=self.label,
        )
        state = str(result.get("state") or "").strip().lower()
        if state == "failed":
            reason = ""
            quality = result.get("quality")
            if isinstance(quality, dict):
                reason = str(quality.get("quality_reason") or "").strip()
            raise EngineError(
                f"{self.label} 训练这个音色失败" + (f"：{reason}" if reason else "。")
            )

        voice_id = str(result.get("_id") or "").strip()
        note = "" if state in ("trained", "") else f"（当前状态 {state}，尚不能合成）"
        return VoiceMeta(
            id=voice_id,
            name=title,
            description=description + note,
            category="",
            tags=("克隆",),
        )
