from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.producers.builtin.audiobook.qc_stage import (
    compare,
    inspect_acoustic,
    normalize,
    syllables,
)


def test_normalize_drops_marks_and_collapses_numbers():
    assert normalize("他[[停顿:0.4]]说：2024 年。") == "他说 \u03a9 年"
    assert normalize("2000") == normalize("两千") == normalize("2000")


def test_syllables_are_toneless():
    assert syllables("重庆") == ["chong", "qing"]
    assert syllables("医院") == syllables("医愿")


def test_homophones_pass():
    result = compare("他去了医院看病。", "他去了医愿看病")
    assert result["verdict"] == "ok"


def test_missing_tail_is_reported():
    result = compare("他推开门走进屋子看见桌上放着一封没有署名的信。",
                     "他推开门走进屋子")
    assert result["verdict"] == "suspect"
    assert any(item["kind"] == "missing" for item in result["detail"]["issues"])


def test_empty_audio_is_suspect():
    result = compare("他推开门。", "")
    assert result["verdict"] == "suspect"
    assert result["detail"]["issues"][0]["kind"] == "silent"


def test_fillers_do_not_count_as_repeat():
    result = compare("他推开门走进屋子坐下。", "嗯他推开门走进屋子坐下")
    assert result["verdict"] == "ok"


def test_length_gap_is_caught():
    result = compare("他数到一百然后停下来看着窗外", "他数到一百")
    assert result["verdict"] == "suspect"
    assert result["detail"]["issues"][0]["kind"] == "missing"


def test_blank_source_is_unknown():
    assert compare("", "任意")["verdict"] == "unknown"


def test_punctuation_spacing_does_not_trigger_missing_or_repeat():
    assert compare("“哟！少爷也一道来了？”", "哟少爷也一道来了。")["verdict"] == "ok"
    assert compare("如今菊治走在寺院山中小鸟啁啾鸣啭的庭院里", "如今，菊治走在寺院山中，小鸟啁啾鸣啭的庭院里")["verdict"] == "ok"


def test_short_line_single_asr_slip_is_tolerated():
    # 单字章节号与 2-4 字短句的单个音节识别偏差不再直接判疑似
    assert compare("一", "Yeah.")["verdict"] == "ok"
    assert compare("近子说，", "镜子说。")["verdict"] == "ok"
    assert compare("“您早！”", "您则？")["verdict"] == "ok"
    # 多字短句连续走偏仍能检出
    assert compare("“不是相亲。”", "不是星期。")["verdict"] == "suspect"


@pytest.mark.parametrize("heard", ["二零二四", "2024", "两千零二十四"])
def test_number_readings_agree(heard: str) -> None:
    assert compare("2024年出版", f"{heard}年出版")["verdict"] == "ok"


def _write_wav(path: Path, samples: list[int], rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_acoustic_detects_silent_wav(tmp_path: Path) -> None:
    wav = tmp_path / "silent.wav"
    _write_wav(wav, [0] * 8000)
    result = inspect_acoustic(wav, "你好世界")
    assert any(item["kind"] == "silent" for item in result["issues"])


def test_acoustic_detects_truncated_wav(tmp_path: Path) -> None:
    wav = tmp_path / "cut.wav"
    # 0.5 秒持续高能量正弦波，结尾无衰减 -> 尾音截断
    samples = [int(12000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(8000)]
    _write_wav(wav, samples)
    result = inspect_acoustic(wav, "你好")
    assert any(item["kind"] == "truncated" for item in result["issues"])


def test_acoustic_passes_clean_faded_wav(tmp_path: Path) -> None:
    wav = tmp_path / "clean.wav"
    # 1.0 秒人声段 + 末尾 0.1 秒静音收尾，匹配约 5 字台词
    speech = [int(10000 * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(14400)]
    tail = [0] * 1600
    _write_wav(wav, speech + tail)
    result = inspect_acoustic(wav, "他推开门看")
    assert result["issues"] == []


def test_acoustic_detects_too_short_duration(tmp_path: Path) -> None:
    wav = tmp_path / "short.wav"
    speech = [int(10000 * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(3200)]
    _write_wav(wav, speech + [0] * 800)
    result = inspect_acoustic(wav, "这是一段很长很长需要好几秒才能念完的正文内容")
    assert any(
        item["kind"] == "duration" and item.get("detail") == "too_short"
        for item in result["issues"]
    )
