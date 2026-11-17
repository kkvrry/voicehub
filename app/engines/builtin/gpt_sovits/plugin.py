"""GPT-SoVITS 引擎插件——**一台跑在本机的语音合成服务**。

它声明自己是 :data:`~app.engines.base.KIND_LOCAL`：请求不出这台机器、没有密钥、
没有计费。左栏「平台音色」下因此出现一个 GPT-SoVITS 条目，界面照它声明的能力渲染。

**它和云端那几家的形状根本不同，这一点决定了下面几乎每一处取舍**：

- 没有"音色 id"，也**没有音色清单**——这一家的"音色"就是**一段参考音频 + 那段
  音频念的内容**。所以它的音色来源是**你自己的本地音色**（样例就在磁盘上），
  而不是拉一份平台清单。
- 没有情绪参数——情绪**全由参考音频决定**（见映射表的「说明」列）。
- 模型（权重）**不在请求里**：整台服务共用一个模型，换模型要动服务端。
- 没有克隆端点：训练音色是另一套离线流程（它的 WebUI），**不是一次 HTTP 调用**。
  所以这里如实声明 ``voice_cloning=False``，而不是摆一个点了就报错的按钮。

能力与字段照官方 ``api_v2.py`` 逐条核过（``github.com/RVC-Boss/GPT-SoVITS``）。
**不做的事**：不内置任何音色、不在本地路径与服务端路径之间做翻译、不猜错误原因。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.engines.base import (
    KIND_LOCAL,
    Capabilities,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    SynthesisSpec,
)
from app.engines.builtin.gpt_sovits import client
from app.engines.builtin.gpt_sovits.mapping import MAPPING
from app.engines.builtin.gpt_sovits.settings import (
    GptSovitsSettings,
    defaults_for,
)
from app.engines.mapping import EngineMapping, apply_mapping

__all__ = ["GptSovitsEngine"]


def _as_settings(cfg: BaseModel) -> GptSovitsSettings:
    """把框架递过来的配置收窄成本引擎的模型。

    框架按 :meth:`settings_schema` 建的就是这个类，所以这一条只会在**实现被改坏**
    时失败（比如换了设置模型却漏了这一处）——那正是应该立刻炸掉的时候，
    而不是等 ``cfg.host`` 抛一个看不出所以然的 ``AttributeError``。
    """
    assert isinstance(cfg, GptSovitsSettings)  # 见上：这是实现错误的哨兵
    return cfg


class GptSovitsEngine(EnginePlugin):
    """GPT-SoVITS 本机推理服务（api_v2.py）。"""

    key = "gpt_sovits"
    label = "GPT-SoVITS"
    version = "1.0.0"
    homepage = "https://github.com/RVC-Boss/GPT-SoVITS"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return GptSovitsSettings

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_LOCAL,
            # 没有清单端点——它的音色就是你的参考音频（见模块顶部）。
            voice_listing=False,
            # 训练音色是离线的另一套流程，不是一次 HTTP 调用。**如实说没有**，
            # 界面上就不会出现一颗点了才报错的「音色克隆」。
            voice_cloning=False,
            # 这是它的看家能力：参考音频 + 参考文本就是它的"音色"。
            reference_audio=True,
            # 没有情绪参数——情绪跟着参考音频走。
            emotion_control=False,
            # 长度本身不设限：长文本由它自己按 ``text_split_method`` 断句（默认 cut5，
            # 按标点切），服务端没有字数检查。0 = 不拦（本机推理也没有"超长 = 多花钱"这回事）。
            max_chars=0,
            max_concurrency=1,
            # 语言：服务端白名单就是中英日韩粤五家（见 ``mapping.SUPPORTED_LANGUAGES``
            # 的 ``v2_languages``）。**正文语言与参考语言是两个独立字段**——这一家把
            # 跨语言写成契约的一部分，不是"碰运气也能出音"。
            languages=("zh", "yue", "en", "ja", "ko"),
            cross_lingual=True,
            dialects=("粤语",),
            language_note="服务端按模型版本校验语言代码，v1 只认前六种",
        )

    def synthesis_spec(self) -> SynthesisSpec:
        """这一家**必须**有参考音频——缺了正是"把平台音色喂给本地引擎"的样子。

        ``needs_transcript=True``：**不分版本，缺了就补**。gsv.cpp 对 V3/V4/V5 直接回
        ``prompt_text is required (V3/V4/V5 do not support reference-text-free mode)``；
        官方 v1/v2/v2Pro 允许留空，但空着只是"少一点相似度"——用户看不出来的损失。
        而"服务端是哪一版"客户端问不到，让用户为此去猜一版没有意义。

        ``reference_formats=("wav",)``：**统一转成 wav 再发**，但这条边界只属于 gsv.cpp，
        不是 GPT-SoVITS 的边界。gsv.cpp 的 ``prep_reference`` 只解 PCM（8/16/24/32/float），
        喂 mp3 直接回 ``prep_reference failed: …mp3``；它的 GUI（GAG 的
        ``prepare_reference_audio``）自己就先把非 wav 读成单声道 PCM_16 wav 再上传，
        说明这条边界在引擎里而不在界面。

        官方 Python 版**没有**这条约束：``api_v2`` 走
        ``GPT_SoVITS/TTS_infer_pack/TTS.py`` 的 ``torchaudio.load``，本机那套
        （torchaudio 2.7.0+cu128，后端只有 soundfile）实测 mp3 读得出来（44.1k 与 48k
        各一条，峰值正常），中文路径也读得出来。所以这里声明 wav 是**取交集**的结果，
        不是它读不了：客户端问不到服务端是哪个实现（版本号不对外，而 gsv.cpp 同样能跑
        v2Pro/v2ProPlus/v3/v4，按 ``model_version`` 判不出来），统一转一道，同一份
        请求体就能通吃两台。代价是官方版多一次转码——按源指纹缓存
        （``infra.ffmpeg.to_wav``），同一段素材只转一次。

        ``reference_duration=(3, 10)``：服务端 ``_set_prompt_semantic`` 里是一条硬判据
        （``48000 <= 采样点 <= 160000``，即 3–10 秒）。它越界时**不报错**——
        ``TTS.run`` 的 except 会吐一秒静音、``api_v2`` 再包成 200，用户看到的是
        "合成成功但没声音"。所以在发之前量：超上限截到 9.5 秒（逐字稿随之作废、
        按截出来的那段重听），短于 3 秒直接拒绝——短了补不回来，补静音只会骗过判据。

        ``reference_path_ascii=True``：**路径本身**必须纯 ASCII。gsv.cpp 的
        ``gsv_server.exe`` 把路径转本地代码页，样例名带中文时回 500
        ``No mapping for the Unicode character exists in the target multi-byte code page.``
        （同一个文件换 ASCII 名就正常，实测过）。官方 Python 版没这问题，但复制一份
        ASCII 副本对它也无害——声明在这里比按服务端实现分支简单。
        """
        return SynthesisSpec(
            requires=("reference_audio",),
            formats=("wav", "ogg", "aac", "raw"),
            needs_transcript=True,
            reference_formats=("wav",),
            reference_duration=(3.0, 10.0),
            reference_path_ascii=True,
        )

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        """规范里有、这一家没有落点的项：面板逐条写原因，不静默丢。

        ``language`` / ``reference_language`` **不在这里**：它们有落点（``text_lang``
        / ``prompt_lang``），只是服务端认不出的代码会退回配置里的默认值。
        """
        return {
            "voice_id": "以参考音频代表音色，没有音色 ID。",
            "emotion": "没有情绪参数，情绪由参考音频决定。",
            "volume": "本引擎无对应参数。",
            "pitch": "本引擎无对应参数。",
            "guidance_scale": "本引擎无对应参数。",
            "lead_silence": "本引擎无对应参数；句间停顿由配置项 fragment_interval 统一控制。",
            "lag_silence": "本引擎无对应参数；句间停顿由配置项 fragment_interval 统一控制。",
            "sample_rate": "本引擎无对应参数；输出采样率由服务端模型决定。",
            "bitrate": "本引擎无对应参数。",
            "channels": "本引擎无对应参数；输出为单声道。",
        }

    # 本机服务：地址有默认值、不需要凭据，**永远就绪**。
    # 不去探测端口——`readiness` 是同步的、会被左栏每刷一次就调一遍，
    # 在这里发网络请求既拖慢界面，又会把"服务没起"变成一个每几秒闪一次的角标，
    # 而它真正需要被说出口的时刻是**用户按下合成那一下**（那是 client 的事）。

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def build_body(self, cfg: GptSovitsSettings, req: SynthesisRequest) -> dict[str, Any]:
        """统一参数 → ``/tts`` 请求体。

        两步：先按 :data:`MAPPING` 落**上游给的**值，再补**配置里的默认值**。
        顺序不能反——反了的话配置会覆盖上游的明确要求（"上游说了 1.3 倍速，
        却被这台引擎自己的默认 1.0 盖掉"）。
        """
        body = apply_mapping(MAPPING, req.as_values())

        # 语言与断句方式**只有配置里有**：上游的统一格式不认识它们，
        # 而服务端两个语言字段都是必填（缺了直接 400），所以这里必须补上。
        body.setdefault("text_lang", cfg.text_lang)
        body.setdefault("prompt_lang", cfg.prompt_lang)
        body.setdefault("text_split_method", cfg.text_split_method)
        body.setdefault("media_type", cfg.media_type)
        body.setdefault("speed_factor", cfg.speed_factor)
        body.setdefault("temperature", cfg.temperature)
        body.setdefault("top_k", cfg.top_k)
        body.setdefault("top_p", cfg.top_p)
        body.setdefault("repetition_penalty", cfg.repetition_penalty)
        body.setdefault("fragment_interval", cfg.fragment_interval)
        body.setdefault("batch_size", cfg.batch_size)
        body.setdefault("seed", cfg.seed)
        # V5 的两个旋钮，0 / 空串 = 不发送（由服务端按模型决定）。
        # 官方 api_v2 没有 dbcache、gsv.cpp 也不认 batch_size——两家都会静默丢掉
        # 自己不认识的字段（实测 200，官方是 pydantic 默认 ignore），所以这里不需要
        # 按服务端实现分支，一份 body 打两边。
        if cfg.sample_steps > 0:
            body.setdefault("sample_steps", cfg.sample_steps)
        if cfg.dbcache:
            body.setdefault("dbcache", cfg.dbcache)
        return body

    def reconcile_settings(
        self, incoming: dict[str, Any], stored: dict[str, Any]
    ) -> dict[str, Any]:
        """换模型版本时，把采样参数一并带过去。

        **只动那些仍等于旧版本推荐值的字段**——那是"没被手改过"唯一可用的痕迹。
        用户亲手调过的值一律保留，否则切一次版本就把他的调校抹掉了。
        """
        old = str(stored.get("model_version") or "")
        new = str(incoming.get("model_version") or old)
        if not new or new == old:
            return incoming
        before, after = defaults_for(old), defaults_for(new)
        if before == after:
            return incoming
        merged = dict(incoming)
        for name, target in after.items():
            if before.get(name) == target:
                continue  # 两版推荐值一样，没有要换的
            if merged.get(name, stored.get(name)) == before.get(name):
                merged[name] = target
        return merged

    # ------------------------------------------------------------------ #
    # 能力
    # ------------------------------------------------------------------ #

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        return await client.synthesize(settings, self.build_body(settings, req), label=self.label)
