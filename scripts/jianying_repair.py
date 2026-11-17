"""一次性修复：把 89 条试听和 harvest_map 重新对齐。

发现的两处错位（都是早先那版临时改名脚本留下的，现在的 ``--favorites`` 已有防撞保护）：

1. ``剪映试听02``（缓存 ``f396f34c…_0.mp3``）整个文件夹不见了——音频还在剪映缓存里，重新收一份。
2. ``剪映试听84``（缓存 ``016ed1aa…_0.mp3``）被塞进了 ``曼波讲故事/默认 (2).wav``——
   两条试听并成了一个音色。拆回自己的文件夹。

做完按磁盘现状重写 ``harvest_map.json``：每条音频一个文件夹、一个 md5 键、一个点击时刻。

``--reset-names``：把所有文件夹退回「按点击顺序的序号名」，再交给 ``jianying_collect.py --favorites``
重新配一次（配对手的毫秒时刻修好之后，旧名字里有一部分是错的）。
"""

import hashlib
import importlib.util
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "jc", Path(__file__).resolve().with_name("jianying_collect.py")
)
assert SPEC and SPEC.loader
jc = importlib.util.module_from_spec(SPEC)
sys.modules["jc"] = jc
SPEC.loader.exec_module(jc)

TRY = Path("D:/JianyingPro/User Data/Cache/tryInputText")
LIB = jc.LIB_ROOT
MAP_PATH = LIB / "_catalog" / "harvest_map.json"
AUD = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}

# 丢的那条、并错的那条：库文件夹名 → 缓存 clip 文件名
LOST = ("剪映试听02", "f396f34c4a36e10619cc3a1d165aed0a/f396f34c4a36e10619cc3a1d165aed0a_0.mp3")
MERGED = ("剪映试听84", "016ed1aae9d0435535aa356b22f84c62/016ed1aae9d0435535aa356b22f84c62_0.mp3")


def cache_clip(rel: str) -> Path:
    return TRY / rel


def audio_of(folder: Path) -> list[Path]:
    return sorted(f for f in folder.iterdir() if f.suffix.lower() in AUD)


def write_sidecar(folder: Path, clip_name: str, text: str, f0: float, keep: dict) -> None:
    body: dict = {"version": 1}
    gender = "male" if 0 < f0 < 160 else ("female" if f0 >= 185 else "")
    if gender:
        body["gender"] = gender
    if text and any("一" <= ch <= "鿿" for ch in text):
        body["language"] = "zh"
    entry = dict(keep)
    if text:
        entry["text"] = text
    body["samples"] = {clip_name: entry}
    (folder / "voice.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def collect_into(folder_name: str, clip: Path, keep_sample: dict | None = None) -> dict:
    """缓存里的一条 clip → ``<库>/<folder_name>/默认.wav`` + sidecar。"""
    folder = LIB / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "默认.wav"
    if jc.is_riff(clip):
        shutil.copyfile(clip, out)
    elif not jc.to_wav(clip, out):
        out = folder / "默认.mp3"
        shutil.copyfile(clip, out)
    seconds = jc.wav_seconds(out) if out.suffix == ".wav" else 0.0
    f0 = jc.median_f0(out)
    text = jc.asr_text(out)
    write_sidecar(folder, out.name, text, f0, keep_sample or {})
    print(f"  收 {folder_name} {seconds:5.1f}s {f0:5.1f}Hz {text[:22]}")
    return {"clip": clip.name, "seconds": round(seconds, 2), "f0": round(f0, 1)}


def lru_times() -> dict[str, float]:
    p = TRY / "lru_cache.json"
    if not p.exists():
        return {}
    return {Path(t).name: int(k) / 1000.0 for k, t in json.loads(p.read_text(encoding="utf-8")).items()}


def reset_names() -> int:
    """文件夹一律退回「按点击顺序的序号名」，sidecar 只留能如实给的那几项。

    为什么重来：上一轮配对用的是**秒级**时刻——``click_times()`` 拿 ``Path(clip).stem``
    去查 ``lru_cache.json``，而缓存目录名是不带 ``_0`` 的那一段，于是毫秒键从没查到过、
    全部退回秒级。秒级差值会把贪心配对的顺序带偏，产出明显的错位（同一条收藏既能配到
    ``解说小帅`` 又能配到 ``真人播客女``）。修好之后先退回序号再重配一次，比在错名字上
    叠「·xxxx」后缀干净。
    """
    m = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    lru = lru_times()
    rows = sorted(
        ((lru.get(Path(v["clip"]).name.split("_")[0]) or float("inf")), k, v) for k, v in m.items()
    )
    if len(rows) != len(m):
        print("行数对不上，停手。")
        return 1
    MAP_PATH.with_name("harvest_map.before_reset.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    changed = []
    for rank, (_when, _key, entry) in enumerate(rows, 1):
        entry["_staging"] = f"__stg{rank:02d}"
    for rank, (_when, _key, entry) in enumerate(rows, 1):
        src = LIB / entry["folder"]
        if src.is_dir():
            shutil.move(str(src), str(LIB / entry["_staging"]))
    new: dict[str, dict] = {}
    for rank, (when, key, entry) in enumerate(rows, 1):
        stage = LIB / entry["_staging"]
        final = LIB / f"{jc.HARVEST_PREFIX}{rank:02d}"
        shutil.move(str(stage), str(final))
        auds = audio_of(final)
        f0 = float(entry.get("f0") or 0.0)
        text = ""
        if auds:
            side = final / "voice.json"
            body = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
            for samp in (body.get("samples") or {}).values():
                if isinstance(samp, dict) and samp.get("text"):
                    text = str(samp["text"])
                    break
            write_sidecar(final, auds[0].name, text, f0, {})
        row = {
            "folder": final.name,
            "clip": entry["clip"],
            "seconds": entry.get("seconds", 0.0),
            "f0": round(f0, 1),
        }
        if when != float("inf"):
            row["clicked"] = datetime.fromtimestamp(when).strftime("%m-%d %H:%M:%S.%f")[:-3]
        new[key] = row
        if entry["folder"] != final.name:
            changed.append((entry["folder"], final.name))
    MAP_PATH.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"退回序号：{len(rows)} 个文件夹，其中 {len(changed)} 个原本有真名。")
    return 0


def main() -> int:
    old = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    lru_path = TRY / "lru_cache.json"
    lru: dict[str, float] = {}
    if lru_path.exists():
        for key, target in json.loads(lru_path.read_text(encoding="utf-8")).items():
            lru[Path(target).name] = int(key) / 1000.0

    # 1) 丢的补回来
    lost_name, lost_rel = LOST
    lost_clip = cache_clip(lost_rel)
    print("补丢的那条：")
    lost_info = None
    if lost_clip.exists():
        lost_info = collect_into(lost_name, lost_clip)
    else:
        print(f"  ! 缓存里已经找不到 {lost_rel}，这条只能认丢")

    # 2) 并错的拆开
    merged_name, merged_rel = MERGED
    print("拆并错的那条：")
    merged_info = None
    mambo = LIB / "曼波讲故事"
    stray = [f for f in audio_of(mambo) if f.name != "默认.wav"]
    if stray:
        s = stray[0]
        dst = LIB / merged_name
        dst.mkdir(parents=True, exist_ok=True)
        side = mambo / "voice.json"
        body = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        sample = (body.get("samples") or {}).get(s.name, {})
        moved = dst / s.name
        shutil.move(str(s), str(moved))
        seconds = jc.wav_seconds(moved) if moved.suffix == ".wav" else 0.0
        f0 = jc.median_f0(moved)
        write_sidecar(dst, moved.name, str(sample.get("text", "")), f0, {})
        # 曼波讲故事只留自己那条
        body["samples"] = {
            k: v for k, v in (body.get("samples") or {}).items() if k != s.name
        }
        side.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {merged_name} <- 曼波讲故事/{s.name} {seconds:5.1f}s {f0:5.1f}Hz")
        merged_info = {"clip": cache_clip(merged_rel).name, "seconds": round(seconds, 2), "f0": round(f0, 1)}
    else:
        print("  曼波讲故事 只有一段音频，无需拆")

    # 3) 按磁盘现状重写 map（键 = 缓存 clip 内容的 md5）
    print("重写 harvest_map：")
    by_folder = {v["folder"]: (k, v) for k, v in old.items()}
    new: dict[str, dict] = {}
    orphans = []
    for folder in sorted(p for p in LIB.iterdir() if p.is_dir() and not p.name.startswith("_")):
        auds = audio_of(folder)
        if len(auds) != 1:
            orphans.append((folder.name, [a.name for a in auds]))
        info = None
        if folder.name == lost_name and lost_info:
            info = lost_info
        elif folder.name == merged_name and merged_info:
            info = merged_info
        elif folder.name in by_folder:
            key, entry = by_folder[folder.name]
            info = {"clip": entry["clip"], "seconds": entry.get("seconds", 0.0), "f0": entry.get("f0", 0.0)}
        if info is None:
            orphans.append((folder.name, ["<无来路>"]))
            continue
        clip_path = TRY / Path(info["clip"]).name.split("_")[0] / info["clip"]
        digest = hashlib.md5(clip_path.read_bytes()).hexdigest() if clip_path.exists() else by_folder.get(folder.name, ("", {}))[0]
        when = lru.get(Path(info["clip"]).stem)
        row = {
            "folder": folder.name,
            "clip": info["clip"],
            "seconds": info["seconds"],
            "f0": info["f0"],
        }
        if when:
            row["clicked"] = datetime.fromtimestamp(when).strftime("%m-%d %H:%M:%S")
        new[digest] = row
    MAP_PATH.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    dirs = [p.name for p in LIB.iterdir() if p.is_dir() and not p.name.startswith("_")]
    print(f"  磁盘 {len(dirs)} 个文件夹 · map {len(new)} 条 · 时刻齐的 {sum(1 for v in new.values() if v.get('clicked'))} 条")
    if orphans:
        print("  还有对不上的：", orphans)
    return 0


if __name__ == "__main__":
    sys.exit(reset_names() if "--reset-names" in sys.argv else main())
