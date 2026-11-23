"""BreezeTTS 2 引擎插件——**一台跑在本机的 zero-shot 语音合成服务**。

它声明自己是 :data:`~app.engines.base.KIND_LOCAL`：请求不出这台机器、没有密钥、
没有计费。左栏出现一个 BreezeTTS 2 条目，界面照它声明的能力渲染。

**它接的是哪一层**：audio.cpp 推理服务（``audiocpp_server.exe``，OpenAI 兼容端点）。
**不是**整合包外面那层加密的 FastAPI 网关（``main.cp312-win_amd64.pyd``，7863 端口）
——那一层只是网页 UI + 音色库 + ASR + 降噪，推理能力与字段它一样不少地转发给下面这一层，
而下面这一层是开源的、可独立启动的。插件直接打下面那一层：少一个必须活着的加密进程，
字段也全部可控（网关会吞掉 ``depth_temperature`` / ``top_k`` / ``top_p`` / ``max_tokens``
这些它自己界面上没有的格子）。

**它有两条路，这一点决定了下面几乎每一处取舍**：

1. **克隆 / 引导**（`reference_audio` + `reference_text` + `instruction`）——
   拿一段参考音频去换它的嗓子，再让 ``instruction`` 微调语气。库里本地音色走的就是这条。
2. **音色设计**（只有 `text` + `instruction`）——没有参考音频，**一句话描述出一个音色**。
   官方管这叫 Voice Design。

所以 :meth:`BreezeTtsEngine.synthesis_spec` 的 ``requires`` 是**空元组**，而不是
``reference_audio``：写死成后者等于把第二条路整个抹掉（上游会收到一句
"必须提供参考音频"），而"缺料"的判断在这家有两条路，静态的 required 元组表达不了。
它由 :func:`_guard` 在装配时判——那条守卫给出的说法比通用那句更具体。

**最要注意的一条**（本插件存在的理由之一）：**「引导指令」必须放进请求的 ``options``**
（``options.instruction``）——摆在上层的 ``instruction`` 会被引擎**静默忽略**：请求照样
200、声音照样出，只是那句话一个字都没进去，而听感上的差别没人能一眼看出来。这一脚
踩过（上一轮整个"指令不生效"的结论都是它造成的），实测对照与那件事的来龙去脉记在
:mod:`app.engines.builtin.breeze_tts.mapping` 顶部。

**CFG（``options.guidance_scale``）的默认值看这次有没有引导**：有引导指令（含由情绪
转写出来的那句）时按官方推荐的 4 发，没有引导时发 1。官方 README 的原话是
``Use --cfg-scale 4 to strengthen instruction-following``，而 1 是引擎自己的中性
默认——官方只在 4 这一档给了"加强"的说法。所以"有引导却发 1"很可能让那句话白写，
"没引导却发 4"则只是白跑一遍无条件分支。上游明确给了 ``guidance_scale`` 时仍以它
为准（0.125 → 1 是正当要求）。见 :func:`resolve_guidance_scale`。

**第二条要注意的**：参考音频**走 base64，不走路径**。引擎在 Windows 上会把路径转成本地
代码页，路径里有中文就直接失败（实测：同一个文件，纯 ASCII 路径 200、中文目录 500）——
而中文目录在这儿是常态。整合包外层那个网关本来就走 base64，这里跟上。
细节与实测对照表见 :func:`app.engines.builtin.breeze_tts.client.voice_ref_payload`。

**不做的事**：不内置任何音色、不在插件里做音频转码（只声明参考音频要求，由核心在发送前
按声明自动转换）、不猜错误原因（只报引擎原话）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.domain.emotion_instruction import EMOTION_INSTRUCTIONS, emotion_instruction
from app.domain.marks import render_marks
from app.engines.base import (
    KIND_LOCAL,
    Capabilities,
    CloneRequest,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
    VoiceMeta,
    param_label,
)
from app.engines.builtin.breeze_tts import client
from app.engines.builtin.breeze_tts.mapping import MAPPING, MARK_MAP, MARK_MAP_EN
from app.engines.builtin.breeze_tts.presets import load_presets
from app.engines.builtin.breeze_tts.settings import (
    DEFAULT_GUIDANCE_SCALE,
    BreezeTtsSettings,
)
from app.engines.mapping import EngineMapping, apply_mapping
from app.engines.seed import resolve_seed as _resolve_seed
from app.infra.loghub import CHANNEL

__all__ = [
    "EMOTION_INSTRUCTIONS",
    "RANDOM_SEED",
    "BreezeTtsEngine",
    "emotion_instruction",
    "mark_map_for",
    "resolve_guidance_scale",
]


#: 种子写成负数（约定写作 ``-1``）= **随机**：由插件抽一个，而不是引擎抽。
#:
#: 这个约定**不是这一层的**，是整合包网页 UI 那一格写着「种子 seed（-1 随机）」：
#: 它在那层网关里被抽成一个具体的数，再随响应头 ``X-Seed`` 回给界面。而下面这一层
#: （audio.cpp）**不收负数**——实测 ``seed=-1`` 回 500 ``seed must be an unsigned
#: integer``。所以「-1 = 随机」这件事得有人在中间做掉，插件就是那个"中间"。
#:
#: **顺带说清另一半事实**：引擎自己的默认（省略 seed）是 **0，定死的**——实测同一条
#: 文本连发三次（省略 / 省略 / 明确写 0），三份 wav 逐字节相同。也就是说"每次都不一样"
#: 在这一层不会自己发生，要么换种子，要么写 -1 让它抽。
#:
#: **插件这边的默认就是 -1（随机）**：开箱即用的行为是每次都不一样；要复现同一段
#: 音频，再去引擎配置里写一个固定值。
RANDOM_SEED = -1

#: 中文字符：**用来决定正文的指令写哪一套**（中文方括号 / 英文圆括号，见 :func:`mark_map_for`）。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_LATIN = re.compile(r"[A-Za-z]")

#: 上游给了、但这一家**明说没有落点**的那几项 → 原因（面板映射表的「说明」列）。
#:
#: **收哪几项进来有一条判据：只报「用户表达了意图、而这一家做不到」的。** 于是：
#:
#: - ``voice_id`` **不在这里**。它是「走哪一条音色」的身份字段，**每一条请求都带着它**
#:   （规范里是必填），报它等于每一条回话都添一句废话；面板那一行由 ``_TABLE_ONLY`` 补。
#: - ``format`` / ``sample_rate`` / ``channels`` / ``bitrate`` **也不在**。
#:   前者的坏值由 ``synthesis_spec().formats`` 在发出去之前就挡下了（那句报错更早、
#:   更准）；后三项是这一家的**恒定输出**（实测 24kHz 单声道 wav），
#:   不该把"语速没生效"这种真正要紧的话淹掉。
_UNSUPPORTED: dict[str, str] = {
    "speed": "只能用引导指令描述，不是精确倍速",
    "volume": "本引擎无对应参数",
    "pitch": "本引擎无对应参数",
    "lead_silence": "要留空只能在合成之后拼接",
    "lag_silence": "要留空只能在合成之后拼接",
    "language": "双语单模型，按正文判断",
    "reference_language": "本引擎无对应参数",
}

#: 只在面板映射表里出现的说明（这些项不会随请求报「未发送」：不是每一条都值得说）。
_TABLE_ONLY: dict[str, str] = {
    "voice_id": "以参考音频或设计描述代表音色，没有平台音色 ID。",
    "format": "输出固定为 WAV。",
    "sample_rate": "输出参数固定，不可指定。",
    "bitrate": "输出参数固定，不可指定。",
    "channels": "输出参数固定，不可指定。",
}


def _as_settings(cfg: BaseModel) -> BreezeTtsSettings:
    """把框架递过来的配置收窄成本引擎的模型。

    框架按 :meth:`settings_schema` 建的就是这个类，所以这一条只会在**实现被改坏**
    时失败（比如换了设置模型却漏了这一处）——那正是应该立刻炸掉的时候，
    而不是等 ``cfg.host`` 抛一个看不出所以然的 ``AttributeError``。
    """
    assert isinstance(cfg, BreezeTtsSettings)  # 见上：这是实现错误的哨兵
    return cfg


def mark_map_for(text: str) -> dict[str, str]:
    """按**正文语言**挑一套指令写法。

    官方 README 把两套写法定死了：中文正文用方括号（``[笑]``），英文正文用圆括号
    （``(laugh)``）。统一指令名（``[[笑]]``）是中文的，写成哪种形态是装配时的事——
    所以这里按正文里中文字符与拉丁字母的多少挑一套，而不是让上游去挑。

    判别故意做得**不能更简单**：数一数谁多。正文是中文、夹几个英文单词时仍然是中文那套
    （这正是要的结果），整段英文时才翻过去。空正文（只有指令没有字）落回中文那套——
    反正那时正文里剩下的也只有指令本身。

    :param text: **还没翻译**的正文（里面是 ``[[笑]]`` 那种统一写法）。
    """
    raw = text or ""
    return MARK_MAP if len(_CJK.findall(raw)) >= len(_LATIN.findall(raw)) else MARK_MAP_EN


#: 情绪 → 一句引导指令：表与翻译函数在 :mod:`app.domain.emotion_instruction`，
#: BreezeTTS 2 / CosyVoice3 / DotTTS 共用一份。这里保持原名重导出，旧导入路径不变。


def resolve_guidance_scale(
    cfg: BreezeTtsSettings, requested: Any, *, has_guidance: bool
) -> float:
    """定这次合成用多大的 CFG——**上游给了就用上游的**，没给才看有没有引导。

    三条：

    1. 上游明确给了 ``guidance_scale``（或 ``cfg`` / ``cfg_scale`` 别名）→ 原样用。
       0.125 → 1 是"我就要 1"，那是正当要求，不抬也不警告。
    2. 没给、这次也**没有**引导（既没指令也没情绪）→ 1。CFG 管的是"引导有多强"，
       没有引导时给 4 只是白跑一遍无条件分支。
    3. 没给、这次**有**引导 → 配置里那一格；若那一格 ≤ 1（引擎自己的中性默认，
       官方只在 4 这一档给了"加强指令遵循"的说法），回落到官方推荐的 4
       （``--cfg-scale 4``）。

    第 3 条不是"替用户改值"：它只在**没有明确要求**时决定默认值，而且只在配置的
    值等于"关闭"时回落——有引导却按 1 发，等于让刚写的那句话不起作用。要强制 1，
    在请求里给 ``guidance_scale: 0.125`` 即可，那条会走第 1 条。

    旧版本这里曾经写死"配置里那一格就是默认，与有没有指令无关"；那条在
    ``guidance_scale`` 还是旧默认 1 的机器上会静默发出 1，正是"填了情绪却没效果"
    的来源。
    """
    if requested is not None:
        return float(requested)
    if not has_guidance:
        return 1.0
    configured = float(cfg.guidance_scale)
    return configured if configured > 1.0 else float(DEFAULT_GUIDANCE_SCALE)


def resolve_seed(value: Any) -> tuple[int, str]:
    """定这一次真正发出去的种子（负数 = 随机，抽到的数报回来）。"""
    seed, note = _resolve_seed(value, engine="breeze_tts", ceiling=2**31 - 1)
    if seed is None:
        raise EngineError("种子缺失。")
    return seed, note


def _guard(reference_audio: str, reference_text: str, instruction: str) -> None:
    """两条路都不通时**在这里拦下**——发出去才发现的话，引擎会回一句
    ``must set voice_id, voice_ref, or reference_text``，而那时用户已经等了一轮。

    为什么在装配时判而不是写成 ``synthesis_spec().requires``：这一家"缺料"的样子
    是至少给一条（参考音频和/或引导指令），静态的元组表达不了"至少一个"。

    为什么抛 :class:`EngineError` 而不是 ``ValueError``（映射层缺料抛的是后者）：
    ``ValueError`` 只被 minimax / gpt_sovits 那两条路"提前"用 ``requires`` 挡住过，
    API 层并不认识它——它只会变成 500。这里抛出的正是要**端到端回给用户**的那句话，
    所以走 :class:`EngineError`（api 层原样翻成 400）。
    """
    has_ref = bool(reference_audio.strip())
    has_instruction = bool(instruction.strip())
    if not has_ref and not has_instruction:
        raise EngineError(
            "BreezeTTS 2 需要参考音频或引导指令，本次请求两者均为空。"
            "本地音色请提供参考音频与逐字稿；音色设计请提供引导指令。"
        )
    if has_ref and not reference_text.strip():
        raise EngineError(
            "BreezeTTS 2 使用参考音频克隆时必须提供逐字稿（参考音频的文本内容），"
            "否则引擎将返回 requires reference_text。"
            "请在该音色的样例中填写「说话内容」。"
        )


class BreezeTtsEngine(EnginePlugin):
    """BreezeTTS 2 本机推理服务（audio.cpp 的 OpenAI 兼容端点）。"""

    key = "breeze_tts"
    label = "BreezeTTS 2"
    version = "1.0.0"
    homepage = "https://github.com/breezeblue-ai/breeze-tts"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return BreezeTtsSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 没有清单端点——它的音色就是你的参考音频（见模块顶部）。
            voice_listing=False,
            # zero-shot：参考音频本身就是音色，不需要（也没有）"注册一个音色 id"这一步。
            # **如实说没有**，界面上就不会出现一颗点了才报错的「音色克隆」。
            voice_cloning=False,
            voice_design=True,
            # 引导指令是它的主要控制手段（instruction）；情绪是另一条翻译路径。
            instruction_control=True,
            # 这是它的看家能力：参考音频 + 逐字稿 = 它的"音色"。
            reference_audio=True,
            # 没有情绪参数——语气由 instruction 与参考音频决定。
            # 别声明成 True：界面会给出一格"情绪参数"，而这一家真正认的是
            # 自然语言指令。请求里的 emotion 会被装配成一句引导语（见
            # emotion_instruction），那是"翻译"，不是"原生支持情绪枚举"。
            emotion_control=False,
            # 长文本由它自己分块（``text_chunk_size`` 默认 600 字符 / ``text_chunk_mode``），
            # 本机推理也没有"超长 = 多花钱"这回事。0 = 不拦。
            max_chars=0,
            # 本机单模型服务：一次只跑一条。开两条不会更快，只会让第二条排队。
            max_concurrency=1,
            # 语言：官方 README 的 Highlights 写的是 Bilingual Support（英语与中文，
            # 单模型），没有第三方语言，也没有方言与语言参数——正文是什么语种它就按
            # 什么念。**跨语种克隆官方没写、也没实测过，所以那一格留空（不声明）**。
            languages=("zh", "en"),
            language_note="双语单模型，按正文判断语种，没有语言参数",
        )

    async def clone_voice(self, cfg: BaseModel, req: CloneRequest) -> VoiceMeta:
        if not req.design_prompt.strip():
            raise EngineError("BreezeTTS 2 音色设计需要音色描述。")
        raw = req.voice_id.strip()
        voice_id = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", raw).strip("_") or f"design-{uuid.uuid4().hex}"
        return VoiceMeta(
            id=voice_id,
            name=(req.name or voice_id).strip(),
            # 描述写的就是这段设计描述本身：它是这条音色唯一的凭据，改它等于改音色。
            # "每次按描述重新生成、平台不保存音色"是这条通道的常识，写在创建窗口里。
            description=req.design_prompt.strip(),
            category="设计音色",
            tags=("设计",),
        )

    def synthesis_spec(self) -> SynthesisSpec:
        """契约：**不写 ``requires``**（参考音频与引导指令可同时给、至少给一个，
        见模块顶部与 :func:`_guard`）。

        ``formats`` 只有 ``wav``：实测这一家的输出格式恒定（把 ``response_format``
        写成 mp3，回来的仍是一模一样的 wav 字节）。声明 mp3 只会让上游以为可以直接要它。

        ``reference_formats=("wav",)``：**参考音频这一侧也只认 WAV**（它自己的原话是
        ``input is MP3, not WAV; convert it first``）。写在这里之后，中转站会在发出去
        之前把 mp3 那一类转成 wav——而**转了要说**（那件事会出现在结果的说明里）。
        这一条与 ``client.voice_ref_payload`` 里那道检查是一件事的两道岗：那里是
        最后一道（字节都读出来了，认得出不是 RIFF/WAVE），这里是**能补救的那一道**
        （那时还来得及拿 ffmpeg 转）。

        ``needs_transcript=True``：拿参考音频克隆时它必须有那段音频的逐字稿
        （缺了它回 ``requires reference_text``）。**不写进 ``requires``**：那是无条件
        的要求，而这一家有第二条路——只给一句描述（音色设计）就不要逐字稿了。
        """
        return SynthesisSpec(
            requires=(),
            formats=("wav",),
            reference_formats=("wav",),
            needs_transcript=True,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """面板映射表里的「无落点」原因；``_UNSUPPORTED`` 另管随请求报的那几句。"""
        return {**_TABLE_ONLY, **_UNSUPPORTED}

    def dynamic_mappings(self) -> dict[str, tuple[str, str]]:
        """落点随「有没有引导指令」变的那一格。

        ``emotion`` 没有原生落点，但**不是没有落点**：没有引导指令时它会被写成
        一句引导语发到 ``options.instruction``；已有引导指令时以指令为准（情绪不
        另发）。这条"二选一"静态映射表表达不了，故在这里声明给面板看。
        """
        return {
            "emotion": (
                "options.instruction",
                "没有引导指令时写成一句引导语；有引导指令时以引导指令为准。",
            ),
            "voice_design": (
                "options.instruction",
                "设计描述在前，追加引导指令或情绪；使用设计描述时不发送参考音频。",
            ),
        }

    def mark_map(self) -> dict[str, str]:
        """正文里的语气词标签：官方 README 那四条 + 整合包给的两条（中文写法那套）。

        键**全是统一写法里的中文名**（``喘气`` 而不是 ``gasp``——后者是同一个概念的
        英文写法，不是另一个标签）。哪些进「通用」、哪些算这一家独有**由核心跨引擎
        算**（``domain.marks.mark_groups``），插件**不标注谁是谁**——那条规则随别的
        引擎变，写在插件里就成了 N 份要同步的抄本。

        不在表里的标签在装配时被**从正文里删掉**（见 :meth:`prepare_text` 与
        映射表的「说明」列）。

        这里发的是**中文那套**：它是这套统一写法（``[[笑]]``）的默认形态，
        而正文到底是中文还是英文由装配时按正文判（英文正文换成 ``(laugh)``）。
        两套的**键逐条相同**，所以"这一家认哪几条"与正文语言无关。
        """
        return MARK_MAP

    def prepare_text(self, text: str) -> str:
        """写法随正文语言变，所以这个方法要重写：中文 ``[笑]`` / 英文 ``(laugh)``。

        判语言的尺子见 :func:`mark_map_for`，其余与基类同一档（不认的删掉）。
        """
        return render_marks(text, mark_map_for(text))

    def field_presets(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """「引导指令」那一格的现成说法——**读插件目录下的 ``instructions.json``**。

        为什么不写在代码里：这一格是"改一句话、删一条、加几条"的那类东西，数据文件
        改起来不用碰 Python（也不怕把插件改崩）。加载器与那份文件的格式说明见
        :mod:`app.engines.builtin.breeze_tts.presets`。

        **键用的是规范里那一格的名字**（``instruction``）：前端照它把候选挂到那一格上，
        而"这一格叫什么"是规范的事（``domain/request_spec.py``），不是这一家的事。
        """
        return {"instruction": load_presets()}

    # 本机服务：地址有默认值、不需要凭据，**永远就绪**。
    # 不去探端口——readiness 是同步的、会被左栏每刷一次就调一遍，在这里发网络请求
    # 既拖慢界面，又会把"服务没起"变成一个每几秒闪一次的角标，而它真正需要被说出口
    # 的时刻是**用户按下合成那一下**（那是 client 的事）。

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: BreezeTtsSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``/v1/audio/speech`` 请求体（只要请求体的场合用这个）。

        **"被丢掉的那几项"那句话要用** :meth:`build_body_with_notes` 才拿得到
        ——只丢不说是最坏的一种，所以真正发请求的那条路（:meth:`synthesize`）
        走的是后者。
        """
        return self.build_body_with_notes(cfg, req)[0]

    def build_body_with_notes(
        self, cfg: BreezeTtsSettings, req: SynthesisRequest
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """装配请求体，**并带回要对用户说的话**。

        顺序：先按 :data:`MAPPING` 落**上游给的**值，再补**配置里的默认值**。
        顺序不能反——反了的话配置会覆盖上游的明确要求（"上游说了 4，却被这台引擎
        自己的默认盖掉"）。

        **正文里的语气词标签已由网关换成本家的写法**（中文 ``[笑]`` / 英文
        ``(laugh)``，见 :meth:`prepare_text`）；不认的整段删掉，这里收到的是干净的正文。

        **请求体分两层**（理由见 :data:`MAPPING` 与模块顶部）：顶层摆
        ``input`` / ``voice_ref`` / ``reference_text`` / ``seed``，其余——**包括引导指令**
        ——全在 ``options`` 里。下面凡是往 options 写的地方都走同一个句柄：这一格
        **平铺到顶层不会报错，只会静默失效**，而那正是这台引擎最坑的一处。
        """
        values = dict(req.as_values())
        raw_text = str(values.get("text") or "")

        dropped: list[str] = []
        body = apply_mapping(MAPPING, values, dropped)
        # 映射表按点号路径已经把 ``options`` 那一层建出来了（里面至少有一个落点）。
        # 仍然走 setdefault：不假设"上游一定给过至少一个 options 里的值"。
        options: dict[str, Any] = body.setdefault("options", {})

        # 模型 id 每次都要带（它是配置，不是"上游给不给"的事）。
        body["model"] = cfg.model

        # ---- 引导指令：有明确指令就用它；没有才把情绪写成一句引导语 ----
        # 上游可能走别名（``speaker`` / ``instruct``），映射表已经把三个名字都落到
        # ``options.instruction``；这里再取一次原值，是为了**配置里那一格也能补上来**
        # ——配置不是"上游的字段"，它进不了映射表。
        #
        # **优先级是用户点名的**：有引导指令就发它，没有才用情绪的值。两者都没有时
        # 整个不发，让引擎用它自己的默认引导语（``Speak clearly and naturally.``）；
        # 发空串是另一回事——那是"要求一句空指令"。
        # 引导指令的旧写法（speaker / instruct）已由网关并成 ``instruction``。
        explicit = str(values.get("instruction") or "").strip() or cfg.instruction.strip()
        emotion = str(values.get("emotion") or "").strip()
        instruction = explicit
        if not instruction and emotion:
            instruction = emotion_instruction(emotion, raw_text)
            if instruction:
                CHANNEL.info(
                    f"情绪「{emotion}」按引导指令发送：{instruction}",
                    source="engine",
                    engine="breeze_tts",
                )
        design = str(values.get("voice_design") or "").strip()
        if design:
            instruction = f"{design}\n{instruction}" if instruction else design
            body.pop("voice_ref", None)
            body.pop("reference_text", None)
        if instruction:
            options["instruction"] = instruction
        else:
            options.pop("instruction", None)

        # ---- 守卫：两条路都不通就在**发出去之前**拦下 ----
        # 覆盖上游可能用别名给的那几个键（装配用的 ``reference_audio`` 是规范里的正名）。
        # **只把"明确写下的引导指令"交给守卫**：情绪是语气，不是音色描述；只有情绪、
        # 既没有参考音频也没有指令的请求仍该拦下，否则它会替用户凭空设计一个音色。
        _guard(
            str(body.get("voice_ref") or ""),
            str(body.get("reference_text") or ""),
            design or explicit,
        )

        # ---- 参考音频：路径 → base64 ----
        # **放在守卫之后**：上面那条守卫判的是"有没有给"，用的是人写的路径；
        # 这一步才是"把这段音频变成引擎要的形状"。为什么不发路径见
        # ``client.voice_ref_payload``——引擎在 Windows 上会把路径转成本地代码页，
        # 中文目录直接失败（实测）。
        if body.get("voice_ref"):
            body["voice_ref"] = client.voice_ref_payload(str(body["voice_ref"]), label=self.label)

        # ---- CFG：有引导时默认 4，没有引导时 1（见 resolve_guidance_scale） ----
        # ``apply_mapping`` 只在"上游真的给了"时才会写进去，所以这里 ``.get``
        # 拿到 ``None`` 就是"上游没提"——它与"上游提了 1"是两回事，不能合并。
        options["guidance_scale"] = resolve_guidance_scale(
            cfg, options.get("guidance_scale"), has_guidance=bool(instruction)
        )

        # ---- 剩下的采样与分块：上游没给就补配置里的 ----
        options.setdefault("temperature", cfg.temperature)
        options.setdefault("depth_temperature", cfg.depth_temperature)
        options.setdefault("top_k", cfg.top_k)
        options.setdefault("top_p", cfg.top_p)
        options.setdefault("max_tokens", cfg.max_tokens)
        options.setdefault("text_chunk_size", cfg.text_chunk_size)
        options.setdefault("text_chunk_mode", cfg.text_chunk_mode)

        # ---- 种子：负数 = 随机（**这一步只有插件做得来**，引擎不收负数） ----
        seed, seed_note = resolve_seed(body.get("seed", cfg.seed))
        body["seed"] = seed

        return body, self._notes_for((seed_note,) if seed_note else (), values)

    @staticmethod
    def _notes_for(leading: tuple[str, ...], values: Mapping[str, Any]) -> tuple[str, ...]:
        """要对用户说的那几句（种子那句说明 + 这一次被丢掉的那几项）。

        **只丢不说是最坏的一种**：声音照样出，用户却以为自己要的语速生效了。
        所以每一格都要说清"这一个参数没发出去"。
        """
        notes: list[str] = list(leading)
        ignored = [
            param_label(key)
            for key in _UNSUPPORTED
            if key not in _MAPPED_SOURCES and _provided(values.get(key))
        ]
        if ignored:
            notes.append("未发送：" + "、".join(ignored) + "。")
        return tuple(notes)

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        body, notes = self.build_body_with_notes(settings, req)
        result = await client.synthesize(settings, body, label=self.label)
        # ``notes`` 在这一层的用途是"这一次哪里没全量生效"——原样交给调用方，
        # 中转站不替它解释（与 minimax 那条同一条口径）。
        return SynthesisResult(
            audio=result.audio,
            format=result.format,
            cost_note=result.cost_note,
            notes=notes,
            subtitle_url=result.subtitle_url,
        )


#: 映射表里出现过的那些**规范参数名**——``_UNSUPPORTED`` 里若有谁其实是有落点的，
#: 就不该在"没发出去"那句话里出现。放在类后面是为了让它读起来是"这张表的下游"。
_MAPPED_SOURCES: frozenset[str] = frozenset(p.source for p in MAPPING.params)


def _provided(value: Any) -> bool:
    """上游**真的给了**这一项吗——空串与 ``None`` 都是"没要求"。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
