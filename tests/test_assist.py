"""微型 AI 任务：结构化校验、试跑、一次修复与批量写入。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.models import Voice
from app.producers.builtin.audiobook.assist import voice_assign, voice_profile, voice_recall
from app.producers.builtin.audiobook.assist.regex_rule import RegexTask
from app.producers.builtin.audiobook.assist.voice_assign import (
    Candidate,
    Prepared,
    VoiceAssignTask,
)
from app.producers.builtin.audiobook.pipeline import AudiobookService
from app.usecase.assist import (
    AssistContext,
    AssistError,
    AssistInvalid,
    run_task,
)

SAMPLE = "第一章 起\n[1] 这是注释。\n下一段。\n"
BRACKET = r"(?m)^[ \t　]*\[\d+\][^\r\n]*\r?\n?"


def _regex_ctx(
    tmp_path: Path,
    *,
    kind: str = "bracket",
    sample: str = "[1] 这是注释。",
):
    service = AudiobookService(tmp_path)
    project = service.create("样书", SAMPLE)
    ctx = AssistContext(
        service=service,
        library=None,
        settings=service.settings,
        project_id=project["id"],
        body={"kind": kind, "sample": sample},
    )
    return ctx, project["id"]


def _fake_voice(voice_id: str, name: str, sample_count: int = 2) -> Voice:
    return Voice(
        id=voice_id,
        source="local",
        name=name,
        display=name,
        gender="女",
        age="青年",
        language="中文",
        tags=("活泼",),
        sample_count=sample_count,
    )


def _voice_prepared() -> Prepared:
    first = _fake_voice("voice-1", "胡桃")
    second = _fake_voice("voice-2", "钟离")
    roles = (
        {"speaker": "阿明", "rows": 12},
        {"speaker": "小美", "rows": 8},
        {"speaker": "老周", "rows": 3},
    )
    return Prepared(
        project_id="p",
        project_name="样书",
        roles=roles,
        candidates=(
            {"key": "v1", "name": "胡桃"},
            {"key": "v2", "name": "钟离"},
        ),
        candidate_map={
            "v1": Candidate(key="v1", voice=first, engines=("fake",)),
            "v2": Candidate(key="v2", voice=second, engines=("fake",)),
        },
        voice_by_id={first.id: first, second.id: second},
        role_map={item["speaker"]: item for item in roles},
        unassigned=("阿明", "小美", "老周"),
        existing={},
        dominant_engine="fake",
        engine_labels={"fake": "假引擎"},
        warnings=(),
    )


def test_regex_task_build_rejects_empty_input(tmp_path: Path) -> None:
    ctx, _ = _regex_ctx(tmp_path, sample="")
    with pytest.raises(AssistError, match="需求|样例"):
        RegexTask().build(ctx)


def test_regex_task_validate_rejects_bad_patterns(tmp_path: Path) -> None:
    ctx, _ = _regex_ctx(tmp_path)
    task = RegexTask()
    prepared = task.build(ctx)
    with pytest.raises(AssistInvalid, match="空字符串"):
        task.validate(prepared, {"pattern": "a*", "replacement": ""})
    with pytest.raises(AssistInvalid, match="正则写错了"):
        task.validate(prepared, {"pattern": "（[", "replacement": ""})
    with pytest.raises(AssistInvalid, match="没有给出正则"):
        task.validate(prepared, {"replacement": ""})


def test_regex_task_accepts_a_description_as_input(tmp_path: Path) -> None:
    """输入可以是一句描述；校验只要求它是纯正则，不再要求命中那句描述。"""
    text = "第一章\n11、第一句。\n12、第二句。\n正文行。\n"
    service = AudiobookService(tmp_path)
    project = service.create("样书", text)
    ctx = AssistContext(
        service=service,
        library=None,
        settings=service.settings,
        project_id=project["id"],
        body={"kind": "bracket", "sample": "删除行首像 11、12 这样的数字编号"},
    )
    task = RegexTask()
    prepared = task.build(ctx)
    proposal = task.validate(
        prepared,
        {
            # 模型偶尔会把正则包在代码围栏里；服务端只留正则本身。
            "pattern": "```regex\n(?m)^[ \\t　]*\\d+、\n```",
            "replacement": "",
            "explanation": "删除行首的数字顿号编号",
        },
    )
    assert proposal["pattern"] == r"(?m)^[ \t　]*\d+、"
    preview = task.preview(prepared, proposal)
    assert preview["total"] == 2
    assert [hit["before"] for hit in preview["hits"]] == ["11、", "12、"]


def test_regex_task_validate_rejects_bad_replacement() -> None:
    task = RegexTask()
    prepared = type(
        "P",
        (),
        {"kind": "dialog", "sample": "甲", "project_id": "", "service": None},
    )()
    with pytest.raises(AssistInvalid, match="替换串"):
        task.validate(prepared, {"pattern": "(甲)", "replacement": r"\2"})


def test_regex_task_prompt_keeps_the_input_and_drops_the_hardcoded_rules(
    tmp_path: Path,
) -> None:
    """提示词只转述用户输入与输出格式，不再塞一套“先判断再写”的固定写法。"""
    ctx, _ = _regex_ctx(tmp_path, sample="删除行首像 11、12 这样的数字编号")
    task = RegexTask()
    prepared = task.build(ctx)
    prompt = task.messages(prepared)[1].content
    assert "删除行首像 11、12 这样的数字编号" in prompt
    assert "只输出 JSON" in prompt
    assert "pattern 只放正则本身" in prompt
    assert "先判断再写" not in prompt
    assert "编号 + 空白 + 任意正文" not in prompt
    assert "指的是" not in prompt


def test_numbered_annotation_pattern_covers_same_shape_lines(tmp_path: Path) -> None:
    text = "第一章\n1　指的是卖糖果的店铺。\n2　表示甜食铺子。\n正文行。\n"
    service = AudiobookService(tmp_path)
    project = service.create("样书", text)
    ctx = AssistContext(
        service=service,
        library=None,
        settings=service.settings,
        project_id=project["id"],
        body={
            "kind": "bracket",
            "sample": "1　指的是卖糖果的店铺。",
        },
    )
    task = RegexTask()
    prepared = task.build(ctx)
    proposal = task.validate(
        prepared,
        {
            "pattern": r"(?m)^[ \t　]*\d+[ \t　]+[^\r\n]*\r?\n?",
            "replacement": "",
            "explanation": "删除编号开头的整行注释",
        },
    )
    preview = task.preview(prepared, proposal)
    assert preview["total"] == 2
    befores = [hit["before"] for hit in preview["hits"]]
    assert "1　指的是卖糖果的店铺。\n" in befores
    assert "2　表示甜食铺子。\n" in befores


def test_regex_task_preview_runs_on_project_text(tmp_path: Path) -> None:
    ctx, _ = _regex_ctx(tmp_path)
    task = RegexTask()
    prepared = task.build(ctx)
    proposal = task.validate(
        prepared,
        {
            "pattern": BRACKET,
            "replacement": "",
            "explanation": "删除方括号编号整行",
        },
    )
    preview = task.preview(prepared, proposal)
    assert preview["total"] == 1
    assert preview["changed"] is True
    assert preview["hits"][0]["before"] == "[1] 这是注释。\n"


def test_regex_task_preview_rejects_nested_quantifier(tmp_path: Path) -> None:
    ctx, _ = _regex_ctx(tmp_path, sample="[1] 这是注释。")
    task = RegexTask()
    prepared = task.build(ctx)
    proposal = {"pattern": r"(\[1\].+)+", "replacement": "", "explanation": ""}
    with pytest.raises(AssistInvalid, match="嵌套量词"):
        task.preview(prepared, proposal)


def test_run_task_repairs_once(tmp_path: Path) -> None:
    ctx, _ = _regex_ctx(tmp_path)
    calls: list[dict] = []

    async def fake_chat(body):
        calls.append(body)
        if len(calls) == 1:
            return {
                "text": "```json\n{\"pattern\":\"(\",\"replacement\":\"\"}\n```",
                "provider": "fake",
                "model": "fake",
                "notes": [],
                "usage": {},
            }
        return {
            "text": json.dumps(
                {
                    "pattern": BRACKET,
                    "replacement": "",
                    "explanation": "删除方括号编号整行",
                },
                ensure_ascii=False,
            ),
            "provider": "fake",
            "model": "fake",
            "notes": [],
            "usage": {"total_tokens": 12},
        }

    result = asyncio.run(run_task(RegexTask(), ctx, chat=fake_chat))
    assert len(calls) == 2
    assert calls[0]["json_object"] is True
    assert calls[0]["thinking"] == "off"
    assert "没有通过校验" in calls[1]["messages"][-1]["content"]
    assert result["proposal"]["pattern"] == BRACKET
    assert result["preview"]["total"] == 1


def test_voice_task_validate_and_preview() -> None:
    task = VoiceAssignTask()
    proposal = task.validate(
        _voice_prepared(),
        {
            "assignments": [
                {"speaker": "阿明", "voice": "v1", "reason": "女声"},
                {"speaker": "小美", "voice": "v1", "reason": "复用"},
                {"speaker": "不存在", "voice": "v2", "reason": "bad"},
                {"speaker": "阿明", "voice": "v2", "reason": "重复"},
            ],
            "unmatched": [],
        },
    )
    assert [item["speaker"] for item in proposal["assignments"]] == ["阿明", "小美"]
    assert proposal["assignments"][0]["engine"] == "fake"
    assert proposal["assignments"][0]["shared"] is False
    assert proposal["assignments"][1]["shared"] is True
    assert proposal["unmatched"] == [{"speaker": "老周", "reason": "模型没有给出建议"}]
    assert len(proposal["rejected"]) == 2
    assert proposal["warnings"]

    preview = task.preview(_voice_prepared(), proposal)
    assert preview["assignments"][0]["voice"]["name"] == "胡桃"
    assert preview["assignments"][0]["engine_label"] == "假引擎"
    assert preview["role_count"] == 3


def test_voice_task_build_only_unassigned(tmp_path: Path, monkeypatch) -> None:
    first = _fake_voice("voice-1", "胡桃")
    second = _fake_voice("voice-2", "钟离")

    class Store:
        def project(self, _project_id):
            return {"id": "p", "name": "样书"}

        def speakers(self, _project_id):
            return [
                {"speaker": "阿明", "rows": 12, "voice_id": ""},
                {"speaker": "小美", "rows": 8, "voice_id": "already"},
            ]

        def voice_map(self, _project_id):
            return {"小美": {"engine": "fake", "voice_id": "already", "voice_name": "已有"}}

        def speaker_lines(self, _project_id, names, limit=2):
            return {name: ["你来了。"] for name in names}

    class Service:
        store = Store()

    class Library:
        def list_voices(self):
            return [first, second]

    monkeypatch.setattr(
        voice_assign,
        "engine_caps",
        lambda: {"fake": {"label": "假引擎", "ready": True, "reference": True}},
    )
    ctx = AssistContext(
        service=Service(),
        library=Library(),
        settings=None,
        project_id="p",
        body={},
    )
    prepared = VoiceAssignTask().build(ctx)
    assert prepared.unassigned == ("阿明",)
    assert prepared.scope == ("阿明",)
    assert len(prepared.candidates) == 2
    assert len(prepared.pool) == 2
    assert prepared.semantic is False
    assert prepared.dominant_engine == "fake"
    # 对白不进提示词：模型判断音色靠画像，不靠几句截断的台词。
    assert "lines" not in prepared.roles[0]
    assert "profile" not in prepared.roles[0]


def _adjust_ctx(body, monkeypatch):
    """一个已指定音色的角色 + 一个未指定的角色；用来测「点名调整」。"""
    first = _fake_voice("voice-1", "胡桃")
    second = _fake_voice("voice-2", "钟离")

    class Store:
        def project(self, _project_id):
            return {"id": "p", "name": "样书"}

        def speakers(self, _project_id):
            return [
                {"speaker": "阿明", "rows": 4, "voice_id": ""},
                {"speaker": "小美", "rows": 3, "voice_id": "voice-2", "voice_name": "钟离"},
            ]

        def voice_map(self, _project_id):
            return {
                "小美": {"engine": "fake", "voice_id": "voice-2", "voice_name": "钟离"}
            }

        def speaker_lines(self, _project_id, names, limit=2):
            return {name: ["坐吧。"] for name in names}

    class Service:
        store = Store()

    class Library:
        def list_voices(self):
            return [first, second]

    monkeypatch.setattr(
        voice_assign,
        "engine_caps",
        lambda: {
            "fake": {
                "label": "假引擎",
                "ready": True,
                "reference": True,
                "kind": "local",
                "modes": ["clone"],
                "default_mode": "clone",
            }
        },
    )
    return AssistContext(
        service=Service(),
        library=Library(),
        settings=None,
        project_id="p",
        body=body,
    ), first


def test_voice_task_can_target_a_role_that_already_has_a_voice(monkeypatch) -> None:
    """点名调整：已指定音色的角色也能进来，roles 带 current 供模型参照。"""
    ctx, first = _adjust_ctx(
        {
            "speakers": ["小美"],
            "choices": {"小美": {"engine": "fake", "mode": "clone"}},
        },
        monkeypatch,
    )
    task = VoiceAssignTask()
    prepared = task.build(ctx)

    assert prepared.scope == ("小美",)
    # 未指定是全书口径：阿明还空着，只是这次没让他参与。
    assert prepared.unassigned == ("阿明",)
    assert prepared.roles[0]["current"]["voice_id"] == "voice-2"

    proposal = task.validate(
        prepared,
        {"assignments": [{"speaker": "小美", "voice": "v1", "reason": "更年轻"}], "unmatched": []},
    )
    row = proposal["assignments"][0]
    assert row["voice_id"] == first.id
    assert proposal["unmatched"] == []

    # 只有她自己在用的音色不算「复用」——调整时不该被这条提示吓住。
    same = task.validate(
        prepared,
        {"assignments": [{"speaker": "小美", "voice": "v2", "reason": "保持"}], "unmatched": []},
    )
    assert same["assignments"][0]["shared"] is False

    preview = task.preview(prepared, proposal)
    assert preview["assignments"][0]["current"]["voice_name"] == "钟离"


def test_voice_task_scope_rejects_roles_outside_the_pick(monkeypatch) -> None:
    """点名的表之外的角色不给建议，也不悄悄换成「只补未指定」。"""
    ctx, _ = _adjust_ctx(
        {
            "speakers": ["小美"],
            "choices": {"小美": {"engine": "fake", "mode": "clone"}},
        },
        monkeypatch,
    )
    prepared = VoiceAssignTask().build(ctx)
    assert prepared.unassigned == ("阿明",)

    proposal = VoiceAssignTask().validate(
        prepared,
        {"assignments": [{"speaker": "阿明", "voice": "v1", "reason": "沉稳"}], "unmatched": []},
    )
    assert proposal["assignments"] == []
    assert proposal["rejected"][0]["speaker"] == "阿明"

    unknown, _ = _adjust_ctx({"speakers": ["查无此人"]}, monkeypatch)
    with pytest.raises(AssistError, match="角色"):
        VoiceAssignTask().build(unknown)


def _choice_ctx(library_voices, choices, speakers=None):
    """两个未指定角色 + 一份 choices；库与引擎由用例给。"""

    class Store:
        def project(self, _project_id):
            return {"id": "p", "name": "样书"}

        def speakers(self, _project_id):
            return [
                {"speaker": "阿明", "rows": 4, "voice_id": ""},
                {"speaker": "小美", "rows": 3, "voice_id": ""},
            ]

        def voice_map(self, _project_id):
            return {}

        def speaker_lines(self, _project_id, names, limit=2):
            return {name: [] for name in names}

    class Service:
        store = Store()

    class Library:
        def list_voices(self):
            return library_voices

    body = {"choices": choices}
    if speakers is not None:
        body["speakers"] = speakers
    return AssistContext(
        service=Service(),
        library=Library(),
        settings=None,
        project_id="p",
        body=body,
    )


def _local_cloud_caps() -> dict[str, dict[str, object]]:
    return {
        "local": {
            "label": "本地引擎",
            "ready": True,
            "reference": True,
            "kind": "local",
            "listing": False,
            "design": False,
            "modes": ["clone"],
            "default_mode": "clone",
        },
        "cloud": {
            "label": "云端引擎",
            "ready": True,
            "reference": False,
            "kind": "cloud",
            "listing": True,
            "design": False,
            "modes": ["preset"],
            "default_mode": "preset",
        },
    }


def test_voice_choices_default_to_local_clone(monkeypatch) -> None:
    """未选引擎与方式：本地引擎 + 参考音频克隆；指定云端则用预置音色。"""
    local_voice = _fake_voice("local-1", "本地女声", sample_count=2)
    cloud_voice = Voice(
        id="cloud:1", source="cloud", name="V1", display="云端女声", sample_count=0
    )
    info = _local_cloud_caps()
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [local_voice, cloud_voice],
        {"阿明": {}, "小美": {"engine": "cloud", "mode": "preset"}},
    )

    prepared = VoiceAssignTask().build(ctx)

    assert prepared.prefs["阿明"] == {
        "engine": "local",
        "mode": "clone",
        "design_prompt": "",
    }
    assert prepared.prefs["小美"]["engine"] == "cloud"
    assert prepared.prefs["小美"]["mode"] == "preset"
    roles = {role["speaker"]: role for role in prepared.roles}
    assert roles["阿明"]["candidates"] == ["v1"]
    assert roles["小美"]["candidates"] == ["v2"]


def test_voice_choices_reject_a_voice_outside_the_role_candidates(monkeypatch) -> None:
    local_voice = _fake_voice("local-1", "本地女声", sample_count=2)
    cloud_voice = Voice(
        id="cloud:1", source="cloud", name="V1", display="云端女声", sample_count=0
    )
    info = _local_cloud_caps()
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [local_voice, cloud_voice],
        {"阿明": {}, "小美": {"engine": "cloud", "mode": "preset"}},
    )
    prepared = VoiceAssignTask().build(ctx)

    proposal = VoiceAssignTask().validate(
        prepared, {"assignments": [{"speaker": "阿明", "voice": "v2"}], "unmatched": []}
    )

    assert proposal["assignments"] == []
    assert proposal["rejected"][0]["reason"] == "音色不在该角色可选范围内"


def test_voice_choices_design_needs_no_candidate(monkeypatch) -> None:
    info = _local_cloud_caps()
    info["cloud"]["modes"] = ["preset", "design"]
    info["cloud"]["design"] = True
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [],
        {
            "阿明": {
                "engine": "cloud",
                "mode": "design",
                "design_prompt": "低沉的老者。",
            },
            "小美": {"engine": "cloud", "mode": "design"},
        },
    )

    prepared = VoiceAssignTask().build(ctx)

    assert prepared.pool == ()
    proposal = VoiceAssignTask().validate(
        prepared, {"assignments": [], "unmatched": []}
    )
    rows = {item["speaker"]: item for item in proposal["assignments"]}
    assert rows["阿明"]["mode"] == "design"
    assert rows["阿明"]["design_prompt"] == "低沉的老者。"
    assert rows["阿明"]["voice_id"] == ""
    assert proposal["unmatched"] == [
        {"speaker": "小美", "reason": "音色设计缺少描述（可先自己填一句）"}
    ]
    preview = VoiceAssignTask().preview(prepared, proposal)
    assert preview["assignments"][0]["voice"]["name"] == "（按描述新建）"


def test_design_role_without_prompt_is_rejected_once(monkeypatch) -> None:
    """全是设计角色、用户没填、模型一条也没给：按校验失败回喂，而不是当成结果。"""
    info = _local_cloud_caps()
    info["cloud"]["modes"] = ["preset", "design"]
    info["cloud"]["design"] = True
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [],
        {"阿明": {"engine": "cloud", "mode": "design"}},
        speakers=["阿明"],
    )
    prepared = VoiceAssignTask().build(ctx)

    with pytest.raises(AssistInvalid) as caught:
        VoiceAssignTask().validate(
            prepared,
            {
                "assignments": [],
                "unmatched": [
                    {"speaker": "阿明", "reason": "候选音色表为空，无法选编号"}
                ],
            },
        )

    assert "阿明" in str(caught.value)
    assert "音色设计" in str(caught.value)


def test_design_role_hides_the_empty_candidate_list(monkeypatch) -> None:
    """提示词不给设计角色发空的 candidates，并把设计规则放到前面。"""
    info = _local_cloud_caps()
    info["cloud"]["modes"] = ["preset", "design"]
    info["cloud"]["design"] = True
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [],
        {"阿明": {"engine": "cloud", "mode": "design"}},
        speakers=["阿明"],
    )
    task = VoiceAssignTask()
    prepared = task.build(ctx)

    role = prepared.roles[0]
    assert role["mode"] == "design"
    assert "candidates" not in role
    prompt = task.messages(prepared)[1].content
    assert "3. mode=design 的是音色设计" in prompt
    assert '"candidates": []' not in prompt


def test_design_prompt_can_come_from_the_model(monkeypatch) -> None:
    """音色设计不必先手打一段话：用户没填，模型当场写的那句就是凭据。

    用户填了则照用户的——模型只是补空，不是覆盖。
    """
    info = _local_cloud_caps()
    info["cloud"]["modes"] = ["preset", "design"]
    info["cloud"]["design"] = True
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [],
        {
            "阿明": {"engine": "cloud", "mode": "design"},
            "小美": {
                "engine": "cloud",
                "mode": "design",
                "design_prompt": "用户自己写的那句。",
            },
        },
    )
    prepared = VoiceAssignTask().build(ctx)

    proposal = VoiceAssignTask().validate(
        prepared,
        {
            "assignments": [
                {
                    "speaker": "阿明",
                    "design": "五十岁男声，沙哑低沉，语速偏慢。",
                    "reason": "年纪偏大",
                },
                {"speaker": "小美", "design": "模型另写的一句", "reason": "x"},
            ],
            "unmatched": [],
        },
    )

    rows = {item["speaker"]: item for item in proposal["assignments"]}
    assert rows["阿明"]["mode"] == "design"
    assert rows["阿明"]["design_prompt"] == "五十岁男声，沙哑低沉，语速偏慢。"
    assert rows["阿明"]["reason"] == "年纪偏大"
    assert rows["小美"]["design_prompt"] == "用户自己写的那句。"
    assert rows["小美"]["reason"] == "用户描述"
    assert proposal["unmatched"] == []


def test_design_description_is_final_and_cleaned(monkeypatch) -> None:
    """设计描述在同一次回复里写定：清洗后直接落库，不再二次改写。

    用户自己填的那一行是定稿，模型给的那句不覆盖它。
    """
    info = _local_cloud_caps()
    info["cloud"]["modes"] = ["preset", "design"]
    info["cloud"]["design"] = True
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: info)
    ctx = _choice_ctx(
        [],
        {
            "阿明": {"engine": "cloud", "mode": "design"},
            "小美": {
                "engine": "cloud",
                "mode": "design",
                "design_prompt": "用户自己写的那句。",
            },
        },
    )
    task = VoiceAssignTask()
    prepared = task.build(ctx)

    # 提示词要求直接写最终描述，不再说「之后重写」。
    prompt = task.messages(prepared)[1].content
    assert "最终落库的音色描述" in prompt
    assert voice_assign.voice_design.BRIEF_RULES in prompt
    assert "重写一次" not in prompt

    proposal = task.validate(
        prepared,
        {
            "assignments": [
                {
                    "speaker": "阿明",
                    "design": "音色描述：「五十岁男声，沙哑低沉。」\n「语速偏慢，像在讲旧事。」",
                    "reason": "年纪偏大",
                },
                {"speaker": "小美", "design": "模型另写的一句", "reason": "x"},
            ],
            "unmatched": [],
        },
    )

    rows = {item["speaker"]: item for item in proposal["assignments"]}
    assert rows["阿明"]["design_prompt"] == "五十岁男声，沙哑低沉。语速偏慢，像在讲旧事。"
    assert rows["小美"]["design_prompt"] == "用户自己写的那句。"


def test_voice_options_prefers_a_ready_local_engine(monkeypatch) -> None:
    info = _local_cloud_caps()
    # 云端排在前面，仍应先选本地。
    reordered = {"cloud": info["cloud"], "local": info["local"]}
    monkeypatch.setattr(voice_assign, "engine_caps", lambda: reordered)

    options = voice_assign.voice_options()

    assert options["default_engine"] == "local"
    assert options["default_mode"] == "clone"
    assert [item["key"] for item in options["engines"]] == ["cloud", "local"]
    assert "voices" not in options  # 没给库就不下发清单，老调用不变


def test_voice_options_ships_a_filtered_library(monkeypatch) -> None:
    """手动挑音色要的那份清单：能落到哪台引擎由后端算，没有样例的不出现。"""
    monkeypatch.setattr(voice_assign, "engine_caps", _local_cloud_caps)

    class Library:
        def list_voices(self):
            return [
                _fake_voice("v1", "胡桃"),
                _fake_voice("v2", "没样例的", sample_count=0),
            ]

    options = voice_assign.voice_options(Library())

    assert [row["id"] for row in options["voices"]] == ["v1"]
    assert options["voices"][0]["engines"] == ["local"]
    assert options["voices"][0]["samples"] == 2
    assert options["voices"][0]["design"] is False
    assert options["voices"][0]["design_prompt"] == ""


def test_allowed_engines_lets_samples_travel_across_platforms() -> None:
    """带样例的平台音色可以给别的克隆引擎用——样例是通用素材。

    没有样例时只剩自己的平台：那个音色 id 别的引擎不认识。
    """
    info = {
        "minimax": {
            "label": "MiniMax",
            "ready": True,
            "reference": False,
            "reference_supply": False,
            "listing": True,
            "modes": ["preset"],
            "default_mode": "preset",
        },
        "mimo": {
            "label": "MiMo",
            "ready": True,
            "reference": True,
            "reference_supply": False,
            "listing": True,
            "modes": ["preset", "clone"],
            "default_mode": "preset",
        },
    }
    with_samples = Voice(id="minimax:1", source="minimax", name="v1", sample_count=2)
    without = Voice(id="minimax:2", source="minimax", name="v2", sample_count=0)

    assert voice_assign.allowed_engines(with_samples, info) == ["minimax", "mimo"]
    assert voice_assign.allowed_engines(without, info) == ["minimax"]


def test_voice_options_carries_the_design_prompt(monkeypatch) -> None:
    """设计音色的描述要一起下发：调整时输入框里填得回原话。"""
    monkeypatch.setattr(
        voice_assign,
        "engine_caps",
        lambda: {
            "mimo": {
                "label": "MiMo",
                "ready": True,
                "reference": True,
                "listing": True,
                "design": True,
                "modes": ["preset", "clone", "design"],
                "default_mode": "preset",
            }
        },
    )
    designed = replace(
        _fake_voice("v9", "老者旁白"),
        source="mimo",
        payload={"design_prompt": "低沉的老者。"},
    )

    class Library:
        def list_voices(self):
            return [designed]

    row = voice_assign.voice_options(Library())["voices"][0]
    assert row["design"] is True
    assert row["design_prompt"] == "低沉的老者。"


def _fresh_profile_cache(monkeypatch, tmp_path):
    voice_profile._CACHE.clear()
    voice_profile._LOADED = False
    monkeypatch.setattr(voice_profile, "_cache_path", lambda: tmp_path / "profiles.json")


def _profile_reply(**fields) -> dict:
    row = {"speaker": "老周", "known": True, "gender": "男", "age": "老年", "timbre": ["低沉"]}
    row.update(fields)
    return {"text": json.dumps({"roles": [row]}, ensure_ascii=False)}


def test_voice_profile_book_title_strips_import_markers() -> None:
    """项目名来自文件名：带版本、丛书、来源站的书名模型认不出来。"""
    assert (
        voice_profile._book_title("山音（2021年版修订版） (川端康成作品集) (（日_ (Z-Library)")
        == "山音"
    )
    assert voice_profile._book_title("罪与罚 (陀思妥耶夫斯基；汝龙 译；) (Z-Library)") == "罪与罚"
    assert voice_profile._book_title("雨夜回声") == "雨夜回声"
    assert voice_profile._book_title("（上）") == "（上）"


def test_voice_profile_sends_author_with_the_title(monkeypatch, tmp_path) -> None:
    """同名书很多：作者进了书名括号，模型才认得出是哪一个。"""
    _fresh_profile_cache(monkeypatch, tmp_path)
    prompts: list[str] = []

    async def fake_chat(body):
        prompts.append(body["messages"][1].content)
        return _profile_reply(speaker="信吾", age="老年")

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)

    profiles, _ = asyncio.run(
        voice_profile.ensure(
            "山音（2021年版修订版） (川端康成作品集) (（日_ (Z-Library)",
            [("信吾", 40)],
            author="川端康成（日）",
        )
    )

    assert "《山音》（川端康成 著）" in prompts[0]
    assert profiles["信吾"]["age"] == "老年"


def test_voice_profile_falls_back_to_author_in_the_name(monkeypatch, tmp_path) -> None:
    """用户没填作者时，从文件名括号里捞：清洗书名会把这条线索一起切掉。"""
    _fresh_profile_cache(monkeypatch, tmp_path)
    prompts: list[str] = []

    async def fake_chat(body):
        prompts.append(body["messages"][1].content)
        return _profile_reply(speaker="信吾")

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)

    asyncio.run(
        voice_profile.ensure(
            "山音（2021年版修订版） (川端康成作品集) (（日_ (Z-Library)",
            [("信吾", 40)],
        )
    )

    assert "《山音》（川端康成 著）" in prompts[0]
    assert voice_profile._author_from_name("罪与罚 (陀思妥耶夫斯基；汝龙 译；) (Z-Library)") == (
        "陀思妥耶夫斯基"
    )
    assert voice_profile._author_from_name("雨夜回声") == ""


def test_voice_profile_cache_separates_authors(monkeypatch, tmp_path) -> None:
    """同名不同作者是两本书，画像不能复用。"""
    _fresh_profile_cache(monkeypatch, tmp_path)
    calls: list[str] = []

    async def fake_chat(body):
        calls.append(body["messages"][1].content)
        return _profile_reply(speaker="沟口")

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)

    asyncio.run(voice_profile.ensure("金阁寺", [("沟口", 10)], author="三岛由纪夫"))
    asyncio.run(voice_profile.ensure("金阁寺", [("沟口", 10)], author="水上勉"))
    assert len(calls) == 2

    asyncio.run(voice_profile.ensure("金阁寺", [("沟口", 10)], author="三岛由纪夫"))
    assert len(calls) == 2


def test_voice_profile_failure_is_not_reported_as_unknown(monkeypatch, tmp_path) -> None:
    """请求失败只说明没拿到，不能写成「模型不认识这个角色」。"""
    _fresh_profile_cache(monkeypatch, tmp_path)

    async def boom(body):
        raise RuntimeError("网络断了")

    monkeypatch.setattr(voice_profile.llm_uc, "chat", boom)

    profiles, note = asyncio.run(voice_profile.ensure("样书", [("老周", 3)]))

    assert profiles["老周"] == {}
    assert "没拿到" in note


def test_voice_profile_researches_online_then_extracts_json(monkeypatch, tmp_path) -> None:
    """联网由平台执行：先要一次带搜索的回答，正文不是 JSON 时补一次纯 JSON。"""
    _fresh_profile_cache(monkeypatch, tmp_path)
    sent: list[dict] = []

    async def fake_chat(body):
        sent.append(dict(body))
        if body.get("web_search"):
            return {"text": "老周是年近七十的老人，声音低沉。"}
        return _profile_reply()

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)

    profiles, note = asyncio.run(voice_profile.ensure("样书", [("老周", 3)]))

    assert profiles["老周"]["age"] == "老年"
    assert note == ""
    # 第一轮声明服务端搜索、不给 JSON 约束；第二轮只做抽取，不再联网。
    assert sent[0]["web_search"] is True
    assert "json_object" not in sent[0]
    assert sent[1]["json_object"] is True
    assert "web_search" not in sent[1]
    assert "年近七十" in sent[1]["messages"][-2].content
    assert sent[1]["messages"][-2].role == "assistant"


def test_voice_profile_falls_back_when_research_fails(monkeypatch, tmp_path) -> None:
    """联网这一步失败（通道不支持 / 中转拒绝）时退回普通 JSON 对话，画像照写。"""
    _fresh_profile_cache(monkeypatch, tmp_path)
    sent: list[dict] = []

    async def fake_chat(body):
        sent.append(dict(body))
        if body.get("web_search"):
            raise RuntimeError("平台不支持联网")
        return _profile_reply()

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)

    profiles, note = asyncio.run(voice_profile.ensure("样书", [("老周", 3)]))

    assert profiles["老周"]["age"] == "老年"
    assert note == ""
    assert sent[1]["json_object"] is True
    assert "web_search" not in sent[1]


def test_voice_profile_research_failure_is_a_degrade(monkeypatch, tmp_path) -> None:
    """联网与退路都失败时只说明没拿到，不让整次分配失败，也不写成「不认识」。"""
    _fresh_profile_cache(monkeypatch, tmp_path)

    async def boom(body):
        raise RuntimeError("搜索服务挂了")

    monkeypatch.setattr(voice_profile.llm_uc, "chat", boom)

    profiles, note = asyncio.run(voice_profile.ensure("样书", [("老周", 3)]))

    assert profiles == {"老周": {}}
    assert "没拿到" in note


def test_voice_profile_normalizes_and_caches(monkeypatch, tmp_path) -> None:
    """画像只问一次、只收认识的角色，认不出的标成 false 而不是硬编。"""
    voice_profile._CACHE.clear()
    voice_profile._LOADED = False
    monkeypatch.setattr(voice_profile, "_cache_path", lambda: tmp_path / "profiles.json")
    calls: list[dict] = []

    async def fake_chat(body):
        calls.append(dict(body))
        return {
            "text": json.dumps(
                {
                    "roles": [
                        {
                            "speaker": "阿明",
                            "known": True,
                            "gender": "男",
                            "age": "青年",
                            "timbre": ["低沉", "沙哑", "低沉"],
                            "tone": ["沉稳"],
                            "pace": "偏慢",
                            "note": "族长",
                        },
                        {"speaker": "小美", "known": False, "gender": "女"},
                        {"speaker": "书里没有的人", "known": True},
                    ]
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(voice_profile.llm_uc, "chat", fake_chat)
    roles = [("阿明", 3), ("小美", 1)]
    profiles, note = asyncio.run(voice_profile.ensure("样书", roles))

    assert note == ""
    assert profiles["阿明"]["timbre"] == ["低沉", "沙哑"]  # 去重
    assert profiles["阿明"]["pace"] == "偏慢"
    # 模型说不知道就是不认识：不保留它顺手填的性别。
    assert profiles["小美"] == {"known": False}
    assert "书里没有的人" not in profiles
    prompt = calls[0]["messages"][1].content
    assert "《样书》" in prompt
    assert "阿明（3 行）" in prompt
    assert "对白" not in prompt

    asyncio.run(voice_profile.ensure("样书", roles))
    assert len(calls) == 1  # 第二次全部命中缓存
    assert (tmp_path / "profiles.json").is_file()


def test_voice_profile_text_only_carries_voice_words() -> None:
    profile = {
        "known": True,
        "gender": "男",
        "age": "中年",
        "timbre": ["低沉"],
        "tone": ["克制"],
        "pace": "偏慢",
        "note": "族长",
    }
    assert voice_profile.text(profile) == "男，中年；音色：低沉；语气：克制；语速偏慢"
    assert voice_profile.summary(profile) == "男 · 中年 · 低沉"
    assert voice_profile.text({"known": False}) == ""
    assert voice_profile.summary({"known": False}) == "模型不认识这个角色"


def test_voice_task_profiles_roles_without_dialogue(monkeypatch) -> None:
    seen: dict = {}

    async def fake_ensure(book, roles, *, author="", provider="", model=""):
        seen["book"] = book
        seen["author"] = author
        seen["roles"] = list(roles)
        return (
            {
                "阿明": {
                    "known": True,
                    "gender": "男",
                    "age": "青年",
                    "timbre": ["低沉"],
                    "tone": ["沉稳"],
                    "pace": "偏慢",
                    "note": "",
                }
            },
            "",
        )

    monkeypatch.setattr(voice_assign.voice_profile, "ensure", fake_ensure)
    monkeypatch.setattr("app.embedding.service.ready_provider", lambda: None)
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    prepared = asyncio.run(
        VoiceAssignTask().enrich(
            ctx, replace(_voice_prepared(), project_author="川端康成")
        )
    )

    # 只发书名、作者与「角色名 + 行数」：不发对白，也不发候选。
    assert seen["book"] == "样书"
    assert seen["author"] == "川端康成"
    assert seen["roles"] == [("阿明", 12), ("小美", 8), ("老周", 3)]
    assert prepared.roles[0]["profile"] == "男 · 青年 · 低沉"
    assert prepared.profiles["阿明"]["pace"] == "偏慢"
    prompt = VoiceAssignTask().messages(prepared)[1].content
    assert "低沉" in prompt
    assert "lines" not in prompt


def test_voice_task_profile_failure_keeps_assigning(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(voice_assign.voice_profile, "ensure", boom)
    monkeypatch.setattr("app.embedding.service.ready_provider", lambda: None)
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    prepared = asyncio.run(VoiceAssignTask().enrich(ctx, _voice_prepared()))

    assert any("角色画像没拿到" in warning for warning in prepared.warnings)
    assert prepared.candidates  # 仍按名字顺序给出候选，只是没画像
    assert all("profile" not in role for role in prepared.roles)


def test_voice_task_marks_unknown_roles(monkeypatch) -> None:
    async def fake_ensure(book, roles, *, author="", provider="", model=""):
        return {name: {"known": False} for name, _ in roles}, ""

    monkeypatch.setattr(voice_assign.voice_profile, "ensure", fake_ensure)
    monkeypatch.setattr("app.embedding.service.ready_provider", lambda: None)
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    prepared = asyncio.run(VoiceAssignTask().enrich(ctx, _voice_prepared()))

    assert prepared.roles[0]["profile"] == "模型不认识这个角色"
    proposal = VoiceAssignTask().validate(
        prepared,
        {"assignments": [{"speaker": "阿明", "voice": "v1", "reason": "合适"}], "unmatched": []},
    )
    preview = VoiceAssignTask().preview(prepared, proposal)
    assert preview["profile_count"] == 3
    assert preview["assignments"][0]["profile"] == "模型不认识这个角色"
    assert "sources" not in preview["assignments"][0]


class _KeywordEmbedding:
    """把「沉稳 / 活泼」各占一维，足够验证排序，不碰真模型。"""

    key = "fake"
    version = "1"

    def embed(self, texts, *, query=False):
        return [
            [1.0 if "沉稳" in text else 0.0, 1.0 if "活泼" in text else 0.0]
            for text in texts
        ]


class _BrokenEmbedding:
    key = "broken"
    version = "1"

    def embed(self, texts, *, query=False):
        raise RuntimeError("模型坏了")


class _PersistentKeywordEmbedding:
    """同款关键词向量，但带模型目录，用来验证磁盘缓存跨进程复用。"""

    key = "fake_persist"
    version = "1"
    dimension = 2

    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls = 0

    def model_dir(self) -> Path:
        return self._root

    def embed(self, texts, *, query=False):
        self.calls += 1
        return [
            [1.0 if "沉稳" in text else 0.0, 1.0 if "活泼" in text else 0.0]
            for text in texts
        ]


def _tagged_voice(voice_id: str, name: str, tag: str) -> Voice:
    return Voice(
        id=voice_id,
        source="local",
        name=name,
        display=name,
        gender="女",
        age="青年",
        language="中文",
        tags=(tag,),
        sample_count=1,
    )


def _large_pool(size: int = 250) -> tuple[tuple[Voice, tuple[str, ...]], ...]:
    steady = _tagged_voice("steady", "沉稳叔", "沉稳")
    voices = [steady] + [
        _tagged_voice(f"plain-{index}", f"路人{index:03d}", "日常")
        for index in range(size - 1)
    ]
    return tuple((voice, ("fake",)) for voice in voices)


_STEADY_PROFILE = {
    "known": True,
    "gender": "男",
    "age": "青年",
    "timbre": ["沉稳"],
    "tone": [],
    "pace": "",
    "note": "",
}


async def _fake_profiles(book, roles, *, author="", provider="", model=""):
    return {"阿明": dict(_STEADY_PROFILE)}, ""


def _one_role_prepared(
    pool: tuple[tuple[Voice, tuple[str, ...]], ...],
    *,
    role_allowed: dict[str, tuple[str, ...]] | None = None,
) -> Prepared:
    role = {"speaker": "阿明", "rows": 1, "profile": "男 · 青年 · 沉稳", "candidates": []}
    return replace(
        _voice_prepared(),
        roles=(role,),
        role_map={"阿明": role},
        unassigned=("阿明",),
        pool=pool,
        profiles={"阿明": dict(_STEADY_PROFILE)},
        role_allowed=role_allowed or {},
    )


def test_voice_task_semantic_recall_keeps_best_of_large_pool(monkeypatch) -> None:
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    monkeypatch.setattr(
        "app.embedding.service.ready_provider", lambda: _KeywordEmbedding()
    )
    prepared = _one_role_prepared(_large_pool())
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    recalled = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    assert recalled.semantic is True
    assert len(recalled.pool) == 250
    # 画像是「沉稳」，候选收到 40 条：够模型挑，也不让无关音色稀释结论。
    assert len(recalled.candidates) == 40
    assert recalled.candidates[0]["name"] == "沉稳叔"
    assert "steady" in recalled.voice_by_id
    preview = VoiceAssignTask().preview(recalled, {"assignments": [], "unmatched": []})
    assert preview["semantic"] is True
    assert preview["candidate_pool"] == 250
    assert preview["candidate_count"] == 40


def test_voice_task_recall_respects_each_role_engine(monkeypatch) -> None:
    """点名指定了引擎的角色，召回的候选只能落在它的范围内。"""
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    monkeypatch.setattr(
        "app.embedding.service.ready_provider", lambda: _KeywordEmbedding()
    )
    prepared = _one_role_prepared(_large_pool(), role_allowed={"阿明": ("steady",)})
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    recalled = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    keys = recalled.roles[0]["candidates"]
    assert [recalled.candidate_map[key].voice.id for key in keys] == ["steady"]
    # 全局补满的部分仍然可以出现别的音色，但不在这个角色的可选范围内。
    assert len(recalled.candidates) > 1


def test_voice_task_without_model_keeps_order_and_points_to_settings(monkeypatch) -> None:
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    monkeypatch.setattr("app.embedding.service.ready_provider", lambda: None)
    prepared = _one_role_prepared(_large_pool())
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    recalled = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    assert recalled.semantic is False
    assert recalled.candidates == prepared.candidates
    assert any("语义检索" in warning for warning in recalled.warnings)


def test_voice_task_semantic_failure_does_not_block(monkeypatch) -> None:
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    monkeypatch.setattr(
        "app.embedding.service.ready_provider", lambda: _BrokenEmbedding()
    )
    prepared = _one_role_prepared(_large_pool())
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    recalled = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    assert recalled.semantic is False
    assert recalled.candidates == prepared.candidates
    assert any("语义召回失败" in warning for warning in recalled.warnings)


def test_voice_task_semantic_recall_keeps_small_pool_whole(monkeypatch) -> None:
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    monkeypatch.setattr(
        "app.embedding.service.ready_provider", lambda: _KeywordEmbedding()
    )
    first = _tagged_voice("voice-1", "胡桃", "活泼")
    second = _tagged_voice("voice-2", "钟离", "沉稳")
    prepared = _one_role_prepared(((first, ("fake",)), (second, ("fake",))))
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    recalled = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    assert recalled.semantic is True
    assert len(recalled.candidates) == 2
    assert recalled.candidates[0]["name"] == "钟离"


def test_voice_task_semantic_vectors_survive_restart(monkeypatch, tmp_path) -> None:
    voice_recall._VECTORS.clear()
    monkeypatch.setattr(voice_assign.voice_profile, "ensure", _fake_profiles)
    provider = _PersistentKeywordEmbedding(tmp_path / "model")
    monkeypatch.setattr(
        "app.embedding.service.ready_provider", lambda: provider
    )
    prepared = _one_role_prepared(
        (
            (_tagged_voice("steady", "沉稳叔", "沉稳"), ("fake",)),
            (_tagged_voice("lively", "活泼妹", "活泼"), ("fake",)),
        )
    )
    ctx = type("C", (), {"body": {}, "settings": None, "project_id": "p"})()

    first = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))
    assert first.semantic is True
    assert provider.calls > 0
    assert (tmp_path / "model" / "vectors.db").is_file()

    voice_recall._VECTORS.clear()
    provider.calls = 0
    second = asyncio.run(VoiceAssignTask().enrich(ctx, prepared))

    assert second.semantic is True
    assert provider.calls == 0
    assert second.candidates[0]["name"] == "沉稳叔"


def test_store_speaker_lines_and_set_voices(tmp_path: Path) -> None:
    service = AudiobookService(tmp_path)
    project = service.create("样书", SAMPLE)
    assert service.store.speaker_lines(project["id"], ["甲"]) == {"甲": []}

    rows = [
        {
            "speaker": "甲",
            "engine": "fake",
            "voice_id": "v1",
            "voice_name": "甲声",
            "sample_mode": "emotion",
            "sample_file": "",
        }
    ]
    assert service.store.set_voices(project["id"], rows) == ["甲"]
    assert service.store.set_voices(project["id"], rows) == []
    changed = service.store.set_voices(
        project["id"], [{**rows[0], "voice_id": "v2", "voice_name": "乙声"}]
    )
    assert changed == ["甲"]
    service.store.invalidate_speakers(project["id"], changed)


def test_voice_options_api_carries_the_library(client) -> None:
    """弹窗手动挑音色要的那份清单：接口给得出，每条带可用引擎。"""
    resp = client.get("/api/v1/producers/audiobook/assist/voice-options")
    assert resp.status_code == 200
    payload = resp.json()
    assert {"engines", "default_engine", "default_mode", "voices"} <= set(payload)
    assert isinstance(payload["voices"], list)
    for row in payload["voices"]:
        assert row["id"] and row["engines"]


def test_assist_api_status_and_regex(client, monkeypatch) -> None:
    created = client.post(
        "/api/v1/producers/audiobook/projects",
        json={"name": "样书", "text": SAMPLE},
    ).json()
    project_id = created["id"]

    status = client.get("/api/v1/producers/audiobook/assist/status").json()
    assert status["object"] == "assist_status"
    assert "ready" in status
    assert "search" in status

    async def fake_chat(_body):
        return {
            "text": json.dumps(
                {
                    "pattern": BRACKET,
                    "replacement": "",
                    "explanation": "删除方括号编号整行",
                },
                ensure_ascii=False,
            ),
            "provider": "fake",
            "model": "fake",
            "notes": [],
            "usage": {},
        }

    monkeypatch.setattr("app.usecase.llm.chat", fake_chat)
    response = client.post(
        "/api/v1/producers/audiobook/assist/regex_rule",
        json={
            "project_id": project_id,
            "kind": "bracket",
            "sample": "[1] 这是注释。",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["object"] == "assist_proposal"
    assert payload["proposal"]["pattern"] == BRACKET
    assert payload["preview"]["total"] == 1
