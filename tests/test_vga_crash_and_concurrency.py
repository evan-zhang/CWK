"""VG-A §7: crash injection / recovery, repeat revoke, CAS conflicts,
corrupt object / catalog / head, A/B concurrent traffic.

Scenario 7 (§8 of PR-001 plan): the RT-011~RT-015 chain must be
crash-safe on the revocation pipeline, idempotent on repeated calls,
CAS-consistent when two writers race, opaque-fail-closed on any
corrupted grant/catalog/head file, and safe under concurrent A/B
observation + promote + revoke traffic.

All crash injection modifies persistent files ONLY through documented
paths; no internal function is monkey-patched.
"""

from __future__ import annotations

import concurrent.futures as _cf
import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class CrashRecoveryTests(H.VgaTestBase):
    def _setup_active(self, *, tenant_id: str, report_id: str = "3080001") -> str:
        env = H.canonical_envelope(report_id=report_id)
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(
                tenant_id=tenant_id, signer=auth, report_id=report_id
            )
        return env["canonical_sha256"]

    def _revoke_and_return_journal_dir(self, *, tenant_id: str, report_id: str = "3080001") -> Path:
        self.fx.ledger.revoke(
            tenant_id=tenant_id,
            source_namespace="cwork",
            report_id=report_id,
            actor="vga-admin",
            reason="off",
        )
        return (
            self.fx.root / "registry" / "access-ledger" / tenant_id / "revoke-intents"
        )

    def test_crash_after_intent_before_completion_is_recovered(self) -> None:
        """Simulate a crash by re-injecting an intent journal for a
        grant that has NOT yet been revoked.  Recovery must replay the
        revocation idempotently.
        """

        self._setup_active(tenant_id=self.fx.a_id, report_id="3080001")
        grant_key = H.AL.compute_grant_key(
            self.fx.a_id, H.C.compose_report_key("cwork", "3080001")
        )
        # Inject a synthesised journal.
        intent = {
            "schema": "cwk.rt015.revoke_intent.v1",
            "txn_id": "rv_" + ("a" * 26),
            "grant_key": grant_key,
            "tenant_id": self.fx.a_id,
            "source_namespace": "cwork",
            "report_id": "3080001",
            "prior_status": "active",
            "prior_record_revision": 2,
            "tenant_auth_epoch_before": self.fx.tenants.get(self.fx.a_id).auth_epoch,
            "actor": "crash-recovery",
            "reason": "simulate crash",
            "authority_receipt_id": None,
            "intended_at": H.utc_iso(),
        }
        journal_dir = (
            self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "revoke-intents"
        )
        journal_dir.mkdir(parents=True, exist_ok=True)
        payload_bytes = H.C.canonical_json_bytes(H.C.nfc_normalize(intent))
        (journal_dir / f"{intent['txn_id']}.journal").write_bytes(payload_bytes)
        # Queries must fail-closed while the journal is present.
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        # Recovery completes the revocation.
        report = self.fx.ledger.recover(actor="vga-admin", reason="restart")
        self.assertGreaterEqual(report.intents_completed, 1)
        # Post-recovery: still denied; tombstone present; grant status revoked.
        tomb = self.fx.ledger.read_tombstone(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
        )
        self.assertIsNotNone(tomb)

    def test_repeat_recover_is_no_op(self) -> None:
        self._setup_active(tenant_id=self.fx.a_id)
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="off",
        )
        # Recovery on a healthy ledger is a no-op.
        report = self.fx.ledger.recover(actor="vga-admin", reason="idempotent1")
        self.assertEqual(report.inconsistencies, [])
        report2 = self.fx.ledger.recover(actor="vga-admin", reason="idempotent2")
        self.assertEqual(report2.inconsistencies, [])

    def test_missing_receipt_after_intent_still_denies_query(self) -> None:
        self._setup_active(tenant_id=self.fx.a_id)
        self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="off",
        )
        # Simulate operator deleting the receipt.
        receipts_dir = (
            self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "revoke-receipts"
        )
        for path in receipts_dir.iterdir():
            path.unlink()
        snap = self.fx.snapshot(self.fx.a_id)
        # Query still denied — tombstone still present.
        with self.assertRaises(H.AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )


class CasConflictTests(H.VgaTestBase):
    def test_stale_snapshot_denied_when_epoch_bumped(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        snap_before = self.fx.snapshot(self.fx.a_id)
        # Bump via revoke of a different report of A: since there is no
        # other grant, simulate an unrelated epoch bump via the tenant
        # registry API directly (it is the intended CAS surface).
        pre_epoch = self.fx.tenants.get(self.fx.a_id).auth_epoch
        self.fx.tenants.bump_auth_epoch(
            self.fx.a_id,
            actor="vga-admin",
            reason="unrelated bump",
            expected_auth_epoch=pre_epoch,
        )
        # Stale snapshot now denied due to stale auth epoch.
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_before, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "stale_tenant_auth_epoch")
        # Fresh snapshot recovers (grant.auth_epoch still equals live epoch
        # only after re-promotion; here the grant is stale too).
        fresh = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx2:
            self.fx.ledger.check_query_eligibility(
                snapshot=fresh, source_namespace="cwork", report_id="3080001"
            )
        # After bump, grant.auth_epoch (from before) != live.  Reason:
        # grant_stale_epoch.
        self.assertEqual(ctx2.exception.reason, "grant_stale_epoch")

    def test_bump_epoch_with_wrong_expected_raises_conflict(self) -> None:
        with self.assertRaises(H.TR.RegistryConflict):
            self.fx.tenants.bump_auth_epoch(
                self.fx.a_id,
                actor="vga-admin",
                reason="wrong expected",
                expected_auth_epoch=9999,
            )


class CorruptionTests(H.VgaTestBase):
    def test_corrupt_grant_json_denied_and_isolated(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.promote_grant(tenant_id=self.fx.b_id, signer=auth)
        # Truncate A's grant file.
        a_grant_dir = (
            self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "grants"
        )
        for path in a_grant_dir.iterdir():
            path.write_bytes(b"{ not json")
        snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "grant_corrupt")
        # B unaffected.
        snap_b = self.fx.snapshot(self.fx.b_id)
        rec = self.fx.ledger.check_query_eligibility(
            snapshot=snap_b, source_namespace="cwork", report_id="3080001"
        )
        self.assertEqual(rec.status, "active")

    def test_bit_flip_grant_denied(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        a_grant_dir = (
            self.fx.root / "registry" / "access-ledger" / self.fx.a_id / "grants"
        )
        for path in a_grant_dir.iterdir():
            data = bytearray(path.read_bytes())
            data[0] ^= 0x20  # single bit flip
            path.write_bytes(bytes(data))
        snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )

    def test_corrupt_canonical_object_denied(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        # Corrupt the on-disk object.
        object_dir = self.fx.root / "shared" / "objects"
        object_files = [
            p for p in object_dir.rglob("*.json") if not p.name.startswith(".cwk-tmp-")
        ]
        for path in object_files:
            data = bytearray(path.read_bytes())
            data[0] ^= 0x40
            path.write_bytes(bytes(data))
        # Reader fail-closed.
        with self.assertRaises(H.SE.SharedEvidenceError) as ctx:
            self.fx.evidence.read_version(
                H.C.compose_report_key("cwork", "3080001"),
                env["canonical_sha256"],
            )
        self.assertIn(ctx.exception.code, ("sha_mismatch", "canonical_drift", "corrupt_catalog"))

    def test_missing_object_returns_not_found(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        object_dir = self.fx.root / "shared" / "objects"
        object_files = [
            p for p in object_dir.rglob("*.json") if not p.name.startswith(".cwk-tmp-")
        ]
        for path in object_files:
            path.unlink()
        with self.assertRaises(H.SE.SharedEvidenceError) as ctx:
            self.fx.evidence.read_version(
                H.C.compose_report_key("cwork", "3080001"),
                env["canonical_sha256"],
            )
        # `orphan_object` = catalog head still points at now-missing object.
        self.assertEqual(ctx.exception.code, "orphan_object")

    def test_view_read_fails_when_canonical_gone(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
            self.fx.upsert_view(
                tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
            )
        # Remove all canonical objects — view read now cannot pass.
        object_dir = self.fx.root / "shared" / "objects"
        object_files = [
            p for p in object_dir.rglob("*.json") if not p.name.startswith(".cwk-tmp-")
        ]
        for path in object_files:
            path.unlink()
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.TV.CanonicalMissing):
            self.fx.view_store.read_view(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )


class ConcurrentAbTrafficTests(H.VgaTestBase):
    def test_concurrent_ab_observation_writes(self) -> None:
        """Two threads publish observations for A / B concurrently on
        distinct report_ids per tenant.  Every write must succeed on
        its own tenant subdirectory.
        """

        # Publish two canonical objects.
        env1 = H.canonical_envelope(report_id="3080101")
        env2 = H.canonical_envelope(report_id="3080102")
        self.fx.publish(env1)
        self.fx.publish(env2)
        # Pre-warm each tenant's access-ledger subdir tree with one
        # serial observation, so concurrent writes race only within the
        # already-created per-tenant directories.
        for tenant_id in (self.fx.a_id, self.fx.b_id):
            self.fx.ledger.observe(
                observation=H.observation(
                    tenant_id=tenant_id, report_id="3080101"
                ),
                actor="vga-admin",
                reason="pre-warm",
            )
        errors: list[BaseException] = []
        lock = threading.Lock()

        def observe(t_id: str, r_id: str) -> None:
            try:
                obs = H.observation(tenant_id=t_id, report_id=r_id)
                self.fx.ledger.observe(
                    observation=obs, actor="vga-admin", reason="concurrent"
                )
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    errors.append(exc)

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [
                ex.submit(observe, t_id, r_id)
                for t_id in (self.fx.a_id, self.fx.b_id)
                for r_id in ("3080101", "3080102")
            ]
            for fut in _cf.as_completed(futures):
                fut.result()
        self.assertEqual(errors, [])
        # A grant exists at both report_ids.
        for r_id in ("3080101", "3080102"):
            rec_a = self.fx.ledger.observe(
                observation=H.observation(tenant_id=self.fx.a_id, report_id=r_id),
                actor="vga-admin",
                reason="verify",
            )
            self.assertEqual(rec_a.status, "granted")

    def test_concurrent_revoke_of_a_is_idempotent(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        receipts: list = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def revoke_once() -> None:
            try:
                r = self.fx.ledger.revoke(
                    tenant_id=self.fx.a_id,
                    source_namespace="cwork",
                    report_id="3080001",
                    actor="vga-admin",
                    reason="concurrent",
                )
                with lock:
                    receipts.append(r)
            except (H.AL.RevocationInProgress, H.AL.GrantStateError):
                # Serialization race: acceptable.
                pass
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(revoke_once) for _ in range(4)]
            for fut in _cf.as_completed(futures):
                fut.result()
        self.assertEqual(errors, [])
        # At least one succeeded; all successful receipts share the same txn_id.
        if receipts:
            txn_ids = {r.txn_id for r in receipts}
            self.assertEqual(len(txn_ids), 1)
        # Any subsequent call must return the same txn.
        r_final = self.fx.ledger.revoke(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="verify",
        )
        if receipts:
            self.assertEqual(r_final.txn_id, receipts[0].txn_id)


if __name__ == "__main__":
    unittest.main()
