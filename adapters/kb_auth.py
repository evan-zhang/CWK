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

RT-067 adds access auditing.  These services deliberately log nothing about
requests — query paths and arguments carry protected source text — and the
price showed up in RT-062: with the ``/read`` hole open for days, nobody could
tell whether it had been used.  The audit line therefore records only *that*
an identity reached a bank and how it ended: timestamp, token id, principal,
bank, endpoint, status and reason.  Never the query, never a doc id, never a
byte of the answer.  Off unless ``RAG_AUTH_AUDIT_PATH`` is set.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

TOKEN_HEADER = "X-KB-Token"
REGISTRY_SCHEMA = "cwk.kb.token-registry.v1"
AUTHZ_SCHEMA = "cwk.kb.authz.v1"

MODE_SCOPE = "scope"
MODE_GRANTS = "grants"

AUDIT_SCHEMA = "cwk.kb.access-audit.v1"
ENV_AUDIT_PATH = "RAG_AUTH_AUDIT_PATH"
ENV_AUDIT_MAX_BYTES = "RAG_AUTH_AUDIT_MAX_BYTES"
DEFAULT_AUDIT_MAX_BYTES = 64 * 1024 * 1024
#: Fields an audit line may ever carry.  A test asserts the written keys are a
#: subset of this, so a future "just add the query for debugging" cannot pass.
AUDIT_FIELDS = ("schema", "ts", "endpoint", "bank", "status", "reason", "token_id", "principal", "client")
_AUDIT_LOCK = threading.Lock()
_AUDIT_BROKEN = False
ROLES = ("owner", "writer", "reader")
BANK_ACTIVE = "active"
TOKEN_KIND_SHARED_KB = "shared_kb"
AUTHZ_GRANTS = "grants"


def _audit_max_bytes() -> int:
    try:
        value = int(os.getenv(ENV_AUDIT_MAX_BYTES, "") or DEFAULT_AUDIT_MAX_BYTES)
    except ValueError:
        return DEFAULT_AUDIT_MAX_BYTES
    return value if value > 0 else DEFAULT_AUDIT_MAX_BYTES


def audit_access(
    *,
    endpoint: str,
    bank: object,
    status: int,
    reason: str,
    token_id: str = "",
    principal: str = "",
    client: str = "",
    path: str | None = None,
) -> None:
    """Append one access line.  Silent about content, loud about failure once.

    A request must never fail because the audit file cannot be written, but a
    silently broken audit is worse than none — so the first failure prints one
    line to stderr (which lands in the container log) and later ones stay quiet.
    """
    global _AUDIT_BROKEN
    target = path if path is not None else os.getenv(ENV_AUDIT_PATH, "").strip()
    if not target:
        return
    record = {
        "schema": AUDIT_SCHEMA,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": str(endpoint or ""),
        "bank": bank if isinstance(bank, str) else "",
        "status": int(status),
        "reason": str(reason or ""),
        "token_id": str(token_id or ""),
        "principal": str(principal or ""),
        "client": str(client or ""),
    }
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    file = Path(target)
    try:
        with _AUDIT_LOCK:
            file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            limit = _audit_max_bytes()
            if file.exists() and file.stat().st_size + len(line) > limit:
                # Keep exactly one previous generation: an unbounded audit file
                # eventually fills the disk that the services run on.
                file.replace(file.with_name(file.name + ".1"))
            handle = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(handle, line)
            finally:
                os.close(handle)
    except OSError as exc:
        if not _AUDIT_BROKEN:
            _AUDIT_BROKEN = True
            print(f"kb_auth: access audit unavailable ({type(exc).__name__})", file=sys.stderr, flush=True)


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
) -> tuple[str, str, str, str]:
    """Return (status, reason, token_id, principal): ok, unauthorized, or forbidden."""
    try:
        data = _load(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return "unauthorized", "registry_unreadable", "", ""
    match = _match(data, presented)
    if match is None:
        return "unauthorized", "unknown_token", "", ""
    token_id = str(match.get("token_id") or "")
    principal = str(match.get("principal") or "")
    if bool(match.get("revoked")):
        return "unauthorized", "revoked", token_id, principal
    expires = _parse_time(match.get("expires_at"))
    if expires is None or expires <= datetime.now(timezone.utc):
        return "unauthorized", "expired", token_id, principal
    if mode not in (MODE_SCOPE, MODE_GRANTS):
        # A typo in the switch must not silently fall back to either rule.
        return "unauthorized", "authz_mode_invalid", token_id, principal
    authz = None
    if mode == MODE_GRANTS:
        try:
            authz = load_authz(authz_path) if authz_path else None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            authz = None
    status, reason = record_verdict(match, bank, mode=mode, authz=authz)
    return status, reason, token_id, principal


def authorize(
    headers: Mapping[str, str], bank: str, *, endpoint: str = "", client: str = ""
) -> tuple[int, dict[str, Any]] | None:
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
        status, reason, token_id, principal = _decision(
            Path(registry), presented, bank, mode=mode, authz_path=Path(authz_path) if authz_path else None
        )
    else:
        status, reason, token_id, principal = ("unauthorized", "registry_unreadable", "", "")
    code = 200 if status == "ok" else (403 if status == "forbidden" else 401)
    audit_access(endpoint=endpoint, bank=bank, status=code, reason=reason,
                 token_id=token_id, principal=principal, client=client)
    if status == "ok":
        return None
    if status == "forbidden":
        return 403, {"error": "token is valid but not authorized for this bank", "reason": reason}
    return 401, {"error": "missing or invalid X-KB-Token", "reason": reason}
