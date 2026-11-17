"""量一次「角色 → 音色」的召回精度：用项目里已有的分配当弱标签，比较几种排序。

**为什么要有它**：现在提示词里塞的是全局前 200 条候选，`_recall` 只改这 200 条是谁。
「每条角色其实只需要看 20 条」是对的判断，但要把候选从 200 收到 30，得先知道
向量到底把正确答案排到第几名、加性别硬筛能提升多少。没有这个数就只能凭感觉调。

它只读库、只写向量缓存，不调模型、不改项目数据。标签来自
``projects.db`` 的 ``voices`` 表（用户/AI 定下的分配），所以**有循环偏差**：
这些音色可能是当初 AI 用同一套向量挑出来的，向量策略会因此占便宜。看相对排名，
别看绝对分数。

跑法（数据目录必须是真实那一份）：

```
VOICEHUB_DATA_DIR=F:/ai/audiobook-studio/data \\
  .venv/Scripts/python.exe scripts/eval_voice_match.py
.venv/Scripts/python.exe scripts/eval_voice_match.py --examples 20
```

输出：

- **库现状**：规模与 gender/age/description 的填充率——它决定硬筛能收多紧。
- **策略对比**：每个策略把「用户选的那条」排到 top-1/5/10/20/50/200 的比例与 MRR。
- **样例**：逐角色列出前 5 条与用户的选择，人工看像不像。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 好让下面那几行 import 生效

_ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("VOICEHUB_DATA_DIR"):
    # 不带这个变量，data_dir() 会落到 %LOCALAPPDATA%\\voicehub（空的），
    # 结果是"默认配置 + 没有模型"。脚本默认指向仓库里的那一份。
    os.environ["VOICEHUB_DATA_DIR"] = str(_ROOT / "data")

from app.embedding import service as embedding_uc
from app.infra.config import data_dir
from app.producers.builtin.audiobook.assist import voice_recall

#: 看这几档：200 是现网提示词上限，1~20 是"收回几十条"之后要的精度。
_STAGES = (1, 5, 10, 20, 50, 200)


@dataclass(frozen=True, slots=True)
class _Voice:
    """``voice_recall.voice_text`` 用得到的字段，从 hub.db 直接摊平。"""

    id: str
    shown_name: str
    gender: str
    age: str
    language: str
    tags: tuple[str, ...]
    description: str
    category: str
    sample_count: int
    favorite: bool


def _tags(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ()
    return (
        tuple(str(item) for item in value if str(item).strip()) if isinstance(value, list) else ()
    )


def _pool() -> list[_Voice]:
    conn = sqlite3.connect(data_dir() / "hub.db")
    conn.row_factory = sqlite3.Row
    try:
        samples = {
            row["voice_id"]: row["n"]
            for row in conn.execute("SELECT voice_id, COUNT(*) AS n FROM samples GROUP BY voice_id")
        }
        rows = conn.execute(
            "SELECT id, name, display, category, gender, age, language, tags,"
            " description, favorite FROM voices WHERE hidden = 0"
        ).fetchall()
    finally:
        conn.close()
    pool = [
        _Voice(
            id=row["id"],
            shown_name=str(row["display"] or row["name"] or ""),
            gender=str(row["gender"] or ""),
            age=str(row["age"] or ""),
            language=str(row["language"] or ""),
            tags=_tags(str(row["tags"] or "")),
            description=str(row["description"] or ""),
            category=str(row["category"] or ""),
            sample_count=int(samples.get(row["id"], 0)),
            favorite=bool(row["favorite"]),
        )
        for row in rows
    ]
    return [voice for voice in pool if voice.shown_name]


def _labels(pool_ids: set[str]) -> list[tuple[str, str, list[str], str]]:
    """历史分配 → (角色, 目标音色 id, 对白, 书名)。只留音色还在库里的。"""
    conn = sqlite3.connect(data_dir() / "producers" / "audiobook" / "projects.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT v.project_id, v.speaker, v.voice_id, p.name AS book FROM voices v"
            " JOIN projects p ON p.id = v.project_id"
        ).fetchall()
        out: list[tuple[str, str, list[str], str]] = []
        for row in rows:
            if row["voice_id"] not in pool_ids:
                continue
            lines = [
                str(item["text"])
                for item in conn.execute(
                    "SELECT r.text FROM rows r JOIN chapters c ON c.id = r.chapter_id"
                    " WHERE c.project_id = ? AND r.speaker = ? AND r.text <> ''"
                    " ORDER BY c.ordinal, r.ordinal LIMIT 2",
                    (row["project_id"], row["speaker"]),
                )
            ]
            if lines:
                out.append(
                    (str(row["speaker"]), str(row["voice_id"]), lines, str(row["book"] or ""))
                )
    finally:
        conn.close()
    return out


def _baseline_order(pool: Sequence[_Voice]) -> list[int]:
    """``_candidate_voices`` 的顺序：有样例、收藏优先，再按名字。"""
    return sorted(
        range(len(pool)),
        key=lambda index: (
            pool[index].sample_count == 0,
            not pool[index].favorite,
            pool[index].shown_name,
        ),
    )


def _ranks(order: Sequence[int], target: int) -> int:
    return order.index(target) + 1


def _self_check(
    pool: Sequence[_Voice], doc_vectors: object, provider: object, sample: int = 400
) -> None:
    """自检索：拿音色自己的描述 + 标签当查询，看它能不能把自己排回来。

    这一项把「角色文本写得好不好」这个问题摘出去，只剩「文本向量能不能区分
    音色」——如果连自己都找不回来，换更大的模型也没用；如果找得回来，瓶颈就
    在角色那侧的描述上。
    """
    import numpy as np

    picked = [index for index, voice in enumerate(pool) if voice.description.strip()][:sample]
    if not picked:
        print("\n自检索：没有带描述的音色，跳过。")
        return
    queries = [
        "，".join(part for part in (pool[index].description, " ".join(pool[index].tags)) if part)
        for index in picked
    ]
    vectors = voice_recall._vectors(provider, queries, query=True)
    scores = doc_vectors @ vectors.T
    hits = {stage: 0 for stage in (1, 5, 10, 50)}
    for column, target in enumerate(picked):
        order = np.argsort(-np.asarray(scores[:, column]))
        rank = int(np.flatnonzero(order == target)[0]) + 1
        for stage in hits:
            if rank <= stage:
                hits[stage] += 1
    count = len(picked)
    cells = "，".join(f"top{stage} {hits[stage] / count:.0%}" for stage in hits)
    print(f"\n自检索（{count} 条带描述的音色，用描述+标签找回自己）：{cells}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比较角色→音色的几种召回策略")
    parser.add_argument("--examples", type=int, default=12, help="列多少个角色的前 5 条")
    args = parser.parse_args(argv)

    import numpy as np

    provider = embedding_uc.ready_provider()
    if provider is None:
        print("没有已下载的向量模型：先在设置 → 语义检索下载。")
        return 2

    pool = _pool()
    index_of = {voice.id: index for index, voice in enumerate(pool)}
    labels = _labels(set(index_of))
    if not labels:
        print("没有可用的历史分配（projects.db 的 voices 表为空或音色已不在库里）。")
        return 2

    total = len(pool)
    print(f"库：{total} 条；历史分配可用 {len(labels)} 条；模型 {provider.label}")
    filled = {
        "gender": sum(1 for voice in pool if voice.gender),
        "age": sum(1 for voice in pool if voice.age),
        "description": sum(1 for voice in pool if voice.description),
        "language": sum(1 for voice in pool if voice.language),
        "tags": sum(1 for voice in pool if voice.tags),
    }
    print(
        "填充率："
        + "，".join(f"{key} {value / total:.0%}" for key, value in filled.items())
        + f"；有样例 {sum(1 for voice in pool if voice.sample_count)} 条"
    )

    print("向量化全库……（首次会写 vectors.db，之后只读缓存）")
    documents = [voice_recall.voice_text(voice) for voice in pool]
    doc_vectors = voice_recall._vectors(provider, documents, query=False)

    queries = [
        voice_recall.role_text({"speaker": speaker, "lines": lines}, ())
        for speaker, _, lines, _ in labels
    ]
    query_vectors = voice_recall._vectors(provider, queries, query=True)
    scores = doc_vectors @ query_vectors.T  # 已 L2 归一化，点积即余弦

    # 只用角色名当查询：用来判断「对白到底是帮忙还是稀释」。
    name_vectors = voice_recall._vectors(
        provider,
        [
            voice_recall.role_text({"speaker": speaker, "lines": []}, ())
            for speaker, _, _, _ in labels
        ],
        query=True,
    )
    name_scores = doc_vectors @ name_vectors.T

    baseline = _baseline_order(pool)
    strategies = (
        "名字基线",
        "纯向量(名+对白)",
        "纯向量(只名字)",
        "向量+标签",
        "性别硬筛(上界)",
        "性别+向量(上界)",
    )
    hits = {name: {stage: 0 for stage in _STAGES} for name in strategies}
    mrr = dict.fromkeys(strategies, 0.0)
    details: list[tuple[str, str, list[str], int, np.ndarray, list[int]]] = []
    filtered_pool: list[int] = []

    for column, (speaker, target_id, lines, book) in enumerate(labels):
        target = index_of[target_id]
        column_scores = np.asarray(scores[:, column], dtype=np.float32)
        # 标签命中：音色的标签词出现在角色名 + 对白里，直接加一点分。
        query = queries[column]
        bonus = np.array(
            [sum(1 for tag in voice.tags if tag and tag in query) * 0.06 for voice in pool],
            dtype=np.float32,
        )
        # 目标音色自己没有性别标记时这条上界不成立：退回全库，不参与收窄统计。
        same_gender = [
            index
            for index, voice in enumerate(pool)
            if pool[target].gender and voice.gender == pool[target].gender
        ]
        if same_gender:
            filtered_pool.append(len(same_gender))
        else:
            same_gender = list(range(len(pool)))

        orders = {
            "名字基线": baseline,
            "纯向量(名+对白)": np.argsort(-column_scores).tolist(),
            "纯向量(只名字)": np.argsort(-np.asarray(name_scores[:, column])).tolist(),
            "向量+标签": np.argsort(-(column_scores + bonus)).tolist(),
            "性别硬筛(上界)": same_gender,
            "性别+向量(上界)": sorted(same_gender, key=lambda index: -float(column_scores[index])),
        }
        for name, order in orders.items():
            rank = _ranks(order, target)
            details_rank = rank
            for stage in _STAGES:
                if details_rank <= stage:
                    hits[name][stage] += 1
            mrr[name] += 1.0 / details_rank
        details.append(
            (speaker, book, lines, column, column_scores, np.argsort(-column_scores).tolist())
        )

    count = len(labels)
    header = f"{'策略':<16}" + "".join(f"top{stage:<5}" for stage in _STAGES) + "  MRR"
    print()
    print(header)
    print("-" * len(header))
    for name in strategies:
        cells = "".join(f"{hits[name][stage] / count:<8.0%}" for stage in _STAGES)
        print(f"{name:<16}{cells}  {mrr[name] / count:.3f}")
    print(
        f"\n性别硬筛后平均可选 {sum(filtered_pool) / max(1, len(filtered_pool)):.0f} 条"
        f"（未过滤 {total} 条，{len(filtered_pool)}/{count} 条标签自身有性别）；"
        "这是把「AI 判性别」判对之后的上界。"
    )

    _self_check(pool, doc_vectors, provider)

    print(f"\n样例（{min(args.examples, count)} 个角色的纯向量前 5）：")
    for speaker, book, lines, column, column_scores, order in details[: max(0, args.examples)]:
        target_id = labels[column][1]
        target_index = index_of[target_id]
        rank = order.index(target_index) + 1
        print(f"\n  {speaker}（{book}）→ 用户：{pool[target_index].shown_name}（第 {rank} 名）")
        print(f"    对白：{(lines[0] if lines else '')[:40]}")
        for place, index in enumerate(order[:5], 1):
            voice = pool[index]
            mark = "  ←" if index == target_index else ""
            tags = " ".join(voice.tags[:3])
            print(
                f"    {place}. {voice.shown_name} [{voice.gender or '?'}/{voice.age or '?'}"
                f" {tags}] {float(column_scores[index]):.3f}{mark}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
