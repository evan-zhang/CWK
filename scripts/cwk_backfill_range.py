#!/usr/bin/env python3
"""Fully page a CWork date range and promote every missing report to raw.

Source rows default to the 3.1 inbox endpoint (second-level begin/end
timestamps).  The 6.16 searchPage lane remains available via
``--source search-list`` as a fallback: live verification on 2026-09-03
showed the same window returning 0 rows through search-list and 38 rows
through the inbox endpoint, and the inbox API rejects 13-digit millisecond
timestamps, so the second-level conversion here is load-bearing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cwk_collect_live import CWORK, QUERY, run_tool, write_markdown
from cwk_raw_store import promote, raw_index


PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
_CST = timezone(timedelta(hours=8))


def epoch_seconds(date_text: str, *, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to a second-level epoch pinned to UTC+8.

    The 3.1 inbox endpoint accepts 10-digit second timestamps only and
    silently matches zero rows when fed 13-digit milliseconds (verified live
    2026-09-03).  The scale guard refuses any value that drifted into
    millisecond territory.
    """
    dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=_CST)
    if end_of_day:
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    seconds = int(dt.timestamp())
    if not 10**9 <= seconds < 10**12:
        raise ValueError(f"epoch for {date_text!r} is not second-scale: {seconds}")
    return seconds


def rows_from_result(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or result)[:1000])
    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("search-list returned non-object data")
    rows = data.get("list") or data.get("rows") or data.get("items") or []
    return (rows if isinstance(rows, list) else []), data.get("total")


def rows_from_page_data(data: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(data, dict):
        raise RuntimeError("source page returned non-object data")
    rows = data.get("list") or data.get("rows") or data.get("items") or []
    total = data.get("total")
    return (rows if isinstance(rows, list) else []), (int(total) if total is not None else None)


def search_list_source_rows(app_key: str, start_date: str, end_date: str, page_size: int = 100) -> tuple[list[dict[str, Any]], int]:
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


def _inbox_client(app_key: str) -> Any:
    # The cms-cwork-workflow skill ships outside this repository; import it
    # lazily so unit tests and CI never require the skill to be installed.
    if str(CWORK) not in sys.path:
        sys.path.insert(0, str(CWORK))
    from cwork_client import CWorkClient  # noqa: PLC0415 - lazy on purpose

    return CWorkClient(app_key)


def enrich_inbox_row(row: dict[str, Any]) -> dict[str, Any]:
    """Materialize the numeric reportTime that inbox rows omit.

    Inbox list items carry the event timestamp inside reportEventVO.time
    (ISO text) instead of a numeric reportTime.  Coverage auditing buckets
    rows by reportTime, so derive it as a second-level integer (the scale the
    inbox endpoint itself speaks).  A server-provided value is never
    overwritten.
    """
    if row.get("reportTime") is not None:
        return row
    event = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
    try:
        row["reportTime"] = int(datetime.fromisoformat(str(event.get("time") or "")).timestamp())
    except ValueError:
        pass
    return row


def inbox_source_rows(
    client: Any, start_date: str, end_date: str, page_size: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    begin_sec = epoch_seconds(start_date)
    end_sec = epoch_seconds(end_date, end_of_day=True)
    rows: list[dict[str, Any]] = []
    expected: int | None = None
    for page in range(1, 1001):
        try:
            data = client.get_inbox_list(
                page_size=page_size, page_index=page,
                begin_time=begin_sec, end_time=end_sec,
            )
        except Exception as exc:  # noqa: BLE001 - surface as a data-source failure
            raise RuntimeError(f"inbox page {page} failed: {exc}") from exc
        batch, total = rows_from_page_data(data)
        expected = int(total) if total is not None else expected
        rows.extend(enrich_inbox_row(row) for row in batch if isinstance(row, dict))
        if len(batch) < page_size:
            break
    unique = {str(row.get("id") or row.get("reportId") or row.get("reportRecordId") or ""): row for row in rows}
    unique.pop("", None)
    if expected is not None and len(unique) != expected:
        raise RuntimeError(f"source pagination incomplete: expected {expected}, got {len(unique)} unique rows")
    return list(unique.values()), expected if expected is not None else len(unique)


def source_rows(
    app_key: str, start_date: str, end_date: str, page_size: int = 100,
    *, source: str = "inbox", client_factory: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    if source == "search-list":
        return search_list_source_rows(app_key, start_date, end_date, page_size)
    if source != "inbox":
        raise ValueError(f"unknown source: {source!r}")
    make_client = client_factory or _inbox_client
    return inbox_source_rows(make_client(app_key), start_date, end_date, page_size)


def writer_from_row(row: dict[str, Any]) -> str:
    value = row.get("fromEmp")
    if isinstance(value, dict) and value.get("name"):
        return str(value["name"])
    event = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
    return str(event.get("name") or "")


def report_time_from_row(row: dict[str, Any]) -> str:
    value = row.get("reportTime")
    if isinstance(value, (int, float)):
        number = float(value) / 1000 if value > 10**12 else float(value)
        return datetime.fromtimestamp(number).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    event = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
    text = str(value or row.get("createTime") or event.get("time") or "")
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def normalized_row(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.setdefault("reportId", str(row.get("id") or ""))
    value.setdefault("writeEmpName", writer_from_row(row))
    value.setdefault("createTime", report_time_from_row(row))
    return value


def fetch_one(row: dict[str, Any], app_key: str, raw_dir: Path, source_scopes: set[str] | None = None) -> dict[str, Any]:
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
        change_type="historical_backfill", source_scopes=source_scopes or {"date_range_search"}, collection_mode="historical-backfill",
    )
    return {"report_id": rid, "status": "written", "path": str(path)}


def run_backfill(
    *, app_key: str, start_date: str, end_date: str, run_name: str,
    mirror_root: Path, max_parallel: int, page_size: int, cloud_first: bool = False,
    source: str = "inbox",
) -> dict[str, Any]:
    run_dir = PROJECT / "runs" / run_name
    staging = run_dir / "collected-raw"
    staging.mkdir(parents=True, exist_ok=True)
    rows, source_total = source_rows(app_key, start_date, end_date, page_size, source=source)
    source_by_id = {str(row.get("id") or row.get("reportId") or row.get("reportRecordId")): row for row in rows}
    existing = raw_index(mirror_root / "raw")
    missing = sorted(set(source_by_id) - set(existing))
    scopes = {"inbox_range"} if source == "inbox" else {"date_range_search"}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {pool.submit(fetch_one, source_by_id[rid], app_key, staging, scopes): rid for rid in missing}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - record and resume safely
                results.append({"report_id": rid, "status": "failed", "error": str(exc)[:1000]})

    promotion = promote([staging], mirror_root, cloud_first=cloud_first)
    final_raw = raw_index(mirror_root / "raw")
    remaining = sorted(set(source_by_id) - set(final_raw))
    manifest = {
        "schema_version": "cwk.date-range-backfill.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_name": run_name,
        "start_date": start_date,
        "end_date": end_date,
        "source_mode": source,
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
    parser.add_argument("--cloud-first", action="store_true", help="Preserve experimental Cloud-First raw manifest semantics.")
    parser.add_argument(
        "--source", choices=["inbox", "search-list"], default="inbox",
        help="List endpoint for source rows. inbox = 3.1 inbox API with second-level timestamps (default; live-verified 2026-09-03). search-list = 6.16 searchPage fallback.",
    )
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required")
    result = run_backfill(
        app_key=args.app_key, start_date=args.start_date, end_date=args.end_date,
        run_name=args.run_name, mirror_root=Path(args.mirror_root).expanduser().resolve(),
        max_parallel=args.max_parallel, page_size=args.page_size, cloud_first=args.cloud_first,
        source=args.source,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not result["remaining_missing"] else 2)


if __name__ == "__main__":
    main()
