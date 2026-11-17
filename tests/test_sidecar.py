"""sidecar：缺省不建文件、全缺省删文件、坏文件回落。"""

import json
from pathlib import Path

from app.infra.sidecar import SIDECAR_NAME, Sidecar, read_sidecar, write_sidecar


class TestRead:
    def test_missing_file_gives_all_defaults(self, tmp_path: Path):
        side = read_sidecar(tmp_path)
        assert side.gender == "" and side.samples == {} and side.main_sample == ""

    def test_a_broken_file_falls_back_to_defaults(self, tmp_path: Path):
        (tmp_path / SIDECAR_NAME).write_text("{not json", encoding="utf-8")
        assert read_sidecar(tmp_path) == Sidecar()


class TestWrite:
    def test_all_defaults_removes_the_file(self, tmp_path: Path):
        path = tmp_path / SIDECAR_NAME
        path.write_text("{}", encoding="utf-8")
        write_sidecar(tmp_path, Sidecar())
        assert not path.exists()

    def test_only_non_default_fields_are_written(self, tmp_path: Path):
        write_sidecar(tmp_path, Sidecar(gender="female", tags=["活泼"]))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw == {"version": 1, "gender": "female", "tags": ["活泼"]}

    def test_empty_sample_entries_are_dropped(self, tmp_path: Path):
        write_sidecar(
            tmp_path,
            Sidecar(samples={"a.wav": {"text": "", "emotion": ""}, "b.wav": {"text": "x"}}),
        )
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"] == {"b.wav": {"text": "x"}}

    def test_round_trip(self, tmp_path: Path):
        side = Sidecar(
            gender="female",
            main_sample="默认.wav",
            samples={"默认.wav": {"text": "你好", "emotion": "默认"}},
        )
        write_sidecar(tmp_path, side)
        assert read_sidecar(tmp_path) == side

    def test_sample_tags_round_trip(self, tmp_path: Path):
        side = Sidecar(samples={"a.wav": {"text": "你好", "tags": ["笑声"]}})
        write_sidecar(tmp_path, side)
        assert read_sidecar(tmp_path).samples["a.wav"]["tags"] == ["笑声"]

    def test_sample_without_tags_does_not_get_an_empty_key(self, tmp_path: Path):
        """空列表与"从来没设过"在文件里不该有区别——所以**没有就不写这个键**。"""
        write_sidecar(tmp_path, Sidecar(samples={"a.wav": {"text": "你好"}}))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"] == {"a.wav": {"text": "你好"}}

    def test_sample_params_round_trip(self, tmp_path: Path):
        """生成这一段时用的参数（"当时到底发了什么"）——**保序**地存、**保序**地读。"""
        side = Sidecar(
            samples={
                "a.mp3": {"text": "你好", "params": {"speed": "1.2", "pitch": "1.3"}},
            }
        )
        write_sidecar(tmp_path, side)
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"]["a.mp3"]["params"] == {"speed": "1.2", "pitch": "1.3"}
        assert read_sidecar(tmp_path).samples["a.mp3"]["params"] == {"speed": "1.2", "pitch": "1.3"}

    def test_empty_params_do_not_get_a_key(self, tmp_path: Path):
        write_sidecar(tmp_path, Sidecar(samples={"a.mp3": {"text": "你好", "params": {}}}))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"] == {"a.mp3": {"text": "你好"}}

    def test_a_nested_params_value_is_not_swallowed_as_a_dict(self, tmp_path: Path):
        """只认一层平表：嵌套的东西收进来会让"这一格显示成什么"变成猜谜。"""
        (tmp_path / SIDECAR_NAME).write_text(
            json.dumps(
                {"version": 1, "samples": {"a.mp3": {"params": {"speed": {"x": 1}, "ok": "1.0"}}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        params = read_sidecar(tmp_path).samples["a.mp3"]["params"]
        assert params == {"speed": "{'x': 1}", "ok": "1.0"}


class TestSampleDuration:
    """样例时长在 sidecar 里只是**备份**：删了库重扫时不必再跑一遍 ffprobe。"""

    def test_round_trip(self, tmp_path: Path):
        write_sidecar(tmp_path, Sidecar(samples={"a.mp3": {"duration": 8.5712}}))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"]["a.mp3"]["duration"] == 8.571  # 三位小数，不留尾巴
        assert read_sidecar(tmp_path).samples["a.mp3"]["duration"] == 8.571

    def test_zero_does_not_get_a_key(self, tmp_path: Path):
        """0 秒是"没量过"，不是事实——留个键会让读的人以为这条真的这么短。"""
        write_sidecar(tmp_path, Sidecar(samples={"a.mp3": {"text": "你好", "duration": 0.0}}))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["samples"] == {"a.mp3": {"text": "你好"}}

    def test_a_bad_value_reads_as_unknown(self, tmp_path: Path):
        (tmp_path / SIDECAR_NAME).write_text(
            json.dumps({"version": 1, "samples": {"a.mp3": {"duration": "很久"}}}),
            encoding="utf-8",
        )
        assert "duration" not in read_sidecar(tmp_path).samples["a.mp3"]

    def test_bytes_ride_with_the_duration(self, tmp_path: Path):
        """字节数是时长备份的**校验**：扫描器靠它判"还是不是同一段音频"。"""
        write_sidecar(
            tmp_path, Sidecar(samples={"a.mp3": {"duration": 8.5712, "bytes": 48321}})
        )
        entry = read_sidecar(tmp_path).samples["a.mp3"]
        assert entry["duration"] == 8.571
        assert entry["bytes"] == 48321


class TestPrefs:
    """收藏 / 隐藏写进 sidecar：文件夹改名搬家后 id 会变，只有它留得住。"""

    def test_round_trip(self, tmp_path: Path):
        write_sidecar(tmp_path, Sidecar(favorite=True, hidden=True))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["favorite"] is True and raw["hidden"] is True
        side = read_sidecar(tmp_path)
        assert side.favorite is True and side.hidden is True

    def test_absent_means_never_set(self, tmp_path: Path):
        """没有键 ≠ False：没见过这个键时，库里现有的偏好不能被"默认值"顶掉。"""
        write_sidecar(tmp_path, Sidecar(display="胡桃"))
        assert read_sidecar(tmp_path).favorite is None
        assert read_sidecar(tmp_path).hidden is None

    def test_false_is_written_when_explicitly_cleared(self, tmp_path: Path):
        """取消收藏要留一个 false——不然换个位置重扫，旧的那份 true 又会被捡回来。"""
        write_sidecar(tmp_path, Sidecar(favorite=False))
        raw = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert raw["favorite"] is False
