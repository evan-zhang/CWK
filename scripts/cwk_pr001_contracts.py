#!/usr/bin/env python3
"""RT-011: frozen v1 schemas, byte contracts, and machine-readable validators.

Scope (per `PR/PR-001-multitenant-knowledge-spaces/plans/开发计划.md` §3):

- Freeze v1 schemas for the seven PR-001 payloads
  (ReportKey, CanonicalEnvelope, TenantViewEnvelope, AccessObservation,
  AccessGrant, KnowledgeProfile, RouteDecision, QueryRequest,
  sample_manifest_v1, verified_shared_extensions_vN, capability probe,
  security defaults) and provide validators.
- Freeze byte contracts:
  * `object_id = "o_" + base32(random 128 bit)` with regex enforcement;
  * canonical JSON = NFC-normalise strings then RFC 8785 JCS, encoded
    as UTF-8;
  * `profile_sha256` = domain-separated SHA-256 recipe covering
    proposal + sample_manifest_sha256 + prompt_template_sha256 +
    model_id;
  * `ReportKey` default composition `source_namespace + ":" + report_id`.

Strict boundaries:

- No network, no filesystem writes outside the RT-011 fixture area,
  no reading of `CWORK_APP_KEY` or real CWork data.
- The validators are declarative: they mirror the JSON schema files in
  `PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/` but do not
  depend on any third-party JSON-Schema library so tests can run in the
  standard `python3.11 -m unittest discover` environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants and regex frozen by RT-011
# ---------------------------------------------------------------------------

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts"

# Object ID: "o_" + base32(random 128-bit), lowercase, no padding.
# base32 of 16 bytes = 26 characters using alphabet a-z2-7 (lowercased RFC 4648).
OBJECT_ID_REGEX = re.compile(r"^o_[a-z2-7]{26}$")
# Tenant/space IDs are opaque; we only pin the string shape here so downstream RTs
# have a single reference to import.
TENANT_ID_REGEX = re.compile(r"^t_[a-z0-9]{26}$")
SPACE_ID_REGEX = re.compile(r"^sp_[a-z0-9]{10,32}$")
SOURCE_NAMESPACE_REGEX = re.compile(r"^[a-z][a-z0-9_]*$")
REPORT_ID_REGEX = re.compile(r"^[^\s]{1,256}$")
REPORT_KEY_REGEX = re.compile(r"^[a-z][a-z0-9_]*:[^\s]{1,256}$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")
VERSION_REGEX = re.compile(r"^v[0-9]+$")

# Domain separator for profile_sha256; encoded as raw ASCII bytes.
PROFILE_DOMAIN_SEPARATOR = b"cwk-profile-v1"
NULL_BYTE = b"\x00"

# Fields that must NEVER appear in a CanonicalEnvelope.  Kept in sync with
# canonical_report.schema.json's `forbiddenProperties`.
CANONICAL_FORBIDDEN_FIELDS = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_id_hash",
        "credential_ref",
        "lane",
        "read_status",
        "todo_status",
        "allowed_actions",
        "role",
        "roles",
        "reply",
        "replies",
        "node",
        "nodes",
        "attachment",
        "attachments",
        "attachment_url",
        "preview_url",
        "short_url",
        "presign_url",
        "download_url",
        "collected_at",
        "path",
        "absolute_path",
        "mirror_root",
    }
)

QUERY_REQUEST_FORBIDDEN_FIELDS = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_id_hash",
        "binding_epoch",
        "auth_epoch",
        "credential_ref",
        "credentials",
        "app_key",
        "mirror_root",
        "root",
        "path",
        "absolute_path",
        "profile_version",
        "profile_sha256",
    }
)

PROFILE_FORBIDDEN_FIELDS = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_binding",
        "credential_ref",
        "credentials",
        "path",
        "mirror_root",
        "grant",
        "grants",
        "shell",
        "command",
        "tool",
    }
)

# Frozen seven-state Access Grant transition table.  Encoded as adjacency map;
# imported by tests and future RTs.
ACCESS_GRANT_STATES = (
    "discovered",
    "granted",
    "active",
    "revalidation_due",
    "revoked",
    "purge_pending",
    "purged",
)

ACCESS_GRANT_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "discovered": ("granted", "revoked"),
    "granted": ("active", "revoked"),
    "active": ("revalidation_due", "revoked"),
    "revalidation_due": ("active", "revoked"),
    "revoked": ("purge_pending",),
    "purge_pending": ("purged",),
    "purged": (),
}
ACCESS_GRANT_QUERY_ELIGIBLE = frozenset({"active"})

# Frozen six-state Knowledge Profile version transitions.  Rollback is expressed
# as a separate append-only event, not a state; see
# `profile_pointer_rollback.schema.json`.
KNOWLEDGE_PROFILE_STATES = (
    "draft",
    "proposed",
    "preview",
    "confirmed",
    "active",
    "superseded",
)

KNOWLEDGE_PROFILE_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("proposed",),
    "proposed": ("preview", "draft"),
    "preview": ("confirmed", "proposed"),
    "confirmed": ("active",),
    "active": ("superseded",),
    "superseded": ("active",),  # only through profile_pointer_rollback
}

DISPOSITIONS = ("index", "archive_no_index", "review")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ContractError(Exception):
    """Raised when a payload violates a frozen v1 contract."""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        if self.path:
            return f"{self.path}: {self.args[0]}"
        return str(self.args[0])


# ---------------------------------------------------------------------------
# Byte contracts
# ---------------------------------------------------------------------------


def new_object_id(random_bytes: bytes | None = None) -> str:
    """Return a fresh opaque `object_id` in the frozen format.

    Callers may pass explicit `random_bytes` to keep the value deterministic
    inside tests; production callers should leave it as ``None`` so a
    cryptographic RNG is used.
    """

    if random_bytes is None:
        random_bytes = secrets.token_bytes(16)
    if len(random_bytes) != 16:
        raise ContractError(
            f"object_id requires exactly 128 bits (16 bytes), got {len(random_bytes)}"
        )
    encoded = base64.b32encode(random_bytes).decode("ascii").rstrip("=").lower()
    value = f"o_{encoded}"
    if not OBJECT_ID_REGEX.match(value):
        raise ContractError(f"generated object_id does not match frozen regex: {value}")
    return value


def validate_object_id(value: str) -> None:
    if not isinstance(value, str) or not OBJECT_ID_REGEX.match(value):
        raise ContractError(
            f"object_id must match {OBJECT_ID_REGEX.pattern!r}, got {value!r}",
            path="object_id",
        )


def compose_report_key(source_namespace: str, report_id: str) -> str:
    """Return the frozen ReportKey string ``source_namespace:report_id``.

    Enforces the RT-011 default even if callers try to skip the namespace.
    """

    if not isinstance(source_namespace, str) or not SOURCE_NAMESPACE_REGEX.match(source_namespace):
        raise ContractError(
            f"source_namespace must match {SOURCE_NAMESPACE_REGEX.pattern!r}, got {source_namespace!r}",
            path="source_namespace",
        )
    if not isinstance(report_id, str) or not REPORT_ID_REGEX.match(report_id):
        raise ContractError(
            "report_id must be a non-empty string with no whitespace and <=256 chars",
            path="report_id",
        )
    return f"{source_namespace}:{report_id}"


def parse_report_key(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ContractError(f"report_key must be str, got {type(value).__name__}", path="report_key")
    if ":" not in value:
        raise ContractError(
            "report_key must be 'source_namespace:report_id' (default RT-011 composition)",
            path="report_key",
        )
    namespace, report_id = value.split(":", 1)
    if not SOURCE_NAMESPACE_REGEX.match(namespace):
        raise ContractError(
            f"source_namespace component must match {SOURCE_NAMESPACE_REGEX.pattern!r}",
            path="report_key",
        )
    if not REPORT_ID_REGEX.match(report_id):
        raise ContractError("report_id component must be non-empty and whitespace-free", path="report_key")
    return namespace, report_id


def nfc_normalize(value: Any) -> Any:
    """Recursively NFC-normalise every string inside ``value``.

    Applied before JCS to satisfy the DESIGN §7.2 rule that JCS itself does
    not perform Unicode normalisation.
    """

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [nfc_normalize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(nfc_normalize(item) for item in value)
    if isinstance(value, dict):
        return {nfc_normalize(k): nfc_normalize(v) for k, v in value.items()}
    return value


def _jcs_number(value: float | int) -> str:
    """Format a number per RFC 8785 (ECMA-262 Number.prototype.toString subset).

    RT-011 payloads only use integers, `review_threshold`-style floats in
    [0.0, 1.0], and confidence values.  We reject non-finite floats and
    reject `bool` (Python `bool` is a subclass of `int` so it must be
    filtered upstream).
    """

    if isinstance(value, bool):
        raise ContractError("booleans must be serialised as true/false, not numbers")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ContractError("JCS forbids NaN and Infinity")
        # For canonical hashing purposes the RT-011 payloads only use
        # simple floats.  Python's ``repr`` produces the shortest round-trip
        # representation; if the value is integral emit it without the
        # trailing ``.0`` to match ECMA-262.
        if value.is_integer():
            return str(int(value))
        text = repr(value)
        # Python may emit "1e-05" already; RFC 8785 requires lower-case ``e``
        # and no leading ``+`` in the exponent, which repr satisfies.
        return text
    raise ContractError(f"unsupported numeric type: {type(value).__name__}")


def _jcs_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jcs_serialize(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs_serialize(item) for item in value) + "]"
    if isinstance(value, tuple):
        return "[" + ",".join(_jcs_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        # Sort keys by their UTF-16 code-unit sequence (RFC 8785 §3.2.3).
        # Python str comparison uses code points; for the BMP that matches
        # UTF-16 code units.  Above the BMP we explicitly sort by the
        # UTF-16-BE byte encoding to be safe.
        def _sort_key(key: Any) -> bytes:
            if not isinstance(key, str):
                raise ContractError("JCS requires string object keys")
            return key.encode("utf-16-be")

        items = sorted(value.items(), key=lambda kv: _sort_key(kv[0]))
        return "{" + ",".join(_jcs_string(k) + ":" + _jcs_serialize(v) for k, v in items) + "}"
    raise ContractError(f"JCS cannot serialise {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return NFC-normalised RFC 8785 JCS canonical JSON as UTF-8 bytes."""

    normalised = nfc_normalize(value)
    return _jcs_serialize(normalised).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compute_profile_sha256(
    *,
    nfc_normalized_proposal: Mapping[str, Any],
    sample_manifest_sha256: str,
    prompt_template_sha256: str,
    model_id: str,
) -> str:
    """Return `profile_sha256` per DESIGN §7.2 with the domain separator recipe.

    Formula (byte-level, no delimiter-less concatenation)::

        sha256(
            b"cwk-profile-v1" + b"\\x00"
            + jcs_utf8(nfc_normalized_proposal) + b"\\x00"
            + sample_manifest_sha256_ascii + b"\\x00"
            + prompt_template_sha256_ascii + b"\\x00"
            + model_id_utf8
        )

    Callers MUST pass an already-normalised proposal payload; we still run
    ``nfc_normalize`` defensively so an accidental non-normalised input
    cannot silently produce a divergent hash.
    """

    if not SHA256_HEX_REGEX.match(sample_manifest_sha256 or ""):
        raise ContractError(
            "sample_manifest_sha256 must be lowercase hex sha256",
            path="sample_manifest_sha256",
        )
    if not SHA256_HEX_REGEX.match(prompt_template_sha256 or ""):
        raise ContractError(
            "prompt_template_sha256 must be lowercase hex sha256",
            path="prompt_template_sha256",
        )
    if not isinstance(model_id, str) or not model_id:
        raise ContractError("model_id must be a non-empty string", path="model_id")

    body = canonical_json_bytes(nfc_normalized_proposal)
    hasher = hashlib.sha256()
    hasher.update(PROFILE_DOMAIN_SEPARATOR)
    hasher.update(NULL_BYTE)
    hasher.update(body)
    hasher.update(NULL_BYTE)
    hasher.update(sample_manifest_sha256.encode("ascii"))
    hasher.update(NULL_BYTE)
    hasher.update(prompt_template_sha256.encode("ascii"))
    hasher.update(NULL_BYTE)
    hasher.update(model_id.encode("utf-8"))
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Schema validators
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str, path: str) -> None:
    if not condition:
        raise ContractError(message, path=path)


def _require_type(value: Any, expected: type | tuple[type, ...], path: str) -> None:
    if isinstance(expected, tuple):
        ok = isinstance(value, expected)
    else:
        ok = isinstance(value, expected)
    if isinstance(value, bool) and (expected is int or (isinstance(expected, tuple) and int in expected and bool not in expected)):
        # Never accept bool where an int/number is required.
        ok = False
    if not ok:
        expected_name = getattr(expected, "__name__", str(expected))
        raise ContractError(f"expected {expected_name}, got {type(value).__name__}", path=path)


def _require_regex(value: Any, regex: re.Pattern[str], path: str) -> None:
    if not isinstance(value, str) or not regex.match(value):
        raise ContractError(f"value {value!r} does not match {regex.pattern!r}", path=path)


def _require_enum(value: Any, allowed: Iterable[str], path: str) -> None:
    allowed_tuple = tuple(allowed)
    if value not in allowed_tuple:
        raise ContractError(f"value {value!r} not in {allowed_tuple!r}", path=path)


def _reject_forbidden_keys(payload: Mapping[str, Any], forbidden: Iterable[str], path: str) -> None:
    hits = sorted(set(payload.keys()) & set(forbidden))
    if hits:
        raise ContractError(f"forbidden fields present: {hits}", path=path)


def _require_iso_datetime(value: Any, path: str, *, nullable: bool = False) -> None:
    if value is None:
        if nullable:
            return
        raise ContractError("value must be a non-null ISO 8601 datetime", path=path)
    if not isinstance(value, str):
        raise ContractError("value must be an ISO 8601 datetime string", path=path)
    # Accept any RFC 3339 shape via fromisoformat; Python 3.11 accepts trailing "Z".
    try:
        from datetime import datetime as _dt

        text = value.replace("Z", "+00:00")
        _dt.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"invalid ISO 8601 datetime: {value!r} ({exc})", path=path)


def validate_report_key_payload(payload: Any) -> None:
    _require_type(payload, dict, "report_key")
    _require(payload.get("schema") == "cwk.report_key.v1", "schema mismatch", "report_key.schema")
    _require_regex(payload.get("source_namespace"), SOURCE_NAMESPACE_REGEX, "report_key.source_namespace")
    _require_regex(payload.get("report_id"), REPORT_ID_REGEX, "report_key.report_id")


def validate_canonical_envelope(payload: Any) -> None:
    _require_type(payload, dict, "canonical_report")
    _require(payload.get("schema") == "cwk.canonical_report.v1", "schema mismatch", "canonical_report.schema")
    _reject_forbidden_keys(payload, CANONICAL_FORBIDDEN_FIELDS, "canonical_report")

    for name, regex in (
        ("source_namespace", SOURCE_NAMESPACE_REGEX),
        ("report_id", REPORT_ID_REGEX),
        ("canonical_sha256", SHA256_HEX_REGEX),
        ("normalizer_version", VERSION_REGEX),
    ):
        _require_regex(payload.get(name), regex, f"canonical_report.{name}")

    _require_type(payload.get("title"), str, "canonical_report.title")
    _require_type(payload.get("body"), str, "canonical_report.body")
    _require_iso_datetime(payload.get("created_at"), "canonical_report.created_at")
    _require_iso_datetime(payload.get("source_updated_at"), "canonical_report.source_updated_at")

    author = payload.get("author")
    _require_type(author, dict, "canonical_report.author")
    _require_type(author.get("source_user_id"), str, "canonical_report.author.source_user_id")

    ext = payload.get("verified_shared_extensions_ref")
    if ext is not None:
        _require_type(ext, dict, "canonical_report.verified_shared_extensions_ref")
        _require_regex(ext.get("version"), VERSION_REGEX, "canonical_report.verified_shared_extensions_ref.version")
        _require_regex(ext.get("sha256"), SHA256_HEX_REGEX, "canonical_report.verified_shared_extensions_ref.sha256")

    # Confirm canonical_sha256 matches the payload minus canonical_sha256 itself.
    without_hash = {k: v for k, v in payload.items() if k != "canonical_sha256"}
    computed = canonical_sha256(without_hash)
    if computed != payload["canonical_sha256"]:
        raise ContractError(
            f"canonical_sha256 mismatch (computed {computed})",
            path="canonical_report.canonical_sha256",
        )


def validate_tenant_view(payload: Any) -> None:
    _require_type(payload, dict, "tenant_view")
    _require(payload.get("schema") == "cwk.tenant_view.v1", "schema mismatch", "tenant_view.schema")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "tenant_view.tenant_id")
    _require_regex(payload.get("report_key"), REPORT_KEY_REGEX, "tenant_view.report_key")
    _require_regex(payload.get("canonical_sha256"), SHA256_HEX_REGEX, "tenant_view.canonical_sha256")
    _require_iso_datetime(payload.get("observed_at"), "tenant_view.observed_at")

    for name, allowed in (
        ("read_status", (None, "unread", "read")),
        ("todo_status", (None, "pending", "done", "cancelled")),
    ):
        if name in payload:
            _require_enum(payload[name], allowed, f"tenant_view.{name}")


def validate_access_observation(payload: Any) -> None:
    _require_type(payload, dict, "access_observation")
    _require(payload.get("schema") == "cwk.access_observation.v1", "schema mismatch", "access_observation.schema")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "access_observation.tenant_id")
    _require_regex(payload.get("source_namespace"), SOURCE_NAMESPACE_REGEX, "access_observation.source_namespace")
    _require_regex(payload.get("report_id"), REPORT_ID_REGEX, "access_observation.report_id")
    _require_iso_datetime(payload.get("observed_at"), "access_observation.observed_at")
    _require_enum(
        payload.get("observation_source"),
        (
            "tenant_appkey_observation",
            "legacy_raw_decomposition",
            "delegated_admin_bootstrap",
        ),
        "access_observation.observation_source",
    )
    _require_enum(
        payload.get("initial_status"),
        ("discovered", "granted"),
        "access_observation.initial_status",
    )


def validate_access_grant(payload: Any) -> None:
    _require_type(payload, dict, "access_grant")
    _require(payload.get("schema") == "cwk.access_grant.v1", "schema mismatch", "access_grant.schema")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "access_grant.tenant_id")
    _require_regex(payload.get("source_namespace"), SOURCE_NAMESPACE_REGEX, "access_grant.source_namespace")
    _require_regex(payload.get("report_id"), REPORT_ID_REGEX, "access_grant.report_id")
    _require_enum(payload.get("status"), ACCESS_GRANT_STATES, "access_grant.status")
    _require_type(payload.get("roles"), list, "access_grant.roles")
    _require_enum(payload.get("visibility_scope"), ("full", "partial", "unknown"), "access_grant.visibility_scope")
    _require_enum(
        payload.get("permission_source"),
        (
            "tenant_appkey_observation",
            "authoritative_permission_api",
            "legacy_raw_decomposition",
            "delegated_admin_bootstrap",
        ),
        "access_grant.permission_source",
    )
    _require_type(payload.get("auth_epoch"), int, "access_grant.auth_epoch")
    _require(payload["auth_epoch"] >= 1, "auth_epoch must be >=1", "access_grant.auth_epoch")

    for name in ("granted_at", "last_verified_at", "lease_expires_at", "revoked_at"):
        _require_iso_datetime(payload.get(name), f"access_grant.{name}", nullable=True)


def validate_access_grant_transition(from_status: str, to_status: str) -> None:
    _require_enum(from_status, ACCESS_GRANT_STATES, "access_grant.transition.from")
    _require_enum(to_status, ACCESS_GRANT_STATES, "access_grant.transition.to")
    allowed = ACCESS_GRANT_ALLOWED_TRANSITIONS[from_status]
    if to_status not in allowed:
        raise ContractError(
            f"illegal access_grant transition {from_status} -> {to_status}; "
            f"allowed: {list(allowed)}",
            path="access_grant.transition",
        )


def validate_knowledge_profile(payload: Any) -> None:
    _require_type(payload, dict, "knowledge_profile")
    _require(payload.get("schema") == "cwk.knowledge_profile.v1", "schema mismatch", "knowledge_profile.schema")
    _reject_forbidden_keys(payload, PROFILE_FORBIDDEN_FIELDS, "knowledge_profile")
    _require_regex(payload.get("version"), VERSION_REGEX, "knowledge_profile.version")
    _require_enum(payload.get("status"), KNOWLEDGE_PROFILE_STATES, "knowledge_profile.status")
    _require_type(payload.get("spaces"), list, "knowledge_profile.spaces")
    for i, space in enumerate(payload["spaces"]):
        _require_type(space, dict, f"knowledge_profile.spaces[{i}]")
        _require_regex(space.get("space_id"), SPACE_ID_REGEX, f"knowledge_profile.spaces[{i}].space_id")
    _require_type(payload.get("entity_policy"), dict, "knowledge_profile.entity_policy")
    _require_type(payload.get("attention"), dict, "knowledge_profile.attention")
    _require_type(payload.get("routing_rules"), list, "knowledge_profile.routing_rules")
    _require_type(payload.get("archive_rules"), list, "knowledge_profile.archive_rules")
    _require_type(payload.get("review_threshold"), (int, float), "knowledge_profile.review_threshold")
    threshold = payload["review_threshold"]
    _require(0.0 <= float(threshold) <= 1.0, "review_threshold must be in [0,1]", "knowledge_profile.review_threshold")
    for name in ("sample_manifest_ref", "holdout_manifest_ref", "model_id"):
        _require_type(payload.get(name), str, f"knowledge_profile.{name}")
        _require(bool(payload[name]), f"{name} must be non-empty", f"knowledge_profile.{name}")
    for name in ("sample_manifest_sha256", "prompt_template_sha256", "profile_sha256"):
        _require_regex(payload.get(name), SHA256_HEX_REGEX, f"knowledge_profile.{name}")


def validate_knowledge_profile_transition(from_status: str, to_status: str) -> None:
    _require_enum(from_status, KNOWLEDGE_PROFILE_STATES, "knowledge_profile.transition.from")
    _require_enum(to_status, KNOWLEDGE_PROFILE_STATES, "knowledge_profile.transition.to")
    if to_status not in KNOWLEDGE_PROFILE_ALLOWED_TRANSITIONS[from_status]:
        raise ContractError(
            f"illegal knowledge_profile transition {from_status} -> {to_status}",
            path="knowledge_profile.transition",
        )


def validate_profile_pointer_rollback(payload: Any) -> None:
    _require_type(payload, dict, "profile_pointer_rollback")
    _require(payload.get("schema") == "cwk.profile_pointer_rollback.v1", "schema mismatch", "profile_pointer_rollback.schema")
    _require(payload.get("event_type") == "profile_pointer_rollback", "event_type must be profile_pointer_rollback", "profile_pointer_rollback.event_type")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "profile_pointer_rollback.tenant_id")
    _require_regex(payload.get("from_version"), VERSION_REGEX, "profile_pointer_rollback.from_version")
    _require_regex(payload.get("to_version"), VERSION_REGEX, "profile_pointer_rollback.to_version")
    if payload["from_version"] == payload["to_version"]:
        raise ContractError(
            "profile_pointer_rollback must swap to a different version",
            path="profile_pointer_rollback",
        )
    _require_regex(payload.get("from_profile_sha256"), SHA256_HEX_REGEX, "profile_pointer_rollback.from_profile_sha256")
    _require_regex(payload.get("to_profile_sha256"), SHA256_HEX_REGEX, "profile_pointer_rollback.to_profile_sha256")
    _require_type(payload.get("actor"), str, "profile_pointer_rollback.actor")
    _require(bool(payload["actor"]), "actor must be non-empty", "profile_pointer_rollback.actor")
    _require_type(payload.get("reason"), str, "profile_pointer_rollback.reason")
    _require(bool(payload["reason"]), "reason must be non-empty", "profile_pointer_rollback.reason")
    _require_iso_datetime(payload.get("occurred_at"), "profile_pointer_rollback.occurred_at")
    for name in ("auth_epoch_before", "auth_epoch_after"):
        _require_type(payload.get(name), int, f"profile_pointer_rollback.{name}")
        _require(payload[name] >= 1, f"{name} must be >=1", f"profile_pointer_rollback.{name}")
    _require(
        payload["auth_epoch_after"] > payload["auth_epoch_before"],
        "auth_epoch_after must be strictly greater than auth_epoch_before",
        "profile_pointer_rollback.auth_epoch_after",
    )


def validate_route_decision(payload: Any) -> None:
    _require_type(payload, dict, "route_decision")
    _require(payload.get("schema") == "cwk.route_decision.v1", "schema mismatch", "route_decision.schema")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "route_decision.tenant_id")
    _require_regex(payload.get("report_key"), REPORT_KEY_REGEX, "route_decision.report_key")
    _require_regex(payload.get("canonical_sha256"), SHA256_HEX_REGEX, "route_decision.canonical_sha256")
    _require_enum(payload.get("disposition"), DISPOSITIONS, "route_decision.disposition")

    space_ids = payload.get("space_ids")
    _require_type(space_ids, list, "route_decision.space_ids")
    seen: set[str] = set()
    for i, sid in enumerate(space_ids):
        if not isinstance(sid, str) or not SPACE_ID_REGEX.match(sid):
            raise ContractError(
                f"space_ids[{i}] {sid!r} is not an opaque space_id (slugs are forbidden)",
                path="route_decision.space_ids",
            )
        if sid in seen:
            raise ContractError(f"duplicate space_id {sid!r}", path="route_decision.space_ids")
        seen.add(sid)

    confidence = payload.get("confidence")
    _require_type(confidence, (int, float), "route_decision.confidence")
    _require(0.0 <= float(confidence) <= 1.0, "confidence must be in [0,1]", "route_decision.confidence")

    _require_type(payload.get("reason_codes"), list, "route_decision.reason_codes")
    _require_regex(payload.get("profile_version"), VERSION_REGEX, "route_decision.profile_version")
    _require_regex(payload.get("profile_sha256"), SHA256_HEX_REGEX, "route_decision.profile_sha256")
    _require_type(payload.get("decided_by"), str, "route_decision.decided_by")
    _require(bool(payload["decided_by"]), "decided_by must be non-empty", "route_decision.decided_by")
    _require_iso_datetime(payload.get("decided_at"), "route_decision.decided_at")


def validate_query_request(payload: Any) -> None:
    _require_type(payload, dict, "query_request")
    _require(payload.get("schema") == "cwk.query_request.v1", "schema mismatch", "query_request.schema")
    _reject_forbidden_keys(payload, QUERY_REQUEST_FORBIDDEN_FIELDS, "query_request")
    _require_type(payload.get("query"), str, "query_request.query")
    query = payload["query"]
    _require(1 <= len(query) <= 4000, "query length must be 1..4000", "query_request.query")

    selector = payload.get("space_selector", [])
    _require_type(selector, list, "query_request.space_selector")
    seen: set[str] = set()
    for i, sid in enumerate(selector):
        if not isinstance(sid, str) or not SPACE_ID_REGEX.match(sid):
            raise ContractError(
                f"space_selector[{i}] {sid!r} must be an opaque space_id; slugs are forbidden",
                path="query_request.space_selector",
            )
        if sid in seen:
            raise ContractError(f"duplicate space_id {sid!r}", path="query_request.space_selector")
        seen.add(sid)

    dispositions = payload.get("include_dispositions", ["index"])
    _require_type(dispositions, list, "query_request.include_dispositions")
    for i, disp in enumerate(dispositions):
        _require_enum(disp, DISPOSITIONS, f"query_request.include_dispositions[{i}]")

    limit = payload.get("limit", 8)
    _require_type(limit, int, "query_request.limit")
    _require(1 <= limit <= 64, "limit must be in [1, 64]", "query_request.limit")


def validate_sample_manifest(payload: Any) -> None:
    _require_type(payload, dict, "sample_manifest")
    _require(payload.get("schema") == "cwk.sample_manifest.v1", "schema mismatch", "sample_manifest.schema")
    _require_regex(payload.get("tenant_id"), TENANT_ID_REGEX, "sample_manifest.tenant_id")
    _require_regex(payload.get("random_seed"), re.compile(r"^[0-9a-f]{16,64}$"), "sample_manifest.random_seed")
    _require_type(payload.get("target_sample_size"), int, "sample_manifest.target_sample_size")
    _require(
        100 <= payload["target_sample_size"] <= 200,
        "target_sample_size must be in [100, 200]",
        "sample_manifest.target_sample_size",
    )
    _require_type(payload.get("actual_sample_size"), int, "sample_manifest.actual_sample_size")
    _require(payload["actual_sample_size"] >= 1, "actual_sample_size must be >=1", "sample_manifest.actual_sample_size")
    _require_type(payload.get("strata"), list, "sample_manifest.strata")
    _require_type(payload.get("chunk_size"), int, "sample_manifest.chunk_size")
    _require(1 <= payload["chunk_size"] <= 50, "chunk_size must be in [1, 50]", "sample_manifest.chunk_size")
    _require_type(payload.get("chunk_layout"), list, "sample_manifest.chunk_layout")

    samples = payload.get("samples")
    _require_type(samples, list, "sample_manifest.samples")
    sample_keys = set()
    for i, entry in enumerate(samples):
        _require_type(entry, dict, f"sample_manifest.samples[{i}]")
        _require_regex(entry.get("report_key"), REPORT_KEY_REGEX, f"sample_manifest.samples[{i}].report_key")
        _require_regex(entry.get("canonical_sha256"), SHA256_HEX_REGEX, f"sample_manifest.samples[{i}].canonical_sha256")
        sample_keys.add(entry["report_key"])

    holdout = payload.get("holdout")
    _require_type(holdout, list, "sample_manifest.holdout")
    holdout_keys = set()
    for i, entry in enumerate(holdout):
        _require_type(entry, dict, f"sample_manifest.holdout[{i}]")
        _require_regex(entry.get("report_key"), REPORT_KEY_REGEX, f"sample_manifest.holdout[{i}].report_key")
        _require_regex(entry.get("canonical_sha256"), SHA256_HEX_REGEX, f"sample_manifest.holdout[{i}].canonical_sha256")
        holdout_keys.add(entry["report_key"])

    overlap = sample_keys & holdout_keys
    if overlap:
        raise ContractError(
            f"holdout overlaps training samples: {sorted(overlap)[:5]}...",
            path="sample_manifest.holdout",
        )
    _require_iso_datetime(payload.get("created_at"), "sample_manifest.created_at")


def validate_verified_shared_extensions(payload: Any) -> None:
    _require_type(payload, dict, "verified_shared_extensions")
    _require(payload.get("schema") == "cwk.verified_shared_extensions.v1", "schema mismatch", "verified_shared_extensions.schema")
    _require_regex(payload.get("version"), VERSION_REGEX, "verified_shared_extensions.version")
    _require_regex(payload.get("manifest_sha256"), SHA256_HEX_REGEX, "verified_shared_extensions.manifest_sha256")
    _require_type(payload.get("compared_sample_size"), int, "verified_shared_extensions.compared_sample_size")
    _require(payload["compared_sample_size"] >= 50, "compared_sample_size must be >=50", "verified_shared_extensions.compared_sample_size")
    _require_type(payload.get("min_field_match_rate"), (int, float), "verified_shared_extensions.min_field_match_rate")
    _require(
        0.9 <= float(payload["min_field_match_rate"]) <= 1.0,
        "min_field_match_rate must be in [0.9, 1.0]",
        "verified_shared_extensions.min_field_match_rate",
    )
    _require_type(payload.get("approved_by"), str, "verified_shared_extensions.approved_by")
    _require(bool(payload["approved_by"]), "approved_by must be non-empty", "verified_shared_extensions.approved_by")
    _require_iso_datetime(payload.get("approved_at"), "verified_shared_extensions.approved_at")

    entries = payload.get("entries")
    _require_type(entries, list, "verified_shared_extensions.entries")
    for i, entry in enumerate(entries):
        _require_type(entry, dict, f"verified_shared_extensions.entries[{i}]")
        field_path = entry.get("field_path")
        _require_type(field_path, str, f"verified_shared_extensions.entries[{i}].field_path")
        if _is_forbidden_extension_path(field_path):
            raise ContractError(
                f"field_path {field_path!r} is on the never-shareable list "
                "(URLs, tokens, identity fields)",
                path=f"verified_shared_extensions.entries[{i}].field_path",
            )
        rate = entry.get("match_rate")
        _require_type(rate, (int, float), f"verified_shared_extensions.entries[{i}].match_rate")
        _require(
            0.9 <= float(rate) <= 1.0,
            "match_rate must be in [0.9, 1.0]",
            f"verified_shared_extensions.entries[{i}].match_rate",
        )
        _require_type(entry.get("sample_ids"), list, f"verified_shared_extensions.entries[{i}].sample_ids")
        _require(len(entry["sample_ids"]) >= 1, "sample_ids must be non-empty", f"verified_shared_extensions.entries[{i}].sample_ids")


_URL_FIELD_HINTS = (
    "_url",
    "url",
    "presign",
    "temporary_url",
    "preview_url",
    "short_url",
    "download",
)
_IDENTITY_FIELD_HINTS = (
    "tenant_id",
    "agent_id",
    "agent_id_hash",
    "credential",
    "app_key",
    "auth_epoch",
    "binding_epoch",
)


def _is_forbidden_extension_path(field_path: str) -> bool:
    lower = field_path.lower()
    for token in _URL_FIELD_HINTS + _IDENTITY_FIELD_HINTS:
        if token in lower:
            return True
    return False


def validate_capability_probe(payload: Any) -> None:
    _require_type(payload, dict, "capability_probe")
    _require(payload.get("schema") == "cwk.capability_probe.v1", "schema mismatch", "capability_probe.schema")
    _require_enum(
        payload.get("probe_id"),
        (
            "report_id_global_uniqueness",
            "permission_authoritative_events",
            "permission_authoritative_api",
            "trusted_agent_identity_openclaw_tool",
            "trusted_agent_identity_uds_peercred",
            "sandbox_transport_openclaw_tool",
            "sandbox_transport_uds",
            "sandbox_transport_loopback_http_self_reported",
            "verified_shared_extensions_dual_user_sample",
        ),
        "capability_probe.probe_id",
    )
    _require_enum(payload.get("result"), ("verified", "conservative_unknown"), "capability_probe.result")
    _require_type(payload.get("evidence_refs"), list, "capability_probe.evidence_refs")
    _require_iso_datetime(payload.get("run_at"), "capability_probe.run_at")

    if payload["probe_id"] == "sandbox_transport_loopback_http_self_reported":
        if payload["result"] != "conservative_unknown":
            raise ContractError(
                "sandbox_transport_loopback_http_self_reported is policy-forbidden; "
                "result must always be conservative_unknown",
                path="capability_probe.result",
            )
    if payload["result"] == "verified" and len(payload["evidence_refs"]) == 0:
        raise ContractError(
            "verified capability probes require non-empty evidence_refs",
            path="capability_probe.evidence_refs",
        )


def validate_security_defaults(payload: Any) -> None:
    _require_type(payload, dict, "security_defaults")
    _require(payload.get("schema") == "cwk.security_defaults.v1", "schema mismatch", "security_defaults.schema")
    _require_regex(payload.get("version"), VERSION_REGEX, "security_defaults.version")
    for section in (
        "report_key_composition",
        "canonical_json",
        "object_id",
        "profile_sha256_recipe",
        "transport_and_identity",
        "access_grant",
        "profile_lifecycle",
        "cold_archive",
        "cache_key",
        "logging",
        "break_glass",
    ):
        _require_type(payload.get(section), dict, f"security_defaults.{section}")


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


VALIDATORS: dict[str, Any] = {
    "cwk.report_key.v1": validate_report_key_payload,
    "cwk.canonical_report.v1": validate_canonical_envelope,
    "cwk.tenant_view.v1": validate_tenant_view,
    "cwk.access_observation.v1": validate_access_observation,
    "cwk.access_grant.v1": validate_access_grant,
    "cwk.knowledge_profile.v1": validate_knowledge_profile,
    "cwk.profile_pointer_rollback.v1": validate_profile_pointer_rollback,
    "cwk.route_decision.v1": validate_route_decision,
    "cwk.query_request.v1": validate_query_request,
    "cwk.sample_manifest.v1": validate_sample_manifest,
    "cwk.verified_shared_extensions.v1": validate_verified_shared_extensions,
    "cwk.capability_probe.v1": validate_capability_probe,
    "cwk.security_defaults.v1": validate_security_defaults,
}


def validate(payload: Any) -> None:
    """Dispatch validation by the payload's declared ``schema`` field."""

    if not isinstance(payload, dict):
        raise ContractError("payload must be a JSON object with a `schema` field", path="<root>")
    schema = payload.get("schema")
    if schema not in VALIDATORS:
        raise ContractError(f"unknown schema {schema!r}", path="<root>.schema")
    VALIDATORS[schema](payload)


def load_security_defaults() -> dict[str, Any]:
    """Load and validate the frozen security defaults."""

    path = SCHEMA_ROOT / "security_defaults.json"
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    validate_security_defaults(payload)
    return payload


def load_verified_shared_extensions_v1() -> dict[str, Any]:
    """Load and validate the bootstrap ``verified_shared_extensions_v1`` manifest.

    Because the bootstrap manifest deliberately contains no entries and uses a
    zero placeholder for ``manifest_sha256`` we skip the sha check on this
    single well-known file; real versions must supply the real sha256.
    """

    path = SCHEMA_ROOT / "verified_shared_extensions_v1.json"
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    # Only structural validation for the bootstrap; the placeholder sha means
    # entries are empty by construction.
    _require(payload.get("schema") == "cwk.verified_shared_extensions.v1", "schema mismatch", "verified_shared_extensions.schema")
    _require_regex(payload.get("version"), VERSION_REGEX, "verified_shared_extensions.version")
    _require_type(payload.get("entries"), list, "verified_shared_extensions.entries")
    if payload["entries"]:
        raise ContractError(
            "bootstrap manifest MUST have empty entries; add entries by publishing v2",
            path="verified_shared_extensions.entries",
        )
    return payload


# ---------------------------------------------------------------------------
# Data-class helpers for downstream RTs (kept lightweight)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportKey:
    source_namespace: str
    report_id: str

    def __post_init__(self) -> None:  # pragma: no cover - trivial guard
        if not SOURCE_NAMESPACE_REGEX.match(self.source_namespace):
            raise ContractError("invalid source_namespace", path="source_namespace")
        if not REPORT_ID_REGEX.match(self.report_id):
            raise ContractError("invalid report_id", path="report_id")

    def as_string(self) -> str:
        return compose_report_key(self.source_namespace, self.report_id)


__all__ = [
    "ACCESS_GRANT_ALLOWED_TRANSITIONS",
    "ACCESS_GRANT_QUERY_ELIGIBLE",
    "ACCESS_GRANT_STATES",
    "CANONICAL_FORBIDDEN_FIELDS",
    "DISPOSITIONS",
    "KNOWLEDGE_PROFILE_ALLOWED_TRANSITIONS",
    "KNOWLEDGE_PROFILE_STATES",
    "NULL_BYTE",
    "OBJECT_ID_REGEX",
    "PROFILE_DOMAIN_SEPARATOR",
    "PROFILE_FORBIDDEN_FIELDS",
    "QUERY_REQUEST_FORBIDDEN_FIELDS",
    "REPORT_ID_REGEX",
    "REPORT_KEY_REGEX",
    "SCHEMA_ROOT",
    "SHA256_HEX_REGEX",
    "SOURCE_NAMESPACE_REGEX",
    "SPACE_ID_REGEX",
    "TENANT_ID_REGEX",
    "VERSION_REGEX",
    "VALIDATORS",
    "ContractError",
    "ReportKey",
    "canonical_json_bytes",
    "canonical_sha256",
    "compose_report_key",
    "compute_profile_sha256",
    "load_security_defaults",
    "load_verified_shared_extensions_v1",
    "new_object_id",
    "nfc_normalize",
    "parse_report_key",
    "validate",
    "validate_access_grant",
    "validate_access_grant_transition",
    "validate_access_observation",
    "validate_canonical_envelope",
    "validate_capability_probe",
    "validate_knowledge_profile",
    "validate_knowledge_profile_transition",
    "validate_object_id",
    "validate_profile_pointer_rollback",
    "validate_query_request",
    "validate_report_key_payload",
    "validate_route_decision",
    "validate_sample_manifest",
    "validate_security_defaults",
    "validate_tenant_view",
    "validate_verified_shared_extensions",
]
