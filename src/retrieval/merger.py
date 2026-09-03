"""Reciprocal Rank Fusion across multiple per-source hit lists.

The OpenSearch source already does its own KNN+BM25 RRF inside one index.
This module is the *cross-source* merge used when ``sources=["*"]`` (or any
list of >1 sources) is requested.

Two contributions per hit:

* The hit's per-source rank (descending position within its source's list).
* A small bonus for the hit's normalised ``score`` so two hits at the same
  rank from different sources still order deterministically.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from retrieval.models import SearchResult


def rrf_merge(
    per_source_hits: dict[str, list[SearchResult]],
    *,
    rrf_k: int = 60,
    top_k: int | None = None,
    score_weight: float = 0.001,
) -> list[SearchResult]:
    """Merge ranked hit lists by source key into one ranked list.

    Hits are de-duplicated by ``(source, chunk_id or record_id)`` so the same
    item never appears twice when a logical source returns it from both its
    Airtable and OpenSearch adapters.
    """

    if not per_source_hits:
        return []

    fused: dict[tuple[str, str], dict[str, object]] = {}
    for source, hits in per_source_hits.items():
        for rank, hit in enumerate(hits):
            key = (source, hit.chunk_id or hit.record_id or f"_idx:{rank}")
            contribution = 1.0 / (rrf_k + rank + 1) + score_weight * float(hit.score or 0.0)
            entry = fused.get(key)
            if entry is None:
                fused[key] = {"hit": hit, "score": contribution, "best_rank": rank}
            else:
                entry["score"] = float(entry["score"]) + contribution  # type: ignore[arg-type]
                if rank < int(entry["best_rank"]):  # type: ignore[arg-type]
                    entry["best_rank"] = rank
                    entry["hit"] = hit  # prefer the better-ranked instance

    ordered = sorted(fused.values(), key=lambda e: float(e["score"]), reverse=True)  # type: ignore[arg-type]
    out: list[SearchResult] = []
    for rank, entry in enumerate(ordered):
        hit: SearchResult = entry["hit"]  # type: ignore[assignment]
        hit.score = float(entry["score"])  # type: ignore[arg-type]
        hit.payload = dict(hit.payload) if hit.payload else {}
        hit.payload["fused_rank"] = rank
        hit.payload["fused_score"] = hit.score
        out.append(hit)
        if top_k is not None and len(out) >= top_k:
            break
    return out


def group_by_source(hits: Iterable[SearchResult]) -> dict[str, list[SearchResult]]:
    """Stable grouping helper used by the router."""

    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for h in hits:
        grouped[h.source].append(h)
    return dict(grouped)
