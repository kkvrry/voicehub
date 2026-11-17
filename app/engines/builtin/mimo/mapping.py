"""统一参数 → MiMo 请求体：**这台引擎认哪些、不认哪些，全写在明面上**。

VoiceHub 的上游只按 :class:`~app.engines.base.SynthesisRequest` 说话；这里说明
它们落在 MiMo（``POST /v1/chat/completions``）的哪里。这一家的请求体形状与别家
**根本不同**，值得先看一遍再读映射表：

===============  ====================================  ============================
统一参数         MiMo 落点                              备注
===============  ====================================  ============================
``text``         ``messages`` 里 ``role=assistant``      **官方规定，位置不能换**
``instruction``  ``messages`` 里 ``role=user``           风格指令，不进语音
``voice_id``     ``audio.voice``                        仅预置音色那一路
``reference_audio`` ``audio.voice``（``data:`` 前缀）   仅音色复刻那一路
``voice_design``    ``messages`` 里 ``role=user``          仅音色设计那一路
``format``          ``audio.format``                       ``pcm`` 与 ``pcm16`` 同义
===============  ====================================  ============================

**``messages`` 是数组，映射表表达不了**（:class:`~app.engines.mapping.ParamMapping`
的落点是点号路径，塞不进数组下标）。所以请求体由 :meth:`MimoEngine.build_body`
装配，映射表只登记**静态落点**那一格（``format``），其余由插件的
``dynamic_mappings()`` 声明——半张真半张假的表比没有表更糟。

**``emotion`` 与 ``instruction`` 是两条独立的风格通道**：同时给时只发
``instruction``，``emotion`` 由核心撤掉（判据见
:meth:`~app.engines.base.EnginePlugin.dual_expression`）——一处说"愤怒"、一处说
"压低嗓子放慢"，引擎各让一半，读法反而不自然。单独给情绪时照发，写在台词开头。

**三条路，三个模型**（这就是没有 ``model`` 配置栏的原因）：

- 请求带了 ``voice_design`` → ``mimo-v2.5-tts-voicedesign``（user 消息是音色描述，
  **省略 ``audio.voice``**）；
- 否则带了参考音频 → ``mimo-v2.5-tts-voiceclone``（拿音频样本复刻音色）；
- 否则 → ``mimo-v2.5-tts``（用 9 个预置音色之一，见 :mod:`voices`）。

模型由**请求自己的形状**决定，不由配置决定：通道（``own`` / ``library``）已经
替用户选过一遍音色，再摆一格"模型"让人选，等于把确定的事再问一遍——而两处对不上
时（配了预置模型却挑了个本地音色）没有一处会报错。

**音色设计不是固定音色**：平台不返回音色 id，也不提供注册接口；描述随每条请求发出，
平台每次现场设计，同一描述不保证两次完全一致。项目里把描述挂在库内音色上
（``payload.design_prompt``），让它可被反复指定，但这只是"复用同一句描述"。
规范仍要求带 ``voice_id``——设计音色是库里一条普通平台音色，不凿"无音色"的口子。
"""

from __future__ import annotations

from app.engines.mapping import EngineMapping, ParamMapping

__all__ = [
    "AUDIO_FORMATS",
    "EMOTION_MAP",
    "MAPPING",
    "MARK_MAP",
    "MODEL_CLONE",
    "MODEL_DESIGN",
    "MODEL_PRESET",
]

#: 预置音色那一路的模型 id（官方）。
MODEL_PRESET = "mimo-v2.5-tts"
#: 音色复刻那一路的模型 id（官方）。
MODEL_CLONE = "mimo-v2.5-tts-voiceclone"
#: 音色设计那一路的模型 id（官方）。
MODEL_DESIGN = "mimo-v2.5-tts-voicedesign"

#: VoiceHub 的情绪词 → MiMo 的「整体风格」词。
#:
#: 规范词那一组（开心 / 悲伤 / 愤怒 / 恐惧 / 惊讶 / 兴奋 / 委屈 / 平静 / 冷漠）**就是**
#: 官方基础情绪表的用词，逐字相同，不需要翻译——这张表只剩两头：旧词（难过 / 生气 /
#: 害怕 / 中性）与官方不受理、但官方明说"支持自定义风格"的那些（复合情绪、态度
#: 两组原样透传，由 :func:`~app.engines.builtin.mimo.plugin._with_emotion` 的
#: ``get(x, x)`` 兜住）。
EMOTION_MAP: dict[str, str] = {
    "中性": "平静",
    "难过": "悲伤",
    "生气": "愤怒",
    "害怕": "恐惧",
}

#: 接受的输出格式。**与 ``plugin.synthesis_spec().formats`` 是同一张表**
#: （那边从这边取，免得两处各写一遍而对不上）。
#:
#: ``pcm`` 与 ``pcm16`` 官方明说同义（都是 24kHz / 16bit / 单声道裸样），
#: 两个名字都收：上游规范里那一格叫 ``pcm``，而平台文档两个写法都出现过。
AUDIO_FORMATS: tuple[str, ...] = ("wav", "mp3", "pcm", "pcm16")

#: 正文里的语气词标签（:mod:`app.domain.marks` 那套统一写法 → 这一家的原生写法）。
#:
#: 这一家的标签是**中文词加括号**（``(笑)``），官方给了一份推荐风格与
#: 音频标签表，并明说"支持使用不在列表中的风格"。所以本表只登记几条**写法不同**的
#: 常见项；表里没有的由 ``render_marks(passthrough="({name})")`` 原样转过去。
#:
#: 键一律是统一写法里的中文名（``domain.marks.MARKS``），不新起名：
#: 同一条标签在两台引擎上两个名字，「通用」那一组就永远算不出来。
#:
#: **``停顿`` 刻意不在表里**：这一家的标签不带时长参数（官方示例里只有
#: ``(沉默片刻)`` 这种定性说法），而 ``[[停顿:0.4]]`` 的 0.4 秒无处安放——
#: 走兜底会变成 ``(停顿)``，不假装它能精确到秒。
MARK_MAP: dict[str, str] = {
    "笑": "(笑)",
    "轻笑": "(轻笑)",
    "叹气": "(叹气)",
    "咳嗽": "(咳嗽)",
    "喘气": "(喘息)",
    "吸气": "(吸气)",
}

#: 表外标签按什么写：这一家的标签是**中文词加圆括号**，且官方明说支持不在列表中的
#: 风格。所以表只登记几个常见项的写法，其余原样带括号转过去（``[[诡异]]`` → ``(诡异)``）。
#: 唯一产地在这里：装配用它，界面据此给一组自定义输入框（``mark_passthrough()``）。
MARK_PASSTHROUGH = "({name})"

#: 这张表就是「MiMo 要哪几个键」的答案里**静态的那一格**。
#:
#: ``messages`` 是数组、``audio.voice`` 有两种含义（音色 id / 音频样本），
#: 都随模型而变——静态的映射表表达不了，由 ``plugin.build_body`` 装配；那几项
#: 在插件里用 ``dynamic_mappings()`` 声明（面板的映射表照它写落点）。
MAPPING = EngineMapping(
    params=(
        ParamMapping("format", "audio.format", note="输出音频容器。"),
    ),
    # 本层不做流式。**显式写 false 而不是省略**：平台默认就是 false，但把
    # "我们没接流式"这件事写进请求体，将来接了也不会忘了改这里。
    constants={"stream": False},
    cautions=(
        "台词放在 assistant 消息，风格指令放在 user 消息；位置不可互换，否则指令会被念出来。",
        "复刻样本每次请求随音频重发，作为 data URI 发送，单条上限 10 MB。",
        "带 voice_design 时使用音色设计模型并省略 audio.voice；描述每次随请求发送，平台不保存音色。",
        "带参考音频时使用复刻模型，否则使用预置音色模型；模型由请求形状决定。",
        "输出固定为 24 kHz 单声道。",
    ),
)
