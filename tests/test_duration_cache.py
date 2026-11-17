"""时长缓存与批量探测：**该量的量、该跳的跳**。

这一层只有一个目标：别为没动过的文件再起一次 ffprobe（一次约 200ms，三千条就是
十分钟）。所以测试的重点是"什么时候**不**量"——跳过判错的表现是界面上一串错的
时长，比慢得多严重。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import make_wav

from app.infra import audioprobe
from app.infra.db import HubDB, path_key
from app.infra.duration_cache import DurationCache
from app.infra.walker import walk_root


@pytest.fixture()
def fake_ffprobe(monkeypatch: pytest.MonkeyPatch):
    """把 ffprobe 换成一台**记数器**：返回本文件名的长度，同时把每次调用记下来。

    既 stub 找路径、又 stub 调用本身：本机 PATH 上有没有 ffprobe 不该影响这些用例
    的结果（`probe_many` 在没有 ffprobe 时根本不走这条路）。
    """
    calls: list[Path] = []

    def measure(path: Path, _ffprobe: str) -> float:
        calls.append(path)
        return 1.5

    monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
    monkeypatch.setattr(audioprobe, "_ffprobe_duration", measure)
    return calls


class TestDurationCache:
    @staticmethod
    def _known(cache: DurationCache) -> dict[str, tuple[int, float, float]]:
        """把刚量的一批取出来，做成"下次从库里读回来"的那一份。"""
        return {key: (size, mtime, duration) for key, size, mtime, duration in cache.entries()}

    def test_miss_then_hit(self, tmp_path: Path):
        audio = tmp_path / "开心.mp3"
        audio.write_bytes(b"abc")
        cache = DurationCache.loaded(None)
        assert cache.lookup(audio) is None  # 第一次：没得可命中
        cache.remember(audio, 3.25)

        # 第二次扫描（同一份从库里读回来）：指纹一致 → 命中，不必再量
        assert DurationCache.loaded(self._known(cache)).lookup(audio) == 3.25

    def test_size_change_invalidates_the_cache(self, tmp_path: Path):
        audio = tmp_path / "开心.mp3"
        audio.write_bytes(b"abc")
        cache = DurationCache.loaded(None)
        cache.lookup(audio)
        cache.remember(audio, 3.0)

        audio.write_bytes(b"abcde")  # 原地换过文件：长度变了
        assert DurationCache.loaded(self._known(cache)).lookup(audio) is None

    def test_mtime_change_invalidates_the_cache(self, tmp_path: Path):
        audio = tmp_path / "开心.mp3"
        audio.write_bytes(b"abc")
        cache = DurationCache.loaded(None)
        cache.lookup(audio)
        cache.remember(audio, 3.0)

        os.utime(audio, (0, 0))  # 内容没变，只被碰过一下
        assert DurationCache.loaded(self._known(cache)).lookup(audio) is None

    def test_key_is_the_folded_path_the_database_uses(self, tmp_path: Path):
        """键必须是 ``db.path_key`` 那一套折形——库里存的折过，这边不折就永不命中。

        症状只是"它还是那么慢"，看不出错在哪；所以这里把契约钉死。
        """
        audio = tmp_path / "y.mp3"
        audio.write_bytes(b"abc")
        cache = DurationCache.loaded(None)
        cache.lookup(audio)
        cache.remember(audio, 2.0)
        key, size, mtime, _duration = cache.entries()[0]
        assert key == path_key(str(audio))

        # 另一种写法（正斜杠 + 大写）经同一套折形之后是同一个键 → 照样命中
        other_spelling = str(audio).replace("\\", "/").upper()
        assert other_spelling != str(audio)
        known = {path_key(other_spelling): (size, mtime, 2.0)}
        assert DurationCache.loaded(known).lookup(audio) == 2.0

    def test_remember_without_lookup_records_nothing(self, tmp_path: Path):
        """没查过指纹就记一条——那会写出一行"大小 0"的垃圾，下次还是对不上。"""
        cache = DurationCache.loaded(None)
        cache.remember(tmp_path / "没有过.mp3", 9.0)
        assert cache.entries() == []

    def test_missing_file_is_not_remembered(self, tmp_path: Path):
        cache = DurationCache.loaded(None)
        assert cache.lookup(tmp_path / "不存在.mp3") is None
        cache.remember(tmp_path / "不存在.mp3", 0.0)
        assert cache.entries() == []

    def test_unmeasurable_result_is_not_remembered(self, tmp_path: Path):
        """``0.0`` 是"这一次没量出来"，不是"这个文件没有时长"——记下来就把「未知」钉死了。"""
        audio = tmp_path / "这次量不出来.mp3"
        audio.write_bytes(b"abc")
        cache = DurationCache.loaded(None)
        cache.lookup(audio)
        cache.remember(audio, 0.0)
        assert cache.entries() == []


class TestProbeMany:
    def test_keeps_input_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """顺序错位 = 把时长挂到别的文件上，而这种错在界面上看不出来。"""
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(
            audioprobe, "_ffprobe_duration", lambda path, _ffprobe: float(path.stem)
        )
        paths = []
        for index in range(50):  # 多于线程数，确保真的走了线程池
            audio = tmp_path / f"{index}.mp3"
            audio.write_bytes(b"x")
            paths.append(audio)

        assert audioprobe.probe_many(paths) == [float(index) for index in range(50)]

    def test_without_ffprobe_only_wav_is_measured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """没有 ffprobe：wav 走标准库读得出，其余**如实给 0.0**（界面显示「未知」）。"""
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "")
        make_wav(tmp_path / "开心.wav", seconds=0.4)
        (tmp_path / "开心.mp3").write_bytes(b"x")

        wav, mp3 = audioprobe.probe_many([tmp_path / "开心.wav", tmp_path / "开心.mp3"])
        assert wav == pytest.approx(0.4, abs=0.01)
        assert mp3 == 0.0

    def test_empty_input_measures_nothing(self, fake_ffprobe):
        assert audioprobe.probe_many([]) == []
        assert fake_ffprobe == []


class TestDatabaseRoundTrip:
    def test_remember_then_load(self, data_dir: Path):
        db = HubDB(data_dir / "hub.db")
        db.remember_durations([("c:\\lib\\a.mp3", 4096, 1700000000.5, 3.25)])
        assert db.duration_cache() == {"c:\\lib\\a.mp3": (4096, 1700000000.5, 3.25)}

    def test_same_path_is_updated_not_duplicated(self, data_dir: Path):
        db = HubDB(data_dir / "hub.db")
        db.remember_durations([("c:\\lib\\a.mp3", 4096, 1.0, 3.25)])
        db.remember_durations([("c:\\lib\\a.mp3", 8192, 2.0, 7.5)])
        assert db.duration_cache() == {"c:\\lib\\a.mp3": (8192, 2.0, 7.5)}

    def test_rows_without_fingerprint_are_not_offered(self, data_dir: Path):
        """``size = 0`` 是"没有指纹"的行——它永远比不中，别让它进缓存。"""
        db = HubDB(data_dir / "hub.db")
        db.remember_durations([("c:\\lib\\a.mp3", 0, 0.0, 0.0)])
        assert db.duration_cache() == {}


class TestScanReusesDurations:
    """扫描的端到端行为：第二次扫描**一次 ffprobe 都不该起**。"""

    @staticmethod
    def _library(tmp_path: Path) -> Path:
        root = tmp_path / "lib"
        voice = root / "原神" / "胡桃"
        voice.mkdir(parents=True)
        for index in range(3):
            (voice / f"开心_{index}.mp3").write_bytes(b"mp3-" + bytes([index]))
        return root

    def test_second_scan_probes_nothing(self, service, tmp_path: Path, fake_ffprobe):
        root = self._library(tmp_path)
        service.save_folders([str(root)])
        assert len(fake_ffprobe) == 3  # 首次：三条各量一次

        service.scan()
        assert len(fake_ffprobe) == 3  # 重扫：三条全部命中，一次都没多量

        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        durations = {s.file: s.duration for s in service.get_voice(hutao.id).samples}
        assert set(durations.values()) == {1.5}

    def test_changed_file_is_measured_again(self, service, tmp_path: Path, fake_ffprobe):
        root = self._library(tmp_path)
        service.save_folders([str(root)])
        assert len(fake_ffprobe) == 3

        (root / "原神" / "胡桃" / "开心_0.mp3").write_bytes(b"a-changed-audio-file")
        service.scan()
        assert len(fake_ffprobe) == 4  # 只有被改过的那一条重量

    def test_new_voice_is_measured(self, service, tmp_path: Path, fake_ffprobe):
        root = self._library(tmp_path)
        service.save_folders([str(root)])
        (root / "原神" / "钟离").mkdir()
        (root / "原神" / "钟离" / "默认.mp3").write_bytes(b"x")

        service.scan()
        assert len(fake_ffprobe) == 4  # 新增的那一条
        assert {v.name for v in service.list_voices()} == {"胡桃", "钟离"}

    def test_unmeasurable_files_are_retried_next_scan(
        self, service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """这次量不出来（比如 ffprobe 还没装）→ 下次扫描**再试一次**，不会被缓存钉住。"""
        calls: list[Path] = []

        def nothing(_path: Path, _ffprobe: str) -> float:
            calls.append(_path)
            return 0.0

        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "ffprobe")
        monkeypatch.setattr(audioprobe, "_ffprobe_duration", nothing)

        root = self._library(tmp_path)
        service.save_folders([str(root)])
        service.scan()
        assert len(calls) == 6  # 两条各量了两次：一次都没量出来，所以没有一条进缓存

        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        assert {s.duration for s in service.get_voice(hutao.id).samples} == {0.0}


class TestWalkRootWithoutCache:
    """不传缓存的那条老路仍然现量（单文件调用与测试走它）。"""

    def test_measures_every_file(self, tmp_path: Path, fake_ffprobe):
        make_wav(tmp_path / "原神" / "胡桃" / "开心.wav", seconds=0.4)
        (tmp_path / "原神" / "胡桃" / "难过.mp3").write_bytes(b"x")

        found = walk_root(str(tmp_path))
        assert len(found) == 1
        durations = {sample["file"]: sample["duration"] for sample in found[0].samples}
        assert durations["开心.wav"] == pytest.approx(0.4, abs=0.01)  # 标准库读的
        assert durations["难过.mp3"] == 1.5  # ffprobe 读的
        assert len(fake_ffprobe) == 1  # wav 不该起进程
