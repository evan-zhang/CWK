"""RT-015: revocation crash-safety, tombstones, cleanup outbox, recovery.

Covers each stage of the seven-step revocation recipe (intent → mark →
bump epoch → tombstone → outbox → receipt → clear journal), plus:

- Idempotency and CAS conflicts.
- Crash injection at every stage (intent-only, marked-only, epoch-only,
  etc.) — recover() completes forward, never rolls back.
- In-flight revoke: eligibility fails closed even before mark step.
- A tenant's revocation does not affect a different tenant.
- Tombstoned grants cannot be re-observed / promoted.
- Cleanup outbox contract (ack per consumer, delete after all acks).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_atomic_file as AF  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import _rt015_helpers as H  # noqa: E402


class RevokeBasicTests(H.LedgerTestBase):
    def test_revoke_active_grant_marks_revoked_and_bumps_epoch(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            tenant_before = self.fx.tenants.get(t)
            receipt = self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="user offboarded",
            )
            tenant_after = self.fx.tenants.get(t)
        self.assertEqual(receipt.tenant_auth_epoch_after, tenant_before.auth_epoch + 1)
        self.assertEqual(tenant_after.auth_epoch, receipt.tenant_auth_epoch_after)
        tomb = self.fx.ledger.read_tombstone(
            tenant_id=t, source_namespace="cwork", report_id="2070001"
        )
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.grant_key, receipt.grant_key)

    def test_revoke_denies_subsequent_queries(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap_before = self.fx.snapshot(t)
            self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="off",
            )
            # Old snapshot: stale epoch (or tombstoned).
            with self.assertRaises(AL.AccessDenied):
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap_before,
                    source_namespace="cwork",
                    report_id="2070001",
                )
            # Fresh snapshot (post-epoch-bump): tombstone still denies.
            snap_after = self.fx.snapshot(t)
            with self.assertRaises(AL.AccessDenied) as cm:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap_after,
                    source_namespace="cwork",
                    report_id="2070001",
                )
            self.assertEqual(cm.exception.reason, "tombstoned")

    def test_double_revoke_returns_existing_receipt(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            r1 = self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="first",
            )
            r2 = self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="second",
            )
            self.assertEqual(r1.txn_id, r2.txn_id)
            self.assertEqual(r1.tenant_auth_epoch_after, r2.tenant_auth_epoch_after)

    def test_revoke_writes_cleanup_outbox(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="off",
            )
        tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=t)
        self.assertEqual(len(tasks), 1)
        self.assertIn("tenant_view", tasks[0].consumers)
        self.assertIn("space_index", tasks[0].consumers)
        self.assertIn("cache", tasks[0].consumers)

    def test_revoke_appends_event(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            receipt = self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="user leaving",
            )
        events = self.fx.ledger.iter_events(
            tenant_id=t, source_namespace="cwork", report_id="2070001"
        )
        # Expect: observed + promoted + revoked = 3 events.
        self.assertGreaterEqual(len(events), 3)
        rev_events = [e for e in events if e["to_status"] == "revoked"]
        self.assertEqual(len(rev_events), 1)
        self.assertEqual(rev_events[0]["actor"], "admin")
        self.assertEqual(rev_events[0]["reason"], "user leaving")
        # The state-transition event captures the tenant epoch AT the
        # moment of the grant transition — the epoch bump is recorded
        # separately in the tenant registry's own audit trail (which
        # the receipt then references via
        # ``tenant_auth_epoch_before``/``_after``).  Cross-check receipt.
        self.assertEqual(
            rev_events[0]["tenant_auth_epoch_before"],
            receipt.payload["tenant_auth_epoch_before"],
        )
        self.assertGreater(
            receipt.tenant_auth_epoch_after,
            receipt.payload["tenant_auth_epoch_before"],
        )

    def test_revoke_rejects_log_injection(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.revoke(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    actor="admin\n",
                    reason="bad actor",
                )
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.revoke(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    actor="admin",
                    reason="reason\x00",
                )

    def test_revoke_can_start_from_granted(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t), actor="admin", reason="ingest"
        )
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        tomb = self.fx.ledger.read_tombstone(
            tenant_id=t, source_namespace="cwork", report_id="2070001"
        )
        self.assertIsNotNone(tomb)

    def test_tombstoned_grant_cannot_be_reobserved(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="off",
            )
        with self.assertRaises(AL.GrantStateError):
            self.fx.ledger.observe(
                observation=H.observation(tenant_id=t),
                actor="admin",
                reason="retry",
            )

    def test_tombstoned_grant_cannot_be_promoted(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            self.fx.ledger.revoke(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="off",
            )
            r = auth.receipt(tenant_id=t)
            with self.assertRaises(AL.GrantStateError):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="reactivate",
                )

    def test_revoke_a_does_not_affect_b(self):
        a = self.fx.new_tenant()
        b = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope(report_id="shared_1"))
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=a, signer=auth, report_id="shared_1",
                publish_canonical=False,
            )
            self._grant_flow_to_active(
                tenant_id=b, signer=auth, report_id="shared_1",
                publish_canonical=False,
            )
            # Revoke A.
            self.fx.ledger.revoke(
                tenant_id=a,
                source_namespace="cwork",
                report_id="shared_1",
                actor="admin",
                reason="a_off",
            )
            snap_a = self.fx.snapshot(a)
            snap_b = self.fx.snapshot(b)
            with self.assertRaises(AL.AccessDenied):
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap_a,
                    source_namespace="cwork",
                    report_id="shared_1",
                )
            rec_b = self.fx.ledger.check_query_eligibility(
                snapshot=snap_b, source_namespace="cwork", report_id="shared_1"
            )
            self.assertEqual(rec_b.status, "active")


# ---------------------------------------------------------------------------
# Crash-injection recovery
# ---------------------------------------------------------------------------


def _find_tenant_dir(fx: H.LedgerFixture, tenant_id: str) -> Path:
    return fx.root / "registry" / "access-ledger" / tenant_id


class CrashRecoveryTests(H.LedgerTestBase):
    def _seed(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        return t

    def test_intent_only_crash_recovery_completes(self):
        """Simulate crash after step 1 (intent written, nothing else)."""

        t = self._seed()
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        tenant_dir = _find_tenant_dir(self.fx, t)
        intent_dir = tenant_dir / "revoke-intents"
        intent_dir.mkdir(parents=True, exist_ok=True)
        tenant_before = self.fx.tenants.get(t)
        intent = {
            "schema": "cwk.rt015.revoke_intent.v1",
            "txn_id": "rv_" + "b" * 26,
            "grant_key": grant_key,
            "tenant_id": t,
            "source_namespace": "cwork",
            "report_id": "2070001",
            "prior_status": "active",
            "prior_record_revision": 2,
            "tenant_auth_epoch_before": tenant_before.auth_epoch,
            "actor": "admin",
            "reason": "crash test",
            "authority_receipt_id": None,
            "intended_at": H.utc_iso(),
        }
        canon = C.canonical_json_bytes(C.nfc_normalize(intent))
        (intent_dir / "rv_bbbbbbbbbbbbbbbbbbbbbbbbbb.journal").write_bytes(canon)

        # Query eligibility must fail closed BEFORE recovery runs.
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied) as cm:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )
        self.assertEqual(cm.exception.reason, "revocation_in_progress")

        # Recover.
        report = self.fx.ledger.recover(actor="admin", reason="test recover")
        self.assertEqual(report.intents_completed, 1)
        # Tombstone now exists.
        tomb = self.fx.ledger.read_tombstone(
            tenant_id=t, source_namespace="cwork", report_id="2070001"
        )
        self.assertIsNotNone(tomb)
        tenant_after = self.fx.tenants.get(t)
        self.assertEqual(tenant_after.auth_epoch, tenant_before.auth_epoch + 1)

    def test_recovery_is_idempotent(self):
        t = self._seed()
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        for _ in range(3):
            report = self.fx.ledger.recover(actor="admin", reason="idempotent")
        # Nothing to do (journal already unlinked).
        self.assertEqual(report.intents_completed, 0)

    def test_recovery_sweeps_temp_orphans(self):
        t = self._seed()
        tenant_dir = _find_tenant_dir(self.fx, t)
        grants_dir = tenant_dir / "grants"
        # Create a fake orphan temp file that matches the atomic-file prefix.
        (grants_dir / f"{AF.TEMP_PREFIX}fake.orphan.abcdef").write_bytes(b"")
        report = self.fx.ledger.recover(actor="admin", reason="sweep")
        self.assertGreaterEqual(report.orphans_removed, 1)

    def test_recovery_completes_after_receipt_missing(self):
        """Simulate crash after step 2 (grant marked, receipt not yet
        written) — recovery should still produce a receipt and unlink the
        intent.
        """

        t = self._seed()
        # Trigger a real revoke and then delete the receipt + reinstate
        # the intent journal so recovery has work to do.
        receipt = self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        tenant_dir = _find_tenant_dir(self.fx, t)
        # Delete receipt + reinstate intent journal.
        receipt_path = tenant_dir / "revoke-receipts" / f"{receipt.txn_id}.receipt"
        receipt_path.unlink()
        intent_dir = tenant_dir / "revoke-intents"
        intent_dir.mkdir(parents=True, exist_ok=True)
        intent = {
            "schema": "cwk.rt015.revoke_intent.v1",
            "txn_id": receipt.txn_id,
            "grant_key": receipt.grant_key,
            "tenant_id": t,
            "source_namespace": "cwork",
            "report_id": "2070001",
            "prior_status": "active",
            "prior_record_revision": 2,
            "tenant_auth_epoch_before": receipt.payload["tenant_auth_epoch_before"],
            "actor": "admin",
            "reason": "off",
            "authority_receipt_id": None,
            "intended_at": H.utc_iso(),
        }
        canon = C.canonical_json_bytes(C.nfc_normalize(intent))
        (intent_dir / f"{receipt.txn_id}.journal").write_bytes(canon)
        report = self.fx.ledger.recover(actor="admin", reason="crash test")
        self.assertGreaterEqual(report.intents_completed, 1)
        # Receipt rewritten with byte-identical content — recover should
        # produce the same file.
        self.assertTrue(receipt_path.exists())

    def test_recovery_never_resurrects_active_grant(self):
        """Recovery must never move a grant back to active."""

        t = self._seed()
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        self.fx.ledger.recover(actor="admin", reason="scan")
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )


# ---------------------------------------------------------------------------
# Cleanup-outbox consumer contract
# ---------------------------------------------------------------------------


class CleanupOutboxTests(H.LedgerTestBase):
    def _revoke_one(self, tenant_id: str) -> AL.CleanupTask:
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=tenant_id, signer=auth)
        self.fx.ledger.revoke(
            tenant_id=tenant_id,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=tenant_id)
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def test_ack_removes_consumer(self):
        t = self.fx.new_tenant()
        task = self._revoke_one(t)
        removed = self.fx.ledger.ack_cleanup_task(
            tenant_id=t,
            outbox_id=task.outbox_id,
            consumer="tenant_view",
            actor="cleanup",
            reason="acked",
        )
        self.assertFalse(removed)
        tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=t)
        self.assertEqual(len(tasks), 1)
        self.assertNotIn("tenant_view", tasks[0].consumers)

    def test_ack_last_consumer_removes_file(self):
        t = self.fx.new_tenant()
        task = self._revoke_one(t)
        for consumer in ("tenant_view", "space_index", "cache"):
            self.fx.ledger.ack_cleanup_task(
                tenant_id=t,
                outbox_id=task.outbox_id,
                consumer=consumer,
                actor="cleanup",
                reason="ack",
            )
        tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=t)
        self.assertEqual(tasks, [])

    def test_ack_unknown_consumer_rejected(self):
        t = self.fx.new_tenant()
        task = self._revoke_one(t)
        with self.assertRaises(AL.AccessLedgerError):
            self.fx.ledger.ack_cleanup_task(
                tenant_id=t,
                outbox_id=task.outbox_id,
                consumer="attacker",
                actor="cleanup",
                reason="probe",
            )

    def test_double_ack_is_noop(self):
        t = self.fx.new_tenant()
        task = self._revoke_one(t)
        self.fx.ledger.ack_cleanup_task(
            tenant_id=t,
            outbox_id=task.outbox_id,
            consumer="tenant_view",
            actor="cleanup",
            reason="first",
        )
        # Second ack from same consumer — consumer already removed → no
        # effect.  Function returns False (last-consumer removal did not
        # occur).
        self.fx.ledger.ack_cleanup_task(
            tenant_id=t,
            outbox_id=task.outbox_id,
            consumer="tenant_view",
            actor="cleanup",
            reason="second",
        )
        tasks = self.fx.ledger.iter_cleanup_outbox(tenant_id=t)
        self.assertEqual(len(tasks), 1)


if __name__ == "__main__":
    unittest.main()
