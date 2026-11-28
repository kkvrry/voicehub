"""整份替换：临时文件 + 改名，读者永远看不到半份。

适用两种情形：

- 一份 JSON / 文本的整份写入（配置、词表、词表偏差、伴生信息）——:func:`write_json`；
- 一段已经完整拿到的字节（合成出来的音频）——:func:`write_bytes`。

临时文件与目标同目录，改名才在同一个卷上原子生效；写完不保证已落盘，
掉电后可能回到上一份，但不会留下半个对象。

特意**不**走这里的：压缩包与模型权重。它们要续写、可续传、还要在失败时保留片段，
由各自的 ``.part`` 流程处理（见 :mod:`app.infra.ffmpeg`）。

并发写同一个目标也是安全的：临时文件名带 pid 与随机后缀，各写各的，改名只有
一个赢家——同名的 ``X.part`` 是两个写者互相截断的老来源。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = ["replace_file", "write_bytes", "write_json"]


def replace_file(scratch: Path, target: Path, *, attempts: int = 6) -> None:
    """用临时文件原子替换目标；Windows 上被短暂占用时退避重试。"""
    for attempt in range(attempts):
        try:
            os.replace(scratch, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_json(path: Path, payload: Any, *, indent: int | None = 2, allow_nan: bool = True) -> None:
    """把 ``payload`` 写成 ``path`` 的一份 JSON；中途失败时 ``path`` 保持原样。

    两个格式参数只在明确需要时覆盖：大表用 ``indent=None`` 免得体积翻倍，
    数值必须可被标准 JSON 解析时传 ``allow_nan=False``（宁可抛错也不写出 ``NaN``）。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=allow_nan)
    write_bytes(path, text.encode("utf-8"))


def write_bytes(path: Path, data: bytes, *, attempts: int = 6) -> None:
    """把一段完整字节写成 ``path``：中途失败时目标保持原样。

    Windows 上改名要求目标没有被别的句柄攥着（播放器正在放、杀软正在扫刚写完的
    文件）。占用通常是毫秒级的，退避几轮比让整次合成失败划算。
    """
    tag = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    tmp = path.with_name(f"{path.name}.{tag}.part")
    try:
        tmp.write_bytes(data)
        replace_file(tmp, path, attempts=attempts)
    finally:
        tmp.unlink(missing_ok=True)
