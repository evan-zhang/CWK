"""Bounded merge and document collapse for channel results."""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, Sequence

from .types import RetrievalHit


def _better(candidate: RetrievalHit, current: RetrievalHit) -> bool:
    return (candidate.score, candidate.channel == "exact", candidate.chunk_id or "") > (
        current.score, current.channel == "exact", current.chunk_id or ""
    )


def collapse_hits(hits: Iterable[RetrievalHit], *, top_k: int) -> list[RetrievalHit]:
    """Collapse chunks by source document with deterministic tie-breaking."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    selected: "OrderedDict[str, RetrievalHit]" = OrderedDict()
    for hit in hits:
        current = selected.get(hit.doc_id)
        if current is None or _better(hit, current):
            selected[hit.doc_id] = hit
    return sorted(selected.values(), key=lambda hit: (-hit.score, hit.doc_id, hit.chunk_id or ""))[:top_k]


def merge_channel_hits(
    exact_hits: Sequence[RetrievalHit], lexical_hits: Sequence[RetrievalHit], *, top_k: int = 10
) -> list[RetrievalHit]:
    """Merge exact and lexical lanes, keeping the strongest chunk per document."""
    merged: dict[str, RetrievalHit] = {}
    channels: dict[str, set[str]] = {}
    for hit in (*exact_hits, *lexical_hits):
        channels.setdefault(hit.doc_id, set()).add(hit.channel)
        previous = merged.get(hit.doc_id)
        if previous is None or _better(hit, previous):
            merged[hit.doc_id] = hit
    result: list[RetrievalHit] = []
    for doc_id, hit in merged.items():
        channel_set = channels[doc_id]
        channel = "hybrid" if len(channel_set) > 1 else next(iter(channel_set))
        result.append(RetrievalHit(doc_id, hit.score, channel, hit.parent_id, hit.chunk_id))
    return collapse_hits(result, top_k=top_k)
