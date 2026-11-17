"""直连一台 Qwen3-ASR 服务的识别引擎。

**它是什么形状**：一台**单模型**服务——「服务地址」指向谁，就用谁加载的那个模型
（默认 ``http://127.0.0.1:18999`` 上的 ``qwen3_asr``）。不连 hub 发现模型：填错
端口时「测试连接」会把那台服务上实际加载的模型列出来，一眼看出指错。

**输入**：服务端核心音频路由接收 WAV；MP3 / FLAC 需要额外的解码前端，是否启用
取决于部署。所以引擎按最保守口径声明只收 WAV，其余格式由 :mod:`.client` 用
ffmpeg 转成 16 kHz 单声道后上传——同一份引擎在开没开解码前端的服务上都工作。

**流式**：服务端支持 transcript SSE（``stream=true``），但本项目的调用
（样例逐字稿、麦克风输入、合成前补文本）都要整段文字后才继续，流式没有落点，
因此不启用。能力里如实写 ``streaming=False``，面板上把服务端能力一并说明。

**它给什么、不给什么**（实测，不是照抄文档）：

- ``text``：有。
- ``language``：**有**——但只在 ``/details`` 端点上（``Chinese`` / ``Japanese``），
  普通端点只回 ``text`` 与 ``timing``。官方声明覆盖 30 种语言 + 22 种中文方言，
  清单登记在 :mod:`.languages`，面板照它画表。
- ``emotion``：**没有**。Qwen3-ASR 是"音频→文本"，不带情感分类。契约里留了
  ``emotion`` 字段，接上带情感标签的模型那天界面自然多一格，核心不用改。
- ``events``：**没有**。声音事件（笑声、掌声、音乐）不是这台模型的任务。
- ``timestamps``：**没有**。``timing`` 里是推理耗时（``wall_ms`` /
  ``audio_duration_ms`` / ``rtf``），不是逐词时间轴；逐词时间轴要另配
  Qwen3-ForcedAligner，这台服务没有加载它。

注册表扫到本目录的 :class:`Qwen3AsrEngine` 即完成接入，主代码零改动。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.engines.asr_base import AsrCapabilities, AsrEngine, Transcript
from app.engines.builtin.qwen3_asr import client
from app.engines.builtin.qwen3_asr.languages import LANGUAGE_GROUPS, LANGUAGE_SUMMARY
from app.engines.builtin.qwen3_asr.settings import Qwen3AsrSettings

__all__ = ["Qwen3AsrEngine"]


class Qwen3AsrEngine(AsrEngine):
    """直连一台 Qwen3-ASR 服务（OpenAI 兼容识别端点）。"""

    key = "qwen3_asr"
    label = "Qwen3-ASR"
    vendor = "Qwen"
    homepage = "https://github.com/QwenLM/Qwen3-ASR"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return Qwen3AsrSettings

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
            streaming_note="服务端支持 SSE，本引擎按整段结果调用。",
        )

    @classmethod
    def api_format(cls) -> dict[str, str]:
        """请求与返回格式（实测口径，不是照抄文档）。换模型时跟着换。"""
        return {
            "request": (
                "POST {服务地址}/v1/audio/transcriptions；回报语种时改走 /details。"
                "multipart 上传 WAV，表单带 model / response_format / 可选 language"
            ),
            "response": (
                "text 恒有；/details 另返回 language。无 emotion 与声音事件；"
                "timing 是推理耗时（wall_ms / rtf），不是时间轴"
            ),
        }

    def readiness(self, cfg: BaseModel) -> str:
        conf = cfg if isinstance(cfg, Qwen3AsrSettings) else Qwen3AsrSettings()
        if not (conf.base_url or "").strip():
            return "未填写服务地址。"
        if not (conf.model or "").strip():
            return "未填写模型 id。"
        return ""

    def transcribe(self, cfg: BaseModel, path: Path) -> Transcript:
        conf = cfg if isinstance(cfg, Qwen3AsrSettings) else Qwen3AsrSettings()
        return client.transcribe(conf, Path(path))

    def probe(self, cfg: BaseModel) -> tuple[bool, str]:
        conf = cfg if isinstance(cfg, Qwen3AsrSettings) else Qwen3AsrSettings()
        return client.probe(conf)
