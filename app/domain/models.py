"""音色与样例的领域模型。

identity 规矩：本地音色的身份 = 文件夹绝对路径的散列（同名音色在不同根目录各是一条）；
平台音色的身份 = ``平台名:音色id``（与 audiobook 的 ``引擎:音色`` 同形状，对接零换算）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

__all__ = [
    "AGES",
    "AGE_CHOICES",
    "AUDIO_EXTS",
    "EMOTIONS",
    "EMOTION_CHOICES",
    "EMOTION_GROUPS",
    "GENDERS",
    "GENDER_CHOICES",
    "LANGUAGES",
    "LANGUAGE_CHOICES",
    "SOURCE_LOCAL",
    "TIMBRES",
    "TIMBRE_CHOICES",
    "TONES",
    "TONE_CHOICES",
    "Sample",
    "Voice",
    "platform_voice_id",
    "voice_id_for",
]

#: 算作音频的扩展名。``.m4a/.ogg`` 读不了时长（本机无 ffprobe）但照样是可播的样例。
AUDIO_EXTS: tuple[str, ...] = (".wav", ".mp3", ".flac", ".m4a", ".ogg")

#: 本地音色的 source 值。
SOURCE_LOCAL = "local"

#: 性别与年龄感的候选词表：``(存进库的值, 界面上的说法)``。
#:
#: 值用稳定的英文键而非中文：改显示说法不用做数据迁移；说法由这里唯一给出，
#: 经 ``GET /api/v1/tree`` 的 ``vocab`` 下发，前端不自写中文。
#:
#: 候选**不是白名单**：词表外的值原样保留、原样显示。这是标签不是契约。
GENDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("female", "女声"),
    ("male", "男声"),
    ("other", "其他"),
)

#: 情绪的候选词表：**分组给出**，``(组名, (词, …))``。
#:
#: 分组只决定界面上怎么摆，不参与校验；词是规范词，**原样发给引擎**，本层不做翻译
#: （各家枚举的对照表在 ``app.engines.builtin.*.mapping``）。候选不是白名单。
#:
#: 基础情绪那一组与 MiMo 官方的基础情绪表逐字一致：改这里就是改给引擎的规范词，
#: 引擎侧的映射表要连着一起来（``tests/test_mimo.py`` 盯着）。这一组**不动**——
#: 它是各家枚举的公约数，动了等于给所有引擎重新谈一遍翻译。
#:
#: 「态度」是后来补的一组：**对人或对事的姿态**，念法与它对面的情绪不同
#: （越是个性活泼的角色，越需要这一组——整体语调是「活泼」，这一句可以是「严肃」。
#: 语调描述音色本身的质地，情绪描述这一句怎么念，两者可以打架，且本该允许打架）。
#: 它与 :data:`TONE_CHOICES` 里同名的词（``严肃``）是刻意的两格：那格挂在音色上，
#: 这格进合成参数。
#:
#: 入选标准只有一条：**它能不能改变这一句的念法，且现有词改变不了同样的东西**。
#: 所以「宁可」「倔强」「孤独」这类只能靠语义猜、念法上又落回悲伤/无奈的词没有进来。
EMOTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("基础情绪", ("开心", "悲伤", "愤怒", "恐惧", "惊讶", "兴奋", "委屈", "平静", "冷漠")),
    (
        "复合情绪",
        (
            "怅然", "欣慰", "无奈", "愧疚", "释然", "嫉妒", "厌倦", "忐忑", "动情",
            "害羞", "疑惑", "失望", "担忧",
        ),
    ),
    ("态度", ("严肃", "轻蔑", "恳求", "得意", "撒娇")),
)

#: 情绪的候选词表：``(值, 说法)``。由 :data:`EMOTION_GROUPS` 摊平，顺序即界面顺序。
EMOTION_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (word, word) for _, words in EMOTION_GROUPS for word in words
)

#: 角色卡（音色详情）的两组固定标签：``(值, 说法)``。
#:
#: 语调 = 怎么念，音色 = 声音本身的质地——两回事，分两格选，落成同一种东西（标签）。
#: 与情绪那两组一样：只是候选，词表外的自填词照旧保留。
#:
#: **两组都要有不好听的词**。这不是措辞问题，是能不能找回来的问题：平淡、木讷、絮叨、
#: 倨傲、发闷、单薄这些词，正是"这条下次别再用"和"这个角色恰好需要它"时唯一能把
#: 它翻出来的索引。只收夸人的词，这张表就是宣传语，不是索引。
#:
#: **顺序是谱系，不是拼音也不是频率**：意思挨着的排在一起，相邻两个词只差一点点，
#: 选的时候眼睛能顺着一条线走下来（温柔 → 轻柔 → 活泼 是"软到亮"的一段；
#: 严肃 → 威严 → 深沉 → 高冷 → 冷峻 是"压人"的一段）。改词表时请维持这个性质：
#: 新词插到它最像的那个词旁边，不要追加到末尾。
#:
#: 「轻柔」从音色挪到这里：它说的是"怎么说话"（收着、放缓），不是嗓音本身的质地；
#: 同一个词挂在两格里，选的时候会看见两颗一模一样的胶囊，一颗亮一颗不亮。
TONE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (word, word)
    for word in (
        # 软、暖
        "温柔", "轻柔",
        # 亮、外放
        "活泼", "俏皮", "戏谑",
        # 轻而浮（不讨人喜欢，但要找得出来）
        "轻浮", "絮叨",
        # 收得住、办事的口气
        "冷静", "沉稳", "严谨", "干脆", "干练",
        # 平到没有起伏
        "平淡", "木讷", "拘谨",
        # 压人
        "严肃", "威严", "深沉", "高冷", "冷峻", "凌厉", "倨傲", "刻薄", "急躁",
        # 松
        "慵懒",
    )
)

#: 音色：声音本身的质地。同一个词在不同情绪下仍然是同一个音色，所以这一格
#: 不写"怎么说话"，只写"听起来什么样"。
#:
#: 醇厚 / 醇雅 两个词已删：前者的意思由「浑厚」承担，后者是"醇"（厚度）与"雅"（气质）
#: 拼出来的，跨了轴——气质那一半属于语调，不该在音色格里。
#:
#: 「清沙」是新造的词：亮里带一点沙，汉语里没有现成说法（「烟嗓」偏低偏暗，
#: 不是这个意思）。取「清亮」与「沙哑」各一半，望文知义；如果更习惯别的叫法，
#: 改这一个字符串即可。
#:
#: 判据是**维度**，不是"还能想到什么词"：音高、厚度、亮度、颗粒、气、鼻化、
#: 圆润度、干湿、松紧、响度、年龄、甜度各有一两个词守着，缺一格才补一格。
#: 「洪亮」守响度（与「高亢」的音高是两回事），「发紧」守松紧，「干涩」守干湿，
#: 「清冷」是"干净但不近人"这一格，「金属感」是指认合成味的那一格——
#: 这个库里全是 TTS 音色，"听着像不像机器"本身就是要能标出来的一件事。
TIMBRE_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (word, word)
    for word in (
        # 厚、暗、暖
        "磁性", "浑厚", "低沉", "圆润", "软糯", "温润",
        # 亮
        "明亮", "清亮", "清脆", "高亢", "洪亮", "空灵", "清冷",
        # 亮里带沙 → 全沙 → 低而沙 → 气 → 鼻 → 电子
        "清沙", "沙哑", "烟嗓", "气声", "鼻音", "金属感",
        # 薄、紧、糊、干、糙
        "尖细", "单薄", "发紧", "发闷", "干涩", "粗糙", "含混",
        # 年龄与甜度
        "稚嫩", "甜美", "苍老",
    )
)

#: 年龄感：性别之外的独立一维（"少女" = 少年 + 女声，故不按性别分叉）。
AGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("child", "儿童"),
    ("teen", "少年"),
    ("young", "青年"),
    ("middle", "中年"),
    ("senior", "老年"),
)

#: 语言的候选词表：``(存进库的值, 界面上的说法)``，值用 BCP-47 主语言子标签。
#:
#: 与 ``traits.LANGUAGES`` 的分工：那边是「写法 → 代码」，这边是「代码 → 说法」；
#: 两张表的代码由测试钉住必须完全一致。
LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("zh", "中文"),
    ("yue", "粤语"),
    # 汉语方言按**方言点**列，比笼统的「中文」有用：「成都话」和「西安话」挑参考音频时
    # 是两回事。代码 = ISO 639-3 主语言 + 方言点两字母（``cmn`` 官话、``wuu`` 吴语、
    # ``nan`` 闽南语、``hsn`` 湘语），不是 BCP-47 的行政区码。
    ("cmn-cd", "成都话"),
    ("cmn-wh", "武汉话"),
    ("cmn-xa", "西安话"),
    ("cmn-zz", "郑州话"),
    ("cmn-jn", "济南话"),
    ("cmn-nj", "南京话"),
    ("wuu-sz", "苏州话"),
    ("wuu-hz", "杭州话"),
    ("nan-qz", "泉州话"),
    ("hsn-cs", "长沙话"),
    ("en", "英语"),
    ("ja", "日语"),
    ("ko", "韩语"),
    ("es", "西班牙语"),
    ("fr", "法语"),
    ("de", "德语"),
    ("ru", "俄语"),
    ("pt", "葡萄牙语"),
    ("it", "意大利语"),
    ("ar", "阿拉伯语"),
    ("nl", "荷兰语"),
    ("tr", "土耳其语"),
    ("uk", "乌克兰语"),
    ("id", "印尼语"),
    ("vi", "越南语"),
)

#: 性别标签的合法取值（"" = 未标）。严格校验：库里的既有约定。
GENDERS: tuple[str, ...] = tuple(value for value, _ in GENDER_CHOICES)

#: 年龄感的合法取值。只用于渲染候选，不参与校验。
AGES: tuple[str, ...] = tuple(value for value, _ in AGE_CHOICES)

#: 语言的合法取值。只用于渲染候选与筛选，不参与校验（认不出即空串）。
LANGUAGES: tuple[str, ...] = tuple(value for value, _ in LANGUAGE_CHOICES)

#: 情绪的合法取值。"自动补值"层用它判断主样例的情绪候选（文件名剩余段可能不是情绪）。
EMOTIONS: tuple[str, ...] = tuple(value for value, _ in EMOTION_CHOICES)

#: 角色卡两组固定标签的合法取值（只用于渲染候选，不参与校验）。
TONES: tuple[str, ...] = tuple(value for value, _ in TONE_CHOICES)
TIMBRES: tuple[str, ...] = tuple(value for value, _ in TIMBRE_CHOICES)


def voice_id_for(folder: str) -> str:
    """本地音色 id：规范化路径的散列（**路径动 = 音色换人**，重扫自然脱钩）。"""
    import os

    norm = os.path.normcase(os.path.abspath(folder))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def platform_voice_id(platform: str, voice_id: str) -> str:
    """平台音色 id：``平台名:音色id``——``:`` 在 Windows 文件名里不允许，撞不了本地。"""
    return f"{platform.strip()}:{voice_id.strip()}"


@dataclass(frozen=True, slots=True)
class Sample:
    """一条音频样例。

    :param file: 文件名（folder 内唯一，也是它在 API 里的身份）。
    :param text: 说话内容——合成参数的一部分（参考文本），不是备注。
    :param emotion: 情绪标签（扫描时从文件名推导，可编辑）。
    :param duration: 秒；读不出 = ``0.0``（界面显示「未知」）。
    :param tags: 本条自己的标签，只挂在样例行上，不进音色的标签云。
    :param params: 生成这段时用的参数（``(键, 值)``），仅生成出来的样例有；
        值统一存字符串，用途是让人看清这一段的来历。用元组保持 ``Sample`` 可哈希。
    """

    file: str = ""
    text: str = ""
    emotion: str = ""
    duration: float = 0.0
    tags: tuple[str, ...] = ()
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Voice:
    """一个音色。``display or name`` 是唯一的显示名回落规则，前端不自写 ``||``。"""

    id: str = ""
    source: str = SOURCE_LOCAL  # "local" | 平台名
    name: str = ""  # 人物名 / 平台音色 id
    display: str = ""  # 显示名；空 = 用 name
    category: str = ""  # 分类（本地 = 相对扫描根的目录路径，``/`` 连接；平台 = 平台给的）
    gender: str = ""
    age: str = ""
    #: 语言：与性别、年龄感并排的第三个筛选维度。本地音色只能手动填；平台音色由
    #: :mod:`app.engines.traits` 认出。认出来的不许盖掉用户填过的。
    language: str = ""
    tags: tuple[str, ...] = ()
    description: str = ""
    folder: str = ""  # 本地音色才有
    cover: str = ""  # 封面文件名（相对 folder）
    hidden: bool = False
    favorite: bool = False
    samples: tuple[Sample, ...] = field(default=())
    main_sample: str = ""  # 当前生效的主样例文件名（推导结果，不是配置原值）
    #: 引擎私有的“这条音色怎么再生成”的凭据（只平台音色用）。例如 MiMo 的复刻音色
    #: 存 ``{"ref_audio": "<绝对路径>"}``——平台没有音色 id，每次合成都要带这段样本。
    payload: dict = field(default_factory=dict)
    #: 平台归属：这个文件夹的 ``voice.json`` 里 ``origin.voice_id`` 记的那条引擎音色
    #: （``平台名:音色id``）。有值 = 它只是那条平台音色在磁盘上的样例目录，
    #: 按平台音色出现在界面上（见 ``LibraryService._with_generated``）；空 = 普通本地音色。
    origin_id: str = ""
    #: 样例条数——列表查询不装 samples，计数由数据库聚合子查询填。
    sample_count: int = 0

    @property
    def shown_name(self) -> str:
        """界面上那一行显示什么——**回落只有这一处**。"""
        return self.display or self.name

    @property
    def is_local(self) -> bool:
        return self.source == SOURCE_LOCAL

    @property
    def has_sample(self) -> bool:
        """手上有可当参考的音频。

        样例目录、主样例、样例计数（列表查询只带计数、不装 ``samples``）都算。
        复刻凭据 ``payload.ref_audio`` 不算：那是"平台没保存的音色"，由
        :func:`app.domain.credential.credential_of` 单独判。
        """
        if self.folder and (self.samples or self.main_sample):
            return True
        return int(self.sample_count or 0) > 0
