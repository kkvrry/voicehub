"""模型回复 → 结构化数据。

模型不是 JSON 生成器：它会把对象包进代码围栏、在前后加一句「好的，结果如下」；
被截断时还会留下一段解析不了的开头。这里只做一件事——把能认出来的那个对象抠出来，
认不出就返回空字典，由调用方决定这算失败、重试还是降级。

与 :mod:`app.llm.base` 的区别：那是插件契约，这里是宿主与插件都要用的共用件，
因此不 import 任何具体插件，也不带调用方的业务判断。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["parse_json"]

#: 代码围栏：开头那行 ``` 或 ```json，以及结尾那行 ```。
_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def parse_json(text: str) -> dict[str, Any]:
    """回复里的 JSON 对象；取不到返回 ``{}``。

    先按整段解析（正常情况）；不成再截「第一个 ``{`` 到最后一个 ``}``」——
    模型在对象前后加的说明文字大多落在这两处之外。仍不是对象就返回空字典。
    """
    raw = _FENCE.sub("", (text or "").strip()).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}
