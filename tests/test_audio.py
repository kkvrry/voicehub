"""音频库：三处来源的索引、入库与移入回收站。

走真实文件：生成的样例与缓存产物由 :func:`archive.write_audio` 落盘，录音直接写进
录音目录，扫描根用 ``demo_library``。测的是"盘上的东西经接口变成列表、入库真的复制、
删除真的进回收站"，不测前端。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra import archive, recordings
from app.infra.sidecar import SIDECAR_NAME, read_sidecar
from app.usecase import audio as audio_uc


@pytest.fixture()
def audio_lib(service, demo_library: Path, tmp_path: Path):
    """一段生成样例、一条缓存产物、一段内录、一段麦克风录音。"""
    from conftest import make_wav

    make_wav(tmp_path / "seed.wav", seconds=0.4)
    data = (tmp_path / "seed.wav").read_bytes()
    service.save_folders([str(demo_library)])
    archive.write_audio(
        "mimo",
        "MiMo",
        "冰糖",
        data,
        fmt="wav",
        voice_id="mimo:冰糖",
        text="你好，世界。",
        emotion="开心",
    )
    archive.write_audio(
        "mimo",
        "MiMo",
        "冰糖",
        data,
        fmt="wav",
        voice_id="mimo:冰糖",
        text="生成页跑出来的。",
        as_sample=False,
    )
    (recordings.root() / "内录-20260101-000000.wav").write_bytes(data)
    (recordings.root() / "录音-20260101-000100.wav").write_bytes(data)
    audio_uc.forget()
    return service


class TestIndex:
    def test_three_sources_are_one_list(self, audio_lib):
        result = audio_uc.index()
        assert result["counts"] == {"all": 4, "record": 1, "loopback": 1, "engine": 2}
        assert result["engines"] == [{"label": "MiMo", "count": 2}]
        assert result["unabsorbed"] == 4
        assert {row["source"] for row in result["data"]} == {"engine", "loopback", "record"}

    def test_urls_point_at_the_existing_endpoints(self, audio_lib):
        rows = {row["source"]: row for row in audio_uc.index()["data"]}
        assert rows["engine"]["url"].startswith("/api/v1/generated/")
        assert rows["loopback"]["url"].startswith("/api/v1/recordings/")

    def test_filters(self, audio_lib):
        engine = audio_uc.index(source="engine")
        assert engine["total"] == 2
        assert all(row["source"] == "engine" for row in engine["data"])
        assert audio_uc.index(source="engine", engine="MiniMax")["total"] == 0
        assert audio_uc.index(q="你好")["total"] == 1
        assert audio_uc.index(status="no")["total"] == 4
        assert audio_uc.index(status="yes")["total"] == 0


class TestAbsorb:
    def test_absorbs_into_existing_voice(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        hutao = next(voice for voice in audio_lib.list_voices() if voice.name == "胡桃")
        done = audio_uc.absorb(
            audio_lib, [item["id"]], {"kind": "voice", "voice_id": hutao.id, "emotion": "平静"}
        )
        assert done[0]["voice_id"] == hutao.id
        target = Path(hutao.folder) / item["file"]
        assert target.is_file()
        assert target.stat().st_size == Path(item["path"]).stat().st_size
        sample = read_sidecar(Path(hutao.folder)).samples[item["file"]]
        assert sample["text"] == item["text"]
        assert sample["emotion"] == "平静"
        assert sample["source"] == item["id"]

        again = audio_uc.index()
        assert again["unabsorbed"] == 3
        assert again["data"][0]["absorbed"][0]["voice_id"] == hutao.id

    def test_absorb_twice_is_idempotent(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        hutao = next(voice for voice in audio_lib.list_voices() if voice.name == "胡桃")
        target = {"kind": "voice", "voice_id": hutao.id}
        audio_uc.absorb(audio_lib, [item["id"]], target)
        audio_uc.absorb(audio_lib, [item["id"]], target)
        files = [path.name for path in Path(hutao.folder).iterdir() if path.suffix == ".wav"]
        assert files.count(item["file"]) == 1
        assert len(read_sidecar(Path(hutao.folder)).samples) == 1

    def test_new_voice_lands_in_its_folder(self, audio_lib, demo_library: Path):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "loopback")
        done = audio_uc.absorb(
            audio_lib,
            [item["id"]],
            {
                "kind": "new",
                "name": "新录",
                "folder": str(demo_library / "原神"),
                "gender": "female",
                "main_sample": True,
            },
        )
        folder = demo_library / "原神" / "新录"
        assert done[0]["voice_id"]
        assert (folder / item["file"]).is_file()
        side = read_sidecar(folder)
        assert side.display == "新录"
        assert side.gender == "female"
        assert side.main_sample == item["file"]

    def test_origin_without_voice_is_rejected(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "record")
        with pytest.raises(audio_uc.AudioError) as error:
            audio_uc.absorb(audio_lib, [item["id"]], {"kind": "origin"})
        assert error.value.code == "target_required"

    def test_unknown_audio_is_not_found(self, audio_lib):
        with pytest.raises(audio_uc.AudioError) as error:
            audio_uc.absorb(audio_lib, ["gen/mimo/不存在/x.wav"], {"kind": "origin"})
        assert error.value.code == "audio_not_found"


class TestDiscard:
    def test_generated_audio_goes_to_trash(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        removed = audio_uc.discard([item["id"]])
        assert removed == 1
        assert not Path(item["path"]).exists()
        assert audio_uc.index()["counts"]["engine"] == 1

    def test_recording_goes_to_trash(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "loopback")
        audio_uc.discard([item["id"]])
        assert not Path(item["path"]).exists()
        assert audio_uc.index()["counts"]["loopback"] == 0


class TestHttp:
    def test_list_endpoint(self, client, audio_lib):
        response = client.get("/api/v1/audio")
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "list"
        assert payload["counts"]["all"] == 4

    def test_absorb_endpoint_reports_bad_target(self, client, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        response = client.post(
            "/api/v1/audio/absorb", json={"ids": [item["id"]], "target": {"kind": "voice"}}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "target_required"

    def test_delete_endpoint(self, client, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        response = client.request("DELETE", "/api/v1/audio", json={"ids": [item["id"]]})
        assert response.status_code == 200
        assert response.json()["removed"] == 1

    def test_sidecar_is_the_same_file_the_library_reads(self, audio_lib):
        item = next(row for row in audio_uc.index()["data"] if row["source"] == "engine")
        folder = Path(item["folder"])
        assert (folder / SIDECAR_NAME).is_file()
