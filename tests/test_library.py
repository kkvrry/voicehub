"""用例层：扫描合并、编辑先写 sidecar、主样例、平台音色、对接口。"""

import json
import time
from pathlib import Path

import pytest

from app.infra.sidecar import SIDECAR_NAME
from app.usecase.library import LibraryError


@pytest.fixture()
def scanned(service, demo_library: Path):
    summary = service.save_folders([str(demo_library)])
    return service, summary


class TestScan:
    def test_voice_folders_and_flat_files_are_all_found(self, scanned):
        service, summary = scanned
        assert summary["voices"] == 4  # 胡桃、钟离、阿米娅、散件
        names = {v.name for v in service.list_voices()}
        assert names == {"胡桃", "钟离", "阿米娅", "散件"}

    def test_category_is_the_parent_folder(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        assert hutao.category == "原神"
        loose = next(v for v in service.list_voices() if v.name == "散件")
        assert loose.category == ""

    def test_same_named_voices_in_two_roots_are_two_entries(self, service, tmp_path: Path):
        from conftest import make_wav

        for root_name in ("lib_a", "lib_b"):
            make_wav(tmp_path / root_name / "原神" / "胡桃" / "默认.wav")
        service.save_folders([str(tmp_path / "lib_a"), str(tmp_path / "lib_b")])
        hutao = [v for v in service.list_voices() if v.name == "胡桃"]
        assert len(hutao) == 2
        assert hutao[0].id != hutao[1].id

    def test_sidecar_txt_and_list_feed_sample_text(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        full = service.get_voice(hutao.id)
        text_of = {s.file: s.text for s in full.samples}
        assert text_of["开心.wav"] == "吃饱喝饱，一路走好！"  # 同名 txt
        zhongli = next(v for v in service.list_voices() if v.name == "钟离")
        zfull = service.get_voice(zhongli.id)
        assert zfull.samples[0].text == "欲买桂花同载酒"  # list 清单

    def test_main_sample_prefers_the_default_mark(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        assert hutao.main_sample == "默认.wav"

    def test_duration_is_probed_for_wav(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        full = service.get_voice(hutao.id)
        durations = {s.file: s.duration for s in full.samples}
        assert durations["开心.wav"] == pytest.approx(0.4, abs=0.05)

    def test_scanning_never_writes_into_the_library(self, scanned, demo_library: Path):
        sidecars = list(demo_library.rglob(SIDECAR_NAME))
        assert sidecars == []

    def test_a_second_scan_is_refused_while_one_is_running(self, scanned):
        """两次并行扫描会互相把对方刚写的行当成"已消失"——第二次直接 409。"""
        service, _ = scanned
        service._scan_lock.acquire()
        try:
            with pytest.raises(LibraryError) as caught:
                service.scan()
        finally:
            service._scan_lock.release()
        assert caught.value.status == 409
        assert caught.value.code == "scan_in_progress"


class TestEdit:
    def _hutao(self, service):
        return next(v for v in service.list_voices() if v.name == "胡桃")

    def test_update_writes_the_sidecar_first(self, scanned, demo_library: Path):
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_voice(hutao.id, {"gender": "female", "tags": ["活泼", "火系"]})
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["gender"] == "female" and raw["tags"] == ["活泼", "火系"]
        again = self._hutao(service)
        assert again.gender == "female"

    def test_rescan_keeps_sidecar_values(self, scanned):
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_voice(hutao.id, {"gender": "female", "description": "适合元气少女"})
        service.scan()
        again = self._hutao(service)
        assert again.gender == "female" and again.description == "适合元气少女"

    def test_sample_tags_land_in_the_sidecar(self, scanned, demo_library: Path):
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_sample(hutao.id, "难过.wav", {"tags": ["哭腔", "低声"]})
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["samples"]["难过.wav"]["tags"] == ["哭腔", "低声"]

    def test_sample_tags_survive_a_rescan(self, scanned):
        """标签要跟着**这一段音频**走：重扫一次还在（它是 sidecar 里的一行，不是内存里的）。"""
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_sample(hutao.id, "难过.wav", {"tags": ["哭腔"]})
        service.scan()
        full = service.get_voice(hutao.id)
        tags_of = {s.file: s.tags for s in full.samples}
        assert tags_of["难过.wav"] == ("哭腔",)
        # 别的样例没被顺手写上——一次只改一条。
        assert tags_of["开心.wav"] == ()

    def test_sample_text_edit_lands_in_the_sidecar(self, scanned, demo_library: Path):
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_sample(hutao.id, "难过.wav", {"text": "呜……", "emotion": "难过"})
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        entry = raw["samples"]["难过.wav"]
        assert entry["text"] == "呜……"
        assert entry["emotion"] == "难过"
        # 时长备份搭这一条一起写（它是重扫时的兜底，见 tests/test_scan_incremental.py）。
        assert entry["duration"] > 0
        # 字节数是那份备份的校验值：改文本不能把它丢了，否则这个文件下次要被重量。
        assert entry["bytes"] > 0

    def test_sidecar_beats_txt_after_an_edit(self, scanned):
        service, _ = scanned
        hutao = self._hutao(service)
        service.update_sample(hutao.id, "开心.wav", {"text": "改过的台词"})
        service.scan()
        full = service.get_voice(hutao.id)
        text_of = {s.file: s.text for s in full.samples}
        assert text_of["开心.wav"] == "改过的台词"

    def test_main_sample_round_trip(self, scanned):
        service, _ = scanned
        hutao = self._hutao(service)
        service.set_main_sample(hutao.id, "开心.wav")
        assert self._hutao(service).main_sample == "开心.wav"
        service.set_main_sample(hutao.id, "")
        assert self._hutao(service).main_sample == "默认.wav"

    def test_prefs_go_into_the_sidecar_too(self, scanned, demo_library: Path):
        """偏好写库**也写目录里的 sidecar**：搬家后 id 变了，靠它才找得回来。"""
        service, _ = scanned
        hutao = self._hutao(service)
        service.set_prefs(hutao.id, favorite=True, hidden=True)
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["favorite"] is True and raw["hidden"] is True
        again = self._hutao(service)
        assert again.favorite and again.hidden

    def test_prefs_survive_a_rescan(self, scanned):
        service, _ = scanned
        hutao = self._hutao(service)
        service.set_prefs(hutao.id, favorite=True)
        service.scan()
        assert self._hutao(service).favorite


class TestRemoveSample:
    """移除一段样例：文件走回收站，清单与 sidecar 同时少一条。

    回收站是**系统的**：用例里换成"直接删掉那个文件"，验的是"文件真的离开了原位"
    （换成人手点会进回收站，可还原），不在测试机上往回收站塞东西。
    """

    def _hutao(self, service):
        return next(v for v in service.list_voices() if v.name == "胡桃")

    def test_file_leaves_disk_and_the_sidecar_row_goes_with_it(
        self, scanned, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import app.usecase.library as library_module

        service, _ = scanned
        hutao = self._hutao(service)
        # 两条都先改过：这样 sidecar 里两条都在，删掉一条之后能验"别的那条没被顺手抹掉"。
        service.update_sample(hutao.id, "难过.wav", {"text": "呜……", "tags": ["哭腔"]})
        service.update_sample(hutao.id, "开心.wav", {"text": "吃饱喝饱，一路走好！"})
        target = demo_library / "原神" / "胡桃" / "难过.wav"
        trashed: list[str] = []

        def fake_trash(path: Path) -> None:
            trashed.append(str(path))
            Path(path).unlink()

        monkeypatch.setattr(library_module, "move_to_trash", fake_trash)

        full = service.delete_sample(hutao.id, "难过.wav")

        assert trashed == [str(target)], "文件要走那条可还原的路，不是只改库里那行"
        assert not target.exists()
        assert "难过.wav" not in {s.file for s in full.samples}
        raw = json.loads((target.parent / SIDECAR_NAME).read_text(encoding="utf-8"))
        assert "难过.wav" not in raw.get("samples", {})
        assert raw["samples"]["开心.wav"]["text"] == "吃饱喝饱，一路走好！", "别的那条不该被顺手抹掉"
        # 重扫不会把它认回来（只摘记录那一路会）。
        service.scan()
        assert "难过.wav" not in {s.file for s in service.get_voice(hutao.id).samples}

    def test_removing_the_main_sample_moves_the_mark(
        self, scanned, demo_library: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import app.usecase.library as library_module

        service, _ = scanned
        hutao = self._hutao(service)
        service.set_main_sample(hutao.id, "开心.wav")
        monkeypatch.setattr(library_module, "move_to_trash", lambda path: Path(path).unlink())

        service.delete_sample(hutao.id, "开心.wav")

        again = self._hutao(service)
        # 用户指定那条没了 → 按"默认标记 → 升序第一条"重新落一条，
        # sidecar 里也不许留一个指向空文件的名字。
        assert again.main_sample == "默认.wav"
        raw = json.loads(
            (demo_library / "原神" / "胡桃" / SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert raw["main_sample"] == "默认.wav"

    def test_unknown_sample_is_a_readable_404(self, scanned):
        from app.usecase.library import LibraryError

        service, _ = scanned
        hutao = self._hutao(service)
        with pytest.raises(LibraryError) as err:
            service.delete_sample(hutao.id, "没有这段.wav")
        assert err.value.code == "sample_not_found"
        assert err.value.status == 404

    def test_trash_error_but_the_file_is_gone_still_finishes(
        self, scanned, monkeypatch: pytest.MonkeyPatch
    ):
        """**文件真的走了、回收站却回一个错误码**时照样收尾。

        沙箱里实测过这种：``SHFileOperationW`` 回 2（找不到文件），而文件确实已经
        进了回收站。这时若照错误码退出，库里就留下"有记录、没文件"的残样——
        点开详情是一条播不了的音频。
        """
        import app.usecase.library as library_module
        from app.infra.trash import TrashError

        service, _ = scanned
        hutao = self._hutao(service)

        def fake_trash(path: Path) -> None:
            Path(path).unlink()
            raise TrashError("移入回收站失败（系统返回 2），文件未变更，库中记录保留。")

        monkeypatch.setattr(library_module, "move_to_trash", fake_trash)

        full = service.delete_sample(hutao.id, "难过.wav")
        assert "难过.wav" not in {s.file for s in full.samples}

    def test_trash_error_with_the_file_still_there_keeps_the_record(
        self, scanned, monkeypatch: pytest.MonkeyPatch
    ):
        """文件还在 = 什么都没动，记录也不该变——这条是上面那条的对照组。"""
        import app.usecase.library as library_module
        from app.infra.trash import TrashError
        from app.usecase.library import LibraryError

        service, _ = scanned
        hutao = self._hutao(service)
        monkeypatch.setattr(
            library_module, "move_to_trash", lambda path: (_ for _ in ()).throw(TrashError("磁盘忙"))
        )

        with pytest.raises(LibraryError) as err:
            service.delete_sample(hutao.id, "难过.wav")
        assert "磁盘忙" in str(err.value)
        assert "难过.wav" in {s.file for s in service.get_voice(hutao.id).samples}


class TestPlatformVoices:
    def test_register_and_update(self, service):
        from app.engines.base import VoiceMeta

        voice = service.add_platform_voice(
            "minimax", VoiceMeta(id="MX_001", name="林晚", category="女声", tags=("温柔",))
        )
        assert voice.id == "minimax:MX_001"
        assert service.get_voice("minimax:MX_001").shown_name == "林晚"
        service.update_voice("minimax:MX_001", {"gender": "female"})
        assert service.get_voice("minimax:MX_001").gender == "female"

    def test_local_voices_cannot_be_deleted_through_the_api(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        with pytest.raises(Exception, match="本地音色"):
            service.delete_platform_voice(hutao.id)

    def test_clearing_a_source_removes_only_its_entries(self, service):
        from app.engines.base import VoiceMeta

        service.add_platform_voice("fish_audio", VoiceMeta(id="f1", name="甲"))
        service.add_platform_voice("fish_audio", VoiceMeta(id="f2", name="乙"))
        service.add_platform_voice("minimax", VoiceMeta(id="m1", name="丙"))

        assert service.clear_source_voices("fish_audio") == 2
        assert service.list_voices(source="fish_audio") == []
        assert [v.source for v in service.list_voices()] == ["minimax"]

    def test_local_voices_cannot_be_cleared_by_source(self, scanned):
        service, _ = scanned
        with pytest.raises(LibraryError, match="平台来源"):
            service.clear_source_voices("local")


class TestReference:
    def test_reference_gives_path_text_emotion_in_one_call(self, scanned):
        service, _ = scanned
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        ref = service.reference_of(hutao.id)
        assert ref["sample_path"].endswith("默认.wav")
        assert ref["voice"] == "胡桃"
        service.set_main_sample(hutao.id, "开心.wav")
        ref = service.reference_of(hutao.id)
        assert ref["sample_path"].endswith("开心.wav")
        assert ref["text"] == "吃饱喝饱，一路走好！"


class TestGeneratedFilesInsideTheLibrary:
    """生成结果目录指进音色库时，引擎产物仍然是平台音色的样例，不是本地音色。"""

    def test_engine_folders_inside_a_scan_root_are_not_scanned_as_local_voices(
        self, service, data_dir: Path, tmp_path: Path
    ) -> None:
        from conftest import make_wav

        from app.engines.registry import list_engines
        from app.infra.config import load_config, save_config

        engine = list_engines()["minimax"]
        root = tmp_path / "library"
        make_wav(root / engine.label / "林晚" / "默认.wav")

        # 把「生成结果目录」指到扫描根：引擎保存目录 = 扫描根/<引擎 label>。
        config = load_config()
        config.output_dir = str(root)
        save_config(config)

        summary = service.save_folders([str(root)])
        assert summary["voices"] == 0
        assert service.list_voices() == []

    def test_a_sibling_folder_is_still_a_local_voice(
        self, service, data_dir: Path, tmp_path: Path
    ) -> None:
        from conftest import make_wav

        from app.engines.registry import list_engines
        from app.infra.config import load_config, save_config

        engine = list_engines()["minimax"]
        root = tmp_path / "library"
        make_wav(root / "原神" / "胡桃" / "默认.wav")
        make_wav(root / engine.label / "林晚" / "默认.wav")

        config = load_config()
        config.output_dir = str(root)
        save_config(config)

        summary = service.save_folders([str(root)])
        assert summary["voices"] == 1
        assert [voice.name for voice in service.list_voices()] == ["胡桃"]

    def test_changing_the_global_output_dir_can_move_what_was_generated(
        self, client, data_dir: Path
    ) -> None:
        old = data_dir / "generated" / "MiniMax" / "林晚"
        old.mkdir(parents=True)
        (old / "x.wav").write_bytes(b"RIFF-cache")
        target = data_dir / "out2"

        preview = client.post("/api/v1/output/preview", json={"dir": str(target)}).json()
        assert preview["needed"] is True
        assert preview["entries"] == 1

        body = client.put(
            "/api/v1/output", json={"dir": str(target), "migrate": True}
        ).json()
        assert body["moved"]["moved"] == 1
        assert (target / "MiniMax" / "林晚" / "x.wav").is_file()
        assert not (old / "x.wav").exists()


@pytest.fixture()
def generated_output(data_dir: Path, tmp_path: Path):
    """把全局生成目录指到 ``tmp``，并在里面放一条 MiniMax 的产物（林晚）。"""
    from conftest import make_wav

    from app.engines.registry import list_engines
    from app.infra.config import load_config, save_config

    engine = list_engines()["minimax"]
    root = tmp_path / "output"
    make_wav(root / engine.label / "林晚" / "默认.wav", seconds=0.3)
    config = load_config()
    config.output_dir = str(root)
    save_config(config)
    return engine, root


@pytest.fixture()
def scan_counter(monkeypatch):
    """数一数真的扫了几次磁盘（命中缓存就不该再扫）。"""
    from app.usecase.library import LibraryService

    scans: list[Path] = []
    original = LibraryService._scan_generated

    def counting(path: Path):
        scans.append(path)
        return original(path)

    monkeypatch.setattr(LibraryService, "_scan_generated", staticmethod(counting))
    return scans


class TestGeneratedIndexCache:
    """产物索引按「根目录 mtime + TTL」缓存。

    列表接口每次都要知道"引擎保存目录里有哪些产物"，原来每次都 iterdir 全目录 +
    逐文件夹读 sidecar；规模上来后这是主要成本。缓存必须能**立刻**反映新产物，
    所以写盘后主动失效，新目录也会改根 mtime 立刻重建。
    """

    def test_the_second_list_reuses_the_index(
        self, service, demo_library: Path, generated_output, scan_counter
    ):
        service.save_folders([str(demo_library)])  # 库里有本地音色，列表才会去挂引擎产物
        service.list_voices()
        first = len(scan_counter)
        assert first >= 1
        service.list_voices()
        service.list_voices()
        assert len(scan_counter) == first  # 后两次全部命中缓存

    def test_invalidation_forces_a_rescan(
        self, service, demo_library: Path, generated_output, scan_counter
    ):
        service.save_folders([str(demo_library)])
        service.list_voices()
        before = len(scan_counter)
        service.invalidate_generated()
        service.list_voices()
        assert len(scan_counter) == before + 1

    def test_a_new_output_folder_is_visible_on_the_next_list(self, service, generated_output):
        """新目录会改根 mtime——不必等 TTL 过期，下一次列表就该看到。"""
        from conftest import make_wav

        engine, root = generated_output
        assert "林晚" in service._generated_index(engine.key).by_name
        # Windows 的目录 mtime 分辨率可能比连续两次创建更粗；给文件系统一个刻度。
        time.sleep(0.02)
        make_wav(root / engine.label / "顾青" / "默认.wav", seconds=0.3)
        assert "顾青" in service._generated_index(engine.key).by_name


class TestPlatformSamplesInsideTheLibrary:
    """平台音色的样例就躺在扫描根里（生成时落进那个文件夹）。

    那 16 个文件夹的名字是平台音色 id（``Chinese (Mandarin)_Gentle_Senior``），
    而平台音色自己有显示名（``温柔学姐``）。所以文件夹不再单独列：按平台音色出现，
    样例、主样例、封面都还是同一个文件夹里的东西——「生成样例」也写回那里。
    """

    @pytest.fixture()
    def linked(self, service, tmp_path: Path):
        from conftest import make_wav

        from app.engines.base import VoiceMeta
        from app.infra.sidecar import Sidecar, write_sidecar

        root = tmp_path / "library"
        folder = root / "MiniMax" / "Chinese (Mandarin)_Gentle_Senior"
        make_wav(folder / "20261031-201440-01.wav", seconds=0.3)
        write_sidecar(
            folder,
            Sidecar(
                origin={
                    "voice_id": "minimax:Chinese (Mandarin)_Gentle_Senior",
                    "engine": "minimax",
                },
                samples={"20261031-201440-01.wav": {"text": "你说的对"}},
            ),
        )
        voice = service.add_platform_voice(
            "minimax", VoiceMeta(id="Chinese (Mandarin)_Gentle_Senior", name="温柔学姐")
        )
        service.save_folders([str(root)])
        return service, root, folder, voice

    def test_the_folder_is_not_a_local_voice_of_its_own(self, linked):
        service, *_ = linked
        listed = service.list_voices()
        assert [voice.id for voice in listed] == ["minimax:Chinese (Mandarin)_Gentle_Senior"]
        # 卡片上写平台给的显示名，不是文件夹名（= 音色 id）。
        assert listed[0].shown_name == "温柔学姐"

    def test_the_platform_voice_owns_the_samples_in_that_folder(self, linked):
        service, _root, folder, voice = linked
        full = service.get_voice(voice.id)
        assert full.folder == str(folder)
        assert [sample.file for sample in full.samples] == ["20261031-201440-01.wav"]
        assert full.samples[0].text == "你说的对"
        assert full.main_sample == "20261031-201440-01.wav"

    def test_the_scan_root_stops_counting_it(self, linked):
        service, *_ = linked
        tree = service.tree()
        assert tree["roots"][0]["total"] == 0
        assert tree["total"] == 1  # 只剩那条平台音色

    def test_removing_the_platform_voice_gives_the_folder_back(self, linked):
        service, _root, _folder, voice = linked
        service.delete_platform_voice(voice.id)
        listed = service.list_voices()
        assert [item.name for item in listed] == ["Chinese (Mandarin)_Gentle_Senior"]
        assert listed[0].sample_count == 1

    def test_new_samples_are_written_back_into_that_folder(self, linked):
        from conftest import make_wav

        from app.infra import archive

        service, _root, folder, voice = linked
        full = service.get_voice(voice.id)
        target = service.sample_folder(full)
        assert target == folder

        fresh = folder / "tmp.wav"
        make_wav(fresh, seconds=0.3)
        archive.write_audio(
            "minimax",
            "MiniMax",
            full.name,
            fresh.read_bytes(),
            fmt="wav",
            voice_id=voice.id,
            text="新的一段",
            folder=target,
        )
        fresh.unlink()
        # 不重扫：样例目录就是事实，下一次读详情就该看到两段。
        again = service.get_voice(voice.id)
        assert len(again.samples) == 2


class TestPlatformSamplesApi:
    """接口那一侧：卡片有 ``main_audio_url``，点开就能播。"""

    def test_the_card_points_at_a_playable_url(self, client, library, tmp_path: Path):
        from conftest import make_wav

        from app.engines.base import VoiceMeta
        from app.infra.sidecar import Sidecar, write_sidecar

        root = tmp_path / "library"
        folder = root / "MiMo" / "Chloe"
        make_wav(folder / "20261017-180614-01.wav", seconds=0.3)
        write_sidecar(
            folder,
            Sidecar(origin={"voice_id": "mimo:Chloe", "engine": "mimo"}),
        )
        library.add_platform_voice("mimo", VoiceMeta(id="Chloe", name="Chloe"))
        library.save_folders([str(root)])

        body = client.get("/api/v1/voices", params={"source": "mimo"}).json()
        card = body["data"][0]
        assert card["sample_count"] == 1
        assert card["main_audio_url"]

        audio = client.get(card["main_audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"].startswith("audio/")

        detail = client.get(f"/api/v1/voices/{card['id']}").json()
        assert detail["folder"] == str(folder)
        assert detail["samples"][0]["file"] == "20261017-180614-01.wav"
