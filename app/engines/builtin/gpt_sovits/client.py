"""GPT-SoVITS 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

端点与字段照官方 ``api_v2.py``：

==========  ==========================  ===========================================
用途        端点                        形状
==========  ==========================  ===========================================
合成        ``POST /tts``               JSON body；**成功时回的是音频字节本身**
控制        ``GET /control?command=``   本插件不用（它会重启/结束服务端进程）
设权重      ``GET /set_*_weights``      本插件不用（服务端全局）
==========  ==========================  ===========================================

**三个不显然的地方**，都体现在代码里：

1. **用 POST 不用 GET。** 官方两个都提供，但 GET 把正文塞进查询串——几千字的一章
   会撞上 URL 长度上限（各家 2KB–8KB 不等），而且中文还要再膨胀三倍。POST 没这问题。
2. **错误与音频共用一个状态码空间，靠 HTTP 状态区分。** 业务错误是
   ``400 + {"message": ..., "Exception": ...}``（那是官方 ``tts_handle`` 的
   ``except`` 分支），成功是 ``200 + audio/*``。所以先看状态码、再看内容类型；
   只看内容类型的话，"服务端把异常也当成 200 回"这种情况会让人拿到一堆 JSON 当音频。
3. **"服务没起"和"服务起了但这段合成不出来"必须分开说。** 前者是这个引擎
   **最常见的**失败（用户忘了起 api_v2.py，或者端口对不上），而它的报错形状
   （连接被拒）与后者完全不同——混成一句"合成失败"的话，用户会去查文本和参考音频，
   而问题根本不在这儿。
"""

from __future__ import annotations

from typing import Any

from app.engines.audio_http import (
    MIN_AUDIO_BYTES,
    audio_bytes,
    error_detail,
    net_error,
)
from app.engines.base import CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.builtin.gpt_sovits.settings import GptSovitsSettings
from app.engines.http_pool import borrow, post_within

__all__ = ["MIN_AUDIO_BYTES", "SYNTH_PATH", "synthesize"]

SYNTH_PATH = "/tts"


def _timeout(cfg: GptSovitsSettings) -> float:
    return float(cfg.timeout)


async def _client(cfg: GptSovitsSettings, **kw: Any):  # 返回类型不外露：httpx 是可选细节
    # ``trust_env=False``：**别让系统代理接管本机请求**。
    # httpx 默认读 ``HTTP_PROXY`` / ``ALL_PROXY`` 这些环境变量，而它们几乎都是
    # "给公网用的"——于是发给 ``127.0.0.1:9880`` 的请求会被绕到代理上，
    # 代理再连它自己的 upstream，报出来的是**代理的**一句话（实测拿到的是
    # "upstream connect failed: 由于目标计算机积极拒绝……"），
    # 而不是"本机这个端口没人监听"。用户会去查代理，而问题只是服务没起。
    return borrow(base_url=cfg.host, timeout=_timeout(cfg), trust_env=False, **kw)


def _net_error(exc: Exception, cfg: GptSovitsSettings, label: str) -> EngineError:
    """网络层失败 → 可读说明：引擎名 + 地址 + 失败形状（不推断服务为什么没起来）。"""
    return net_error(exc, cfg, label=label, code=CODE_UNREACHABLE)


async def synthesize(
    cfg: GptSovitsSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成（``POST /tts``）。``body`` 由映射表装配好（见 ``mapping``）。"""
    import httpx

    try:
        async with await _client(cfg) as client:
            response = await post_within(client, SYNTH_PATH, seconds=_timeout(cfg), json=body)
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, label) from exc

    if response.status_code >= 400:
        raise EngineError(f"{label} 拒绝了这次合成：{error_detail(response)}")

    audio = audio_bytes(response, label, hint="可能是文本为空或参考音频未读取")

    # 本机推理没有按量计费这一说，也就没有 cost_note 可报——**空着，不编一个**。
    return SynthesisResult(
        audio=audio,
        format=str(body.get("media_type") or cfg.media_type),
    )
