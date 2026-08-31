#!/usr/bin/env python3
"""RT-012: Tenant Registry — the single source-of-truth tenant record.

Owned by RT-012.  This module is the *only* place that:

- generates opaque tenant IDs (``t_[a-z0-9]{26}``);
- validates and persists tenant records at
  ``registry/tenants/<tenant_id>.json`` — the sole authoritative record;
- exposes the frozen six-state life-cycle FSM and the derived
  operation-permission matrix;
- bumps ``record_revision`` and ``auth_epoch`` (both CAS + monotonic);
- runs the two-phase provisioning transaction with a journal + commit
  receipt so a crash between artefact creation and record publication
  leaves a recoverable state.

Everything else — enable / disable / release, credential resolution,
binding registry, ACL, collector — is out of scope for RT-012 and is
explicitly rejected by the state machine (only :func:`init_tenant`
issues transitions; downstream RTs must add their own APIs behind their
own provider modules and can only supply *policy-valid* transitions).

The RT-011 report is explicit that any tenant config file elsewhere on
disk MUST be a read-only projection with a revision/hash pointer back to
this record; RT-012 therefore refuses to write a second tenant config
file.  The only file this module writes to per-tenant disk is
``config/tenant.projection.json`` — a projection annotated with
``record_revision`` and ``record_sha256`` — and it is regenerated
whenever ``bump_record_revision`` is called.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import cwk_atomic_file as A
import cwk_instance as I
import cwk_pr001_contracts as C  # frozen; RT-012 must NOT edit RT-011 helpers


# ---------------------------------------------------------------------------
# Frozen constants — the six-state life-cycle and per-operation permission
# matrix.  DO NOT rename or introduce alias states (enabled / disabled /
# provisioning / retiring are forbidden by PRD FR-02).
# ---------------------------------------------------------------------------


TENANT_STATES: tuple[str, ...] = (
    "draft",
    "profile_pending",
    "pilot",
    "active",
    "suspended",
    "offboarded",
)

# Allowed transitions per PRD FR-02.  `suspended` may go back to
# `profile_pending`, `pilot`, or `active` iff the caller supplies a fresh
# verifier receipt (see :func:`resume_from_suspended`).
TENANT_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("profile_pending",),
    "profile_pending": ("pilot", "suspended", "offboarded"),
    "pilot": ("active", "suspended", "offboarded"),
    "active": ("suspended", "offboarded"),
    "suspended": ("profile_pending", "pilot", "active", "offboarded"),
    "offboarded": (),
}

# Operation permission matrix (used by downstream RTs; RT-012 exposes but
# does not consume beyond doctor/state-graph output).
_ALL_OPS = (
    "admin_configure",
    "credential_resolve",
    "sampling_collect_bounded",
    "collector_run",
    "scheduler_run",
    "profile_ai",
    "profile_confirm",
    "query_broker",
)

TENANT_OPERATION_MATRIX: dict[str, frozenset[str]] = {
    "draft": frozenset({"admin_configure"}),
    "profile_pending": frozenset(
        {"admin_configure", "credential_resolve", "sampling_collect_bounded", "profile_ai", "profile_confirm"}
    ),
    "pilot": frozenset(
        {
            "admin_configure",
            "credential_resolve",
            "collector_run",
            "scheduler_run",
            "profile_ai",
            "profile_confirm",
            "query_broker",
        }
    ),
    "active": frozenset(
        {
            "admin_configure",
            "credential_resolve",
            "collector_run",
            "scheduler_run",
            "profile_ai",
            "profile_confirm",
            "query_broker",
        }
    ),
    "suspended": frozenset({"admin_configure"}),
    "offboarded": frozenset(),
}

TERMINAL_STATE = "offboarded"

# ID + txn ID grammars.  Tenant ID re-uses the RT-011 regex; txn IDs are
# their own opaque namespace.
import re as _re

TXN_ID_REGEX = _re.compile(r"\Atxn_[a-z0-9]{26}\Z")


TENANT_RECORD_SCHEMA = "cwk.rt012.tenant_record.v1"
PROVISION_RECEIPT_SCHEMA = "cwk.rt012.provision_receipt.v1"

# The maximum in-flight provision receipts we retain per tenant in the
# journal.  Anything older is compacted by :func:`recover`.
_MAX_JOURNAL_ENTRIES = 32
_MAX_INT_SAFE = C.IJSON_MAX_SAFE_INT

_UTC = _dt.timezone.utc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class TenantNotFound(RegistryError):
    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"tenant {tenant_id!r} not found", code="not_found")


class TenantExists(RegistryError):
    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"tenant {tenant_id!r} already exists", code="exists")


class InvalidTransition(RegistryError):
    def __init__(self, from_status: str, to_status: str) -> None:
        allowed = TENANT_ALLOWED_TRANSITIONS.get(from_status, ())
        super().__init__(
            f"illegal tenant transition {from_status} -> {to_status}; allowed: {list(allowed)}",
            code="illegal_transition",
        )
        self.from_status = from_status
        self.to_status = to_status


class RegistryConflict(RegistryError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="conflict")


class VerifierRequired(RegistryError):
    def __init__(self) -> None:
        super().__init__(
            "resuming from suspended requires a verifier receipt", code="verifier_required"
        )


class SchemaError(RegistryError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="schema")


class RecordCorruption(RegistryError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="corruption")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_bytes(payload: Any) -> bytes:
    return C.canonical_json_bytes(payload)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(payload: Any) -> str:
    return _sha256_bytes(_canonical_bytes(payload))


def _tenant_record_name(tenant_id: str) -> str:
    I.validate_tenant_id(tenant_id)
    return f"{tenant_id}.json"


def _tenant_record_lock_name(tenant_id: str) -> str:
    I.validate_tenant_id(tenant_id)
    return f".{tenant_id}.lock"


def _provision_journal_name(tenant_id: str, txn_id: str) -> str:
    I.validate_tenant_id(tenant_id)
    if not TXN_ID_REGEX.match(txn_id):
        raise RegistryError(f"txn_id does not match {TXN_ID_REGEX.pattern!r}", code="txn_id")
    return f"{tenant_id}.{txn_id}.journal"


def _provision_receipt_name(tenant_id: str, txn_id: str) -> str:
    I.validate_tenant_id(tenant_id)
    if not TXN_ID_REGEX.match(txn_id):
        raise RegistryError(f"txn_id does not match {TXN_ID_REGEX.pattern!r}", code="txn_id")
    return f"{tenant_id}.{txn_id}.receipt"


# ---------------------------------------------------------------------------
# Frozen state machine helpers (pure policy — no side effects)
# ---------------------------------------------------------------------------


def is_valid_transition(from_status: str, to_status: str) -> bool:
    if from_status not in TENANT_STATES or to_status not in TENANT_STATES:
        return False
    return to_status in TENANT_ALLOWED_TRANSITIONS[from_status]


def assert_valid_transition(from_status: str, to_status: str) -> None:
    if from_status not in TENANT_STATES:
        raise RegistryError(f"unknown source state {from_status!r}", code="state")
    if to_status not in TENANT_STATES:
        raise RegistryError(f"unknown target state {to_status!r}", code="state")
    if to_status not in TENANT_ALLOWED_TRANSITIONS[from_status]:
        raise InvalidTransition(from_status, to_status)


def state_graph() -> dict[str, Any]:
    """Machine-readable dump of the FSM + operation matrix."""

    return {
        "schema": "cwk.rt012.state_graph.v1",
        "states": list(TENANT_STATES),
        "terminal_state": TERMINAL_STATE,
        "transitions": {k: list(v) for k, v in TENANT_ALLOWED_TRANSITIONS.items()},
        "operation_matrix": {k: sorted(v) for k, v in TENANT_OPERATION_MATRIX.items()},
        "forbidden_aliases": ["enabled", "disabled", "provisioning", "retiring"],
    }


# ---------------------------------------------------------------------------
# Tenant record schema validation
# ---------------------------------------------------------------------------


_SCHEMA_PATH = (
    C.SCHEMA_ROOT / "rt012" / "schemas" / "tenant_record.schema.json"
)
_PROVISION_SCHEMA_PATH = (
    C.SCHEMA_ROOT / "rt012" / "schemas" / "provision_receipt.schema.json"
)

_TENANT_RECORD_SCHEMA_CACHE: dict[str, Any] = {}


def _tenant_record_schema() -> Any:
    if "record" not in _TENANT_RECORD_SCHEMA_CACHE:
        _TENANT_RECORD_SCHEMA_CACHE["record"] = C.strict_json_load_path(_SCHEMA_PATH)
    return _TENANT_RECORD_SCHEMA_CACHE["record"]


def _provision_receipt_schema() -> Any:
    if "receipt" not in _TENANT_RECORD_SCHEMA_CACHE:
        _TENANT_RECORD_SCHEMA_CACHE["receipt"] = C.strict_json_load_path(_PROVISION_SCHEMA_PATH)
    return _TENANT_RECORD_SCHEMA_CACHE["receipt"]


def validate_tenant_record(payload: Any) -> None:
    """Validate a tenant record against the RT-012 schema.

    We reuse the RT-011 Draft 2020-12 subset engine so we get the same
    ``additionalProperties: false`` / ``uniqueItems`` / regex enforcement.
    """

    schema = _tenant_record_schema()
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise SchemaError(str(exc)) from exc
    # Belt-and-braces: reject bool-as-int (Python bools ARE ints).
    for key in ("auth_epoch", "record_revision"):
        val = payload.get(key)
        if isinstance(val, bool):
            raise SchemaError(f"{key} must be an integer, not bool")


def validate_provision_receipt(payload: Any) -> None:
    schema = _provision_receipt_schema()
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise SchemaError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Fresh IDs
# ---------------------------------------------------------------------------


def new_tenant_id() -> str:
    """Generate a fresh opaque tenant ID matching ``t_[a-z0-9]{26}``."""

    # 26 chars of base32-alphabet (a-z0-9) provides ~130 bits of entropy.
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    tid = f"t_{body}"
    I.validate_tenant_id(tid)
    return tid


def new_txn_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    txn = f"txn_{body}"
    if not TXN_ID_REGEX.match(txn):  # pragma: no cover - unreachable
        raise RegistryError("generated txn_id failed regex", code="txn_id")
    return txn


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantRecord:
    """Immutable in-memory view of a tenant record.

    ``on_disk_sha256`` is the sha256 of the exact bytes stored on disk
    (needed for CAS via :func:`cwk_atomic_file.cas_write`) whereas
    :meth:`canonical_sha256` returns the RFC 8785 JCS SHA-256 for use in
    receipts / audit rows (payload-shape-stable, independent of pretty
    printing).
    """

    payload: dict[str, Any]
    on_disk_sha256: str = ""

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def status(self) -> str:
        return self.payload["status"]

    @property
    def record_revision(self) -> int:
        return int(self.payload["record_revision"])

    @property
    def auth_epoch(self) -> int:
        return int(self.payload["auth_epoch"])

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload)


@dataclass(frozen=True)
class ProvisionReceipt:
    payload: dict[str, Any]

    @property
    def txn_id(self) -> str:
        return self.payload["txn_id"]

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]


class TenantRegistry:
    """Single-writer, dirfd-anchored tenant registry."""

    def __init__(self, layout: I.InstanceLayout) -> None:
        self.layout = layout

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def list_tenant_ids(self) -> list[str]:
        """Return sorted opaque tenant IDs for which a record exists.

        Fails closed on a genuinely broken registry (LayoutError raised from
        containment / symlink / non-dir) but returns an empty list for the
        legitimate case where the registry directory simply hasn't been
        initialised yet.
        """

        ids: list[str] = []
        try:
            with self.layout.registry_fd("tenants") as rfd:
                with os.scandir(rfd) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        candidate = entry.name[:-5]
                        try:
                            I.validate_tenant_id(candidate)
                        except I.TenantIdError:
                            continue
                        ids.append(candidate)
        except FileNotFoundError:
            return []
        except I.LayoutError as exc:
            # Missing directory (i.e. instance not yet initialised) is fine;
            # a broken symlink / non-dir means fail closed.
            if "does not exist" in str(exc):
                return []
            raise
        ids.sort()
        return ids

    def get(self, tenant_id: str) -> TenantRecord:
        """Load and validate a tenant record.  Raises ``TenantNotFound``."""

        I.validate_tenant_id(tenant_id)
        try:
            with self.layout.registry_fd("tenants") as rfd:
                try:
                    raw = A.read_file(rfd, _tenant_record_name(tenant_id))
                except FileNotFoundError as exc:
                    raise TenantNotFound(tenant_id) from exc
        except I.LayoutError as exc:
            # Missing directory (i.e. instance not yet initialised) means
            # the tenant demonstrably does not exist.  Any other LayoutError
            # (symlink, non-dir) is fail closed as containment.
            if "does not exist" in str(exc):
                raise TenantNotFound(tenant_id) from exc
            raise
        try:
            payload = C.strict_json_loads(raw.decode("utf-8"))
        except C.ContractError as exc:
            raise RecordCorruption(f"tenant record for {tenant_id!r} is not strict JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RecordCorruption(f"tenant record for {tenant_id!r} is not JSON: {exc.msg}") from exc
        except UnicodeDecodeError as exc:
            raise RecordCorruption(f"tenant record for {tenant_id!r} is not UTF-8") from exc
        # A corrupt record MUST NOT be treated as empty; that would let an
        # attacker who could truncate the file re-init the tenant.
        validate_tenant_record(payload)
        if payload["tenant_id"] != tenant_id:
            raise RecordCorruption(
                f"tenant record filename does not match tenant_id {tenant_id!r}"
            )
        return TenantRecord(payload=payload, on_disk_sha256=_sha256_bytes(raw))

    # ------------------------------------------------------------------
    # Provision (init) — two-phase with journal + receipt
    # ------------------------------------------------------------------

    def init_tenant(
        self,
        *,
        tenant_id: Optional[str] = None,
        actor: str,
        reason: str = "tenant_init",
    ) -> tuple[TenantRecord, ProvisionReceipt]:
        """Create a brand-new tenant in ``draft`` state.

        The transaction is: journal → tenant directory tree → tenant record
        (via CAS: expected_previous_sha256=None) → append commit receipt.
        A crash between any step and receipt commit is recoverable via
        :meth:`recover`.

        Only the ``tenant_id`` optionally supplied here is used, and it must
        be opaque; user-derived IDs are forbidden.  If ``tenant_id`` is
        omitted a fresh opaque ID is generated.
        """

        # Reject any caller who supplied a user-facing name / path here.
        if tenant_id is None:
            tenant_id = new_tenant_id()
        else:
            I.validate_tenant_id(tenant_id)

        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise RegistryError("actor must be a non-empty <=128 char str", code="actor")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise RegistryError("reason must be a non-empty <=256 char str", code="reason")

        # Fast conflict check outside the lock.
        try:
            self.get(tenant_id)
            raise TenantExists(tenant_id)
        except TenantNotFound:
            pass

        # Acquire an exclusive lock on the tenant record before doing anything.
        with self.layout.registry_fd("tenants") as reg_tenants_fd:
            lock_name = _tenant_record_lock_name(tenant_id)
            with A.exclusive_lock(reg_tenants_fd, lock_name):
                # Re-check under the lock.
                if A.child_exists(reg_tenants_fd, _tenant_record_name(tenant_id)):
                    raise TenantExists(tenant_id)

                txn_id = new_txn_id()

                # Phase 1: write journal describing intended artefacts.
                journal_payload = self._build_journal(tenant_id=tenant_id, txn_id=txn_id, actor=actor, reason=reason)
                with self.layout.registry_fd("provision-journal") as jfd:
                    A.write_atomic(
                        jfd,
                        _provision_journal_name(tenant_id, txn_id),
                        (json.dumps(journal_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                        exclusive=True,
                    )

                # Phase 2: create tenant directory tree.
                tenant_layout = self.layout.tenant(tenant_id)
                tenant_layout.initialize()

                # Compute artefact digests for the receipt.
                artefacts = self._digest_artefacts(tenant_layout)

                # Phase 3: publish tenant record via CAS (no prior state).
                now = _utcnow_iso()
                record_payload = {
                    "schema": TENANT_RECORD_SCHEMA,
                    "tenant_id": tenant_id,
                    "status": "draft",
                    "credential_ref": None,
                    "active_profile_version": None,
                    "auth_epoch": 1,
                    "record_revision": 1,
                    "quota": _default_quota_scaffold(),
                    "created_at": now,
                    "updated_at": now,
                    "provisioning": {
                        "last_receipt_id": txn_id,
                        "last_receipt_sha256": "0" * 64,  # placeholder; overwritten below
                    },
                    "state_history": [
                        {
                            "from_status": None,
                            "to_status": "draft",
                            "at": now,
                            "actor": actor,
                            "reason": reason,
                            "record_revision_after": 1,
                            "auth_epoch_after": 1,
                        }
                    ],
                }

                # Build receipt from the record (record_revision/auth_epoch
                # both come from the record we're about to publish).
                receipt_payload = {
                    "schema": PROVISION_RECEIPT_SCHEMA,
                    "txn_id": txn_id,
                    "tenant_id": tenant_id,
                    "action": "tenant_init",
                    "committed_at": now,
                    "record_revision_after": 1,
                    "auth_epoch_after": 1,
                    "tenant_status_after": "draft",
                    "artefacts": artefacts,
                    "receipt_sha256": "0" * 64,
                }
                receipt_sha = _canonical_sha256(
                    {k: v for k, v in receipt_payload.items() if k != "receipt_sha256"}
                )
                receipt_payload["receipt_sha256"] = receipt_sha
                record_payload["provisioning"]["last_receipt_sha256"] = receipt_sha

                # Validate before writing so a corrupt draft never lands.
                validate_tenant_record(record_payload)
                validate_provision_receipt(receipt_payload)

                # Phase 4: publish tenant record via write_atomic
                # (exclusive=True enforces "no prior state").
                record_bytes = (
                    json.dumps(record_payload, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                A.write_atomic(
                    reg_tenants_fd,
                    _tenant_record_name(tenant_id),
                    record_bytes,
                    exclusive=True,
                )

                # Phase 5: append commit receipt (the presence of a valid
                # receipt file is the "committed" signal to :meth:`recover`).
                with self.layout.registry_fd("provision-receipts") as rcpt_fd:
                    A.write_atomic(
                        rcpt_fd,
                        _provision_receipt_name(tenant_id, txn_id),
                        (json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n").encode(
                            "utf-8"
                        ),
                        exclusive=True,
                    )

                # Phase 6: safe to remove the journal entry — receipt is durable.
                with self.layout.registry_fd("provision-journal") as jfd:
                    A.unlink_at(jfd, _provision_journal_name(tenant_id, txn_id), missing_ok=True)

                # Also write the read-only projection under tenants/<id>/config/tenant.projection.json.
                self._write_projection(tenant_layout, record_payload)

        return TenantRecord(payload=record_payload), ProvisionReceipt(payload=receipt_payload)

    # ------------------------------------------------------------------
    # CAS record mutations
    # ------------------------------------------------------------------

    def bump_record_revision(
        self,
        tenant_id: str,
        *,
        actor: str,
        reason: str,
        expected_revision: int,
    ) -> TenantRecord:
        """Increment ``record_revision`` under CAS."""

        record = self.get(tenant_id)
        if record.record_revision != expected_revision:
            raise RegistryConflict(
                f"expected record_revision {expected_revision}, got {record.record_revision}"
            )
        with self.layout.registry_fd("tenants") as reg_tenants_fd:
            with A.exclusive_lock(reg_tenants_fd, _tenant_record_lock_name(tenant_id)):
                # Re-load inside the lock to prevent lost update.
                record = self.get(tenant_id)
                if record.record_revision != expected_revision:
                    raise RegistryConflict(
                        f"lost update: expected {expected_revision}, disk has {record.record_revision}"
                    )
                new_payload = dict(record.payload)
                new_payload["record_revision"] = _monotonic(new_payload["record_revision"])
                new_payload["updated_at"] = _utcnow_iso()
                self._commit_record(reg_tenants_fd, tenant_id, record, new_payload, actor=actor, reason=reason, action="record_revision_bump")
        return self.get(tenant_id)

    def bump_auth_epoch(
        self,
        tenant_id: str,
        *,
        actor: str,
        reason: str,
        expected_auth_epoch: int,
    ) -> TenantRecord:
        """Monotonically increment ``auth_epoch`` under CAS.

        The API refuses direct ``set`` semantics and rejects wrap-around
        past ``2**53 - 1``.  Callers MUST provide the current epoch they
        observed; a lost update raises :class:`RegistryConflict`.
        """

        record = self.get(tenant_id)
        if record.auth_epoch != expected_auth_epoch:
            raise RegistryConflict(
                f"expected auth_epoch {expected_auth_epoch}, got {record.auth_epoch}"
            )
        with self.layout.registry_fd("tenants") as reg_tenants_fd:
            with A.exclusive_lock(reg_tenants_fd, _tenant_record_lock_name(tenant_id)):
                record = self.get(tenant_id)
                if record.auth_epoch != expected_auth_epoch:
                    raise RegistryConflict(
                        f"lost update: expected auth_epoch {expected_auth_epoch}, disk has {record.auth_epoch}"
                    )
                new_payload = dict(record.payload)
                new_payload["auth_epoch"] = _monotonic(new_payload["auth_epoch"])
                # Any state change must bump auth_epoch, so bumping auth_epoch
                # also bumps record_revision to keep both monotonic.
                new_payload["record_revision"] = _monotonic(new_payload["record_revision"])
                new_payload["updated_at"] = _utcnow_iso()
                self._commit_record(
                    reg_tenants_fd,
                    tenant_id,
                    record,
                    new_payload,
                    actor=actor,
                    reason=reason,
                    action="auth_epoch_bump",
                )
        return self.get(tenant_id)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Reconcile any half-finished provisioning transactions.

        Deterministic outcome:

        1. Sweep the ``provision-journal/`` directory.  For each journal
           entry, look for a matching committed receipt in
           ``provision-receipts/``.  Two cases:

           - **Committed**: the tenant record already exists (or can be
             produced by re-validating the receipt).  In this case the
             journal entry is compacted (unlinked).
           - **Uncommitted**: no receipt file matches.  The tenant record
             is either absent (safe to remove any leftover tenant tree) or
             present without a matching last_receipt_id (that means the
             record + receipt landed but the journal cleanup crashed —
             also safe to unlink the journal entry).

        2. Sweep any ``.cwk-tmp-*`` orphans in the registry directories.

        The recovery step is idempotent; running it twice is a no-op and
        never touches active/committed tenants.  Returns a redacted summary
        for logging.
        """

        summary: dict[str, Any] = {
            "journal_swept": 0,
            "orphans_removed": 0,
            "uncommitted_rolled_back": 0,
        }

        # Sweep orphans in every RT-012-controlled dir.
        for sub in ("tenants", "provision-journal", "provision-receipts"):
            with self.layout.registry_fd(sub) as fd:
                for name in A.recover_orphans(fd):
                    summary["orphans_removed"] += 1
                    del name  # unused

        # Now walk the journal.
        with self.layout.registry_fd("provision-journal") as jfd:
            journal_entries = []
            with os.scandir(jfd) as entries:
                for entry in entries:
                    if not entry.name.endswith(".journal"):
                        continue
                    if entry.name.startswith(A.TEMP_PREFIX):
                        continue
                    journal_entries.append(entry.name)

        for name in journal_entries:
            # <tenant_id>.<txn_id>.journal
            try:
                tenant_id, txn_id, _ = name.split(".", 2)
            except ValueError:
                # Malformed, skip.
                continue
            try:
                I.validate_tenant_id(tenant_id)
                if not TXN_ID_REGEX.match(txn_id):
                    continue
            except I.TenantIdError:
                continue

            with self.layout.registry_fd("provision-receipts") as rfd:
                receipt_exists = A.child_exists(rfd, _provision_receipt_name(tenant_id, txn_id))

            with self.layout.registry_fd("provision-journal") as jfd:
                if receipt_exists:
                    # Committed — safe to unlink journal.
                    A.unlink_at(jfd, name, missing_ok=True)
                    summary["journal_swept"] += 1
                else:
                    # Uncommitted.  Only roll back if the tenant record itself
                    # does not exist (otherwise a later commit is racing and
                    # we must not touch it).
                    try:
                        self.get(tenant_id)
                        # Record exists; the journal is stale.  Unlink.
                        A.unlink_at(jfd, name, missing_ok=True)
                        summary["journal_swept"] += 1
                    except TenantNotFound:
                        # Uncommitted staging tree.  Best-effort: remove the
                        # tenant tree only if it contains nothing but the
                        # frozen directories we ourselves created.
                        rolled = self._rollback_uncommitted_tenant_tree(tenant_id)
                        if rolled:
                            summary["uncommitted_rolled_back"] += 1
                        A.unlink_at(jfd, name, missing_ok=True)

        return summary

    def _rollback_uncommitted_tenant_tree(self, tenant_id: str) -> bool:
        """Remove an uncommitted ``tenants/<tenant_id>/`` tree.

        Refuses to touch anything if:

        - the directory contains files or non-frozen names (i.e. downstream
          RTs already populated it);
        - the tenant record file exists (would be a committed tenant).

        This intentionally does NOT do wildcard glob deletes or
        ``rm -rf INSTANCE_ROOT`` — the only names we ever unlink are those
        we ourselves initialised.
        """

        try:
            with self.layout.child_fd("tenants") as tenants_fd:
                if not A.child_exists(tenants_fd, tenant_id):
                    return False
                with self.layout.tenant(tenant_id).tenant_fd() as tfd:
                    frozen = set(I.TENANT_CHILDREN)
                    with os.scandir(tfd) as entries:
                        names = [e.name for e in entries]
                    unknown = [n for n in names if n not in frozen]
                    if unknown:
                        return False
                    # Every entry must be an empty frozen sub-directory.
                    for name in names:
                        # Descend and check emptiness.
                        # Refuse deletion if any child exists inside — that
                        # would be RT-013+ data.
                        try:
                            with self.layout.tenant(tenant_id).child_fd(name) as sub_fd:
                                with os.scandir(sub_fd) as sub_entries:
                                    for _ in sub_entries:
                                        return False
                        except I.LayoutError:
                            return False
                    # All frozen sub-directories are empty.  Remove them.
                    for name in names:
                        try:
                            os.rmdir(name, dir_fd=tfd)
                        except OSError:
                            return False
                    A.fsync_dir(tfd)
                # Now rmdir the tenant root.
                os.rmdir(tenant_id, dir_fd=tenants_fd)
                A.fsync_dir(tenants_fd)
                return True
        except I.LayoutError:
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_journal(
        self, *, tenant_id: str, txn_id: str, actor: str, reason: str
    ) -> dict[str, Any]:
        return {
            "schema": "cwk.rt012.provision_journal.v1",
            "tenant_id": tenant_id,
            "txn_id": txn_id,
            "action": "tenant_init",
            "actor": actor,
            "reason": reason,
            "started_at": _utcnow_iso(),
            "frozen_tenant_children": list(I.TENANT_CHILDREN),
        }

    def _digest_artefacts(self, tenant_layout: I.TenantLayout) -> list[dict[str, Any]]:
        """Return artefact digest list for the receipt.

        We record only the directory shape at RT-012 time; every frozen
        tenant sub-directory contributes one row with sha256 = "0" * 64 as
        the "empty directory" sentinel.  Downstream RTs augment this with
        file-level artefacts.
        """

        rows: list[dict[str, Any]] = []
        for name in I.TENANT_CHILDREN:
            rows.append(
                {
                    "relative_path": f"{name}/",
                    "kind": "directory",
                    "mode": A.DIRECTORY_MODE,
                    "sha256": "0" * 64,
                }
            )
        return rows

    def _write_projection(self, tenant_layout: I.TenantLayout, record: dict[str, Any]) -> None:
        """Write ``config/tenant.projection.json`` — a read-only projection.

        The projection includes ``record_revision`` and ``record_sha256`` so
        anyone reading the projection can (a) know exactly which registry
        revision it mirrors and (b) detect drift.
        """

        projection = {
            "schema": "cwk.rt012.tenant_projection.v1",
            "tenant_id": record["tenant_id"],
            "status": record["status"],
            "record_revision": record["record_revision"],
            "auth_epoch": record["auth_epoch"],
            "record_sha256": _canonical_sha256(record),
            "note": "READ-ONLY PROJECTION. The authoritative record lives at registry/tenants/<tenant_id>.json.",
        }
        body = (json.dumps(projection, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with tenant_layout.child_fd("config") as cfd:
            A.write_atomic(cfd, "tenant.projection.json", body)

    def _commit_record(
        self,
        reg_tenants_fd: int,
        tenant_id: str,
        previous: TenantRecord,
        new_payload: dict[str, Any],
        *,
        actor: str,
        reason: str,
        action: str,
    ) -> None:
        """Commit a mutated record via CAS + fresh receipt."""

        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise RegistryError("actor must be a non-empty <=128 char str", code="actor")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise RegistryError("reason must be a non-empty <=256 char str", code="reason")

        txn_id = new_txn_id()
        history = list(new_payload.get("state_history", []))
        history.append(
            {
                "from_status": previous.status,
                "to_status": new_payload["status"],
                "at": new_payload["updated_at"],
                "actor": actor,
                "reason": reason,
                "record_revision_after": new_payload["record_revision"],
                "auth_epoch_after": new_payload["auth_epoch"],
            }
        )
        new_payload["state_history"] = history
        new_payload["provisioning"] = {
            "last_receipt_id": txn_id,
            "last_receipt_sha256": "0" * 64,
        }

        # Write a journal entry first.
        journal_payload = self._build_journal(
            tenant_id=tenant_id, txn_id=txn_id, actor=actor, reason=reason
        )
        journal_payload["action"] = action
        with self.layout.registry_fd("provision-journal") as jfd:
            A.write_atomic(
                jfd,
                _provision_journal_name(tenant_id, txn_id),
                (json.dumps(journal_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                exclusive=True,
            )

        # Build receipt.
        receipt_payload = {
            "schema": PROVISION_RECEIPT_SCHEMA,
            "txn_id": txn_id,
            "tenant_id": tenant_id,
            "action": action,
            "committed_at": new_payload["updated_at"],
            "record_revision_after": new_payload["record_revision"],
            "auth_epoch_after": new_payload["auth_epoch"],
            "tenant_status_after": new_payload["status"],
            "artefacts": [
                {
                    "relative_path": f"registry/tenants/{tenant_id}.json",
                    "kind": "file",
                    "mode": A.FILE_MODE,
                    "sha256": _canonical_sha256(new_payload),
                }
            ],
            "receipt_sha256": "0" * 64,
        }
        receipt_sha = _canonical_sha256(
            {k: v for k, v in receipt_payload.items() if k != "receipt_sha256"}
        )
        receipt_payload["receipt_sha256"] = receipt_sha
        new_payload["provisioning"]["last_receipt_sha256"] = receipt_sha

        validate_tenant_record(new_payload)
        validate_provision_receipt(receipt_payload)

        record_bytes = (
            json.dumps(new_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        A.cas_write(
            reg_tenants_fd,
            _tenant_record_name(tenant_id),
            record_bytes,
            expected_previous_sha256=previous.on_disk_sha256,
        )

        with self.layout.registry_fd("provision-receipts") as rcpt_fd:
            A.write_atomic(
                rcpt_fd,
                _provision_receipt_name(tenant_id, txn_id),
                (json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
                exclusive=True,
            )

        with self.layout.registry_fd("provision-journal") as jfd:
            A.unlink_at(jfd, _provision_journal_name(tenant_id, txn_id), missing_ok=True)

        # Refresh the projection.
        self._write_projection(self.layout.tenant(tenant_id), new_payload)


def _monotonic(current: int) -> int:
    """Return ``current + 1``; reject bool and wrap-around past 2**53 - 1."""

    if isinstance(current, bool) or not isinstance(current, int):
        raise RegistryError("value must be an int (not bool)", code="type")
    if current < 1:
        raise RegistryError("value must be >= 1", code="value")
    if current >= _MAX_INT_SAFE:
        raise RegistryError("value would exceed I-JSON safe integer range", code="overflow")
    return current + 1


def _default_quota_scaffold() -> dict[str, Any]:
    """Return the frozen unset-quota scaffold.

    RT-024 owns quota measurement; RT-012 only freezes the *structure* and
    the CAS interface, so every limit is intentionally ``null`` (not zero
    and not infinity — see PRD §NFR-05).
    """

    return {
        "scheme": "cwk.rt012.quota.unset.v1",
        "measurement_owner": "RT-024",
        "confirmation_owner": "RT-026",
        "limits": {
            "collector_concurrency": None,
            "scheduler_concurrency": None,
            "disk_bytes": None,
            "ai_calls_per_day": None,
            "retention_days": None,
        },
    }


__all__ = [
    "InvalidTransition",
    "PROVISION_RECEIPT_SCHEMA",
    "ProvisionReceipt",
    "RecordCorruption",
    "RegistryConflict",
    "RegistryError",
    "SchemaError",
    "TENANT_ALLOWED_TRANSITIONS",
    "TENANT_OPERATION_MATRIX",
    "TENANT_RECORD_SCHEMA",
    "TENANT_STATES",
    "TERMINAL_STATE",
    "TXN_ID_REGEX",
    "TenantExists",
    "TenantNotFound",
    "TenantRecord",
    "TenantRegistry",
    "VerifierRequired",
    "assert_valid_transition",
    "is_valid_transition",
    "new_tenant_id",
    "new_txn_id",
    "state_graph",
    "validate_provision_receipt",
    "validate_tenant_record",
]
