#!/usr/bin/env python3
"""RT-015: Tenant View Store — overlay-only per-tenant view of one canonical
version.

Owned by RT-015.  This module provides the on-disk overlay store described
in PRD FR-07/DESIGN §5 C-09.  The tenant view is strictly an overlay:

- It references a canonical evidence version via
  ``canonical_sha256`` and verifies the reference through
  :meth:`cwk_shared_evidence.SharedEvidenceStore.read_version` before
  persisting or returning any view.
- It NEVER copies the canonical body / reply-body / node-body text.  The
  frozen ``cwk.tenant_view.v1`` schema only permits opaque IDs plus
  optional ``content_sha256`` fingerprints for replies and nodes.
- Temporary URLs (attachment presign, preview) are permitted ONLY in
  ``attachment_permissions[].temporary_url`` on the overlay, and never
  bleed into canonical / catalog / event.  Callers are still expected
  to redact these before logging.
- The overlay is NOT a second permission source.  Every read runs a
  fail-closed :meth:`AccessLedger.check_query_eligibility` before AND
  after loading the overlay, so an in-flight revocation cannot leak a
  stale view.
- File-system layout: ``tenants/<tenant_id>/views/<view_key>.json`` where
  ``view_key`` is the same opaque ``g_...`` grant_key derived from
  ``H(tenant_id, report_key)`` — no ``report_id`` in path, no
  ``source_namespace`` in path, no cross-tenant enumeration surface.

Boundary notes (see references/讨论决策记录, references/安全威胁模型, and
RT-016 pre-review from 2026-08-19):

- The frozen ``cwk.tenant_view.v1`` schema keeps ``reply_overlay`` and
  ``node_overlay`` at IDs + optional SHA-256 only.  Full reply/node
  payload text lives on the source system.  Legacy RT-007 timeline
  snapshots contain the full payload but with untrusted ordering — RT-016
  may only use them for hash / crosswalk / review; it cannot upgrade them
  into ``verified_shared`` fields or extend the tenant view schema through
  RT-015 without a fresh RT-011-level review.
- No CLI / HTTP surface.  Consumers instantiate the store via
  ``TenantViewStore(layout, ledger, shared_store)``.
"""

from __future__ import annotations

import datetime as _dt
import errno
import os
import stat as _stat_mod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import cwk_access_ledger as _AL
import cwk_agent_context as _AC
import cwk_atomic_file as _A
import cwk_instance as _I
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _SE


_UTC = _dt.timezone.utc

_VIEW_RECORD_SCHEMA_ID = "cwk.pr001.rt015.tenant_view_record.v1"
_VIEW_LEAF_SUFFIX = ".json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TenantViewError(Exception):
    """Base error for view store failures.

    ``__str__`` never contains absolute paths, body bytes, tenant IDs
    beyond a truncated prefix, or credential material.
    """

    def __init__(self, message: str, *, code: str, view_key: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.view_key = view_key

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.view_key:
            return f"[{self.code}] {base} (view_key={self.view_key})"
        return f"[{self.code}] {base}"


class ViewNotFound(TenantViewError):
    def __init__(self, view_key: str | None = None) -> None:
        super().__init__("tenant view not found", code="not_found", view_key=view_key)


class ViewDenied(TenantViewError):
    """Raised when access-ledger eligibility fails during a view read."""

    def __init__(self) -> None:
        super().__init__("tenant view access denied", code="denied")


class ViewCorruption(TenantViewError):
    def __init__(self, message: str, view_key: str | None = None) -> None:
        super().__init__(message, code="corrupt", view_key=view_key)


class CanonicalMissing(TenantViewError):
    def __init__(self, message: str = "canonical version not found") -> None:
        super().__init__(message, code="canonical_missing")


class OverlayContract(TenantViewError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="overlay_contract")


class PurgeReceipt:
    """Return type for :meth:`TenantViewStore.purge_for_revoked_grant`.

    Purge is idempotent: repeat calls return ``PurgeReceipt(removed=False)``.
    """

    __slots__ = ("removed", "view_key")

    def __init__(self, *, removed: bool, view_key: str) -> None:
        self.removed = removed
        self.view_key = view_key


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


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(payload: Any) -> bytes:
    return _C.canonical_json_bytes(_C.nfc_normalize(payload))


def _open_child_dir_nofollow(parent_fd: int, name: str) -> int:
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
            raise TenantViewError(
                "child is a symlink; refusing to follow", code="contract"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise TenantViewError("child is not a directory", code="contract") from exc
        if exc.errno == errno.ENOENT:
            raise TenantViewError("child does not exist", code="not_found") from exc
        raise TenantViewError(
            f"cannot open child (errno={exc.errno})", code="io"
        ) from exc
    st = os.fstat(fd)
    if not _stat_mod.S_ISDIR(st.st_mode):
        os.close(fd)
        raise TenantViewError("child is not a directory", code="contract")
    return fd


# ---------------------------------------------------------------------------
# Data record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewRecord:
    payload: dict[str, Any]

    @property
    def view_key(self) -> str:
        return self.payload["view_key"]

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def canonical_sha256(self) -> str:
        return self.payload["canonical_sha256"]

    @property
    def source_namespace(self) -> str:
        return self.payload["source_namespace"]

    @property
    def report_id(self) -> str:
        return self.payload["report_id"]

    @property
    def view(self) -> dict[str, Any]:
        return self.payload["view"]

    @property
    def record_revision(self) -> int:
        return int(self.payload["record_revision"])


# ---------------------------------------------------------------------------
# TenantViewStore
# ---------------------------------------------------------------------------


class TenantViewStore:
    """Overlay-only tenant view store.

    Instances are stateless; every method opens its own dir-FDs and
    closes them before returning.
    """

    __slots__ = ("_layout", "_ledger", "_evidence")

    def __init__(
        self,
        layout: _I.InstanceLayout,
        ledger: _AL.AccessLedger,
        shared_store: _SE.SharedEvidenceStore,
    ) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise TenantViewError("layout must be InstanceLayout", code="contract")
        if not isinstance(ledger, _AL.AccessLedger):
            raise TenantViewError("ledger must be AccessLedger", code="contract")
        if not isinstance(shared_store, _SE.SharedEvidenceStore):
            raise TenantViewError(
                "shared_store must be SharedEvidenceStore", code="contract"
            )
        self._layout = layout
        self._ledger = ledger
        self._evidence = shared_store

    # ------------------------------------------------------------------
    # Directory handles
    # ------------------------------------------------------------------

    @contextmanager
    def _views_fd(self, tenant_id: str) -> Iterator[int]:
        _I.validate_tenant_id(tenant_id)
        tenant = self._layout.tenant(tenant_id)
        if not tenant.exists():
            raise TenantViewError("tenant directory missing", code="not_initialized")
        with tenant.child_fd("views") as vfd:
            yield vfd

    # ------------------------------------------------------------------
    # Upsert (writes require a valid grant snapshot AND canonical existence)
    # ------------------------------------------------------------------

    def upsert_overlay(
        self,
        *,
        snapshot: _AC.AgentContextSnapshot,
        view_envelope: dict[str, Any],
    ) -> ViewRecord:
        """Write / replace an overlay for the caller's tenant.

        Preconditions:

        - ``snapshot`` must resolve to a query-eligible grant for the
          same ``(source_namespace, report_id)`` — otherwise the write
          fails closed with :class:`ViewDenied`.
        - The referenced ``canonical_sha256`` must exist in the
          Canonical Evidence Store (verified via
          :meth:`SharedEvidenceStore.read_version`).  If not,
          :class:`CanonicalMissing` is raised.
        - The overlay must pass RT-011 ``validate_tenant_view`` (frozen
          v1 schema) — this rejects any full reply/node body text,
          canonical body copies, credentials, absolute paths, etc.
        - Reply / node overlays are enforced to only expose IDs +
          optional SHA-256 fingerprints (already the schema shape).
        - ``tenant_id`` inside the envelope MUST match the snapshot
          (rejects cross-tenant writes).

        The write is atomic and CAS-guarded by record_revision.
        """

        if not isinstance(snapshot, _AC.AgentContextSnapshot):
            raise TenantViewError(
                "snapshot must be AgentContextSnapshot", code="contract"
            )
        if not isinstance(view_envelope, dict):
            raise TenantViewError("view_envelope must be dict", code="contract")
        # Frozen v1 validation.
        _C.validate_tenant_view(view_envelope)
        # Overlay-vs-tenant binding.
        if view_envelope["tenant_id"] != snapshot.tenant_id:
            raise ViewDenied()
        # Additional overlay contract checks.
        self._reject_full_payload_overlays(view_envelope)

        report_key = view_envelope["report_key"]
        try:
            ns, rid = _C.parse_report_key(report_key)
        except _C.ContractError as exc:
            raise OverlayContract(f"bad report_key: {exc}") from exc
        # Fail closed if not eligible.
        try:
            grant = self._ledger.check_query_eligibility(
                snapshot=snapshot,
                source_namespace=ns,
                report_id=rid,
            )
        except _AL.AccessDenied:
            raise ViewDenied()
        # canonical_sha256 must exist.
        canonical_sha = view_envelope["canonical_sha256"]
        try:
            self._evidence.read_version(report_key, canonical_sha)
        except _SE.SharedEvidenceError as exc:
            if exc.code == "not_found":
                raise CanonicalMissing() from exc
            raise CanonicalMissing(
                f"canonical read failed: {exc.code}"
            ) from exc

        view_key = _AL.compute_grant_key(snapshot.tenant_id, report_key)
        now = _utcnow_iso()
        with self._views_fd(snapshot.tenant_id) as vfd:
            leaf = f"{view_key}{_VIEW_LEAF_SUFFIX}"
            if _A.child_exists(vfd, leaf):
                existing_raw = _A.read_file(vfd, leaf)
                existing_sha = _sha256_hex(existing_raw)
                try:
                    existing_payload = _C.strict_json_loads(existing_raw.decode("utf-8"))
                except (_C.ContractError, ValueError, UnicodeDecodeError) as exc:
                    raise ViewCorruption(
                        f"existing view corrupt: {exc}", view_key=view_key
                    ) from exc
                self._validate_record(existing_payload, view_key=view_key)
                record_revision = existing_payload["record_revision"] + 1
                created_at = existing_payload["created_at"]
            else:
                existing_sha = None
                record_revision = 1
                created_at = now

            new_record = self._wrap_view(
                view_key=view_key,
                tenant_id=snapshot.tenant_id,
                source_namespace=ns,
                report_id=rid,
                canonical_sha256=canonical_sha,
                view=view_envelope,
                record_revision=record_revision,
                created_at=created_at,
                updated_at=now,
            )
            self._validate_record(new_record, view_key=view_key)
            # Second recheck immediately before durable write.
            try:
                self._ledger.check_query_eligibility(
                    snapshot=snapshot,
                    source_namespace=ns,
                    report_id=rid,
                )
            except _AL.AccessDenied:
                raise ViewDenied()
            _A.cas_write(
                vfd,
                leaf,
                _canonical_bytes(new_record),
                expected_previous_sha256=existing_sha,
            )
            # NOTE: mutation ends with a fresh record; caller may treat
            # the returned ViewRecord as authoritative for the snapshot.
        return ViewRecord(payload=new_record)

    # ------------------------------------------------------------------
    # Read (double ACL check)
    # ------------------------------------------------------------------

    def read_view(
        self,
        *,
        snapshot: _AC.AgentContextSnapshot,
        source_namespace: str,
        report_id: str,
    ) -> ViewRecord:
        """Return the overlay for one report, failing closed on any ACL check.

        Both the pre-load and post-load eligibility checks use
        :meth:`AccessLedger.check_query_eligibility`.  An in-flight
        revocation causes the second check to raise, and the view is
        never returned.
        """

        if not isinstance(snapshot, _AC.AgentContextSnapshot):
            raise TenantViewError(
                "snapshot must be AgentContextSnapshot", code="contract"
            )
        # First ACL.
        try:
            self._ledger.check_query_eligibility(
                snapshot=snapshot,
                source_namespace=source_namespace,
                report_id=report_id,
            )
        except _AL.AccessDenied:
            raise ViewDenied()
        report_key = _C.compose_report_key(source_namespace, report_id)
        view_key = _AL.compute_grant_key(snapshot.tenant_id, report_key)
        with self._views_fd(snapshot.tenant_id) as vfd:
            leaf = f"{view_key}{_VIEW_LEAF_SUFFIX}"
            try:
                raw = _A.read_file(vfd, leaf)
            except FileNotFoundError as exc:
                raise ViewNotFound(view_key=view_key) from exc
            except _A.ContainmentError as exc:
                raise ViewCorruption(
                    "view containment failure", view_key=view_key
                ) from exc
        try:
            payload = _C.strict_json_loads(raw.decode("utf-8"))
        except (_C.ContractError, ValueError, UnicodeDecodeError) as exc:
            raise ViewCorruption(
                f"view not strict JSON: {exc}", view_key=view_key
            ) from exc
        self._validate_record(payload, view_key=view_key)
        # Canonical linkage sanity: read_version verifies fully.
        try:
            self._evidence.read_version(report_key, payload["canonical_sha256"])
        except _SE.SharedEvidenceError as exc:
            raise CanonicalMissing(
                f"canonical read failed: {exc.code}"
            ) from exc
        # Second ACL — must pass right before returning evidence.
        try:
            self._ledger.check_query_eligibility(
                snapshot=snapshot,
                source_namespace=source_namespace,
                report_id=report_id,
            )
        except _AL.AccessDenied:
            raise ViewDenied()
        return ViewRecord(payload=payload)

    # ------------------------------------------------------------------
    # Purge (cleanup consumer contract)
    # ------------------------------------------------------------------

    def purge_for_revoked_grant(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        actor: str,
        reason: str,
    ) -> PurgeReceipt:
        """Idempotently remove any overlay for a revoked grant.

        Called by the cleanup consumer registered against the
        ``tenant_view`` outbox entry.  Never re-checks the ledger's
        eligibility (the ledger has already denied the grant); instead
        it verifies that either a tombstone OR a revoked/purge_pending
        grant record is present, so a rogue caller cannot purge an
        active view.
        """

        _AL._validate_actor_reason(actor, reason)
        _I.validate_tenant_id(tenant_id)
        report_key = _C.compose_report_key(source_namespace, report_id)
        view_key = _AL.compute_grant_key(tenant_id, report_key)
        # Verify tombstone OR revoked/purge_pending grant.
        tombstone = self._ledger.read_tombstone(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
        )
        if tombstone is None:
            # Fall back to grant-record status: allow purge iff revoked/
            # purge_pending.  Never allow purge on active/discovered/
            # granted/revalidation_due to prevent accidental erasure.
            try:
                with self._ledger._tenant_fd(tenant_id) as tfd:
                    grant = self._ledger._read_grant_file(tfd, view_key)
            except _AL.GrantNotFound:
                raise TenantViewError(
                    "cannot purge: no tombstone and no grant record",
                    code="purge_refused",
                    view_key=view_key,
                )
            if grant.status not in ("revoked", "purge_pending", "purged"):
                raise TenantViewError(
                    f"cannot purge: grant status {grant.status!r} not revoked",
                    code="purge_refused",
                    view_key=view_key,
                )
        with self._views_fd(tenant_id) as vfd:
            leaf = f"{view_key}{_VIEW_LEAF_SUFFIX}"
            if not _A.child_exists(vfd, leaf):
                return PurgeReceipt(removed=False, view_key=view_key)
            _A.unlink_at(vfd, leaf, missing_ok=True)
            _A.fsync_dir(vfd)
        return PurgeReceipt(removed=True, view_key=view_key)

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Idempotent recovery: sweep tmp-orphan files under each tenant's
        ``views/`` sub-directory.

        Recovery never deletes committed view files.  Returns a redacted
        summary.
        """

        summary = {"tenants_scanned": 0, "orphans_removed": 0}
        with self._layout.child_fd("tenants") as tfd:
            with os.scandir(tfd) as entries:
                tenants = sorted(
                    e.name for e in entries
                    if _C.TENANT_ID_REGEX.match(e.name) and e.is_dir(follow_symlinks=False)
                )
        for tenant_id in tenants:
            summary["tenants_scanned"] += 1
            try:
                with self._views_fd(tenant_id) as vfd:
                    orphans = _A.recover_orphans(vfd)
                    summary["orphans_removed"] += len(orphans)
            except TenantViewError:
                continue
        return summary

    # ------------------------------------------------------------------
    # Internal validators
    # ------------------------------------------------------------------

    def _validate_record(self, payload: Any, *, view_key: str) -> None:
        # Envelope schema.
        _AL._validate_against(_VIEW_RECORD_SCHEMA_ID, payload)
        # Nested frozen v1 view.
        try:
            _C.validate_tenant_view(payload["view"])
        except _C.ContractError as exc:
            raise ViewCorruption(
                f"view.v1 failed: {exc}", view_key=view_key
            ) from exc
        # Envelope <-> view cross-check.
        for field_name in ("tenant_id", "canonical_sha256"):
            if payload[field_name] != payload["view"][field_name]:
                raise ViewCorruption(
                    f"envelope/{field_name} mismatch", view_key=view_key
                )
        # report_key equivalence check.
        report_key = payload["view"]["report_key"]
        try:
            ns, rid = _C.parse_report_key(report_key)
        except _C.ContractError as exc:
            raise ViewCorruption(f"bad report_key: {exc}", view_key=view_key) from exc
        if payload["source_namespace"] != ns or payload["report_id"] != rid:
            raise ViewCorruption(
                "envelope report_key parts mismatch", view_key=view_key
            )
        # view_key must equal H(tenant_id, report_key).
        if _AL.compute_grant_key(payload["tenant_id"], report_key) != view_key:
            raise ViewCorruption(
                "view_key does not match H(tenant, report_key)", view_key=view_key
            )
        # Also enforce the overlay contract (no bodies).
        self._reject_full_payload_overlays(payload["view"])

    def _reject_full_payload_overlays(self, view: dict[str, Any]) -> None:
        # The frozen v1 schema already restricts reply_overlay / node_overlay
        # to IDs + optional SHA-256.  This defensive check catches future
        # bugs where a caller synthesises the envelope in-memory with
        # additionalProperties bypassed via unexpected keys.  We iterate
        # explicitly against RT-011 forbidden field set.
        for name in ("reply_overlay", "node_overlay"):
            for item in view.get(name, []):
                for key in item.keys():
                    if key.lower() in ("body", "content", "text", "raw", "payload", "html"):
                        raise OverlayContract(
                            f"forbidden field {key!r} in {name} item"
                        )
        # Attachment permissions may carry temporary_url; that IS allowed
        # in tenant view but MUST NOT leak elsewhere.  Do NOT scrub here
        # (that would mutate the payload); simply refuse if the URL is
        # not a string when present.
        for att in view.get("attachment_permissions", []):
            if "temporary_url" in att and att["temporary_url"] is not None:
                if not isinstance(att["temporary_url"], str):
                    raise OverlayContract(
                        "temporary_url must be null or string"
                    )

    # ------------------------------------------------------------------
    # Helpers (module-internal)
    # ------------------------------------------------------------------

    def _wrap_view(
        self,
        *,
        view_key: str,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        canonical_sha256: str,
        view: dict[str, Any],
        record_revision: int,
        created_at: str,
        updated_at: str,
    ) -> dict[str, Any]:
        return {
            "schema": "cwk.rt015.tenant_view_record.v1",
            "view_key": view_key,
            "tenant_id": tenant_id,
            "source_namespace": source_namespace,
            "report_id": report_id,
            "canonical_sha256": canonical_sha256,
            "view": view,
            "record_revision": record_revision,
            "created_at": created_at,
            "updated_at": updated_at,
        }


__all__ = [
    "CanonicalMissing",
    "OverlayContract",
    "PurgeReceipt",
    "TenantViewError",
    "TenantViewStore",
    "ViewCorruption",
    "ViewDenied",
    "ViewNotFound",
    "ViewRecord",
]
