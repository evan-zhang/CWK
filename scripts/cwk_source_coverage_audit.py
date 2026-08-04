#!/usr/bin/env python3
"""Compare the authoritative date-range list with local raw and Wiki pages."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from cwk_backfill_range import source_rows
from cwk_raw_store import raw_index


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"


def row_date(row: dict) -> str:
    value = row.get("reportTime")
    if isinstance(value, (int, float)):
        number = float(value) / 1000 if value > 10**12 else float(value)
        return datetime.fromtimestamp(number).astimezone().strftime("%Y-%m-%d")
    return "unknown"


def audit(app_key: str, start_date: str, end_date: str, mirror_root: Path, page_size: int = 100) -> dict:
    rows, source_total = source_rows(app_key, start_date, end_date, page_size)
    source = {str(row.get("id") or row.get("reportId") or row.get("reportRecordId")): row for row in rows}
    raw = raw_index(mirror_root / "raw")
    summaries = {path.stem for path in (mirror_root / "wiki" / "summaries").glob("*.md")}
    by_day: dict[str, Counter] = defaultdict(Counter)
    for rid, row in source.items():
        counts = by_day[row_date(row)]
        counts["source"] += 1
        counts["raw"] += int(rid in raw)
        counts["summary"] += int(rid in summaries)
    missing_raw = sorted(set(source) - set(raw))
    missing_summary = sorted(set(source) - summaries)
    return {
        "schema_version": "cwk.source-coverage-audit.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "start_date": start_date,
        "end_date": end_date,
        "source_total": source_total,
        "raw_covered": source_total - len(missing_raw),
        "summary_covered": source_total - len(missing_summary),
        "missing_raw_count": len(missing_raw),
        "missing_summary_count": len(missing_summary),
        "missing_raw_ids": missing_raw,
        "missing_summary_ids": missing_summary,
        "daily": {day: dict(counts) for day, counts in sorted(by_day.items())},
        "complete": not missing_raw and not missing_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit source/raw/Wiki coverage for a date range.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--mirror-root", default=str(MIRROR))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--manifest-out")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required")
    result = audit(args.app_key, args.start_date, args.end_date, Path(args.mirror_root).expanduser().resolve(), args.page_size)
    if args.manifest_out:
        output = Path(args.manifest_out).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.strict and not result["complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
