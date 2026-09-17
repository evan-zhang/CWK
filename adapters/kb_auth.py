"""RT-047-compatible per-Agent bearer-token verification for RT-055.

Only the read side is implemented here.  The issuing/revoking authority remains
CWK's ``scripts/kb_token.py``; this module consumes its tokens.json shape and
re-reads it on every request so revocation is immediate.

RT-061 adds a second way to answer "may this token read this bank", selected
by ``RAG_AUTHZ_MODE``:

- ``scope`` (default, the pre-RT-061 behaviour): the bank must be in the
  token's own ``kb_ids``.
- ``grants``: the token names a principal, and the membership table written
  by ``scripts/kb_authz.py`` (``RAG_AUTHZ_PATH``) decides.  Tokens issued
  before the table existed carry no ``authz: grants`` marker and are judged by
  membership **and** their own ``kb_ids``, so switching modes can only narrow
  what an old token reaches, never widen it.

Both files are re-read per request, so a membership change is effective on
the next request without re-issuing any token.
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
AUTHZ_SCHEMA = "cwk.kb.authz.v1"

MODE_SCOPE = "scope"
MODE_GRANTS = "grants"
ROLES = ("owner", "writer", "reader")
BANK_ACTIVE = "active"
TOKEN_KIND_SHARED_KB = "shared_kb"
AUTHZ_GRANTS = "grants"


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


def load_authz(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != AUTHZ_SCHEMA:
        raise ValueError("invalid authz store")
    if not isinstance(data.get("banks"), dict) or not isinstance(data.get("grants"), list):
        raise ValueError("invalid authz store")
    return data


def scope_verdict(record: Mapping[str, Any], bank: object) -> tuple[str, str]:
    scope = record.get("kb_ids")
    if not isinstance(scope, list) or not isinstance(bank, str) or bank not in scope:
        return "forbidden", "kb_not_in_scope"
    return "ok", "authorized"


def membership_verdict(authz: Mapping[str, Any], principal: object, bank: object) -> tuple[str, str]:
    """Does ``principal`` hold any role on an active ``bank``?

    The single definition of membership: ``scripts/kb_authz.py`` calls this
    too, so the write side can never disagree with what production enforces.
    """
    if not isinstance(principal, str) or not principal:
        return "forbidden", "no_principal"
    banks = authz.get("banks")
    entry = banks.get(bank) if isinstance(banks, dict) and isinstance(bank, str) else None
    if not isinstance(entry, dict) or entry.get("status") != BANK_ACTIVE:
        return "forbidden", "bank_inactive"
    for grant in authz.get("grants") or ():
        if (
            isinstance(grant, dict)
            and grant.get("bank_id") == bank
            and grant.get("principal") == principal
            and grant.get("role") in ROLES
        ):
            return "ok", "authorized"
    return "forbidden", "not_a_member"


def grants_verdict(record: Mapping[str, Any], bank: object, authz: Mapping[str, Any]) -> tuple[str, str]:
    if record.get("token_kind") == TOKEN_KIND_SHARED_KB:
        # A shared token was exported for exactly one bank by whoever holds
        # the registry; it has no person behind it.  The bank must still be
        # live, and the token's own single-bank scope still decides.
        banks = authz.get("banks")
        entry = banks.get(bank) if isinstance(banks, dict) and isinstance(bank, str) else None
        if not isinstance(entry, dict) or entry.get("status") != BANK_ACTIVE:
            return "forbidden", "bank_inactive"
        return scope_verdict(record, bank)
    status, reason = membership_verdict(authz, record.get("principal"), bank)
    if status != "ok":
        return status, reason
    if record.get("authz") != AUTHZ_GRANTS:
        return scope_verdict(record, bank)
    return "ok", "authorized"


def record_verdict(
    record: Mapping[str, Any], bank: object, *, mode: str, authz: Mapping[str, Any] | None
) -> tuple[str, str]:
    """Verdict for a token record that is already known to be live."""
    if mode == MODE_SCOPE:
        return scope_verdict(record, bank)
    if mode == MODE_GRANTS and authz is not None:
        return grants_verdict(record, bank, authz)
    return "unauthorized", "authz_unavailable"


def _match(data: Mapping[str, Any], presented: str) -> dict[str, Any] | None:
    digest = hashlib.sha256((presented or "").encode("utf-8")).hexdigest()
    match: dict[str, Any] | None = None
    for row in data["tokens"]:
        if not isinstance(row, dict):
            continue
        stored = row.get("token_sha256")
        if isinstance(stored, str) and len(stored) == 64 and hmac.compare_digest(digest, stored):
            match = row
    return match


def _decision(
    path: Path, presented: str, bank: str, *, mode: str = MODE_SCOPE, authz_path: Path | None = None
) -> tuple[str, str, str]:
    """Return (status, reason, token_id): ok, unauthorized, or forbidden."""
    try:
        data = _load(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return "unauthorized", "registry_unreadable", ""
    match = _match(data, presented)
    if match is None:
        return "unauthorized", "unknown_token", ""
    token_id = str(match.get("token_id") or "")
    if bool(match.get("revoked")):
        return "unauthorized", "revoked", token_id
    expires = _parse_time(match.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return "unauthorized", "expired", token_id
    if mode not in (MODE_SCOPE, MODE_GRANTS):
        # A typo in the switch must not silently fall back to either rule.
        return "unauthorized", "authz_mode_invalid", token_id
    authz = None
    if mode == MODE_GRANTS:
        try:
            authz = load_authz(authz_path) if authz_path else None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            authz = None
    status, reason = record_verdict(match, bank, mode=mode, authz=authz)
    return status, reason, token_id


def authorize(headers: Mapping[str, str], bank: str) -> tuple[int, dict[str, Any]] | None:
    """Return an HTTP refusal, or None when the request is authorized.

    ``RAG_AUTH_ENABLED`` defaults false.  When false this function does not
    inspect the registry at all, preserving local development and synthetic
    tests.  Enabled mode fails closed if the registry is absent or invalid,
    and in ``grants`` mode also if the membership table is.
    """
    enabled = os.getenv("RAG_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    presented = header_value(headers, TOKEN_HEADER)
    registry = os.getenv("RAG_AUTH_REGISTRY", "").strip()
    mode = os.getenv("RAG_AUTHZ_MODE", MODE_SCOPE).strip().lower() or MODE_SCOPE
    authz_path = os.getenv("RAG_AUTHZ_PATH", "").strip()
    if registry:
        status, reason, token_id = _decision(
            Path(registry), presented, bank, mode=mode, authz_path=Path(authz_path) if authz_path else None
        )
    else:
        status, reason, token_id = ("unauthorized", "registry_unreadable", "")
    if status == "ok":
        return None
    if status == "forbidden":
        return 403, {"error": "token is valid but not authorized for this bank", "reason": reason}
    return 401, {"error": "missing or invalid X-KB-Token", "reason": reason}
