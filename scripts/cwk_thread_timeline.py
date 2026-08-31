#!/usr/bin/env python3
"""Immutable evidence timelines for mutable CWork report threads.

The canonical raw report remains the latest source snapshot for existing CWK
consumers.  This module adds an append-only evidence trail below
``raw/_system/timelines/<report_id>/`` so replies and workflow opinions are
never silently lost when a report is refreshed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "cwk.thread-timeline.v1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def json_section(text: str, heading: str) -> Any:
    match = re.search(rf"##\s+{re.escape(heading)}\s+```json\s*(.*?)\s*```", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def first_text(value: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        candidate = value.get(name)
        if candidate not in (None, ""):
            return str(candidate)
    return ""


def event_record(report_id: str, kind: str, source: str, payload: Any, *, actor: str = "", occurred_at: str = "") -> dict[str, Any]:
    payload_hash = sha256_bytes(canonical(payload).encode("utf-8"))
    identity = {
        "report_id": report_id,
        "kind": kind,
        "source": source,
        "actor": actor,
        "occurred_at": occurred_at,
        "payload_sha256": payload_hash,
    }
    event_id = sha256_bytes(canonical(identity).encode("utf-8"))
    return {
        "schema_version": SCHEMA,
        "event_id": event_id,
        "report_id": report_id,
        "kind": kind,
        "source": source,
        "actor": actor or None,
        "occurred_at": occurred_at or None,
        "payload_sha256": payload_hash,
        "payload": payload,
    }


def reply_events(report_id: str, reply_list: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        actor = first_text(value, ("replyEmpName", "writeEmpName", "creatorName", "userName", "name", "empName"))
        occurred_at = first_text(value, ("replyTime", "createTime", "time", "updateTime", "sendTime"))
        content = first_text(value, ("replyContent", "content", "comment", "message", "opinion"))
        source_id = first_text(value, ("replyId", "id", "commentId", "recordId"))
        if content or source_id:
            events.append(event_record(
                report_id,
                "reply",
                "record-simple-info.replyList",
                value,
                actor=actor,
                occurred_at=occurred_at,
            ))
        for child_key in ("replyList", "children", "childList", "subReplyList"):
            if child_key in value:
                walk(value[child_key])

    walk(reply_list)
    return events


def workflow_events(report_id: str, node_list: Any) -> list[dict[str, Any]]:
    if not isinstance(node_list, list):
        return []
    events: list[dict[str, Any]] = []
    for node in node_list:
        if not isinstance(node, dict):
            continue
        node_context = {key: node.get(key) for key in ("id", "nodeId", "nodeName", "type", "status", "level", "createTime", "updateTime") if key in node}
        users = node.get("userList")
        if not isinstance(users, list) or not users:
            if node_context:
                events.append(event_record(
                    report_id,
                    "workflow_status",
                    "node-detail.nodeList",
                    {"node": node_context},
                    occurred_at=first_text(node, ("updateTime", "finishTime", "createTime")),
                ))
            continue
        for user in users:
            if not isinstance(user, dict):
                continue
            actor = first_text(user, ("name", "userName", "empName", "writeEmpName"))
            occurred_at = first_text(user, ("finishTime", "updateTime", "operateTime", "createTime"))
            has_opinion = bool(first_text(user, ("content", "opinion", "comment", "operate")))
            events.append(event_record(
                report_id,
                "workflow_opinion" if has_opinion else "workflow_status",
                "node-detail.nodeList",
                {"node": node_context, "participant": user},
                actor=actor,
                occurred_at=occurred_at,
            ))
    return events


def events_from_raw(report_id: str, raw_text: str) -> list[dict[str, Any]]:
    simple = json_section(raw_text, "Record Simple Info")
    node = json_section(raw_text, "Node / Opinion Chain")
    reply_list = simple.get("replyList") if isinstance(simple, dict) else []
    node_list = node.get("nodeList") if isinstance(node, dict) else []
    events = reply_events(report_id, reply_list) + workflow_events(report_id, node_list)
    unique: dict[str, dict[str, Any]] = {item["event_id"]: item for item in events}
    return [unique[key] for key in sorted(unique)]


def timeline_root(mirror_root: Path, report_id: str) -> Path:
    return mirror_root / "raw" / "_system" / "timelines" / report_id


def capture(mirror_root: Path, report_id: str, raw_bytes: bytes) -> dict[str, Any]:
    """Append a raw snapshot and its reply/workflow events, idempotently."""
    root = timeline_root(mirror_root, report_id)
    raw_hash = sha256_bytes(raw_bytes)
    snapshot = root / "snapshots" / f"{raw_hash}.md"
    changed: list[str] = []
    if not snapshot.exists():
        atomic_write(snapshot, raw_bytes)
        changed.append(snapshot.relative_to(mirror_root).as_posix())

    text = raw_bytes.decode("utf-8", errors="replace")
    events = events_from_raw(report_id, text)
    new_events = 0
    for event in events:
        path = root / "events" / f"{event['event_id']}.json"
        if path.exists():
            continue
        atomic_write(path, (json.dumps(event, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        changed.append(path.relative_to(mirror_root).as_posix())
        new_events += 1

    event_files = sorted((root / "events").glob("*.json")) if (root / "events").exists() else []
    snapshot_files = sorted((root / "snapshots").glob("*.md")) if (root / "snapshots").exists() else []
    manifest_path = root / "manifest.json"
    manifest = {
        "schema_version": SCHEMA,
        "report_id": report_id,
        "latest_snapshot_sha256": raw_hash,
        "snapshot_count": len(snapshot_files),
        "event_count": len(event_files),
        "event_ids": [path.stem for path in event_files],
    }
    previous: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    prior_semantics = {key: value for key, value in previous.items() if key != "rebuilt_at"}
    if prior_semantics != manifest:
        manifest["rebuilt_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        atomic_write(manifest_path, rendered.encode("utf-8"))
        changed.append(manifest_path.relative_to(mirror_root).as_posix())
    return {
        "report_id": report_id,
        "snapshot_sha256": raw_hash,
        "snapshot_created": snapshot.relative_to(mirror_root).as_posix() in changed,
        "event_count": len(events),
        "events_created": new_events,
        "changed_relative_paths": changed,
    }


def audit(mirror_root: Path, raw_paths: Iterable[Path]) -> dict[str, Any]:
    checked = 0
    missing_snapshots: list[str] = []
    missing_events: list[dict[str, str]] = []
    for path in raw_paths:
        data = path.read_bytes()
        fields = parse_frontmatter(data.decode("utf-8", errors="replace"))
        report_id = fields.get("report_id") or path.name.split("-", 1)[0]
        if not report_id:
            continue
        checked += 1
        root = timeline_root(mirror_root, report_id)
        if not (root / "snapshots" / f"{sha256_bytes(data)}.md").exists():
            missing_snapshots.append(report_id)
        for event in events_from_raw(report_id, data.decode("utf-8", errors="replace")):
            if not (root / "events" / f"{event['event_id']}.json").exists():
                missing_events.append({"report_id": report_id, "event_id": event["event_id"], "kind": event["kind"]})
    return {
        "schema_version": "cwk.thread-timeline-audit.v1",
        "checked_raw_count": checked,
        "missing_snapshot_count": len(missing_snapshots),
        "missing_event_count": len(missing_events),
        "missing_snapshot_report_ids": missing_snapshots,
        "missing_events": missing_events,
        "complete": not missing_snapshots and not missing_events,
    }
