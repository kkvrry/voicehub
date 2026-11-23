"""实时对话插件：默认状态、规范化、背景与回复语音。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.producers.base import ProducerError
from app.producers.builtin.realtime_chat.service import RealtimeChatService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WAV = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 32


def test_default_state(client):
    response = client.get("/api/v1/producers/realtime_chat/state")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "realtime_chat_state"
    assert payload["version"] == 2
    assert payload["active_id"] == "xiaonuan"
    character = payload["characters"][0]
    assert character["name"] == "小暖"
    assert character["intro"] == "旧书店的夜班。话不多，只把话接住。"
    assert character["background"] == ""
    assert len(character["sessions"]) == 1
    session = character["sessions"][0]
    assert character["active_session"] == session["id"]
    assert session["messages"][0]["role"] == "assistant"


def test_save_normalizes_and_persists(client):
    body = {
        "active_id": "missing",
        "characters": [
            {
                "id": "hero",
                "name": "  阿夏  ",
                "intro": "  海边长大，说话带风声。  ",
                "persona": "x" * 2000,
                "voice_id": "v1",
                "voice_name": "清亮",
                "voice_engine": "engine-gone",
                "voice_engines": ["engine-a", "engine-b", "engine-a"],
                "voice_cover": "",
                "background": "builtin:not-a-room",
                "llm_provider": "provider-a",
                "llm_model": "model-a",
                "sessions": [
                    {
                        "id": "rec-old",
                        "created": 1,
                        "messages": [
                            {"id": "skip", "role": "narrator", "text": "旁白不算对话"},
                        ],
                    },
                    {
                        "id": "rec-now",
                        "created": 2,
                        "messages": [
                            {"id": "m1", "role": "user", "text": " 你好 ", "created": 1},
                            {
                                "id": "m2",
                                "role": "assistant",
                                "text": "在。",
                                "error": True,
                                "audio_error": "没有音色",
                                "notes": ["重试过", "重试过"],
                            },
                        ],
                    },
                ],
                # 指向一份不存在的记录：落到最后一份，不进空窗。
                "active_session": "gone",
            }
        ],
    }
    saved = client.put("/api/v1/producers/realtime_chat/state", json=body)
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["active_id"] == "hero"
    character = payload["characters"][0]
    assert character["name"] == "阿夏"
    assert character["intro"] == "海边长大，说话带风声。"
    assert len(character["persona"]) == 1200
    assert character["background"] == ""
    assert character["voice_engine"] == "engine-a"
    assert character["voice_engines"] == ["engine-a", "engine-b"]
    assert [row["id"] for row in character["sessions"]] == ["rec-old", "rec-now"]
    assert character["active_session"] == "rec-now"
    assert character["sessions"][0]["messages"] == []
    now = character["sessions"][1]
    assert [row["id"] for row in now["messages"]] == ["m1", "m2"]
    assert now["messages"][1]["error"] is True
    assert now["messages"][1]["notes"] == ["重试过", "重试过"]

    again = client.get("/api/v1/producers/realtime_chat/state").json()
    assert again["characters"][0]["name"] == "阿夏"
    assert len(again["characters"][0]["sessions"]) == 2


def test_engine_auto_and_sample_strategy(client):
    """合成引擎的自动档与样例策略要能原样存住；钉了样例却没文件时退回情绪匹配。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    char = state["characters"][0]
    char.update(
        {
            "voice_id": "v1",
            "voice_name": "清亮",
            "voice_engine": "auto",
            "voice_engines": ["engine-a", "engine-b"],
            "voice_local": True,
            "voice_sample_mode": "fixed",
            "voice_sample_file": "03_开心.wav",
        }
    )
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    character = saved["characters"][0]
    assert character["voice_engine"] == "auto"
    assert character["voice_local"] is True
    assert character["voice_sample_mode"] == "fixed"
    assert character["voice_sample_file"] == "03_开心.wav"

    # 钉了样例却没有文件：退回按情绪匹配，不留一个永远不会生效的档位。
    character["voice_sample_file"] = ""
    saved = client.put(
        "/api/v1/producers/realtime_chat/state", json={"characters": [character]}
    ).json()
    character = saved["characters"][0]
    assert character["voice_sample_mode"] == "emotion"
    assert character["voice_sample_file"] == ""

    # 旧状态里停用 / 写错的引擎键仍然落到这条音色可用的第一台。
    character["voice_engine"] = "engine-gone"
    saved = client.put(
        "/api/v1/producers/realtime_chat/state", json={"characters": [character]}
    ).json()
    assert saved["characters"][0]["voice_engine"] == "engine-a"


def test_legacy_messages_become_one_record(client):
    """版本 1 的状态只有一条消息数组：读进来要收成一份记录，聊到一半的内容不能丢。"""
    body = {
        "version": 1,
        "active_id": "old",
        "characters": [
            {
                "id": "old",
                "name": "旧角色",
                "messages": [
                    {"id": "a", "role": "user", "text": "在吗", "created": 1},
                    {"id": "b", "role": "assistant", "text": "在。", "created": 2},
                ],
            }
        ],
    }
    saved = client.put("/api/v1/producers/realtime_chat/state", json=body).json()
    character = saved["characters"][0]
    assert len(character["sessions"]) == 1
    session = character["sessions"][0]
    assert character["active_session"] == session["id"]
    assert [row["id"] for row in session["messages"]] == ["a", "b"]


def test_records_are_capped(client):
    sessions = [
        {
            "id": f"rec-{index}",
            "created": index,
            "messages": [{"role": "user", "text": f"第 {index} 次"}],
        }
        for index in range(40)
    ]
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    state["characters"][0]["sessions"] = sessions
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    kept = saved["characters"][0]["sessions"]
    assert len(kept) == 30
    # 留最新的那些：最旧的一份被挤出去。
    assert kept[0]["id"] == "rec-10"
    assert kept[-1]["id"] == "rec-39"


def test_background_upload_and_use(client):
    uploaded = client.post(
        "/api/v1/producers/realtime_chat/backgrounds",
        files={"file": ("wall.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["key"].startswith("custom:bg-")
    assert payload["name"].startswith("bg-")

    fetched = client.get(payload["url"])
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert fetched.headers["content-type"].startswith("image/png")

    state = client.get("/api/v1/producers/realtime_chat/state").json()
    state["characters"][0]["background"] = payload["key"]
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state)
    assert saved.status_code == 200
    assert saved.json()["characters"][0]["background"] == payload["key"]


def test_background_rejects_non_image(client):
    response = client.post(
        "/api/v1/producers/realtime_chat/backgrounds",
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )
    assert response.status_code == 400
    assert "PNG" in response.json()["error"]["message"]


def test_missing_custom_background_falls_back(client):
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    state["characters"][0]["background"] = "custom:gone.png"
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    assert saved["characters"][0]["background"] == ""


def test_builtin_backgrounds_keep_known_keys_and_drop_legacy(client):
    """四张随包背景要认得住；旧版本留下的键已经没有素材，落回默认而不是黑底。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    character = state["characters"][0]
    for key in ("study", "rain", "dawn", "neon"):
        character["background"] = f"builtin:{key}"
        saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
        assert saved["characters"][0]["background"] == f"builtin:{key}"
    for legacy in ("builtin:scene", "builtin:code", "builtin:aurora", "builtin:mist"):
        character["background"] = legacy
        saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
        assert saved["characters"][0]["background"] == ""


def test_portrait_only_accepts_shipped_art(client):
    """立绘只认插件包里那十一张；点了不认识的就当没选。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    character = state["characters"][0]
    assert character["portrait"] == ""
    for key in ("deepseek", "claude-chibi", "glm-wheelchair"):
        character["portrait"] = f"builtin:{key}"
        saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
        assert saved["characters"][0]["portrait"] == f"builtin:{key}"
    for bad in ("builtin:nobody", "custom:own.png", "deepseek"):
        character["portrait"] = bad
        saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
        assert saved["characters"][0]["portrait"] == ""


def test_portrait_transform_is_clamped(client):
    """立绘的站位与大小：默认靠左站，越界夹回边界，坏值回默认。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    character = state["characters"][0]
    assert character["portrait_x"] == 0.02
    assert character["portrait_y"] == 0
    assert character["portrait_scale"] == 1

    def put(**fields):
        character.update(fields)
        body = {"characters": [character]}
        saved = client.put("/api/v1/producers/realtime_chat/state", json=body).json()
        return saved["characters"][0]

    character = put(portrait_x=0.36, portrait_y=0.2, portrait_scale=1.25)
    assert character["portrait_x"] == 0.36
    assert character["portrait_y"] == 0.2
    assert character["portrait_scale"] == 1.25

    character = put(portrait_x=9, portrait_y=-3, portrait_scale=0.1)
    assert character["portrait_x"] == 0.72
    assert character["portrait_y"] == 0
    assert character["portrait_scale"] == 0.6

    # JSON 里的字符串数字能收。
    character = put(portrait_x="0.1", portrait_y="0.05", portrait_scale="1.2")
    assert character["portrait_x"] == 0.1
    assert character["portrait_y"] == 0.05
    assert character["portrait_scale"] == 1.2

    # 布尔 / 文本 / 缺字段回默认。
    character = put(portrait_x=True, portrait_y="左", portrait_scale=None)
    assert character["portrait_x"] == 0.02
    assert character["portrait_y"] == 0
    assert character["portrait_scale"] == 1


def test_model_choice_lives_on_state_and_migrates_from_character(client):
    """对话模型挂整份状态：旧状态写在角色里也要搬上来，换角色不跟着换模型。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    state["characters"][0]["llm_provider"] = "provider-a"
    state["characters"][0]["llm_model"] = "model-a"
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    assert saved["llm_provider"] == "provider-a"
    assert saved["llm_model"] == "model-a"
    assert "llm_provider" not in saved["characters"][0]
    assert "llm_model" not in saved["characters"][0]

    # 顶层已经选过：角色里再写别的也不该盖掉它。
    saved["characters"][0]["llm_provider"] = "provider-b"
    saved["characters"][0]["llm_model"] = "model-b"
    again = client.put("/api/v1/producers/realtime_chat/state", json=saved).json()
    assert again["llm_provider"] == "provider-a"
    assert again["llm_model"] == "model-a"


def test_background_file_rejects_paths(tmp_path: Path):
    service = RealtimeChatService(tmp_path / "realtime_chat")
    with pytest.raises(ProducerError):
        service.background_file("../state.json")


def test_corrupt_state_is_parked(tmp_path: Path):
    root = tmp_path / "realtime_chat"
    service = RealtimeChatService(root)
    (root / "state.json").write_text("{not json", encoding="utf-8")

    state = service.state()

    assert state["active_id"] == "xiaonuan"
    parked = list(root.glob("state.corrupt-*.json"))
    assert len(parked) == 1
    assert parked[0].read_text(encoding="utf-8") == "{not json"


def test_created_accepts_numbers_strings_and_falls_back(client):
    """``created`` 是手写状态里最容易写坏的一格：不能因为 "昨天" 这种值 500。"""
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    session = state["characters"][0]["sessions"][0]
    session["created"] = "1720000000123"
    session["messages"][0]["created"] = "不是数字"

    saved = client.put("/api/v1/producers/realtime_chat/state", json=state)
    assert saved.status_code == 200
    payload = saved.json()
    session = payload["characters"][0]["sessions"][0]
    assert session["created"] == 1720000000123
    assert isinstance(session["messages"][0]["created"], int)
    assert session["messages"][0]["created"] > 0


def test_load_tolerates_bad_created(tmp_path: Path):
    root = tmp_path / "realtime_chat"
    RealtimeChatService(root)
    state = {
        "version": 2,
        "active_id": "hero",
        "characters": [
            {
                "id": "hero",
                "name": "阿夏",
                "sessions": [
                    {
                        "id": "rec-1",
                        "created": "昨天",
                        "messages": [
                            {
                                "id": "m1",
                                "role": "user",
                                "text": "在吗",
                                "created": None,
                            }
                        ],
                    }
                ],
                "active_session": "rec-1",
            }
        ],
    }
    (root / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    loaded = RealtimeChatService(root).state()

    session = loaded["characters"][0]["sessions"][0]
    assert isinstance(session["created"], int)
    assert session["created"] > 0
    assert isinstance(session["messages"][0]["created"], int)
    assert not list(root.glob("state.corrupt-*.json"))


def test_load_parks_state_when_normalize_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "realtime_chat"
    service = RealtimeChatService(root)
    (root / "state.json").write_text('{"characters": []}', encoding="utf-8")

    def boom(_payload):
        raise ValueError("形状不对")

    monkeypatch.setattr(service, "_normalize", boom)
    state = service.state()

    assert state["active_id"] == "xiaonuan"
    parked = list(root.glob("state.corrupt-*.json"))
    assert len(parked) == 1


def test_audio_upload_serve_and_delete(client):
    uploaded = client.post(
        "/api/v1/producers/realtime_chat/audio",
        files={"file": ("reply.wav", WAV, "audio/wav")},
    )
    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["name"].startswith("reply-")
    assert payload["url"].endswith(payload["name"])

    fetched = client.get(payload["url"])
    assert fetched.status_code == 200
    assert fetched.content == WAV
    assert fetched.headers["content-type"].startswith("audio/")

    state = client.get("/api/v1/producers/realtime_chat/state").json()
    state["characters"][0]["sessions"][0]["messages"][0]["audio_url"] = payload["url"]
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    assert saved["characters"][0]["sessions"][0]["messages"][0]["audio_url"] == payload["url"]

    removed = client.delete(payload["url"])
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    # 删除后状态里不再留一个必然 404 的地址。
    again = client.get("/api/v1/producers/realtime_chat/state").json()
    assert again["characters"][0]["sessions"][0]["messages"][0]["audio_url"] == ""


def test_messages_only_keep_plugin_audio_urls(client):
    state = client.get("/api/v1/producers/realtime_chat/state").json()
    message = state["characters"][0]["sessions"][0]["messages"][0]
    message["audio_url"] = "/api/v1/generated/old-cache.wav?engine=x"
    saved = client.put("/api/v1/producers/realtime_chat/state", json=state).json()
    assert saved["characters"][0]["sessions"][0]["messages"][0]["audio_url"] == ""


def test_audio_file_rejects_paths(tmp_path: Path):
    service = RealtimeChatService(tmp_path / "realtime_chat")
    with pytest.raises(ProducerError):
        service.audio_file("../state.json")


def test_asr_stream_aborts_while_connecting():
    """连接途中 stop / abort / 卸载：start 不能在后台继续建链。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过流式客户端的行为检查")
    root = Path(__file__).resolve().parents[1]
    script = root / "tests" / "js" / "asr_stream_abort.mjs"
    result = subprocess.run(
        [node, str(script)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
