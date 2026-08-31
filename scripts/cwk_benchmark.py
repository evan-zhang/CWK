#!/usr/bin/env python3
"""Reproducible latency/error benchmark for CWK local and cloud query paths."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import platform
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cwk_wiki_query import DEFAULT_MIRROR, query_cloud, query_mirror


DEFAULT_QUERIES = [
    "Token 消耗异常",
    "下半年大模型预算",
    "AI 财务单据审核",
    "屈军利 费用",
    "云端虾 使用情况",
]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    rows = sorted(values)
    index = min(len(rows) - 1, max(0, int(round((len(rows) - 1) * fraction))))
    return rows[index]


def execute(label: str, jobs: list[Callable[[], dict[str, Any]]], concurrency: int) -> dict[str, Any]:
    durations: list[float] = []
    errors: list[str] = []
    confidences: list[str] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(job): index for index, job in enumerate(jobs)}
        for future in as_completed(futures):
            one_started = time.monotonic()
            try:
                payload = future.result()
                duration = float(payload.pop("_benchmark_duration"))
                durations.append(duration)
                confidences.append(str(payload.get("confidence") or ""))
            except Exception as exc:
                errors.append(str(exc))
                durations.append(time.monotonic() - one_started)
    return {
        "label": label,
        "request_count": len(jobs),
        "concurrency": concurrency,
        "wall_seconds": round(time.monotonic() - started, 4),
        "success_count": len(jobs) - len(errors),
        "error_count": len(errors),
        "error_rate": len(errors) / max(1, len(jobs)),
        "latency_seconds": {
            "mean": round(statistics.mean(durations), 4) if durations else 0.0,
            "p50": round(percentile(durations, 0.50), 4),
            "p95": round(percentile(durations, 0.95), 4),
            "max": round(max(durations), 4) if durations else 0.0,
        },
        "confidences": confidences,
        "errors": errors[:10],
    }


def timed(fn: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    def call() -> dict[str, Any]:
        started = time.monotonic()
        payload = fn()
        payload["_benchmark_duration"] = time.monotonic() - started
        return payload
    return call


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark CWK local scan/index and optional cloud query modes.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--include-cloud", action="store_true")
    parser.add_argument("--min-index-version", type=int, default=0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    queries = DEFAULT_QUERIES * args.iterations
    local_scan = [timed(lambda q=q: query_mirror(mirror, q, top_k=5, use_index=False)) for q in queries]
    local_index = [timed(lambda q=q: query_mirror(mirror, q, top_k=5, use_index=True)) for q in queries]
    results = [
        execute("local_live_scan", local_scan, args.concurrency),
        execute("local_persistent_index", local_index, args.concurrency),
    ]
    if args.include_cloud:
        cloud = [
            timed(
                lambda q=q: query_cloud(
                    q, top_k=5, max_evidence=2, from_date="", to_date="", writer="", kind="all",
                    min_score=0.1, sender_id=os.environ.get("CWK_SENDER_ID", ""),
                    account_id=os.environ.get("CWK_ACCOUNT_ID", "default"),
                    project_id=os.environ.get("CWK_DOCDB_PROJECT_ID", ""),
                    root_file_id=os.environ.get("CWK_DOCDB_ROOT_FILE_ID", ""),
                    cache_root=os.environ.get("CWK_CLOUD_CACHE_ROOT", ""),
                    min_index_version=args.min_index_version,
                )
            )
            for q in queries
        ]
        results.append(execute("cloud_first", cloud, args.concurrency))
    payload = {
        "schema_version": "cwk.benchmark.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "queries": DEFAULT_QUERIES,
        "iterations": args.iterations,
        "results": results,
    }
    target = Path(args.output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({row["label"]: row["latency_seconds"] for row in results}, ensure_ascii=False))
    return 0 if all(row["error_count"] == 0 for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
