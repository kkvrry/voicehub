"""音色通道——「这台引擎的音色由谁供给」的域概念。

插件声明能力（上限，代码写死），通道决定用法（mode，用户可在引擎面板改）。
默认推导：有 ``voice_listing`` → ``own``；否则有 ``reference_audio`` → ``library``；
都没有则通道留空。本模块零 IO：只认两个布尔与存下的配置段。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CHANNEL_LABELS",
    "CHANNEL_LIBRARY",
    "CHANNEL_OWN",
    "SCOPE_ALL",
    "VoiceChannel",
    "allowed_modes",
    "as_payload",
    "default_channel",
    "resolve_channel",
]

#: 自有音色：引擎有它自己的音色清单/克隆（平台音色条目），生成物挂在音色上。
CHANNEL_OWN = "own"
#: 库桥：引擎没有自有音色，用**音色库**的音色（合成时经参考音频供给）。
CHANNEL_LIBRARY = "library"

#: ``library`` 通道的可用范围。第一版只有"全部"；配置写了别的值也落回 ``all``。
SCOPE_ALL = "all"

#: 通道在界面上的显示文案（中文只在这一处写，前端不认识模式名）。
CHANNEL_LABELS: dict[str, tuple[str, str]] = {
    CHANNEL_OWN: (
        "自有音色",
        "音色来自平台清单与克隆，入库后与本地音色统一管理；合成用这台引擎自己的音色 id",
    ),
    CHANNEL_LIBRARY: (
        "音色库供给",
        "没有自有音色；合成时把音色库的音色经参考音频提供给这台引擎",
    ),
}


@dataclass(frozen=True, slots=True)
class VoiceChannel:
    """一台引擎与音色库之间的供给关系（配置的结果，不是能力的别名）。

    :param mode: ``own`` / ``library``；空串 = 这台引擎没有可供给的音色。
    :param scope: ``library`` 通道的可用范围，第一版只有 ``all``。
    """

    mode: str = ""
    scope: str = SCOPE_ALL

    @property
    def is_own(self) -> bool:
        return self.mode == CHANNEL_OWN

    @property
    def is_library(self) -> bool:
        return self.mode == CHANNEL_LIBRARY


def default_channel(*, voice_listing: bool, reference_audio: bool) -> VoiceChannel:
    """能力 → 默认通道。清单赢过参考音频；两个能力都没有的引擎通道留空。"""
    if voice_listing:
        return VoiceChannel(mode=CHANNEL_OWN)
    if reference_audio:
        return VoiceChannel(mode=CHANNEL_LIBRARY)
    return VoiceChannel()


def allowed_modes(*, voice_listing: bool, reference_audio: bool) -> tuple[str, ...]:
    """这台引擎允许配成的通道——能力是上限，越界配置会被 :func:`resolve_channel` 丢回默认。"""
    modes: list[str] = []
    if voice_listing:
        modes.append(CHANNEL_OWN)
    if reference_audio:
        modes.append(CHANNEL_LIBRARY)
    return tuple(modes)


def resolve_channel(
    default: VoiceChannel, allowed: tuple[str, ...], stored: Mapping[str, Any]
) -> VoiceChannel:
    """默认通道 + 存储的配置段 → 生效通道。

    容错口径：认不出的键丢掉、越界的值回落，绝不让引擎读不出配置。
    旧配置里的 ``promote_generated`` 直接忽略——那个开关已经删了：生成物一律挂回
    它自己的音色，不再按目录扫成第二条。
    """
    raw = stored if isinstance(stored, Mapping) else {}
    mode = str(raw.get("mode") or "").strip()
    if mode not in allowed:
        # 没写或越界 → 一律默认；allowed 为空时任何值都落回默认。
        mode = default.mode
    if not mode:
        return VoiceChannel()
    scope = str(raw.get("scope") or SCOPE_ALL).strip() or SCOPE_ALL
    return VoiceChannel(
        mode=mode,
        scope=scope if mode == CHANNEL_LIBRARY else SCOPE_ALL,
    )


def as_payload(
    channel: VoiceChannel, *, default: VoiceChannel, allowed: tuple[str, ...]
) -> dict[str, Any]:
    """通道 → 下发给界面的那一包（含默认值与可选列表，前端不自行推导）。"""
    label, note = CHANNEL_LABELS.get(channel.mode, ("", ""))
    default_label = CHANNEL_LABELS.get(default.mode, ("", ""))[0]
    return {
        "mode": channel.mode,
        "mode_label": label,
        "mode_note": note,
        "scope": channel.scope,
        "default_mode": default.mode,
        "default_mode_label": default_label,
        "allowed_modes": [
            {
                "mode": mode,
                "label": CHANNEL_LABELS.get(mode, ("", ""))[0],
                "note": CHANNEL_LABELS.get(mode, ("", ""))[1],
            }
            for mode in allowed
        ],
        "configurable": bool(allowed),
    }
