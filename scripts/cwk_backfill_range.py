#!/usr/bin/env python3
"""Fully page a CWork date range and promote every missing report to raw."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_collect_live import QUERY, run_tool, write_markdown
from cwk_raw_store import promote, raw_index


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"


def rows_from_result(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or result)[:1000])
    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("search-list returned non-object data")
    rows = data.get("list") or data.get("rows") or data.get("items") or []
    return (rows if isinstance(rows, list) else []), data.get("total")


def source_rows(app_key: str, start_date: str, end_date: str, page_size: int = 100) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    expected: int | None = None
    for page in range(1, 1001):
        result = run_tool(
            QUERY,
            [
                "--mode", "search-list", "--page-size", str(page_size), "--page-index", str(page),
                "--start-date", start_date, "--end-date", end_date, "--no-share-link",
            ],
            app_key,
        )
        batch, total = rows_from_result(result)
        expected = int(total) if total is not None else expected
        rows.extend(batch)
        if len(batch) < page_size:
            break
    unique = {str(row.get("id") or row.get("reportId") or row.get("reportRecordId") or ""): row for row in rows}
    unique.pop("", None)
    if expected is not None and len(unique) != expected:
        raise RuntimeError(f"source pagination incomplete: expected {expected}, got {len(unique)} unique rows")
    return list(unique.values()), expected if expected is not None else len(unique)


def writer_from_row(row: dict[str, Any]) -> str:
    value = row.get("fromEmp")
    return str(value.get("name") or "") if isinstance(value, dict) else ""


def report_time_from_row(row: dict[str, Any]) -> str:
    value = row.get("reportTime")
    if isinstance(value, (int, float)):
        number = float(value) / 1000 if value > 10**12 else float(value)
        return datetime.fromtimestamp(number).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def normalized_row(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.setdefault("reportId", str(row.get("id") or ""))
    value.setdefault("writeEmpName", writer_from_row(row))
    value.setdefault("createTime", report_time_from_row(row))
    return value


def fetch_one(row: dict[str, Any], app_key: str, raw_dir: Path) -> dict[str, Any]:
    row = normalized_row(row)
    rid = str(row.get("reportId") or "")
    full = run_tool(QUERY, ["--mode", "full-content-for-ai", "--report-record-id", rid], app_key)
    # Full content is the required truth-source payload.  The other two calls
    # enrich the evidence but do not block capture when their endpoint rejects
    # a particular report type.
    simple = run_tool(QUERY, ["--mode", "record-simple-info", "--report-record-id", rid, "--type", "content", "--type", "reply"], app_key)
    node = run_tool(QUERY, ["--mode", "node-detail", "--report-id", rid, "--no-share-link"], app_key)
    if not full.get("success"):
        return {"report_id": rid, "status": "failed", "error": str(full.get("error") or full)[:1000]}
    path = write_markdown(
        raw_dir, rid, row, "inbox_awareness", full, simple, node,
        change_type="historical_backfill", source_scopes={"date_range_search"}, collection_mode="historical-backfill",
    )
    return {"report_id": rid, "status": "written", "path": str(path)}


def run_backfill(
    *, app_key: str, start_date: str, end_date: str, run_name: str,
    mirror_root: Path, max_parallel: int, page_size: int,
) -> dict[str, Any]:
    run_dir = PROJECT / "runs" / run_name
    staging = run_dir / "collected-raw"
    staging.mkdir(parents=True, exist_ok=True)
    rows, source_total = source_rows(app_key, start_date, end_date, page_size)
    source_by_id = {str(row.get("id") or row.get("reportId") or row.get("reportRecordId")): row for row in rows}
    existing = raw_index(mirror_root / "raw")
    missing = sorted(set(source_by_id) - set(existing))

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {pool.submit(fetch_one, source_by_id[rid], app_key, staging): rid for rid in missing}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - record and resume safely
                results.append({"report_id": rid, "status": "failed", "error": str(exc)[:1000]})

    promotion = promote([staging], mirror_root)
    final_raw = raw_index(mirror_root / "raw")
    remaining = sorted(set(source_by_id) - set(final_raw))
    manifest = {
        "schema_version": "cwk.date-range-backfill.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_name": run_name,
        "start_date": start_date,
        "end_date": end_date,
        "source_total": source_total,
        "missing_before": len(missing),
        "written_count": sum(item.get("status") == "written" for item in results),
        "failed_count": sum(item.get("status") == "failed" for item in results),
        "remaining_missing": len(remaining),
        "remaining_ids": remaining,
        "errors": [item for item in results if item.get("status") == "failed"],
        "promotion": promotion,
        "mutating_cwork_commands_called": [],
    }
    output = run_dir / "backfill-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill every visible CWork report in a business-date range.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--run-name", default=datetime.now().strftime("date-backfill-%Y%m%d-%H%M%S"))
    parser.add_argument("--mirror-root", default=str(MIRROR))
    parser.add_argument("--max-parallel", type=int, default=6)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required")
    result = run_backfill(
        app_key=args.app_key, start_date=args.start_date, end_date=args.end_date,
        run_name=args.run_name, mirror_root=Path(args.mirror_root).expanduser().resolve(),
        max_parallel=args.max_parallel, page_size=args.page_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not result["remaining_missing"] else 2)


if __name__ == "__main__":
    main()
