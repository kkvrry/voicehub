"""一次性回填：把库里已有样例的时长写进各自的 ``voice.json``。

**为什么要有这一趟**：时长备份是「落盘那一刻记一次」的（见 ``infra/archive.record_sample``），
本功能上线之前的样例一条都没有。没有它，`hub.db` 重建或整个目录改名之后，扫描要为每个
文件重起一次 ffprobe；有了它，字节数对得上就直接采用。

跑法（默认只统计、不写）：

```
VOICEHUB_DATA_DIR=F:/ai/audiobook-studio/data \\
    .venv/Scripts/python.exe scripts/backfill_durations.py          # 先看数
VOICEHUB_DATA_DIR=F:/ai/audiobook-studio/data \\
    .venv/Scripts/python.exe scripts/backfill_durations.py --write   # 再写
```

几条纪律：

- **只碰 ``voice.json``**，且默认只改**已经存在**的那一份（连目录里的音频都不 rename）；
  要给还没有 sidecar 的音色建一份，显式加 ``--create``——那是在用户的库里凭空多一个文件。
- 只补 ``duration`` / ``bytes``，条目里原有的 ``text`` / ``emotion`` / ``tags`` / ``params``
  原样保留（读-改-写整份，不重排、不丢字段）。
- 已有记录且字节数对得上的跳过，所以随时可以重复跑。
- 量不出来（没有 ffprobe、编码不认）就跳过，绝不写 0（0 秒是"没量过"，写进去会被当真）。

它顺带把这一趟量到的时长写进 ``hub.db`` 的时长缓存，下次扫描直接命中。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 好让下面那几行 import 生效

from app.infra.config import data_dir, load_config
from app.infra.db import HubDB
from app.infra.duration_cache import DurationCache
from app.infra.sidecar import sample_bytes, sample_duration, write_sidecar
from app.infra.walker import walk_root


def _round(value: float) -> float:
    """与 ``sidecar._seconds`` 同一个折形：三位小数。"""
    return round(float(value), 3)


def backfill_root(root: str, cache: DurationCache, *, write: bool, create: bool) -> dict[str, int]:
    """扫一个根，把缺的时长补进各音色目录的 sidecar。返回这一根的计数。"""
    stats = {"folders": 0, "touched": 0, "entries": 0, "new_files": 0, "skipped": 0}
    for found in walk_root(root, ffprobe_path=load_config().ffprobe_path, cache=cache):
        stats["folders"] += 1
        side = found.sidecar
        has_sidecar = (Path(found.folder) / "voice.json").is_file()
        if not has_sidecar and not create:
            stats["skipped"] += 1
            continue
        entries = dict(side.samples)
        changed = 0
        for sample in found.samples:
            path = Path(found.folder) / sample["file"]
            try:
                size = path.stat().st_size
            except OSError:
                continue
            existing = entries.get(sample["file"]) or {}
            seconds = _round(sample["duration"] or sample_duration(existing))
            if seconds <= 0 or size <= 0:
                continue
            # 已经记过、且记的是"这个大小"的同一段音频：不必动它。
            if sample_duration(existing) > 0 and sample_bytes(existing) == size:
                continue
            entry = dict(existing)
            entry["duration"] = seconds
            entry["bytes"] = size
            entries[sample["file"]] = entry
            changed += 1
        if not changed:
            continue
        stats["touched"] += 1
        stats["entries"] += changed
        if not has_sidecar:
            stats["new_files"] += 1
        if write:
            write_sidecar(Path(found.folder), dataclasses.replace(side, samples=entries))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把已有样例的时长回填进 voice.json")
    parser.add_argument("--write", action="store_true", help="真的写盘（默认只统计）")
    parser.add_argument(
        "--create",
        action="store_true",
        help="连还没有 voice.json 的音色目录也建一份（默认不动它们）",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="只做这个扫描根（可重复）；不给就是配置里的全部",
    )
    args = parser.parse_args(argv)

    db_path = data_dir() / "hub.db"
    if not db_path.is_file():
        print(f"找不到 {db_path}——先确认 VOICEHUB_DATA_DIR 指对了。", file=sys.stderr)
        return 2
    roots = args.root or list(load_config().folders)
    if not roots:
        print("配置里没有扫描根，无事可做。", file=sys.stderr)
        return 2

    print(f"数据目录：{data_dir()}")
    print(f"扫描根　：{', '.join(roots)}")
    print(f"模式　　：{'写盘' if args.write else '只统计（加 --write 才写）'}"
          f"{'，含新建 voice.json' if args.create else ''}")

    db = HubDB(db_path)
    cache = DurationCache.loaded(db.duration_cache())
    total = {"folders": 0, "touched": 0, "entries": 0, "new_files": 0, "skipped": 0}
    for root in roots:
        if not Path(root).is_dir():
            print(f"跳过（目录不在）：{root}")
            continue
        stats = backfill_root(root, cache, write=args.write, create=args.create)
        for key in total:
            total[key] += stats[key]
        print(
            f"{root}\n  音色 {stats['folders']}，要补 {stats['touched']} 个目录 / "
            f"{stats['entries']} 条；跳过 {stats['skipped']}"
        )

    if args.write:
        # 这一趟量到的时长也进库：下次扫描不必再量。写库失败不算失败——voice.json 里
        # 已经记下了，扫描会从那里捡起来（服务正在跑时偶发锁表就是这种情况）。
        try:
            db.remember_durations(cache.entries())
        except Exception as exc:  # noqa: BLE001 - 一次性脚本，写库只是顺手
            print(f"（时长缓存没写进 hub.db：{exc}；voice.json 已写好，扫描会补上）")
    print(
        f"\n合计：音色 {total['folders']}，需补 {total['touched']} 个目录 / "
        f"{total['entries']} 条（其中新建 {total['new_files']} 份 voice.json），"
        f"跳过 {total['skipped']} 个目录"
    )
    if not args.write:
        print("这次没有写盘；确认无误后加 --write 再跑一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
