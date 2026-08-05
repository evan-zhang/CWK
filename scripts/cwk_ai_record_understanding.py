#!/usr/bin/env python3
"""Create one traceable AI understanding JSON for each CWK report."""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from cwk_ai_common import (
    PROJECT,
    RECORD_SCHEMA,
    env_bool,
    fallback_record,
    invoke_openclaw_json,
    load_json,
    normalize_record,
    parse_frontmatter,
    validate_record,
    sanitize_record_evidence,
    write_json,
)


def prompt_for(raw_path: Path, extracted: dict[str, Any]) -> str:
    raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
    _, body = parse_frontmatter(raw_text)
    return f"""# CWK AI record understanding

You are processing one Chinese work-collaboration report in read-only mode.
Return exactly one JSON object with schema_version `{RECORD_SCHEMA}`.
Never invent people, dates, decisions, tasks, risks, or status. Every decision,
action item and risk must contain a short evidence quote copied from the report.
The top-level evidence_refs must include this report_id and at least one quote.
Use Asia/Shanghai time in `YYYY-MM-DD HH:mm:ss`. Valid priority_hint values are
must_read, review, FYI, archive. Valid source_lane and document_type values must
follow the RT schema. Keep quotes short. Preserve source values exactly when they
are needed as evidence; do not suppress or rewrite text because it resembles a
credential, token, key, or other technical identifier.

Required JSON shape (all keys must be present):
{{
  "schema_version": "{RECORD_SCHEMA}",
  "report_id": "string",
  "title": "string",
  "writer": "string",
  "created_at_shanghai": "YYYY-MM-DD HH:mm:ss",
  "source_lane": "todo_backed|reply_chain|persistent_stream|inbox_awareness|unknown",
  "document_type": "meeting_minutes|request|daily_report|weekly_report|contract_legal|technical_plan|other",
  "event_anchor": "specific event or project name",
  "event_anchor_confidence": 0.0,
  "summary": "string",
  "background": "string",
  "decisions": [{{"text": "string", "evidence": "exact short quote"}}],
  "action_items": [{{"task": "string", "owner": null, "due_date": null, "status": "unknown", "evidence": "exact short quote"}}],
  "risks": [{{"risk": "string", "severity": "low|medium|high|unknown", "evidence": "exact short quote"}}],
  "entities": {{"people": [], "teams": [], "systems": [], "products": [], "projects": []}},
  "priority_hint": "must_read|review|FYI|archive",
  "noise_flags": [],
  "evidence_refs": [{{"report_id": "string", "quote": "exact short quote"}}]
}}

## Deterministic extraction context

{json.dumps(extracted, ensure_ascii=False)}

## Original report

{body}
"""


def repair_prompt(raw_path: Path, extracted: dict[str, Any], errors: list[str]) -> str:
    return prompt_for(raw_path, extracted) + f"""

## Contract correction

Your previous JSON failed validation: {json.dumps(errors, ensure_ascii=False)}
Return the full JSON object again. Evidence values must be literal, contiguous
substrings copied character-for-character from the Original report above. Do not
paraphrase evidence, normalize punctuation, add ellipses, or use Markdown quotes.
Omit an unsupported decision, action item, or risk instead of inventing evidence.
"""


def process_one(
    raw_path: Path,
    extracted: dict[str, Any],
    *,
    dry_run: bool,
    model: str,
    timeout_seconds: int,
    prompt_dir: Path,
) -> tuple[dict[str, Any], str | None, float]:
    started = time.monotonic()
    fallback = fallback_record(raw_path, extracted, "dry_run" if dry_run else "failed")
    report_id = fallback["report_id"]
    raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
    if dry_run:
        return fallback, None, time.monotonic() - started
    try:
        payload = invoke_openclaw_json(
            prompt_for(raw_path, extracted),
            model=model,
            stage=f"record-{report_id}",
            timeout_seconds=timeout_seconds,
            prompt_dir=prompt_dir,
        )
        payload = normalize_record(payload, fallback)
        payload = sanitize_record_evidence(payload, fallback, raw_text)
        payload["ai_status"] = "completed"
        errors = validate_record(payload, report_id, raw_text)
        if errors:
            payload = invoke_openclaw_json(
                repair_prompt(raw_path, extracted, errors),
                model=model,
                stage=f"record-{report_id}-repair",
                timeout_seconds=timeout_seconds,
                prompt_dir=prompt_dir,
            )
            payload = normalize_record(payload, fallback)
            payload = sanitize_record_evidence(payload, fallback, raw_text)
            payload["ai_status"] = "completed"
            errors = validate_record(payload, report_id, raw_text)
        if errors:
            raise ValueError("; ".join(errors))
        return payload, None, time.monotonic() - started
    except Exception as exc:  # Individual model failures intentionally degrade.
        return fallback, str(exc)[:500], time.monotonic() - started


def main() -> None:
    parser = argparse.ArgumentParser(description="Create CWK AI record-understanding artifacts.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--model", default=os.environ.get("CWK_AI_RECORD_MODEL", ""))
    parser.add_argument("--max-parallel", type=int, default=int(os.environ.get("CWK_AI_MAX_PARALLEL", "4")))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CWK_AI_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("CWK_AI_DRY_RUN"))
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    output_dir = run_dir / "ai-understanding"
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir = run_dir / ".ai-prompts"
    extracted_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "extracted").glob("*.json")):
        payload = load_json(path)
        for report_id in payload.get("source_ids", []):
            extracted_by_id[str(report_id)] = payload

    jobs = []
    for raw_path in sorted((run_dir / "raw").glob("*.md")):
        meta, _ = parse_frontmatter(raw_path.read_text(encoding="utf-8", errors="ignore"))
        report_id = str(meta.get("report_id") or raw_path.name.split("-", 1)[0])
        jobs.append((raw_path, extracted_by_id.get(report_id, {"source_ids": [report_id], "title": meta.get("title", raw_path.stem)})))
    if not jobs:
        raise SystemExit(f"no raw reports found under {run_dir / 'raw'}")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as executor:
        futures = {
            executor.submit(
                process_one,
                raw_path,
                extracted,
                dry_run=args.dry_run,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                prompt_dir=prompt_dir,
            ): raw_path
            for raw_path, extracted in jobs
        }
        for future in as_completed(futures):
            payload, error, duration = future.result()
            write_json(output_dir / f"{payload['report_id']}.json", payload)
            results.append({"report_id": payload["report_id"], "status": payload["ai_status"], "duration_seconds": round(duration, 3), "error": error})

    failures = sum(1 for item in results if item["status"] == "failed")
    completed = sum(1 for item in results if item["status"] == "completed")
    summary = {
        "schema_version": "cwk.ai_record_summary.v1",
        "model": "dry-run" if args.dry_run else args.model,
        "dry_run": args.dry_run,
        "processed_count": len(results),
        "completed_count": completed,
        "failed_count": failures,
        "degraded": failures > 0,
        "records": sorted(results, key=lambda item: item["report_id"]),
    }
    write_json(run_dir / "ai-record-summary.json", summary)
    if prompt_dir.exists() and not any(prompt_dir.iterdir()):
        prompt_dir.rmdir()
    print(json.dumps({key: summary[key] for key in ("processed_count", "completed_count", "failed_count", "degraded")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
