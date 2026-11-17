"""ffmpeg 那一层：**认壳**（不需要 ffmpeg）与**转码**（需要 ffmpeg）。

认壳那一半是纯字节判断，随便跑；转码那一半真起 ffmpeg，所以在没装 ffmpeg 的机器上
**跳过**而不是失败——那条路本来就不该因为环境缺一个外部程序而把整份测试弄红。
"""

from __future__ import annotations

import os
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.infra import ffmpeg


@pytest.fixture(autouse=True)
def _forget_the_cached_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """每次用例前把"PATH 上找过 ffmpeg"那个缓存清掉。

    那个缓存是给生产用的（结果不会在一个进程里变），但在一个**同一个进程里跑几百个
    用例**的地方，它会把第一个用例看到的那条路径一直留着——于是后面那些
    monkeypatch ``shutil.which`` 的用例全都测不到东西（它们以为在测"没有 ffmpeg"，
    其实拿到的是真有 ffmpeg 的那台机器上的结果）。
    """
    monkeypatch.setattr(ffmpeg, "_PATH_FFMPEG", "")


@pytest.fixture()
def ffmpeg_ready() -> None:
    """这台机器上没有 ffmpeg 就跳过（而不是失败）：转换这条路本来就依赖一个外部程序。"""
    if not ffmpeg.find_ffmpeg():
        pytest.skip("这台机器上没有 ffmpeg")


# --------------------------------------------------------------------------- #
# 认壳
# --------------------------------------------------------------------------- #


class TestSniffing:
    """看魔数认容器——**这一步不需要 ffmpeg 装好**，所以"这段是 mp3"随时说得出口。"""

    def test_a_real_wav_is_recognised(self, tmp_path: Path) -> None:
        from conftest import make_wav

        path = tmp_path / "a.wav"
        make_wav(path)
        assert ffmpeg.container_of(path) == "wav"

    def test_the_common_containers(self) -> None:
        head = ffmpeg.container_of_bytes
        assert head(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"
        assert head(b"fLaC\x00\x00\x00\x22") == "flac"
        assert head(b"OggS\x00\x02\x00\x00") == "ogg"
        assert head(b"\x00\x00\x00\x20ftypM4A ") == "m4a"
        assert head(b"ID3\x04\x00\x00\x00") == "mp3"
        # 没有 ID3 标签的 mp3：帧同步字打头。
        assert head(b"\xff\xfb\x90\x00") == "mp3"
        assert head(b"FORM\x00\x00\x00\x00AIFF") == "aiff"

    def test_a_riff_that_is_not_wave_is_not_wav(self) -> None:
        """``RIFF`` 是一族容器（avi / webp 也长这样），第二段才是"这是 wave"。"""
        assert ffmpeg.container_of_bytes(b"RIFF\x00\x00\x00\x00AVI ") == ""

    def test_unknown_is_an_empty_string_not_a_guess(self) -> None:
        """认不出就**说认不出**：裸 PCM 本来就没有文件头，这里猜一个只会骗到调用方。"""
        assert ffmpeg.container_of_bytes(b"\x00\x01\x02\x03") == ""

    def test_a_missing_file_reads_as_unknown(self, tmp_path: Path) -> None:
        assert ffmpeg.container_of(tmp_path / "没有这个文件.wav") == ""


# --------------------------------------------------------------------------- #
# 找 ffmpeg
# --------------------------------------------------------------------------- #


class TestFinding:
    def test_the_configured_folder_wins(self, tmp_path: Path, monkeypatch) -> None:
        """设置里指过 ffprobe 在哪，ffmpeg 就在它旁边——那是同一个人装同一样东西。"""
        package = tmp_path / "ffmpeg-build"
        package.mkdir()
        (package / "ffprobe").write_bytes(b"x")
        (package / "ffmpeg").write_bytes(b"x")
        monkeypatch.setattr(ffmpeg.shutil, "which", lambda _name: "/nope/ffmpeg")
        found = ffmpeg.find_ffmpeg(str(package / "ffprobe"))
        assert Path(found) == package / "ffmpeg"

    def test_it_falls_back_to_path(self, monkeypatch) -> None:
        monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: f"/usr/bin/{name}")
        # **显式给空串** = 别去翻配置（测试就是这么用的）。
        assert ffmpeg.find_ffmpeg("") == "/usr/bin/ffmpeg"

    def test_nothing_found_is_an_empty_string(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ffmpeg.shutil, "which", lambda _name: None)
        assert ffmpeg.find_ffmpeg(str(tmp_path / "没有这个目录" / "ffprobe")) == ""

    def test_without_ffmpeg_it_says_what_to_install(self, tmp_path: Path, monkeypatch) -> None:
        """**没装要说清下一步**（装一个 / 填设置里那一格），不是一句 "转换失败"。"""
        monkeypatch.setattr(ffmpeg.shutil, "which", lambda _name: None)
        source = tmp_path / "a.mp3"
        source.write_bytes(b"ID3" + b"\x00" * 32)
        with pytest.raises(ffmpeg.FfmpegError) as caught:
            ffmpeg.to_wav(source, configured="")
        assert "PATH" in str(caught.value)


# --------------------------------------------------------------------------- #
# 转码（要 ffmpeg——没有就跳过）
# --------------------------------------------------------------------------- #


def _encode_mp3(source: Path, target: Path) -> bool:
    """用 ffmpeg 自己造一条 mp3 出来（测试不该往仓库里塞二进制素材）。"""
    done = subprocess.run(
        [
            ffmpeg.find_ffmpeg(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "libmp3lame",
            str(target),
        ],
        check=False,
        capture_output=True,
    )
    return done.returncode == 0 and target.is_file()


@pytest.mark.usefixtures("ffmpeg_ready")
class TestConversion:
    @pytest.fixture()
    def mp3(self, tmp_path: Path) -> Path:
        from conftest import make_wav

        wav = tmp_path / "源.wav"
        make_wav(wav, seconds=0.3)
        target = tmp_path / "源.mp3"
        if not _encode_mp3(wav, target):
            pytest.skip("这台机器上的 ffmpeg 没有 mp3 编码器")
        return target

    def test_an_mp3_comes_out_as_a_mono_44k_wav(self, mp3: Path, data_dir: Path) -> None:
        out = ffmpeg.to_wav(mp3)
        assert ffmpeg.container_of(out) == "wav"
        with wave.open(str(out)) as handle:
            assert handle.getnchannels() == 1
            assert handle.getframerate() == ffmpeg.REF_RATE
            assert handle.getsampwidth() == 2

    def test_the_same_source_is_not_converted_twice(self, mp3: Path, data_dir: Path) -> None:
        """连着跑几次合成，只有第一次真起 ffmpeg——第二次直接拿缓存那一份。"""
        first = ffmpeg.to_wav(mp3)
        stamp = first.stat().st_mtime_ns
        assert ffmpeg.to_wav(mp3) == first
        assert first.stat().st_mtime_ns == stamp  # 没有被重写一遍

    def test_many_threads_converting_the_same_source_agree_on_one_file(
        self, mp3: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一段参考音频被很多行同时用：**只起一个 ffmpeg，只出一份文件**。

        批量合成的每一行都会走进来，缓存还没生成时它们都判成"要转"。从前它们写的是
        同一个 ``.part``：后起的 ffmpeg 把先起的截掉，先转完的那个去改名时对方还开着
        句柄——Windows 回 ``WinError 32``，整次合成 500（见 :func:`ffmpeg._serialized`）。
        """
        from concurrent.futures import ThreadPoolExecutor

        runs: list[int] = []
        real = ffmpeg.subprocess.run

        def counted(*args, **kwargs):
            runs.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(ffmpeg.subprocess, "run", counted)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: ffmpeg.to_wav(mp3), range(8)))
        assert len(set(outputs)) == 1
        assert outputs[0].is_file()
        assert len(runs) == 1
        # 中间文件与那本锁表都不该留下痕迹。
        assert not list(outputs[0].parent.glob("*.part"))
        assert ffmpeg._LOCKS == {}

    def test_a_briefly_locked_target_is_retried_instead_of_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """改名撞上"文件被占"要退避重试：引擎正在上传这段 wav 时就是这种局面。"""
        scratch = tmp_path / "a.part"
        scratch.write_bytes(b"x")
        target = tmp_path / "a.wav"
        real_replace = ffmpeg.os.replace
        calls: list[int] = []

        def locked_twice(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
            return real_replace(src, dst)

        monkeypatch.setattr(ffmpeg.os, "replace", locked_twice)
        ffmpeg._publish(scratch, target)
        assert len(calls) == 3

    def test_a_new_conversion_triggers_one_sweep(
        self, mp3: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """清扫跟着"写下新文件"走，而不是跟着请求走：命中缓存的那一次不该扫。"""
        swept: list[int] = []
        monkeypatch.setattr(
            ffmpeg,
            "trim_cache",
            lambda **_kw: swept.append(1) or {"removed": 0, "freed": 0},
        )
        monkeypatch.setattr(ffmpeg, "_SWEEPS", {})
        ffmpeg.to_wav(mp3)
        assert len(swept) == 1
        ffmpeg.to_wav(mp3)  # 缓存命中：没有写，就没得清
        assert len(swept) == 1

    def test_a_one_off_conversion_is_not_kept(self, mp3: Path, data_dir: Path) -> None:
        """识别那一路的转码是一次性的：每次一个新文件，不落缓存目录。"""
        first = ffmpeg.to_wav(mp3, rate=16000, transient=True)
        second = ffmpeg.to_wav(mp3, rate=16000, transient=True)
        assert first != second, "一次性转码不该被复用"
        assert data_dir / "cache" / "refaudio" not in first.parents
        with wave.open(str(first)) as handle:
            assert handle.getframerate() == 16000
            assert handle.getnchannels() == 1
        first.unlink()
        second.unlink()

    def test_changing_the_source_produces_a_new_file(self, mp3: Path, data_dir: Path) -> None:
        """**改过内容的源文件不能再拿老的那一份**——拿过期采样去克隆是最难查的错。"""
        first = ffmpeg.to_wav(mp3)
        from conftest import make_wav

        make_wav(mp3.parent / "别的.wav", seconds=0.5, freq=660)
        assert _encode_mp3(mp3.parent / "别的.wav", mp3)
        assert ffmpeg.to_wav(mp3) != first

    def test_a_broken_file_says_what_ffmpeg_said(self, tmp_path: Path, data_dir: Path) -> None:
        broken = tmp_path / "坏的.mp3"
        broken.write_bytes(b"ID3" + b"\x00" * 64)
        with pytest.raises(ffmpeg.FfmpegError) as caught:
            ffmpeg.to_wav(broken)
        assert "坏的.mp3" in str(caught.value)

    def test_a_missing_file_is_reported_before_ffmpeg_runs(self, tmp_path: Path, data_dir: Path) -> None:
        """文件不在时那句提示要**指向文件**：ffmpeg 自己回的是 "Invalid argument"，
        照着它查会查到格式那一头去。"""
        with pytest.raises(ffmpeg.FfmpegError) as caught:
            ffmpeg.to_wav(tmp_path / "没有这个.mp3")
        assert "移动" in str(caught.value)

    def test_the_result_lands_in_the_cache_not_in_the_library(self, mp3: Path, data_dir: Path) -> None:
        """转换结果是**中间产物**：住在服务数据目录的缓存里，不在用户的素材库里。"""
        out = ffmpeg.to_wav(mp3)
        assert out.parent == data_dir / "cache" / "refaudio"
        assert mp3.parent == data_dir.parent  # 源文件一动不动

    def test_a_wav_that_is_already_fine_is_still_converted_cleanly(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """已经是 wav 也能转（幂等）——万一它是 8bit 或者立体声，这里顺手统一成
        引擎要的那一档（调用方按后缀判断要不要转，判断错了也不会出坏事）。"""
        from conftest import make_wav

        wav = tmp_path / "立体声.wav"
        make_wav(wav)
        with wave.open(str(wav), "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        stereo = tmp_path / "双声道.wav"
        with wave.open(str(stereo), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(raw + raw)
        out = ffmpeg.to_wav(stereo)
        with wave.open(str(out)) as handle:
            assert handle.getnchannels() == 1
            assert handle.getframerate() == ffmpeg.REF_RATE
        # 也顺手钉一下上面那段造素材的代码没有写反（2 声道确实读出来是 2）。
        with wave.open(str(stereo)) as handle:
            assert handle.getnchannels() == 2
        assert struct.calcsize("<h") == 2


# --------------------------------------------------------------------------- #
# 句读断点
# --------------------------------------------------------------------------- #


def _speechy(tmp_path: Path, segments: list[tuple[float, bool]]) -> Path:
    """造一段"说一句、停一下"的录音：``(秒数, 是否出声)`` 逐段拼起来。

    出声段是一个 200Hz 的方波近似（一个周期重复，既快又确实是周期信号），
    静音段是纯 0——``silencedetect`` 看的就是这个差别。
    """
    rate = 16000
    period = b"".join(
        struct.pack("<h", 12000 * (1 if i < 40 else -1)) for i in range(80)
    )
    frames = bytearray()
    for seconds, loud in segments:
        count = int(rate * seconds)
        if loud:
            frames += period * (count // 80)
        else:
            frames += b"\x00\x00" * count
    path = tmp_path / "说话.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


class TestAsciiStaging:
    """非 ASCII 路径的副本：**只复制、不改内容**，名字换成单纯的源指纹。

    这是给 gsv.cpp 那类"把路径转本地代码页"的后端用的（见
    ``SynthesisSpec.reference_path_ascii``）；官方 Python 版不需要，但复制一份对它也无害。
    """

    def test_the_copy_gets_an_ascii_name_and_the_same_bytes(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        source = tmp_path / "战场原.wav"
        source.write_bytes(b"RIFF" + b"\x00" * 120)
        staged = ffmpeg.stage_ascii_path(source)
        assert staged.name.isascii()
        assert staged.read_bytes() == source.read_bytes()

    def test_the_same_source_is_not_copied_twice(self, tmp_path: Path, data_dir: Path) -> None:
        """缓存名带源指纹：同一条样例连发几十行，只该复制第一次。"""
        source = tmp_path / "素材.wav"
        source.write_bytes(b"RIFF" + b"\x00" * 120)
        assert ffmpeg.stage_ascii_path(source) == ffmpeg.stage_ascii_path(source)

    def test_changing_the_source_produces_a_new_copy(self, tmp_path: Path, data_dir: Path) -> None:
        """源换了一段录音（大小/时间变了）→ 新副本，不会拿旧的顶上。"""
        source = tmp_path / "素材.wav"
        source.write_bytes(b"RIFF" + b"\x00" * 120)
        first = ffmpeg.stage_ascii_path(source)
        source.write_bytes(b"RIFF" + b"\x00" * 240)
        assert ffmpeg.stage_ascii_path(source) != first


class TestCacheRecycling:
    """派生产物能重建，但不回收就一直涨：一份 12 秒样例的 44.1k wav 约 1MB。

    两条规则：超过 ``max_age`` 的一律清掉；其余按"最久没碰过"从旧到新删到总量
    落到 ``limit`` 以下。删不掉（正被读/写）的跳过，不算错。
    """

    def _make(
        self, data_dir: Path, folder: str, name: str, *, size: int, age: float
    ) -> Path:
        path = data_dir / "cache" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_the_stalest_go_first(self, data_dir: Path) -> None:
        old = self._make(data_dir, "refaudio", "old.wav", size=1000, age=40 * 86400)
        fresh = self._make(data_dir, "refaudio", "fresh.wav", size=1000, age=60)
        result = ffmpeg.trim_cache(limit=1 << 30, max_age=30 * 86400)
        assert (result["removed"], result["freed"]) == (1, 1000)
        assert not old.exists()
        assert fresh.is_file(), "还在用的那份不能因为别人过期被牵连"

    def test_the_total_size_is_capped(self, data_dir: Path) -> None:
        first = self._make(data_dir, "refaudio", "a.wav", size=800, age=300)
        second = self._make(data_dir, "refaudio", "b.wav", size=800, age=200)
        newest = self._make(data_dir, "refaudio", "c.wav", size=800, age=100)
        # 2400 字节 → 900：删最旧的两份刚好落到线下。
        result = ffmpeg.trim_cache(limit=900, max_age=365 * 86400)
        assert result["removed"] == 2
        assert newest.is_file()
        assert not first.exists() and not second.exists()

    def test_a_file_in_use_is_skipped(self, data_dir: Path) -> None:
        """正被引擎读/正在写的文件删不掉——那是"下次再说"，不是失败。"""
        stale = self._make(data_dir, "refaudio", "stale.wav", size=100, age=40 * 86400)
        other = self._make(data_dir, "refaudio", "other.wav", size=100, age=40 * 86400)
        with stale.open("rb"):
            result = ffmpeg.trim_cache(limit=1 << 30, max_age=30 * 86400)
            if os.name != "nt":
                pytest.skip("只有 Windows 会拒绝删除正在打开的文件")
            assert result["removed"] == 1
            assert stale.is_file()
            assert not other.exists()

    def test_the_work_files_are_never_touched(self, data_dir: Path) -> None:
        """``.part`` 是别人正在写的中间文件，清扫不能碰。"""
        part = self._make(data_dir, "refaudio", "a.wav.deadbeef.part", size=100, age=40 * 86400)
        result = ffmpeg.trim_cache(limit=1, max_age=1)
        assert result["removed"] == 0
        assert part.is_file()


    def test_the_policy_comes_from_settings(self, data_dir: Path) -> None:
        """不传参数时读设置里那两格；都填 0 = 一条规则都不启用。"""
        from app.infra.config import load_config, save_config

        stale = self._make(data_dir, "refaudio", "old.wav", size=4000, age=60 * 86400)
        config = load_config()
        config.cache_limit_mb = 0
        config.cache_keep_days = 0
        save_config(config)
        assert ffmpeg.trim_cache()["removed"] == 0
        assert stale.is_file()

        config.cache_keep_days = 1  # 60 天前的那份算过期
        save_config(config)
        assert ffmpeg.trim_cache()["removed"] == 1
        assert not stale.exists()

    def test_the_old_recognition_cache_is_dropped(self, data_dir: Path) -> None:
        """识别音频不再缓存（一次性转码）：旧版留下的那个目录整块清掉。"""
        stale = self._make(data_dir, "asr", "旧.wav", size=100, age=60)
        result = ffmpeg.trim_cache(limit=1 << 30, max_age=365 * 86400)
        assert result["removed"] == 1
        assert not stale.exists()


class TestCutPoint:
    def test_it_cuts_at_the_last_pause_inside_the_window(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """在 [3, 9.5] 里挑**最靠后**的那句说完处——截得越少越接近原音色。

        素材是 "2 秒话 + 0.3 秒停" 重复五遍：静音段在 2–2.3、4.3–4.6、6.6–6.9、
        8.9–9.2，落在窗口里的最后一个是 8.9–9.2，取中点 9.05。
        """
        path = _speechy(tmp_path, [(2.0, True), (0.3, False)] * 5 + [(0.5, False)])
        point, at_pause = ffmpeg.cut_point(path, low=3.0, high=9.5)
        assert at_pause is True
        assert 8.9 <= point <= 9.2

    def test_a_continuous_recording_falls_back_to_the_ceiling(
        self, tmp_path: Path, ffmpeg_ready: None
    ) -> None:
        """没有停顿就别硬找：退回上限，并如实说"没找到"（调用方据此写说明）。"""
        path = _speechy(tmp_path, [(12.0, True)])
        point, at_pause = ffmpeg.cut_point(path, low=3.0, high=9.5)
        assert point == 9.5
        assert at_pause is False

    def test_the_answer_is_memoised_for_the_same_file(
        self, tmp_path: Path, ffmpeg_ready: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一条样例会被同一音色的每一行反复用到——不该每行都解码一遍。"""
        path = _speechy(tmp_path, [(2.0, True), (0.3, False)] * 5)
        first = ffmpeg.cut_point(path, low=3.0, high=9.5)

        calls: list[object] = []

        def boom(*args: object, **kwargs: object) -> list[tuple[float, float]]:
            calls.append(args)
            return []

        monkeypatch.setattr(ffmpeg, "_silence_spans", boom)
        assert ffmpeg.cut_point(path, low=3.0, high=9.5) == first
        assert calls == []
