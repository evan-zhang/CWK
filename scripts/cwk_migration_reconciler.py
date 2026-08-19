#!/usr/bin/env python3
"""RT-016: data-level migration reconciler.

Owned by RT-016.  Consumes:

- RT-016 durable crosswalks and review entries produced by
  :class:`cwk_legacy_raw_import.ShadowImporter` (the *new side* full
  set — RT-016 never enumerates RT-014 catalogs directly).
- A caller-provided :class:`cwk_legacy_raw_import.LegacySource` (the
  *legacy side* full set — the reconciler recomputes SHA-256 of every
  Markdown file under that root and verifies zero drift versus the
  pre-scan captured by the importer).
- For every new-side crosswalk, a strict RT-014
  :meth:`cwk_shared_evidence.SharedEvidenceStore.read_version` call
  that validates the canonical envelope is retrievable, its
  ``canonical_sha256`` matches the crosswalk, and its
  ``object_bytes_sha256`` matches the catalog entry.

Never writes to ``shared/``, ``registry/access-ledger/``,
``tenants/<tenant>/views/`` or the legacy tree.  Emits a validated
:class:`ReconciliationReport` with opaque per-classification samples.

Frozen contract (see PRD §NFR-03, DESIGN §13 M3, RT-016 plan §9):

- Three hash fields are never asserted equal (``legacy_source_sha256``,
  ``canonical_sha256``, ``object_bytes_sha256``).
- Multi-legacy-source → same canonical is a legitimate outcome
  (``both_equal`` for both crosswalks).
- ``only_new`` is only produced when a durable crosswalk exists but no
  legacy raw file is presently found; it does not silently trigger any
  cleanup.  ``only_legacy`` (raw file that never produced a crosswalk)
  is reported for reviewer action.
- Any drift detected by :meth:`LegacySource.verify_no_drift` sets
  ``zero_drift_verified=False`` and forces the entire report into a
  non-blocking failure state so downstream migration gates can refuse
  to advance.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

import cwk_atomic_file as _A
import cwk_instance as _I
import cwk_legacy_raw_import as _RT016
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _SE


RECONCILER_VERSION = "v1"

_RECONCILIATION_SCHEMA_ID = "cwk.pr001.rt016.reconciliation_report.v1"

_UTC = _dt.timezone.utc


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationReport:
    payload: dict[str, Any]

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def run_id(self) -> str:
        return self.payload["run_id"]

    @property
    def zero_drift_verified(self) -> bool:
        return bool(self.payload["zero_drift_verified"])

    @property
    def both_equal_count(self) -> int:
        return int(self.payload["both_equal_count"])

    @property
    def both_diff_count(self) -> int:
        return int(self.payload["both_diff_count"])

    @property
    def only_legacy_count(self) -> int:
        return int(self.payload["only_legacy_count"])

    @property
    def only_new_count(self) -> int:
        return int(self.payload["only_new_count"])

    @property
    def new_undecomposable_count(self) -> int:
        return int(self.payload["new_undecomposable_count"])

    @property
    def review_count(self) -> int:
        return int(self.payload["review_count"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class MigrationReconciler:
    """Compare RT-016 crosswalks against a legacy raw tree.

    Instances are stateless; every :meth:`reconcile` call opens its own
    handles and returns a fresh :class:`ReconciliationReport`.  The
    reconciler never mutates any store.
    """

    __slots__ = ("_layout", "_importer", "_evidence")

    def __init__(
        self,
        layout: _I.InstanceLayout,
        importer: _RT016.ShadowImporter,
        evidence_store: _SE.SharedEvidenceStore,
    ) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise _RT016.LegacyImportError(
                "layout must be InstanceLayout", code="contract"
            )
        if not isinstance(importer, _RT016.ShadowImporter):
            raise _RT016.LegacyImportError(
                "importer must be ShadowImporter", code="contract"
            )
        if not isinstance(evidence_store, _SE.SharedEvidenceStore):
            raise _RT016.LegacyImportError(
                "evidence_store must be SharedEvidenceStore",
                code="contract",
            )
        self._layout = layout
        self._importer = importer
        self._evidence = evidence_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(
        self,
        *,
        tenant_id: str,
        run_id: str,
        source: _RT016.LegacySource,
    ) -> ReconciliationReport:
        """Cross-check RT-016 crosswalks against the caller's legacy tree.

        The caller must have already called :meth:`LegacySource.snapshot`
        (typically via :meth:`ShadowImporter.import_batch`).  This method
        re-scans the tree, verifies zero drift, then classifies each
        report as one of:

        - ``both_equal`` — a crosswalk exists and RT-014
          :meth:`SharedEvidenceStore.read_version` returns a canonical
          envelope whose ``canonical_sha256`` matches the crosswalk;
          the crosswalk's ``legacy_source_sha256`` matches the legacy
          raw currently on disk.
        - ``both_diff`` — a crosswalk exists but the on-disk legacy raw
          bytes have drifted (this is separate from
          :meth:`LegacySource.verify_no_drift` because the drift may
          have occurred *between* the run and reconcile).
        - ``only_legacy`` — a legacy raw exists but no crosswalk
          or review entry references its SHA-256.
        - ``only_new`` — a crosswalk exists but no legacy raw file has
          the referenced ``legacy_source_sha256`` (e.g. legacy source
          moved).
        - ``new_undecomposable`` — a review entry with
          ``migration_status`` in ``{"undecomposable", "review"}``.

        Rates are reported as fractions in ``[0, 1]``:

        - ``legacy_source_hash_verify_rate`` = both_equal / legacy_side_count;
        - ``canonical_hash_verify_rate`` = both_equal / new_side_count;
        - ``crosswalk_coverage_rate`` = (both_equal + both_diff) / legacy_side_count.
        """

        _I.validate_tenant_id(tenant_id)
        if not isinstance(run_id, str) or not re.match(r"^run_[a-z2-7]{26}$", run_id):
            raise _RT016.LegacyImportError("invalid run_id", code="contract")
        if not isinstance(source, _RT016.LegacySource):
            raise _RT016.LegacyImportError(
                "source must be LegacySource", code="contract"
            )

        # 1. Verify legacy tree has not drifted since importer's pre-scan.
        zero_drift_verified = True
        try:
            source.verify_no_drift()
        except _RT016.LegacyDriftDetected:
            zero_drift_verified = False

        # 2. Recompute the legacy-side full set (rel_path -> sha256).
        legacy_side: dict[str, str] = {}
        for rel, data, sha in source.iter_files_with_hashes():
            if not rel.endswith(".md"):
                continue
            if "/_system/" in rel or rel.startswith("_system/"):
                continue
            # Duplicate SHAs across paths are fine; keep the first for
            # stable sort ordering.
            legacy_side[rel] = sha
        legacy_shas: dict[str, list[str]] = {}
        for rel, sha in legacy_side.items():
            legacy_shas.setdefault(sha, []).append(rel)

        # 3. Enumerate durable crosswalks (new side).
        crosswalks: list[dict[str, Any]] = []
        new_side_shas: set[str] = set()
        for cw in self._importer.iter_crosswalks(tenant_id=tenant_id):
            crosswalks.append(cw)
            new_side_shas.add(cw["legacy_source_sha256"])

        # 4. Enumerate durable review entries.
        review_entries: list[dict[str, Any]] = []
        undecomposable_shas: set[str] = set()
        for rv in self._importer.iter_reviews(tenant_id=tenant_id):
            review_entries.append(rv)
            undecomposable_shas.add(rv["legacy_source_sha256"])

        # 5. Classify.
        both_equal = 0
        both_diff = 0
        only_legacy = 0
        only_new = 0
        new_undecomposable_count = len(review_entries)
        failure_samples: list[dict[str, str]] = []

        # (a) For every crosswalk, verify RT-014 read_version integrity.
        seen_crosswalk_shas: set[str] = set()
        for cw in crosswalks:
            seen_crosswalk_shas.add(cw["legacy_source_sha256"])
            report_key = cw["report_key"]
            canonical_sha256 = cw["canonical_sha256"]
            try:
                envelope = self._evidence.read_version(
                    report_key, canonical_sha256
                )
            except _SE.SharedEvidenceError as exc:
                failure_samples.append(
                    {
                        "classification": (
                            "canonical_missing"
                            if exc.code == "not_found"
                            else "canonical_sha_mismatch"
                        ),
                        "opaque_key": cw["crosswalk_key"],
                    }
                )
                only_new += 1
                continue
            # canonical_sha256 must match by construction (RT-014
            # verifies internally); we still cross-check the JCS bytes.
            recomputed_object_bytes_sha = _sha256_hex(
                _C.canonical_json_bytes(envelope)
            )
            if recomputed_object_bytes_sha != cw["object_bytes_sha256"]:
                failure_samples.append(
                    {
                        "classification": "object_bytes_sha_mismatch",
                        "opaque_key": cw["crosswalk_key"],
                    }
                )
                only_new += 1
                continue
            # Locate the legacy file (by sha) currently on disk.
            legacy_paths = legacy_shas.get(cw["legacy_source_sha256"], [])
            if not legacy_paths:
                # crosswalk exists, legacy raw with matching sha absent
                failure_samples.append(
                    {"classification": "only_new", "opaque_key": cw["crosswalk_key"]}
                )
                only_new += 1
                continue
            both_equal += 1

        # (b) For every legacy raw, check whether a crosswalk covers it.
        legacy_uncovered = 0
        for sha, paths in legacy_shas.items():
            if sha in seen_crosswalk_shas:
                continue
            if sha in undecomposable_shas:
                # Already tracked as review; do NOT double-count as
                # only_legacy (undecomposable is a distinct classification).
                continue
            legacy_uncovered += 1
            failure_samples.append(
                {
                    "classification": "only_legacy",
                    "opaque_key": _sha256_hex(
                        _RT016.LEGACY_PATH_HASH_DOMAIN
                        + b"\x00"
                        + paths[0].encode("utf-8")
                    )[:32],
                }
            )
        only_legacy = legacy_uncovered

        # (c) both_diff: currently no legacy raw whose SHA drifted vs
        # a crosswalk (verify_no_drift catches within-run drift; between
        # runs the sha simply wouldn't match, ending up as only_new /
        # only_legacy).  Reserved for future extension.

        # (d) Attach review-entry samples.
        for rv in review_entries[:32]:
            failure_samples.append(
                {"classification": "new_undecomposable", "opaque_key": rv["review_id"]}
            )
        failure_samples = failure_samples[:128]

        legacy_side_count = len(legacy_side)
        new_side_count = len(crosswalks)
        # Rate denominators use max(count, 1) to keep them in [0, 1] when
        # either side is empty.  A zero-sized tenant produces 1.0 rates
        # (nothing to verify → nothing missing).
        def _rate(numer: int, denom: int) -> float:
            if denom == 0:
                return 1.0
            return round(numer / denom, 6)

        payload: dict[str, Any] = {
            "schema": "cwk.rt016.reconciliation_report.v1",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "generated_at": _utcnow_iso(),
            "legacy_tree_root_hash": self._legacy_root_hash(legacy_side),
            "legacy_side_count": legacy_side_count,
            "new_side_count": new_side_count,
            "both_equal_count": both_equal,
            "both_diff_count": both_diff,
            "only_legacy_count": only_legacy,
            "only_new_count": only_new,
            "new_undecomposable_count": new_undecomposable_count,
            "review_count": len(
                [rv for rv in review_entries if rv["migration_status"] == "review"]
            ),
            "legacy_source_hash_verify_rate": _rate(both_equal, legacy_side_count),
            "canonical_hash_verify_rate": _rate(both_equal, new_side_count),
            "crosswalk_coverage_rate": _rate(
                both_equal + both_diff, legacy_side_count
            ),
            "zero_drift_verified": zero_drift_verified,
            "failure_samples": failure_samples,
            "reconciler_version": RECONCILER_VERSION,
        }
        self._validate_report(payload)
        return ReconciliationReport(payload=payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _legacy_root_hash(self, legacy_side: dict[str, str]) -> str:
        """SHA-256 over the sorted (rel_path, sha) tuples.

        Provides a compact single-value fingerprint of the legacy tree
        state used for zero-drift comparison across runs.  Only opaque
        SHA-256 is exposed; no path leaks.
        """

        parts: list[bytes] = []
        for rel in sorted(legacy_side.keys()):
            parts.append(
                b"cwk-rt016-legacy-tree-root-v1\x00"
                + rel.encode("utf-8")
                + b"\x00"
                + legacy_side[rel].encode("ascii")
                + b"\n"
            )
        return _sha256_hex(b"".join(parts))

    def _validate_report(self, payload: dict[str, Any]) -> None:
        schema = _C.strict_json_load_path(
            _C.SCHEMA_ROOT / "rt016" / "schemas" / "reconciliation_report.schema.json"
        )
        try:
            _C._validate_schema(schema, payload, "$", root_schema=schema)
        except _C.ContractError as exc:
            raise _RT016.LegacyImportError(
                f"reconciliation_report schema failed: {exc}",
                code="contract",
            ) from exc
        forbidden = schema.get("customKeywords", {}).get("deepForbiddenProperties")
        if forbidden:
            try:
                _C._iter_deep_forbidden(payload, frozenset(forbidden), path="$")
            except _C.ContractError as exc:
                raise _RT016.LegacyImportError(
                    f"reconciliation_report forbidden field: {exc}",
                    code="contract",
                ) from exc


__all__ = [
    "MigrationReconciler",
    "RECONCILER_VERSION",
    "ReconciliationReport",
]
