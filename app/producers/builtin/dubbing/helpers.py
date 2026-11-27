"""配音工作区的纯规则、格式化与说话人折算辅助函数。

与 :mod:`app.producers.builtin.dubbing.service` 拆分：只保留无状态辅助函数与常量，
不持有数据库、任务锁或运行时状态。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra.atomic import replace_file
from app.producers.builtin.dubbing import diar as speaker_models
from app.usecase.engines import is_auto_engine

#: 太短的段不单独成句：那是呼吸或杂音，识别出来也只有一个字。
_MIN_SEGMENT_S = 0.35
#: 切段时两端各留一点余量，避免把首尾的辅音切掉。
_SEGMENT_PAD_S = 0.06

#: out/ 里的中间产物：不算成品，也不用来给字幕起名。
_MIX_INTERMEDIATE = {"vocal.wav", "mix.wav", "bed.wav", "vocals-masked.wav"}

#: 引用导入接受的容器；与上传同一组，不在这里的路径不交给 ffmpeg 猜。
_SOURCE_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
}
#: 重新指定源文件时允许的时长偏差：段落时间码按原时长排，换一条片子会整片错位。
_RELINK_TOLERANCE_S = 0.2

#: 分轨按块跑：audio.cpp 把整段音频编成 base64 放在响应里，两小时的片一次几十 GB
#: 内存会直接打穿。十分钟一块把峰值压到几百 MB，也顺带给了任务进度。
_STEM_CHUNK_S = 600.0
#: 相邻块重叠一点再切掉：模型在边界缺上下文，直接拼会听到一道接缝。
_STEM_OVERLAP_S = 0.5
#: 模型输出与输入时长的容许偏差：对不上就说明返回的不是这一块。
_STEM_TOLERANCE_S = 0.25
#: 运行中分离模型的缓存时长（秒）：详情会被反复拉取，不必每次去连实例。
_STEM_MODELS_TTL = 5.0

#: 字幕产物的扩展名。成品区据此把字幕与成片分开；重写字幕时也只清这些，
#: 不碰 out 里别的文件。
_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt", ".lrc", ".txt", ".ass"})

#: 转录的段与说话轮次的重叠低于这个秒数不算命中：分离模型在段边界会多铺几帧。
_TURN_MIN_OVERLAP_S = 0.05

#: 引擎中文名只在这里查一次：日志里写 key 等于让人自己翻译。
_ENGINE_NAMES: dict[str, str] = {}


class _JobCancelled(Exception):
    """任务被用户取消：由检查点抛出，``_run_job`` 记成 cancelled 而不是 failed。"""


def _assembly_message(result: dict[str, Any]) -> str:
    """贴齐完成后给任务条的一句话；与前端原先的完成提示同一组口径。"""
    parts = [f"替换 {result['dubbed']} 段"]
    if result.get("kept"):
        parts.append(f"保留原声 {result['kept']} 段")
    if result.get("delayed"):
        parts.append(f"顺延 {len(result['delayed'])} 段")
    if result.get("fitted"):
        parts.append(f"变速 {len(result['fitted'])} 段")
    if result.get("truncated"):
        parts.append(f"截断 {len(result['truncated'])} 段")
    if result.get("skipped"):
        parts.append(f"{result['skipped']} 段未配音")
    return " · ".join(parts)


def _export_message(result: dict[str, Any]) -> str:
    size = int(result.get("size") or 0)
    return f"已导出 {result['file']}（{size / 1024 / 1024:.1f} MB）"


def _engine_name(key: str) -> str:
    """引擎 key → 界面上的名字；插件卸载后照 key 记，不抛。"""
    key = str(key or "")
    if not key:
        return "—"
    if is_auto_engine(key):
        return "自动"
    if key not in _ENGINE_NAMES:
        try:
            from app.engines.registry import detect_engine

            engine = detect_engine(key)
            _ENGINE_NAMES[key] = engine.label if engine is not None else key
        except Exception:  # noqa: BLE001 - 日志不能因为插件坏掉再抛一次
            _ENGINE_NAMES[key] = key
    return _ENGINE_NAMES[key]


def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _tc_text(ms: int) -> str:
    total = max(0, int(ms))
    return f"{total // 60000:02d}:{total % 60000 // 1000:02d}.{total % 1000:03d}"


def _params_text(params: dict[str, Any]) -> str:
    """角色参数压成一行：空值与内部分解出来的情绪不重复写。"""
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if key == "emotion" or value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = text.strip()
        if not text:
            continue
        parts.append(f"{key}={text[:28]}{'…' if len(text) > 28 else ''}")
    return " · ".join(parts)


def _sample_text(role: dict[str, Any]) -> str:
    mode = str(role.get("sample_mode") or "")
    if mode == "fixed":
        return f"固定样例 {role.get('sample_file') or '（未指定）'}"
    if mode == "emotion":
        return "按情绪匹配样例"
    return "主样例"


def _role_text(role: dict[str, Any]) -> str:
    """一段话里说清"谁、用什么声音、哪台引擎"。"""
    voice = str(role.get("voice_name") or role.get("voice_id") or "未选音色")
    return f"{role.get('name') or '旁白'} / {voice} / {_engine_name(str(role.get('engine') or ''))}"


def _cue_text(cue: dict[str, Any]) -> str:
    text = " ".join(str(cue.get("text") or "").split())
    short = f"{text[:18]}{'…' if len(text) > 18 else ''}"
    return (
        f"第 {cue.get('ordinal')} 段（{_tc_text(int(cue.get('start_ms') or 0))}–"
        f"{_tc_text(int(cue.get('end_ms') or 0))}）「{short}」"
    )


def role_of(cue: dict[str, Any], roles: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """这一段归谁：段落没指定角色时归旁白（第一条角色）。

    空 ``role_id`` 的段落一律算旁白，**不是**"随便找一条会配音的角色"。
    旁白选原声时必须真的不合成——按后一种理解，这些段会被别人的音色念一遍。
    """
    if not roles:
        return None
    wanted = str(cue.get("role_id") or "")
    for role in roles:
        if role["id"] == wanted:
            return role
    return roles[0]


def is_dubbed(cue: dict[str, Any], roles: Sequence[dict[str, Any]]) -> bool:
    """这一段要不要重新配音：角色是配音模式即要。原声角色只保留源音频。"""
    role = role_of(cue, roles)
    return bool(role and role.get("mode") == "tts")


def _file_version(path: Path) -> str:
    """文件的版本号：重配写的是同一个路径，只能靠 mtime + 大小区分"这是新的一版"。"""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def _file_time(path: Path, *, precise: bool = False) -> str:
    """文件时间。``precise`` 用微秒：与段落时间戳比先后时，同一秒也要分得清。"""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
        return stamp.isoformat(timespec="microseconds" if precise else "seconds")
    except OSError:
        return ""


def _newest_first(path: Path) -> tuple[int, str]:
    """成片排序用：修改时间取到纳秒，同一秒导出的两份也分得出先后。"""
    try:
        return (path.stat().st_mtime_ns, path.name)
    except OSError:
        return (0, path.name)


def _stamp(path: Path) -> str:
    """源文件的指纹：切出来的试听片段按它命名，换源或改切分后不复用旧片段。"""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_size:x}-{stat.st_mtime_ns:x}"


def _publish_media(scratch: Path, target: Path, *, attempts: int = 6) -> None:
    """媒体文件改名成正式名字；播放器占着目标时退避重试。"""
    replace_file(scratch, target, attempts=attempts)


def _join_text(left: str, right: str) -> str:
    """合并两段文本：中文直接相连，西文之间补一个空格。

    中文句子之间插空格会让字幕看着像机器拼接的；西文不补空格又会粘成一个词。
    判据只看边界字符，不猜语种。
    """
    left, right = left.strip(), right.strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "，。！？；：、,.!?;:）)" or right[0] in "，。！？；：、,.!?;:（(":
        return left + right
    if "\u4e00" <= left[-1] <= "\u9fff" or "\u4e00" <= right[0] <= "\u9fff":
        return left + right
    return f"{left} {right}"


def _now_ms(seconds: float) -> int:
    return round(max(0.0, seconds) * 1000)


def _speech_segments(
    spans: Sequence[tuple[float, float]],
    duration: float,
    *,
    max_segment: float,
    min_speech: float = _MIN_SEGMENT_S,
    pad: float = _SEGMENT_PAD_S,
) -> list[tuple[float, float]]:
    """静音区间取补集 → 说话区间。

    - 两端各留 ``pad`` 秒，不切断字头字尾；
    - 短于 ``min_speech`` 的丢弃（呼吸、翻页、椅子响）；
    - 超过 ``max_segment`` 的按等长硬切：没有停顿就没有依据，硬切至少让每段
      能被一次识别请求容纳，字数分配由识别结果决定。
    """
    if duration <= 0:
        return []
    edges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        start = max(0.0, float(start))
        end = min(duration, float(end))
        if start - cursor >= min_speech:
            edges.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_speech:
        edges.append((cursor, duration))

    out: list[tuple[float, float]] = []
    for start, end in edges:
        start = max(0.0, start - pad)
        end = min(duration, end + pad)
        if end - start < min_speech:
            continue
        if max_segment > 0 and end - start > max_segment:
            pieces = int((end - start) // max_segment) + 1
            span = (end - start) / pieces
            out.extend((start + span * i, start + span * (i + 1)) for i in range(pieces))
        else:
            out.append((start, end))
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _running_speaker_models() -> list[dict[str, Any]]:
    """运行中的说话人分离模型：模型白名单在 ``dubbing.diar``，这里只读状态。"""
    try:
        from app.usecase.producers import started_service

        service = started_service("audiocpp")
        if service is not None:
            return speaker_models.options(list(service.running_models()))
    except Exception:  # noqa: BLE001 - 读不到运行时就是没有可选模型
        return []
    return []


def _speaker_labels_from_turns(
    segments: Sequence[tuple[float, float]], turns: Sequence[dict[str, Any]]
) -> dict[int, int]:
    """把说话轮次折到切段上：每段取重叠时间最长的轮次的说话人。

    簇号按 ``speaker_id`` 首次出现的顺序排成 0,1,2…，界面从「说话人 1」数起。
    分离模型覆盖全部语音区，正常每段都有命中；边界抖动靠重叠时长最大值吸收。
    """
    order: dict[str, int] = {}
    labels: dict[int, int] = {}
    for index, (start, end) in enumerate(segments):
        best_speaker, best_overlap = "", 0.0
        for turn in turns:
            overlap = min(end, float(turn["end"])) - max(start, float(turn["start"]))
            if overlap > best_overlap:
                best_speaker, best_overlap = str(turn["speaker"]), overlap
        if best_overlap < _TURN_MIN_OVERLAP_S:
            continue
        if best_speaker not in order:
            order[best_speaker] = len(order)
        labels[index] = order[best_speaker]
    return labels
