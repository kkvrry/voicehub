"""IndexTTS2 引擎插件——本机 audio.cpp hub 里的零样本克隆模型。

**参数怎么落**（这一家只有一套情感控制通路）：

- **引导指令 → 情感文本**（``emotion_text`` + ``use_emotion_text=true``）。
  我们的「情绪」标签是兜底：没写引导指令时才用它当情感文本。
- **指令强度 → 情感强度**（``emotion_alpha``）：规范里就是 0–1 的统一刻度，
  这一家的 alpha 也是 0–1，**直接落**（0.5 = 两家的推荐默认）。
  没有情感文本时不发 alpha（没有对象）；有情感文本而没给强度时按 0.5 发。
- **语速 → 时长倍率**（``duration_scale = 1 / speed``）：两家对"快"的定义相反，
  1.2 倍速 → 0.833 的时长。
- **采样默认值 → options**（``temperature`` / ``top_p`` / ``top_k`` /
  ``repetition_penalty`` / ``max_tokens``）：引擎设置里配的那几个默认值，
  **每次请求都落进** ``options``——实测（同 seed）五个都生效，``max_tokens``
  调小直接截断；摆在顶层会被服务端静默忽略。
- **种子**：负数（``-1``）= 随机，插件抽一个 0 ~ 2³²−1 发出去并报回来；
  省略 = 引擎自己随机（它的规格里就是这么写的）。
- **参考音频**：``voice_ref`` 收**本机绝对路径**（实测中文路径可用，与 BreezeTTS 2
  那条 base64 的做法不同）。参考文本 ``reference_text`` 同时发。
- **语气词标签**：这一家一条都不认（:meth:`mark_map` 空表），``[[倒吸气]]`` 这类
  标签由网关在发出前从正文里删掉（``EnginePlugin.prepare_text``），不会被念出来。

没接的项（音量/音高/前后静默/正文语言）在结果说明里逐条说清"这次没发出去"，
不静默丢。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from pydantic import BaseModel

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
from app.engines.builtin.indextts2 import client
from app.engines.builtin.indextts2.settings import IndexTts2Settings
from app.engines.mapping import (
    EngineMapping,
    ParamMapping,
    apply_mapping,
    register_transform,
)
from app.engines.presets import load_presets
from app.infra.loghub import CHANNEL

__all__ = ["PRESETS_PATH", "IndexTts2Engine", "resolve_seed"]

#: 种子上限（audio.cpp 的 seed 是 uint32）。
SEED_CEILING = 2**32 - 1

#: 「引导指令」的现成说法：与 mimo 同一个数据文件形状（改文案不用动 Python）。
PRESETS_PATH = Path(__file__).resolve().parent / "instructions.json"


def _speed_to_duration(value: Any) -> float | None:
    """语速倍率 → 时长倍率（1.2 倍速 = 原时长的 0.833）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return round(1.0 / number, 4)


register_transform("speed_to_duration", _speed_to_duration, label="语速 → 时长倍率")

#: 统一参数 → IndexTTS2 请求体。落点名字来自模型自带的 ``options.request`` 规格。
#:
#: **模型参数必须放进 ``options``**（实测：摆在顶层时服务端 200 但静默忽略——
#: 换情绪文本/强度/时长倍率，输出逐字节相同）。'``seed`` 是例外，它在顶层。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "input", note="要合成的正文。"),
        ParamMapping(
            "reference_audio", "voice_ref", note="参考音频；本机绝对路径。", warning=True
        ),
        ParamMapping("reference_text", "reference_text", note="参考音频的逐字稿。"),
        # 正文语言：两个版本读的地方不同——2.0 认顶层 ``language``（zh/en），2.5 认
        # ``options.language``（auto/zh/en/ja/es/ar）。装配时同一个值两边各放一份。
        # **非中英的正文必须显式给**：2.5 的自动判定只看"有没有汉字"，日语会被判成中文。
        ParamMapping(
            "language",
            "language",
            note="正文语言；顶层与 options 各发一份（2.0 / 2.5 读的位置不同）。",
        ),
        ParamMapping(
            "instruction",
            "options.emotion_text",
            note="情感描述文本，配合指令强度使用。",
        ),
        # 规范里的「指令强度」就是 0–1，与这一家的 emotion_alpha 同刻度，直接落。
        ParamMapping(
            "guidance_scale",
            "options.emotion_alpha",
            transform="number",
            note="情感强度，0–1 同为这一家的刻度。",
        ),
        ParamMapping(
            "speed",
            "options.duration_scale",
            transform="speed_to_duration",
            note="语速倍率换算为时长倍率。",
        ),
    ),
    cautions=(
        "情感控制由「引导指令」与「指令强度」共同生效，需同时提供。",
        "参考音频使用本机绝对路径；服务不在本机时不能直接用本机路径。",
        "模型参数必须位于 options 内；写入顶层会被服务端静默忽略。",
    ),
)

#: 上游给了、这一家没有落点的项 → 可读说法（只在真的给了时出现）。
_UNSUPPORTED: dict[str, str] = {
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "本引擎无对应参数",
    "lag_silence": "本引擎无对应参数",
    "reference_language": "本引擎无对应参数",
}

#: 只在面板映射表里出现的说明（都是这台引擎的恒定量）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频代表音色，没有音色 ID。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出参数固定，不可指定。",
    "channels": "输出参数固定，不可指定。",
}


def resolve_seed(value: Any) -> tuple[int | None, str]:
    """这一次发出去的种子：``-1`` 抽一个、其他原样、空着不发（引擎自己随机）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ""
    try:
        seed = int(value)
    except (TypeError, ValueError):
        raise EngineError(f"种子「{value}」不是整数。") from None
    if seed >= 0:
        value = min(seed, SEED_CEILING)
        return value, f"seed={value}"
    drawn = random.randrange(0, SEED_CEILING + 1)
    CHANNEL.info(
        f"种子写的是 {seed}（= 随机），抽取结果 {drawn}（可用它复现）",
        source="engine",
        engine="indextts2",
    )
    return drawn, f"seed={drawn}"


def _provided(value: Any) -> bool:
    return not (value is None or (isinstance(value, str) and not value.strip()))


class IndexTts2Engine(EnginePlugin):
    """IndexTTS2（audio.cpp hub，OpenAI 兼容端点）。"""

    key = "indextts2"
    label = "IndexTTS2"
    version = "1.0.0"
    homepage = "https://github.com/index-tts/index-tts"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return IndexTts2Settings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            voice_listing=False,
            voice_cloning=False,
            reference_audio=True,
            # 自然语言指令驱动情感表达（映射到 options.emotion_text）。
            instruction_control=True,
            # 情感走"文本 + 强度"两格，都在请求里（见模块顶部）。
            emotion_control=True,
            # 服务端的框架分块默认**关**（整段一次进模型），但模型内部按
            # ``text_chunk_tokens``（1–600，默认 120）切段逐个生成再拼——长文本照样
            # 能发，只是慢。所以没有硬上限；0 = 不拦。
            max_chars=0,
            max_concurrency=1,
            # 语言：这一族两个版本。2.0 只有中英；2.5（同一个 family，按模型配置里的
            # version 选）另加日语、西班牙语、阿拉伯语，并保留了 2.0 的跨语种能力
            # （官方发布说明：keeping the cross-lingual ... capabilities）。
            languages=("zh", "en", "ja", "es", "ar"),
            cross_lingual=True,
            language_note="中文与英语是 2.0 的全部；日语、西班牙语、阿拉伯语要 2.5 的包",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(
            # 零样本克隆：必须有一段参考音频当音色。
            requires=("reference_audio",),
            formats=("wav",),
            reference_formats=("wav",),
            needs_transcript=True,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """面板映射表里的「无落点」原因；``_UNSUPPORTED`` 另管随请求报的那几句。"""
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """映射表表达不了、但确有落点的项。

        - ``seed``：负数在插件里抽样，落点在装配时决定。
        - ``emotion``：没有引导指令时作为情感文本落到同一格；有指令时以指令为准
          （``dual_expression`` 因此为 False——两路共用 ``options.emotion_text``）。
        """
        return {
            "seed": ("seed", "固定种子；-1 表示由核心抽样。"),
            "emotion": ("options.emotion_text", "无引导指令时作为情感文本；有指令时以指令为准。"),
        }

    def field_presets(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """「引导指令」的现成说法——这一格在这里就是**情感描述**（落到 ``emotion_text``）。

        内容读同目录的 ``instructions.json``（与 mimo 同一个加载器）：改文案是数据的事，
        不是代码的事。格式与「只写语气、不写声线」的规矩写在文件自己的 ``note`` 里。
        """
        return {"instruction": load_presets(PRESETS_PATH)}

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: IndexTts2Settings, req: SynthesisRequest) -> dict[str, Any]:
        """只要请求体的场合（与其余几家同一形状）。"""
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: IndexTts2Settings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """统一参数 → ``/v1/audio/speech`` 请求体 + 要对用户说的话。"""
        values = dict(req.as_values())
        body = apply_mapping(MAPPING, values, None)
        body["model"] = cfg.model
        body["input"] = str(values.get("text") or "")

        # ---- 情感文本：引导指令优先，其次「情绪」标签（兜底）；都放进 options ----
        options: dict[str, Any] = body.setdefault("options", {})
        # 正文语言：2.0 读顶层、2.5 读 options，同一个值两边各放一份（见 MAPPING）。
        language = str(body.get("language") or "").strip()
        if language:
            options["language"] = language
        emotion_text = str(options.get("emotion_text") or "").strip()
        if not emotion_text:
            emotion_text = str(req.emotion or "").strip()
        if emotion_text:
            options["emotion_text"] = emotion_text
            options["use_emotion_text"] = True
            # 上游没给强度时按规范的默认 0.5 发（与 BreezeTTS 的 CFG 4 对齐），
            # 别让"默认 0.5"只停在界面上、引擎却按自己的 1.0 跑。
            options.setdefault("emotion_alpha", 0.5)
        else:
            # 没有情感文本，强度项没有对象：不发（引擎默认 1.0 也无所谓）。
            options.pop("emotion_text", None)
            options.pop("emotion_alpha", None)
            options.pop("use_emotion_text", None)
        if not options:
            body.pop("options", None)

        # ---- 采样参数：引擎设置里的默认值，随请求落进 options ----
        # 实测（audio.cpp 18091，固定 seed）：这五个在 options 里都真生效——
        # 改变任一参数输出互不相同，max_tokens 调小直接截断。**摆在顶层会被
        # 静默忽略**（与情感参数同一条纪律）。默认值也照发：日志里那份请求体
        # 才是真正生效的那一份，"没发" 与 "发了默认值" 不是一回事。
        # interval_silence_ms 不在这个 build 的生效集里，设置里没有那一格。
        options = body.setdefault("options", {})
        options["temperature"] = cfg.temperature
        options["top_p"] = cfg.top_p
        options["top_k"] = cfg.top_k
        options["repetition_penalty"] = cfg.repetition_penalty
        options["max_tokens"] = cfg.max_tokens

        # ---- 参考音频：这一家收本机绝对路径 ----
        reference = str(body.get("voice_ref") or "").strip()
        if not reference:
            raise EngineError(
                "IndexTTS2 需要参考音频（音色样例）：它是零样本克隆，没有参考音频就没有音色。"
                "请先在左栏选一条带本地样例的音色。"
            )
        source = Path(reference)
        if not source.is_file():
            raise EngineError(f"IndexTTS2 读不到参考音频：{reference}（文件不存在或已被移动）。")
        body["voice_ref"] = str(source)

        # ---- 种子：-1 = 随机（插件抽），空着 = 引擎自己随机 ----
        seed, seed_note = resolve_seed(values.get("seed"))
        if seed is None:
            body.pop("seed", None)
        else:
            body["seed"] = seed

        # ---- 没落点的那几项：给了就说，不静默丢 ----
        ignored = [
            param_label(key) for key in _UNSUPPORTED if _provided(values.get(key))
        ]
        notes: list[str] = []
        if seed_note:
            notes.append(seed_note)
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        return body, tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        assert isinstance(cfg, IndexTts2Settings)
        body, notes = self.build_body_with_notes(cfg, req)
        result = await client.synthesize(cfg, body, label=self.label)
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )
