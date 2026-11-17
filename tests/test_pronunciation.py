"""读音替换：最长匹配、方括号保护、保存校验与接口。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.usecase import pronunciation


def _save(*pairs: tuple[str, str]) -> None:
    pronunciation.save([{"word": word, "replacement": replacement} for word, replacement in pairs])


def test_empty_table_leaves_text_untouched(data_dir: Path):
    text, applied = pronunciation.apply("[笑]重庆银行")

    assert text == "[笑]重庆银行"
    assert applied == []


def test_replaces_words_and_reports_what_hit(data_dir: Path):
    _save(("重庆", "崇庆"), ("银行", "银航"))

    text, applied = pronunciation.apply("她从重庆坐车去银行。")

    assert text == "她从崇庆坐车去银航。"
    assert applied == ["重庆", "银行"]


def test_longest_word_wins(data_dir: Path):
    _save(("长", "常"), ("长大", "掌大"))

    text, applied = pronunciation.apply("长大的长度")

    assert text == "掌大的常度"
    assert applied == ["长大", "长"]


def test_markers_are_not_replaced(data_dir: Path):
    _save(("重庆", "崇庆"))

    assert pronunciation.apply("[重庆][笑]重庆")[0] == "[重庆][笑]崇庆"


def test_replacement_is_not_applied_recursively(data_dir: Path):
    _save(("重", "崇"), ("崇", "虫"))

    assert pronunciation.apply("重")[0] == "崇"


def test_duplicate_word_is_refused(data_dir: Path):
    with pytest.raises(pronunciation.PronunciationError, match="重复"):
        _save(("重庆", "崇庆"), ("重庆", "虫庆"))


def test_empty_or_bracket_entry_is_refused(data_dir: Path):
    with pytest.raises(pronunciation.PronunciationError):
        _save(("", "崇庆"))
    with pytest.raises(pronunciation.PronunciationError, match="方括号"):
        _save(("重庆", "[崇庆]"))


def test_hand_edited_bad_entries_are_filtered(data_dir: Path):
    (data_dir / "pronunciations.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"word": "重庆", "replacement": "崇庆"},
                    {"word": "", "replacement": "空词"},
                    {"word": "银行", "replacement": "[银航]"},
                    {"word": "重庆", "replacement": "虫庆"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert pronunciation.state()["entries"] == [{"word": "重庆", "replacement": "虫庆"}]


def test_revision_changes_only_with_entries(data_dir: Path):
    assert pronunciation.revision() == ""

    _save(("重庆", "崇庆"))
    first = pronunciation.revision()
    assert first

    _save(("重庆", "虫庆"))
    assert pronunciation.revision() != first

    _save()
    assert pronunciation.revision() == ""


def test_api_roundtrip(client):
    response = client.put(
        "/api/v1/pronunciations",
        json={"entries": [{"word": "重庆", "replacement": "崇庆"}]},
    )

    assert response.status_code == 200
    assert response.json()["entries"] == [{"word": "重庆", "replacement": "崇庆"}]
    assert client.get("/api/v1/pronunciations").json()["entries"] == [
        {"word": "重庆", "replacement": "崇庆"}
    ]


def test_api_rejects_duplicate(client):
    response = client.put(
        "/api/v1/pronunciations",
        json={
            "entries": [
                {"word": "重庆", "replacement": "崇庆"},
                {"word": "重庆", "replacement": "虫庆"},
            ]
        },
    )

    assert response.status_code == 400
    assert "重复" in response.json()["error"]["message"]
