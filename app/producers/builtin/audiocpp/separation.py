"""音乐分轨模型：从混音里取人声与伴奏。

只列会同时给出人声与伴奏的族。语音分离（``mossformer2`` / ``tf_gridnet``）不列：
它们分的是两个人，不是音乐与配音要用的两轨。可选模型只认运行中实例注册了什么，
与音色处理的模型清单同一条边界；插件不在这里下载或安装模型。
"""

from __future__ import annotations

from typing import Any

from app.producers.builtin.audiocpp import catalog

__all__ = ["MODEL_FAMILIES", "options"]

#: 同时给出人声与伴奏的族，顺序即界面推荐顺序。
MODEL_FAMILIES: tuple[str, ...] = (
    "bs_roformer",
    "mel_band_roformer",
    "htdemucs",
    "htdemucs_6stems",
)

#: 族 → 一句话取舍：界面要说的是"什么时候该选它"，不是抄模型能力。
_NOTES: dict[str, str] = {
    "bs_roformer": "人声最干净，较慢",
    "mel_band_roformer": "人声干净，较快",
    "htdemucs": "通用分轨，快；伴奏由多轨相加",
    "htdemucs_6stems": "六轨分轨，输出多、较慢",
}

#: 分离类输入：44.1 kHz 立体声，伴奏信息在声道差里。
_PREP = (44100, 2)


def _label(family_row: dict[str, Any], package: dict[str, Any], fallback: str) -> str:
    text = str(package.get("display_name") or family_row.get("name") or fallback)
    return text.replace(" GGUF", "").strip()


def options(running: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """运行中的实例里能用于音乐分轨的模型。

    ``running`` 是 audio.cpp 插件的 ``running_models()`` 行：``{id, family, task,
    path, package, url}``。返回 ``[{id, label, family, note, prep, url}]``。
    """
    rows: list[dict[str, Any]] = []
    for item in running:
        family = str(item.get("family") or "")
        model_id = str(item.get("id") or "")
        if family not in MODEL_FAMILIES or not model_id:
            continue
        package_id = str(item.get("package") or model_id)
        found_family, package = catalog.package(package_id, None)
        if not package:
            found_family, package = catalog.package(model_id, None)
        label = _label(
            found_family or catalog.family(family, None),
            package,
            family,
        )
        rows.append(
            {
                "id": model_id,
                "label": label,
                "family": family,
                "note": _NOTES.get(family, ""),
                "prep": list(_PREP),
                "url": str(item.get("url") or ""),
            }
        )
    order = {family: index for index, family in enumerate(MODEL_FAMILIES)}
    rows.sort(key=lambda row: (order.get(row["family"], 99), row["label"]))
    return rows
