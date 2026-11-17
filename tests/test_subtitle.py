"""字幕项目：SRT 导入 + 时间轴拼装。

守三件事：

1. **解析**：毫秒精度、样式标签剥掉、多行合并成一行、说话人标记（只出现一次的
   冒号前缀当正文）、坏块跳过不报错；
2. **导入**：单章项目、一行一条带时间码、阶段直接到「拆句完成」、原始文件另存；
3. **拼装**：按原时间码补静音；音频比时隙长时**顺延**并报出来；字幕按实际时间轴
   写且**不切 cue**。

书稿项目那条路一行没动——那一批老测试（`tests/test_producers.py`）继续守它。
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE = """1
00:00:00,000 --> 00:00:00,800
张三：第一句。

2
00:00:00,800 --> 00:00:01,600
张三：第二句。

3
00:00:01,600 --> 00:00:03,000
李四：第三句，长一点。

4
00:00:03,000 --> 00:00:04,000
李四：第四句。
"""


def _srt_bytes(text: str = SAMPLE) -> bytes:
    return text.encode("utf-8-sig")


def _tone(path: Path, seconds: float = 0.2, rate: int = 22050) -> None:
    """440Hz 的 wav：ffmpeg 能识别即可，内容不重要（与 test_producers 同一做法）。"""
    if not rate:
        rate = 22050
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )


class TestParse:
    def test_millisecond_precision_is_kept(self):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        cues, skipped = parse_srt("1\n00:00:08,608 --> 00:00:12,345\n你好\n")
        assert (cues[0].start_ms, cues[0].end_ms, skipped) == (8608, 12345, 0)

    def test_tags_are_stripped_and_lines_joined(self):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        cues, _ = parse_srt("1\n00:00:01,000 --> 00:00:02,000\n<i>第一行</i>\n{\\an8}第二行\n")
        assert cues[0].text == "第一行 第二行"

    def test_speaker_marker_needs_to_repeat(self):
        """`张三：` 出现了两次 → 认成说话人；只出现一次的前缀（`他说：`）当正文。"""
        from app.producers.builtin.audiobook.subtitles import parse_srt

        cues, _ = parse_srt(SAMPLE)
        assert [cue.speaker for cue in cues] == ["张三", "张三", "李四", "李四"]
        assert cues[0].text == "第一句。"

        single, _ = parse_srt("1\n00:00:01,000 --> 00:00:02,000\n他说：别动。\n")
        assert single[0].speaker == ""
        assert single[0].text == "他说：别动。"

    def test_broken_block_is_skipped(self):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        text = "1\n没有时间行\n正文\n\n2\n00:00:01,000 --> 00:00:02,000\n好的\n"
        cues, skipped = parse_srt(text)
        assert [cue.text for cue in cues] == ["好的"]
        assert skipped == 1

    def test_semicolon_millis_and_short_forms(self):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        cues, _ = parse_srt("1\n00:00:01;500 --> 00:00:02;000\n好\n")
        assert (cues[0].start_ms, cues[0].end_ms) == (1500, 2000)
        cues, _ = parse_srt("1\n00:01,5 --> 00:02\n短\n")
        assert (cues[0].start_ms, cues[0].end_ms) == (1500, 2000)

    def test_end_before_start_is_clamped(self):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        cues, _ = parse_srt("1\n00:00:02,000 --> 00:00:01,000\n倒的\n")
        assert (cues[0].start_ms, cues[0].end_ms) == (2000, 2000)


class TestImport:
    def test_srt_import_builds_single_chapter_project(self, client):
        response = client.post(
            "/api/v1/producers/audiobook/projects/import",
            files={"file": ("样片.srt", _srt_bytes(), "text/plain")},
        )
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["kind"] == "subtitle"
        assert detail["stats"]["chapters"] == 1
        assert detail["chapters"][0]["title"] == "全片"
        # 行已经在库里：这一步等于拆句完成，下一步是分析/配声
        assert detail["chapters"][0]["stage"] == "sentences"

        chapter = detail["chapters"][0]
        rows = client.get(
            f"/api/v1/producers/audiobook/projects/{detail['id']}/chapters/{chapter['id']}"
        ).json()["rows"]
        assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
            (0, 800),
            (800, 1600),
            (1600, 3000),
            (3000, 4000),
        ]
        assert [row["speaker"] for row in rows] == ["张三", "张三", "李四", "李四"]
        assert all(row["kind"] == "dialogue" for row in rows)

        folder = Path(detail["folder"])
        assert (folder / "source" / "样片.srt").is_file(), "原始字幕要留一份"

    def test_unknown_subtitle_format_is_a_readable_400(self, client):
        response = client.post(
            "/api/v1/producers/audiobook/projects/import",
            files={"file": ("样片.vtt", b"WEBVTT", "text/plain")},
        )
        assert response.status_code == 400
        assert "不支持" in response.json()["error"]["message"]


class TestConcatBatching:
    def test_more_than_one_batch_keeps_the_timeline(self, tmp_path: Path):
        """超过一批（60 段）走分批：批产物同一档，拼出来时长仍然对得上。

        分批那条路是最容易藏 bug 的地方（批内滤镜、批间解复用器），而正常章节
        动辄几百行——所以直接拿 61 段喂 `_concat`。
        """
        from app.producers.builtin.audiobook.pipeline import AudiobookService

        service = AudiobookService(tmp_path)
        folder = tmp_path / "merged"
        folder.mkdir(parents=True, exist_ok=True)
        segments = []
        for index in range(61):
            path = tmp_path / "seg" / f"{index:03d}.wav"
            # 采样率交替：验证分批时也做了重采样
            _tone(path, 0.1, rate=22050 if index % 2 else 24000)
            segments.append(path)

        output = folder / "batched.mp3"
        service.output._concat(
            service.output._ffmpeg(), "mp3", "192k", segments, output, folder, metadata={"title": "分批"}
        )

        from app.infra.audioprobe import find_ffprobe, probe_duration

        assert abs(probe_duration(output, ffprobe_path=find_ffprobe()) - 6.1) < 0.2
        assert not list(folder.glob("part-*.wav")), "分批的中间产物要清掉"


class TestTimelineMerge:
    def test_pads_silence_and_reports_delayed_rows(self, tmp_path: Path):
        from app.producers.builtin.audiobook.pipeline import AudiobookService

        service = AudiobookService(tmp_path)
        project = service.import_book("样片.srt", _srt_bytes())
        chapter = service.store.chapters(project["id"])[0]
        rows = service.store.rows(chapter["id"])
        # 第一条 0.5s（时隙 0.8s，够）；第二条 1.2s（时隙 0.8s，必定挤到第三条）；
        # 第三条 0.4s；第四条 0.3s。
        for row, seconds in zip(rows, (0.5, 1.2, 0.4, 0.3), strict=True):
            path = tmp_path / "audio" / f"{row['id']}.wav"
            _tone(path, seconds)
            service.store.set_row_audio(row["id"], str(path))

        result = service.merge(project["id"], chapter["id"])
        # 第二条超时隙，把第三条从 1.6s 挤到 2.0s
        assert result["delayed"] == [3], result

        raw = Path(result["subtitles"]["srt"]).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 会让部分播放器跳过首条字幕"
        srt = raw.decode("utf-8")
        assert "00:00:00,000 --> 00:00:00,500" in srt
        assert "00:00:00,800 --> 00:00:02,000" in srt
        assert "00:00:02,000 --> 00:00:02,400" in srt
        assert "00:00:03,000 --> 00:00:03,300" in srt

        # 成品时长 = 时间轴终点（3.3s），不是「四条音频 + 固定间歇」
        from app.infra.audioprobe import find_ffprobe, probe_duration

        assert abs(probe_duration(Path(result["audio"]), ffprobe_path=find_ffprobe()) - 3.3) < 0.15

    def test_book_project_keeps_the_old_merge_path(self, tmp_path: Path):
        """书稿项目没有时间码：同样的调用来一遍，delayed 必为空、字幕按顺序累加。"""
        from app.producers.builtin.audiobook.pipeline import AudiobookService

        service = AudiobookService(tmp_path)
        project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])
        chapter = service.store.chapters(project["id"])[0]
        for row in service.store.rows(chapter["id"]):
            path = tmp_path / "audio" / f"{row['id']}.wav"
            _tone(path, 0.3)
            service.store.set_row_audio(row["id"], str(path))

        rows = service.store.rows(chapter["id"])
        result = service.merge(project["id"], chapter["id"])
        assert result["delayed"] == []
        assert (service.store.project(project["id"]) or {}).get("kind") == "book"
        for suffix in ("srt", "lrc"):
            assert not Path(result["subtitles"][suffix]).read_bytes().startswith(b"\xef\xbb\xbf")

        # 时长 = 章首静音 + 各行 + 行间静音 + 章尾静音。这条从前没人钉，
        # 混采样率（24 kHz 行 + 44.1 kHz 静音）把时长拼错也没红过。
        from app.infra.audioprobe import find_ffprobe, probe_duration

        settings = service.settings
        expected = (
            int(settings.chapter_lead_ms)
            + int(settings.chapter_tail_ms)
            + int(settings.row_gap_ms) * (len(rows) - 1)
            + 300 * len(rows)
        ) / 1000
        actual = probe_duration(Path(result["audio"]), ffprobe_path=find_ffprobe())
        assert abs(actual - expected) < 0.2, (actual, expected)


class TestFitToSlot:
    """「变速贴齐」是**开关**，不是默认行为。

    三条边界值钱：贴得进去才变（上限够不着就整条不动）、贴齐后字幕回到原时间码、
    改设置重合并时旧的变速产物不积在目录里。
    """

    @staticmethod
    def _service(root: Path, **overrides):
        from app.producers.builtin.audiobook.pipeline import AudiobookService
        from app.producers.builtin.audiobook.settings import AudiobookSettings

        return AudiobookService(root, lambda: AudiobookSettings(**overrides))

    @staticmethod
    def _prepare(service, root: Path):
        """四条字幕、时隙 0.8 / 0.8 / 1.4 / 1.0 秒；第二条音频 1.2 秒，必定超时隙。"""
        project = service.import_book("样片.srt", _srt_bytes())
        chapter = service.store.chapters(project["id"])[0]
        rows = service.store.rows(chapter["id"])
        for row, seconds in zip(rows, (0.5, 1.2, 0.4, 0.3), strict=True):
            path = root / "audio" / f"{row['id']}.wav"
            _tone(path, seconds)
            service.store.set_row_audio(row["id"], str(path))
        return project, chapter

    def test_speeding_up_fits_a_line_back_into_its_slot(self, tmp_path: Path):
        from app.producers.builtin.audiobook.subtitles import parse_srt

        service = self._service(tmp_path, subtitle_fit=True, subtitle_fit_max=1.6)
        project, chapter = self._prepare(service, tmp_path)
        result = service.merge(project["id"], chapter["id"])

        # 1.2 秒压到 1.5 倍 = 0.8 秒，正好塞回 0.8–1.6 的时隙。
        assert result["fitted"] == [2], result
        assert result["delayed"] == [], "贴齐之后不该再顺延"
        cues, _ = parse_srt(Path(result["subtitles"]["srt"]).read_text(encoding="utf-8-sig"))
        assert cues[1].start_ms == 800
        assert abs(cues[1].end_ms - 1600) <= 40, cues[1]
        assert abs(cues[2].start_ms - 1600) <= 40, "第三条回到原时间码"

        from app.infra.audioprobe import find_ffprobe, probe_duration

        # 贴齐不改变总时长（它就是被顺延撑长的）
        actual = probe_duration(Path(result["audio"]), ffprobe_path=find_ffprobe())
        assert abs(actual - (cues[-1].end_ms / 1000)) < 0.2

    def test_speed_cap_leaves_the_line_untouched(self, tmp_path: Path):
        """上限够不着就不变：白白改语气、字幕照样错位，不值这一刀。"""
        service = self._service(tmp_path, subtitle_fit=True, subtitle_fit_max=1.3)
        project, chapter = self._prepare(service, tmp_path)
        result = service.merge(project["id"], chapter["id"])

        assert result["fitted"] == []
        assert result["delayed"] == [3]
        assert not list(Path(result["audio"]).parent.glob("fit-*.wav"))

    def test_remerge_reuses_and_then_drops_the_speed_files(self, tmp_path: Path):
        from app.producers.builtin.audiobook.pipeline import AudiobookService
        from app.producers.builtin.audiobook.settings import AudiobookSettings

        # 设置工厂每次现读：改这份 dict 就等于在设置页改完再合并。
        values = {"subtitle_fit": True, "subtitle_fit_max": 1.6}
        service = AudiobookService(tmp_path, lambda: AudiobookSettings(**values))
        project, chapter = self._prepare(service, tmp_path)

        result = service.merge(project["id"], chapter["id"])
        merged = Path(result["audio"]).parent
        produced = list(merged.glob("fit-*.wav"))
        assert len(produced) == 1 and "-1.500-" in produced[0].stem, produced

        # 同一倍率重跑：复用那份，不重新编码（mtime 不变）。
        before = produced[0].stat().st_mtime_ns
        service.merge(project["id"], chapter["id"])
        assert produced[0].stat().st_mtime_ns == before

        # 倍率调小到贴不进去：这次不再需要变速，旧产物要清掉，不能越积越多。
        values["subtitle_fit_max"] = 1.3
        result = service.merge(project["id"], chapter["id"])
        assert result["fitted"] == []
        assert not list(Path(result["audio"]).parent.glob("fit-*.wav"))

    def test_book_project_ignores_the_fit_switch(self, tmp_path: Path):
        service = self._service(tmp_path, subtitle_fit=True, subtitle_fit_max=2.0)
        project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])
        chapter = service.store.chapters(project["id"])[0]
        for row in service.store.rows(chapter["id"]):
            path = tmp_path / "audio" / f"{row['id']}.wav"
            _tone(path, 0.3)
            service.store.set_row_audio(row["id"], str(path))

        result = service.merge(project["id"], chapter["id"])
        assert result["fitted"] == []
        assert result["delayed"] == []
        assert not list(Path(result["audio"]).parent.glob("fit-*.wav"))
