"""统一参数 → GPT-SoVITS 请求体。

这一家的形状与云端那几家不同：它没有「音色 ID」这个概念——**要用的声音就是参考音频
本身**。所以 ``reference_audio`` / ``reference_text`` 在这里不是可选装饰，而是主体；
反过来 ``voice_id`` 没有落点，界面的映射表会把它列成「无落点」并说明原因。

语言有两格（正文 / 参考），服务端两个字段都必填。上游给了**它认得的**代码就用上游的，
认不得（西班牙语、法语这些统一词表里有、它没收的）就剔掉这一格、改用配置里的默认值——
整章里混一句外语不该让整段合成失败。
"""

from __future__ import annotations

from typing import Any

from app.engines.mapping import EngineMapping, ParamMapping, register_transform

__all__ = ["MAPPING", "SUPPORTED_LANGUAGES"]

#: 服务端 ``check_params`` 会校验的语言代码（``configs/TTS.py`` 的 ``v2_languages``）。
#: v1 的模型只认前六个，这里列全是因为"服务端是什么版本"客户端问不到。
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "auto",
        "en",
        "zh",
        "ja",
        "yue",
        "ko",
        "auto_yue",
        "all_zh",
        "all_ja",
        "all_yue",
        "all_ko",
    }
)


def _language(value: Any) -> str | None:
    """统一语言代码 → 这一家认的代码；认不得给 ``None``（装配时剔掉这一格）。

    统一词表用 BCP-47（``zh``/``es``/``fr``…），这一家只收中英日韩粤那一套。
    认不出的不能硬发：服务端会回 400 并带上版本号，一次 400 会拖死整章合成。
    """
    text = str(value).strip().lower()
    return text if text in SUPPORTED_LANGUAGES else None


register_transform("gsv_language", _language, label="语言代码")


#: 这张表就是「GPT-SoVITS 要哪几个键」的答案（字段名照官方 ``api_v2.py`` 的 ``/tts``）。
MAPPING = EngineMapping(
    params=(
        ParamMapping("text", "text", required=True, note="要合成的正文。"),
        # 本引擎的主体：参考音频。**必填**——没有它这一家合成不出「谁的声音」，
        # 而它缺了正是「平台音色喂给了本地引擎」这个错误最可能的样子。
        ParamMapping(
            "reference_audio",
            "ref_audio_path",
            required=True,
            note="音色来源；服务端可读的本机绝对路径。",
            warning=True,
        ),
        # 参考音频念的那句话。v1/v2 可空（省了它就少一点相似度），v3+ 必填。
        ParamMapping(
            "reference_text",
            "prompt_text",
            note="参考音频的逐字稿；v3 / v4 模型必需。",
            warning=True,
            label="参考文本",
        ),
        ParamMapping("format", "media_type", note="输出音频容器。"),
        # 上游给了就用上游的，没给就用配置里的默认值（见 plugin.build_body）。
        ParamMapping("speed", "speed_factor", transform="number", note="语速倍率。"),
        ParamMapping("seed", "seed", transform="number", note="随机种子。"),
        # 语言：上游值在服务端的白名单里才发；不在就剔掉，用配置里那两格。
        ParamMapping(
            "language",
            "text_lang",
            transform="gsv_language",
            drop_unmapped=True,
            note="正文语言；服务端不认的代码会改用引擎配置。",
        ),
        ParamMapping(
            "reference_language",
            "prompt_lang",
            transform="gsv_language",
            drop_unmapped=True,
            note="参考语言；服务端不认的代码会改用引擎配置。",
        ),
    ),
    # 要**一整段音频**，不要流式分片：分片是给边播边放的场景准备的，
    # 而中转站的调用方（有声书）要的是能落盘的完整文件。
    constants={"streaming_mode": False},
    cautions=(
        "参考音频路径按 GPT-SoVITS 服务端视角填写；服务不在本机时不能直接用本机路径。",
        "参考音频须在 3–10 秒内；超出的会截取到上限以内再发，短于 3 秒的直接拒绝。",
        "模型权重由服务端启动参数决定，不在请求里；换模型需要重启或切换服务端权重。",
        "正文中的语气词标签一条都不认，装配时会从正文删除。",
    ),
)
