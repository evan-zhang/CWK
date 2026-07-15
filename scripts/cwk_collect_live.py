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


def write_markdown(out_dir: Path, rid: str, row: dict[str, Any], lane: str, full: dict[str, Any], simple: dict[str, Any], node: dict[str, Any]) -> Path:
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
        "collection_mode: live-read-only",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect live CWork records in read-only mode for CWK.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--run-name", default=datetime.now().strftime("live-read-%Y%m%d-%H%M%S"))
    parser.add_argument("--only-unread", action="store_true", help="Collect only unread report pages for backlog calibration.")
    parser.add_argument("--inbox-size", type=int, default=50)
    parser.add_argument("--unread-size", type=int, default=30)
    parser.add_argument("--unread-start-page", type=int, default=1)
    parser.add_argument("--unread-pages", type=int, default=1)
    parser.add_argument("--pending-size", type=int, default=30)
    parser.add_argument("--todo-size", type=int, default=30)
    parser.add_argument("--detail-cap", type=int, default=40)
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required.")

    run_dir = PROJECT / "runs" / args.run_name
    raw_dir = run_dir / "collected-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    commands: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, Any]] = {}
    candidate_lanes: dict[str, set[str]] = {}

    list_specs = []
    if not args.only_unread:
        list_specs.extend(
            [
                ("inbox", ["--mode", "inbox", "--page-size", str(args.inbox_size), "--page-index", "1", "--no-share-link"]),
                ("pending", ["--mode", "pending", "--page-size", str(args.pending_size), "--page-index", "1", "--no-share-link"]),
            ]
        )
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
        for row in rows_from_page(result):
            rid = report_id(row)
            if not rid:
                continue
            candidates.setdefault(rid, row)
            candidate_lanes.setdefault(rid, set()).add(lane)

    if not args.only_unread:
        todo_result = run_tool(TODO, ["list", "--page-size", str(args.todo_size), "--page-index", "1", "--status", "pending", "--no-share-link"], args.app_key)
        commands.append({"tool": "cwork-todo.py", "lane": "todo", "args": ["list", "--status", "pending"], "success": todo_result.get("success")})
        for todo in todo_rows(todo_result):
            rid = report_id(todo)
            if not rid:
                continue
            candidates.setdefault(rid, todo)
            candidate_lanes.setdefault(rid, set()).add("todo")

    selected = select_candidates(candidates, candidate_lanes, args.detail_cap)
    written: list[str] = []
    errors: list[dict[str, Any]] = []
    for rid, row in selected:
        lanes = candidate_lanes.get(rid, set())
        lane = infer_lane(row, lanes)
        full = run_tool(QUERY, ["--mode", "full-content-for-ai", "--report-record-id", rid], args.app_key)
        simple = run_tool(QUERY, ["--mode", "record-simple-info", "--report-record-id", rid, "--type", "content", "--type", "reply"], args.app_key)
        node = run_tool(QUERY, ["--mode", "node-detail", "--report-id", rid, "--no-share-link"], args.app_key)
        if not (full.get("success") or simple.get("success") or node.get("success")):
            errors.append({"report_id": rid, "title": title(row), "errors": [full, simple, node]})
            continue
        path = write_markdown(raw_dir, rid, row, lane, full, simple, node)
        written.append(str(path.relative_to(PROJECT)))

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "live-read-only",
        "run_name": args.run_name,
        "raw_dir": str(raw_dir.relative_to(PROJECT)),
        "candidate_count": len(candidates),
        "detail_cap": args.detail_cap,
        "selected_ids": [rid for rid, _row in selected],
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
