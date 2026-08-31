"""RT-015: Access Ledger lifecycle, observation, promotion, lease, query.

Every test targets a specific requirement from PRD FR-08, DESIGN §5 C-08,
and the RT-015 rt-intake.  The tests are strictly black-box: they only
touch the public ``AccessLedger`` API and the on-disk layout described in
the RT-015 rt-intake.
"""

from __future__ import annotations

import copy
import datetime as _dt
import os
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_registry as TR  # noqa: E402
import _rt015_helpers as H  # noqa: E402


# ---------------------------------------------------------------------------
# Public surface locked down
# ---------------------------------------------------------------------------


class PublicSurfaceTests(unittest.TestCase):
    FORBIDDEN_METHODS = (
        "rollback", "reactivate", "unrevoke", "undo_revoke",
        "delete_grant", "delete_all", "purge_all",
        "list_all_tenants", "iter_all_grants", "enumerate",
        "has_report_id", "report_id_exists", "resolve_by_report_id",
    )

    def test_no_forbidden_methods(self):
        for name in self.FORBIDDEN_METHODS:
            self.assertFalse(
                hasattr(AL.AccessLedger, name),
                f"AccessLedger accidentally exposes forbidden {name!r}",
            )

    def test_no_cli_or_http(self):
        # RT-015 must not add any CLI/HTTP.  The module has no ``main``.
        self.assertFalse(hasattr(AL, "main"))
        self.assertFalse(hasattr(AL, "app"))
        self.assertFalse(hasattr(AL, "cli"))


# ---------------------------------------------------------------------------
# Observation → discovered / granted
# ---------------------------------------------------------------------------


class ObservationTests(H.LedgerTestBase):
    def test_first_observation_creates_granted(self):
        t = self.fx.new_tenant()
        obs = H.observation(tenant_id=t)
        rec = self.fx.ledger.observe(observation=obs, actor="admin", reason="first")
        self.assertEqual(rec.status, "granted")
        self.assertEqual(rec.tenant_id, t)
        self.assertEqual(rec.source_namespace, "cwork")
        self.assertEqual(rec.report_id, "2070001")
        self.assertEqual(rec.record_revision, 1)

    def test_first_observation_can_start_at_discovered(self):
        t = self.fx.new_tenant()
        obs = H.observation(tenant_id=t, initial_status="discovered")
        rec = self.fx.ledger.observe(observation=obs, actor="admin", reason="discovery")
        self.assertEqual(rec.status, "discovered")

    def test_observation_never_becomes_active(self):
        t = self.fx.new_tenant()
        for _ in range(3):
            self.fx.ledger.observe(
                observation=H.observation(tenant_id=t),
                actor="collector",
                reason="ingest",
            )
        # No matter how many observations we replay, status stays granted.
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )

    def test_discovered_then_granted_is_recorded(self):
        t = self.fx.new_tenant()
        rec1 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t, initial_status="discovered"),
            actor="collector",
            reason="probe",
        )
        self.assertEqual(rec1.status, "discovered")
        rec2 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t, initial_status="granted"),
            actor="collector",
            reason="upgrade",
        )
        self.assertEqual(rec2.status, "granted")
        self.assertEqual(rec2.record_revision, rec1.record_revision + 1)

    def test_reobserving_same_state_is_noop(self):
        t = self.fx.new_tenant()
        rec1 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="first",
        )
        rec2 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="second",
        )
        self.assertEqual(rec1.record_revision, rec2.record_revision)

    def test_role_change_bumps_revision(self):
        t = self.fx.new_tenant()
        rec1 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t, roles=["receiver"]),
            actor="collector",
            reason="first",
        )
        rec2 = self.fx.ledger.observe(
            observation=H.observation(tenant_id=t, roles=["receiver", "cc"]),
            actor="collector",
            reason="role change",
        )
        self.assertEqual(rec2.record_revision, rec1.record_revision + 1)
        self.assertEqual(sorted(rec2.grant["roles"]), ["cc", "receiver"])

    def test_observation_rejected_for_draft_tenant(self):
        t = self.fx.new_tenant(status="draft")
        with self.assertRaises(AL.GrantStateError):
            self.fx.ledger.observe(
                observation=H.observation(tenant_id=t),
                actor="collector",
                reason="try",
            )

    def test_observation_rejected_for_offboarded_tenant(self):
        t = self.fx.new_tenant(status="offboarded")
        with self.assertRaises(AL.GrantStateError):
            self.fx.ledger.observe(
                observation=H.observation(tenant_id=t),
                actor="collector",
                reason="try",
            )

    def test_observation_rejected_when_unknown_tenant(self):
        with self.assertRaises(AL.AccessLedgerError):
            self.fx.ledger.observe(
                observation=H.observation(tenant_id="t_" + "z" * 26),
                actor="collector",
                reason="try",
            )

    def test_observation_rejects_log_injection_actor(self):
        t = self.fx.new_tenant()
        for bad in ["admin\n", "admin\r", "admin\x00", "a\x1b[31m"]:
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.observe(
                    observation=H.observation(tenant_id=t),
                    actor=bad,
                    reason="reason",
                )

    def test_observation_rejects_log_injection_reason(self):
        t = self.fx.new_tenant()
        for bad in ["reason\n", "\x00"]:
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.observe(
                    observation=H.observation(tenant_id=t),
                    actor="admin",
                    reason=bad,
                )

    def test_observation_appends_state_event(self):
        t = self.fx.new_tenant()
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="first",
        )
        events = self.fx.ledger.iter_events(
            tenant_id=t, source_namespace="cwork", report_id="2070001"
        )
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["from_status"], "_initial_")
        self.assertEqual(e["to_status"], "granted")
        self.assertEqual(e["actor"], "collector")
        self.assertEqual(e["reason"], "first")
        self.assertEqual(e["tenant_auth_epoch_before"], 1)
        self.assertEqual(e["tenant_auth_epoch_after"], 1)
        self.assertEqual(e["record_revision_before"], 0)
        self.assertEqual(e["record_revision_after"], 1)


# ---------------------------------------------------------------------------
# Authority-driven promotion + lease refresh
# ---------------------------------------------------------------------------


class AuthorityTests(H.LedgerTestBase):
    def test_default_authority_is_fail_closed(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        # Without a registered fake authority, promotion fails closed.
        with H.FakeAuthorityContext() as auth:
            valid = auth.receipt(tenant_id=t)
        # We built a valid signed receipt inside the with-block but
        # the fake authority has been unregistered.  Attempt promote.
        with self.assertRaises(AL.AuthorityRejected):
            self.fx.ledger.promote_to_active(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                authority_receipt=valid,
                actor="admin",
                reason="promote",
            )

    def test_promotion_with_valid_receipt(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext() as auth:
            rec = self.fx.ledger.promote_to_active(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                authority_receipt=auth.receipt(tenant_id=t),
                actor="admin",
                reason="promote",
                lease_ttl_seconds=600,
            )
        self.assertEqual(rec.status, "active")
        self.assertIsNotNone(rec.lease_expires_at)

    def test_promotion_receipt_binding_mismatch(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext() as auth:
            r = auth.receipt(tenant_id=t)
            # Tamper: pretend receipt is for a different report.
            r["report_id"] = "9999"
            with self.assertRaises(AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )

    def test_promotion_signature_mismatch(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext() as auth:
            r = auth.receipt(tenant_id=t)
            r["signature"] = "0" * 64
            with self.assertRaises(AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )

    def test_promotion_unknown_signer(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext(signer_id="known") as auth:
            r = auth.receipt(tenant_id=t)
            r["signer_id"] = "unknown_signer"
            with self.assertRaises(AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )

    def test_promotion_receipt_type_mismatch(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext() as auth:
            r = auth.receipt(tenant_id=t, receipt_type="lease_refresh")
            with self.assertRaises(AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )

    def test_promotion_expired_receipt(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t),
            actor="collector",
            reason="ingest",
        )
        with H.FakeAuthorityContext() as auth:
            r = auth.receipt(tenant_id=t, lease_ttl_seconds=1)
            # Force lease_expires_at to yesterday.
            r["lease_expires_at"] = "2020-01-01T00:00:00Z"
            r_signed = auth.sign({k: v for k, v in r.items() if k != "signature"})
            with self.assertRaises(AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r_signed,
                    actor="admin",
                    reason="promote",
                )

    def test_refresh_lease_bumps_expiry(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            rec = self._grant_flow_to_active(tenant_id=t, signer=auth)
            first_expiry = rec.lease_expires_at
            r2 = auth.receipt(
                tenant_id=t, receipt_type="lease_refresh", lease_ttl_seconds=900
            )
            rec2 = self.fx.ledger.refresh_lease(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                authority_receipt=r2,
                actor="admin",
                reason="refresh",
                lease_ttl_seconds=900,
            )
            self.assertGreaterEqual(rec2.lease_expires_at, first_expiry)
            self.assertEqual(rec2.status, "active")

    def test_promotion_requires_granted_or_revalidation_due(self):
        t = self.fx.new_tenant()
        self.fx.publish(H.canonical_envelope())
        # No observation → GrantNotFound wrapped as GrantStateError path
        # Actually the internal read_grant_file raises GrantNotFound.
        with H.FakeAuthorityContext() as auth:
            r = auth.receipt(tenant_id=t)
            with self.assertRaises(AL.AccessLedgerError):
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )


class LeaseRevalidationTests(H.LedgerTestBase):
    def test_mark_revalidation_due_moves_to_revalidation_due(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            rec = self._grant_flow_to_active(tenant_id=t, signer=auth)
        rec2 = self.fx.ledger.mark_revalidation_due(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="scheduler",
            reason="lease expired",
        )
        self.assertEqual(rec2.status, "revalidation_due")

    def test_mark_revalidation_due_denies_query(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            self.fx.ledger.mark_revalidation_due(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="scheduler",
                reason="lease expired",
            )
            snap = self.fx.snapshot(t)
            with self.assertRaises(AL.AccessDenied):
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )

    def test_refresh_lease_can_recover_revalidation_due(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            self.fx.ledger.mark_revalidation_due(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                actor="scheduler",
                reason="lease expired",
            )
            r = auth.receipt(tenant_id=t, receipt_type="lease_refresh")
            rec = self.fx.ledger.refresh_lease(
                tenant_id=t,
                source_namespace="cwork",
                report_id="2070001",
                authority_receipt=r,
                actor="admin",
                reason="refresh",
            )
            self.assertEqual(rec.status, "active")


# ---------------------------------------------------------------------------
# check_query_eligibility & list_query_eligible
# ---------------------------------------------------------------------------


class QueryEligibilityTests(H.LedgerTestBase):
    def test_active_grant_with_fresh_lease_passes(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            rec = self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )
            self.assertEqual(rec.status, "active")

    def test_expired_lease_denied(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            future = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(seconds=99999)
            with self.assertRaises(AL.AccessDenied) as cm:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap,
                    source_namespace="cwork",
                    report_id="2070001",
                    now=future,
                )
            self.assertEqual(cm.exception.reason, "lease_expired")

    def test_no_grant_denied(self):
        t = self.fx.new_tenant()
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="never_seen"
            )

    def test_granted_but_not_active_denied(self):
        t = self.fx.new_tenant()
        self.fx.ledger.observe(
            observation=H.observation(tenant_id=t), actor="admin", reason="ingest"
        )
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )

    def test_stale_snapshot_denied(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            # Manually bump tenant.auth_epoch to simulate a concurrent
            # revocation.
            self.fx.tenants.bump_auth_epoch(
                t, actor="admin", reason="test", expected_auth_epoch=snap.tenant_auth_epoch
            )
            with self.assertRaises(AL.AccessDenied) as cm:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )
            self.assertEqual(cm.exception.reason, "stale_tenant_auth_epoch")

    def test_non_pilot_active_denied(self):
        t = self.fx.new_tenant(status="profile_pending")
        with H.FakeAuthorityContext() as auth:
            # Temporarily promote to allow promotion, then move back.
            H.promote_tenant(self.fx.layout, t, "active")
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            H.promote_tenant(self.fx.layout, t, "profile_pending")
            snap = self.fx.snapshot(t)
            with self.assertRaises(AL.AccessDenied) as cm:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )
            self.assertIn(
                cm.exception.reason, ("tenant_status", "live_tenant_status_drift")
            )

    def test_pilot_snapshot_allowed(self):
        t = self.fx.new_tenant(status="pilot")
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(t)
            rec = self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )
            self.assertEqual(rec.status, "active")

    def test_snapshot_status_and_live_status_must_agree(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
            snap = self.fx.snapshot(
                t, override_tenant_status="pilot"
            )  # live is active, snap says pilot
            with self.assertRaises(AL.AccessDenied) as cm:
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap, source_namespace="cwork", report_id="2070001"
                )
            self.assertEqual(cm.exception.reason, "live_tenant_status_drift")

    def test_bare_snapshot_type_rejected(self):
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot={"tenant_id": "t_" + "a" * 26},  # type: ignore
                source_namespace="cwork",
                report_id="foo",
            )

    def test_list_query_eligible(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth, report_id="1")
            self._grant_flow_to_active(tenant_id=t, signer=auth, report_id="2")
            # Add a granted-but-not-active obs.
            self.fx.publish(H.canonical_envelope(report_id="3"))
            self.fx.ledger.observe(
                observation=H.observation(tenant_id=t, report_id="3"),
                actor="admin",
                reason="ingest",
            )
            snap = self.fx.snapshot(t)
            eligible = self.fx.ledger.list_query_eligible(snapshot=snap)
            report_ids = sorted(r.report_id for r in eligible)
            self.assertEqual(report_ids, ["1", "2"])


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class IsolationTests(H.LedgerTestBase):
    def test_same_canonical_shared_but_grants_isolated(self):
        # Two tenants observe the same canonical version → shared object
        # count 1 in evidence store, but two separate grant records.
        a = self.fx.new_tenant()
        b = self.fx.new_tenant()
        env = H.canonical_envelope(report_id="shared_report_1")
        receipt_pub = self.fx.publish(env)
        # Publish the same envelope again → dedupe.
        receipt_pub2 = self.fx.publish(env)
        self.assertFalse(receipt_pub2.is_new_version)
        self.assertEqual(receipt_pub.object_id, receipt_pub2.object_id)

        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(
                tenant_id=a, signer=auth, report_id="shared_report_1",
                publish_canonical=False,
            )
            self._grant_flow_to_active(
                tenant_id=b, signer=auth, report_id="shared_report_1",
                publish_canonical=False,
            )
            snap_a = self.fx.snapshot(a)
            snap_b = self.fx.snapshot(b)
            self.assertEqual(
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap_a,
                    source_namespace="cwork",
                    report_id="shared_report_1",
                ).status,
                "active",
            )
            self.assertEqual(
                self.fx.ledger.check_query_eligibility(
                    snapshot=snap_b,
                    source_namespace="cwork",
                    report_id="shared_report_1",
                ).status,
                "active",
            )
            # Grant keys are distinct.
            self.assertNotEqual(
                AL.compute_grant_key(a, "cwork:shared_report_1"),
                AL.compute_grant_key(b, "cwork:shared_report_1"),
            )

    def test_snapshot_from_one_tenant_cannot_query_another(self):
        a = self.fx.new_tenant()
        b = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=b, signer=auth, report_id="onlyB")
        snap_a = self.fx.snapshot(a)
        with self.assertRaises(AL.AccessDenied):
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="onlyB"
            )

    def test_list_query_eligible_scoped_to_snapshot_tenant(self):
        a = self.fx.new_tenant()
        b = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=a, signer=auth, report_id="1a")
            self._grant_flow_to_active(tenant_id=b, signer=auth, report_id="1b")
            snap_a = self.fx.snapshot(a)
            eligible = self.fx.ledger.list_query_eligible(snapshot=snap_a)
            self.assertEqual([r.report_id for r in eligible], ["1a"])


if __name__ == "__main__":
    unittest.main()
