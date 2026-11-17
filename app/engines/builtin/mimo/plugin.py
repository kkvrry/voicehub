"""MiMo 引擎插件——**小米的云端语音合成服务**（要密钥、要联网、按量计费）。

契约来自官方文档（``mimo.mi.com/docs``，OpenAI 兼容的 ``POST /v1/chat/completions``）。
与别家**根本不同**的两处，决定了下面几乎每一处取舍：

1. **要念的台词在 ``messages`` 里 ``role=assistant`` 那条**，``role=user`` 那条是
   给模型的**风格指令**（"语速慢一点，像纪录片旁白"），不进语音。官方明说目标文本
   不可放在 user 消息内——位置反了的症状是"用户听见自己的指令被念出来"，
   而请求照样 200。
2. **音色不叫 ``voice_id``，叫 ``audio.voice``**，且它有两种含义：预置音色那一路
   收音色 id（9 个，见 :mod:`voices`），复刻那一路收**音频样本的 base64**
   （``data:audio/mpeg;base64,...``）。设计那一路**整个省略这个字段**。
   走哪一条由请求自己的形状决定——**所以没有 ``model`` 配置栏**（见 :mod:`mapping` 顶部）。

**音色设计（voicedesign）是第三条路**：请求带 ``voice_design``（一句音色描述）时走
``mimo-v2.5-tts-voicedesign``，描述落在 user 消息里、不进语音。平台不返回音色 id，
也不保存音色：描述随每条请求发出，每次现场设计，同一描述不保证两次一致。项目把描述
挂在库内音色上（``payload.design_prompt``），使它可被反复指定；这不等于平台音色固定。

**情绪不是参数，是正文开头的一对括号**：官方叫「整体风格标签」，写法是
``(开心)待合成内容``（半角 / 全角圆括号、方括号都认，一条括号里可写多个风格）。
它必须落在 ``role=assistant`` 的 content 里，与台词同一格——所以
:func:`_with_emotion` 在装配时把情绪拼到正文最前面，而不是另发一个字段。

**认证头两份一起发**（``Authorization`` 与 ``api-key``）：官方 Python 示例走
``openai`` SDK 发前者，文档页与社区文章写后者，两处不一致，少发一个可能 401。
理由与实测背景见 :func:`app.engines.builtin.mimo.client.headers`。

不做的事：不拉音色清单（官方没有列表接口，9 个预置音色写死）、不猜错误原因
（只报平台原话）、不替上游决定参数转换（转换器在 :mod:`app.engines.mapping`）。
"""

from __future__ import annotations

import re
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
    UiSection,
    VoiceList,
    VoiceMeta,
    param_label,
)
from app.engines.builtin.mimo import client
from app.engines.builtin.mimo.mapping import (
    AUDIO_FORMATS,
    EMOTION_MAP,
    MAPPING,
    MARK_MAP,
    MARK_PASSTHROUGH,
    MODEL_CLONE,
    MODEL_DESIGN,
    MODEL_PRESET,
)
from app.engines.builtin.mimo.settings import MimoSettings
from app.engines.builtin.mimo.voices import PRESET_VOICES
from app.engines.mapping import EngineMapping, apply_mapping
from app.engines.presets import load_presets

__all__ = ["PRESETS_PATH", "MimoEngine"]

#: 「引导指令」的现成说法（官方例句 + 同形状整理的几条）——数据文件，改它不用碰代码。
PRESETS_PATH = Path(__file__).resolve().parent / "instructions.json"

#: 上游给了、但这一家**明说没有落点**的那几项 → 原因（面板映射表的「说明」列）。
#:
#: **收哪几项进来有一条判据：只报「用户表达了意图、而这一家做不到」的。** 于是：
#:
#: - ``voice_id`` **不在这里**：复刻那一路它确实不参与，但那是"走哪条路"的身份字段，
#:   报它等于每次库桥请求都添一句废话——模型切换那句已经说清了。
#: - ``reference_text`` **也不在**：复刻不需要逐字稿，而库桥请求里它由网关自动补齐
#:   （本地音色的主样例说话内容），每请求必现——报它只会把真正要紧的话淹掉。
_UNSUPPORTED: dict[str, str] = {
    "speed": "只能用引导指令描述，不是精确倍速",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "guidance_scale": "本引擎没有引导强度档位",
    "seed": "本引擎无对应参数",
    "lead_silence": "要留空只能在合成之后拼接",
    "lag_silence": "要留空只能在合成之后拼接",
    "language": "预置音色自带语言",
    "reference_language": "本引擎无对应参数",
    "sample_rate": "输出固定为 24 kHz 单声道",
    "bitrate": "输出固定为 24 kHz 单声道",
    "channels": "输出固定为 24 kHz 单声道",
}

#: 只在面板映射表里出现的说明（不随请求报：复刻不需要逐字稿是常驻事实）。
_TABLE_ONLY: dict[str, str] = {
    "reference_text": "复刻不需要逐字稿。",
}


def _as_settings(cfg: BaseModel) -> MimoSettings:
    """把框架递过来的配置收窄成本引擎的模型。

    框架按 :meth:`settings_schema` 建的就是这个类，所以这一条只会在**实现被改坏**
    时失败（比如换了设置模型却漏了这一处）——那正是应该立刻炸掉的时候，
    而不是等 ``cfg.host`` 抛一个看不出所以然的 ``AttributeError``。
    """
    assert isinstance(cfg, MimoSettings)  # 见上：这是实现错误的哨兵
    return cfg


#: 正文开头那个「整体风格」标签：``(开心)`` / ``（温柔）`` / ``[悲伤]``。
#:
#: 官方文档把三种括号都列为可用（半角圆括号、全角圆括号、方括号），所以合并已有
#: 标签时三种都要认——只认圆括号的话，``[开心]话。`` 会变成 ``(平静)[开心]话。``，
#: 同一段开头挂两个整体风格标签（第二个虽然多半也能当细粒度标签用，但那不是本意）。
_STYLE_TAG = re.compile(r"^\s*[（(\[]([^）)\]]*)[）)\]]")


def _with_emotion(text: str, emotion: str) -> str:
    """把 VoiceHub 选的情绪翻成 MiMo 的「整体风格」，挂到正文最前面。

    官方原文：``在目标文本开头添加 (风格) 标签，即可指定语音的发音风格。支持同时
    设置多种风格，将多个风格名称置于同一对括号内，分隔符不限。`` 并且**所有标签
    控制内容都放在 ``role=assistant`` 的 content 里**——这正是这里改的那条消息。

    正文开头已经有风格标签时并进同一对括号（官方允许一条括号里写多个风格）；
    再开一对括号属于没验证过的写法，不赌。
    """
    label = EMOTION_MAP.get(emotion, emotion)
    match = _STYLE_TAG.match(text or "")
    if match:
        return f"({label}，{match.group(1)})" + text[match.end():]
    return f"({label}){text or ''}"


def _provided(value: Any) -> bool:
    """上游**真的给了**这一项吗——空串与 ``None`` 都是"没要求"。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


class MimoEngine(EnginePlugin):
    """MiMo 语音合成（小米 MiMo 开放平台，OpenAI 兼容端点）。"""

    key = "mimo"
    label = "MiMo"
    version = "1.0.0"
    homepage = "https://mimo.mi.com"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return MimoSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_CLOUD,
            # 有清单——**静态的那 9 个**（官方没有列表接口，见 voices 模块顶部）。
            voice_listing=True,
            # 复刻走 ``mimo-v2.5-tts-voiceclone``：样本**每次请求随音频重发**，
            # 平台没有“注册一个音色 id”这一步。界面上的「音色克隆」因此是
            # “存一段样本当这条音色”，不是“换一个 id 回来”。
            sample_cloning=True,
            sample_formats=("mp3", "wav"),
            # 描述即音色：平台不返回 id，每次合成按描述现场设计。
            voice_design=True,
            # 风格指令写进 user 消息（``instruction``），是它的原生控制手段。
            instruction_control=True,
            # 复刻那一路吃参考音频（mp3 / wav，base64 随请求发）。
            reference_audio=True,
            # 情绪落到正文开头的整体风格标签（``(开心)``），算有情绪控制。
            emotion_control=True,
            # 官方「模型规格」写明上下文 **8K tokens**（输入上限与最大输出都是 8K），
            # 但没给字符数；这一家**不自己分块**，超了就是上游报错，所以这里必须填。
            # 8K token 按中文 1 字 ≈ 1–1.5 token 折算约 5400–8000 字，再留出
            # user 那条风格指令的位置，取 5000（偏保守：宁可早拒，不要发出去才失败）。
            max_chars=5_000,
            # 语言：这一家的语言是音色自带的（9 个预置音色里中文 4 个、英文 4 个，
            # 默认音色随集群）。平台没给语言清单，跨语言也没写——所以只声明预置音色
            # 覆盖的这两种，跨语言那一格留空。
            languages=("zh", "en"),
            language_note=(
                "预置音色自带语言（中文 4 个、英文 4 个）；"
                "复刻与设计两路按样本与描述决定，平台没给语言清单"
            ),
        )

    def ui_sections(self) -> tuple[UiSection, ...]:
        """在默认页签上补事实：克隆与设计各走哪个模型（由插件声明，前端只渲染）。"""
        sections: list[UiSection] = []
        for section in super().ui_sections():
            if section.key == "clone":
                sections.append(
                    UiSection(
                        section.key,
                        section.label,
                        "从音色库选一段样本；成功后它就是这条音色的参考样本。",
                        {"model": MODEL_CLONE},
                    )
                )
            elif section.key == "design":
                sections.append(
                    UiSection(
                        section.key,
                        section.label,
                        "填一句音色描述；成功后它就是这条音色的设计凭据。",
                        {"model": MODEL_DESIGN},
                    )
                )
            else:
                sections.append(section)
        return tuple(sections)

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        """MiMo 没有平台登记步骤：音色 = 一段样本，或一句描述。

        这里只产出库内 id / 名字；样本落盘与 ``payload`` 登记由 usecase 做
        （它才拿得到 archive）。
        """
        design = (req.design_prompt or "").strip()
        if not design and not (req.audio_path or "").strip():
            raise EngineError("音色复刻需要一段参考音频（mp3 / wav）。")
        raw = (req.voice_id or req.name or "").strip()
        fallback = "design" if design else "clone"
        voice_id = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", raw).strip("_") or fallback
        if design:
            return VoiceMeta(
                id=voice_id,
                name=(req.name or voice_id).strip(),
                # 描述就是这段设计描述：它是这条音色唯一的凭据，改它等于改音色。
                description=design,
                category="设计音色",
                tags=("设计",),
            )
        return VoiceMeta(
            id=voice_id,
            name=(req.name or voice_id).strip(),
            description=(
                "样本复刻音色：MiMo 没有平台音色 id，每次合成都会带上这段样本，"
                "走 mimo-v2.5-tts-voiceclone。"
            ),
            category="克隆音色",
            tags=("克隆",),
        )

    def synthesis_spec(self) -> SynthesisSpec:
        """契约：**不写 ``requires``**（三条路由请求形状二选一，见模块顶部）。

        - ``formats``：官方那四种（``pcm`` 与 ``pcm16`` 同义，都收）；
        - ``reference_formats=("mp3", "wav")``：复刻样本只认这两种（官方原话）。
          写在这里之后，中转站会在发出去之前把别的容器转成 wav——**转了要说**
          （那件事会出现在结果的说明里）；
        - ``needs_transcript=False``：复刻**不需要**逐字稿，别为它跑一次识别模型；
        - ``voice_design`` 不在规范参数表里：落点由装配阶段决定，
          登记在 ``dynamic_mappings()`` 中，网关才放行。
        """
        return SynthesisSpec(
            requires=(),
            formats=AUDIO_FORMATS,
            reference_formats=("mp3", "wav"),
            needs_transcript=False,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """面板映射表里的「无落点」原因；``_UNSUPPORTED`` 另管随请求报的那几句。"""
        return {**_UNSUPPORTED, **_TABLE_ONLY}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """随请求形状变的那几项：落点在装配时按「预置 / 复刻」两条路决定。"""
        return {
            "text": ("messages[assistant].content", "台词；[[标签]] 转为 (标签)。"),
            "voice_id": ("audio.voice", "预置音色 id；带参考音频时该位置改为样本 data URI。"),
            "reference_audio": ("audio.voice", "复刻样本，按 data URI 发送。"),
            "voice_design": ("messages[user].content", "音色设计描述；带它走设计模型并省略 audio.voice。"),
            "emotion": ("messages[assistant].content", "作为整体风格标签写在台词开头。"),
            "instruction": ("messages[user].content", "风格指令。"),
        }

    def mark_map(self) -> dict[str, str]:
        """正文里的语气词标签：几条常见项 + **认所有自定义标签**（见 :meth:`mark_passthrough`）。

        这一家的标签是中文词加括号（``(笑)``），官方明说"支持不在列表中的风格"。
        所以表只登记写法不同的常见项，其余交给 ``render_marks(passthrough=...)``
        原样转过去（``[[诡异]]`` → ``(诡异)``），不删。
        """
        return MARK_MAP

    def mark_passthrough(self) -> str:
        """表外标签的写法：``(名)``。

        声明了它，"认哪些"才是诚实的：装配时表外标签照样发得出去，跨引擎的「通用」
        也不会因为这一家只登记了六条就把别人的通用项压掉；界面据此在它那一组末尾
        补一格自定义输入。
        """
        return MARK_PASSTHROUGH

    def field_presets(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """「引导指令」那一格的现成说法——**读插件目录下的 ``instructions.json``**。

        内容是官方例句加同形状整理的几条；改数据不用碰 Python（加载器与格式说明见
        :mod:`app.engines.presets`）。键用的是规范里那一格的名字（``instruction``）：
        前端照它把候选挂到那一格上，而"这一格叫什么"是规范的事，不是这一家的事。
        """
        return {"instruction": load_presets(PRESETS_PATH)}

    def readiness(self, cfg: BaseModel) -> str:
        if not getattr(cfg, "configured", False):
            return "还没填 API Key。"
        return ""

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: MimoSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``/v1/chat/completions`` 请求体（只要请求体的场合用这个）。

        **"被丢掉的那几项"那句话要用** :meth:`build_body_with_notes` 才拿得到
        ——只丢不说是最坏的一种，所以真正发请求的那条路（:meth:`synthesize`）
        走的是后者。
        """
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: MimoSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """装配请求体，**并带回要对用户说的话**。

        顺序：先按 :data:`MAPPING` 落**上游给的**值（静态落点只有 ``format``
        那一格），再装配 ``messages`` 与 ``audio``——这两处随模型而变，
        映射表表达不了（数组下标 / 一个键两种含义 / 设计路整个不设这个键），
        理由见 :mod:`mapping` 顶部。

        正文里的语气词标签已由网关换成平台写法（``[[笑]]`` → ``(笑)``，见
        ``EnginePlugin.prepare_text``），情绪照平台的「整体风格」写法挂到正文最前面。
        **带引导指令时情绪已被网关撤掉**（两条风格通道只发一条，见 :mod:`mapping`），
        这里只剩"只有情绪"的那一路。
        """
        values = req.as_values()
        text = str(values.get("text") or "")
        if not text.strip():
            # 网关那边已经拦过一次；这里是给绕过网关的直接调用留的
            # （插件被测试或将来的内部路径直接用）。发一句空台词下去，
            # 平台回的话指不到"文本空了"这件事上。
            #
            # **这一判要在拼情绪标签之前**：先拼 ``(开心)`` 再判空，正文就成了
            # 只有一对括号、没有台词，请求照样发出去。
            raise EngineError("正文不能为空。", code="empty_text")
        emotion = str(values.get("emotion") or "").strip()
        if emotion:
            text = _with_emotion(text, emotion)
        values["text"] = text
        dropped: list[str] = []
        body = apply_mapping(MAPPING, values, dropped)

        # ---- 三条路：设计描述 → 设计；参考音频 → 复刻；否则 → 预置 ----
        # 模型由**请求自己的形状**决定，不由配置决定（理由见 mapping 顶部）。
        design = str(values.get("voice_design") or "").strip()
        sample = str(values.get("reference_audio") or "").strip()
        designing = bool(design)
        cloning = not designing and bool(sample)
        if designing:
            body["model"] = MODEL_DESIGN
        elif cloning:
            body["model"] = MODEL_CLONE
        else:
            body["model"] = MODEL_PRESET

        audio: dict[str, Any] = body.setdefault("audio", {})
        if designing:
            # 官方原话：设计模型不用预置音色、不用克隆样本，audio.voice 整个不传。
            audio.pop("voice", None)
        elif cloning:
            # 参考音频 → data URI（读文件、查容器、查大小，都在发出去之前）。
            audio["voice"] = client.voice_sample_data_uri(sample, label=self.label)
        else:
            voice = str(values.get("voice_id") or "").strip() or cfg.voice
            if not voice:
                raise EngineError(
                    "MiMo 预置音色那一路需要一个音色 id（9 个预置音色之一），"
                    "而这次请求与配置里都是空的。"
                    "要么在请求里带上音色，要么在引擎设置里填一个默认音色。"
                )
            # 不在那 9 个里也照发——平台会自己拒，而它的原话比我们猜的准。
            # （不拦：这一家的音色清单是静态的，平台哪天加了新的，这里不该先拒。）
            audio["voice"] = voice

        # ---- messages：user = 设计描述 / 风格指令（可选），assistant = 台词 ----
        # **位置不能换**：官方明说目标文本不可放在 user 消息内（放反了会被念出来）。
        # 引导指令的旧写法（speaker / instruct）已由网关并成 ``instruction``。
        instruction = str(values.get("instruction") or "").strip()
        messages: list[dict[str, Any]] = []
        if designing:
            # 设计模型的 user 消息必填且不会被朗读：描述在前，引导指令并入同一条。
            content = f"{design}\n{instruction}" if instruction else design
            messages.append({"role": "user", "content": content})
        elif instruction:
            messages.append({"role": "user", "content": instruction})
        messages.append({"role": "assistant", "content": text})
        body["messages"] = messages

        return body, self._notes_for(
            cloning, designing, values, ignored_audio=designing and bool(sample)
        )

    @staticmethod
    def _notes_for(
        cloning: bool,
        designing: bool,
        values: Mapping[str, Any],
        *,
        ignored_audio: bool = False,
    ) -> tuple[str, ...]:
        """要对用户说的那几句（模型切换 + 这一次被丢掉的那几项）。

        **只丢不说是最坏的一种**：声音照样出，用户却以为自己要的语速生效了。
        所以每一格都要说清"这一个参数没发出去"；设计音色的不稳定性属于必须
        提前知道的风险，也写在这里。
        """
        notes: list[str] = []
        if designing:
            notes.append(
                f"按描述即时设计音色（{MODEL_DESIGN}）；平台不保存音色，"
                "每次合成重新设计，同一描述不保证完全一致。"
            )
        elif cloning:
            notes.append(f"按参考音频复刻音色（{MODEL_CLONE}），不使用预置音色。")
        if ignored_audio:
            notes.append("本次带了参考音频，但设计音色不使用它。")
        ignored = [
            param_label(key)
            for key in _UNSUPPORTED
            if key not in _MAPPED_SOURCES and _provided(values.get(key))
        ]
        if ignored:
            notes.append(
                "这些参数 MiMo 没有落点，这次没发出去：" + "、".join(ignored) + "。"
            )
        return tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def list_voices(self, cfg: BaseModel) -> VoiceList:
        """预置音色——**原样返回，不发请求**（官方没有列表接口，见 :mod:`voices`）。

        所以这一路不需要密钥也能看：设置页刚装上、密钥还没填时，下拉里已经有
        那 9 个可以挑。
        """
        return VoiceList(
            items=PRESET_VOICES,
            cost_note="MiMo 当前标注限时免费（官方文档 2026-07），以控制台账单为准。",
        )

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        body, notes = self.build_body_with_notes(settings, req)
        result = await client.synthesize(settings, body, label=self.label)
        if not notes:
            return result
        # 引擎那边也说了话（如果有）时**两条都留**，别用后面这句盖掉前面那句。
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=(*result.notes, *notes),
            subtitle_url=result.subtitle_url,
        )


#: 映射表里出现过的那些**规范参数名**——``_UNSUPPORTED`` 里若有谁其实是有落点的，
#: 就不该在"没发出去"那句话里出现。放在类后面是为了让它读起来是"这张表的下游"。
_MAPPED_SOURCES: frozenset[str] = frozenset(p.source for p in MAPPING.params)
