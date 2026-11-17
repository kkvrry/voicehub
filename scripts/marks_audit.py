"""写引擎时的一次性核对：这台引擎声明的标签，有几条对得上项目的大库。

**为什么要有这一趟**：语气词的「通用」是跨引擎按**规范名**求交集的（``domain.marks``）。
插件照平台原文写法登记（``gasp`` / ``喘息``）本来就能对上，前提是那个写法收在
:data:`~app.domain.marks.MARKS` 的 ``aliases`` 里。收不到的名字不会报错——它静默地
算成"这一家的自有概念"。所以要有人把这件事显式摆出来：**加一台引擎、或改一份声明
之后跑一次**，看有没有该归不归的。

它只读不写，也不连模型：列出的候选由人（或一次人工触发的模型比对）判断，
结论落进 ``MARKS`` 的 ``aliases``。运行时不调它——归一是查表，不是推理。

跑法：

```
.venv/Scripts/python.exe scripts/marks_audit.py
.venv/Scripts/python.exe scripts/marks_audit.py --min-ratio 0.6   # 放宽候选
```

输出三段：

- **未收录**：大库与别名都没有的名字。要么是这一家独有的概念（认下），要么是漏收的
  近义写法（补别名）。
- **疑似同义**：与某条规范名相近但还没收的名字，附相似度，供逐条判断。
- **无人登记**：大库里一条引擎都没用到的标签——提示大库可以收，也可以删。
"""

from __future__ import annotations

import argparse
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 好让下面那几行 import 生效

from app.domain.marks import canonical_mark, mark_names, normalize_marks
from app.usecase.engines import list_engines


def _similar(name: str, candidates: tuple[str, ...], min_ratio: float) -> list[tuple[str, float]]:
    """与规范名的相似度：包含关系算满分，其余按编辑序列。只列达到阈值的。"""
    hits: list[tuple[str, float]] = []
    for candidate in candidates:
        if name == candidate:
            continue
        ratio = 1.0 if (name in candidate or candidate in name) else SequenceMatcher(
            None, name, candidate
        ).ratio()
        if ratio >= min_ratio:
            hits.append((candidate, ratio))
    return sorted(hits, key=lambda item: -item[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对各引擎声明的语气词标签与大库的对齐情况")
    parser.add_argument("--min-ratio", type=float, default=0.75, help="疑似同义的相似度下限")
    args = parser.parse_args(argv)

    canonical = mark_names()
    engines = list_engines()
    unknown: list[tuple[str, str]] = []
    suspects: list[tuple[str, str, tuple[str, float]]] = []
    covered: set[str] = set()

    print(f"{'引擎':<14}{'声明':>4}  {'认任意':<6}")
    for key, engine in engines.items():
        declared = engine.mark_map()
        covered |= set(normalize_marks(declared))
        loose = [
            name
            for name in declared
            if canonical_mark(name) == name.strip() and name not in canonical
        ]
        any_tag = "是" if engine.mark_passthrough() else "—"
        print(f"{key:<14}{len(declared):>4}  {any_tag:<6}")
        for name in loose:
            unknown.append((key, name))
            candidates = _similar(name, canonical, args.min_ratio)
            if candidates:
                suspects.append((key, name, candidates))

    print("\n未收录（大库与别名都没有）")
    if not unknown:
        print("  —")
    for key, name in unknown:
        print(f"  {key:<14}{name}")

    print("\n疑似同义（判断后补进 aliases，或认作这一家的自有概念）")
    if not suspects:
        print("  —")
    for key, name, candidates in suspects:
        best = "、".join(f"{c}({r:.2f})" for c, r in candidates[:3])
        print(f"  {key:<14}{name:<8}{best}")

    print("\n无人登记（大库里有、没有一台引擎声明）")
    idle = [name for name in canonical if name not in covered]
    print("  " + ("、".join(idle) if idle else "—"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
