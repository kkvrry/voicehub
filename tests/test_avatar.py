"""头像编排：来源只给 URL，宿主下载、裁成方形、落盘并重扫。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.infra import avatar_sources
from app.infra.avatar_sources import Candidate, SourceError
from app.usecase import avatar


def _png(width: int = 1024, height: int = 1024) -> bytes:
    image = np.full((height, width, 3), (90, 130, 210), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


@pytest.fixture()
def library(service, demo_library: Path, monkeypatch: pytest.MonkeyPatch):
    service.save_folders([str(demo_library)])

    async def fake_download(client, candidate):
        return _png(), ".png"

    monkeypatch.setattr(avatar, "_download", fake_download)
    return service, demo_library


def _search_returning(*keys: str, source: str = "萌娘百科"):
    async def fake_search(key, client, name, category, folder=""):
        if key not in keys:
            return []
        return [
            Candidate(
                url="https://example.test/portrait.png",
                source=source,
                page="https://example.test/character",
            )
        ]

    return fake_search


def test_status_lists_sources_without_network():
    result = avatar.status()

    assert result["ready"] is True
    labels = [row["label"] for row in result["sources"]]
    assert labels == ["作品 Wiki", "萌娘百科", "AniList", "Bing 图片"]


def test_client_creation_survives_bracketed_ipv6_no_proxy(monkeypatch: pytest.MonkeyPatch):
    """系统代理里的 ``[::1]`` 不能让找头像在创建客户端时就崩。"""
    value = "localhost,127.0.0.1,127.0.0.0/8,[::1],::1"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    async def run() -> bool:
        client = avatar._client()
        try:
            return client.is_closed
        finally:
            await client.aclose()

    assert asyncio.run(run()) is False
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,127.0.0.0/8,::1,::1"


def test_generate_writes_square_avatar_and_rescans(library, monkeypatch):
    service, root = library
    voice = next(v for v in service.list_voices() if v.name == "胡桃")
    monkeypatch.setattr(avatar_sources, "search", _search_returning("moegirl"))

    result = asyncio.run(avatar.generate(service, voice.id))

    target = root / "原神" / "胡桃" / "avatar.png"
    assert target.is_file()
    image = cv2.imdecode(np.frombuffer(target.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (avatar._AVATAR_SIZE, avatar._AVATAR_SIZE)
    # 下载到的整张原图也留在音色目录里，不是裁完就丢。
    original = root / "原神" / "胡桃" / "avatar-source.png"
    assert original.read_bytes() == _png()
    assert result["source"] == "萌娘百科"
    assert result["voice"].cover == "avatar.png"


def test_blocked_source_falls_back_to_next(library, monkeypatch):
    service, _ = library
    voice = next(v for v in service.list_voices() if v.name == "阿米娅")

    async def fake_search(key, client, name, category, folder=""):
        if key == "prts":
            raise SourceError("prts.wiki 拒绝访问")
        if key == "moegirl":
            return [
                Candidate(
                    url="https://example.test/amiya.png",
                    source="萌娘百科",
                    page="https://zh.moegirl.org.cn/阿米娅",
                )
            ]
        return []

    monkeypatch.setattr(avatar_sources, "search", fake_search)

    result = asyncio.run(avatar.generate(service, voice.id))

    assert result["source"] == "萌娘百科"


def test_missing_image_is_a_clear_error(library, monkeypatch):
    service, _ = library
    voice = next(v for v in service.list_voices() if v.name == "胡桃")
    monkeypatch.setattr(avatar_sources, "search", _search_returning())

    with pytest.raises(avatar.AvatarError) as caught:
        asyncio.run(avatar.generate(service, voice.id))

    assert caught.value.code == "avatar_not_found"
    assert "胡桃" in caught.value.reason


def test_source_extension_comes_from_image_bytes():
    assert avatar._ext_for(b"\x89PNG\r\n\x1a\nrest") == ".png"
    assert avatar._ext_for(b"\xff\xd8\xff\xe0rest", "image/png") == ".jpg"


def test_tiny_image_is_rejected():
    assert avatar._render(_png(80, 80)) is None


def test_small_square_icon_is_kept_whole():
    result = avatar._render(_png(100, 100))
    assert result is not None
    image = cv2.imdecode(np.frombuffer(result.png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (avatar._AVATAR_SIZE, avatar._AVATAR_SIZE)
    assert result.has_face is False
    assert result.ready is True


def test_landscape_without_face_keeps_full_height():
    """横图兜底不能只取 55% 高度——那样会裁成一条窄缝，只剩眼睛。"""
    image = np.full((250, 600, 3), (90, 130, 210), dtype=np.uint8)
    image[:8, :] = (0, 0, 255)
    ok, buffer = cv2.imencode(".png", image)
    assert ok

    result = avatar._render(buffer.tobytes())

    assert result is not None
    rendered = cv2.imdecode(np.frombuffer(result.png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert tuple(rendered[0, 0])[2] > 200


def test_quality_prefers_resolution_and_faces():
    assert avatar._quality(1200, 1200, 1200, False) > avatar._quality(300, 300, 300, False)
    assert avatar._quality(800, 800, 800, True) > avatar._quality(800, 800, 800, False)


def test_transparent_icon_gets_white_background():
    image = np.zeros((100, 100, 4), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", image)
    assert ok

    result = avatar._render(buffer.tobytes())

    assert result is not None
    rendered = cv2.imdecode(np.frombuffer(result.png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert tuple(rendered[0, 0]) == (255, 255, 255)


def test_find_match_picks_highest_quality_candidate(monkeypatch):
    """同一来源里的多张候选不是「第一张能解码就用」，取画质分最高的那张。"""
    small = _png(300, 300)
    large = _png(1400, 1000)
    by_url = {
        "https://example.test/small.png": small,
        "https://example.test/large.png": large,
    }

    async def fake_search(key, client, name, category, folder=""):
        return [
            Candidate(url=url, source="萌娘百科", page="https://example.test/page")
            for url in by_url
        ]

    async def fake_download(client, candidate):
        return by_url[candidate.url], ".png"

    monkeypatch.setattr(avatar_sources, "search", fake_search)
    monkeypatch.setattr(avatar, "_download", fake_download)
    voice = SimpleNamespace(id="v1", shown_name="胡桃", name="胡桃", category="原神")

    match = asyncio.run(avatar._find_match(None, voice))

    assert match.original == large


def test_moegirl_rejects_redirect_to_another_character(monkeypatch):
    """李云龙 在萌百重定向到「李Saber」，条目配图不是这个人，不能拿来当头像。"""
    wrong = {
        "title": "李Saber",
        "original": {"source": "https://example.test/saber.jpg", "width": 411, "height": 559},
    }

    async def fake_page(client, titles):
        return wrong

    async def fake_json(client, url, params):
        return {"query": {"search": []}}

    monkeypatch.setattr(avatar_sources, "_moegirl_page", fake_page)
    monkeypatch.setattr(avatar_sources, "_get_json", fake_json)

    assert asyncio.run(avatar_sources._moegirl(None, "李云龙", "网络")) == []


def test_moegirl_accepts_page_title_that_still_names_the_character(monkeypatch):
    """「梁非凡」重定向到「刘醒和梁非凡」仍然认得出名字，可以用。"""
    page = {
        "title": "刘醒和梁非凡",
        "original": {"source": "https://example.test/leung.jpg", "width": 496, "height": 730},
    }

    async def fake_page(client, titles):
        return page

    monkeypatch.setattr(avatar_sources, "_moegirl_page", fake_page)

    found = asyncio.run(avatar_sources._moegirl(None, "梁非凡", "网络"))

    assert [item.source for item in found] == ["萌娘百科"]


def test_candidates_then_apply_saves_the_chosen_one(library, monkeypatch):
    """单条音色先拿候选（不落盘），用户选了才写盘。"""
    service, root = library
    voice = next(v for v in service.list_voices() if v.name == "胡桃")
    monkeypatch.setattr(avatar_sources, "search", _search_returning("moegirl"))

    result = asyncio.run(avatar.candidates(service, voice.id))

    items = result["items"]
    assert result["voice"].id == voice.id
    assert len(items) == 1
    assert items[0]["token"]
    assert items[0]["preview"].startswith("data:image/jpeg;base64,")
    assert items[0]["source"] == "萌娘百科"
    folder = root / "原神" / "胡桃"
    assert not (folder / "avatar.png").exists()

    applied = asyncio.run(avatar.apply_candidate(service, voice.id, items[0]["token"]))

    assert (folder / "avatar.png").is_file()
    assert (folder / "avatar-source.png").is_file()
    assert applied["source"] == "萌娘百科"


def test_apply_rejects_unknown_token(library):
    service, _ = library
    voice = next(v for v in service.list_voices() if v.name == "胡桃")

    with pytest.raises(avatar.AvatarError) as caught:
        asyncio.run(avatar.apply_candidate(service, voice.id, "not-a-token"))

    assert caught.value.code == "avatar_candidate_expired"


def test_generate_skips_voices_that_already_have_a_picture(library, monkeypatch):
    """批量只补空缺：目录里已有图（这里是一张手工 cover.jpg）就不上网、不覆盖。"""
    service, root = library
    folder = root / "原神" / "胡桃"
    (folder / "cover.jpg").write_bytes(b"manual-cover")
    voice = next(v for v in service.list_voices() if v.name == "胡桃")

    async def refuse(key, client, name, category, folder=""):
        raise AssertionError("已有图的音色不该再去搜")

    monkeypatch.setattr(avatar_sources, "search", refuse)

    result = asyncio.run(avatar.generate(service, voice.id))

    assert result["skipped"] == "cover.jpg"
    assert result["source"] == ""
    assert not (folder / "avatar.png").exists()
    assert (folder / "cover.jpg").read_bytes() == b"manual-cover"


def test_replacing_the_avatar_keeps_the_previous_copy(library, monkeypatch):
    """换头像走候选（用户挑的这张）：旧图留成 avatar-prev.*，认错人时能退回去。"""
    service, root = library
    folder = root / "原神" / "胡桃"
    (folder / "avatar.png").write_bytes(b"old-avatar")
    voice = next(v for v in service.list_voices() if v.name == "胡桃")
    monkeypatch.setattr(avatar_sources, "search", _search_returning("moegirl"))

    items = asyncio.run(avatar.candidates(service, voice.id))["items"]
    asyncio.run(avatar.apply_candidate(service, voice.id, items[0]["token"]))

    assert (folder / "avatar-prev.png").read_bytes() == b"old-avatar"
    assert (folder / "avatar.png").read_bytes() != b"old-avatar"


def test_replacing_avatar_removes_old_extension(library, monkeypatch):
    service, root = library
    folder = root / "原神" / "胡桃"
    (folder / "avatar.jpg").write_bytes(b"old")
    (folder / "avatar-source.jpg").write_bytes(b"old")
    voice = next(v for v in service.list_voices() if v.name == "胡桃")
    monkeypatch.setattr(avatar_sources, "search", _search_returning("moegirl"))

    items = asyncio.run(avatar.candidates(service, voice.id))["items"]
    asyncio.run(avatar.apply_candidate(service, voice.id, items[0]["token"]))

    assert (folder / "avatar.png").is_file()
    assert (folder / "avatar-source.png").is_file()
    assert not (folder / "avatar.jpg").exists()
    assert not (folder / "avatar-source.jpg").exists()
