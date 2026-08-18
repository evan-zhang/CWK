#!/usr/bin/env python3
"""Query the backend-owned current-user/report relationship in bounded batches."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_ai_common import PROJECT, write_json
from cwk_person_relation import SCHEMA, normalize_backend_relation


DEFAULT_BASE_URL = "https://sg-al-cwork-web.mediportal.com.cn"


def report_ids_from_run(run_dir: Path) -> list[str]:
    ids = []
    for path in sorted((run_dir / "raw").glob("*.md")):
        report_id = path.name.split("-", 1)[0].strip()
        if report_id and report_id not in ids:
            ids.append(report_id)
    return ids


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _post(base_url: str, endpoint_path: str, app_key: str, report_ids: list[str], timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/" + endpoint_path.lstrip("/")
    request = urllib.request.Request(
        url,
        data=json.dumps({"reportIds": report_ids}).encode("utf-8"),
        headers={"appKey": app_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"relationship API request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("resultCode") != 1:
        raise RuntimeError("relationship API returned a non-success result")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("relationship API data is not an object")
    return data


def query_relationships(
    *,
    base_url: str,
    endpoint_path: str,
    app_key: str,
    report_ids: list[str],
    batch_size: int = 200,
    timeout: int = 30,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(value).strip() for value in report_ids if str(value).strip()))
    normalized: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    relation_version = ""
    evaluated_at = ""
    for batch in chunks(requested, batch_size):
        try:
            data = _post(base_url, endpoint_path, app_key, batch, timeout)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        relation_version = str(data.get("relationVersion") or relation_version)
        evaluated_at = str(data.get("evaluatedAt") or evaluated_at)
        values = data.get("items")
        if not isinstance(values, list):
            errors.append("relationship API items is not a list")
            continue
        seen_in_batch: set[str] = set()
        for value in values:
            if not isinstance(value, dict):
                continue
            report_id = str(value.get("reportId") or "").strip()
            if not report_id:
                continue
            if report_id not in batch:
                errors.append("relationship API returned an unrequested reportId")
                continue
            if report_id in seen_in_batch:
                errors.append("relationship API returned a duplicate reportId")
                normalized.pop(report_id, None)
                continue
            seen_in_batch.add(report_id)
            normalized[report_id] = {
                "reportId": report_id,
                **normalize_backend_relation(
                    value,
                    expected_report_id=report_id,
                    relation_version=relation_version,
                ),
            }
    missing = [report_id for report_id in requested if report_id not in normalized]
    provider_status = "ok" if not errors and not missing else "partial" if normalized else "unavailable"
    return {
        "schema_version": SCHEMA,
        "provider": "work-report-backend",
        "provider_status": provider_status,
        "relation_version": relation_version,
        "evaluated_at": evaluated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "requested_count": len(requested),
        "resolved_count": sum(1 for value in normalized.values() if value.get("relationship_status") != "unknown"),
        "missing_report_ids": missing,
        "errors": list(dict.fromkeys(errors)),
        "items": normalized,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Query authoritative current-user/report relationships.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--base-url", default=os.environ.get("CWK_RELATION_API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--endpoint-path", default=os.environ.get("CWK_RELATION_API_PATH", ""))
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or "")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if not args.endpoint_path:
        raise SystemExit("CWK_RELATION_API_PATH/--endpoint-path is required")
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY/--app-key is required")
    if args.batch_size < 1 or args.batch_size > 200:
        raise SystemExit("--batch-size must be between 1 and 200")
    run_dir = PROJECT / "runs" / args.run_name
    output = Path(args.output).expanduser().resolve() if args.output else run_dir / "report-relationships.json"
    result = query_relationships(
        base_url=args.base_url,
        endpoint_path=args.endpoint_path,
        app_key=args.app_key,
        report_ids=report_ids_from_run(run_dir),
        batch_size=args.batch_size,
        timeout=args.timeout,
    )
    write_json(output, result)
    print(json.dumps({key: result[key] for key in ("provider_status", "requested_count", "resolved_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
