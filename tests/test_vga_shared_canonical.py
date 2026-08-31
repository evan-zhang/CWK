"""VG-A §1: shared canonical, isolated grants / views / receipts / errors / paths.

Scenario 1 (§8 of PR-001 plan): the same canonical evidence object can be
shared by tenants A and B — but every grant, tenant view, revocation
receipt, error surface, and on-disk path stays strictly isolated.

Nothing here mutates RT-011~RT-015 modules, schemas, tests, or docs.
Everything runs against synthesised tenants inside a per-test
``tempfile.TemporaryDirectory``.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class SharedCanonicalIsolationTests(H.VgaTestBase):
    """Same canonical serves A and B; everything else is isolated."""

    def test_two_tenants_share_single_canonical_object(self) -> None:
        """A and B publish the exact same canonical envelope; only one
        object exists on disk.  The publish receipts still bind to the
        same ``canonical_sha256`` (RT-014 idempotent identity).
        """

        env = H.canonical_envelope(report_id="3080001", body="shared body δ")
        r1 = self.fx.publish(env)
        r2 = self.fx.publish(env)
        # Idempotent publish returns the same identity.
        self.assertEqual(r1.canonical_sha256, r2.canonical_sha256)
        self.assertEqual(r1.object_id, r2.object_id)
        self.assertEqual(r1.catalog_key, r2.catalog_key)
        self.assertFalse(r2.is_new_version)
        # Verify the physical object count is 1.
        object_dir = self.fx.root / "shared" / "objects"
        object_files = [p for p in object_dir.rglob("*.json") if not p.name.startswith(".cwk-tmp-")]
        self.assertEqual(len(object_files), 1)

    def test_a_and_b_grants_are_isolated_by_grant_key(self) -> None:
        """Same report → different opaque ``grant_key`` per tenant.

        Because ``grant_key = H(GRANT_KEY_DOMAIN, tenant_id,
        report_key)``, A's grant_key is not derivable from B's.
        """

        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            ga = self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            gb = self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        self.assertNotEqual(ga.grant_key, gb.grant_key)
        # But both refer to the same canonical envelope.
        vr_a = self.fx.evidence.read_version(
            H.C.compose_report_key("cwork", "3080001"), env["canonical_sha256"]
        )
        vr_b = self.fx.evidence.read_version(
            H.C.compose_report_key("cwork", "3080001"), env["canonical_sha256"]
        )
        # `read_version` returns the canonical envelope; the two reads must
        # yield byte-identical canonical bytes.
        self.assertEqual(
            H.C.canonical_json_bytes(H.C.nfc_normalize(vr_a)),
            H.C.canonical_json_bytes(H.C.nfc_normalize(vr_b)),
        )
        self.assertEqual(vr_a["canonical_sha256"], vr_b["canonical_sha256"])

    def test_view_upserts_are_stored_under_separate_tenant_directories(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
            self.fx.upsert_view(
                tenant_id=self.fx.b_id, canonical_sha256=env["canonical_sha256"]
            )
        a_views = list((self.fx.root / "tenants" / self.fx.a_id / "views").iterdir())
        b_views = list((self.fx.root / "tenants" / self.fx.b_id / "views").iterdir())
        self.assertEqual(len(a_views), 1)
        self.assertEqual(len(b_views), 1)
        # File names must differ (both use grant_key = H(tenant, report_key)).
        self.assertNotEqual(a_views[0].name, b_views[0].name)
        # Neither tenant directory contains the other's file bytes.
        a_bytes = a_views[0].read_bytes()
        b_bytes = b_views[0].read_bytes()
        self.assertIn(self.fx.a_id.encode(), a_bytes)
        self.assertNotIn(self.fx.b_id.encode(), a_bytes)
        self.assertIn(self.fx.b_id.encode(), b_bytes)
        self.assertNotIn(self.fx.a_id.encode(), b_bytes)

    def test_a_and_b_grant_files_stored_under_separate_ledger_dirs(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        ledger_root = self.fx.root / "registry" / "access-ledger"
        a_grants = list((ledger_root / self.fx.a_id / "grants").iterdir())
        b_grants = list((ledger_root / self.fx.b_id / "grants").iterdir())
        self.assertEqual(len(a_grants), 1)
        self.assertEqual(len(b_grants), 1)
        # A and B use different grant_key file names.
        self.assertNotEqual(a_grants[0].name, b_grants[0].name)
        # There is no shared/global grant directory.
        self.assertFalse((ledger_root / "grants").exists())

    def test_revoke_receipt_paths_are_isolated_per_tenant(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        # Only revoke A.
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="isolate A revoke",
        )
        a_receipts = list(
            (self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "revoke-receipts").iterdir()
        )
        b_receipts_dir = self.fx.root / "registry" / "access-ledger" / self.fx.b_id / "revoke-receipts"
        b_receipts = list(b_receipts_dir.iterdir()) if b_receipts_dir.exists() else []
        self.assertEqual(len(a_receipts), 1)
        self.assertEqual(len(b_receipts), 0)
        a_tomb = list(
            (self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "tombstones").iterdir()
        )
        b_tomb_dir = self.fx.root / "registry" / "access-ledger" / self.fx.b_id / "tombstones"
        b_tomb = list(b_tomb_dir.iterdir()) if b_tomb_dir.exists() else []
        self.assertEqual(len(a_tomb), 1)
        self.assertEqual(len(b_tomb), 0)

    def test_error_surface_does_not_leak_cross_tenant_ids(self) -> None:
        """Errors observed by callers do not include the ``tenant_id`` of
        peer tenants in message strings.  ``AccessDenied.__str__`` is
        deliberately opaque; unknown-tenant fails-closed use a stable
        code without echoing tenant IDs.
        """

        # Snapshot for A, but query with A's snapshot for B's grant → snapshot's
        # tenant_id is A; report has no grant for A.
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        # `.reason` may exist for callers, but `__str__` is opaque.
        self.assertEqual(str(ctx.exception), "[denied] access denied")
        self.assertNotIn(self.fx.b_id, str(ctx.exception))
        self.assertNotIn(self.fx.a_id, str(ctx.exception))


class ViewIsolationTests(H.VgaTestBase):
    def test_a_view_upsert_does_not_create_b_side_files(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
        # B has no view directory content.
        b_views_dir = self.fx.root / "tenants" / self.fx.b_id / "views"
        self.assertTrue(b_views_dir.exists())
        self.assertEqual(len(list(b_views_dir.iterdir())), 0)

    def test_snapshot_of_b_cannot_read_a_view(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
        # B snapshot: no grant → ViewDenied (fail-closed) not ViewNotFound.
        snap_b = self.fx.snapshot(self.fx.b_id)
        with self.assertRaises(H.TV.ViewDenied):
            self.fx.view_store.read_view(
                snapshot=snap_b, source_namespace="cwork", report_id="3080001"
            )

    def test_upserting_view_with_peer_tenant_id_is_denied(self) -> None:
        """A snapshot cannot upsert a view envelope tagged with B's tenant_id."""

        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        snap_a = self.fx.snapshot(self.fx.a_id)
        # Impersonation attempt: A snapshot writes a B-labelled envelope.
        env_view = H.view_envelope(
            tenant_id=self.fx.b_id, canonical_sha256=env["canonical_sha256"]
        )
        with self.assertRaises(H.TV.ViewDenied):
            self.fx.view_store.upsert_overlay(snapshot=snap_a, view_envelope=env_view)


if __name__ == "__main__":
    unittest.main()
