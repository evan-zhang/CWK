#!/usr/bin/env python3
"""Run the CWK read-only nightly pipeline and publish daily mirror outputs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"
RUNS = PROJECT / "runs"
MIRROR = PROJECT / "knowledge" / "工作协同镜像"
DEFAULT_HISTORY_RUN = os.environ.get("CWK_HISTORY_RUN_NAME", "")


SECRET_FLAGS = {"--app-key", "--api-key", "--token"}


def redact_cmd(args: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in args:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(item)
        if item in SECRET_FLAGS:
            skip_next = True
    return redacted


def run_cmd(args: list[str], dry_run: bool = False, env: dict[str, str] | None = None) -> dict:
    if dry_run:
        return {"cmd": redact_cmd(args), "returncode": 0, "stdout": "", "stderr": "", "skipped": True}
    proc = subprocess.run(args, cwd=str(PROJECT), env=env, text=True, capture_output=True)
    return {
        "cmd": redact_cmd(args),
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def require_ok(step: str, result: dict) -> None:
    if result["returncode"] != 0:
        raise SystemExit(f"{step} failed\nSTDOUT:\n{result['stdout']}\nSTDERR:\n{result['stderr']}")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_config(path: str | None) -> dict:
    if not path:
        return {}
    config_path = Path(path).expanduser().resolve()
    return json.loads(config_path.read_text(encoding="utf-8"))


def env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def config_value(args: argparse.Namespace, config: dict, name: str, default=None):
    value = getattr(args, name)
    if value not in (None, "", []):
        return value
    return config.get(name) if name in config else default


def copy_to_mirror(run_dir: Path, date: str) -> dict[str, str]:
    month = date[:7]
    daily_md = run_dir / "digest-human-v4.md"
    if not daily_md.exists():
        daily_md = run_dir / "digest-human.md"
    if not daily_md.exists():
        daily_md = run_dir / "digest.md"
    daily_html = run_dir / "digest-human-v4.html"

    outputs: dict[str, str] = {}
    daily_dir = MIRROR / "daily" / month
    daily_dir.mkdir(parents=True, exist_ok=True)
    if daily_md.exists():
        dst = daily_dir / f"{date}.md"
        shutil.copy2(daily_md, dst)
        outputs["daily_md"] = str(dst.relative_to(PROJECT))
    if daily_html.exists():
        dst = daily_dir / f"{date}.html"
        shutil.copy2(daily_html, dst)
        outputs["daily_html"] = str(dst.relative_to(PROJECT))

    run_publish_dir = MIRROR / "runs"
    run_publish_dir.mkdir(parents=True, exist_ok=True)
    for src_name, suffix in [
        ("ACCEPTANCE-RESULT.md", "acceptance"),
        ("incremental-link-preview-v1.md", "incremental-link-preview"),
    ]:
        src = run_dir / src_name
        if src.exists():
            dst = run_publish_dir / f"{date}-{run_dir.name}-{suffix}.md"
            shutil.copy2(src, dst)
            outputs[suffix] = str(dst.relative_to(PROJECT))
    return outputs


def write_manifest(run_dir: Path, manifest: dict) -> Path:
    path = run_dir / "nightly-pipeline-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CWK nightly read-only pipeline.")
    parser.add_argument("--config", default=None, help="Optional JSON config file for reusable deployments.")
    parser.add_argument("--run-name", default=datetime.now().strftime("nightly-%Y%m%d-%H%M"))
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--history-run-name", default=None)
    parser.add_argument("--detail-cap", type=int, default=None)
    parser.add_argument("--source-dir", action="append", default=[], help="Use existing raw source dir instead of collecting live CWork records.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--no-publish-mirror", action="store_true", help="Run the pipeline without copying daily/run outputs into the mirror.")
    parser.add_argument("--sync-docdb", action="store_true", help="Sync daily/ and runs/ mirror outputs to the personal knowledge base.")
    parser.add_argument("--sync-dry-run", action="store_true", help="Dry-run docdb sync even when --sync-docdb is set.")
    parser.add_argument("--docdb-project-id", default=None)
    parser.add_argument("--docdb-root-file-id", default=None)
    args = parser.parse_args()
    config = read_config(args.config)
    args.history_run_name = config_value(args, config, "history_run_name", os.environ.get("CWK_HISTORY_RUN_NAME", DEFAULT_HISTORY_RUN))
    args.detail_cap = int(config_value(args, config, "detail_cap", os.environ.get("CWK_DETAIL_CAP", 60)))
    args.app_key = config_value(args, config, "app_key", os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    args.docdb_project_id = config_value(args, config, "docdb_project_id", os.environ.get("CWK_DOCDB_PROJECT_ID"))
    args.docdb_root_file_id = config_value(args, config, "docdb_root_file_id", os.environ.get("CWK_DOCDB_ROOT_FILE_ID"))
    if not args.sync_docdb:
        args.sync_docdb = bool(config.get("sync_docdb", env_bool("CWK_SYNC_DOCDB") or False))
    if args.no_publish_mirror:
        args.sync_docdb = False

    run_dir = RUNS / args.run_name
    steps: list[dict] = []

    if args.source_dir:
        source_dirs = [Path(p).expanduser().resolve() for p in args.source_dir]
    else:
        if not args.app_key:
            raise SystemExit("CWORK_APP_KEY is required when --source-dir is not provided.")
        collect_run = f"{args.run_name}-collect"
        result = run_cmd(
            [
                sys.executable,
                str(SCRIPTS / "cwk_collect_live.py"),
                "--run-name",
                collect_run,
                "--detail-cap",
                str(args.detail_cap),
            ],
            env={**os.environ, "CWORK_APP_KEY": args.app_key},
        )
        steps.append({"step": "collect_live", **result})
        require_ok("collect_live", result)
        source_dirs = [RUNS / collect_run / "collected-raw"]

    sample_cmd = [
        sys.executable,
        str(SCRIPTS / "cwk_sample_pilot.py"),
        "--run-name",
        args.run_name,
    ]
    for source_dir in source_dirs:
        sample_cmd.extend(["--source-dir", str(source_dir)])
    result = run_cmd(sample_cmd)
    steps.append({"step": "sample_pilot", **result})
    require_ok("sample_pilot", result)

    result = run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "cwk_human_digest.py"),
            "--run-name",
            args.run_name,
            "--output",
            str(run_dir / "digest-human-v4.md"),
        ]
    )
    steps.append({"step": "human_digest", **result})
    require_ok("human_digest", result)

    result = run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "cwk_daily_html.py"),
            "--input",
            str(run_dir / "digest-human-v4.md"),
            "--output",
            str(run_dir / "digest-human-v4.html"),
        ]
    )
    steps.append({"step": "daily_html", **result})
    require_ok("daily_html", result)

    incremental_report = run_dir / "incremental-link-preview-v1.md"
    if args.history_run_name:
        result = run_cmd(
            [
                sys.executable,
                str(SCRIPTS / "cwk_incremental_link_preview.py"),
                "--run-name",
                args.run_name,
                "--history-run-name",
                args.history_run_name,
                "--incoming-count",
                "0",
                "--output",
                str(incremental_report),
            ]
        )
        steps.append({"step": "incremental_link_preview", **result})
        require_ok("incremental_link_preview", result)
    else:
        incremental_report.write_text(
            "# CWK 增量链接预演\n\n- 未配置 `history_run_name`，本轮跳过历史基线链接。\n",
            encoding="utf-8",
        )
        steps.append({"step": "incremental_link_preview", "returncode": 0, "stdout": "", "stderr": "", "skipped": True})

    mirror_outputs = {} if args.no_publish_mirror else copy_to_mirror(run_dir, args.date)

    sync_manifest = None
    if args.sync_docdb:
        sync_manifest = RUNS / f"docdb-{args.run_name}-daily-runs-sync.json"
        sync_cmd = [
            sys.executable,
            str(SCRIPTS / "cwk_sync_mirror_to_docdb.py"),
            "--only-prefix",
            "daily/",
            "--manifest",
            str(sync_manifest),
        ]
        if args.docdb_project_id:
            sync_cmd.extend(["--project-id", args.docdb_project_id])
        if args.docdb_root_file_id:
            sync_cmd.extend(["--root-file-id", args.docdb_root_file_id])
        if args.sync_dry_run:
            sync_cmd.append("--dry-run")
        result = run_cmd(sync_cmd)
        steps.append({"step": "sync_daily_docdb", **result})
        require_ok("sync_daily_docdb", result)

        sync_runs_manifest = RUNS / f"docdb-{args.run_name}-runs-sync.json"
        sync_runs_cmd = [
            sys.executable,
            str(SCRIPTS / "cwk_sync_mirror_to_docdb.py"),
            "--only-prefix",
            "runs/",
            "--manifest",
            str(sync_runs_manifest),
        ]
        if args.docdb_project_id:
            sync_runs_cmd.extend(["--project-id", args.docdb_project_id])
        if args.docdb_root_file_id:
            sync_runs_cmd.extend(["--root-file-id", args.docdb_root_file_id])
        if args.sync_dry_run:
            sync_runs_cmd.append("--dry-run")
        result = run_cmd(sync_runs_cmd)
        steps.append({"step": "sync_runs_docdb", **result})
        require_ok("sync_runs_docdb", result)

    summary = read_json(run_dir / "run.json")
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_name": args.run_name,
        "date": args.date,
        "history_run_name": args.history_run_name,
        "source_dirs": [str(p) for p in source_dirs],
        "processed_count": summary.get("processed_count"),
        "overall_pass": summary.get("overall_pass"),
        "mirror_outputs": mirror_outputs,
        "sync_manifest": str(sync_manifest.relative_to(PROJECT)) if sync_manifest else None,
        "steps": steps,
    }
    manifest_path = write_manifest(run_dir, manifest)
    print(json.dumps({k: manifest[k] for k in ["run_name", "processed_count", "overall_pass", "mirror_outputs"]}, ensure_ascii=False, indent=2))
    print(manifest_path)


if __name__ == "__main__":
    main()
