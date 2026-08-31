#!/usr/bin/env python3
"""RT-015: Access Ledger + revocation core.

Owned by RT-015.  Every host-side runtime component that needs to know "may
this tenant see this report?" MUST go through :class:`AccessLedger` — which
is the sole SoR for grant status, lease freshness, revocation intent /
tombstone and downstream cleanup outbox.

Frozen invariants (see PRD FR-08, DESIGN §5 C-08, §10, references/安全威胁模型):

- Grant lookup keys are always the opaque ``grant_key = H(tenant_id, report_key)``
  (32 bytes SHA-256 → 128 bit → base32).  ``report_id`` never appears in a
  file-system path.  This prevents ``report_id`` enumeration from leaking
  the existence of a grant.
- Grant state machine follows RT-011 frozen v1 exactly:
  ``discovered → granted → active → revalidation_due → active | revoked``
  and ``revoked → purge_pending → purged``.  Only ``active`` (with an
  unexpired lease) is queryable; every other state — including missing
  records — fails closed.
- ``observe()`` from a bounded per-tenant collector can NEVER promote a
  grant past ``granted``; only :meth:`promote_to_active` /
  :meth:`refresh_lease` may raise the status, and both require a valid
  ``cwk.rt015.authority_receipt.v1`` from an authority adapter.  RT-015
  ships a fail-closed default adapter (real integration is out-of-scope);
  tests inject a fake HMAC-signer via ``_register_test_authority``.
- Revocation is crash-safe and cannot be resurrected:
    1. Append ``revoke-intent`` journal (from that instant, every
       eligibility check fails closed).
    2. CAS-mark grant ``revoked`` + append event.
    3. CAS-bump tenant ``auth_epoch`` via
       :class:`cwk_tenant_registry.TenantRegistry.bump_auth_epoch`.
    4. Write immutable tombstone (never queryable, only auditable).
    5. Write idempotent cleanup-outbox record enumerating downstream
       consumers (``tenant_view``, ``space_index``, ``cache``).
    6. Write revocation receipt.
    7. Unlink journal.
  Any crash re-runs :meth:`recover`, which only ever moves the transaction
  forward toward complete revocation — never back to ``active``.
- Every state-transition event carries ``actor``, ``reason``,
  ``tenant_auth_epoch`` before/after, ``record_revision`` before/after and
  opaque ``evidence_refs``.  ``actor`` / ``reason`` are restricted to
  printable ASCII (no NUL/CR/LF/ESC/other control chars) to defeat log
  injection.
- Query APIs never accept a bare ``tenant_id`` / ``agent_id``; they use an
  :class:`cwk_agent_context.AgentContextSnapshot` and re-check both the
  tenant record's current ``auth_epoch`` and the snapshot's before
  returning eligibility.  Mutation APIs may accept a validated
  ``tenant_id`` because they are only invoked by host-side runtime.
- Nothing in this module touches ``.env`` / ``CWORK_APP_KEY`` / real
  Work-collab / DocDB / cron / Cloud.  Only stdlib + frozen RT-011~014
  modules are imported.
"""

from __future__ import annotations

import base64
import datetime as _dt
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat as _stat_mod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import cwk_agent_context as _AC
import cwk_atomic_file as _A
import cwk_instance as _I
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _SE
import cwk_tenant_registry as _R


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

SCHEMA_DIR = (
    _C.SCHEMA_ROOT / "rt015" / "schemas"
)

# Grant-key derivation.  ``H(domain-separator, tenant_id, report_key)``
# truncated to 128 bit and base32-lowercased.  128 bits gives us collision
# resistance well beyond any realistic (tenant, report) population.
GRANT_KEY_DOMAIN = b"cwk-access-ledger-grant-key-v1"

# Frozen ID prefixes; must match the corresponding schemas.
GRANT_KEY_PREFIX = "g_"
REVOKE_TXN_PREFIX = "rv_"
EVENT_PREFIX = "ev_"
OUTBOX_PREFIX = "co_"
AUTHORITY_RECEIPT_PREFIX = "ar_"

_BASE32_TAIL = "aeimquy4"  # RT-011 valid tail characters for 128-bit base32.
_GRANT_KEY_REGEX_STR = r"^g_[a-z2-7]{26}$"
_REVOKE_TXN_REGEX_STR = r"^rv_[a-z2-7]{26}$"
_EVENT_REGEX_STR = r"^ev_[a-z2-7]{26}$"
_OUTBOX_REGEX_STR = r"^co_[a-z2-7]{26}$"
_RECEIPT_REGEX_STR = r"^ar_[a-z2-7]{26}$"

# Frozen leaf structure inside every ``<tenant_id>/`` under access-ledger.
_TENANT_SUBDIRS: tuple[str, ...] = (
    "grants",
    "events",
    "revoke-intents",
    "revoke-receipts",
    "tombstones",
    "cleanup-outbox",
    "locks",
)

# Grants have file leaves ``<grant_key>.json``; events ``<grant_key>.jsonl``;
# journals ``<txn>.journal`` inside revoke-intents/; receipts ``<txn>.receipt``
# inside revoke-receipts/; outbox ``<outbox_id>.json``; tombstones
# ``<grant_key>.json``.
_GRANT_LEAF_SUFFIX = ".json"
_EVENT_LEAF_SUFFIX = ".jsonl"
_JOURNAL_SUFFIX = ".journal"
_RECEIPT_SUFFIX = ".receipt"

# Grant lease default (15 minutes — DESIGN §5 C-08 upper bound; the caller
# can override on promote/refresh, subject to the same cap).
DEFAULT_LEASE_TTL_SECONDS = 15 * 60
LEASE_TTL_MIN_SECONDS = 30
LEASE_TTL_MAX_SECONDS = 15 * 60

# Actor / reason.
_ACTOR_MAX_LEN = 128
_REASON_MAX_LEN = 256

# Frozen schema IDs.
_GRANT_RECORD_SCHEMA_ID = "cwk.pr001.rt015.access_grant_record.v1"
_STATE_EVENT_SCHEMA_ID = "cwk.pr001.rt015.state_transition_event.v1"
_REVOKE_INTENT_SCHEMA_ID = "cwk.pr001.rt015.revoke_intent.v1"
_REVOKE_RECEIPT_SCHEMA_ID = "cwk.pr001.rt015.revoke_receipt.v1"
_TOMBSTONE_SCHEMA_ID = "cwk.pr001.rt015.access_tombstone.v1"
_CLEANUP_OUTBOX_SCHEMA_ID = "cwk.pr001.rt015.cleanup_outbox.v1"
_AUTHORITY_RECEIPT_SCHEMA_ID = "cwk.pr001.rt015.authority_receipt.v1"
_TENANT_VIEW_RECORD_SCHEMA_ID = "cwk.pr001.rt015.tenant_view_record.v1"

_UTC = _dt.timezone.utc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AccessLedgerError(Exception):
    """Base error.  ``code`` is a stable taxonomy string; ``__str__`` never
    contains absolute host paths or raw bodies."""

    def __init__(self, message: str, *, code: str, grant_key: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.grant_key = grant_key

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.grant_key:
            return f"[{self.code}] {base} (grant_key={self.grant_key})"
        return f"[{self.code}] {base}"


class NotInitialized(AccessLedgerError):
    def __init__(self, message: str = "access-ledger not initialised") -> None:
        super().__init__(message, code="not_initialized")


class GrantNotFound(AccessLedgerError):
    def __init__(self, grant_key: str | None = None) -> None:
        super().__init__("grant not found", code="not_found", grant_key=grant_key)


class GrantStateError(AccessLedgerError):
    def __init__(self, message: str, grant_key: str | None = None) -> None:
        super().__init__(message, code="state", grant_key=grant_key)


class GrantConflict(AccessLedgerError):
    def __init__(self, message: str, grant_key: str | None = None) -> None:
        super().__init__(message, code="conflict", grant_key=grant_key)


class AuthorityRejected(AccessLedgerError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="authority_rejected")


class RevocationInProgress(AccessLedgerError):
    def __init__(self, grant_key: str | None = None) -> None:
        super().__init__(
            "revocation in progress for this grant",
            code="revocation_in_progress",
            grant_key=grant_key,
        )


class LogInjectionDetected(AccessLedgerError):
    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"log injection detected in {field_name!r}",
            code="log_injection",
        )


class AccessDenied(AccessLedgerError):
    """Unified fail-closed error for query eligibility.

    The message is deliberately opaque — callers get a stable ``reason``
    tag but no information about which of grant/lease/epoch/tombstone
    check failed, so that adversarial callers cannot triangulate which
    reports exist in the ledger.
    """

    def __init__(self, reason: str = "not_eligible") -> None:
        super().__init__("access denied", code="denied")
        self.reason = reason


class GrantCorruption(AccessLedgerError):
    def __init__(self, message: str, grant_key: str | None = None) -> None:
        super().__init__(message, code="corrupt", grant_key=grant_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value: str) -> _dt.datetime:
    """Parse an RFC 3339 / ISO-8601 timestamp; treat both ``Z`` and ``+00:00``.

    Refuses microseconds beyond 6 digits and any other timezone.
    """

    if not isinstance(value, str):
        raise AccessLedgerError("timestamp must be a string", code="contract")
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise AccessLedgerError(f"invalid ISO 8601 timestamp: {exc}", code="contract") from exc
    if dt.tzinfo is None:
        raise AccessLedgerError("timestamp must include a UTC offset", code="contract")
    return dt


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(payload: Any) -> bytes:
    return _C.canonical_json_bytes(_C.nfc_normalize(payload))


def _canonical_sha256(payload: Any) -> str:
    return _sha256_bytes(_canonical_bytes(payload))


def _new_ev_id() -> str:
    return _new_opaque_id(EVENT_PREFIX)


def _new_txn_id() -> str:
    return _new_opaque_id(REVOKE_TXN_PREFIX)


def _new_outbox_id() -> str:
    return _new_opaque_id(OUTBOX_PREFIX)


def _new_opaque_id(prefix: str) -> str:
    raw = secrets.token_bytes(16)  # 128 bit
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    # base32 always yields exactly 26 chars for 16 bytes.
    if len(encoded) != 26:  # pragma: no cover - defensive
        raise AccessLedgerError(
            "opaque id length mismatch (expected 26 chars)", code="internal"
        )
    return prefix + encoded


def _validate_actor_reason(actor: str, reason: str) -> None:
    for name, value, max_len in (
        ("actor", actor, _ACTOR_MAX_LEN),
        ("reason", reason, _REASON_MAX_LEN),
    ):
        if not isinstance(value, str):
            raise LogInjectionDetected(name)
        if not value or len(value) > max_len:
            raise LogInjectionDetected(name)
        for ch in value:
            # Printable ASCII only.  This defeats log injection and any
            # attempt to embed CR/LF, ANSI escape sequences, NUL, etc.
            code = ord(ch)
            if code < 0x20 or code == 0x7f or code > 0x7e:
                raise LogInjectionDetected(name)


def compute_grant_key(tenant_id: str, report_key: str) -> str:
    """Return ``g_<base32(16)>`` = SHA-256(domain \0 tenant \0 report_key)[:16]."""

    validated = _I.validate_tenant_id(tenant_id)
    if not isinstance(report_key, str) or not _C.REPORT_KEY_REGEX.match(report_key):
        raise AccessLedgerError(
            "report_key must match REPORT_KEY_REGEX", code="contract"
        )
    material = (
        GRANT_KEY_DOMAIN
        + b"\x00"
        + validated.encode("utf-8")
        + b"\x00"
        + report_key.encode("utf-8")
    )
    digest = hashlib.sha256(material).digest()[:16]
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    # 128 bit → 26 base32 chars.  Belt: enforce tail constraint like RT-011.
    if len(encoded) != 26 or encoded[-1] not in _BASE32_TAIL:
        raise AccessLedgerError(
            "grant_key encoding failed byte-contract",
            code="internal",
        )
    return GRANT_KEY_PREFIX + encoded


# ---------------------------------------------------------------------------
# Schema validators (thin wrappers around RT-011 engine)
# ---------------------------------------------------------------------------


_SCHEMA_CACHE: dict[str, Any] = {}


def _load_schema(schema_id: str) -> Any:
    if schema_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[schema_id]
    filename_by_id = {
        _GRANT_RECORD_SCHEMA_ID: "access_grant_record.schema.json",
        _STATE_EVENT_SCHEMA_ID: "state_transition_event.schema.json",
        _REVOKE_INTENT_SCHEMA_ID: "revoke_intent.schema.json",
        _REVOKE_RECEIPT_SCHEMA_ID: "revoke_receipt.schema.json",
        _TOMBSTONE_SCHEMA_ID: "access_tombstone.schema.json",
        _CLEANUP_OUTBOX_SCHEMA_ID: "cleanup_outbox.schema.json",
        _AUTHORITY_RECEIPT_SCHEMA_ID: "authority_receipt.schema.json",
        _TENANT_VIEW_RECORD_SCHEMA_ID: "tenant_view_record.schema.json",
    }
    filename = filename_by_id.get(schema_id)
    if filename is None:  # pragma: no cover - defensive
        raise AccessLedgerError(f"unknown schema id {schema_id!r}", code="contract")
    payload = _C.strict_json_load_path(SCHEMA_DIR / filename)
    _SCHEMA_CACHE[schema_id] = payload
    return payload


def _validate_against(schema_id: str, payload: Any) -> None:
    schema = _load_schema(schema_id)
    try:
        _C._validate_schema(schema, payload, "$", root_schema=schema)
    except _C.ContractError as exc:
        raise AccessLedgerError(
            f"schema {schema_id} failed: {exc}", code="contract"
        ) from exc
    forbidden = schema.get("customKeywords", {}).get("deepForbiddenProperties")
    if forbidden:
        try:
            _C._iter_deep_forbidden(payload, frozenset(forbidden), path="$")
        except _C.ContractError as exc:
            raise AccessLedgerError(
                f"forbidden field present: {exc}", code="contract"
            ) from exc


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantRecord:
    payload: dict[str, Any]
    on_disk_sha256: str = ""

    @property
    def grant_key(self) -> str:
        return self.payload["grant_key"]

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def source_namespace(self) -> str:
        return self.payload["source_namespace"]

    @property
    def report_id(self) -> str:
        return self.payload["report_id"]

    @property
    def report_key(self) -> str:
        return _C.compose_report_key(self.source_namespace, self.report_id)

    @property
    def grant(self) -> dict[str, Any]:
        return self.payload["grant"]

    @property
    def status(self) -> str:
        return self.grant["status"]

    @property
    def auth_epoch(self) -> int:
        return int(self.grant["auth_epoch"])

    @property
    def lease_expires_at(self) -> Optional[str]:
        return self.grant.get("lease_expires_at")

    @property
    def record_revision(self) -> int:
        return int(self.payload["record_revision"])


@dataclass(frozen=True)
class RevokeReceipt:
    payload: dict[str, Any]

    @property
    def txn_id(self) -> str:
        return self.payload["txn_id"]

    @property
    def grant_key(self) -> str:
        return self.payload["grant_key"]

    @property
    def tenant_auth_epoch_after(self) -> int:
        return int(self.payload["tenant_auth_epoch_after"])


@dataclass(frozen=True)
class Tombstone:
    payload: dict[str, Any]

    @property
    def grant_key(self) -> str:
        return self.payload["grant_key"]


@dataclass(frozen=True)
class CleanupTask:
    payload: dict[str, Any]

    @property
    def outbox_id(self) -> str:
        return self.payload["outbox_id"]

    @property
    def grant_key(self) -> str:
        return self.payload["grant_key"]

    @property
    def consumers(self) -> list[str]:
        return list(self.payload["consumers"])


@dataclass(frozen=True)
class RecoveryReport:
    intents_completed: int = 0
    intents_already_committed: int = 0
    orphans_removed: int = 0
    inconsistencies: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Authority adapter (default = fail-closed; tests can inject a fake signer)
# ---------------------------------------------------------------------------


class AuthorityAdapter:
    """Interface for authoritative permission verification.

    Real integrations (Work-collab permission API, delegated admin
    bootstrap) are out-of-scope for RT-015; RT-015 ships only the
    fail-closed default.  Tests register a fake HMAC-signer via
    :func:`_register_test_authority` with the module-private sentinel.
    """

    def verify(self, receipt: dict[str, Any], *, purpose: str) -> None:  # pragma: no cover - abstract
        raise AuthorityRejected("real authority not integrated (conservative_unknown default)")


class _FailClosedAuthority(AuthorityAdapter):
    def verify(self, receipt: dict[str, Any], *, purpose: str) -> None:
        raise AuthorityRejected(
            "real authority not integrated; RT-015 default fails closed"
        )


_DEFAULT_AUTHORITY = _FailClosedAuthority()
_TEST_AUTHORITY_TOKEN = object()
_TEST_AUTHORITY: Optional[AuthorityAdapter] = None
_TEST_AUTHORITY_SIGNERS: dict[str, bytes] = {}


def _register_test_authority(adapter: AuthorityAdapter, *, token: object) -> None:
    """Test-only.  Callers must import the private ``_TEST_AUTHORITY_TOKEN``.

    Production code cannot swap the authority because it lacks the sentinel
    reference; the extra safety-belt is enforced by :meth:`verify` still
    running through the registered adapter.
    """

    global _TEST_AUTHORITY
    if token is not _TEST_AUTHORITY_TOKEN:
        raise AccessLedgerError(
            "unauthorised authority registration attempt", code="internal"
        )
    if not isinstance(adapter, AuthorityAdapter):
        raise AccessLedgerError("adapter must be AuthorityAdapter", code="contract")
    _TEST_AUTHORITY = adapter


def _unregister_test_authority(*, token: object) -> None:
    global _TEST_AUTHORITY
    if token is not _TEST_AUTHORITY_TOKEN:
        raise AccessLedgerError(
            "unauthorised authority deregistration", code="internal"
        )
    _TEST_AUTHORITY = None


def _register_fake_signer(signer_id: str, secret: bytes, *, token: object) -> None:
    if token is not _TEST_AUTHORITY_TOKEN:
        raise AccessLedgerError("unauthorised signer registration", code="internal")
    if not isinstance(signer_id, str) or not signer_id or len(signer_id) > 64:
        raise AccessLedgerError("signer_id length invalid", code="contract")
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
        raise AccessLedgerError("signer secret must be >=32 bytes", code="contract")
    _TEST_AUTHORITY_SIGNERS[signer_id] = bytes(secret)


def _unregister_fake_signer(signer_id: str, *, token: object) -> None:
    if token is not _TEST_AUTHORITY_TOKEN:
        raise AccessLedgerError("unauthorised signer deregistration", code="internal")
    _TEST_AUTHORITY_SIGNERS.pop(signer_id, None)


class FakeSigningAuthority(AuthorityAdapter):
    """Test-only.  Verifies HMAC signatures over the canonical bytes.

    Fixed signature envelope:

        signed = canonical_json_bytes({receipt without ``signature`` field})
        expected = hex(hmac_sha256(secret, signed))

    Failure modes (all raise :class:`AuthorityRejected`):
      - unknown signer_id
      - missing signature
      - wrong receipt schema
      - constant-time-inequal signature
      - non-matching (tenant, source_namespace, report_id, grant_key)
      - purpose mismatch (grant_promote vs lease_refresh)
      - lease_expires_at in the past
    """

    def verify(self, receipt: dict[str, Any], *, purpose: str) -> None:
        _validate_against(_AUTHORITY_RECEIPT_SCHEMA_ID, receipt)
        signer_id = receipt["signer_id"]
        secret = _TEST_AUTHORITY_SIGNERS.get(signer_id)
        if secret is None:
            raise AuthorityRejected(f"unknown signer_id {signer_id!r}")
        signature = receipt["signature"]
        payload_wo_sig = {k: v for k, v in receipt.items() if k != "signature"}
        expected = hmac.new(secret, _canonical_bytes(payload_wo_sig), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AuthorityRejected("authority receipt signature mismatch")
        # Purpose gating.
        purpose_by_type = {
            "promote_to_active": "grant_promote",
            "refresh_lease": "lease_refresh",
        }
        expected_type = purpose_by_type.get(purpose)
        if expected_type is None:
            raise AuthorityRejected(f"unknown authority purpose {purpose!r}")
        if receipt["receipt_type"] != expected_type:
            raise AuthorityRejected(
                f"receipt_type {receipt['receipt_type']!r} != required {expected_type!r}"
            )
        # Lease expiry check.
        lease_dt = _parse_iso(receipt["lease_expires_at"])
        now = _dt.datetime.now(tz=_UTC)
        if lease_dt <= now:
            raise AuthorityRejected("authority receipt lease already expired")


def _authority() -> AuthorityAdapter:
    return _TEST_AUTHORITY or _DEFAULT_AUTHORITY


# ---------------------------------------------------------------------------
# Directory helpers (dir-FD anchored)
# ---------------------------------------------------------------------------


def _open_child_dir_nofollow(parent_fd: int, name: str) -> int:
    """Open a subdirectory beneath ``parent_fd`` with O_DIRECTORY|O_NOFOLLOW.

    Mirrors the local helper in cwk_shared_evidence; keeps RT-015 from
    depending on RT-012's fixed-leaf allow-list for arbitrary tenant IDs.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AccessLedgerError(
                "child is a symlink; refusing to follow", code="contract"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise AccessLedgerError(
                "child is not a directory", code="contract"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise AccessLedgerError("child does not exist", code="not_found") from exc
        raise AccessLedgerError(
            f"cannot open child (errno={exc.errno})", code="io"
        ) from exc
    st = os.fstat(fd)
    if not _stat_mod.S_ISDIR(st.st_mode):
        os.close(fd)
        raise AccessLedgerError("child is not a directory", code="contract")
    return fd


# ---------------------------------------------------------------------------
# The AccessLedger
# ---------------------------------------------------------------------------


class AccessLedger:
    """The sole source of truth for tenant→report access grants.

    Instances are stateless and cheap; every method takes its own dir-FDs
    and closes them before returning.  Concurrency safety: mutation
    operations acquire per-grant advisory ``flock`` locks; tenant
    ``auth_epoch`` mutation is delegated to :class:`TenantRegistry` which
    performs its own CAS.
    """

    __slots__ = ("_layout", "_tenants", "_evidence")

    def __init__(
        self,
        layout: _I.InstanceLayout,
        tenant_registry: _R.TenantRegistry,
        shared_store: _SE.SharedEvidenceStore,
    ) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise AccessLedgerError("layout must be InstanceLayout", code="contract")
        if not isinstance(tenant_registry, _R.TenantRegistry):
            raise AccessLedgerError(
                "tenant_registry must be TenantRegistry", code="contract"
            )
        if not isinstance(shared_store, _SE.SharedEvidenceStore):
            raise AccessLedgerError(
                "shared_store must be SharedEvidenceStore", code="contract"
            )
        self._layout = layout
        self._tenants = tenant_registry
        self._evidence = shared_store

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Ensure ``registry/access-ledger/`` is prepared.

        The root ``registry/access-ledger/`` directory itself is
        pre-created by RT-012's :attr:`InstanceLayout.initialize`; this
        method exists to fsync it and (later) create per-tenant subdirs on
        demand.  Idempotent.
        """

        with self._layout.registry_fd("access-ledger") as afd:
            _A.fsync_dir(afd)

    # ------------------------------------------------------------------
    # Per-tenant subdir open (creates on demand)
    # ------------------------------------------------------------------

    @contextmanager
    def _tenant_fd(self, tenant_id: str, *, create: bool = False) -> Iterator[int]:
        validated = _I.validate_tenant_id(tenant_id)
        with self._layout.registry_fd("access-ledger") as afd:
            if not _A.child_exists(afd, validated):
                if not create:
                    raise GrantNotFound()
                _A.mkdir_at(afd, validated, mode=_A.DIRECTORY_MODE, exist_ok=True)
                _A.fsync_dir(afd)
            fd = _open_child_dir_nofollow(afd, validated)
            try:
                if create:
                    for sub in _TENANT_SUBDIRS:
                        _A.mkdir_at(fd, sub, mode=_A.DIRECTORY_MODE, exist_ok=True)
                    _A.fsync_dir(fd)
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _sub_fd(self, tenant_fd: int, name: str) -> Iterator[int]:
        if name not in _TENANT_SUBDIRS:
            raise AccessLedgerError(
                f"unknown ledger subdir {name!r}", code="contract"
            )
        if not _A.child_exists(tenant_fd, name):
            raise NotInitialized(f"{name} missing")
        fd = _open_child_dir_nofollow(tenant_fd, name)
        try:
            yield fd
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Grant lookup / disk IO
    # ------------------------------------------------------------------

    def _grant_leaf(self, grant_key: str) -> str:
        return f"{grant_key}{_GRANT_LEAF_SUFFIX}"

    def _events_leaf(self, grant_key: str) -> str:
        return f"{grant_key}{_EVENT_LEAF_SUFFIX}"

    def _tombstone_leaf(self, grant_key: str) -> str:
        return f"{grant_key}{_GRANT_LEAF_SUFFIX}"

    def _lock_leaf(self, grant_key: str) -> str:
        return f"grant.{grant_key}.lock"

    def _read_grant_file(self, tenant_fd: int, grant_key: str) -> GrantRecord:
        with self._sub_fd(tenant_fd, "grants") as gfd:
            try:
                raw = _A.read_file(gfd, self._grant_leaf(grant_key))
            except FileNotFoundError as exc:
                raise GrantNotFound(grant_key=grant_key) from exc
            except _A.ContainmentError as exc:
                raise GrantCorruption(
                    "grant file failed containment", grant_key=grant_key
                ) from exc
        try:
            payload = _C.strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
            raise GrantCorruption(
                "grant file is not strict JSON", grant_key=grant_key
            ) from exc
        # Envelope schema.
        _validate_against(_GRANT_RECORD_SCHEMA_ID, payload)
        # Frozen v1 payload schema.
        try:
            _C.validate_access_grant(payload["grant"])
        except _C.ContractError as exc:
            raise GrantCorruption(
                f"grant.v1 failed: {exc}", grant_key=grant_key
            ) from exc
        # Cross-check envelope fields against the frozen payload.
        for field_name in ("tenant_id", "source_namespace", "report_id"):
            if payload[field_name] != payload["grant"][field_name]:
                raise GrantCorruption(
                    f"envelope/{field_name} mismatch",
                    grant_key=grant_key,
                )
        if payload["grant_key"] != grant_key:
            raise GrantCorruption(
                "envelope grant_key does not match filename",
                grant_key=grant_key,
            )
        # Recompute grant_key from tenant_id + report_key and verify.
        report_key = _C.compose_report_key(
            payload["source_namespace"], payload["report_id"]
        )
        expected_gk = compute_grant_key(payload["tenant_id"], report_key)
        if expected_gk != grant_key:
            raise GrantCorruption(
                "grant_key does not match H(tenant, report_key)",
                grant_key=grant_key,
            )
        # Canonical byte round-trip: the on-disk bytes MUST be canonical.
        if _canonical_bytes(payload) != raw:
            raise GrantCorruption(
                "grant bytes are not canonical JCS", grant_key=grant_key
            )
        return GrantRecord(payload=payload, on_disk_sha256=_sha256_bytes(raw))

    def _write_grant_file(
        self,
        tenant_fd: int,
        grant_key: str,
        new_payload: dict[str, Any],
        expected_sha256: Optional[str],
    ) -> GrantRecord:
        _validate_against(_GRANT_RECORD_SCHEMA_ID, new_payload)
        # Also validate the frozen nested grant.v1 payload.
        _C.validate_access_grant(new_payload["grant"])
        bytes_ = _canonical_bytes(new_payload)
        with self._sub_fd(tenant_fd, "grants") as gfd:
            _A.cas_write(
                gfd,
                self._grant_leaf(grant_key),
                bytes_,
                expected_previous_sha256=expected_sha256,
            )
        return GrantRecord(payload=new_payload, on_disk_sha256=_sha256_bytes(bytes_))

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def _append_event(
        self,
        tenant_fd: int,
        grant_key: str,
        event: dict[str, Any],
    ) -> None:
        _validate_against(_STATE_EVENT_SCHEMA_ID, event)
        line = _canonical_bytes(event) + b"\n"
        with self._sub_fd(tenant_fd, "events") as efd:
            leaf = self._events_leaf(grant_key)
            if _A.child_exists(efd, leaf):
                current = _A.read_file(efd, leaf)
                current_sha = _sha256_bytes(current)
                new_bytes = current + line
                _A.cas_write(
                    efd, leaf, new_bytes, expected_previous_sha256=current_sha
                )
            else:
                _A.cas_write(efd, leaf, line, expected_previous_sha256=None)

    def iter_events(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
    ) -> list[dict[str, Any]]:
        """Return the parsed event log for one grant (chronological).

        Not a query API — used by RT-015 tests and by RT-024 audit-metrics
        consumers.  Never accepts a ``snapshot`` because event history is
        derived state, not eligibility.
        """

        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "events") as efd:
                leaf = self._events_leaf(grant_key)
                if not _A.child_exists(efd, leaf):
                    return []
                raw = _A.read_file(efd, leaf)
        return self._parse_event_log(raw, grant_key)

    def _parse_event_log(self, raw: bytes, grant_key: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not raw:
            return events
        text = raw.decode("utf-8")
        for lineno, line in enumerate(text.split("\n"), 1):
            if not line:
                continue
            try:
                entry = _C.strict_json_loads(line)
            except (_C.ContractError, ValueError) as exc:
                raise GrantCorruption(
                    f"event log line {lineno} corrupt: {exc}", grant_key=grant_key
                ) from exc
            _validate_against(_STATE_EVENT_SCHEMA_ID, entry)
            if entry["grant_key"] != grant_key:
                raise GrantCorruption(
                    f"event log line {lineno} grant_key mismatch", grant_key=grant_key
                )
            events.append(entry)
        return events

    def _make_event(
        self,
        *,
        grant_key: str,
        tenant_id: str,
        from_status: str,
        to_status: str,
        tenant_auth_epoch_before: int,
        tenant_auth_epoch_after: int,
        record_revision_before: int,
        record_revision_after: int,
        actor: str,
        reason: str,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        return {
            "schema": "cwk.rt015.state_transition_event.v1",
            "event_id": _new_ev_id(),
            "grant_key": grant_key,
            "tenant_id": tenant_id,
            "from_status": from_status,
            "to_status": to_status,
            "tenant_auth_epoch_before": tenant_auth_epoch_before,
            "tenant_auth_epoch_after": tenant_auth_epoch_after,
            "record_revision_before": record_revision_before,
            "record_revision_after": record_revision_after,
            "actor": actor,
            "reason": reason,
            "evidence_refs": evidence_refs,
            "happened_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------
    # Grant helpers
    # ------------------------------------------------------------------

    def _build_grant_payload(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        status: str,
        roles: list[str],
        visibility_scope: str,
        permission_source: str,
        auth_epoch: int,
        granted_at: Optional[str],
        last_verified_at: Optional[str],
        lease_expires_at: Optional[str],
        revoked_at: Optional[str],
    ) -> dict[str, Any]:
        return {
            "schema": "cwk.access_grant.v1",
            "tenant_id": tenant_id,
            "source_namespace": source_namespace,
            "report_id": report_id,
            "status": status,
            "roles": sorted(set(roles)),
            "visibility_scope": visibility_scope,
            "permission_source": permission_source,
            "auth_epoch": auth_epoch,
            "granted_at": granted_at,
            "last_verified_at": last_verified_at,
            "lease_expires_at": lease_expires_at,
            "revoked_at": revoked_at,
        }

    def _wrap_grant(
        self,
        *,
        grant_key: str,
        grant_payload: dict[str, Any],
        record_revision: int,
        created_at: str,
        updated_at: str,
    ) -> dict[str, Any]:
        return {
            "schema": "cwk.rt015.access_grant_record.v1",
            "grant_key": grant_key,
            "tenant_id": grant_payload["tenant_id"],
            "source_namespace": grant_payload["source_namespace"],
            "report_id": grant_payload["report_id"],
            "grant": grant_payload,
            "record_revision": record_revision,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _lease_expiry(self, ttl_seconds: int) -> str:
        if not isinstance(ttl_seconds, int) or ttl_seconds < LEASE_TTL_MIN_SECONDS or ttl_seconds > LEASE_TTL_MAX_SECONDS:
            raise AccessLedgerError(
                f"lease_ttl_seconds out of range [{LEASE_TTL_MIN_SECONDS}, {LEASE_TTL_MAX_SECONDS}]",
                code="contract",
            )
        now = _dt.datetime.now(tz=_UTC).replace(microsecond=0)
        expiry = now + _dt.timedelta(seconds=ttl_seconds)
        return expiry.isoformat().replace("+00:00", "Z")

    def _load_tenant(self, tenant_id: str) -> _R.TenantRecord:
        try:
            return self._tenants.get(tenant_id)
        except _R.TenantNotFound as exc:
            raise AccessLedgerError("unknown tenant", code="unknown_tenant") from exc

    def _assert_tenant_active_or_pilot_for_mutation(
        self, tenant_record: _R.TenantRecord
    ) -> None:
        """Only certain tenant statuses may accept mutations.

        For observation we accept ``profile_pending`` too (the collector
        runs bounded sample_collect there).  Revocation is always allowed
        (safety operation).  Explicit ``draft/offboarded/suspended`` are
        refused for grants that would extend privileges — but revocation
        is a privilege-reducing op, so it's not gated here.
        """

        # Left intentionally light — per-method gating below.
        _ = tenant_record

    # ------------------------------------------------------------------
    # Observation ingest — never promotes beyond `granted`
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        observation: dict[str, Any],
        actor: str,
        reason: str,
    ) -> GrantRecord:
        """Consume a :class:`cwk.access_observation.v1` from a per-tenant
        collector.

        Ingestion rules:

        - The observation is validated against the RT-011 frozen v1 schema.
        - The tenant must exist and be in a status that allows observation
          (``draft`` and ``offboarded`` are refused; ``profile_pending``
          onwards are allowed because bounded sample collection runs
          there).
        - Initial status may only be ``discovered`` or ``granted`` — the
          RT-011 schema already enforces this.  Even if the observation
          claims ``granted``, RT-015 records it as such but never as
          ``active``.  Promotion to ``active`` requires an authoritative
          receipt via :meth:`promote_to_active`.
        - Re-observing an existing grant is idempotent: the on-disk record
          is only rewritten if ``initial_status`` moves the grant to
          ``granted`` from ``discovered``, or a role/visibility change is
          detected.  Every rewrite appends an event.

        Returns the current :class:`GrantRecord` after the ingest.
        """

        _validate_actor_reason(actor, reason)
        _C.validate_access_observation(observation)
        tenant_id = observation["tenant_id"]
        tenant = self._load_tenant(tenant_id)
        if tenant.status in ("draft", "offboarded"):
            raise GrantStateError(
                f"tenant status {tenant.status!r} refuses access observations"
            )

        source_namespace = observation["source_namespace"]
        report_id = observation["report_id"]
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        initial_status = observation["initial_status"]  # discovered|granted
        roles = list(observation.get("roles", []))
        visibility_scope = observation.get("visibility_scope", "unknown")
        permission_source = observation["observation_source"]
        evidence_refs = observation.get("evidence_refs", [])

        with self._tenant_fd(tenant_id, create=True) as tfd:
            with self._sub_fd(tfd, "locks") as lfd:
                with _A.exclusive_lock(lfd, self._lock_leaf(grant_key)):
                    now = _utcnow_iso()
                    # Reject if a revocation intent is present.
                    if self._intent_exists_for(tfd, grant_key):
                        raise RevocationInProgress(grant_key=grant_key)
                    # Reject if a tombstone exists.
                    if self._tombstone_exists(tfd, grant_key):
                        raise GrantStateError(
                            "grant is tombstoned; cannot re-observe",
                            grant_key=grant_key,
                        )
                    try:
                        existing = self._read_grant_file(tfd, grant_key)
                    except GrantNotFound:
                        # First observation.
                        grant_payload = self._build_grant_payload(
                            tenant_id=tenant_id,
                            source_namespace=source_namespace,
                            report_id=report_id,
                            status=initial_status,
                            roles=roles,
                            visibility_scope=visibility_scope,
                            permission_source=permission_source,
                            auth_epoch=tenant.auth_epoch,
                            granted_at=now if initial_status == "granted" else None,
                            last_verified_at=now,
                            lease_expires_at=None,
                            revoked_at=None,
                        )
                        wrap = self._wrap_grant(
                            grant_key=grant_key,
                            grant_payload=grant_payload,
                            record_revision=1,
                            created_at=now,
                            updated_at=now,
                        )
                        rec = self._write_grant_file(tfd, grant_key, wrap, expected_sha256=None)
                        event = self._make_event(
                            grant_key=grant_key,
                            tenant_id=tenant_id,
                            from_status="_initial_",
                            to_status=initial_status,
                            tenant_auth_epoch_before=tenant.auth_epoch,
                            tenant_auth_epoch_after=tenant.auth_epoch,
                            record_revision_before=0,
                            record_revision_after=1,
                            actor=actor,
                            reason=reason,
                            evidence_refs=[f"observation_source:{permission_source}", *[
                                _clip_evidence_ref(ref)
                                for ref in evidence_refs
                                if _is_safe_evidence_ref(ref)
                            ]],
                        )
                        self._append_event(tfd, grant_key, event)
                        return rec
                    else:
                        # Existing grant.
                        cur_status = existing.status
                        transition_needed = False
                        # discovered → granted is the only legal
                        # observation-driven transition.
                        if cur_status == "discovered" and initial_status == "granted":
                            _C.validate_access_grant_transition(cur_status, "granted")
                            transition_needed = True
                            new_status = "granted"
                        elif cur_status in ("active", "revalidation_due"):
                            # Observation cannot demote; only refresh
                            # last_verified_at + roles/visibility as
                            # metadata (record still needs a bump if
                            # anything changed).
                            new_status = cur_status
                            transition_needed = (
                                sorted(existing.grant["roles"]) != sorted(set(roles))
                                or existing.grant["visibility_scope"] != visibility_scope
                            )
                        elif cur_status == "revoked" or cur_status in ("purge_pending", "purged"):
                            raise GrantStateError(
                                f"grant already in terminal status {cur_status!r}",
                                grant_key=grant_key,
                            )
                        else:  # cur_status == "granted"
                            new_status = "granted"
                            transition_needed = (
                                sorted(existing.grant["roles"]) != sorted(set(roles))
                                or existing.grant["visibility_scope"] != visibility_scope
                            )
                        if not transition_needed:
                            # Purely idempotent no-op re-observation; do
                            # not append event, do not rewrite file.
                            return existing
                        new_grant_payload = self._build_grant_payload(
                            tenant_id=tenant_id,
                            source_namespace=source_namespace,
                            report_id=report_id,
                            status=new_status,
                            roles=roles,
                            visibility_scope=visibility_scope,
                            permission_source=permission_source,
                            auth_epoch=tenant.auth_epoch,
                            granted_at=(
                                existing.grant.get("granted_at")
                                or (now if new_status == "granted" else None)
                            ),
                            last_verified_at=now,
                            lease_expires_at=existing.grant.get("lease_expires_at"),
                            revoked_at=None,
                        )
                        new_wrap = self._wrap_grant(
                            grant_key=grant_key,
                            grant_payload=new_grant_payload,
                            record_revision=existing.record_revision + 1,
                            created_at=existing.payload["created_at"],
                            updated_at=now,
                        )
                        rec = self._write_grant_file(
                            tfd, grant_key, new_wrap, expected_sha256=existing.on_disk_sha256
                        )
                        event = self._make_event(
                            grant_key=grant_key,
                            tenant_id=tenant_id,
                            from_status=cur_status,
                            to_status=new_status,
                            tenant_auth_epoch_before=tenant.auth_epoch,
                            tenant_auth_epoch_after=tenant.auth_epoch,
                            record_revision_before=existing.record_revision,
                            record_revision_after=existing.record_revision + 1,
                            actor=actor,
                            reason=reason,
                            evidence_refs=[
                                f"observation_source:{permission_source}",
                                *[
                                    _clip_evidence_ref(ref)
                                    for ref in evidence_refs
                                    if _is_safe_evidence_ref(ref)
                                ],
                            ],
                        )
                        self._append_event(tfd, grant_key, event)
                        return rec

    # ------------------------------------------------------------------
    # Promotion / lease refresh — require authority receipt
    # ------------------------------------------------------------------

    def promote_to_active(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        authority_receipt: dict[str, Any],
        actor: str,
        reason: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> GrantRecord:
        """Move a ``granted`` grant to ``active`` after authoritative verify."""

        _validate_actor_reason(actor, reason)
        return self._authoritative_promote(
            purpose="promote_to_active",
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            authority_receipt=authority_receipt,
            actor=actor,
            reason=reason,
            lease_ttl_seconds=lease_ttl_seconds,
            allowed_current=("granted", "revalidation_due"),
            to_status="active",
        )

    def refresh_lease(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        authority_receipt: dict[str, Any],
        actor: str,
        reason: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> GrantRecord:
        """Re-affirm an ``active`` or ``revalidation_due`` grant."""

        _validate_actor_reason(actor, reason)
        return self._authoritative_promote(
            purpose="refresh_lease",
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            authority_receipt=authority_receipt,
            actor=actor,
            reason=reason,
            lease_ttl_seconds=lease_ttl_seconds,
            allowed_current=("active", "revalidation_due"),
            to_status="active",
        )

    def _authoritative_promote(
        self,
        *,
        purpose: str,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        authority_receipt: dict[str, Any],
        actor: str,
        reason: str,
        lease_ttl_seconds: int,
        allowed_current: tuple[str, ...],
        to_status: str,
    ) -> GrantRecord:
        # Verify against authority BEFORE touching disk.
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        # Cross-check receipt.
        if not isinstance(authority_receipt, dict):
            raise AccessLedgerError("authority_receipt must be dict", code="contract")
        _validate_against(_AUTHORITY_RECEIPT_SCHEMA_ID, authority_receipt)
        if (
            authority_receipt["tenant_id"] != tenant_id
            or authority_receipt["source_namespace"] != source_namespace
            or authority_receipt["report_id"] != report_id
            or authority_receipt["grant_key"] != grant_key
        ):
            raise AuthorityRejected(
                "authority receipt binding does not match request"
            )
        _authority().verify(authority_receipt, purpose=purpose)
        tenant = self._load_tenant(tenant_id)
        if tenant.status in ("draft", "suspended", "offboarded"):
            raise GrantStateError(
                f"tenant status {tenant.status!r} refuses promotion"
            )

        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "locks") as lfd:
                with _A.exclusive_lock(lfd, self._lock_leaf(grant_key)):
                    if self._intent_exists_for(tfd, grant_key):
                        raise RevocationInProgress(grant_key=grant_key)
                    if self._tombstone_exists(tfd, grant_key):
                        raise GrantStateError(
                            "grant is tombstoned; cannot re-promote",
                            grant_key=grant_key,
                        )
                    existing = self._read_grant_file(tfd, grant_key)
                    if existing.status not in allowed_current:
                        raise GrantStateError(
                            f"cannot {purpose}: current status {existing.status!r} "
                            f"not in {list(allowed_current)}",
                            grant_key=grant_key,
                        )
                    # Enforce declared transition (no-op transition
                    # active→active for lease refresh is fine).
                    if existing.status != to_status:
                        _C.validate_access_grant_transition(
                            existing.status, to_status
                        )
                    now = _utcnow_iso()
                    lease_expires_at = self._lease_expiry(lease_ttl_seconds)
                    # Cap by authority receipt lease (never extend beyond
                    # authority's stated expiry).
                    receipt_expiry = _parse_iso(
                        authority_receipt["lease_expires_at"]
                    )
                    computed_expiry = _parse_iso(lease_expires_at)
                    if receipt_expiry < computed_expiry:
                        lease_expires_at = (
                            receipt_expiry.astimezone(_UTC)
                            .replace(microsecond=0)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                    new_grant = self._build_grant_payload(
                        tenant_id=tenant_id,
                        source_namespace=source_namespace,
                        report_id=report_id,
                        status=to_status,
                        roles=list(authority_receipt["roles"]),
                        visibility_scope=authority_receipt["visibility_scope"],
                        permission_source=authority_receipt["permission_source"],
                        auth_epoch=tenant.auth_epoch,
                        granted_at=existing.grant.get("granted_at") or now,
                        last_verified_at=now,
                        lease_expires_at=lease_expires_at,
                        revoked_at=None,
                    )
                    new_wrap = self._wrap_grant(
                        grant_key=grant_key,
                        grant_payload=new_grant,
                        record_revision=existing.record_revision + 1,
                        created_at=existing.payload["created_at"],
                        updated_at=now,
                    )
                    rec = self._write_grant_file(
                        tfd, grant_key, new_wrap, expected_sha256=existing.on_disk_sha256
                    )
                    event = self._make_event(
                        grant_key=grant_key,
                        tenant_id=tenant_id,
                        from_status=existing.status,
                        to_status=to_status,
                        tenant_auth_epoch_before=tenant.auth_epoch,
                        tenant_auth_epoch_after=tenant.auth_epoch,
                        record_revision_before=existing.record_revision,
                        record_revision_after=existing.record_revision + 1,
                        actor=actor,
                        reason=reason,
                        evidence_refs=[
                            f"authority_receipt_id:{authority_receipt['receipt_id']}",
                            f"signer_id:{authority_receipt['signer_id']}",
                            f"permission_source:{authority_receipt['permission_source']}",
                        ],
                    )
                    self._append_event(tfd, grant_key, event)
                    return rec

    def mark_revalidation_due(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        actor: str,
        reason: str,
    ) -> GrantRecord:
        """Downgrade an ``active`` grant to ``revalidation_due``.

        Callers use this when a lease has expired without a fresh
        authoritative receipt.  Subsequent eligibility checks fail closed
        until :meth:`refresh_lease` runs.  Never accepts an authority
        receipt (the whole point is that no fresh receipt is available).
        """

        _validate_actor_reason(actor, reason)
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        tenant = self._load_tenant(tenant_id)
        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "locks") as lfd:
                with _A.exclusive_lock(lfd, self._lock_leaf(grant_key)):
                    if self._intent_exists_for(tfd, grant_key):
                        raise RevocationInProgress(grant_key=grant_key)
                    if self._tombstone_exists(tfd, grant_key):
                        raise GrantStateError(
                            "grant is tombstoned; already revoked",
                            grant_key=grant_key,
                        )
                    existing = self._read_grant_file(tfd, grant_key)
                    if existing.status != "active":
                        raise GrantStateError(
                            f"cannot mark revalidation_due from {existing.status!r}",
                            grant_key=grant_key,
                        )
                    _C.validate_access_grant_transition("active", "revalidation_due")
                    now = _utcnow_iso()
                    new_grant = dict(existing.grant)
                    new_grant["status"] = "revalidation_due"
                    new_grant["last_verified_at"] = now
                    new_wrap = self._wrap_grant(
                        grant_key=grant_key,
                        grant_payload=new_grant,
                        record_revision=existing.record_revision + 1,
                        created_at=existing.payload["created_at"],
                        updated_at=now,
                    )
                    rec = self._write_grant_file(
                        tfd, grant_key, new_wrap, expected_sha256=existing.on_disk_sha256
                    )
                    event = self._make_event(
                        grant_key=grant_key,
                        tenant_id=tenant_id,
                        from_status="active",
                        to_status="revalidation_due",
                        tenant_auth_epoch_before=tenant.auth_epoch,
                        tenant_auth_epoch_after=tenant.auth_epoch,
                        record_revision_before=existing.record_revision,
                        record_revision_after=existing.record_revision + 1,
                        actor=actor,
                        reason=reason,
                        evidence_refs=["lease_expired"],
                    )
                    self._append_event(tfd, grant_key, event)
                    return rec

    # ------------------------------------------------------------------
    # Revocation — crash-safe
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        actor: str,
        reason: str,
        authority_receipt: Optional[dict[str, Any]] = None,
    ) -> RevokeReceipt:
        """Crash-safe revocation, following the seven-step recipe.

        Idempotent per grant: calling twice returns the receipt of the
        winning attempt; the second attempt raises ``GrantStateError`` or
        returns the existing receipt via :meth:`_find_revoke_receipt`.
        """

        _validate_actor_reason(actor, reason)
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        # Optional authority receipt verified but never required for
        # revocation — safety-reducing operations MUST always be possible.
        if authority_receipt is not None:
            _validate_against(_AUTHORITY_RECEIPT_SCHEMA_ID, authority_receipt)
            if authority_receipt.get("receipt_type") != "revoke_confirm":
                raise AuthorityRejected(
                    "authority_receipt must be revoke_confirm for revoke()"
                )
            if (
                authority_receipt["tenant_id"] != tenant_id
                or authority_receipt["grant_key"] != grant_key
            ):
                raise AuthorityRejected(
                    "authority_receipt binding mismatch"
                )
        with self._tenant_fd(tenant_id, create=True) as tfd:
            with self._sub_fd(tfd, "locks") as lfd:
                with _A.exclusive_lock(lfd, self._lock_leaf(grant_key)):
                    # Existing tombstone → already committed.
                    if self._tombstone_exists(tfd, grant_key):
                        existing_receipt = self._find_completed_receipt(tfd, grant_key)
                        if existing_receipt is not None:
                            return existing_receipt
                        # Tombstone exists but no receipt — this can only
                        # happen if recovery is still in progress; fail
                        # closed and let recovery finish first.
                        raise RevocationInProgress(grant_key=grant_key)
                    # Existing intent journal → another writer / crash.
                    if self._intent_exists_for(tfd, grant_key):
                        # Complete via recovery path; the caller retries.
                        raise RevocationInProgress(grant_key=grant_key)
                    grant = self._read_grant_file(tfd, grant_key)
                    if grant.status in ("revoked", "purge_pending", "purged"):
                        raise GrantStateError(
                            f"grant already {grant.status!r}",
                            grant_key=grant_key,
                        )
                    # Legal target?  RT-011 allows revoked from
                    # {discovered, granted, active, revalidation_due}.
                    _C.validate_access_grant_transition(grant.status, "revoked")

                    txn_id = _new_txn_id()
                    intent = {
                        "schema": "cwk.rt015.revoke_intent.v1",
                        "txn_id": txn_id,
                        "grant_key": grant_key,
                        "tenant_id": tenant_id,
                        "source_namespace": source_namespace,
                        "report_id": report_id,
                        "prior_status": grant.status,
                        "prior_record_revision": grant.record_revision,
                        "tenant_auth_epoch_before": grant.auth_epoch,
                        "actor": actor,
                        "reason": reason,
                        "authority_receipt_id": (
                            authority_receipt["receipt_id"]
                            if authority_receipt is not None
                            else None
                        ),
                        "intended_at": _utcnow_iso(),
                    }
                    self._write_intent(tfd, txn_id, intent)
                    # From this instant, eligibility checks fail closed.
                    receipt = self._complete_revocation(tfd, tenant_id, intent)
                    return receipt

    def _write_intent(
        self, tenant_fd: int, txn_id: str, intent: dict[str, Any]
    ) -> None:
        _validate_against(_REVOKE_INTENT_SCHEMA_ID, intent)
        with self._sub_fd(tenant_fd, "revoke-intents") as ifd:
            _A.write_atomic(
                ifd,
                f"{txn_id}{_JOURNAL_SUFFIX}",
                _canonical_bytes(intent),
                exclusive=True,
            )

    def _complete_revocation(
        self,
        tenant_fd: int,
        tenant_id: str,
        intent: dict[str, Any],
    ) -> RevokeReceipt:
        """Run steps 2–7 of the revocation recipe idempotently."""

        grant_key = intent["grant_key"]
        txn_id = intent["txn_id"]

        # 2. CAS-mark grant revoked (if not already).
        grant = self._read_grant_file(tenant_fd, grant_key)
        if grant.status != "revoked":
            _C.validate_access_grant_transition(grant.status, "revoked")
            now = _utcnow_iso()
            new_grant = dict(grant.grant)
            new_grant["status"] = "revoked"
            new_grant["revoked_at"] = now
            new_grant["last_verified_at"] = now
            new_wrap = self._wrap_grant(
                grant_key=grant_key,
                grant_payload=new_grant,
                record_revision=grant.record_revision + 1,
                created_at=grant.payload["created_at"],
                updated_at=now,
            )
            grant = self._write_grant_file(
                tenant_fd, grant_key, new_wrap, expected_sha256=grant.on_disk_sha256
            )
            event = self._make_event(
                grant_key=grant_key,
                tenant_id=tenant_id,
                from_status=intent["prior_status"],
                to_status="revoked",
                tenant_auth_epoch_before=intent["tenant_auth_epoch_before"],
                tenant_auth_epoch_after=intent["tenant_auth_epoch_before"],
                record_revision_before=intent["prior_record_revision"],
                record_revision_after=grant.record_revision,
                actor=intent["actor"],
                reason=intent["reason"],
                evidence_refs=[f"revoke_intent:{txn_id}"],
            )
            self._append_event(tenant_fd, grant_key, event)

        # 3. CAS-bump tenant.auth_epoch (delegates to TenantRegistry).
        tenant = self._tenants.get(tenant_id)
        if tenant.auth_epoch == intent["tenant_auth_epoch_before"]:
            self._tenants.bump_auth_epoch(
                tenant_id,
                actor=intent["actor"],
                reason=f"grant_revocation:{grant_key}:{intent['reason']}"[:256],
                expected_auth_epoch=intent["tenant_auth_epoch_before"],
            )
        tenant_after = self._tenants.get(tenant_id)
        auth_epoch_after = tenant_after.auth_epoch

        # 4. Write tombstone (idempotent — same bytes if repeated).
        tombstone = {
            "schema": "cwk.rt015.access_tombstone.v1",
            "grant_key": grant_key,
            "tenant_id": tenant_id,
            "source_namespace": intent["source_namespace"],
            "report_id": intent["report_id"],
            "revoked_at": grant.grant["revoked_at"] or _utcnow_iso(),
            "tenant_auth_epoch_at_revoke": auth_epoch_after,
            "revocation_receipt_id": txn_id,
        }
        _validate_against(_TOMBSTONE_SCHEMA_ID, tombstone)
        tomb_bytes = _canonical_bytes(tombstone)
        with self._sub_fd(tenant_fd, "tombstones") as sfd:
            leaf = self._tombstone_leaf(grant_key)
            if _A.child_exists(sfd, leaf):
                existing_bytes = _A.read_file(sfd, leaf)
                if _sha256_bytes(existing_bytes) != _sha256_bytes(tomb_bytes):
                    raise AccessLedgerError(
                        "tombstone content drift detected", code="corrupt"
                    )
            else:
                _A.write_atomic(sfd, leaf, tomb_bytes, exclusive=True)
        tombstone_sha256 = _sha256_bytes(tomb_bytes)

        # 5. Write cleanup outbox (idempotent — recover reuses same outbox_id).
        outbox_id = _stable_outbox_id(txn_id)
        outbox = {
            "schema": "cwk.rt015.cleanup_outbox.v1",
            "outbox_id": outbox_id,
            "grant_key": grant_key,
            "tenant_id": tenant_id,
            "source_namespace": intent["source_namespace"],
            "report_id": intent["report_id"],
            "tenant_auth_epoch_after": auth_epoch_after,
            "revocation_receipt_id": txn_id,
            "consumers": ["tenant_view", "space_index", "cache"],
            "created_at": _utcnow_iso(),
        }
        _validate_against(_CLEANUP_OUTBOX_SCHEMA_ID, outbox)
        outbox_bytes = _canonical_bytes(outbox)
        with self._sub_fd(tenant_fd, "cleanup-outbox") as ofd:
            leaf = f"{outbox_id}{_GRANT_LEAF_SUFFIX}"
            if _A.child_exists(ofd, leaf):
                # Same outbox already exists — leave it as-is (consumer
                # may still be draining).  Compare header sanity.
                existing = _A.read_file(ofd, leaf)
                try:
                    parsed = _C.strict_json_loads(existing.decode("utf-8"))
                except (_C.ContractError, ValueError, UnicodeDecodeError):
                    raise AccessLedgerError(
                        "cleanup-outbox file corrupt", code="corrupt"
                    )
                if (
                    parsed.get("grant_key") != grant_key
                    or parsed.get("revocation_receipt_id") != txn_id
                ):
                    raise AccessLedgerError(
                        "cleanup-outbox binding mismatch", code="corrupt"
                    )
            else:
                _A.write_atomic(ofd, leaf, outbox_bytes, exclusive=True)
        outbox_sha256 = _sha256_bytes(outbox_bytes)

        # 6. Write revocation receipt (final, immutable).
        receipt = {
            "schema": "cwk.rt015.revoke_receipt.v1",
            "txn_id": txn_id,
            "grant_key": grant_key,
            "tenant_id": tenant_id,
            "source_namespace": intent["source_namespace"],
            "report_id": intent["report_id"],
            "tenant_auth_epoch_before": intent["tenant_auth_epoch_before"],
            "tenant_auth_epoch_after": auth_epoch_after,
            "record_revision_after": grant.record_revision,
            "tombstone_sha256": tombstone_sha256,
            "cleanup_outbox_sha256": outbox_sha256,
            "revoked_at": tombstone["revoked_at"],
            "actor": intent["actor"],
            "reason": intent["reason"],
        }
        _validate_against(_REVOKE_RECEIPT_SCHEMA_ID, receipt)
        receipt_bytes = _canonical_bytes(receipt)
        with self._sub_fd(tenant_fd, "revoke-receipts") as rfd:
            leaf = f"{txn_id}{_RECEIPT_SUFFIX}"
            if _A.child_exists(rfd, leaf):
                existing_bytes = _A.read_file(rfd, leaf)
                if _sha256_bytes(existing_bytes) != _sha256_bytes(receipt_bytes):
                    raise AccessLedgerError(
                        "revoke receipt content drift", code="corrupt"
                    )
            else:
                _A.write_atomic(rfd, leaf, receipt_bytes, exclusive=True)

        # 7. Unlink intent journal.
        with self._sub_fd(tenant_fd, "revoke-intents") as ifd:
            _A.unlink_at(ifd, f"{txn_id}{_JOURNAL_SUFFIX}", missing_ok=True)
            _A.fsync_dir(ifd)

        return RevokeReceipt(payload=receipt)

    def _intent_exists_for(self, tenant_fd: int, grant_key: str) -> bool:
        """Return True iff any pending revoke intent journal exists for grant_key.

        Optimised for the common case (no intents).  Journals are named
        by ``txn_id``; we scan and load each journal file to match on
        ``grant_key``.
        """

        with self._sub_fd(tenant_fd, "revoke-intents") as ifd:
            with os.scandir(ifd) as entries:
                for entry in entries:
                    if not entry.name.endswith(_JOURNAL_SUFFIX):
                        continue
                    if entry.name.startswith(_A.TEMP_PREFIX):
                        continue
                    try:
                        raw = _A.read_file(ifd, entry.name)
                    except (FileNotFoundError, _A.ContainmentError):
                        continue
                    try:
                        payload = _C.strict_json_loads(raw.decode("utf-8"))
                    except (_C.ContractError, ValueError, UnicodeDecodeError):
                        # Corrupt journal → treat as still-in-flight so
                        # we fail closed.
                        return True
                    if payload.get("grant_key") == grant_key:
                        return True
        return False

    def _tombstone_exists(self, tenant_fd: int, grant_key: str) -> bool:
        with self._sub_fd(tenant_fd, "tombstones") as sfd:
            return _A.child_exists(sfd, self._tombstone_leaf(grant_key))

    def _find_completed_receipt(
        self, tenant_fd: int, grant_key: str
    ) -> Optional[RevokeReceipt]:
        with self._sub_fd(tenant_fd, "revoke-receipts") as rfd:
            with os.scandir(rfd) as entries:
                for entry in entries:
                    if not entry.name.endswith(_RECEIPT_SUFFIX):
                        continue
                    if entry.name.startswith(_A.TEMP_PREFIX):
                        continue
                    try:
                        raw = _A.read_file(rfd, entry.name)
                    except FileNotFoundError:
                        continue
                    try:
                        payload = _C.strict_json_loads(raw.decode("utf-8"))
                    except (_C.ContractError, ValueError, UnicodeDecodeError):
                        continue
                    if payload.get("grant_key") == grant_key:
                        _validate_against(_REVOKE_RECEIPT_SCHEMA_ID, payload)
                        return RevokeReceipt(payload=payload)
        return None

    def read_tombstone(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
    ) -> Optional[Tombstone]:
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(tenant_id, report_key)
        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "tombstones") as sfd:
                if not _A.child_exists(sfd, self._tombstone_leaf(grant_key)):
                    return None
                raw = _A.read_file(sfd, self._tombstone_leaf(grant_key))
        payload = _C.strict_json_loads(raw.decode("utf-8"))
        _validate_against(_TOMBSTONE_SCHEMA_ID, payload)
        return Tombstone(payload=payload)

    # ------------------------------------------------------------------
    # Cleanup outbox — consumer contract
    # ------------------------------------------------------------------

    def iter_cleanup_outbox(
        self, *, tenant_id: str
    ) -> list[CleanupTask]:
        _I.validate_tenant_id(tenant_id)
        tasks: list[CleanupTask] = []
        try:
            with self._tenant_fd(tenant_id) as tfd:
                with self._sub_fd(tfd, "cleanup-outbox") as ofd:
                    with os.scandir(ofd) as entries:
                        names = sorted(
                            e.name for e in entries
                            if e.name.endswith(_GRANT_LEAF_SUFFIX)
                            and not e.name.startswith(_A.TEMP_PREFIX)
                        )
                    for name in names:
                        raw = _A.read_file(ofd, name)
                        payload = _C.strict_json_loads(raw.decode("utf-8"))
                        _validate_against(_CLEANUP_OUTBOX_SCHEMA_ID, payload)
                        tasks.append(CleanupTask(payload=payload))
        except GrantNotFound:
            return []
        return tasks

    def ack_cleanup_task(
        self,
        *,
        tenant_id: str,
        outbox_id: str,
        consumer: str,
        actor: str,
        reason: str,
    ) -> bool:
        """Idempotent ack.  Once every consumer has acked, the outbox file
        is unlinked.  Returns True iff the outbox file was actually
        removed by this call.
        """

        _validate_actor_reason(actor, reason)
        if consumer not in ("tenant_view", "space_index", "cache", "review_queue", "archive_metadata"):
            raise AccessLedgerError(
                f"unknown consumer {consumer!r}", code="contract"
            )
        with self._tenant_fd(tenant_id) as tfd:
            with self._sub_fd(tfd, "cleanup-outbox") as ofd:
                with _A.exclusive_lock(
                    ofd, f"outbox.{outbox_id}.lock", blocking=True
                ):
                    leaf = f"{outbox_id}{_GRANT_LEAF_SUFFIX}"
                    if not _A.child_exists(ofd, leaf):
                        return False
                    raw = _A.read_file(ofd, leaf)
                    payload = _C.strict_json_loads(raw.decode("utf-8"))
                    _validate_against(_CLEANUP_OUTBOX_SCHEMA_ID, payload)
                    consumers = list(payload.get("consumers", []))
                    if consumer not in consumers:
                        return False
                    consumers.remove(consumer)
                    if not consumers:
                        # Last consumer.  Unlink.  Idempotent.
                        _A.unlink_at(ofd, leaf, missing_ok=True)
                        _A.fsync_dir(ofd)
                        return True
                    new_payload = dict(payload)
                    new_payload["consumers"] = consumers
                    _validate_against(_CLEANUP_OUTBOX_SCHEMA_ID, new_payload)
                    _A.cas_write(
                        ofd,
                        leaf,
                        _canonical_bytes(new_payload),
                        expected_previous_sha256=_sha256_bytes(raw),
                    )
                    return False

    # ------------------------------------------------------------------
    # Query eligibility (snapshot-only)
    # ------------------------------------------------------------------

    def check_query_eligibility(
        self,
        *,
        snapshot: _AC.AgentContextSnapshot,
        source_namespace: str,
        report_id: str,
        now: Optional[_dt.datetime] = None,
    ) -> GrantRecord:
        """Fail-closed query eligibility check.

        Raises :class:`AccessDenied` for ANY of:

        - snapshot has non-queryable tenant status (per RT-013/RT-022 the
          allowed set is ``pilot``/``active``);
        - a revoke-intent journal exists for the grant;
        - a tombstone exists for the grant;
        - grant not found;
        - grant status is not ``active``;
        - lease has expired;
        - tenant.auth_epoch on disk differs from
          ``snapshot.tenant_auth_epoch``;
        - grant.auth_epoch does not equal tenant.auth_epoch at time of
          promotion (i.e. the grant is stale).

        The error message never distinguishes the cause; callers only see
        ``AccessDenied`` + a stable ``reason`` tag on the error object.
        """

        if not isinstance(snapshot, _AC.AgentContextSnapshot):
            raise AccessDenied(reason="snapshot_type")
        # Snapshot ↔ live tenant auth_epoch match.
        try:
            live_tenant = self._tenants.get(snapshot.tenant_id)
        except _R.TenantNotFound:
            raise AccessDenied(reason="tenant_unknown")
        if live_tenant.auth_epoch != snapshot.tenant_auth_epoch:
            raise AccessDenied(reason="stale_tenant_auth_epoch")
        if snapshot.tenant_status not in ("pilot", "active"):
            raise AccessDenied(reason="tenant_status")
        if live_tenant.status != snapshot.tenant_status:
            raise AccessDenied(reason="live_tenant_status_drift")

        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(snapshot.tenant_id, report_key)
        try:
            with self._tenant_fd(snapshot.tenant_id) as tfd:
                if self._intent_exists_for(tfd, grant_key):
                    raise AccessDenied(reason="revocation_in_progress")
                if self._tombstone_exists(tfd, grant_key):
                    raise AccessDenied(reason="tombstoned")
                try:
                    grant = self._read_grant_file(tfd, grant_key)
                except GrantNotFound:
                    raise AccessDenied(reason="no_grant")
                except GrantCorruption:
                    raise AccessDenied(reason="grant_corrupt")
        except GrantNotFound:
            # Tenant subdir does not yet exist → no grants → deny.
            raise AccessDenied(reason="no_grant")

        if grant.status != "active":
            raise AccessDenied(reason="not_active")
        if grant.auth_epoch != live_tenant.auth_epoch:
            raise AccessDenied(reason="grant_stale_epoch")
        lease = grant.lease_expires_at
        if lease is None:
            raise AccessDenied(reason="no_lease")
        current = now if now is not None else _dt.datetime.now(tz=_UTC)
        if current.tzinfo is None:
            raise AccessDenied(reason="naive_now")
        if _parse_iso(lease) <= current:
            raise AccessDenied(reason="lease_expired")

        return grant

    def read_grant_snapshot(
        self,
        *,
        snapshot: _AC.AgentContextSnapshot,
        source_namespace: str,
        report_id: str,
    ) -> GrantRecord:
        """Read the grant record for a query snapshot without eligibility gating.

        For callers who want to introspect a grant they already know they
        cannot use (e.g. audit consumers).  Still refuses bare tenant_id.
        """

        if not isinstance(snapshot, _AC.AgentContextSnapshot):
            raise AccessLedgerError(
                "snapshot must be AgentContextSnapshot", code="contract"
            )
        report_key = _C.compose_report_key(source_namespace, report_id)
        grant_key = compute_grant_key(snapshot.tenant_id, report_key)
        with self._tenant_fd(snapshot.tenant_id) as tfd:
            return self._read_grant_file(tfd, grant_key)

    def list_query_eligible(
        self,
        *,
        snapshot: _AC.AgentContextSnapshot,
        now: Optional[_dt.datetime] = None,
    ) -> list[GrantRecord]:
        """Return all currently query-eligible grants for a snapshot.

        Uses :meth:`check_query_eligibility` internally so the same
        fail-closed gates apply.  Never enumerates other tenants; the
        tenant subdirectory scoping is enforced by the snapshot.
        """

        if not isinstance(snapshot, _AC.AgentContextSnapshot):
            raise AccessLedgerError(
                "snapshot must be AgentContextSnapshot", code="contract"
            )
        current = now if now is not None else _dt.datetime.now(tz=_UTC)
        eligible: list[GrantRecord] = []
        try:
            with self._tenant_fd(snapshot.tenant_id) as tfd:
                with self._sub_fd(tfd, "grants") as gfd:
                    with os.scandir(gfd) as entries:
                        leaves = sorted(
                            e.name for e in entries
                            if e.name.endswith(_GRANT_LEAF_SUFFIX)
                            and not e.name.startswith(_A.TEMP_PREFIX)
                        )
                for leaf in leaves:
                    grant_key = leaf[:-len(_GRANT_LEAF_SUFFIX)]
                    try:
                        rec = self._read_grant_file(tfd, grant_key)
                    except GrantCorruption:
                        continue
                    if rec.status != "active":
                        continue
                    if self._intent_exists_for(tfd, grant_key):
                        continue
                    if self._tombstone_exists(tfd, grant_key):
                        continue
                    try:
                        self.check_query_eligibility(
                            snapshot=snapshot,
                            source_namespace=rec.source_namespace,
                            report_id=rec.report_id,
                            now=current,
                        )
                    except AccessDenied:
                        continue
                    eligible.append(rec)
        except GrantNotFound:
            return []
        return eligible

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self, *, actor: str, reason: str) -> RecoveryReport:
        """Reconcile any in-flight revocation transactions.

        Idempotent.  For each tenant subdir under ``registry/access-ledger/``:

        - Walk ``revoke-intents/*.journal``.  If a matching receipt is
          present, unlink the journal.  Otherwise, re-run steps 2–7
          idempotently and unlink.
        - Sweep ``.cwk-tmp-*`` orphans in every subdir.

        Returns a redacted :class:`RecoveryReport`.
        """

        _validate_actor_reason(actor, reason)
        report = RecoveryReport()
        try:
            with self._layout.registry_fd("access-ledger") as afd:
                # Enumerate tenant subdirs (validated ids only).
                with os.scandir(afd) as entries:
                    tenant_ids = sorted(
                        e.name for e in entries
                        if _C.TENANT_ID_REGEX.match(e.name) and e.is_dir(follow_symlinks=False)
                    )
        except FileNotFoundError:
            return report

        for tenant_id in tenant_ids:
            try:
                report = self._recover_tenant(tenant_id, report, actor=actor, reason=reason)
            except Exception as exc:  # pragma: no cover - opaque
                report.inconsistencies.append(
                    {"code": "recover_error", "tenant_prefix": tenant_id[:8], "detail": str(exc)[:80]}
                )
        return report

    def _recover_tenant(
        self,
        tenant_id: str,
        report: RecoveryReport,
        *,
        actor: str,
        reason: str,
    ) -> RecoveryReport:
        with self._tenant_fd(tenant_id) as tfd:
            # 1. Sweep tmp orphans in every subdir.
            for sub in _TENANT_SUBDIRS:
                try:
                    with self._sub_fd(tfd, sub) as sfd:
                        removed = _A.recover_orphans(sfd)
                        report = RecoveryReport(
                            intents_completed=report.intents_completed,
                            intents_already_committed=report.intents_already_committed,
                            orphans_removed=report.orphans_removed + len(removed),
                            inconsistencies=list(report.inconsistencies),
                        )
                except NotInitialized:
                    continue

            # 2. Walk intents.
            try:
                with self._sub_fd(tfd, "revoke-intents") as ifd:
                    with os.scandir(ifd) as entries:
                        journals = sorted(
                            e.name for e in entries
                            if e.name.endswith(_JOURNAL_SUFFIX)
                            and not e.name.startswith(_A.TEMP_PREFIX)
                        )
            except NotInitialized:
                return report

            for journal_name in journals:
                try:
                    with self._sub_fd(tfd, "revoke-intents") as ifd:
                        raw = _A.read_file(ifd, journal_name)
                    intent = _C.strict_json_loads(raw.decode("utf-8"))
                    _validate_against(_REVOKE_INTENT_SCHEMA_ID, intent)
                    grant_key = intent["grant_key"]
                    txn_id = intent["txn_id"]
                    # If receipt already present, just unlink journal.
                    with self._sub_fd(tfd, "revoke-receipts") as rfd:
                        receipt_leaf = f"{txn_id}{_RECEIPT_SUFFIX}"
                        if _A.child_exists(rfd, receipt_leaf):
                            with self._sub_fd(tfd, "revoke-intents") as ifd2:
                                _A.unlink_at(ifd2, journal_name, missing_ok=True)
                                _A.fsync_dir(ifd2)
                            report = RecoveryReport(
                                intents_completed=report.intents_completed,
                                intents_already_committed=report.intents_already_committed + 1,
                                orphans_removed=report.orphans_removed,
                                inconsistencies=list(report.inconsistencies),
                            )
                            continue
                    with self._sub_fd(tfd, "locks") as lfd:
                        with _A.exclusive_lock(lfd, self._lock_leaf(grant_key)):
                            # Idempotently complete.
                            self._complete_revocation(tfd, tenant_id, intent)
                    report = RecoveryReport(
                        intents_completed=report.intents_completed + 1,
                        intents_already_committed=report.intents_already_committed,
                        orphans_removed=report.orphans_removed,
                        inconsistencies=list(report.inconsistencies),
                    )
                except Exception as exc:  # pragma: no cover - opaque
                    report.inconsistencies.append(
                        {
                            "code": "intent_recover_failed",
                            "journal_prefix": journal_name[:16],
                            "detail": str(exc)[:80],
                        }
                    )
                    continue
        return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_outbox_id(txn_id: str) -> str:
    """Deterministic outbox_id from a txn_id.

    Used so retries in :meth:`recover` produce the same outbox file
    (byte-identical) rather than accumulating duplicate entries.
    """

    material = b"cwk-access-ledger-outbox-v1\x00" + txn_id.encode("ascii")
    digest = hashlib.sha256(material).digest()[:16]
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return OUTBOX_PREFIX + encoded


_EVIDENCE_REF_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-:/+=")


def _is_safe_evidence_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if not value or len(value) > 256:
        return False
    lowered = value.lower()
    for ch in lowered:
        if ch not in _EVIDENCE_REF_ALLOWED:
            return False
    return True


def _clip_evidence_ref(value: str) -> str:
    return value[:256]


__all__ = [
    "AccessLedger",
    "AccessLedgerError",
    "AccessDenied",
    "AuthorityAdapter",
    "AuthorityRejected",
    "CleanupTask",
    "DEFAULT_LEASE_TTL_SECONDS",
    "FakeSigningAuthority",
    "GrantConflict",
    "GrantCorruption",
    "GrantNotFound",
    "GrantRecord",
    "GrantStateError",
    "LEASE_TTL_MAX_SECONDS",
    "LEASE_TTL_MIN_SECONDS",
    "LogInjectionDetected",
    "NotInitialized",
    "RecoveryReport",
    "RevocationInProgress",
    "RevokeReceipt",
    "SCHEMA_DIR",
    "Tombstone",
    "compute_grant_key",
    "_register_test_authority",
    "_unregister_test_authority",
    "_register_fake_signer",
    "_unregister_fake_signer",
    "_TEST_AUTHORITY_TOKEN",
]
