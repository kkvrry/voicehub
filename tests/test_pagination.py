"""分页契约：``limit`` / ``offset`` / ``has_more`` 的真假都要对得上。

默认 ``limit=0`` 是"一次给全"——老界面靠一次拿全量在本地筛选，改默认值会让它
静默少数据；分页只在调用方显式传 ``limit`` 时生效。
"""

from pathlib import Path


def _items(resp):
    return resp.json()["data"]


def _envelope(resp):
    return resp.json()


def test_a_page_reports_whether_more_follows(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})

    first = _envelope(client.get("/api/v1/voices", params={"source": "local", "limit": 2}))
    assert len(first["data"]) == 2
    assert first["has_more"] is True

    second = _envelope(
        client.get("/api/v1/voices", params={"source": "local", "limit": 2, "offset": 2})
    )
    assert len(second["data"]) == 2
    assert second["has_more"] is False  # 4 条，第 2 页正好取完

    ids = [voice["id"] for voice in first["data"] + second["data"]]
    assert len(set(ids)) == 4  # 翻页不重不漏


def test_an_exact_page_boundary_does_not_claim_more(client, demo_library: Path):
    """第 2 页正好取完时 ``has_more`` 必须是假——多取一条来判断，不能靠数总数。"""
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    page = _envelope(client.get("/api/v1/voices", params={"source": "local", "limit": 4}))
    assert len(page["data"]) == 4
    assert page["has_more"] is False


def test_without_a_limit_everything_comes_back(client, demo_library: Path):
    """不传 ``limit``：老界面拿到的还是全量（且不会误说"还有更多"）。"""
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    everything = _envelope(client.get("/api/v1/voices", params={"source": "local"}))
    assert len(everything["data"]) == 4
    assert everything["has_more"] is False

    zero = _envelope(client.get("/api/v1/voices", params={"source": "local", "limit": 0}))
    assert [voice["id"] for voice in zero["data"]] == [
        voice["id"] for voice in everything["data"]
    ]


def test_an_offset_past_the_end_is_an_empty_page(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    page = _envelope(
        client.get("/api/v1/voices", params={"source": "local", "limit": 10, "offset": 99})
    )
    assert page["data"] == []
    assert page["has_more"] is False


def test_negative_paging_values_fold_back_to_defaults(client, demo_library: Path):
    """``limit=-1`` 不能变成 SQL 里的 ``LIMIT -1``（那是"不限"），这里折成 0 = 全量。"""
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    page = _envelope(
        client.get("/api/v1/voices", params={"source": "local", "limit": -1, "offset": -5})
    )
    assert len(page["data"]) == 4
    assert page["has_more"] is False


def test_paging_keeps_the_filters(client, demo_library: Path):
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    page = _envelope(
        client.get("/api/v1/voices", params={"category": "原神", "limit": 1})
    )
    assert len(page["data"]) == 1
    assert page["has_more"] is True
    assert page["data"][0]["category"] == "原神"


def test_root_filter_agrees_with_the_tree_total(client, demo_library: Path):
    """``root`` 筛的是"这个根下面的"，与左栏 ``total`` 同一套前缀规则。"""
    client.put("/api/v1/folders", json={"folders": [str(demo_library)]})
    tree = client.get("/api/v1/tree").json()
    root = tree["roots"][0]
    found = _items(client.get("/api/v1/voices", params={"root": root["path"]}))
    assert len(found) == root["total"] == 4


def test_root_filter_does_not_swallow_a_sibling_prefix(client, tmp_path: Path):
    """根 ``lib`` 不能把 ``library`` 里的音色也数进来（前缀要带分隔符）。"""
    from conftest import make_wav

    make_wav(tmp_path / "lib" / "甲" / "默认.wav")
    make_wav(tmp_path / "library" / "乙" / "默认.wav")
    client.put(
        "/api/v1/folders",
        json={"folders": [str(tmp_path / "lib"), str(tmp_path / "library")]},
    )

    found = _items(client.get("/api/v1/voices", params={"root": str(tmp_path / "lib")}))
    assert [voice["name"] for voice in found] == ["甲"]
