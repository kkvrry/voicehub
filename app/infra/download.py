"""带校验的流式下载：临时文件、size + sha256、进度回调。

这是**通用基础设施**，不认识模型、包或镜像语义。audio.cpp 的运行包与内置
音频工具权重都用它，插件只提供 ``url`` / ``sha256`` / 期望大小。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

__all__ = ["fetch", "fetch_with_mirrors"]

Progress = Callable[[int, int], None]
MirrorPicked = Callable[[str], None]


def fetch(
    url: str,
    target: Path,
    *,
    sha256: str = "",
    size: int = 0,
    timeout: httpx.Timeout | float = 60.0,
    headers: dict[str, str] | None = None,
    on_progress: Progress | None = None,
    client: httpx.Client | None = None,
) -> None:
    """把一个 URL 下到 ``target``；大小或哈希不对就丢弃并抛 ``ProducerError``。

    先写同目录唯一临时文件，校验通过才 ``os.replace``。失败时目标保持原样。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
    digest = hashlib.sha256()
    own = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        with http.stream(
            "GET",
            url,
            headers=headers or {"User-Agent": "VoiceHub"},
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or size or 0)
            written = 0
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(512 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, total)
        if size and part.stat().st_size != size:
            raise ValueError(f"下载大小不对：期望 {size} 字节，实际 {part.stat().st_size}。")
        if sha256 and digest.hexdigest().lower() != sha256.strip().lower():
            raise ValueError("校验和不匹配。")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
        if own:
            http.close()


def fetch_with_mirrors(
    url: str,
    target: Path,
    *,
    mirrors: Sequence[str] = ("",),
    sha256: str = "",
    size: int = 0,
    timeout: httpx.Timeout | float = 60.0,
    headers: dict[str, str] | None = None,
    on_progress: Progress | None = None,
    on_mirror: MirrorPicked | None = None,
    client: httpx.Client | None = None,
) -> str:
    """按 ``mirrors`` 顺序尝试 ``url``；返回成功的镜像前缀。

    ``on_mirror`` 只在成功后调用，调用方可以据此记住最快的镜像。
    全部失败时抛最后一个异常。
    """
    last: Exception | None = None
    for prefix in mirrors:
        try:
            fetch(
                f"{prefix}{url}",
                target,
                sha256=sha256,
                size=size,
                timeout=timeout,
                headers=headers,
                on_progress=on_progress,
                client=client,
            )
            if on_mirror is not None:
                on_mirror(prefix)
            return prefix
        except (httpx.HTTPError, OSError, ValueError) as exc:
            last = exc
    if last is None:
        raise ValueError("没有可用的下载地址。")
    raise last
