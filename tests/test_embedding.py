"""向量模型域：注册、文件清单、下载校验、状态机与 HTTP 形状。**不联网**。

真模型有 24MB，测试把 ``_FILES`` 换成几个字节的假文件，HTTP 走
``httpx.MockTransport``；哈希校验、``.part`` 落盘与后台状态机仍走真代码。
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.api import errors as api_errors
from app.embedding import service as embedding_uc
from app.embedding.base import EmbeddingError, EmbeddingFile
from app.embedding.builtin.bge_small_zh import plugin as bge
from app.embedding.builtin.bge_small_zh.plugin import BgeSmallZh
from app.embedding.cache import VectorCache, cache_for
from app.embedding.registry import detect_provider, list_providers

_TINY = {
    "onnx/model_int8.onnx": b"fake-onnx-weights",
    "tokenizer.json": b'{"fake": true}',
    "tokenizer_config.json": b"{}",
    "special_tokens_map.json": b"{}",
    "config.json": b"{}",
}


def _tiny_files() -> tuple[EmbeddingFile, ...]:
    return tuple(
        EmbeddingFile(
            url=f"https://example.com/{path}",
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        for path, data in _TINY.items()
    )


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """让 ``BgeSmallZh.download`` 里的 ``httpx.Client`` 走假传输。"""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        bge.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


@pytest.fixture(autouse=True)
def _clean_tasks() -> Iterator[None]:
    embedding_uc._TASKS.clear()
    yield
    embedding_uc._TASKS.clear()


def test_builtin_bge_is_discovered() -> None:
    providers = list_providers()
    assert "bge_small_zh" in providers
    provider = detect_provider("bge_small_zh")
    assert isinstance(provider, BgeSmallZh)
    assert provider.dimension == 512
    assert provider.total_size() == 24_343_727
    assert "bge_small_zh" in provider.describe()["path"]


def test_missing_files_are_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())
    provider = BgeSmallZh(root=tmp_path / "model")
    assert provider.installed() is False
    assert len(provider.missing_files()) == len(_TINY)
    provider.remove()
    assert not provider.model_dir().exists()


def test_download_verifies_and_moves_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path not in _TINY:
            return httpx.Response(404)
        return httpx.Response(200, content=_TINY[path])

    _mock_client(monkeypatch, handler)
    provider = BgeSmallZh(root=tmp_path / "model")
    progress: list[tuple[int, int, str]] = []
    provider.download(lambda done, total, current: progress.append((done, total, current)))

    assert provider.installed() is True
    assert (provider.model_dir() / "onnx" / "model_int8.onnx").read_bytes() == (
        _TINY["onnx/model_int8.onnx"]
    )
    total = sum(len(data) for data in _TINY.values())
    assert progress[-1][0] == total
    assert progress[-1][1] == total
    assert not list(provider.model_dir().rglob("*.part"))


def test_download_survives_bracketed_ipv6_no_proxy(tmp_path: Path, monkeypatch) -> None:
    """系统代理里的 ``[::1]`` 不能让模型下载在创建客户端时就崩。"""
    monkeypatch.setattr(bge, "_FILES", _tiny_files())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path not in _TINY:
            return httpx.Response(404)
        return httpx.Response(200, content=_TINY[path])

    _mock_client(monkeypatch, handler)
    value = "localhost,127.0.0.1,[::1],::1"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    provider = BgeSmallZh(root=tmp_path / "model")
    provider.download(lambda *_: None)

    assert provider.installed() is True
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1,::1"


def test_download_rejects_bad_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        data = _TINY[path]
        if path == "onnx/model_int8.onnx":
            data = b"x" * len(data)
        return httpx.Response(200, content=data)

    _mock_client(monkeypatch, handler)
    provider = BgeSmallZh(root=tmp_path / "model")
    with pytest.raises(EmbeddingError, match="校验失败"):
        provider.download(lambda *_: None)
    assert provider.installed() is False
    assert not list(provider.model_dir().rglob("*.part"))


def test_download_reports_http_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())
    _mock_client(monkeypatch, lambda request: httpx.Response(500))
    provider = BgeSmallZh(root=tmp_path / "model")
    with pytest.raises(EmbeddingError, match="下载"):
        provider.download(lambda *_: None)


def test_embed_without_model_is_explained(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())
    provider = BgeSmallZh(root=tmp_path / "model")
    with pytest.raises(EmbeddingError, match="还没下载"):
        provider.embed(["你好"])


def test_vector_cache_roundtrip_and_version_prune(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    cache = VectorCache(path, "1.5")
    cache.put({"a": [1.0, 0.0], "b": [0.0, 2.0], "bad": [1.0]}, 2)

    again = VectorCache(path, "1.5")
    got = again.get(["a", "b", "bad", "missing"], 2)
    assert set(got) == {"a", "b"}
    assert got["a"].tolist() == [1.0, 0.0]
    assert got["b"].tolist() == [0.0, 2.0]
    # 维度对不上（模型换了）时整批忽略，交给推理重算。
    assert again.get(["a"], 3) == {}

    newer = VectorCache(path, "1.6")
    assert newer.get(["a"], 2) == {}
    newer.put({"a": [3.0, 0.0]}, 2)
    assert VectorCache(path, "1.6").get(["a"], 2)["a"].tolist() == [3.0, 0.0]


def test_cache_for_follows_model_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())
    provider = BgeSmallZh(root=tmp_path / "model")
    assert cache_for(provider).path == provider.model_dir() / "vectors.db"


def test_api_list_refuses_embed_and_unknown_key(client, monkeypatch) -> None:
    rows = client.get("/api/v1/embeddings/providers").json()["data"]
    assert rows[0]["key"] == "bge_small_zh"
    assert rows[0]["state"] == "not_downloaded"
    assert rows[0]["dimension"] == 512

    missing = client.post(
        "/api/v1/embeddings/providers/bge_small_zh/embed",
        json={"texts": ["你好"]},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "embedding_missing"

    unknown = client.get("/api/v1/embeddings/providers/nope")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "embedding_not_found"


def test_api_download_then_embed_and_remove(client, monkeypatch) -> None:
    monkeypatch.setattr(bge, "_FILES", _tiny_files())

    def fake_download(self, progress) -> None:
        root = self.model_dir()
        root.mkdir(parents=True, exist_ok=True)
        done = 0
        total = sum(len(data) for data in _TINY.values())
        for path, data in _TINY.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            done += len(data)
            progress(done, total, path)

    monkeypatch.setattr(BgeSmallZh, "download", fake_download)
    monkeypatch.setattr(
        BgeSmallZh,
        "embed",
        lambda self, texts, query=False: [[0.0] * 512 for _ in texts],
    )

    started = client.post("/api/v1/embeddings/providers/bge_small_zh/download")
    assert started.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        row = client.get("/api/v1/embeddings/providers/bge_small_zh").json()
        if row["state"] != "downloading":
            break
        time.sleep(0.02)
    assert row["state"] == "ready"
    assert row["installed"] is True

    embedded = client.post(
        "/api/v1/embeddings/providers/bge_small_zh/embed",
        json={"texts": ["温柔的女声", "沉稳的男声"], "query": True},
    )
    assert embedded.status_code == 200
    body = embedded.json()
    assert body["dimension"] == 512
    assert len(body["vectors"]) == 2

    removed = client.post("/api/v1/embeddings/providers/bge_small_zh/remove")
    assert removed.status_code == 200
    assert removed.json()["state"] == "not_downloaded"
    assert api_errors.CODES["embedding_missing"]
