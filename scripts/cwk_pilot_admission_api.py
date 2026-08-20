#!/usr/bin/env python3
"""PR-001 cross-RT pilot-admission ABI.

This module is the neutral contract shared by the RT-017 collector,
RT-019 profile workflow and RT-022 query broker.  It deliberately does not
know where the admission policy lives.  A provider is bound to exactly one
purpose at construction time; callers can only request a snapshot for a
trusted :class:`cwk_agent_context.AgentContextSnapshot`.

The production policy adapter belongs to RT-026.  Until that adapter is
injected, :class:`NullPilotAdmissionProvider` fails closed with the stable
``unavailable`` error.  Importing or using this module never reads the
environment, current working directory, filesystem, network or credentials.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable

import cwk_agent_context as _AC
import cwk_pr001_contracts as _C


PILOT_ADMISSION_PROVIDER_API_VERSION = "cwk.pilot_admission_provider.v1"
PILOT_ADMISSION_SNAPSHOT_SCHEMA = "cwk.pilot_admission_snapshot.v1"
PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN = b"cwk-pilot-admission-snapshot-v1\x00"
PILOT_ADMISSION_MAX_TTL_SECONDS = 300
PILOT_ADMISSION_FIXED_VECTOR_JCS = (
    b'{"admission_policy_revision":1,'
    b'"admission_policy_sha256":"1111111111111111111111111111111111111111111111111111111111111111",'
    b'"admitted":true,"as_of":"2026-08-20T00:00:00Z",'
    b'"expires_at":"2026-08-20T00:05:00Z","purpose":"query_broker",'
    b'"schema":"cwk.pilot_admission_snapshot.v1",'
    b'"tenant_id":"t_aaaaaaaaaaaaaaaaaaaaaaaaaa"}'
)
PILOT_ADMISSION_FIXED_VECTOR_SHA256 = (
    "f5d1f7b4269b71db7f50985d00b600c8a950eb7e09844bbbee99bbf8694f2528"
)

PILOT_ADMISSION_PURPOSES: tuple[str, ...] = (
    "collector_run",
    "profile_workflow",
    "query_broker",
)

PILOT_ADMISSION_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "schema",
    "tenant_id",
    "purpose",
    "admitted",
    "admission_policy_revision",
    "admission_policy_sha256",
    "as_of",
    "expires_at",
    "snapshot_sha256",
)

_HASHED_FIELDS = PILOT_ADMISSION_SNAPSHOT_FIELDS[:-1]
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_RFC3339_UTC_SECONDS_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_UTC = _dt.timezone.utc


class PilotAdmissionError(Exception):
    """Base class for stable, fail-closed admission errors."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PilotAdmissionContractError(PilotAdmissionError):
    """The provider or returned snapshot violates the frozen ABI."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="contract")


class PilotAdmissionDenied(PilotAdmissionError):
    """The authoritative policy evaluated this tenant/purpose as denied."""

    def __init__(self) -> None:
        super().__init__("pilot admission denied", code="denied")


class PilotAdmissionUnavailable(PilotAdmissionError):
    """No authoritative admission provider is available."""

    def __init__(self) -> None:
        super().__init__("pilot admission provider unavailable", code="unavailable")


@dataclass(frozen=True, slots=True)
class PilotAdmissionSnapshotV1:
    """Closed, immutable pilot admission result.

    ``snapshot_sha256`` authenticates the other eight fields with
    :data:`PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN`.  It is an integrity binding,
    not a signature; the consumer must still obtain the snapshot from its
    injected trusted provider.
    """

    schema: str
    tenant_id: str
    purpose: str
    admitted: bool
    admission_policy_revision: int
    admission_policy_sha256: str
    as_of: str
    expires_at: str
    snapshot_sha256: str

    def to_payload(self) -> dict[str, Any]:
        """Return the exact nine-field JSON object in frozen field order."""

        return {
            "schema": self.schema,
            "tenant_id": self.tenant_id,
            "purpose": self.purpose,
            "admitted": self.admitted,
            "admission_policy_revision": self.admission_policy_revision,
            "admission_policy_sha256": self.admission_policy_sha256,
            "as_of": self.as_of,
            "expires_at": self.expires_at,
            "snapshot_sha256": self.snapshot_sha256,
        }


@runtime_checkable
class PilotAdmissionProviderV1(Protocol):
    """Constructor-purpose-bound provider ABI.

    Implementations bind ``purpose`` in their constructor.  The request
    method intentionally has no purpose argument, preventing a caller from
    switching policy surfaces per call.
    """

    API_VERSION: str

    @property
    def purpose(self) -> str:  # pragma: no cover - protocol declaration
        ...

    def snapshot(
        self, *, agent_snapshot: _AC.AgentContextSnapshot
    ) -> PilotAdmissionSnapshotV1:  # pragma: no cover - protocol declaration
        ...


def _require_purpose(purpose: Any) -> str:
    if type(purpose) is not str or purpose not in PILOT_ADMISSION_PURPOSES:
        raise PilotAdmissionContractError(
            "purpose must be one of collector_run, profile_workflow, query_broker"
        )
    return purpose


def _payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, PilotAdmissionSnapshotV1):
        return payload.to_payload()
    if not isinstance(payload, Mapping):
        raise PilotAdmissionContractError("snapshot must be a mapping or PilotAdmissionSnapshotV1")
    try:
        copied = dict(payload)
    except (TypeError, ValueError) as exc:
        raise PilotAdmissionContractError("snapshot mapping could not be copied") from exc
    if not all(type(key) is str for key in copied):
        raise PilotAdmissionContractError("snapshot field names must be strings")
    return copied


def compute_pilot_admission_snapshot_sha256(payload: Any) -> str:
    """Hash exactly the eight non-hash snapshot fields.

    The input may be the eight-field preimage or the complete nine-field
    snapshot.  No other omission is permitted: missing or extra fields fail
    before canonicalisation.  Consequently ``snapshot_sha256`` is the only
    excluded field.
    """

    body = _payload_dict(payload)
    actual = frozenset(body)
    complete = frozenset(PILOT_ADMISSION_SNAPSHOT_FIELDS)
    preimage = frozenset(_HASHED_FIELDS)
    if actual == complete:
        del body["snapshot_sha256"]
    elif actual != preimage:
        missing = sorted(preimage - actual)
        extra = sorted(actual - complete)
        raise PilotAdmissionContractError(
            f"snapshot hash surface mismatch; missing={missing!r}, extra={extra!r}"
        )
    try:
        canonical = _C.canonical_json_bytes(body)
    except _C.ContractError as exc:
        raise PilotAdmissionContractError("snapshot is not canonical-JSON safe") from exc
    return hashlib.sha256(PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN + canonical).hexdigest()


def _parse_rfc3339_utc_seconds(value: Any, *, field: str) -> _dt.datetime:
    if type(value) is not str or _RFC3339_UTC_SECONDS_RE.fullmatch(value) is None:
        raise PilotAdmissionContractError(
            f"{field} must be RFC3339 UTC at second precision with trailing Z"
        )
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise PilotAdmissionContractError(f"{field} is not a real UTC timestamp") from exc
    return parsed.replace(tzinfo=_UTC)


def _normalise_now(now: Any) -> _dt.datetime:
    if not isinstance(now, _dt.datetime) or now.tzinfo is None:
        raise PilotAdmissionContractError("now must be a timezone-aware datetime")
    try:
        offset = now.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise PilotAdmissionContractError("now has an invalid timezone") from exc
    if offset is None or offset != _dt.timedelta(0):
        raise PilotAdmissionContractError("now must use a zero-offset UTC timezone")
    return now.astimezone(_UTC)


def validate_pilot_admission_snapshot(
    payload: Any,
    *,
    agent_snapshot: _AC.AgentContextSnapshot,
    expected_purpose: str,
    now: _dt.datetime,
) -> PilotAdmissionSnapshotV1:
    """Validate and bind a provider result to one agent snapshot and purpose.

    This validates structure, types, tenant/purpose equality, integrity,
    strict UTC timestamps, maximum TTL and freshness.  ``admitted=false`` is
    a valid policy result; use :func:`require_pilot_admission` when denial
    must stop an operation.
    """

    if not isinstance(agent_snapshot, _AC.AgentContextSnapshot):
        raise PilotAdmissionContractError("agent_snapshot must be AgentContextSnapshot")
    purpose = _require_purpose(expected_purpose)
    observed_now = _normalise_now(now)
    body = _payload_dict(payload)

    actual_fields = frozenset(body)
    expected_fields = frozenset(PILOT_ADMISSION_SNAPSHOT_FIELDS)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        extra = sorted(actual_fields - expected_fields)
        raise PilotAdmissionContractError(
            f"snapshot field set mismatch; missing={missing!r}, extra={extra!r}"
        )

    if type(body["schema"]) is not str or body["schema"] != PILOT_ADMISSION_SNAPSHOT_SCHEMA:
        raise PilotAdmissionContractError("snapshot schema is not cwk.pilot_admission_snapshot.v1")
    if type(body["tenant_id"]) is not str or _C.TENANT_ID_REGEX.fullmatch(body["tenant_id"]) is None:
        raise PilotAdmissionContractError("tenant_id is invalid")
    if body["tenant_id"] != agent_snapshot.tenant_id:
        raise PilotAdmissionContractError("snapshot tenant_id does not match agent_snapshot")
    _require_purpose(body["purpose"])
    if body["purpose"] != purpose:
        raise PilotAdmissionContractError("snapshot purpose does not match provider purpose")
    if type(body["admitted"]) is not bool:
        raise PilotAdmissionContractError("admitted must be a boolean")
    if (
        type(body["admission_policy_revision"]) is not int
        or body["admission_policy_revision"] < 1
        or body["admission_policy_revision"] > _C.IJSON_MAX_SAFE_INT
    ):
        raise PilotAdmissionContractError("admission_policy_revision must be a positive safe integer")
    if (
        type(body["admission_policy_sha256"]) is not str
        or _SHA256_RE.fullmatch(body["admission_policy_sha256"]) is None
    ):
        raise PilotAdmissionContractError("admission_policy_sha256 must be lowercase SHA-256")
    if type(body["snapshot_sha256"]) is not str or _SHA256_RE.fullmatch(body["snapshot_sha256"]) is None:
        raise PilotAdmissionContractError("snapshot_sha256 must be lowercase SHA-256")

    as_of = _parse_rfc3339_utc_seconds(body["as_of"], field="as_of")
    expires_at = _parse_rfc3339_utc_seconds(body["expires_at"], field="expires_at")
    ttl_seconds = (expires_at - as_of).total_seconds()
    if not (0 < ttl_seconds <= PILOT_ADMISSION_MAX_TTL_SECONDS):
        raise PilotAdmissionContractError("snapshot TTL must be greater than 0 and at most 300 seconds")
    if not (as_of <= observed_now < expires_at):
        raise PilotAdmissionContractError("snapshot is not current at the evaluation instant")

    expected_hash = compute_pilot_admission_snapshot_sha256(body)
    if body["snapshot_sha256"] != expected_hash:
        raise PilotAdmissionContractError("snapshot_sha256 mismatch")

    return PilotAdmissionSnapshotV1(**body)


def require_pilot_admission(
    payload: Any,
    *,
    agent_snapshot: _AC.AgentContextSnapshot,
    expected_purpose: str,
    now: _dt.datetime,
) -> PilotAdmissionSnapshotV1:
    """Return a valid admitted snapshot or fail closed with ``denied``."""

    snapshot = validate_pilot_admission_snapshot(
        payload,
        agent_snapshot=agent_snapshot,
        expected_purpose=expected_purpose,
        now=now,
    )
    if not snapshot.admitted:
        raise PilotAdmissionDenied()
    return snapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class NullPilotAdmissionProvider:
    """Default provider: constructor-bound and permanently unavailable."""

    purpose: str

    API_VERSION: ClassVar[str] = PILOT_ADMISSION_PROVIDER_API_VERSION

    def __post_init__(self) -> None:
        _require_purpose(self.purpose)

    def snapshot(
        self, *, agent_snapshot: _AC.AgentContextSnapshot
    ) -> PilotAdmissionSnapshotV1:
        if not isinstance(agent_snapshot, _AC.AgentContextSnapshot):
            raise PilotAdmissionContractError("agent_snapshot must be AgentContextSnapshot")
        raise PilotAdmissionUnavailable()


__all__ = [
    "NullPilotAdmissionProvider",
    "PILOT_ADMISSION_FIXED_VECTOR_JCS",
    "PILOT_ADMISSION_FIXED_VECTOR_SHA256",
    "PILOT_ADMISSION_MAX_TTL_SECONDS",
    "PILOT_ADMISSION_PROVIDER_API_VERSION",
    "PILOT_ADMISSION_PURPOSES",
    "PILOT_ADMISSION_SNAPSHOT_FIELDS",
    "PILOT_ADMISSION_SNAPSHOT_HASH_DOMAIN",
    "PILOT_ADMISSION_SNAPSHOT_SCHEMA",
    "PilotAdmissionContractError",
    "PilotAdmissionDenied",
    "PilotAdmissionError",
    "PilotAdmissionProviderV1",
    "PilotAdmissionSnapshotV1",
    "PilotAdmissionUnavailable",
    "compute_pilot_admission_snapshot_sha256",
    "require_pilot_admission",
    "validate_pilot_admission_snapshot",
]
