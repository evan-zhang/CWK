"""Public query result value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalHit:
    doc_id: str
    score: float
    channel: str
    parent_id: str = ""
    chunk_id: str = ""
