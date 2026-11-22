"""音色处理对 audio.cpp 的最小依赖：跑一次任务，取回人声那一路。

请求、响应解析与多路输出都在 :mod:`app.producers.builtin.audiocpp.tasks`；
这里只保留"音色处理要的是人声"这一条选择规则，分离出来的伴奏不参与替换。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.producers.builtin.audiocpp.tasks import pick_vocal
from app.producers.builtin.audiocpp.tasks import run_task as _run_task

__all__ = ["run_task"]


def run_task(
    host: str,
    *,
    model: str,
    audio: Path,
    options: dict[str, Any] | None = None,
    timeout: float = 600.0,
) -> bytes:
    """跑一次模型任务，返回人声那一路的音频字节。

    :param model: 服务端注册的模型 id（``/v1/models`` 里那一个）。
    :param audio: 服务端本机可读的 wav 路径。
    :param options: 模型自己的请求选项（``request.options``）。
    """
    return pick_vocal(
        _run_task(
            host,
            model=model,
            audio=audio,
            options=options,
            timeout=timeout,
        )
    )
