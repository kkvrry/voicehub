"""SenseVoice-Small 的语言覆盖：audio.cpp 的 ``sense_asr`` 清单。

``auto`` 与 ``nospeech`` 不是语言：前者是"由模型判断"的提示值，后者是非语音
标签。这里只登记能填进「语种提示」的语言代码。
"""

from __future__ import annotations

from app.engines.asr_base import AsrLanguage, AsrLanguageGroup

__all__ = ["LANGUAGE_GROUPS", "LANGUAGE_SUMMARY"]

#: 界面上的短说法。
LANGUAGE_SUMMARY = "21 种语言"

LANGUAGE_GROUPS: tuple[AsrLanguageGroup, ...] = (
    AsrLanguageGroup(
        key="languages",
        label="支持语言",
        note="代码可填入「语种提示」；留空或填 auto 则由模型判断。",
        items=(
            AsrLanguage("zh", "中文"),
            AsrLanguage("en", "英语"),
            AsrLanguage("yue", "粤语"),
            AsrLanguage("ja", "日语"),
            AsrLanguage("ko", "韩语"),
            AsrLanguage("pt", "葡萄牙语"),
            AsrLanguage("ru", "俄语"),
            AsrLanguage("es", "西班牙语"),
            AsrLanguage("it", "意大利语"),
            AsrLanguage("fr", "法语"),
            AsrLanguage("de", "德语"),
            AsrLanguage("nl", "荷兰语"),
            AsrLanguage("pl", "波兰语"),
            AsrLanguage("tr", "土耳其语"),
            AsrLanguage("ar", "阿拉伯语"),
            AsrLanguage("hi", "印地语"),
            AsrLanguage("vi", "越南语"),
            AsrLanguage("th", "泰语"),
            AsrLanguage("id", "印尼语"),
            AsrLanguage("ms", "马来语"),
            AsrLanguage("fa", "波斯语"),
        ),
    ),
)
