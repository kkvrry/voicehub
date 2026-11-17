"""音色凭据：一条音色落到某台引擎上，这次请求该带什么。

四种结果，没有第五种：

- :data:`DESIGN` —— 带一句音色描述，引擎按描述现场设计（任何声明了这项能力的引擎）；
- :data:`REFERENCE` —— 带一段音频，引擎按它复刻；
- :data:`PLATFORM` —— 带平台音色 id，平台自己存着这条音色；
- :data:`NONE` —— 落不到这台引擎上（跨平台又没样例、本地音色没样例）。

**这是唯一判定**。网关按它决定发什么，界面按它决定给不给选项，生产者不另推。
两边各写一遍时，界面上的"可选项"要到合成时才以失败的形式暴露。

几条不显然的取舍：

- **预置音色一律发 id**：平台认识它、自己也存着它，拿它自己的样例去复刻只会更差，
  也与通道配置无关——通道不改变"这条音色怎么发"。
- **跨平台只能借样例**：别的平台不认识这个 id；有样例就能用那台引擎的复刻能力，
  没样例就是"落不到"。
- **设计音色哪家都能用**：它的全部就是一句自然语言描述，凡声明了「音色设计」
  能力的引擎都能按它生成；具体用谁由用户在合成页选，能力不够的引擎会被筛掉。
"""

from __future__ import annotations

from typing import Any

__all__ = ["DESIGN", "NONE", "PLATFORM", "REFERENCE", "credential_of"]

#: 带音色描述（设计）。
DESIGN = "design"
#: 带参考音频（本地音色 / 复刻音色 / 跨平台借样例）。
REFERENCE = "reference"
#: 带平台音色 id。
PLATFORM = "platform"
#: 落不到这台引擎上。
NONE = ""


def credential_of(voice: Any, *, engine_key: str = "") -> str:
    """这条音色在这台引擎上带什么。

    :param voice: :class:`app.domain.models.Voice`（或形状相同的对象）。
    :param engine_key: 目标引擎；空串 = 还没定引擎，按"用它自己的平台"处理
        （``normalize_voice`` 的旧调用形态靠这一条保持原意）。
    """
    source = str(getattr(voice, "source", "") or "")
    own = not engine_key or source == engine_key
    payload = getattr(voice, "payload", None) or {}

    if str(payload.get("design_prompt") or "").strip():
        # 设计音色的全部就是那一句描述——自然语言，不是某家平台的私有 id。
        # 凡声明了「音色设计」能力的引擎都能按它现场生成，用谁由用户在合成页选；
        # 落不到的那几台由 :func:`app.usecase.engines.engines_for_voice` 按能力筛掉。
        return DESIGN
    if getattr(voice, "is_local", False):
        return REFERENCE if getattr(voice, "has_sample", False) else NONE
    if str(payload.get("ref_audio") or "").strip():
        # 平台不保存音色：这段样本就是它，换到别的引擎也一样能用。
        return REFERENCE
    if own:
        return PLATFORM
    return REFERENCE if getattr(voice, "has_sample", False) else NONE
