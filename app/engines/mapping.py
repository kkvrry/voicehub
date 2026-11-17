"""统一参数 → 引擎参数：映射是数据，不是代码。

上游只按一种格式说话（:class:`~app.engines.base.SynthesisRequest`），每台引擎要什么键、
塞在哪层、值怎么换算写在它自己的映射表里。不做成"万能适配器"：各家请求体形状差异
太大，这里只统一声明方式，装配发生在引擎自己的目录里。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TRANSFORMS",
    "TRANSFORM_LABELS",
    "EngineMapping",
    "ParamMapping",
    "apply_mapping",
    "register_transform",
    "transform_label",
    "transform_of",
]


# --------------------------------------------------------------------------- #
# 取值换算器
# --------------------------------------------------------------------------- #


def _identity(value: Any) -> Any:
    return value


def _as_number(value: Any) -> float | int | None:
    """数字原样过；不是数字给 ``None``（语义是"发不出去"，不能悄悄换成 0）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except ValueError:
        return None


def _ms_to_sec(value: Any) -> float | None:
    number = _as_number(value)
    return None if number is None else round(float(number) / 1000.0, 3)


def _sec_to_ms(value: Any) -> int | None:
    number = _as_number(value)
    return None if number is None else round(float(number) * 1000)


def _percent_to_ratio(value: Any) -> float | None:
    """0–100 → 0–1（有的引擎音高/音量按百分比收）。"""
    number = _as_number(value)
    return None if number is None else round(float(number) / 100.0, 4)


def _ratio_to_percent(value: Any) -> int | None:
    number = _as_number(value)
    return None if number is None else round(float(number) * 100)


def _round_half_away(number: float) -> int:
    """四舍五入到整数，半数向外（``+2.5→3``、``−2.5→−3``）；
    内置 round 是半数取偶，正负两边会落进不同档。"""
    return math.floor(number + 0.5) if number >= 0 else -math.floor(-number + 0.5)


def _as_int(value: Any) -> int | None:
    """整型落点用：整数还整数，带小数的四舍五入。

    单独存在是因为按类型校验的网关会把 ``24000.0`` 判成小数而拒收
    （那时请求已发出）。取整是有损的，哪些落点是整型写在各引擎映射表里。
    """
    number = _as_number(value)
    return None if number is None else _round_half_away(float(number))


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "是"}:
        return True
    if text in {"0", "false", "no", "off", "否"}:
        return False
    return None


#: 转换器登记册。键即映射表里写的名字；用了没登记的会在装配时报出（不静默）。
TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "": _identity,  # 空 = 原样
    "identity": _identity,
    "number": _as_number,
    "int": _as_int,
    "ms_to_sec": _ms_to_sec,
    "sec_to_ms": _sec_to_ms,
    "percent_to_ratio": _percent_to_ratio,
    "ratio_to_percent": _ratio_to_percent,
    "bool": _as_bool,
    "str": lambda value: str(value).strip(),
}

#: 换算器的中文说法（界面「换算」列）。插件自定义换算器随登记一起给。
TRANSFORM_LABELS: dict[str, str] = {
    "": "原样",
    "identity": "原样",
    "number": "数值",
    "int": "取整",
    "ms_to_sec": "毫秒 → 秒",
    "sec_to_ms": "秒 → 毫秒",
    "percent_to_ratio": "百分比 → 倍率",
    "ratio_to_percent": "倍率 → 百分比",
    "bool": "布尔",
    "str": "文本",
}


def register_transform(name: str, fn: Callable[[Any], Any], *, label: str = "") -> None:
    """登记一个取值换算器（插件可在自己的目录里调用它，VoiceHub 不用改）。

    :param label: 换算方式的中文说法；留空时界面显示登记名。
    """
    key = name.strip()
    TRANSFORMS[key] = fn
    if label:
        TRANSFORM_LABELS[key] = label


def transform_label(name: str) -> str:
    """换算器的界面说法；没登记说法时退回登记名（空 = 原样）。"""
    key = (name or "").strip()
    return TRANSFORM_LABELS.get(key, key or "原样")


def transform_of(name: str) -> Callable[[Any], Any]:
    """按名取转换器；**没登记就抛**——静默用一个恒等函数会让"换算没生效"查不出来。"""
    key = (name or "").strip()
    if key not in TRANSFORMS:
        raise KeyError(
            f"没有登记叫「{key}」的取值换算器。已登记：{sorted(k for k in TRANSFORMS if k)}"
        )
    return TRANSFORMS[key]


# --------------------------------------------------------------------------- #
# 映射表
# --------------------------------------------------------------------------- #

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True, slots=True)
class ParamMapping:
    """一条映射：统一参数名 → 这台引擎的落点。

    :param source: 统一参数名（请求字段名或 ``extra`` 键）。
    :param target: 引擎请求里的字段路径，点号分层（嵌套在此说清）。
    :param transform: :data:`TRANSFORMS` 里的名字（空 = 原样）。
    :param skip_empty: 上游没给就整个不发（默认 True）——省略才是"没要求"的表示法。
    :param drop_unmapped: 给了值但认不出时：False 报错，True 剔掉该格照发
        （仅"少了也能念"的参数可开，且须另给说明）。
    :param required: 必填项，缺了在装配时报。
    :param note: 这一项在本引擎上的含义或约束（面板映射表的「说明」列）。
    :param warning: 重点说明（界面上带警示标记）。
    :param label: 引擎专有参数的中文名；留空时按项目规范或 ``extra_schema`` 取。
    """

    source: str
    target: str
    transform: str = ""
    skip_empty: bool = True
    drop_unmapped: bool = False
    required: bool = False
    note: str = ""
    warning: bool = False
    label: str = ""

    def __post_init__(self) -> None:
        if not _PATH_RE.match(self.target):
            raise ValueError(
                f"映射落点「{self.target}」不像字段路径（只能是 a.b.c 这种点号分层）"
            )


@dataclass(frozen=True, slots=True)
class EngineMapping:
    """一台引擎的整张映射表 + 固定量。

    :param params: 逐项映射（顺序即装配顺序）。
    :param constants: 每次请求原样带的键值（模型名、格式等），不来自上游。
    :param cautions: 跨参数的重点说明，逐条（界面按点换行并重点标记）。
    """

    params: tuple[ParamMapping, ...] = ()
    constants: dict[str, Any] = field(default_factory=dict)
    cautions: tuple[str, ...] = ()

    def targets(self) -> tuple[str, ...]:
        return tuple(p.target for p in self.params)

    def required_sources(self) -> tuple[str, ...]:
        return tuple(p.source for p in self.params if p.required)

    def describe(self) -> list[dict[str, Any]]:
        """给界面/文档看的平表（键名、落点、换算、说明、必填）。"""
        return [
            {
                "source": p.source,
                "target": p.target,
                "transform": p.transform,
                "transform_label": transform_label(p.transform),
                "required": p.required,
                "skip_empty": p.skip_empty,
                "drop_unmapped": p.drop_unmapped,
                "note": p.note,
                "warning": p.warning,
                "label": p.label,
            }
            for p in self.params
        ]


def _put(body: dict[str, Any], path: str, value: Any) -> None:
    """把值塞进 ``body`` 的点号路径，中间层不存在就建。"""
    parts = path.split(".")
    node = body
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def apply_mapping(
    mapping: EngineMapping,
    values: Mapping[str, Any],
    dropped: list[str] | None = None,
) -> dict[str, Any]:
    """统一参数 → 这台引擎的请求体。

    :param values: 统一参数；``None`` 与"键不存在"都当"没要求"。
    :param dropped: 收集被剔掉的那几格（认不出 + 开了 ``drop_unmapped``），
        就地追加；不传则内部丢弃。
    :raises ValueError: 必填缺、用了未登记换算器、给了认不出的值——都在发请求前报出。
        认不出的值不能吞：静默换成默认会让上游要的倍速无声丢失。
    """
    body: dict[str, Any] = {}
    for path, value in mapping.constants.items():
        _put(body, path, value)

    missing: list[str] = []
    unusable: list[str] = []
    sink = dropped if dropped is not None else []
    for item in mapping.params:
        raw = values.get(item.source)
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if blank and item.skip_empty:
            # 先判空再换算：恒等换算器会把空串递进请求体，而省略才是"没要求"。
            if item.required:
                missing.append(item.source)
            continue
        try:
            converter = transform_of(item.transform)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        value = converter(raw)

        if value is None and not blank:
            # 给了值但认不出来（"很快"不是数字、"激动"不在情绪表里等）。
            if item.drop_unmapped:
                sink.append(item.source)
                continue
            # 报出来：硬发的后果是一次失败（且可能计费）的请求。
            unusable.append(f"{item.source}={raw!r}（{item.transform or '原样'} 认不出）")
            continue
        _put(body, item.target, value)

    if missing:
        raise ValueError(
            "这次请求缺少这台引擎必填的参数：" + "、".join(sorted(set(missing)))
        )
    if unusable:
        raise ValueError(
            "这次请求里有这台引擎用不了的值：" + "；".join(unusable) + "。已经拦下，没有发出去。"
        )
    return body
