"""Kitta Audio 的 HTTP 调用——"怎么发"是这一家自己的事，留在它的目录里。

端点与字段照官方文档（``docs.kittaaudio.com``）：

==============  ==================================  ==================================
用途            端点                                形状
==============  ==================================  ==================================
能力目录        ``GET /api/open/v3/speech/tts/capabilities``  无需鉴权（本插件不调用）
音色清单        ``GET /api/open/v3/voices``                    查询串；``items`` 数组
同步合成        ``POST /api/open/v3/speech/tts``               JSON 进、**音频字节出**
音色克隆        ``POST /api/open/v1/voices``                   ``multipart/form-data``
==============  ==================================  ==================================

**三处与别家不同的地方**：

1. 合成成功**没有 JSON 信封**，响应体就是音频；错误反而一律是 JSON
   ``{code, message, requestId}``。所以先看状态码，再决定按音频还是按错误处理。
2. 平台会**静默忽略**当前模型不支持的控制参数，把参数名列在
   ``X-OpenAPI-Ignored-Parameters`` 里。这个头必须转成用户看得见的说明——否则
   "调了没生效"永远查不出来。
3. 请求合同很严：``provider``、供应商原生字段、其它未知字段都是 400。本插件只发文档
   里列出的那几个键。

错误文案**只报平台原话**，另附 ``requestId``：平台支持人员定位问题要的就是它。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.engines.audio_http import audio_bytes, net_error
from app.engines.base import CODE_RATE_LIMITED, EngineError, SynthesisResult, VoiceList, param_label
from app.engines.builtin.kitta.settings import TTS_BASE_PATH, KittaSettings
from app.engines.builtin.kitta.voices import parse_voice_list
from app.engines.http_pool import borrow, post_within

__all__ = [
    "CLONE_PATH",
    "MAX_SAMPLE_BYTES",
    "TTS_PATH",
    "VOICES_PATH",
    "create_voice",
    "fetch_voices",
    "headers",
    "synthesize",
]

TTS_PATH = f"{TTS_BASE_PATH}/speech/tts"
VOICES_PATH = f"{TTS_BASE_PATH}/voices"
#: 克隆走 v1：文档里创建音色只有这一条路。
CLONE_PATH = "/api/open/v1/voices"

PAGE_SIZE = 100

#: 单个音源文件上限（官方 10MB）。
MAX_SAMPLE_BYTES = 10 * 1024 * 1024

#: 允许的音源扩展名（官方列表）。
SAMPLE_EXTS: tuple[str, ...] = (
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
)

#: 状态码 → 一句事实说明（不含推测；平台原话接在后面）。
_HINTS: dict[int, str] = {
    401: "密钥无效或缺失",
    402: "API 额度不足",
    404: "音色或任务不存在",
    409: "幂等键冲突或请求正在处理",
    413: "请求体超过上限",
    503: "所选模型当前不可用",
}


def headers(cfg: KittaSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_key.strip()}"}


def _client(cfg: KittaSettings):
    # ``trust_env=True``：云端引擎在配了 HTTP(S)_PROXY 的机器上照常走代理。
    return borrow(
        base_url=cfg.host,
        headers=headers(cfg),
        timeout=float(cfg.timeout),
        trust_env=True,
        follow_redirects=True,
    )


def _reason(response: Any) -> str:
    """错误体 ``{code, message, requestId}`` → 平台原话；``requestId`` 附在后面。

    保留 ``requestId`` 是给平台支持用的：没有它，用户只能描述"报了个错"。
    """
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] if text else "（响应体是空的）"
    if not isinstance(payload, dict):
        return str(payload)[:200]
    message = str(payload.get("message") or "").strip()
    code = str(payload.get("code") or "").strip()
    request_id = str(payload.get("requestId") or "").strip()
    parts = [message or "未提供原因"]
    if code:
        parts.append(f"code={code}")
    if request_id:
        parts.append(f"requestId={request_id}")
    return "，".join(parts)


def _ignored_notes(response: Any) -> tuple[str, ...]:
    """``X-OpenAPI-Ignored-Parameters`` → 逐条说明。

    平台"接受了请求但没采用这个参数"时只在这条头里说。不转出来，用户听到的是
    "调了没反应"，而日志里也看不出原因。
    """
    raw = str(response.headers.get("x-openapi-ignored-parameters") or "").strip()
    if not raw:
        return ()
    names = [part.strip() for part in raw.split(",") if part.strip()]
    notes: list[str] = []
    for name in names:
        label = param_label(name)
        shown = f"{name}（{label}）" if label != name else name
        notes.append(f"{shown} 当前模型不支持，平台已忽略。")
    return tuple(notes)


def _credits_note(response: Any) -> str:
    used = str(response.headers.get("x-openapi-credits-used") or "").strip()
    return f"本次消耗 {used} 积分" if used else ""


async def fetch_voices(cfg: KittaSettings, *, label: str) -> VoiceList:
    """拉音色清单（``GET /v3/voices``）。**拉不到不抛异常**，把理由写进 ``why``。"""
    import httpx

    if not cfg.configured:
        return VoiceList(why="还没填 API Key。")

    params: dict[str, str] = {"page": "1", "pageSize": str(PAGE_SIZE)}
    if cfg.voice_scope == "personal":
        params["modelType"] = "personal"
    elif cfg.voice_scope == "public":
        params["includePersonal"] = "false"
    else:
        params["includePersonal"] = "true"

    try:
        async with _client(cfg) as client:
            response = await client.get(VOICES_PATH, params=params)
    except httpx.HTTPError as exc:
        return VoiceList(why=net_error(exc, cfg, label=label, step="拉音色清单").reason)

    if response.status_code >= 400:
        return VoiceList(
            why=f"拉音色清单失败：HTTP {response.status_code}。{_reason(response)}"
        )
    try:
        payload = response.json()
    except ValueError:
        return VoiceList(why="拉音色清单失败：响应不是 JSON。")

    items = parse_voice_list(payload)
    if items:
        return VoiceList(items=items)
    if cfg.voice_scope == "personal":
        return VoiceList(why="账号下还没有个人音色——先用「音色克隆」建一个。")
    return VoiceList(why="音色清单返回 0 条，换一个拉取范围或稍后重试。")


async def synthesize(
    cfg: KittaSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """同步合成（``POST /v3/speech/tts``）。``body`` 由映射表装配好。"""
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")

    fmt = str(body.get("format") or cfg.audio_format)
    try:
        async with _client(cfg) as client:
            response = await post_within(
                client, TTS_PATH, seconds=float(cfg.timeout), json=body
            )
    except httpx.HTTPError as exc:
        raise net_error(exc, cfg, label=label, step="合成") from exc

    if response.status_code == 429:
        raise EngineError(
            f"{label} 合成失败：HTTP 429（请求过多）。{_reason(response)}",
            retryable=True,
            code=CODE_RATE_LIMITED,
        )
    if response.status_code >= 400:
        hint = _HINTS.get(response.status_code, "")
        prefix = f"{hint}。" if hint else ""
        raise EngineError(
            f"{label} 拒绝了这次合成：{prefix}HTTP {response.status_code}。{_reason(response)}"
        )

    audio = audio_bytes(
        response, label, hint="可能是正文为空、音色 id 无效或格式不被该模型接受"
    )
    return SynthesisResult(
        audio=audio,
        format=fmt,
        cost_note=_credits_note(response),
        notes=_ignored_notes(response),
    )


async def create_voice(
    cfg: KittaSettings,
    *,
    name: str,
    audio_path: str,
    transcript: str,
    description: str,
    label: str,
) -> dict[str, Any]:
    """克隆（``POST /v1/voices``）：上传参考音频换一个可复用的 ``voiceId``。

    本地能判的（文件在不在、格式收不收、有没有超过 10MB）都排在发请求之前：
    上传一次是白花的，而且平台回的那句 400 只说"文件不支持"。
    """
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")
    source = Path(audio_path)
    if not source.is_file():
        raise EngineError(f"音源文件不存在：{audio_path}")
    if source.suffix.lower() not in SAMPLE_EXTS:
        raise EngineError(
            f"音源容器不受支持：{source.suffix or '（无扩展名）'}。"
            f"平台收：{'、'.join(ext.lstrip('.') for ext in SAMPLE_EXTS)}。"
        )
    size = source.stat().st_size
    if size > MAX_SAMPLE_BYTES:
        raise EngineError(
            f"音源 {size / 1024 / 1024:.1f} MB，超过平台上限 10 MB。切一段更短的音频。"
        )

    fields: list[tuple[str, str]] = [("name", name), ("visibility", "private")]
    if description:
        fields.append(("description", description))
    if transcript:
        fields.append(("referenceText", transcript))

    try:
        async with _client(cfg) as client:
            # 音源不超过 10MB，交给 httpx 读一次即可。
            with source.open("rb") as handle:  # noqa: ASYNC230 - 见上
                response = await client.post(
                    CLONE_PATH,
                    data=fields,
                    files={"audioFiles": (source.name, handle, "application/octet-stream")},
                )
    except httpx.HTTPError as exc:
        raise net_error(exc, cfg, label=label, step="音色克隆") from exc

    if response.status_code == 429:
        raise EngineError(
            f"{label} 克隆失败：HTTP 429（请求过多）。{_reason(response)}",
            retryable=True,
            code=CODE_RATE_LIMITED,
        )
    if response.status_code >= 400:
        hint = _HINTS.get(response.status_code, "")
        prefix = f"{hint}。" if hint else ""
        raise EngineError(
            f"{label} 拒绝了这次克隆：{prefix}HTTP {response.status_code}。{_reason(response)}"
        )
    try:
        payload = response.json()
    except ValueError:
        raise EngineError(f"{label} 克隆返回的不是 JSON，无法取得音色 id。") from None
    if not isinstance(payload, dict) or not str(payload.get("voiceId") or "").strip():
        raise EngineError(f"{label} 克隆成功但响应缺少 voiceId，无法入库。")
    return payload
