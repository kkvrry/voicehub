"""说话人分离模型：转录后给段落标说话人。

与音乐分轨（``audiocpp.separation``）同一条边界：可选模型只认运行中实例
注册了什么（``task == "diar"``），装了没启动、实例没加载都不列；插件不在
这里下载或安装模型。加一族或换一族只改本文件。

输入统一转成 16 kHz 单声道（diar 模型只吃 16 kHz）后再调
:func:`audiocpp.tasks.run_diar`；返回的说话轮次按重叠时长折到转录切段上。
"""

from __future__ import annotations

from typing import Any

__all__ = ["MODEL_FAMILIES", "options"]

#: 支持的说话人分离族，顺序即界面推荐顺序。
MODEL_FAMILIES: tuple[str, ...] = (
    "nemotron_3_diar",
    "sortformer_diar",
    "sortformer_diar_v2",
)

#: 族 → 一句话取舍：界面要说的是"什么时候该选它"，不是抄模型能力。
_NOTES: dict[str, str] = {
    "nemotron_3_diar": "最多 8 人，离线与流式，情绪表演稳",
    "sortformer_diar": "最多 4 人，英语为主",
    "sortformer_diar_v2": "最多 4 人，含中文会议数据；仅限本地使用",
}


def _label(family_row: dict[str, Any], fallback: str) -> str:
    return str(family_row.get("name") or fallback)


def options(running: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """运行中的实例里能用于说话人分离的模型。

    ``running`` 是 audio.cpp 插件的 ``running_models()`` 行：``{id, family,
    task, path, url}``。返回 ``[{id, label, family, note, url}]``。
    """
    rows: list[dict[str, Any]] = []
    for item in running:
        family = str(item.get("family") or "")
        model_id = str(item.get("id") or "")
        if family not in MODEL_FAMILIES or not model_id:
            continue
        rows.append(
            {
                "id": model_id,
                "label": _label({"name": model_id}, model_id),
                "family": family,
                "note": _NOTES.get(family, ""),
                "url": str(item.get("url") or ""),
            }
        )
    order = {family: index for index, family in enumerate(MODEL_FAMILIES)}
    rows.sort(key=lambda row: (order.get(row["family"], 99), row["label"]))
    return rows
