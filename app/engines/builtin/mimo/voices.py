"""MiMo 的预置音色——**官方一共 9 个，写死，不拉取**。

平台**没有公开的音色列表接口**（``mimo.mi.com/docs`` 上只有这份表格）。为九个
条目发一次网络请求、还要处理它失败——那不是"动态"，是把一个常量变成一个故障点。
所以 :data:`PRESET_VOICES` 就是权威清单，``list_voices`` 原样返回它
（见 :mod:`app.engines.builtin.mimo.plugin`）。

**语言与性别是官方表格里写着的**（不是认出来的）：这一家的每条音色都标了语言与
性别，照抄即可，不必过 :mod:`app.engines.traits` 那套推断——那是给"只写在名字与
说明里"的平台用的。``mimo_default`` 的语言随集群而变，官方明说，所以留空不猜。

``id`` 就是发给平台的 ``audio.voice`` 值；``name`` 是官方表格里那一列人话名
（多数与 id 相同，``mimo_default`` 例外）。

**说明里那几个词照抄成标签与年龄**（:data:`_DESCRIBED`）：官方那句「英文女声：偏
活泼」里，「活泼」是这个声音的特点、「年轻」是年龄感——它们是散文里的字，界面要的
是能筛的标签与格子，所以在这里落成结构化字段。这一步以前漏了：九条音色只抄了语言
与性别，说明里的词只剩在简介那段散文里，卡片上没有、标签云里也筛不到。
"""

from __future__ import annotations

from app.engines.base import VoiceMeta

__all__ = [
    "PRESET_VOICES",
    "VOICE_CHOICES",
    "VOICE_IDS",
    "voice_meta",
]

#: ``(id, 显示名, 说明, 语言, 性别)``——顺序即官方表格的顺序。
#:
#: 语言用 BCP-47 主语言子标签、性别用 ``female`` / ``male``，与
#: :data:`~app.domain.models.LANGUAGE_CHOICES` / :data:`~app.domain.models.GENDER_CHOICES`
#: 同一批代码（界面筛选栏直接可用）。
_PAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    ("mimo_default", "MiMo-默认", "默认音色：按部署集群而定（国内集群默认「冰糖」）", "", ""),
    ("冰糖", "冰糖", "中文女声，国内集群的默认音色", "zh", "female"),
    ("茉莉", "茉莉", "中文女声：清亮", "zh", "female"),
    ("苏打", "苏打", "中文男声：年轻", "zh", "male"),
    ("白桦", "白桦", "中文男声：稳重", "zh", "male"),
    ("Mia", "Mia", "英文女声", "en", "female"),
    ("Chloe", "Chloe", "英文女声：偏活泼", "en", "female"),
    ("Milo", "Milo", "英文男声：年轻", "en", "male"),
    ("Dean", "Dean", "英文男声：成熟", "en", "male"),
)

#: 说明那一句里写着的``(音色特点, 年龄感)``——照抄，不是推断（见模块顶部）。
#:
#: 表里没出现的 id = 官方说明里没有这类词，两样都留空。年龄代码与
#: :data:`~app.domain.models.AGE_CHOICES` 同一批；特点落成标签（可多选、进标签云）。
_DESCRIBED: dict[str, tuple[tuple[str, ...], str]] = {
    "茉莉": (("清亮",), ""),
    "苏打": ((), "young"),
    "白桦": (("稳重",), ""),
    "Chloe": (("活泼",), ""),
    "Milo": ((), "young"),
    "Dean": (("成熟",), ""),
}


def voice_meta(voice_id: str) -> VoiceMeta | None:
    """按 id 取一条预置音色；不在表里返回 ``None``（调用方自己决定怎么办）。"""
    for item in _PAIRS:
        if item[0] == voice_id:
            tags, age = _DESCRIBED.get(item[0], ((), ""))
            return VoiceMeta(
                id=item[0],
                name=item[1],
                description=item[2],
                category="预置音色",
                tags=tags,
                language=item[3],
                gender=item[4],
                age=age,
            )
    return None


#: 全部预置音色（顺序即官方表格顺序）。
PRESET_VOICES: tuple[VoiceMeta, ...] = tuple(
    voice_meta(item[0]) for item in _PAIRS  # type: ignore[misc]
)

#: 音色 id（顺序同上）——界面下拉与"这个 id 合法吗"都查它。
VOICE_IDS: tuple[str, ...] = tuple(item[0] for item in _PAIRS)

#: 语言与性别的短说法（下拉里用；完整说明在 :data:`PRESET_VOICES` 的
#: ``description`` 里，两边不写第二遍）。
_SHORT: dict[tuple[str, str], str] = {
    ("zh", "female"): "中文女声",
    ("zh", "male"): "中文男声",
    ("en", "female"): "英文女声",
    ("en", "male"): "英文男声",
    ("", ""): "随集群而定",
}

#: ``(值, 显示名)``——给 ``select`` 控件用（见 :func:`~app.engines.base.ui_hints`）。
VOICE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (item[0], f"{item[1]} · {_SHORT.get((item[3], item[4]), '')}".rstrip(" ·"))
    for item in _PAIRS
)
