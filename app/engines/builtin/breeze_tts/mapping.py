"""统一参数 → BreezeTTS 2 请求体：**这台引擎认哪些、不认哪些，全写在明面上**。

这一家是**本机 zero-shot 克隆**（audio.cpp 的 ``POST /v1/audio/speech``）：它的
"音色"就是**一段参考音频 + 那段音频念的内容**，没有音色 id、没有平台清单——形状与
GPT-SoVITS 同族，落点却完全不同（见下表）。

落点对照（**逐个实测过**，不是照文档抄的）：

==================  ========================  ============================================
统一参数            引擎字段                    备注
==================  ========================  ============================================
``text``            ``input``                 OpenAI 风格：正文那一格叫 ``input``
``reference_audio`` ``voice_ref``             **base64**，不发路径（中文路径会被拒）
``reference_text``  ``reference_text``        克隆时**必填**（引擎会回 ``requires reference_text``）
``instruction``     ``options.instruction``   **必须在 options 里**，见下面那段
``emotion``         ``options.instruction``   没有引导指令时写成一句引导语；有指令时以指令为准
``guidance_scale``  ``options.guidance_scale`` CFG
``temperature`` 等  ``options.*``             采样那一批全在 options 里
``seed``            ``seed``                  **顶层**（与它自己的网页界面一致）
==================  ========================  ============================================

**``instruction`` 必须放进 ``options``，放顶层会被静默忽略**——这是接这一家最容易踩、
也最难发现的一脚（没有报错、没有警告，请求 200，声音照出，只是那句话一个字都没进去）：

- 引擎自己的规格（``model_spec.options.request``）把这一格列在 **options** 之下；
  放顶层的 ``instruction`` 它**不当成错误**，只是认不出来、丢掉；
- 顶上那一层还多认一个 **``instructions``（复数）**——那是 OpenAI ``/v1/audio/speech``
  自己的字段名，两处指向同一个值，实测两者产出**逐字节相同**的音频；
- ``options`` 里的键是**校验过的**：写错一个名字会当场回
  ``unknown BreezeTTS request option: xxx``（实测）。所以这一批参数都往 options 放
  ——**写错名字会响**，而顶层写错只会静默丢掉。

上面那张表里的落点路径由 :class:`~app.engines.mapping.ParamMapping` 的点号语法表达
（``"options.instruction"``），装配代码里不必再为每个键写一遍 if。

没有落点的项（``voice_id`` / ``speed`` / ``volume`` / ``pitch`` /
``lead_silence`` / ``lag_silence`` / ``language`` / ``reference_language`` / ``format``）
由插件的 ``unsupported()`` 逐条写原因，面板的映射表直接呈现。

``emotion`` **不在这张表里**：它没有原生的情绪参数，但会由插件在装配时写成一句
引导语（见 ``plugin.emotion_instruction``），落点仍是 ``options.instruction``。
这条落点随「有没有引导指令」而变（有指令时以指令为准），静态表表达不了，
所以在 ``dynamic_mappings()`` 里声明。

:data:`MAPPING` 里那几个**别名**（``instruct`` / ``cfg`` / ``cfg_scale`` / ``speaker``）
是给上游留的余地，每一个都有来历：``guidance_scale`` 在别家文档里也叫 ``cfg`` /
``cfg_scale``（同一件事的三个叫法）；``speaker`` 是规范当年移除、这一轮又以
``instruction`` 的身份加回来的那一路（"一句话描述音色"，而这一家正好吃它，
见 docs/request-spec.md §7.7 与 §7.14）。**认几个常见写法，上游用哪个名字都通。**
**顺序即优先级**（``apply_mapping`` 是顺序写入，后写的覆盖先写的）：正名排在最后，
于是「正名与别名同时给了」时以正名为准。
"""

from __future__ import annotations

from app.engines.mapping import EngineMapping, ParamMapping, register_transform


def _strength_to_cfg(value):
    """规范里的「指令强度」（0–1）→ 这一家的 CFG（0–8）：0.5 → 4。

    **只保留一位小数**：这一格是给用户看的刻度（0.05 一档），换算后到三位小数
    只会让日志里出现 ``3.6000000000000005`` 这种数；而 CFG 这一层本来也不是
    需要小数点后三位才听得出来的东西。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(8.0, number * 8.0)), 1)


register_transform("strength_to_cfg", _strength_to_cfg, label="强度 0–1 → CFG 0–8")

__all__ = ["MAPPING", "MARK_MAP", "MARK_MAP_EN"]

#: 正文里的语气词标签：**中文正文写成方括号那套**（官方 README 的中文示例就是 ``[笑]``）。
#:
#: **键一律是统一写法里那个中文名**（``domain.marks.MARKS``）——这一家的原生写法是
#: 另一个词，先把它的**意思**翻成中文再登记，别照抄：
#:
#: - 官方 README 给过的四条：``[笑]`` ``[咳嗽]`` ``[清嗓子]`` ``[叹气]``；
#: - ``(gasp)`` **就是「喘气」**（倒抽一口气），不是一个新概念。照抄成 ``gasp`` 的话，
#:   界面上它会与 MiniMax 的 ``(pant)`` 变成两条，而"通用"永远算不出来——
#:   翻译成 ``喘气`` 之后，这一条才有机会进「通用」那一组。它只在整合包的**英文**
#:   列表里（中文那列没有对应词），所以中文正文里也原样写 ``(gasp)``。
#:
#: 为什么都给登记（从前是不登记的）：登记了界面才有入口，用户才点得到；
#: 不登记就只能照着手册手打 `(gasp)`，而那正是"手写映射表"最容易漏掉的一块。
#: 风险对所有条目一视同仁——**引擎不认时那串标记会被当成字念出来**，
#: 面板的标签表如实标了每一条的来路（通用 / 本引擎独有），别让人以为有保证。
MARK_MAP: dict[str, str] = {
    "笑": "[笑]",
    "叹气": "[叹气]",
    "咳嗽": "[咳嗽]",
    "清嗓": "[清嗓子]",
    # 中文列表里没有 gasp 的对应词：**按官方列表，中文正文里也写 (gasp)**。
    "喘气": "(gasp)",
}

#: 同一批标签的**英文写法**——英文正文用圆括号那套（官方 README 的英文示例）。
#:
#: 两套不是一个"备选"，是**按正文语言分开用**的：README 明说英文用括号、中文用方括号。
#: 由 ``plugin.mark_map_for`` 按正文里中文字符的多少挑一套，不在这里挑。
#:
#: 键与上面那份**逐条相同**（同一个概念两种写法），否则"这一家认哪几条"会随正文
#: 语言变来变去——而"认不认"与正文是什么语言无关，只有"怎么写"才有关。
MARK_MAP_EN: dict[str, str] = {
    "笑": "(laugh)",
    "叹气": "(sigh)",
    "咳嗽": "(cough)",
    "清嗓": "(clears throat)",
    "喘气": "(gasp)",
}

#: 这张表就是"BreezeTTS 2 要哪几个键"的答案（字段名照 audio.cpp 的 ``/v1/audio/speech``）。
#:
#: 两处分界，都是实测出来的：
#:
#: - **顶层**：``input`` / ``voice_ref`` / ``reference_text`` / ``seed``——
#:   它自己那个网页界面就是这四样摆在外面；
#: - **``options``**：其余全在里头，**包括 ``instruction``**（放顶层会被静默丢掉，
#:   见模块顶部）。放在 options 里还有个好处：名字写错会当场报错，而不是悄悄少一项。
MAPPING = EngineMapping(
    params=(
        # 正文那个字段在 OpenAI 风格里叫 ``input``。
        ParamMapping("text", "input", required=True, note="要合成的正文。"),
        # 本引擎的主体：参考音频。**不给也能走**（那时就是纯音色设计，靠 instruction），
        # 而两者同时给也支持——参考音频定音色，instruction 在其上调整语气。
        # 所以这里不写 required=True：静态的 required 元组表达不了"至少给一个"，
        # 由 plugin.build_body_with_notes 里的守卫判。
        ParamMapping(
            "reference_audio",
            "voice_ref",
            note="参考音频；可与「引导指令」同时使用，两者都缺时拦截请求。",
            warning=True,
        ),
        ParamMapping(
            "reference_text",
            "reference_text",
            note="参考音频的逐字稿；有参考音频时必需。",
            warning=True,
        ),
        ParamMapping(
            "instruction",
            "options.instruction",
            note="自然语言描述；有参考音频时调整语气，否则描述音色。",
            warning=True,
        ),
        # 规范里的「指令强度」是 0–1：0.5 对齐这一家的推荐值 4，乘 8 落进去。
        # cfg / cfg_scale 是给老调用方留的别名，仍按 0–8 原样发。
        ParamMapping("cfg", "options.guidance_scale", transform="number", note="别名，CFG 原值。"),
        ParamMapping(
            "cfg_scale", "options.guidance_scale", transform="number", note="别名，CFG 原值。"
        ),
        ParamMapping(
            "guidance_scale",
            "options.guidance_scale",
            transform="strength_to_cfg",
            note="统一强度 0–1 换算为 CFG 0–8（保留一位小数）；没给时按有没有引导取 4 / 1。",
        ),
        # ---- 采样参数（上游给了就用上游的，没给用配置里的） ----
        ParamMapping(
            "temperature", "options.temperature", transform="number", note="采样温度。"
        ),
        ParamMapping(
            "depth_temperature",
            "options.depth_temperature",
            transform="number",
            note="采样温度（深层）。",
        ),
        ParamMapping("top_k", "options.top_k", transform="int", note="采样候选数。"),
        ParamMapping("top_p", "options.top_p", transform="number", note="采样累积概率。"),
        ParamMapping("max_tokens", "options.max_tokens", transform="int", note="单次生成上限。"),
        ParamMapping(
            "text_chunk_size",
            "options.text_chunk_size",
            transform="int",
            note="正文分块长度。",
        ),
        ParamMapping("text_chunk_mode", "options.text_chunk_mode", note="分块方式。"),
        # 种子在**顶层**（与它的网页界面一致；实测 options 里也给得进，两处等价，
        # 就照它自己的写法来）。
        ParamMapping(
            "seed", "seed", transform="int", note="固定种子；-1 表示由核心抽样。"
        ),
    ),
    # 这一家没有"每次都要原样带上的固定量"——模型 id 由 client 放进 body（那是配置，
    # 不是常量），格式是它自己决定的（恒定 wav）。
    constants={},
    cautions=(
        "「引导指令」必须位于请求的 options 内；写入顶层会被静默忽略，请求仍返回 200。",
        "参考音频以 base64 发送，单条上限 5 MB；超限时报出实际大小。",
        "参考音频仅接受 WAV；其他容器由核心在发送前自动转码。",
        "输出固定为 WAV；请求其他格式不会生效。",
    ),
)
