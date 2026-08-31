"""RT-016 ShadowImporter behaviour tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    AC,
    AF,
    AL,
    C,
    FakeAuthorityContext,
    Fixture,
    I,
    R16,
    RT016TestBase,
    SE,
    TR,
    TV,
    build_legacy_tree,
    promote_tenant,
    sample_raw,
    utc_iso,
)


class InitializeAndLayoutTests(RT016TestBase):
    def test_initialize_creates_rt016_root(self):
        rt016_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR
        self.assertTrue(rt016_dir.is_dir())
        self.assertEqual(stat.S_IMODE(rt016_dir.stat().st_mode), 0o700)

    def test_no_tenant_dir_until_first_import(self):
        tenant_id = self.fx.new_tenant()
        rt016_tenant = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / tenant_id
        self.assertFalse(rt016_tenant.exists())


class ImportOneHappyPathTests(RT016TestBase):
    def test_writes_canonical_observation_and_crosswalk(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_id = self.new_run_id()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="rt016 test import",
            legacy_path_hint="legacy-raw/2070001.md",
        )
        self.assertEqual(rec.outcome, "complete")
        self.assertRegex(rec.crosswalk_key, r"^cw_[a-z2-7]{26}$")
        self.assertRegex(rec.canonical_sha256, r"^[0-9a-f]{64}$")
        self.assertFalse(rec.tenant_view_written)
        self.assertEqual(
            rec.tenant_view_deferred_reason, "no_authority_receipt_available"
        )
        # Canonical object exists in RT-014.
        envelope = self.fx.evidence.read_version(
            "cwork:2070001", rec.canonical_sha256
        )
        self.assertEqual(envelope["schema"], "cwk.canonical_report.v1")
        # Grant exists in RT-015 with status `granted` (never active).
        with self.fx.ledger._tenant_fd(tenant_id) as tfd:
            grant = self.fx.ledger._read_grant_file(tfd, rec.grant_key)
        self.assertEqual(grant.status, "granted")
        # Crosswalk record round-trips.
        cw = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
        )
        self.assertEqual(cw["report_key"], "cwork:2070001")
        self.assertEqual(cw["observation_source"], "legacy_raw_decomposition")
        self.assertEqual(cw["initial_status"], "granted")
        # Manifest has one entry.
        entries = list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "complete")

    def test_hash_fields_distinct(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="hash distinct",
            legacy_path_hint="a.md",
        )
        cw = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
        )
        legacy = cw["legacy_source_sha256"]
        canonical = cw["canonical_sha256"]
        obj = cw["object_bytes_sha256"]
        self.assertNotEqual(legacy, canonical)
        self.assertNotEqual(canonical, obj)
        self.assertNotEqual(legacy, obj)

    def test_multiple_legacy_sha_same_canonical(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw_a = sample_raw(source_lane="inbox")
        raw_b = sample_raw(source_lane="outbox")
        run_id = self.new_run_id()
        rec_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw_a,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="import a",
            legacy_path_hint="a.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw_b,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="import b",
            legacy_path_hint="b.md",
        )
        self.assertEqual(rec_a.canonical_sha256, rec_b.canonical_sha256)
        self.assertNotEqual(rec_a.crosswalk_key, rec_b.crosswalk_key)
        # RT-014 stored a single version.
        catalog_dir = (
            self.fx.root / "shared" / "report-versions"
        )
        # Count entries in the catalog.jsonl.
        catalogs = list(catalog_dir.rglob("catalog.jsonl"))
        self.assertEqual(len(catalogs), 1)
        raw_jsonl = catalogs[0].read_bytes().splitlines()
        self.assertEqual(len(raw_jsonl), 1)

    def test_body_change_creates_new_catalog_version(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw_v1 = sample_raw(body="first")
        raw_v2 = sample_raw(body="second")
        run_id = self.new_run_id()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw_v1,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="v1",
            legacy_path_hint="v1.md",
        )
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw_v2,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="v2",
            legacy_path_hint="v2.md",
        )
        catalog_dir = self.fx.root / "shared" / "report-versions"
        catalogs = list(catalog_dir.rglob("catalog.jsonl"))
        self.assertEqual(len(catalogs), 1)
        entries = catalogs[0].read_bytes().splitlines()
        self.assertEqual(len(entries), 2)

    def test_idempotent_within_run(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_id = self.new_run_id()
        rec1 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="one",
            legacy_path_hint="x.md",
        )
        rec2 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="two",
            legacy_path_hint="x.md",
        )
        self.assertEqual(rec1.crosswalk_key, rec2.crosswalk_key)
        # Manifest still contains one line for this legacy_source_sha256.
        entries = list(
            self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id)
        )
        self.assertEqual(len(entries), 1)

    def test_idempotent_across_runs(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_a = self.new_run_id()
        run_b = self.new_run_id()
        rec_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_a,
            run_started_at=utc_iso(),
            actor="admin",
            reason="run_a",
            legacy_path_hint="x.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_b,
            run_started_at=utc_iso(),
            actor="admin",
            reason="run_b",
            legacy_path_hint="x.md",
        )
        self.assertEqual(rec_a.crosswalk_key, rec_b.crosswalk_key)


class NamespaceIsolationTests(RT016TestBase):
    def test_two_tenants_share_canonical_but_have_separate_crosswalks(self):
        t_a = self.fx.new_tenant(status="pilot")
        t_b = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec_a = self.fx.importer.import_one(
            tenant_id=t_a,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="a",
            legacy_path_hint="x.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=t_b,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="b",
            legacy_path_hint="x.md",
        )
        # Same canonical.
        self.assertEqual(rec_a.canonical_sha256, rec_b.canonical_sha256)
        # Different crosswalks.
        self.assertNotEqual(rec_a.crosswalk_key, rec_b.crosswalk_key)
        # Different view keys.
        self.assertNotEqual(rec_a.view_key, rec_b.view_key)
        # RT-016 crosswalks are namespaced under separate tenant dirs.
        a_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / t_a
        b_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / t_b
        self.assertTrue(a_dir.is_dir())
        self.assertTrue(b_dir.is_dir())
        # Tenant A cannot list Tenant B's crosswalks via public API.
        cws_a = list(self.fx.importer.iter_crosswalks(tenant_id=t_a))
        cws_b = list(self.fx.importer.iter_crosswalks(tenant_id=t_b))
        self.assertEqual(len(cws_a), 1)
        self.assertEqual(len(cws_b), 1)
        self.assertNotEqual(cws_a[0]["crosswalk_key"], cws_b[0]["crosswalk_key"])

    def test_crosswalk_lives_in_rt016_namespace_only(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="one",
            legacy_path_hint="x.md",
        )
        # Views directory MUST be empty (no tenant view upsert without
        # authority receipt).
        views_dir = self.fx.root / "tenants" / tenant_id / "views"
        self.assertTrue(views_dir.is_dir())
        self.assertEqual(
            [p for p in views_dir.iterdir() if not p.name.startswith(".cwk-tmp-")],
            [],
        )
        # Access-ledger tenant dir contains a grant (via observe).
        al_dir = self.fx.root / "registry" / "access-ledger" / tenant_id / "grants"
        self.assertTrue(al_dir.is_dir())
        self.assertTrue(any(p.name.endswith(".json") for p in al_dir.iterdir()))
        # RT-016 owns the crosswalk subdir.
        cw_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / tenant_id / "crosswalks"
        self.assertTrue(cw_dir.is_dir())
        # File modes are 0o600 for crosswalk leaves.
        for p in cw_dir.iterdir():
            if p.is_file() and p.name.endswith(".json"):
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)


class TenantViewUpsertViaFakeAuthorityTests(RT016TestBase):
    def test_valid_receipt_upserts_view(self):
        tenant_id = self.fx.new_tenant(status="active")
        raw = sample_raw()
        with FakeAuthorityContext() as ctx:
            grant_key = AL.compute_grant_key(tenant_id, "cwork:2070001")
            receipt = ctx.receipt(
                tenant_id=tenant_id,
                source_namespace="cwork",
                report_id="2070001",
                grant_key=grant_key,
            )
            rec = self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="upsert",
                legacy_path_hint="x.md",
                authority_receipt=receipt,
            )
        self.assertTrue(rec.tenant_view_written)
        self.assertIsNone(rec.tenant_view_deferred_reason)
        self.assertEqual(rec.tenant_view_record_revision, 1)
        # RT-015 view store now contains the overlay.
        views_dir = self.fx.root / "tenants" / tenant_id / "views"
        overlay_files = [p for p in views_dir.iterdir() if p.name.endswith(".json")]
        self.assertEqual(len(overlay_files), 1)

    def test_bad_receipt_defers_view_but_still_records_crosswalk(self):
        tenant_id = self.fx.new_tenant(status="active")
        raw = sample_raw()
        with FakeAuthorityContext() as ctx:
            grant_key = AL.compute_grant_key(tenant_id, "cwork:2070001")
            receipt = ctx.receipt(
                tenant_id=tenant_id,
                source_namespace="cwork",
                report_id="2070001",
                grant_key=grant_key,
            )
            # Corrupt the signature.
            receipt = dict(receipt)
            receipt["signature"] = "0" * 64
            rec = self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="bad_sig",
                legacy_path_hint="x.md",
                authority_receipt=receipt,
            )
        self.assertFalse(rec.tenant_view_written)
        self.assertEqual(rec.tenant_view_deferred_reason, "authority_receipt_rejected")
        # But canonical + observation succeeded.
        self.assertRegex(rec.crosswalk_key, r"^cw_[a-z2-7]{26}$")
        self.assertRegex(rec.canonical_sha256, r"^[0-9a-f]{64}$")


class QuarantinePathTests(RT016TestBase):
    def test_quarantined_raw_writes_review_only(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw(create_time="never")
        run_id = self.new_run_id()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="quarantine",
            legacy_path_hint="q.md",
        )
        self.assertIn(rec.outcome, ("review", "undecomposable"))
        self.assertIsNotNone(rec.review_id)
        self.assertIsNone(rec.crosswalk_key)
        # No canonical was published.
        catalogs = list(
            (self.fx.root / "shared" / "report-versions").rglob("catalog.jsonl")
        )
        self.assertEqual(catalogs, [])
        # No grant was observed.
        al_dir = self.fx.root / "registry" / "access-ledger" / tenant_id
        if al_dir.exists():
            grants = list((al_dir / "grants").iterdir()) if (al_dir / "grants").exists() else []
            self.assertEqual(grants, [])
        # Review entry exists.
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        self.assertEqual(len(reviews), 1)
        self.assertIn("unparseable_timestamp", reviews[0]["quarantine_reasons"])


class DraftTenantRefusalTests(RT016TestBase):
    def test_draft_tenant_refuses_observation(self):
        # New tenant defaults to 'draft'; do NOT promote.
        tenant_id = self.fx.new_tenant(status="draft")
        raw = sample_raw()
        with self.assertRaises(R16.LegacyImportError) as cm:
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="draft refuse",
                legacy_path_hint="x.md",
            )
        self.assertEqual(cm.exception.code, "ledger_denied")


class BatchAndLegacySourceTests(RT016TestBase):
    def test_batch_import_verifies_zero_drift(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "2024-06/2070001.md": sample_raw(report_id="2070001"),
                    "2024-06/2070002.md": sample_raw(report_id="2070002", body="report 2"),
                },
            )
            source = R16.LegacySource(str(root))
            receipts = self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="batch",
            )
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(r.outcome == "complete" for r in receipts))

    def test_batch_drift_raises(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {"a.md": sample_raw(report_id="207A")},
            )
            source = R16.LegacySource(str(root))
            source.snapshot()
            # Simulate a rogue writer mutating the legacy tree.
            target = root / "a.md"
            target.write_bytes(sample_raw(report_id="207A", body="mutated"))
            with self.assertRaises(R16.LegacyDriftDetected):
                source.verify_no_drift()

    def test_legacy_source_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "real"
            target.mkdir()
            link = Path(td) / "link"
            link.symlink_to(target)
            with self.assertRaises(R16.LegacyImportError) as cm:
                R16.LegacySource(str(link))
            self.assertEqual(cm.exception.code, "path_containment")


class RecoveryTests(RT016TestBase):
    def test_recover_sweeps_orphans_and_reports(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="pre-recover",
            legacy_path_hint="x.md",
        )
        # Drop an orphan .cwk-tmp- into the crosswalks/ dir.
        cw_dir = self.fx.root / "registry" / R16.REGISTRY_SUBDIR / tenant_id / "crosswalks"
        orphan = cw_dir / ".cwk-tmp-orphan.orphan.xxxx"
        orphan.write_bytes(b"junk")
        report = self.fx.importer.recover(actor="admin", reason="orphan sweep")
        self.assertEqual(report.tenants_scanned, 1)
        self.assertEqual(report.crosswalk_orphans_removed, 1)
        self.assertFalse(orphan.exists())
        # Recovery does not affect the durable crosswalk.
        cw = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
        )
        self.assertEqual(cw["crosswalk_key"], rec.crosswalk_key)


class CrosswalkReadIntegrityTests(RT016TestBase):
    def test_read_crosswalk_detects_tamper(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="tamper",
            legacy_path_hint="x.md",
        )
        leaf = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "crosswalks"
            / f"{rec.crosswalk_key}.json"
        )
        original = leaf.read_bytes()
        # Tamper: change a byte in the middle of the file.
        payload = json.loads(original.decode("utf-8"))
        payload["record_revision"] = 999
        leaf.write_bytes(json.dumps(payload).encode("utf-8"))
        with self.assertRaises(R16.LegacyImportError) as cm:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
            )
        # Depending on the tamper it might be schema or corrupt.
        self.assertIn(cm.exception.code, ("corrupt", "contract"))


if __name__ == "__main__":
    unittest.main()
