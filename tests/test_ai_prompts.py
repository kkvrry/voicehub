"""AI 提示词：默认在仓库、用户只记覆盖，且各任务用的就是设置页那一份。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

PROMPTS_URL = "/api/v1/producers/audiobook/prompts"


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    """ai_prompts 读盘缓存按路径与 mtime；每个用例换目录时清掉最稳。"""
    from app.producers.builtin.audiobook import ai_prompts

    ai_prompts._CACHE = None
    yield
    ai_prompts._CACHE = None


def test_defaults_without_user_file(client, data_dir: Path):
    data = client.get(PROMPTS_URL).json()
    keys = [item["key"] for item in data["analysis"]["effective"]]
    assert keys == ["default", "instruction", "instruction_emotion", "terse", "conservative"]
    fixed = {group["key"]: group for group in data["fixed"]}
    assert set(fixed) == {"voice_assign", "voice_profile", "regex_rule"}
    assert fixed["voice_profile"]["items"][0]["text"] == "你是中文有声书的配音导演，只输出 JSON。"
    assert "{roles}" in fixed["voice_profile"]["items"][1]["text"]
    assert not (data_dir / "producers" / "audiobook" / "prompts.json").exists(), "读一次不写盘"


def test_editing_an_analysis_preset_changes_what_the_dialog_costs(client):
    """设置里改的预设 -> 插件自描述 -> 分析弹窗下拉，同一份正文。"""
    saved = client.put(
        PROMPTS_URL,
        json={
            "analysis": {
                "custom": [
                    {
                        "key": "instruction",
                        "label": "只写念法",
                        "text": "只写念法。给每句写一条引导指令：{rows}",
                    }
                ]
            }
        },
    ).json()
    item = next(p for p in saved["analysis"]["effective"] if p["key"] == "instruction")
    assert item["label"] == "只写念法"
    assert item["text"].startswith("只写念法。")
    presets = client.get("/api/v1/producers/audiobook").json()["analysis_presets"]
    hit = next(p for p in presets if p["key"] == "instruction")
    assert hit["label"] == "只写念法", "弹窗读的是生效值，不是仓库默认"


def test_deleted_preset_disappears_from_the_dialog(client):
    saved = client.put(
        PROMPTS_URL, json={"analysis": {"hidden": ["conservative"]}}
    ).json()
    assert [p["key"] for p in saved["analysis"]["effective"]] == [
        "default",
        "instruction",
        "instruction_emotion",
        "terse",
    ]
    presets = client.get("/api/v1/producers/audiobook").json()["analysis_presets"]
    assert "conservative" not in [p["key"] for p in presets]


def test_added_preset_infers_its_output_fields(client):
    from app.producers.builtin.audiobook import ai_prompts

    client.put(
        PROMPTS_URL,
        json={
            "analysis": {
                "custom": [
                    {"label": "只给念法", "text": "给每句写一条引导指令：{rows}"}
                ]
            }
        },
    )
    assert ai_prompts.analysis_fields("给每句写一条引导指令：{rows}") == (
        "speaker",
        "instruction",
    )


def test_duplicate_preset_labels_are_a_400(client):
    resp = client.put(
        PROMPTS_URL,
        json={
            "analysis": {
                "custom": [
                    {"label": "同一句", "text": "第一版：{rows}"},
                    {"label": "同一句", "text": "第二版：{rows}"},
                ]
            }
        },
    )
    assert resp.status_code == 400
    assert "重复" in resp.json()["error"]["message"]


def test_fixed_prompt_must_keep_required_placeholders(client):
    resp = client.put(
        PROMPTS_URL,
        json={"fixed": {"voice_profile": {"template": "没有占位的一段话。"}}},
    )
    assert resp.status_code == 400
    assert "{title}" in resp.json()["error"]["message"]


def test_fixed_prompt_override_reaches_the_task(client):
    from app.producers.builtin.audiobook import ai_prompts
    from app.producers.builtin.audiobook.assist.regex_rule import Prepared, RegexTask

    saved = client.put(
        PROMPTS_URL,
        json={
            "fixed": {
                "regex_rule": {
                    "system": "你是正则助手，只输出 JSON。",
                    "template": "{action}\n请解决：\n{input}\n只输出 JSON。",
                }
            }
        },
    ).json()
    group = next(item for item in saved["fixed"] if item["key"] == "regex_rule")
    assert [item["text"] for item in group["items"]] == [
        "你是正则助手，只输出 JSON。",
        "{action}\n请解决：\n{input}\n只输出 JSON。",
    ]
    assert ai_prompts.revision("regex_rule")
    messages = RegexTask().messages(
        Prepared(service=None, project_id="", kind="dialog", sample="删除行首编号")
    )
    assert messages[0].content == "你是正则助手，只输出 JSON。"
    assert "请解决：\n删除行首编号" in messages[1].content


def test_fixed_prompt_equal_to_default_is_not_written(client, data_dir: Path):
    """改回出厂原文不算差别：文件里不留覆盖，仓库以后改默认能跟着走。"""
    state = client.get(PROMPTS_URL).json()
    group = next(item for item in state["fixed"] if item["key"] == "voice_profile")
    defaults = {item["key"]: item["default"] for item in group["items"]}
    client.put(
        PROMPTS_URL,
        json={
            "fixed": {
                "voice_profile": {
                    "system": "另一段系统提示词。",
                    "template": "为{title}写画像：{roles}",
                }
            }
        },
    )
    client.put(PROMPTS_URL, json={"fixed": {"voice_profile": defaults}})
    path = data_dir / "producers" / "audiobook" / "prompts.json"
    assert "voice_profile" not in json.loads(path.read_text(encoding="utf-8"))["fixed"]


def test_resetting_a_fixed_group_removes_the_override(client, data_dir: Path):
    client.put(
        PROMPTS_URL,
        json={
            "fixed": {
                "voice_profile": {
                    "system": "另一段系统提示词。",
                    "template": "为{title}写画像：{roles}",
                }
            }
        },
    )
    saved = client.put(PROMPTS_URL, json={"fixed": {"voice_profile": {}}}).json()
    group = next(item for item in saved["fixed"] if item["key"] == "voice_profile")
    assert group["items"][0]["text"] == "你是中文有声书的配音导演，只输出 JSON。"
    assert group["items"][1]["text"].startswith("为{title}里的角色各写一份声音画像")
    path = data_dir / "producers" / "audiobook" / "prompts.json"
    assert "voice_profile" not in json.loads(path.read_text(encoding="utf-8"))["fixed"]


def test_edited_preset_drops_the_default_note(client):
    saved = client.put(
        PROMPTS_URL,
        json={
            "analysis": {
                "custom": [
                    {"key": "terse", "label": "精简", "text": "短一点：{rows}"}
                ]
            }
        },
    ).json()
    item = next(p for p in saved["analysis"]["effective"] if p["key"] == "terse")
    assert item["note"] == "", "改了正文就不再显示默认那句说明"


def test_profile_cache_key_follows_prompt_edits(client):
    from app.producers.builtin.audiobook import ai_prompts
    from app.producers.builtin.audiobook.assist import voice_profile

    before = voice_profile._key("样书", "", "老周")
    client.put(
        PROMPTS_URL,
        json={
            "fixed": {
                "voice_profile": {
                    "system": "新系统提示词。",
                    "template": "为{title}写画像：{roles}",
                }
            }
        },
    )
    assert ai_prompts.assist_messages("voice_profile")[1] == "为{title}写画像：{roles}"
    assert voice_profile._key("样书", "", "老周") != before


def test_analysis_uses_the_selected_preset_and_follows_edits(
    client, tmp_path, monkeypatch
):
    """项目存的是预设键：设置里改了正文，下次分析发的就是新正文。"""
    from app.producers.builtin.audiobook import ai_prompts
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

    service = AudiobookService(
        tmp_path, lambda: AudiobookSettings(analysis_preset="instruction")
    )
    project = service.create("样书", "第一章 甲\n“你来了。”\n")
    service.split_project(project["id"], "general")
    service.split_sentences(project["id"])
    row = next(
        item for item in service.store.all_rows(project["id"]) if item["speaker"] == "对话"
    )

    seen: list[str] = []

    async def fake_chat(body):
        prompt = body["messages"][1]["content"]
        seen.append(prompt)
        text = json.loads(prompt.rsplit("对白行：", 1)[1])[0]["text"]
        return {"text": json.dumps({"rows": [[0, "老人", "压低嗓子一字一顿", text[:6]]]})}

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    asyncio.run(service.analyze(project["id"]))
    assert seen and "引导指令" in seen[0], "选的是「说话人+引导指令」"
    assert service.store.row(row["id"])["speaker"] == "老人"

    ai_prompts.save(
        {
            "analysis": {
                "custom": [
                    {
                        "key": "instruction",
                        "label": "说话人+引导指令",
                        "text": (
                            "本次要分析的书是《{book}》。\n\n{roles}"
                            "给每句写一条引导指令（设置里改过）。\n\n"
                            "正文上下文：\n{text}\n\n对白行：\n{rows}"
                        ),
                    }
                ]
            }
        }
    )
    seen.clear()
    asyncio.run(service.analyze(project["id"]))
    assert seen and "设置里改过" in seen[0], "改了预设，下一次请求就用新正文"
