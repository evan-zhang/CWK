#!/usr/bin/env python3
"""Run the CWK read-only nightly pipeline and publish daily mirror outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from cwk_ai_common import ai_runtime_guard


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"
RUNS = PROJECT / "runs"
MIRROR = PROJECT / "knowledge" / "工作协同镜像"


def load_local_env(path: Path) -> None:
    """Load a gitignored .env without overriding an existing process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


load_local_env(PROJECT / ".env")
DEFAULT_HISTORY_RUN = os.environ.get("CWK_HISTORY_RUN_NAME", "")


SECRET_FLAGS = {"--app-key", "--api-key", "--token"}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:Bearer\s+)[A-Za-z0-9._~+/=-]{20,}\b", re.I),
)


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


def redact_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def sanitize_value(value, secrets: tuple[str, ...] = ()):
    if isinstance(value, dict):
        return {key: sanitize_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def find_publish_secrets(paths: list[Path], secrets: tuple[str, ...] = ()) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(secret and secret in text for secret in secrets) or any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(path.name)
    return findings


def require_publish_safe(paths: list[Path], secrets: tuple[str, ...] = ()) -> None:
    findings = find_publish_secrets(paths, secrets)
    if findings:
        raise RuntimeError("secret gate blocked publishable artifacts: " + ", ".join(findings))


def run_cmd(
    args: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> dict:
    started = time.monotonic()
    if dry_run:
        return {"cmd": redact_cmd(args), "returncode": 0, "stdout": "", "stderr": "", "skipped": True, "duration_seconds": 0.0}
    proc = subprocess.run(args, cwd=str(PROJECT), env=env, text=True, capture_output=True)
    return {
        "cmd": redact_cmd(args),
        "returncode": proc.returncode,
        "stdout": redact_text(proc.stdout[-4000:], secrets),
        "stderr": redact_text(proc.stderr[-4000:], secrets),
        "duration_seconds": round(time.monotonic() - started, 3),
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


def copy_to_mirror(run_dir: Path, date: str, secrets: tuple[str, ...] = ()) -> dict[str, str]:
    month = date[:7]
    daily_md = run_dir / "digest-human-v4.md"
    if not daily_md.exists():
        daily_md = run_dir / "digest-human.md"
    if not daily_md.exists():
        daily_md = run_dir / "digest.md"
    daily_html = run_dir / "digest-human-v4.html"

    publishable = [
        daily_md,
        daily_html,
        run_dir / "digest-ai-enhanced.md",
        run_dir / "digest-ai-enhanced.html",
        run_dir / "ACCEPTANCE-RESULT.md",
        run_dir / "incremental-link-preview-v1.md",
        run_dir / "quality-review.md",
    ]
    require_publish_safe(publishable, secrets)

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

    for src_name, output_key, daily_name in [
        ("digest-ai-enhanced.md", "daily_ai_md", f"{date}-ai-enhanced.md"),
        ("digest-ai-enhanced.html", "daily_ai_html", f"{date}-ai-enhanced.html"),
    ]:
        src = run_dir / src_name
        if src.exists():
            dst = daily_dir / daily_name
            shutil.copy2(src, dst)
            outputs[output_key] = str(dst.relative_to(PROJECT))

    run_publish_dir = MIRROR / "runs"
    run_publish_dir.mkdir(parents=True, exist_ok=True)
    for src_name, suffix in [
        ("ACCEPTANCE-RESULT.md", "acceptance"),
        ("incremental-link-preview-v1.md", "incremental-link-preview"),
        ("quality-review.md", "ai-quality-review"),
    ]:
        src = run_dir / src_name
        if src.exists():
            dst = run_publish_dir / f"{date}-{run_dir.name}-{suffix}.md"
            shutil.copy2(src, dst)
            outputs[suffix] = str(dst.relative_to(PROJECT))
    return outputs


def relative_outputs(run_dir: Path, names: list[str]) -> dict[str, str]:
    outputs = {}
    for name in names:
        path = run_dir / name
        if path.exists():
            outputs[name] = str(path.relative_to(PROJECT))
    return outputs


def run_ai_stages(args: argparse.Namespace, run_dir: Path, steps: list[dict]) -> dict:
    ai = {
        "enabled": True,
        "dry_run": args.ai_dry_run,
        "degraded": False,
        "models": {
            "record": "dry-run" if args.ai_dry_run else args.ai_record_model,
            "cluster": "dry-run" if args.ai_dry_run else args.ai_cluster_model,
            "quality": "dry-run" if args.ai_dry_run else args.ai_quality_model,
        },
        "stages": {},
        "outputs": {},
    }

    def execute(stage: str, command: list[str]) -> bool:
        result = run_cmd(command)
        steps.append({"step": stage, **result})
        ai["stages"][stage] = {
            "status": "completed" if result["returncode"] == 0 else "failed",
            "returncode": result["returncode"],
            "duration_seconds": result["duration_seconds"],
        }
        if result["returncode"] != 0:
            ai["degraded"] = True
            ai["stages"][stage]["error"] = (result["stderr"] or result["stdout"])[-1000:]
            return False
        return True

    common = ["--timeout-seconds", str(args.ai_timeout_seconds)]
    dry_run = ["--dry-run"] if args.ai_dry_run else []
    record_ok = execute(
        "ai_record_understanding",
        [
            sys.executable,
            str(SCRIPTS / "cwk_ai_record_understanding.py"),
            "--run-name",
            args.run_name,
            "--model",
            args.ai_record_model,
            "--max-parallel",
            str(args.ai_max_parallel),
            *common,
            *dry_run,
        ],
    )
    record_summary = run_dir / "ai-record-summary.json"
    if record_summary.exists():
        summary = read_json(record_summary)
        ai["stages"]["ai_record_understanding"].update(
            {
                "processed_count": summary.get("processed_count"),
                "failed_count": summary.get("failed_count"),
                "skipped_sensitive_count": summary.get("skipped_sensitive_count", 0),
            }
        )
        ai["degraded"] = ai["degraded"] or bool(summary.get("degraded"))
    if not record_ok:
        ai["outputs"] = relative_outputs(run_dir, ["ai-record-summary.json"])
        return ai

    cluster_cmd = [
        sys.executable,
        str(SCRIPTS / "cwk_ai_event_clustering.py"),
        "--run-name",
        args.run_name,
        "--model",
        args.ai_cluster_model,
        *common,
        *dry_run,
    ]
    if args.history_run_name:
        cluster_cmd.extend(["--history-run-name", args.history_run_name])
    if not execute("ai_event_clustering", cluster_cmd):
        ai["outputs"] = relative_outputs(run_dir, ["ai-record-summary.json"])
        return ai

    if not execute(
        "ai_enhanced_digest",
        [
            sys.executable,
            str(SCRIPTS / "cwk_ai_enhanced_digest.py"),
            "--run-name",
            args.run_name,
            "--output",
            str(run_dir / "digest-ai-enhanced.md"),
        ],
    ):
        ai["outputs"] = relative_outputs(run_dir, ["ai-events.json", "ai-daily-priorities.json"])
        return ai

    if not execute(
        "ai_enhanced_html",
        [
            sys.executable,
            str(SCRIPTS / "cwk_daily_html.py"),
            "--input",
            str(run_dir / "digest-ai-enhanced.md"),
            "--output",
            str(run_dir / "digest-ai-enhanced.html"),
        ],
    ):
        ai["outputs"] = relative_outputs(run_dir, ["digest-ai-enhanced.md"])
        return ai

    execute(
        "ai_quality_review",
        [
            sys.executable,
            str(SCRIPTS / "cwk_ai_quality_review.py"),
            "--run-name",
            args.run_name,
            "--model",
            args.ai_quality_model,
            *common,
            *dry_run,
        ],
    )
    ai["outputs"] = relative_outputs(
        run_dir,
        [
            "ai-record-summary.json",
            "ai-clustering-summary.json",
            "ai-events.json",
            "ai-daily-priorities.json",
            "digest-ai-enhanced.md",
            "digest-ai-enhanced.html",
            "quality-review.json",
            "quality-review.md",
        ],
    )
    return ai


def write_manifest(run_dir: Path, manifest: dict, secrets: tuple[str, ...] = ()) -> Path:
    path = run_dir / "nightly-pipeline-manifest.json"
    path.write_text(json.dumps(sanitize_value(manifest, secrets), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CWK nightly read-only pipeline.")
    parser.add_argument("--config", default=None, help="Optional JSON config file for reusable deployments.")
    parser.add_argument("--run-name", default=datetime.now().strftime("nightly-%Y%m%d-%H%M"))
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--history-run-name", default=None)
    parser.add_argument("--detail-cap", type=int, default=None)
    parser.add_argument("--continuation-cap", type=int, default=None)
    parser.add_argument("--backfill-enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--backfill-cap", type=int, default=None)
    parser.add_argument("--backfill-page-size", type=int, default=None)
    parser.add_argument("--collection-state-file", default=None)
    parser.add_argument("--source-dir", action="append", default=[], help="Use existing raw source dir instead of collecting live CWork records.")
    parser.add_argument("--app-key", default=os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    parser.add_argument("--no-publish-mirror", action="store_true", help="Run the pipeline without copying daily/run outputs into the mirror.")
    parser.add_argument("--sync-docdb", action="store_true", help="Sync daily/ and runs/ mirror outputs to the personal knowledge base.")
    parser.add_argument("--sync-dry-run", action="store_true", help="Dry-run docdb sync even when --sync-docdb is set.")
    parser.add_argument("--docdb-project-id", default=None)
    parser.add_argument("--docdb-root-file-id", default=None)
    parser.add_argument("--ai-enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--ai-dry-run", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--ai-record-model", default=os.environ.get("CWK_AI_RECORD_MODEL"))
    parser.add_argument("--ai-cluster-model", default=os.environ.get("CWK_AI_CLUSTER_MODEL"))
    parser.add_argument("--ai-quality-model", default=os.environ.get("CWK_AI_QUALITY_MODEL"))
    parser.add_argument("--ai-max-parallel", type=int, default=int(os.environ["CWK_AI_MAX_PARALLEL"]) if os.environ.get("CWK_AI_MAX_PARALLEL") else None)
    parser.add_argument("--ai-timeout-seconds", type=int, default=int(os.environ["CWK_AI_TIMEOUT_SECONDS"]) if os.environ.get("CWK_AI_TIMEOUT_SECONDS") else None)
    args = parser.parse_args()
    config = read_config(args.config)
    args.history_run_name = config_value(args, config, "history_run_name", os.environ.get("CWK_HISTORY_RUN_NAME", DEFAULT_HISTORY_RUN))
    args.detail_cap = int(config_value(args, config, "detail_cap", os.environ.get("CWK_DETAIL_CAP", 60)))
    args.continuation_cap = int(config_value(args, config, "continuation_cap", os.environ.get("CWK_CONTINUATION_CAP", 15)))
    if args.backfill_enabled is None:
        env_backfill = env_bool("CWK_BACKFILL_ENABLED")
        args.backfill_enabled = env_backfill if env_backfill is not None else bool(config.get("backfill_enabled", True))
    args.backfill_cap = int(config_value(args, config, "backfill_cap", os.environ.get("CWK_BACKFILL_CAP", 20)))
    args.backfill_page_size = int(config_value(args, config, "backfill_page_size", os.environ.get("CWK_BACKFILL_PAGE_SIZE", 20)))
    args.collection_state_file = config_value(args, config, "collection_state_file", os.environ.get("CWK_COLLECTION_STATE_FILE", str(PROJECT / "state" / "collection-state.json")))
    args.app_key = config_value(args, config, "app_key", os.environ.get("CWORK_APP_KEY") or os.environ.get("XG_BIZ_API_KEY") or "")
    args.docdb_project_id = config_value(args, config, "docdb_project_id", os.environ.get("CWK_DOCDB_PROJECT_ID"))
    args.docdb_root_file_id = config_value(args, config, "docdb_root_file_id", os.environ.get("CWK_DOCDB_ROOT_FILE_ID"))
    if not args.sync_docdb:
        args.sync_docdb = bool(config.get("sync_docdb", env_bool("CWK_SYNC_DOCDB") or False))
    if args.no_publish_mirror:
        args.sync_docdb = False
    if args.ai_enabled is None:
        env_ai_enabled = env_bool("CWK_AI_ENABLED")
        args.ai_enabled = env_ai_enabled if env_ai_enabled is not None else bool(config.get("ai_enabled", False))
    if args.ai_dry_run is None:
        env_ai_dry_run = env_bool("CWK_AI_DRY_RUN")
        args.ai_dry_run = env_ai_dry_run if env_ai_dry_run is not None else bool(config.get("ai_dry_run", False))
    args.ai_record_model = config_value(args, config, "ai_record_model", os.environ.get("CWK_AI_RECORD_MODEL", ""))
    args.ai_cluster_model = config_value(args, config, "ai_cluster_model", os.environ.get("CWK_AI_CLUSTER_MODEL", ""))
    args.ai_quality_model = config_value(args, config, "ai_quality_model", os.environ.get("CWK_AI_QUALITY_MODEL", ""))
    args.ai_max_parallel = int(config_value(args, config, "ai_max_parallel", os.environ.get("CWK_AI_MAX_PARALLEL", 4)))
    args.ai_timeout_seconds = int(config_value(args, config, "ai_timeout_seconds", os.environ.get("CWK_AI_TIMEOUT_SECONDS", 120)))

    run_dir = RUNS / args.run_name
    steps: list[dict] = []
    collection_manifest = None

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
                "--continuation-cap",
                str(args.continuation_cap),
                "--backfill-cap",
                str(args.backfill_cap),
                "--backfill-page-size",
                str(args.backfill_page_size),
                "--state-file",
                str(args.collection_state_file),
                "--backfill-enabled" if args.backfill_enabled else "--no-backfill-enabled",
            ],
            env={**os.environ, "CWORK_APP_KEY": args.app_key},
            secrets=(args.app_key,),
        )
        steps.append({"step": "collect_live", **result})
        require_ok("collect_live", result)
        source_dirs = [RUNS / collect_run / "collected-raw"]
        collection_manifest_path = RUNS / collect_run / "collect-manifest.json"
        if collection_manifest_path.exists():
            collection_manifest = read_json(collection_manifest_path)

    sample_cmd = [
        sys.executable,
        str(SCRIPTS / "cwk_sample_pilot.py"),
        "--run-name",
        args.run_name,
    ]
    for source_dir in source_dirs:
        sample_cmd.extend(["--source-dir", str(source_dir)])
    if collection_manifest is not None:
        sample_cmd.extend(["--acceptance-profile", "incremental"])
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

    safe_materialize_manifest = None
    safe_materialize_manifest_path = None
    if not args.no_publish_mirror:
        result = run_cmd(
            [
                sys.executable,
                str(SCRIPTS / "cwk_materialize_safe.py"),
                "--run-name",
                args.run_name,
            ]
        )
        steps.append({"step": "safe_materialize_knowledge", **result})
        require_ok("safe_materialize_knowledge", result)
        candidate_manifest = run_dir / "safe-materialize-manifest.json"
        if candidate_manifest.exists():
            safe_materialize_manifest_path = candidate_manifest
            safe_materialize_manifest = read_json(candidate_manifest)
    else:
        steps.append({"step": "safe_materialize_knowledge", "returncode": 0, "stdout": "", "stderr": "", "skipped": True})

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

    if args.ai_enabled:
        with ai_runtime_guard():
            ai_manifest = run_ai_stages(args, run_dir, steps)
    else:
        ai_manifest = {"enabled": False, "dry_run": False, "degraded": False, "models": {}, "stages": {}, "outputs": {}}

    mirror_outputs = {} if args.no_publish_mirror else copy_to_mirror(run_dir, args.date, (args.app_key,))

    sync_manifest = None
    structured_sync_manifests: list[str] = []
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

        for prefix in ("history/", "events/", "entities/", "_index/"):
            label = prefix.strip("/").replace("/", "-")
            structured_manifest = RUNS / f"docdb-{args.run_name}-{label}-sync.json"
            structured_cmd = [
                sys.executable,
                str(SCRIPTS / "cwk_sync_mirror_to_docdb.py"),
                "--only-prefix",
                prefix,
                "--manifest",
                str(structured_manifest),
            ]
            if safe_materialize_manifest_path:
                structured_cmd.extend(["--paths-manifest", str(safe_materialize_manifest_path)])
            if args.docdb_project_id:
                structured_cmd.extend(["--project-id", args.docdb_project_id])
            if args.docdb_root_file_id:
                structured_cmd.extend(["--root-file-id", args.docdb_root_file_id])
            if args.sync_dry_run:
                structured_cmd.append("--dry-run")
            result = run_cmd(structured_cmd)
            steps.append({"step": f"sync_{label}_docdb", **result})
            require_ok(f"sync_{label}_docdb", result)
            structured_sync_manifests.append(str(structured_manifest.relative_to(PROJECT)))

    summary = read_json(run_dir / "run.json")
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_name": args.run_name,
        "date": args.date,
        "history_run_name": args.history_run_name,
        "source_dirs": [str(p) for p in source_dirs],
        "processed_count": summary.get("processed_count"),
        "collection": {
            key: collection_manifest.get(key)
            for key in (
                "candidate_count",
                "selected_daily_count",
                "selected_backfill_count",
                "selected_change_counts",
                "candidate_delta_counts",
                "pending_count",
                "backfill_run",
            )
        } if collection_manifest else None,
        "safe_materialize": safe_materialize_manifest,
        "overall_pass": summary.get("overall_pass"),
        "degraded": bool(ai_manifest.get("degraded")),
        "ai": ai_manifest,
        "mirror_outputs": mirror_outputs,
        "sync_manifest": str(sync_manifest.relative_to(PROJECT)) if sync_manifest else None,
        "structured_sync_manifests": structured_sync_manifests,
        "steps": steps,
    }
    manifest_path = write_manifest(run_dir, manifest, (args.app_key,))
    print(json.dumps({k: manifest[k] for k in ["run_name", "processed_count", "overall_pass", "mirror_outputs"]}, ensure_ascii=False, indent=2))
    print(manifest_path)


if __name__ == "__main__":
    main()
