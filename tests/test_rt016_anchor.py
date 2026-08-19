"""RT-016 v2 anchor-remediation black-box tests.

Closes the coordinated-crosswalk-tampering Major and related follow-ups
from the acceptance report on commit ``a2789ef``:

- ReconciliationAnchor is mandatory; missing / mismatched anchor fails
  closed and never falls back to trusting crosswalk-self-reported
  identity.
- v2 identity binds ``(tenant_id, source_namespace, source_kind,
  legacy_path_hash, legacy_source_sha256)``.  Same raw bytes across
  different namespaces / paths / kinds produce independent v2
  crosswalks / reviews / manifest lines.
- The bound reader refuses any crosswalk whose file name / parent
  tenant / declared namespace / source_kind / path_hash / raw_sha
  disagree with the caller's expectations, whether the entry point is
  the importer's finder or a CAS conflict branch.
- Coordinated tampering — where an attacker replaces one crosswalk's
  entire byte contents with a self-consistent payload borrowed from a
  different legacy raw — is caught by the reconciler's re-decompose +
  read_version pipeline: the fresh decomposition and RT-014's returned
  canonical envelope are compared bit-for-bit against the crosswalk's
  claims.
- Reconciler consumes RT-014 only through the public
  :meth:`SharedEvidenceStore.read_version` API — no private-directory
  enumeration, no backdoor calls.
- v1 records remain readable via :meth:`read_crosswalk` for audit but
  are never idempotency hits for a v2 importer and never contribute to
  the reconciler's ``both_equal`` count (always reported as
  ``unanchored_v1``).
- Manifest entries carry namespace / source_kind / path_hash
  identity; dedupe is v2-aware.
- Zero-drift enforcement across RT-011~015 frozen files, legacy
  fixtures, and no secret leakage in owned files.

The detection guarantee only holds if the RT-016 registry is compromised
while LegacySource and RT-014 SharedEvidenceStore remain independent
and were not compromised in the same window.  These tests do not make
production/real-system claims.

All fixtures are synthetic (mktemp instance root, in-memory RT-015
fake authority); no real ``CWORK_APP_KEY`` / legacy raw / DocDB /
Cloud / cron / Gateway / workflow collaboration API is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    AF,
    AL,
    C,
    MR,
    R16,
    RT016TestBase,
    build_legacy_tree,
    default_anchor,
    sample_raw,
    utc_iso,
)


def _rewrite_bytes(path: Path, mutator) -> None:
    """Rewrite ``path`` as strict NFC + JCS after applying ``mutator``.

    Produces a byte-for-byte legitimate JCS payload so the loader's
    byte round-trip check would happily accept the file — every check
    that fires must be a semantic / integrity binding, not a
    parser-level detection.
    """

    payload = json.loads(path.read_bytes().decode("utf-8"))
    mutator(payload)
    path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))


def _crosswalk_path(fx, tenant_id: str, crosswalk_key: str) -> Path:
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "crosswalks"
        / f"{crosswalk_key}.json"
    )


# ---------------------------------------------------------------------------
# v2 identity & manifest namespace/path coverage
# ---------------------------------------------------------------------------


class V2IdentityIsolationTests(RT016TestBase):
    def test_same_raw_different_path_yields_two_v2_crosswalks(self):
        """Same raw bytes at two different legacy paths → two v2 crosswalks.

        v2 identity binds legacy_path_hash so the path is part of the
        crosswalk identity, not just an opaque hint on the manifest.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_id = self.new_run_id()
        rec_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="path-a",
            legacy_path_hint="folder/a.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="path-b",
            legacy_path_hint="folder/b.md",
        )
        # v2 crosswalk keys differ because legacy_path_hash differs.
        self.assertNotEqual(rec_a.crosswalk_key, rec_b.crosswalk_key)
        cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        self.assertEqual(len(cws), 2)
        keys = {cw["crosswalk_key"] for cw in cws}
        self.assertEqual(keys, {rec_a.crosswalk_key, rec_b.crosswalk_key})
        # Each crosswalk stores its own legacy_path_hash and it agrees
        # with what compute_legacy_path_hash produces from the hint.
        for cw in cws:
            self.assertEqual(cw["schema"], "cwk.rt016.migration_crosswalk.v2")
            self.assertEqual(cw["identity_version"], "v2")
            self.assertIn(cw["legacy_path_hash"], {
                R16.compute_legacy_path_hash("folder/a.md"),
                R16.compute_legacy_path_hash("folder/b.md"),
            })
        # Manifest carries two independent entries with distinct
        # (source_namespace, source_kind, legacy_path_hash, sha, outcome).
        entries = list(
            self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id)
        )
        self.assertEqual(len(entries), 2)
        seen = {
            (
                e["source_namespace"],
                e["source_kind"],
                e["legacy_path_hash"],
                e["legacy_source_sha256"],
                e["outcome"],
            )
            for e in entries
        }
        self.assertEqual(len(seen), 2)
        for e in entries:
            self.assertEqual(e["schema"], "cwk.rt016.migration_manifest_entry.v2")
            self.assertEqual(e["identity_version"], "v2")
            self.assertEqual(e["source_namespace"], "cwork")
            self.assertEqual(e["source_kind"], "current_raw")

    def test_same_raw_same_path_same_namespace_stays_idempotent(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_id = self.new_run_id()
        r1 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="one",
            legacy_path_hint="a.md",
        )
        r2 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="two",
            legacy_path_hint="a.md",
        )
        self.assertEqual(r1.crosswalk_key, r2.crosswalk_key)
        cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        self.assertEqual(len(cws), 1)
        entries = list(
            self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id)
        )
        self.assertEqual(len(entries), 1)

    def test_same_bad_raw_different_namespace_yields_two_reviews(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        bad_raw = sample_raw(create_time="never")  # unparseable timestamp
        r_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns-a bad",
            legacy_path_hint="q.md",
        )
        r_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork_alt",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns-b bad",
            legacy_path_hint="q.md",
        )
        self.assertIn(r_a.outcome, ("review", "undecomposable"))
        self.assertIn(r_b.outcome, ("review", "undecomposable"))
        self.assertNotEqual(r_a.review_id, r_b.review_id)
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        self.assertEqual(len(reviews), 2)
        by_id = {r["review_id"]: r for r in reviews}
        self.assertEqual(by_id[r_a.review_id]["schema"], "cwk.rt016.review_entry.v2")
        self.assertEqual(by_id[r_a.review_id]["identity_version"], "v2")
        self.assertEqual(by_id[r_a.review_id]["source_namespace"], "cwork")
        self.assertEqual(by_id[r_a.review_id]["source_kind"], "current_raw")
        self.assertEqual(by_id[r_b.review_id]["source_namespace"], "cwork_alt")

    def test_same_bad_raw_different_path_yields_two_reviews(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        bad_raw = sample_raw(create_time="never")
        r_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="path-a bad",
            legacy_path_hint="q1.md",
        )
        r_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="path-b bad",
            legacy_path_hint="q2.md",
        )
        self.assertNotEqual(r_a.review_id, r_b.review_id)
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        self.assertEqual(len(reviews), 2)

    def test_compute_v2_key_helpers_are_namespace_path_kind_bound(self):
        tenant = "t_" + "a" * 26
        ns_a = "cwork"
        ns_b = "cwork_alt"
        kind_a = "current_raw"
        kind_b = "timeline_snapshot"
        ph_a = R16.compute_legacy_path_hash("a.md")
        ph_b = R16.compute_legacy_path_hash("b.md")
        sha = "0" * 64
        # Determinism.
        self.assertEqual(
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha),
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha),
        )
        # Namespace / path / kind change → different key.
        self.assertNotEqual(
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha),
            R16.compute_crosswalk_key_v2(tenant, ns_b, kind_a, ph_a, sha),
        )
        self.assertNotEqual(
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha),
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_b, ph_a, sha),
        )
        self.assertNotEqual(
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha),
            R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_b, sha),
        )
        # v1 vs v2 domain differ → keys never collide.
        v1 = R16.compute_crosswalk_key(tenant, "g_" + "a" * 26, sha)
        v2 = R16.compute_crosswalk_key_v2(tenant, ns_a, kind_a, ph_a, sha)
        self.assertNotEqual(v1, v2)


# ---------------------------------------------------------------------------
# Bound reader misplaced-payload negatives
# ---------------------------------------------------------------------------


class BoundReaderNegativesTests(RT016TestBase):
    """Directly exercise ``_load_crosswalk_payload_bound`` — the shared
    finder / CAS-conflict entry point — under every identity mismatch.
    """

    def _seed(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="folder/a.md",
        )
        cw = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
        )
        cw_path = _crosswalk_path(self.fx, tenant_id, rec.crosswalk_key)
        raw = cw_path.read_bytes()
        expect = R16._BoundReaderExpect(
            tenant_id=tenant_id,
            source_namespace="cwork",
            source_kind="current_raw",
            legacy_path_hash=cw["legacy_path_hash"],
            legacy_source_sha256=cw["legacy_source_sha256"],
            filename_crosswalk_key=rec.crosswalk_key,
        )
        return tenant_id, rec.crosswalk_key, cw, raw, expect

    def test_bound_reader_accepts_matching_identity(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        payload = R16._load_crosswalk_payload_bound(raw, expect=expect)
        self.assertEqual(payload["crosswalk_key"], expect.filename_crosswalk_key)

    def test_bound_reader_rejects_tenant_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        bad = R16._BoundReaderExpect(
            tenant_id="t_" + "b" * 26,
            source_namespace=expect.source_namespace,
            source_kind=expect.source_kind,
            legacy_path_hash=expect.legacy_path_hash,
            legacy_source_sha256=expect.legacy_source_sha256,
            filename_crosswalk_key=expect.filename_crosswalk_key,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bound_reader_rejects_namespace_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        bad = R16._BoundReaderExpect(
            tenant_id=expect.tenant_id,
            source_namespace="cwork_alt",
            source_kind=expect.source_kind,
            legacy_path_hash=expect.legacy_path_hash,
            legacy_source_sha256=expect.legacy_source_sha256,
            filename_crosswalk_key=expect.filename_crosswalk_key,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bound_reader_rejects_source_kind_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        bad = R16._BoundReaderExpect(
            tenant_id=expect.tenant_id,
            source_namespace=expect.source_namespace,
            source_kind="timeline_snapshot",
            legacy_path_hash=expect.legacy_path_hash,
            legacy_source_sha256=expect.legacy_source_sha256,
            filename_crosswalk_key=expect.filename_crosswalk_key,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bound_reader_rejects_path_hash_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        bad = R16._BoundReaderExpect(
            tenant_id=expect.tenant_id,
            source_namespace=expect.source_namespace,
            source_kind=expect.source_kind,
            legacy_path_hash=R16.compute_legacy_path_hash("elsewhere.md"),
            legacy_source_sha256=expect.legacy_source_sha256,
            filename_crosswalk_key=expect.filename_crosswalk_key,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bound_reader_rejects_raw_sha_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        bad = R16._BoundReaderExpect(
            tenant_id=expect.tenant_id,
            source_namespace=expect.source_namespace,
            source_kind=expect.source_kind,
            legacy_path_hash=expect.legacy_path_hash,
            legacy_source_sha256="0" * 64,
            filename_crosswalk_key=expect.filename_crosswalk_key,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bound_reader_rejects_filename_key_mismatch(self):
        _tid, _ck, _cw, raw, expect = self._seed()
        # Fabricate a different filename key so the recomputed v2 key
        # will disagree with expect.filename_crosswalk_key.
        bad = R16._BoundReaderExpect(
            tenant_id=expect.tenant_id,
            source_namespace=expect.source_namespace,
            source_kind=expect.source_kind,
            legacy_path_hash=expect.legacy_path_hash,
            legacy_source_sha256=expect.legacy_source_sha256,
            filename_crosswalk_key="cw_" + "z" * 26,
        )
        with self.assertRaises(R16.LegacyImportError) as cm:
            R16._load_crosswalk_payload_bound(raw, expect=bad)
        self.assertEqual(cm.exception.code, "corrupt")


# ---------------------------------------------------------------------------
# Coordinated tampering: swap A's crosswalk bytes with a self-consistent
# payload borrowed from B's canonical
# ---------------------------------------------------------------------------


class CoordinatedTamperingReconcilerTests(RT016TestBase):
    def test_swap_full_payload_between_two_crosswalks_rejected(self):
        """The core Major from the acceptance report.

        Import two different raws A and B at different paths, then
        overwrite A's crosswalk file bytes with B's crosswalk payload
        (only the file name / crosswalk_key are kept as A's).  Every
        internal field is self-consistent (B's own bindings) and every
        integrity check inside :meth:`ShadowImporter.read_crosswalk`
        (which is anchor-free) would accept it.  The v2 reconciler must
        still refuse to count this as ``both_equal`` because the
        crosswalk_key derived from A's identity (tenant, ns, kind, A's
        path_hash, A's raw sha) does not match B's payload — the bound
        reader catches this — and the re-decomposition of A's legacy
        bytes with the anchor does not produce B's canonical.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A body"),
                    "b.md": sample_raw(report_id="207B", body="B body"),
                },
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            receipts = self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            self.assertEqual(len(receipts), 2)
            cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
            self.assertEqual(len(cws), 2)
            by_ph = {
                cw["legacy_path_hash"]: cw for cw in cws
            }
            ph_a = R16.compute_legacy_path_hash("a.md")
            ph_b = R16.compute_legacy_path_hash("b.md")
            cw_a = by_ph[ph_a]
            cw_b = by_ph[ph_b]
            # Coordinated tampering: overwrite A's file bytes with B's
            # payload bytes.  We must not change the filename, so we
            # rewrite the filename slot with B's full payload; the
            # filename key still points at what the file name says.
            a_path = _crosswalk_path(self.fx, tenant_id, cw_a["crosswalk_key"])
            b_path = _crosswalk_path(self.fx, tenant_id, cw_b["crosswalk_key"])
            b_bytes = b_path.read_bytes()
            a_path.write_bytes(b_bytes)

            # 1. read_crosswalk on A must fail closed — the bound
            # binding at the API level checks filename==payload key.
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.read_crosswalk(
                    tenant_id=tenant_id, crosswalk_key=cw_a["crosswalk_key"]
                )
            self.assertEqual(cm.exception.code, "corrupt")

            # 2. The reconciler classification: iter_crosswalks reads
            # every file — the tampered A payload raises corrupt from
            # inside iter_crosswalks; the reconciler propagates.
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            with self.assertRaises(R16.LegacyImportError) as cm2:
                self.fx.reconciler.reconcile(
                    anchor=default_anchor(tenant_id),
                    run_id=self.new_run_id(),
                    source=source2,
                )
            self.assertEqual(cm2.exception.code, "corrupt")

    def test_swap_payload_bytes_only_forged_key_kept_in_place(self):
        """Attacker takes B's fields but rewrites crosswalk_key to A's key.

        In this variant every internal field agrees with A's
        crosswalk_key (attacker changes the payload's ``crosswalk_key``
        to A's opaque value).  With v1 integrity checks this would
        JCS-round-trip successfully.  With v2 integrity checks the
        crosswalk_key derivation is
        ``H(tenant, ns, kind, path_hash, raw_sha)`` — and the payload
        still carries B's ``legacy_path_hash`` + B's raw_sha, so the
        recomputed key won't equal A's.  Bound reader fails closed.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A body"),
                    "b.md": sample_raw(report_id="207B", body="B body"),
                },
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
            ph_a = R16.compute_legacy_path_hash("a.md")
            ph_b = R16.compute_legacy_path_hash("b.md")
            by_ph = {cw["legacy_path_hash"]: cw for cw in cws}
            cw_a = by_ph[ph_a]
            cw_b = by_ph[ph_b]
            a_key = cw_a["crosswalk_key"]
            a_path = _crosswalk_path(self.fx, tenant_id, a_key)
            # Rewrite A's file with B's contents but force the payload
            # crosswalk_key back to A's key (self-consistency at the
            # top level while borrowing B's downstream fields).
            def mutator(p):
                # Overwrite every top-level key with B's payload's values
                # except crosswalk_key.  Simplest: copy all of B, then
                # patch crosswalk_key back to A.
                b_payload = json.loads(
                    _crosswalk_path(self.fx, tenant_id, cw_b["crosswalk_key"])
                    .read_bytes()
                    .decode("utf-8")
                )
                p.clear()
                p.update(b_payload)
                p["crosswalk_key"] = a_key
            _rewrite_bytes(a_path, mutator)

            # read_crosswalk fails: filename key vs payload's canonical
            # cross-check inside _validate_crosswalk_integrity now
            # detects that payload.crosswalk_key != H(...) for either
            # A's or B's identity (A's identity yields A's key, but the
            # payload holds B's path_hash / raw_sha, so recomputed key
            # equals B's key which != A).
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.read_crosswalk(
                    tenant_id=tenant_id, crosswalk_key=a_key
                )
            self.assertEqual(cm.exception.code, "corrupt")

    def test_swap_reconciler_bound_reader_from_finder_side(self):
        """After coordinated swap, a fresh import (finder path) must
        not silently return B's stale record as an "idempotent hit".
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A body"),
                    "b.md": sample_raw(report_id="207B", body="B body"),
                },
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
            ph_a = R16.compute_legacy_path_hash("a.md")
            by_ph = {cw["legacy_path_hash"]: cw for cw in cws}
            cw_a = by_ph[ph_a]
            ph_b_key = [cw["crosswalk_key"] for cw in cws if cw["legacy_path_hash"] != ph_a][0]
            b_bytes = _crosswalk_path(self.fx, tenant_id, ph_b_key).read_bytes()
            _crosswalk_path(self.fx, tenant_id, cw_a["crosswalk_key"]).write_bytes(b_bytes)
            # A fresh import of a.md must not return B's payload from
            # the finder — the finder's bound reader refuses the
            # tampered slot.
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.import_one(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    raw_bytes=(root / "a.md").read_bytes(),
                    run_id=self.new_run_id(),
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="post-tamper",
                    legacy_path_hint="a.md",
                )
            self.assertEqual(cm.exception.code, "corrupt")


# ---------------------------------------------------------------------------
# Anchor enforcement — anchor missing / mismatch / drift
# ---------------------------------------------------------------------------


class AnchorEnforcementTests(RT016TestBase):
    def setUp(self) -> None:  # noqa: D401
        super().setUp()
        # Per-test durable tempdir so reconcile() sees a stable tree.
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def _setup_ok(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        root = build_legacy_tree(Path(self._td.name), {"a.md": sample_raw()})
        source = R16.LegacySource(str(root))
        run_id = self.new_run_id()
        self.fx.importer.import_batch(
            tenant_id=tenant_id,
            source_namespace="cwork",
            source=source,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="setup",
        )
        return tenant_id, run_id, root

    def test_anchor_is_required(self):
        tenant_id, run_id, root = self._setup_ok()
        source2 = R16.LegacySource(str(root))
        source2.snapshot()
        # Passing anchor=None must fail closed; the reconciler cannot
        # fall back to trusting crosswalk self-declared identity.
        with self.assertRaises(R16.LegacyImportError) as cm:
            self.fx.reconciler.reconcile(
                anchor=None,  # type: ignore[arg-type]
                run_id=run_id,
                source=source2,
            )
        self.assertEqual(cm.exception.code, "contract")

    def test_anchor_namespace_mismatch_treated_as_anchor_mismatch(self):
        tenant_id, run_id, root = self._setup_ok()
        source2 = R16.LegacySource(str(root))
        source2.snapshot()
        report = self.fx.reconciler.reconcile(
            anchor=default_anchor(tenant_id, source_namespace="cwork_alt"),
            run_id=run_id,
            source=source2,
        )
        # crosswalk was written under 'cwork', anchor says 'cwork_alt' →
        # crosswalk falls into anchor_mismatch which counts against
        # only_new_count.
        self.assertEqual(report.both_equal_count, 0)
        self.assertGreaterEqual(report.only_new_count, 1)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("anchor_mismatch", classes)

    def test_anchor_source_kind_mismatch_treated_as_anchor_mismatch(self):
        tenant_id, run_id, root = self._setup_ok()
        source2 = R16.LegacySource(str(root))
        source2.snapshot()
        report = self.fx.reconciler.reconcile(
            anchor=default_anchor(tenant_id, source_kind="timeline_snapshot"),
            run_id=run_id,
            source=source2,
        )
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("anchor_mismatch", classes)

    def test_anchor_decomposer_version_mismatch_treated_as_anchor_mismatch(self):
        tenant_id, run_id, root = self._setup_ok()
        source2 = R16.LegacySource(str(root))
        source2.snapshot()
        report = self.fx.reconciler.reconcile(
            anchor=default_anchor(tenant_id, decomposer_version="v999"),
            run_id=run_id,
            source=source2,
        )
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("anchor_mismatch", classes)

    def test_legacy_drift_flips_zero_drift_flag(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": sample_raw()})
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            (root / "a.md").write_bytes(sample_raw(body="drifted"))
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=run_id,
                source=source2,
            )
        self.assertFalse(report.zero_drift_verified)

    def test_rt014_object_missing_fails_both_equal(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="setup",
            legacy_path_hint="a.md",
        )
        shared_obj_root = self.fx.root / "shared" / "objects"
        for shard in shared_obj_root.iterdir():
            for f in shard.iterdir():
                f.unlink()
            shard.rmdir()
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": raw})
            source = R16.LegacySource(str(root))
            source.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source,
            )
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertTrue(
            classes & {"canonical_missing", "object_bytes_sha_mismatch", "canonical_sha_mismatch"},
            f"expected canonical-side failure, saw {classes}",
        )

    def test_re_decompose_mismatch_when_legacy_bytes_drift_between_versions(self):
        """If the on-disk legacy bytes silently mutate to different but
        parseable Markdown, the re-decomposition + read_version pipeline
        must not silently PASS.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"a.md": sample_raw(report_id="207A", body="orig")}
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            # Silently mutate the legacy file bytes without re-importing.
            (root / "a.md").write_bytes(
                sample_raw(report_id="207A", body="mutated")
            )
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        # Mutation is caught by (sha != crosswalk.legacy_source_sha256)
        # which classifies as legacy_source_sha_drift; the crosswalk is
        # not counted as both_equal.
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("legacy_source_sha_drift", classes)


# ---------------------------------------------------------------------------
# v1 records: readable but not accepted for v2 idempotency / PASS
# ---------------------------------------------------------------------------


def _minimal_valid_v1_crosswalk(
    *,
    tenant_id: str,
    source_namespace: str,
    report_id: str,
    body: str,
    run_id: str,
    legacy_path_hash: str,
    legacy_source_sha256: str,
    source_kind: str = "current_raw",
) -> dict:
    """Construct a synthetic but fully-self-consistent v1 crosswalk.

    Used only in the ``V1BackwardsCompatTests`` fixture below; this
    payload is manually crafted (no RT-014 publish happens for it) so
    it will legitimately fail reconciler read_version — that's fine,
    because the reconciler must never even consider promoting a v1
    record to PASS regardless of whether its canonical exists in
    RT-014.  We only care that ``read_crosswalk`` can load it (audit).
    """

    # v1 canonical binding derivations must all be self-consistent.
    envelope_partial = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": source_namespace,
        "report_id": report_id,
        "title": "v1 legacy synthetic",
        "author": {"source_user_id": "u_v1"},
        "created_at": "2024-06-15T10:30:00Z",
        "source_updated_at": "2024-06-16T09:00:00Z",
        "body": body,
        "normalizer_version": R16.NORMALIZER_VERSION,
    }
    normalised = C.nfc_normalize(envelope_partial)
    canonical_sha = C.canonical_sha256(normalised)
    envelope = dict(normalised)
    envelope["canonical_sha256"] = canonical_sha
    object_bytes = C.canonical_json_bytes(envelope)
    object_bytes_sha = hashlib.sha256(object_bytes).hexdigest()
    report_key = C.compose_report_key(source_namespace, report_id)
    grant_key = AL.compute_grant_key(tenant_id, report_key)
    v1_cw_key = R16.compute_crosswalk_key(tenant_id, grant_key, legacy_source_sha256)
    object_id = "o_" + "a" * 26
    catalog_key = "r_" + "b" * 26
    now = "2024-06-16T10:00:00Z"
    tenant_view = {
        "schema": "cwk.tenant_view.v1",
        "tenant_id": tenant_id,
        "report_key": report_key,
        "canonical_sha256": canonical_sha,
        "observed_at": now,
    }
    C.validate_tenant_view(tenant_view)
    payload = {
        "schema": "cwk.rt016.migration_crosswalk.v1",
        "crosswalk_key": v1_cw_key,
        "tenant_id": tenant_id,
        "view_key": grant_key,
        "report_key": report_key,
        "source_namespace": source_namespace,
        "report_id": report_id,
        "source_kind": source_kind,
        "legacy_source_sha256": legacy_source_sha256,
        "canonical_sha256": canonical_sha,
        "object_bytes_sha256": object_bytes_sha,
        "object_id": object_id,
        "catalog_key": catalog_key,
        "catalog_revision": 1,
        "publish_receipt": {
            "schema": "cwk.rt014.publish_receipt.v1",
            "report_key": report_key,
            "canonical_sha256": canonical_sha,
            "object_id": object_id,
            "catalog_key": catalog_key,
            "is_new_version": True,
            "is_new_report": True,
            "catalog_revision": 1,
            "catalog_head_sha256": "e" * 64,
            "object_bytes_sha256": object_bytes_sha,
        },
        "observe_grant_key": grant_key,
        "observation_source": "legacy_raw_decomposition",
        "initial_status": "granted",
        "tenant_view_envelope": tenant_view,
        "tenant_view_written": False,
        "tenant_view_deferred_reason": "no_authority_receipt_available",
        "tenant_view_record_revision": None,
        "timeline_snapshot_hashes": [],
        "timeline_event_hashes": [],
        "decomposer_version": R16.DECOMPOSER_VERSION,
        "normalizer_version": R16.NORMALIZER_VERSION,
        "decompose_report": {
            "schema": "cwk.rt016.decompose_report.v1",
            "decomposer_version": R16.DECOMPOSER_VERSION,
            "normalizer_version": R16.NORMALIZER_VERSION,
            "legacy_source_sha256": legacy_source_sha256,
            "canonical_sha256": canonical_sha,
            "object_bytes_sha256": object_bytes_sha,
            "body_bytes_length": len(body.encode("utf-8")),
            "body_truncation_would_occur": False,
            "field_sources": {},
            "hit_rules": [],
            "unknown_frontmatter_keys": [],
            "unknown_row_keys": [],
            "unknown_reply_keys": [],
            "unknown_node_keys": [],
            "timeline_event_hash_check": {
                "snapshot_count": 0,
                "event_count": 0,
                "matched_event_count": 0,
                "unmatched_event_count": 0,
                "coverage_ok": True,
            },
            "decomposition_status": "ok",
            "quarantine_reasons": [],
        },
        "migration_status": "complete",
        "run_id": run_id,
        "run_started_at": now,
        "created_at": now,
        "record_revision": 1,
    }
    return payload


class V1BackwardsCompatTests(RT016TestBase):
    def _plant_v1_crosswalk(self, tenant_id: str, run_id: str) -> tuple[str, Path, dict]:
        """Manually plant a v1 crosswalk under the tenant subdir.

        We can plant it because v1 keys use a different domain from v2
        keys, so no v2 lookup will ever collide.  The importer's
        `initialize` + `_tenant_fd(create=True)` requires a first
        import to lay down the tenant subdirs, so seed one legit v2
        first.
        """

        # Seed one v2 crosswalk so tenant subdirs exist.
        seed = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(report_id="207SEED", body="seed"),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="seed.md",
        )
        _seed = seed  # keep receipt referenced
        # Construct a v1 payload keyed under a different raw + path.
        legacy_bytes = b"legacy raw bytes for v1 record"
        legacy_source_sha256 = hashlib.sha256(legacy_bytes).hexdigest()
        legacy_path_hash = R16.compute_legacy_path_hash("legacy/v1-doc.md")
        payload = _minimal_valid_v1_crosswalk(
            tenant_id=tenant_id,
            source_namespace="cwork",
            report_id="207V1",
            body="v1 body",
            run_id=run_id,
            legacy_path_hash=legacy_path_hash,
            legacy_source_sha256=legacy_source_sha256,
        )
        cw_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / tenant_id / "crosswalks"
        cw_dir.mkdir(parents=True, exist_ok=True)
        cw_path = cw_dir / f"{payload['crosswalk_key']}.json"
        cw_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        os.chmod(cw_path, 0o600)
        return payload["crosswalk_key"], cw_path, payload

    def test_v1_crosswalk_is_readable_via_read_crosswalk_for_audit(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        ck, _cw_path, planted = self._plant_v1_crosswalk(tenant_id, run_id)
        cw = self.fx.importer.read_crosswalk(tenant_id=tenant_id, crosswalk_key=ck)
        self.assertEqual(cw["schema"], "cwk.rt016.migration_crosswalk.v1")
        self.assertEqual(cw["crosswalk_key"], planted["crosswalk_key"])

    def test_v1_crosswalk_never_hits_v2_idempotency_finder(self):
        """A subsequent import with the same raw bytes must NOT return
        the pre-existing v1 record — it must write a fresh v2 crosswalk
        under a different (v2) key.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        legacy_bytes = b"another v1 raw"
        # Plant a v1 crosswalk for these bytes under a fake path.
        legacy_source_sha256 = hashlib.sha256(legacy_bytes).hexdigest()
        legacy_path_hash = R16.compute_legacy_path_hash("legacy/x.md")
        payload = _minimal_valid_v1_crosswalk(
            tenant_id=tenant_id,
            source_namespace="cwork",
            report_id="207V1X",
            body="does not matter",
            run_id=run_id,
            legacy_path_hash=legacy_path_hash,
            legacy_source_sha256=legacy_source_sha256,
        )
        cw_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / tenant_id / "crosswalks"
        cw_dir.mkdir(parents=True, exist_ok=True)
        cw_path = cw_dir / f"{payload['crosswalk_key']}.json"
        cw_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        os.chmod(cw_path, 0o600)
        # Import a real, decomposable raw with a matching (namespace,
        # path) but different bytes.  This will get its own v2 key.
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(report_id="207V1Y"),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="fresh",
            legacy_path_hint="legacy/x.md",
        )
        self.assertEqual(rec.outcome, "complete")
        # v2 key differs from v1 key (different domain).
        self.assertNotEqual(rec.crosswalk_key, payload["crosswalk_key"])
        # Both records now exist side by side.
        cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        schemas = sorted({cw["schema"] for cw in cws})
        self.assertIn("cwk.rt016.migration_crosswalk.v1", schemas)
        self.assertIn("cwk.rt016.migration_crosswalk.v2", schemas)

    def test_v1_records_counted_as_unanchored_never_both_equal(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        _ck, _cw_path, _planted = self._plant_v1_crosswalk(tenant_id, run_id)
        # Reconcile with an empty tree: v1 planted crosswalk must count
        # as unanchored_v1, not both_equal.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty"
            root.mkdir()
            source = R16.LegacySource(str(root))
            source.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source,
            )
        # v1 planted crosswalk + seed v2 crosswalk (whose legacy raw
        # is absent from the empty tree so it's only_new).
        self.assertGreaterEqual(report.unanchored_v1_count, 1)
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("unanchored_v1", classes)


# ---------------------------------------------------------------------------
# Reconciler consumes RT-014 only through read_version
# ---------------------------------------------------------------------------


class ReconcilerRT014PublicApiOnlyTests(unittest.TestCase):
    def test_reconciler_source_calls_only_read_version(self):
        source = (PROJECT / "scripts" / "cwk_migration_reconciler.py").read_text(
            encoding="utf-8"
        )
        # Only the read_version method is called on self._evidence /
        # evidence_store; no publish / enumerate / list / write /
        # delete / recover.
        for banned in (
            "_evidence.publish",
            "_evidence.recover",
            "_evidence.enumerate",
            "_evidence.list",
            "_evidence._",
            "self._evidence.__",
        ):
            self.assertNotIn(banned, source, f"reconciler must not call {banned}")
        self.assertIn("self._evidence.read_version", source)

    def test_reconciler_source_does_not_import_legacy_writers(self):
        source = (PROJECT / "scripts" / "cwk_migration_reconciler.py").read_text(
            encoding="utf-8"
        )
        for banned_mod in (
            "cwk_raw_store",
            "cwk_thread_timeline",
            "cwk_collect_live",
            "cwk_nightly_pipeline",
            "cwk_docdb_cloud",
            "cwk_sync_mirror_to_docdb",
            "cwk_wiki_query",
        ):
            self.assertNotIn(
                f"import {banned_mod}", source,
                f"reconciler must not import {banned_mod}",
            )

    def test_importer_source_never_calls_shared_write_apis_beyond_publish(self):
        source = (PROJECT / "scripts" / "cwk_legacy_raw_import.py").read_text(
            encoding="utf-8"
        )
        # ShadowImporter is allowed to call publish + read; that's it.
        for banned in (
            "self._evidence.recover",
            "self._evidence.enumerate",
            "self._evidence.list",
            "self._evidence.delete",
        ):
            self.assertNotIn(banned, source)


# ---------------------------------------------------------------------------
# Manifest binding validation
# ---------------------------------------------------------------------------


class ManifestV2BindingTests(RT016TestBase):
    def test_manifest_entries_carry_v2_identity_bindings(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        for ns, hint in (("cwork", "a.md"), ("cwork_alt", "b.md")):
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace=ns,
                raw_bytes=sample_raw(report_id="207M"),
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason=f"seed-{ns}",
                legacy_path_hint=hint,
            )
        entries = list(
            self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id)
        )
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertEqual(e["schema"], "cwk.rt016.migration_manifest_entry.v2")
            self.assertEqual(e["identity_version"], "v2")
            self.assertIn(e["source_namespace"], ("cwork", "cwork_alt"))
            self.assertEqual(e["source_kind"], "current_raw")
            self.assertRegex(e["legacy_path_hash"], r"^[0-9a-f]{64}$")

    def test_manifest_line_tamper_detected_as_corrupt(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="a.md",
        )
        mf_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "manifests"
            / f"{run_id}.jsonl"
        )
        text = mf_path.read_bytes()
        # Replace the schema constant to force validation failure.
        mutated = text.replace(
            b"cwk.rt016.migration_manifest_entry.v2",
            b"cwk.rt016.migration_manifest_entry.vx",
        )
        mf_path.write_bytes(mutated)
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))
        self.assertEqual(cm.exception.code, "corrupt")


# ---------------------------------------------------------------------------
# Happy-path reconcile PASS with anchor + re-decompose
# ---------------------------------------------------------------------------


class HappyPathAnchorReconcileTests(RT016TestBase):
    def test_happy_path_all_bindings_pass(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A"),
                    "b.md": sample_raw(report_id="207B", body="B"),
                },
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        self.assertTrue(report.zero_drift_verified)
        self.assertEqual(report.both_equal_count, 2)
        self.assertEqual(report.only_legacy_count, 0)
        self.assertEqual(report.only_new_count, 0)
        self.assertEqual(report.unanchored_v1_count, 0)
        self.assertEqual(
            report.payload["schema"], "cwk.rt016.reconciliation_report.v2"
        )
        self.assertEqual(
            report.payload["reconciliation_anchor"]["source_namespace"], "cwork"
        )


# ---------------------------------------------------------------------------
# Zero drift over RT-011~015 frozen files and no secret leakage
# ---------------------------------------------------------------------------


class RT016V2FrozenFilesZeroDriftTests(unittest.TestCase):
    """v2 anchor-remediation zero-drift guard — delegates to the
    explicit allowlist baseline in ``test_rt016_schemas`` so both
    test files share one source of truth and neither uses a git-HEAD
    self-comparison.
    """

    def test_frozen_baselines_match_pinned_shas(self):
        from test_rt016_schemas import (  # noqa: E402
            FrozenFilesZeroDriftTests,
            _FROZEN_RT011_015_BASELINE_SHAS,
            _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS,
        )
        # Baseline maps must never be empty (defensive against merge
        # accidents deleting the pinned dict).
        self.assertGreaterEqual(len(_FROZEN_RT011_015_BASELINE_SHAS), 24)
        self.assertGreaterEqual(len(_FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS), 5)
        # Delegate to the two dedicated tests to actually compare
        # SHA-256s so any failure names the drifted file.
        for method in (
            "test_rt011_015_files_match_baseline_sha",
            "test_rt016_v1_schemas_match_pinned_baseline_sha",
        ):
            case = FrozenFilesZeroDriftTests(method)
            result = unittest.TestResult()
            case.run(result)
            if not result.wasSuccessful():
                msgs = [str(e[1]) for e in (result.failures + result.errors)]
                self.fail(
                    f"delegated frozen check {method} failed:\n"
                    + "\n".join(msgs)
                )


class RT016V2NoSecretLeakageTests(unittest.TestCase):
    _OWNED_FILES: tuple[str, ...] = (
        "scripts/cwk_legacy_raw_import.py",
        "scripts/cwk_migration_reconciler.py",
        "tests/_rt016_helpers.py",
        "tests/test_rt016_anchor.py",
        "tests/test_rt016_attacks.py",
        "tests/test_rt016_decomposer.py",
        "tests/test_rt016_importer.py",
        "tests/test_rt016_reconciler.py",
        "tests/test_rt016_remediation.py",
        "tests/test_rt016_schemas.py",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_crosswalk_v2.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/review_entry_v2.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_manifest_entry_v2.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/reconciliation_report_v2.schema.json",
    )

    def test_owned_files_have_no_real_secret_literals(self):
        import re as _re
        # Patterns that would indicate a real production secret leaking
        # into RT-016-owned files.  We tolerate variable names /
        # allow-list constants like `app_key` in `deepForbiddenProperties`
        # arrays; we look for value literals.
        value_patterns = [
            _re.compile(r"CWORK_APP_KEY\s*=\s*['\"][^'\"]+['\"]"),
            _re.compile(r"api_?key['\"]\s*[:=]\s*['\"][A-Za-z0-9_-]{16,}['\"]"),
            _re.compile(r"bearer\s+[A-Za-z0-9._-]{20,}", _re.IGNORECASE),
            _re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+"),
            _re.compile(r"xg_biz_api_key\s*=\s*['\"][^'\"]+['\"]"),
        ]
        for rel in self._OWNED_FILES:
            path = PROJECT / rel
            self.assertTrue(path.exists(), f"{rel} missing")
            text = path.read_text(encoding="utf-8")
            for pat in value_patterns:
                match = pat.search(text)
                self.assertIsNone(
                    match,
                    f"secret literal leaked in {rel}: {match.group(0) if match else ''!r}",
                )


if __name__ == "__main__":
    unittest.main()
