"""Hybrid-retrieval score fusion (pure stdlib). Combines ranked lists from lexical, vector and graph signals."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Hashable, Sequence

DEFAULT_WEIGHTS = {"lexical": 1.0, "vector": 1.0, "graph": 0.5}


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("vectors must be non-empty and of equal dimension")
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return 0.0 if na == 0 or nb == 0 else sum(x * y for x, y in zip(a, b)) / (na * nb)


def reciprocal_rank_fusion(rankings: dict[str, Sequence[Hashable]], *, k: int = 60,
                           weights: dict[str, float] | None = None) -> list[tuple[Hashable, float, dict[str, int]]]:
    """Return (item, fused_score, {signal: 1-based rank}) sorted best-first; ties broken by item order of appearance."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    scores: dict[Hashable, float] = defaultdict(float)
    ranks: dict[Hashable, dict[str, int]] = defaultdict(dict)
    order: dict[Hashable, int] = {}
    for signal, items in rankings.items():
        for pos, item in enumerate(items, start=1):
            if item in ranks[item] and signal in ranks[item]:
                continue  # duplicate within one signal: keep the best rank only
            scores[item] += w.get(signal, 1.0) / (k + pos)
            ranks[item][signal] = pos
            order.setdefault(item, len(order))
    ranked = sorted(scores, key=lambda i: (-scores[i], order[i]))
    return [(i, round(scores[i], 8), ranks[i]) for i in ranked]
