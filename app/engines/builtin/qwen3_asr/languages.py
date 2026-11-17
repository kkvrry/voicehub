"""Qwen3-ASR 官方声明的语言覆盖：30 种语言 + 22 种中文方言。

清单照 Qwen3-ASR 模型卡逐项登记。语言的 ``code`` 是官方列表里的 id，「语种提示」
可以直接填；方言没有独立语言代码，保持空串。这里只做登记，不参与识别逻辑。
"""

from __future__ import annotations

from app.engines.asr_base import AsrLanguage, AsrLanguageGroup

__all__ = ["LANGUAGE_GROUPS", "LANGUAGE_SUMMARY"]

#: 界面上的短说法（两张表的合计）。
LANGUAGE_SUMMARY = "30 种语言 · 22 种中文方言"

LANGUAGE_GROUPS: tuple[AsrLanguageGroup, ...] = (
    AsrLanguageGroup(
        key="languages",
        label="通用语言",
        note="代码可填入「语种提示」；留空则由模型判断。",
        items=(
            AsrLanguage("zh", "中文"),
            AsrLanguage("en", "英语"),
            AsrLanguage("yue", "粤语"),
            AsrLanguage("ar", "阿拉伯语"),
            AsrLanguage("de", "德语"),
            AsrLanguage("fr", "法语"),
            AsrLanguage("es", "西班牙语"),
            AsrLanguage("pt", "葡萄牙语"),
            AsrLanguage("id", "印尼语"),
            AsrLanguage("it", "意大利语"),
            AsrLanguage("ko", "韩语"),
            AsrLanguage("ru", "俄语"),
            AsrLanguage("th", "泰语"),
            AsrLanguage("vi", "越南语"),
            AsrLanguage("ja", "日语"),
            AsrLanguage("tr", "土耳其语"),
            AsrLanguage("hi", "印地语"),
            AsrLanguage("ms", "马来语"),
            AsrLanguage("nl", "荷兰语"),
            AsrLanguage("sv", "瑞典语"),
            AsrLanguage("da", "丹麦语"),
            AsrLanguage("fi", "芬兰语"),
            AsrLanguage("pl", "波兰语"),
            AsrLanguage("cs", "捷克语"),
            AsrLanguage("fil", "菲律宾语"),
            AsrLanguage("fa", "波斯语"),
            AsrLanguage("el", "希腊语"),
            AsrLanguage("hu", "匈牙利语"),
            AsrLanguage("mk", "马其顿语"),
            AsrLanguage("ro", "罗马尼亚语"),
        ),
    ),
    AsrLanguageGroup(
        key="dialects",
        label="中文方言",
        note="方言没有独立语言代码。",
        items=(
            AsrLanguage("", "安徽话"),
            AsrLanguage("", "东北话"),
            AsrLanguage("", "福建话"),
            AsrLanguage("", "甘肃话"),
            AsrLanguage("", "贵州话"),
            AsrLanguage("", "河北话"),
            AsrLanguage("", "河南话"),
            AsrLanguage("", "湖北话"),
            AsrLanguage("", "湖南话"),
            AsrLanguage("", "江西话"),
            AsrLanguage("", "宁夏话"),
            AsrLanguage("", "山东话"),
            AsrLanguage("", "陕西话"),
            AsrLanguage("", "山西话"),
            AsrLanguage("", "四川话"),
            AsrLanguage("", "天津话"),
            AsrLanguage("", "云南话"),
            AsrLanguage("", "浙江话"),
            AsrLanguage("", "粤语（香港口音）"),
            AsrLanguage("", "粤语（广东口音）"),
            AsrLanguage("", "吴语"),
            AsrLanguage("", "闽南语"),
        ),
    ),
)
