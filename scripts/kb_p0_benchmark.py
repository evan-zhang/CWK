#!/usr/bin/env python3
"""Run the RT-054 candidate postings algorithm only against synthetic data.

It never opens a KB, NAS, token file, or gateway.  The command is useful as a
portable P0 correctness baseline; authorized environments collect transport
measurements through ``kb_gateway.py --p0-diagnostics`` separately.
"""
from __future__ import annotations

import json
import time

from kb_lexical import best_spans, bm25_rank, build_index, chunk_body
from kb_p0 import candidate_best_spans, candidate_bm25_rank, compare_exact


def main() -> int:
    docs = (("fixture:a", chunk_body("会议 交付 package alpha\n" * 160)),
            ("fixture:b", chunk_body("体外模拟 会议 beta\n" * 160)))
    index = build_index(docs)
    differences = []
    timings = {"legacy_ns": 0, "candidate_ns": 0}
    for query in ("会议", "package", "体外模拟", "会议 会议", "missing"):
        started = time.perf_counter_ns()
        old_rank = bm25_rank(index, query)
        old_spans = {lineage: best_spans(index, lineage, query) for lineage, _ in old_rank}
        timings["legacy_ns"] += time.perf_counter_ns() - started
        started = time.perf_counter_ns()
        new_rank = candidate_bm25_rank(index, query)
        new_spans = {lineage: candidate_best_spans(index, lineage, query) for lineage, _ in new_rank}
        timings["candidate_ns"] += time.perf_counter_ns() - started
        for label, old, new in (("rank", old_rank, new_rank), ("span", old_spans, new_spans)):
            difference = compare_exact(f"{query}:{label}", old, new)
            if difference:
                differences.append(difference)
    print(json.dumps({"schema": "cwk.kb.p0.offline-benchmark.v1", "ok": not differences,
                      "docs": len(docs), "chunks": index.n_chunks, "timings": timings,
                      "differences": differences}, ensure_ascii=False, sort_keys=True))
    return 0 if not differences else 1


if __name__ == "__main__":
    raise SystemExit(main())
