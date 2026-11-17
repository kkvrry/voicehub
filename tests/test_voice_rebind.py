"""扫描后音色引用自愈：目录移动导致旧 id 失效时，按唯一同名重绑并保住旧音频指纹。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.producers.builtin.audiobook import audio_stage, rebind
from app.producers.builtin.audiobook.audio_stage import row_key


class FakeLibrary:
    def __init__(self, voices):
        self._voices = voices

    def list_voices(self, *, source=""):
        return self._voices


class FakeStore:
    def __init__(self, *, assignments, maps, rows=None):
        self.assignments = assignments
        self.maps = maps
        self.rows = rows or {}
        self.updated = []
        self.keys = []

    def voice_assignments(self):
        return self.assignments

    def voice_map(self, project_id):
        return self.maps.get(project_id, {})

    def all_rows(self, project_id):
        return self.rows.get(project_id, [])

    def rebind_voice_ids(self, items):
        self.updated = items
        return len(items)

    def set_row_audio_keys(self, items):
        self.keys = items


def _voice(voice_id, name, *, local=True, hidden=False):
    return SimpleNamespace(id=voice_id, shown_name=name, is_local=local, hidden=hidden)


def _assignment(speaker="老周", name="胡桃", voice_id="old-id", project="p1"):
    return {
        "project_id": project,
        "speaker": speaker,
        "engine": "fake",
        "voice_id": voice_id,
        "voice_name": name,
    }


def _mapping(voice_id="old-id", name="胡桃"):
    return {
        "voice_id": voice_id,
        "voice_name": name,
        "engine": "fake",
        "sample_mode": "emotion",
        "sample_file": "",
        "reason": "",
    }


def _row(**fields):
    row = {
        "id": "r1",
        "speaker": "老周",
        "text": "你来了。",
        "emotion": "平静",
        "params": "{}",
        "audio": "r1.mp3",
        "audio_key": "",
        "audio_key_v": 2,
    }
    row.update(fields)
    return row


@pytest.fixture(autouse=True)
def _fake_engine(monkeypatch):
    monkeypatch.setattr(rebind, "engine_caps", lambda: {"fake": {"ready": True}})
    monkeypatch.setattr(
        rebind, "allowed_engines", lambda voice, caps: ["fake"]
    )
    monkeypatch.setattr(rebind.engines_uc, "active_engines", lambda: {"fake": object()})
    monkeypatch.setattr(rebind, "voice_identity", lambda engine: "v1")
    monkeypatch.setattr(rebind, "emotion_words", lambda: ["平静"])
    monkeypatch.setattr(audio_stage.pronunciation, "matches", lambda text: "")
    monkeypatch.setattr(audio_stage.pronunciation, "revision", lambda: "")


def test_rebind_unique_name_and_keep_audio_keys() -> None:
    old = _mapping()
    new = _mapping(voice_id="new-id")
    row = _row()
    row["audio_key"] = row_key(
        row, old, words={"平静"}, identity="v1", version=2, revision=""
    )
    store = FakeStore(
        assignments=[_assignment()],
        maps={"p1": {"老周": old}},
        rows={"p1": [row]},
    )

    report = rebind.rebind_stale_voices(store, FakeLibrary([_voice("new-id", "胡桃")]))

    assert report == {"rebound": 1, "keys": 1, "ambiguous": 0, "missing": 0}
    assert store.updated == [("p1", "老周", "new-id", "胡桃")]
    # 旧音频没变，只是路径换了：指纹跟着新 id 重算，但不把音频判成过期。
    assert store.keys == [
        (
            row_key(
                row, new, words={"平静"}, identity="v1", version=2, revision=""
            ),
            "r1",
        )
    ]


def test_rebind_skips_ambiguous_and_missing_names() -> None:
    store = FakeStore(
        assignments=[
            _assignment(speaker="美国兵", name="张飞", voice_id="old-a"),
            _assignment(speaker="路人", name="已删除", voice_id="old-b"),
            _assignment(speaker="老周", name="胡桃", voice_id="old-c"),
        ],
        maps={
            "p1": {
                "美国兵": _mapping("old-a", "张飞"),
                "路人": _mapping("old-b", "已删除"),
                "老周": _mapping("old-c", "胡桃"),
            }
        },
    )
    library = FakeLibrary(
        [
            _voice("t1", "张飞"),
            _voice("t2", "张飞"),
            _voice("h1", "胡桃"),
        ]
    )

    report = rebind.rebind_stale_voices(store, library)

    assert report == {"rebound": 1, "keys": 0, "ambiguous": 1, "missing": 1}
    assert store.updated == [("p1", "老周", "h1", "胡桃")]


def test_rebind_keeps_narration_and_updates_blank_speaker_rows() -> None:
    old = _mapping()
    row = _row(speaker="")
    row["audio_key"] = row_key(
        row, old, words={"平静"}, identity="v1", version=2, revision=""
    )
    store = FakeStore(
        assignments=[_assignment(speaker="旁白")],
        maps={"p1": {"旁白": old}},
        rows={"p1": [row]},
    )

    report = rebind.rebind_stale_voices(store, FakeLibrary([_voice("new-id", "胡桃")]))

    assert report["rebound"] == 1
    assert report["keys"] == 1
    assert store.updated == [("p1", "旁白", "new-id", "胡桃")]


def test_rebind_leaves_stale_fingerprint_alone() -> None:
    row = _row(audio_key="not-the-current-key")
    store = FakeStore(
        assignments=[_assignment()],
        maps={"p1": {"老周": _mapping()}},
        rows={"p1": [row]},
    )

    report = rebind.rebind_stale_voices(store, FakeLibrary([_voice("new-id", "胡桃")]))

    # 映射照修（试听/生成能用），但旧指纹本来就对不上，不冒充它还有效。
    assert report["rebound"] == 1
    assert report["keys"] == 0
    assert store.keys == []
