"""RT-047-compatible per-Agent bearer-token verification for RT-055.

Only the read side is implemented here.  The issuing/revoking authority remains
CWK's ``scripts/kb_token.py``; this module consumes its tokens.json shape and
re-reads it on every request so revocation is immediate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

TOKEN_HEADER = "X-KB-Token"
REGISTRY_SCHEMA = "cwk.kb.token-registry.v1"


def header_value(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is not None:
        return str(value)
    lowered = name.lower()
    return next((str(v) for k, v in headers.items() if str(k).lower() == lowered), "")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load(path: Path) -> dict[str, Any]:
    # read_bytes owns and closes its descriptor before returning.
    data = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("invalid token registry")
    if not isinstance(data.get("tokens"), list):
        raise ValueError("invalid token registry")
    return data


def _decision(path: Path, presented: str, bank: str) -> tuple[str, str, str]:
    """Return (status, reason, token_id): ok, unauthorized, or forbidden."""
    try:
        data = _load(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return "unauthorized", "registry_unreadable", ""
    digest = hashlib.sha256((presented or "").encode("utf-8")).hexdigest()
    match: dict[str, Any] | None = None
    for row in data["tokens"]:
        if not isinstance(row, dict):
            continue
        stored = row.get("token_sha256")
        if isinstance(stored, str) and len(stored) == 64 and hmac.compare_digest(digest, stored):
            match = row
    if match is None:
        return "unauthorized", "unknown_token", ""
    token_id = str(match.get("token_id") or "")
    if bool(match.get("revoked")):
        return "unauthorized", "revoked", token_id
    expires = _parse_time(match.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return "unauthorized", "expired", token_id
    scope = match.get("kb_ids")
    if not isinstance(scope, list) or not isinstance(bank, str) or bank not in scope:
        return "forbidden", "kb_not_in_scope", token_id
    return "ok", "authorized", token_id


def authorize(headers: Mapping[str, str], bank: str) -> tuple[int, dict[str, Any]] | None:
    """Return an HTTP refusal, or None when the request is authorized.

    ``RAG_AUTH_ENABLED`` defaults false.  When false this function does not
    inspect the registry at all, preserving local development and synthetic
    tests.  Enabled mode fails closed if the registry is absent or invalid.
    """
    enabled = os.getenv("RAG_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    presented = header_value(headers, TOKEN_HEADER)
    registry = os.getenv("RAG_AUTH_REGISTRY", "").strip()
    status, reason, token_id = _decision(Path(registry), presented, bank) if registry else ("unauthorized", "registry_unreadable", "")
    if status == "ok":
        return None
    if status == "forbidden":
        return 403, {"error": "token is valid but not authorized for this bank", "reason": reason}
    return 401, {"error": "missing or invalid X-KB-Token", "reason": reason}
