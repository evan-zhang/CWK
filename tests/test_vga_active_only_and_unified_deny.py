"""VG-A §2: only active+valid-lease can read; unified deny for unknowns.

Scenario 2 (§8 of PR-001 plan): the ledger's `check_query_eligibility`
is the sole gate.  Every non-active grant status, expired lease,
missing grant, unknown tenant, unknown report, and unknown object must
raise the module's uniform :class:`AccessDenied` — with an opaque
``__str__`` that never distinguishes which of {grant existence,
tombstone, lease, epoch} triggered the denial.
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class ActiveOnlyEligibilityTests(H.VgaTestBase):
    def _publish_and_promote(self, *, report_id: str = "3080001") -> str:
        env = H.canonical_envelope(report_id=report_id)
        self.fx.publish(env)
        return env["canonical_sha256"]

    def test_discovered_grant_is_never_queryable(self) -> None:
        sha = self._publish_and_promote()
        obs = H.observation(
            tenant_id=self.fx.a_id, initial_status="discovered"
        )
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(str(ctx.exception), "[denied] access denied")
        self.assertEqual(ctx.exception.reason, "not_active")
        del sha  # published but unused after obs

    def test_granted_grant_is_never_queryable(self) -> None:
        self._publish_and_promote()
        obs = H.observation(
            tenant_id=self.fx.a_id, initial_status="granted"
        )
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "not_active")

    def test_revalidation_due_grant_is_never_queryable(self) -> None:
        self._publish_and_promote()
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        # Manually downgrade to revalidation_due.
        self.fx.ledger.mark_revalidation_due(
            tenant_id=self.fx.a_id,
            source_namespace="cwork",
            report_id="3080001",
            actor="vga-admin",
            reason="lease expired",
        )
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "not_active")

    def test_expired_lease_denied(self) -> None:
        self._publish_and_promote()
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(
                tenant_id=self.fx.a_id, signer=auth, lease_ttl_seconds=60
            )
        snap = self.fx.snapshot(self.fx.a_id)
        # Query with `now` set to well past the lease horizon.
        future = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(hours=2)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001", now=future
            )
        self.assertEqual(ctx.exception.reason, "lease_expired")

    def test_active_short_lease_allowed(self) -> None:
        self._publish_and_promote()
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(
                tenant_id=self.fx.a_id, signer=auth, lease_ttl_seconds=600
            )
        snap = self.fx.snapshot(self.fx.a_id)
        rec = self.fx.ledger.check_query_eligibility(
            snapshot=snap, source_namespace="cwork", report_id="3080001"
        )
        self.assertEqual(rec.status, "active")


class UnifiedDenyTests(H.VgaTestBase):
    """Every "unknown" surface fails-closed with the same opaque error."""

    def test_unknown_tenant_snapshot_denied(self) -> None:
        # Construct a snapshot pointing to a tenant that RT-012 has never
        # heard of (uses a legal opaque tenant_id shape).
        fake_tenant = "t_" + "b" * 26
        snap = H.AC.AgentContextSnapshot(
            agent_id_hash="v" * 64,
            tenant_id=fake_tenant,
            tenant_auth_epoch=1,
            binding_epoch=1,
            binding_secret_epoch=1,
            tenant_status="active",
            resolved_at=H.utc_iso(),
        )
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        # Opaque string, non-existence not distinguishable from active-but-denied.
        self.assertEqual(str(ctx.exception), "[denied] access denied")

    def test_unknown_report_id_denied_same_shape(self) -> None:
        # Same tenant, active, but no grant for report `9999999`.
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx_missing:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="9999999"
            )
        # Same __str__ as any other denial.
        self.assertEqual(str(ctx_missing.exception), "[denied] access denied")

    def test_unknown_object_via_shared_evidence_read_version(self) -> None:
        """Reading an unknown object never confirms existence.

        A B-side attacker with an active snapshot for a *different*
        report cannot use ``read_version`` to prove that A's
        canonical exists — the store returns opaque errors and no
        cross-tenant enumeration surface exists.
        """

        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        # Attacker crafts an unpublished sha and asks read_version.
        with self.assertRaises(H.SE.SharedEvidenceError) as ctx:
            self.fx.evidence.read_version(
                H.C.compose_report_key("cwork", "3080001"), "f" * 64
            )
        # Stable code, opaque message.
        self.assertEqual(ctx.exception.code, "not_found")

    def test_unknown_report_key_denied_at_shared_evidence(self) -> None:
        # Reading a completely unknown report_key returns not_found.
        with self.assertRaises(H.SE.SharedEvidenceError) as ctx:
            self.fx.evidence.read_version(
                H.C.compose_report_key("cwork", "unknown0000"), "e" * 64
            )
        self.assertEqual(ctx.exception.code, "not_found")

    def test_unified_deny_shape_across_scenarios(self) -> None:
        """Emit denials from three unrelated failure paths and confirm
        every :class:`AccessDenied` renders to exactly the same string.
        """

        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        snaps = []
        # 1) unknown tenant.
        snaps.append(
            H.AC.AgentContextSnapshot(
                agent_id_hash="v" * 64,
                tenant_id="t_" + "c" * 26,
                tenant_auth_epoch=1,
                binding_epoch=1,
                binding_secret_epoch=1,
                tenant_status="active",
                resolved_at=H.utc_iso(),
            )
        )
        # 2) known tenant, no grant.
        snaps.append(self.fx.snapshot(self.fx.a_id))
        # 3) known tenant, discovered-only grant.
        obs = H.observation(tenant_id=self.fx.b_id, initial_status="discovered")
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        snaps.append(self.fx.snapshot(self.fx.b_id))
        strings = set()
        for snap in snaps:
            with self.assertRaises(H.AL.AccessDenied) as ctx:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap, source_namespace="cwork", report_id="3080001"
                )
            strings.add(str(ctx.exception))
        # All three denials render to the exact same string.
        self.assertEqual(strings, {"[denied] access denied"})


class SnapshotAndStatusGateTests(H.VgaTestBase):
    def test_bare_dict_snapshot_denied(self) -> None:
        # `check_query_eligibility` must refuse non-snapshot inputs — this is
        # the fail-closed contract per RT-015.
        with self.assertRaises(H.AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot={"tenant_id": self.fx.a_id},  # type: ignore[arg-type]
                source_namespace="cwork",
                report_id="3080001",
            )

    def test_suspended_tenant_snapshot_denied(self) -> None:
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        # Suspend tenant via test-only helper; the snapshot must still
        # be denied even though the grant is live and active on disk.
        H.promote_tenant_status(self.fx.layout, self.fx.a_id, "suspended")
        snap = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="3080001"
            )
        self.assertEqual(ctx.exception.reason, "tenant_status")


if __name__ == "__main__":
    unittest.main()
