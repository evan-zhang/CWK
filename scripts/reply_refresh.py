#!/usr/bin/env python3
"""Refresh raw snapshots when a report's reply activity changed.

The canonical raw file is immutable by policy: it stays the first-capture
snapshot.  When the CWork list endpoints report a replyCount/hasNewReply
that differs from the last observed baseline, this tool re-fetches the
detail payloads (read-only), writes a NEW ``<id>-v2-<title>.md`` raw file
next to the original, registers it in the raw manifest, and optionally
triggers recompilation of that report's wiki page.

Baseline: ``wiki/_system/reply-state.json`` holds ``report_id →
{reply_count, has_new_reply, checked_at}``.  First run establishes the
baseline without re-fetching.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
SCRIPTS = PROJECT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from cwk_backfill_range import _inbox_client, epoch_seconds, outbox_source_rows  # noqa: E402
from cwk_raw_store import parse_frontmatter, raw_index, sha256_bytes  # noqa: E402

BASELINE_REL = "wiki/_system/reply-state.json"
SCHEMA = "cwk.reply-refresh.v1"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def load_baseline(mirror_root: Path) -> dict[str, Any]:
    path = mirror_root / BASELINE_REL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    reports = data.get("reports") if isinstance(data, dict) else None
    return reports if isinstance(reports, dict) else {}


def save_baseline(mirror_root: Path, reports: dict[str, Any], *, established: bool) -> Path:
    path = mirror_root / BASELINE_REL
    payload = {
        "schema_version": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "baseline-established" if established else "refresh-run",
        "report_count": len(reports),
        "reports": reports,
    }
    atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return path


def list_state(row: dict[str, Any]) -> tuple[int, bool]:
    return int(row.get("replyCount") or 0), bool(row.get("hasNewReply"))


def detect_changes(
    baseline: dict[str, Any], rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split rows into changed / unchanged vs the baseline.

    Returns (changed_rows, new_baseline_entries).  Rows absent from the
    baseline seed fresh entries (no re-fetch on first sight).
    """
    changed: list[dict[str, Any]] = []
    fresh: dict[str, Any] = {}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for row in rows:
        rid = str(row.get("id") or row.get("reportId") or "")
        if not rid:
            continue
        count, has_new = list_state(row)
        entry = {"reply_count": count, "has_new_reply": has_new, "checked_at": now}
        prior = baseline.get(rid)
        if prior is None:
            fresh[rid] = entry
            continue
        if int(prior.get("reply_count") or 0) != count or bool(prior.get("has_new_reply")) != has_new:
            row["_reply_state_entry"] = entry
            changed.append(row)
    return changed, fresh


def next_version_path(original: Path, title_slug: str) -> Path:
    """Version a NEW sibling raw file without touching the original.

    ``209xxx-标题.md`` → ``209xxx-v2-标题.md`` (then -v3, -v4, ...).
    """
    parent = original.parent
    match = re.match(r"^(?P<rid>\d+?)(?:-v(?P<ver>\d+))?(?:-.*)?$", original.stem)
    rid = match.group("rid") if match else original.stem.split("-", 1)[0]
    version = int(match.group("ver") or 1) + 1 if match else 2
    while True:
        candidate = parent / f"{rid}-v{version}-{title_slug}.md"
        if not candidate.exists():
            return candidate
        version += 1


def fetch_detail(row: dict[str, Any], app_key: str) -> dict[str, Any]:
    from cwk_collect_live import QUERY, run_tool  # noqa: PLC0415 - lazy on purpose

    rid = str(row.get("id") or row.get("reportId"))
    full = run_tool(QUERY, ["--mode", "full-content-for-ai", "--report-record-id", rid], app_key)
    simple = run_tool(QUERY, ["--mode", "record-simple-info", "--report-record-id", rid, "--type", "content", "--type", "reply"], app_key)
    node = run_tool(QUERY, ["--mode", "node-detail", "--report-id", rid, "--no-share-link"], app_key)
    return {"full": full, "simple": simple, "node": node}


def write_v2_raw(
    mirror_root: Path, original: Path, row: dict[str, Any], detail: dict[str, Any],
) -> Path:
    """Write the v2 snapshot directly (bypass promote's by-id dedupe).

    The original file is never opened for writing.  The new file carries the
    same frontmatter contract plus ``change_type: reply_refresh`` and
    ``supersedes: <original name>``.
    """
    from cwk_collect_live import slug, title  # noqa: PLC0415 - lazy on purpose

    rid = str(row.get("id") or row.get("reportId"))
    target = next_version_path(original, slug(title(row)))
    text = original.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    header = [
        "---",
        f'report_id: "{rid}"',
        f'title: "{title(row).replace(chr(34), chr(39))}"',
        f'writer: "{str(row.get("writeEmpName") or fields.get("writer") or "").replace(chr(34), chr(39))}"',
        f'create_time: "{fields.get("create_time", "")}"',
        "source_lane: reply_refresh",
        f"collection_mode: {fields.get('collection_mode', 'reply-refresh')}",
        "change_type: reply_refresh",
        'source_scopes: "outbox_range,reply_refresh"' if row.get("_from_outbox") else 'source_scopes: "inbox_range,reply_refresh"',
        f'supersedes: "{original.name}"',
        'reply_count: %d' % int(row.get("replyCount") or 0),
        "---",
        "",
    ]
    body = [
        *header,
        f"# {title(row) or rid}",
        "",
        "## Original Full Content For AI",
        "",
        (detail["full"].get("data") or {}).get("fullContent") or "",
        "",
        "## List Row Metadata",
        "",
        "```json",
        json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Record Simple Info",
        "",
        "```json",
        json.dumps(detail["simple"].get("data") if detail["simple"].get("success") else detail["simple"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Node / Opinion Chain",
        "",
        "```json",
        json.dumps(detail["node"].get("data") if detail["node"].get("success") else detail["node"], ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    atomic_write(mirror_root / target.relative_to(mirror_root) if target.is_absolute() else target, "\n".join(body).encode("utf-8"))
    return target


def register_in_manifest(mirror_root: Path, new_path: Path) -> None:
    """Append the v2 file to the raw manifest (extend, never rewrite records)."""
    from cwk_raw_store import atomic_write as _aw  # noqa: PLC0415

    manifest_path = mirror_root / "raw" / "_system" / "raw-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {
        "schema_version": "cwk.raw-truth-source.v2",
        "records": [],
    }
    rel = new_path.relative_to(mirror_root).as_posix()
    rid = parse_frontmatter(new_path.read_text(encoding="utf-8")).get("report_id") or new_path.name.split("-", 1)[0]
    data["records"] = [r for r in data.get("records", []) if r.get("report_id") != rid or "-v" not in Path(r.get("canonical_path", "")).name]
    data["records"].append({"report_id": rid, "sha256": sha256_bytes(new_path.read_bytes()), "canonical_path": rel})
    data["record_count"] = len(data["records"])
    data["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _aw(manifest_path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def trigger_recompile(report_id: str, *, mirror_root: Path | None = None, model: str = "") -> dict[str, Any]:
    root = (mirror_root or MIRROR).expanduser().resolve()
    cmd = [sys.executable, str(SCRIPTS / "cwk_cloud_wiki_compile.py"),
           "--mirror-root", str(root), "--report-ids", report_id, "--limit", "1"]
    if model:
        cmd.extend(["--model", model])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return {"returncode": proc.returncode, "tail": (proc.stdout or proc.stderr)[-800:]}


def refresh(
    *, app_key: str, start_date: str, end_date: str, mirror_root: Path,
    recompile: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    mirror_root = mirror_root.expanduser().resolve()
    baseline = load_baseline(mirror_root)
    client = _inbox_client(app_key)
    from cwk_backfill_range import inbox_source_rows  # noqa: PLC0415

    inbox_rows, _ = inbox_source_rows(client, start_date, end_date)
    outbox_rows, _ = outbox_source_rows(client, start_date, end_date)
    for row in outbox_rows:
        row["_from_outbox"] = True
    rows = inbox_rows + outbox_rows
    changed, fresh = detect_changes(baseline, rows)

    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "start_date": start_date,
        "end_date": end_date,
        "listed_rows": len({str(r.get("id")) for r in rows}),
        "baseline_count": len(baseline),
        "fresh_baseline_entries": len(fresh),
        "changed_count": len(changed),
        "changed_ids": [str(r.get("id")) for r in changed],
        "refreshed": [],
        "recompiles": [],
        "dry_run": dry_run,
        "baseline_established": bool(fresh) and not baseline,
        "mutating_cwork_commands_called": [],
    }
    if dry_run or not changed:
        if not dry_run and (fresh or changed):
            baseline.update(fresh)
            for r in changed:
                baseline[str(r.get("id"))] = r["_reply_state_entry"]
            save_baseline(mirror_root, baseline, established=result["baseline_established"])
            result["baseline_saved"] = True
        return result

    index = raw_index(mirror_root / "raw")
    for row in changed:
        rid = str(row.get("id"))
        original = index.get(rid)
        if original is None:
            result["refreshed"].append({"report_id": rid, "status": "skipped", "reason": "no raw original"})
            continue
        detail = fetch_detail(row, app_key)
        if not detail["full"].get("success"):
            result["refreshed"].append({"report_id": rid, "status": "failed", "error": str(detail["full"].get("error"))[:300]})
            continue
        original_bytes_before = original.read_bytes()
        new_path = write_v2_raw(mirror_root, original, row, detail)
        unchanged = sha256_bytes(original.read_bytes()) == sha256_bytes(original_bytes_before)
        register_in_manifest(mirror_root, new_path)
        entry = {"report_id": rid, "status": "written", "path": str(new_path), "original_untouched": unchanged}
        result["refreshed"].append(entry)
        if recompile and unchanged:
            rec = trigger_recompile(rid, mirror_root=mirror_root)
            result["recompiles"].append({"report_id": rid, "returncode": rec["returncode"]})
        baseline[rid] = row["_reply_state_entry"]
    baseline.update(fresh)
    save_baseline(mirror_root, baseline, established=False)
    result["baseline_saved"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh raw snapshots whose reply activity changed (read-only CWork API; originals immutable).")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--mirror-root", default=str(MIRROR))
    parser.add_argument("--no-recompile", action="store_true", help="Write v2 raw without triggering wiki recompilation.")
    parser.add_argument("--dry-run", action="store_true", help="Compare against baseline only; write nothing.")
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required")
    result = refresh(
        app_key=args.app_key, start_date=args.start_date, end_date=args.end_date,
        mirror_root=Path(args.mirror_root), recompile=not args.no_recompile,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not any(item.get("status") == "failed" for item in result["refreshed"]) else 2)


if __name__ == "__main__":
    main()
