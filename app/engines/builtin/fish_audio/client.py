"""Fish Audio 的 HTTP 调用——"怎么发"是这一家自己的事，留在它的目录里。

端点与字段照官方文档（``docs.fish.audio``）：

==============  ==============================  ==================================
用途            端点                            形状
==============  ==============================  ==================================
合成            ``POST /v1/tts``                 JSON；响应体**就是音频字节**
音色清单        ``GET /model``                   查询串；``items`` 数组
音色克隆        ``POST /model``                  ``multipart/form-data``，返回 ``_id``
==============  ==============================  ==================================

**三处容易踩的**，都体现在代码里：

1. 合成成功**没有 JSON 信封**：200 的响应体是音频，``content-type`` 是 ``audio/*``。
   把 200 当 JSON 解析只会得到一句"解析失败"，而真正的原因在 4xx 的 JSON 里。
2. **模型走请求头** ``model``，不在请求体。漏了它平台按默认模型处理，请求照样成功，
   换成"读法不对"也很难回溯。
3. 克隆是 **multipart**，``voices`` 是文件字段、``texts`` 是与文件一一对应的逐字稿；
   批量字段要按重复键提交，不能拼成数组字符串。

错误一律翻成 :class:`EngineError`，**只报平台的原话**，必要时在前面补一句这一步在做什么。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.engines.audio_http import audio_bytes, error_detail, net_error
from app.engines.base import CODE_RATE_LIMITED, EngineError, SynthesisResult, VoiceList
from app.engines.builtin.fish_audio.settings import FishAudioSettings
from app.engines.builtin.fish_audio.voices import parse_voice_list
from app.engines.http_pool import borrow, post_within

__all__ = [
    "MODELS_PATH",
    "PAGE_SIZE",
    "TTS_PATH",
    "create_voice",
    "fetch_voices",
    "headers",
    "synthesize",
]

TTS_PATH = "/v1/tts"
MODELS_PATH = "/model"

#: 一次拉多少条。平台分页，插件只取第一页（更多请用更窄的拉取范围）。
PAGE_SIZE = 50

#: 克隆的固定参数。``fast`` = 建完即可用；``private`` = 不进公开音色库。
TRAIN_MODE = "fast"
VISIBILITY = "private"


def headers(cfg: FishAudioSettings, *, model: str = "") -> dict[str, str]:
    out = {"Authorization": f"Bearer {cfg.api_key.strip()}"}
    if model:
        out["model"] = model
    return out


def _client(cfg: FishAudioSettings, *, model: str = ""):
    # ``trust_env=True``：云端引擎在配了 HTTP(S)_PROXY 的机器上照常走代理。
    return borrow(
        base_url=cfg.host,
        headers=headers(cfg, model=model),
        timeout=float(cfg.timeout),
        trust_env=True,
        follow_redirects=True,
    )


def _reason(response: Any) -> str:
    """4xx/5xx 的响应体 → 平台原话。

    这一家的错误体是 ``{"status": …, "message": …, "reason": …}``：``message`` 常是
    英文短语，``reason`` 常常为空。两者拼起来，都不给时退回整个响应体。
    """
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] if text else "（响应体是空的）"
    if isinstance(payload, dict):
        parts = [str(payload.get(key) or "").strip() for key in ("message", "reason")]
        joined = "：".join(part for part in parts if part)
        if joined:
            return joined
    return error_detail(response)


#: 状态码 → 一句事实说明（不含推测；平台原话接在后面）。
_HINTS: dict[int, str] = {
    401: "密钥无效或缺失",
    402: "账户余额不足",
    403: "密钥没有调用权限",
    422: "请求参数不被接受",
}


async def fetch_voices(cfg: FishAudioSettings, *, label: str) -> VoiceList:
    """拉音色清单（``GET /model``）。**拉不到不抛异常**，把理由写进 ``why``。"""
    import httpx

    if not cfg.configured:
        return VoiceList(why="还没填 API Key。")

    params: dict[str, str] = {"page_size": str(PAGE_SIZE)}
    if cfg.voice_scope == "self":
        params["self"] = "true"
        params["sort_by"] = "created_at"

    try:
        async with _client(cfg) as client:
            response = await client.get(MODELS_PATH, params=params)
    except httpx.HTTPError as exc:
        return VoiceList(
            why=net_error(exc, cfg, label=label, step="拉音色清单").reason
        )

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
    if cfg.voice_scope == "self":
        return VoiceList(
            why="账号下还没有可用音色——先用「音色克隆」建一个，"
            "或把「拉取范围」改为公开音色库。"
        )
    return VoiceList(why="公开音色库没有返回条目。")


async def synthesize(
    cfg: FishAudioSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成（``POST /v1/tts``）。``body`` 由映射表装配好（见 ``mapping``）。"""
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")

    fmt = str(body.get("format") or cfg.audio_format)
    try:
        async with _client(cfg, model=cfg.model) as client:
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
        response, label, hint="可能是正文为空、音色 id 无效或模型不支持该格式"
    )
    return SynthesisResult(audio=audio, format=fmt)


async def create_voice(
    cfg: FishAudioSettings,
    *,
    title: str,
    audio_path: str,
    transcript: str,
    description: str,
    label: str,
) -> dict[str, Any]:
    """克隆（``POST /model``）：上传一段音源换一个可复用的音色 id。

    返回平台原始对象——调用方要拿 ``_id`` 入库、拿 ``state`` 说话，这里不做取舍。
    """
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")
    source = Path(audio_path)
    if not source.is_file():
        raise EngineError(f"音源文件不存在：{audio_path}")

    fields: list[tuple[str, str]] = [
        ("type", "tts"),
        ("title", title),
        ("train_mode", TRAIN_MODE),
        ("visibility", VISIBILITY),
    ]
    if description:
        fields.append(("description", description))
    # ``texts`` 与 ``voices`` 一一对应；不给则平台自行识别音源内容。
    if transcript:
        fields.append(("texts", transcript))

    try:
        async with _client(cfg) as client:
            # 音源只有几 MB，交给 httpx 流式读一次即可（与 minimax 同处理）。
            with source.open("rb") as handle:  # noqa: ASYNC230 - 见上
                response = await client.post(
                    MODELS_PATH,
                    data=fields,
                    files={
                        "voices": (source.name, handle, "application/octet-stream"),
                    },
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
    if not isinstance(payload, dict) or not str(payload.get("_id") or "").strip():
        raise EngineError(f"{label} 克隆成功但响应缺少 _id，无法入库。")
    return payload
