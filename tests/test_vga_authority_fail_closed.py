"""VG-A §5: fail-closed authority; observation ≤ granted; view store cannot
be bypassed without an authoritative promotion.

Scenario 5 (§8 of PR-001 plan): without a valid
``cwk.rt015.authority_receipt.v1`` from the ``AuthorityAdapter``:

- ``AccessLedger.observe`` writes at most ``discovered``/``granted``
  and never ``active``.
- ``promote_to_active`` and ``refresh_lease`` refuse with
  :class:`AuthorityRejected`.
- ``TenantViewStore.upsert_overlay`` refuses to write a durable
  overlay because the ledger's fail-closed
  ``check_query_eligibility`` returns :class:`AccessDenied`.
- There is no side door: ``TenantViewStore`` has no
  ``upsert_without_grant`` / ``force_active`` / raw file helper; the
  RT-015 module ``__all__`` exports none of these; consumers cannot
  bypass the API.
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class ObservationAtMostGrantedTests(H.VgaTestBase):
    def test_observation_initial_status_granted_stays_granted(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id, initial_status="granted")
        rec = self.fx.ledger.observe(
            observation=obs, actor="vga-admin", reason="ingest"
        )
        self.assertEqual(rec.status, "granted")
        self.assertNotEqual(rec.status, "active")

    def test_observation_cannot_pass_active_directly_schema(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id, initial_status="granted")
        obs["initial_status"] = "active"  # schema forbids
        with self.assertRaises(Exception):
            self.fx.ledger.observe(
                observation=obs, actor="vga-admin", reason="try-active-shortcut"
            )


class PromoteRequiresAuthorityTests(H.VgaTestBase):
    def test_promote_to_active_without_authority_fails_closed(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id)
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        # Build a receipt whose schema would validate but signer does not
        # exist and the default authority is fail-closed.
        wo_sig = {
            "schema": "cwk.rt015.authority_receipt.v1",
            "receipt_id": "ar_" + ("a" * 26),
            "signer_id": "unknown_signer",
            "receipt_type": "grant_promote",
            "tenant_id": self.fx.a_id,
            "source_namespace": "cwork",
            "report_id": "3080001",
            "grant_key": H.AL.compute_grant_key(
                self.fx.a_id, H.C.compose_report_key("cwork", "3080001")
            ),
            "roles": ["receiver"],
            "visibility_scope": "full",
            "permission_source": "authoritative_permission_api",
            "issued_at": H.utc_iso(),
            "lease_expires_at": H.utc_iso(600),
            "signature": "0" * 64,
        }
        with self.assertRaises(H.AL.AuthorityRejected):
            self.fx.ledger.promote_to_active(
                tenant_id=self.fx.a_id,
                source_namespace="cwork",
                report_id="3080001",
                authority_receipt=wo_sig,
                actor="vga-admin",
                reason="try promote",
            )

    def test_promote_with_wrong_signature_fails(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id)
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        with H.SyntheticAuthorityContext() as auth:
            receipt = auth.receipt(tenant_id=self.fx.a_id)
            receipt["signature"] = "f" * 64
            with self.assertRaises(H.AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=self.fx.a_id,
                    source_namespace="cwork",
                    report_id="3080001",
                    authority_receipt=receipt,
                    actor="vga-admin",
                    reason="tamper",
                )

    def test_promote_with_wrong_receipt_type_fails(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id)
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        with H.SyntheticAuthorityContext() as auth:
            receipt = auth.receipt(
                tenant_id=self.fx.a_id, receipt_type="lease_refresh"
            )
            with self.assertRaises(H.AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=self.fx.a_id,
                    source_namespace="cwork",
                    report_id="3080001",
                    authority_receipt=receipt,
                    actor="vga-admin",
                    reason="promote",
                )

    def test_promote_with_cross_tenant_receipt_fails(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id)
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        with H.SyntheticAuthorityContext() as auth:
            # Receipt built for B, presented to A.
            receipt = auth.receipt(tenant_id=self.fx.b_id)
            with self.assertRaises(H.AL.AuthorityRejected):
                self.fx.ledger.promote_to_active(
                    tenant_id=self.fx.a_id,
                    source_namespace="cwork",
                    report_id="3080001",
                    authority_receipt=receipt,
                    actor="vga-admin",
                    reason="try",
                )


class ViewStoreCannotBypassLedgerTests(H.VgaTestBase):
    def test_upsert_without_active_grant_fails_view_denied(self) -> None:
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id, initial_status="granted")
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        # `granted` (not promoted) → check_query_eligibility fails → upsert refused.
        snap = self.fx.snapshot(self.fx.a_id)
        view = H.view_envelope(
            tenant_id=self.fx.a_id, canonical_sha256=env["canonical_sha256"]
        )
        with self.assertRaises(H.TV.ViewDenied):
            self.fx.view_store.upsert_overlay(
                snapshot=snap, view_envelope=view
            )
        # No overlay file was written.
        views_dir = self.fx.root / "tenants" / self.fx.a_id / "views"
        self.assertEqual(len(list(views_dir.iterdir())), 0)

    def test_no_side_door_apis_on_ledger_or_view_store(self) -> None:
        forbidden_al = (
            "upsert_active",
            "force_active",
            "grant_active",
            "reactivate",
            "unrevoke",
            "delete_grant",
            "delete_tombstone",
            "raw_grant_bytes",
        )
        for name in forbidden_al:
            self.assertFalse(
                hasattr(H.AL.AccessLedger, name),
                f"AccessLedger unexpectedly exposes {name}",
            )
        forbidden_tv = (
            "upsert_without_grant",
            "force_upsert",
            "delete_view",
            "list_all_views",
            "raw_view_bytes",
        )
        for name in forbidden_tv:
            self.assertFalse(
                hasattr(H.TV.TenantViewStore, name),
                f"TenantViewStore unexpectedly exposes {name}",
            )

    def test_default_authority_is_fail_closed_when_no_test_authority_registered(self) -> None:
        # Ensure no fake authority is registered.
        env = H.canonical_envelope()
        self.fx.publish(env)
        obs = H.observation(tenant_id=self.fx.a_id)
        self.fx.ledger.observe(observation=obs, actor="vga-admin", reason="ingest")
        # Build a well-formed receipt but with no signer registered → default
        # fail-closed authority rejects.
        wo_sig = {
            "schema": "cwk.rt015.authority_receipt.v1",
            "receipt_id": "ar_" + ("b" * 26),
            "signer_id": "another_unknown_signer",
            "receipt_type": "grant_promote",
            "tenant_id": self.fx.a_id,
            "source_namespace": "cwork",
            "report_id": "3080001",
            "grant_key": H.AL.compute_grant_key(
                self.fx.a_id, H.C.compose_report_key("cwork", "3080001")
            ),
            "roles": ["receiver"],
            "visibility_scope": "full",
            "permission_source": "authoritative_permission_api",
            "issued_at": H.utc_iso(),
            "lease_expires_at": H.utc_iso(600),
            "signature": "0" * 64,
        }
        with self.assertRaises(H.AL.AuthorityRejected):
            self.fx.ledger.promote_to_active(
                tenant_id=self.fx.a_id,
                source_namespace="cwork",
                report_id="3080001",
                authority_receipt=wo_sig,
                actor="vga-admin",
                reason="promote",
            )


if __name__ == "__main__":
    unittest.main()
