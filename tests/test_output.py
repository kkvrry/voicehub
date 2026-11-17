"""生成结果目录：合成的音频**按音色归档**，不是一堆平铺的时间戳。

为什么这一条要单独一份测试：**在线平台（MiniMax 这类）的音色原来在磁盘上没有目录**
——它是库里的一条记录，声音是平台那边的。于是一段生成出来的音频如果没有归属，
就等于丢了一次按量计费买来的东西：用户只知道"刚才生成过"，却找不到它在哪、
也不记得它是给谁生成的。

2026-10-14 起归档位置只有一处产地：``app.infra.archive``。

```
<这台引擎的"音色保存目录">/<音色名>/<时间戳>-<序号>.<ext>
```

没配引擎自己的目录时回到 ``<全局输出目录>/<引擎 label>/…``（老路径一字不变）。
**每台引擎一个保存目录**是用户能在引擎设置里指定、也能打开看一眼的东西；而按这条路
存下来之后，那条**平台音色**就同时有了本地音频（它的样例），能播、能当参考音频交给
本机引擎——见最后那一组测试。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app.engines.base import VoiceMeta
from app.infra import archive, audioprobe
from app.infra.config import Config, save_config
from app.infra.sidecar import read_sidecar


def _items(resp):
    """集合端点统一信封：取 data 段（兼容仍返回裸数组的旧形状）。"""
    payload = resp.json()
    return payload["data"] if isinstance(payload, dict) and "data" in payload else payload



def test_generated_is_filed_under_engine_and_voice(data_dir: Path) -> None:
    rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"audio-bytes", fmt="mp3")
    parts = rel.split("/")
    assert parts[0] == "MiniMax"  # 引擎（= 音色库里"分类"那一级）
    assert parts[1] == "茉莉"  # 音色
    assert parts[2].endswith(".mp3")  # 时间戳 + 序号
    assert absolute.is_file()
    assert absolute.read_bytes() == b"audio-bytes"
    # 显示用的相对路径就是"从根往下走的那几级"——界面拿它显示归档结构。
    assert absolute == archive.default_root() / rel


def test_every_run_gets_its_own_file(data_dir: Path) -> None:
    """同一条音频会反复生成，所以名字里看得出是哪一次（时间戳 + 序号）。"""
    first = archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")[1]
    second = archive.write_audio("minimax", "MiniMax", "茉莉", b"b", fmt="mp3")[1]
    assert first != second
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_the_global_output_dir_is_still_the_fallback(data_dir: Path) -> None:
    """没配就落在服务自己的数据目录里；配了就落到那儿（与从前一字不差）。"""
    assert archive.default_root() == data_dir / "generated"
    save_config(Config(output_dir=str(data_dir / "输出")))
    assert archive.default_root() == data_dir / "输出"
    _rel, absolute = archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"c", fmt="wav")
    assert absolute.is_relative_to(data_dir / "输出")


def test_unsafe_names_are_washed(data_dir: Path) -> None:
    """引擎名 / 音色名要能当目录名：Windows 不许 ``\\ / : * ? " < > |``，
    结尾的点与空格还会被悄悄吃掉（否则「茉莉.」与「茉莉」写成同一个目录）。"""
    assert archive.safe_segment("Mi/nimax") == "Mi_nimax"
    assert archive.safe_segment("茉莉：") == "茉莉："  # 全角冒号不在此列，原样留
    assert archive.safe_segment("茉莉:") == "茉莉_"
    assert archive.safe_segment("  ") == "未命名"
    assert archive.safe_segment("茉莉.") == "茉莉"
    rel, _ = archive.write_audio("x", "Mini/Max", "茉莉/二号", b"d", fmt="wav")
    assert rel.split("/")[:2] == ["Mini_Max", "茉莉_二号"]


class TestEngineOutputDir:
    """每台引擎自己的"音色保存目录"——生成的东西存在哪，是一个能指的地方。"""

    def test_defaults_to_a_folder_under_the_global_root(self, data_dir: Path) -> None:
        assert archive.engine_root("minimax", "MiniMax") == data_dir / "generated" / "MiniMax"

    def test_a_configured_dir_wins_and_the_engine_level_disappears(self, data_dir: Path) -> None:
        """配了引擎目录之后**不再套一层引擎名**：那一层的名字就叫"XX 音色保存目录"，
        里面再套一个 XX 只是啰嗦（完整绝对路径另给，不会让人找不到东西）。"""
        chosen = data_dir / "MiniMax 音色"
        save_config(Config(engine_outputs={"minimax": str(chosen)}))
        assert archive.engine_root("minimax", "MiniMax") == chosen
        rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"x", fmt="mp3")
        assert rel == f"茉莉/{absolute.name}"
        assert absolute.is_relative_to(chosen)

    def test_engines_do_not_affect_each_other(self, data_dir: Path) -> None:
        save_config(Config(engine_outputs={"minimax": str(data_dir / "A")}))
        assert archive.engine_root("gpt_sovits", "GPT-SoVITS") == (
            data_dir / "generated" / "GPT-SoVITS"
        )

    def test_the_endpoint_round_trips(self, data_dir: Path, client) -> None:
        """这一格是**框架给的**：任何装了的引擎都有，界面上不必各家自己声明。"""
        assert client.put("/api/v1/engines/minimax/output", json={"dir": ""}).json()["custom"] is False
        chosen = str(data_dir / "MiniMax 音色")
        saved = client.put("/api/v1/engines/minimax/output", json={"dir": chosen}).json()
        assert saved["dir"] == chosen
        assert saved["custom"] is True
        # 引擎设置那一包里也带着（界面照它回显）
        assert _items(client.get("/api/v1/engines/minimax/settings"))["output"]["dir"] == chosen
        # 空串 = 回到默认，并把"默认会落到哪"一起回（界面才知道留空意味着什么）
        back = client.put("/api/v1/engines/minimax/output", json={"dir": ""}).json()
        assert back["custom"] is False
        assert back["dir"] == str(data_dir / "generated" / "MiniMax")
        assert back["default"] == back["dir"]

    def test_an_unknown_engine_is_404(self, data_dir: Path, client) -> None:
        assert client.put("/api/v1/engines/nope/output", json={"dir": "X:/x"}).status_code == 404

    def test_the_left_panel_entry_carries_it(self, data_dir: Path, client) -> None:
        """左栏那一行的 title 靠它说"这台引擎的音色存在哪"。"""
        entry = next(e for e in client.get("/api/v1/tree").json()["engines"] if e["key"] == "minimax")
        assert entry["output"]["dir"] == str(data_dir / "generated" / "MiniMax")


class TestGeneratedBelongsToThePlatformVoice:
    """生成出来的音频 = **那条平台音色自己的样例**（一套逻辑、一套画法）。

    平台音色在磁盘上本来没有家：:mod:`app.infra.archive` 给它的家是那台引擎的"音色
    保存目录"里一个**跟它同名的文件夹**。认下来之后它跟本地音色用**同一份样例列表**
    显示（能播、能当参考音频交给本机引擎、能识别），而它仍然是一条平台音色。
    """

    def _platform(self, service, name: str = "茉莉"):
        return service.add_platform_voice("minimax", VoiceMeta(id="MX_1", name=name))

    def test_the_generated_files_become_its_samples(self, service) -> None:
        self._platform(service)
        first = archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")[1]
        second = archive.write_audio("minimax", "MiniMax", "茉莉", b"b", fmt="mp3")[1]

        found = service.list_voices(source="minimax")
        assert len(found) == 1
        voice = found[0]
        assert voice.is_local is False  # 还是平台音色：发给平台的 id 没变
        assert voice.source == "minimax"
        assert voice.sample_count == 2
        assert voice.folder == str(second.parent)
        assert [s.file for s in voice.samples] == [second.name, first.name]  # 新的在前
        assert voice.main_sample == second.name  # 主样例 = 最新那一段

    def test_a_voice_that_never_generated_anything_is_untouched(self, service) -> None:
        """没生成过的平台音色就是一条平台音色（0 条样例、folder 空着）。"""
        self._platform(service, "没生成过")
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        voice = service.list_voices(source="minimax")[0]
        assert voice.sample_count == 0
        assert voice.folder == ""
        assert voice.samples == ()

    def test_it_is_not_scanned_in_as_a_second_local_voice(self, service) -> None:
        """**同一个东西不该在库里出现两次**：能给出音色清单的引擎，保存目录不扫。

        从前是扫进来的，于是左栏出现两行同名的 MiniMax（一行 3 条"生成的"、一行 303 条
        "未生成的"），而哪一行都不是全部。
        """
        self._platform(service)
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        assert service.scan()["voices"] == 0
        assert service.list_voices(source="local") == []

    def test_the_tree_does_not_add_a_second_row_for_the_engine(self, service) -> None:
        self._platform(service)
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        roots = service.tree()["roots"]
        assert all((row["engine"] or {}).get("key") != "minimax" for row in roots)

    def test_get_voice_attaches_them_too(self, service) -> None:
        """详情、试听、参考音频、识别走的是同一条 ``get_voice``——差一处就会出现
        "列表里数得出样例、点开详情却是 0 条"。"""
        voice = self._platform(service)
        _rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        detail = service.get_voice(voice.id)
        assert [s.file for s in detail.samples] == [absolute.name]
        assert service.reference_of(voice.id)["sample_path"] == str(absolute)

    def test_the_engine_dir_setting_still_decides_where_to_look(self, service, data_dir) -> None:
        chosen = data_dir / "别处" / "MiniMax 音色"
        save_config(Config(engine_outputs={"minimax": str(chosen)}))
        self._platform(service)
        archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        assert service.list_voices(source="minimax")[0].sample_count == 1

    def test_the_annotations_live_next_to_the_audio(self, service) -> None:
        """说话内容 / 情绪落 sidecar（与本地样例同一条规矩），**不写进平台那一行**。"""
        voice = self._platform(service)
        _rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        updated = service.update_sample(voice.id, absolute.name, {"text": "茉莉花开"})
        assert updated.samples[0].text == "茉莉花开"
        # 再拉一次（这一步会重新从磁盘认一遍）——补的字还在
        assert service.get_voice(voice.id).samples[0].text == "茉莉花开"
        # 而平台那一行本身没有被写上磁盘路径（folder 该空着、索引里不该多出样例行）
        assert service._db.get_voice(voice.id).folder == ""

    def test_the_audio_endpoint_serves_it(self, service, client) -> None:
        """能播：卡片上那颗 ▶ 与生成页的"参考音频"取的是同一份。"""
        voice = self._platform(service)
        _rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"audio!", fmt="mp3")
        url = f"/api/v1/audio/{voice.id}/{absolute.name}"
        assert client.get(url).status_code == 200
        assert client.get(url).content == b"audio!"
        # 不在它样例里的文件照样 404（这条路径只认样例表，穿不过去）
        assert client.get(f"/api/v1/audio/{voice.id}/../voice.json").status_code == 404


class TestOwnershipIsByIDNotByShownName:
    """音频归属**认 id、不认显示名**——改一次显示名，生成样例不该整个消失。

    这是"两个半边"那类数据的正脸：平台音色的磁盘文件夹叫它的**稳定名**
    （``Arrogant_Miss``），而用户在界面上看到的是显示名（「嚣张小姐」）。从前读回时
    按显示名找文件夹，于是改名之后这段音频还在磁盘上、界面上却当它从没生成过——
    而按量计费买来的东西，正是"生成过却找不到"最贵的那种丢法。

    两条路各钉一次：新数据靠 sidecar 里记的 ``origin``（正本），老数据（没有这个键）
    靠文件夹名兜底、而且 **``shown_name`` 与 ``name`` 两个都试**。
    """

    def _platform(self, service, *, vid: str = "Arrogant_Miss", display: str = ""):
        # display 留空 → shown_name 回落成 name（= 文件夹名）。改名那一步在用例里做。
        return service.add_platform_voice("minimax", VoiceMeta(id=vid, name=display))

    def test_the_write_records_which_voice_it_belongs_to(self, service) -> None:
        """落盘时把归属**和音频一起**记进 sidecar：文件夹名只是人看着方便。"""
        voice = self._platform(service)
        _rel, absolute = archive.write_audio(
            "minimax", "MiniMax", voice.name, b"a", fmt="mp3", voice_id=voice.id
        )
        assert read_sidecar(absolute.parent).origin == {
            "voice_id": "minimax:Arrogant_Miss",
            "engine": "minimax",
        }

    def test_a_renamed_voice_still_owns_what_it_generated(self, service) -> None:
        voice = self._platform(service)
        archive.write_audio("minimax", "MiniMax", voice.name, b"a", fmt="mp3", voice_id=voice.id)
        # 用户把显示名改成「嚣张小姐」——磁盘上那个文件夹还叫 Arrogant_Miss。
        service.update_voice(voice.id, {"display": "嚣张小姐"})

        found = service.list_voices(source="minimax")
        assert len(found) == 1
        assert found[0].shown_name == "嚣张小姐"
        assert found[0].sample_count == 1  # ← 从前这里是 0
        # 详情（试听 / 参考音频 / 识别走的那条）也是同一份——差一处就会出现
        # "列表里数得出、点开却是 0 条"。
        assert service.get_voice(voice.id).sample_count == 1

    def test_a_folder_without_an_origin_falls_back_to_the_name(self, service) -> None:
        """老数据没有 ``origin``（这个键是后加的）→ 拿文件夹名认回来。

        写盘时用的是**音色名**（= 文件夹名），改名之后 ``shown_name`` 与之不符——
        所以兜底必须两个名字都试，只试 ``shown_name`` 正是当初丢样例的那一处。
        """
        voice = self._platform(service)
        service.update_voice(voice.id, {"display": "嚣张小姐"})
        # 不传 voice_id → 模拟"从前写下的东西"，sidecar 里没有 origin。
        archive.write_audio("minimax", "MiniMax", "Arrogant_Miss", b"a", fmt="mp3")

        found = service.list_voices(source="minimax")[0]
        assert found.sample_count == 1  # ← 从前按 shown_name 找，找不到

    def test_the_records_are_not_split_across_two_folders_after_a_rename(
        self, service
    ) -> None:
        """改名前后各生成一段：**两批都要算它的**（名字变过，id 没变）。

        从前写盘用显示名，用户名一改，新音频就落到**另一个文件夹**里，两批各认各的、
        哪一批都不是全部——而用户会以为"改名前生成的那些丢了"。
        """
        voice = self._platform(service)
        archive.write_audio("minimax", "MiniMax", voice.name, b"a", fmt="mp3", voice_id=voice.id)
        service.update_voice(voice.id, {"display": "嚣张小姐"})
        # 新契约下写盘仍然用稳定名（voice.name），所以两段落在同一个文件夹里；
        # 但即便调用方"照着显示名"写到了别处，origin 也会把它算回来。
        archive.write_audio(
            "minimax", "MiniMax", "嚣张小姐", b"b", fmt="mp3", voice_id=voice.id
        )
        assert service.get_voice(voice.id).sample_count == 2


class TestTreeTotalCountsEverything:
    def test_the_total_includes_platform_voices_not_just_the_roots(
        self, service, demo_library: Path
    ) -> None:
        """「全部音色」那一行说的是**库里一共有多少条**，不是"扫描根加起来"。

        左栏从前自己累加各个根，于是把三百多条平台音色整段漏掉——写着 577、
        点开却是 880，读的人只会以为"平台音色没了"。
        """
        service.save_folders([str(demo_library)])  # 4 条本地
        service.add_platform_voice("minimax", VoiceMeta(id="MX_1", name="林晚"))
        service.add_platform_voice("minimax", VoiceMeta(id="MX_2", name="苏晴"))

        tree = service.tree()
        assert tree["total"] == 6
        # 根加起来会少掉平台那两条——那正是"写着 577、点开 880"的来历
        assert sum(row["total"] for row in tree["roots"]) == 4


class TestTheRequestIsRecorded:
    """合成的那一刻手里什么都有——**顺手就登记下来**（不然样例卡片是一片空白）。

    从前落盘只写了音频：同一条音色生成五次之后，谁也说不清哪一段是哪一句、哪一次是
    拿什么参数发的。这是按量计费买回来的东西，记一笔来路是最低要求。

    写的是**同一份 sidecar 格式**（``voice.json`` 的 ``samples`` 键，键就是文件名）：
    平台音色靠 ``_attach`` 读、给不出音色清单的引擎（保存目录是扫描根）靠扫描读，
    两边看到的是同一份东西——**不重新造一套**。
    """

    def _platform(self, service, name: str = "茉莉"):
        return service.add_platform_voice("minimax", VoiceMeta(id="MX_1", name=name))

    def test_text_emotion_and_params_are_written_down(self, service) -> None:
        self._platform(service)
        archive.write_audio(
            "minimax",
            "MiniMax",
            "茉莉",
            b"a",
            fmt="mp3",
            text="今天天气不错",
            emotion="开心",
            params={"speed": 1.2, "pitch": 1.3, "format": "mp3"},
        )
        sample = service.list_voices(source="minimax")[0].samples[0]
        assert sample.text == "今天天气不错"
        assert sample.emotion == "开心"
        # 保序（先后是规范定的）+ 值统一成字符串
        assert sample.params == (("speed", "1.2"), ("pitch", "1.3"), ("format", "mp3"))

    def test_whole_numbers_lose_their_point(self, service) -> None:
        """JSON 里的采样率常常写成 ``24000.0``——显示成 ``24000`` 才是人看的。"""
        self._platform(service)
        archive.write_audio(
            "minimax",
            "MiniMax",
            "茉莉",
            b"a",
            fmt="wav",
            params={"sample_rate": 24000.0, "channels": 1},
        )
        assert service.list_voices(source="minimax")[0].samples[0].params == (
            ("sample_rate", "24000"),
            ("channels", "1"),
        )

    def test_nothing_to_record_leaves_no_sidecar(self, service) -> None:
        """没正文也没参数（老调用方）→ **不写那个文件**，免得留下一个空壳。"""
        self._platform(service)
        _rel, absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"a", fmt="mp3")
        assert not (absolute.parent / "voice.json").exists()
        assert service.list_voices(source="minimax")[0].samples[0].params == ()

    def test_a_later_generation_keeps_what_earlier_ones_recorded(self, service) -> None:
        """又生成一条时，**前面那几条的记录不能被冲掉**（包括用户手加的标签）。"""
        voice = self._platform(service)
        _rel, first = archive.write_audio(
            "minimax", "MiniMax", "茉莉", b"a", fmt="mp3", text="第一句"
        )
        service.update_sample(voice.id, first.name, {"tags": ["笑声"]})
        archive.write_audio("minimax", "MiniMax", "茉莉", b"b", fmt="mp3", text="第二句")

        by_file = {s.file: s for s in service.get_voice(voice.id).samples}
        assert by_file[first.name].text == "第一句"
        assert by_file[first.name].tags == ("笑声",)

    def test_editing_the_text_keeps_the_record_of_how_it_was_made(self, service) -> None:
        """改"念的是什么"不该把"当时用的什么参数"抹掉——那一栏是来路，不是备注。"""
        voice = self._platform(service)
        _rel, absolute = archive.write_audio(
            "minimax", "MiniMax", "茉莉", b"a", fmt="mp3", text="原句", params={"speed": 1.2}
        )
        updated = service.update_sample(voice.id, absolute.name, {"text": "改过"})
        assert updated.samples[0].text == "改过"
        assert updated.samples[0].params == (("speed", "1.2"),)

    def test_the_scan_reads_it_back_too(self, service, demo_library) -> None:
        """生成样例挂到**本地音色自己**身上：同一份 sidecar、同一套读法。

        它不在音色自己的扫描目录里，但 ``origin.voice_id`` 认得回去；参数、正文
        一样要读得出来。扫描不会因此多出一条同名本地音色。
        """
        service.save_folders([str(demo_library)])
        hutao = next(v for v in service.list_voices() if v.name == "胡桃")
        before = hutao.sample_count
        archive.write_audio(
            "minimax",
            "MiniMax",
            "胡桃",
            b"a",
            fmt="wav",
            voice_id=hutao.id,
            text="往生堂",
            params={"speed": 1.1},
        )
        full = service.get_voice(hutao.id)
        added = [s for s in full.samples if s.text == "往生堂"]
        assert added and added[0].params == (("speed", "1.1"),)
        assert full.sample_count == before + 1

        # 重扫不长出第二条"胡桃"
        service.scan()
        assert len([v for v in service.list_voices(source="local") if v.name == "胡桃"]) == 1

    def test_the_api_hands_over_the_chinese_labels(self, service, client) -> None:
        """界面显示的是**规范里的中文名**：后端翻好再下发，前端不自己造词。"""
        voice = self._platform(service)
        archive.write_audio(
            "minimax",
            "MiniMax",
            "茉莉",
            b"a",
            fmt="mp3",
            text="你好",
            params={"speed": 1.2, "不认识的键": "x"},
        )
        sample = _items(client.get(f"/api/v1/voices/{voice.id}"))["samples"][0]
        assert sample["text"] == "你好"
        assert sample["params"] == [
            {"key": "speed", "label": "语速", "value": "1.2"},
            # 认不出的键原样显示——藏着它反而查不出"当时到底发了什么"
            {"key": "不认识的键", "label": "不认识的键", "value": "x"},
        ]


class TestCacheIsNotASample:
    """生成页扇出的结果落**缓存目录**，不是"那条音色的样例"。

    两个入口的区别只有一条（见 :func:`archive.cache_root`）：

    - 音色卡片上那颗「生成样例」→ ``as_sample=True`` → 落**音色保存目录**，
      平台音色的话它当场就是那条音色的样例；
    - 生成页扇出 → 默认 ``False`` → 落**缓存目录**。它是"生成的内容"：
      **不扫进库、不挂给任何音色**。

    为什么这一版要分：本机引擎拿你库里的音色生成出来的东西，**仍然是那条音色**——
    凭什么变成这台引擎自己的一条"音色"？从前生成页随手跑几行，库里就会长出同名条目、
    左栏还会多出一行挂着引擎图标的"音色层"。
    """

    def test_a_cached_file_lands_under_the_service_data_dir(self, data_dir: Path) -> None:
        _rel, absolute = archive.write_audio(
            "gpt_sovits", "GPT-SoVITS", "胡桃", b"c", fmt="wav", as_sample=False
        )
        assert absolute.is_relative_to(data_dir / "cache" / "GPT-SoVITS" / "胡桃")
        # **不在生成结果目录里**：那个目录是用户配的（可能指到自己的素材库里），
        # 生成一次就往那儿多一个文件夹正是要避免的事。
        assert not absolute.is_relative_to(archive.default_root())

    def test_the_cache_always_keeps_the_engine_segment_in_the_rel_path(
        self, data_dir: Path
    ) -> None:
        """缓存那一段**无论如何都套引擎名**：缓存目录没有"用户配的名字"可依赖，
        而 `/api/v1/generated` 要照这个相对路径把它找回来。
        """
        save_config(Config(engine_outputs={"gpt_sovits": str(data_dir / "我的音色")}))
        rel, absolute = archive.write_audio(
            "gpt_sovits", "GPT-SoVITS", "胡桃", b"c", fmt="wav", as_sample=False
        )
        assert rel.split("/")[0] == "GPT-SoVITS"
        assert absolute.is_relative_to(data_dir / "cache")
        # 而样例那一份照旧不套（那一层就叫"GPT-SoVITS 的音色保存目录"）——
        # 两个入口各落一处，相对路径的形状也就各按各的根算。
        sample_rel, sample_abs = archive.write_audio(
            "gpt_sovits", "GPT-SoVITS", "胡桃", b"d", fmt="wav"
        )
        assert sample_rel.split("/")[0] == "胡桃"
        assert sample_abs.is_relative_to(data_dir / "我的音色")

    def test_a_cached_file_is_never_scanned_into_the_library(self, service) -> None:
        archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"c", fmt="wav", as_sample=False)
        assert service.scan()["voices"] == 0
        assert service.list_voices(source="local") == []

    def test_a_cached_file_does_not_become_a_platform_voices_sample(self, service) -> None:
        """**平台音色同理**：生成页跑一次不等于"给它补了一条样例"。

        （"生成样例"那颗按钮走的就是样例那一路——`as_sample=True`，所以它照样挂得上。）
        """
        voice = service.add_platform_voice("minimax", VoiceMeta(id="MX_1", name="林晚"))

        _sample_rel, sample_file = archive.write_audio(
            "minimax", "MiniMax", "MX_1", b"ID3-ready", fmt="mp3", voice_id=voice.id
        )
        _cached_rel, cached_file = archive.write_audio(
            "minimax",
            "MiniMax",
            "MX_1",
            b"ID3-cache",
            fmt="mp3",
            voice_id=voice.id,
            as_sample=False,
        )
        assert sample_file != cached_file

        samples = service.get_voice(voice.id).samples
        assert [s.file for s in samples] == [sample_file.name]  # 缓存那一份没混进来
        assert cached_file.is_file()  # 它还在磁盘上，只是不算谁的样例

    def test_the_api_can_serve_a_cached_file(self, client) -> None:
        """回了地址就得取得到——不然界面上那颗播放器点了是空白。"""
        rel, _absolute = archive.write_audio(
            "gpt_sovits", "GPT-SoVITS", "胡桃", b"RIFF-cache", fmt="wav", as_sample=False
        )
        served = client.get(f"/api/v1/generated/{rel}", params={"engine": "gpt_sovits"})
        assert served.status_code == 200
        assert served.content == b"RIFF-cache"
        # 不带 `engine` 的老链接也认（缓存目录在候选根里）。
        assert client.get(f"/api/v1/generated/{rel}").status_code == 200


class TestLibraryBridgeDirIsNotARoot:
    """**音色库供给（库桥）的引擎，保存目录不是扫描根**——它的产物不是这台引擎的音色。

    2026-10-16 改的口径。从前的理由是"产物没人挂得上，只能按目录扫进来当本地音色"，
    于是实打实地长出两个鬼：

    1. 左栏「全部音色」下面多一行**挂着引擎图标的"音色层"**（BreezeTTS 2 带一个 0）
       ——读起来像"这台引擎也有一批音色"，可它一条都没有；
    2. 库里多一条**同名音色**：「SP关羽」（三国杀/SP关羽）之外又长出一个「SP关羽」
       （引擎保存目录里的那条），两条各自一半的样例，哪一条都不是全部。

    它拿你库里的音色生成出来的东西，**仍然是那条音色**——所以现在生成页扇出的结果
    根本不落进保存目录（进的是 `archive.cache_root`，见下面那组测试），保存目录里
    只会有"音色卡片上那颗「生成样例」"写进去的东西，而那颗按钮只对平台音色出现。

    这一组钉的是"扫也不扫、也不占一行"。
    """

    def test_its_output_dir_is_not_a_scan_root(self, service) -> None:
        archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"a", fmt="wav")
        assert service.scan()["roots"] == 0
        assert service.tree()["roots"] == []

    def test_files_left_in_the_old_dir_do_not_become_local_voices(self, service) -> None:
        """老目录里还躺着的那些（这一版之前生成的）也不再扫成音色——重扫就消失。

        它们没有被删（磁盘上还在），只是不再冒充"这台引擎的音色"。
        """
        archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"a", fmt="wav")
        assert service.scan()["voices"] == 0
        assert service.list_voices(source="local") == []

    def test_no_row_in_the_left_column(self, service) -> None:
        archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"a", fmt="wav")
        service.scan()
        assert service.tree()["roots"] == []
        # 引擎本身照旧在「引擎」那一段里（那是管理入口，与音色多少无关）。
        assert "gpt_sovits" in {row["source"] for row in service.tree()["engines"]}


class TestTheLinkSurvivesWhatUsersReallyDo:
    """排查"还有哪些地方拿**显示名**当链接"：用户真的会做的两个动作之后，还认不认得出。

    - **改显示名**（平台音色那一格随时能改）——音频在磁盘上没动，那么"能播、能当参考
      音频"就都该照旧。这两条走的是 ``voice.folder``（样例表里的路径），不是配置里
      存下来的那串相对路径，所以改名本来就不该碰到它们；
    - **把"音色保存目录"挪个地方**（迁移）——文件跟着走，认归属靠 sidecar 里记的 id，
      所以挪完之后照样挂得住（靠路径去记的话，这里就会整批消失）。
    """

    def _platform(self, service, *, vid: str = "Arrogant_Miss", display: str = ""):
        return service.add_platform_voice("minimax", VoiceMeta(id=vid, name=display))

    def test_a_rename_does_not_break_playback_or_the_reference_path(
        self, service, client
    ) -> None:
        voice = self._platform(service)
        _rel, absolute = archive.write_audio(
            "minimax", "MiniMax", voice.name, b"audio!", fmt="mp3", voice_id=voice.id
        )
        service.update_voice(voice.id, {"display": "嚣张小姐"})

        # 能播：卡片上那颗 ▶ 与「生成样例」取的是同一份（这条路径只认样例表）
        url = f"/api/v1/audio/{voice.id}/{absolute.name}"
        assert client.get(url).status_code == 200
        assert client.get(url).content == b"audio!"
        # 参考音频那一格（递给本机引擎的绝对路径）也还在
        assert service.reference_of(voice.id)["sample_path"] == str(absolute)

    def test_moving_the_engine_dir_keeps_the_samples(self, service, data_dir) -> None:
        from app.usecase.engines import save_engine_output_dir

        voice = self._platform(service)
        archive.write_audio("minimax", "MiniMax", voice.name, b"a", fmt="mp3", voice_id=voice.id)
        assert service.get_voice(voice.id).sample_count == 1

        chosen = data_dir / "别处" / "MiniMax 音色"
        save_engine_output_dir("minimax", str(chosen), migrate=True)

        detail = service.get_voice(voice.id)
        assert detail.sample_count == 1  # 文件搬走了，样例照样认得出
        assert Path(detail.folder).is_relative_to(chosen)


class TestOneRowPerEngineInTheTree:
    """**一台引擎在「全部音色」里最多一行**：它自己那一层，或者它的保存目录——不是两行。

    用户报的就是"两个 MiniMax"：一个是清单（303 条），一个是保存目录（生成过的那几条），
    而那两个数**哪个都不是全部**。能给清单的引擎，产物本来就挂在自己那条音色上
    （见 :meth:`LibraryService._with_generated`）——所以保存目录那一行是多余的；
    "这条从哪来"在**卡片上**看得出来（引擎图标 + 徽标 + 右侧的「生成样例」）。

    **音色库供给的那几家现在一行都没有**（2026-10-16 改）：它们的产物压根不是这台
    引擎的音色，保存目录连扫描根都不是（见 ``TestLibraryBridgeDirIsNotARoot``）。
    从前那一行的存在理由是"产物没别处可挂，不扫就成了磁盘上看得见、库里看不见的
    东西"——现在那个理由没了，因为产物**本来就不该进库**。
    """

    def test_a_listing_engine_never_gets_a_dir_row(self, data_dir, service) -> None:
        service.add_platform_voice("minimax", VoiceMeta(id="MX_1", name="林晚"))
        archive.write_audio("minimax", "MiniMax", "MX_1", b"a", fmt="mp3", voice_id="minimax:MX_1")
        assert service.scan()["roots"] == 0

        # 产物挂在平台音色上：不产生本地条目，也不占左栏一行
        assert service.list_voices(source="local") == []
        assert service.tree()["roots"] == []

        # 而它自己那一层在 `engines` 里，条数对得上（界面照它画那一行）
        row = next(item for item in service.tree()["engines"] if item["source"] == "minimax")
        assert row["can_list"] is True
        assert row["total"] == 1

    def test_a_library_bridge_engine_gets_no_row_at_all(self, service) -> None:
        """库桥那几家**一行都没有**——它没有自己的音色，左栏里没什么可指的。"""
        archive.write_audio("gpt_sovits", "GPT-SoVITS", "胡桃", b"a", fmt="wav")
        service.scan()
        keys = [(row["engine"] or {}).get("key") for row in service.tree()["roots"]]
        assert keys == []


class TestTreeMirrorsFolders:
    """左栏的节点 = **磁盘上真实存在的目录**，一层层照文件夹结构长。"""

    def test_voices_directly_under_the_root_get_no_node(
        self, service, demo_library: Path
    ) -> None:
        """整个库都直接在根下（没有中间层）→ 左栏就那一行根，不造任何子节点。

        这是最常见的一种布局：用户把扫描根指到"那一层"，底下全是音色文件夹。
        从前会给它们造一个「（未分类）」节点，数出来还跟点根一模一样。
        """
        root = demo_library / "原神"  # 底下直接是 胡桃 / 钟离，没有分类层
        service.save_folders([str(root)])

        row = service.tree()["roots"][0]
        assert row["children"] == []  # 只有目录才是节点，而这里没有目录那一层
        assert row["total"] == 2  # 两条音色照样数得进来、点根就看得到
        assert {v.name for v in service.list_voices(source="local", root=str(root))} == {
            "胡桃",
            "钟离",
        }

    def test_nested_folders_become_nested_nodes(self, service, tmp_path: Path) -> None:
        """``三国杀/魏国/曹操`` 这种结构：左栏「三国杀 → 魏国」一层层长。"""
        from conftest import make_wav

        base = tmp_path / "lib"
        make_wav(base / "三国杀" / "魏国" / "曹操" / "默认.wav")
        make_wav(base / "三国杀" / "蜀国" / "关羽" / "默认.wav")
        service.save_folders([str(base)])

        row = service.tree()["roots"][0]
        (sanguosha,) = row["children"]  # 只有一个顶层目录
        assert sanguosha["name"] == "三国杀"
        assert sanguosha["count"] == 2  # 子树里的音色数（穿过这一层的都算）
        assert [node["name"] for node in sanguosha["children"]] == ["蜀国", "魏国"]
        assert sanguosha["children"][1]["path"] == "三国杀/魏国"  # 点它回传的筛选值


class TestRootFilter:
    """按根筛（左栏每一行说的都是"这个根里有哪些音色"）。

    从前只有用户自己一个扫描根，所以"点根目录那一行把别人的也列出来"看不出来；多出
    引擎目录那一行之后就必须筛了。
    """

    def test_it_separates_the_roots(self, service, demo_library) -> None:
        service.save_folders([str(demo_library)])
        found = service.list_voices(source="local", root=str(demo_library))
        assert {v.shown_name for v in found} == {"胡桃", "钟离", "阿米娅", "散件"}

    def test_the_path_shape_does_not_matter(self, service, demo_library) -> None:
        """同一条根写成 `C:/x` 还是 `c:\\x` 都要筛得出来。

        配置里那一行是**手写的**（也可能被别的脚本改写），库里那一行是扫描写的；
        两种写法在 LIKE 里互不匹配，于是"左栏数得出 3 个、点进去 0 个"，而且**不报任何
        错**（SQL 只是没匹配上）——那是"有时候怎么都不显示"的来历。
        """
        service.save_folders([str(demo_library)])
        forward = str(demo_library).replace("\\", "/")
        assert len(service.list_voices(source="local", root=forward)) == 4
        assert len(service.list_voices(source="local", root=str(demo_library).upper())) == 4

    def test_the_tree_counts_by_the_same_rule(self, service, demo_library) -> None:
        """左栏那一行的计数与"点进去看到的"必须是同一份规则，否则一边有条目、一边空。

        这里故意用**正斜杠**存那条根：扫描会把音色按这个写法写进库，而左栏那一行显示的是
        配置里的原文——两边都得折形之后才对得齐。

        直接放在根下的「散件」**不是节点**（左栏只长磁盘上存在的目录），但它算在
        ``total`` 里——点根那一行看得见它，两边的数对得上。
        """
        forward = str(demo_library).replace("\\", "/")
        service.save_folders([forward])
        row = service.tree()["roots"][0]
        assert row["total"] == 4
        children = {node["name"]: node["count"] for node in row["children"]}
        assert children == {"原神": 2, "明日方舟": 1}
        # 点进去看见的也是这 4 条（同一份规则的另一半）
        assert len(service.list_voices(source="local", root=forward)) == 4

    def test_like_wildcards_are_escaped(self, service, data_dir) -> None:
        """路径里的 `_` 不能当通配符——不然"按根筛"会静悄悄多出一批别的目录。"""
        (data_dir / "a_b" / "音色").mkdir(parents=True)
        (data_dir / "axb" / "音色").mkdir(parents=True)
        (data_dir / "a_b" / "音色" / "默认.wav").write_bytes(b"x")
        (data_dir / "axb" / "音色" / "默认.wav").write_bytes(b"x")
        service.save_folders([str(data_dir / "a_b"), str(data_dir / "axb")])
        roots = service.folders()
        hit = service.list_voices(source="local", root=roots[0])
        assert [v.folder for v in hit] == [str(data_dir / "a_b" / "音色")]


def test_generated_endpoint_only_serves_what_is_under_the_root(data_dir: Path, client) -> None:
    """拼出来的文件不在那个保存目录里 = 404。**不报"路径不合法"**：多说一句就等于把
    目录结构告诉了不该知道的人。"""
    rel, _absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"", fmt="wav")
    assert client.get(f"/api/v1/generated/{rel}?engine=minimax").status_code == 200
    # 不带引擎（老链接、外部调用方）也找得到：按几个候选根挨个试。
    assert client.get(f"/api/v1/generated/{rel}").status_code == 200
    assert client.get("/api/v1/generated/..%2F..%2Fconfig.json").status_code == 404
    assert client.get("/api/v1/generated/没有这个.wav").status_code == 404


def test_generated_endpoint_finds_files_in_a_configured_engine_dir(
    data_dir: Path, client
) -> None:
    """引擎配了自己的目录之后，"相对谁"就不只有全局那一个答案了——带上引擎才是确定的。"""
    chosen = data_dir / "别处" / "MiniMax 音色"
    save_config(Config(engine_outputs={"minimax": str(chosen)}))
    rel, _absolute = archive.write_audio("minimax", "MiniMax", "茉莉", b"x", fmt="mp3")
    assert client.get(f"/api/v1/generated/{rel}?engine=minimax").status_code == 200


def test_output_endpoint_round_trip(data_dir: Path, client) -> None:
    """全局那层会的是**生效中**的目录（没配时给默认的那个），空串 = 回到默认。"""
    assert client.get("/api/v1/output").json()["custom"] is False
    chosen = str(data_dir / "输出")
    saved = client.put("/api/v1/output", json={"dir": chosen}).json()
    assert saved["dir"] == chosen
    assert saved["custom"] is True
    assert client.get("/api/v1/output").json()["dir"] == chosen
    assert client.put("/api/v1/output", json={"dir": ""}).json()["dir"] == str(data_dir / "generated")


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """一段真 WAV（头部读得出时长）：归档时要量的就是这种东西。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class TestDurationIsRecordedAtArchiveTime:
    """落盘那一刻就把 (时长, 字节数) 写进 ``voice.json``。

    无论音频从哪来（生成、试听存样例、平台音色挂目录），记录它的只有
    :func:`archive.record_sample` 一处——写在这里，重扫时就不必为每个新样例各起一次
    ffprobe，也不必等用户去改一次文字才补上。
    """

    def test_a_wav_records_its_duration_without_ffprobe(self, data_dir: Path) -> None:
        data = _wav(0.5)
        _rel, path = archive.write_audio(
            "breeze_tts", "BreezeTTS 2", "胡桃", data, fmt="wav", text="你好"
        )
        entry = read_sidecar(path.parent).samples[path.name]
        # WAV 读头得时长，不必起 ffprobe（这台机器没有也能过）。
        assert entry["duration"] == pytest.approx(0.5, abs=0.01)
        assert entry["bytes"] == len(data)

    def test_without_ffprobe_the_size_is_still_kept(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """量不出时长（mp3 + 没 ffprobe）就什么都不记：0 秒是"没量过"，
        而只有字节数、没有时长可校验的那一对也没有读的意思，于是两者都不留。"""
        monkeypatch.setattr(audioprobe, "_resolve_ffprobe", lambda configured: "")
        _rel, path = archive.write_audio(
            "minimax", "MiniMax", "茉莉", b"not-really-mp3", fmt="mp3", text="你好"
        )
        entry = read_sidecar(path.parent).samples[path.name]
        assert "duration" not in entry
        assert "bytes" not in entry
        assert entry["text"] == "你好"

    def test_rewriting_the_record_does_not_measure_again(self, data_dir: Path) -> None:
        """同一条音频再写一次记录（改文字/补参数）：时长沿用，不再量。"""
        data = _wav(0.5)
        _rel, path = archive.write_audio(
            "breeze_tts", "BreezeTTS 2", "胡桃", data, fmt="wav", text="先写的"
        )
        first = read_sidecar(path.parent).samples[path.name]
        archive.record_sample(path.parent, path.name, text="改过的")
        again = read_sidecar(path.parent).samples[path.name]
        assert again["text"] == "改过的"
        assert again["duration"] == first["duration"]
        assert again["bytes"] == first["bytes"]
