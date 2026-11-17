"""增量扫描：没动过的音色整条复用，动过的才重新合并。

口径是 ``FoundVoice.stamp``（这一层每个文件的名字 / 大小 / mtime_ns 摘要）。这里
守住四件事：**重复扫描不重写、改动只影响那一个文件夹、改过的元数据不被扫回去、
上次没量出时长的下次还要再试**。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.infra import audioprobe
from app.infra.sidecar import SIDECAR_NAME, Sidecar, write_sidecar


def _changed(folder: Path, name: str) -> None:
    """往文件夹里丢一个新文件——目录指纹必然变。"""
    (folder / name).write_text("变了", encoding="utf-8")


class TestReuse:
    def test_second_scan_reuses_every_voice(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        second = service.scan()
        assert second["voices"] == 4
        assert second["unchanged"] == 4  # 一个字节都没重写

    def test_third_scan_is_still_stable(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        service.scan()
        assert service.scan()["unchanged"] == 4

    def test_only_the_touched_folder_is_remerged(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        _changed(demo_library / "原神" / "胡桃", "新片段.txt")
        summary = service.scan()
        assert summary["unchanged"] == 3  # 其余三条照旧复用

    def test_new_flat_file_does_not_remerge_its_neighbours(
        self, service, demo_library: Path
    ):
        """根目录平铺的那几条各自一枚指纹：丢一条新音频进去，不该让别的重扫。"""
        service.save_folders([str(demo_library)])
        (demo_library / "新散件.txt").write_text("不是音频", encoding="utf-8")
        summary = service.scan()
        # 加的是非音频文件，四条全复用（也没有新音色冒出来）。
        assert summary["voices"] == 4
        assert summary["unchanged"] == 4


class TestChangedContentIsPickedUp:
    def test_sample_text_edit_lands_on_next_scan(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        (demo_library / "原神" / "胡桃" / "开心.txt").write_text(
            "改过的台词，长一点。", encoding="utf-8"
        )
        service.scan()
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        samples = {s.file: s.text for s in service.get_voice(hutao.id).samples}
        assert samples["开心.wav"] == "改过的台词，长一点。"

    def test_audio_added_to_a_folder_appears(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        (demo_library / "原神" / "胡桃" / "惊讶.mp3").write_bytes(b"mp3")
        summary = service.scan()
        assert summary["unchanged"] == 3
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        assert "惊讶.mp3" in {s.file for s in service.get_voice(hutao.id).samples}

    def test_metadata_edited_through_the_service_survives_a_scan(
        self, service, demo_library: Path
    ):
        """改语言写的是 sidecar（``voice.json``）——它在文件夹里，指纹会变，
        下次扫描重新读 sidecar，手填的值不会被"没动过"的复用路径吞掉。"""
        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.update_voice(hutao.id, {"language": "zh"})

        service.scan()
        again = service.get_voice(hutao.id)
        assert again.language == "zh"

    def test_deleted_folder_is_dropped(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        shutil.rmtree(demo_library / "明日方舟")
        summary = service.scan()
        assert summary["voices"] == 3
        assert "阿米娅" not in {v.name for v in service.list_voices()}


class TestUnmeasuredDurations:
    def test_zero_duration_voices_are_retried(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """量不出来的（ffprobe 没装/被占用）不进复用——否则会被一条 0.0 永久钉住。"""
        calls: list[Path] = []

        def nothing(path: Path, _ffprobe: str) -> float:
            calls.append(path)
            return 0.0

        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(audioprobe, "_ffprobe_duration", nothing)

        (demo_library / "原神" / "胡桃" / "惊讶.mp3").write_bytes(b"x")  # 只能走 ffprobe
        service.save_folders([str(demo_library)])
        first = len(calls)
        assert first == 1  # 首次量了一次，没量出来
        service.scan()
        assert len(calls) > first  # 又试了一轮


class TestDurationBackupInSidecar:
    """``voice.json`` 里的时长备份：删库重扫时省掉 ffprobe，但不能一直信它。

    备份是**跟着音色目录走**的（不像 ``probe_cache`` 存在 hub.db 里），代价是它没有
    指纹可校验。所以口径是"缓存没有才用它、用完立刻记进缓存"——文件被换掉以后
    size/mtime 变了，缓存就不认这份旧值了。
    """

    def test_backup_spares_ffprobe_when_the_cache_is_empty(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[Path] = []
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(
            audioprobe, "_ffprobe_duration", lambda path, _ffprobe: calls.append(path) or 9.9
        )
        folder = demo_library / "原神" / "胡桃"
        (folder / "惊讶.mp3").write_bytes(b"x")
        write_sidecar(folder, Sidecar(samples={"惊讶.mp3": {"duration": 6.5}}))

        service.save_folders([str(demo_library)])

        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        durations = {s.file: s.duration for s in service.get_voice(hutao.id).samples}
        assert durations["惊讶.mp3"] == 6.5
        assert calls == []  # 备份顶上了，一次 ffprobe 都没起

    def test_a_replaced_file_is_measured_again(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(audioprobe, "_ffprobe_duration", lambda path, _ffprobe: 9.9)
        folder = demo_library / "原神" / "胡桃"
        (folder / "惊讶.mp3").write_bytes(b"x")
        write_sidecar(folder, Sidecar(samples={"惊讶.mp3": {"duration": 6.5}}))
        service.save_folders([str(demo_library)])

        # 同名文件换了内容：备份那份 6.5 秒已经不作数了。
        (folder / "惊讶.mp3").write_bytes(b"xx")
        service.scan()

        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        durations = {s.file: s.duration for s in service.get_voice(hutao.id).samples}
        assert durations["惊讶.mp3"] == 9.9

    def test_a_verified_backup_spares_ffprobe_after_the_folder_moves(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """改了目录名 = 所有缓存键失效，但文件一个字节没变：备份里记着字节数，照样不用量。"""
        calls: list[Path] = []
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(
            audioprobe, "_ffprobe_duration", lambda path, _ffprobe: calls.append(path) or 9.9
        )
        folder = demo_library / "原神" / "胡桃"
        (folder / "惊讶.mp3").write_bytes(b"x")
        write_sidecar(folder, Sidecar(samples={"惊讶.mp3": {"duration": 6.5, "bytes": 1}}))
        service.save_folders([str(demo_library)])
        assert calls == []  # 第一次就是靠备份

        # 搬家：路径变了（voice_id 与缓存键全换），大小没变
        (demo_library / "原神" / "胡桃").rename(demo_library / "原神" / "胡桃B")
        service.scan()

        assert calls == []  # 仍然一次都没量
        moved = next(v for v in service.list_voices() if v.name == "胡桃B")
        assert {s.file: s.duration for s in service.get_voice(moved.id).samples}["惊讶.mp3"] == 6.5

    def test_a_moved_file_with_a_different_size_is_measured(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """备份记的字节数对不上 → 不认（宁可慢一次，也不拿错的值顶上去）。"""
        calls: list[Path] = []
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(
            audioprobe, "_ffprobe_duration", lambda path, _ffprobe: calls.append(path) or 9.9
        )
        folder = demo_library / "原神" / "胡桃"
        (folder / "惊讶.mp3").write_bytes(b"x")
        write_sidecar(folder, Sidecar(samples={"惊讶.mp3": {"duration": 6.5, "bytes": 999}}))
        service.save_folders([str(demo_library)])

        assert len(calls) == 1
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        assert {s.file: s.duration for s in service.get_voice(hutao.id).samples}["惊讶.mp3"] == 9.9


class TestPrefsInSidecar:
    """收藏/隐藏的正本写在音色目录里——搬家后 id 变了，这两栏还在。"""

    def test_favorite_is_written_to_the_sidecar(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_prefs(hutao.id, favorite=True)
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["favorite"] is True

    def test_favorite_survives_moving_the_folder(self, service, demo_library: Path):
        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_prefs(hutao.id, favorite=True, hidden=True)

        # 搬家：voice_id 由路径散列而来，改名即换 id，数据库里那两栏随旧行消失
        (demo_library / "原神" / "胡桃").rename(demo_library / "原神" / "胡桃B")
        service.scan()

        moved = next(v for v in service.list_voices() if v.name == "胡桃B")
        assert moved.id != hutao.id
        full = service.get_voice(moved.id)
        assert full.favorite is True
        assert full.hidden is True

    def test_editing_metadata_does_not_drop_the_prefs(self, service, demo_library: Path):
        """改名字/改样例写的是同一份 sidecar——别把偏好两栏顺手抹了。"""
        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        service.set_prefs(hutao.id, favorite=True)
        service.update_voice(hutao.id, {"display": "胡桃木"})
        service.update_sample(hutao.id, "开心.wav", {"text": "改过的台词"})
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["favorite"] is True


class TestSidecarIsReadOnce:
    """每层 sidecar 只读一次：时长备份和元信息合并共用遍历时读的那一份。

    两处各读一遍是白花的 IO（也给了两边看到不同版本的机会）：改回两次，这条会红。
    """

    def test_the_folder_is_read_once_per_scan(
        self, service, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from app.infra import walker
        from app.usecase import library as usecase

        original = walker.read_sidecar
        reads: list[Path] = []

        def counting(folder: Path) -> Sidecar:
            reads.append(Path(folder))
            return original(folder)

        monkeypatch.setattr(walker, "read_sidecar", counting)
        monkeypatch.setattr(usecase, "read_sidecar", counting)

        service.save_folders([str(demo_library)])

        hutao = demo_library / "原神" / "胡桃"
        assert reads.count(hutao) == 1
        # 元信息仍来自那一份（不是"读不到就退回推导值"）：文字是 sidecar 里的原话。
        voice = next(v for v in service.list_voices() if v.name == "胡桃")
        texts = {s.file: s.text for s in service.get_voice(voice.id).samples}
        assert texts["开心.wav"] == "吃饱喝饱，一路走好！"
