"""RT-016 MigrationReconciler behaviour tests."""

from __future__ import annotations

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
    C,
    R16,
    RT016TestBase,
    build_legacy_tree,
    default_anchor,
    sample_raw,
    utc_iso,
)


class HappyPathReconcileTests(RT016TestBase):
    def test_both_equal_full_coverage(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "2024-06/2070001.md": sample_raw(report_id="2070001"),
                    "2024-06/2070002.md": sample_raw(report_id="2070002", body="b2"),
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
                reason="reconcile happy",
            )
            # For reconciliation we need a fresh LegacySource anchored
            # on the same root (batch already verified drift once).
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=run_id,
                source=source2,
            )
        self.assertEqual(report.both_equal_count, 2)
        self.assertEqual(report.only_legacy_count, 0)
        self.assertEqual(report.only_new_count, 0)
        self.assertEqual(report.new_undecomposable_count, 0)
        self.assertTrue(report.zero_drift_verified)
        self.assertAlmostEqual(
            report.payload["legacy_source_hash_verify_rate"], 1.0
        )
        self.assertAlmostEqual(
            report.payload["canonical_hash_verify_rate"], 1.0
        )

    def test_two_legacy_files_same_canonical_still_both_equal(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(source_lane="inbox"),
                    "b.md": sample_raw(source_lane="outbox"),
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
                reason="overlay diff",
            )
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=run_id,
                source=source2,
            )
        self.assertEqual(report.both_equal_count, 2)
        self.assertEqual(report.only_legacy_count, 0)
        self.assertEqual(report.only_new_count, 0)


class ClassificationTests(RT016TestBase):
    def test_only_legacy_uncovered(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {"only-legacy.md": sample_raw(report_id="207X")},
            )
            source = R16.LegacySource(str(root))
            source.snapshot()
            # Reconcile without importing anything: legacy file has no
            # matching crosswalk.
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source,
            )
        self.assertEqual(report.only_legacy_count, 1)
        self.assertEqual(report.both_equal_count, 0)
        # Also emitted as failure sample.
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("only_legacy", classes)

    def test_only_new_when_legacy_missing(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        # Import into an intermediate tree, then reconcile against an
        # empty tree — the crosswalk exists but no legacy file matches.
        with tempfile.TemporaryDirectory() as td1:
            root1 = build_legacy_tree(
                Path(td1),
                {"x.md": sample_raw()},
            )
            source1 = R16.LegacySource(str(root1))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source1,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="import",
            )
        with tempfile.TemporaryDirectory() as td2:
            empty_root = Path(td2) / "empty"
            empty_root.mkdir()
            source2 = R16.LegacySource(str(empty_root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        self.assertEqual(report.only_new_count, 1)
        self.assertEqual(report.only_legacy_count, 0)
        self.assertEqual(report.both_equal_count, 0)

    def test_new_undecomposable_review_entries_reported(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw(create_time="never")
        run_id = self.new_run_id()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="quar",
            legacy_path_hint="q.md",
        )
        with tempfile.TemporaryDirectory() as td:
            # Reconcile with the same raw present in a legacy tree.
            root = build_legacy_tree(Path(td), {"q.md": raw})
            source = R16.LegacySource(str(root))
            source.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=run_id,
                source=source,
            )
        self.assertEqual(report.new_undecomposable_count, 1)
        # Undecomposable raws are counted as review entries, NOT
        # only_legacy.
        self.assertEqual(report.only_legacy_count, 0)

    def test_zero_drift_flag_flips_on_mutation(self):
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
                reason="import",
            )
            # New LegacySource capturing the tree as-is.
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            # Now mutate the legacy tree.
            (root / "a.md").write_bytes(sample_raw(body="mutated"))
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=run_id,
                source=source2,
            )
        self.assertFalse(report.zero_drift_verified)


class ReconciliationCorruptionDetectionTests(RT016TestBase):
    def test_canonical_missing_after_shared_delete(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="import",
            legacy_path_hint="a.md",
        )
        # Nuke the canonical object.  RT-014 would then raise not_found
        # on read_version; reconciler must surface this.
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
        # RT-014 catalog still shows the entry but the object is gone;
        # reconciler classifies as canonical_missing, canonical_sha_mismatch
        # or object_bytes_sha_mismatch depending on the specific RT-014
        # error surfaced.  All three are legitimate failure modes for
        # the reconciler to flag — the only invariant is that they show
        # up in failure_samples.
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertTrue(
            classes
            & {
                "canonical_missing",
                "canonical_sha_mismatch",
                "object_bytes_sha_mismatch",
            },
            f"expected a canonical-level failure, saw {classes}",
        )
        # Reflected in only_new_count (crosswalk exists, canonical unusable).
        self.assertEqual(report.only_new_count, 1)
        self.assertEqual(report.both_equal_count, 0)


class EmptyTenantTests(RT016TestBase):
    def test_empty_tenant_yields_rate_of_1(self):
        tenant_id = self.fx.new_tenant(status="pilot")
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
        self.assertEqual(report.both_equal_count, 0)
        self.assertEqual(report.only_legacy_count, 0)
        self.assertEqual(report.only_new_count, 0)
        # Rates 1.0 when both denominators are 0 (division-by-zero guard).
        self.assertEqual(report.payload["legacy_source_hash_verify_rate"], 1.0)
        self.assertEqual(report.payload["canonical_hash_verify_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
