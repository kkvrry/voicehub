"""IndexTTS2 插件：采样参数怎么落（照实测核过的点，每条钉一个断言）。

没有真实网络请求：``build_body`` 是纯函数，断言的是"到底发出了什么"。

这一轮实测（audio.cpp 实例 18091，固定 seed=42，同一段参考音频）核过的事：

1. ``temperature`` / ``top_p`` / ``top_k`` / ``repetition_penalty`` / ``max_tokens``
   放进 ``options`` **都真生效**——同 seed 下输出互不相同，``max_tokens=60``
   直接截断（音频短一半）；
2. 摆在顶层会被服务端**静默忽略**（与情感参数同一条纪律，插件的 cautions 里也写着）；
3. ``interval_silence_ms`` 这个 build **不生效**（0 与 3000 的输出逐字节相同），
   所以设置模型里没有那一格——这里钉住它别被加回来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engines.base import SynthesisRequest
from app.engines.builtin.indextts2.plugin import IndexTts2Engine
from app.engines.builtin.indextts2.settings import IndexTts2Settings


@pytest.fixture()
def ref_wav(tmp_path: Path) -> Path:
    """一段真能读到的参考音频（插件会检查文件存在，零样本克隆没它不行）。"""
    target = tmp_path / "ref.wav"
    target.write_bytes(b"RIFFfakeWAVEdata")
    return target


def _req(ref_wav: Path, **over: object) -> SynthesisRequest:
    data = {
        "text": "他停下脚步，回头看了她一眼。",
        "voice_id": "叶奈法",
        "reference_audio": str(ref_wav),
        "reference_text": "某个角度说，的确。",
    }
    data.update(over)
    return SynthesisRequest(**data)  # type: ignore[arg-type]


def _build(cfg: IndexTts2Settings, req: SynthesisRequest) -> dict:
    return IndexTts2Engine().build_body(cfg, req)


class TestSamplingDefaults:
    def test_defaults_land_in_options(self, ref_wav: Path) -> None:
        options = _build(IndexTts2Settings(), _req(ref_wav))["options"]
        assert options["temperature"] == 0.8
        assert options["top_p"] == 0.8
        assert options["top_k"] == 30
        assert options["repetition_penalty"] == 10.0
        assert options["max_tokens"] == 1500

    def test_they_are_not_on_the_top_level(self, ref_wav: Path) -> None:
        """顶层同名参数会被服务端静默忽略——发错层等于没发。"""
        body = _build(IndexTts2Settings(), _req(ref_wav))
        for name in ("temperature", "top_p", "top_k", "repetition_penalty", "max_tokens"):
            assert name not in body, f"{name} 落错了层"

    def test_custom_values_go_straight_through(self, ref_wav: Path) -> None:
        cfg = IndexTts2Settings(
            temperature=0.3, top_p=0.6, top_k=5, repetition_penalty=2.0, max_tokens=800
        )
        options = _build(cfg, _req(ref_wav))["options"]
        assert options["temperature"] == 0.3
        assert options["top_p"] == 0.6
        assert options["top_k"] == 5
        assert options["repetition_penalty"] == 2.0
        assert options["max_tokens"] == 800

    def test_options_survive_a_request_without_emotion(self, ref_wav: Path) -> None:
        """没有情感参数时 options 不再被清空——采样默认值照样要发出去。"""
        body = _build(IndexTts2Settings(), _req(ref_wav))
        assert "options" in body
        assert "emotion_text" not in body["options"]

    def test_emotion_and_sampling_coexist(self, ref_wav: Path) -> None:
        # 引导指令 / 指令强度走 extra（统一格式的扩展位，映射表按名字取）。
        body = _build(
            IndexTts2Settings(),
            _req(ref_wav, extra={"instruction": "语气温柔平静", "guidance_scale": 0.7}),
        )
        options = body["options"]
        assert options["emotion_text"] == "语气温柔平静"
        assert options["use_emotion_text"] is True
        assert options["emotion_alpha"] == 0.7
        assert options["temperature"] == 0.8


class TestMarks:
    """这一家一条语气词都不认：标签由网关在发出前从正文删掉，绝不原样发。

    原样发出去的后果不是"少一条标签"，而是正文里多出"倒吸气"三个字被念出来
    （实测：生成页写了 ``[[倒吸气]]``，合成结果把它读了出来）。
    """

    def test_the_declaration_says_it_knows_none(self) -> None:
        assert IndexTts2Engine().mark_map() == {}
        assert IndexTts2Engine().mark_passthrough() == ""

    def test_a_tag_never_reaches_the_body(self, ref_wav: Path) -> None:
        engine = IndexTts2Engine()
        body = engine.build_body(
            IndexTts2Settings(),
            _req(ref_wav, text=engine.prepare_text("[[倒吸气]]他往后退了一步。")),
        )
        assert body["input"] == "他往后退了一步。"


class TestWhatIsNotExposed:
    def test_interval_silence_is_not_a_setting(self) -> None:
        """实测不生效（0 与 3000 输出逐字节相同）——不画"改了不生效"的栏。"""
        assert "interval_silence_ms" not in IndexTts2Settings.model_fields

    def test_every_sampling_field_can_be_rendered(self) -> None:
        """界面照 JSON Schema 画：没有 label / group 的字段分组渲染不出来。"""
        properties = IndexTts2Settings.model_json_schema()["properties"]
        for name in ("temperature", "top_p", "top_k", "repetition_penalty", "max_tokens"):
            spec = properties[name]
            assert spec.get("label"), f"{name} 没有显示名"
            assert spec.get("group") == "采样", f"{name} 不在「采样」组"

    def test_ranges_match_the_engine_declaration(self) -> None:
        """范围照 audio.cpp 的 paramSchema 写：超范围的值在保存时就被拒。"""
        with pytest.raises(ValueError):
            IndexTts2Settings(temperature=5.0)
        with pytest.raises(ValueError):
            IndexTts2Settings(top_k=500)
        with pytest.raises(ValueError):
            IndexTts2Settings(repetition_penalty=0.5)
