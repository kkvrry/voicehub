"""音色凭据：一条音色落到某台引擎上带什么——四种结果，一处判定。

这张矩阵是"界面允许、合成必败"的收口：网关、分配弹窗、下发给界面的 ``engines``
必须对同一组输入给出同一个答案。任何一处再自己推一遍，这里就会红。
"""

from __future__ import annotations

import asyncio

from app.domain.credential import DESIGN, NONE, PLATFORM, REFERENCE, credential_of
from app.domain.models import Sample, Voice
from app.engines.base import (
    KIND_CLOUD,
    Capabilities,
    EngineError,
    EnginePlugin,
    SynthesisSpec,
)
from app.producers.builtin.audiobook.assist import voice_assign
from app.usecase import engines as engines_uc
from app.usecase.gateway import _library_request, synthesize


class _PresetOnly(EnginePlugin):
    """有清单、不吃参考音频（MiniMax 那一类）。"""

    key = "preset_only"
    label = "预置音色引擎"

    def capabilities(self) -> Capabilities:
        return Capabilities(kind=KIND_CLOUD, voice_listing=True)

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",))


class _CloneCapable(EnginePlugin):
    """既有清单、也吃参考音频（MiMo 那一类）。"""

    key = "clone_capable"
    label = "可复刻引擎"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=KIND_CLOUD, voice_listing=True, reference_audio=True, voice_design=True
        )

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",))


ENGINES = {"clone_capable": _CloneCapable(), "preset_only": _PresetOnly()}


def _local(sample: bool = True) -> Voice:
    return Voice(
        id="local-1",
        name="胡桃",
        folder="/voices/胡桃",
        main_sample="a.wav" if sample else "",
        samples=(Sample(file="a.wav", text="你好"),) if sample else (),
        sample_count=1 if sample else 0,
    )


def _platform(source: str, sample: bool = False) -> Voice:
    return Voice(
        id=f"{source}:1",
        source=source,
        name="V1",
        folder="/voices/V1" if sample else "",
        main_sample="a.wav" if sample else "",
        samples=(Sample(file="a.wav", text="你好"),) if sample else (),
        sample_count=1 if sample else 0,
    )


def _clone() -> Voice:
    return Voice(
        id="clone_capable:c1",
        source="clone_capable",
        name="c1",
        payload={"ref_audio": "/archive/clone_capable/s1.mp3"},
    )


def _designed() -> Voice:
    return Voice(
        id="clone_capable:d1",
        source="clone_capable",
        name="d1",
        payload={"design_prompt": "低沉的老者。"},
    )


#: 每一条音色在每台引擎上的凭据（与引擎能力无关，只看音色自己 + 是不是同平台）。
CREDENTIALS = {
    "本地音色": (_local(), {"preset_only": REFERENCE, "clone_capable": REFERENCE}),
    "本地音色没有样例": (_local(False), {"preset_only": NONE, "clone_capable": NONE}),
    "预置音色回到自己的平台": (
        _platform("preset_only", sample=True),
        {"preset_only": PLATFORM, "clone_capable": REFERENCE},
    ),
    "预置音色没有样例去别家": (
        _platform("preset_only"),
        {"preset_only": PLATFORM, "clone_capable": NONE},
    ),
    "复刻音色（样本即音色）": (_clone(), {"preset_only": REFERENCE, "clone_capable": REFERENCE}),
    "设计音色哪家都能用": (_designed(), {"preset_only": DESIGN, "clone_capable": DESIGN}),
}

#: 上面那些凭据落到"引擎能力"上之后，还剩哪些引擎真能用。
USABLE = {
    "本地音色": ["clone_capable"],
    "本地音色没有样例": [],
    "预置音色回到自己的平台": ["clone_capable", "preset_only"],
    "预置音色没有样例去别家": ["preset_only"],
    "复刻音色（样本即音色）": ["clone_capable"],
    "设计音色哪家都能用": ["clone_capable"],
}


def _fakes(monkeypatch):
    monkeypatch.setattr(engines_uc, "list_engines", lambda: dict(ENGINES))
    # 这几台假引擎不在配置的启用清单里：被测的是"已启用之后"的判定。
    monkeypatch.setattr(engines_uc, "enabled_keys", lambda: set(ENGINES))


def test_local_voice_never_carries_its_platform_id() -> None:
    """本地音色没有平台 id：哪台引擎都是按参考音频发。"""
    assert credential_of(_local(), engine_key="preset_only") == REFERENCE
    assert credential_of(_local(), engine_key="clone_capable") == REFERENCE


def test_a_voice_without_any_audio_lands_nowhere() -> None:
    assert credential_of(_local(False), engine_key="clone_capable") == NONE
    assert credential_of(_platform("preset_only"), engine_key="clone_capable") == NONE


def test_engines_for_voice_follows_the_credential(monkeypatch) -> None:
    _fakes(monkeypatch)
    for label, (voice, _) in CREDENTIALS.items():
        assert engines_uc.engines_for_voice(voice) == USABLE[label], label


def test_allowed_engines_follows_the_credential(monkeypatch) -> None:
    """分配弹窗的候选引擎与网关同一口径（这里只多一道"就绪"过滤）。"""
    caps = {
        key: {
            "label": engine.label,
            "ready": True,
            "reference": engine.capabilities().reference_audio,
            "listing": engine.capabilities().voice_listing,
            "design": engine.capabilities().voice_design,
        }
        for key, engine in ENGINES.items()
    }
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: caps)
    for label, (voice, _) in CREDENTIALS.items():
        assert voice_assign.allowed_engines(voice, caps) == USABLE[label], label


def test_the_gateway_sends_exactly_that_credential() -> None:
    """网关发出去的东西与凭据一致；``NONE`` 在发请求之前就说清。"""
    for label, (voice, expected) in CREDENTIALS.items():
        for key, cred in expected.items():
            if cred == NONE:
                try:
                    _library_request(voice, emotion="", engine_key=key)
                except EngineError:
                    continue
                raise AssertionError(f"{label} / {key}：该拒没拒")
            request = _library_request(voice, emotion="", engine_key=key)
            if cred == PLATFORM:
                assert request.voice_id == voice.name and not request.reference_audio, label
            elif cred == REFERENCE:
                assert request.reference_audio and not request.extra.get("voice_design"), label
            else:
                assert request.extra.get("voice_design"), label


def test_the_loose_call_site_keeps_the_old_default() -> None:
    """没给引擎键时按"自己的平台"处理：先前的 ``normalize_voice`` 调用形态照旧。"""
    voice = _platform("preset_only", sample=True)
    assert credential_of(voice) == PLATFORM
    assert _library_request(voice, emotion="").voice_id == voice.name


def test_synthesize_refuses_before_calling_the_engine() -> None:
    """跨平台没样例：错误在网关这一层说，引擎一次都不该被调到。"""

    calls: list[str] = []

    class Named(_CloneCapable):
        async def synthesize(self, cfg, req):  # pragma: no cover - 不该被调用
            calls.append(req.voice_id)
            raise AssertionError("引擎被调用了")

    class Service:
        def get_voice(self, voice_id):
            return _platform("preset_only")

    try:
        asyncio.run(
            synthesize(
                Service(),
                engine_key="clone_capable",
                voice_id="preset_only:1",
                text="你好",
                engines={"clone_capable": Named()},
            )
        )
    except EngineError:
        pass
    else:  # pragma: no cover - 没报错就是漏了
        raise AssertionError("应当拒绝")
    assert calls == []
