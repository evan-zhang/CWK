#!/usr/bin/env python3
"""Read-only live CWork collector for CWK Phase 1.

This script calls the existing cms-cwork-workflow command-line tools and writes
local Markdown evidence files. It never calls mutating CWork modes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_collection_state import (
    choose_backfill,
    choose_incremental,
    classify_candidate,
    default_state,
    dedupe_pending,
    load_state,
    minimal_row,
    now_iso,
    pending_entry,
    row_fingerprint,
    save_state,
)


PROJECT = Path(__file__).resolve().parents[1]
CWORK = Path.home() / ".openclaw" / "skills" / "cms-cwork-workflow" / "scripts"
QUERY = CWORK / "cwork-query-report.py"
TODO = CWORK / "cwork-todo.py"


def run_tool(script: Path, args: list[str], app_key: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=True) as f:
        json.dump({"app_key": app_key}, f, ensure_ascii=False)
        f.flush()
        cmd = ["python3", str(script), "--params-file", f.name, *args]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.strip() or proc.stdout.strip(), "cmd": [str(script.name), *args]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"success": False, "error": "non-json output", "stdout": proc.stdout[:1000], "cmd": [str(script.name), *args]}


def rows_from_page(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") if result.get("success") else {}
    if not isinstance(data, dict):
        return []
    rows = data.get("list") or data.get("rows") or data.get("items") or []
    return rows if isinstance(rows, list) else []


def todo_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("items") or []
    return rows if isinstance(rows, list) else []


def report_id(row: dict[str, Any]) -> str:
    return str(row.get("reportId") or row.get("id") or row.get("reportRecordId") or "").strip()


def title(row: dict[str, Any]) -> str:
    return str(row.get("main") or row.get("title") or row.get("reportTitle") or "").strip()


def writer(row: dict[str, Any]) -> str:
    event = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
    return str(row.get("writeEmpName") or row.get("creator") or event.get("name") or "").strip()


def created_at(row: dict[str, Any]) -> str:
    event = row.get("reportEventVO") if isinstance(row.get("reportEventVO"), dict) else {}
    return str(row.get("createTime") or event.get("time") or "").strip()


def is_recurring(name: str) -> bool:
    return any(word in name for word in ["周报", "月报", "季报", "进度汇总", "运营报告", "统计报表"])


def infer_lane(row: dict[str, Any], lanes: set[str]) -> str:
    name = title(row)
    reply_count = int(row.get("replyCount") or 0)
    if "todo" in lanes:
        return "todo_backed"
    if reply_count > 0 or row.get("hasNewReply"):
        return "reply_chain"
    if is_recurring(name):
        return "inbox_awareness"
    if any(word in name for word in ["会议纪要", "方案", "决策", "项目", "系统", "AI", "云端虾", "BP"]):
        return "persistent_stream"
    return "inbox_awareness"


def select_candidates(candidates: dict[str, dict[str, Any]], candidate_lanes: dict[str, set[str]], detail_cap: int) -> list[tuple[str, dict[str, Any]]]:
    selected: list[tuple[str, dict[str, Any]]] = []
    picked: set[str] = set()

    def pick(predicate, limit: int) -> None:
        for rid, row in candidates.items():
            if rid in picked:
                continue
            if predicate(rid, row):
                selected.append((rid, row))
                picked.add(rid)
            if len([1 for sid, srow in selected if predicate(sid, srow)]) >= limit or len(selected) >= detail_cap:
                break

    pick(lambda rid, row: "todo" in candidate_lanes.get(rid, set()), min(10, detail_cap))
    pick(lambda rid, row: int(row.get("replyCount") or 0) > 0 or bool(row.get("hasNewReply")), min(10, detail_cap))
    pick(lambda rid, row: infer_lane(row, candidate_lanes.get(rid, set())) == "persistent_stream", min(12, detail_cap))
    pick(lambda rid, row: is_recurring(title(row)), min(8, detail_cap))

    for rid, row in candidates.items():
        if len(selected) >= detail_cap:
            break
        if rid not in picked:
            selected.append((rid, row))
            picked.add(rid)
    return selected[:detail_cap]


def slug(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", value).strip("-")
    return value[:80] or "untitled"


def fenced_json(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def write_markdown(
    out_dir: Path,
    rid: str,
    row: dict[str, Any],
    lane: str,
    full: dict[str, Any],
    simple: dict[str, Any],
    node: dict[str, Any],
    *,
    change_type: str = "new",
    source_scopes: set[str] | None = None,
    collection_mode: str = "live-incremental",
) -> Path:
    data = full.get("data") if full.get("success") else {}
    full_content = data.get("fullContent") if isinstance(data, dict) else None
    if not full_content:
        full_content = row.get("content") or ""
    meta = [
        "---",
        f'report_id: "{rid}"',
        f'title: "{title(row).replace(chr(34), chr(39))}"',
        f'writer: "{writer(row).replace(chr(34), chr(39))}"',
        f'create_time: "{created_at(row)}"',
        f"source_lane: {lane}",
        f"collection_mode: {collection_mode}",
        f"change_type: {change_type}",
        f'source_scopes: "{",".join(sorted(source_scopes or set()))}"',
        "---",
        "",
    ]
    body = [
        *meta,
        f"# {title(row) or rid}",
        "",
        "## Original Full Content For AI",
        "",
        full_content,
        "",
        "## List Row Metadata",
        "",
        fenced_json(row),
        "",
        "## Record Simple Info",
        "",
        fenced_json(simple.get("data") if simple.get("success") else simple),
        "",
        "## Node / Opinion Chain",
        "",
        fenced_json(node.get("data") if node.get("success") else node),
        "",
    ]
    path = out_dir / f"{rid}-{slug(title(row))}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def add_rows(
    rows: list[dict[str, Any]],
    scope: str,
    candidates: dict[str, dict[str, Any]],
    candidate_scopes: dict[str, set[str]],
) -> None:
    for row in rows:
        rid = report_id(row)
        if not rid:
            continue
        candidates.setdefault(rid, row)
        candidate_scopes.setdefault(rid, set()).add(scope)


def extract_prior_list_row(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"## List Row Metadata\s+```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def bootstrap_from_latest_run(state_path: Path, current_run_name: str) -> dict[str, Any]:
    if state_path.exists():
        return load_state(state_path)
    manifests = sorted(
        path
        for path in (PROJECT / "runs").glob("*-collect/collect-manifest.json")
        if path.parent.name != current_run_name
    )
    if not manifests:
        return default_state()
    latest = manifests[-1]
    state = default_state()
    try:
        prior_manifest = json.loads(latest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        prior_manifest = {}
    for raw_path in sorted((latest.parent / "collected-raw").glob("*.md")):
        row = extract_prior_list_row(raw_path)
        if not row:
            continue
        rid = report_id(row)
        if not rid:
            continue
        state["records"][rid] = {
            "fingerprint": row_fingerprint(row),
            "first_processed_at": latest.stat().st_mtime,
            "last_processed_at": latest.stat().st_mtime,
            "last_seen_at": latest.stat().st_mtime,
            "last_change_type": "bootstrap",
        }
    state["bootstrap_source"] = str(latest.relative_to(PROJECT))
    state["incremental_cutoff"] = prior_manifest.get("generated_at") or datetime.fromtimestamp(latest.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    return state


BACKFILL_LANES = (
    "history_inbox",
    "history_outbox",
    "history_pending_report",
    "history_todo_pending",
    "history_todo_completed",
)


def fetch_backfill_page(
    lane: str,
    page_index: int,
    page_size: int,
    app_key: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if lane.startswith("history_todo_"):
        status = "completed" if lane.endswith("completed") else "pending"
        args = ["list", "--page-size", str(page_size), "--page-index", str(page_index), "--status", status, "--no-share-link"]
        result = run_tool(TODO, args, app_key)
        return result, todo_rows(result), args
    mode = {
        "history_inbox": "inbox",
        "history_outbox": "outbox",
        "history_pending_report": "pending",
    }[lane]
    args = ["--mode", mode, "--page-size", str(page_size), "--page-index", str(page_index), "--no-share-link"]
    result = run_tool(QUERY, args, app_key)
    return result, rows_from_page(result), args


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect live CWork records in read-only mode for CWK.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--run-name", default=datetime.now().strftime("live-read-%Y%m%d-%H%M%S"))
    parser.add_argument("--only-unread", action="store_true", help="Collect only unread report pages for backlog calibration.")
    parser.add_argument("--inbox-size", type=int, default=50)
    parser.add_argument("--inbox-pages", type=int, default=3)
    parser.add_argument("--unread-size", type=int, default=30)
    parser.add_argument("--unread-start-page", type=int, default=1)
    parser.add_argument("--unread-pages", type=int, default=2)
    parser.add_argument("--pending-size", type=int, default=30)
    parser.add_argument("--pending-pages", type=int, default=2)
    parser.add_argument("--outbox-size", type=int, default=30)
    parser.add_argument("--outbox-pages", type=int, default=2)
    parser.add_argument("--todo-size", type=int, default=30)
    parser.add_argument("--todo-pages", type=int, default=2)
    parser.add_argument("--detail-cap", type=int, default=60, help="Maximum new/updated/carryover records processed per run.")
    parser.add_argument("--continuation-cap", type=int, default=15)
    parser.add_argument("--state-file", default=str(PROJECT / "state" / "collection-state.json"))
    parser.add_argument("--backfill-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--backfill-cap", type=int, default=20)
    parser.add_argument("--backfill-page-size", type=int, default=20)
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required.")

    run_dir = PROJECT / "runs" / args.run_name
    collection_started_at = now_iso()
    raw_dir = run_dir / "collected-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    commands: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, Any]] = {}
    candidate_scopes: dict[str, set[str]] = {}
    state_path = Path(args.state_file).expanduser().resolve()
    state = bootstrap_from_latest_run(state_path, args.run_name)

    list_specs = []
    if not args.only_unread:
        for page_index in range(1, args.inbox_pages + 1):
            list_specs.append(("inbox", ["--mode", "inbox", "--page-size", str(args.inbox_size), "--page-index", str(page_index), "--no-share-link"]))
        for page_index in range(1, args.pending_pages + 1):
            list_specs.append(("pending", ["--mode", "pending", "--page-size", str(args.pending_size), "--page-index", str(page_index), "--no-share-link"]))
        for page_index in range(1, args.outbox_pages + 1):
            list_specs.append(("outbox", ["--mode", "outbox", "--page-size", str(args.outbox_size), "--page-index", str(page_index), "--no-share-link"]))
    unread_mode = "search-list" if args.only_unread else "unread"
    for page_index in range(args.unread_start_page, args.unread_start_page + args.unread_pages):
        unread_args = [
            "--mode",
            unread_mode,
            "--page-size",
            str(args.unread_size),
            "--page-index",
            str(page_index),
            "--no-share-link",
        ]
        if unread_mode == "search-list":
            unread_args.extend(["--status", "0"])
        list_specs.append(
            (
                "unread",
                unread_args,
            )
        )
    for lane, cmd_args in list_specs:
        result = run_tool(QUERY, cmd_args, args.app_key)
        commands.append({"tool": "cwork-query-report.py", "lane": lane, "args": cmd_args, "success": result.get("success")})
        add_rows(rows_from_page(result), lane, candidates, candidate_scopes)

    if not args.only_unread:
        for page_index in range(1, args.todo_pages + 1):
            todo_args = ["list", "--page-size", str(args.todo_size), "--page-index", str(page_index), "--status", "pending", "--no-share-link"]
            todo_result = run_tool(TODO, todo_args, args.app_key)
            commands.append({"tool": "cwork-todo.py", "lane": "todo_pending", "args": todo_args, "success": todo_result.get("success")})
            add_rows(todo_rows(todo_result), "todo_pending", candidates, candidate_scopes)

    selected_daily, pending_daily, delta_counts = choose_incremental(
        candidates,
        candidate_scopes,
        state,
        args.detail_cap,
        args.continuation_cap,
    )

    backfill_entries: list[dict[str, Any]] = []
    backfill_run = {"enabled": bool(args.backfill_enabled and not args.only_unread), "lane": None, "page_index": None, "row_count": 0, "success": None}
    if backfill_run["enabled"]:
        backfill_state = state.setdefault("backfill", {"next_lane_index": 0, "lanes": {}})
        start_index = int(backfill_state.get("next_lane_index", 0)) % len(BACKFILL_LANES)
        lane_index = None
        for offset in range(len(BACKFILL_LANES)):
            candidate_index = (start_index + offset) % len(BACKFILL_LANES)
            candidate_lane = BACKFILL_LANES[candidate_index]
            candidate_state = backfill_state.setdefault("lanes", {}).setdefault(candidate_lane, {"next_page": 1, "exhausted": False, "failure_count": 0})
            if not candidate_state.get("exhausted"):
                lane_index = candidate_index
                break
        if lane_index is not None:
            lane = BACKFILL_LANES[lane_index]
            lane_state = backfill_state["lanes"][lane]
            page_index = int(lane_state.get("next_page", 1))
            result, rows, tool_args = fetch_backfill_page(lane, page_index, args.backfill_page_size, args.app_key)
            backfill_run.update({"lane": lane, "page_index": page_index, "row_count": len(rows), "success": bool(result.get("success"))})
            commands.append({"tool": "cwork-todo.py" if lane.startswith("history_todo_") else "cwork-query-report.py", "lane": lane, "args": tool_args, "success": result.get("success")})
            if result.get("success"):
                for row in rows:
                    rid = report_id(row)
                    if not rid:
                        continue
                    scopes = {lane}
                    backfill_entries.append(pending_entry(rid, row, scopes, "historical_backfill", "backfill"))
                lane_state["last_success_at"] = now_iso()
                lane_state["last_row_count"] = len(rows)
                lane_state["failure_count"] = 0
                if len(rows) < args.backfill_page_size:
                    lane_state["exhausted"] = True
                else:
                    lane_state["next_page"] = page_index + 1
            else:
                lane_state["failure_count"] = int(lane_state.get("failure_count", 0)) + 1
                lane_state["last_error_at"] = now_iso()
            backfill_state["next_lane_index"] = (lane_index + 1) % len(BACKFILL_LANES)
        else:
            backfill_run.update({"success": True, "exhausted": True})

    daily_selected_ids = {entry["report_id"] for entry in selected_daily}
    backfill_view = {**state, "records": {**state.get("records", {}), **{rid: {"fingerprint": "selected-daily"} for rid in daily_selected_ids}}}
    selected_backfill, pending_backfill = choose_backfill(backfill_view, backfill_entries, args.backfill_cap)
    selected = selected_daily + selected_backfill
    written: list[str] = []
    errors: list[dict[str, Any]] = []
    successful_ids: set[str] = set()
    failed_entries: list[dict[str, Any]] = []
    for entry in selected:
        rid = entry["report_id"]
        row = entry.get("row") or {}
        scopes = set(entry.get("scopes") or [])
        lane = infer_lane(row, {"todo" if "todo_pending" in scopes else scope for scope in scopes})
        full = run_tool(QUERY, ["--mode", "full-content-for-ai", "--report-record-id", rid], args.app_key)
        simple = run_tool(QUERY, ["--mode", "record-simple-info", "--report-record-id", rid, "--type", "content", "--type", "reply"], args.app_key)
        node = run_tool(QUERY, ["--mode", "node-detail", "--report-id", rid, "--no-share-link"], args.app_key)
        if not (full.get("success") or simple.get("success") or node.get("success")):
            errors.append({"report_id": rid, "title": title(row), "errors": [full, simple, node]})
            failed_entries.append(entry)
            continue
        collection_mode = "historical-backfill" if entry.get("origin") == "backfill" else "live-incremental"
        path = write_markdown(
            raw_dir,
            rid,
            row,
            lane,
            full,
            simple,
            node,
            change_type=entry.get("change_type") or "updated",
            source_scopes=scopes,
            collection_mode=collection_mode,
        )
        written.append(str(path.relative_to(PROJECT)))
        successful_ids.add(rid)
        previous = state.setdefault("records", {}).get(rid) or {}
        state["records"][rid] = {
            "fingerprint": entry.get("fingerprint") or row_fingerprint(row),
            "first_processed_at": previous.get("first_processed_at") or now_iso(),
            "last_processed_at": now_iso(),
            "last_seen_at": now_iso(),
            "last_change_type": entry.get("change_type"),
        }

    for rid in set(candidates).intersection(state.get("records", {})):
        state["records"][rid]["last_seen_at"] = now_iso()
    state["pending"] = dedupe_pending(pending_daily + pending_backfill + failed_entries)
    daily_source_failures = [
        command for command in commands
        if command.get("lane") in {"inbox", "pending", "outbox", "unread", "todo_pending"} and not command.get("success")
    ]
    state["last_attempt_run_at"] = now_iso()
    state["last_attempt_run_name"] = args.run_name
    if not daily_source_failures:
        state["last_successful_run_at"] = now_iso()
        state["last_successful_run_name"] = args.run_name
        state["incremental_cutoff"] = collection_started_at
    save_state(state_path, state)

    selected_change_counts: dict[str, int] = {}
    for entry in selected:
        key = str(entry.get("change_type") or "unknown")
        selected_change_counts[key] = selected_change_counts.get(key, 0) + 1

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "live-read-only",
        "run_name": args.run_name,
        "raw_dir": str(raw_dir.relative_to(PROJECT)),
        "candidate_count": len(candidates),
        "detail_cap": args.detail_cap,
        "continuation_cap": args.continuation_cap,
        "backfill_cap": args.backfill_cap,
        "selected_ids": [entry["report_id"] for entry in selected],
        "selected_daily_count": len(selected_daily),
        "selected_backfill_count": len(selected_backfill),
        "selected_change_counts": selected_change_counts,
        "candidate_delta_counts": delta_counts,
        "pending_count": len(state["pending"]),
        "daily_source_complete": not daily_source_failures,
        "daily_source_failure_count": len(daily_source_failures),
        "backfill_run": backfill_run,
        "state_file": str(state_path),
        "written_count": len(written),
        "written": written,
        "errors": errors,
        "commands": commands,
        "mutating_commands_called": [],
    }
    (run_dir / "collect-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
