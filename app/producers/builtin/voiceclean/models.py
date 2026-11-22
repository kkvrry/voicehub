"""步骤声明与可用的模型。

步骤顺序按处理常识固定，不随提交顺序变化：**人声分离 → 语音降噪 → 带宽扩展 →
统一声道与峰值**。分离在最前，后面的降噪与带宽扩展才只面对人声（先降噪会把伴奏
当噪声削掉，分离也就没有依据了）；降噪在带宽扩展之前，补出来的高频后面不会再被
降噪当噪声削掉；收尾是确定性的 ffmpeg 处理，放在所有模型之后，原声与结果各做一次。

候选模型有两类：

- 官方包：来自 audio.cpp 的模型清单，装了没装按包 id 判断；
- 内置工具：audio.cpp 的 ``builtin_audio_utils``，官方清单里没有 packages，权重由
  VoiceHub 自己下载（固定上游版本与 sha256），加载时按 ``path`` + ``load_options.utility``
  登记。

执行仍由运行中的 audio.cpp 实例完成。模型是否“能用”只看权重是否已下载；加载由服务
在下载后或处理前自动完成，界面不需要“已下载未加载”这一档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CORE_STEP",
    "STEPS",
    "Step",
    "builtin_asset",
    "builtin_downloaded",
    "builtin_path",
    "candidate_key",
    "candidates_for",
    "family_note",
    "find_candidate",
    "running_of",
    "step_of",
]

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
    "s2s": (
        "mossformer2_se",
        "sidon",
        "reuse",
        "realtime_reuse",
        "mossformer2_sr",
        "deepfilternet2",
        "zipenhancer",
        "rnnoise",
        "gtcrn_dns3",
        "gtcrn_vctk",
        "gtcrn_streaming",
        "flashsr",
    ),
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

#: 内置工具的权重：固定到 audio.cpp 的发布标签，不跟 main 漂移。文件名、大小与
#: sha256 都写在清单里；下载完成后校验，校验不过就丢弃。
_BUILTIN_REF = "v0.9.4"
_BUILTIN_BASE = (
    f"https://raw.githubusercontent.com/0xShug0/audio.cpp/{_BUILTIN_REF}"
    "/assets/framework/audio_utilities"
)

#: 内置音频工具（``builtin_audio_utils``）没有 packages，服务端只回族名。它们不是
#: 独立模型包，只能按 ``load_options.utility`` 或权重目录认；这里收的是上游
#: ``options.load.utility`` 的枚举，加一个工具只改这张表。
_UTILITIES: dict[str, dict[str, Any]] = {
    "deepfilternet2": {
        "label": "DeepFilterNet2",
        "note": "快速降噪，48 kHz，CPU 友好",
        "prep": (48000, 1),
        "file": "deepfilternet2/deepfilternet2.safetensors",
        "bytes": 9368328,
        "sha256": "544740171cd81e55dcc7c5d1d889ec79df5808a75eb840a669a681f0d34b0626",
        "recommended": True,
    },
    "rnnoise": {
        "label": "RNNoise",
        "note": "极速降噪，48 kHz，质量偏低",
        "prep": (48000, 1),
        "file": "rnnoise/rnnoise10Gb_15.safetensors",
        "bytes": 11540996,
        "sha256": "60dc1bf25427fad2a4ba84482fc1ebc6ceddb53b58c8a0ea605a1e1dfc7cd87d",
    },
    "zipenhancer": {
        "label": "ZipEnhancer",
        "note": "16 kHz 降噪，输出 16 kHz",
        "prep": (16000, 1),
        "file": "zipenhancer/zipenhancer.safetensors",
        "bytes": 8236256,
        "sha256": "5e1de1f4ac4b365942459dc686f9bee334e5399bdd377719e77fa4fd513b94d2",
    },
    "gtcrn_dns3": {
        "label": "GTCRN DNS3",
        "note": "16 kHz 轻量降噪，输出 16 kHz",
        "prep": (16000, 1),
        "file": "gtcrn/gtcrn_dns3.safetensors",
        "bytes": 242892,
        "sha256": "b42c37ba6be02ea3ebd339dabc0a182af892006f8947c1a7c72cc7ea26405db5",
    },
    "gtcrn_streaming": {
        "label": "GTCRN Streaming",
        "note": "16 kHz 流式降噪，可整段跑",
        "prep": (16000, 1),
        "file": "gtcrn/gtcrn_streaming.safetensors",
        "bytes": 242900,
        "sha256": "65cca0a3b4c58fb73b220d3408cda9cf0a40c243ae1622b3aff8708f17c38e53",
    },
    "gtcrn_vctk": {
        "label": "GTCRN VCTK",
        "note": "16 kHz 轻量降噪，输出 16 kHz",
        "prep": (16000, 1),
        "file": "gtcrn/gtcrn_vctk.safetensors",
        "bytes": 242892,
        "sha256": "449def35c7f8bff390c22ffc3ea90ad9e18b22e2758b6425ff4aee13d3997ff2",
    },
    "flashsr": {
        "label": "FlashSR",
        "note": "带宽扩展，16 kHz → 48 kHz；补出的是生成高频",
        "prep": (16000, 1),
        "file": "flashsr/flashsr.safetensors",
        "bytes": 356008,
        "sha256": "cfa15eb8576e50cff30bc9851fc2ca8e74b6ea379fbcded03c9cc44458b1c6e2",
    },
}

#: 归一化后的别名 → 工具 id。最长的别名先匹配，``gtcrn_streaming`` 才不会被
#: ``gtcrn`` 抢先认成同一个。
_UTILITY_ALIASES: dict[str, str] = {
    "deepfilternet2": "deepfilternet2",
    "deepfilternet": "deepfilternet2",
    "rnnoise10gb15": "rnnoise",
    "rnnoise": "rnnoise",
    "zipenhancer": "zipenhancer",
    "gtcrndns3": "gtcrn_dns3",
    "gtcrnstreaming": "gtcrn_streaming",
    "gtcrnvctk": "gtcrn_vctk",
    "gtcrn": "gtcrn_streaming",
    "flashsr": "flashsr",
}
_UTILITY_ALIASES_LONGEST = tuple(sorted(_UTILITY_ALIASES, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class Step:
    """一个可勾选的步骤。

    ``families`` 为空时按 ``task`` 取 :data:`_ALLOWED`；写出来表示这一步只收这几族，
    同一个 task 的相邻步骤才不会显示同一批模型。``utilities`` 只作用于
    ``builtin_audio_utils``：非空时只收列出的工具，用来把降噪和带宽扩展分开。
    """

    key: str
    label: str
    note: str
    task: str
    order: int
    default: bool = False
    families: tuple[str, ...] = ()
    utilities: tuple[str, ...] = ()


#: 小工具里做降噪的那几个；FlashSR 是带宽扩展，单独一张卡。
_BUILTIN_DENOISE = (
    "deepfilternet2",
    "zipenhancer",
    "rnnoise",
    "gtcrn_dns3",
    "gtcrn_vctk",
    "gtcrn_streaming",
)

STEPS: tuple[Step, ...] = (
    Step(
        "separate",
        "人声分离",
        "从含伴奏或多人说话的素材中取出人声；单人干声不需要。",
        "sep",
        1,
    ),
    Step(
        "denoise",
        "语音降噪",
        "去除底噪；大模型保真，小工具更快。",
        "s2s",
        2,
        default=True,
        families=(*_ALLOWED["s2s"], "builtin_audio_utils"),
        utilities=_BUILTIN_DENOISE,
    ),
    Step(
        "bandwidth",
        "带宽扩展",
        "只补高频；补出的是生成高频，不适合训练素材。"
        "只用于本身窄带的素材：44.1/48 kHz 的会先降到 16 kHz 再补，真高频反而丢了。",
        "s2s",
        3,
        families=("builtin_audio_utils",),
        utilities=("flashsr",),
    ),
)

#: 收尾步骤：不是模型，默认不选。名字直接说做了什么，避免"规整"这种没有
#: 可验证含义的说法；执行内容全部写在 note 里。
CORE_STEP: dict[str, Any] = {
    "key": "normalize",
    "label": "统一声道与峰值",
    "order": 4,
    "core": True,
    "default": False,
    "note": "模型之后单独做：高通 60 Hz · 峰值 ≤ −1 dBFS · 原声与结果同一套参数 · 时长校验",
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


def builtin_asset(utility: str) -> dict[str, Any] | None:
    """内置工具的一条权重记录：文件名、大小、sha256 与下载地址。"""
    meta = _UTILITIES.get(utility)
    if meta is None:
        return None
    return {**meta, "url": f"{_BUILTIN_BASE}/{meta['file']}"}


def builtin_path(root: Path, utility: str) -> Path:
    """权重落到 ``<root>/<utility>/<原文件名>``；audio.cpp 按目录加载。"""
    meta = _UTILITIES[utility]
    return root / utility / Path(str(meta["file"])).name


def builtin_downloaded(root: Path, utility: str) -> bool:
    """按文件大小判断是否已下载；哈希只在下载完成时校验一次。"""
    meta = _UTILITIES.get(utility)
    if meta is None:
        return False
    path = builtin_path(root, utility)
    try:
        return path.is_file() and path.stat().st_size == int(meta["bytes"])
    except OSError:
        return False


def candidate_key(candidate: dict[str, Any]) -> str:
    if candidate.get("source") == "builtin":
        return f"builtin:{candidate.get('utility') or candidate.get('id')}"
    return f"pkg:{candidate.get('package') or candidate.get('id')}"


def running_of(
    candidate: dict[str, Any], running: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """候选模型对应哪一条运行中的注册；没加载时返回空。"""
    for row in running:
        if not isinstance(row, dict):
            continue
        if candidate.get("source") == "builtin":
            if _utility_of(row) == candidate.get("utility"):
                return row
            continue
        package = str(row.get("package") or "")
        if package and package == candidate.get("package"):
            return row
        # 只按族名注册、没带包 id 时，只有该族的默认包能认。
        if (
            not package
            and candidate.get("default")
            and row.get("family") == candidate.get("family")
        ):
            return row
    return None


def _packages_for_step(family_row: dict[str, Any], task: str) -> list[dict[str, Any]]:
    """这一步在该族里要列哪些包；MossFormer2 按 ss / se / sr 分开。"""
    packages = [
        row for row in family_row.get("packages") or [] if isinstance(row, dict)
    ]
    hints = _PACKAGE_HINTS.get(task) if family_row.get("family") == "mossformer2" else None
    if hints:
        picked = [
            row
            for row in packages
            if any(hint in str(row.get("id") or "") for hint in hints)
        ]
        if picked:
            return picked
    return packages


def _package_candidate(
    family_row: dict[str, Any],
    package: dict[str, Any],
    task: str,
    installed: set[str],
    running: list[dict[str, Any]],
) -> dict[str, Any]:
    package_id = str(package.get("id") or "")
    family = str(family_row.get("family") or "")
    default = bool(package.get("default"))
    row = {
        "id": package_id,
        "label": _package_label(family_row, package, family),
        "family": family,
        "note": package_note(package_id, family),
        "prep": list(_prep(package_id, family, task)),
        "source": "package",
        "package": package_id,
        "default": default,
        "downloaded": package_id in installed,
        "ready": False,
        "recommended": False,
    }
    row["ready"] = running_of(row, running) is not None
    # 已经在跑的模型不一定能在模型目录里扫到（外部实例、按路径加载），
    # 运行中本身就是最强的“已下载”证据。
    row["downloaded"] = row["downloaded"] or row["ready"]
    return row


def _builtin_candidate(
    utility: str,
    root: Path,
    running: list[dict[str, Any]],
) -> dict[str, Any] | None:
    meta = _UTILITIES.get(utility)
    if meta is None:
        return None
    row = {
        "id": utility,
        "label": str(meta["label"]),
        "family": "builtin_audio_utils",
        "note": str(meta["note"]),
        "prep": list(meta["prep"]),
        "source": "builtin",
        "utility": utility,
        "default": False,
        "downloaded": builtin_downloaded(root, utility),
        "ready": False,
        "recommended": bool(meta.get("recommended")),
    }
    row["ready"] = running_of(row, running) is not None
    row["downloaded"] = row["downloaded"] or row["ready"]
    return row


def candidates_for(
    step: Step,
    running: list[dict[str, Any]],
    installed: set[str],
    builtin_root: Path,
    catalog_root: Path | None = None,
) -> list[dict[str, Any]]:
    """这一步登记的全部模型：官方包 + 内置工具，带下载与运行状态。

    这里不裁掉未下载的模型：卡片要把它们列出来并提供下载。是否可执行由服务在
    下载/加载后回答。
    """
    from app.producers.builtin.audiocpp import catalog

    allowed = step.families or _ALLOWED.get(step.task, ())
    rows: list[dict[str, Any]] = []
    for family in allowed:
        if family == "builtin_audio_utils":
            continue
        family_row = catalog.family(family, catalog_root)
        if not family_row:
            continue
        packages = _packages_for_step(family_row, step.task)
        # 只列默认精度，加上用户已经装过/正在跑的其它精度：q8/f16 是同一个模型，
        # 全列出来只会把卡片塞满。没有默认项时退回第一个包作为这一族的代表。
        kept = [
            package
            for package in packages
            if bool(package.get("default"))
            or str(package.get("id") or "") in installed
            or any(
                str(row.get("package") or "") == str(package.get("id") or "")
                for row in running
            )
        ]
        packages = kept or packages[:1]
        # 族名注册的服务端不带包 id：把该步骤排在最前的一个包当作它的身份，
        # 只有这一行会认那条运行记录，避免同族两个包同时显示“已就绪”。
        if packages and not any(bool(item.get("default")) for item in packages):
            packages = [dict(item) for item in packages]
            packages[0]["default"] = True
        for package in packages:
            row = _package_candidate(family_row, package, step.task, installed, running)
            if row["id"]:
                rows.append(row)
    rows.sort(
        key=lambda row: (
            0 if row.get("default") else 1,
            *_rank(
                step.task, str(row["family"]), str(row.get("package") or row["id"])
            ),
        )
    )
    for utility in step.utilities:
        row = _builtin_candidate(utility, builtin_root, running)
        if row is not None:
            rows.append(row)
    return rows


def find_candidate(
    step: Step,
    model_id: str,
    running: list[dict[str, Any]],
    installed: set[str],
    builtin_root: Path,
    catalog_root: Path | None = None,
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in candidates_for(
                step, running, installed, builtin_root, catalog_root
            )
            if row["id"] == model_id
        ),
        None,
    )


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


def _package_label(family: dict[str, Any], package: dict[str, Any], fallback: str) -> str:
    label = str(package.get("display_name") or family.get("name") or fallback)
    return label.replace(" GGUF", "").strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _utility_of(item: dict[str, Any]) -> str:
    """从模型 id / 路径 / load_options 里认内置工具；认不出返回空串。"""
    for key in ("utility", "package", "id", "name", "path"):
        text = _norm(item.get(key) or "")
        if not text:
            continue
        for alias in _UTILITY_ALIASES_LONGEST:
            if alias in text:
                return _UTILITY_ALIASES[alias]
    return ""
