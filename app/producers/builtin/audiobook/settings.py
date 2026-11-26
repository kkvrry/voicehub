"""有声书生产方配置。

配置项只保留与这条流水线直接相关的开关；语气词映射、插入式标记等
属于后续阶段，不在这里预留空字段。

分析模型走 VoiceHub 的「通道」体系：先选对话模型通道，再从通道拉到的
可用模型里选；提示词留空就用内置的整章上下文提示词。

设置页只画「项目」一组（见 ``SETTINGS_PAGE_GROUPS``）：合并、字幕、BGM、
分析与合成参数都在创作页对应的卡片上改，这里只是它们共用的声明。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.uischema import ui_hints

__all__ = [
    "ANALYSIS_CONTEXT_CHOICES",
    "ANALYSIS_GRANULARITY_CHOICES",
    "ANALYSIS_SCOPE_CHOICES",
    "BITRATE_CHOICES",
    "GROUP_LOUDNESS",
    "MASTER_LOUDNESS_PRESET_CHOICES",
    "MERGE_FORMAT_CHOICES",
    "ROW_BALANCE_MODE_CHOICES",
    "SETTINGS_PAGE_GROUPS",
    "AudiobookSettings",
]

#: 章节成品的容器格式；M4A 体积小，WAV 无损但体积大。
MERGE_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("m4a", "M4A（AAC）"),
    ("mp3", "MP3"),
    ("wav", "WAV"),
)

BITRATE_CHOICES: tuple[tuple[str, str], ...] = (
    ("64k", "64 kbps"),
    ("96k", "96 kbps"),
    ("128k", "128 kbps"),
    ("160k", "160 kbps"),
    ("192k", "192 kbps"),
    ("256k", "256 kbps"),
)

#: 一次分析请求覆盖多少对白行。
ANALYSIS_GRANULARITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("batch", "按批发送"),
    ("chapter", "整章一次"),
)

#: 分析哪些行：全部对白（复核），或只补还没标注过的。
ANALYSIS_SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "全部对白"),
    ("pending", "只补未标注"),
)

#: 送给模型的正文上下文范围。
ANALYSIS_CONTEXT_CHOICES: tuple[tuple[str, str], ...] = (
    ("chapter", "整章正文"),
    ("window", "前后若干行"),
)

ROW_BALANCE_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("voice", "音色基线"),
    ("row", "逐行温和"),
)

MASTER_LOUDNESS_PRESET_CHOICES: tuple[tuple[str, str], ...] = (
    ("podcast", "播客（单声道 −19 LUFS）"),
    ("acx", "ACX 响度参考（RMS −20.5 dBFS）"),
    ("custom", "自定义 LUFS"),
)

# ---- 设置页卡片分组（同一组画进一张卡） --------------------------------
GROUP_PROJECT = "项目"
GROUP_ANALYSIS = "对话分析"
GROUP_SYNTH = "逐行合成"
GROUP_MERGE = "成品合并"
GROUP_LOUDNESS = "响度处理"
GROUP_SUBTITLE = "字幕"
GROUP_BGM = "背景音乐"

#: 设置页只放这一组：合并、字幕、BGM、分析与合成参数都在创作页对应的卡片上改，
#: 参数离使用现场只有一步。schema 仍完整下发——工作区的卡片读同一份候选与默认值。
SETTINGS_PAGE_GROUPS: tuple[str, ...] = (GROUP_PROJECT,)


class AudiobookSettings(BaseModel):

    project_dir: str = Field(
        default="",
        description="项目存储目录；留空使用服务数据目录。",
        json_schema_extra=ui_hints("项目目录", group=GROUP_PROJECT),
    )

    # ---- 分析 -------------------------------------------------------------
    analysis_channel: str = Field(
        default="",
        description="对话模型通道（app/llm 下的插件）；留空用默认通道。",
        json_schema_extra=ui_hints("分析通道", widget="llm_provider", group=GROUP_ANALYSIS),
    )
    analysis_model: str = Field(
        default="",
        description="通道里的模型 id；留空用通道默认模型。",
        json_schema_extra=ui_hints(
            "分析模型",
            widget="llm_model",
            depends_on="analysis_channel",
            group=GROUP_ANALYSIS,
        ),
    )
    analysis_preset: str = Field(
        default="",
        description=(
            "选中的分析提示词预设（设置 → 词表 → 对话分析提示词）；"
            "留空时用默认预设，预设被删除后回落到内置提示词。"
        ),
        json_schema_extra=ui_hints("分析提示词预设", group=GROUP_ANALYSIS),
    )
    analysis_prompt: str = Field(
        default="",
        description=(
            "本项目的自定义分析提示词；支持 {book} / {text} / {rows} 占位。"
            "留空时用所选预设的生效正文。"
        ),
        json_schema_extra=ui_hints(
            "自定义分析提示词",
            widget="textarea",
            group=GROUP_ANALYSIS,
            placeholder="留空使用所选预设",
        ),
    )
    analysis_scope: str = Field(
        default="all",
        description=(
            "全部对白会把已标注的行一起复核；只补未标注只处理仍写着「对话」的行。"
        ),
        json_schema_extra=ui_hints(
            "分析范围",
            widget="select",
            choices=ANALYSIS_SCOPE_CHOICES,
            group=GROUP_ANALYSIS,
        ),
    )
    analysis_granularity: str = Field(
        default="batch",
        description=(
            "一批一个请求；整章一次尽量把一章装进一个请求。"
            "两者都不跨章，超出回复上限时自动分段。"
        ),
        json_schema_extra=ui_hints(
            "发送粒度",
            widget="select",
            choices=ANALYSIS_GRANULARITY_CHOICES,
            group=GROUP_ANALYSIS,
        ),
    )
    analysis_batch: int = Field(
        default=120,
        ge=10,
        le=400,
        description="按批发送时每批的上限行数；一批一个请求，超出回复上限自动分段。",
        json_schema_extra=ui_hints(
            "每批对白行数",
            widget="number",
            group=GROUP_ANALYSIS,
            enabled_when=("analysis_granularity", "batch"),
        ),
    )
    analysis_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "同时在途的分析请求数。DeepSeek 账号级并发上限为 Flash 2500 / Pro 500，"
            "本地 4–8 足够；再高只增加带宽与限流风险。"
        ),
        json_schema_extra=ui_hints("并发请求", widget="number", group=GROUP_ANALYSIS),
    )
    analysis_context: str = Field(
        default="chapter",
        description="送给模型的正文范围：整章正文，或本批对白前后若干行。",
        json_schema_extra=ui_hints(
            "上下文范围",
            widget="select",
            choices=ANALYSIS_CONTEXT_CHOICES,
            group=GROUP_ANALYSIS,
        ),
    )
    analysis_context_lines: int = Field(
        default=80,
        ge=5,
        le=400,
        description=(
            "「前后若干行」时每侧取多少行（窗口约 2 倍加本批）。"
            "接口无状态：每批重新发送这段正文，模型不保留上一批内容。"
        ),
        json_schema_extra=ui_hints(
            "上下文行数（每侧）",
            widget="number",
            group=GROUP_ANALYSIS,
            enabled_when=("analysis_context", "window"),
        ),
    )

    # ---- 逐行合成（每行一条 TTS） -----------------------------------------
    # 逐行音频的容器不设项：写盘时按引擎回报的真实格式（`SynthesisResult.format`）
    # 命名，成品格式由下方的「合并格式」决定。曾经有一格「音频格式」，它只进了文件名
    # 与指纹、从没发给引擎，改成 mp3 会得到一批 WAV 字节的 .mp3。
    concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "同时进行的合成请求数。只对没有自带并发设置的引擎生效：MiniMax / MiMo 在"
            "自己的引擎设置里限流，本机引擎按自己声明只跑一条。"
        ),
        json_schema_extra=ui_hints("合成并发", widget="number", group=GROUP_SYNTH),
    )
    qc_enabled: bool = Field(
        default=False,
        description=(
            "生成结束后把新音频再识别一遍，与原文比对，疑似行入行表的「疑似」档。"
            "只作提示，不改行状态、不阻断合并；识别引擎与「设置 → 识别」用的是同一台。"
        ),
        json_schema_extra=ui_hints("生成后预检", widget="switch", group=GROUP_SYNTH),
    )

    # ---- 成品合并（把逐行产物拼成章节） ------------------------------------
    merge_format: str = Field(
        default="m4a",
        description="成品容器格式；M4A（AAC）体积小，MP3 兼容老设备，WAV 无损但体积大。",
        json_schema_extra=ui_hints(
            "合并格式", widget="select", choices=MERGE_FORMAT_CHOICES, group=GROUP_MERGE
        ),
    )
    row_gap_ms: int = Field(
        default=120,
        ge=0,
        le=2000,
        description="行间静音长度；0 表示不留空隙。",
        json_schema_extra=ui_hints("行间静音（毫秒）", widget="number", group=GROUP_MERGE),
    )
    chapter_lead_ms: int = Field(
        default=3000,
        ge=0,
        le=10000,
        description="每章开头留的静音；字幕时间轴会一起后移。",
        json_schema_extra=ui_hints("章节前静音（毫秒）", widget="number", group=GROUP_MERGE),
    )
    chapter_tail_ms: int = Field(
        default=5000,
        ge=0,
        le=20000,
        description="每章结尾留的静音。",
        json_schema_extra=ui_hints("章节后静音（毫秒）", widget="number", group=GROUP_MERGE),
    )
    merge_bitrate: str = Field(
        default="128k",
        description="成品码率；仅 M4A / MP3 生效。",
        json_schema_extra=ui_hints(
            "成品码率",
            widget="select",
            choices=BITRATE_CHOICES,
            group=GROUP_MERGE,
            enabled_when=("merge_format", ("m4a", "mp3")),
        ),
    )

    # ---- 响度处理 ---------------------------------------------------------
    row_balance_enabled: bool = Field(
        default=False,
        description=(
            "逐行生成后、拼接与 BGM 前处理行音频；与成品响度开关独立。"
            "保存设置后需重新处理行音频或合并，旧成品不会自动改变。"
        ),
        json_schema_extra=ui_hints("行级均衡", widget="switch", group=GROUP_LOUDNESS),
    )
    row_balance_mode: Literal["voice", "row"] = Field(
        default="voice",
        description=(
            "音色模式基于全书同音色常态语音中位数，只缩小组间差异，同组统一增益。"
            "逐行模式按每行响度温和修正，带非中性情绪或表达标注的行仍用音色基线。"
        ),
        json_schema_extra=ui_hints(
            "均衡模式",
            widget="select",
            choices=ROW_BALANCE_MODE_CHOICES,
            group=GROUP_LOUDNESS,
            enabled_when=("row_balance_enabled", "true"),
        ),
    )
    row_balance_strength: int = Field(
        default=50,
        ge=0,
        le=100,
        description="缩小响度差异的强度；0 不修正，100 按所选模式完整修正，仍受增益与峰值限制。",
        json_schema_extra=ui_hints(
            "均衡强度（%）",
            widget="number",
            group=GROUP_LOUDNESS,
            enabled_when=("row_balance_enabled", "true"),
        ),
    )
    row_balance_max_gain_db: float = Field(
        default=3,
        ge=0,
        le=12,
        description="每行提高或降低的最大幅度；不自动压缩或限峰，峰值余量不足时仅减少提升量。",
        json_schema_extra=ui_hints(
            "最大调整幅度（dB）",
            widget="number",
            group=GROUP_LOUDNESS,
            enabled_when=("row_balance_enabled", "true"),
        ),
    )
    master_loudness_enabled: bool = Field(
        default=False,
        description=(
            "拼接与 BGM 后处理成品响度；与行级均衡开关独立。"
            "不自动压缩或限峰，峰值余量不足时降低增益并提示目标未达到。"
        ),
        json_schema_extra=ui_hints("成品响度", widget="switch", group=GROUP_LOUDNESS),
    )
    master_loudness_preset: Literal["podcast", "acx", "custom"] = Field(
        default="podcast",
        description=(
            "播客以单声道 −19 LUFS 为目标；自定义使用所填 LUFS。"
            "ACX 仅作 RMS −20.5 dBFS 与峰值参考，不验证底噪和完整编码合规。"
        ),
        json_schema_extra=ui_hints(
            "成品响度预设",
            widget="select",
            choices=MASTER_LOUDNESS_PRESET_CHOICES,
            group=GROUP_LOUDNESS,
            enabled_when=("master_loudness_enabled", "true"),
        ),
    )
    master_loudness_target: float = Field(
        default=-19,
        ge=-36,
        le=-9,
        description="仅自定义预设使用此 LUFS 目标；受峰值余量限制，实际响度可能低于目标。",
        json_schema_extra=ui_hints(
            "目标响度（LUFS）",
            widget="number",
            group=GROUP_LOUDNESS,
            enabled_when=("master_loudness_preset", "custom"),
        ),
    )
    master_true_peak_db: float = Field(
        default=-3,
        ge=-9,
        le=-1,
        description=(
            "成品真峰值上限；通过降低增益保留余量，不自动限峰。"
            "ACX 预设的有效峰值上限不高于 −3 dBTP；界面固定为 −3 dBTP。"
        ),
        json_schema_extra=ui_hints(
            "真峰值上限（dBTP）",
            widget="number",
            group=GROUP_LOUDNESS,
            enabled_when=("master_loudness_enabled", "true"),
        ),
    )

    # ---- 字幕 -------------------------------------------------------------
    subtitle_srt: bool = Field(
        default=True,
        description="章节合并时生成 SRT 字幕。",
        json_schema_extra=ui_hints("生成 SRT 字幕", widget="switch", group=GROUP_SUBTITLE),
    )
    subtitle_lrc: bool = Field(
        default=True,
        description="章节合并时生成 LRC 歌词。",
        json_schema_extra=ui_hints("生成 LRC 字幕", widget="switch", group=GROUP_SUBTITLE),
    )
    subtitle_fit: bool = Field(
        default=False,
        description=(
            "仅字幕项目：某行音频长过它原字幕时隙时，按需加速把它贴回去；"
            "关掉则顺延（保音质，但后面的字幕会整体后移）。变速会改变语气节奏，默认关。"
        ),
        json_schema_extra=ui_hints("变速贴齐时隙", widget="switch", group=GROUP_SUBTITLE),
    )
    subtitle_fit_max: float = Field(
        default=1.3,
        ge=1.0,
        le=2.0,
        description="贴齐时最多加速多少倍（1.0 = 不变速）；到上限仍贴不进去的行照旧顺延。",
        json_schema_extra=ui_hints(
            "最大加速倍率",
            widget="number",
            group=GROUP_SUBTITLE,
            enabled_when=("subtitle_fit", "true"),
        ),
    )

    # ---- 背景音乐 ---------------------------------------------------------
    bgm_enabled: bool = Field(
        default=False,
        description="章节合并时另出一版混入 BGM 的成品；原声版仍保留。",
        json_schema_extra=ui_hints("叠加背景音乐", widget="switch", group=GROUP_BGM),
    )
    bgm_folder: str = Field(
        default="",
        description=(
            "BGM 文件夹；支持 mp3 / wav / flac / m4a / ogg。"
            "每次合并随机抽曲，一曲放完接下一曲，不放单曲循环。"
        ),
        json_schema_extra=ui_hints("BGM 文件夹", group=GROUP_BGM),
    )
    bgm_gain_db: float = Field(
        default=-20.0,
        ge=-60.0,
        le=0.0,
        description=(
            "BGM 相对人声的增益。开启音量标准化后，-20 dB 约等于旧脚本 -35 dBFS "
            "的绝对电平（比人声低约 15 dB）；低于 -30 基本听不见。"
        ),
        json_schema_extra=ui_hints("BGM 音量（dB）", widget="number", group=GROUP_BGM),
    )
    bgm_normalize: bool = Field(
        default=True,
        description=(
            "先把 BGM 拉到统一响度（EBU R128），再按「BGM 音量」混入；"
            "关闭后按曲目原始音量叠加，不同曲目大小不一致。"
        ),
        json_schema_extra=ui_hints("音量标准化", widget="switch", group=GROUP_BGM),
    )
