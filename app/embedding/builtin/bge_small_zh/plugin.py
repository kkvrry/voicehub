"""bge-small-zh-v1.5：512 维中文向量模型，INT8 ONNX 约 23MB。

不随安装包分发：用户在设置里点「下载并启用」后才落到数据目录。
推理走 onnxruntime + tokenizers，CPU 即可；CLS pooling 后 L2 归一化。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from app.embedding.base import EmbeddingError, EmbeddingFile, EmbeddingProvider, Progress
from app.infra.netenv import normalize_no_proxy_env

__all__ = ["BgeSmallZh"]

_BASE = "https://modelscope.cn/models/Xenova/bge-small-zh-v1.5/resolve/master/"
_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
_MAX_TOKENS = 512

#: 文件大小与 SHA256 来自 ModelScope 仓库清单（2026-11 核验）。
_FILES = (
    EmbeddingFile(
        url=_BASE + "onnx/model_int8.onnx",
        path="onnx/model_int8.onnx",
        sha256="b9837c19ce154ff0726d398ee77abbc03a7faf0476c6f93016c84e531be7ebb5",
        size=23903394,
    ),
    EmbeddingFile(
        url=_BASE + "tokenizer.json",
        path="tokenizer.json",
        sha256="48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
        size=439125,
    ),
    EmbeddingFile(
        url=_BASE + "tokenizer_config.json",
        path="tokenizer_config.json",
        sha256="e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
        size=367,
    ),
    EmbeddingFile(
        url=_BASE + "special_tokens_map.json",
        path="special_tokens_map.json",
        sha256="b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
        size=125,
    ),
    EmbeddingFile(
        url=_BASE + "config.json",
        path="config.json",
        sha256="d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f",
        size=716,
    ),
)


class BgeSmallZh(EmbeddingProvider):
    key = "bge_small_zh"
    label = "BGE small 中文"
    version = "1.5"
    homepage = "https://huggingface.co/BAAI/bge-small-zh-v1.5"
    license = "MIT"
    description = "512 维中文向量模型，INT8 ONNX，约 23MB；CPU 可跑，适合音色库语义召回。"
    dimension = 512

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None

    def files(self) -> tuple[EmbeddingFile, ...]:
        return _FILES

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #

    def download(self, progress: Progress) -> None:
        root = self.model_dir()
        root.mkdir(parents=True, exist_ok=True)
        total = sum(item.size for item in self.files())
        done = 0
        normalize_no_proxy_env()
        with httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=True,
            headers={"User-Agent": "VoiceHub/0.1 (+embedding model download)"},
        ) as client:
            for item in self.files():
                dest = root / item.path
                if self._matches(dest, item):
                    done += item.size
                    progress(done, total, item.path)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".part")
                digest = hashlib.sha256()
                written = 0
                try:
                    with client.stream("GET", item.url) as response:
                        response.raise_for_status()
                        with part.open("wb") as handle:
                            for chunk in response.iter_bytes(1024 * 1024):
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                digest.update(chunk)
                                written += len(chunk)
                                progress(done + written, total, item.path)
                except httpx.HTTPError as exc:
                    part.unlink(missing_ok=True)
                    raise EmbeddingError(
                        f"下载 {item.path} 失败：{exc}。请检查网络后重试。"
                    ) from exc
                if written != item.size or digest.hexdigest() != item.sha256:
                    part.unlink(missing_ok=True)
                    raise EmbeddingError(
                        f"{item.path} 校验失败（大小或 SHA256 不符），请重试。"
                    )
                os.replace(part, dest)
                done += item.size
                progress(done, total, item.path)
        self._session = None
        self._tokenizer = None

    @staticmethod
    def _matches(path: Path, item: EmbeddingFile) -> bool:
        if not path.is_file() or path.stat().st_size != item.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == item.sha256

    # ------------------------------------------------------------------ #
    # 推理
    # ------------------------------------------------------------------ #

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                return self._session, self._tokenizer
            if not self.installed():
                raise EmbeddingError("模型还没下载：先在设置里下载并启用。")
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给一句可读说明
                raise EmbeddingError(
                    "缺少 onnxruntime 或 tokenizers，无法运行本地向量模型。"
                ) from exc
            root = self.model_dir()
            tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=_MAX_TOKENS)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.intra_op_num_threads = 0
            session = ort.InferenceSession(
                str(root / "onnx" / "model_int8.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._session = session
            self._tokenizer = tokenizer
            return session, tokenizer

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        values = [str(text or "").strip() for text in texts]
        if not values:
            return []
        session, tokenizer = self._load()
        prefixed = [_INSTRUCTION + text if query else text for text in values]
        encoded = tokenizer.encode_batch(prefixed)
        input_ids = np.asarray([row.ids for row in encoded], dtype=np.int64)
        attention = np.asarray([row.attention_mask for row in encoded], dtype=np.int64)
        feeds: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention,
        }
        names = {item.name for item in session.get_inputs()}
        if "token_type_ids" in names:
            feeds["token_type_ids"] = np.asarray(
                [row.type_ids for row in encoded], dtype=np.int64
            )
        outputs = session.run(None, feeds)
        hidden = outputs[0]
        if hidden.ndim == 3:
            vectors = hidden[:, 0, :]  # BGE：CLS pooling
        elif hidden.ndim == 2:
            vectors = hidden
        else:
            raise EmbeddingError(f"不认识的向量输出形状：{hidden.shape}。")
        norm = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.clip(norm, 1e-12, None)
        return vectors.astype(np.float32).tolist()
