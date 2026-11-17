"""缓存与失效判据：分析请求、逐行音频、样例策略的指纹。

三个键回答同一个问题——「这份产物还是不是这一次输入的结果」：

- :func:`analysis_key`：一次分析请求的缓存键，命中就不重复计费；
- :func:`audio_key`：一行的合成输入指纹，变了就要重做音频；
- :func:`sample_key`：样例策略进指纹，换样例就是换声音。

键的取值只由输入决定，与时间、顺序无关；调用方拿它比对旧记录，不再自己拼串。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.producers.builtin.audiobook.prompts import PROMPT_VERSION

__all__ = ["analysis_key", "audio_key", "audio_key_v2", "sample_key"]

#: 分析回复的采样温度。进键：改温度等于换一次请求，旧缓存不该继续命中。
_ANALYSIS_TEMPERATURE = 0.2


def analysis_key(
    *,
    channel: str,
    model: str,
    prompt: str,
    book: str,
    text: str,
    rows: list[str],
    granularity: str,
    context: str,
    context_lines: int,
    emotions: list[str],
) -> str:
    """一次分析请求的缓存键：通道 / 模型 / 提示词 / 书 / 上下文 / 这批对白 / 情绪词表。

    **候选角色不进键**：它是从项目现状派生的（上一轮的结果会长出新的名字），
    进键的话第二次运行必然全部未命中——重跑不重复计费的承诺就没了。

    **情绪词表进键**：它决定提示词里列哪些词（用户改词表后同一条对白该重新问），
    也决定回写认不认模型给的词。

    「要回哪几格」不进键：它由提示词正文推出来（见 ``prompts.fields_for_prompt``），
    提示词本身已经在键里了。
    """
    raw = json.dumps(
        {
            "v": PROMPT_VERSION,
            "channel": channel,
            "model": model,
            "prompt": prompt,
            "book": book,
            "text": text,
            "rows": rows,
            "granularity": granularity,
            "context": context,
            "context_lines": context_lines,
            "emotions": emotions,
            "temperature": _ANALYSIS_TEMPERATURE,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def sample_key(mapping: dict[str, Any]) -> str:
    """样例策略进指纹：换样例就是换声音，旧音频不能算数。"""
    mode = str(mapping.get("sample_mode") or "emotion")
    return f"{mode}:{mapping.get('sample_file') or ''}" if mode == "fixed" else mode


#: 行音频的容器**不是**设置项：写盘按引擎回报的真实格式命名。「格式」这一维仍在
#: 指纹里，值固定成旧的默认值——它是老项目 key 的组成部分，换掉或删掉等于让每本书
#: 全量重做（云引擎还要重新计费），而它现在不描述任何可变的东西。
LEGACY_FORMAT = "mp3"


def audio_key(
    *,
    text: str,
    engine: str,
    voice_id: str,
    emotion: str,
    fmt: str = LEGACY_FORMAT,
    params: dict[str, Any] | None = None,
    sample: str = "",
    revision: str = "",
) -> str:
    """合成输入指纹：文本 / 音色 / 情绪 / 样例 / 行级参数任一变化都要重做。

    :param fmt: 已废弃的维度（见 :data:`LEGACY_FORMAT`），默认值即历史口径。
    :param revision: 读音表版本。空串（一条替换规则都没有）不加这一维：老项目的
        指纹因此不变；真有规则时它必须进来——读音表改了，同一段原文的发音就变了，
        旧音频不该冒充这一版的产物（生成页的幂等指纹也是这么做的）。
    """
    extra = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    raw = f"{text}\x1f{engine}\x1f{voice_id}\x1f{emotion}\x1f{fmt}\x1f{sample}\x1f{extra}"
    if revision:
        raw = f"{raw}\x1fpron:{revision}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def audio_key_v2(
    *,
    text: str,
    engine: str,
    voice_id: str,
    emotion: str,
    identity: str = "",
    params: dict[str, Any] | None = None,
    sample: str = "",
    pron: str = "",
) -> str:
    """第二版合成指纹：只把"真的会改变这一行声音"的东西算进来。

    与 :func:`audio_key` 的两处差别：

    - ``pron`` 是**这一行实际命中的**读音替换（见 ``pronunciation.matches``），
      不是整张词表的版本——改一条与这行无关的规则不该让它重做；
    - ``identity`` 是引擎身份（插件版本 + 模型 / 后端，有就带上）：换模型、升级
      插件后旧音频不该继续冒充新产物。

    ``fmt`` 不再进键（容器由引擎决定，由落盘文件名体现）；旧行继续用 v1 比对，
    懒迁移见 ``audio_stage._audio_plan``。
    """
    extra = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    raw = f"{text}\x1f{engine}\x1f{identity}\x1f{voice_id}\x1f{emotion}\x1f{sample}\x1f{extra}"
    if pron:
        raw = f"{raw}\x1fpron:{pron}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
