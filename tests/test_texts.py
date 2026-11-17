"""文案库（生成样例时"挑一句现成的"那几组句子）。

它从前**写死在前端**（加一句要改代码重构建），所以这一份测试守的是"它现在是
一份配置"这件事实，以及两条最容易做错的规则：

1. **文件不在 = 用默认，且不写盘**：写盘的话用户清空后一刷新又长回来，
   "删不掉"比"空着"更难解释；
2. **空的是有效配置**：用户可以把文案库删空，那时不能偷偷补回默认。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _groups(resp):
    return resp.json().get("groups", [])


def _names(groups):
    return [row["name"] for row in groups]


class TestDefaults:
    def test_without_a_file_we_serve_the_defaults(self, client, data_dir: Path):
        groups = _groups(client.get("/api/v1/texts"))
        assert groups, "没配过也该有得挑"
        # 分组是给用户看的：默认那几组要覆盖"念什么"的几种口味。
        assert {"现代诗", "哲学", "文学", "烂梗"} <= set(_names(groups))
        for row in groups:
            assert row["lines"], f"「{row['name']}」是空的组，摆出来也没得挑"

    def test_reading_does_not_write_the_file(self, client, data_dir: Path):
        """读一次不该顺手落盘：写过的文件从此就是"用户改过的"，
        默认那份再也不会跟着代码更新。"""
        client.get("/api/v1/texts")
        assert not (data_dir / "texts.json").exists()

    def test_shipped_defaults_are_not_truncated(self):
        """默认文件里的长段原样进内存；从前读默认就按 MAX_CHARS 截过一道。"""
        from app.infra import texts

        raw = json.loads(
            (Path(texts.__file__).parent / "texts_default.json").read_text(encoding="utf-8")
        )
        shipped = [
            [str(line).strip() for line in row["lines"] if str(line).strip()]
            for row in raw["groups"]
        ]
        assert [row["lines"] for row in texts.DEFAULT_GROUPS] == shipped


class TestSaving:
    def test_saved_groups_come_back_as_they_went(self, client, data_dir: Path):
        wanted = [
            {"name": "我的", "lines": ["第一句。", "第二句。"]},
            {"name": "诗句", "lines": ["落霞与孤鹜齐飞。"]},
        ]
        saved = _groups(client.put("/api/v1/texts", json={"groups": wanted}))
        assert saved == wanted
        assert _groups(client.get("/api/v1/texts")) == wanted
        assert (data_dir / "texts.json").exists(), "改过就该落盘"

    def test_an_empty_library_stays_empty(self, client):
        """空是有效配置：用户删空了就别再长回来。"""
        assert _groups(client.put("/api/v1/texts", json={"groups": []})) == []
        assert _groups(client.get("/api/v1/texts")) == []

    def test_blank_lines_are_dropped(self, client):
        saved = _groups(
            client.put(
                "/api/v1/texts", json={"groups": [{"name": "组", "lines": ["  ", "真的。", ""]}]}
            )
        )
        assert saved == [{"name": "组", "lines": ["真的。"]}]

    def test_a_long_line_is_stored_whole(self, client):
        """整段原典 / 长台词得能整条存进来：不报 400，也不悄悄截短。
        从前单句限 200 字，写盘报错、读盘截成 200 字。"""
        long_line = "他想到海潮的后浪推前浪，" * 60  # 720 字，远超旧上限
        long_name = "念起来很长的那一组" * 5
        wanted = [{"name": long_name, "lines": [long_line]}]
        assert _groups(client.put("/api/v1/texts", json={"groups": wanted})) == wanted
        assert _groups(client.get("/api/v1/texts")) == wanted

    def test_a_group_without_a_name_is_a_readable_400(self, client):
        resp = client.put("/api/v1/texts", json={"groups": [{"lines": ["没有名字。"]}]})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "texts_invalid"
        assert "名字" in resp.json()["error"]["message"]

    def test_too_many_groups_is_a_readable_400(self, client):
        rows = [{"name": f"组{i}", "lines": ["一句。"]} for i in range(25)]
        resp = client.put("/api/v1/texts", json={"groups": rows})
        assert resp.status_code == 400
        assert "组" in resp.json()["error"]["message"]

    def test_reset_restores_the_shipped_defaults(self, client):
        client.put("/api/v1/texts", json={"groups": [{"name": "只有一组", "lines": ["一句。"]}]})
        groups = _groups(client.post("/api/v1/texts/reset"))
        assert "只有一组" not in _names(groups)
        assert {"现代诗", "烂梗"} <= set(_names(groups))
        assert _groups(client.get("/api/v1/texts")) == groups, "恢复之后就该是它自己那一份"

    def test_a_hand_broken_file_falls_back_to_defaults(self, client, data_dir: Path):
        (data_dir / "texts.json").write_text("{ 不是 json", encoding="utf-8")
        assert _groups(client.get("/api/v1/texts")), "坏文件不该让页面没有句子可挑"


@pytest.mark.parametrize("payload", [{"groups": {}}, {"groups": "一行字"}])
def test_shapes_we_cannot_render_are_refused(client, payload):
    resp = client.put("/api/v1/texts", json=payload)
    assert resp.status_code in (400, 422)


def test_all_endpoints_share_one_envelope(client):
    """读 / 保存 / 恢复默认是同一个信封；少剥一层 groups，界面就会拿到对象。"""
    responses = [
        client.get("/api/v1/texts"),
        client.put("/api/v1/texts", json={"groups": [{"name": "组", "lines": ["一句。"]}]}),
        client.post("/api/v1/texts/reset"),
    ]
    for resp in responses:
        body = resp.json()
        assert body["object"] == "texts"
        assert isinstance(body["groups"], list)
