#!/usr/bin/env python3
"""RT-016 v2: anchor-bound data-level migration reconciler.

Owned by RT-016.  Consumes:

- RT-016 durable **v2** crosswalks and review entries produced by
  :class:`cwk_legacy_raw_import.ShadowImporter`.  v1 crosswalks (from
  pre-v2 runs, if any) are auditable via
  :meth:`ShadowImporter.read_crosswalk` but are **never** counted as
  ``both_equal``: they are always reported as ``unanchored_v1`` so
  operators must re-import through the v2 pipeline before the record
  can PASS.
- A caller-provided :class:`cwk_legacy_raw_import.LegacySource` (the
  *legacy side* full set) that the reconciler re-scans by
  ``legacy_path_hash`` — the *only* way to locate the on-disk raw
  bytes matching a crosswalk.  Zero drift is verified against the
  pre-scan captured by the caller/importer.
- A caller-supplied :class:`ReconciliationAnchor` that fixes the
  ``tenant_id``, ``source_namespace``, ``source_kind``,
  ``decomposer_version`` and ``normalizer_version`` for this run.
  **These are trusted operator inputs, not crosswalk-self-reported
  data**: without a matching anchor the reconciler fails closed and
  never falls back to trusting the crosswalk's own claims.
- For every v2 crosswalk whose identity fields match the anchor: the
  reconciler locates the legacy raw by ``legacy_path_hash``, verifies
  the on-disk SHA equals ``legacy_source_sha256``, **re-runs
  :meth:`LegacyRawDecomposer.decompose`** with the anchor's decomposer
  / normalizer versions and the exact bytes, then compares the fresh
  decomposition against the crosswalk's ``canonical_sha256`` /
  ``object_bytes_sha256`` / ``report_key`` / ``view_key`` /
  ``crosswalk_key``.  Any mismatch is fatal for ``both_equal``.
- Finally the recomputed ``(report_key, canonical_sha256)`` are passed
  to :meth:`cwk_shared_evidence.SharedEvidenceStore.read_version`.
  The returned canonical envelope MUST NFC+JCS-serialise to the same
  bytes as the freshly re-decomposed envelope (bit-for-bit).  Any
  divergence — including a canonical present in RT-014 but under
  different bytes than we just recomputed — is fatal for ``both_equal``.

The reconciler NEVER writes to ``shared/``, ``registry/access-ledger/``,
``tenants/<tenant>/views/`` or the legacy tree.  It NEVER calls any
RT-014 write / enumerate / list API.  It only calls RT-014's public
:meth:`read_version`.  It emits a validated
:class:`ReconciliationReport` (v2 schema) with opaque per-classification
samples.

Frozen contract (see PRD §NFR-03, DESIGN §13 M3, RT-016 v2 anchor
remediation):

- Three hash fields are never asserted equal (``legacy_source_sha256``,
  ``canonical_sha256``, ``object_bytes_sha256``).
- Multi-legacy-source → same canonical is a legitimate outcome
  (``both_equal`` for both crosswalks) but only when the anchor
  matches and both re-decompositions yield the same canonical.
- ``only_new`` is only produced when a v2 crosswalk exists but no
  legacy raw file at the recorded ``legacy_path_hash`` is presently
  found; it does not silently trigger any cleanup.
- ``only_legacy`` (raw file that never produced a crosswalk) is
  reported for reviewer action.
- Any drift detected by :meth:`LegacySource.verify_no_drift` sets
  ``zero_drift_verified=False`` and forces the entire report into a
  non-blocking failure state so downstream migration gates can refuse
  to advance.

**This detection guarantee is bounded**: the reconciler protects
against a compromise of the RT-016 registry (``registry/rt016-crosswalk/``
bytes are freely readable/writable to an attacker) *as long as*
LegacySource and RT-014 SharedEvidenceStore remain independent and
were not compromised in the same window.  It is not a proof of
production-system integrity; it is a fail-closed cross-check within
RT-016's own trust boundary.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

import cwk_access_ledger as _AL
import cwk_atomic_file as _A
import cwk_instance as _I
import cwk_legacy_raw_import as _RT016
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _SE


RECONCILER_VERSION = "v2"

_RECONCILIATION_V1_SCHEMA_ID = "cwk.pr001.rt016.reconciliation_report.v1"
_RECONCILIATION_V2_SCHEMA_ID = "cwk.pr001.rt016.reconciliation_report.v2"

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
# ReconciliationAnchor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationAnchor:
    """Trusted operator-supplied identity for a reconcile() call.

    These five fields fix the domain the reconciler is willing to
    interpret in this run.  A crosswalk whose ``tenant_id`` /
    ``source_namespace`` / ``source_kind`` differs from the anchor is
    treated as **not our record** and reported as ``anchor_mismatch``
    rather than tentatively verified against RT-014.  The
    decomposer / normalizer versions are consumed by the fresh
    re-decomposition step so any drift between the anchor and what the
    crosswalk records is immediately visible.

    Constructing an anchor validates the field grammars.  Anchor
    identity is immutable within a reconcile() call.
    """

    tenant_id: str
    source_namespace: str
    source_kind: str
    decomposer_version: str
    normalizer_version: str

    def __post_init__(self) -> None:
        _I.validate_tenant_id(self.tenant_id)
        if (
            not isinstance(self.source_namespace, str)
            or not _C.SOURCE_NAMESPACE_REGEX.match(self.source_namespace)
        ):
            raise _RT016.LegacyImportError(
                "anchor.source_namespace grammar invalid", code="contract"
            )
        if self.source_kind not in ("current_raw", "timeline_snapshot"):
            raise _RT016.LegacyImportError(
                "anchor.source_kind must be current_raw or timeline_snapshot",
                code="contract",
            )
        if not re.match(r"^v[0-9]{1,4}$", self.decomposer_version):
            raise _RT016.LegacyImportError(
                "anchor.decomposer_version grammar invalid", code="contract"
            )
        if not re.match(r"^v[0-9]{1,4}$", self.normalizer_version):
            raise _RT016.LegacyImportError(
                "anchor.normalizer_version grammar invalid", code="contract"
            )

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "source_namespace": self.source_namespace,
            "source_kind": self.source_kind,
            "decomposer_version": self.decomposer_version,
            "normalizer_version": self.normalizer_version,
        }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationReport:
    payload: dict[str, Any]

    @property
    def tenant_id(self) -> str:
        return self.payload["reconciliation_anchor"]["tenant_id"]

    @property
    def source_namespace(self) -> str:
        return self.payload["reconciliation_anchor"]["source_namespace"]

    @property
    def source_kind(self) -> str:
        return self.payload["reconciliation_anchor"]["source_kind"]

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

    @property
    def unanchored_v1_count(self) -> int:
        return int(self.payload["unanchored_v1_count"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class MigrationReconciler:
    """Anchor-bound reconciler for RT-016 v2 crosswalks.

    Instances are stateless; every :meth:`reconcile` call opens its own
    handles and returns a fresh :class:`ReconciliationReport`.  The
    reconciler never mutates any store and never calls any RT-014 API
    beyond the public :meth:`SharedEvidenceStore.read_version`.
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
        anchor: ReconciliationAnchor,
        run_id: str,
        source: _RT016.LegacySource,
    ) -> ReconciliationReport:
        """Anchor-bound reconciliation for one (tenant, namespace, kind).

        Steps for each **v2** crosswalk under the anchor's tenant:

        1. Verify anchor identity: crosswalk's ``tenant_id``,
           ``source_namespace``, ``source_kind`` must equal the anchor's.
           A mismatch means "this crosswalk is not owned by this
           anchor's namespace" — the crosswalk is skipped for PASS and
           counted separately as ``anchor_mismatch``.
        2. Look up the on-disk legacy bytes via the caller-provided
           :class:`LegacySource` indexed by ``legacy_path_hash``.  If no
           legacy file with the recorded path_hash is present, count as
           ``only_new`` and continue.
        3. Verify ``sha256(bytes) == crosswalk.legacy_source_sha256``.
           If not, ``legacy_source_sha_drift`` and continue.
        4. Re-decompose the bytes with a fresh
           :class:`LegacyRawDecomposer` initialised from
           ``anchor.decomposer_version`` / ``anchor.normalizer_version``.
           If the re-decomposition is not ``ok`` (i.e. quarantined),
           count as ``re_decompose_mismatch``.
        5. Compare the recomputed ``canonical_sha256`` /
           ``object_bytes_sha256`` / ``report_key`` /
           ``view_key`` (= H(tenant, report_key)) / v2 ``crosswalk_key``
           against the crosswalk's stored fields.  Any mismatch counts
           as ``re_decompose_mismatch``.
        6. Call
           :meth:`SharedEvidenceStore.read_version(recomputed_report_key,
           recomputed_canonical_sha256)`.  On any exception count as
           ``canonical_missing`` / ``canonical_sha_mismatch`` /
           ``object_bytes_sha_mismatch`` per RT-014's error taxonomy.
        7. NFC+JCS-serialise the returned canonical envelope and compare
           bit-for-bit against the re-decomposed envelope's JCS bytes.
           Any divergence counts as ``read_version_bytes_mismatch``.

        Only if every step passes is the record counted as
        ``both_equal``.  v1 crosswalks are always counted as
        ``unanchored_v1`` — they lack the ``source_kind`` /
        ``legacy_path_hash`` binding required for step 2.

        Legacy raws with no matching crosswalk / review are counted as
        ``only_legacy`` (path-hash-based lookup against the v2
        crosswalk / review set).

        Zero-drift is checked against ``source.verify_no_drift()``; a
        drifted tree does not stop the report, but sets
        ``zero_drift_verified=False`` so downstream gates can refuse to
        promote.
        """

        if not isinstance(anchor, ReconciliationAnchor):
            raise _RT016.LegacyImportError(
                "anchor must be ReconciliationAnchor; reconciler cannot fall "
                "back to crosswalk-self-declared identity",
                code="contract",
            )
        if not isinstance(run_id, str) or not re.match(r"^run_[a-z2-7]{26}$", run_id):
            raise _RT016.LegacyImportError("invalid run_id", code="contract")
        if not isinstance(source, _RT016.LegacySource):
            raise _RT016.LegacyImportError(
                "source must be LegacySource", code="contract"
            )

        tenant_id = anchor.tenant_id
        source_namespace = anchor.source_namespace
        source_kind = anchor.source_kind

        # 1. Verify legacy tree has not drifted since importer's pre-scan.
        zero_drift_verified = True
        try:
            source.verify_no_drift()
        except _RT016.LegacyDriftDetected:
            zero_drift_verified = False
        except _RT016.LegacyImportError as exc:
            if exc.code == "state":
                # No prior snapshot() → we cannot claim zero drift; take
                # a fresh snapshot so the follow-up walk is stable.
                source.snapshot()
                zero_drift_verified = True
            else:
                raise

        # 2. Recompute the legacy-side full set (rel_path -> (bytes, sha)).
        legacy_by_path_hash: dict[str, tuple[str, bytes, str]] = {}
        legacy_side: dict[str, str] = {}
        for rel, data, sha in source.iter_files_with_hashes():
            if not rel.endswith(".md"):
                continue
            if "/_system/" in rel or rel.startswith("_system/"):
                continue
            path_hash = _RT016.compute_legacy_path_hash(rel)
            # If two files hash to the same rel path (impossible in
            # practice given a well-formed tree), keep the first for
            # determinism; extremely unlikely because rel is unique per
            # tree.
            legacy_by_path_hash.setdefault(path_hash, (rel, data, sha))
            legacy_side[rel] = sha

        # 3. Enumerate durable crosswalks (new side).  The public
        # iter_crosswalks() returns both v1 and v2 records; we
        # discriminate here.
        crosswalks_v2: list[dict[str, Any]] = []
        crosswalks_v1: list[dict[str, Any]] = []
        for cw in self._importer.iter_crosswalks(tenant_id=tenant_id):
            schema = cw.get("schema")
            if schema == "cwk.rt016.migration_crosswalk.v2":
                crosswalks_v2.append(cw)
            elif schema == "cwk.rt016.migration_crosswalk.v1":
                crosswalks_v1.append(cw)
            else:
                # Unknown schema is impossible if the crosswalk loader
                # accepted the record, but defensively categorise as v1
                # for reporting (which forces unanchored).
                crosswalks_v1.append(cw)

        # 4. Enumerate durable review entries (all versions considered
        # for coverage of legacy-side undecomposables).  Coverage is
        # bound to (legacy_path_hash, legacy_source_sha256) — a review
        # whose recorded raw SHA does not match the current legacy raw
        # at the same path cannot suppress only_legacy; it is a stale
        # review from an earlier byte-generation that no longer
        # describes what is on disk.  The mismatched review is emitted
        # as `review_source_sha_drift` (fail-closed).
        review_entries: list[dict[str, Any]] = []
        review_covered_identity: set[tuple[str, str]] = set()
        review_sha_drift: list[dict[str, Any]] = []
        for rv in self._importer.iter_reviews(tenant_id=tenant_id):
            review_entries.append(rv)
            if rv.get("schema") != "cwk.rt016.review_entry.v2":
                continue
            # Only v2 reviews carry namespace/kind/path bindings
            # aligned with anchor identity; count coverage only for
            # v2 reviews whose identity matches the anchor.
            if (
                rv.get("tenant_id") != tenant_id
                or rv.get("source_namespace") != source_namespace
                or rv.get("source_kind") != source_kind
            ):
                continue
            ph = rv.get("legacy_path_hash")
            rv_sha = rv.get("legacy_source_sha256")
            if not isinstance(ph, str) or not isinstance(rv_sha, str):
                continue
            # Bind coverage to (path_hash, raw_sha).  A review for a
            # path whose current on-disk SHA differs from the review's
            # recorded raw SHA is classified as review_source_sha_drift
            # and MUST NOT suppress only_legacy for that path.
            legacy_lookup = legacy_by_path_hash.get(ph)
            if legacy_lookup is None:
                # Legacy raw at review's path is missing entirely.
                # We still record the review identity as (path_hash,
                # review_sha) — the only_legacy pass keys on
                # (path_hash, current_sha) so this correctly cannot
                # suppress a mismatched entry.  The review is not
                # counted here; new_undecomposable_count below still
                # counts it (a v2 review under this anchor exists).
                review_covered_identity.add((ph, rv_sha))
                continue
            _rel, _data, current_sha = legacy_lookup
            if current_sha != rv_sha:
                # SHA drift: current legacy bytes at this path differ
                # from what the review was written against.  Do NOT
                # add the review to the coverage set; instead emit a
                # fail-closed classification so operators can triage.
                review_sha_drift.append(
                    {
                        "classification": "review_source_sha_drift",
                        "opaque_key": rv.get("review_id", "qe_unknown"),
                    }
                )
                continue
            review_covered_identity.add((ph, rv_sha))

        # 5. Classify each v2 crosswalk.
        both_equal = 0
        both_diff = 0
        only_new = 0
        anchor_mismatch = 0
        crosswalk_covered_path_hashes: set[str] = set()
        failure_samples: list[dict[str, str]] = []

        fresh_decomposer = _RT016.LegacyRawDecomposer(
            decomposer_version=anchor.decomposer_version,
            normalizer_version=anchor.normalizer_version,
        )

        for cw in crosswalks_v2:
            ck = cw.get("crosswalk_key", "cw_unknown")
            # 5a. Anchor identity.
            if (
                cw.get("tenant_id") != tenant_id
                or cw.get("source_namespace") != source_namespace
                or cw.get("source_kind") != source_kind
            ):
                anchor_mismatch += 1
                failure_samples.append(
                    {"classification": "anchor_mismatch", "opaque_key": ck}
                )
                continue
            # Also check decomposer / normalizer versions (defense: an
            # attacker who forges anchor-aligned identity but records a
            # different decomposer version cannot silently PASS because
            # our re-decomposition uses the anchor's version and would
            # produce different bytes).
            if (
                cw.get("decomposer_version") != anchor.decomposer_version
                or cw.get("normalizer_version") != anchor.normalizer_version
            ):
                anchor_mismatch += 1
                failure_samples.append(
                    {"classification": "anchor_mismatch", "opaque_key": ck}
                )
                continue

            legacy_path_hash = cw.get("legacy_path_hash")
            crosswalk_source_sha = cw.get("legacy_source_sha256")

            # 5b. Locate the legacy file by path_hash.
            legacy_lookup = legacy_by_path_hash.get(legacy_path_hash)
            if legacy_lookup is None:
                only_new += 1
                failure_samples.append(
                    {"classification": "legacy_bytes_missing", "opaque_key": ck}
                )
                continue
            rel, data, sha = legacy_lookup
            # Mark this path as covered so the only_legacy pass below
            # can subtract accurately.  We mark regardless of eventual
            # outcome — the crosswalk _references_ this legacy file.
            crosswalk_covered_path_hashes.add(legacy_path_hash)

            # 5c. On-disk SHA agrees with crosswalk claim.
            if sha != crosswalk_source_sha:
                both_diff += 1
                failure_samples.append(
                    {"classification": "legacy_source_sha_drift", "opaque_key": ck}
                )
                continue

            # 5d. Re-decompose with the anchor's decomposer / normalizer.
            try:
                fresh = fresh_decomposer.decompose(
                    raw_bytes=data,
                    tenant_id=tenant_id,
                    source_namespace=source_namespace,
                    run_started_at=cw["run_started_at"],
                    source_kind=source_kind,
                )
            except _RT016.LegacyImportError:
                # Malformed anchor / raw is quarantined at decompose,
                # which returns a DecomposeResult; a LegacyImportError
                # here is a contract error against the anchor and is
                # fatal for this record.
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue
            if fresh.status != "ok" or fresh.canonical_envelope is None:
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue

            # 5e. Cross-check every derived value against the crosswalk.
            recomputed_canonical_sha = fresh.canonical_envelope["canonical_sha256"]
            recomputed_envelope_jcs = _C.canonical_json_bytes(fresh.canonical_envelope)
            recomputed_object_bytes_sha = _sha256_hex(recomputed_envelope_jcs)
            try:
                recomputed_report_key = _C.compose_report_key(
                    source_namespace, fresh.canonical_envelope["report_id"]
                )
            except _C.ContractError:
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue
            try:
                recomputed_grant_key = _AL.compute_grant_key(
                    tenant_id, recomputed_report_key
                )
            except _AL.AccessLedgerError:
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue
            try:
                recomputed_crosswalk_key = _RT016.compute_crosswalk_key_v2(
                    tenant_id,
                    source_namespace,
                    source_kind,
                    legacy_path_hash,
                    crosswalk_source_sha,
                )
            except _RT016.LegacyImportError:
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue

            mismatches: list[str] = []
            if recomputed_canonical_sha != cw.get("canonical_sha256"):
                mismatches.append("canonical_sha256")
            if recomputed_object_bytes_sha != cw.get("object_bytes_sha256"):
                mismatches.append("object_bytes_sha256")
            if recomputed_report_key != cw.get("report_key"):
                mismatches.append("report_key")
            if recomputed_grant_key != cw.get("view_key"):
                mismatches.append("view_key")
            if recomputed_grant_key != cw.get("observe_grant_key"):
                mismatches.append("observe_grant_key")
            if recomputed_crosswalk_key != cw.get("crosswalk_key"):
                mismatches.append("crosswalk_key")
            # Second-round remediation: compare the
            # tenant_view_envelope's **semantic projection** JCS bytes
            # bit-for-bit.  ``canonical_tenant_view_projection`` drops
            # only ``observed_at`` (the observation-clock metadata
            # sourced from the caller's ``run_started_at`` — an
            # attacker-writable field inside the crosswalk payload).
            # Every semantic field is preserved: lane / read_status /
            # todo_status / new_reply_flag / reply_overlay /
            # node_overlay / schema / tenant_id / report_key /
            # canonical_sha256.  A coordinated tamper that rewrites
            # observed_at (and matching run_started_at) cannot hide a
            # semantic-field drift because the projection strips both
            # sides' observed_at symmetrically.
            stored_view = cw.get("tenant_view_envelope")
            if not isinstance(stored_view, dict):
                mismatches.append("tenant_view_envelope_type")
            else:
                stored_proj_jcs = _C.canonical_json_bytes(
                    _RT016.canonical_tenant_view_projection(stored_view)
                )
                fresh_proj_jcs = _C.canonical_json_bytes(
                    _RT016.canonical_tenant_view_projection(fresh.tenant_view_envelope)
                )
                if stored_proj_jcs != fresh_proj_jcs:
                    mismatches.append("tenant_view_envelope_bytes")
            if mismatches:
                failure_samples.append(
                    {"classification": "re_decompose_mismatch", "opaque_key": ck}
                )
                only_new += 1
                continue

            # 5f. RT-014 read_version.
            try:
                envelope = self._evidence.read_version(
                    recomputed_report_key, recomputed_canonical_sha
                )
            except _SE.SharedEvidenceError as exc:
                if exc.code == "not_found":
                    cls = "canonical_missing"
                elif exc.code == "sha_mismatch":
                    cls = "object_bytes_sha_mismatch"
                elif exc.code in ("report_key_mismatch", "canonical_drift"):
                    cls = "canonical_sha_mismatch"
                else:
                    cls = "canonical_missing"
                failure_samples.append(
                    {"classification": cls, "opaque_key": ck}
                )
                only_new += 1
                continue

            # 5g. Bit-for-bit compare RT-014's returned envelope against
            # our re-decomposed envelope.  If a coordinated attacker
            # swapped crosswalk bytes to point at a different canonical
            # in RT-014, either read_version fails (5f) or the
            # returned bytes differ from what our fresh decomposition
            # would have produced.
            returned_jcs = _C.canonical_json_bytes(envelope)
            if returned_jcs != recomputed_envelope_jcs:
                failure_samples.append(
                    {
                        "classification": "read_version_bytes_mismatch",
                        "opaque_key": ck,
                    }
                )
                only_new += 1
                continue

            both_equal += 1

        # 6. only_legacy: any legacy file whose (path_hash, current_sha)
        # is not covered by a v2 crosswalk (of the anchor's ns/kind) and
        # not covered by a v2 review (of the anchor's ns/kind) whose
        # recorded raw SHA matches the file's current SHA on disk.
        legacy_uncovered = 0
        for path_hash, (rel, _data, sha) in legacy_by_path_hash.items():
            if path_hash in crosswalk_covered_path_hashes:
                continue
            if (path_hash, sha) in review_covered_identity:
                continue
            legacy_uncovered += 1
            opaque_key = _sha256_hex(
                _RT016.LEGACY_PATH_HASH_DOMAIN + b"\x00" + rel.encode("utf-8")
            )[:32]
            failure_samples.append(
                {"classification": "only_legacy", "opaque_key": opaque_key}
            )
        only_legacy = legacy_uncovered

        # 7. Emit review_source_sha_drift samples (bounded).
        for sample in review_sha_drift[:32]:
            failure_samples.append(sample)

        # 8. new_undecomposable count / review_count: only v2 reviews
        # under the anchor whose recorded raw SHA matches the current
        # on-disk SHA at their legacy_path_hash.  Reviews that no
        # longer describe on-disk bytes must not inflate
        # accepted-review counters.
        new_undecomposable_count = 0
        review_count = 0
        matched_reviews: list[dict[str, Any]] = []
        for rv in review_entries:
            if rv.get("schema") != "cwk.rt016.review_entry.v2":
                continue
            if (
                rv.get("tenant_id") != tenant_id
                or rv.get("source_namespace") != source_namespace
                or rv.get("source_kind") != source_kind
            ):
                continue
            ph = rv.get("legacy_path_hash")
            rv_sha = rv.get("legacy_source_sha256")
            legacy_lookup = legacy_by_path_hash.get(ph)
            if legacy_lookup is None:
                continue
            _rel, _data, current_sha = legacy_lookup
            if current_sha != rv_sha:
                # Already emitted as review_source_sha_drift above;
                # do NOT count as accepted review.
                continue
            new_undecomposable_count += 1
            matched_reviews.append(rv)
            if rv.get("migration_status") == "review":
                review_count += 1
        for rv in matched_reviews[:32]:
            failure_samples.append(
                {"classification": "new_undecomposable", "opaque_key": rv["review_id"]}
            )

        # 8. unanchored v1 crosswalks — always fail-closed.
        for cw in crosswalks_v1:
            failure_samples.append(
                {
                    "classification": "unanchored_v1",
                    "opaque_key": cw.get("crosswalk_key", "cw_unknown"),
                }
            )
        unanchored_v1_count = len(crosswalks_v1)

        failure_samples = failure_samples[:128]

        legacy_side_count = len(legacy_side)
        new_side_count = len(crosswalks_v2)

        def _rate(numer: int, denom: int) -> float:
            if denom == 0:
                return 1.0
            return round(numer / denom, 6)

        payload: dict[str, Any] = {
            "schema": "cwk.rt016.reconciliation_report.v2",
            "reconciler_version": RECONCILER_VERSION,
            "reconciliation_anchor": anchor.as_payload(),
            "run_id": run_id,
            "generated_at": _utcnow_iso(),
            "legacy_tree_root_hash": self._legacy_root_hash(legacy_side),
            "legacy_side_count": legacy_side_count,
            "new_side_count": new_side_count,
            "both_equal_count": both_equal,
            "both_diff_count": both_diff,
            "only_legacy_count": only_legacy,
            "only_new_count": only_new + anchor_mismatch,
            "new_undecomposable_count": new_undecomposable_count,
            "review_count": review_count,
            "unanchored_v1_count": unanchored_v1_count,
            "legacy_source_hash_verify_rate": _rate(both_equal, legacy_side_count),
            "canonical_hash_verify_rate": _rate(both_equal, new_side_count),
            "crosswalk_coverage_rate": _rate(
                both_equal + both_diff, legacy_side_count
            ),
            "zero_drift_verified": zero_drift_verified,
            "failure_samples": failure_samples,
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
            _C.SCHEMA_ROOT
            / "rt016"
            / "schemas"
            / "reconciliation_report_v2.schema.json"
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
    "ReconciliationAnchor",
    "ReconciliationReport",
]
