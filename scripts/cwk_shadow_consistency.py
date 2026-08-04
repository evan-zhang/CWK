#!/usr/bin/env python3
"""Compare deterministic live-scan and persistent-index Top-K retrieval."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_wiki_query import DEFAULT_MIRROR, query_mirror


DEFAULT_QUERIES = [
    "Token 消耗异常",
    "下半年大模型预算",
    "AI 财务单据审核",
    "屈军利 费用",
    "云端虾 使用情况",
    "小龙虾运营数据日报",
    "OpenClaw Token 过亿",
    "MiniMax GLM 成本",
    "工作汇报图片解析费用",
    "notex 费用异常",
    "模型账号采购",
    "云端虾 GPT 资源申请",
    "缓存读取 Token 异常",
    "企业 AI 使用周报",
    "康哲 AI Token",
    "德镁医药 小龙虾",
    "玄关健康 AI 使用",
    "New API 额度",
    "大模型 1 亿 Token 成本",
    "模型费用归属",
]


def ids(payload: dict[str, Any], top_k: int) -> list[str]:
    return [str(row.get("report_id") or "") for row in (payload.get("results") or [])[:top_k] if row.get("report_id")]


def compare(mirror: Path, queries: list[str], *, top_k: int, min_overlap: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    overlap_points = 0
    overlap_possible = 0
    exact_count = 0
    for query in queries:
        live_ids = ids(query_mirror(mirror, query, top_k=top_k, use_index=False), top_k)
        index_ids = ids(query_mirror(mirror, query, top_k=top_k, use_index=True), top_k)
        denominator = max(len(live_ids), len(index_ids), 1)
        overlap = len(set(live_ids) & set(index_ids))
        rate = overlap / denominator
        overlap_points += overlap
        overlap_possible += denominator
        exact = live_ids == index_ids
        exact_count += int(exact)
        rows.append(
            {
                "query": query,
                "live_scan_ids": live_ids,
                "persistent_index_ids": index_ids,
                "overlap_count": overlap,
                "denominator": denominator,
                "overlap_rate": rate,
                "exact_ranking": exact,
            }
        )
    aggregate = overlap_points / max(1, overlap_possible)
    return {
        "schema_version": "cwk.shadow_consistency.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query_count": len(queries),
        "top_k": top_k,
        "minimum_overlap_rate": min_overlap,
        "aggregate_overlap_rate": aggregate,
        "exact_ranking_rate": exact_count / max(1, len(queries)),
        "queries": rows,
        "overall_pass": len(queries) >= 20 and aggregate >= min_overlap,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify live-scan vs persistent-index Top-K consistency.")
    parser.add_argument("--mirror-root", default=os.environ.get("CWK_MIRROR_ROOT", str(DEFAULT_MIRROR)))
    parser.add_argument("--queries", default="", help="Optional JSON file containing a list of at least 20 queries.")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--min-overlap", type=float, default=0.99)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    queries = DEFAULT_QUERIES
    if args.queries:
        queries = [str(value) for value in json.loads(Path(args.queries).read_text(encoding="utf-8"))]
    result = compare(
        Path(args.mirror_root).expanduser().resolve(),
        queries,
        top_k=max(1, args.top_k),
        min_overlap=args.min_overlap,
    )
    target = Path(args.output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("query_count", "top_k", "aggregate_overlap_rate", "exact_ranking_rate", "overall_pass")}, ensure_ascii=False))
    return 0 if result["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
