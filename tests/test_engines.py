"""引擎的发现、配置读写、左栏条目、以及"没配密钥时一次请求都不发"。

这一层是**插件与界面之间**的那一段：插件自描述，这里把它变成界面上的一行、
一个表单、一次调用。所以断言集中在"用户看到/拿到的是不是真的"——
掩码、就绪、空串=不改、"装了就有一条"。
"""

from __future__ import annotations

import asyncio

import pytest

from app.engines.base import ORPHAN_LABEL, EnginePlugin, SynthesisResult
from app.engines.builtin.minimax.settings import MinimaxSettings
from app.engines.registry import detect_engine, list_engines
from app.infra.config import load_config, save_config
from app.usecase.engines import (
    EngineConfigError,
    EngineSettingsError,
    clone_voice,
    engine_entries,
    refresh_voices,
    require_engine,
    save_engine_settings,
    settings_for,
    settings_payload,
)


def _items(resp):
    """集合端点统一信封：取 data 段（兼容仍返回裸数组的旧形状）。"""
    payload = resp.json()
    return payload["data"] if isinstance(payload, dict) and "data" in payload else payload


class TestRegistry:
    def test_discovers_builtin_plugins_by_directory(self):
        """**内置引擎不写名单**——扫 ``app/engines/builtin/<key>/`` 就能发现。"""
        engines = list_engines()
        assert {"minimax"} <= set(engines)
        assert list(engines) == sorted(engines)

    def test_the_removed_manual_engine_stays_removed(self):
        """手动登记那台引擎已删——登记出来的条目没有任何引擎认得它的 id。

        钉住这条是因为它的**残留**很难发现：插件目录删了、``config.json`` 里
        那一格还在，不会报错，只会安静地被忽略。
        """
        assert "manual" not in list_engines()
        with pytest.raises(EngineConfigError, match="不存在"):
            require_engine("manual")

    def test_unknown_engine_is_none(self):
        assert detect_engine("nope") is None

    def test_require_engine_lists_what_exists(self):
        with pytest.raises(EngineConfigError, match="minimax"):
            require_engine("nope")

    def test_cloud_engines_expose_their_own_concurrency_setting(self):
        """云端并发写在引擎自己的设置里，用户可改；项目设置不再盖住它。"""
        from app.engines.builtin.mimo.settings import MimoSettings

        for schema, default in ((MinimaxSettings, 3), (MimoSettings, 5)):
            field = schema.model_fields["concurrency"]
            extra = field.json_schema_extra or {}
            assert field.default == default
            assert extra.get("label") == "合成并发"
            assert extra.get("group") == "并发"
            assert extra.get("widget") == "number"

        assert require_engine("minimax").configured_concurrency(MinimaxSettings()) == 3
        assert require_engine("mimo").configured_concurrency(MimoSettings()) == 5


class TestSettingsFor:
    def test_reads_config_by_instance_not_by_key(self, data_dir):
        """**认实例不认 key**：网关手里已经有插件对象了，再按 key 查一遍既多余，
        又会让"注入了自定义引擎"的调用方（测试、将来的热插拔）查不到配置。"""
        from pydantic import BaseModel

        class Custom(EnginePlugin):
            key = "custom_probe"
            label = "自定义"

            @classmethod
            def settings_schema(cls):
                class S(BaseModel):
                    note: str = ""

                return S

        config = load_config()
        config.engines["custom_probe"] = {"note": "来自配置"}
        save_config(config)

        assert settings_for(Custom()).note == "来自配置"  # type: ignore[attr-defined]

    def test_bad_stored_value_does_not_break_the_read(self, data_dir):
        """配置是人手也能改的文件：一个值从枚举里消失了，不该让整台引擎读不出来。"""
        config = load_config()
        config.engines["minimax"] = {"region": "gone-region", "model": "speech-2.8-turbo"}
        save_config(config)

        cfg = settings_for(require_engine("minimax"))
        assert cfg.region == "cn"  # 坏值丢掉，回落默认
        assert cfg.model == "speech-2.8-turbo"  # 其余字段保留

    def test_config_is_per_engine(self, data_dir):
        config = load_config()
        config.engines["minimax"] = {"api_key": "sk-a"}
        config.engines["other"] = {"api_key": "sk-b"}
        save_config(config)
        assert settings_for(require_engine("minimax")).api_key == "sk-a"  # type: ignore[attr-defined]


class TestSaveSettings:
    def test_empty_secret_keeps_the_stored_value(self, data_dir):
        """留空即不改——否则每次改个模型名都会把密钥抹掉。"""
        save_engine_settings("minimax", {"api_key": "sk-keep"})
        save_engine_settings("minimax", {"api_key": "   ", "model": "speech-2.6-hd"})
        cfg = settings_for(require_engine("minimax"))
        assert cfg.api_key == "sk-keep"  # type: ignore[attr-defined]
        assert cfg.model == "speech-2.6-hd"

    def test_clear_erases_it_explicitly(self, data_dir):
        """空串表示"不改"，所以"真的抹掉"得另有说法——否则填错了就删不掉。"""
        save_engine_settings("minimax", {"api_key": "sk-keep"})
        save_engine_settings("minimax", {"_clear": ["api_key"]})
        assert settings_for(require_engine("minimax")).api_key == ""  # type: ignore[attr-defined]

    def test_clear_does_not_touch_non_secret_fields(self, data_dir):
        save_engine_settings("minimax", {"api_key": "sk-keep", "model": "speech-2.6-hd"})
        save_engine_settings("minimax", {"_clear": ["api_key", "model"]})
        cfg = settings_for(require_engine("minimax"))
        assert cfg.api_key == ""  # type: ignore[attr-defined]
        assert cfg.model == "speech-2.6-hd"  # 不是机密，清不动它

    def test_unknown_field_is_dropped(self, data_dir):
        save_engine_settings("minimax", {"nonsense": 1, "model": "speech-2.6-hd"})
        assert "nonsense" not in (load_config().engines["minimax"] or {})

    def test_unknown_engine_is_reported(self, data_dir):
        with pytest.raises(EngineConfigError, match="不存在"):
            save_engine_settings("nope", {})


class TestPayload:
    def test_masks_secrets_and_reports_that_they_are_set(self, data_dir):
        save_engine_settings("minimax", {"api_key": "sk-secret"})
        payload = settings_payload("minimax")
        assert payload["values"]["api_key"] == ""
        assert payload["secrets_set"] == ["api_key"]
        assert payload["ready"] is True
        assert payload["note"] == ""

    def test_not_ready_says_what_is_missing(self, data_dir):
        payload = settings_payload("minimax")
        assert payload["ready"] is False
        assert "API Key" in payload["note"]

    def test_payload_carries_everything_the_form_needs(self, data_dir):
        payload = settings_payload("minimax")
        assert payload["icon_url"] == "/api/v1/engines/minimax/icon"
        assert payload["settings_schema"]["properties"]["api_key"]["widget"] == "password"
        assert payload["capability_notes"]
        assert payload["mapping"]
        assert payload["mapping_constants"] == {"stream": False}


class TestPlatformEntries:
    """左栏「平台音色」那一段：**主语是插件**，不是库里的行。"""

    @pytest.fixture(autouse=True)
    def _enabled(self, data_dir):
        from conftest import enable_every_engine

        enable_every_engine()

    def test_engine_appears_even_with_zero_voices(self, data_dir):
        entries = {e["source"]: e for e in engine_entries({})}
        assert "minimax" in entries  # 装了就有一条，0 条音色也在
        assert entries["minimax"]["total"] == 0
        assert entries["minimax"]["can_list"] is True
        assert entries["minimax"]["can_clone"] is True
        assert entries["minimax"]["ready"] is False
        assert "API Key" in entries["minimax"]["note"]

    def test_becomes_ready_once_configured(self, data_dir):
        save_engine_settings("minimax", {"api_key": "sk-ok"})
        entries = {e["source"]: e for e in engine_entries({})}
        assert entries["minimax"]["ready"] is True
        assert entries["minimax"]["note"] == ""

    def test_counts_are_attached_to_the_right_engine(self, data_dir):
        entries = {e["source"]: e for e in engine_entries({"minimax": 7})}
        assert entries["minimax"]["total"] == 7

    def test_unknown_source_from_the_database_is_still_listed(self, data_dir):
        """库里有、但没有对应插件的 source（历史条目）**照样显示**——
        藏起来的话那些音色会凭空消失，用户只会以为库坏了。"""
        entries = {e["source"]: e for e in engine_entries({"legacy": 3})}
        assert entries["legacy"]["total"] == 3
        assert entries["legacy"]["key"] == ""
        assert entries["legacy"]["can_list"] is False

    def test_orphan_rows_say_so_instead_of_leaving_it_to_be_guessed(self, data_dir):
        """**"插件没了"必须是一个明说的字段**（`orphan`），不能让界面去猜"`key`
        是空串就说明插件没了"——拿空值当信号，读的人得先知道那条潜规则。

        它**不是引擎**：没有 `channel`（那格是"音色从哪来"，而这台引擎已经不在了）。
        所以界面不能拿它当引擎去开面板——那样开出来会是**一整块空白的主区**
        （真发生过：点左栏那条残留条目，右侧什么都没有、也没有任何报错）。
        """
        rows = {e["source"]: e for e in engine_entries({"legacy": 3})}
        assert rows["legacy"]["orphan"] is True
        assert "channel" not in rows["legacy"]
        # 说法也在后端（与 KIND_LABELS 同一条纪律：中文只写一处）。
        assert rows["legacy"]["kind_label"] == ORPHAN_LABEL[0]
        assert rows["legacy"]["kind_note"] == ORPHAN_LABEL[1]
        # 真引擎那边是 `orphan: False`（界面靠它把两种行分到两段里）。
        assert all(row["orphan"] is False for row in engine_entries({}))

    def test_tree_carries_the_platform_section(self, service):
        assert any(p["source"] == "minimax" for p in service.tree()["engines"])


class TestCallsWithoutCredentials:
    """没配密钥时：**一次网络请求都不该发出去**，理由要说清。"""

    def test_refresh_returns_a_reason_instead_of_raising(self, data_dir, service):
        """拉不到不是错误——空表与"这个平台没有音色"长得一样，那句话必须有人说。"""
        result = asyncio.run(refresh_voices(service, "minimax"))
        assert result["ok"] is False
        assert "API Key" in result["reason"]
        assert result["total"] == 0

    def test_refresh_of_a_listing_less_engine_says_so(self, data_dir, service, monkeypatch):
        """**不提供清单的引擎**（本地推理那种）拉清单时要如实说，而不是报网络错。"""
        _inject(monkeypatch, _NoListing())
        result = asyncio.run(refresh_voices(service, "probe_nolisting"))
        assert result["ok"] is False
        assert "不提供音色清单" in result["reason"]

    def test_refresh_imports_what_it_got(self, data_dir, service, monkeypatch):
        """拉到了就入库，并把"新增/更新"分开报——两者对用户是两件事。"""
        from app.engines.base import VoiceList, VoiceMeta
        from app.engines.builtin.minimax.plugin import MinimaxEngine

        items = (VoiceMeta(id="v1", name="音色一"), VoiceMeta(id="v2", name="音色二"))

        # **打在类上**：注册表每次都会新建一个实例，打在实例上根本不会被调到。
        async def fake_list(self, cfg):
            return VoiceList(items=items)

        monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
        save_engine_settings("minimax", {"api_key": "sk-ok"})

        result = asyncio.run(refresh_voices(service, "minimax"))
        assert result == {
            "ok": True,
            "reason": "",
            "added": 2,
            "updated": 0,
            "total": 2,
            "cost_note": "",
        }
        again = asyncio.run(refresh_voices(service, "minimax"))
        assert (again["added"], again["updated"]) == (0, 2)
        # 二次导入不新增，但库里还是那两条（**只增改、不删**）
        assert len(service.list_voices(source="minimax")) == 2

    def test_clone_on_a_non_cloning_engine_is_refused(self, data_dir, service, monkeypatch):
        """**一次都不该发出去**——插件说它不会克隆，用例层就不该去敲它。

        抛 ``EngineError``（→ 400）而不是 ``EngineConfigError``（→ 404）：
        引擎在、只是不做这件事，报 404 会让调用方去查名字拼错了没有。
        """
        from app.engines.base import CloneRequest, EngineError

        _inject(monkeypatch, _NoListing())
        with pytest.raises(EngineError, match="不支持音色克隆"):
            asyncio.run(clone_voice(service, "probe_nolisting", CloneRequest(name="x")))


class _NoListing(EnginePlugin):
    """一台**什么都不声明**的探针引擎（= 本地推理那种：没有清单、没有克隆）。

    用它替代从前内置的 ``manual``——那个插件删了，但"没有清单的引擎"这个**情形**
    还在（GPT-SoVITS 就是），所以造一台只存在于用例里的探针来验它。
    """

    key = "probe_nolisting"
    label = "无清单探针"


def _inject(monkeypatch, engine: EnginePlugin) -> None:
    """把一台假引擎塞进注册表：用例层找引擎走的是 ``detect_engine``。

    探针引擎不在启用清单里，故连"已启用"一并放开——它被测的是"启用了之后的行为"。
    """
    from app.usecase import engines as engines_module

    real = engines_module.detect_engine
    monkeypatch.setattr(
        engines_module,
        "detect_engine",
        lambda key: engine if key == engine.key else real(key),
    )
    real_enabled = engines_module.is_enabled
    monkeypatch.setattr(
        engines_module,
        "is_enabled",
        lambda key: key == engine.key or real_enabled(key),
    )


class TestGatewayUsesRealSettings:
    def _probe_config(self, key: str, **values: object) -> None:
        """给一台**没注册**的探针引擎写配置（探针只存在于这个用例里）。"""
        config = load_config()
        config.engines[key] = dict(values)
        save_config(config)

    def test_gateway_hands_the_plugin_its_own_config(self, data_dir, service):
        """**用这台引擎真实的配置**，不是空模型——否则"配好的密钥"会凭空消失。"""
        from app.engines.base import EnginePlugin, SynthesisSpec

        seen: dict[str, object] = {}

        class Probe(EnginePlugin):
            key = "probe"
            label = "探针"

            @classmethod
            def settings_schema(cls):
                return MinimaxSettings

            def synthesis_spec(self) -> SynthesisSpec:
                return SynthesisSpec(requires=("voice_id",))

            async def synthesize(self, cfg, req) -> SynthesisResult:
                seen["api_key"] = getattr(cfg, "api_key", None)
                seen["format"] = req.format
                return SynthesisResult(audio=b"x")

        self._probe_config("probe", api_key="sk-from-config")
        voice = service.add_platform_voice("probe", _meta("V1"))
        from app.usecase.gateway import synthesize

        asyncio.run(
            synthesize(
                service,
                engine_key="probe",
                voice_id=voice.id,
                text="你好",
                engines={"probe": Probe()},
            )
        )
        assert seen["api_key"] == "sk-from-config"
        # **不替调用方编格式**：没说就是空，让引擎按自己的配置定（见 gateway.synthesize）
        assert seen["format"] == ""

    def test_explicit_format_is_passed_through(self, data_dir, service):
        from app.engines.base import EnginePlugin, SynthesisSpec

        seen: dict[str, object] = {}

        class Probe(EnginePlugin):
            key = "probe2"
            label = "探针"

            def synthesis_spec(self) -> SynthesisSpec:
                return SynthesisSpec(requires=("voice_id",), formats=("mp3", "wav"))

            async def synthesize(self, cfg, req) -> SynthesisResult:
                seen["format"] = req.format
                return SynthesisResult(audio=b"x")

        voice = service.add_platform_voice("probe2", _meta("V1"))
        from app.usecase.gateway import synthesize

        asyncio.run(
            synthesize(
                service,
                engine_key="probe2",
                voice_id=voice.id,
                text="你好",
                format="wav",
                engines={"probe2": Probe()},
            )
        )
        assert seen["format"] == "wav"


def _meta(voice_id: str):
    from app.engines.base import VoiceMeta

    return VoiceMeta(id=voice_id, name=voice_id)


class TestFieldDescriptionContract:
    """给界面用的契约：**每台引擎的每个配置字段都要能渲染**。"""

    def test_every_builtin_field_has_a_label_and_a_type(self):
        for key, engine in list_engines().items():
            properties = engine.settings_schema().model_json_schema().get("properties", {})
            for name, spec in properties.items():
                assert spec.get("label"), f"{key}.{name} 没有 label"
                assert spec.get("type") or spec.get("anyOf"), f"{key}.{name} 没有 type"

    def test_secret_fields_marked_in_the_schema_are_the_ones_masked(self):
        """``secret`` 标记与掩码名单必须是同一份——两处各说各的就会漏一个。"""
        for key, engine in list_engines().items():
            properties = engine.settings_schema().model_json_schema().get("properties", {})
            marked = {name for name, spec in properties.items() if spec.get("secret")}
            assert marked == set(engine.secret_fields()), key

    def test_choices_are_value_label_pairs(self):
        for key, engine in list_engines().items():
            properties = engine.settings_schema().model_json_schema().get("properties", {})
            for name, spec in properties.items():
                for pair in spec.get("choices") or []:
                    assert len(pair) == 2, f"{key}.{name} 的候选不是 (值, 显示名)"
                    assert pair[1], f"{key}.{name} 的候选 {pair[0]!r} 没有显示名"


class TestEnginePageContract:
    """引擎页面的分区表：**页签由后端出**——前端不认识任何一台引擎，也不认识任何
    一项能力键。它只把这张表画成页签（规则见 ``AGENTS.md`` 的「音色通道与引擎面板」一节）。"""

    def test_every_engine_declares_settings_voices_about(self):
        from app.usecase.engines import page_sections

        for key, engine in list_engines().items():
            sections = page_sections(engine)
            keys = [section["key"] for section in sections]
            assert keys[0] == "settings", key
            assert "voices" in keys and "about" in keys, key
            for section in sections:
                assert section["label"], f"{key}.{section['key']} 没有说法"

    def test_clone_section_follows_capability(self):
        """不摆一颗点了才报错的页签：克隆那一下只有声明了能力的引擎才有。"""
        from app.usecase.engines import page_sections

        assert "clone" in [s["key"] for s in page_sections(require_engine("minimax"))]
        assert "clone" not in [s["key"] for s in page_sections(require_engine("gpt_sovits"))]

    def test_voices_section_note_comes_from_the_channel(self):
        """「音色」那一块的说明由**生效通道**填——插件是静态的，通道是配置：

        "它有哪些音色"取决于用户在通道里选了什么，所以这句话不能写在插件里。
        """
        from app.usecase.engines import page_sections

        note = next(
            s["note"] for s in page_sections(require_engine("gpt_sovits")) if s["key"] == "voices"
        )
        assert note and "参考音频" in note

    def test_unknown_section_keys_are_dropped(self):
        """框架画不出的分区键丢掉——免得前端拿到一个无从渲染的名字。"""
        from app.engines.base import UiSection
        from app.usecase.engines import page_sections

        class Probe(EnginePlugin):
            key = "probe_sections"
            label = "探针"

            def ui_sections(self):
                return (
                    UiSection("settings", "设置"),
                    UiSection("nonsense", "画不出来的那一块"),
                    UiSection("about", "能力与参数"),
                )

        assert [s["key"] for s in page_sections(Probe())] == ["settings", "about"]

    def test_payload_carries_page_and_mapping(self, data_dir):
        """一包发全：页签、映射表、重点说明。

        插件默认配置不再单列：设置表单本身就按默认值渲染，另开一张表只是第二份产地。
        """
        payload = settings_payload("minimax")
        keys = [s["key"] for s in payload["page"]["sections"]]
        assert keys[:2] == ["settings", "voices"]
        assert "defaults" not in payload
        assert any(row["source"] == "text" and row["target"] == "text" for row in payload["mapping"])
        assert payload["mapping_cautions"]

    def test_dual_expression_engines_say_only_one_channel_is_sent(self, data_dir):
        """情绪与引导指令各有落点的引擎，在参数映射页说明「带指令不发情绪」。

        MiMo 是这一类；BreezeTTS 把情绪翻成指令（落点相同），不适用这条。
        """
        from app.engines.base import DUAL_EXPRESSION_NOTE

        assert DUAL_EXPRESSION_NOTE in settings_payload("mimo")["mapping_cautions"]
        assert DUAL_EXPRESSION_NOTE not in settings_payload("breeze_tts")["mapping_cautions"]

    def test_mapping_table_covers_the_whole_spec_with_notes(self, data_dir):
        """映射表按项目规范逐项列全，每行都有说明——表下不再摆第二份说明。"""
        from app.domain.request_spec import SPEC_FIELDS

        for key in ("minimax", "breeze_tts", "mimo"):
            rows = settings_payload(key)["mapping"]
            assert [row["source"] for row in rows[: len(SPEC_FIELDS)]] == [
                item.key for item in SPEC_FIELDS
            ], key
            assert all(row["note"] for row in rows), key
            # 有落点与没落点都在同一张表里，没落点的写「—」而不是消失。
            assert any(row["target"] for row in rows), key
            assert any(not row["target"] for row in rows), key

    def test_reference_requirements_are_declared_by_the_plugin(self):
        """参考音频的要求只能在 ``synthesis_spec()`` 里声明，核心照声明自动处理。

        面板只把声明画成标签；这条守卫钉的是「用到参考音频的引擎必须说清要求」，
        免得将来加一台引擎时把转码/补逐字稿交给"猜"。
        """
        from app.engines.registry import list_engines

        for key, engine in list_engines().items():
            if not engine.capabilities().reference_audio:
                continue
            spec = engine.synthesis_spec()
            assert (
                spec.reference_formats
                or spec.needs_transcript
                or "reference_audio" in spec.requires
            ), f"{key} 用参考音频，却没在 synthesis_spec() 里声明要求"


class TestCapabilityContract:
    """能力布尔是声明，方法实现是事实：两者必须一致。

    声明得比实现多，界面会出现一颗点了才报错的按钮；声明得比实现少，用户看不到
    本来有的能力。这一组按能力逐项对照，新增引擎不用记得加测试。
    """

    def test_declared_capabilities_have_implementations(self):
        for key, engine in list_engines().items():
            caps = engine.capabilities()
            cls = type(engine)
            sections = {section.key for section in engine.ui_sections()}

            if caps.voice_listing:
                assert cls.list_voices is not EnginePlugin.list_voices, (
                    f"{key}: voice_listing=True，但没有覆写 list_voices"
                )
            if caps.voice_cloning or caps.voice_design or caps.sample_cloning:
                assert cls.clone_voice is not EnginePlugin.clone_voice, (
                    f"{key}: 声明了克隆/设计，但没有覆写 clone_voice"
                )
            if caps.voice_cloning or caps.sample_cloning:
                assert "clone" in sections, (
                    f"{key}: 声明了克隆（voice_cloning / sample_cloning），但没有 clone 页签"
                )
            if caps.voice_design:
                assert "design" in sections, f"{key}: voice_design=True，但没有 design 页签"
            if caps.emotion_control:
                placements = {item.source for item in engine.mapping().params}
                placements |= set(engine.dynamic_mappings())
                placements |= set(engine.value_choices())
                assert "emotion" in placements, (
                    f"{key}: emotion_control=True，但映射、动态映射与取值表都没有 emotion"
                )

    def test_capability_flags_are_derived_from_the_dataclass(self):
        """``describe()`` 的 flags 自动取自 ``Capabilities``：加字段不会漏下发。

        只有"另有一处下发"的字段（语言面、样本容器）才登记在隐藏集合里；
        其余字段手写第二遍迟早会漏，这里把集合钉住。
        """
        from dataclasses import fields

        from app.engines.base import _CAPABILITY_FIELDS_HIDDEN, Capabilities

        expected = {item.name for item in fields(Capabilities)} - _CAPABILITY_FIELDS_HIDDEN
        for key, engine in list_engines().items():
            assert set(engine.describe()["capabilities"]) == expected, key

    def test_accepted_extra_comes_from_the_declarations(self):
        """网关过滤与 describe() 读同一份：动态映射必须在内，顶层四格必须不在。"""
        core = {"text", "voice_id", "emotion", "format"}
        for key, engine in list_engines().items():
            accepted = engine.accepted_extra_keys()
            assert set(engine.dynamic_mappings()) - core <= accepted, key
            assert not (core & accepted), key
            assert engine.describe()["synthesis_spec"]["accepted_extra"] == sorted(accepted), key

    def test_instruction_capability_matches_its_placement(self):
        """引导指令必须有落点才声明；有落点就必须声明（页面不再漏掉这一条）。"""
        for key, engine in list_engines().items():
            placements = {item.source for item in engine.mapping().params}
            placements |= set(engine.dynamic_mappings())
            assert engine.capabilities().instruction_control == ("instruction" in placements), (
                f"{key}: instruction_control 与映射/动态映射不一致"
            )

    def test_capability_notes_merge_cloning_and_keep_constraints_out(self):
        """三种克隆只占一条；参考音频与文本上限不冒充能力。"""
        for key, engine in list_engines().items():
            caps = engine.capabilities()
            notes = engine.describe()["capability_notes"]
            keys = [note["key"] for note in notes]
            cloning = [note for note in notes if note["key"] == "voice_cloning"]
            if caps.voice_cloning or caps.sample_cloning or caps.reference_audio:
                assert len(cloning) == 1, key
            else:
                assert cloning == [], key
            assert "reference_audio" not in keys, key
            assert "max_chars" not in keys, key
            if "instruction_control" in keys and "emotion_control" in keys:
                assert keys.index("instruction_control") < keys.index("emotion_control"), key
            if caps.voice_listing:
                listing = next(note for note in notes if note["key"] == "voice_listing")
                assert listing["label"] == "内置音色", key


class TestSettingsReset:
    """``_reset``：非机密字段回到插件声明的默认值，**机密保留**。"""

    def test_reset_restores_defaults_and_keeps_secrets(self, data_dir):
        save_engine_settings(
            "minimax", {"api_key": "sk-keep-me", "model": "speech-01-turbo", "sample_rate": 16000}
        )
        saved = save_engine_settings("minimax", {"_reset": True})
        assert saved.model == "speech-2.8-hd"
        assert saved.sample_rate == 32000
        assert saved.api_key == "sk-keep-me"  # "恢复默认"不顺手删密钥（那是 _clear 的活）

    def test_reset_composes_with_values_in_the_same_request(self, data_dir):
        """先复位、再落这次给的值——"回默认但要改一格"不必跑两趟。"""
        save_engine_settings("minimax", {"model": "speech-01-turbo", "sample_rate": 16000})
        saved = save_engine_settings("minimax", {"_reset": True, "sample_rate": 24000})
        assert saved.model == "speech-2.8-hd"
        assert saved.sample_rate == 24000


class TestSettingsValidation:
    """超时这类数值字段：读能回落，写不能回落——非法值要当场拒绝。"""

    def test_a_valid_timeout_is_saved_and_read_back(self, data_dir):
        saved = save_engine_settings("minimax", {"timeout": 7.5})
        assert saved.timeout == 7.5
        assert settings_for(require_engine("minimax")).timeout == 7.5

    @pytest.mark.parametrize("bad", [0, -1, "abc", None])
    def test_an_invalid_timeout_is_rejected(self, data_dir, bad):
        with pytest.raises(EngineSettingsError, match="设置未保存"):
            save_engine_settings("minimax", {"timeout": bad})

    def test_an_invalid_value_does_not_overwrite_the_stored_one(self, data_dir):
        save_engine_settings("minimax", {"timeout": 30})
        with pytest.raises(EngineSettingsError):
            save_engine_settings("minimax", {"timeout": 0})
        assert settings_for(require_engine("minimax")).timeout == 30

    def test_the_api_reports_it_as_a_bad_request(self, client, data_dir):
        resp = client.put("/api/v1/engines/minimax/settings", json={"timeout": 0})
        assert resp.status_code == 400
        assert "超时" in resp.json()["error"]["message"]

    def test_a_legacy_bad_value_does_not_block_another_field(self, data_dir):
        """旧版本按旧约束存下的脏值（那时 timeout 还没有 gt）：读回落，但不该
        让用户改别的字段也存不进去——报错还会指向一个他没碰过的格子。"""
        from app.infra.config import load_config, save_config

        config = load_config()
        config.engines["minimax"] = {"timeout": 0, "api_key": "sk-old"}
        save_config(config)

        saved = save_engine_settings("minimax", {"api_key": "sk-new"})
        assert saved.api_key == "sk-new"
        assert settings_for(require_engine("minimax")).timeout == 45.0


class TestChannelEndpoint:
    """``PUT /api/v1/engines/{key}/channel``：通道是配置，界面上改得动。"""

    def test_channel_can_be_set_and_cleared(self, client, data_dir):
        # 与默认相同的通道**不落盘**：留着一段"和默认一模一样"的配置，下一次改默认
        # 值时它就成了一个看不见的钉子（用户没配过，行为却不跟着默认走）。
        got = client.put("/api/v1/engines/minimax/channel", json={"mode": "own"}).json()
        assert got["mode"] == "own"
        assert "minimax" not in load_config().channels

        got = client.put("/api/v1/engines/minimax/channel", json={"mode": ""}).json()
        assert got["mode"] == "own"
        assert "minimax" not in load_config().channels

    def test_out_of_range_mode_falls_back_without_error(self, client, data_dir):
        """越界不是错误，是回落：能力是上限（GPT-SoVITS 没有清单端点可拉）。"""
        got = client.put("/api/v1/engines/gpt_sovits/channel", json={"mode": "own"}).json()
        assert got["mode"] == "library"
        assert "gpt_sovits" not in load_config().channels

    def test_unknown_engine_is_404(self, client, data_dir):
        response = client.put("/api/v1/engines/nope/channel", json={"mode": "own"})
        assert response.status_code == 404


class TestListingFollowsCapabilityNotChannel:
    """「能不能给出音色清单」是**插件声明的事实**，不是通道配置。

    这两件事是**两个问题**：能力回答"这家有没有清单端点"，通道回答"合成时音色从
    哪来"。混在一起（判据读成通道）的症状很具体：用户把一台有清单的引擎配成
    「本地音色」，平台那三百多条就从「全部音色」里凭空消失——而界面不会报任何错。
    """

    def test_a_listing_engine_still_lists_under_the_library_channel(
        self, client, data_dir, monkeypatch
    ) -> None:
        from app.engines.base import VoiceList, VoiceMeta
        from app.engines.builtin.minimax.plugin import MinimaxEngine

        async def fake_list(self, cfg):
            return VoiceList(items=(VoiceMeta(id="v1", name="音色一"),))

        # **打在类上**：注册表每次都会新建一个实例（同一条纪律见上面的 refresh 用例）。
        monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
        save_engine_settings("minimax", {"api_key": "sk-ok"})
        client.put("/api/v1/engines/minimax/channel", json={"mode": "library"})

        body = _items(client.get("/api/v1/engines/minimax/voices"))
        assert body["ok"] is True
        assert [item["id"] for item in body["items"]] == ["v1"]

    def test_refresh_still_pulls_under_the_library_channel(
        self, data_dir, service, monkeypatch
    ) -> None:
        """``/refresh`` 同理：拉到就入库，不该因为通道是库桥就说"不提供清单"。"""
        from app.engines.base import VoiceList, VoiceMeta
        from app.engines.builtin.minimax.plugin import MinimaxEngine
        from app.infra.config import load_config

        async def fake_list(self, cfg):
            return VoiceList(items=(VoiceMeta(id="v1", name="音色一"),))

        monkeypatch.setattr(MinimaxEngine, "list_voices", fake_list)
        save_engine_settings("minimax", {"api_key": "sk-ok"})
        config = load_config()
        config.channels["minimax"] = {"mode": "library"}
        save_config(config)

        result = asyncio.run(refresh_voices(service, "minimax"))
        assert result["ok"] is True
        assert result["total"] == 1
        assert len(service.list_voices(source="minimax")) == 1



class TestEnableDisable:
    """启用与停用：**发现一台插件不等于它出现在界面上**。

    这一组刻意从"一台都没启用"开始（其余用例的夹具把每台都启用了）——被钉的
    正是这条默认：新装之后由用户挑，不是一次把九台摆满。
    """

    @staticmethod
    def _reset() -> None:
        """回到"一台都没启用"（``[]`` 与"没写过这个字段"不同，见 config.engines_enabled）。"""
        config = load_config()
        config.engines_enabled = []
        save_config(config)

    def test_a_fresh_profile_lists_nothing_but_offers_everything(self, client) -> None:
        self._reset()
        assert _items(client.get("/api/v1/engines")) == []
        catalog = {row["key"]: row for row in _items(client.get("/api/v1/engines/catalog"))}
        assert "minimax" in catalog
        assert catalog["minimax"]["enabled"] is False
        # 清单里给出"这台是干什么的、添不添"所需的两句：形态与就绪。
        assert catalog["minimax"]["kind_label"]
        assert catalog["minimax"]["ready"] is False

    def test_enabling_brings_it_into_every_surface(self, client) -> None:
        self._reset()
        assert client.put("/api/v1/engines/minimax/enabled", json={"enabled": True}).status_code == 200
        assert [row["key"] for row in _items(client.get("/api/v1/engines"))] == ["minimax"]
        assert [row["key"] for row in _items(client.get("/api/v1/engines/choices"))] == ["minimax"]
        tree = {row["source"]: row for row in client.get("/api/v1/tree").json()["engines"]}
        assert tree["minimax"]["can_list"] is True
        assert tree["minimax"]["enabled"] is True
        # 同一个字段、同一个极性：清单说启用，左栏也得说启用（曾经是 enabled/disabled 各一套）。
        catalog = {row["key"]: row for row in _items(client.get("/api/v1/engines/catalog"))}
        assert catalog["minimax"]["enabled"] is tree["minimax"]["enabled"]

    def test_disabling_takes_it_out_but_keeps_the_voices_it_left(self, client, library) -> None:
        from app.engines.base import VoiceMeta

        self._reset()
        client.put("/api/v1/engines/minimax/enabled", json={"enabled": True})
        library.add_platform_voice("minimax", VoiceMeta(id="MX_1", name="林晚"))

        client.put("/api/v1/engines/minimax/enabled", json={"enabled": False})
        assert _items(client.get("/api/v1/engines")) == []
        # 库里那一条还在：左栏留一行"停用"，点开仍能看它留下的音色。
        tree = {row["source"]: row for row in client.get("/api/v1/tree").json()["engines"]}
        assert tree["minimax"]["enabled"] is False
        assert tree["minimax"]["total"] == 1
        assert client.get(f"/api/v1/voices/{library.list_voices(source='minimax')[0].id}").status_code == 200

    def test_a_disabled_engine_cannot_synthesize(self, client) -> None:
        self._reset()
        resp = client.post("/api/v1/synthesize", json={"engine": "minimax", "voice_id": "x", "text": "你好"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "engine_not_enabled"

    def test_saving_settings_adds_it_without_a_separate_step(self, client) -> None:
        """填了配置就等于在用：省掉「先开开关、再填密钥」那一步。"""
        self._reset()
        client.put("/api/v1/engines/minimax/settings", json={"api_key": "sk-ok"})
        assert [row["key"] for row in _items(client.get("/api/v1/engines"))] == ["minimax"]

    def test_mark_groups_only_cover_enabled_engines(self, client) -> None:
        """语气词的「通用」是对**已启用**那几台求的交集。

        把停用的算进去，已启用那几台共同的标签会被它挤掉——界面上会少掉几个
        其实人人认的标签，而且看不出是谁在拖后腿。
        """
        self._reset()
        client.put("/api/v1/engines/mimo/enabled", json={"enabled": True})
        only = client.get("/api/v1/marks").json()
        assert {group["key"] for group in only["engines"]} == {"mimo"}

        client.put("/api/v1/engines/mimo/enabled", json={"enabled": False})
        assert client.get("/api/v1/marks").json()["engines"] == []

    def test_health_only_reports_enabled_engines(self, client) -> None:
        """健康检查回答的是"现在在用的那几台能不能用"，不是"装了哪些"。"""
        self._reset()
        assert client.get("/api/v1/health").json()["engines"] == {}
        client.put("/api/v1/engines/minimax/enabled", json={"enabled": True})
        assert set(client.get("/api/v1/health").json()["engines"]) == {"minimax"}

    def test_assignment_candidates_only_cover_enabled_engines(self, client) -> None:
        """配音分配弹窗的候选与网关同一口径：停用的那台不该出现在下拉里。"""
        from app.producers.builtin.audiobook.assist.voice_assign import engine_caps

        self._reset()
        assert engine_caps() == {}
        client.put("/api/v1/engines/minimax/enabled", json={"enabled": True})
        assert set(engine_caps()) == {"minimax"}
