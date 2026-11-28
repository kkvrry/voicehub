"""生产方插件域：发现、配置、分章拆句与项目生命周期。"""

from __future__ import annotations

import itertools
import math
import shutil
import struct
import wave
from pathlib import Path

from app.producers.builtin.audiobook.rules import split_chapters, split_rows

SAMPLE = (
    "第一章 起\n少年推门而入。\n“你来了。”老人说道。\n\n第二章 承\n风从窗缝里进来。\n“坐吧。”\n"
)


def _tone(path: Path, seconds: float = 0.2) -> None:
    """写一段 440Hz 的 wav：ffmpeg 能识别即可，内容不重要。"""
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


def _merged_project(tmp_path: Path, *, settings=None):
    """两章都写好了音频并逐章合并过的项目。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path, settings=settings)
    project = service.create("样书", SAMPLE)
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    for chapter in service.store.chapters(project["id"]):
        for row in service.store.rows(chapter["id"]):
            path = tmp_path / "audio" / f"{row['id']}.wav"
            _tone(path)
            service.store.set_row_audio(row["id"], str(path))
        service.merge(project["id"], chapter["id"])
    return service, project["id"]


def test_detail_chapters_carry_speakers(tmp_path: Path) -> None:
    """章节栏判定「本章有没有这个角色」用的按章名单。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", SAMPLE)
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapters = service.store.chapters(project["id"])
    first_rows = service.store.rows(chapters[0]["id"])
    service.store.set_row_annotation(first_rows[0]["id"], "少年", "")

    detail = service.detail(project["id"])
    assert all(isinstance(item["speakers"], list) for item in detail["chapters"])
    by_id = {item["id"]: item["speakers"] for item in detail["chapters"]}
    assert "少年" in by_id[chapters[0]["id"]]
    assert "少年" not in by_id[chapters[1]["id"]]


def test_registry_lists_steps_and_settings() -> None:
    from app.producers.registry import discover

    plugins = {plugin.key: plugin for plugin in discover()}
    assert "audiobook" in plugins
    data = plugins["audiobook"].describe()
    assert [step["key"] for step in data["steps"]] == [
        "chapters",
        "sentences",
        "analysis",
        "audio",
        "qc",
        "merge",
    ]
    assert data["settings_group"] == {"key": "producer:audiobook", "label": "创作"}
    # 设置页只放目录一组：合并、字幕、BGM、分析与合成参数都在创作页的卡片上改。
    assert data["settings_page_groups"] == ["项目"]
    assert "project_dir" in data["settings_schema"]["properties"]
    assert data["settings_schema"]["properties"]["merge_bitrate"]["default"] == "128k"
    assert data["settings_schema"]["properties"]["analysis_model"]["widget"] == "llm_model"
    assert data["settings_schema"]["properties"]["analysis_channel"]["widget"] == "llm_provider"
    assert data["settings_schema"]["properties"]["analysis_prompt"]["widget"] == "textarea"
    assert data["settings_schema"]["properties"]["bgm_enabled"]["widget"] == "switch"
    # 默认值：每批 120 行、成品 M4A、分析并发 4、章节前后 3s / 5s 静音。
    properties = data["settings_schema"]["properties"]
    assert properties["analysis_batch"]["default"] == 120
    assert properties["analysis_concurrency"]["default"] == 4
    assert properties["analysis_context_lines"]["default"] == 80
    assert properties["concurrency"]["default"] == 4
    assert properties["chapter_lead_ms"]["default"] == 3000
    assert properties["chapter_tail_ms"]["default"] == 5000
    assert properties["merge_format"]["default"] == "m4a"
    assert properties["bgm_normalize"]["default"] is True
    assert properties["bgm_gain_db"]["default"] == -20.0
    # 逐行音频的容器不设项：它由引擎决定（见 audio_stage 的落盘处）。
    assert "audio_format" not in properties
    # 设置页按 `group` 分卡片；两个条件项各自标出依赖。
    assert properties["analysis_prompt"]["group"] == "对话分析"
    assert properties["concurrency"]["group"] == "逐行合成"
    assert properties["merge_bitrate"]["group"] == "成品合并"
    assert properties["merge_format"]["group"] == "成品合并"
    assert properties["bgm_normalize"]["group"] == "背景音乐"
    assert properties["analysis_batch"]["enabled_when"] == {
        "field": "analysis_granularity",
        "equals": "batch",
    }
    assert properties["analysis_context_lines"]["enabled_when"] == {
        "field": "analysis_context",
        "equals": "window",
    }
    # 码率只对压缩成品有意义：格式换成 WAV 时置灰。
    assert properties["merge_bitrate"]["enabled_when"] == {
        "field": "merge_format",
        "equals": ["m4a", "mp3"],
    }
    bitrates = [value for value, _ in properties["merge_bitrate"]["choices"]]
    assert bitrates == ["64k", "96k", "128k", "160k", "192k", "256k"]
    formats = [value for value, _ in properties["merge_format"]["choices"]]
    assert formats == ["m4a", "mp3", "wav"]
    # 提示词预设随自描述下发；第一条是「说话人+情绪」，带 {text} / {rows} 占位。
    presets = data["analysis_presets"]
    assert [item["key"] for item in presets] == [
        "default",
        "instruction",
        "instruction_emotion",
        "terse",
        "conservative",
    ]
    assert presets[0]["label"] == "说话人+情绪"
    assert "{text}" in presets[0]["text"] and "{rows}" in presets[0]["text"]
    assert presets[1]["label"] == "说话人+引导指令"
    assert presets[2]["label"] == "说话人+引导指令+情绪"
    # 引导指令要自带情绪基调：只认指令的引擎上，情绪没有第二次机会。
    for index in (1, 2):
        text = presets[index]["text"]
        assert "情绪" in text and "引导指令" in text
        assert "不超过 20 字" in text
    # 步骤文案只写动作，不带完成状态。
    assert data["steps"][0]["label"] == "分章"


def test_split_chapters_prefers_headings() -> None:
    chapters = split_chapters(SAMPLE)
    assert [title for title, _ in chapters] == ["第一章 起", "第二章 承"]
    assert split_chapters("没有章节标题的一段话。") == [("全文", "没有章节标题的一段话。")]
    assert split_chapters("   ") == []


def test_split_rows_only_writes_placeholders() -> None:
    """拆句不猜人名、不改写原文：只写“对话”“旁白”，真名留给分析。"""
    rows = split_rows("少年推门而入。\n“你来了。”老人说道。\n")
    assert [(row["kind"], row["speaker"], row["text"]) for row in rows] == [
        ("narration", "旁白", "少年推门而入。"),
        ("dialogue", "对话", "“你来了。”"),
        ("narration", "旁白", "老人说道。"),
    ]


def test_quoted_terms_are_not_dialogue() -> None:
    """引号里有没有句读，决定它是台词还是给词加引号。"""

    def kinds(text: str) -> list[str]:
        return [row["kind"] for row in split_rows(text)]

    # 术语、专名、行内强调：整句都是旁白。
    assert kinds("所谓“自由”，不过是幻觉。") == ["narration"]
    assert kinds("“永恒”自天而降。") == ["narration"]
    assert kinds("他说“自由”很重要。") == ["narration"]
    # 紧跟在文字后面的引用（旧版口径）也按旁白。
    assert kinds("他低声说“我不去”，然后就走了。") == ["narration"]
    # 引号里有句读 = 有人在说话。
    assert kinds("“你来了。”") == ["dialogue"]
    # 台词中间夹提示语：台词 / 提示语 / 台词。
    assert kinds("“你呀，”老师说，“一毕业就懂了。”") == [
        "dialogue",
        "narration",
        "dialogue",
    ]
    # 引号没闭合：不敢断言是台词，按旁白处理。
    assert kinds("“他没有闭合。") == ["narration"]


def test_split_keeps_every_character() -> None:
    """拆句只切不改字：各行文本合起来就是原文（去掉空白）。"""
    import re

    text = (
        "少年推门而入，屋里很暗。“你来了。”老人说道。\n"
        "“嗯。”他应了一声。\n"
        "所谓“自由”，不过是幻觉。\n"
        "他低声说“我不去”，然后就走了。\n"
    )
    rows = split_rows(text)
    joined = re.sub(r"\s+", "", "".join(row["text"] for row in rows))
    assert joined == re.sub(r"\s+", "", text)


def test_long_dialogue_keeps_ellipsis_with_its_sentence() -> None:
    """超长台词按句读再切，但省略号不能自己掉出来成一行「…」。"""
    from app.producers.builtin.audiobook.rules import SplitOptions

    quote = (
        "“一个人本来可以把样样东西都捞到手，可是只因为胆小，就全放过去，什么也没抓到……"
        "这真是明显的道理……我倒很想弄清楚人们最怕的究竟是什么。"
        "他们最怕的就是采取新的做法，说出自己心里新的话语……不过呢，我唠叨得太多了。"
        "我什么事也没干，就是因为我老是唠叨。然而事情也许是这样：因为我什么事也不干，"
        "才唠唠叨叨。最近这一个月来，我学会了唠唠叨叨，一连几昼夜躺在屋角里，想啊想的……"
        "尽想些戈罗赫王的事。”"
    )
    rows = split_rows(quote, options=SplitOptions(max_chars=80))
    texts = [row["text"] for row in rows]
    assert len(texts) > 1
    assert {row["kind"] for row in rows} == {"dialogue"}
    # 只切不改字：各行合起来仍是原文。
    assert "".join(texts) == quote
    # 省略号跟着它前面的句子，不掉队成一行「…」——那样的行念不出声，还白占一次生成。
    assert all(len(text) <= 80 for text in texts)
    assert any("什么也没抓到……" in text for text in texts)
    assert all(any(char.isalnum() for char in text) for text in texts)


def test_long_quote_packs_sentences_until_the_cap() -> None:
    """长台词按行容量打包，不是一句一行；收口引号留在最后一行。"""
    from app.producers.builtin.audiobook.rules import SplitOptions

    quote = "“" + "这是一句用来测试打包的话。" * 10 + "最后一句。”"
    rows = split_rows(quote, options=SplitOptions(max_chars=80))
    assert len(rows) <= 3
    assert {row["kind"] for row in rows} == {"dialogue"}
    assert all(len(row["text"]) <= 80 for row in rows)
    assert "".join(row["text"] for row in rows) == quote
    assert rows[-1]["text"].endswith("”")


def test_split_report_flags_lost_characters() -> None:
    """行合计与正文对不上要说出来：这是"只切不改字"的守门人。"""
    from app.producers.builtin.audiobook.rules import SplitOptions, check_split

    text = "甲甲甲。乙乙乙。"
    rows = [{"kind": "narration", "speaker": "旁白", "text": "甲甲甲。"}]
    report = check_split(text, rows, SplitOptions())
    assert report["rows_chars"] < report["chars"]
    assert any("对不上" in warning for warning in report["warnings"])

    # 正常拆句不报：行合计就是正文（去空白）。
    healthy = split_rows(text)
    clean = check_split(text, healthy, SplitOptions())
    assert clean["rows_chars"] == clean["chars"]
    assert not any("对不上" in warning for warning in clean["warnings"])


def test_merge_speakers_updates_rows(tmp_path) -> None:
    """合并角色：跨章把旧名字统一成目标名，旧音频标过期（留着、不删）、章节退回分析。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create(
        "样书", "第一章 甲\n“你来了。”\n“坐吧。”\n\n第二章 乙\n“走吧。”\n"
    )
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapters = service.store.chapters(project["id"])
    first = service.store.rows(chapters[0]["id"])
    service.store.set_row_annotation(first[0]["id"], "老人", "平静")
    service.store.set_row_annotation(first[1]["id"], "老者", "平静")
    second = service.store.rows(chapters[1]["id"])[0]
    service.store.set_row_annotation(second["id"], "老者", "平静")
    # 造一条旧音频：说话人变了，它不能算数。
    service.store.set_row_audio(first[1]["id"], "old.wav", "old-key")

    detail = service.merge_speakers(project["id"], ["老人", "老者"], "老人")

    assert [item["speaker"] for item in detail["speakers"]] == ["老人"]
    assert all(row["speaker"] == "老人" for row in service.store.rows(chapters[0]["id"]))
    assert service.store.rows(chapters[1]["id"])[0]["speaker"] == "老人"
    merged = service.store.row(first[1]["id"])
    # 文件与指纹都留着（改名回去还能复活），只是状态不再算「已生成」。
    assert merged["audio"] == "old.wav" and merged["audio_key"] == "old-key"
    assert merged["status"] == "stale"
    assert merged["speaker_edited"] == 1
    assert service.store.chapter(project["id"], chapters[0]["id"])["stage"] == "analysis"


def test_merge_speakers_rejects_bad_input(tmp_path) -> None:
    """旁白不能参与、少于两个角色不合并、不存在的名字要报出来。"""
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    row = service.store.rows(service.store.chapters(project["id"])[0]["id"])[0]
    service.store.set_row_annotation(row["id"], "老人", "平静")

    with pytest_mod.raises(ProducerError, match="至少选两个"):
        service.merge_speakers(project["id"], ["老人"], "老人")
    with pytest_mod.raises(ProducerError, match="旁白"):
        service.merge_speakers(project["id"], ["老人", "旁白"], "旁白")
    with pytest_mod.raises(ProducerError, match="不在本书里"):
        service.merge_speakers(project["id"], ["老人", "不存在"], "老人")


def test_merge_speakers_endpoint(client) -> None:
    """接口：POST /speakers/merge 把两行并成一个名字后返回详情。"""
    created = client.post(
        "/api/v1/producers/audiobook/projects",
        json={"name": "样书", "text": "第一章 甲\n“你来了。”\n“坐吧。”\n"},
    ).json()
    base = f"/api/v1/producers/audiobook/projects/{created['id']}"
    client.post(f"{base}/split", json={"pattern_key": "general"})
    client.post(f"{base}/steps/sentences")
    chapters = client.get(base).json()["chapters"]
    rows = client.get(f"{base}/chapters/{chapters[0]['id']}").json()["rows"]
    client.put(
        f"{base}/rows/batch",
        json={"ids": [rows[0]["id"]], "speaker": "老人"},
    )
    client.put(
        f"{base}/rows/batch",
        json={"ids": [rows[1]["id"]], "speaker": "老者"},
    )

    detail = client.post(
        f"{base}/speakers/merge", json={"names": ["老人", "老者"], "target": "老人"}
    ).json()
    assert [item["speaker"] for item in detail["speakers"]] == ["老人"]


def test_settings_roundtrip_and_mask(client) -> None:
    saved = client.put("/api/v1/producers/audiobook/settings", json={"analysis_batch": 12}).json()
    assert saved["values"]["analysis_batch"] == 12

    saved = client.put(
        "/api/v1/producers/audiobook/settings",
        json={
            "analysis_granularity": "chapter",
            "analysis_concurrency": 4,
            "analysis_context": "window",
            "analysis_context_lines": 12,
            "merge_format": "wav",
            "bgm_normalize": False,
        },
    ).json()
    assert saved["values"]["analysis_granularity"] == "chapter"
    assert saved["values"]["analysis_concurrency"] == 4
    assert saved["values"]["analysis_context"] == "window"
    assert saved["values"]["analysis_context_lines"] == 12
    assert saved["values"]["merge_format"] == "wav"
    assert saved["values"]["bgm_normalize"] is False

    reset = client.put("/api/v1/producers/audiobook/settings", json={"_reset": True}).json()
    assert reset["values"]["analysis_batch"] == 120
    assert reset["values"]["analysis_granularity"] == "batch"
    assert reset["values"]["analysis_concurrency"] == 4
    assert reset["values"]["analysis_context"] == "chapter"
    assert reset["values"]["analysis_context_lines"] == 80
    assert reset["values"]["concurrency"] == 4
    assert reset["values"]["chapter_lead_ms"] == 3000
    assert reset["values"]["chapter_tail_ms"] == 5000
    assert reset["values"]["merge_format"] == "m4a"
    assert reset["values"]["bgm_normalize"] is True


def test_invalid_setting_is_a_bad_request(client) -> None:
    response = client.put("/api/v1/producers/audiobook/settings", json={"concurrency": 99})
    assert response.status_code == 400
    assert "concurrency" in response.json()["error"]["message"]


def test_project_lifecycle(client) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    )
    assert created.status_code == 200
    project = created.json()
    # 新建只落原文：切几章、怎么切，由用户在分章那一步决定。
    assert project["stats"]["chapters"] == 0
    assert project["source"]["chars"] > 0

    preview = client.post(
        f"/api/v1/producers/audiobook/projects/{project['id']}/split/preview",
        json={"pattern_key": "general"},
    ).json()
    assert preview["count"] == 2
    assert preview["conservation"]["ok"] is True

    split = client.post(
        f"/api/v1/producers/audiobook/projects/{project['id']}/split",
        json={"pattern_key": "general"},
    ).json()
    assert split["stats"]["chapters"] == 2
    assert split["pattern_key"] == "general"

    sentences = client.post(
        f"/api/v1/producers/audiobook/projects/{project['id']}/steps/sentences"
    )
    assert sentences.status_code == 200
    detail = sentences.json()
    assert detail["stats"]["rows"] == 5

    chapter = detail["chapters"][0]
    rows = client.get(
        f"/api/v1/producers/audiobook/projects/{project['id']}/chapters/{chapter['id']}"
    ).json()["rows"]
    assert rows[0]["text"] == "少年推门而入。"

    assert client.delete(f"/api/v1/producers/audiobook/projects/{project['id']}").status_code == 200
    assert client.get("/api/v1/producers/audiobook/overview").json()["projects"] == []


def test_unknown_producer_reports_a_reason(client) -> None:
    response = client.get("/api/v1/producers/missing")
    assert response.status_code == 400
    assert "missing" in response.json()["error"]["message"]


def test_row_edit_updates_speaker_and_emotion(client) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/split",
        json={"pattern_key": "general"},
    )
    detail = client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/steps/sentences"
    ).json()
    chapter = detail["chapters"][0]
    rows = client.get(
        f"/api/v1/producers/audiobook/projects/{created['id']}/chapters/{chapter['id']}"
    ).json()["rows"]
    dialogue = next(row for row in rows if row["kind"] == "dialogue")

    updated = client.put(
        f"/api/v1/producers/audiobook/projects/{created['id']}/rows/{dialogue['id']}",
        json={"speaker": "老人", "emotion": "开心"},
    ).json()
    assert (updated["speaker"], updated["emotion"]) == ("老人", "开心")
    # 类型即角色：有角色名就是对白。
    assert updated["kind"] == "dialogue"
    # 类型即角色：有角色名就是对白。
    assert updated["kind"] == "dialogue"

    detail = client.get(f"/api/v1/producers/audiobook/projects/{created['id']}").json()
    # 还有没分析的对白行，占位“对话”仍在；被改成“老人”的那一行进角色表。
    assert {"老人", "对话"} <= {item["speaker"] for item in detail["speakers"]}
    # 情绪候选由后端下发，前端不维护第二份词表。
    assert "开心" in client.get("/api/v1/producers/audiobook/overview").json()["emotions"]


def test_broken_text_is_flagged_on_rows_and_chapters(client) -> None:
    """「需校对」的行标记与章节缺字计数由后端同一句判据给出。"""
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/split",
        json={"pattern_key": "general"},
    )
    detail = client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/steps/sentences"
    ).json()
    chapter = detail["chapters"][0]
    rows = client.get(
        f"/api/v1/producers/audiobook/projects/{created['id']}/chapters/{chapter['id']}"
    ).json()["rows"]
    assert all(not row["broken"] for row in rows)

    target = rows[0]
    updated = client.put(
        f"/api/v1/producers/audiobook/projects/{created['id']}/rows/{target['id']}",
        json={"text": '少年推门而入。width="2"'},
    ).json()
    assert updated["broken"] is True

    chapter_rows = client.get(
        f"/api/v1/producers/audiobook/projects/{created['id']}/chapters/{chapter['id']}"
    ).json()["rows"]
    assert next(row for row in chapter_rows if row["id"] == target["id"])["broken"] is True

    refreshed = client.get(f"/api/v1/producers/audiobook/projects/{created['id']}").json()
    counts = {item["id"]: item["broken"] for item in refreshed["chapters"]}
    assert counts[chapter["id"]] == 1
    assert refreshed["stats"]["broken"] == 1


def test_detail_source_stats_and_meta_without_text(tmp_path) -> None:
    """详情里的原文统计与原文一致；项目元数据不带原文（它可能是几 MB）。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    text = "第一章 起\n\n少年推门而入。\n"
    service = AudiobookService(tmp_path, lambda: AudiobookSettings())
    created = service.create("样书", text)

    detail = service.detail(created["id"])
    # 11 = 「第一章起」+「少年推门而入。」；空行不算段落。
    assert detail["source"] == {"chars": 11, "paragraphs": 2}

    meta = service.store.project_meta(created["id"])
    assert meta and "source" not in meta
    assert meta["name"] == "样书"
    # 需要原文的路径仍走 project()：两边的元数据必须一致。
    assert {k: v for k, v in meta.items()} == {
        k: v for k, v in service.store.project(created["id"]).items() if k != "source"
    }


def test_voice_sample_policy_roundtrip(client) -> None:
    """角色音色的样例策略：两种模式，固定模式必须带样例，其它值一律 400。"""
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    base = f"/api/v1/producers/audiobook/projects/{created['id']}/voices"

    # 不传策略 = 按情绪匹配（老调用方的默认）。
    detail = client.put(
        base,
        json={"speaker": "旁白", "engine": "breeze_tts", "voice_id": "v1", "voice_name": "旁白音"},
    ).json()
    assert detail["voices"]["旁白"]["sample_mode"] == "emotion"

    fixed = client.put(
        base,
        json={
            "speaker": "旁白",
            "engine": "breeze_tts",
            "voice_id": "v1",
            "voice_name": "旁白音",
            "sample_mode": "fixed",
            "sample_file": "03_默认.wav",
        },
    ).json()
    assert fixed["voices"]["旁白"]["sample_file"] == "03_默认.wav"

    # 按情绪匹配时把固定样例清掉，免得旧值藏着不生效。
    emotion = client.put(
        base,
        json={
            "speaker": "旁白",
            "engine": "breeze_tts",
            "voice_id": "v1",
            "voice_name": "旁白音",
            "sample_mode": "emotion",
            "sample_file": "03_默认.wav",
        },
    ).json()
    assert emotion["voices"]["旁白"]["sample_file"] == ""

    assert client.put(base, json={"speaker": "旁白", "sample_mode": "nope"}).status_code == 400
    assert (
        client.put(base, json={"speaker": "旁白", "sample_mode": "fixed"}).status_code == 400
    )


def test_batch_assigns_the_narrator(client, seed_platform_voice, monkeypatch) -> None:
    """旁白没有名字，但同样是可指定的一行：批量接口接受它，不再当成「角色不在这本书里」。

    测试数据目录里没有真实凭据，``minimax`` 平时不就绪；这里只声明它能接平台音色，
    与应用前的复核（音色还在、能落到这台引擎）走同一条路。
    """
    from app.producers.builtin.audiobook.assist import voice_assign as voice_assign_task

    monkeypatch.setattr(
        voice_assign_task,
        "engine_caps",
        lambda: {"minimax": {"ready": True, "reference": True, "design": True}},
    )
    voice = seed_platform_voice()
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    base = f"/api/v1/producers/audiobook/projects/{created['id']}"
    client.post(f"{base}/split", json={"pattern_key": "general"})
    client.post(f"{base}/steps/sentences", json={})

    data = client.put(
        f"{base}/voices/batch",
        json={
            "assignments": [
                {
                    "speaker": "旁白",
                    "engine": "minimax",
                    "voice_id": voice.id,
                    "mode": "preset",
                }
            ]
        },
    ).json()

    assert data["applied"] == 1
    assert data["skipped"] == []
    assert data["detail"]["voices"]["旁白"]["voice_id"] == voice.id


def test_chapter_voices_apply_to_the_whole_book(
    client, seed_platform_voice, monkeypatch
) -> None:
    """没识别出角色时按章分配：只为第一章指定，其余章沿用这颗音色。"""
    from app.producers.builtin.audiobook.assist import voice_assign as voice_assign_task

    monkeypatch.setattr(
        voice_assign_task,
        "engine_caps",
        lambda: {"minimax": {"ready": True, "reference": True, "design": True}},
    )
    voice = seed_platform_voice()
    created = client.post(
        "/api/v1/producers/audiobook/projects",
        json={"name": "样书", "text": "第一章 甲\n他推门。\n\n第二章 乙\n天黑以后。\n"},
    ).json()
    base = f"/api/v1/producers/audiobook/projects/{created['id']}"
    client.post(f"{base}/split", json={"pattern_key": "general"})
    detail = client.post(f"{base}/steps/sentences", json={}).json()
    assert detail["speakers"] == []
    first, second = detail["chapters"]

    data = client.put(
        f"{base}/chapter-voices",
        json={
            "assignments": [
                {
                    "chapter_id": first["id"],
                    "engine": "minimax",
                    "voice_id": voice.id,
                    "mode": "preset",
                }
            ]
        },
    ).json()

    table = data["detail"]["chapter_voices"]
    assert table[first["id"]]["voice_id"] == voice.id
    # 只为一章指定，其余章沿用同一颗音色。
    assert table[second["id"]]["voice_id"] == voice.id


def test_chapter_voice_keeps_other_chapters_until_roles_exist(tmp_path) -> None:
    """改一章只改这一章；书里出现具名角色后，分配表回到按角色。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n\n第二章 乙\n天黑以后。\n")
    project_id = project["id"]
    service.split_project(project_id, "general")
    service.split_sentences(project_id)
    first, second = service.store.chapters(project_id)
    first_rows = service.store.rows(first["id"])
    second_rows = service.store.rows(second["id"])
    for row in first_rows + second_rows:
        service.store.set_row_audio(row["id"], "rows/a.wav", "k")

    service.store.set_chapter_voices(
        project_id,
        [{"chapter_id": first["id"], "engine": "e", "voice_id": "v1", "voice_name": "甲"}],
    )
    changed = service.store.set_chapter_voices(
        project_id,
        [{"chapter_id": second["id"], "engine": "e", "voice_id": "v2", "voice_name": "乙"}],
    )
    service.store.invalidate_chapters(project_id, changed)

    table = service.store.chapter_voice_map(project_id)
    assert table[first["id"]]["voice_id"] == "v1"
    assert table[second["id"]]["voice_id"] == "v2"
    # 只作废改过的那一章，另一章的旧音频仍算数。
    assert all(service.store.row(row["id"])["status"] == "audio" for row in first_rows)
    assert all(service.store.row(row["id"])["status"] == "stale" for row in second_rows)

    mapping = service.mapping_for(
        second_rows[0],
        service.store.voice_map(project_id),
        service.chapter_voice_rows(project_id),
    )
    assert mapping["voice_id"] == "v2"

    # 出现具名角色：按章音色不再生效，分配表改回按角色。
    service.store.set_row_annotation(first_rows[0]["id"], "少年", "")
    assert service.chapter_voice_rows(project_id) == {}
    assert "少年" in {item["speaker"] for item in service.store.speakers(project_id)}


def test_row_params_are_per_row_and_validated(client) -> None:
    """行参数：只落在这一行上，空串当没设，越界与未知键一律 400。"""
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/split",
        json={"pattern_key": "general"},
    )
    detail = client.post(
        f"/api/v1/producers/audiobook/projects/{created['id']}/steps/sentences"
    ).json()
    chapter = detail["chapters"][0]
    base = f"/api/v1/producers/audiobook/projects/{created['id']}/rows"
    rows = client.get(
        f"/api/v1/producers/audiobook/projects/{created['id']}/chapters/{chapter['id']}"
    ).json()["rows"]
    first, second = rows[0], rows[1]

    meta = client.get(f"{base}/{first['id']}/params").json()
    assert [group["key"] for group in meta["groups"]] == ["expression", "silence"]
    # 没分配音色时不知道是哪台引擎：全部按可用给，保存不被堵住。
    assert all(field["supported"] for field in meta["fields"])
    assert first["params"] == {}

    saved = client.put(
        f"{base}/{first['id']}/params",
        json={"params": {"instruction": "压低嗓音", "guidance_scale": 0.6, "speed": ""}},
    ).json()
    assert saved["params"] == {"instruction": "压低嗓音", "guidance_scale": 0.6}

    other = client.get(f"{base}/{second['id']}/params").json()
    assert other["fields"] and all(field["value"] is None for field in other["fields"])

    # 存过的值要能读回来：弹框照每个 field 的 value 回填（已生成过的音频再打开，
    # 看见的就是当时用的那套参数）。这条契约从前没被钉住，前端读错了键一直没红。
    again = client.get(f"{base}/{first['id']}/params").json()
    got = {field["key"]: field["value"] for field in again["fields"]}
    assert got["instruction"] == "压低嗓音"
    assert got["guidance_scale"] == 0.6
    assert got["speed"] is None, "空串当没设"

    assert client.put(f"{base}/{first['id']}/params", json={"params": {"speed": 9}}).status_code == 400
    assert client.put(f"{base}/{first['id']}/params", json={"params": {"nope": 1}}).status_code == 400


def test_long_subtitle_is_split_into_short_cues() -> None:
    """长对白切成几条短 cue：总字数不变、时间连续、总时长不变。"""
    from app.producers.builtin.audiobook.cues import split_cues, split_subtitle_text

    text = "甲" * 20 + "，" + "乙" * 20 + "。" + "丙" * 20
    parts = split_subtitle_text(text, 32)
    assert len(parts) > 1
    assert all(len(part) <= 32 for part in parts)
    assert "".join(parts) == text

    cues = split_cues([(0.0, 6.0, text)], 32)
    assert len(cues) == len(parts)
    assert cues[0][0] == 0.0
    assert abs(cues[-1][1] - 6.0) < 1e-6
    assert all(abs(cues[i][1] - cues[i + 1][0]) < 1e-6 for i in range(len(cues) - 1))


def test_merge_writes_tags_and_checks_duration(tmp_path) -> None:
    """成品带书名/章名/序号；合完量一遍时长，对不上要报出来。"""
    import subprocess

    service, project_id = _merged_project(tmp_path)
    chapter = service.store.chapters(project_id)[0]
    result = service.merge(project_id, chapter["id"])

    check = result["check"]
    assert check["checked"] is True
    assert check["ok"] is True
    assert abs(check["drift"]) <= 1.0

    probe = shutil.which("ffprobe")
    if probe:
        found = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=title,album,track",
                "-of",
                "default=nw=1",
                result["audio"],
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        assert "title=第一章 起" in found
        assert "album=样书" in found
        assert "track=1" in found


def test_book_tags_map_name_author_engine_and_source() -> None:
    """标签口径：专辑=书名，专辑艺术家=作者，作曲人=引擎，备注=来源；artist 留空。"""
    from app.producers.builtin.audiobook.merge_stage import (
        SOURCE_URL,
        _book_tags,
        _chapter_tags,
    )

    project = {"id": "abc123", "name": "样书（修订）", "author": "某甲"}
    tags = _chapter_tags(
        project, {"title": "第一章", "ordinal": 0}, ["引擎甲", "引擎乙", "引擎甲"]
    )
    assert tags["title"] == "第一章"
    assert tags["album"] == "样书（修订）"
    assert tags["album_artist"] == "某甲"
    assert tags["composer"] == "引擎甲、引擎乙"
    assert tags["comment"] == SOURCE_URL
    assert tags["track"] == "1"
    assert "artist" not in tags

    # 空值不写：没有作者不出专辑艺术家，没有引擎不出作曲人。
    bare = _book_tags({"id": "abc123", "name": "样书", "author": ""}, [])
    assert "album_artist" not in bare and "composer" not in bare
    assert bare["comment"] == SOURCE_URL


def test_engine_names_skip_rows_without_engine(tmp_path, monkeypatch) -> None:
    """引擎名只取「分配了引擎且进了成品」的行；插件已卸载时退回 key。"""
    from types import SimpleNamespace

    from app.engines import registry
    from app.producers.builtin.audiobook.merge_stage import MergeStage

    voices = {
        "旁白": {"engine": "known", "voice_id": "v1"},
        "少年": {"engine": "gone", "voice_id": "v2"},
        "未分配": {"engine": "", "voice_id": ""},
    }
    stage = MergeStage.__new__(MergeStage)
    stage.svc = SimpleNamespace(
        store=SimpleNamespace(voice_map=lambda project_id: voices),
        chapter_voice_rows=lambda project_id: {},
        mapping_for=lambda row, table, chapter_table: table.get(row.get("speaker") or "旁白"),
    )
    monkeypatch.setattr(
        registry, "list_engines", lambda: {"known": SimpleNamespace(label="已知引擎")}
    )
    rows = [{"speaker": "旁白"}, {"speaker": "旁白"}, {"speaker": "未分配"}, {"speaker": ""}]
    assert stage._engines("p1", rows, {}) == ["已知引擎"]
    assert stage._engines("p1", rows + [{"speaker": "少年"}], {}) == ["已知引擎", "gone"]


def test_export_tags_follow_the_renamed_book(tmp_path) -> None:
    """合并后改书名与作者：导出包里的标签跟着改，不再沿用合并那一刻的值。"""
    import subprocess
    import zipfile

    service, project_id = _merged_project(tmp_path)
    chapters = service.store.chapters(project_id)
    service.update_project(project_id, name="改过的书名", author="某甲")

    with zipfile.ZipFile(service.export_chapters(project_id)) as bundle:
        entry = f"001-{chapters[0]['title']}.m4a"
        staging = tmp_path / "check.m4a"
        staging.write_bytes(bundle.read(entry))

    probe = shutil.which("ffprobe")
    if not probe:
        return
    found = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format_tags",
            "-of",
            "default=nw=1",
            str(staging),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout
    assert "album=改过的书名" in found
    assert "album_artist=某甲" in found
    assert "title=第一章 起" in found
    assert "track=1" in found
    assert "genre=Audiobook" in found
    assert not [line for line in found.splitlines() if line.startswith("TAG:artist=")]


def test_merge_joins_row_audio(tmp_path) -> None:
    """合并走 ffmpeg：逐行 wav + 行间静音 → 一个 m4a。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n甲甲甲甲。\n乙乙乙乙。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])

    for row in rows:
        path = tmp_path / "audio" / f"{row['id']}.wav"
        _tone(path)
        service.store.set_row_audio(row["id"], str(path))

    result = service.merge(project["id"], chapter["id"])
    output = Path(result["audio"])
    assert output.is_file()
    assert output.stat().st_size > 1000
    assert output.suffix == ".m4a"

    if shutil.which("ffprobe"):
        assert set(result["subtitles"]) == {"srt", "lrc"}
        srt = Path(result["subtitles"]["srt"]).read_text(encoding="utf-8-sig")
        lrc = Path(result["subtitles"]["lrc"]).read_text(encoding="utf-8-sig")
        assert "-->" in srt and "甲甲甲甲。" in srt
        assert lrc.startswith("[00:03.00]")  # 默认章前 3 秒静音，时间轴一起后移
        assert service.chapter_subtitle(project["id"], chapter["id"], "srt").is_file()


def test_chapter_export(tmp_path) -> None:
    """分章导出：每章一份最终音频，条目名 ``序号-章节名``（不带书名、不带 -bgm）。"""
    import zipfile

    service, project_id = _merged_project(tmp_path)

    archive = service.export_chapters(project_id)
    assert archive.is_file()
    # 包名就是书名：不带「分章」后缀。
    assert archive.name == "样书.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        expected = ["001-第一章 起.m4a", "002-第二章 承.m4a"]
        if shutil.which("ffprobe"):
            expected = []
            for stem in ("001-第一章 起", "002-第二章 承"):
                expected += [f"{stem}.m4a", f"{stem}.srt", f"{stem}.lrc"]
        assert names == expected
        assert all(bundle.getinfo(name).file_size > 0 for name in names)


def test_export_picks_final_audio_only(tmp_path) -> None:
    """开了 BGM：包里每章只放混音版，条目名不带 -bgm 后缀。"""
    import zipfile

    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service, project_id = _merged_project(
        tmp_path, settings=lambda: AudiobookSettings(bgm_enabled=True)
    )
    chapters = service.store.chapters(project_id)
    base = service.resolve_audio(project_id, chapters[0]["audio"])
    mixed = base.with_name(f"{base.stem}-bgm{base.suffix}")
    mixed.write_bytes(b"mixed")

    with zipfile.ZipFile(service.export_chapters(project_id)) as bundle:
        audio = [name for name in bundle.namelist() if name.endswith((".m4a", ".mp3", ".wav"))]
        assert audio == ["001-第一章 起.m4a", "002-第二章 承.m4a"]
        # 有混音版的章取混音版；没有的回落原声，不出现第二份。
        assert bundle.read("001-第一章 起.m4a") == b"mixed"


def test_export_keeps_duplicate_titles_apart_by_ordinal(tmp_path) -> None:
    """章节同名时靠序号区分：序号已按章节顺序给出，不另加后缀。"""
    import zipfile

    service, project_id = _merged_project(tmp_path)
    chapters = service.store.chapters(project_id)
    service.rename_chapter(project_id, chapters[1]["id"], chapters[0]["title"])

    with zipfile.ZipFile(service.export_chapters(project_id)) as bundle:
        audio = [name for name in bundle.namelist() if name.endswith((".m4a", ".mp3", ".wav"))]
    assert audio == ["001-第一章 起.m4a", "002-第一章 起.m4a"]


def test_bgm_playlist_draws_at_random_and_plays_tracks_end_to_end(monkeypatch) -> None:
    """随机抽曲够长即停：一轮内不重复，曲库不够再洗一轮，且交界处不连撞同一首。"""
    from pathlib import Path

    from app.producers.builtin.audiobook import merge_stage as stage

    tracks = [Path(f"{name}.mp3") for name in "abcde"]
    monkeypatch.setattr(stage, "_track_seconds", lambda probe, path: 120.0)

    picked = stage._bgm_playlist(tracks, 300.0, "ffprobe")
    assert len(picked) == 3  # 120 × 3 ≥ 300，不必抽第四首
    assert len(set(picked)) == 3  # 一轮之内不重复
    assert all(item in tracks for item in picked)

    longer = stage._bgm_playlist(tracks, 700.0, "ffprobe")
    assert len(longer) == 6  # 5 首铺不满 700 秒，第二轮接着抽
    assert len(set(longer[:5])) == 5  # 第一轮把整库用完
    assert all(a != b for a, b in itertools.pairwise(longer))


def test_bgm_playlist_handles_empty_library() -> None:
    """曲库为空时返回空表，由调用方跳过混音——不能拼出 concat=n=0。"""
    from app.producers.builtin.audiobook.merge_stage import _bgm_playlist

    assert _bgm_playlist([], 120.0, "ffprobe") == []


def test_bgm_playlist_survives_unreadable_durations(monkeypatch) -> None:
    """读不出曲目时长时按 3 分钟估，抽样仍能推进——不能因为时长为 0 卡死。"""
    from pathlib import Path

    from app.producers.builtin.audiobook import merge_stage as stage

    monkeypatch.setattr(stage, "_track_seconds", lambda probe, path: 0.0)
    picked = stage._bgm_playlist([Path("a.mp3")], 100.0, "ffprobe")
    assert len(picked) == 1


def test_bgm_playlist_randomises_between_runs() -> None:
    """每次合并重新抽签：同一组曲目两次抽样顺序不同（不是按文件名的固定顺序）。"""
    from pathlib import Path

    from app.producers.builtin.audiobook import merge_stage as stage

    tracks = [Path(f"{name}.mp3") for name in "abcdefg"]
    stage._BGM_RANDOM.seed(1)
    first = [item.name for item in stage._bgm_playlist(tracks, 600.0, "probe")]
    stage._BGM_RANDOM.seed(2)
    second = [item.name for item in stage._bgm_playlist(tracks, 600.0, "probe")]
    assert first != second


def test_bgm_playlist_filter_concats_tracks_in_order() -> None:
    """多曲相接：每曲各自拉平电平后按顺序 concat，淡变只落在成品首尾。"""
    from app.producers.builtin.audiobook.merge_stage import _bgm_playlist_filter

    graph = _bgm_playlist_filter(3, -20.0, True, 3.0, 55.0, 5.0)
    assert graph.count("loudnorm") == 3
    assert "concat=n=3:v=0:a=1" in graph
    assert graph.index("concat") < graph.index("afade=t=in")
    assert graph.rstrip().endswith("[b]")
    assert "aloop" not in graph


def test_concat_listing_escapes_a_quote_in_the_path() -> None:
    """书名/项目名带撇号时，清单这一行必须断开转义，否则 ffmpeg 只读到半条路径。"""
    from pathlib import Path

    from app.producers.builtin.audiobook.merge_stage import _concat_line

    assert _concat_line(Path("/books/Book's 1.mp3")) == "file '/books/Book'\\''s 1.mp3'"


def test_ffmetadata_values_escape_separators(tmp_path) -> None:
    """书名/章节名里的 = ; # 不转义会把元数据行拆坏。"""
    from app.producers.builtin.audiobook.merge_stage import MergeStage, _meta_escape

    assert _meta_escape("a=b;c#d\\e") == "a\\=b\\;c\\#d\\\\e"
    meta = tmp_path / "meta.txt"
    MergeStage._write_m4b_meta(
        meta,
        {"title": "A=B; C#D"},
        [{"title": "第1章=开端;上", "ordinal": 0}],
        [10.0],
    )
    text = meta.read_text(encoding="utf-8")
    assert "title=A\\=B\\; C\\#D" in text
    assert "title=第1章\\=开端\\;上" in text


def test_a_row_without_duration_aborts_subtitles(tmp_path, monkeypatch) -> None:
    """读不出时长不能静默跳过：跳过后整章字幕都会提前这一行的时长。"""
    import pytest

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook import merge_stage
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    stage = merge_stage.MergeStage(AudiobookService(tmp_path))
    monkeypatch.setattr(merge_stage, "find_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(merge_stage, "probe_duration", lambda *args, **kwargs: 0.0)
    rows = [{"ordinal": 0, "text": "第一句。"}]
    with pytest.raises(ProducerError, match="第 1 行"):
        stage._write_subtitles({}, rows, [tmp_path / "0001-r1.wav"], tmp_path, "第一章", 200)


def test_merge_format_setting(tmp_path) -> None:
    """合并格式是可选项：章节成品与 ZIP 条目都跟着换后缀。"""
    import zipfile

    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service, project_id = _merged_project(
        tmp_path, settings=lambda: AudiobookSettings(merge_format="wav")
    )
    chapter = service.store.chapters(project_id)[0]
    assert chapter["audio"].endswith(".wav")

    archive = service.export_chapters(project_id)
    with zipfile.ZipFile(archive) as bundle:
        audio_names = [
            name for name in bundle.namelist() if name.endswith((".mp3", ".wav"))
        ]
    assert audio_names == ["001-第一章 起.wav", "002-第二章 承.wav"]


def test_export_m4b_has_chapters(tmp_path) -> None:
    """整本导出：一个 m4b，章节标题与顺序来自章节表。"""
    import json
    import subprocess

    service, project_id = _merged_project(tmp_path)

    path = service.export_m4b(project_id)
    assert path.name == "样书.m4b"
    assert path.is_file() and path.stat().st_size > 1000

    probe = shutil.which("ffprobe")
    if not probe:
        return
    found = subprocess.run(
        [probe, "-v", "error", "-show_chapters", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout
    chapters = json.loads(found)["chapters"]
    assert [item["tags"]["title"] for item in chapters] == ["第一章 起", "第二章 承"]
    assert float(chapters[0]["start_time"]) == 0.0
    assert float(chapters[1]["start_time"]) > 0.0


def test_export_m4b_from_other_formats(tmp_path) -> None:
    """章节不是 AAC 时先统一重编码，再封装成 m4b。"""
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service, project_id = _merged_project(
        tmp_path, settings=lambda: AudiobookSettings(merge_format="mp3")
    )

    path = service.export_m4b(project_id)
    assert path.suffix == ".m4b"
    assert path.is_file() and path.stat().st_size > 1000


def test_import_epub_creates_chapters(client) -> None:
    """EPUB 直接落成项目与章节，不再让前端拆 zip。"""
    from test_ebooks import _epub_bytes

    response = client.post(
        "/api/v1/producers/audiobook/projects/import",
        files={"file": ("样书.epub", _epub_bytes(), "application/epub+zip")},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["name"] == "样书"
    assert [item["title"] for item in detail["chapters"]] == ["第一章 起", "第二章 承"]
    assert detail["stats"]["chapters"] == 2


def test_import_azw3_creates_chapters(client) -> None:
    """KF8 的 skeleton 直接落成章节，和 EPUB 一样不用先选分章模式。"""
    from test_ebooks import _azw3_bytes

    parts = [
        (b"<html><body><h1>Chapter 1</h1></body></html>", [b"<p>Alpha.</p>"]),
        (b"<html><body><h1>Chapter 2</h1></body></html>", [b"<p>Beta.</p>"]),
    ]
    response = client.post(
        "/api/v1/producers/audiobook/projects/import",
        files={"file": ("样书.azw3", _azw3_bytes(parts), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert [item["title"] for item in detail["chapters"]] == ["Chapter 1", "Chapter 2"]
    assert detail["stats"]["chapters"] == 2


def test_import_rejects_unknown_format(client) -> None:
    response = client.post(
        "/api/v1/producers/audiobook/projects/import",
        files={"file": ("样书.kfx", b"whatever", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "不支持" in response.json()["error"]["message"]


def test_host_has_no_reference_to_the_audiobook_plugin() -> None:
    """宿主不认识具体生产方：删掉插件目录即删掉功能。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    for path in root.rglob("*.py"):
        if "producers/builtin" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        assert "builtin.audiobook" not in text, path
        assert "AudiobookService" not in text, path


def test_synthesize_uses_voice_mapping_and_writes_files(tmp_path, monkeypatch) -> None:
    """合成只认角色映射；引擎调用被替换，验证参数与落盘。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    calls: list[dict] = []

    async def fake_synthesize(_service, **kwargs):
        calls.append(kwargs)
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    assert {call["voice_id"] for call in calls} == {"v-narrator", "v-elder"}
    rows = [
        row
        for chapter in service.store.chapters(project["id"])
        for row in service.store.rows(chapter["id"])
    ]
    assert all(row["audio"] and row["status"] == "audio" for row in rows)


def test_audio_files_are_named_by_row_id(tmp_path, monkeypatch) -> None:
    """文件名带序号方便认，但唯一性靠行 id：只按序号走，序号一挪就串音。"""
    import asyncio
    from pathlib import Path

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n旁白一。\n“对白一。”\n旁白二。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    chapter = service.store.chapters(project["id"])[0]
    for row in service.store.rows(chapter["id"]):
        stem = Path(row["audio"]).stem
        assert stem.endswith(row["id"])  # 行 id 是唯一性的那一半
        assert stem.split("-", 1)[0].isdigit()  # 前面那截是给人看的序号
        assert service.resolve_audio(project["id"], row["audio"]).is_file()


def test_row_audio_extension_is_the_format_the_engine_returned(tmp_path, monkeypatch) -> None:
    """容器由引擎决定，文件名照实说它是什么。

    从前按设置里那格「逐行音频格式」命名，而那一格从来没发给引擎：接 IndexTTS2 /
    OmniVoice 这类只出 WAV 的引擎，盘上落的是一批叫 .mp3 的 WAV。
    """
    import asyncio
    from pathlib import Path

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    assert Path(row["audio"]).suffix == ".wav"
    assert service.resolve_audio(project["id"], row["audio"]).is_file()


def test_audio_key_is_unchanged_for_projects_without_pronunciation_rules() -> None:
    """读音表一条规则都没有时不加那一维——这一条钉的是"老项目的音频不许失效"。

    有规则时才进指纹（规则改了，同一段原文的发音就变了）。指纹里的「格式」一维同理：
    它已不是设置项，值固定成旧默认值，改这一处不该让整本书重做。
    """
    from app.producers.builtin.audiobook.fingerprint import audio_key

    base = {
        "text": "你好。",
        "engine": "minimax",
        "voice_id": "v1",
        "emotion": "",
        "params": {},
        "sample": "emotion",
    }
    assert audio_key(**base) == "d1c5834ec8803cd0"
    assert audio_key(**base, revision="") == audio_key(**base)
    assert audio_key(**base, revision="abc123") != audio_key(**base)


def test_audio_key_v2_only_cares_about_what_really_changed() -> None:
    """v2 只看命中本行的读音与引擎身份；两种口径不互相冒充。"""
    from app.producers.builtin.audiobook.fingerprint import audio_key, audio_key_v2

    base = {"text": "重庆下雨。", "engine": "minimax", "voice_id": "v1", "emotion": ""}
    assert audio_key_v2(**base) == audio_key_v2(**base)
    assert audio_key_v2(**base, pron="a") != audio_key_v2(**base, pron="b")
    assert audio_key_v2(**base, identity="1.0|m1") != audio_key_v2(**base, identity="1.0|m2")
    assert audio_key_v2(**base) != audio_key(**base)


def test_pronunciation_matches_only_counts_the_hits(data_dir) -> None:
    """指纹用"这一行命中什么"，不是整张词表的版本。"""
    from app.usecase import pronunciation

    pronunciation.save([{"word": "重庆", "replacement": "崇庆"}])
    hit = pronunciation.matches("重庆下雨。")
    assert hit
    assert pronunciation.matches("无关的一句话。") == ""
    # 加一条与这行无关的规则：它的指纹不变。
    pronunciation.save(
        [
            {"word": "重庆", "replacement": "崇庆"},
            {"word": "银行", "replacement": "银航"},
        ]
    )
    assert pronunciation.matches("重庆下雨。") == hit
    # 改中了的词：必须变。
    pronunciation.save([{"word": "重庆", "replacement": "重情"}])
    assert pronunciation.matches("重庆下雨。") != hit


def test_legacy_key_is_upgraded_without_regenerating(tmp_path) -> None:
    """老指纹对得上时只换口径：音频不动、也不进生成队列，一分钱不花。"""
    from app.producers.builtin.audiobook.fingerprint import audio_key, sample_key
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.rows import row_params
    from app.usecase import pronunciation

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n旁白一。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    mapping = service.store.voice_map(project["id"])["旁白"]
    legacy = audio_key(
        text=row["text"],
        engine=mapping["engine"],
        voice_id=mapping["voice_id"],
        emotion="",
        params=row_params(row),
        sample=sample_key(mapping),
        revision=pronunciation.revision(),
    )
    rel = f"audio/{chapter['id']}/0000-{row['id']}.wav"
    path = service.project_folder(project["id"]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....")
    service.store.set_row_audio(row["id"], rel, legacy)

    plan = service.prepare_audio(project["id"])
    assert plan.tasks == []
    assert plan.skipped == 1
    saved = service.store.row(row["id"])
    assert saved["audio"] == rel
    assert saved["audio_key_v"] == 2
    assert saved["audio_key"] != legacy


def test_an_unrelated_pronunciation_rule_keeps_v2_audio(tmp_path, monkeypatch) -> None:
    """v2 生成过的行：改别的读音规则不该让它重做，改中它的才重做。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.usecase import pronunciation

    pronunciation.save([{"word": "重庆", "replacement": "崇庆"}])
    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n重庆下雨。\n无关的话。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")
    chapter = service.store.chapters(project["id"])[0]
    assert len(service.store.rows(chapter["id"])) == 2

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))
    assert {row["audio_key_v"] for row in service.store.rows(chapter["id"])} == {2}

    pronunciation.save(
        [
            {"word": "重庆", "replacement": "崇庆"},
            {"word": "银行", "replacement": "银航"},
        ]
    )
    assert service.prepare_audio(project["id"]).tasks == []

    pronunciation.save([{"word": "重庆", "replacement": "重情"}])
    plan = service.prepare_audio(project["id"])
    assert [task[1]["text"] for task in plan.tasks] == ["重庆下雨。"]


def test_changing_the_engine_identity_invalidates_v2_audio(tmp_path, monkeypatch) -> None:
    """换模型 / 升级插件后，旧产物不能再冒充新口径的产物。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.usecase import engines as engines_uc

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n旁白一。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))
    assert service.prepare_audio(project["id"]).tasks == []

    engine = engines_uc.detect_engine("minimax")
    assert engine is not None
    monkeypatch.setattr(type(engine), "version", "9.9.9")
    assert len(service.prepare_audio(project["id"]).tasks) == 1


def test_audio_overwritten_by_ordinal_shift_is_regenerated(tmp_path, monkeypatch) -> None:
    """旧库串音的自愈：同一文件被两行指着，序号对得上的那行是写入者，另一行重生成。"""
    import asyncio
    from pathlib import Path

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n旁白一。\n“对白一。”\n旁白二。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    victim, owner = rows[0], rows[1]
    # 摆成旧版的样子：两行都指着「序号 1」的文件，序号 1 的那行才是最后写进去的。
    legacy = Path(chapter["id"]) / "0001.wav"
    for row in (victim, owner):
        # 两行的键都是刚合成出来的 v2 口径；这里模拟"文件名被顶掉"，不是指纹过期。
        service.store.set_row_audio(
            row["id"], f"audio/{legacy.as_posix()}", row["audio_key"], key_version=2
        )
    target = service.project_folder(project["id"]) / "audio" / legacy
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"RIFF....")

    plan = service.audio._audio_plan(project["id"])
    tasks, skipped = plan.tasks, plan.skipped
    refreshed = {str(item[1]["id"]) for item in tasks}
    assert str(victim["id"]) in refreshed
    assert str(owner["id"]) not in refreshed
    assert skipped == len(rows) - 1


def test_synthesize_groups_rows_by_voice(tmp_path, monkeypatch) -> None:
    """先把这个角色的行生成完再换人：模型换音色要重载参考音频，逐行来回切最费。"""
    import asyncio
    import itertools

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create(
        "样书",
        "第一章\n旁白一。\n“对白一。”\n旁白二。\n“对白二。”\n旁白三。\n“对白三。”\n",
    )
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    calls: list[str] = []

    async def fake_synthesize(_service, **kwargs):
        calls.append(kwargs["voice_id"])
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    assert len(calls) == 6
    # 整轮只换一次音色；换几次就说明有多少行在来回切参考音频。
    assert sum(1 for left, right in itertools.pairwise(calls) if left != right) == 1
    assert calls.count("v-narrator") == 3 and calls.count("v-elder") == 3


def test_analysis_uses_whole_chapter_context_and_skips_narration(tmp_path, monkeypatch) -> None:
    """分析把整章正文当上下文，只让模型给对白行认人；旁白不动。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n“坐吧。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    before = service.store.rows(chapter["id"])
    # 提示语「老人说道。」也是一行旁白：拆句不改写、不删除任何文字。
    assert [row["speaker"] for row in before] == ["旁白", "对话", "对话", "旁白"]

    bodies: list[dict] = []

    async def fake_chat(body):
        bodies.append(body)
        return {
            "text": json.dumps(
                {
                    "rows": [
                        {"i": 0, "speaker": "老人", "emotion": "开心"},
                        {"i": 1, "speaker": "少年", "emotion": "平静"},
                    ]
                }
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))

    assert len(bodies) == 1
    content = bodies[0]["messages"][1]["content"]
    chapter_text, rows_text = content.rsplit("对白行：", 1)
    # 整章正文都在提示词里（含旁白），模型据此认人。
    assert "他推门。" in chapter_text and "老人说道。" in chapter_text
    payload = json.loads(rows_text)
    assert [item["text"] for item in payload] == ["“你来了。”", "“坐吧。”"]

    after = service.store.rows(chapter["id"])
    assert [row["speaker"] for row in after] == ["旁白", "老人", "少年", "旁白"]
    # 旁白既没被改名，也没被顺手改情绪。
    assert after[0]["emotion"] == ""
    assert after[0]["status"] != "audio"


def test_project_writes_source_and_chapter_files(tmp_path) -> None:
    """目录照原版落盘：source/ 原稿 + chapters/ 每章一个 txt。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n\n第二章 乙\n风进来。\n")
    folder = Path(service.store.project(project["id"])["folder"])
    assert (folder / "source" / "样书.txt").is_file()

    service.split_project(project["id"], "general")
    names = sorted(path.name for path in (folder / "chapters").glob("*.txt"))
    assert names == ["001_第一章 甲.txt", "002_第二章 乙.txt"]
    assert "他推门。" in (folder / "chapters" / "001_第一章 甲.txt").read_text(encoding="utf-8")

    chapter = service.store.chapters(project["id"])[1]
    service.rename_chapter(project["id"], chapter["id"], "第二章 丙")
    names = sorted(path.name for path in (folder / "chapters").glob("*.txt"))
    assert names == ["001_第一章 甲.txt", "002_第二章 丙.txt"]

    service.delete_chapter(project["id"], chapter["id"])
    names = sorted(path.name for path in (folder / "chapters").glob("*.txt"))
    assert names == ["001_第一章 甲.txt"]


def test_old_layout_is_migrated_once(tmp_path) -> None:
    """旧项目：目录是项目号、没有 source/chapters → 迁移成书名目录并补文件。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("旧书", "第一章 甲\n他推门。\n")
    service.split_project(project["id"], "general")
    old_folder = Path(service.store.project(project["id"])["folder"])

    # 还原成旧布局：目录改回项目号、删掉 source/chapters、标记未迁移。
    import shutil

    legacy = tmp_path / "projects" / project["id"]
    old_folder.rename(legacy)
    for name in ("source", "chapters"):
        shutil.rmtree(legacy / name, ignore_errors=True)
    service.store.set_project_folder(project["id"], str(legacy))
    service.store.set_layout_version(project["id"], 0)

    migrated = AudiobookService(tmp_path)
    folder = Path(migrated.store.project(project["id"])["folder"])
    assert folder.name == "旧书"
    assert (folder / "source" / "旧书.txt").is_file()
    assert (folder / "chapters" / "001_第一章 甲.txt").is_file()
    assert migrated.store.projects_pending_layout() == []


def test_chapter_text_is_editable(tmp_path) -> None:
    """手改章节正文：落库、行作废、退回分章；内容没变时什么都不做。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    assert service.store.rows(chapter["id"])

    saved = service.save_chapter_text(project["id"], chapter["id"], "他推门。\n“进来。”\n")
    assert saved["changed"] is True
    live = service.store.chapter(project["id"], chapter["id"])
    assert live["text"] == "他推门。\n“进来。”\n"
    assert live["stage"] == "chapters"
    assert service.store.rows(chapter["id"]) == []

    same = service.save_chapter_text(project["id"], chapter["id"], "他推门。\n“进来。”\n")
    assert same["changed"] is False


def test_clear_chapter_rows_keeps_text(tmp_path) -> None:
    """想整章改字：先清掉行，正文保留，回到可编辑状态。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    before = chapter["text"]

    service.clear_chapter_rows(project["id"], chapter["id"])
    live = service.store.chapter(project["id"], chapter["id"])
    assert service.store.rows(chapter["id"]) == []
    assert live["text"] == before
    assert live["stage"] == "chapters"


def test_row_text_edit_writes_back_to_chapter(tmp_path) -> None:
    """行文本是正文的投影：改一行等于改正文，重拆不会把改动丢掉。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = next(item for item in service.store.rows(chapter["id"]) if item["kind"] == "dialogue")

    service.edit_row(project["id"], row["id"], text="“你来啦。”")
    live = service.store.chapter(project["id"], chapter["id"])
    assert "“你来啦。”" in live["text"]
    assert "“你来了。”" not in live["text"]


def test_custom_regex_guard_rejects_nested_quantifiers() -> None:
    """自定义正则的护栏：嵌套量词直接拒，正常写法照用。"""
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.patterns import preprocess

    with pytest_mod.raises(ProducerError, match="嵌套量词"):
        preprocess("aaaa", custom_dialog=("(a+)+$", ""))
    with pytest_mod.raises(ProducerError, match="嵌套量词"):
        preprocess("aaaa", custom_bracket=r"(a*)*")
    assert preprocess("第1页（注1）", custom_bracket=r"（注\d+）").text == "第1页"


def test_custom_regex_guard_rejects_huge_text(monkeypatch) -> None:
    """自定义正则一次处理的字数有上限；内置规则不受这个上限影响。"""
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook import patterns

    monkeypatch.setattr(patterns, "MAX_CUSTOM_CHARS", 10)
    with pytest_mod.raises(ProducerError, match="超过自定义正则"):
        patterns.preprocess("x" * 11, custom_bracket=r"\d+")
    assert patterns.preprocess("x" * 11, bracket=["移除方括号数字"]).text


def test_text_processing_is_chapter_scoped_and_keeps_rows(tmp_path) -> None:
    """拆过句的章逐行改：行与角色保留，空行删掉；排除 key 带章节 + 行作用域。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create(
        "样书", "第一章 甲\n他说：“你来了。”[1]\n\n第二章 乙\n她说：“坐吧。”[2]\n"
    )
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapters = service.store.chapters(project["id"])

    preview = service.preview_text(
        project["id"], {"dialog": ["移除双引号 “ ”"], "bracket": ["移除方括号数字"]}
    )
    assert preview["scope"] == "chapters"
    assert len(preview["hits"]) == 4
    # 每处的 key 带章节 + 行作用域：两章的 #1 不会串车。
    assert len({hit["key"].split("|")[0] for hit in preview["hits"]}) == 2
    assert all(hit["line"] for hit in preview["hits"])
    assert {hit["chapter"] for hit in preview["hits"]} == {"第一章 甲", "第二章 乙"}

    keep = preview["hits"][-1]["key"]  # 第二章的 [2]
    saved = service.apply_text(
        project["id"],
        {"dialog": ["移除双引号 “ ”"], "bracket": ["移除方括号数字"], "exclude": [keep]},
    )
    live = service.store.chapters(project["id"])
    assert live[0]["text"] == "他说：你来了。"
    assert live[1]["text"] == "她说：坐吧。[2]"
    # [1] 整行被删空 → 行删掉；未命中的 [2] 行原样保留。
    assert [row["text"] for row in service.store.rows(chapters[0]["id"])] == [
        "他说：",
        "你来了。",
    ]
    assert [row["text"] for row in service.store.rows(chapters[1]["id"])] == [
        "她说：",
        "坐吧。",
        "[2]",
    ]
    assert all(item["stage"] == "analysis" for item in live)
    assert saved["updated_rows"] == 3


def test_text_processing_keeps_rows_and_untouched_audio(tmp_path) -> None:
    """改一行只作废这一行的音频（留着标过期）；没改到的行连同音频、行 id 都不动。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他说：“你来了。”\n他笑了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    dialogue = next(row for row in rows if row["kind"] == "dialogue")
    narration = next(row for row in rows if row["text"] == "他笑了。")
    for row in rows:
        path = tmp_path / "audio" / f"{row['id']}.wav"
        _tone(path)
        service.store.set_row_audio(row["id"], str(path), f"key-{row['id']}")
    service.store.set_chapter_stage(chapter["id"], "audio", audio="chapter.mp3")

    service.apply_text(project["id"], {"dialog": ["移除双引号 “ ”"]})

    live = service.store.chapters(project["id"])[0]
    assert live["stage"] == "analysis"
    assert live["audio"] == ""
    after = {row["id"]: row for row in service.store.rows(chapter["id"])}
    assert after[dialogue["id"]]["text"] == "你来了。"
    # 文本变了：旧音频留着（改回原文还能复活），只是不再算「已生成」。
    assert after[dialogue["id"]]["audio"] == str(tmp_path / "audio" / f"{dialogue['id']}.wav")
    assert after[dialogue["id"]]["status"] == "stale"
    assert after[narration["id"]]["text"] == "他笑了。"
    assert after[narration["id"]]["audio"] == str(
        tmp_path / "audio" / f"{narration['id']}.wav"
    )
    assert after[narration["id"]]["audio_key"] == f"key-{narration['id']}"


def test_text_processing_without_chapters_edits_source(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "他说：“你来了。”\n")
    preview = service.preview_text(project["id"], {"dialog": ["移除双引号 “ ”"]})
    assert preview["scope"] == "source"
    service.apply_text(project["id"], {"dialog": ["移除双引号 “ ”"]})
    assert service.store.source_text(project["id"]) == "他说：你来了。\n"


def test_split_options_modes_and_dialogue(tmp_path) -> None:
    """三档拆分、引号选择、对话开关与同一段内的短句合并。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.rules import SplitOptions

    service = AudiobookService(tmp_path)
    text = "第一章\n\n他说：“你来了。”\n「坐吧。」\n" + "长" * 300 + "\n"
    project = service.create("样书", text)
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]

    options = SplitOptions(mode="paragraph", max_chars=200)
    rows = split_rows(chapter["text"], options=options)
    # 默认中文双引号：第一段有对白；「坐吧。」要显式选直角引号才认。
    assert any(row["kind"] == "dialogue" for row in rows)
    assert all(len(row["text"]) <= 200 for row in rows)
    assert split_rows("「坐吧。」")[0]["kind"] == "narration"
    assert split_rows("「坐吧。」", options=SplitOptions(quote="corner"))[0]["kind"] == "dialogue"
    assert split_rows("「坐吧。」", options=SplitOptions(quote="auto"))[0]["kind"] == "dialogue"

    quiet = split_rows(chapter["text"], options=SplitOptions(dialogue=False))
    assert all(row["kind"] == "narration" for row in quiet)
    # 提示语既不改写也不删除：同一行里的前缀留在对白行，独立成行的留在旁白行。
    assert any("他说：“你来了。”" in row["text"] for row in quiet)
    assert split_rows("老人说道。")[0]["text"] == "老人说道。"
    with_dialogue = split_rows("“你来了。”\n老人说道。\n")
    assert [row["text"] for row in with_dialogue] == ["“你来了。”", "老人说道。"]
    assert [row["kind"] for row in with_dialogue] == ["dialogue", "narration"]
    # 提示语与台词同一行：拆成旁白 + 对白两条，字一个不少。
    prefixed = split_rows("老人说道：“你来了。”")
    assert [(row["kind"], row["text"]) for row in prefixed] == [
        ("narration", "老人说道："),
        ("dialogue", "“你来了。”"),
    ]

    # 按句模式也在同一段内并短句，但不跨段。
    assert split_rows("一。二。三。", options=SplitOptions(max_chars=50))[0]["text"] == "一。二。三。"
    assert [row["text"] for row in split_rows("一。二。三。", options=SplitOptions(max_chars=3))] == [
        "一。",
        "二。",
        "三。",
    ]
    assert len(split_rows("一。\n二。", options=SplitOptions(max_chars=50))) == 2
    # 同一对引号内的短句可以并；两次独立对白不并。
    assert split_rows("“你来了。快坐。”", options=SplitOptions(max_chars=50))[0]["text"] == (
        "“你来了。快坐。”"
    )
    assert len(split_rows("“你好。”“再见。”", options=SplitOptions(max_chars=50))) == 2

    # 不拆：整章固定一行旁白，即使正文里有引号也不判对白。
    whole = split_rows(chapter["text"], options=SplitOptions(mode="none"))
    assert len(whole) == 1
    assert whole[0]["kind"] == "narration"
    assert "他说" in whole[0]["text"]


def test_split_quote_migrates_from_legacy_pairs(tmp_path) -> None:
    """旧库没有引号写法：显式选过的一对按对迁移，空对保持原来的自动识别。"""
    import sqlite3

    from app.producers.builtin.audiobook.store import AudiobookStore

    AudiobookStore(tmp_path)
    with sqlite3.connect(tmp_path / "projects.db") as conn:
        conn.execute("ALTER TABLE projects ADD COLUMN split_open TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE projects ADD COLUMN split_close TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE projects DROP COLUMN split_quote")
        for pid, opens, closes in (("corner", "「", "」"), ("auto", "", "")):
            conn.execute(
                "INSERT INTO projects(id, name, source, split_open, split_close,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (pid, pid, "", opens, closes, "2026-01-01", "2026-01-01"),
            )

    migrated = AudiobookStore(tmp_path)
    assert migrated.project("corner")["split_quote"] == "corner"
    assert migrated.project("auto")["split_quote"] == "auto"


def test_split_quality_reports_unpaired_quotes() -> None:
    from app.producers.builtin.audiobook.rules import SplitOptions, check_split

    rows = [{"kind": "dialogue", "speaker": "对话", "text": "“你来了。"}]
    report = check_split("“你来了。", rows, SplitOptions())
    assert any("引号不配对" in item for item in report["warnings"])

    # 关闭对话识别后不看引号，也不该报引号警告。
    quiet = check_split(
        "“你来了。",
        [{"kind": "narration", "speaker": "旁白", "text": "“你来了。"}],
        SplitOptions(dialogue=False),
    )
    assert not any("引号不配对" in item for item in quiet["warnings"])

    # 自动模式遇到一本书混用两种引号：明确提示，建议选定一种。
    mixed = check_split(
        "“你来了。”\n「坐吧。」",
        rows,
        SplitOptions(quote="auto"),
    )
    assert any("多种对白引号" in item for item in mixed["warnings"])


def test_resplit_keeps_audio_for_unchanged_rows(tmp_path) -> None:
    """重拆时文本没变的行连音频一起认亲，阶段不被无故退回。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    path = tmp_path / "audio" / "keep.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"RIFF")
    service.store.set_row_audio(rows[0]["id"], str(path), "k1")
    service.store.set_chapter_stage(chapter["id"], "audio")

    service.resplit_chapter(project["id"], chapter["id"])
    live = service.store.chapters(project["id"])[0]
    after = service.store.rows(chapter["id"])
    assert live["stage"] == "audio"
    assert after[0]["audio"] == str(path)
    assert after[0]["status"] == "audio"


def test_split_all_matches_resplit_and_keeps_audio(tmp_path) -> None:
    """两个拆句入口同一套规则：同一章从哪颗按钮进来，行、角色、音频都一样。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    path = tmp_path / "audio" / "keep.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"RIFF")
    service.store.set_row_audio(rows[0]["id"], str(path), "k1")
    service.store.update_row(rows[1]["id"], speaker="老人")

    service.resplit_chapter(project["id"], chapter["id"])
    by_chapter = [
        (row["text"], row["speaker"], row["audio"]) for row in service.store.rows(chapter["id"])
    ]

    # 默认范围 = 全部章节：已经拆过的章也要重切，且结果与“重拆本章”一致。
    payload = service.split_sentences(project["id"])
    by_all = [
        (row["text"], row["speaker"], row["audio"]) for row in service.store.rows(chapter["id"])
    ]
    assert by_all == by_chapter
    assert by_all[0][2] == str(path)
    assert payload["split_report"]["chapters"] == 1
    assert payload["split_report"]["kept"] == 1


def test_split_scope_pending_only_touches_unsplit_chapters(tmp_path) -> None:
    """``scope="pending"`` 只补还没拆的章：清空行之后那一章才被重切。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n\n第二章 乙\n风进来了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    first, second = service.store.chapters(project["id"])
    service.clear_chapter_rows(project["id"], second["id"])
    assert service.store.rows(first["id"])

    payload = service.split_sentences(project["id"], "pending")
    assert payload["split_report"]["chapters"] == 1
    assert service.store.rows(second["id"])


def test_resplit_drops_stale_failure_without_audio(tmp_path) -> None:
    """没有音频的失败是上一次尝试的残影：重拆后不能继续标红。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    service.store.set_row_error(rows[0]["id"], "引擎 local 不存在。")

    service.resplit_chapter(project["id"], chapter["id"])
    after = service.store.rows(chapter["id"])
    assert all(row["status"] != "failed" for row in after)
    assert all(row["error"] == "" for row in after)


def test_voice_change_clears_failed_status(tmp_path) -> None:
    """换音色后旧的失败原因已经无效，行不能继续显示“重试”。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.store.set_row_error(row["id"], "引擎 local 不存在。")

    service.store.invalidate_speaker(project["id"], "旁白")
    after = service.store.row(row["id"])
    assert after["status"] != "failed"
    assert after["error"] == ""


def test_voice_change_invalidates_legacy_narration_rows(tmp_path) -> None:
    """旧库里旁白行的 speaker 是空串：换旁白音色时它们同样要标过期。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = next(item for item in service.store.rows(chapter["id"]) if item["speaker"] == "旁白")
    service.store.set_row_audio(row["id"], "rows/old.wav")
    # 模拟旧数据：当年旁白行不给 speaker 赋值。
    with service.store._conn() as conn:
        conn.execute("UPDATE rows SET speaker = '' WHERE id = ?", (row["id"],))

    service.store.invalidate_speaker(project["id"], "旁白")

    assert service.store.row(row["id"])["status"] == "stale"


def test_store_clears_legacy_local_engine_and_fake_failures(tmp_path) -> None:
    """旧库里的 local 引擎与“failed 但没原因”的行在启动时清理。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.store import AudiobookStore

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    service.store.set_voice(project["id"], "旁白", "local", "v1", "旁白音")
    service.store.set_row_error(rows[0]["id"], "引擎 local 不存在。可用引擎：minimax。")
    service.store.set_row_error(rows[1]["id"], "")

    reopened = AudiobookStore(tmp_path)
    assert reopened.voice_map(project["id"])["旁白"]["engine"] == ""
    after = reopened.rows(chapter["id"])
    assert all(row["status"] != "failed" for row in after)
    assert all(row["error"] == "" for row in after)


def test_voice_reason_survives_voice_and_engine_changes(tmp_path) -> None:
    """AI 写下的音色描述属于角色：换引擎、换音色、换样例都留着，只有 AI 重写才替换。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”老人说道。\n")
    project_id = project["id"]

    service.store.set_voices(
        project_id,
        [
            {
                "speaker": "老人",
                "engine": "e1",
                "voice_id": "v1",
                "voice_name": "甲",
                "reason": "老年男性，沙哑低沉",
            }
        ],
    )
    assert service.store.voice_map(project_id)["老人"]["reason"] == "老年男性，沙哑低沉"

    # 换引擎、音色不变：描述留着。
    service.store.set_voice(project_id, "老人", "e2", "v1", "甲")
    assert service.store.voice_map(project_id)["老人"]["reason"] == "老年男性，沙哑低沉"

    # 只钉一条样例：描述留着。
    service.store.set_voices(
        project_id,
        [
            {
                "speaker": "老人",
                "engine": "e2",
                "voice_id": "v1",
                "voice_name": "甲",
                "sample_mode": "fixed",
                "sample_file": "a.wav",
            }
        ],
    )
    assert service.store.voice_map(project_id)["老人"]["reason"] == "老年男性，沙哑低沉"

    # 换成另一颗音色：描述跟着角色走，不跟着音色走。
    service.store.set_voice(project_id, "老人", "e2", "v2", "乙")
    assert service.store.voice_map(project_id)["老人"]["reason"] == "老年男性，沙哑低沉"

    # 带新描述才替换。
    service.store.set_voices(
        project_id,
        [
            {
                "speaker": "老人",
                "engine": "e2",
                "voice_id": "v2",
                "voice_name": "乙",
                "reason": "换成清亮些的中年男声",
            }
        ],
    )
    assert service.store.voice_map(project_id)["老人"]["reason"] == "换成清亮些的中年男声"


def test_sentences_endpoint_takes_scope(client) -> None:
    """拆句接口默认重切全部章节；``scope="pending"`` 仍按“只补没拆的章”执行。"""
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    base = f"/api/v1/producers/audiobook/projects/{created['id']}"
    client.post(f"{base}/split", json={"pattern_key": "general"})

    first = client.post(f"{base}/steps/sentences", json={"scope": "all"}).json()
    assert first["split_report"]["chapters"] == 2
    assert first["stats"]["rows"] == 5

    # 已经拆过也照样重切：两次请求都不是空操作。
    again = client.post(f"{base}/steps/sentences").json()
    assert again["split_report"]["rows"] == 5

    # 没有待拆的章时明确报错，而不是静默返回“完成 0 行”。
    pending = client.post(f"{base}/steps/sentences", json={"scope": "pending"})
    assert pending.status_code == 400
    assert "没有需要拆句的章节" in pending.json()["error"]["message"]


def test_split_options_roundtrip(client) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    assert created["split_options"]["quote"] == "cn_double"
    saved = client.put(
        f"/api/v1/producers/audiobook/projects/{created['id']}/split-options",
        json={
            "mode": "paragraph",
            "dialogue": False,
            "quote": "corner",
            "max_chars": 260,
        },
    ).json()
    assert saved["split_options"]["mode"] == "paragraph"
    assert saved["split_options"]["dialogue"] is False
    assert saved["split_options"]["quote"] == "corner"
    assert saved["split_options"]["max_chars"] == 260

    meta = client.get("/api/v1/producers/audiobook/split/options").json()
    assert [item["key"] for item in meta["modes"]] == ["rule", "paragraph", "none"]
    assert meta["quotes"][0]["key"] == "cn_double"
    assert meta["sentence_marks"] == "。；！？!?…"
    assert "symbols" not in meta


def test_analysis_cache_skips_second_call(tmp_path, monkeypatch) -> None:
    """同一章同一批对白重跑分析：命中缓存，不再请求模型。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])

    calls = 0

    async def fake_chat(body):
        nonlocal calls
        calls += 1
        return {
            "text": json.dumps({"rows": [{"i": 0, "speaker": "老人", "emotion": "开心"}]})
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert calls == 1
    assert service.store.cache_size() == 1

    asyncio.run(service.analyze(project["id"]))
    assert calls == 1  # 第二次全走缓存
    chapter = service.store.chapters(project["id"])[0]
    speakers = [row["speaker"] for row in service.store.rows(chapter["id"])]
    assert "老人" in speakers


def test_analysis_applies_compact_rows(tmp_path, monkeypatch) -> None:
    """紧凑回复：``[行号, 说话人, 情绪, 回显]`` 直接写回行。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n“坐吧。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]

    async def fake_chat(body):
        payload = json.loads(body["messages"][1]["content"].rsplit("对白行：", 1)[1])
        return {
            "text": json.dumps(
                {
                    "rows": [
                        [0, "老人", "开心", payload[0]["text"][:6]],
                        [1, "少年", "平静", payload[1]["text"][:6]],
                    ]
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))

    saved = service.store.rows(chapter["id"])
    assert [(row["speaker"], row["emotion"]) for row in saved] == [
        ("老人", "开心"),
        ("少年", "平静"),
    ]


def test_prompt_decides_which_fields_come_back() -> None:
    """要回哪几格由提示词决定：先认预设正文，自定义的按它写了什么判。"""
    from app.producers.builtin.audiobook.prompts import (
        BUILTIN_PROMPT,
        COMBINED_PROMPT,
        INSTRUCTION_PROMPT,
        fields_for_prompt,
    )

    assert fields_for_prompt("") == ("speaker", "emotion")
    assert fields_for_prompt(BUILTIN_PROMPT) == ("speaker", "emotion")
    assert fields_for_prompt(INSTRUCTION_PROMPT) == ("speaker", "instruction")
    assert fields_for_prompt(COMBINED_PROMPT) == ("speaker", "instruction", "emotion")
    assert fields_for_prompt("给每句写一条引导指令：{rows}") == ("speaker", "instruction")
    assert fields_for_prompt("引导指令与情绪都给：{rows}") == (
        "speaker",
        "instruction",
        "emotion",
    )


def test_analysis_reads_fields_regardless_of_order(tmp_path) -> None:
    """模型把情绪写在引导指令前面也不串格：词表里的算情绪，其余算指令。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.rows import row_params

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]

    items = [[0, "老人", "平静", "沙哑低沉，收着声音", row["text"][:6]]]
    service._apply_analysis(
        [row], items, {"平静"}, ("speaker", "instruction", "emotion")
    )
    saved = service.store.row(row["id"])
    assert saved["emotion"] == "平静"
    assert row_params(saved)["instruction"] == "沙哑低沉，收着声音"


def test_analysis_with_instruction_preset_writes_row_params(tmp_path, monkeypatch) -> None:
    """「说话人+引导指令」：引导指令进行参数，情绪维持原样。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.prompts import INSTRUCTION_PROMPT
    from app.producers.builtin.audiobook.rows import row_params
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service = AudiobookService(
        tmp_path,
        lambda: AudiobookSettings(analysis_prompt=INSTRUCTION_PROMPT),
    )
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    emotion = row["emotion"]

    async def fake_chat(body):
        prompt = body["messages"][1]["content"]
        text = json.loads(prompt.rsplit("对白行：", 1)[1])[0]["text"]
        return {"text": json.dumps({"rows": [[0, "老人", "苍老沙哑，语速很慢", text[:6]]]})}

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))

    saved = service.store.row(row["id"])
    assert saved["speaker"] == "老人"
    assert saved["emotion"] == emotion, "只标引导指令时不该动情绪"
    assert row_params(saved)["instruction"] == "苍老沙哑，语速很慢"


def test_analysis_combined_fields_write_instruction_and_emotion(tmp_path) -> None:
    """引导指令与情绪一起回：数组里按预设顺序定位，各进各的格。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.rows import row_params

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]

    items = [[0, "老人", "沙哑低沉，收着声音", "愤怒", row["text"][:6]]]
    changed, skipped, names = service._apply_analysis(
        [row], items, {"愤怒"}, ("speaker", "instruction", "emotion")
    )
    assert (changed, skipped, names) == (1, 0, ["老人"])
    saved = service.store.row(row["id"])
    assert saved["speaker"] == "老人"
    assert saved["emotion"] == "愤怒"
    assert row_params(saved)["instruction"] == "沙哑低沉，收着声音"


def test_analysis_keeps_manual_instruction(tmp_path) -> None:
    """行里已有引导指令就不覆盖：那是用户自己写的，模型只补空位。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.rows import row_params

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.save_row_params(project["id"], row["id"], {"instruction": "我自己写的那句"})

    service._apply_analysis(
        [row],
        [[0, "老人", "模型写的那句", row["text"][:6]]],
        set(),
        ("speaker", "instruction"),
    )
    saved = service.store.row(row["id"])
    assert row_params(saved)["instruction"] == "我自己写的那句"
    assert saved["speaker"] == "老人"


def test_clean_instruction_folds_and_trims() -> None:
    """引导指令压成一行：去外层引号、压掉换行，超长截到上限。"""
    from app.producers.builtin.audiobook.analyze_stage import _MAX_INSTRUCTION, clean_instruction

    assert clean_instruction("“沙哑，\n  语速慢”") == "沙哑， 语速慢"
    long = clean_instruction("声" * 200)
    assert len(long) == _MAX_INSTRUCTION
    assert clean_instruction(None) == ""


def test_analysis_skips_rows_with_wrong_echo(tmp_path, monkeypatch) -> None:
    """回显对不上说明模型串位了：这一条不写回，并列进任务说明。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]

    async def fake_chat(_body):
        return {
            "text": json.dumps(
                {"rows": [[0, "老人", "开心", "完全不同"]]},
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    job = service.store.create_job(project["id"], "analysis", 0)
    asyncio.run(service.analyze(project["id"], job["id"]))

    saved = service.store.rows(chapter["id"])
    assert saved[0]["speaker"] == "对话"  # 保持占位，没被串位改坏
    assert "回显不符" in service.store.job(job["id"])["message"]


def test_analysis_prompt_carries_candidate_roles(tmp_path, monkeypatch) -> None:
    """项目里已有的名字会作为候选发进提示词，供后续批次沿用叫法。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n“坐吧。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    first = service.store.rows(chapter["id"])[0]
    service.store.set_row_annotation(first["id"], "老人", "平静")

    prompts: list[str] = []

    async def fake_chat(body):
        prompts.append(body["messages"][1]["content"])
        payload = json.loads(prompts[-1].rsplit("对白行：", 1)[1])
        return {
            "text": json.dumps(
                {
                    "rows": [
                        [item["i"], "老人", "平静", item["text"][:6]] for item in payload
                    ]
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert "已出现过的角色（优先沿用这些叫法" in prompts[0]
    assert "老人" in prompts[0].split("正文上下文：")[0]


def test_analysis_budget_follows_channel_limit() -> None:
    """回复预算跟着通道上限走，行数上限与每行预算同一条公式。"""
    from app.producers.builtin.audiobook.llmio import output_tokens, rows_per_request

    assert rows_per_request(8192) == 256
    assert rows_per_request(16384) > rows_per_request(8192)
    assert output_tokens(100, 8192) == 100 * 24 + 2048
    assert output_tokens(1000, 8192) == 8192  # 不超过通道上限


def test_analysis_pending_scope_skips_labelled_rows(tmp_path, monkeypatch) -> None:
    """范围选「只补未标注」：已经有说话人的行不送，省一次请求。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service = AudiobookService(
        tmp_path, settings=lambda: AudiobookSettings(analysis_scope="pending")
    )
    project = service.create("样书", "第一章 甲\n“你来了。”\n“坐吧。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    service.store.set_row_annotation(rows[0]["id"], "老人", "平静")

    sent: list[list[dict]] = []

    async def fake_chat(body):
        payload = json.loads(body["messages"][1]["content"].rsplit("对白行：", 1)[1])
        sent.append(payload)
        return {
            "text": json.dumps(
                {"rows": [[item["i"], "少年", "平静", item["text"][:6]] for item in payload]},
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert [[item["text"] for item in batch] for batch in sent] == [["“坐吧。”"]]

    # 全标好之后再跑：没有待标注的行，直接说明而不是发一次空请求。
    import pytest as pytest_mod

    from app.producers.base import ProducerError

    service.store.set_row_annotation(rows[1]["id"], "少年", "平静")
    with pytest_mod.raises(ProducerError, match="没有待标注的行"):
        service.start_analysis(project["id"])


def test_analysis_retries_retryable_errors(tmp_path, monkeypatch) -> None:
    """限流/超时（retryable）重试一次；密钥错这类不重试。"""
    import asyncio
    import json

    import pytest as pytest_mod

    from app.llm.base import LlmError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)

    def make(name: str) -> dict:
        project = service.create(name, "第一章 甲\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])
        return project

    calls = 0

    async def flaky(body):
        nonlocal calls
        calls += 1
        payload = json.loads(body["messages"][1]["content"].rsplit("对白行：", 1)[1])
        if calls == 1:
            raise LlmError("请求过于频繁", retryable=True)
        return {
            "text": json.dumps(
                {"rows": [[item["i"], "老人", "平静", item["text"][:6]] for item in payload]},
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", flaky)
    asyncio.run(service.analyze(make("样书甲")["id"]))
    assert calls == 2

    async def denied(_body):
        nonlocal calls
        calls += 1
        raise LlmError("密钥无效", retryable=False)

    calls = 0
    monkeypatch.setattr("app.usecase.llm.chat", denied)
    with pytest_mod.raises(LlmError, match="密钥无效"):
        asyncio.run(service.analyze(make("样书乙")["id"]))
    assert calls == 1


def test_audio_precheck_rejects_overlong_rows(tmp_path, monkeypatch) -> None:
    """超过引擎单次上限的行在开任务前就报，不截断。"""
    import pytest as pytest_mod

    from app.engines.base import EnginePlugin
    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    class Caps:
        max_chars = 3

    class Engine(EnginePlugin):
        key = "fake"
        label = "假引擎"

        def capabilities(self):
            return Caps()

    monkeypatch.setattr(
        "app.usecase.engines.list_engines", lambda: {"minimax": Engine()}
    )
    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n这一句很长。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")

    with pytest_mod.raises(ProducerError, match="超过"):
        service.start_audio(project["id"], None)


def test_analysis_uses_channel_model_and_custom_prompt(tmp_path, monkeypatch) -> None:
    """分析走通道 + 模型下拉；自定义提示词仍会把对白行带进去。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    def settings():
        return AudiobookSettings(
            analysis_channel="chan",
            analysis_model="mdl",
            analysis_prompt="只输出这些对白的标注：{rows}",
        )

    service = AudiobookService(tmp_path, settings)
    project = service.create("样书", "第一章\n他推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])

    captured = {}

    async def fake_chat(body):
        captured.update(body)
        return {"text": json.dumps({"rows": [{"i": 0, "speaker": "老人", "emotion": "中性"}]})}

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert captured["provider"] == "chan"
    assert captured["model"] == "mdl"
    # 思考开关属于引擎配置（默认关闭），分析请求不写死它。
    assert "thinking" not in captured
    prompt = captured["messages"][1]["content"]
    assert prompt.startswith("只输出这些对白的标注")
    assert "你来了" in prompt


def _analysis_project(root: Path, *, dialogue: int = 12, narration: int = 20, settings=None):
    """一章“若干叙述 + 若干对白”：正文里留一个只在前几行出现的标记。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(root, settings)
    lines = ["开头标记甲。"]
    lines += [f"叙述{index}。" for index in range(narration - 1)]
    lines += [f"“对白{index}。”" for index in range(dialogue)]
    project = service.create("样书", "第一章 甲\n" + "\n\n".join(lines) + "\n")
    service.split_project(project["id"], "general")
    # 一段一行：叙述与对白各自成行，上下文窗口才数得清。
    service.save_split_options(project["id"], {"mode": "paragraph", "max_chars": 50})
    service.split_sentences(project["id"])
    return service, project


def test_analysis_granularity_and_context_shape_each_request(tmp_path, monkeypatch) -> None:
    """整章一次 / 按批发送，以及整章正文 / 前后若干行，都真的改变请求内容。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.settings import AudiobookSettings

    def run(root: Path, settings: AudiobookSettings) -> list[str]:
        service, project = _analysis_project(root, settings=lambda: settings)
        prompts: list[str] = []

        async def fake_chat(body):
            prompts.append(body["messages"][1]["content"])
            payload = json.loads(prompts[-1].rsplit("对白行：", 1)[1])
            return {
                "text": json.dumps(
                    {
                        "rows": [
                            {"i": item["i"], "speaker": "角色", "emotion": "中性"}
                            for item in payload
                        ]
                    }
                )
            }

        monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
        asyncio.run(service.analyze(project["id"]))
        return prompts

    # 整章一次 + 前后各行：一次请求装下全部对白，远处叙述不进上下文。
    prompts = run(
        tmp_path / "chapter",
        AudiobookSettings(
            analysis_granularity="chapter",
            analysis_context="window",
            analysis_context_lines=5,
        ),
    )
    assert len(prompts) == 1
    assert "开头标记甲" not in prompts[0]
    payload = json.loads(prompts[0].rsplit("对白行：", 1)[1])
    assert len(payload) == 12

    # 按批 + 整章正文：12 行按 10 行一批拆成两次，每次都带着整章（含开头标记）。
    prompts = run(
        tmp_path / "batch",
        AudiobookSettings(
            analysis_granularity="batch",
            analysis_batch=10,
            analysis_context="chapter",
        ),
    )
    assert len(prompts) == 2
    assert all("开头标记甲" in prompt for prompt in prompts)
    first = json.loads(prompts[0].rsplit("对白行：", 1)[1])
    assert len(first) == 10


def test_analysis_runs_requests_concurrently(tmp_path, monkeypatch) -> None:
    """并发设置真的让多个请求同时在途（而不是一个接一个）。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service, project = _analysis_project(
        tmp_path,
        dialogue=25,
        settings=lambda: AudiobookSettings(analysis_batch=10, analysis_concurrency=3),
    )
    active = 0
    peak = 0

    async def fake_chat(body):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        payload = json.loads(body["messages"][1]["content"].rsplit("对白行：", 1)[1])
        return {
            "text": json.dumps(
                {"rows": [{"i": item["i"], "speaker": "角色"} for item in payload]}
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert peak == 3


def test_analysis_splits_truncated_batch_in_half(tmp_path, monkeypatch) -> None:
    """回复被截断就对半分再发：输出上限不会让整批标注作废。"""
    import asyncio
    import json

    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service, project = _analysis_project(
        tmp_path,
        settings=lambda: AudiobookSettings(analysis_granularity="chapter"),
    )
    sizes: list[int] = []
    budgets: list[int] = []

    async def fake_chat(body):
        payload = json.loads(body["messages"][1]["content"].rsplit("对白行：", 1)[1])
        sizes.append(len(payload))
        budgets.append(body["max_tokens"])
        if len(payload) > 8:
            return {"text": '{"rows":[', "finish_reason": "length"}
        return {
            "text": json.dumps(
                {"rows": [{"i": item["i"], "speaker": "角色"} for item in payload]}
            )
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    detail = asyncio.run(service.analyze(project["id"]))
    assert sizes == [12, 6, 6]
    # 回复上限按行数算：12 行 2336，6 行 2192（都含信封与思考的 2048 预留）。
    assert budgets[0] == 12 * 24 + 2048
    assert budgets[1] == 6 * 24 + 2048
    assert detail["stats"]["analyzed"] == 12


def test_merge_can_mix_bgm(tmp_path) -> None:
    """开了 BGM 且目录里有曲子：另出一版 -bgm 成品，原声版不动。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    if not shutil.which("ffmpeg"):
        import pytest as pytest_mod

        pytest_mod.skip("没有 ffmpeg")

    _service, project_id = _merged_project(tmp_path)
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir()
    _tone(bgm_dir / "theme.wav")
    mixed = AudiobookService(
        tmp_path,
        settings=lambda: AudiobookSettings(bgm_enabled=True, bgm_folder=str(bgm_dir)),
    )
    chapter = mixed.store.chapters(project_id)[0]
    result = mixed.merge(project_id, chapter["id"])
    assert result["bgm"] and Path(result["bgm"]).is_file()
    assert Path(result["audio"]).is_file()
    assert "bgm" not in Path(result["audio"]).stem
    # BGM 版跟着成品格式走，默认 M4A。
    assert Path(result["bgm"]).suffix == ".m4a"


def test_bgm_fades_follow_chapter_silence() -> None:
    """BGM 的淡入淡出落在章节前后静音里；没留静音用短过渡，读不到时长就不淡。"""
    from app.producers.builtin.audiobook.merge_stage import _bgm_fades, _bgm_filter

    assert _bgm_fades(60.0, 3000, 5000) == (3.0, 55.0, 5.0)
    # 前后没有静音：给一段短过渡——BGM 硬起硬收比淡变更像断了。
    assert _bgm_fades(60.0, 0, 0) == (1.0, 58.5, 1.5)
    # 读不到成品时长：没有长度依据，不加淡变。
    assert _bgm_fades(0.0, 3000, 5000) == (0.0, 0.0, 0.0)
    # 前后静音长过成品时等比例让位：淡出起点不为负，两段时长恰好铺满。
    fade_in, fade_start, fade_out = _bgm_fades(4.0, 3000, 5000)
    assert fade_start >= 0
    assert round(fade_in + fade_out, 6) == 4.0
    chain = _bgm_filter(-20.0, False, fade_in, fade_start, fade_out)
    assert "afade=t=in" in chain and "afade=t=out" in chain
    assert "afade" not in _bgm_filter(-20.0, False)


def test_chapter_audio_plays_bgm_version_when_enabled(tmp_path) -> None:
    """试听跟随最终产物：开着 BGM 且有 -bgm 版就播它，关掉后回原声。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    _service, project_id = _merged_project(tmp_path)
    chapter = _service.store.chapters(project_id)[0]
    base = _service.resolve_audio(project_id, chapter["audio"])
    mixed = base.with_name(f"{base.stem}-bgm{base.suffix}")
    mixed.write_bytes(base.read_bytes())

    enabled = AudiobookService(
        tmp_path, settings=lambda: AudiobookSettings(bgm_enabled=True)
    )
    assert enabled.chapter_audio(project_id, chapter["id"]) == mixed

    plain = AudiobookService(
        tmp_path, settings=lambda: AudiobookSettings(bgm_enabled=False)
    )
    assert plain.chapter_audio(project_id, chapter["id"]) == base


def test_merge_removes_stale_bgm_when_not_produced(tmp_path) -> None:
    """这次没出 BGM 版，就删掉上一轮的旧版——试听不能拿到过期音频。"""
    _service, project_id = _merged_project(tmp_path)
    chapter = _service.store.chapters(project_id)[0]
    base = _service.resolve_audio(project_id, chapter["audio"])
    stale = base.with_name(f"{base.stem}-bgm{base.suffix}")
    stale.write_bytes(base.read_bytes())

    _service.merge(project_id, chapter["id"])

    assert not stale.exists()


def test_bgm_filter_normalizes_before_gain() -> None:
    """BGM 先按 EBU R128 归一再按用户增益叠加；关掉标准化就只剩增益。"""
    from app.producers.builtin.audiobook.merge_stage import _bgm_filter

    chain = _bgm_filter(-20.0, True)
    assert chain.index("loudnorm") < chain.index("volume=-20.0dB")
    assert "aresample=44100" in chain  # loudnorm 内部走 192kHz，落回 44.1k
    assert "aloop" in chain
    plain = _bgm_filter(-12.0, False)
    assert "loudnorm" not in plain
    assert "volume=-12.0dB" in plain


def test_bgm_mix_keeps_voice_level_and_limits_peaks() -> None:
    """`amix` 默认会把人声也压 6 dB；BGM 电平由增益控制，混音时不再自动归一。"""
    from app.producers.builtin.audiobook.merge_stage import _bgm_mix

    mix = _bgm_mix()
    assert "normalize=0" in mix
    assert "alimiter=limit=0.95:level=0:latency=1" in mix


def test_host_frontend_has_no_reference_to_the_audiobook_plugin() -> None:
    """前端同样按目录插件化：宿主只认注册表与通用插槽。

    删掉 `web/src/producers/audiobook/` 后 `App.vue`、store、api 仍能构建，
    导航与工作区只是少一项——这条守住\"宿主不认识具体生产方\"。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "web" / "src"
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".js", ".ts", ".vue"}:
            continue
        if "producers/audiobook" in path.as_posix():
            continue
        if "audiobook" in path.read_text(encoding="utf-8").lower():
            offenders.append(path.relative_to(root).as_posix())
    assert not offenders, offenders


def test_analysis_respects_manual_edits(tmp_path) -> None:
    """人工改过的字段，分析不再覆盖；自动字段照常更新。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    dialogue = next(row for row in rows if row["kind"] == "dialogue")

    service.store.update_row(dialogue["id"], speaker="老人", emotion="开心")
    assert service.store.set_row_annotation(dialogue["id"], "少年", "生气") is False
    saved = service.store.row(dialogue["id"])
    assert (saved["speaker"], saved["emotion"]) == ("老人", "开心")

    narration = next(row for row in rows if row["kind"] == "narration")
    assert service.store.set_row_annotation(narration["id"], "", "平静") is True
    assert service.store.row(narration["id"])["emotion"] == "平静"


def test_analysis_marks_a_named_row_as_dialogue(tmp_path) -> None:
    """说话人一填上，`kind` 也得跟着变成 dialogue。

    界面按 `kind` 判断"这一行有人说话"（对白行高亮、筛选、对白统计）。旧的分析
    回写只写 speaker、不写 kind，于是有人说话的行在表里长得跟旁白一样——"对话行
    没高亮"就是这么来的。规则与 `update_row` 同一条：有名字就是对白。
    """
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n风从窗缝里进来。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    narration = next(
        row for row in service.store.rows(chapter["id"]) if row["kind"] == "narration"
    )

    assert service.store.set_row_annotation(narration["id"], "少年", "平静") is True
    saved = service.store.row(narration["id"])
    assert (saved["speaker"], saved["kind"]) == ("少年", "dialogue")


def test_old_rows_named_by_analysis_are_repaired_on_open(tmp_path) -> None:
    """老库里的同类遗留：打开时补成 dialogue，不必重跑分析。"""
    import sqlite3

    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.store import AudiobookStore

    service = AudiobookService(tmp_path)
    project = service.create("样书", SAMPLE)
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = next(r for r in service.store.rows(chapter["id"]) if r["kind"] == "narration")

    conn = sqlite3.connect(tmp_path / "projects.db")
    try:
        conn.execute("UPDATE rows SET speaker = '老人' WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()

    assert AudiobookStore(tmp_path).row(row["id"])["kind"] == "dialogue"


def test_rename_project_and_chapter_then_resplit(client) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects",
        json={"name": "旧名", "text": SAMPLE},
    ).json()
    project_id = created["id"]
    client.post(
        f"/api/v1/producers/audiobook/projects/{project_id}/split",
        json={"pattern_key": "general"},
    )
    client.post(f"/api/v1/producers/audiobook/projects/{project_id}/steps/sentences")
    detail = client.get(f"/api/v1/producers/audiobook/projects/{project_id}").json()
    chapter = detail["chapters"][0]
    assert chapter["rows"] > 0

    renamed = client.put(
        f"/api/v1/producers/audiobook/projects/{project_id}", json={"name": "新名"}
    ).json()
    assert renamed["name"] == "新名"
    # 只传 name 时作者保持原值（这里还是空串），不会被顺手抹成别的。
    assert renamed["author"] == ""

    authored = client.put(
        f"/api/v1/producers/audiobook/projects/{project_id}",
        json={"name": "新名", "author": "老舍"},
    ).json()
    assert authored["author"] == "老舍"
    assert authored["name"] == "新名"
    # 只传作者时书名保持原样。
    renamed = client.put(
        f"/api/v1/producers/audiobook/projects/{project_id}", json={"author": "舒庆春"}
    ).json()
    assert renamed["name"] == "新名"
    assert renamed["author"] == "舒庆春"
    # 管理栏直接读清单：书名和作者得在清单里，不另开一次详情请求。
    listed = client.get("/api/v1/producers/audiobook/overview").json()["projects"]
    assert [(item["name"], item["author"]) for item in listed] == [("新名", "舒庆春")]

    renamed = client.put(
        f"/api/v1/producers/audiobook/projects/{project_id}/chapters/{chapter['id']}",
        json={"title": "卷首"},
    ).json()
    assert renamed["chapters"][0]["title"] == "卷首"

    again = client.post(
        f"/api/v1/producers/audiobook/projects/{project_id}/chapters/{chapter['id']}/resplit"
    ).json()
    assert again["chapters"][0]["stage"] == "sentences"
    assert again["chapters"][0]["audio"] == ""
    assert again["chapters"][0]["rows"] == chapter["rows"]


def test_audio_paths_are_relative_to_the_project(tmp_path, monkeypatch) -> None:
    """音频入库用项目内相对路径：项目目录可整体搬走，设置页改根目录也不影响老项目。"""
    import asyncio
    from pathlib import Path

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    asyncio.run(service.synthesize(project["id"], None))

    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    assert not Path(row["audio"]).is_absolute()
    assert (service.project_folder(project["id"]) / row["audio"]).is_file()


def test_audio_job_skips_finished_rows_and_reports_progress(tmp_path, monkeypatch) -> None:
    """已生成且输入未变的行不再调用引擎；第二次任务只报跳过。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")
    calls: list[str] = []

    async def fake_synthesize(_service, **kwargs):
        calls.append(kwargs["voice_id"])
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    job = service.store.create_job(project["id"], "audio", 0)
    asyncio.run(service.synthesize(project["id"], None, job["id"]))
    assert len(calls) == 3
    assert service.store.job(job["id"])["done"] == 3

    calls.clear()
    again = service.store.create_job(project["id"], "audio", 0)
    asyncio.run(service.synthesize(project["id"], None, again["id"]))
    assert calls == []
    assert "已是最新" in service.store.job(again["id"])["message"]


def test_audio_job_keeps_going_after_one_row_fails(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")

    async def fake_synthesize(_service, **kwargs):
        if kwargs["voice_id"] == "v-elder":
            raise RuntimeError("引擎拒绝")
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    job = service.store.create_job(project["id"], "audio", 0)
    asyncio.run(service.synthesize(project["id"], None, job["id"]))

    saved = service.store.job(job["id"])
    assert saved["failed"] == 1 and saved["done"] == 2
    failed = [
        row
        for chapter in service.store.chapters(project["id"])
        for row in service.store.rows(chapter["id"])
        if row["status"] == "failed"
    ]
    assert len(failed) == 1 and "引擎拒绝" in failed[0]["error"]


def test_unreachable_engine_halts_the_rest_of_the_batch(tmp_path, monkeypatch) -> None:
    """连不上就停这一台：剩余行不再发请求、不标失败，只留一条日志。

    服务没起时逐行重试毫无意义——一万五千行就是一万五千次白撞，行状态还会被
    刷成满屏失败，用户既看不出原因，重试前还得先清一遍错。
    """
    import asyncio

    from app.domain.log import MemorySink
    from app.engines.base import CODE_UNREACHABLE, EngineError
    from app.infra.loghub import CHANNEL
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n风停了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    # 本机引擎的硬上限是 1：并发定死，才数得清「发了几次」。
    service.store.set_voice(project["id"], "旁白", "breeze_tts", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "breeze_tts", "v-elder", "老人音")
    calls: list[str] = []

    async def fake_synthesize(_service, **kwargs):
        calls.append(kwargs["voice_id"])
        raise EngineError("连不上 http://127.0.0.1:8080", code=CODE_UNREACHABLE)

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    sink = MemorySink(limit=100)
    CHANNEL.add_sink(sink)
    try:
        job = service.store.create_job(project["id"], "audio", 0)
        asyncio.run(service.synthesize(project["id"], None, job["id"]))
    finally:
        CHANNEL.remove_sink(sink)

    assert len(calls) == 1
    saved = service.store.job(job["id"])
    assert saved["failed"] == 0 and saved["done"] == 0
    assert "未生成" in saved["message"] and "8080" in saved["message"]
    rows = service.store.all_rows(project["id"])
    assert all(not row["error"] for row in rows)
    # 同一次失败只报一条：网关那条降成 debug，批量这条带引擎与原因。
    reported = [r for r in sink.recent(100) if "连不上" in r.message]
    assert [r.level for r in reported] == ["warning"]
    assert not [r for r in sink.recent(100) if "合成失败" in r.message]


def test_engine_disabled_after_plan_halts_the_rest_of_the_batch(tmp_path) -> None:
    """计划算完后引擎被停用：第一行撞上「未启用」就停，剩余行不再发请求。

    计划期摘行挡不住中途停用——开关随时能关，计划却是几分钟前算的。
    不在这里停车，一本书就是几千行失败配一个原因。
    """
    import asyncio

    from app.domain.log import MemorySink
    from app.infra.config import load_config, save_config
    from app.infra.loghub import CHANNEL
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n风停了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    # 本机引擎的硬上限是 1：并发定死，才数得清「发了几次」。
    service.store.set_voice(project["id"], "旁白", "breeze_tts", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "breeze_tts", "v-elder", "老人音")
    plan = service.prepare_audio(project["id"])
    assert len(plan.tasks) > 1

    config = load_config()
    config.engines_enabled = [key for key in config.engines_enabled or [] if key != "breeze_tts"]
    save_config(config)

    sink = MemorySink(limit=100)
    CHANNEL.add_sink(sink)
    try:
        job = service.store.create_job(project["id"], "audio", 0)
        asyncio.run(service.synthesize(project["id"], None, job["id"], plan))
    finally:
        CHANNEL.remove_sink(sink)

    saved = service.store.job(job["id"])
    assert saved["failed"] == 0 and saved["done"] == 0
    assert f"{len(plan.tasks)} 行未生成" in saved["message"] and "未启用" in saved["message"]
    rows = service.store.all_rows(project["id"])
    assert all(not row["error"] for row in rows)
    # 无论并发撞上几次，都只报一条：其余按「这台引擎停了」记数，不逐行报错。
    complaints = [r for r in sink.recent(100) if r.level == "warning"]
    assert len(complaints) == 1
    assert "已停止该引擎的剩余行" in complaints[0].message and "未启用" in complaints[0].message


def test_cancelled_job_does_not_call_the_engine(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v-elder", "老人音")
    calls: list[str] = []

    async def fake_synthesize(_service, **kwargs):
        calls.append(kwargs["voice_id"])
        raise AssertionError("取消之后不应调用引擎")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )
    job = service.store.create_job(project["id"], "audio", 0)
    service.store.request_cancel(job["id"])
    asyncio.run(service.synthesize(project["id"], None, job["id"]))
    assert calls == []


def test_started_audio_job_finishes_in_the_background(tmp_path, monkeypatch) -> None:
    """start_audio 立刻返回任务，进度由任务自己推进到 done。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 起\n少年推门。\n第二章 承\n风进来。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )

    async def scenario():
        job = service.start_audio(project["id"], None)
        for _ in range(100):
            await asyncio.sleep(0.01)
            current = service.store.job(job["id"])
            if current["state"] not in ("running", "cancelling"):
                return current
        return service.store.job(job["id"])

    current = asyncio.run(scenario())
    assert current["state"] == "done"
    assert current["done"] == current["total"] == 2


def test_audio_skips_rows_without_voice(tmp_path, monkeypatch) -> None:
    """没配音色的行只跳过自己：其余照常生成，任务消息说得清跳过了谁。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n少年推门。\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v-narrator", "旁白音")
    dialogue = next(item for item in rows if item["kind"] == "dialogue")
    service.store.set_row_annotation(dialogue["id"], "老人", "")

    async def fake_synthesize(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake_synthesize
    )

    async def scenario():
        job = service.start_audio(project["id"], None)
        for _ in range(100):
            await asyncio.sleep(0.01)
            current = service.store.job(job["id"])
            if current["state"] not in ("running", "cancelling"):
                return current
        return service.store.job(job["id"])

    current = asyncio.run(scenario())
    assert current["state"] == "done"
    assert current["total"] == 1  # 只排了旁白那一行
    assert "老人" in current["message"]
    assert service.store.row(dialogue["id"])["audio"] == ""


def _dead_pid() -> int:
    """一个确定已经退出的进程号（重启后那个 owner 的 pid 就长这样）。"""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_running_job_left_by_a_dead_process_is_interrupted(tmp_path) -> None:
    from app.producers.builtin.audiobook.store import AudiobookStore

    store = AudiobookStore(tmp_path)
    job = store.create_job("p1", "audio", 10)
    assert store.job(job["id"])["state"] == "running"
    # 模拟上一个进程崩溃：owner 指向一个已经不存在的 pid。
    with store._conn() as conn:
        conn.execute(
            "UPDATE jobs SET owner = ? WHERE id = ?", (f"{_dead_pid()}-dead", job["id"])
        )
    AudiobookStore(tmp_path)  # 重启，打开同一个库
    assert store.job(job["id"])["state"] == "interrupted"


def test_a_job_of_a_live_instance_is_not_touched(tmp_path) -> None:
    """另一个实例正在跑（进程活着、心跳新鲜）：第二个实例不得把它判死。

    这正是"同一行被两个批次同时合成"的来源——以前启动时一律清 running。
    """
    from app.producers.builtin.audiobook.store import AudiobookStore

    first = AudiobookStore(tmp_path)
    job = first.create_job("p1", "audio", 10)
    AudiobookStore(tmp_path)  # 第二个实例打开同一个库
    assert first.job(job["id"])["state"] == "running"


def test_row_claim_keeps_a_second_worker_off_the_same_row(tmp_path) -> None:
    from app.producers.builtin.audiobook.store import AudiobookStore

    first = AudiobookStore(tmp_path)
    second = AudiobookStore(tmp_path)
    assert first.claim_row("row-1")
    assert not second.claim_row("row-1")
    first.release_row("row-1")
    assert second.claim_row("row-1")


def test_an_expired_row_claim_can_be_taken_over(tmp_path) -> None:
    """进程崩了不会有人来 release：占位靠时间过期自己放开。"""
    from app.producers.builtin.audiobook.store import AudiobookStore

    first = AudiobookStore(tmp_path)
    assert first.claim_row("row-1")
    with first._conn() as conn:
        conn.execute("UPDATE row_claims SET claimed_at = 0 WHERE row_id = 'row-1'")
    assert first.claim_row("row-1")


def test_row_delete_renumbers_and_invalidates(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一句够长。\n第二句也长。\n第三句还行。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])

    service.delete_row(project["id"], rows[1]["id"])
    rows = service.store.rows(chapter["id"])
    assert [row["text"] for row in rows] == ["第一句够长。", "第三句还行。"]
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert service.store.chapter(project["id"], chapter["id"])["audio"] == ""


def test_row_split_and_merge(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "前半句，后半句。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    assert row["text"] == "前半句，后半句。"

    service.split_row(project["id"], row["id"], at=4)
    rows = service.store.rows(chapter["id"])
    assert [item["text"] for item in rows] == ["前半句，", "后半句。"]

    service.merge_rows(project["id"], [rows[0]["id"], rows[1]["id"]])
    rows = service.store.rows(chapter["id"])
    assert [item["text"] for item in rows] == [row["text"]]


def test_merge_rows_rejects_non_adjacent_rows(tmp_path) -> None:
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "甲甲甲甲。\n乙乙乙乙。\n丙丙丙丙。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])

    with pytest_mod.raises(ProducerError, match="相邻"):
        service.merge_rows(project["id"], [rows[0]["id"], rows[2]["id"]])
    assert [item["text"] for item in service.store.rows(chapter["id"])] == [
        item["text"] for item in rows
    ]


def test_merge_rows_never_repeats_text(tmp_path) -> None:
    """合并只把每行文本接一次：同一个 id 传两遍也不会念两遍，原文重复的句子照旧重复。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "前半句，后半句。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    service.split_row(project["id"], rows[0]["id"], at=4)
    rows = service.store.rows(chapter["id"])

    # 直接走存储层：上层会先拒掉「不相邻」，这里要保证重复 id 也不接两遍。
    service.store.merge_rows([rows[0]["id"], rows[0]["id"], rows[1]["id"]])
    merged = service.store.rows(chapter["id"])
    assert [item["text"] for item in merged] == ["前半句，后半句。"]


def test_auto_merge_joins_short_narration_and_stops_at_dialogue(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "占位正文。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]
    service.store.replace_rows(
        chapter["id"],
        [
            {"kind": "narration", "speaker": "旁白", "text": "他说。"},
            {"kind": "narration", "speaker": "旁白", "text": "外面下雨了。"},
            {"kind": "dialogue", "speaker": "对话", "text": "“谁？”"},
            {"kind": "narration", "speaker": "旁白", "text": "她抬起头。"},
        ],
    )

    service.auto_merge_rows(project["id"], chapter["id"], 40)
    rows = service.store.rows(chapter["id"])
    assert [row["text"] for row in rows] == ["他说。外面下雨了。", "“谁？”", "她抬起头。"]
    assert [row["kind"] for row in rows] == ["narration", "dialogue", "narration"]
    assert [row["ordinal"] for row in rows] == [0, 1, 2]
    assert service.store.chapter(project["id"], chapter["id"])["stage"] == "sentences"


def test_auto_merge_skips_lines_over_the_limit(tmp_path) -> None:
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "占位正文。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]
    service.store.replace_rows(
        chapter["id"],
        [
            {"kind": "narration", "speaker": "旁白", "text": "长" * 50},
            {"kind": "narration", "speaker": "旁白", "text": "短。"},
        ],
    )

    # 第一行不短于上限，不作合并的起点；第二行后面没有旁白可并 → 没有可合并的组。
    with pytest_mod.raises(ProducerError, match="没有可合并"):
        service.auto_merge_rows(project["id"], chapter["id"], 40)
    assert [row["text"] for row in service.store.rows(chapter["id"])] == ["长" * 50, "短。"]


def test_auto_merge_joins_short_line_into_long_one(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "占位正文。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]
    service.store.replace_rows(
        chapter["id"],
        [
            {"kind": "narration", "speaker": "旁白", "text": "他问。"},
            {"kind": "narration", "speaker": "旁白", "text": "长" * 80},
        ],
    )

    # 上限是「短」的判据：短旁白并入下一行，即使合计超过 40 字。
    service.auto_merge_rows(project["id"], chapter["id"], 40)
    assert [row["text"] for row in service.store.rows(chapter["id"])] == [
        "他问。" + "长" * 80
    ]


def test_auto_merge_stops_when_the_limit_is_reached(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "占位正文。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]
    service.store.replace_rows(
        chapter["id"],
        [
            {"kind": "narration", "speaker": "旁白", "text": "甲" * 20},
            {"kind": "narration", "speaker": "旁白", "text": "乙" * 20},
            {"kind": "narration", "speaker": "旁白", "text": "丙" * 20},
        ],
    )

    # 20 + 20 已到 40 字，第三行不再并入。
    service.auto_merge_rows(project["id"], chapter["id"], 40)
    assert [row["text"] for row in service.store.rows(chapter["id"])] == [
        "甲" * 20 + "乙" * 20,
        "丙" * 20,
    ]


def test_auto_merge_rejects_bad_limit(tmp_path) -> None:
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "占位正文。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]

    with pytest_mod.raises(ProducerError, match="2–1000"):
        service.auto_merge_rows(project["id"], chapter["id"], 1)


def test_batch_update_marks_rows_as_edited(tmp_path) -> None:
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "甲甲甲甲。\n乙乙乙乙。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])

    changed = service.batch_update_rows(
        project["id"], [rows[0]["id"], rows[1]["id"]], speaker="群演", emotion="平静"
    )
    assert changed["stats"]["rows"] == 2
    saved = service.store.rows(chapter["id"])
    assert len(saved) == 2
    assert len(saved) == 2
    assert {row["speaker"] for row in saved} == {"群演"}
    assert {row["kind"] for row in saved} == {"dialogue"}
    assert {row["kind"] for row in saved} == {"dialogue"}
    assert all(row["speaker_edited"] and row["emotion_edited"] for row in saved)

    # 批量改过之后，分析不再覆盖。
    service.store.set_row_annotation(saved[0]["id"], "别的人", "生气")
    assert service.store.row(saved[0]["id"])["speaker"] == "群演"


def test_batch_update_writes_once_per_batch(tmp_path, monkeypatch) -> None:
    """框选几百行改角色：读一遍、写一批，不是一行一次连接。

    旧写法每行 ``row()`` + 一条 UPDATE，500 行实测 3.6 秒；分批后 23 毫秒。
    """
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n开头。\n")
    service.split_project(project["id"], "general")
    chapter = service.store.chapters(project["id"])[0]
    service.store.replace_rows(
        chapter["id"], [{"text": f"第 {index} 句。"} for index in range(520)]
    )
    ids = [row["id"] for row in service.store.rows(chapter["id"])]
    assert len(ids) == 520  # 跨过 500 的分批边界

    calls = {"n": 0}
    original = service.store._conn

    def counted():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(service.store, "_conn", counted)
    changed = service.store.batch_update(ids, speaker="群演")

    assert changed == 520
    # 读两批 + 写两批 + 章阶段一条，连接数与批数同阶，不随行数增长。
    assert calls["n"] <= 6
    saved = service.store.rows(chapter["id"])
    assert {row["speaker"] for row in saved} == {"群演"}
    # 行状态必须落在合法值上：'analysis' 不是状态机里的词，会让这些行永远
    # 不算"已标注"，也会被原样搬进下一版。
    assert {row["status"] for row in saved} == {"analyzed"}

def test_marks_expose_engine_support_and_roles(client) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects", json={"name": "样书", "text": SAMPLE}
    ).json()
    client.put(
        f"/api/v1/producers/audiobook/projects/{created['id']}/voices",
        json={"speaker": "旁白", "engine": "minimax", "voice_id": "v1", "voice_name": "测试"},
    )
    data = client.get(f"/api/v1/producers/audiobook/projects/{created['id']}/marks").json()

    assert data["speaker_engine"]["旁白"] == "minimax"
    assert "minimax" in data["engine_marks"]
    names = {mark["name"] for mark in data["common"]}
    for group in data["engines"]:
        names |= {mark["name"] for mark in group["marks"]}
    assert "笑" in names
    assert "停顿" in data["engine_marks"]["minimax"]


def test_the_local_engine_caps_concurrency_by_what_it_declares() -> None:
    """本机单模型服务一次只跑一条——**设置里的并发再大也得压回它声明的上限**。

    实测：单条 7.0s，两条并发是 7.0s + 14.3s（第二条在服务端排队），总时长与串行
    一样而每行都更慢。这条钉的是"不许给它开两条"。
    """
    from types import SimpleNamespace

    from app.engines.base import Capabilities, EnginePlugin
    from app.producers.builtin.audiobook.audio_stage import _engine_limits

    class _Local(EnginePlugin):
        key = "breeze_tts"
        label = "BreezeTTS"

        def capabilities(self):
            return Capabilities(kind="local", max_concurrency=1)

    tasks = [
        ({}, {}, {"engine": "breeze_tts"}, "k1"),
        ({}, {}, {"engine": "breeze_tts"}, "k2"),
        ({}, {}, {"engine": "breeze_tts"}, "k3"),
    ]
    assert _engine_limits(tasks, {"breeze_tts": _Local()}, 4) == {"breeze_tts": 1}
    # 就算配置里写了更大的数，硬上限也不能被突破。
    assert _engine_limits(
        tasks, {"breeze_tts": _Local()}, 4, lambda _: SimpleNamespace(concurrency=6)
    ) == {"breeze_tts": 1}


def test_cloud_engines_follow_their_own_concurrency_not_the_project_setting() -> None:
    """云端引擎把并发写在自己的设置里：它直接生效，项目设置再大也盖不住。"""
    from types import SimpleNamespace

    from app.engines.base import Capabilities, EnginePlugin
    from app.producers.builtin.audiobook.audio_stage import _engine_limits

    class _Cloud(EnginePlugin):
        key = "minimax"
        label = "MiniMax"

        def capabilities(self):
            return Capabilities(kind="cloud")

    tasks = [
        ({}, {}, {"engine": "minimax"}, "k1"),
        ({}, {}, {"engine": "minimax"}, "k2"),
    ]
    limits = _engine_limits(
        tasks,
        {"minimax": _Cloud()},
        12,
        lambda _: SimpleNamespace(concurrency=3),
    )
    assert limits == {"minimax": 3}


def test_an_engine_without_its_own_setting_follows_the_project_setting() -> None:
    """没有自带并发设置的引擎（或插件已经不在了）由项目那个数字说了算。"""
    from types import SimpleNamespace

    from app.engines.base import Capabilities, EnginePlugin
    from app.producers.builtin.audiobook.audio_stage import _engine_limits

    class _Cloud(EnginePlugin):
        key = "minimax"
        label = "MiniMax"

        def capabilities(self):
            return Capabilities(kind="cloud")

    def config(_):
        return SimpleNamespace()

    tasks = [({}, {}, {"engine": "minimax"}, "k")]
    assert _engine_limits(tasks, {"minimax": _Cloud()}, 4, config) == {"minimax": 4}
    # 引擎插件已经不在了：同样听用户的（不知道就别说"限 1"）
    assert _engine_limits(tasks, {}, 3) == {"minimax": 3}


def test_chapter_counts_match_the_rows(tmp_path: Path) -> None:
    """左栏那几枚计数（未 / 待 / 失）与行本身同一口径，章章对得上。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", SAMPLE)
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapters = service.store.chapters(project["id"])
    first = service.store.rows(chapters[0]["id"])
    second = service.store.rows(chapters[1]["id"])
    # 拆完句默认全是「旁白」；造两条问题行：一条没定人、一条合成失败
    service.store.set_row_annotation(first[0]["id"], "老人", "中性")
    service.store.update_row(second[0]["id"], speaker="")
    service.store.set_row_error(second[1]["id"], "引擎报错")

    detail = service.detail(project["id"])
    counts = {chapter["id"]: chapter for chapter in detail["chapters"]}
    assert counts[chapters[1]["id"]]["unassigned"] == 1
    assert counts[chapters[1]["id"]]["failed"] == 1
    assert counts[chapters[0]["id"]]["unassigned"] == 0

    # 「待生成」= 定了人、没失败、当前没有算数的音频；全书合计要与逐章相加一致
    pending = sum(
        1
        for chapter in chapters
        for row in service.store.rows(chapter["id"])
        if row["speaker"] not in ("", "未知") and row["status"] not in ("audio", "failed")
    )
    assert detail["stats"]["pending"] == pending
    assert detail["stats"]["rows"] == sum(int(c["rows"]) for c in detail["chapters"])
def _record_row_audio(service, project_id: str, chapter_id: str, row: dict):
    """给一行造一条"已生成"的音频（文件 + 正确指纹），不经过引擎。"""
    from app.producers.builtin.audiobook.fingerprint import audio_key, sample_key
    from app.producers.builtin.audiobook.rows import emotion_words, row_params

    mapping = {
        "engine": "fake",
        "voice_id": "voice-1",
        "sample_mode": "emotion",
        "sample_file": "",
    }
    service.store.set_voice(project_id, row["speaker"], "fake", "voice-1", "测试音色")
    words = set(emotion_words())
    key = audio_key(
        text=row["text"],
        engine="fake",
        voice_id="voice-1",
        emotion=row["emotion"] if row["emotion"] in words else "",
        params=row_params(row),
        sample=sample_key(mapping),
    )
    rel = f"audio/{chapter_id}/{int(row['ordinal']):04d}.wav"
    path = service.project_folder(project_id) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....")
    service.store.set_row_audio(row["id"], rel, key)
    return path


def test_editing_a_role_keeps_the_old_audio_as_stale(tmp_path) -> None:
    """改角色：旧音频留着标过期，不算失败也不算未分配——它是「待生成」。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.store.set_row_annotation(row["id"], "老人", "平静")
    row = service.store.row(row["id"])
    path = _record_row_audio(service, project["id"], chapter["id"], row)
    key = service.store.row(row["id"])["audio_key"]

    service.edit_row(project["id"], row["id"], speaker="少年")

    after = service.store.row(row["id"])
    assert after["status"] == "stale"
    # 文件、引用、指纹都还在：改回去能认回来。
    assert after["audio"] == path.relative_to(service.project_folder(project["id"])).as_posix()
    assert after["audio_key"] == key
    assert path.is_file()

    detail = service.detail(project["id"])
    assert detail["stats"]["pending"] == 1
    assert detail["stats"]["stale"] == 1
    assert detail["stats"]["unassigned"] == 0 and detail["stats"]["failed"] == 0

    # 「待生成」那一档能看到它：状态是过期，而不是已生成或失败。
    assert service.store.rows(chapter["id"])[0]["status"] == "stale"


def test_editing_back_restores_the_audio_without_regenerating(tmp_path) -> None:
    """改回去就自动变回「已生成」——不重新合成、也不动文件。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.store.set_row_annotation(row["id"], "老人", "平静")
    text = service.store.row(row["id"])["text"]
    _record_row_audio(service, project["id"], chapter["id"], service.store.row(row["id"]))
    original = service.store.row(row["id"])["audio"]

    service.edit_row(project["id"], row["id"], speaker="少年", emotion="愤怒")
    assert service.store.row(row["id"])["status"] == "stale"
    # 情绪改回去、角色还没改：仍然过期（两个参数都得对上）
    service.edit_row(project["id"], row["id"], emotion="平静")
    assert service.store.row(row["id"])["status"] == "stale"

    service.edit_row(project["id"], row["id"], speaker="老人")
    back = service.store.row(row["id"])
    assert back["status"] == "audio"
    assert back["audio"] == original
    assert service.detail(project["id"])["stats"]["stale"] == 0

    # 文本也一样：改了先过期，改回原文就恢复。
    service.edit_row(project["id"], row["id"], text="你来了吗？")
    assert service.store.row(row["id"])["status"] == "stale"
    service.edit_row(project["id"], row["id"], text=text)
    assert service.store.row(row["id"])["status"] == "audio"


def test_stale_audio_needs_the_file_to_come_back(tmp_path) -> None:
    """文件被删了就不再复活：只认"同一次合成的产物还在"。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.store.set_row_annotation(row["id"], "老人", "平静")
    path = _record_row_audio(service, project["id"], chapter["id"], service.store.row(row["id"]))

    service.edit_row(project["id"], row["id"], speaker="少年")
    path.unlink()
    service.edit_row(project["id"], row["id"], speaker="老人")
    assert service.store.row(row["id"])["status"] == "stale"


def test_merging_a_chapter_refuses_stale_rows(tmp_path) -> None:
    """过期音频不能混进成品：先重新生成（或改回原样）。"""
    import pytest as pytest_mod

    from app.producers.base import ProducerError
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    service.store.set_row_annotation(row["id"], "老人", "平静")
    _record_row_audio(service, project["id"], chapter["id"], service.store.row(row["id"]))
    service.edit_row(project["id"], row["id"], speaker="少年")

    with pytest_mod.raises(ProducerError, match="过期"):
        service.merge(project["id"], chapter["id"])


def test_changing_a_voice_keeps_rows_pending_not_lost(tmp_path) -> None:
    """换音色：整角色标过期；换回原音色时自动恢复。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章 甲\n“你来了。”\n“坐吧。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    rows = service.store.rows(chapter["id"])
    for row in rows:
        service.store.set_row_annotation(row["id"], "老人", "平静")
    for row in service.store.rows(chapter["id"]):
        _record_row_audio(service, project["id"], chapter["id"], row)

    service.store.set_voice(project["id"], "老人", "fake", "voice-2", "另一个音色")
    service.store.invalidate_speaker(project["id"], "老人")

    assert service.detail(project["id"])["stats"]["stale"] == len(rows)
    assert all(row["audio"] for row in service.store.rows(chapter["id"]))

    # 换回来：指纹又对上了，两行都自动恢复。
    service.store.set_voice(project["id"], "老人", "fake", "voice-1", "测试音色")
    service.store.invalidate_speaker(project["id"], "老人")
    assert service.detail(project["id"])["stats"]["stale"] == 0
    assert all(row["status"] == "audio" for row in service.store.rows(chapter["id"]))


def test_row_audio_version_follows_the_file(tmp_path) -> None:
    """重新生成会覆盖同名文件、参数指纹却不变：播放地址的版本号要跟着文件走。

    只认指纹的话，重新生成后同一个地址仍是"这一段"，前端会把点击当成暂停/继续，
    新音频根本装不进去（浏览器也可能拿旧缓存）。
    """
    import os

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门而入。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    _record_row_audio(service, project["id"], chapter["id"], row)

    view = service.chapter_rows(project["id"], chapter["id"])["rows"][0]
    path = service.resolve_audio(project["id"], view["audio"])
    assert view["audio_version"] == path.stat().st_mtime_ns

    # 重生成 = 同一个文件原地覆盖：指纹不动，写入时刻动。
    later = view["audio_version"] + 1_000_000
    os.utime(path, ns=(later, later))
    again = service.chapter_rows(project["id"], chapter["id"])["rows"][0]
    assert again["audio_key"] == view["audio_key"]
    assert again["audio_version"] == later


# ---- 任务收尾、并发与插件发现 ----------------------------------------------

def test_global_limit_caps_multi_engine_batches_without_cutting_engine_quota() -> None:
    from app.producers.builtin.audiobook.audio_stage import _global_limit

    assert _global_limit({}, 4) == 4
    assert _global_limit({"a": 4}, 4) == 4
    # 三台都回退到项目设置：总并发不该变成 12。
    assert _global_limit({"a": 4, "b": 4, "c": 4}, 4) == 4
    # 引擎自己的额度比项目设置大时不被盖住。
    assert _global_limit({"a": 8}, 4) == 8
    assert _global_limit({"a": 8, "b": 4}, 4) == 8


def test_job_is_closed_when_runner_is_cancelled(tmp_path) -> None:
    """CancelledError 属 BaseException：不写终态会让项目永久拒绝新任务。"""
    import asyncio

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n你好。\n")

    async def cancelled(_job_id):
        raise asyncio.CancelledError()

    async def scenario():
        job = service._start_job(project["id"], "analysis", 1, cancelled)
        for _ in range(100):
            await asyncio.sleep(0.01)
            current = service.store.job(job["id"])
            if current["state"] not in ("running", "cancelling"):
                await asyncio.sleep(0)
                return current
        return service.store.job(job["id"])

    current = asyncio.run(scenario())
    assert current["state"] == "cancelled"
    assert service._tasks == {}


def test_job_is_closed_when_runner_raises_a_base_exception(tmp_path) -> None:
    import asyncio

    from app.producers.builtin.audiobook.pipeline import AudiobookService

    class HardFailure(BaseException):
        pass

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n你好。\n")

    async def failing(_job_id):
        raise HardFailure("意外终止")

    async def scenario():
        job = service._start_job(project["id"], "analysis", 1, failing)
        task = service._tasks[job["id"]]
        for _ in range(100):
            await asyncio.sleep(0.01)
            current = service.store.job(job["id"])
            if current["state"] not in ("running", "cancelling"):
                await asyncio.sleep(0)
                return current, task
        return service.store.job(job["id"]), task

    current, task = asyncio.run(scenario())
    assert current["state"] == "failed"
    # callback 已检索过异常：否则 asyncio 在 GC 时补一条 "never retrieved"，
    # 真实故障只出现在日志末尾。_log_traceback 为 False 即已检索。
    assert getattr(task, "_log_traceback", None) is False
    assert isinstance(task.exception(), HardFailure)
    assert service._tasks == {}


def test_broken_builtin_producer_does_not_break_discovery(tmp_path, monkeypatch, caplog) -> None:
    import logging

    from app.infra import plugin_registry
    from app.producers import registry

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.py").write_text("raise RuntimeError('坏插件')\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_BUILTIN", tmp_path)

    def exploding(_name):
        raise RuntimeError("坏插件")

    # 发现日志归共用模块：四类插件的失败说明出自同一处，按域分 logger 只会分叉。
    log = "app.infra.plugin_registry"
    with caplog.at_level(logging.WARNING, logger=log):
        assert all(plugin.key != "broken" for plugin in registry.discover())
    # 临时目录不在模块搜索路径上，这一路落到的是「模块找不到」。
    assert "broken" in caplog.text
    assert "ModuleNotFoundError" in caplog.text

    caplog.clear()
    # 内置根没变、只换了导入器，缓存键命中，得先清掉才会真正重扫。
    plugin_registry.clear_cache()
    monkeypatch.setattr(plugin_registry.importlib, "import_module", exploding)
    with caplog.at_level(logging.WARNING, logger=log):
        assert all(plugin.key != "broken" for plugin in registry.discover())
    # 插件自身抛错也要跳过，且日志能区分编程错误与缺依赖。
    assert "RuntimeError" in caplog.text


def test_list_payload_discovers_once(client, monkeypatch) -> None:
    from app.usecase import producers as uc

    calls: list[int] = []
    original = uc.discover

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(uc, "discover", counted)
    uc.list_payload()
    assert len(calls) == 1


def test_book_import_is_capped(client, monkeypatch) -> None:
    monkeypatch.setattr("app.producers.builtin.audiobook.api._MAX_BOOK_BYTES", 1024)
    resp = client.post(
        "/api/v1/producers/audiobook/projects/import",
        files={"file": ("样书.txt", b"x" * 2048, "text/plain")},
    )
    assert resp.status_code == 400
    assert "1 KB" in resp.json()["error"]["message"]


def test_audio_plan_reads_the_project_folder_once(tmp_path, monkeypatch) -> None:
    """一行的音频在不在，不该按行回头查一次库。

    曾经每行都走 ``resolve_audio`` → ``project_folder``（一次 SQLite 查询），
    4600 行的书光这一步 35 秒——点「生成」后那段长时间空白就是它。
    """
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n“你来了。”老人说道。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")
    service.store.set_voice(project["id"], "对话", "minimax", "v2", "老人音")

    async def fake(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake
    )
    asyncio.run(service.synthesize(project["id"], None))

    calls: list[str] = []
    original = service.project_folder

    def counted(project_id: str):
        calls.append(project_id)
        return original(project_id)

    monkeypatch.setattr(service, "project_folder", counted)
    skipped = service.audio._audio_plan(project["id"]).skipped

    assert skipped >= 1  # 上一步真的生成了音频，计划才走到"文件在不在"这一步
    assert len(calls) == 1


def test_prepare_audio_reads_the_rows_once(tmp_path, monkeypatch) -> None:
    """准备生成时全项目的行只取一遍。

    以前是「逐章读一遍做空文本校验」+「逐章再读一遍算计划」，106 章的书光读行
    就 1.8 秒；一次 join 只要 0.17 秒，同一份数据没有读两遍的理由。
    """
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n第二章\n风进来。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")

    calls = {"rows": 0, "all": 0}
    original_rows = service.store.rows
    original_all = service.store.all_rows

    def counted_rows(chapter_id: str):
        calls["rows"] += 1
        return original_rows(chapter_id)

    def counted_all(project_id: str):
        calls["all"] += 1
        return original_all(project_id)

    monkeypatch.setattr(service.store, "rows", counted_rows)
    monkeypatch.setattr(service.store, "all_rows", counted_all)

    tasks = service.prepare_audio(project["id"]).tasks

    assert tasks  # 都还没生成，计划里应当有行
    assert calls == {"rows": 0, "all": 1}


def test_refresh_audio_stages_does_not_reread_rows(tmp_path, monkeypatch) -> None:
    """章阶段只认章聚合里的行统计，不再为「齐没齐」把整章的行读一遍。"""
    import asyncio

    from app.engines.base import SynthesisResult
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n第二章\n风进来。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "minimax", "v1", "旁白音")

    async def fake(_service, **_kwargs):
        return SynthesisResult(audio=b"RIFF....", format="wav")

    monkeypatch.setattr(
        "app.producers.builtin.audiobook.audio_stage.gateway.synthesize", fake
    )
    asyncio.run(service.synthesize(project["id"], None))

    def refuse(*_args, **_kwargs):
        raise AssertionError("章阶段不该读行")

    monkeypatch.setattr(service.store, "rows", refuse)
    service.audio._refresh_audio_stages(project["id"])

    stages = [chapter["stage"] for chapter in service.store.chapters(project["id"])]
    assert stages and all(stage == "audio" for stage in stages)


def test_disable_producer_stops_service(client) -> None:
    """停用是后端状态：接口拒绝、服务收掉；启用后重新可用。"""
    listed = client.get("/api/v1/producers").json()["data"]
    row = next(item for item in listed if item["key"] == "audiobook")
    assert row["enabled"] is True

    off = client.put("/api/v1/producers/audiobook/enabled", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    blocked = client.get("/api/v1/producers/audiobook/patterns")
    assert blocked.status_code == 400
    assert "已停用" in blocked.json()["error"]["message"]

    on = client.put("/api/v1/producers/audiobook/enabled", json={"enabled": True})
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert client.get("/api/v1/producers/audiobook/patterns").status_code == 200


def test_disabled_producer_is_not_constructed_at_boot(data_dir) -> None:
    """启动时停用的插件不构造服务：构造函数里的自动启动也就不会发生。"""
    from fastapi.testclient import TestClient

    from app.infra.config import Config, save_config
    from app.main import create_app
    from app.producers.registry import discover
    from app.usecase.producers import runtime_for

    plugin = next(item for item in discover() if item.key == "audiobook")
    runtime_for(plugin).shutdown()
    save_config(Config(producers_disabled=["audiobook"]))

    with TestClient(create_app()) as client:
        row = next(
            item
            for item in client.get("/api/v1/producers").json()["data"]
            if item["key"] == "audiobook"
        )
        assert row["enabled"] is False
        assert client.get("/api/v1/producers/audiobook/patterns").status_code == 400

    assert runtime_for(plugin).started is False


def test_audio_plan_skips_rows_of_a_disabled_engine(tmp_path) -> None:
    """停用的引擎发不出声：这些行在计划里就摘掉，原因按引擎记一次。

    不在计划期收掉的话，每一行都要撞一次网关的 ``engine_not_enabled``——
    一万人书就是一万条行错误，而原因只有一个。这种"整台不可用"和"这一行失败"
    不是一回事，日志与界面都不该按后者说话。
    """
    from app.infra.config import load_config, save_config
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n风停了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    service.store.set_voice(project["id"], "旁白", "breeze_tts", "v-narrator", "旁白音")
    rows = [
        row
        for chapter in service.store.chapters(project["id"])
        for row in service.store.rows(chapter["id"])
    ]

    config = load_config()
    assert "breeze_tts" in (config.engines_enabled or [])  # 夹具默认全启用
    config.engines_enabled = [key for key in config.engines_enabled if key != "breeze_tts"]
    save_config(config)

    plan = service.audio._audio_plan(project["id"])
    assert plan.tasks == []
    assert plan.disabled == {"BreezeTTS 2": len(rows)}

    # 再启用回来，同一批行照旧能排进计划（"停用"是可逆的，不是把行弄坏了）。
    config = load_config()
    config.engines_enabled = sorted(set(config.engines_enabled) | {"breeze_tts"})
    save_config(config)
    assert len(service.audio._audio_plan(project["id"]).tasks) == len(rows)


def test_set_row_error_preserves_previous_audio_and_retries_in_plan(tmp_path) -> None:
    """重试失败时保留已有音频与指纹，但计划仍必须重跑失败行（不能按命中跳过）。"""
    from app.producers.builtin.audiobook.pipeline import AudiobookService

    service = AudiobookService(tmp_path)
    project = service.create("样书", "第一章\n少年推门。\n风停了。\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    chapter = service.store.chapters(project["id"])[0]
    row = service.store.rows(chapter["id"])[0]
    _record_row_audio(service, project["id"], chapter["id"], row)
    # _record_row_audio 会把旁白改绑到 fake（未注册引擎），这里绑回真实启用的 breeze_tts。
    service.store.set_voice(project["id"], "旁白", "breeze_tts", "v-narrator", "旁白音")
    before = service.store.row(row["id"])
    assert before is not None and before["audio"] and before["audio_key"]

    service.store.set_row_error(row["id"], "临时超时")
    after = service.store.row(row["id"])
    assert after is not None
    assert after["status"] == "failed"
    assert after["error"] == "临时超时"
    assert after["audio"] == before["audio"]
    assert after["audio_key"] == before["audio_key"]

    # 即使 audio 与 audio_key 都在，失败行也不得被 _audio_plan 跳过。
    with service.store._conn() as conn:
        from app.producers.builtin.audiobook.audio_stage import row_key, voice_identity
        from app.producers.builtin.audiobook.rows import emotion_words
        from app.usecase import engines as engines_uc

        engine = engines_uc.detect_engine("breeze_tts")
        mapping = service.store.voice_map(project["id"])["旁白"]
        key = row_key(
            after,
            mapping,
            words=set(emotion_words()),
            identity=voice_identity(engine) if engine else "",
            version=2,
        )
        conn.execute(
            "UPDATE rows SET audio_key = ?, audio_key_v = 2 WHERE id = ?",
            (key, row["id"]),
        )
    plan = service.audio._audio_plan(project["id"])
    assert any(item[1]["id"] == row["id"] for item in plan.tasks)
