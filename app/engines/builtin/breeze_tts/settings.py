"""BreezeTTS 2 引擎的配置模型——**前端照这个模型的 JSON Schema 直接渲染表单**。

字段分三类：

1. **服务地址**（``base_url``）：**本机自己起的** audio.cpp 推理服务（默认 8080）。
   它不是云平台，没有密钥。
2. **引导指令与 CFG**（``instruction`` / ``guidance_scale``）：请求里没给时用它们。
3. **采样与长文本**：请求里没给时用它们。

**``guidance_scale`` 是"有引导时"的 CFG，默认 4**
--------------------------------------------------

官方 README 用 ``--cfg-scale 4`` 加强指令遵循（``strengthen instruction-following``），
而 1 是引擎自己的中性默认。插件因此按**这次有没有引导**决定默认：有引导（指令或
情绪转写的引导语）时用这一格（默认 4），没有引导时固定发 1——没有引导时给 4 只是
白跑一遍无条件分支。这一格填 ≤ 1 时，遇到有引导的请求会回落到 4；要强制 1，请在
请求里明确给 ``guidance_scale``（0.125 就是 1）。

**``instruction`` 的默认值是空串**，不是一句"看起来合理"的引导语。空串的含义是
「没有要求」——这时若有情绪，情绪会写成一句引导语；两者都没有才留给引擎自己的
默认（``Speak clearly and naturally.``）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.base import ui_hints

__all__ = [
    "CHUNK_MODE_CHOICES",
    "DEFAULT_BASE_URL",
    "DEFAULT_GUIDANCE_SCALE",
    "DEFAULT_MODEL",
    "BreezeTtsSettings",
]

#: audio.cpp 的服务默认端口（``cpp/server.json`` 里那个 ``port``）。
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: 模型 id——**必须与 ``cpp/server.json`` 的 ``models[].id`` 一致**，
#: 否则后端回 ``unknown model id: ...``。
DEFAULT_MODEL = "breeze-tts-2"

#: 官方 README 给强引导场景的推荐值（``--cfg-scale 4``）。
DEFAULT_GUIDANCE_SCALE = 4.0

#: 长文本分块模式——名字照抄引擎自己的规格（``model_spec.options.request``），
#: **不替它解释语义**：这四种各自怎么切只有引擎知道。
#:
#: ``tag_aware`` 值得单独提一句：正文里带 ``[笑]`` 这类标记时，它按标记感知来切，
#: 标记不容易掉到两段中间（这个是名字看出来的，**不是实测结论**）。
CHUNK_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    ("default", "default"),
    ("tag_aware", "tag_aware · 照顾正文标记"),
    ("japanese", "japanese"),
    ("endline", "endline"),
)


class BreezeTtsSettings(BaseModel):

    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description=(
            "本机 audio.cpp 推理服务地址（整合包里的 audiocpp_server.exe）。"
            "端口在 cpp/server.json 里配，默认 8080。"
        ),
        json_schema_extra=ui_hints("服务地址", group="连接与服务"),
    )
    model: str = Field(
        default=DEFAULT_MODEL,
        description=(
            "模型 id，必须与 cpp/server.json 里 models[].id 一致。"
        ),
        json_schema_extra=ui_hints("模型", group="连接与服务"),
    )
    instruction: str = Field(
        default="",
        description=(
            "一句自然语言，描述这一段该怎么念、或者这个音色是什么样。"
            "留空时若有情绪，会把情绪写成一句引导语；两者都没有才用引擎自己的默认"
            "（Speak clearly and naturally.）。"
        ),
        json_schema_extra=ui_hints("引导指令", group="引导指令", widget="textarea"),
    )
    guidance_scale: float = Field(
        default=DEFAULT_GUIDANCE_SCALE,
        ge=0.0,
        description=(
            "有引导时用的 CFG 强度（这一家自己的刻度，0–8）。官方推荐 4；"
            "没有引导时固定发 1，这一格不生效。生成页的「指令强度」是 0–1 刻度，"
            "0.5 → 4。这一格填 ≤ 1 会被当作「未设置」，遇到有引导的请求按 4 发"
            "——要强制 1，只能在请求里明确给 guidance_scale（0.125 对应 1）。"
        ),
        json_schema_extra=ui_hints("CFG 强度（0–8）", group="引导指令", widget="number"),
    )
    temperature: float = Field(
        default=0.9,
        ge=0.0,
        description="主干首个码本的采样温度。调低更稳，调高更活。",
        json_schema_extra=ui_hints("温度", group="采样", widget="number"),
    )
    depth_temperature: float = Field(
        default=0.9,
        ge=0.0,
        description="深度解码器码本的采样温度。整段听起来「含糊 / 发飘」时先动它。",
        json_schema_extra=ui_hints("深度温度", group="采样", widget="number"),
    )
    top_k: int = Field(
        default=50,
        ge=0,
        description="只从概率最高的 K 个里选；0 = 关掉这项过滤。",
        json_schema_extra=ui_hints("top_k", group="采样", widget="number"),
    )
    top_p: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="核采样阈值。1 = 不裁剪。",
        json_schema_extra=ui_hints("top_p", group="采样", widget="number"),
    )
    max_tokens: int = Field(
        default=1500,
        ge=1,
        description="最多生成多少个声学帧。这是一段的**时长上限**，不是文本字数。",
        json_schema_extra=ui_hints("帧数上限", group="采样", widget="number"),
    )
    text_chunk_size: int = Field(
        default=600,
        ge=1,
        description=(
            "长文本每块最多多少个字符（引擎自己的默认就是 600）。"
            "切小一点有时更稳，代价是段与段之间的接缝更多。"
        ),
        json_schema_extra=ui_hints("分块字数", group="长文本", widget="number"),
    )
    text_chunk_mode: str = Field(
        default="default",
        description="长文本分块方式。非必要别改。",
        json_schema_extra=ui_hints("分块方式", group="长文本", widget="select", choices=CHUNK_MODE_CHOICES),
    )
    seed: int = Field(
        default=-1,
        description=(
            "生成种子。-1 = 每次随机（插件在发出去之前抽一个，并把抽到的数告诉你——"
            "引擎自己不收负数，且省略 seed 时引擎用定值 0，随机必须显式抽）。"
            "0 及以上 = 固定：同一个数、同一段文本，出来的音频逐字节相同"
            "（实测：省略 seed 与 seed=0 输出完全一致）。"
        ),
        json_schema_extra=ui_hints("随机种子", group="采样", widget="number"),
    )
    timeout: float = Field(
        default=600.0,
        gt=0,
        description=(
            "单次请求超时（秒）。本机推理没有并发优势，长文本要慢慢等；"
            "开 CFG 会让耗时翻倍。别设太小。"
        ),
        json_schema_extra=ui_hints("超时", group="连接与服务", widget="number"),
    )

    @property
    def host(self) -> str:
        """可用的服务地址：补协议、去尾部斜杠；留空则回落默认端口。

        补 ``http://`` 是因为省掉协议头太常见了（``127.0.0.1:8080``），而 httpx
        对它的报错是 ``unsupported protocol``——那句话不会让人想到"少写了 http://"。
        """
        raw = (self.base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        return raw if "://" in raw else f"http://{raw}"
