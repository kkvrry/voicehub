"""配音生产方配置。

设置页只画「项目」一组：切分阈值、贴齐、导出这些都在工作区右列改，
参数离使用现场只有一步；这里存的是它们共用的默认值。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.uischema import ui_hints

__all__ = [
    "AUDIO_FORMAT_CHOICES",
    "ENCODING_CHOICES",
    "LOUDNESS_CHOICES",
    "SETTINGS_PAGE_GROUPS",
    "DubbingSettings",
]

AUDIO_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("mp3", "MP3"),
    ("m4a", "M4A（AAC）"),
    ("wav", "WAV"),
)

ENCODING_CHOICES: tuple[tuple[str, str], ...] = (
    ("utf-8", "UTF-8"),
    ("utf-8-sig", "UTF-8（带 BOM）"),
    ("gb18030", "GB18030"),
)

#: 混音响度目标：视频平台按 −16 LUFS 更稳，播客按 −19，纯校对不需要动。
LOUDNESS_CHOICES: tuple[tuple[str, str], ...] = (
    ("video", "视频（−16 LUFS）"),
    ("podcast", "播客（−19 LUFS）"),
    ("off", "不校准"),
)

#: 响度预设 → ffmpeg ``loudnorm`` 参数。
LOUDNESS_TARGETS: dict[str, str] = {
    "video": "I=-16:TP=-1.5:LRA=11",
    "podcast": "I=-19:TP=-2:LRA=11",
    "off": "",
}

GROUP_PROJECT = "项目"
GROUP_SPLIT = "切分与识别"
GROUP_SYNTH = "合成与贴齐"
GROUP_EXPORT = "导出"

SETTINGS_PAGE_GROUPS: tuple[str, ...] = (GROUP_PROJECT,)


def _at(step: str, label: str, **kwargs: Any) -> dict[str, Any]:
    """字段归属的流水线步骤：工作区按 ``step`` 把参数画进对应步骤卡，
    不再另抄一份分组表。步骤 key 与 ``service.STEP_MANIFEST`` 一致。"""
    return ui_hints(label, step=step, **kwargs)


class DubbingSettings(BaseModel):
    project_dir: str = Field(
        default="",
        description="项目存储目录；留空使用服务数据目录。",
        json_schema_extra=ui_hints("项目目录", group=GROUP_PROJECT),
    )

    # ---- 切分与识别 -------------------------------------------------------
    silence_db: float = Field(
        default=-35.0,
        ge=-60.0,
        le=-10.0,
        description=(
            "音量低于它算停顿。语音底噪一般在 −50 dB 以下，取值越接近 0 切得越碎。"
        ),
        json_schema_extra=_at(
            "transcribe", "静音阈值（dB）", widget="number", group=GROUP_SPLIT
        ),
    )
    silence_min: float = Field(
        default=0.35,
        ge=0.1,
        le=3.0,
        description=(
            "短于这个长度的停顿不切（那是换气）。字幕按切出来的段走，"
            "调得太小会让一句话拆成两三行。"
        ),
        json_schema_extra=_at(
            "transcribe", "最短停顿（秒）", widget="number", group=GROUP_SPLIT
        ),
    )
    max_segment: float = Field(
        default=15.0,
        ge=5.0,
        le=60.0,
        description=(
            "没有停顿时最多按这个长度硬切，避免整段音频塞进一次识别请求。"
        ),
        json_schema_extra=_at(
            "transcribe", "最长段（秒）", widget="number", group=GROUP_SPLIT
        ),
    )
    speaker_diar: bool = Field(
        default=False,
        description=(
            "转录后按说话人分离给每个说话人建一个角色，段落自动挂到角色上；"
            "需要 audio.cpp 装了说话人分离模型（diar）并在运行，没有就跳过。"
        ),
        json_schema_extra=_at(
            "transcribe", "说话人分离", widget="switch", group=GROUP_SPLIT
        ),
    )

    # ---- 合成与贴齐 -------------------------------------------------------
    fit: bool = Field(
        default=True,
        description=(
            "长过时隙的段先加速再放，贴得进去就不算顺延；超过最大倍率仍贴不进去的"
            "按原句时隙截断，不越过下一段。关闭后不加速，长段仍会截断。"
        ),
        json_schema_extra=_at("align", "变速贴齐", widget="switch", group=GROUP_SYNTH),
    )
    max_speed: float = Field(
        default=1.35,
        ge=1.05,
        le=2.0,
        description=(
            "贴齐时允许的最大倍率。调大能贴进更多段，但语气会被改变；"
            "超过 1.4 人耳就能听出赶。"
        ),
        json_schema_extra=_at(
            "align",
            "最大倍率",
            widget="number",
            group=GROUP_SYNTH,
            enabled_when=("fit", "true"),
        ),
    )
    concurrency: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "同时在途的合成请求数。本机单模型服务必须为 1：并发只增加排队时间。"
        ),
        json_schema_extra=_at("synth", "并发合成", widget="number", group=GROUP_SYNTH),
    )
    loudness: str = Field(
        default="video",
        description="混音后的响度目标；不校准时保留各轨的原始电平。",
        json_schema_extra=_at(
            "align",
            "响度目标",
            widget="select",
            choices=LOUDNESS_CHOICES,
            group=GROUP_SYNTH,
        ),
    )

    # ---- 导出 -------------------------------------------------------------
    audio_format: str = Field(
        default="m4a",
        description="纯音频项目的成品容器；视频项目一律封装成 MP4。",
        json_schema_extra=_at(
            "export",
            "音频格式",
            widget="select",
            choices=AUDIO_FORMAT_CHOICES,
            group=GROUP_EXPORT,
        ),
    )
    bitrate: str = Field(
        default="192k",
        description="有损容器的码率；WAV 忽略这一项。",
        json_schema_extra=_at("export", "码率", group=GROUP_EXPORT),
    )
    subtitle_formats: str = Field(
        default="srt",
        description="同时写出的字幕格式，逗号分隔：srt / vtt / lrc / txt / ass。",
        json_schema_extra=_at("export", "字幕格式", group=GROUP_EXPORT),
    )
    subtitle_max_chars: int = Field(
        default=0,
        ge=0,
        le=80,
        description=(
            "导出时把长句切成不超过这么多字的一条；0 = 不切。"
            "只影响字幕文件，不改变时间轴上的段落。"
        ),
        json_schema_extra=_at(
            "export", "每行最多字数", widget="number", group=GROUP_EXPORT
        ),
    )
    subtitle_encoding: str = Field(
        default="utf-8",
        description="字幕文件编码。播放器对 BOM 敏感，默认不带。",
        json_schema_extra=_at(
            "export",
            "字幕编码",
            widget="select",
            choices=ENCODING_CHOICES,
            group=GROUP_EXPORT,
        ),
    )
    burn_subtitles: bool = Field(
        default=False,
        description=(
            "视频项目导出时带上字幕轨（可开关，不烧进画面）。"
            "播放器不显示字幕轨时关掉它，另发 SRT。"
        ),
        json_schema_extra=_at("export", "字幕轨", widget="switch", group=GROUP_EXPORT),
    )
