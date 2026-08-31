#!/usr/bin/env python3
"""Normalize backend-owned CWork report relationships.

CWK deliberately does not infer a user's business relationship by walking
writer/node/user lists.  Only the Work Report backend can apply the complete
rules for delegation, transfer, dynamic nodes, permissions and current state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "cwk.report_relationships.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _code(value: Any, default: str = "unknown") -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text or default


def unknown_relation(reason: str = "后台关系接口未提供权威结论") -> dict[str, Any]:
    return {
        "relationship_status": "unknown",
        "relationship_role": "unknown",
        "relationship_roles": [],
        "visible_only": False,
        "relationship_evidence": reason,
        "relationship_confidence": 0.0,
        "relationship_source": "work-report-backend",
        "relationship_reason_code": "UNRESOLVED",
        "relationship_action_required": None,
        "relationship_pending_actions": [],
        "relationship_relation_version": "",
    }


def normalize_backend_relation(
    payload: dict[str, Any] | None,
    *,
    expected_report_id: str = "",
    relation_version: str = "",
) -> dict[str, Any]:
    """Normalize one backend result without inventing missing semantics."""

    if not isinstance(payload, dict):
        return unknown_relation()
    report_id = _text(payload.get("reportId") or payload.get("report_id"))
    if expected_report_id and report_id and report_id != expected_report_id:
        return unknown_relation("后台关系结果 reportId 不匹配")
    status = _code(payload.get("status"))
    visibility = _code(payload.get("visibility"))
    if status != "resolved" or visibility not in {"related", "visible_only"}:
        reason = _text(payload.get("reasonCode") or payload.get("reason_code"))
        return unknown_relation(f"后台关系未解析：{reason or status}")

    roles_value = payload.get("roles")
    roles = []
    if isinstance(roles_value, list):
        for value in roles_value:
            role = _code(value)
            if role != "unknown" and role not in roles:
                roles.append(role)
    primary_role = _code(payload.get("primaryRole") or payload.get("primary_role"))
    if primary_role == "unknown" and visibility == "visible_only":
        primary_role = "observer"
    if primary_role != "unknown" and primary_role not in roles and visibility == "related":
        roles.insert(0, primary_role)

    action_required_value = payload.get("actionRequired")
    if action_required_value is None:
        action_required_value = payload.get("action_required")
    action_required = action_required_value if isinstance(action_required_value, bool) else None
    pending_value = payload.get("pendingActions")
    if pending_value is None:
        pending_value = payload.get("pending_actions")
    pending_actions = []
    if isinstance(pending_value, list):
        for value in pending_value:
            action = _code(value)
            if action != "unknown" and action not in pending_actions:
                pending_actions.append(action)
    reason_code = _text(payload.get("reasonCode") or payload.get("reason_code")) or "BACKEND_RESOLVED"
    version = _text(payload.get("relationVersion") or payload.get("relation_version") or relation_version)
    visible_only = visibility == "visible_only"
    return {
        "relationship_status": "visible_only" if visible_only else "author" if primary_role == "author" else "participant",
        "relationship_role": primary_role,
        "relationship_roles": roles,
        "visible_only": visible_only,
        "relationship_evidence": f"后台权威关系：{reason_code}",
        "relationship_confidence": 1.0,
        "relationship_source": "work-report-backend",
        "relationship_reason_code": reason_code,
        "relationship_action_required": action_required,
        "relationship_pending_actions": pending_actions,
        "relationship_relation_version": version,
    }


def load_relationship_manifest(path: str | Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path:
        return {}, {"provider_status": "unavailable"}
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.exists():
        return {}, {"provider_status": "unavailable"}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {"provider_status": "unavailable"}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        return {}, {"provider_status": "unavailable"}
    version = _text(payload.get("relation_version"))
    values = payload.get("items")
    items: dict[str, dict[str, Any]] = {}
    if isinstance(values, dict):
        iterable = values.values()
    elif isinstance(values, list):
        iterable = values
    else:
        iterable = []
    for value in iterable:
        if not isinstance(value, dict):
            continue
        report_id = _text(value.get("reportId") or value.get("report_id"))
        if report_id:
            if "relationship_status" in value:
                items[report_id] = dict(value)
            else:
                items[report_id] = normalize_backend_relation(
                    value,
                    expected_report_id=report_id,
                    relation_version=version,
                )
    return items, payload


def classify_person_relation(
    *,
    backend_relation: dict[str, Any] | None = None,
    **_ignored_local_fields: Any,
) -> dict[str, Any]:
    """Return only a backend-provided relationship, otherwise unknown.

    Extra keyword arguments are accepted for compatibility with older callers,
    but local report/person data is intentionally ignored.
    """

    if not isinstance(backend_relation, dict):
        return unknown_relation()
    if "relationship_status" in backend_relation:
        return dict(backend_relation)
    return normalize_backend_relation(backend_relation)
