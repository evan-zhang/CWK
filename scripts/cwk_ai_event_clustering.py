#!/usr/bin/env python3
"""Cluster traceable CWK AI records into management events and priorities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from cwk_ai_common import (
    EVENTS_SCHEMA,
    PRIORITIES_SCHEMA,
    PROJECT,
    env_bool,
    invoke_openclaw_json,
    load_json,
    validate_events,
    validate_priorities,
    write_json,
)


def stable_id(anchor: str) -> str:
    return "event-" + hashlib.sha1(anchor.encode("utf-8")).hexdigest()[:12]


def normalized_anchor(record: dict[str, Any]) -> str:
    value = record.get("event_anchor") or record.get("title") or record["report_id"]
    value = re.sub(r"20\d{2}[-年./]\d{1,2}(?:[-月./]\d{1,2}日?)?", "", value)
    value = re.sub(r"第?[一二三四五六七八九十0-9]+(?:周|期)", "", value)
    value = re.sub(r"[\s【】()（）_\-—:：]+", "", value)
    return value[:40] or record["report_id"]


def dedupe_dicts(items: list[dict[str, Any]], text_key: str) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        value = item.get(text_key)
        if value and value not in seen:
            seen.add(value)
            result.append(item)
    return result


def dry_run_cluster(records: list[dict[str, Any]], run_name: str, history_titles: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[normalized_anchor(record)].append(record)

    events = []
    for anchor, members in grouped.items():
        record_ids = [str(item["report_id"]) for item in members]
        decisions = dedupe_dicts([value for item in members for value in item.get("decisions", [])], "text")
        actions = dedupe_dicts([value for item in members for value in item.get("action_items", [])], "task")
        risks = dedupe_dicts([value for item in members for value in item.get("risks", [])], "risk")
        must_read = any(item.get("priority_hint") == "must_read" for item in members)
        review = any(item.get("priority_hint") == "review" for item in members)
        matched = anchor in history_titles
        priority = "P0" if any(risk.get("severity") == "high" for risk in risks) else "P1" if must_read else "P2" if review else "FYI"
        events.append(
            {
                "event_id": stable_id(anchor),
                "event_title": members[0].get("event_anchor") or members[0].get("title"),
                "event_type": members[0].get("document_type", "other"),
                "status": "continuing" if matched else "new",
                "priority": priority,
                "record_ids": record_ids,
                "history_match": {
                    "matched": matched,
                    "history_event": anchor if matched else "",
                    "confidence": 0.7 if matched else 0.0,
                    "reason": "normalized event anchor matched history" if matched else "no history match",
                },
                "merged_summary": "；".join(dict.fromkeys(item.get("summary", "") for item in members if item.get("summary")))[:500],
                "decisions": decisions[:8],
                "action_items": actions[:10],
                "risks": risks[:8],
                "why_it_matters": "存在明确行动或风险，需要管理关注。" if actions or risks else "本轮新增工作动态。",
            }
        )
    rank = {"P0": 0, "P1": 1, "P2": 2, "FYI": 3}
    events.sort(key=lambda item: (rank[item["priority"]], item["status"] == "continuing", item["event_title"]))
    events_payload = {"schema_version": EVENTS_SCHEMA, "run_name": run_name, "events": events}
    priorities = []
    for event in events[:10]:
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "event_id": event["event_id"],
                "title": event["event_title"],
                "priority": event["priority"],
                "status": event["status"],
                "summary": event["merged_summary"],
                "why_it_matters": event["why_it_matters"],
                "record_ids": event["record_ids"],
            }
        )
    priorities_payload = {"schema_version": PRIORITIES_SCHEMA, "run_name": run_name, "priorities": priorities}
    return events_payload, priorities_payload


def prompt_for(records: list[dict[str, Any]], run_name: str, history: dict[str, Any] | None) -> str:
    return f"""# CWK AI event clustering

Cluster the Chinese work-report records below into management events. Return one JSON object:
{{
  "schema_version": "cwk.ai_clustering_bundle.v1",
  "events": {{"schema_version": "{EVENTS_SCHEMA}", "run_name": "{run_name}", "events": []}},
  "priorities": {{"schema_version": "{PRIORITIES_SCHEMA}", "run_name": "{run_name}", "priorities": []}}
}}

Rules:
- Merge only records that clearly describe the same event; avoid generic anchors.
- Every event and priority must contain non-empty record_ids copied from input.
- Never add a report_id that is not in input.
- Preserve evidence-bearing decisions, action_items and risks from records.
- Mark repeated history as continuing and rank real changes above unchanged repetition.
- Use P0, P1, P2, FYI. Return at most 10 priorities.
- Do not invent facts. Return JSON only.
- Use the exact field names below. Do not substitute title for event_title,
  summary for merged_summary, level for priority, or related_event_id for event_id.

Each event must contain:
event_id, event_title, event_type, status (new|continuing|updated|blocked|closed|unknown),
priority (P0|P1|P2|FYI), record_ids, history_match, merged_summary, decisions,
action_items, risks, why_it_matters.

Each priority must contain:
rank, event_id, title, priority (P0|P1|P2|FYI), status, summary,
why_it_matters, record_ids.

Current records:
{json.dumps(records, ensure_ascii=False)}

Optional history events:
{json.dumps(history or {}, ensure_ascii=False)}
"""


def normalize_bundle(bundle: dict[str, Any], run_name: str, records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_events = bundle.get("events", {})
    raw_priorities = bundle.get("priorities", {})
    events_list = raw_events.get("events", []) if isinstance(raw_events, dict) else []
    priorities_list = raw_priorities.get("priorities", []) if isinstance(raw_priorities, dict) else []
    priority_by_event = {
        item.get("event_id") or item.get("related_event_id"): item
        for item in priorities_list
        if isinstance(item, dict)
    }
    normalized_events = []
    record_by_id = {str(item["report_id"]): item for item in records}
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "FYI": 3}
    valid_statuses = {"new", "continuing", "updated", "blocked", "closed", "unknown"}
    valid_priorities = {"P0", "P1", "P2", "FYI"}
    for index, item in enumerate(events_list):
        event_id = item.get("event_id") or f"event-{index + 1}"
        linked_priority = priority_by_event.get(event_id, {})
        status = item.get("status")
        if status not in valid_statuses:
            status = item.get("continuity") if item.get("continuity") in valid_statuses else "unknown"
        priority = item.get("priority") or linked_priority.get("priority") or linked_priority.get("level") or "P2"
        if priority not in valid_priorities:
            priority = "P2"
        source_records = [record_by_id[report_id] for report_id in map(str, item.get("record_ids", [])) if report_id in record_by_id]
        has_high_risk = any(risk.get("severity") == "high" for record in source_records for risk in record.get("risks", []) if isinstance(risk, dict))
        hints = {record.get("priority_hint") for record in source_records}
        max_priority = "P0" if has_high_risk else "P1" if "must_read" in hints else "P2" if "review" in hints else "FYI"
        if priority_rank[priority] < priority_rank[max_priority]:
            priority = max_priority
        summary = item.get("merged_summary") or item.get("summary") or ""
        why = item.get("why_it_matters") or linked_priority.get("why_it_matters") or linked_priority.get("reason") or "需结合原文判断。"
        normalized_events.append(
            {
                **item,
                "event_id": event_id,
                "event_title": item.get("event_title") or item.get("title") or item.get("event_anchor") or "未命名事项",
                "event_type": item.get("event_type") or "other",
                "status": status,
                "priority": priority,
                "record_ids": item.get("record_ids", []),
                "history_match": item.get("history_match") or {"matched": status == "continuing", "history_event": "", "confidence": 0.0, "reason": "not provided"},
                "merged_summary": summary,
                "decisions": item.get("decisions", []),
                "action_items": item.get("action_items", []),
                "risks": item.get("risks", []),
                "why_it_matters": why,
            }
        )
    event_by_id = {item["event_id"]: item for item in normalized_events}
    normalized_priorities = []
    for index, item in enumerate(priorities_list):
        event_id = item.get("event_id") or item.get("related_event_id") or ""
        event = event_by_id.get(event_id, {})
        priority = item.get("priority") or item.get("level") or event.get("priority") or "P2"
        if priority not in valid_priorities:
            priority = "P2"
        event_priority = event.get("priority") or "P2"
        if priority_rank[priority] < priority_rank[event_priority]:
            priority = event_priority
        normalized_priorities.append(
            {
                **item,
                "rank": item.get("rank") or index + 1,
                "event_id": event_id,
                "title": item.get("title") or event.get("event_title") or "未命名事项",
                "priority": priority,
                "status": item.get("status") or event.get("status") or "unknown",
                "summary": item.get("summary") or event.get("merged_summary") or "",
                "why_it_matters": item.get("why_it_matters") or item.get("reason") or event.get("why_it_matters") or "需结合原文判断。",
                "record_ids": item.get("record_ids") or event.get("record_ids") or [],
            }
        )
    return (
        {"schema_version": EVENTS_SCHEMA, "run_name": run_name, "events": normalized_events},
        {"schema_version": PRIORITIES_SCHEMA, "run_name": run_name, "priorities": normalized_priorities},
    )


def validate_cluster_evidence(events: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    record_by_id = {str(item["report_id"]): item for item in records}
    errors = []
    for index, event in enumerate(events.get("events", [])):
        allowed = set()
        for report_id in map(str, event.get("record_ids", [])):
            record = record_by_id.get(report_id, {})
            for ref in record.get("evidence_refs", []):
                if isinstance(ref, dict) and ref.get("quote"):
                    allowed.add(ref["quote"])
            for collection in ("decisions", "action_items", "risks"):
                for item in record.get(collection, []):
                    if isinstance(item, dict) and item.get("evidence"):
                        allowed.add(item["evidence"])
        for collection in ("decisions", "action_items", "risks"):
            for item in event.get(collection, []):
                evidence = item.get("evidence") if isinstance(item, dict) else None
                if not evidence or evidence not in allowed:
                    errors.append(f"event {index} {collection} contains untraceable evidence")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster CWK AI record-understanding artifacts.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--history-run-name", default=os.environ.get("CWK_HISTORY_RUN_NAME", ""))
    parser.add_argument("--model", default=os.environ.get("CWK_AI_CLUSTER_MODEL", ""))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CWK_AI_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("CWK_AI_DRY_RUN"))
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    records = [load_json(path) for path in sorted((run_dir / "ai-understanding").glob("*.json"))]
    if not records:
        raise SystemExit("no AI understanding records found")
    valid_ids = {str(item["report_id"]) for item in records}
    history = None
    history_titles: set[str] = set()
    if args.history_run_name:
        history_path = PROJECT / "runs" / args.history_run_name / "ai-events.json"
        if history_path.exists():
            history = load_json(history_path)
            history_titles = {normalized_anchor({"report_id": event.get("event_id", ""), "event_anchor": event.get("event_title", "")}) for event in history.get("events", [])}

    started = time.monotonic()
    if args.dry_run:
        events, priorities = dry_run_cluster(records, args.run_name, history_titles)
        model_label = "dry-run"
    else:
        bundle = invoke_openclaw_json(
            prompt_for(records, args.run_name, history),
            model=args.model,
            stage="event-clustering",
            timeout_seconds=args.timeout_seconds,
            prompt_dir=run_dir / ".ai-prompts",
        )
        events, priorities = normalize_bundle(bundle, args.run_name, records)
        model_label = args.model
    errors = validate_events(events, valid_ids) + validate_priorities(priorities, valid_ids) + validate_cluster_evidence(events, records)
    event_ids = {item.get("event_id") for item in events.get("events", [])}
    for index, item in enumerate(priorities.get("priorities", [])):
        if item.get("event_id") not in event_ids:
            errors.append(f"priority {index} references an unknown event_id")
    if errors:
        raise SystemExit("invalid clustering output: " + "; ".join(errors))
    write_json(run_dir / "ai-events.json", events)
    write_json(run_dir / "ai-daily-priorities.json", priorities)
    summary = {
        "schema_version": "cwk.ai_clustering_summary.v1",
        "model": model_label,
        "dry_run": args.dry_run,
        "duration_seconds": round(time.monotonic() - started, 3),
        "event_count": len(events["events"]),
        "priority_count": len(priorities["priorities"]),
        "degraded": any(item.get("ai_status") == "failed" for item in records),
    }
    write_json(run_dir / "ai-clustering-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
