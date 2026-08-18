#!/usr/bin/env python3
"""Run one read-only high-frequency CWork thread-evidence polling cycle.

This intentionally does not enable its own scheduler. Operators can verify a
manual run first, then use the RT-007 cron template after deciding capacity
and the desired freshness SLA.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"


def run(args: list[str], *, app_key: str) -> dict:
    proc = subprocess.run(args, cwd=str(PROJECT), text=True, capture_output=True, env={**os.environ, "CWORK_APP_KEY": app_key})
    return {"cmd": ["<python>", *args[1:]], "returncode": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only five-minute CWork reply/approval watcher.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--run-name", default=f"thread-watch-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--mirror-root", default=str(PROJECT / "knowledge" / "工作协同镜像"))
    parser.add_argument("--detail-cap", type=int, default=120)
    parser.add_argument("--state-file", default=str(PROJECT / "state" / "collection-state.json"))
    args = parser.parse_args()
    if not args.app_key:
        raise SystemExit("CWORK_APP_KEY is required")

    collect_run = f"{args.run_name}-collect"
    collect = run([
        sys.executable, str(SCRIPTS / "cwk_collect_live.py"),
        "--run-name", collect_run,
        "--state-file", args.state_file,
        "--detail-cap", str(args.detail_cap),
        "--no-backfill-enabled",
    ], app_key=args.app_key)
    if collect["returncode"] != 0:
        print(json.dumps({"run_name": args.run_name, "stage": "collect", **collect}, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    raw_dir = PROJECT / "runs" / collect_run / "collected-raw"
    promotion_path = PROJECT / "runs" / args.run_name / "raw-promotion-manifest.json"
    promote = run([
        sys.executable, str(SCRIPTS / "cwk_raw_store.py"),
        "--mirror-root", args.mirror_root,
        "--source-dir", str(raw_dir),
        "--manifest-out", str(promotion_path),
    ], app_key=args.app_key)
    if promote["returncode"] != 0:
        print(json.dumps({"run_name": args.run_name, "stage": "promote", **promote}, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    audit_path = PROJECT / "runs" / args.run_name / "thread-timeline-audit.json"
    audit = run([
        sys.executable, str(SCRIPTS / "cwk_thread_timeline_audit.py"),
        "--mirror-root", args.mirror_root,
        "--paths-manifest", str(promotion_path),
        "--output", str(audit_path),
    ], app_key=args.app_key)
    print(json.dumps({"run_name": args.run_name, "mode": "read-only-thread-watch", "collect": collect, "promote": promote, "audit": audit, "mutating_cwork_commands_called": []}, ensure_ascii=False, indent=2))
    if audit["returncode"] != 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
