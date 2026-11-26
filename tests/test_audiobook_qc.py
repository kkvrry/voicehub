from __future__ import annotations

import pytest

from app.producers.builtin.audiobook.qc_stage import compare, normalize, syllables


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


@pytest.mark.parametrize("heard", ["二零二四", "2024", "两千零二十四"])
def test_number_readings_agree(heard: str) -> None:
    assert compare("2024年出版", f"{heard}年出版")["verdict"] == "ok"
