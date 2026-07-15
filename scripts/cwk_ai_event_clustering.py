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
- For every decision, action_item and risk, copy its evidence value exactly from
  one of the linked input records. Do not paraphrase evidence. Omit the item if
  no exact evidence value supports it.
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


def repair_prompt(records: list[dict[str, Any]], run_name: str, history: dict[str, Any] | None, errors: list[str]) -> str:
    return prompt_for(records, run_name, history) + f"""

## Contract correction

Your previous JSON failed validation: {json.dumps(errors, ensure_ascii=False)}
Return the complete clustering bundle again. Every decisions/action_items/risks
evidence value must be copied character-for-character from a linked input
record. Do not paraphrase or invent evidence. Omit unsupported items. JSON only.
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
        history_match = item.get("history_match")
        if not isinstance(history_match, dict):
            history_match = {"matched": status == "continuing", "history_event": "", "confidence": 0.0, "reason": "not provided"}
        normalized_events.append(
            {
                **item,
                "event_id": event_id,
                "event_title": item.get("event_title") or item.get("title") or item.get("event_anchor") or "未命名事项",
                "event_type": item.get("event_type") or "other",
                "status": status,
                "priority": priority,
                "record_ids": item.get("record_ids", []),
                "history_match": history_match,
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


def validate_event_coverage(events: dict[str, Any], valid_ids: set[str], minimum: float = 0.95) -> list[str]:
    if not valid_ids:
        return ["no eligible records for clustering"]
    event_list = events.get("events", [])
    if not event_list:
        return ["events must not be empty for a non-empty record set"]
    covered = {str(report_id) for event in event_list for report_id in event.get("record_ids", [])}
    ratio = len(covered & valid_ids) / len(valid_ids)
    if ratio < minimum:
        return [f"event record coverage too low: actual={ratio:.3f}, minimum={minimum:.3f}"]
    return []


def merge_event_batches(batch_events: list[dict[str, Any]], run_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in batch_events:
        key = normalized_anchor({"report_id": event.get("event_id", "unknown"), "event_anchor": event.get("event_title", "")})
        grouped[key].append(event)

    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "FYI": 3}
    status_rank = {"blocked": 0, "updated": 1, "continuing": 2, "new": 3, "unknown": 4, "closed": 5}
    merged = []
    for anchor, members in grouped.items():
        record_ids = sorted({str(report_id) for member in members for report_id in member.get("record_ids", [])})
        priority = min((member.get("priority", "P2") for member in members), key=lambda value: priority_rank.get(value, 2))
        status = min((member.get("status", "unknown") for member in members), key=lambda value: status_rank.get(value, 4))
        if priority == "P0" and status != "blocked":
            priority = "P1"
        summaries = list(dict.fromkeys(member.get("merged_summary", "") for member in members if member.get("merged_summary")))
        reasons = list(dict.fromkeys(member.get("why_it_matters", "") for member in members if member.get("why_it_matters")))
        history_candidates = [member.get("history_match") for member in members if isinstance(member.get("history_match"), dict)]
        matched_history = next((value for value in history_candidates if value.get("matched")), history_candidates[0] if history_candidates else {})
        merged.append(
            {
                "event_id": stable_id(anchor),
                "event_title": members[0].get("event_title") or anchor,
                "event_type": members[0].get("event_type", "other"),
                "status": status,
                "priority": priority,
                "record_ids": record_ids,
                "history_match": matched_history,
                "merged_summary": "；".join(summaries)[:800],
                "decisions": dedupe_dicts([item for member in members for item in member.get("decisions", [])], "text")[:8],
                "action_items": dedupe_dicts([item for member in members for item in member.get("action_items", [])], "task")[:10],
                "risks": dedupe_dicts([item for member in members for item in member.get("risks", [])], "risk")[:8],
                "why_it_matters": "；".join(reasons)[:500] or "需结合原文判断。",
            }
        )
    merged.sort(key=lambda item: (priority_rank[item["priority"]], status_rank[item["status"]], item["event_title"]))
    selected = []
    topic_counts: dict[str, int] = defaultdict(int)
    for event in merged:
        title = event["event_title"]
        topic = "云端虾" if "云端虾" in title or "云龙虾" in title else title
        if topic_counts[topic] >= 2:
            continue
        topic_counts[topic] += 1
        selected.append(event)
        if len(selected) == 10:
            break
    priorities = [
        {
            "rank": index + 1,
            "event_id": event["event_id"],
            "title": event["event_title"],
            "priority": event["priority"],
            "status": event["status"],
            "summary": event["merged_summary"],
            "why_it_matters": event["why_it_matters"],
            "record_ids": event["record_ids"],
        }
        for index, event in enumerate(selected)
    ]
    return (
        {"schema_version": EVENTS_SCHEMA, "run_name": run_name, "events": merged},
        {"schema_version": PRIORITIES_SCHEMA, "run_name": run_name, "priorities": priorities},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster CWK AI record-understanding artifacts.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--history-run-name", default=os.environ.get("CWK_HISTORY_RUN_NAME", ""))
    parser.add_argument("--model", default=os.environ.get("CWK_AI_CLUSTER_MODEL", ""))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CWK_AI_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("CWK_AI_CLUSTER_BATCH_SIZE", "10")))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("CWK_AI_DRY_RUN"))
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    records = [
        record
        for path in sorted((run_dir / "ai-understanding").glob("*.json"))
        if (record := load_json(path)).get("ai_status") != "skipped_sensitive"
    ]
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
        batch_outputs = []
        batch_size = max(1, args.batch_size)
        checkpoint_dir = run_dir / "ai-clustering-batches"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            batch_number = start // batch_size + 1
            batch_ids = {str(item["report_id"]) for item in batch}
            checkpoint_path = checkpoint_dir / f"batch-{batch_number:03d}.json"
            checkpoint = load_json(checkpoint_path) if checkpoint_path.exists() else {}
            expected_ids = sorted(batch_ids)
            if checkpoint.get("record_ids") == expected_ids and checkpoint.get("model") == args.model:
                batch_events = checkpoint.get("events", {})
                batch_priorities = checkpoint.get("priorities", {})
            else:
                bundle = invoke_openclaw_json(
                    prompt_for(batch, args.run_name, history),
                    model=args.model,
                    stage=f"event-clustering-{batch_number}",
                    timeout_seconds=args.timeout_seconds,
                    prompt_dir=run_dir / ".ai-prompts",
                )
                batch_events, batch_priorities = normalize_bundle(bundle, args.run_name, batch)
            batch_errors = (
                validate_events(batch_events, batch_ids)
                + validate_priorities(batch_priorities, batch_ids)
                + validate_cluster_evidence(batch_events, batch)
                + validate_event_coverage(batch_events, batch_ids, 1.0)
            )
            if batch_errors:
                bundle = invoke_openclaw_json(
                    repair_prompt(batch, args.run_name, history, batch_errors),
                    model=args.model,
                    stage=f"event-clustering-{batch_number}-repair",
                    timeout_seconds=args.timeout_seconds,
                    prompt_dir=run_dir / ".ai-prompts",
                )
                batch_events, batch_priorities = normalize_bundle(bundle, args.run_name, batch)
                batch_errors = (
                    validate_events(batch_events, batch_ids)
                    + validate_priorities(batch_priorities, batch_ids)
                    + validate_cluster_evidence(batch_events, batch)
                    + validate_event_coverage(batch_events, batch_ids, 1.0)
                )
            if batch_errors:
                raise SystemExit(f"invalid clustering batch {batch_number}: " + "; ".join(batch_errors))
            write_json(
                checkpoint_path,
                {
                    "schema_version": "cwk.ai_clustering_batch.v1",
                    "model": args.model,
                    "record_ids": expected_ids,
                    "events": batch_events,
                    "priorities": batch_priorities,
                },
            )
            batch_outputs.extend(batch_events["events"])
        events, priorities = merge_event_batches(batch_outputs, args.run_name)
        model_label = args.model

    def validation_errors() -> list[str]:
        found = validate_events(events, valid_ids) + validate_priorities(priorities, valid_ids) + validate_cluster_evidence(events, records) + validate_event_coverage(events, valid_ids)
        event_ids = {item.get("event_id") for item in events.get("events", [])}
        for index, item in enumerate(priorities.get("priorities", [])):
            if item.get("event_id") not in event_ids:
                found.append(f"priority {index} references an unknown event_id")
        return found

    errors = validation_errors()
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
