"""直连一台 SenseVoice-Small 服务的识别引擎。

**线协议与 Qwen3-ASR 相同**（audio.cpp 的 OpenAI 兼容端点）：multipart 上传
WAV，普通端点回 ``text`` + ``timing``，``/details`` 多一个 ``language``。
差异只在模型侧：

- ``language`` 回报的是 BCP-47 代码（``zh``），不是 Qwen 那种名称（``Chinese``）；
- 模型本身带事件/情感标签，但当前 HTTP 结果没有单独字段，``keep_tags`` 在
  streaming 实例上也没生效；本引擎按接口实际返回声明，不假装有；
- ``sense_asr``（offline）需要 Silero VAD 资产，内置运行包不带；
  默认用 ``sense-asr``（streaming）实例，见 :mod:`.settings`。

HTTP 细节在 :mod:`app.engines.asr_http`，本目录只保留配置、能力与语言清单。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.engines import asr_http
from app.engines.asr_base import AsrCapabilities, AsrEngine, Transcript
from app.engines.builtin.sense_asr.languages import LANGUAGE_GROUPS, LANGUAGE_SUMMARY
from app.engines.builtin.sense_asr.settings import SenseAsrSettings

__all__ = ["SenseAsrEngine"]


class SenseAsrEngine(AsrEngine):
    """直连一台 SenseVoice-Small 服务（OpenAI 兼容识别端点）。"""

    key = "sense_asr"
    label = "SenseVoice-Small"
    vendor = "FunAudioLLM"
    homepage = "https://github.com/FunAudioLLM/SenseVoice"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return SenseAsrSettings

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities(
            languages=LANGUAGE_SUMMARY,
            language_groups=LANGUAGE_GROUPS,
            returns_language=True,
            returns_emotion=False,
            returns_events=False,
            timestamps=False,
            local=True,
            formats=("wav",),
            format_note="其余格式先转 16 kHz 单声道 WAV 后上传。",
            streaming=False,
            streaming_note="服务端以 streaming 实例运行；本引擎按整段结果调用。",
        )

    @classmethod
    def api_format(cls) -> dict[str, str]:
        """请求与返回格式（audio.cpp 实测口径）。"""
        return {
            "request": (
                "POST {服务地址}/v1/audio/transcriptions；回报语种时改走 /details。"
                "multipart 上传 WAV，表单带 model / response_format / 可选 language"
            ),
            "response": (
                "text 恒有；/details 另返回 language（BCP-47 代码，如 zh）。"
                "无 emotion 与声音事件字段；timing 是推理耗时（wall_ms / rtf），不是时间轴"
            ),
        }

    def readiness(self, cfg: BaseModel) -> str:
        conf = cfg if isinstance(cfg, SenseAsrSettings) else SenseAsrSettings()
        if not (conf.base_url or "").strip():
            return "未填写服务地址。"
        if not (conf.model or "").strip():
            return "未填写模型 id。"
        return ""

    def transcribe(self, cfg: BaseModel, path: Path) -> Transcript:
        conf = cfg if isinstance(cfg, SenseAsrSettings) else SenseAsrSettings()
        return asr_http.transcribe(conf, Path(path), engine=self.key, label=self.label)

    def probe(self, cfg: BaseModel) -> tuple[bool, str]:
        conf = cfg if isinstance(cfg, SenseAsrSettings) else SenseAsrSettings()
        return asr_http.probe(conf, label=self.label)
