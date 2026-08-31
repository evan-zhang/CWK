"""VG-A §3: A revocation is immediate, isolated, and persists.

Scenario 3 (§8 of PR-001 plan): after tenant A is revoked:

- ``check_query_eligibility`` fails-closed immediately.
- The tenant ``auth_epoch`` on disk has been bumped.
- A ``tombstone`` file and a cleanup-outbox record are present.
- Any previously captured (stale) snapshot for A is denied.
- A fresh snapshot is still denied via the tombstone gate.
- The ``TenantViewStore`` cannot resurrect a stale overlay after revoke
  (double-ACL blocks the read and ``purge_for_revoked_grant`` removes
  the overlay).
- Simulated restart / recovery (``AccessLedger.recover``) does not
  restore the grant.
- Tenant B, whose grant is on a different opaque grant_key, remains
  fully queryable, its ``auth_epoch`` unchanged, and its view intact.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class RevocationIsolationTests(H.VgaTestBase):
    def _setup_two_active_grants(self) -> tuple[str, H.AC.AgentContextSnapshot, H.AC.AgentContextSnapshot]:
        env = H.canonical_envelope(report_id="3080001")
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
        snap_a = self.fx.snapshot(self.fx.a_id)
        snap_b = self.fx.snapshot(self.fx.b_id)
        return env["canonical_sha256"], snap_a, snap_b

    def test_revoke_makes_check_fail_closed_immediately(self) -> None:
        _, snap_a, snap_b = self._setup_two_active_grants()
        pre_epoch_a = self.fx.tenants.get(self.fx.a_id).auth_epoch
        pre_epoch_b = self.fx.tenants.get(self.fx.b_id).auth_epoch
        receipt = self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        # A's auth_epoch was bumped.
        post_epoch_a = self.fx.tenants.get(self.fx.a_id).auth_epoch
        self.assertEqual(post_epoch_a, pre_epoch_a + 1)
        self.assertEqual(receipt.tenant_auth_epoch_after, post_epoch_a)
        # Stale snapshot A → denied (stale epoch).
        with self.assertRaises(H.AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        # Fresh snapshot A → denied (tombstoned).
        fresh_snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx_fresh:
            self.fx.ledger.check_query_eligibility(
                snapshot=fresh_snap_a, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx_fresh.exception.reason, "tombstoned")
        # B's auth_epoch is unchanged.
        post_epoch_b = self.fx.tenants.get(self.fx.b_id).auth_epoch
        self.assertEqual(post_epoch_b, pre_epoch_b)
        # B's snapshot is still queryable.
        rec_b = self.fx.ledger.check_query_eligibility(
            snapshot=snap_b, source_namespace="cwork", report_id="3080001"
        )
        self.assertEqual(rec_b.status, "active")

    def test_tombstone_and_cleanup_outbox_written_for_a(self) -> None:
        self._setup_two_active_grants()
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        tomb = self.fx.ledger.read_tombstone(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
        )
        self.assertIsNotNone(tomb)
        outbox_tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.a_id)
        self.assertEqual(len(outbox_tasks), 1)
        consumers = set(outbox_tasks[0].consumers)
        self.assertEqual(consumers, {"tenant_view", "space_index", "cache"})
        # B has no tombstone or outbox tasks.
        tomb_b = self.fx.ledger.read_tombstone(
            tenant_id=self.fx.b_id,
            source_namespace="cwork",
            report_id="3080001",
        )
        self.assertIsNone(tomb_b)
        self.assertEqual(self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.b_id), [])

    def test_view_read_denied_after_revoke_double_acl(self) -> None:
        _, snap_a, snap_b = self._setup_two_active_grants()
        # Confirm both views can be read pre-revoke.
        self.fx.view_store.read_view(
            snapshot=snap_a, source_namespace="cwork", report_id="3080001"
        )
        self.fx.view_store.read_view(
            snapshot=snap_b, source_namespace="cwork", report_id="3080001"
        )
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        # Stale snapshot A: denied by first-and-second ACL.
        with self.assertRaises(H.TV.ViewDenied):
            self.fx.view_store.read_view(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        # Fresh A snapshot: still denied.
        fresh_snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.TV.ViewDenied):
            self.fx.view_store.read_view(
                snapshot=fresh_snap_a, source_namespace="cwork", report_id="3080001"
            )
        # B view still readable.
        rec = self.fx.view_store.read_view(
            snapshot=snap_b, source_namespace="cwork", report_id="3080001"
        )
        self.assertEqual(rec.view["lane"], "received")

    def test_purge_removes_a_overlay_only(self) -> None:
        canonical_sha, _, _ = self._setup_two_active_grants()
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        purge = self.fx.view_store.purge_for_revoked_grant(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="cleanup",
            reason="vga-cleanup",
        )
        self.assertTrue(purge.removed)
        # B's view file must remain.
        b_views = list((self.fx.root / "tenants" / self.fx.b_id / "views").iterdir())
        self.assertEqual(len(b_views), 1)
        # Idempotent second purge is a no-op.
        purge2 = self.fx.view_store.purge_for_revoked_grant(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="cleanup",
            reason="second call",
        )
        self.assertFalse(purge2.removed)
        # canonical_sha unchanged in canonical store.
        payload = self.fx.evidence.read_version(
            H.C.compose_report_key("cwork", "3080001"), canonical_sha
        )
        self.assertEqual(payload["canonical_sha256"], canonical_sha)

    def test_repeat_revoke_is_idempotent_and_yields_same_receipt(self) -> None:
        self._setup_two_active_grants()
        r1 = self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        r2 = self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="try again",
        )
        self.assertEqual(r1.txn_id, r2.txn_id)
        # auth_epoch is not double-bumped.
        pre = self.fx.tenants.get(self.fx.a_id).auth_epoch
        r3 = self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="third",
        )
        self.assertEqual(r3.tenant_auth_epoch_after, pre)

    def test_restart_recovery_does_not_resurrect_grant(self) -> None:
        """Simulate 'process restart' by constructing a fresh
        ``AccessLedger`` instance against the same on-disk state, running
        ``recover`` and confirming that the previously-revoked A grant
        is still denied and no observation can re-promote it.
        """

        self._setup_two_active_grants()
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="deprovision A",
        )
        # Fresh ledger against the same directories.
        fresh_ledger = H.AL.AccessLedger(self.fx.layout, self.fx.tenants, self.fx.evidence)
        report = fresh_ledger.recover(actor="vga-admin", reason="restart recovery")
        # Recovery must not report any inconsistencies for the healthy
        # committed revoke.
        self.assertEqual(report.inconsistencies, [])
        snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            fresh_ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "tombstoned")
        # Attempt to re-observe → tombstone gate rejects.
        obs = H.observation(tenant_id=self.fx.a_id, initial_status="granted")
        with self.assertRaises(H.AL.GrantStateError):
            fresh_ledger.observe(observation=obs, actor="vga-admin", reason="try re-observe")


class CleanupOutboxIsolationTests(H.VgaTestBase):
    def test_cleanup_outbox_only_visible_within_its_tenant(self) -> None:
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="A off",
        )
        a_tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.a_id)
        b_tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.b_id)
        self.assertEqual(len(a_tasks), 1)
        self.assertEqual(a_tasks[0].payload["tenant_id"], self.fx.a_id)
        self.assertEqual(b_tasks, [])

    def test_ack_cleanup_removes_task_after_last_consumer(self) -> None:
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="off",
        )
        [task] = self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.a_id)
        # First two consumers → task remains.
        self.assertFalse(
            self.fx.ledger.ack_cleanup_task(
                tenant_id=self.fx.a_id,
                outbox_id=task.outbox_id,
                consumer="tenant_view",
                actor="cleanup",
                reason="tv ack",
            )
        )
        self.assertFalse(
            self.fx.ledger.ack_cleanup_task(
                tenant_id=self.fx.a_id,
                outbox_id=task.outbox_id,
                consumer="space_index",
                actor="cleanup",
                reason="si ack",
            )
        )
        # Third (final) consumer → task removed.
        self.assertTrue(
            self.fx.ledger.ack_cleanup_task(
                tenant_id=self.fx.a_id,
                outbox_id=task.outbox_id,
                consumer="cache",
                actor="cleanup",
                reason="cache ack",
            )
        )
        self.assertEqual(self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.a_id), [])

    def test_ack_with_unknown_consumer_rejected(self) -> None:
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="off",
        )
        [task] = self.fx.ledger.iter_cleanup_outbox(tenant_id=self.fx.a_id)
        with self.assertRaises(H.AL.AccessLedgerError) as ctx:
            self.fx.ledger.ack_cleanup_task(
                tenant_id=self.fx.a_id,
                outbox_id=task.outbox_id,
                consumer="totally_unknown_consumer",
                actor="cleanup",
                reason="try",
            )
        self.assertEqual(ctx.exception.code, "contract")


if __name__ == "__main__":
    unittest.main()
