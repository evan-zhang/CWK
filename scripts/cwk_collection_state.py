#!/usr/bin/env python3
"""Persistent state helpers for incremental CWK collection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "cwk.collection_state.v1"
FINGERPRINT_FIELDS = (
    "reportId",
    "reportRecordId",
    "id",
    "main",
    "title",
    "reportTitle",
    "createTime",
    "updateTime",
    "modifyTime",
    "finishTime",
    "replyCount",
    "hasNewReply",
    "status",
    "readStatus",
    "todoStatus",
    "todoType",
    "content",
    "leadContent",
)
STORAGE_FIELDS = (
    "reportId",
    "reportRecordId",
    "id",
    "main",
    "title",
    "reportTitle",
    "createTime",
    "updateTime",
    "modifyTime",
    "finishTime",
    "replyCount",
    "hasNewReply",
    "status",
    "readStatus",
    "todoStatus",
    "todoType",
    "writeEmpName",
    "creator",
    "creatorName",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "last_successful_run_at": None,
        "last_successful_run_name": None,
        "incremental_cutoff": None,
        "records": {},
        "pending": [],
        "backfill": {
            "next_lane_index": 0,
            "lanes": {},
        },
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported collection state schema: {payload.get('schema_version')}")
    state = default_state()
    state.update(payload)
    state["records"] = payload.get("records") or {}
    state["pending"] = payload.get("pending") or []
    state["backfill"] = payload.get("backfill") or state["backfill"]
    state["backfill"].setdefault("next_lane_index", 0)
    state["backfill"].setdefault("lanes", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = now_iso()
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def report_id(row: dict[str, Any]) -> str:
    return str(row.get("reportId") or row.get("reportRecordId") or row.get("id") or "").strip()


def minimal_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in STORAGE_FIELDS if key in row}


def row_fingerprint(row: dict[str, Any]) -> str:
    payload = {key: row.get(key) for key in FINGERPRINT_FIELDS if key in row}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (
        lambda: datetime.fromisoformat(text),
        lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
        lambda: datetime.strptime(text, "%Y-%m-%d"),
    ):
        try:
            parsed = parser()
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def candidate_time(row: dict[str, Any]) -> datetime | None:
    values = [parse_time(row.get(key)) for key in ("updateTime", "modifyTime", "finishTime", "createTime")]
    return max((value for value in values if value is not None), default=None)


def classify_candidate(
    rid: str,
    fingerprint: str,
    scopes: set[str],
    state: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> str:
    previous = (state.get("records") or {}).get(rid)
    if not previous:
        cutoff = parse_time(state.get("incremental_cutoff"))
        observed = candidate_time(row or {})
        if cutoff and (not observed or observed <= cutoff):
            if scopes.intersection({"pending", "todo_pending", "unread"}):
                return "continuation"
            return "unchanged"
        return "new"
    if previous.get("fingerprint") != fingerprint:
        return "updated"
    if scopes.intersection({"pending", "todo_pending", "unread"}):
        return "continuation"
    return "unchanged"


def pending_entry(
    rid: str,
    row: dict[str, Any],
    scopes: set[str],
    change_type: str,
    origin: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "report_id": rid,
        "row": minimal_row(row),
        "scopes": sorted(scopes),
        "change_type": change_type,
        "origin": origin,
        "fingerprint": fingerprint or row_fingerprint(row),
        "queued_at": now_iso(),
    }


def dedupe_pending(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for entry in entries:
        rid = str(entry.get("report_id") or "")
        if not rid:
            continue
        if rid in positions:
            result[positions[rid]] = entry
        else:
            positions[rid] = len(result)
            result.append(entry)
    return result


def choose_incremental(
    candidates: dict[str, dict[str, Any]],
    scopes: dict[str, set[str]],
    state: dict[str, Any],
    detail_cap: int,
    continuation_cap: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    queued_daily = [entry for entry in state.get("pending", []) if entry.get("origin") == "daily"]
    queued_ids = {str(entry.get("report_id")) for entry in queued_daily}
    classified: list[dict[str, Any]] = []
    counts = {"new": 0, "updated": 0, "continuation": 0, "unchanged": 0}

    for entry in queued_daily:
        rid = str(entry.get("report_id") or "")
        current_row = candidates.get(rid)
        row = current_row or entry.get("row") or {}
        item_scopes = scopes.get(rid) or set(entry.get("scopes") or [])
        fingerprint = row_fingerprint(row) if current_row is not None else str(entry.get("fingerprint") or row_fingerprint(row))
        change_type = classify_candidate(rid, fingerprint, item_scopes, state, row)
        if change_type == "unchanged":
            change_type = str(entry.get("change_type") or "updated")
        classified.append(
            pending_entry(rid, row, item_scopes, change_type, "daily", fingerprint)
        )

    for rid, row in candidates.items():
        if rid in queued_ids:
            continue
        fingerprint = row_fingerprint(row)
        change_type = classify_candidate(rid, fingerprint, scopes.get(rid, set()), state, row)
        counts[change_type] += 1
        if change_type != "unchanged":
            classified.append(pending_entry(rid, row, scopes.get(rid, set()), change_type, "daily", fingerprint))

    # A new reply or workflow update can reverse the meaning of an otherwise
    # old report.  Put those threads ahead of ordinary new notices; overflow
    # is still persisted below, so priority never discards evidence.
    def fresh_key(entry: dict[str, Any]) -> tuple[int, int, str]:
        row = entry.get("row") or {}
        reply_or_workflow_change = bool(row.get("hasNewReply")) or int(row.get("replyCount") or 0) > 0
        return (
            0 if reply_or_workflow_change else 1,
            0 if entry.get("change_type") == "updated" else 1,
            str(entry.get("report_id") or ""),
        )

    fresh = sorted(
        (entry for entry in classified if entry["change_type"] in {"new", "updated"}),
        key=fresh_key,
    )

    def continuation_key(entry: dict[str, Any]) -> tuple[int, float, str]:
        previous = (state.get("records") or {}).get(entry["report_id"])
        if not previous:
            return (0, 0.0, entry["report_id"])
        raw_time = previous.get("last_processed_at")
        if isinstance(raw_time, (int, float)):
            timestamp = float(raw_time)
        else:
            parsed = parse_time(raw_time)
            timestamp = parsed.timestamp() if parsed else 0.0
        return (1, timestamp, entry["report_id"])

    continuations = sorted(
        (entry for entry in classified if entry["change_type"] == "continuation"),
        key=continuation_key,
    )[:continuation_cap]
    ordered = fresh + continuations
    selected = ordered[:detail_cap]
    selected_ids = {entry["report_id"] for entry in selected}
    overflow = [entry for entry in classified if entry["report_id"] not in selected_ids and entry["change_type"] in {"new", "updated"}]
    return selected, dedupe_pending(overflow), counts


def choose_backfill(state: dict[str, Any], new_entries: list[dict[str, Any]], backfill_cap: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queued = [entry for entry in state.get("pending", []) if entry.get("origin") == "backfill"]
    processed = state.get("records") or {}
    combined = dedupe_pending(queued + new_entries)
    combined = [entry for entry in combined if entry.get("report_id") not in processed]
    return combined[:backfill_cap], combined[backfill_cap:]
