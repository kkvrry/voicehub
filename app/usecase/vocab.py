"""用户词表用例：情绪候选与引导指令预设的**生效值**。

产地
----

- 默认情绪：:data:`~app.domain.models.EMOTION_GROUPS`（基础 / 复合 / 态度三组，
  基础 9 + 复合 13 + 态度 5；基础那一组与 MiMo 官方的基础情绪表逐字一致，
  见 ``docs/request-spec.md`` §4.5）；
- 默认引导指令：各引擎自己 ``instructions.json`` 的**并集**。曾在前端按引擎合并
  （``presetsOf``），现在后端一次算清：设置页显示的"生效值"就是生成页、试听、
  行参数弹框实际看到的那一份，不会两处对不上。引导指令是自由文本，不支持的引擎
  发请求时那一项会被剔掉，所以合并不按引擎分组，也与启停无关（见
  :func:`_default_instructions`）。

生效规则（两节同一条）
--------------------

- 默认条目按原顺序排，``hidden``（删除）里的不出现；
- ``renamed`` 把默认条目原地改名：位置与分组不变（情绪按默认分组，预设按原顺序）；
- ``custom`` 里与默认**同名**的（情绪是用词、预设是短名）就地覆盖默认的文字，
  不新增一行——想改一句默认预设的措辞，加一条同名自定义即可；
- 其余自定义条目追加在末尾。

删除只改候选表：项目里已经写下的情绪、引导指令是行自己的数据，不因这里删词而变化。
"""

from __future__ import annotations

from typing import Any

from app.domain.models import EMOTION_CHOICES, EMOTION_GROUPS
from app.engines.registry import list_engines
from app.infra import vocab as vocab_store
from app.infra.vocab import (
    MAX_CUSTOM_EMOTIONS,
    MAX_CUSTOM_PRESETS,
    MAX_PRESET_TEXT_CHARS,
    MAX_WORD_CHARS,
    VocabError,
)

__all__ = [
    "emotion_groups",
    "emotion_state",
    "emotions",
    "instruction_state",
    "instructions",
    "save",
    "state",
]


def _default_emotions() -> list[str]:
    """仓库里的默认情绪词（顺序即界面顺序）。"""
    return [word for word, _ in EMOTION_CHOICES]


def _default_instructions() -> list[dict[str, str]]:
    """**所有已发现**引擎 ``instructions.json`` 的并集：按引擎顺序、短名去重（先出现的算数）。

    与启停无关。这一格是自由文本，预设在语义上就是"可以照抄的一句话"，不指向哪台
    引擎；按已启用收束只会让一张静态的说法表随开关增减——停用一台，候选里凭空虚少
    一段，读起来像被删了。发给不认这一格的引擎也无害：装配时按插件声明的参数表
    过滤掉（``usecase/gateway.py``），不报错。
    """
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for plugin in list_engines().values():
        for label, text in plugin.field_presets().get("instruction", ()):
            if label in seen:
                continue
            seen.add(label)
            merged.append({"label": label, "text": text})
    return merged


def emotion_state() -> dict[str, Any]:
    """情绪那一节的六件套：默认 / 删除 / 改名 / 自定义 / 生效 / 分组。"""
    user = vocab_store.load_emotions()
    defaults = _default_emotions()
    known = set(defaults)
    # 删一个不在默认里的词没有意义（仓库删词后会留下这种过期条目）：读时滤掉。
    hidden = [word for word in user["hidden"] if word in known]
    hidden_set = set(hidden)
    # 改名只认还在的默认词；删掉的那条不参与生效值。
    renamed = {
        word: target
        for word, target in user["renamed"].items()
        if word in known and word not in hidden_set
    }
    custom = user["custom"]
    effective: list[str] = []
    for word in defaults:
        if word in hidden_set:
            continue
        value = renamed.get(word, word)
        if value not in effective:
            effective.append(value)
    effective += [word for word in custom if word not in effective]
    return {
        "defaults": defaults,
        "hidden": hidden,
        "renamed": renamed,
        "custom": custom,
        "effective": effective,
        "groups": _emotion_groups(hidden, renamed, custom),
        # 默认分组不受删除与改名影响：设置页据此把默认行摆在原组里，删除后可恢复。
        "default_groups": [
            {"label": label, "words": list(words)} for label, words in EMOTION_GROUPS
        ],
    }


def _emotion_groups(
    hidden: list[str], renamed: dict[str, str], custom: list[str]
) -> list[dict[str, Any]]:
    """按 :data:`~app.domain.models.EMOTION_GROUPS` 摆好的生效词；自定义词单独一组。

    分组只是界面上的摆法，**不改生效值**：拼起来与 ``effective`` 逐项相同。
    空组不摆（一整组都被删掉时不该留一个空标题）；手改文件造成的重名也只留一项。
    """
    hidden_set = set(hidden)
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    for label, words in EMOTION_GROUPS:
        kept: list[str] = []
        for word in words:
            if word in hidden_set:
                continue
            value = renamed.get(word, word)
            if value in seen:
                continue
            seen.add(value)
            kept.append(value)
        if kept:
            groups.append({"label": label, "words": kept})
    extra = [word for word in custom if word not in seen]
    if extra:
        groups.append({"label": "自定义", "words": extra})
    return groups


def instruction_state() -> dict[str, Any]:
    """引导指令那一节的五件套（条目形状 ``{"label","text"}``）。"""
    user = vocab_store.load_instructions()
    defaults = _default_instructions()
    labels = {item["label"] for item in defaults}
    hidden = [label for label in user["hidden"] if label in labels]
    hidden_set = set(hidden)
    renamed = {
        label: target
        for label, target in user["renamed"].items()
        if label in labels and label not in hidden_set
    }
    custom = user["custom"]
    by_label = {item["label"]: item for item in custom}
    effective: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in defaults:
        key = item["label"]
        if key in hidden_set:
            continue
        # 同名自定义就地顶替默认的文字（位置不变）；改名只换显示名。
        label = renamed.get(key, key)
        if label in seen:
            continue
        text = (by_label.get(key) or item)["text"]
        effective.append({"label": label, "text": text})
        seen.add(label)
    effective += [
        item
        for item in custom
        if item["label"] not in labels and item["label"] not in seen
    ]
    return {
        "defaults": defaults,
        "hidden": hidden,
        "renamed": renamed,
        "custom": custom,
        "effective": effective,
    }


def emotions() -> list[str]:
    """生效的情绪候选（生成页规范、创作页下拉、分析提示词都读它）。"""
    return emotion_state()["effective"]


def emotion_groups() -> list[dict[str, Any]]:
    """生效情绪的分组摆法：``[{"label", "words"}, …]``（创作页下拉按它分组）。"""
    return emotion_state()["groups"]


def instructions() -> list[dict[str, str]]:
    """生效的引导指令预设（设置页展示与下游各页点选同一份）。"""
    return instruction_state()["effective"]


def state() -> dict[str, Any]:
    """设置页「词表」那一栏的整包。"""
    return {"emotions": emotion_state(), "instructions": instruction_state()}


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #


def _clean_renames(
    renamed: dict[str, str] | None, known: set[str]
) -> dict[str, str]:
    """默认条目改名：只认还在默认里的键，空值与同名条目丢掉。"""
    cleaned: dict[str, str] = {}
    for key, value in (renamed or {}).items():
        word = str(key or "").strip()
        target = str(value or "").strip()
        if word not in known or not target or target == word:
            continue
        if len(target) > MAX_WORD_CHARS:
            raise VocabError(
                f"「{word}」改名太长：「{target[:MAX_WORD_CHARS]}……」超过 {MAX_WORD_CHARS} 字。"
            )
        cleaned[word] = target
    return cleaned


def _check_custom_emotions(custom: list[str], default_words: set[str]) -> list[str]:
    """自定义情绪词：不空、不长、不与生效中的默认词重名、不超量。"""
    if len(custom) > MAX_CUSTOM_EMOTIONS:
        raise VocabError(
            f"自定义情绪词太多：{len(custom)} 个，最多 {MAX_CUSTOM_EMOTIONS} 个。"
        )
    seen: set[str] = set()
    for word in custom:
        if word in default_words:
            raise VocabError(f"「{word}」是默认情绪词，无需重复添加。")
        if word in seen:
            raise VocabError(f"情绪词重复：「{word}」。")
        seen.add(word)
        if len(word) > MAX_WORD_CHARS:
            raise VocabError(f"情绪词太长：「{word[:MAX_WORD_CHARS]}……」超过 {MAX_WORD_CHARS} 字。")
    return custom


def _check_emotion_labels(
    hidden: list[str], renamed: dict[str, str], custom: list[str]
) -> None:
    """生效值里不能出现两个同名词：改名撞上默认、自定义撞上改名都拦下。"""
    hidden_set = set(hidden)
    seen: set[str] = set()
    for word in _default_emotions():
        if word in hidden_set:
            continue
        value = renamed.get(word, word)
        if value in seen:
            raise VocabError(f"改名后出现重名：「{value}」。")
        seen.add(value)
    for word in custom:
        if word in seen:
            raise VocabError(f"「{word}」与默认词或改名后的词重复。")
        seen.add(word)


def _check_custom_instructions(custom: list[dict[str, str]]) -> list[dict[str, str]]:
    """自定义预设：短名与句子都要有、短名不重复、不超长、不超量。"""
    if len(custom) > MAX_CUSTOM_PRESETS:
        raise VocabError(
            f"自定义预设太多：{len(custom)} 条，最多 {MAX_CUSTOM_PRESETS} 条。"
        )
    seen: set[str] = set()
    for item in custom:
        label = item["label"]
        if label in seen:
            raise VocabError(f"预设短名重复：「{label}」。")
        seen.add(label)
        if len(label) > MAX_WORD_CHARS:
            raise VocabError(f"预设短名太长：「{label[:MAX_WORD_CHARS]}……」超过 {MAX_WORD_CHARS} 字。")
        if len(item["text"]) > MAX_PRESET_TEXT_CHARS:
            raise VocabError(
                f"「{label}」的句子超过 {MAX_PRESET_TEXT_CHARS} 字：{item['text'][:20]}……"
            )
    return custom


def _check_instruction_labels(
    hidden: list[str], renamed: dict[str, str], custom: list[dict[str, str]]
) -> None:
    """生效预设的短名不能重复；自定义只覆盖默认的文字，不另占一行。"""
    defaults = _default_instructions()
    default_labels = {item["label"] for item in defaults}
    hidden_set = set(hidden)
    seen: set[str] = set()
    for item in defaults:
        if item["label"] in hidden_set:
            continue
        label = renamed.get(item["label"], item["label"])
        if label in seen:
            raise VocabError(f"改名后预设重名：「{label}」。")
        seen.add(label)
    for item in custom:
        if item["label"] in default_labels:
            continue
        if item["label"] in seen:
            raise VocabError(f"「{item['label']}」与默认预设或改名后的名字重复。")
        seen.add(item["label"])


def save_emotions(
    hidden: list[str], custom: list[str], renamed: dict[str, str] | None = None
) -> dict[str, Any]:
    """保存情绪那一节（整节替换差别），回新的整包。

    删除只进 ``hidden``：项目行上的情绪是行自己的数据，这里不动它。
    """
    defaults = _default_emotions()
    known = set(defaults)
    hidden = [word for word in hidden if word in known]
    cleaned = _clean_renames(renamed, known)
    hidden_set = set(hidden)
    active = {
        cleaned.get(word, word) for word in defaults if word not in hidden_set
    }
    _check_custom_emotions(custom, active)
    _check_emotion_labels(hidden, cleaned, custom)
    vocab_store.save_emotions(hidden, custom, cleaned)
    return emotion_state()


def save_instructions(
    hidden: list[str],
    custom: list[dict[str, str]],
    renamed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """保存指令那一节（整节替换差别），回新的整包。"""
    known = {item["label"] for item in _default_instructions()}
    hidden = [label for label in hidden if label in known]
    cleaned = _clean_renames(renamed, known)
    _check_custom_instructions(custom)
    _check_instruction_labels(hidden, cleaned, custom)
    vocab_store.save_instructions(hidden, custom, cleaned)
    return instruction_state()


def save(
    *, emotions: dict[str, Any] | None = None, instructions: dict[str, Any] | None = None
) -> dict[str, Any]:
    """按节局部保存：没给的那一节不动（用户文件里那一份原样保留）。

    :param emotions: ``{"hidden", "renamed", "custom"}``；``None`` = 这一节不改。
    :param instructions: 同形状，``custom`` 是 ``[{"label","text"}]``。
    """
    if emotions is not None:
        save_emotions(
            emotions.get("hidden") or [],
            emotions.get("custom") or [],
            emotions.get("renamed") or {},
        )
    if instructions is not None:
        save_instructions(
            instructions.get("hidden") or [],
            instructions.get("custom") or [],
            instructions.get("renamed") or {},
        )
    return state()
