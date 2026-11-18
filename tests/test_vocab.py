"""用户词表（设置页「词表」）：情绪候选与引导指令预设。

守的是四件事：

1. **默认在仓库、用户是差别**：没配过 = 默认，且读一次不写盘；
2. **生效规则**：默认去隐藏、自定义追加、同名就地覆盖默认；
3. **不按引擎分组**：引导指令的默认 = 各引擎 ``instructions.json`` 的并集
   （生成页从前就在前端合并，现在后端一次算清——设置页看到的"生效值"必须与
   生成页、试听、行参数弹框是同一份）；
4. **情绪词表通了全程**：生成规范、创作页总览、分析提示词与回写都认生效值——
   从前写死 ``EMOTIONS``，用户加的词到不了引擎，模型标了也会被丢。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 仓库默认：基础 + 复合 + 态度三组，顺序即界面顺序（与 ``models.EMOTION_GROUPS`` 同源）。
DEFAULT_EMOTIONS = [
    "开心", "悲伤", "愤怒", "恐惧", "惊讶", "兴奋", "委屈", "平静", "冷漠",
    "怅然", "欣慰", "无奈", "愧疚", "释然", "嫉妒", "厌倦", "忐忑", "动情",
    "害羞", "疑惑", "失望", "担忧",
    "严肃", "轻蔑", "恳求", "得意", "撒娇",
]

#: 一个不在默认词表里的词：自定义那一节拿它当例子（默认词会撞"无需重复添加"）。
CUSTOM = "感慨"


def _emotion_options(resp) -> list[str]:
    field = next(f for f in resp.json()["fields"] if f["key"] == "emotion")
    return [value for value, _label in field["options"]]


def _labels(section: dict) -> list[str]:
    return [item["label"] for item in section["defaults"]]


class TestDefaults:
    def test_repo_defaults_without_user_files(self, client, data_dir: Path):
        data = client.get("/api/v1/vocab").json()
        assert data["emotions"]["defaults"] == DEFAULT_EMOTIONS
        assert data["emotions"]["effective"] == DEFAULT_EMOTIONS
        assert data["emotions"]["hidden"] == []
        assert data["emotions"]["custom"] == []
        # 引导指令：各家引擎的预设摆在同一份里（不按引擎分组），短名与正文都去重。
        labels = _labels(data["instructions"])
        assert "喜出望外" in labels, "MiMo 的预设要在"
        assert "耳语" in labels, "Fish Audio 的预设要在"
        assert "平心静气" in labels, "IndexTTS2 的预设也要在"
        assert len(labels) == len(set(labels)), "同标签只留一条"
        texts = [item["text"] for item in data["instructions"]["defaults"]]
        assert len(texts) == len(set(texts)), "同一句话只留一条"
        assert data["instructions"]["effective"] == data["instructions"]["defaults"]

    def test_instruction_texts_are_deduplicated_across_labels(self, monkeypatch):
        """短名不同、句子相同（只差标点）只留先出现的那条。"""
        from app.usecase import vocab as vocab_uc

        class _Plugin:
            def __init__(self, items):
                self.items = items

            def field_presets(self):
                return {"instruction": self.items}

        monkeypatch.setattr(
            vocab_uc,
            "list_engines",
            lambda: {
                "a": _Plugin((("甲", "慢一点。"), ("乙", "快一点。"))),
                "b": _Plugin((("丙", "慢一点！"), ("丁", "轻一点。"))),
            },
        )
        items = vocab_uc._default_instructions()
        assert [item["label"] for item in items] == ["甲", "乙", "丁"]

    def test_reading_writes_nothing(self, client, data_dir: Path):
        """读一次不该顺手落盘：写过的文件从此就是"用户改过的"，
        仓库更新默认词表时再也传不进来。"""
        client.get("/api/v1/vocab")
        assert not (data_dir / "emotions.json").exists()
        assert not (data_dir / "instructions.json").exists()

    def test_instruction_presets_ignore_enable_state(self, client):
        """预设按**全部已发现**插件合并，与启停无关：这一格是自由文本，句子不指向某台
        引擎。按已启用收束过一次，停一台就少一段，用户读到的是"词被删了"。"""
        from app.engines.registry import list_engines
        from app.infra.config import load_config, save_config

        config = load_config()
        config.engines_enabled = []
        save_config(config)

        labels = _labels(client.get("/api/v1/vocab").json()["instructions"])
        declared = {
            label
            for plugin in list_engines().values()
            for label, _text in plugin.field_presets().get("instruction", ())
        }
        assert set(labels) == declared
        assert {"喜出望外", "耳语", "平心静气"} <= set(labels)

    def test_emotions_are_grouped_for_the_ui(self, client, data_dir: Path):
        """分组只是摆法：拼起来必须与生效值逐项相同，否则下拉会少词或多词。"""
        groups = client.get("/api/v1/vocab").json()["emotions"]["groups"]
        assert [group["label"] for group in groups] == ["基础情绪", "复合情绪", "态度"]
        assert groups[0]["words"][0] == "开心"
        assert [word for group in groups for word in group["words"]] == DEFAULT_EMOTIONS

    def test_spec_carries_default_emotions(self, client):
        assert _emotion_options(client.get("/api/v1/spec")) == ["", *DEFAULT_EMOTIONS]


class TestRenamedDefaults:
    def test_renaming_a_default_emotion_keeps_its_group(self, client):
        saved = client.put(
            "/api/v1/vocab",
            json={"emotions": {"renamed": {"开心": "高兴"}}},
        ).json()
        section = saved["emotions"]
        assert section["renamed"] == {"开心": "高兴"}
        assert "开心" not in section["effective"]
        assert "高兴" in section["effective"]
        assert section["effective"].index("高兴") == DEFAULT_EMOTIONS.index("开心")
        assert section["groups"][0]["words"][0] == "高兴", "仍摆在这一组的第一位"
        assert section["defaults"] == DEFAULT_EMOTIONS, "默认那份不动"

    def test_renaming_to_another_default_is_a_400(self, client):
        resp = client.put(
            "/api/v1/vocab",
            json={"emotions": {"renamed": {"开心": "悲伤"}}},
        )
        assert resp.status_code == 400
        assert "重名" in resp.json()["error"]["message"]

    def test_renaming_a_default_instruction_label_and_text(self, client):
        saved = client.put(
            "/api/v1/vocab",
            json={
                "instructions": {
                    "renamed": {"平心静气": "旁白腔"},
                    "custom": [{"label": "平心静气", "text": "像纪录片旁白。"}],
                }
            },
        ).json()
        section = saved["instructions"]
        hit = [item for item in section["effective"] if item["label"] == "旁白腔"]
        assert hit == [{"label": "旁白腔", "text": "像纪录片旁白。"}]
        assert "平心静气" not in [item["label"] for item in section["effective"]]

    def test_renaming_an_unknown_default_is_ignored(self, client):
        saved = client.put(
            "/api/v1/vocab",
            json={"emotions": {"renamed": {"不存在的词": "新词"}}},
        ).json()
        assert saved["emotions"]["renamed"] == {}
        assert saved["emotions"]["effective"] == DEFAULT_EMOTIONS


class TestEmotionDelta:
    def test_custom_words_append(self, client, data_dir: Path):
        saved = client.put(
            "/api/v1/vocab", json={"emotions": {"hidden": [], "custom": [CUSTOM, "恍惚"]}}
        ).json()
        assert saved["emotions"]["effective"] == [*DEFAULT_EMOTIONS, CUSTOM, "恍惚"]
        assert saved["emotions"]["defaults"] == DEFAULT_EMOTIONS, "默认那份不动"
        assert saved["emotions"]["groups"][-1] == {
            "label": "自定义",
            "words": [CUSTOM, "恍惚"],
        }, "自定义词单独一组，排在最后"
        assert (data_dir / "emotions.json").exists(), "改过就该落盘"
        again = client.get("/api/v1/vocab").json()
        assert again["emotions"]["effective"] == saved["emotions"]["effective"]

    def test_hiding_a_default_removes_it(self, client):
        saved = client.put(
            "/api/v1/vocab", json={"emotions": {"hidden": ["动情"], "custom": []}}
        ).json()
        assert "动情" not in saved["emotions"]["effective"]
        assert saved["emotions"]["defaults"] == DEFAULT_EMOTIONS, "藏起来不等于从仓库删掉"
        assert saved["emotions"]["hidden"] == ["动情"]

    def test_hidden_and_custom_coexist(self, client):
        saved = client.put(
            "/api/v1/vocab",
            json={"emotions": {"hidden": ["悲伤"], "custom": [CUSTOM]}},
        ).json()
        assert saved["emotions"]["effective"] == [
            w for w in DEFAULT_EMOTIONS if w != "悲伤"
        ] + [CUSTOM]

    def test_duplicating_a_default_is_a_readable_400(self, client):
        resp = client.put("/api/v1/vocab", json={"emotions": {"custom": ["开心"]}})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "vocab_invalid"
        assert "开心" in resp.json()["error"]["message"]

    def test_blank_words_are_dropped(self, client):
        saved = client.put(
            "/api/v1/vocab", json={"emotions": {"custom": ["", "  ", CUSTOM]}}
        ).json()
        assert saved["emotions"]["custom"] == [CUSTOM]

    def test_too_many_custom_words_is_a_400(self, client):
        resp = client.put(
            "/api/v1/vocab", json={"emotions": {"custom": [f"词{i}" for i in range(41)]}}
        )
        assert resp.status_code == 400
        assert "40" in resp.json()["error"]["message"]

    def test_over_long_word_is_a_400(self, client):
        resp = client.put("/api/v1/vocab", json={"emotions": {"custom": ["词" * 13]}})
        assert resp.status_code == 400
        assert "12" in resp.json()["error"]["message"]

    def test_saving_emotions_leaves_instructions_alone(self, client):
        """按节局部保存：另一节的用户差别原样保留。"""
        preset = {"label": "气声耳语", "text": "气声多一点，像贴着耳朵说话。"}
        client.put("/api/v1/vocab", json={"instructions": {"custom": [preset]}})
        client.put("/api/v1/vocab", json={"emotions": {"custom": [CUSTOM]}})
        data = client.get("/api/v1/vocab").json()
        assert data["instructions"]["custom"] == [preset]
        assert data["emotions"]["custom"] == [CUSTOM]


class TestInstructionDelta:
    def test_custom_preset_appends_at_the_end(self, client, data_dir: Path):
        preset = {"label": "气声耳语", "text": "气声多一点，像贴着耳朵说话。"}
        saved = client.put(
            "/api/v1/vocab", json={"instructions": {"hidden": [], "custom": [preset]}}
        ).json()
        section = saved["instructions"]
        assert section["effective"][-1] == preset
        assert preset not in section["defaults"], "默认那份不动"
        assert (data_dir / "instructions.json").exists()

    def test_same_label_overrides_the_default_in_place(self, client):
        """想改一句默认预设的措辞：加一条同名自定义即可，位置不变、不新增一行。"""
        wanted = "平稳、清楚、克制，像纪录片旁白。（我的版本）"
        saved = client.put(
            "/api/v1/vocab",
            json={"instructions": {"custom": [{"label": "平心静气", "text": wanted}]}},
        ).json()
        section = saved["instructions"]
        hit = [item for item in section["effective"] if item["label"] == "平心静气"]
        assert hit == [{"label": "平心静气", "text": wanted}]
        # 位置就是默认那一条的位置：覆盖不是"删掉默认、追加自定义"。
        assert section["effective"].index(hit[0]) == section["defaults"].index(
            next(i for i in section["defaults"] if i["label"] == "平心静气")
        )

    def test_hiding_a_default_label(self, client):
        saved = client.put(
            "/api/v1/vocab", json={"instructions": {"hidden": ["弦外有音"], "custom": []}}
        ).json()
        labels = [item["label"] for item in saved["instructions"]["effective"]]
        assert "弦外有音" not in labels
        assert "弦外有音" in _labels(saved["instructions"]), "默认那份还在"

    def test_hiding_an_unknown_label_is_ignored(self, client):
        """藏一个不是默认的标签没有意义（仓库删条后会留下这种过期条目）：不报错。"""
        saved = client.put(
            "/api/v1/vocab", json={"instructions": {"hidden": ["不存在的标签"], "custom": []}}
        ).json()
        assert saved["instructions"]["hidden"] == []
        assert saved["instructions"]["effective"] == saved["instructions"]["defaults"]

    def test_duplicate_label_within_custom_is_a_400(self, client):
        resp = client.put(
            "/api/v1/vocab",
            json={
                "instructions": {
                    "custom": [
                        {"label": "气声耳语", "text": "第一版。"},
                        {"label": "气声耳语", "text": "第二版。"},
                    ]
                }
            },
        )
        assert resp.status_code == 400
        assert "气声耳语" in resp.json()["error"]["message"]

    def test_over_long_text_is_a_400(self, client):
        resp = client.put(
            "/api/v1/vocab",
            json={"instructions": {"custom": [{"label": "长句", "text": "字" * 201}]}},
        )
        assert resp.status_code == 400
        assert "200" in resp.json()["error"]["message"]

    def test_entries_missing_half_are_dropped(self, client):
        """只有短名或只有句子：菜单里没法认、按下去框还是空的，drop。"""
        saved = client.put(
            "/api/v1/vocab",
            json={
                "instructions": {
                    "custom": [
                        {"label": "", "text": "没有短名。"},
                        {"label": "只有短名", "text": ""},
                        {"label": "好的", "text": "慢一点，温柔些。"},
                    ]
                }
            },
        ).json()
        assert saved["instructions"]["custom"] == [{"label": "好的", "text": "慢一点，温柔些。"}]


class TestCorruptFiles:
    def test_broken_json_falls_back_to_defaults(self, client, data_dir: Path):
        """文件是给人手改的：写坏了当作没有差别，别让整个设置页 500。"""
        (data_dir / "emotions.json").write_text("{ 坏", encoding="utf-8")
        (data_dir / "instructions.json").write_text("not json at all", encoding="utf-8")
        data = client.get("/api/v1/vocab").json()
        assert data["emotions"]["effective"] == DEFAULT_EMOTIONS
        assert data["instructions"]["effective"] == data["instructions"]["defaults"]


class TestVocabReachesEveryConsumer:
    def test_spec_follows_the_vocab(self, client):
        client.put("/api/v1/vocab", json={"emotions": {"custom": [CUSTOM]}})
        assert _emotion_options(client.get("/api/v1/spec")) == ["", *DEFAULT_EMOTIONS, CUSTOM]

    def test_overview_follows_the_vocab(self, tmp_path, data_dir: Path):
        from app.producers.builtin.audiobook.pipeline import AudiobookService
        from app.usecase import vocab as vocab_uc

        service = AudiobookService(tmp_path)
        assert service.overview()["emotions"] == DEFAULT_EMOTIONS
        vocab_uc.save_emotions([], [CUSTOM])
        assert service.overview()["emotions"] == [*DEFAULT_EMOTIONS, CUSTOM]

    def test_overview_carries_the_grouping(self, tmp_path, data_dir: Path):
        """创作页下拉按组分：总览里得有分组，且拼起来就是生效值。"""
        from app.producers.builtin.audiobook.pipeline import AudiobookService

        overview = AudiobookService(tmp_path).overview()
        groups = overview["emotion_groups"]
        assert [group["label"] for group in groups] == ["基础情绪", "复合情绪", "态度"]
        assert [word for group in groups for word in group["words"]] == overview["emotions"]

    def test_prompt_lists_the_effective_words(self, tmp_path, data_dir: Path):
        """「AI 注入」= 把生效词表写进分析提示词：用户加了词，标注器才看得见，
        否则自定义词只能手动逐行设——标了也白标。"""
        import asyncio

        from app.producers.builtin.audiobook.pipeline import AudiobookService
        from app.usecase import vocab as vocab_uc

        vocab_uc.save_emotions([], [CUSTOM])
        service = AudiobookService(tmp_path)
        project = service.create("样书", "第一章 甲\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])

        seen = []

        async def fake_chat(body):
            seen.append(body["messages"][1]["content"])
            return {"text": json.dumps({"rows": [[0, "老人", CUSTOM, "你来了"]]})}

        import app.usecase.llm as llm_mod

        original = llm_mod.chat
        llm_mod.chat = fake_chat
        try:
            asyncio.run(service.analyze(project["id"]))
        finally:
            llm_mod.chat = original

        assert seen, "该发一次请求"
        assert CUSTOM in seen[0]
        assert "开心、悲伤" in seen[0], "默认那批照列"

    def test_writeback_accepts_words_from_the_vocab(self, tmp_path, data_dir: Path):
        """回写关口：模型给了词表里的词才写回；表外的词保留这一行原来的情绪。"""
        from app.producers.builtin.audiobook.pipeline import AudiobookService

        service = AudiobookService(tmp_path)
        project = service.create("样书", "第一章 甲\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])
        chapter = service.store.chapters(project["id"])[0]
        row = next(r for r in service.store.rows(chapter["id"]) if r["speaker"] == "对话")
        before = row["emotion"]
        items = [[0, "老人", CUSTOM, row["text"][:6]]]

        service._apply_analysis([row], items, set(DEFAULT_EMOTIONS))
        assert service.store.row(row["id"])["emotion"] == before, "表外的词不当真"

        service._apply_analysis([row], items, {*DEFAULT_EMOTIONS, CUSTOM})
        assert service.store.row(row["id"])["emotion"] == CUSTOM

    def test_deleting_a_word_keeps_row_tags(self, tmp_path, data_dir: Path):
        """删候选词只改候选表：行上已经写下的情绪仍是这一行自己的数据。"""
        from app.producers.builtin.audiobook.pipeline import AudiobookService
        from app.usecase import vocab as vocab_uc

        service = AudiobookService(tmp_path)
        project = service.create("样书", "第一章 甲\n“你来了。”\n")
        service.split_project(project["id"], "general")
        service.split_sentences(project["id"])
        row = next(
            item
            for item in service.store.all_rows(project["id"])
            if item["speaker"] == "对话"
        )
        service.edit_row(project["id"], row["id"], emotion="开心")

        vocab_uc.save_emotions(["开心"], [], {})

        assert "开心" not in vocab_uc.emotions(), "候选表里已经删掉"
        assert service.store.row(row["id"])["emotion"] == "开心", "行上的标签不动"

    def test_cache_key_separates_vocabularies(self):
        """词表进缓存键：改了词表，同一条对白该重新问模型。"""
        from app.producers.builtin.audiobook.fingerprint import analysis_key

        base = {
            "channel": "",
            "model": "",
            "prompt": "",
            "book": "样书",
            "text": "正文",
            "rows": ["你来了。"],
            "granularity": "batch",
            "context": "chapter",
            "context_lines": 3,
        }
        assert analysis_key(**base, emotions=DEFAULT_EMOTIONS) != analysis_key(
            **base, emotions=[*DEFAULT_EMOTIONS, CUSTOM]
        )


class TestPromptPlaceholder:
    def test_builtin_prompt_lists_the_defaults(self):
        from app.producers.builtin.audiobook.prompts import compose_prompt

        text = compose_prompt("", book="样书", text="正文", rows="[]")
        assert "、".join(DEFAULT_EMOTIONS) in text

    def test_effective_words_replace_the_listing(self):
        from app.producers.builtin.audiobook.prompts import compose_prompt

        text = compose_prompt("", book="样书", text="正文", rows="[]", emotions=[CUSTOM, "恍惚"])
        assert f"{CUSTOM}、恍惚" in text
        assert "开心" not in text, "给了词表就照它列，不把默认那批也带上"

    def test_custom_prompt_without_the_placeholder_is_untouched(self):
        """用户自己的提示词没写 ``{emotions}``：不替他补那一串词——提示词是用户的。
        （上下文与对白行照旧补，那是另一条老规则：没有它们模型无正文可看。）"""
        from app.producers.builtin.audiobook.prompts import compose_prompt

        text = compose_prompt(
            "只输出 JSON。", book="样书", text="正文", rows="[]", emotions=[CUSTOM]
        )
        assert text.startswith("只输出 JSON。")
        assert CUSTOM not in text
        assert "正文上下文" in text
