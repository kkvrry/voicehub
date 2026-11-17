"""MiniMax 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

端点与字段照官方文档（``platform.minimax.io/docs/api-reference``）：

==============  ==============================  ===================================
用途            端点                            形状
==============  ==============================  ===================================
合成            ``POST /v1/t2a_v2``             JSON；音频在 ``data.audio``（**hex 串**）
音色清单        ``POST /v1/get_voice``          JSON body ``{"voice_type": ...}``
上传音源        ``POST /v1/files/upload``       ``multipart/form-data``，返回 ``file.file_id``
音色克隆        ``POST /v1/voice_clone``        JSON；``file_id`` + ``voice_id``
==============  ==============================  ===================================

**两条容易踩的**，都体现在代码里：

1. ``get_voice`` 是 **POST + JSON body**，不是 GET 查询串。写成 GET 的典型症状是
   拿到一个与"音色清空"长得一样的空结果，查半天查不出所以然。
2. 这一家**业务错误放在 HTTP 200 里**（``base_resp.status_code``），所以每一次
   响应都必须先拆信封再看内容——顺序反了的话，拒绝时的 ``data.audio`` 是空的，
   能挖到的只有一句"没返回音频"，而真正的原因（密钥错、余额不足、音色不存在）
   写在 ``base_resp.status_msg`` 里。

错误一律翻成 :class:`EngineError`，**只报平台的原话**，后面不接我们自己的解释：
平台的 ``status_msg`` 各是各的话（凭据不对、余额不足、音色无效），接一段猜的解释
只会把那一句真正有用的埋掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.engines.audio_http import ensure_audio_size, net_error
from app.engines.base import CODE_RATE_LIMITED, EngineError, SynthesisResult, VoiceList
from app.engines.builtin.minimax.settings import MinimaxSettings
from app.engines.builtin.minimax.voices import parse_voice_list
from app.engines.http_pool import borrow, post_within

__all__ = [
    "CLONE_PATH",
    "RATE_LIMIT_CODE",
    "T2A_PATH",
    "UPLOAD_PATH",
    "VOICES_PATH",
    "check_envelope",
    "clone_voice",
    "decode_audio",
    "fetch_voices",
    "synthesize",
    "upload_audio",
]

T2A_PATH = "/v1/t2a_v2"
VOICES_PATH = "/v1/get_voice"
UPLOAD_PATH = "/v1/files/upload"
CLONE_PATH = "/v1/voice_clone"

#: ``base_resp.status_code`` 里的限流码（文案为 ``rate limit exceeded(RPM)``）。
RATE_LIMIT_CODE = 1002

#: 上传时 ``purpose`` 的取值（官方枚举里与克隆相关的两个）。
PURPOSE_SOURCE = "voice_clone"  # 音源本体
PURPOSE_PROMPT = "prompt_audio"  # 提示音频（可选，提升相似度）


def _timeout(cfg: MinimaxSettings) -> float:
    return float(cfg.timeout)


def headers(cfg: MinimaxSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_key.strip()}"}


def check_envelope(payload: Any, what: str, *, label: str) -> dict[str, Any]:
    """先看 ``base_resp``，再看内容——**顺序不能反**（理由见模块顶部）。

    限流（``1002`` / 文案含 rate limit）单独标成 ``rate_limited``：它与"参数错了"
    的处理完全不同——网关会退避重试并让同一引擎降温，而不是把这一行判死。
    """
    if not isinstance(payload, dict):
        raise EngineError(f"{label} 响应不是 JSON 对象，无法取得{what}。")
    base = payload.get("base_resp")
    if isinstance(base, dict):
        code = base.get("status_code")
        if code not in (0, None):
            msg = str(base.get("status_msg") or "").strip()
            limited = str(code) == str(RATE_LIMIT_CODE) or "rate limit" in msg.lower()
            raise EngineError(
                f"{label} 拒绝了{what}（status_code={code}）：{msg or '未提供原因'}",
                retryable=limited,
                code=CODE_RATE_LIMITED if limited else "engine_failed",
            )
    return payload


def decode_audio(payload: dict[str, Any], *, label: str) -> bytes:
    """``data.audio``（hex 串）→ 音频字节。"""
    data = payload.get("data")
    audio_hex = str(data.get("audio") or "").strip() if isinstance(data, dict) else ""
    if not audio_hex:
        extra = payload.get("extra_info")
        detail = ""
        if isinstance(extra, dict) and extra.get("audio_length") in (0, None):
            detail = "（extra_info 里音频长度也是 0——多半是文本为空）"
        raise EngineError(f"{label} 未返回音频{detail}")
    try:
        return bytes.fromhex(audio_hex)
    except ValueError as exc:
        # 不是 hex：多半是服务端改了编码方式。**说清是哪一种**，否则用户会去查
        # 解码代码，而问题在平台那头。
        raise EngineError(
            f"{label} 返回的音频不是合法的十六进制（{exc}）。前 40 个字符：{audio_hex[:40]!r}"
        ) from exc


async def _client(cfg: MinimaxSettings, **kw: Any):  # 返回类型不外露：httpx 是可选细节
    # ``trust_env=True``：与修复前一致——云端引擎在用户配了 HTTP(S)_PROXY 的机器上
    # 要照常走代理；本机引擎才需要 ``trust_env=False``（见那些模块顶部的说明）。
    return borrow(
        base_url=cfg.host,
        headers=headers(cfg),
        timeout=_timeout(cfg),
        trust_env=True,
        follow_redirects=True,
        **kw,
    )


def _net_error(exc: Exception, cfg: MinimaxSettings, what: str, label: str) -> EngineError:
    """网络层失败 → 可读说明。**说哪一步失败**——这台引擎有拉清单、合成、上传、克隆四个动作。"""
    return net_error(exc, cfg, label=label, step=what)


async def fetch_voices(cfg: MinimaxSettings, *, label: str) -> VoiceList:
    """拉音色清单（``POST /v1/get_voice``）。**拉不到不抛异常**，把理由写进 ``why``。"""
    import httpx

    if not cfg.configured:
        return VoiceList(why="还没填 API Key。")
    payload_in = {"voice_type": cfg.voice_type}
    try:
        async with await _client(cfg) as client:
            response = await client.post(VOICES_PATH, json=payload_in)
    except httpx.HTTPError as exc:
        return VoiceList(why=_net_error(exc, cfg, "拉音色清单", label).reason)

    if response.status_code >= 400:
        return VoiceList(why=f"拉音色清单失败：HTTP {response.status_code}")
    try:
        payload = check_envelope(response.json(), "拉音色清单", label=label)
    except EngineError as exc:
        return VoiceList(why=exc.reason)

    items = parse_voice_list(payload)
    if not items:
        return VoiceList(
            why="账号下无可用音色——system 清单为空，无克隆/文生音色"
            "（克隆音色要先成功合成过一次，才会出现在这里）。"
        )
    return VoiceList(items=items)


async def synthesize(
    cfg: MinimaxSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成（``POST /v1/t2a_v2``）。``body`` 由映射表装配好（见 ``mapping``）。"""
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")
    try:
        async with await _client(cfg) as client:
            response = await post_within(client, T2A_PATH, seconds=_timeout(cfg), json=body)
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, "合成", label) from exc

    if response.status_code == 429:
        raise EngineError(
            f"{label} 合成失败：HTTP 429（请求过多）。{response.text[:200]}",
            retryable=True,
            code=CODE_RATE_LIMITED,
        )
    if response.status_code >= 400:
        raise EngineError(
            f"{label} 合成失败：HTTP {response.status_code}。{response.text[:200]}"
        )
    payload = check_envelope(response.json(), "这次合成", label=label)
    audio = ensure_audio_size(decode_audio(payload, label=label), label)

    extra = payload.get("extra_info")
    note = ""
    if isinstance(extra, dict):
        chars = extra.get("usage_characters")
        if chars not in (None, 0):
            note = f"这次按 {chars} 字符计费"
    return SynthesisResult(
        audio=audio,
        format=str(body.get("audio_setting", {}).get("format") or cfg.audio_format),
        cost_note=note,
        subtitle_url=_subtitle_url(payload),
    )


def _subtitle_url(payload: dict[str, Any]) -> str:
    """``data.subtitle_file``：平台给的字幕**下载链接**（不是字幕内容）。

    只有开了 ``subtitle_enable`` 才会有；没有就是空串，不给占位值——
    "这一家没给字幕"与"字幕是一份空文件"是两回事，混在一起没法判断。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("subtitle_file") or "").strip()


async def upload_audio(
    cfg: MinimaxSettings, path: str, purpose: str, *, label: str
) -> Any:
    """上传一个音频文件（``POST /v1/files/upload``），返回平台的 ``file_id``。

    ``file_id`` **原样返回、不转类型**：上传接口的示例里它是字符串占位，而克隆
    接口把它标成 ``integer<int64>``——两种都出现过，转换反而可能把对的弄坏。
    """
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key。")
    source = Path(path)
    if not source.is_file():
        raise EngineError(f"音源文件不存在：{path}")
    try:
        async with await _client(cfg) as client:
            # 这里就是要把一个**文件对象**交给 httpx 去流式发；它读一下就把句柄关了，
            # 而音源只有几 MB。为它套一层线程池换不来什么，反而多一处出错的地方。
            with source.open("rb") as handle:  # noqa: ASYNC230 - 见上
                response = await client.post(
                    UPLOAD_PATH,
                    data={"purpose": purpose},
                    files={"file": (source.name, handle, "application/octet-stream")},
                )
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, "上传音源", label) from exc

    if response.status_code >= 400:
        raise EngineError(f"上传音源失败：HTTP {response.status_code}。{response.text[:200]}")
    payload = check_envelope(response.json(), "上传音源", label=label)
    file_id = (payload.get("file") or {}).get("file_id")
    if file_id in (None, ""):
        raise EngineError("上传成功但响应缺少 file_id，无法继续克隆。")
    return file_id


async def clone_voice(
    cfg: MinimaxSettings, body: dict[str, Any], *, label: str
) -> dict[str, Any]:
    """克隆（``POST /v1/voice_clone``）。``body`` 由 plugin 装配好。

    返回原始响应（含 ``demo_audio`` 试听链接与 ``extra_info`` 计费信息）——
    调用方要拿它回话，这里不做取舍。
    """
    import httpx

    if not cfg.configured:
        raise EngineError("请先填写 API Key")
    try:
        async with await _client(cfg) as client:
            response = await client.post(CLONE_PATH, json=body)
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, "音色克隆", label) from exc

    if response.status_code >= 400:
        raise EngineError(f"音色克隆失败：HTTP {response.status_code}。{response.text[:200]}")
    return check_envelope(response.json(), "这次音色克隆", label=label)
