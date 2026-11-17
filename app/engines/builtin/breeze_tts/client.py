"""BreezeTTS 2 的 HTTP 调用——**"怎么发"是这一家自己的事**，留在它的目录里。

打的是 audio.cpp 推理服务（``audiocpp_server.exe``）的 **OpenAI 兼容**端点：

======================  ==========================================================
用途                    端点
======================  ==========================================================
合成                    ``POST /v1/audio/speech``   JSON body，成功时**回的就是音频字节**
流式合成                ``POST /v1/audio/speech/live``   本插件不用（要的是能落盘的成品）
音色库                  ``GET /v1/audio/voices``    本插件不用（音色由 VoiceHub 供给）
健康检查                ``GET /health``             本插件不用（见下）
======================  ==========================================================

**四个不显然的地方**，都体现在代码里：

1. **不要流式。** ``/live`` 那条路只支持 ``response_format=pcm``（裸 PCM，没有文件头），
   而调用方（有声书）要的是能直接落盘的完整文件。走普通端点，拿回一整段 wav。

2. **不要在 ``readiness`` 里探端口。** ``readiness`` 是同步的、左栏每刷一次就调一遍；
   在里面发网络请求既拖慢界面，又会把"服务没起"变成一个每几秒闪一次的角标。它真正
   需要被说出口的时刻是**用户按下合成那一下**——那是这里的事。

3. **``trust_env=False``：别让系统代理接管本机请求。** 这条不是照抄 GPT-SoVITS 那份，
   是**实测踩过的**：本机环境里配着 ``HTTP_PROXY``，发给 ``127.0.0.1:8080`` 的请求被
   绕到代理上，拿回来的是一句 ``upstream connect failed: 由于目标计算机积极拒绝``
   ——那句话指的是**代理的** upstream，而问题其实是"本机那个端口没人监听"，
   查的方向完全是错的。

4. **"服务没起"与"这段合成不出来"必须分开说。** 前者是这一家最常见的失败
   （整合包的后端没启动、端口被改过、或者**显存/内存不够时它会自己 assert 退出**），
   报错形状（连接被拒）与后者完全不同——混成一句"合成失败"的话，用户会去查文本和
   参考音频，而问题根本不在这儿。**但分开说只分开到"连不上这个地址"为止**：连接被拒
   的三种成因在本端无法区分，所以报错只说引擎名与地址，不替用户挑一个原因。

   顺带记一笔实测：后端在**宿主内存吃紧**时会以
   ``GGML_ASSERT(ctx->mem_buffer != NULL) failed`` 直接退出（不是回错误码，是进程没了）。
   所以"突然连不上"有时是它刚崩掉——这是排查时要想到的一条，不是要写进报错的一句话。

5. **参考音频走 base64，不走路径。** 引擎在 Windows 上会把路径转成本地代码页，
   **路径里有中文就失败**（实测：同一个文件，ASCII 路径 200、中文路径 500）。
   详见 :func:`voice_ref_payload`——那一段里有实测的对照表。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from app.engines.audio_http import (
    MIN_AUDIO_BYTES,
    audio_bytes,
    error_detail,
    loggable_body,
    net_error,
    open_client,
)
from app.engines.base import CODE_UNREACHABLE, EngineError, SynthesisResult
from app.engines.builtin.breeze_tts.settings import BreezeTtsSettings
from app.engines.http_pool import post_within
from app.infra.ffmpeg import container_of_bytes
from app.infra.loghub import CHANNEL

__all__ = [
    "MAX_VOICE_REF_BASE64",
    "MIN_AUDIO_BYTES",
    "SYNTH_PATH",
    "synthesize",
    "voice_ref_payload",
]

SYNTH_PATH = "/v1/audio/speech"

#: 引擎对 base64 之后的 ``voice_ref`` 有上限（它的原话：``voice_ref base64 data exceeds
#: the 5 MiB limit``）。这里按 **5,000,000** 字节卡，比 5 MiB（5,242,880）再紧一点：
#: 那句话里的 "5 MiB" 到底是 5×1024² 还是 5×10⁶ 没人写清楚，取小的那个不会误放行。
#:
#: 换算成参考音频本身大约是 3.7 MB——一条十几秒的 44.1kHz 立体声 wav 是 2.4 MB 上下，
#: 所以正常素材碰不到这条线；碰到的多半是"整段音频丢进来了"，那时该裁短而不是想办法绕。
MAX_VOICE_REF_BASE64 = 5_000_000

#: WAV 的判定：**这一家只认 WAV**——给 mp3 它回的是 ``input is MP3, not WAV;
#: convert it first, e.g. `ffmpeg -i input -ac 1 -ar 44100 -c:a pcm_s16le output.wav` ``。
#: 那句话是平台自己给的做法，照抄进提示里。
#:
#: 判定本身**交给 :func:`app.infra.ffmpeg.container_of_bytes`**：同一个问题
#: （"这段音频是什么壳"）在全项目只有一个产地，而且它比"看前 12 个字节"认得多
#: （m4a / flac / ogg 也认得出，报错时那句"它其实是 mp3"才说得准）。
def _is_wav(raw: bytes) -> bool:
    return container_of_bytes(raw[:16]) == "wav"


def voice_ref_payload(path: str, *, label: str) -> dict[str, str]:
    """参考音频 → 引擎要的那个形状：**base64，不是路径**。

    为什么不能发路径（**这是实测踩出来的，不是预防性设计**）：引擎在 Windows 上会把
    路径转成本地代码页，路径里有中文时那一步直接失败。同一个文件、同一个请求，只换
    ``voice_ref`` 的写法：

    ==============================================  ========  ==============================
    ``voice_ref``                                   结果      引擎的话
    ==============================================  ========  ==============================
    ``F:\\ai\\_tts_probe\\ascii_ref.wav``（纯 ASCII）   200       回音频
    同一个文件的 base64                              200       回音频
    ``F:\\ai\\音频数据集\\三国杀\\…\\01_以义传魂….wav``   500       ``No mapping for the Unicode
                                                              character exists in the target
                                                              multi-byte code page.``
    ==============================================  ========  ==============================

    而**中文目录在国内用户这儿是常态**（数据集按作品分、角色名带中文），所以路径这条
    路根本不能算可用。整合包外层那个网关走的也是 base64（``audiocpp.py`` 的
    ``voice_ref_payload``）——它早就绕开了这件事，这里只是跟上。

    顺带解决另外三件事：参考音频**不必与服务同机可见**（引擎放进 Docker / WSL /
    另一台机器时路径会失效，base64 不会）、不用管盘符与反斜杠怎么写、
    也不用跟 Windows 的短路径较劲。

    :param label: 引擎显示名，报错时用它（改名只改插件的 ``label``）。
    :raises EngineError: 读不到 / 空文件 / 不是 WAV / 编码后超过上限——**都在发出去
        之前**报出来，而且每一句都带上"那该怎么改"。
    """
    source = Path(path)
    try:
        # ``Path.read_bytes`` 对不存在的文件抛 FileNotFoundError（OSError 的子类），
        # 对没权限的抛 PermissionError——两种都是 OSError，一起接。
        raw = source.read_bytes()
    except OSError as exc:
        raise EngineError(
            f"无法读取参考音频：{path}（{exc.strerror or exc}）。"
            "该字段需要本机文件路径，请确认文件存在且未被移动。"
        ) from exc

    if not raw:
        raise EngineError(f"参考音频为空文件：{path}。")

    if not _is_wav(raw):
        # **这一道是最后一道岗**：正常路径上，中转站已经照 ``synthesis_spec`` 里那句
        # ``reference_formats=("wav",)`` 把 mp3 那一类转好了（见 ``usecase.reference``）。
        # 走到这里还没转，说明调用方绕过了中转站（直接调插件、或者那条路根本没接）——
        # 那就把引擎自己给的那行命令原样转给他，让他手上有个能跑的做法。
        raise EngineError(
            f"{label} 的参考音频仅支持 WAV，而「{source.name}」为"
            f"{container_of_bytes(raw[:16]) or '无法识别的格式'}（文件头是 {raw[:4]!r}）。\n"
            "可执行：\n"
            f'    ffmpeg -i "{path}" -ac 1 -ar 44100 -c:a pcm_s16le "{source.with_suffix(".wav")}"'
        )

    encoded = base64.b64encode(raw).decode("ascii")
    if len(encoded) > MAX_VOICE_REF_BASE64:
        ratio = len(raw) / len(encoded)
        raise EngineError(
            f"参考音频过大：{len(raw) / 1024 / 1024:.1f} MB（编码后 "
            f"{len(encoded) / 1024 / 1024:.1f} MB），超出引擎上限 "
            f"{MAX_VOICE_REF_BASE64 / 1024 / 1024:.0f} MB"
            f"（约合 {MAX_VOICE_REF_BASE64 * ratio / 1024 / 1024:.1f} MB 的 WAV）。"
            "请裁剪至十几秒的干净人声；整段输入会降低克隆相似度。"
        )
    return {"type": "base64", "data": encoded}



def _timeout(cfg: BreezeTtsSettings) -> float:
    return float(cfg.timeout)


async def _client(cfg: BreezeTtsSettings, **kw: Any):  # 返回类型不外露：httpx 是可选细节
    # ``trust_env=False`` 的理由见模块顶部第 3 条；连接复用见 app/engines/http_pool。
    return open_client(cfg, **kw)


def _net_error(exc: Exception, cfg: BreezeTtsSettings, label: str) -> EngineError:
    """网络层失败 → 可读说明：引擎名 + 地址 + 失败形状。

    **不推断原因**：连接被拒可能是整合包后端没启动、端口改过、或者它刚崩掉，
    三者在这儿长得一模一样，替用户指定某一个只会指向错的地方。
    """
    return net_error(exc, cfg, label=label, code=CODE_UNREACHABLE)


async def synthesize(
    cfg: BreezeTtsSettings, body: dict[str, Any], *, label: str
) -> SynthesisResult:
    """合成（``POST /v1/audio/speech``）。``body`` 由映射表装配好（见 ``mapping``）。"""
    import httpx

    # 真正发出去的那一份进日志：参数没生效时，第一个要看的就是它。
    CHANNEL.info(
        f"{label} 请求：" + json.dumps(loggable_body(body), ensure_ascii=False),
        source="engine",
        engine="breeze_tts",
    )
    try:
        async with await _client(cfg) as client:
            response = await post_within(client, SYNTH_PATH, seconds=_timeout(cfg), json=body)
    except httpx.HTTPError as exc:
        raise _net_error(exc, cfg, label) from exc

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    audio = response.content
    CHANNEL.info(
        f"{label} 响应：{response.status_code} / {len(audio)} 字节 / {content_type or '未提供类型'}",
        source="engine",
        engine="breeze_tts",
    )

    if response.status_code >= 400:
        raise EngineError(f"{label} 拒绝了这次合成：{error_detail(response)}")

    audio = audio_bytes(response, label, hint="可能是文本为空或参考音频未读取")

    # 本机推理没有按量计费这一说，也就没有 cost_note 可报——**空着，不编一个**。
    #
    # 格式**照实写 wav**，不看请求体里写了什么：实测把 ``response_format`` 写成 mp3，
    # 回来的仍然是一模一样的 wav 字节（连 md5 都相同）。写别的就是替它撒谎。
    return SynthesisResult(audio=audio, format="wav")
