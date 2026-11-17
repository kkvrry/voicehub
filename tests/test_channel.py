"""音色通道：能力 → 默认通道，配置 → 生效通道。

为什么单独一份测试：通道是"**这台引擎的音色从谁那儿来**"的唯一答案。从前这件事
散在六处 if（``voice_listing`` / ``reference_audio`` 两个布尔各处解释一遍，见
）抽取之后**只要这一处对，六处就都对**——所以这里要
钉住的正是那六处赖以成立的两条：

1. 默认值 = 从前的隐式逻辑（minimax → own、gpt_sovits → library），一字不差；
2. 配置越界（GPT-SoVITS 配成 own 之类）回落默认，**不让引擎按做不到的说法去跑**。
"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.channel import (
    CHANNEL_LIBRARY,
    CHANNEL_OWN,
    VoiceChannel,
    allowed_modes,
    as_payload,
    default_channel,
    resolve_channel,
)
from app.engines.base import (
    KIND_CLOUD,
    Capabilities,
    EngineError,
    EnginePlugin,
    SynthesisRequest,
    SynthesisResult,
    VoiceList,
    VoiceMeta,
)
from app.infra import archive
from app.infra.config import Config, load_config, save_config


class TestDefaultChannel:
    def test_listing_engine_gets_own(self):
        """有清单 → 自有音色（MiniMax 那一类：音色是平台上的，入库管理）。"""
        assert default_channel(voice_listing=True, reference_audio=False).mode == CHANNEL_OWN

    def test_reference_audio_engine_gets_library(self):
        """吃参考音频 → 库桥（GPT-SoVITS 那一类：你要的声音就是那段参考音频）。"""
        assert (
            default_channel(voice_listing=False, reference_audio=True).mode == CHANNEL_LIBRARY
        )

    def test_listing_wins_when_both(self):
        """两者都有时清单赢：音色**主体**是清单给的，参考音频只是锦上添花。"""
        assert default_channel(voice_listing=True, reference_audio=True).mode == CHANNEL_OWN

    def test_no_capability_no_channel(self):
        """两个能力都没有 → 没有通道（产物没人挂，也没法从库里要音色）。"""
        assert default_channel(voice_listing=False, reference_audio=False).mode == ""

    def test_allowed_modes_follow_capabilities(self):
        assert allowed_modes(voice_listing=True, reference_audio=False) == (CHANNEL_OWN,)
        assert allowed_modes(voice_listing=False, reference_audio=True) == (CHANNEL_LIBRARY,)
        assert allowed_modes(voice_listing=True, reference_audio=True) == (
            CHANNEL_OWN,
            CHANNEL_LIBRARY,
        )
        assert allowed_modes(voice_listing=False, reference_audio=False) == ()


class TestResolveChannel:
    def _own_default(self) -> VoiceChannel:
        return default_channel(voice_listing=True, reference_audio=True)

    def test_missing_config_is_default(self):
        default = self._own_default()
        assert resolve_channel(default, (CHANNEL_OWN, CHANNEL_LIBRARY), {}) == default

    def test_override_within_allowed(self):
        """两种能力都有的引擎，配置选库桥 → 库桥生效（能力是上限，通道是用法）。"""
        default = self._own_default()
        got = resolve_channel(
            default, (CHANNEL_OWN, CHANNEL_LIBRARY), {"mode": CHANNEL_LIBRARY}
        )
        assert got.mode == CHANNEL_LIBRARY

    def test_override_outside_capability_falls_back(self):
        """GPT-SoVITS 配成 own（它没有清单端点可拉）→ 回落默认，不按做不到的说法跑。"""
        default = default_channel(voice_listing=False, reference_audio=True)
        got = resolve_channel(default, (CHANNEL_LIBRARY,), {"mode": CHANNEL_OWN})
        assert got.mode == CHANNEL_LIBRARY

    def test_garbage_is_tolerated(self):
        """配置是人手也能改的文件：认不出的值一律回落，**绝不让引擎读不出配置**。"""
        default = default_channel(voice_listing=True, reference_audio=False)
        allowed = (CHANNEL_OWN,)
        assert resolve_channel(default, allowed, {"mode": 123}).mode == CHANNEL_OWN
        assert resolve_channel(default, allowed, {"mode": "nonsense"}).mode == CHANNEL_OWN
        # 旧版本留下的键直接忽略，不再有"再登记成独立本地音色"这个开关。
        assert resolve_channel(default, allowed, {"promote_generated": "yes"}).mode == CHANNEL_OWN
        assert resolve_channel(default, allowed, {"不认识的键": 1}).mode == CHANNEL_OWN

    def test_channel_less_engine_stays_channel_less(self):
        """没有通道的引擎，配置写什么都不会凭空长出一条通道。"""
        default = default_channel(voice_listing=False, reference_audio=False)
        got = resolve_channel(default, (), {"mode": CHANNEL_OWN})
        assert got.mode == ""


class TestPayload:
    def test_payload_carries_labels_defaults_and_choices(self):
        """界面照它渲染：说法、默认是哪个、能选哪几个——都是后端的知识。"""
        default = default_channel(voice_listing=True, reference_audio=False)
        payload = as_payload(default, default=default, allowed=(CHANNEL_OWN,))
        assert payload["mode"] == CHANNEL_OWN
        assert payload["mode_label"]
        assert payload["mode_note"]
        assert payload["default_mode"] == CHANNEL_OWN
        assert [item["mode"] for item in payload["allowed_modes"]] == [CHANNEL_OWN]
        assert payload["configurable"] is True

    def test_channel_less_engine_is_not_configurable(self):
        default = default_channel(voice_listing=False, reference_audio=False)
        payload = as_payload(default, default=default, allowed=())
        assert payload["configurable"] is False
        assert payload["allowed_modes"] == []


class TestChannelOfBuiltins:
    """内置引擎的默认通道 = 从前的隐式逻辑，一字不差。"""

    def test_minimax_is_own_by_default(self, data_dir):
        from app.usecase.engines import channel_of, require_engine

        assert channel_of(require_engine("minimax")).mode == CHANNEL_OWN

    def test_gpt_sovits_is_library_by_default(self, data_dir):
        from app.usecase.engines import channel_of, require_engine

        assert channel_of(require_engine("gpt_sovits")).mode == CHANNEL_LIBRARY

    def test_config_override_is_read(self, data_dir):
        from app.usecase.engines import channel_of, require_engine

        config = load_config()
        config.channels["minimax"] = {"mode": CHANNEL_OWN, "scope": "all"}
        save_config(config)
        channel = channel_of(require_engine("minimax"))
        assert channel.mode == CHANNEL_OWN

    def test_payload_lands_in_engine_endpoints(self, data_dir):
        from conftest import enable_every_engine

        from app.usecase.engines import engine_entries, settings_payload

        enable_every_engine()
        payload = settings_payload("minimax")
        assert payload["channel"]["mode"] == CHANNEL_OWN
        row = next(r for r in engine_entries({}) if r["key"] == "minimax")
        assert row["channel"]["mode"] == CHANNEL_OWN
        # 能力布尔照旧下发（Phase 3/4 的界面改造之前，前端还在读它们）
        assert row["can_list"] is True


class TestScanDecision:
    """引擎保存目录**永远不是扫描根**：生成物按 sidecar 归属挂回它自己的音色。"""

    def test_saving_a_generated_file_does_not_create_a_local_voice(self, data_dir, service):
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="wav", voice_id="minimax:X")
        assert service.scan()["roots"] == 0
        assert service.list_voices(source="local") == []

    def test_the_old_promote_key_does_not_bring_scanning_back(self, data_dir, service):
        """旧配置里还留着 promote_generated：读得出来也不能再影响扫描。"""
        config = load_config()
        config.channels["minimax"] = {"mode": CHANNEL_OWN, "promote_generated": True}
        save_config(config)
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="wav", voice_id="minimax:X")
        assert service.scan()["roots"] == 0
        assert service.list_voices(source="local") == []


class TestConfigRoundTrip:
    def test_channels_survive_a_save_and_load(self, data_dir):
        config = load_config()
        config.channels["probe"] = {"mode": CHANNEL_LIBRARY, "scope": "all"}
        save_config(config)
        assert load_config().channels["probe"] == {"mode": CHANNEL_LIBRARY, "scope": "all"}

    def test_channels_are_not_a_plugin_setting(self, data_dir):
        """**通道与 engines 那一段分开存**：保存引擎设置会整段重写 ``engines[key]``，
        通道放在一起会被顺手抹掉（它是框架层配置，不属于插件的 settings_schema）。"""
        from app.usecase.engines import require_engine, save_engine_settings, settings_for

        config = load_config()
        config.channels["minimax"] = {"mode": CHANNEL_OWN, "scope": "all"}
        save_config(config)
        save_engine_settings("minimax", {"api_key": "sk-test"})
        stored = load_config()
        assert stored.channels["minimax"] == {"mode": CHANNEL_OWN, "scope": "all"}
        assert settings_for(require_engine("minimax")).api_key == "sk-test"  # type: ignore[attr-defined]

    def test_broken_channels_section_is_ignored(self, data_dir):
        """坏结构（不是 dict）不该让服务读不出配置。"""
        path = data_dir / "config.json"
        path.write_text('{"channels": {"x": "nope"}}', encoding="utf-8")
        assert load_config().channels == {}

    def test_config_defaults_have_no_channels(self):
        assert Config().channels == {}


class TestGatewayFallbackFollowsChannel:
    """裸音色名的兜底（"问一次现在的清单"）只有**自有音色**通道才走。"""

    class _ListingProbe(EnginePlugin):
        key = "probe_own"
        label = "自有探针"

        def __init__(self) -> None:
            self.listed = 0
            self.requests: list[SynthesisRequest] = []

        def capabilities(self) -> Capabilities:
            return Capabilities(kind=KIND_CLOUD, voice_listing=True)

        async def list_voices(self, cfg) -> VoiceList:
            self.listed += 1
            return VoiceList(items=(VoiceMeta(id="茉莉", name="茉莉"),))

        async def synthesize(self, cfg, req) -> SynthesisResult:
            self.requests.append(req)
            return SynthesisResult(audio=b"x")

    class _BridgeProbe(EnginePlugin):
        key = "probe_bridge"
        label = "库桥探针"

        def __init__(self) -> None:
            self.listed = 0

        def capabilities(self) -> Capabilities:
            return Capabilities(kind=KIND_CLOUD, reference_audio=True)

        async def list_voices(self, cfg) -> VoiceList:
            self.listed += 1
            return VoiceList(items=())

    def test_own_channel_resolves_bare_name_through_listing(self, data_dir, service):
        from app.usecase.gateway import synthesize

        probe = self._ListingProbe()
        result = asyncio.run(
            synthesize(
                service,
                engine_key="probe_own",
                voice_id="茉莉",
                text="你好",
                engines={"probe_own": probe},
            )
        )
        assert probe.listed == 1
        assert probe.requests and probe.requests[0].voice_id == "茉莉"
        assert result.audio == b"x"

    def test_bridge_channel_does_not_ask_for_a_listing(self, data_dir, service):
        from app.usecase.gateway import synthesize

        probe = self._BridgeProbe()
        with pytest.raises(EngineError, match="不存在"):
            asyncio.run(
                synthesize(
                    service,
                    engine_key="probe_bridge",
                    voice_id="茉莉",
                    text="你好",
                    engines={"probe_bridge": probe},
                )
            )
        assert probe.listed == 0
