"""麦克风那两条路：**语音输入**（录一段 → 一句话）与**参考音频录音**（录一段 → 一个路径）。

它们与"识别库里的样例"（``/asr/transcribe``）的差别只有一件事：**音频不在库里**，
是浏览器刚录的，所以由调用方上传、服务端不认路径。守住的是：

1. 录音**不进归档**：它不是任何音色的样例，也不在扫描范围内——留在磁盘上用户
   既看不见也删不掉，所以只在临时目录里、启动即清；
2. **识别引擎没配好时那句话是"去哪儿配"**，不是 traceback，也不是一句空的 200；
3. 上传的东西有上限：浏览器给的字节不能照单全收；
4. 自动转写关闭时录音照样落盘，另有一条手动补听的入口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra import recordings
from app.usecase import asr


def _wav(tmp_path: Path, *, seconds: float = 0.2) -> bytes:
    from conftest import make_wav

    target = tmp_path / "录音.wav"
    make_wav(target, seconds=seconds)
    return target.read_bytes()


@pytest.fixture()
def heard(monkeypatch: pytest.MonkeyPatch):
    """假掉推理那一层（几 GB 的模型不能真跑）：记下它被问了什么、答一句固定台词。"""
    calls: list[Path] = []

    def fake(path):
        calls.append(Path(path))
        return {"text": "月亮升起来了。"}

    monkeypatch.setattr(asr, "transcribe_file", fake)
    return calls


class TestDictation:
    def test_a_recording_becomes_one_sentence(self, client, tmp_path: Path, heard):
        resp = client.post(
            "/api/v1/asr/dictation", files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")}
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "月亮升起来了。"
        assert heard, "得真的去听一遍"

    def test_the_upload_is_not_kept(self, client, tmp_path: Path, heard):
        """听完即删：这段录音唯一的用途就是这一句话。"""
        client.post(
            "/api/v1/asr/dictation", files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")}
        )
        assert heard and not heard[0].exists()

    def test_without_a_model_we_say_where_to_get_one(
        self, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """顺序是"能不能识别"排在前面：都没装模型时，"去哪儿装"才是用户要听的那句。

        **"没装"这件事由用例造出来，不看这台机器装没装**——否则同一份测试在装了
        模型的机器上会假装通过（它只是识别成功了），守不住这条路径。
        """
        monkeypatch.setattr(asr, "current_key", lambda: "")
        resp = client.post(
            "/api/v1/asr/dictation", files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")}
        )
        assert resp.status_code == 400
        assert "识别引擎" in resp.json()["error"]["message"]

    def test_no_audio_is_a_readable_400(self, client):
        resp = client.post("/api/v1/asr/dictation")
        assert resp.status_code == 422 or resp.status_code == 400

    def test_an_empty_recording_is_a_readable_400(self, client):
        resp = client.post("/api/v1/asr/dictation", files={"file": ("录音.wav", b"", "audio/wav")})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "audio_empty"


class TestReferenceRecording:
    def test_the_file_lands_in_the_recordings_dir(self, client, tmp_path: Path, data_dir: Path):
        resp = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
            data={"transcribe": "0"},
        )
        assert resp.status_code == 200
        path = Path(resp.json()["path"])
        assert path.is_file(), "界面要拿这个路径去合成，落不了盘等于白录"
        assert path.parent == recordings.root()
        assert resp.json()["text"] == "", "没让听就别编一句"

    def test_two_recordings_in_one_second_do_not_overwrite_each_other(
        self, client, tmp_path: Path
    ):
        first = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
            data={"transcribe": "0"},
        ).json()["path"]
        second = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path, seconds=0.3), "audio/wav")},
            data={"transcribe": "0"},
        ).json()["path"]
        assert first != second, "盖掉前一个会让上一条请求指向新音频"
        assert Path(first).is_file()

    def test_the_transcript_comes_along_when_asked(self, client, tmp_path: Path, heard):
        payload = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
            data={"transcribe": "1"},
        ).json()
        assert payload["text"] == "月亮升起来了。"

    def test_a_failed_transcript_does_not_lose_the_recording(self, client, tmp_path: Path, monkeypatch):
        """转不出来不是"录音失败"：音频已经能当参考音频用了，
        逐字稿只是顺手补的一栏（这一格本来也能手填）。"""

        def boom(_path):
            raise asr.AsrError("引擎没配好。")

        monkeypatch.setattr(asr, "transcribe_file", boom)
        payload = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
        ).json()
        assert Path(payload["path"]).is_file()
        assert payload["text"] == ""

    def test_auto_transcript_can_be_turned_off(
        self, client, tmp_path: Path, heard, data_dir: Path
    ):
        asr.save_options({"transcribe_recordings": False})
        payload = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
        ).json()
        assert Path(payload["path"]).is_file()
        assert payload["text"] == ""
        assert heard == [], "关掉自动转写后不该调用识别"

    def test_a_recording_can_be_transcribed_after_the_fact(
        self, client, tmp_path: Path, heard
    ):
        payload = client.post(
            "/api/v1/recordings",
            files={"file": ("录音.wav", _wav(tmp_path), "audio/wav")},
            data={"transcribe": "0"},
        ).json()
        resp = client.post(f"/api/v1/recordings/{payload['name']}/transcribe")
        assert resp.status_code == 200
        assert resp.json()["text"] == "月亮升起来了。"

    def test_transcribing_a_missing_recording_is_a_readable_404(self, client):
        resp = client.post("/api/v1/recordings/不存在.wav/transcribe")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "recording_not_found"


class TestCleanup:
    def test_startup_clears_what_the_last_run_left(self, data_dir: Path):
        """上一次跑留下的录音已经没有请求会指向它们，界面上也没有入口能删。"""
        folder = recordings.root()
        (folder / "录音-20250101-000000.wav").write_bytes(b"x")
        assert recordings.clear() == 1
        assert not (folder / "录音-20250101-000000.wav").exists()
