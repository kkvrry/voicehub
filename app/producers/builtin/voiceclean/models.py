"""步骤声明与可用的模型。

可选模型只认**运行中的 audio.cpp 实例注册了什么**（``/v1/models``）：没安装、
没加载、没启动的模型不列，页面也不提供下载。catalog 只负责名字、说明与输入格式。

输入格式按包 id 给：分离类要 44.1 kHz 立体声（伴奏信息在声道差里），语音增强类是
16 kHz 或 48 kHz 单声道。喂错格式不会报错，只会得到更差的结果——这正是要写死的地方。
服务端注册的是族名而不是包 id 时，按该步骤推荐的包取格式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["CORE_STEP", "STEPS", "Step", "family_note", "options_for"]

#: 每个步骤允许哪些模型族。白名单，不是发现：清单里还有几十个 `s2s` 家族，其中多数
#: 是音乐修复或超分，对人声素材只会补出并不存在的高频。
_ALLOWED: dict[str, tuple[str, ...]] = {
    "sep": ("bs_roformer", "mel_band_roformer", "htdemucs", "htdemucs_6stems", "mossformer2", "tf_gridnet"),
    # 超分（AudioSR / UniverSR）不列进来：它补出的高频是模型生成的，做训练素材会被当成音色。
    "s2s": ("mossformer2", "sidon", "reuse", "realtime_reuse"),
}

#: 界面排序：同一组里先列推荐的那一个。不在表里的按清单原序排在后面。
_PREFERENCE: dict[str, tuple[str, ...]] = {
    "sep": ("bs_roformer", "mel_band_roformer", "htdemucs", "htdemucs_6stems", "mossformer2", "tf_gridnet"),
    "s2s": ("mossformer2_se", "sidon", "reuse", "realtime_reuse", "mossformer2_sr"),
}

#: 族 → 一句话取舍。写在这里而不是抄上游描述：上游说的是模型能力，界面要说的是
#: "什么时候该选它"。
_NOTES: dict[str, str] = {
    "bs_roformer": "人声质量最好，较慢",
    "mel_band_roformer": "人声质量好，较快",
    "htdemucs": "通用分轨，快",
    "htdemucs_6stems": "六轨分轨，慢",
    "mossformer2": "两人重叠说话分离",
    "tf_gridnet": "重叠语音分离，16 kHz",
    "sidon": "单人语音修复，48 kHz",
    "reuse": "吞吐优先，采样率无关",
    "realtime_reuse": "流式增强，质量略低",
    "universr": "只补高频，不改噪声",
    "audiosr": "只补高频，生成痕迹明显",
}

#: 包 id 覆盖的一句话取舍：同一个族里不同包是不同模型，族级说明会在这里说错。
_PACKAGE_NOTES: dict[str, str] = {
    "mossformer2_ss_16k": "两人重叠说话分离",
    "mossformer2_se_48k": "语音增强，48 kHz 输出",
    "mossformer2_sr_48k": "只补高频，不改噪声",
}

#: 包 id 覆盖的输入格式 ``(采样率, 声道)``；没写按族默认（见 :func:`_prep`）。
_PREP_BY_PACKAGE: dict[str, tuple[int, int]] = {
    "mossformer2_ss_16k_f32": (16000, 1),
    "mossformer2_ss_16k_f16": (16000, 1),
    "tf_gridnet_wsj0_2mix_f32": (16000, 1),
}

_PREP_BY_FAMILY: dict[str, tuple[int, int]] = {
    "mossformer2": (48000, 1),
    "sidon": (48000, 1),
    "reuse": (48000, 1),
    "realtime_reuse": (48000, 1),
    "universr": (48000, 1),
    "audiosr": (48000, 1),
}

#: 同一个族里按步骤挑包：MossFormer2 的 ss / se / sr 是三个不同模型。
_PACKAGE_HINTS: dict[str, tuple[str, ...]] = {
    "sep": ("ss_", "_ss"),
    "s2s": ("se_", "_se", "sr_", "_sr"),
}


@dataclass(frozen=True, slots=True)
class Step:
    """一个可勾选的步骤。"""

    key: str
    label: str
    note: str
    task: str
    order: int
    default: bool = False


STEPS: tuple[Step, ...] = (
    Step("separate", "人声分离", "从含伴奏或多人说话的素材中取出人声。", "sep", 1),
    Step("denoise", "语音降噪", "去除底噪与混响，保时长。", "s2s", 2, default=True),
)

#: 收尾步骤：不是模型，默认不选。名字直接说做了什么，避免"规整"这种没有
#: 可验证含义的说法；执行内容全部写在 note 里。
CORE_STEP: dict[str, Any] = {
    "key": "normalize",
    "label": "统一声道与峰值",
    "order": 3,
    "core": True,
    "default": False,
    "note": "单声道 · 采样率对齐 · 高通 60 Hz · 峰值 ≤ −1 dBFS · 时长校验",
}

#: 反过来的索引：步骤 key → 声明，供 run 时反查 task 与顺序。
def step_of(key: str) -> Step | None:
    return next((step for step in STEPS if step.key == key), None)


def family_note(family: str) -> str:
    return _NOTES.get(family, "")


def package_note(package_id: str, family: str) -> str:
    for hint, note in _PACKAGE_NOTES.items():
        if hint in package_id:
            return note
    return family_note(family)


def _rank(task: str, family: str, package_id: str) -> tuple[int, str]:
    """推荐在前。匹配用**最长前缀**：``htdemucs`` 与 ``htdemucs_6stems`` 是两个包。"""
    order = _PREFERENCE.get(task, ())
    best: int | None = None
    for index, hint in enumerate(order):
        if not package_id.startswith(hint):
            continue
        if best is None or len(hint) > len(order[best]):
            best = index
    return (len(order) + 1 if best is None else best), package_id


def _prep(package_id: str, family: str, task: str) -> tuple[int, int]:
    known = _PREP_BY_PACKAGE.get(package_id)
    if known:
        return known
    if task == "sep":
        return 44100, 2
    return _PREP_BY_FAMILY.get(family, (48000, 1))


def _pick_packages(family: dict[str, Any], task: str) -> list[dict[str, Any]]:
    packages = [row for row in family.get("packages") or [] if isinstance(row, dict)]
    hints = _PACKAGE_HINTS.get(task) if family.get("family") == "mossformer2" else None
    if hints:
        picked = [
            row
            for row in packages
            if any(hint in str(row.get("id") or "") for hint in hints)
        ]
        if picked:
            return picked
    default = [row for row in packages if row.get("default")]
    return default or packages[:1]


def _package_label(family: dict[str, Any], package: dict[str, Any], fallback: str) -> str:
    label = str(package.get("display_name") or family.get("name") or fallback)
    return label.replace(" GGUF", "").strip()


def _option(task: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """一个运行中的模型 → 一行可选模型；不属于该步骤就返回空。

    ``id`` 用服务端注册的那一个：族名与包 id 都认，合成时原样发回去。
    """
    from app.producers.builtin.audiocpp import catalog

    model_id = str(item.get("id") or "")
    family = str(item.get("family") or "")
    if not model_id or family not in _ALLOWED.get(task, ()):
        return None
    # 不按实例声明的 task 过滤：一个族可以服务两步（MossFormer2 的 ss / se 是同一族的
    # 两个包），实例配置里的 task 只是族的第一项，不能代表当前加载的是哪个包。
    found_family, package = catalog.package(str(item.get("package") or model_id), None)
    if not package:
        found_family, package = catalog.package(model_id, None)
    if package and str(found_family.get("family") or "") != family:
        found_family, package = {}, {}
    if package:
        package_id = str(package.get("id") or "")
        return {
            "id": model_id,
            "label": _package_label(found_family, package, family),
            "family": family,
            "note": package_note(package_id, family),
            "prep": list(_prep(package_id, family, task)),
            "url": str(item.get("url") or ""),
        }
    family_row = catalog.family(family, None)
    preferred = (_pick_packages(family_row, task) or [{}])[0]
    package_id = str(preferred.get("id") or "")
    return {
        "id": model_id,
        "label": str(family_row.get("name") or family).replace(" GGUF", "").strip(),
        "family": family,
        "note": package_note(package_id, family) if package_id else family_note(family),
        "prep": list(_prep(package_id, family, task)),
        "url": str(item.get("url") or ""),
    }


def options_for(task: str, running: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """某一步现在能选的模型：``[{id, label, family, note, prep}]``。

    来源只有运行中的实例；顺序按步骤的推荐表，界面不再分"已装/未装"。
    """
    rows = [row for item in running if (row := _option(task, item)) is not None]
    rows.sort(key=lambda row: _rank(task, row["family"], row["id"]))
    return rows
