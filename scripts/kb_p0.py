#!/usr/bin/env python3
"""RT-054 P0-only, zero-write diagnostics and offline BM25 comparison.

This module deliberately has no storage constructor, environment lookup, or
writer.  The gateway may use :class:`P0Trace` only when explicitly enabled;
the candidate postings scorer is for offline tests/benchmarks only and is not
imported by the request scoring path.
"""

from __future__ import annotations

import hashlib
import math
import resource
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from kb_lexical import B, K1, MAX_SPANS_PER_DOC, LexicalIndex, idf, tokenize

P0_SCHEMA = "cwk.kb.p0.diagnostic.v1"


def anonymous_kb(kb: str) -> str:
    """Stable, non-reversible diagnostic label; never expose the KB id."""
    return hashlib.sha256((kb or "default").encode("utf-8")).hexdigest()[-12:]


def query_category(query: str) -> str:
    """A coarse category, intentionally not a query digest or excerpt."""
    ascii_only = bool(query) and query.isascii()
    has_ascii = any(ch.isascii() and ch.isalnum() for ch in query)
    has_non_ascii = any(not ch.isascii() and not ch.isspace() for ch in query)
    family = "ascii" if ascii_only else "mixed" if has_ascii and has_non_ascii else "unicode"
    return f"{family}_len_{min(len(query) // 16, 15) * 16}_{min(len(query) // 16, 15) * 16 + 15}"


def rss_bytes() -> Optional[int]:
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError):  # pragma: no cover - platform guard
        return None
    # macOS reports bytes; Linux reports KiB.
    return int(value if sys.platform == "darwin" else value * 1024)


@dataclass
class _Stage:
    elapsed_ns: int = 0
    count: int = 0
    errors: int = 0
    payload_bytes: int = 0


@dataclass
class P0Trace:
    """Per-request in-memory record.  It has no I/O side effects."""

    kb: str
    query: str = ""
    cold_state: str = "unknown"
    started_ns: int = field(default_factory=time.perf_counter_ns)
    stages: Dict[str, _Stage] = field(default_factory=dict)
    docs: Optional[int] = None
    chunks: Optional[int] = None
    generation: Optional[str] = None
    error_category: Optional[str] = None
    _rss_peak: Optional[int] = field(default_factory=rss_bytes)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        stage = self.stages.setdefault(name, _Stage())
        started = time.perf_counter_ns()
        stage.count += 1
        try:
            yield
        except Exception:  # noqa: BLE001 - record then retain gateway semantics
            stage.errors += 1
            raise
        finally:
            stage.elapsed_ns += time.perf_counter_ns() - started
            current = rss_bytes()
            if current is not None:
                self._rss_peak = max(self._rss_peak or current, current)

    def add_bytes(self, name: str, amount: Optional[int]) -> None:
        if amount is not None:
            self.stages.setdefault(name, _Stage()).payload_bytes += int(amount)

    def fail(self, exc: BaseException) -> None:
        # Exception class/code only; never serialize an exception message.
        code = getattr(exc, "code", "")
        self.error_category = str(code or type(exc).__name__)

    def record(self) -> dict:
        names = (
            "auth", "raw_index_logical_read", "lexical_logical_read",
            "json_decode", "structure_recovery", "metadata", "tokenize_bm25_span",
            "rrf", "json_encode",
        )
        stages = {
            name: {
                "elapsed_ns": self.stages.get(name, _Stage()).elapsed_ns or None,
                "count": self.stages.get(name, _Stage()).count,
                "errors": self.stages.get(name, _Stage()).errors,
                "payload_bytes": self.stages.get(name, _Stage()).payload_bytes or None,
            }
            for name in names
        }
        # StorageBackend intentionally does not expose HTTP events.  Unknown
        # is evidence, not a made-up physical measurement.
        physical = {
            name: {"count": None, "wire_bytes": None, "payload_bytes": None,
                   "reason": "storage_backend_transport_not_observable"}
            for name in ("login", "retry", "download")
        }
        elapsed = time.perf_counter_ns() - self.started_ns
        known = sum(v["elapsed_ns"] or 0 for v in stages.values())
        return {
            "schema": P0_SCHEMA,
            "kb_anonymous": anonymous_kb(self.kb),
            "generation_suffix": self.generation[-12:] if self.generation else None,
            "docs": self.docs,
            "chunks": self.chunks,
            "query_category": query_category(self.query),
            "state": self.cold_state,
            "stages": stages,
            "logical": {
                "raw_index_reads": stages["raw_index_logical_read"]["count"],
                "lexical_reads": stages["lexical_logical_read"]["count"],
                "gateway_writes": 0,
            },
            "physical": physical,
            "end_to_end_ns": elapsed,
            "measured_stage_sum_ns": known,
            "stage_error_ns": abs(elapsed - known),
            "rss_peak_bytes": self._rss_peak,
            "error_category": self.error_category,
        }


class TraceBackend:
    """Read-only proxy that accounts for gateway reads without changing them."""

    def __init__(self, backend: object, trace: P0Trace) -> None:
        self._backend = backend
        self._trace = trace

    def read(self, path: str) -> bytes:
        stage = "raw_index_logical_read" if path.endswith("raw-index.json") else (
            "lexical_logical_read" if path.endswith("lexical-index.json") else "other_read"
        )
        with self._trace.stage(stage):
            data = self._backend.read(path)  # type: ignore[attr-defined]
        self._trace.add_bytes(stage, len(data))
        return data

    def __getattr__(self, name: str):
        return getattr(self._backend, name)


def candidate_bm25_rank(
    index: LexicalIndex, query: str, *, lineages_order: Optional[Sequence[str]] = None
) -> Tuple[Tuple[str, float], ...]:
    """Postings-only reference scorer for P0 benchmark; never a gateway path."""
    query_terms = tuple(sorted(set(tokenize(query))))
    if not query_terms or index.n_chunks == 0:
        return ()
    avgdl = index.avgdl or 1.0
    chunk_scores: Dict[str, float] = {}
    for term in query_terms:
        weight = idf(index.n_chunks, index.df(term))
        for post in index.terms.get(term, ()):
            dl = index.chunk_lengths.get(post.chunk_id, 0)
            if dl <= 0:
                continue
            value = weight * (post.tf * (K1 + 1.0)) / (
                post.tf + K1 * (1.0 - B + B * dl / avgdl)
            )
            chunk_scores[post.chunk_id] = chunk_scores.get(post.chunk_id, 0.0) + value
    best: Dict[str, float] = {}
    for chunk_id, score in chunk_scores.items():
        lineage = index.lineages[chunk_id]
        if score > best.get(lineage, 0.0):
            best[lineage] = score
    order = {lineage: i for i, lineage in enumerate(lineages_order or ())}
    return tuple(sorted(best.items(), key=lambda kv: (-kv[1], order.get(kv[0], 10**9), kv[0])))


def candidate_best_spans(
    index: LexicalIndex, lineage: str, query: str, *, max_spans: int = MAX_SPANS_PER_DOC
) -> Tuple[Tuple[str, int, int, float], ...]:
    """Candidate counterpart of ``best_spans`` with the same tie contract."""
    query_terms = tuple(sorted(set(tokenize(query))))
    avgdl = index.avgdl or 1.0
    scores: Dict[str, float] = {}
    for term in query_terms:
        weight = idf(index.n_chunks, index.df(term))
        for post in index.terms.get(term, ()):
            if index.lineages.get(post.chunk_id) != lineage:
                continue
            dl = index.chunk_lengths.get(post.chunk_id, 0)
            if dl > 0:
                scores[post.chunk_id] = scores.get(post.chunk_id, 0.0) + weight * (
                    post.tf * (K1 + 1.0)
                ) / (post.tf + K1 * (1.0 - B + B * dl / avgdl))
    candidates = sorted(((score, cid) for cid, score in scores.items() if score > 0.0),
                        key=lambda pair: (-pair[0], pair[1]))
    accepted: List[Tuple[str, int, int, float]] = []
    for score, cid in candidates:
        if len(accepted) >= max_spans:
            break
        start, end = index.chunk_spans[cid]
        if not any(not (end <= old_start or old_end <= start)
                   for _, old_start, old_end, _ in accepted):
            accepted.append((cid, start, end, score))
    return tuple(accepted)


def compare_exact(label: str, old: object, candidate: object) -> Optional[dict]:
    """Small deliberate fail-closed comparison primitive for benchmark tests."""
    if old == candidate:
        return None
    return {"field": label, "old": old, "candidate": candidate}
