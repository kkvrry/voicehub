"""BreezeTTS 2 插件：**照实测核过的那些点**，每一条都钉一个断言。

和 ``test_gpt_sovits.py`` 一样，这个文件里**没有一次真实网络请求**——假 httpx 客户端
既让测试离线跑，又能断言"到底发了什么"（路径、body）。

这一家最容易被写错的**七**处，正是下面重点钉的：

1. **请求体分两层**：``instruction`` 与采样参数必须放进 ``options``——摆在顶层的
   ``instruction`` 会被引擎**静默忽略**（请求 200、声音照出，只是那句话没进去）。
   这一脚踩过：上一轮所有"指令不生效"的结论都是它造成的；
2. **CFG 不是"引导的开关"**：它管的是引导有多强，实测低强度下换一句指令输出照样
   完全不同（"低强度会忽略指令"那条结论已经删掉，连着它那句警告一起）；
   **规范里那一格是 0–1 的「指令强度」，乘 8 才是这一家的 CFG**；
3. **它有两条路**（参考音频克隆 / 纯指令做音色设计），谁把 ``requires`` 写成
   ``reference_audio`` 就把第二条路抹掉了；
4. **它恒定回 wav**（把 ``response_format`` 写成 mp3 也一样），别替它声明别的格式；
5. 正文里的语气词标签**先翻成统一名再登记**：``(gasp)`` 就是 ``喘气``（不是新造的
   一个标签）；哪几条属于「通用」由核心跨引擎算（``domain.marks.mark_groups``），
   不是这张表自己标；
6. **参考音频走 base64 而不是路径**——引擎在 Windows 上会把路径转本地代码页，
   中文目录直接失败（这一条是踩出来的，见 ``TestReferenceAudio``）；
7. **种子默认 -1 = 随机**（那个随机得插件自己抽：引擎不收负数，且省略 seed 时
   引擎用定值 0）；写 0 及以上 = 固定可复现。
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
from pathlib import Path
from typing import ClassVar, Self

import httpx
import pytest

from app.domain.marks import MARKS, mark_groups
from app.engines.base import KIND_CLOUD, KIND_LOCAL, EngineError, SynthesisRequest
from app.engines.builtin.breeze_tts import client as bz_client
from app.engines.builtin.breeze_tts.mapping import MAPPING, MARK_MAP, MARK_MAP_EN
from app.engines.builtin.breeze_tts.plugin import (
    EMOTION_INSTRUCTIONS,
    BreezeTtsEngine,
    emotion_instruction,
    mark_map_for,
    resolve_guidance_scale,
)
from app.engines.builtin.breeze_tts.settings import BreezeTtsSettings


def _cfg(**over: object) -> BreezeTtsSettings:
    data = {"base_url": "http://127.0.0.1:8080"}
    data.update(over)
    return BreezeTtsSettings(**data)


#: 这整套用例默认用的参考音频路径——**会被下面那个 autouse 夹具换成真实文件**。
#: 参考音频现在是插件自己读的（编码成 base64 再发出去），路径必须真能读到。
_REF = "D:/library/巫师3/叶奈法/默认.wav"


def _req(**over: object) -> SynthesisRequest:
    data = {
        "text": "他停下脚步，回头看了她一眼。",
        "voice_id": "叶奈法",
        "reference_audio": _REF,
        "reference_text": "某个角度说，的确。",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _engine() -> BreezeTtsEngine:
    return BreezeTtsEngine()


def _sent(text: str) -> str:
    """正文过网关那一道：语气词标签在发出前换成这一家的写法（``prepare_text``）。"""
    return _engine().prepare_text(text)


def _wav(size: int = 4096) -> bytes:
    """一段够长、且**文件头是正经 WAV** 的假数据。

    插件会检查参考音频的 ``RIFF`` / ``WAVE`` 两个魔数（引擎只认 WAV），
    所以这里不能只写一个 ``RIFF`` 开头。
    """
    head = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WAVE"
    return head + b"\x00" * (size - len(head))


@pytest.fixture(autouse=True)
def _real_reference_audio(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """把默认那个参考音频路径换成一个**真写出来的小 WAV**。

    **文件名故意带非 ASCII 字符**：那正是这条路当初失败的原因——引擎在 Windows 上把
    路径转本地代码页时炸掉（``could not open WAV input`` / ``No mapping for the
    Unicode character exists…``）。这条夹具于是让每一条装配用例都顺带覆盖着它。
    """
    path = tmp_path / "巫师3" / "叶奈法 默认.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav(512))
    monkeypatch.setitem(globals(), "_REF", str(path))
    return str(path)


# --------------------------------------------------------------------------- #
# 假 httpx：记录"发了什么"，离线断言
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(
        self,
        *,
        content: bytes = b"",
        status_code: int = 200,
        content_type: str = "audio/wav",
        json_body: object = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._json = json_body
        self.text = (
            json.dumps(json_body, ensure_ascii=False)
            if json_body is not None
            else content.decode("utf-8", "replace")
        )

    def json(self) -> object:
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeAsyncClient:
    """替身 httpx.AsyncClient。**只实现 post**——这一家只有合成一个端点。"""

    calls: ClassVar[list[dict[str, object]]] = []
    response: _FakeResponse = _FakeResponse()
    raise_error: Exception | None = None

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"path": path, "init": self.init_kwargs, **kwargs})
        if _FakeAsyncClient.raise_error is not None:
            raise _FakeAsyncClient.raise_error
        return _FakeAsyncClient.response


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    """把 httpx.AsyncClient 换成替身（client 里是函数内 import，所以打真模块就行）。"""
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    _FakeAsyncClient.raise_error = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    def test_every_field_declares_a_label(self):
        """**每个字段都得有界面上的说法**——漏一个就会在表单上显示成英文字段名。"""
        for name, field in BreezeTtsSettings.model_fields.items():
            extra = field.json_schema_extra or {}
            assert extra.get("label"), f"{name} 没有 label"

    def test_no_secret_fields(self):
        """本机服务没有凭据——**别为了对称摆一个空的密钥框**。"""
        assert BreezeTtsEngine.secret_fields() == frozenset()

    def test_host_fills_in_a_missing_scheme(self):
        """省掉 ``http://`` 是常事，而 httpx 对它的报错是 ``unsupported protocol``。"""
        assert _cfg(base_url="127.0.0.1:8080").host == "http://127.0.0.1:8080"
        assert _cfg(base_url="http://127.0.0.1:8080/").host == "http://127.0.0.1:8080"
        assert _cfg(base_url="").host == "http://127.0.0.1:8080"

    def test_default_cfg_is_not_one(self):
        """**这是接这一家的理由之一**：整合包网页 UI 那一格默认 1.0，而 1 等于关掉引导。"""
        assert _cfg().guidance_scale > 1.0

    def test_instruction_defaults_to_empty(self):
        """空串 = 「没有要求」，留给引擎自己的默认——不是一句我们替它编的引导语。"""
        assert _cfg().instruction == ""

    def test_chunk_mode_choices_match_the_model_spec(self):
        """四种分块模式是引擎自己规格里写的那四个（``model_spec.options.request``）。"""
        choices = BreezeTtsSettings.model_fields["text_chunk_mode"].json_schema_extra["choices"]
        assert {value for value, _ in choices} == {"default", "tag_aware", "japanese", "endline"}


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


class TestCapabilities:
    def test_declares_itself_local(self):
        caps = _engine().capabilities()
        assert caps.kind == KIND_LOCAL
        assert caps.kind != KIND_CLOUD

    def test_does_not_claim_a_voice_list_it_cannot_get(self):
        """**没有清单端点就别说有**：声明成 True 会让左栏多一个点了就报错的 ⟳。"""
        assert _engine().capabilities().voice_listing is False

    def test_does_not_claim_cloning(self):
        """zero-shot：参考音频本身就是音色，没有"注册一个音色 id"这一步。"""
        assert _engine().capabilities().voice_cloning is False

    def test_declares_the_reference_audio_capability(self):
        """参考音频 + 逐字稿 = 它的"音色"，这是看家能力。"""
        assert _engine().capabilities().reference_audio is True

    def test_does_not_claim_emotion_control(self):
        """没有情绪参数——语气由引导指令与参考音频决定，别给出一颗没用的按钮。"""
        assert _engine().capabilities().emotion_control is False

    def test_does_not_cap_the_text_length(self):
        """长文本由它自己分块；本机推理也没有"超长 = 多花钱"这回事。"""
        assert _engine().capabilities().max_chars == 0

    def test_spec_does_not_require_a_reference_audio(self):
        """**两条路二选一**：写死 reference_audio 会把"音色设计"整条路抹掉。"""
        assert _engine().synthesis_spec().requires == ()

    def test_spec_declares_wav_only(self):
        """实测 ``response_format`` 是被忽略的——声明 mp3 就是替它撒谎。"""
        assert _engine().synthesis_spec().formats == ("wav",)

    def test_spec_says_the_reference_audio_must_be_wav_too(self):
        """**参考音频这一侧也只认 WAV**（实测 mp3 会被拒）。

        声明它之后中转站会在发出去之前转一道——不声明的话，用户手上那条 mp3
        每次都得自己先转好，而引擎报的是 ``input is MP3, not WAV``。
        """
        assert _engine().synthesis_spec().reference_formats == ("wav",)

    def test_spec_wants_a_transcript_but_not_unconditionally(self):
        """要逐字稿，**但只在给了参考音频时**——写进 ``requires`` 会把音色设计堵死。"""
        spec = _engine().synthesis_spec()
        assert spec.needs_transcript is True
        assert "reference_text" not in spec.requires

    def test_those_two_reach_the_engine_payload(self):
        """**只有进了 ``describe()`` 才到得了界面**——引擎面板那张表靠它。"""
        spec = _engine().describe()["synthesis_spec"]
        assert spec["reference_formats"] == ["wav"]
        assert spec["needs_transcript"] is True


# --------------------------------------------------------------------------- #
# CFG：有引导发 4，没有引导发 1（上游明确给了就听上游的）
# --------------------------------------------------------------------------- #


class TestGuidanceScale:
    """三条规则，按优先级：

    1. 上游给了 ``guidance_scale`` → 原样用（0.125 → 1 是正当要求）；
    2. 没给、这次没有引导 → 1（没有引导时 4 只是白跑无条件分支）；
    3. 没给、这次有引导 → 配置里那一格；若 ≤ 1（引擎自己的中性默认）则用官方
       推荐的 4。

    **旧规则**（"配置里那一格就是默认，与有没有指令无关"）在配置还是旧值 1 的
    机器上会静默发出 1，于是"选了平静却听不出变化"——第 3 条就是为了堵这条。
    它不是"CFG=1 会丢掉指令"那条错结论：那条说的是引擎行为，这条说的是**默认值
    怎么选**；要强制 1，上游明确给 0.125 仍然照发。
    """

    def test_upstream_value_is_used_as_is(self):
        assert resolve_guidance_scale(_cfg(), 2.5, has_guidance=True) == 2.5

    def test_cfg_one_is_a_legitimate_request(self):
        """1 = 不加强。它是个正当要求（听感上就是"别引导"），**不该被动过**。"""
        assert resolve_guidance_scale(_cfg(), 1.0, has_guidance=True) == 1.0

    def test_nothing_requested_with_guidance_uses_the_configured_value(self):
        assert (
            resolve_guidance_scale(_cfg(guidance_scale=2.5), None, has_guidance=True) == 2.5
        )

    def test_nothing_requested_without_guidance_is_one(self):
        """没有引导时 4 没有意义——发引擎自己的中性默认 1。"""
        assert resolve_guidance_scale(_cfg(), None, has_guidance=False) == 1.0

    def test_a_configured_one_falls_back_to_the_recommended_four_when_there_is_guidance(self):
        """配置里还是旧值 1、这次却有引导：按官方推荐的 4 发，别让那句话白写。"""
        assert (
            resolve_guidance_scale(_cfg(guidance_scale=1.0), None, has_guidance=True) == 4.0
        )

    def test_a_configured_one_is_still_one_without_guidance(self):
        assert (
            resolve_guidance_scale(_cfg(guidance_scale=1.0), None, has_guidance=False) == 1.0
        )

    def test_it_is_written_under_options(self):
        """落点是 ``options.guidance_scale``——**摆在顶层会被静默丢掉**（实测）。

        默认请求带参考音频、没有指令与情绪 → 没有引导 → 1。
        """
        assert _engine().build_body(_cfg(), _req())["options"]["guidance_scale"] == 1.0

    def test_an_instruction_makes_it_four(self):
        body = _engine().build_body(_cfg(), _req(extra={"instruction": "放慢语速。"}))
        assert body["options"]["guidance_scale"] == 4.0


# --------------------------------------------------------------------------- #
# 种子：默认是定死的，-1 才是随机（依据是实测，不是文档）
# --------------------------------------------------------------------------- #


class TestSeed:
    """实测拿到的那两条事实，正是这一组要钉住的：

    1. 省略 seed 与 ``seed=0`` **输出逐字节相同**（同一条文本连发三次，md5 一致）
       ——引擎自己的默认种子是 0，**定死的**，"不传就是随机"在这家不成立；
    2. ``seed=-1`` 被引擎拒（500 ``seed must be an unsigned integer``）
       ——所以"随机"这件事只能由插件做：抽一个、发出去、**把抽到的数说回来**。

    **插件默认就是 -1（随机）**：开箱即用每次都不一样；要复现写 0 或具体某个数。
    """

    def test_the_default_seed_is_random(self):
        """**默认 -1 = 随机**：引擎侧省略 seed 是定值 0，随机必须插件代抽。"""
        body = _engine().build_body(_cfg(), _req())
        assert isinstance(body["seed"], int)
        assert 0 <= body["seed"] < 2**31

    def test_zero_is_a_fixed_seed(self):
        """0 是一个合法种子（固定、可复现），不是什么"没设置"。"""
        assert _engine().build_body(_cfg(seed=0), _req())["seed"] == 0

    def test_an_upstream_seed_wins(self):
        assert _engine().build_body(_cfg(), _req(extra={"seed": 1234}))["seed"] == 1234

    def test_a_configured_seed_is_used_when_upstream_is_silent(self):
        assert _engine().build_body(_cfg(seed=77), _req())["seed"] == 77

    def test_minus_one_is_drawn_into_a_real_seed(self):
        """``-1`` = 随机：**发出去的那个数必须能过引擎那一关**（无符号整数）。"""
        body = _engine().build_body(_cfg(), _req(extra={"seed": -1}))
        assert isinstance(body["seed"], int)
        assert 0 <= body["seed"] < 2**31

    def test_a_negative_seed_from_the_config_is_drawn_too(self):
        """两个口子都得管：上游发一个 -1，和用户在引擎设置里填一个 -1。"""
        body = _engine().build_body(_cfg(seed=-1), _req())
        assert 0 <= body["seed"] < 2**31

    def test_the_drawn_seed_is_reported(self):
        """**抽到几号必须说出来**——不然"随机"过一次满意的结果就再也回不去了。"""
        body, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": -1}))
        assert len(notes) == 1
        assert str(body["seed"]) in notes[0]

    def test_two_draws_are_not_pinned_to_the_same_number(self):
        """抽出来的不是常数——否则"随机"只是个说法。

        只查"两次不同"会有极小概率误报（2³¹ 分之一），代价远小于留下一个写死的值。
        """
        drawn = {
            _engine().build_body(_cfg(), _req(extra={"seed": -1}))["seed"] for _ in range(5)
        }
        assert len(drawn) > 1

    def test_a_fixed_seed_is_reported_too(self):
        """固定种子**也要报**：这一条是可复现的，用户得看得到用的是哪一号。

        （旧断言是"正数种子不报"。上一次改动就是为了结果区能显示 seed，
        固定与抽到的两种都报——否则用固定种子跑出来的满意结果同样复现不了。）
        """
        _, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"seed": 1234}))
        assert notes == ("seed=1234",)


# --------------------------------------------------------------------------- #
# 引导指令的预设：读数据文件，不写在代码里
# --------------------------------------------------------------------------- #


class TestInstructionPresets:
    """预设是**数据**（``instructions.json``）：删几条、改几个字都不用动 Python。"""

    def _items(self) -> tuple[tuple[str, str], ...]:
        return _engine().field_presets()["instruction"]

    def test_the_engine_ships_a_preset_list(self):
        assert self._items(), "预设表是空的——instructions.json 没读到或格式写坏了"

    def test_it_is_keyed_by_the_spec_field_name(self):
        """键就是规范里那一格的键名——前端照它挂到那一格上，不认识任何一台引擎。

        名字对不上时界面上**什么都没有**（挂不上去），而接口不报错、测试不红：
        所以这一条要钉住那个键。
        """
        from app.domain.request_spec import SPEC_FIELDS

        keys = set(_engine().field_presets())
        assert keys == {"instruction"}
        assert keys <= {f.key for f in SPEC_FIELDS}

    def test_every_preset_has_both_a_label_and_a_sentence(self):
        """短名用来在菜单里认，句子才是填进那一格的东西——**缺一个这条就没用**。"""
        for label, text in self._items():
            assert label and text
            assert "\n" not in text

    def test_labels_are_unique(self):
        """菜单里两行同名，就分不出点的是哪一个。"""
        labels = [label for label, _text in self._items()]
        assert len(labels) == len(set(labels))

    def test_it_reaches_the_engine_payload(self):
        """**只有进了 ``describe()`` 才到得了界面**——这条钉的是那条通路。"""
        payload = _engine().describe()["field_presets"]["instruction"]
        assert payload[0].keys() == {"label", "text"}
        assert len(payload) == len(self._items())

    def test_the_data_file_is_a_json_next_to_the_plugin(self):
        """它是**数据文件**，不是代码：改预设不该动 Python（也不该动这个包）。"""
        from app.engines.builtin.breeze_tts import presets as mod

        assert mod.PRESETS_PATH.name == "instructions.json"
        assert mod.PRESETS_PATH.parent.name == "breeze_tts"
        assert mod.PRESETS_PATH.is_file()

    def test_a_broken_file_means_no_presets_not_a_crash(self, tmp_path, monkeypatch):
        """手改的文件写坏了，最坏是"这次没有候选"——**不能让整个引擎列表跟着挂**。"""
        from app.engines.builtin.breeze_tts import presets as mod

        broken = tmp_path / "instructions.json"
        broken.write_text("{ 这不是 JSON", encoding="utf-8")
        monkeypatch.setattr(mod, "PRESETS_PATH", broken)
        assert mod.load_presets() == ()
        # 连"文件没了"也要走同一条路（`OSError` 与 `ValueError` 一起被接住）。
        monkeypatch.setattr(mod, "PRESETS_PATH", tmp_path / "不存在的文件.json")
        assert mod.load_presets() == ()

    def test_an_entry_missing_a_field_is_skipped(self, tmp_path, monkeypatch):
        """一条写坏不该拖走整份表——其余照旧可用。"""
        from app.engines.builtin.breeze_tts import presets as mod

        path = tmp_path / "instructions.json"
        path.write_text(
            json.dumps({"presets": [{"label": "好的", "text": "慢一点。"}, {"label": "只有名字"}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "PRESETS_PATH", path)
        assert mod.load_presets() == (("好的", "慢一点。"),)

    def test_the_other_engines_declare_no_presets(self):
        """默认是空表：**没有现成说法可给的引擎，界面上就是干干净净一个输入框**。

        三台带预设的引擎除外（breeze_tts / mimo / indextts2）：它们都能接一句
        「引导指令」（indextts2 那里就是情感描述），给了例句——**哪些话这一家
        听得懂只有它自己知道**，没有例句的引擎不硬凑（硬凑就是替它吹牛）。
        """
        from app.engines.registry import list_engines

        with_presets = ("breeze_tts", "mimo", "indextts2")
        others = [e for key, e in list_engines().items() if key not in with_presets]
        assert all(e.field_presets() == {} for e in others)


# --------------------------------------------------------------------------- #
# 正文指令
# --------------------------------------------------------------------------- #


class TestMarks:
    def test_chinese_text_uses_the_bracket_notation(self):
        assert mark_map_for("他笑了。") is MARK_MAP

    def test_english_text_uses_the_parenthesis_notation(self):
        """官方 README：英文用圆括号那套（``(laugh)``）。"""
        assert mark_map_for("He laughed at the joke.") is MARK_MAP_EN

    def test_a_chinese_sentence_with_an_english_word_stays_chinese(self):
        """夹几个英文单词不改变正文的语言——这正是要的结果。"""
        assert mark_map_for("他 lookup 了一下，笑了。") is MARK_MAP

    def test_marks_are_translated_into_the_chinese_notation(self):
        body = _engine().build_body(_cfg(), _req(text=_sent("[[笑]]他说，[[叹气]]算了。")))
        assert body["input"] == "[笑]他说，[叹气]算了。"

    def test_marks_are_translated_into_the_english_notation(self):
        body = _engine().build_body(_cfg(), _req(text=_sent("[[笑]] He said, [[叹气]] never mind.")))
        assert body["input"] == "(laugh) He said, (sigh) never mind."

    def test_an_unknown_mark_is_deleted_from_the_text(self):
        """**不支持就删**：留着会被当成字念出来，那比没有这个停顿糟得多。"""
        body = _engine().build_body(_cfg(), _req(text=_sent("[[停顿:0.4]]他说。")))
        assert body["input"] == "他说。"

    def test_every_name_is_one_the_vocabulary_has(self):
        """**登记之前先翻成统一名**：``(gasp)`` 就是 ``喘气``，不是新造的一个标签。

        照抄原生写法（``gasp``）的话，同一个概念在两台引擎上是两个名字，
        「通用」永远算不出来——这条把那个坑钉住。
        """
        assert set(MARK_MAP) <= {m.name for m in MARKS}
        assert set(MARK_MAP) == set(MARK_MAP_EN)  # 两套只是写法不同，认的是同一批
        assert MARK_MAP["喘气"] == "(gasp)"
        assert MARK_MAP_EN["喘气"] == "(gasp)"

    def test_gasp_is_the_writing_of_panting(self):
        """``(gasp)`` = 喘气（倒抽一口气）——它翻成统一名之后才进得了「通用」。"""
        engine = _engine()
        groups = mark_groups(
            [
                {"key": "minimax", "label": "MiniMax", "mark_map": {"喘气": "(pant)"}},
                {"key": engine.key, "label": engine.label, "mark_map": engine.mark_map()},
            ]
        )
        assert "喘气" in [m["name"] for m in groups["common"]]

    def test_against_a_narrower_engine_the_rest_falls_to_this_one(self):
        """与一台只认两条的引擎求交集：多出来的那三条落到**这一家自己那一组**。

        分组由核心跨引擎算（``domain.marks.mark_groups``），插件不自报——
        这条钉的是"多出来的归谁"那条规则本身，它是「通用」算得出来的前提。
        """
        engine = _engine()
        groups = mark_groups(
            [
                {"key": "narrow", "label": "窄引擎", "mark_map": {"笑": "(laughs)", "叹气": "(sighs)"}},
                {"key": engine.key, "label": engine.label, "mark_map": engine.mark_map()},
            ]
        )
        assert [m["name"] for m in groups["common"]] == ["笑", "叹气"]
        own = [g for g in groups["engines"] if g["key"] == engine.key]
        assert [m["name"] for m in own[0]["marks"]] == ["喘气", "咳嗽", "清嗓"]
        # 一条都没多出来的那台**不单列一组**（界面上不必挂一行空的分组）。
        assert [g["key"] for g in groups["engines"]] == [engine.key]

    def test_no_mark_of_this_engine_is_its_own(self):
        """这一家登记的五条**别家都认**——对上一条不比它窄的表，它那一组是空的。"""
        engine = _engine()
        groups = mark_groups(
            [
                # 一条比它宽的表：19 条全有，于是交集就是这一家那五条。
                {"key": "wide", "label": "宽引擎", "mark_map": {m.name: f"({m.name})" for m in MARKS}},
                {"key": engine.key, "label": engine.label, "mark_map": engine.mark_map()},
            ]
        )
        assert [m["name"] for m in groups["common"]] == ["笑", "叹气", "喘气", "咳嗽", "清嗓"]
        assert all(g["key"] != engine.key for g in groups["engines"])

    def test_the_translated_names_still_land_on_native_writings(self):
        """翻过名字的那几条照样翻译得出去（``喘气`` → ``(gasp)``、``清嗓`` → ``[清嗓子]``）。"""
        body = _engine().build_body(
            _cfg(), _req(text=_sent("[[咳嗽]]他清了清嗓子，[[清嗓]]又[[喘气]]了一下。"))
        )
        assert body["input"] == "[咳嗽]他清了清嗓子，[清嗓子]又(gasp)了一下。"

    def test_the_english_notation_of_them(self):
        """英文那套是**另一份表**：同一个概念换一种写法，不是"没有这一条"。"""
        body = _engine().build_body(
            _cfg(), _req(text=_sent("[[清嗓]] He said, [[喘气]] then stopped."))
        )
        assert body["input"] == "(clears throat) He said, (gasp) then stopped."

    def test_the_wire_payload_lists_all_of_them_in_the_core_order(self):
        """**只有进了 ``describe()`` 才到得了界面**——这条钉的是那条通路。"""
        marks = _engine().describe()["marks"]
        assert [m["name"] for m in marks] == ["笑", "叹气", "喘气", "咳嗽", "清嗓"]
        # 统一写法里的短名与参数说明一并带上（按钮上的字由那一份说了算）。
        assert marks[0]["label"] == "笑声"
        assert all("native" in m for m in marks)

    def test_mark_map_is_its_own_table_and_is_not_shared_with_other_engines(self):
        """这一家的写法与 MiniMax 那套（``(laughs)``）不是一份——别串了。"""
        assert MARK_MAP["笑"] == "[笑]"
        assert MARK_MAP["清嗓"] == "[清嗓子]"


# --------------------------------------------------------------------------- #
# 请求装配
# --------------------------------------------------------------------------- #


class TestBody:
    def _body(self, cfg: BreezeTtsSettings | None = None, **over: object) -> dict[str, object]:
        return _engine().build_body(cfg or _cfg(), _req(**over))

    def test_text_lands_on_input(self):
        """OpenAI 风格这一家的正文字段叫 ``input``，不叫 ``text``。"""
        assert self._body()["input"] == "他停下脚步，回头看了她一眼。"

    def test_reference_audio_lands_on_voice_ref(self, _real_reference_audio):
        """统一参数里的 ``reference_audio`` → 这一家的 ``voice_ref``。

        **发的是音频本身（base64），不是路径**——路径只在本机被读一次。
        """
        assert self._body()["voice_ref"] == {
            "type": "base64",
            "data": base64.b64encode(Path(_real_reference_audio).read_bytes()).decode("ascii"),
        }

    def test_the_spoken_transcript_lands_on_reference_text(self):
        assert self._body()["reference_text"] == "某个角度说，的确。"

    def test_the_model_id_is_always_present(self):
        """模型 id 是配置，不是"上游给不给"的事——每次都要带。"""
        assert self._body(_cfg(model="breeze-tts-2"))["model"] == "breeze-tts-2"

    def test_the_request_has_exactly_two_layers(self):
        """**这一家的形状：顶层四样，其余全在 ``options`` 里。**

        这条是这一整轮最要紧的一条实测：``instruction`` 摆在**顶层**会被静默忽略
        （请求 200、声音照出，只是那句话没进去），必须放进 ``options``。而 ``options``
        里的键是**校验过的**——写错名字会当场回 ``unknown BreezeTTS request option``，
        顶层写错只会悄悄少一项。所以这一批参数一律走 options。
        """
        body = self._body(extra={"instruction": "放慢语速。"})
        assert set(body) == {"model", "input", "voice_ref", "reference_text", "seed", "options"}
        assert "instruction" not in body  # ← 摆在这一层不会报错，只会失效
        assert body["options"]["instruction"] == "放慢语速。"

    def test_instruction_from_extra_reaches_the_body(self):
        body = self._body(extra={"instruction": "放慢语速，克制而认真。"})
        assert body["options"]["instruction"] == "放慢语速，克制而认真。"

    def test_the_cfg_alias_works(self):
        assert self._body(extra={"cfg": 3.0})["options"]["guidance_scale"] == 3.0

    def test_the_cfg_scale_alias_works(self):
        assert self._body(extra={"cfg_scale": 3.5})["options"]["guidance_scale"] == 3.5

    def test_instruction_from_the_config_is_used_when_upstream_is_silent(self):
        assert self._body(_cfg(instruction="温柔一点。"))["options"]["instruction"] == "温柔一点。"

    def test_upstream_instruction_beats_the_configured_default(self):
        """**顺序不能反**：反了的话上游的明确要求会被这台引擎自己的默认盖掉。"""
        body = self._body(_cfg(instruction="配置里的。"), extra={"instruction": "上游的。"})
        assert body["options"]["instruction"] == "上游的。"

    def test_no_instruction_at_all_means_the_option_is_not_sent(self):
        """配置也空、上游也没给：**整个不发**，让引擎用它自己的默认引导语。

        发一个空串是另一回事——那是"要求一句空指令"，引擎分得出来。
        """
        assert "instruction" not in self._body(_cfg(instruction=""))["options"]

    def test_upstream_sampling_params_beat_the_configured_defaults(self):
        body = self._body(
            _cfg(temperature=0.9, top_k=50, seed=0),
            extra={"temperature": 1.2, "top_k": 10, "seed": 42},
        )
        assert body["options"]["temperature"] == 1.2
        assert body["options"]["top_k"] == 10
        assert body["seed"] == 42

    def test_configured_sampling_defaults_fill_the_gaps(self):
        body = self._body(_cfg(temperature=0.7, depth_temperature=0.6, top_p=0.95, max_tokens=900))
        assert body["options"]["temperature"] == 0.7
        assert body["options"]["depth_temperature"] == 0.6
        assert body["options"]["top_p"] == 0.95
        assert body["options"]["max_tokens"] == 900

    def test_seed_zero_is_sent_rather_than_omitted(self):
        """``0`` 是引擎的合法取值（它的规格里 min=0）——"要求 0"与"没提"是两回事。"""
        assert self._body(_cfg(seed=0))["seed"] == 0

    def test_format_is_never_sent(self):
        """这一家不认 ``response_format``（写成 mp3 也照样回 wav），所以不给它发。"""
        assert "response_format" not in self._body(format="wav")

    def test_a_missing_reference_audio_is_refused_before_any_request(self):
        """**平台音色喂给本地引擎**最容易犯的错——两条路都不通，在这里拦下。"""
        with pytest.raises(EngineError, match="参考音频"):
            self._body(reference_audio="", reference_text="")

    def test_a_reference_audio_without_a_transcript_is_refused(self):
        """引擎会直接回 ``requires reference_text``——那一轮白等，不如在这里说。"""
        with pytest.raises(EngineError, match="逐字稿"):
            self._body(reference_text="")

    def test_an_instruction_alone_is_enough(self):
        """**音色设计**：没有参考音频、只有一句话描述，这是它的第二条路。"""
        body = self._body(reference_audio="", reference_text="", extra={"instruction": "低沉老者。"})
        assert "voice_ref" not in body
        assert body["options"]["instruction"] == "低沉老者。"

    def test_the_configured_cfg_lands_in_the_body_when_there_is_an_instruction(self):
        """它是**算出来的**（见 resolve_guidance_scale），不是可有可无的一格。"""
        body = self._body(extra={"instruction": "放慢语速。"})
        assert body["options"]["guidance_scale"] == _cfg().guidance_scale

    def test_without_guidance_the_cfg_is_one(self):
        """没有引导时发 1：CFG 管的是"引导有多强"，没引导就没什么可加强的。

        上游明确给 ``guidance_scale`` 时不走这条（见 TestGuidanceScale 第 1 条）。
        """
        assert self._body()["options"]["guidance_scale"] == 1.0

    def test_the_strength_is_scaled_and_never_second_guessed(self):
        """上游给的「指令强度」照刻度换算后原样发出——**不抬到推荐值，也不回警告**。

        那句警告（"已按 4 生成"）本身就是错的：它说低强度会忽略引导，而实测不会。
        错误的警告比没有警告更糟——它会让用户以为自己要的引导没生效。

        统一刻度是 0–1，这一家的 CFG 是 0–8：**乘 8**。于是 0.125 → 1（旧用例里
        那个"上游写 1"的等价写法）、0.5 → 4（推荐默认）。
        """
        body, notes = _engine().build_body_with_notes(
            _cfg(), _req(extra={"guidance_scale": 0.125, "instruction": "放慢语速。"})
        )
        assert body["options"]["guidance_scale"] == 1.0
        assert not [n for n in notes if not n.startswith("seed=")]

    def test_half_strength_lands_on_the_recommended_cfg(self):
        """0.5 必须落到这一家官方推荐的 4——**两家在同一个值上都好用**。"""
        assert self._body(extra={"guidance_scale": 0.5})["options"]["guidance_scale"] == 4.0

    def test_a_clean_request_has_nothing_to_report(self):
        """``voice_id`` 每条请求都带着它——它是身份字段，不是"对声音的要求"。

        把它也算进"没发出去"的话，**每一条回话都会多一句废话**，而"这一家要的是
        参考音频而不是音色 id"是个常驻事实（它在面板的映射表里）。
        """
        _, notes = _engine().build_body_with_notes(_cfg(), _req())
        # 种子那句是**结果区要显示的信息**，不算"废话提示"，故单独放行。
        assert not [n for n in notes if not n.startswith("seed=")]

    def test_the_output_details_are_not_reported_per_request(self):
        """输出细节（格式/采样率/声道）是这一家的恒定量，写在映射表的说明里就够了。"""
        _, notes = _engine().build_body_with_notes(
            _cfg(), _req(format="wav", extra={"sample_rate": 24000, "channels": 1})
        )
        assert not [n for n in notes if not n.startswith("seed=")]

    def test_parameters_this_engine_has_no_landing_spot_for_are_reported(self):
        """**只丢不说**会让"我要的语速生效了吗"永远查不出来。"""
        _, notes = _engine().build_body_with_notes(_cfg(), _req(extra={"speed": 0.9, "volume": 1.1}))
        assert any("语速" in n for n in notes)

    def test_unsupported_parameters_do_not_land_in_the_body(self):
        body = self._body(extra={"speed": 0.9, "pitch": 1.2})
        for key in ("speed", "pitch", "voice_id"):
            assert key not in body
            assert key not in body["options"]

    def test_emotion_lands_on_the_instruction_when_there_is_none(self):
        """**没有情绪参数，但情绪不是丢掉**：写成一句引导语，落到 options.instruction。"""
        body = self._body(emotion="平静", extra={"instruction": ""})
        assert body["options"]["instruction"] == "用平静、舒缓的语气朗读。"

    def test_an_explicit_instruction_beats_the_emotion(self):
        """用户点名的优先级：有引导指令就发它，情绪不另写一句。"""
        body = self._body(emotion="平静", extra={"instruction": "像深夜电台那样念。"})
        assert body["options"]["instruction"] == "像深夜电台那样念。"

    def test_a_configured_instruction_beats_the_emotion_too(self):
        """配置里的引导指令也算"有指令"——上游只给情绪时以配置为准。"""
        body = self._body(_cfg(instruction="温柔一点。"), emotion="生气")
        assert body["options"]["instruction"] == "温柔一点。"

    def test_the_emotion_instruction_follows_the_text_language(self):
        """官方要求指令语言与正文一致（Match the instruction language to the target text）。"""
        chinese = emotion_instruction("平静", "她笑了笑。")
        english = emotion_instruction("平静", "She smiled.")
        assert chinese == "用平静、舒缓的语气朗读。"
        assert english == "Speak in a calm, gentle tone."

    def test_emotion_synonyms_use_the_same_sentence(self):
        """高兴 / 快乐 归一到开心：同一情绪不该因为用词不同落到模板句上。"""
        assert emotion_instruction("高兴", "好。") == emotion_instruction("开心", "好。")

    def test_every_canonical_emotion_has_a_sentence(self):
        """规范词一个都不能少——少一个就会掉进模板句，听起来像没写。

        表里另有「中性」「厌恶」两条旧词：候选里已经没有了，但老项目的行还写着，
        掉到「用「中性」的语气朗读。」那种模板句不像话。
        """
        from app.domain.models import EMOTIONS

        assert set(EMOTIONS) <= set(EMOTION_INSTRUCTIONS)
        assert set(EMOTION_INSTRUCTIONS) - set(EMOTIONS) == {"中性", "厌恶"}

    def test_an_unknown_emotion_still_reaches_the_instruction(self):
        assert "恍惚" in emotion_instruction("恍惚", "好。")

    def test_emotion_alone_does_not_pass_the_guard(self):
        """情绪是语气，不是音色描述：没有参考音频与指令时仍要拦下。"""
        with pytest.raises(EngineError, match="参考音频"):
            self._body(reference_audio="", reference_text="", emotion="平静")

    def test_mapping_table_says_what_is_not_supported(self):
        """不认的参数必须有一句解释，否则"请求成功"会把它盖过去。"""
        missing = _engine().unsupported()
        for word in ("speed", "voice_id", "format"):
            assert word in missing, f"说明里没提 {word}"
        # 情绪不在"无落点"里：它有一条随请求形状变的落点。
        assert "emotion" not in missing
        assert "emotion" in _engine().dynamic_mappings()

    def test_mapping_table_records_the_options_layer_trap(self):
        """``instruction`` 要放进 options 这件事**必须写在重点说明里**。

        它不是实现细节：上游自己调这个接口时会踩同一个坑，而症状是"指令填了没用"
        ——没有报错、没有警告，从响应上也看不出来。
        """
        assert any("options" in item and "静默" in item for item in MAPPING.cautions)

    def test_the_landing_spots_are_where_the_engine_reads_them(self):
        """落点路径逐条钉住——**差一层就静默失效**，而这类错不会有任何提示。"""
        targets = {p.source: p.target for p in MAPPING.params}
        assert targets["instruction"] == "options.instruction"
        assert targets["guidance_scale"] == "options.guidance_scale"
        assert targets["top_k"] == "options.top_k"
        # 顶层那四样：正文 / 参考音频 / 逐字稿 / 种子（与它自己的网页界面一致）。
        assert targets["text"] == "input"
        assert targets["reference_audio"] == "voice_ref"
        assert targets["reference_text"] == "reference_text"
        assert targets["seed"] == "seed"

    def test_the_two_keys_the_spec_now_has_are_mapped(self):
        """规范里那两格（``instruction`` / ``guidance_scale``）与这张表**对得上**。

        这是一条**跨层**的断言，而它错起来是安静的：前端按 ``mapping[].source``
        过滤"这台引擎认哪几格"（生成页与试听窗口都这么过滤），名字对不上 = 界面上
        那一格**根本不出现**，而接口不报错、测试不红——只是用户填不了引导。
        """
        from app.domain.request_spec import SPEC_FIELDS

        spec_keys = {f.key for f in SPEC_FIELDS}
        sources = {p.source for p in MAPPING.params}
        assert {"instruction", "guidance_scale"} <= (sources & spec_keys)


# --------------------------------------------------------------------------- #
# 参考音频：三条实测出来的规矩（base64 / 只认 WAV / 有大小上限）
# --------------------------------------------------------------------------- #


class TestReferenceAudio:
    """这一格当初报过一次错，三条规矩都是从"为什么报"里长出来的。

    真实的报错是 ``BreezeTTS 2 拒绝了这次合成：could not open WAV input:
    F:\\ai\\音频数据集\\三国杀\\SP关羽\\01_以义传魂，以武入圣！.wav``——文件本身完全正常
    （标准 PCM WAV，44.1kHz 双声道，3.64 秒，``wave`` 能打开），坏的是**路径里的中文**。
    """

    def test_a_non_ascii_path_is_why_this_goes_out_as_base64(self, _real_reference_audio):
        """**回归用例**：同一个文件，纯 ASCII 路径 200、中文路径 500。

        引擎把路径转本地代码页时炸掉（``No mapping for the Unicode character exists
        in the target multi-byte code page.``）。现在路径只在**本机**被读一次，
        发出去的是音频本身——所以带中文的路径不再是问题。
        """
        assert not str(_real_reference_audio).isascii()
        assert _engine().build_body(_cfg(), _req())["voice_ref"]["type"] == "base64"

    def test_the_bytes_that_go_out_are_the_file_itself(self, _real_reference_audio):
        body = _engine().build_body(_cfg(), _req())
        assert base64.b64decode(body["voice_ref"]["data"]) == Path(_real_reference_audio).read_bytes()

    def test_base64_is_raw_without_a_data_uri_prefix(self, _real_reference_audio):
        """发纯 base64——引擎两种都收（外层网关会剥 ``data:`` 前缀），
        但纯 base64 少一层歧义，也就少一种出错的样子。"""
        assert not _engine().build_body(_cfg(), _req())["voice_ref"]["data"].startswith("data:")

    def test_a_missing_file_is_reported_before_anything_is_sent(self):
        """**读不到就说读不到**，别让它到引擎那边变成一句 ``could not open``。"""
        with pytest.raises(EngineError, match="无法读取参考音频"):
            _engine().build_body(_cfg(), _req(reference_audio="D:/nope/没有这个文件.wav"))

    def test_an_empty_file_is_refused(self, tmp_path):
        empty = tmp_path / "空.wav"
        empty.write_bytes(b"")
        with pytest.raises(EngineError, match="空文件"):
            _engine().build_body(_cfg(), _req(reference_audio=str(empty)))

    def test_a_non_wav_reference_is_refused_with_the_ffmpeg_command(self, tmp_path):
        """引擎只认 WAV。它自己给的做法（那行 ffmpeg）**原样转给用户**——
        这一条不替用户做转换，但必须让他一眼知道该敲什么。"""
        mp3 = tmp_path / "01_以义传魂，以武入圣！.mp3"
        mp3.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 400)
        with pytest.raises(EngineError) as info:
            _engine().build_body(_cfg(), _req(reference_audio=str(mp3)))
        assert "ffmpeg" in str(info.value)
        assert "-c:a pcm_s16le" in str(info.value)

    def test_an_oversized_reference_is_refused_with_its_real_size(self, tmp_path, monkeypatch):
        """超了要说清**实际多大**——"太大了"这三个字帮不上任何忙。"""
        monkeypatch.setattr(bz_client, "MAX_VOICE_REF_BASE64", 512)
        big = tmp_path / "很长.wav"
        big.write_bytes(_wav(4096))
        with pytest.raises(EngineError, match="过大"):
            _engine().build_body(_cfg(), _req(reference_audio=str(big)))

    def test_the_size_limit_is_at_most_what_the_engine_states(self):
        """引擎原话是 5 MiB；这里取更紧的 5,000,000，宁可早报也不误放行。"""
        assert bz_client.MAX_VOICE_REF_BASE64 <= 5 * 1024 * 1024

    def test_a_reference_audio_that_is_not_given_is_still_skipped(self):
        """没有参考音频时**整格不发**（那时走的是纯指令那条路），别塞一个空的 base64。"""
        body = _engine().build_body(
            _cfg(), _req(reference_audio="", reference_text="", extra={"instruction": "低沉老者。"})
        )
        assert "voice_ref" not in body


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #


class TestRequests:
    def test_synthesis_posts_json_to_the_speech_endpoint(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(_engine().synthesize(_cfg(), _req()))

        call = fake_http.calls[0]
        assert call["path"] == bz_client.SYNTH_PATH == "/v1/audio/speech"
        assert isinstance(call["json"], dict)

    def test_it_talks_to_the_audio_cpp_layer_not_the_gateway(self, fake_http):
        """**它接的是 audio.cpp（8080），不是整合包外层那个加密网关（7863）。**

        这条断言是这一整个插件的立场：推理能力在下面那一层，而且是开源的。
        """
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert fake_http.calls[0]["init"]["base_url"] == "http://127.0.0.1:8080"

    def test_local_requests_ignore_the_system_proxy(self, fake_http):
        """``trust_env=False``：**本机请求不能让系统代理接管**。

        实测踩过：环境里有 ``HTTP_PROXY`` 时，发给 ``127.0.0.1:8080`` 的请求会被绕到
        代理上，拿回来的是一句代理的话（``upstream connect failed……``）——于是用户
        去查代理，而问题只是"本机那个端口没人监听"。
        """
        fake_http.response = _FakeResponse(content=_wav())
        asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert fake_http.calls[0]["init"].get("trust_env") is False

    def test_success_returns_the_audio_bytes(self, fake_http):
        fake_http.response = _FakeResponse(content=_wav(8192), content_type="audio/wav")
        result = asyncio.run(_engine().synthesize(_cfg(), _req()))
        assert len(result.audio) == 8192

    def test_the_format_is_always_wav(self, fake_http):
        """实测 ``response_format=mp3`` 回来的仍是一模一样的 wav——照实写 wav。"""
        fake_http.response = _FakeResponse(content=_wav(), content_type="audio/wav")
        assert asyncio.run(_engine().synthesize(_cfg(), _req(format="wav"))).format == "wav"

    def test_local_inference_reports_no_cost(self, fake_http):
        """本机推理不按量计费——**空着，不编一个数字**。"""
        fake_http.response = _FakeResponse(content=_wav())
        assert asyncio.run(_engine().synthesize(_cfg(), _req())).cost_note == ""

    # ---- 错误形状：三种要分得开（服务没起 / 参数不认 / 这段合成不出来） ---- #

    def test_connection_refused_names_the_engine_and_the_address(self, fake_http):
        """**最常见的失败**。只说"哪个引擎、哪个地址连不上"——用户装的不一定是这一台，
        而"没起 / 端口错 / 刚崩掉"在本端长得一模一样，不替用户挑一个原因。
        """
        fake_http.raise_error = httpx.ConnectError("connection refused")
        with pytest.raises(EngineError, match=r"BreezeTTS 2（.*）无法连接"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_timeout_names_the_address_and_the_wait(self, fake_http):
        fake_http.raise_error = httpx.ReadTimeout("too slow")
        with pytest.raises(EngineError, match=r"BreezeTTS 2（.*）响应超时"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_the_openai_style_error_envelope_is_unwrapped(self, fake_http):
        """这一家的错误形状：``{"error": {"message": ..., "type": "invalid_request_error"}}``。"""
        fake_http.response = _FakeResponse(
            status_code=400,
            content_type="application/json",
            json_body={
                "error": {
                    "message": "must set voice_id, voice_ref, or reference_text",
                    "type": "invalid_request_error",
                }
            },
        )
        with pytest.raises(EngineError, match="must set voice_id"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_the_engines_own_words_about_a_missing_transcript_are_carried_through(self, fake_http):
        fake_http.response = _FakeResponse(
            status_code=400,
            content_type="application/json",
            json_body={"error": {"message": "BreezeTTS clone requires reference_text"}},
        )
        with pytest.raises(EngineError, match="requires reference_text"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_200_that_is_not_audio_is_reported_with_its_content_type(self, fake_http):
        """状态码说成功、内容却不是音频——多半是中间隔了个反向代理。"""
        fake_http.response = _FakeResponse(
            content=b"<html>proxy</html>", content_type="text/html"
        )
        with pytest.raises(EngineError, match="text/html"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_a_header_only_wav_is_refused(self, fake_http):
        """只有 44 字节的文件头 = 一个采样点都没出（文本为空之类）。"""
        fake_http.response = _FakeResponse(content=b"RIFF" + b"\x00" * 40)
        with pytest.raises(EngineError, match="合理的最小长度"):
            asyncio.run(_engine().synthesize(_cfg(), _req()))

    def test_short_audio_threshold_is_far_below_a_real_sentence(self):
        """门槛只用来抓"空音频"：这一家 24kHz/16bit/单声道，一秒就是 48000 字节。"""
        assert bz_client.MIN_AUDIO_BYTES < 48_000 / 10


# --------------------------------------------------------------------------- #
# 与中转站、注册表的衔接
# --------------------------------------------------------------------------- #


class TestVoiceDesign:
    def test_design_capability_and_tab_are_declared(self, data_dir):
        from app.usecase.engines import settings_payload

        payload = settings_payload("breeze_tts")
        assert payload["capabilities"]["voice_design"] is True
        assert "voice_design" in payload["synthesis_spec"]["accepted_extra"]
        sections = [s["key"] for s in payload["page"]["sections"]]
        assert "design" in sections
        assert "clone" not in sections
        assert _engine().dynamic_mappings()["voice_design"][0] == "options.instruction"

    def test_design_is_sent_under_options_without_a_reference(self):
        body = _engine().build_body(
            _cfg(),
            SynthesisRequest(text="你好。", voice_id="旁白", extra={"voice_design": "  低沉的老者。  "}),
        )
        assert body["input"] == "你好。"
        assert body["options"]["instruction"] == "低沉的老者。"
        assert body["options"]["guidance_scale"] == 4.0
        assert "instruction" not in body
        assert "voice_ref" not in body
        assert "reference_text" not in body
        assert "voice_id" not in body

    def test_design_keeps_the_requested_direction(self):
        """设计描述在前、引导指令接在后面（旧写法 ``instruct`` / ``speaker`` 由网关归一，
        见 ``usecase.gateway``；插件只认正名）。"""
        body = _engine().build_body(
            _cfg(instruction="配置指令。"),
            SynthesisRequest(
                text="你好。",
                voice_id="旁白",
                emotion="开心",
                extra={"voice_design": "低沉的老者。", "instruction": "语速慢一点。"},
            ),
        )
        assert body["options"]["instruction"] == "低沉的老者。\n语速慢一点。"

    def test_design_keeps_the_configured_direction(self):
        body = _engine().build_body(
            _cfg(instruction="语速慢一点。"),
            SynthesisRequest(text="你好。", voice_id="旁白", extra={"voice_design": "低沉的老者。"}),
        )
        assert body["options"]["instruction"] == "低沉的老者。\n语速慢一点。"

    def test_design_keeps_emotion_when_no_direction_is_set(self):
        body = _engine().build_body(
            _cfg(),
            SynthesisRequest(
                text="你好。", voice_id="旁白", emotion="开心", extra={"voice_design": "低沉的老者。"}
            ),
        )
        expected = emotion_instruction("开心", "你好。")
        assert body["options"]["instruction"] == f"低沉的老者。\n{expected}"

    def test_design_takes_priority_over_a_reference(self):
        body = _engine().build_body(
            _cfg(),
            SynthesisRequest(
                text="你好。",
                voice_id="旁白",
                reference_audio="missing.wav",
                reference_text="",
                extra={"voice_design": "低沉的老者。"},
            ),
        )
        assert body["options"]["instruction"] == "低沉的老者。"
        assert "voice_ref" not in body
        assert "reference_text" not in body

    def test_blank_design_leaves_reference_cloning_unchanged(self):
        body = _engine().build_body(_cfg(), _req(extra={"voice_design": "  "}))
        assert base64.b64decode(body["voice_ref"]["data"]) == Path(_REF).read_bytes()
        assert body["reference_text"] == "某个角度说，的确。"
        assert "instruction" not in body["options"]
        assert body["options"]["guidance_scale"] == 1.0

    def test_blank_design_and_emotion_do_not_bypass_the_guard(self):
        with pytest.raises(EngineError, match="参考音频或引导指令"):
            _engine().build_body(
                _cfg(),
                SynthesisRequest(
                    text="你好。", voice_id="旁白", emotion="开心", extra={"voice_design": "  "}
                ),
            )

    def test_design_respects_explicit_guidance_and_seed(self):
        body = _engine().build_body(
            _cfg(),
            SynthesisRequest(
                text="你好。",
                voice_id="旁白",
                extra={"voice_design": "低沉的老者。", "guidance_scale": 0.125, "seed": 17},
            ),
        )
        assert body["options"]["guidance_scale"] == 1.0
        assert body["seed"] == 17

    def test_registration_requires_a_nonblank_prompt(self):
        from app.engines.base import CloneRequest

        with pytest.raises(EngineError, match="需要音色描述"):
            asyncio.run(_engine().clone_voice(_cfg(), CloneRequest(name="旁白", design_prompt=" ")))

    def test_registration_sanitizes_an_explicit_id(self):
        from app.engines.base import CloneRequest

        meta = asyncio.run(
            _engine().clone_voice(
                _cfg(),
                CloneRequest(name="老者 / 旁白", voice_id="老者 / 旁白", design_prompt="低沉男声。"),
            )
        )
        assert meta.id == "老者_旁白"
        assert meta.name == "老者 / 旁白"

    def test_same_name_designs_do_not_overwrite_each_other(self, data_dir, service):
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        metas = [
            asyncio.run(
                clone_voice(service, "breeze_tts", CloneRequest(name="旁白", design_prompt=prompt))
            )
            for prompt in ("沉稳男声。", "温柔女声。")
        ]
        assert metas[0].id != metas[1].id
        voices = [service.get_voice(f"breeze_tts:{meta.id}") for meta in metas]
        assert [v.payload["design_prompt"] for v in voices] == ["沉稳男声。", "温柔女声。"]
        assert all(v.shown_name == "旁白" for v in voices)

    def test_design_is_registered_without_upload_or_network(self, client, library):
        from app.engines.registry import list_engines
        from app.usecase.engines import engines_for_voice

        response = client.post(
            "/api/v1/engines/breeze_tts/clone",
            data={"name": "老者旁白", "design_prompt": "  低沉的老者。  "},
        )
        assert response.status_code == 200, response.text
        voice = library.get_voice(response.json()["voice_id"])
        assert voice.source == "breeze_tts"
        assert voice.payload["design_prompt"] == "低沉的老者。"
        assert voice.category == "设计音色"
        assert not voice.samples
        # 设计音色是"一句描述"，凡声明了「音色设计」的引擎都能用——名单随插件增减，
        # 写死成 ["breeze_tts"] 会在下次加一台设计引擎时莫名其妙地红。
        design_engines = sorted(
            key for key, engine in list_engines().items() if engine.capabilities().voice_design
        )
        assert engines_for_voice(voice) == design_engines
        assert "breeze_tts" in design_engines

    @pytest.mark.parametrize("extra", [{}, {"reference_audio": "missing.mp3"}])
    def test_repeated_synthesis_uses_the_stored_description(
        self, data_dir, service, fake_http, extra
    ):
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice
        from app.usecase.gateway import synthesize

        meta = asyncio.run(
            clone_voice(
                service, "breeze_tts", CloneRequest(name="旁白", design_prompt="沉稳男声。")
            )
        )
        fake_http.response = _FakeResponse(content=_wav())
        for text in ("第一句。", "第二句。"):
            asyncio.run(
                synthesize(
                    service,
                    engine_key="breeze_tts",
                    voice_id=f"breeze_tts:{meta.id}",
                    text=text,
                    extra=extra,
                )
            )
        bodies = [call["json"] for call in fake_http.calls]
        assert [body["input"] for body in bodies] == ["第一句。", "第二句。"]
        assert all(body["options"]["instruction"] == "沉稳男声。" for body in bodies)
        assert all("voice_ref" not in body and "reference_text" not in body for body in bodies)


class TestWiring:
    def test_it_is_discovered_as_a_builtin(self):
        from app.engines.registry import list_engines

        assert "breeze_tts" in list_engines()

    def test_it_has_an_icon_of_its_own(self):
        """图标按目录取——没有 icon.svg 会回落到一个中性盒子，看不出是哪一台。"""
        svg = _engine().icon_svg()
        assert svg.startswith("<svg") and "breezeGrad" in svg

    def test_a_platform_voice_is_refused_before_any_request(self, data_dir, service):
        """**平台音色喂给这台引擎**是最容易犯的错——它没有 id、只有参考音频。

        这条走完整条网关链（解析 → 校验 → 路由），并在**发请求之前**报出来。
        """
        from app.engines.base import VoiceMeta
        from app.usecase.gateway import synthesize

        voice = service.add_platform_voice("minimax", VoiceMeta(id="MX_V", name="林晚"))
        with pytest.raises(EngineError, match="参考音频"):
            asyncio.run(
                synthesize(service, engine_key="breeze_tts", voice_id=voice.id, text="你好")
            )

    def test_a_local_voice_carries_its_sample_into_the_request(self, demo_library, service):
        """本地音色 → ``voice_ref``（主样例**音频本身**）+ ``reference_text``（那条样例的说话内容）。"""
        from dataclasses import replace

        from app.usecase.gateway import normalize_voice

        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(hutao.id, "开心.wav")
        # ``normalize_voice`` 只负责**音色那一半**（它建出来的请求 text 是空的，
        # 正文由调用方填）——这正是网关里那一步的形状。
        voice_part = normalize_voice(service, hutao.id)
        body = _engine().build_body(_cfg(), replace(voice_part, text="你好，世界。"))
        assert base64.b64decode(body["voice_ref"]["data"]) == Path(
            voice_part.reference_audio
        ).read_bytes()
        assert body["reference_text"] == "吃饱喝饱，一路走好！"

    def test_a_main_sample_without_a_spoken_text_is_refused_here(self, demo_library, service):
        """这一家与 GPT-SoVITS 不同：**逐字稿是必须的**（引擎会回 requires reference_text）。

        所以"样例没填说话内容"这件事必须在这一层就说清楚，而不是让用户等一轮
        （那条样例在演示库里确实没有配套文本）。
        """
        from dataclasses import replace

        from app.usecase.gateway import normalize_voice

        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_main_sample(hutao.id, "默认.wav")
        voice_part = normalize_voice(service, hutao.id)
        assert voice_part.reference_text == ""
        with pytest.raises(EngineError, match="逐字稿"):
            _engine().build_body(_cfg(), replace(voice_part, text="你好，世界。"))

    def test_refresh_says_it_has_no_listing(self, data_dir, service):
        """点 ⟳ 时如实说"这台不提供清单"，而不是报一个网络错误。"""
        from app.usecase.engines import refresh_voices

        result = asyncio.run(refresh_voices(service, "breeze_tts"))
        assert result["ok"] is False
        assert "不提供音色清单" in result["reason"]

    def test_clone_is_refused_with_a_400_not_a_404(self, data_dir, service):
        """引擎**在**、只是不做这件事 → 该报 400（``EngineError``），不是 404。"""
        from app.engines.base import CloneRequest
        from app.usecase.engines import clone_voice

        with pytest.raises(EngineError, match="不支持音色克隆"):
            asyncio.run(clone_voice(service, "breeze_tts", CloneRequest(name="x")))
