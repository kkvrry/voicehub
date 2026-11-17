"""按角色画像从全库语义召回候选音色。

音色库超过提示词上限时，靠名字排序取前 N 条会漏掉真正合适的音色；这里把
音色元信息与角色画像各自向量化，按余弦相似度为每个角色保底召回一组候选。
查询文本是**画像**（性别、年龄、音色词、语气、语速），不是对白——对白是叙事
文本，和音色标签不是一种语言，塞进去只会稀释。向量按文本内容缓存，并落到模型
目录的 SQLite：音色信息不变，重启后也不会重算。

只在本地向量模型已下载时启用；模型没装、推理失败都由调用方决定怎么退。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from typing import Any

from app.embedding.base import EmbeddingError, EmbeddingProvider
from app.embedding.cache import cache_for

__all__ = ["recall", "voice_text"]

_BATCH = 64
_CACHE_LIMIT = 20000
_VECTORS: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def voice_text(voice: Any) -> str:
    """一条音色拿去向量化的文本：用户能看到的全部可读信息。"""
    tags = getattr(voice, "tags", ()) or ()
    parts = [
        str(getattr(voice, "shown_name", "") or ""),
        str(getattr(voice, "gender", "") or ""),
        str(getattr(voice, "age", "") or ""),
        str(getattr(voice, "language", "") or ""),
        " ".join(str(tag) for tag in tags),
        str(getattr(voice, "description", "") or ""),
        str(getattr(voice, "category", "") or ""),
    ]
    return "，".join(part.strip() for part in parts if part and part.strip())


def role_text(name: str, profile: str) -> str:
    """一个角色的查询文本：角色名 + 画像。

    名字要留：库里确实有按角色命名的音色，这类匹配靠字符串就能中，不要为
    「纯粹语义」把它丢掉。画像为空（模型不认识这个角色）时只剩名字，行为与
    没有向量模型时一致。
    """
    speaker = str(name or "").strip()
    parts = [f"角色：{speaker}"] if speaker else []
    if profile:
        parts.append(profile)
    return "；".join(parts)


def _vector_key(provider: EmbeddingProvider, text: str, query: bool) -> str:
    raw = f"{provider.key}:{provider.version}:{int(query)}:{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _vectors(
    provider: EmbeddingProvider, texts: Sequence[str], *, query: bool
) -> Any:
    """对齐返回每段文本的向量：进程缓存 → 磁盘缓存 → 分批推理。"""
    import numpy as np

    keys = [_vector_key(provider, text, query) for text in texts]
    missing: dict[str, int] = {}
    with _CACHE_LOCK:
        for index, key in enumerate(keys):
            if key not in _VECTORS:
                missing.setdefault(key, index)
    if missing:
        dimension = int(getattr(provider, "dimension", 0) or 0)
        disk = None
        if dimension > 0 and callable(getattr(provider, "model_dir", None)):
            disk = cache_for(provider)
        if disk is not None:
            stored = disk.get(list(missing), dimension)
            if stored:
                with _CACHE_LOCK:
                    _VECTORS.update(stored)
        with _CACHE_LOCK:
            pending = {key: index for key, index in missing.items() if key not in _VECTORS}
        if pending:
            made: dict[str, Any] = {}
            order = list(pending.values())
            for start in range(0, len(order), _BATCH):
                batch = [texts[index] for index in order[start : start + _BATCH]]
                with _INFER_LOCK:
                    vectors = provider.embed(batch, query=query)
                if len(vectors) != len(batch):
                    raise EmbeddingError("向量模型返回的条数与输入不一致。")
                for text, vector in zip(batch, vectors, strict=True):
                    made[_vector_key(provider, text, query)] = np.asarray(
                        vector, dtype=np.float32
                    )
            if disk is not None:
                disk.put(made, dimension)
            with _CACHE_LOCK:
                if len(_VECTORS) + len(made) > _CACHE_LIMIT:
                    _VECTORS.clear()
                _VECTORS.update(made)
    with _CACHE_LOCK:
        return np.stack([_VECTORS[key] for key in keys])


def recall(
    provider: EmbeddingProvider,
    pool: Sequence[tuple[Any, tuple[str, ...]]],
    queries: Sequence[str],
    *,
    masks: Sequence[Sequence[bool] | None] | None = None,
    limit: int,
) -> list[tuple[Any, tuple[str, ...]]]:
    """给每个角色保底召回一组最接近的音色，再按全局相似度补满。

    ``pool`` 是 (音色, 可用引擎) 的完整候选池；``queries`` 是每个角色的查询
    文本（见 :func:`role_text`），顺序与角色一致。``masks`` 与 ``queries`` 对齐，
    ``False`` 的位置表示该角色不能选这条（引擎不匹配）——保底名额只从能选的里取，
    免得一个指定了本地引擎的角色拿到云端音色。

    返回同样形状，顺序即提示词顺序。角色多时平均分配名额，避免前面的角色
    把上限吃光。
    """
    import numpy as np

    if not pool or not queries:
        return []
    documents = [voice_text(voice) for voice, _ in pool]
    doc_vectors = _vectors(provider, documents, query=False)
    query_vectors = _vectors(provider, [q or " " for q in queries], query=True)
    scores = doc_vectors @ query_vectors.T

    role_count = max(1, scores.shape[1])
    per_role = max(1, min(24, limit // role_count))
    best = scores.max(axis=1) if scores.shape[1] else np.zeros(len(documents))

    chosen: list[int] = []
    seen: set[int] = set()
    for column in range(scores.shape[1]):
        ranking = scores[:, column]
        mask = masks[column] if masks is not None and column < len(masks) else None
        if mask is not None:
            allowed = np.asarray(mask, dtype=bool)
            if allowed.shape == ranking.shape and allowed.any():
                ranking = np.where(allowed, ranking, -np.inf)
        for index in np.argsort(-ranking)[:per_role]:
            pick = int(index)
            if pick in seen:
                continue
            seen.add(pick)
            chosen.append(pick)
            if len(chosen) >= limit:
                break
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:
        for index in np.argsort(-best):
            pick = int(index)
            if pick in seen:
                continue
            seen.add(pick)
            chosen.append(pick)
            if len(chosen) >= limit:
                break
    # 每个角色保底的那几条都已在集合里；顺序按「对任一角色的最高相似度」排，
    # 免得第一个角色的候选把整张表压在前面。
    chosen.sort(key=lambda index: float(best[index]), reverse=True)
    return [pool[index] for index in chosen]
