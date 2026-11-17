"""向量模型插件协议。

插件只负责三件事：模型文件清单、下载/删除、把文本变成归一化向量。
下载进度、后台线程、HTTP 状态放在 usecase，插件保持可测试、可替换。
"""

from __future__ import annotations

import hashlib
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.infra.config import data_dir

__all__ = [
    "EmbeddingError",
    "EmbeddingFile",
    "EmbeddingProvider",
]


class EmbeddingError(Exception):
    """向量模型不可用/下载失败/推理失败。``reason`` 是可直接展示的中文。"""

    def __init__(self, reason: str, *, code: str = "embedding_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class EmbeddingFile:
    """模型目录里的一个文件；``path`` 相对模型根目录。"""

    url: str
    path: str
    sha256: str
    size: int


Progress = Callable[[int, int, str], None]


class EmbeddingProvider(ABC):
    """一家本地向量模型。"""

    key: str = ""
    label: str = ""
    version: str = "1.0"
    homepage: str = ""
    license: str = ""
    description: str = ""
    dimension: int = 0
    #: 模型总字节数（界面显示用）；0 = 由清单计算。
    size: int = 0

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    def model_dir(self) -> Path:
        """模型落点：数据目录 / models / embeddings / <key>。"""
        if self._root is not None:
            return Path(self._root)
        return data_dir() / "models" / "embeddings" / self.key

    @abstractmethod
    def files(self) -> tuple[EmbeddingFile, ...]:
        """要下载的文件清单（含相对路径、大小与 SHA256）。"""

    def total_size(self) -> int:
        return int(self.size or sum(item.size for item in self.files()))

    def installed(self) -> bool:
        """文件齐不齐。下载完成时已校验哈希；这里只做存在与大小检查。"""
        root = self.model_dir()
        for item in self.files():
            path = root / item.path
            try:
                if not path.is_file() or path.stat().st_size != item.size:
                    return False
            except OSError:
                return False
        return True

    def missing_files(self) -> list[str]:
        root = self.model_dir()
        missing: list[str] = []
        for item in self.files():
            path = root / item.path
            try:
                if not path.is_file() or path.stat().st_size != item.size:
                    missing.append(item.path)
            except OSError:
                missing.append(item.path)
        return missing

    def remove(self) -> None:
        root = self.model_dir()
        if root.exists():
            shutil.rmtree(root)

    def download(self, progress: Progress) -> None:
        """下载全部文件。先写 ``.part``，哈希通过后再改名，失败不留半截。"""
        raise EmbeddingError(f"{self.label} 没有实现下载。")

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """把文本变成归一化向量。``query=True`` 时加检索指令前缀。"""
        raise EmbeddingError(f"{self.label} 没有实现推理。")

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "version": self.version,
            "homepage": self.homepage,
            "license": self.license,
            "description": self.description,
            "dimension": self.dimension,
            "size": self.total_size(),
            "path": str(self.model_dir()),
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
