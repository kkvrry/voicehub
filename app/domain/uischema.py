"""配置字段的界面提示：写进 pydantic 的 ``json_schema_extra``。

宿主与所有插件共用这一份约定，前端只认 ``label`` / ``widget`` / ``secret`` /
``choices`` / ``group`` / ``enabled_when`` 等几个键——加一个插件不需要改前端。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ui_hints"]


def ui_hints(
    label: str,
    *,
    widget: str = "",
    secret: bool = False,
    choices: tuple[tuple[str, str], ...] = (),
    depends_on: str = "",
    group: str = "",
    placeholder: str = "",
    enabled_when: tuple[str, str | tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """构造一条字段提示。

    :param label: 界面标签（专有术语照原样写）。
    :param widget: ``text`` / ``password`` / ``select`` / ``number`` / ``textarea``
        / ``switch`` / ``llm_provider`` / ``llm_model``。
    :param secret: 读时掩码、提交空串表示不修改。
    :param choices: ``select`` 的候选，``(值, 说法)``。
    :param depends_on: 动态候选依赖哪个字段（如模型依赖通道）。
    :param group: 设置页的卡片分组；同一组画进一张卡片，空 = 归入「其他」。
    :param placeholder: 输入框的占位文案（留空即用默认值这类说明）。
    :param enabled_when: ``(字段名, 取值)``；取值给元组表示任一命中即可。
        前端据此置灰并说明，不把无效的格子画成"改了有用"。
    """
    hints: dict[str, Any] = {"label": label}
    if widget:
        hints["widget"] = widget
    if secret:
        hints["secret"] = True
    if choices:
        hints["choices"] = [list(pair) for pair in choices]
    if depends_on:
        hints["depends_on"] = depends_on
    if group:
        hints["group"] = group
    if placeholder:
        hints["placeholder"] = placeholder
    if enabled_when:
        field, equals = enabled_when
        hints["enabled_when"] = {
            "field": field,
            "equals": list(equals) if isinstance(equals, tuple) else equals,
        }
    return hints
