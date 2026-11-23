"""各引擎共用的种子处理。

引擎对"空种子"的约定不一样（有的自己随机、有的用固定默认值），对负数
（``-1`` 表示随机）也一律不收。这一层只做三件事：

- 空值：调用方给 ``default`` 就替代，没给就表示"不发这个键"；
- 非负整数：截到 ``ceiling`` 以内，原样发；
- 负数：抽一个无符号数发出去，并把抽到的数报回界面与日志。

插件只声明自己的 ``engine`` 名与上限，不各自复制这段逻辑。
"""

from __future__ import annotations

import random
from typing import Any

from app.engines.base import EngineError
from app.infra.loghub import CHANNEL

__all__ = ["SEED_CEILING", "resolve_seed"]

#: 引擎普遍按 32 位无符号整数收种子；超过这个值没有意义。
SEED_CEILING = 2**32 - 1


def resolve_seed(
    value: Any,
    *,
    engine: str,
    ceiling: int = SEED_CEILING,
    default: int | None = None,
) -> tuple[int | None, str]:
    """定这一次真正发出去的种子。

    :param value: 请求里的种子；``None`` / 空串表示没填。
    :param engine: 插件 key，只用于日志。
    :param ceiling: 允许的最大值，默认 32 位无符号上限。
    :param default: 没填时改用这个值；``None`` 表示"不发这个键"。
    :return: ``(发出去的值, 给用户的一句话)``；不发时是 ``(None, "")``。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            return None, ""
        value = default
    try:
        seed = int(value)
    except (TypeError, ValueError):
        raise EngineError(f"种子「{value}」不是整数。") from None
    if seed >= 0:
        sent = min(seed, ceiling)
        return sent, f"seed={sent}"
    drawn = random.randrange(0, ceiling + 1)
    CHANNEL.info(
        f"种子写的是 {seed}（随机），抽取结果 {drawn}（可用它复现）",
        source="engine",
        engine=engine,
    )
    return drawn, f"seed={drawn}"
