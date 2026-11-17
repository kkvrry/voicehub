"""封面缩略图：列表页几百张卡片的头像不该按原图解码。

原图是 0.5–1MB 的 PNG，缩略图是 160px 的 WebP（约 8KB）。这一份守三件事：
尺寸对、缓存会命中、源图换了会重生成。坏图回退由调用方（路由）处理，这里只保证
抛出的是 :class:`ThumbError` 而不是各种 PIL 异常。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from app.infra.thumbs import SIDE, ThumbError, thumbnail


def _png(path: Path, size: tuple[int, int] = (640, 480), color: str = "red") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def test_it_scales_down_and_keeps_a_square(tmp_path: Path) -> None:
    source = _png(tmp_path / "avatar.png")
    cache = tmp_path / "covers"
    out = thumbnail(source, cache)

    assert out.is_file() and out.suffix == ".webp"
    with Image.open(out) as image:
        assert image.size == (SIDE, SIDE)
    # 原图近 1MB 的那种，缩完必须小一个数量级——否则列表还是卡在解码上。
    assert out.stat().st_size < source.stat().st_size


def test_a_second_call_reuses_the_cache(tmp_path: Path) -> None:
    source = _png(tmp_path / "avatar.png")
    cache = tmp_path / "covers"
    first = thumbnail(source, cache)
    stamp = first.stat().st_mtime_ns
    second = thumbnail(source, cache)
    assert second == first
    assert second.stat().st_mtime_ns == stamp


def test_a_replaced_source_regenerates(tmp_path: Path) -> None:
    source = _png(tmp_path / "avatar.png", color="red")
    cache = tmp_path / "covers"
    out = thumbnail(source, cache)
    old_stamp = out.stat().st_mtime_ns

    _png(source, color="blue")
    # 同一个秒内换图也要认出来：指纹用的是 mtime_ns，不是秒。
    os.utime(source, ns=(old_stamp + 10**9, old_stamp + 10**9))
    again = thumbnail(source, cache)
    assert again.stat().st_mtime_ns != old_stamp


def test_a_broken_image_raises_thumb_error(tmp_path: Path) -> None:
    source = tmp_path / "avatar.png"
    source.write_bytes(b"not an image")
    with pytest.raises(ThumbError):
        thumbnail(source, tmp_path / "covers")


def test_the_cover_endpoint_serves_the_thumbnail(client, library, tmp_path: Path) -> None:
    """列表拿到的 ``cover_url`` 指向缩略图；原图只留给"换头像 / 下载"。"""
    from conftest import make_wav

    folder = tmp_path / "library" / "原神" / "胡桃"
    make_wav(folder / "默认.wav", seconds=0.3)
    _png(folder / "avatar.png")
    library.save_folders([str(tmp_path / "library")])

    voice = next(item for item in library.list_voices() if item.name == "胡桃")
    assert voice.cover
    resp = client.get(f"/api/v1/cover/{voice.id}?s=thumb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert resp.content[:4] == b"RIFF"  # WebP 容器头

    # 不带 s 的地址仍是原图（PNG）：这是"要看大图"时的入口。
    full = client.get(f"/api/v1/cover/{voice.id}")
    assert full.status_code == 200
    assert full.headers["content-type"] == "image/png"


