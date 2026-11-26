from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.infra.atomic import write_json
from app.infra.loghub import CHANNEL
from app.infra.process import CREATE_NO_WINDOW
from app.producers.base import ProducerError
from app.producers.builtin.audiobook.loudness import (
    apply_gain,
    balance_gains,
    evaluate_master,
    master_gain,
    measure_audio,
)
from app.producers.builtin.audiobook.paths import abs_path
from app.producers.builtin.audiobook.rules import NARRATOR

if TYPE_CHECKING:
    from app.producers.builtin.audiobook.pipeline import AudiobookService
    from app.producers.builtin.audiobook.settings import AudiobookSettings

_BATCH = 12
_CODECS = {"wav": "pcm_s16le", "m4a": "aac", "mp3": "libmp3lame"}


def _key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _source_key(path: Path) -> str:
    stat = path.stat()
    return _key([2, str(path), stat.st_size, stat.st_mtime_ns])


def _source_keys(paths: list[Path]) -> dict[Path, str]:
    wanted = set(paths)
    result = {}
    for folder in {path.parent for path in wanted}:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    path = folder / entry.name
                    if path in wanted and entry.is_file():
                        stat = entry.stat()
                        result[path] = _key([2, str(path), stat.st_size, stat.st_mtime_ns])
        except OSError as exc:
            raise ProducerError(f"无法读取行音频目录：{folder}") from exc
    if wanted - result.keys():
        missing = next(iter(wanted - result.keys()))
        raise ProducerError(f"行音频不存在：{missing.name}")
    return result


def _row_key(row: dict[str, Any]) -> str:
    return _key([row.get(name) for name in ("audio_key", "speaker", "emotion", "text", "params")])


def _settings_key(settings: AudiobookSettings) -> str:
    return _key([1, settings.row_balance_mode, settings.row_balance_strength,
                 settings.row_balance_max_gain_db])


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    write_json(path, value, indent=None, allow_nan=False)


class LoudnessStage:
    def __init__(self, service: AudiobookService) -> None:
        self.svc = service
        self._plans: dict[Path, tuple[int, dict[str, Any]]] = {}

    def plan(self, root: Path) -> dict[str, Any]:
        path = root / "loudness" / "rows.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return {}
        cached = self._plans.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        plan = _read_json(path)
        self._plans[path] = (stamp, plan)
        return plan

    def row_audio(
        self, root: Path, row: dict[str, Any], plan: dict[str, Any] | None = None
    ) -> tuple[Path, str]:
        original = abs_path(root, row["audio"])
        settings = self.svc.settings
        if not settings.row_balance_enabled:
            return original, "原始音频"
        plan = self.plan(root) if plan is None else plan
        item = plan.get("rows", {}).get(row["id"], {})
        if (row.get("status") != "stale" and plan.get("settings_key") == _settings_key(settings)
                and item.get("row_key") == _row_key(row)):
            try:
                if item.get("source_key") == _source_key(original):
                    name = item.get("file", "")
                    path = root / "loudness" / name if name else original
                    if name and (Path(name).name != name or not path.is_file()):
                        return original, "原始音频 · 处理版不存在"
                    if not item.get("measurable"):
                        return original, "原始音频 · 无有效响度，未调整"
                    return path, f"均衡 {item['gain_db']:+.2f} dB"
            except OSError:
                pass
        return original, "原始音频 · 待响度处理"

    def discard_row_plan(self, root: Path, row_ids: set[str]) -> None:
        plan = self.plan(root)
        rows = plan.get("rows", {})
        remaining = {key: value for key, value in rows.items() if key not in row_ids}
        if len(remaining) != len(rows):
            _write_json(root / "loudness" / "rows.json", {**plan, "rows": remaining})

    def process(
        self,
        project_id: str,
        settings: AudiobookSettings,
        progress: Callable[[str, int, int, str], None],
        cancelled: Callable[[], bool] = lambda: False,
        announce: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        if not settings.row_balance_enabled:
            return {"enabled": False, "seconds": 0.0}
        progress("rows_measure", 0, 0, "正在准备全书行音频响度分析")
        self.svc._settle_stale(project_id)
        rows = [row for row in self.svc.store.all_rows(project_id)
                if row.get("audio") and row.get("status") != "stale"]
        if not rows:
            raise ProducerError("没有可处理的最新行音频。")
        binary = self.svc.output._ffmpeg()
        root = self.svc.project_folder(project_id)
        folder = root / "loudness"
        folder.mkdir(parents=True, exist_ok=True)
        cache_path = folder / "measurements.json"
        cache = _read_json(cache_path)
        paths = {row["id"]: abs_path(root, row["audio"]) for row in rows}
        indexed = _source_keys(list(paths.values()))
        sources = {row_id: (path, indexed[path]) for row_id, path in paths.items()}
        cache = {key: cache[key] for key in set(indexed.values()) if key in cache}
        missing = list(dict.fromkeys(path for path, key in sources.values() if key not in cache))
        keys = {path: key for path, key in sources.values()}
        for offset in range(0, len(missing), _BATCH):
            if cancelled():
                return {"cancelled": True}
            batch = missing[offset:offset + _BATCH]
            progress("rows_measure", offset, len(missing),
                     f"行级响度测量 {offset}/{len(missing)} · 其余复用缓存")
            values = measure_audio(binary, batch)
            for path, metrics in values.items():
                if _source_key(path) != keys[path]:
                    raise ProducerError("分析期间行音频发生变化，请重新处理。")
                cache[keys[path]] = metrics
            if (offset + len(batch)) % 240 == 0 or offset + len(batch) == len(missing):
                _write_json(cache_path, cache)
        measured_at = time.perf_counter()
        voices = self.svc.store.voice_map(project_id)
        chapter_voices = self.svc.chapter_voice_rows(project_id)
        items = []
        for row in rows:
            speaker = row["speaker"] or NARRATOR
            voice = self.svc.mapping_for(row, voices, chapter_voices) or {}
            group = [voice.get("engine"), voice.get("voice_id"), voice.get("sample_file")]
            if not voice.get("voice_id"):
                group = [speaker]
            items.append({
                "id": row["id"], "group": _key(group),
                "expressive": (str(row.get("emotion") or "").strip().lower()
                               not in {"", "平静", "正常", "中性", "neutral"}
                               or "[[" in row.get("text", "")),
                **cache[sources[row["id"]][1]],
            })
        gains, reference = balance_gains(
            items, mode=settings.row_balance_mode,
            strength=settings.row_balance_strength / 100,
            max_gain_db=settings.row_balance_max_gain_db,
        )
        old = self.plan(root)
        plan: dict[str, Any] = {"settings_key": _settings_key(settings), "rows": {},
                                "reference_lufs": reference}
        renders: list[tuple[Path, Path, float]] = []
        changed: set[str] = set()
        with os.scandir(folder) as entries:
            existing = {entry.name for entry in entries if entry.is_file()}
        for row in rows:
            path, source_key = sources[row["id"]]
            gain = round(gains[row["id"]], 4)
            name = f"{_key([source_key, gain, 1])}.wav" if gain else ""
            if name and name not in existing:
                renders.append((path, folder / name, gain))
            entry = {"source_key": source_key, "row_key": _row_key(row),
                     "gain_db": gain, "file": name,
                     "measurable": cache[source_key]["integrated"] is not None}
            plan["rows"][row["id"]] = entry
            previous = old.get("rows", {}).get(row["id"], {})
            if previous != entry:
                changed.add(row["chapter_id"])
        renders = list({target: (source, target, gain) for source, target, gain in renders}.values())
        for offset in range(0, len(renders), _BATCH):
            if cancelled():
                return {"cancelled": True}
            progress("rows_apply", offset, len(renders),
                     f"行级增益处理 {offset}/{len(renders)} · 原始音频保留")
            self._render_batch(binary, renders[offset:offset + _BATCH])
        if cancelled():
            return {"cancelled": True}
        if _source_keys(list(indexed)) != indexed:
            raise ProducerError("处理期间行音频发生变化，请重新处理。")
        if changed:
            self.svc.store.set_chapters_stage(list(changed), "audio", clear_audio=True)
        if plan != old:
            _write_json(folder / "rows.json", plan)
        finished = time.perf_counter()
        unavailable = sum(not item["measurable"] for item in plan["rows"].values())
        warnings = []
        if unavailable:
            warnings.append(f"全书 {unavailable} 行无有效响度，保持原样。")
        baseline_groups = {item["group"] for item in items
                           if item["integrated"] is not None and not item["expressive"]}
        unreferenced = sum(item["integrated"] is not None and item["group"] not in baseline_groups
                          for item in items)
        if unreferenced:
            warnings.append(f"全书 {unreferenced} 行所属音色缺少常态语音基线，保持原样。")
        message = (f"响度处理完成：全书 {len(rows)} 行 · 新测量 {len(missing)} 行 · "
                   f"新处理 {len(renders)} 行 · 无有效响度 {unavailable} 行 · "
                   f"测量 {measured_at - started:.2f}s / 处理 {finished - measured_at:.2f}s")
        if unreferenced:
            message += f" · 全书缺少常态基线 {unreferenced} 行，保持原样"
        progress("rows_done", len(rows), len(rows), message)
        if announce or missing or renders:
            CHANNEL.info(message, source="audiobook", project=project_id)
        return {"enabled": True, "rows": len(rows), "measured": len(missing),
                "rendered": len(renders), "unmeasurable": unavailable, "warnings": warnings,
                "reference_lufs": reference, "seconds": finished - started,
                "measure_seconds": measured_at - started,
                "apply_seconds": finished - measured_at, "message": message}

    @staticmethod
    def _render_batch(binary: str, items: list[tuple[Path, Path, float]]) -> None:
        command = [binary, "-hide_banner", "-nostdin", "-y", "-threads", "1"]
        for source, _, _ in items:
            command += ["-i", str(source)]
        temporaries = []
        for index, (_, target, gain) in enumerate(items):
            temporary = target.with_suffix(".part.wav")
            temporaries.append((temporary, target))
            command += ["-map", f"{index}:a:0", "-af", f"volume={gain:.4f}dB:precision=double",
                        "-c:a", "pcm_f32le", "-ar", "44100", "-threads", "1", str(temporary)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise ProducerError("行级增益处理失败：" + result.stderr.decode("utf-8", "replace")[-800:])
            for temporary, target in temporaries:
                temporary.replace(target)
        finally:
            for temporary, _ in temporaries:
                temporary.unlink(missing_ok=True)

    def master(
        self, binary: str, source: Path, target: Path, settings: AudiobookSettings,
        metadata: dict[str, str], progress: Callable[[str, int, int, str], None],
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        progress("master_measure", 0, 1, "成品响度测量 · 不改变行间强弱关系")
        measured = measure_audio(binary, [source], cancelled)[source]
        plan = master_gain(
            measured, preset=settings.master_loudness_preset,
            target_lufs=settings.master_loudness_target, peak_db=settings.master_true_peak_db,
            headroom_db=0.3 if settings.merge_format != "wav" else 0.0,
        )
        progress("master_apply", 0, 1, f"成品固定增益 {plan['gain_db']:+.2f} dB · 不自动压缩")
        def render() -> None:
            apply_gain(binary, source, target, plan["gain_db"],
                       codec=_CODECS[settings.merge_format], bitrate=settings.merge_bitrate,
                       metadata=metadata, cancelled=cancelled)
        render()
        progress("master_check", 0, 1, "编码后响度与真峰值复检")
        after = measure_audio(binary, [target], cancelled)[target]
        if after["true_peak"] is not None and after["true_peak"] > plan["peak_ceiling"]:
            plan["gain_db"] -= after["true_peak"] - plan["peak_ceiling"] + 0.2
            plan["limited"] = True
            render()
            after = measure_audio(binary, [target], cancelled)[target]
        result = evaluate_master(after, plan)
        return {**result, "before": measured, "gain_db": plan["gain_db"],
                "seconds": time.perf_counter() - started, "plan": plan}
