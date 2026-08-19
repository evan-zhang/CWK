"""RT-015: Tenant View Store overlay-only behaviour, double-ACL, purge.

Covers:

- Overlay-only invariant (no body copy, reply/node only carry IDs+SHA).
- Canonical linkage: upsert requires canonical to exist in the
  SharedEvidenceStore; read verifies canonical again.
- Double-ACL: read runs check_query_eligibility both before and after
  loading the overlay, so an in-flight revocation cannot leak.
- Cross-tenant isolation: A cannot upsert or read B's view even if the
  view envelope explicitly names B.
- Purge only after tombstone / revoked grant; idempotent.
- Temp URLs are permitted in overlay attachment_permissions but never
  leak into any other artefact.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_view as TV  # noqa: E402
import _rt015_helpers as H  # noqa: E402


def _view_envelope(
    *,
    tenant_id: str,
    source_namespace: str = "cwork",
    report_id: str = "2070001",
    canonical_sha256: str,
    include_attachment_temp_url: bool = False,
) -> dict:
    env = {
        "schema": "cwk.tenant_view.v1",
        "tenant_id": tenant_id,
        "report_key": C.compose_report_key(source_namespace, report_id),
        "canonical_sha256": canonical_sha256,
        "lane": "received",
        "read_status": "unread",
        "todo_status": "pending",
        "new_reply_flag": False,
        "roles": ["receiver"],
        "allowed_actions": ["read"],
        "visible_event_ids": [],
        "attachment_permissions": [],
        "reply_overlay": [
            {"reply_id": "r1", "content_sha256": "1" * 64, "visible": True}
        ],
        "node_overlay": [
            {"node_id": "n1", "type": "approval", "visible": True, "content_sha256": "2" * 64}
        ],
        "observed_at": H.utc_iso(),
    }
    if include_attachment_temp_url:
        env["attachment_permissions"] = [
            {
                "attachment_id": "a1",
                "permission": "view",
                "temporary_url": "https://presign.example/x?token=…",
                "expires_at": "2026-08-19T05:00:00Z",
            }
        ]
    return env


class OverlayContractTests(H.LedgerTestBase):
    def test_upsert_requires_active_grant(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        snap = self.fx.snapshot(t)
        view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
        with self.assertRaises(TV.ViewDenied):
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_upsert_requires_canonical_present(self):
        t = self.fx.new_tenant()
        # Don't publish canonical.
        with H.FakeAuthorityContext() as auth:
            # We need an active grant even to attempt the write.
            # Publish first, then remove object — but SharedEvidenceStore
            # doesn't allow removal.  Use a fake sha instead.
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256="0" * 64)
            with self.assertRaises(TV.CanonicalMissing):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_upsert_cross_tenant_rejected(self):
        a = self.fx.new_tenant()
        b = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=a, signer=auth, publish_canonical=False
            )
            snap_a = self.fx.snapshot(a)
            # Try to upsert a view naming tenant B while using A's
            # snapshot.  Must be denied.
            view = _view_envelope(tenant_id=b, canonical_sha256=env.canonical_sha256)
            with self.assertRaises(TV.ViewDenied):
                self.fx.view_store.upsert_overlay(
                    snapshot=snap_a, view_envelope=view
                )

    def test_upsert_then_read_returns_same_overlay(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            rec = self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
            self.assertEqual(rec.canonical_sha256, env.canonical_sha256)
            read = self.fx.view_store.read_view(
                snapshot=snap,
                source_namespace="cwork",
                report_id="2070001",
            )
            self.assertEqual(read.view["lane"], "received")
            self.assertEqual(read.view["reply_overlay"][0]["reply_id"], "r1")

    def test_upsert_body_leak_rejected_by_frozen_schema(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            view["body"] = "smuggled body"  # forbidden by RT-011 frozen tenant_view.v1
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_upsert_reply_full_payload_rejected(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            # Frozen v1 rejects unknown keys on reply items.
            view["reply_overlay"][0]["body"] = "smuggled"
            with self.assertRaises(Exception):
                self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)

    def test_temp_url_allowed_only_in_overlay_attachment_permissions(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(
                tenant_id=t,
                canonical_sha256=env.canonical_sha256,
                include_attachment_temp_url=True,
            )
            rec = self.fx.view_store.upsert_overlay(
                snapshot=snap, view_envelope=view
            )
            # Read back — url is preserved on the overlay.
            read = self.fx.view_store.read_view(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )
            att = read.view["attachment_permissions"][0]
            self.assertEqual(att["attachment_id"], "a1")
            self.assertTrue(att["temporary_url"].startswith("https://"))
            # …but the URL never leaks into canonical, grant record,
            # tombstone, or receipt.  Verify canonical bytes on disk
            # do not contain the URL.
            catalog_root = self.fx.root / "shared" / "canonical-events"
            for path in self.fx.root.rglob("*.json*"):
                if path.parts[-2:][0] == "views":
                    continue  # expected to contain URL
                if path.parent.name.startswith("tmp"):
                    continue
                if "views" in path.parts:
                    continue
                data = path.read_bytes()
                self.assertNotIn(b"presign.example", data,
                                 f"URL leaked into {path}")

    def test_view_does_not_copy_canonical_body(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope(body="secret body text"))
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
        # Any view file must NOT contain the canonical body text.
        views_dir = self.fx.root / "tenants" / t / "views"
        for path in views_dir.iterdir():
            data = path.read_bytes()
            self.assertNotIn(b"secret body text", data,
                             f"canonical body leaked into {path}")


class DoubleAclTests(H.LedgerTestBase):
    def test_read_view_denied_after_revoke(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
            # Now revoke.
            self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="off",
            )
            # Even with the stale snapshot: read fails closed.
            with self.assertRaises(TV.ViewDenied):
                self.fx.view_store.read_view(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )
            # Fresh snapshot: still denied (tombstoned).
            snap2 = self.fx.snapshot(t)
            with self.assertRaises(TV.ViewDenied):
                self.fx.view_store.read_view(
                    snapshot=snap2, source_namespace="cwork", report_id="2070001"
                )

    def test_read_view_denied_when_no_view(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            with self.assertRaises(TV.ViewNotFound):
                self.fx.view_store.read_view(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )


class PurgeTests(H.LedgerTestBase):
    def test_purge_only_after_revocation(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
        # Not revoked yet — purge is refused.
        with self.assertRaises(TV.TenantViewError):
            self.fx.view_store.purge_for_revoked_grant(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="cleanup",
                reason="illegal",
            )

    def test_purge_after_revocation_removes_view(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        receipt = self.fx.view_store.purge_for_revoked_grant(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="cleanup",
            reason="ack purge",
        )
        self.assertTrue(receipt.removed)
        # Idempotent.
        receipt2 = self.fx.view_store.purge_for_revoked_grant(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="cleanup",
            reason="ack purge again",
        )
        self.assertFalse(receipt2.removed)

    def test_purge_refused_for_active_grant(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
        # No revocation; purge refused.
        with self.assertRaises(TV.TenantViewError):
            self.fx.view_store.purge_for_revoked_grant(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="cleanup",
                reason="try",
            )

    def test_purge_rejects_log_injection(self):
        t = self.fx.new_tenant()
        env = self.fx.publish(H.canonical_envelope())
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=t, signer=auth, publish_canonical=False
            )
            snap = self.fx.snapshot(t)
            view = _view_envelope(tenant_id=t, canonical_sha256=env.canonical_sha256)
            self.fx.view_store.upsert_overlay(snapshot=snap, view_envelope=view)
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        with self.assertRaises(AL.LogInjectionDetected):
            self.fx.view_store.purge_for_revoked_grant(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="cleanup\n",
                reason="ok",
            )


if __name__ == "__main__":
    unittest.main()
