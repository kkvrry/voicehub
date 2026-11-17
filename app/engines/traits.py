"""从平台自己的文字里认音色属性——语言 / 性别 / 年龄感。

平台清单没有这三样字段，只能从 ``voice_id``（命名）、``voice_name``（中文名）、
``description``（散文）里认。语言落成标签，性别与年龄感落进各自的格子。

纪律：命名先、散文后；同段有两个年龄词取先出现的（平台句式年龄词在最前）；
认不出就空着，不猜（猜错的属性比空着难查）。``中性`` / ``竹马`` / ``成熟`` 都不是
性别或年龄词。年龄只读中文文本：``voice_id`` 里的 ``Senior`` 指"学长"不是"老年"。

表是闭集，加一种说法 = 加一行；表里的词分"清单里真出现过的"与"常见写法"两类，
后者只在该词无歧义时才收——宁可漏，不可误伤。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "AGES",
    "CHARACTERISTICS",
    "GENDERS",
    "LANGUAGES",
    "VoiceTraits",
    "ages_in",
    "characteristics_in",
    "detect_age",
    "detect_characteristics",
    "detect_gender",
    "detect_language",
    "genders_in",
    "languages_in",
    "read_traits",
]

# --------------------------------------------------------------------------- #
# 三张表
# --------------------------------------------------------------------------- #

#: 语言表：``(代码, 认得出的写法)``。
#:
#: 英文按整词、中文按子串匹配（见 :func:`_find`）。代码用 BCP-47 主语言子标签；
#: ``zh`` 与 ``yue`` 分开列（与平台一致，合成时是两种语言）。
#: 代码的中文说法在 ``app/domain/models.py`` 的 ``LANGUAGE_CHOICES``（那边是
#: 「代码 → 说法」的唯一产地）；两张表的代码由测试钉住必须一致。
LANGUAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("zh", ("chinese", "mandarin", "普通话", "中文", "汉语", "国语")),
    ("yue", ("cantonese", "粤语", "广州话", "广府话")),
    # 汉语方言：只列"能落到具体方言点"的写法（语族名挂在代表点上）。
    ("cmn-cd", ("成都话", "四川话", "西南官话")),
    ("cmn-wh", ("武汉话", "湖北话")),
    ("cmn-xa", ("西安话", "关中话", "陕西话")),
    ("cmn-zz", ("郑州话", "河南话")),
    ("cmn-jn", ("济南话", "山东话", "冀鲁官话")),
    ("cmn-nj", ("南京话", "江淮官话")),
    ("wuu-sz", ("苏州话", "吴语")),
    ("wuu-hz", ("杭州话",)),
    ("nan-qz", ("泉州话", "闽南语")),
    ("hsn-cs", ("长沙话", "湘语")),
    ("en", ("english", "英语", "英文")),
    ("ja", ("japanese", "日语", "日文")),
    ("ko", ("korean", "韩语", "朝鲜语", "韩文")),
    ("es", ("spanish", "西班牙语")),
    ("fr", ("french", "法语")),
    ("de", ("german", "德语")),
    ("ru", ("russian", "俄语")),
    ("pt", ("portuguese", "葡萄牙语")),
    ("it", ("italian", "意大利语")),
    ("ar", ("arabic", "阿拉伯语")),
    ("nl", ("dutch", "荷兰语")),
    ("tr", ("turkish", "土耳其语")),
    ("uk", ("ukrainian", "乌克兰语")),
    ("id", ("indonesian", "印尼语", "印度尼西亚语")),
    ("vi", ("vietnamese", "越南语")),
)

#: 语言代码集合：识别引擎回报的是 BCP-47 代码（``zh`` / ``zh-CN``），不是写法。
_LANGUAGE_CODES: frozenset[str] = frozenset(code for code, _ in LANGUAGES if code)

#: 性别表：``(代码, 写法)``。**代码就是音色那个字段里存的值**
#: （与 ``domain/models.py`` 的 ``GENDER_CHOICES`` 同一批代码，由测试钉住不漂移）。
#:
#: 中文按子串匹配，所以 ``女声`` 会穿过 ``少女声音`` 这种写法——那里恰好也是对的
#: （``少女`` 是女性）。真正要小心的是 ``中性`` 这类**音色词**，它不在表里。
GENDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "female",
        (
            # 平台的命名习惯（voice_id 里）
            "female",
            "woman",
            "women",
            "lady",
            "ladies",
            "girl",
            "auntie",
            "antie",
            "sister",
            # 中文：通用说法
            "女性",
            "女声",
            "女童",
            "女孩",
            "女士",
            "小姐",
            "女生",
            "女学生",
            # 角色名（"一位{年龄}女战士声音"这种写法）
            "女战士",
            "女英雄",
            "女主持",
            "女主播",
            "女主人",
            "女冒险家",
            "公主",
            "女王",
            "空姐",
            # 亲属/同窗称呼
            "姐姐",
            "妹妹",
            "学姐",
            "学妹",
            "闺蜜",
            "大婶",
            "大姐",
            "阿姨",
            "奶奶",
            "妈妈",
            # 平台自己造的词
            "御姐",
            "萌妹",
        ),
    ),
    (
        "male",
        (
            "male",
            "man",
            "men",
            "guy",
            "gentleman",
            "boy",
            "brother",
            "男性",
            "男声",
            "男童",
            "男孩",
            "男士",
            "先生",
            "男学生",
            "男主持",
            "男主播",
            "男教师",
            "少爷",
            "丈夫",
            "教父",
            "绅士",
            "花花公子",
            "哥哥",
            "弟弟",
            "学长",
            "学弟",
            "男友",
            "大爷",
            "爷爷",
            "爸爸",
        ),
    ),
)

#: 年龄表：``(代码, 写法)``。代码同样是字段里存的值（``domain/models.py`` 的
#: ``AGE_CHOICES``，由测试钉住不漂移）。
#:
#: **写法全是中文**：``voice_id`` 里那些英文年龄词一个都不能用（见模块开头
#: ``Gentle_Senior`` 那条）。
AGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("child", ("儿童", "男童", "女童", "孩童", "宝宝", "婴儿", "小孩", "小朋友")),
    ("teen", ("少年", "少女", "初中生", "高中生")),
    (
        "young",
        (
            "青年",
            "青春",
            "学生",
            "御姐",
            "萌妹",
            "学妹",
            "学姐",
            "学弟",
            "学长",
            "弟弟",
            "妹妹",
            "男友",
            "少爷",
        ),
    ),
    ("middle", ("中年", "大婶", "大姐", "阿姨", "大叔", "叔叔")),
    ("senior", ("老年", "老人", "长者", "花甲", "大爷", "奶奶", "爷爷", "老太太", "年长")),
)

#: 音色特点（声音本身的性格——温柔 / 磁性 / 俏皮），认出来落成标签。
#:
#: 与上面三张表的不同：平词表没有代码（可多选，落进 ``tags``）；
#: 每个词都得有语料支撑才收（特点是开集，"听起来很对"不是证据）；
#: 认不出就空着。``成熟`` 在这张表里：它与"青年"并存，说的是音色不是年纪。
CHARACTERISTICS: tuple[str, ...] = (
    # 温和暖的一类
    "温柔",
    "柔和",
    "温暖",
    "体贴",
    "善良",
    "亲切",
    "友好",
    "真诚",
    # 甜、亮、活泼
    "甜美",
    "可爱",
    "活泼",
    "开朗",
    "活力",
    "俏皮",
    "天真",
    "阳光",
    "灵动",
    "呆萌",
    "清澈",
    # 沉稳可靠
    "沉稳",
    "稳重",
    "可靠",
    "坚定",
    "严肃",
    "冷静",
    "沉静",
    "权威",
    "果断",
    "有力",
    "内敛",
    # 有味道、有质感
    "迷人",
    "磁性",
    "温润",
    "深情",
    "魅力",
    "优雅",
    # 个性强的那一类（未必讨喜，但它们确实是"特点"）
    "傲慢",
    "冷漠",
    "嚣张",
    "霸道",
    "古怪",
    "傲娇",
    # 平台写在名字里的说法（说明里没有，只有名字里有）
    "知性",
    "青涩",
    "精英",
    "成熟",
    "自信",
    "健谈",
    "幽默",
    "宁静",
)

_ASCII_PATTERNS: dict[str, re.Pattern[str]] = {}


def _find(text: str, word: str) -> int:
    """``word`` 在 ``text`` 里的位置（找不到 = ``-1``）。

    英文按整词（不卡边界会 ``Man`` 撞 ``Manager``、``girl`` 撞 ``Schoolgirl``）；
    中文按子串（中文写法都是完整的词，子串匹配不会误伤 ``语音``/``耳语`` 这类）。
    """
    if not word.isascii():
        return text.find(word)
    pattern = _ASCII_PATTERNS.get(word)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", re.IGNORECASE)
        _ASCII_PATTERNS[word] = pattern
    match = pattern.search(text)
    return match.start() if match else -1


def _hits(table: tuple[tuple[str, tuple[str, ...]], ...], text: str) -> tuple[str, ...]:
    """这段字里提到了表里的哪些代码，按出现先后（同代码只出现一次）；
    顺序是结果的一部分（两个年龄词取先出现的）。"""
    found: list[tuple[int, str]] = []
    for code, words in table:
        positions = [at for at in (_find(text, word) for word in words) if at >= 0]
        if positions:
            found.append((min(positions), code))
    found.sort()
    out: list[str] = []
    for _at, code in found:
        if code not in out:
            out.append(code)
    return tuple(out)


def _first(table: tuple[tuple[str, tuple[str, ...]], ...], *texts: str) -> str:
    """**依次**看这几段字，返回第一个认得出的代码；都认不出 = 空串。

    调用方按"哪个更可信"排好顺序传进来（见 :func:`read_traits`）。空串的含义由调用方
    决定（本项目：字段留空、标签不加）。
    """
    for text in texts:
        codes = _hits(table, text or "")
        if codes:
            return codes[0]
    return ""


def genders_in(text: str) -> tuple[str, ...]:
    """这段字里提到了哪些性别（按出现先后）。"""
    return _hits(GENDERS, text)


def ages_in(text: str) -> tuple[str, ...]:
    """这段字里提到了哪些年龄感（按出现先后）。"""
    return _hits(AGES, text)


def languages_in(text: str) -> tuple[str, ...]:
    """这段字里提到了哪些语言（按出现先后）。"""
    return _hits(LANGUAGES, text)


def detect_gender(*texts: str) -> str:
    """依次看这几段字里的性别；都认不出 = 空串。"""
    return _first(GENDERS, *texts)


def detect_age(*texts: str) -> str:
    """依次看这几段字里的年龄感；都认不出 = 空串。"""
    return _first(AGES, *texts)


def characteristics_in(text: str) -> tuple[str, ...]:
    """这段字里提到了哪些音色特点（按出现先后，同一个词只算一次）。"""
    hits = [(at, word) for word in CHARACTERISTICS if (at := _find(text or "", word)) >= 0]
    out: list[str] = []
    for _at, word in sorted(hits):
        if word not in out:
            out.append(word)
    return tuple(out)


def detect_characteristics(*texts: str) -> tuple[str, ...]:
    """把这几段字认出的特点合起来（按"第几段、第几个位置"，去重）。

    不"第一个认出就停"：特点是可多选的，``傲娇御姐`` 的说明里还有别的特点要认。
    """
    out: list[str] = []
    for text in texts:
        for word in characteristics_in(text):
            if word not in out:
                out.append(word)
    return tuple(out)


def _language_code(text: str) -> str:
    """整段就是语言代码或 locale 标签时返回主标签；散文里的 ``id`` 不算。

    只认 ``zh`` / ``zh-CN`` / ``en_US`` 这种整段值——识别引擎回报的 ``language``
    就是这个形状；在散文里扫两字母代码会把 ``voice id`` 认成印尼语。
    """
    token = (text or "").strip().replace("_", "-").lower()
    if token in _LANGUAGE_CODES:
        return token
    primary = token.partition("-")[0]
    return primary if primary in _LANGUAGE_CODES else ""


def detect_language(*texts: str) -> str:
    """依次看这几段字里的语言；写法（Chinese）与 BCP-47 代码（zh）都认。"""
    for text in texts:
        code = _first(LANGUAGES, text or "") or _language_code(text or "")
        if code:
            return code
    return ""


# --------------------------------------------------------------------------- #
# 一次读全
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VoiceTraits:
    """从一份平台清单条目里读出来的属性（认不出的那几项是空值）。

    ``language`` / ``gender`` / ``age`` 各是**一个值**（多选一，落进各自的格子）；
    ``characteristics`` 是**一组词**（可多选，落成标签）。
    """

    language: str = ""
    gender: str = ""
    age: str = ""
    characteristics: tuple[str, ...] = ()


def read_traits(*, voice_id: str = "", name: str = "", description: str = "") -> VoiceTraits:
    """按可信度顺序读一份平台清单条目的属性。

    ``gender``：``voice_id`` → ``name`` → 说明；``age``：``name`` → 说明（不读 id，
    id 里的 ``Senior`` 指"学长"）；``language``：``voice_id`` → 说明；
    ``characteristics``：``name`` 与说明两处合读，不读 id。
    """
    return VoiceTraits(
        language=detect_language(voice_id, description),
        gender=detect_gender(voice_id, name, description),
        age=detect_age(name, description),
        characteristics=detect_characteristics(name, description),
    )
