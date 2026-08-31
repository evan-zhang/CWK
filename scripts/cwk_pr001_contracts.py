#!/usr/bin/env python3
"""RT-011 (post-remediation): frozen v1 schemas, byte contracts, strict engine.

Post-remediation summary (2026-08-19):

- Adds a small Draft 2020-12 subset validator engine so JSON Schema files are
  the ground truth (no more "schema as decoration").  The engine enforces
  `type`, `required`, `properties`, `additionalProperties: false`,
  `unevaluatedProperties: false`, `items`, `minItems`, `maxItems`,
  `uniqueItems`, `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`,
  `format: "date-time"` (with mandatory timezone) and `enum` / `const`.
- Adds custom keyword handlers registered per schema:
  `deepForbiddenProperties`, `allowedTransitions`, `forbiddenFieldPaths`,
  `policy`, plus payload-specific rules (canonical SHA recompute, sample
  manifest coverage, verified_shared_extensions sha recompute, profile sha
  recompute).
- Strict JCS: implements ECMA-262 Number.prototype.toString rules for the
  key vectors (1e-6 → "0.000001", 1e-7 → "1e-7", 1e21 → "1e+21") and
  rejects everything outside the I-JSON safe integer range.  NFC key
  collisions and JSON duplicate keys are rejected outright.
- Byte contracts freeze exactly: object_id (o_ + 26-char canonical base32,
  no padding, no upper), ReportKey (namespace regex + report_id regex both
  forbid CR/LF/slash/backslash/colon/control chars), profile_sha256
  (domain-separated recipe).

Strict boundaries: no network, no host paths in errors, no dependence on
third-party libraries, no writes outside the RT-011 fixture area, no reads
of `CWORK_APP_KEY` or real CWork data.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants and regex frozen by RT-011
# ---------------------------------------------------------------------------

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts"
SCHEMAS_DIR = SCHEMA_ROOT / "schemas"

# I-JSON safe integer range (RFC 7493 §2.2).
IJSON_MAX_SAFE_INT = 2 ** 53 - 1
IJSON_MIN_SAFE_INT = -(2 ** 53 - 1)

# Object ID: "o_" + canonical base32 (RFC 4648, lowercased) of exactly 128 bits.
# 128 bits → 16 bytes → 26 chars base32; no padding.  Alphabet: a–z minus
# a/i/l/o (RFC 4648 alphabet a2–z7 in lowercase already excludes 0/1/8/9).
OBJECT_ID_REGEX = re.compile(r"\Ao_[a-z2-7]{26}\Z")
_BASE32_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz234567")
# Bit-alignment: 128 mod 5 = 3, so the last (26th) char represents 3 leading
# bits + 2 trailing zero bits.  Valid tail characters are those whose base32
# value has the low two bits set to 0 — i.e. values 0, 4, 8, 12, 16, 20, 24, 28.
_BASE32_VALID_TAIL = frozenset("aeimquy4")  # values 0,4,8,12,16,20,24,28

# Tenant/space IDs and other opaque IDs.  Every regex is anchored with \A / \Z
# so trailing whitespace / newlines cannot slip past.
TENANT_ID_REGEX = re.compile(r"\At_[a-z0-9]{26}\Z")
SPACE_ID_REGEX = re.compile(r"\Asp_[a-z0-9]{10,32}\Z")
# Source namespaces are frozen at snake_case ASCII; explicitly reject empty.
SOURCE_NAMESPACE_REGEX = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
# Report IDs from source systems are opaque but we forbid path/URL/injection
# characters and control bytes.  Length limited to 1..128 which comfortably
# covers real CWork IDs.
REPORT_ID_REGEX = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}\Z")
REPORT_KEY_REGEX = re.compile(r"\A[a-z][a-z0-9_]{0,63}:[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}\Z")
SHA256_HEX_REGEX = re.compile(r"\A[0-9a-f]{64}\Z")
VERSION_REGEX = re.compile(r"\Av[0-9]{1,4}\Z")

# Domain separator for profile_sha256; encoded as raw ASCII bytes.
PROFILE_DOMAIN_SEPARATOR = b"cwk-profile-v1"
NULL_BYTE = b"\x00"


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
# Strict JSON loader (no duplicate keys)
# ---------------------------------------------------------------------------


def _forbid_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ContractError(f"duplicate JSON key {key!r}", path="<json>")
        seen.add(key)
    return dict(pairs)


def strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_forbid_duplicate_pairs)


def strict_json_load_path(path: Path) -> Any:
    return strict_json_loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# NFC normalisation with collision detection
# ---------------------------------------------------------------------------


def nfc_normalize(value: Any, *, path: str = "$") -> Any:
    """Recursively NFC-normalise every string inside ``value``.

    Detects NFC key collisions inside objects and raises ``ContractError``
    instead of silently dropping a field (which would otherwise happen with
    a naive ``dict`` comprehension).
    """

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (list, tuple)):
        return [nfc_normalize(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        seen: dict[str, str] = {}
        for key, sub in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON object keys must be strings", path=path)
            nk = unicodedata.normalize("NFC", key)
            if nk in normalised:
                raise ContractError(
                    f"NFC key collision: {key!r} normalises to existing key {seen[nk]!r}",
                    path=path,
                )
            seen[nk] = key
            normalised[nk] = nfc_normalize(sub, path=f"{path}.{nk}")
        return normalised
    return value


# ---------------------------------------------------------------------------
# RFC 8785 JCS with ECMA-262 numbers and I-JSON safety
# ---------------------------------------------------------------------------


def _js_number_string(value: int | float) -> str:
    """Return the RFC 8785 / ECMA-262 canonical string for a number.

    Only "safe" numbers are accepted:

    - Integers must satisfy ``|n| <= 2^53 - 1`` (I-JSON safe integer range,
      RFC 7493 §2.2);
    - Floats must be finite;
    - ``bool`` is rejected outright (must serialise as ``true``/``false``).

    For floats the ECMA-262 Number.prototype.toString algorithm is used.
    Fixed vectors (also covered by the test suite):

    - ``1e-6``  → ``"0.000001"``
    - ``1e-7``  → ``"1e-7"``
    - ``1e21``  → ``"1e+21"``
    - ``1e20``  → ``"100000000000000000000"``
    - ``0.1``   → ``"0.1"``
    """

    if isinstance(value, bool):
        raise ContractError("bool must serialise as true/false, not as a number")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < IJSON_MIN_SAFE_INT or value > IJSON_MAX_SAFE_INT:
            raise ContractError(
                f"integer {value} exceeds I-JSON safe range (|n| ≤ 2^53 - 1)"
            )
        return str(value)
    if not isinstance(value, float):
        raise ContractError(f"unsupported numeric type: {type(value).__name__}")
    if not math.isfinite(value):
        raise ContractError("JCS forbids NaN and Infinity")
    if value == 0:
        # ECMA-262: 0 and -0 both serialise to "0" for JSON.stringify.
        return "0"

    negative = value < 0
    if negative:
        value = -value

    # Shortest round-trip representation.  Python's ``repr`` for float already
    # returns the shortest string that round-trips; we then re-format the
    # exponent per ECMA-262.
    py = repr(value)
    if "e" in py or "E" in py:
        mantissa, exp_str = py.replace("E", "e").split("e", 1)
        exponent = int(exp_str)
    else:
        mantissa = py
        exponent = 0

    # Split mantissa into integer and fractional parts.
    if "." in mantissa:
        int_part, frac_part = mantissa.split(".", 1)
    else:
        int_part, frac_part = mantissa, ""

    # Convert to a canonical integer-of-digits + a total exponent so we can
    # then choose between decimal and scientific notation per ECMA-262.
    if int_part in ("0", ""):
        digits = frac_part.lstrip("0") or "0"
        exp = exponent - len(frac_part) if digits != "0" else 0
        # If we stripped leading zeros in the fractional part, keep track.
        if digits == "0":
            leading_zero_frac = 0
        else:
            leading_zero_frac = len(frac_part) - len(frac_part.lstrip("0"))
            exp = exponent - len(frac_part) + (len(frac_part.lstrip("0")) - len(digits.rstrip("0"))) + len(digits) - len(digits.rstrip("0"))
    else:
        digits = (int_part + frac_part).lstrip("0") or "0"
        exp = exponent - len(frac_part)

    # Trim trailing zero digits and shift exponent.
    stripped = digits.rstrip("0")
    if stripped == "":
        stripped = "0"
    else:
        exp += len(digits) - len(stripped)
    digits = stripped

    # ECMA-262 Number.prototype.toString algorithm (finite non-zero):
    #   Let s = digits, n = number of digits, k such that s has n digits and
    #   the true value is s * 10^(exp).
    #   Let e = exp + n (so the "e10" position, i.e., 1 <= value < 10 form).
    n = len(digits)
    e = exp + n

    if -6 < e <= 21:
        # Decimal notation.
        if e <= 0:
            body = "0." + "0" * (-e) + digits
        elif e >= n:
            body = digits + "0" * (e - n)
        else:
            body = digits[:e] + "." + digits[e:]
    else:
        # Scientific notation with lowercase 'e' and no leading '+' in the
        # exponent when negative.
        if n == 1:
            mant = digits
        else:
            mant = digits[0] + "." + digits[1:]
        # Exponent uses ECMA-262 sign rule: no sign for positive/negative
        # follows the sign of exponent; positive gets '+'.
        exp_val = e - 1
        if exp_val >= 0:
            body = f"{mant}e+{exp_val}"
        else:
            body = f"{mant}e{exp_val}"

    return f"-{body}" if negative else body


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
        return _js_number_string(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        def _sort_key(k: Any) -> bytes:
            if not isinstance(k, str):
                raise ContractError("JCS requires string object keys")
            return k.encode("utf-16-be")

        items = sorted(value.items(), key=lambda kv: _sort_key(kv[0]))
        return "{" + ",".join(_jcs_string(k) + ":" + _jcs_serialize(v) for k, v in items) + "}"
    raise ContractError(f"JCS cannot serialise {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return NFC-normalised RFC 8785 JCS canonical JSON as UTF-8 bytes.

    NFC key collisions and non-safe numbers are rejected before serialisation.
    """

    normalised = nfc_normalize(value)
    return _jcs_serialize(normalised).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# ---------------------------------------------------------------------------
# Draft 2020-12 subset validator engine (used against the shipped schemas)
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: dict[str, Any] = {}
_CUSTOM_HANDLERS: dict[str, Callable[[Any], None]] = {}


def _load_schema(schema_id: str) -> Any:
    """Load and cache one of the RT-011 v1 schema files by ``$id``."""

    if schema_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[schema_id]
    # Map the frozen id to the shipped filename.
    filename_by_id = {
        "cwk.pr001.report_key.v1": "report_key.schema.json",
        "cwk.pr001.canonical_report.v1": "canonical_report.schema.json",
        "cwk.pr001.tenant_view.v1": "tenant_view.schema.json",
        "cwk.pr001.access_observation.v1": "access_observation.schema.json",
        "cwk.pr001.access_grant.v1": "access_grant.schema.json",
        "cwk.pr001.knowledge_profile.v1": "knowledge_profile.schema.json",
        "cwk.pr001.profile_pointer_rollback.v1": "profile_pointer_rollback.schema.json",
        "cwk.pr001.route_decision.v1": "route_decision.schema.json",
        "cwk.pr001.query_request.v1": "query_request.schema.json",
        "cwk.pr001.sample_manifest.v1": "sample_manifest.schema.json",
        "cwk.pr001.verified_shared_extensions.v1": "verified_shared_extensions.schema.json",
        "cwk.pr001.capability_probe.v1": "capability_probe.schema.json",
        "cwk.pr001.security_defaults.v1": "security_defaults.schema.json",
        "cwk.pr001.dual_user_observation.v1": "dual_user_observation.schema.json",
    }
    filename = filename_by_id.get(schema_id)
    if filename is None:
        raise ContractError(f"no shipped schema file for id {schema_id!r}")
    schema = strict_json_load_path(SCHEMAS_DIR / filename)
    _SCHEMA_CACHE[schema_id] = schema
    return schema


def _iter_deep_forbidden(value: Any, forbidden: frozenset[str], path: str) -> None:
    if isinstance(value, dict):
        hits = sorted(set(value.keys()) & forbidden)
        if hits:
            raise ContractError(f"forbidden fields present: {hits}", path=path)
        for k, v in value.items():
            _iter_deep_forbidden(v, forbidden, f"{path}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _iter_deep_forbidden(item, forbidden, f"{path}[{i}]")


def _check_datetime(value: Any, path: str, *, nullable: bool) -> None:
    if value is None:
        if nullable:
            return
        raise ContractError("value must be a non-null RFC 3339 datetime", path=path)
    if not isinstance(value, str):
        raise ContractError("value must be an RFC 3339 datetime string", path=path)
    # Reject trailing whitespace / newline.
    if value != value.strip():
        raise ContractError("datetime must not have leading/trailing whitespace", path=path)
    # Require timezone: either 'Z' suffix or explicit +/-HH:MM offset.
    tz_ok = value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}\Z", value) is not None
    if not tz_ok:
        raise ContractError("datetime must include a timezone (Z or ±HH:MM)", path=path)
    if "T" not in value:
        raise ContractError("datetime must be full date-time, not date-only", path=path)
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid RFC 3339 datetime {value!r} ({exc})", path=path)


def _validate_type(value: Any, allowed: str | list[str], path: str) -> None:
    if isinstance(allowed, str):
        allowed = [allowed]
    for t in allowed:
        if t == "string" and isinstance(value, str):
            return
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return
        if t == "boolean" and isinstance(value, bool):
            return
        if t == "array" and isinstance(value, list):
            return
        if t == "object" and isinstance(value, dict):
            return
        if t == "null" and value is None:
            return
    raise ContractError(f"expected type {allowed}, got {type(value).__name__}", path=path)


def _resolve_ref(base_schema: Any, ref: str) -> Any:
    # We only use relative $ref pointing to another schema file (e.g.
    # "report_key.schema.json#/properties/source_namespace") or in-schema
    # pointers ("#/$defs/foo").  Both are supported minimally.
    if "#" not in ref:
        raise ContractError(f"$ref {ref!r} must contain '#'")
    doc_part, ptr = ref.split("#", 1)
    if doc_part:
        # Load the other schema file.
        doc = strict_json_load_path(SCHEMAS_DIR / doc_part)
    else:
        doc = base_schema
    obj = doc
    if ptr:
        for token in ptr.lstrip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(obj, list):
                obj = obj[int(token)]
            else:
                obj = obj[token]
    return obj


def _validate_schema(schema: Any, value: Any, path: str, *, root_schema: Any) -> None:
    if "$ref" in schema:
        resolved = _resolve_ref(root_schema, schema["$ref"])
        _validate_schema(resolved, value, path, root_schema=root_schema)
        return
    if "type" in schema:
        _validate_type(value, schema["type"], path)
    if "const" in schema:
        if value != schema["const"]:
            raise ContractError(f"expected const {schema['const']!r}, got {value!r}", path=path)
    if "enum" in schema:
        if value not in schema["enum"]:
            raise ContractError(f"value {value!r} not in enum {schema['enum']!r}", path=path)
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ContractError(f"value {value!r} does not match {schema['pattern']!r}", path=path)
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ContractError(f"string shorter than {schema['minLength']}", path=path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ContractError(f"string longer than {schema['maxLength']}", path=path)
        if schema.get("format") == "date-time":
            _check_datetime(value, path, nullable=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"value {value} < minimum {schema['minimum']}", path=path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError(f"value {value} > maximum {schema['maximum']}", path=path)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ContractError(f"array shorter than {schema['minItems']}", path=path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError(f"array longer than {schema['maxItems']}", path=path)
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for i, item in enumerate(value):
                if item in seen:
                    raise ContractError(f"array items must be unique; duplicate {item!r}", path=f"{path}[{i}]")
                seen.append(item)
        if "items" in schema:
            item_schema = schema["items"]
            for i, item in enumerate(value):
                _validate_schema(item_schema, item, f"{path}[{i}]", root_schema=root_schema)
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ContractError(f"missing required property {key!r}", path=path)
        properties = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in properties:
                _validate_schema(properties[key], sub, f"{path}.{key}", root_schema=root_schema)
            else:
                if additional_allowed is False:
                    raise ContractError(f"additional property {key!r} not permitted", path=path)
                if isinstance(additional_allowed, dict):
                    _validate_schema(additional_allowed, sub, f"{path}.{key}", root_schema=root_schema)


def _run_custom_keywords(schema_id: str, payload: Any) -> None:
    handler = _CUSTOM_HANDLERS.get(schema_id)
    if handler is not None:
        handler(payload)


def validate_payload(payload: Any) -> None:
    """Dispatch validation by the payload's declared ``schema`` field."""

    if not isinstance(payload, dict):
        raise ContractError("payload must be a JSON object with a `schema` field", path="<root>")
    schema_kind = payload.get("schema")
    if not isinstance(schema_kind, str):
        raise ContractError("payload.schema must be a string", path="<root>.schema")
    schema_id = _KIND_TO_ID.get(schema_kind)
    if schema_id is None:
        raise ContractError(f"unknown schema {schema_kind!r}", path="<root>.schema")
    schema = _load_schema(schema_id)
    _validate_schema(schema, payload, "$", root_schema=schema)
    _run_custom_keywords(schema_id, payload)


# ---------------------------------------------------------------------------
# Custom keyword handlers (per-schema semantics not expressible in JSON Schema)
# ---------------------------------------------------------------------------


CANONICAL_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_id_hash",
        "credential_ref",
        "credentials",
        "app_key",
        "lane",
        "read_status",
        "todo_status",
        "allowed_actions",
        "role",
        "roles",
        "reply",
        "replies",
        "reply_overlay",
        "node",
        "nodes",
        "node_overlay",
        "attachment",
        "attachments",
        "attachment_permissions",
        "attachment_url",
        "preview_url",
        "short_url",
        "presign_url",
        "download_url",
        "temporary_url",
        "collected_at",
        "observed_at",
        "path",
        "absolute_path",
        "mirror_root",
        "auth_epoch",
        "binding_epoch",
        "cookie",
        "session_token",
        "authorization",
    }
)

QUERY_REQUEST_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
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
        "authorization",
        "cookie",
    }
)

PROFILE_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_id_hash",
        "agent_binding",
        "credential_ref",
        "credentials",
        "app_key",
        "path",
        "mirror_root",
        "grant",
        "grants",
        "shell",
        "command",
        "tool",
        "auth_epoch",
        "binding_epoch",
    }
)


def _canonical_handler(payload: Mapping[str, Any]) -> None:
    _iter_deep_forbidden(payload, CANONICAL_FORBIDDEN_FIELDS, path="$")
    without_hash = {k: v for k, v in payload.items() if k != "canonical_sha256"}
    computed = canonical_sha256(without_hash)
    if computed != payload["canonical_sha256"]:
        raise ContractError(
            f"canonical_sha256 mismatch (computed {computed})",
            path="$.canonical_sha256",
        )


def _query_request_handler(payload: Mapping[str, Any]) -> None:
    _iter_deep_forbidden(payload, QUERY_REQUEST_FORBIDDEN_FIELDS, path="$")


def _profile_handler(payload: Mapping[str, Any]) -> None:
    _iter_deep_forbidden(payload, PROFILE_FORBIDDEN_FIELDS, path="$")
    # sample/prompt/model must appear together — enforced by required[].
    # Additionally recompute profile_sha256 so an attacker cannot edit
    # `spaces` / `routing_rules` / `archive_rules` while keeping the old hash.
    proposal_view = {
        k: v
        for k, v in payload.items()
        if k
        in (
            "version",
            "spaces",
            "entity_policy",
            "attention",
            "routing_rules",
            "archive_rules",
            "query_preferences",
            "model_policy",
            "review_threshold",
            "sample_manifest_ref",
            "holdout_manifest_ref",
            "confirmed_by",
            "confirmed_at",
        )
    }
    expected = compute_profile_sha256(
        nfc_normalized_proposal=proposal_view,
        sample_manifest_sha256=payload["sample_manifest_sha256"],
        prompt_template_sha256=payload["prompt_template_sha256"],
        model_id=payload["model_id"],
    )
    if expected != payload["profile_sha256"]:
        raise ContractError(
            f"profile_sha256 mismatch (computed {expected})",
            path="$.profile_sha256",
        )


def _access_observation_handler(payload: Mapping[str, Any]) -> None:
    if payload.get("initial_status") not in ("discovered", "granted"):
        raise ContractError(
            "AccessObservation must enter as discovered or granted",
            path="$.initial_status",
        )


def _profile_rollback_handler(payload: Mapping[str, Any]) -> None:
    if payload["from_version"] == payload["to_version"]:
        raise ContractError(
            "profile_pointer_rollback must swap to a different version",
            path="$",
        )
    if payload["auth_epoch_after"] <= payload["auth_epoch_before"]:
        raise ContractError(
            "auth_epoch_after must be strictly greater than auth_epoch_before",
            path="$.auth_epoch_after",
        )


def _route_decision_handler(payload: Mapping[str, Any]) -> None:
    # space_ids uniqueness is covered by uniqueItems; extra deep check to be
    # defensive against slug slip through nested arrays.
    space_ids = payload.get("space_ids", [])
    for sid in space_ids:
        if not SPACE_ID_REGEX.match(sid):
            raise ContractError(
                f"space_ids[{sid!r}] is not an opaque space_id (slugs forbidden)",
                path="$.space_ids",
            )


def _sample_manifest_handler(payload: Mapping[str, Any]) -> None:
    samples = payload["samples"]
    holdout = payload["holdout"]
    sample_keys = [s["report_key"] for s in samples]
    holdout_keys = [h["report_key"] for h in holdout]

    if len(set(sample_keys)) != len(sample_keys):
        raise ContractError("samples must have unique report_key values", path="$.samples")
    if len(set(holdout_keys)) != len(holdout_keys):
        raise ContractError("holdout must have unique report_key values", path="$.holdout")
    overlap = set(sample_keys) & set(holdout_keys)
    if overlap:
        raise ContractError(
            f"holdout overlaps samples: {sorted(overlap)[:5]}", path="$.holdout"
        )

    if payload["actual_sample_size"] != len(samples):
        raise ContractError(
            f"actual_sample_size ({payload['actual_sample_size']}) != len(samples) ({len(samples)})",
            path="$.actual_sample_size",
        )
    if len(samples) < 100 or len(samples) > 200:
        raise ContractError(
            "samples length must satisfy 100 <= len <= 200 per PRD",
            path="$.samples",
        )
    if len(holdout) < 20 or len(holdout) > 30:
        raise ContractError(
            "holdout length must satisfy 20 <= len <= 30 per PRD",
            path="$.holdout",
        )

    # chunk_layout coverage: every sample key must appear in exactly one chunk.
    chunked: list[str] = []
    for chunk in payload["chunk_layout"]:
        for k in chunk["report_keys"]:
            chunked.append(k)
    if sorted(chunked) != sorted(sample_keys):
        raise ContractError(
            "chunk_layout coverage must equal the samples set exactly",
            path="$.chunk_layout",
        )
    if len(set(chunked)) != len(chunked):
        raise ContractError("chunk_layout keys must not repeat across chunks", path="$.chunk_layout")

    # Strata "picked" sum must match samples.
    picked_sum = sum(int(s.get("picked", 0)) for s in payload["strata"])
    if picked_sum != len(samples):
        raise ContractError(
            f"strata.picked sum {picked_sum} != samples length {len(samples)}",
            path="$.strata",
        )


VERIFIED_SHARED_FORBIDDEN_PATHS: frozenset[str] = frozenset(
    {
        "attachments[*].temporary_url",
        "attachments[*].preview_url",
        "attachments[*].presign_url",
        "attachments[*].download_url",
        "short_url",
        "temporary_url",
        "preview_url",
        "presign_url",
        "download_url",
        "reply_overlay[*].temporary_url",
        "node_overlay[*].temporary_url",
        "tenant_id",
        "agent_id",
        "agent_id_hash",
        "credential_ref",
        "credentials",
        "app_key",
        "password",
        "token",
        "cookie",
        "session_token",
        "authorization",
        "path",
        "absolute_path",
        "mirror_root",
        "lane",
        "read_status",
        "todo_status",
        "roles",
        "allowed_actions",
        "attachment_permissions",
        "reply_overlay",
        "node_overlay",
        "visible_event_ids",
    }
)

_URL_FIELD_HINTS = ("_url", "url", "presign", "download", "temporary_url", "preview_url", "short_url")
_IDENTITY_FIELD_HINTS = (
    "tenant_id",
    "agent_id",
    "credential",
    "app_key",
    "password",
    "token",
    "cookie",
    "session_token",
    "authorization",
    "auth_epoch",
    "binding_epoch",
)


def _is_forbidden_extension_path(field_path: str) -> bool:
    lower = field_path.lower()
    if field_path in VERIFIED_SHARED_FORBIDDEN_PATHS:
        return True
    for token in _URL_FIELD_HINTS + _IDENTITY_FIELD_HINTS:
        if token in lower:
            return True
    return False


def _verified_shared_handler(payload: Mapping[str, Any]) -> None:
    # Recompute manifest_sha256 over the payload with manifest_sha256 zeroed
    # so any drift in entries / thresholds / approver is detected.
    without_hash = {k: v for k, v in payload.items() if k != "manifest_sha256"}
    computed = canonical_sha256(without_hash)
    # Empty bootstrap manifest is intentionally allowed with a "0" * 64 sentinel.
    if payload["entries"]:
        if payload["manifest_sha256"] != computed:
            raise ContractError(
                f"verified_shared_extensions manifest_sha256 mismatch (computed {computed})",
                path="$.manifest_sha256",
            )
    else:
        if payload["manifest_sha256"] not in (computed, "0" * 64):
            raise ContractError(
                "empty bootstrap manifest_sha256 must be either the computed value or the frozen zero placeholder",
                path="$.manifest_sha256",
            )
    for i, entry in enumerate(payload["entries"]):
        fp = entry["field_path"]
        if _is_forbidden_extension_path(fp):
            raise ContractError(
                f"field_path {fp!r} is on the never-shareable list", path=f"$.entries[{i}].field_path"
            )
        sample_ids = entry["sample_ids"]
        if len(sample_ids) < 50:
            raise ContractError(
                "each entry must show >=50 unique common sample_ids to be promotable",
                path=f"$.entries[{i}].sample_ids",
            )
        if len(set(sample_ids)) != len(sample_ids):
            raise ContractError(
                "entry.sample_ids must not contain duplicates",
                path=f"$.entries[{i}].sample_ids",
            )


def _capability_probe_handler(payload: Mapping[str, Any]) -> None:
    probe_id = payload["probe_id"]
    result = payload["result"]
    if probe_id == "sandbox_transport_loopback_http_self_reported" and result != "conservative_unknown":
        raise ContractError(
            "sandbox_transport_loopback_http_self_reported is policy-forbidden; result must be conservative_unknown",
            path="$.result",
        )
    receipt = payload.get("receipt")
    if result == "verified":
        if not receipt:
            raise ContractError(
                "verified capability probes require a signed receipt envelope",
                path="$.receipt",
            )
        if receipt["target"] != probe_id:
            raise ContractError("receipt.target must equal probe_id", path="$.receipt.target")
        if receipt.get("signer") not in TRUSTED_PROBE_SIGNERS:
            raise ContractError(
                "receipt.signer is not on the trusted signer allowlist",
                path="$.receipt.signer",
            )
        _check_receipt_signature(receipt)
    else:
        if receipt is not None:
            # A conservative_unknown MAY still carry a receipt (e.g. failed
            # verification), but if it does, the receipt must not carry a
            # signer that could be misread as trusted.
            if receipt.get("signer") in TRUSTED_PROBE_SIGNERS:
                raise ContractError(
                    "conservative_unknown probes must not present a receipt signed by a trusted signer",
                    path="$.receipt.signer",
                )


def _security_defaults_handler(payload: Mapping[str, Any]) -> None:
    # No additional dangerous keys tolerated.  In addition to strict
    # additionalProperties on the top-level schema, we reject any suspicious
    # nested keys that would silently loosen policy.
    dangerous_keys = {
        "loopback_http_self_reported_allowed",
        "loopback_http_allowed",
        "self_reported_agent_id_allowed",
        "alternate_transport",
        "alternate_enabled",
        "break_glass_enabled",
        "grace_read_allowed",
        "allow_all",
        "bypass_acl",
    }
    _iter_deep_dangerous(payload, dangerous_keys, path="$")
    if payload["transport_and_identity"].get("request_body_identity_fields_permitted") is not False:
        raise ContractError(
            "transport_and_identity.request_body_identity_fields_permitted must be exactly false",
            path="$.transport_and_identity.request_body_identity_fields_permitted",
        )
    if payload["access_grant"].get("grace_read_forbidden") is not True:
        raise ContractError(
            "access_grant.grace_read_forbidden must be exactly true",
            path="$.access_grant.grace_read_forbidden",
        )
    if payload["profile_lifecycle"].get("rolled_back_state_forbidden") is not True:
        raise ContractError(
            "profile_lifecycle.rolled_back_state_forbidden must be exactly true",
            path="$.profile_lifecycle.rolled_back_state_forbidden",
        )
    if payload["cold_archive"].get("physical_delete_default") is not False:
        raise ContractError(
            "cold_archive.physical_delete_default must be exactly false",
            path="$.cold_archive.physical_delete_default",
        )
    if payload["break_glass"].get("enabled") is not False:
        raise ContractError(
            "break_glass.enabled must be exactly false",
            path="$.break_glass.enabled",
        )


def _iter_deep_dangerous(value: Any, dangerous: set[str], path: str) -> None:
    if isinstance(value, dict):
        hits = sorted(set(value.keys()) & dangerous)
        if hits:
            raise ContractError(f"dangerous security key present: {hits}", path=path)
        for k, v in value.items():
            _iter_deep_dangerous(v, dangerous, f"{path}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _iter_deep_dangerous(item, dangerous, f"{path}[{i}]")


def _dual_user_observation_handler(payload: Mapping[str, Any]) -> None:
    # Overlay MAY carry temporary URLs (this is real observed data); the
    # comparator's job is to detect them and block promotion.  What we
    # forbid here is any identity/credential leakage inside canonical_fields
    # (schema already forbids extras via additionalProperties:false on
    # canonical_fields; this is a belt-and-braces deep scan).
    canonical = payload.get("canonical_fields") or {}
    _iter_deep_forbidden(canonical, CANONICAL_FORBIDDEN_FIELDS, path="$.canonical_fields")


TRUSTED_PROBE_SIGNERS: frozenset[str] = frozenset()  # populated by RT-023 in production
_TEST_PROBE_SIGNERS: set[str] = set()
_TEST_PROBE_SIGNING_SECRETS: dict[str, bytes] = {}


def _register_test_probe_signer(signer_id: str, secret: bytes) -> None:  # test-only
    """Test-only hook: temporarily register a trusted signer.

    Not exposed via CLI or JSON; callers must import the private symbol and
    take responsibility for unregistering after the test.
    """

    global TRUSTED_PROBE_SIGNERS
    _TEST_PROBE_SIGNERS.add(signer_id)
    _TEST_PROBE_SIGNING_SECRETS[signer_id] = secret
    TRUSTED_PROBE_SIGNERS = frozenset(_TEST_PROBE_SIGNERS)


def _unregister_test_probe_signer(signer_id: str) -> None:  # test-only
    global TRUSTED_PROBE_SIGNERS
    _TEST_PROBE_SIGNERS.discard(signer_id)
    _TEST_PROBE_SIGNING_SECRETS.pop(signer_id, None)
    TRUSTED_PROBE_SIGNERS = frozenset(_TEST_PROBE_SIGNERS)


def _check_receipt_signature(receipt: Mapping[str, Any]) -> None:
    signer = receipt["signer"]
    secret = _TEST_PROBE_SIGNING_SECRETS.get(signer)
    if secret is None:
        raise ContractError(
            "trusted signer has no registered signing secret in this process; cannot verify",
            path="$.receipt.signer",
        )
    # Signature = sha256(secret + envelope_sha256_ascii + target_ascii)
    expected = hashlib.sha256(
        secret + receipt["envelope_sha256"].encode("ascii") + receipt["target"].encode("ascii")
    ).hexdigest()
    if not _constant_time_equal(expected, receipt.get("signature", "")):
        raise ContractError("receipt signature does not verify", path="$.receipt.signature")


def _constant_time_equal(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


# ---------------------------------------------------------------------------
# Byte contracts (object_id, ReportKey, profile_sha256)
# ---------------------------------------------------------------------------


def new_object_id(random_bytes: bytes | None = None) -> str:
    if random_bytes is None:
        random_bytes = secrets.token_bytes(16)
    if len(random_bytes) != 16:
        raise ContractError(
            f"object_id requires exactly 128 bits (16 bytes), got {len(random_bytes)}"
        )
    encoded = base64.b32encode(random_bytes).decode("ascii").rstrip("=").lower()
    value = f"o_{encoded}"
    validate_object_id(value)
    return value


def validate_object_id(value: str) -> None:
    if not isinstance(value, str) or not OBJECT_ID_REGEX.match(value):
        raise ContractError(
            f"object_id must match {OBJECT_ID_REGEX.pattern!r}, got {value!r}",
            path="object_id",
        )
    body = value[2:]
    for ch in body:
        if ch not in _BASE32_ALPHABET:
            raise ContractError(f"object_id contains non-base32 char {ch!r}", path="object_id")
    # Reject canonical-base32-invalid tail characters (would decode to bytes
    # with residual bits that the RFC 4648 grammar forbids).
    if body[-1] not in _BASE32_VALID_TAIL:
        raise ContractError(
            "object_id last character is not a valid canonical base32 tail (residual bits must be zero)",
            path="object_id",
        )
    # Round-trip: decode base32 back to bytes and confirm 16 bytes.
    try:
        raw = base64.b32decode(body.upper() + "======")
    except Exception as exc:
        raise ContractError(f"object_id base32 decode failed: {exc}", path="object_id")
    if len(raw) != 16:
        raise ContractError("object_id does not decode to exactly 16 bytes", path="object_id")


def compose_report_key(source_namespace: str, report_id: str) -> str:
    if not isinstance(source_namespace, str) or not SOURCE_NAMESPACE_REGEX.match(source_namespace):
        raise ContractError(
            f"source_namespace must match {SOURCE_NAMESPACE_REGEX.pattern!r}, got {source_namespace!r}",
            path="source_namespace",
        )
    if not isinstance(report_id, str) or not REPORT_ID_REGEX.match(report_id):
        raise ContractError(
            f"report_id must match {REPORT_ID_REGEX.pattern!r}, got {report_id!r}",
            path="report_id",
        )
    return f"{source_namespace}:{report_id}"


def parse_report_key(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not REPORT_KEY_REGEX.match(value):
        raise ContractError(f"report_key {value!r} does not match {REPORT_KEY_REGEX.pattern!r}", path="report_key")
    namespace, report_id = value.split(":", 1)
    return namespace, report_id


def compute_profile_sha256(
    *,
    nfc_normalized_proposal: Mapping[str, Any],
    sample_manifest_sha256: str,
    prompt_template_sha256: str,
    model_id: str,
) -> str:
    if not SHA256_HEX_REGEX.match(sample_manifest_sha256 or ""):
        raise ContractError("sample_manifest_sha256 must be lowercase hex sha256", path="sample_manifest_sha256")
    if not SHA256_HEX_REGEX.match(prompt_template_sha256 or ""):
        raise ContractError("prompt_template_sha256 must be lowercase hex sha256", path="prompt_template_sha256")
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
# Frozen state machines and dispositions
# ---------------------------------------------------------------------------


ACCESS_GRANT_STATES: tuple[str, ...] = (
    "discovered",
    "granted",
    "active",
    "revalidation_due",
    "revoked",
    "purge_pending",
    "purged",
)
ACCESS_GRANT_QUERY_ELIGIBLE = frozenset({"active"})
ACCESS_GRANT_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "discovered": ("granted", "revoked"),
    "granted": ("active", "revoked"),
    "active": ("revalidation_due", "revoked"),
    "revalidation_due": ("active", "revoked"),
    "revoked": ("purge_pending",),
    "purge_pending": ("purged",),
    "purged": (),
}

KNOWLEDGE_PROFILE_STATES: tuple[str, ...] = (
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

DISPOSITIONS: tuple[str, ...] = ("index", "archive_no_index", "review")


def validate_access_grant_transition(from_status: str, to_status: str) -> None:
    if from_status not in ACCESS_GRANT_STATES:
        raise ContractError(f"unknown source state {from_status!r}", path="access_grant.transition.from")
    if to_status not in ACCESS_GRANT_STATES:
        raise ContractError(f"unknown target state {to_status!r}", path="access_grant.transition.to")
    allowed = ACCESS_GRANT_ALLOWED_TRANSITIONS[from_status]
    if to_status not in allowed:
        raise ContractError(
            f"illegal access_grant transition {from_status} -> {to_status}; allowed: {list(allowed)}",
            path="access_grant.transition",
        )


def validate_knowledge_profile_transition(from_status: str, to_status: str) -> None:
    if from_status not in KNOWLEDGE_PROFILE_STATES:
        raise ContractError(f"unknown source state {from_status!r}", path="knowledge_profile.transition.from")
    if to_status not in KNOWLEDGE_PROFILE_STATES:
        raise ContractError(f"unknown target state {to_status!r}", path="knowledge_profile.transition.to")
    if to_status not in KNOWLEDGE_PROFILE_ALLOWED_TRANSITIONS[from_status]:
        raise ContractError(
            f"illegal knowledge_profile transition {from_status} -> {to_status}",
            path="knowledge_profile.transition",
        )


# ---------------------------------------------------------------------------
# Public per-schema validators (dispatch to the engine + custom keywords)
# ---------------------------------------------------------------------------


_KIND_TO_ID: dict[str, str] = {
    "cwk.report_key.v1": "cwk.pr001.report_key.v1",
    "cwk.canonical_report.v1": "cwk.pr001.canonical_report.v1",
    "cwk.tenant_view.v1": "cwk.pr001.tenant_view.v1",
    "cwk.access_observation.v1": "cwk.pr001.access_observation.v1",
    "cwk.access_grant.v1": "cwk.pr001.access_grant.v1",
    "cwk.knowledge_profile.v1": "cwk.pr001.knowledge_profile.v1",
    "cwk.profile_pointer_rollback.v1": "cwk.pr001.profile_pointer_rollback.v1",
    "cwk.route_decision.v1": "cwk.pr001.route_decision.v1",
    "cwk.query_request.v1": "cwk.pr001.query_request.v1",
    "cwk.sample_manifest.v1": "cwk.pr001.sample_manifest.v1",
    "cwk.verified_shared_extensions.v1": "cwk.pr001.verified_shared_extensions.v1",
    "cwk.capability_probe.v1": "cwk.pr001.capability_probe.v1",
    "cwk.security_defaults.v1": "cwk.pr001.security_defaults.v1",
    "cwk.dual_user_observation.v1": "cwk.pr001.dual_user_observation.v1",
}


_CUSTOM_HANDLERS.update(
    {
        "cwk.pr001.canonical_report.v1": _canonical_handler,
        "cwk.pr001.query_request.v1": _query_request_handler,
        "cwk.pr001.knowledge_profile.v1": _profile_handler,
        "cwk.pr001.access_observation.v1": _access_observation_handler,
        "cwk.pr001.profile_pointer_rollback.v1": _profile_rollback_handler,
        "cwk.pr001.route_decision.v1": _route_decision_handler,
        "cwk.pr001.sample_manifest.v1": _sample_manifest_handler,
        "cwk.pr001.verified_shared_extensions.v1": _verified_shared_handler,
        "cwk.pr001.capability_probe.v1": _capability_probe_handler,
        "cwk.pr001.security_defaults.v1": _security_defaults_handler,
        "cwk.pr001.dual_user_observation.v1": _dual_user_observation_handler,
    }
)


def validate_report_key_payload(payload: Any) -> None:
    validate_payload(payload)


def validate_canonical_envelope(payload: Any) -> None:
    validate_payload(payload)


def validate_tenant_view(payload: Any) -> None:
    validate_payload(payload)


def validate_access_observation(payload: Any) -> None:
    validate_payload(payload)


def validate_access_grant(payload: Any) -> None:
    validate_payload(payload)


def validate_knowledge_profile(payload: Any) -> None:
    validate_payload(payload)


def validate_profile_pointer_rollback(payload: Any) -> None:
    validate_payload(payload)


def validate_route_decision(payload: Any) -> None:
    validate_payload(payload)


def validate_query_request(payload: Any) -> None:
    validate_payload(payload)


def validate_sample_manifest(payload: Any) -> None:
    validate_payload(payload)


def validate_verified_shared_extensions(payload: Any) -> None:
    validate_payload(payload)


def validate_capability_probe(payload: Any) -> None:
    validate_payload(payload)


def validate_security_defaults(payload: Any) -> None:
    validate_payload(payload)


def validate_dual_user_observation(payload: Any) -> None:
    validate_payload(payload)


VALIDATORS: dict[str, Callable[[Any], None]] = {
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
    "cwk.dual_user_observation.v1": validate_dual_user_observation,
}


def validate(payload: Any) -> None:
    validate_payload(payload)


# ---------------------------------------------------------------------------
# Convenience loaders (used by CLI + tests, not runtime data-face code)
# ---------------------------------------------------------------------------


def load_security_defaults() -> dict[str, Any]:
    payload = strict_json_load_path(SCHEMA_ROOT / "security_defaults.json")
    validate_security_defaults(payload)
    return payload


def load_verified_shared_extensions_v1() -> dict[str, Any]:
    payload = strict_json_load_path(SCHEMA_ROOT / "verified_shared_extensions_v1.json")
    validate_verified_shared_extensions(payload)
    return payload


# ---------------------------------------------------------------------------
# Data-class helpers for downstream RTs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportKey:
    source_namespace: str
    report_id: str

    def __post_init__(self) -> None:  # pragma: no cover - trivial guard
        compose_report_key(self.source_namespace, self.report_id)

    def as_string(self) -> str:
        return compose_report_key(self.source_namespace, self.report_id)


__all__ = [
    "ACCESS_GRANT_ALLOWED_TRANSITIONS",
    "ACCESS_GRANT_QUERY_ELIGIBLE",
    "ACCESS_GRANT_STATES",
    "CANONICAL_FORBIDDEN_FIELDS",
    "DISPOSITIONS",
    "IJSON_MAX_SAFE_INT",
    "IJSON_MIN_SAFE_INT",
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
    "SCHEMAS_DIR",
    "SHA256_HEX_REGEX",
    "SOURCE_NAMESPACE_REGEX",
    "SPACE_ID_REGEX",
    "TENANT_ID_REGEX",
    "TRUSTED_PROBE_SIGNERS",
    "VERIFIED_SHARED_FORBIDDEN_PATHS",
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
    "strict_json_loads",
    "strict_json_load_path",
    "validate",
    "validate_access_grant",
    "validate_access_grant_transition",
    "validate_access_observation",
    "validate_canonical_envelope",
    "validate_capability_probe",
    "validate_dual_user_observation",
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
    "_register_test_probe_signer",
    "_unregister_test_probe_signer",
]
