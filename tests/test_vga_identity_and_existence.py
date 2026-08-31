"""VG-A §6: identity distinctness and no cross-tenant existence side channels.

Scenario 6 (§8 of PR-001 plan):

- Same body under a different ``source_namespace`` or a different
  ``report_id`` must NOT be merged into the same identity.  (RT-014
  guarantees this at the canonical layer; we validate the composition
  by publishing near-identical envelopes with different report keys
  and asserting distinct object_ids / catalog_keys.)
- The revocation of a grant in A must not create any side-channel via
  which B could probe whether the *underlying report* exists for A
  (e.g. by observing timing / different error codes / directory
  listing).  RT-015 uses grant_key = ``H(tenant, report_key)`` so the
  A grant file's name is not derivable from B's snapshot; the errors
  raised on B's snapshot for an A-only report are the same
  ``AccessDenied`` / same ``__str__`` as any other missing grant.
- Unknown object side-channel: there is no ``has_object`` /
  ``exists_report_id`` / ``list_all_reports`` public method.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _vga_helpers as H  # noqa: E402


class IdentityDistinctnessTests(H.VgaTestBase):
    def test_same_body_different_report_id_yields_different_object(self) -> None:
        env1 = H.canonical_envelope(report_id="3080001", body="identical body α")
        env2 = H.canonical_envelope(report_id="3080002", body="identical body α")
        r1 = self.fx.publish(env1)
        r2 = self.fx.publish(env2)
        self.assertNotEqual(r1.catalog_key, r2.catalog_key)
        self.assertNotEqual(r1.object_id, r2.object_id)
        # The two canonical_sha256 differ because report_id / source_namespace
        # are part of the canonical envelope.
        self.assertNotEqual(r1.canonical_sha256, r2.canonical_sha256)

    def test_same_body_different_source_namespace_yields_different_object(self) -> None:
        env1 = H.canonical_envelope(
            source_namespace="cwork", report_id="3080001", body="identical body β"
        )
        env2 = H.canonical_envelope(
            source_namespace="peer_source", report_id="3080001", body="identical body β"
        )
        r1 = self.fx.publish(env1)
        r2 = self.fx.publish(env2)
        self.assertNotEqual(r1.catalog_key, r2.catalog_key)
        self.assertNotEqual(r1.object_id, r2.object_id)
        self.assertNotEqual(r1.canonical_sha256, r2.canonical_sha256)

    def test_grant_key_encodes_domain_tenant_report_key(self) -> None:
        # grant_key must be different across (namespace, report_id) tuples,
        # and different across tenants.
        gk_a_1 = H.AL.compute_grant_key(
            self.fx.a_id, H.C.compose_report_key("cwork", "3080001")
        )
        gk_a_2 = H.AL.compute_grant_key(
            self.fx.a_id, H.C.compose_report_key("cwork", "3080002")
        )
        gk_a_3 = H.AL.compute_grant_key(
            self.fx.a_id, H.C.compose_report_key("peer_source", "3080001")
        )
        gk_b_1 = H.AL.compute_grant_key(
            self.fx.b_id, H.C.compose_report_key("cwork", "3080001")
        )
        self.assertEqual(len({gk_a_1, gk_a_2, gk_a_3, gk_b_1}), 4)


class NoCrossTenantExistenceSideChannelTests(H.VgaTestBase):
    def test_b_snapshot_cannot_confirm_a_only_report_existence(self) -> None:
        # A alone has a grant on 3080001.
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        # B asks about 3080001 and about 9999999.  Both must raise the
        # same opaque AccessDenied / __str__.
        snap_b = self.fx.snapshot(self.fx.b_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx1:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_b, source_namespace="cwork", report_id="3080001"
            )
        with self.assertRaises(H.AL.AccessDenied) as ctx2:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_b, source_namespace="cwork", report_id="9999999"
            )
        self.assertEqual(str(ctx1.exception), str(ctx2.exception))
        # And neither error mentions the peer tenant.
        for ctx in (ctx1, ctx2):
            self.assertNotIn(self.fx.a_id, str(ctx.exception))
            self.assertNotIn(self.fx.b_id, str(ctx.exception))

    def test_b_cannot_probe_a_grant_key_via_public_api(self) -> None:
        # Even if the attacker computed A's grant_key offline, there is no
        # public API to convert grant_key → tenant.
        env = H.canonical_envelope(report_id="3080001")
        self.fx.publish(env)
        with H.SyntheticAuthorityContext() as auth:
            self.fx.promote_grant(tenant_id=self.fx.a_id, signer=auth)
        # Reflect the module to confirm no key→tenant probe exists.
        for name in (
            "resolve_grant_key",
            "grant_key_exists",
            "has_grant_key",
            "list_grant_keys",
        ):
            self.assertFalse(hasattr(H.AL.AccessLedger, name))
            self.assertFalse(hasattr(H.AL, name))

    def test_after_revoke_error_shape_unchanged(self) -> None:
        """A revoked report vs an unknown report vs a report belonging to
        the peer tenant must all render the same opaque
        :class:`AccessDenied`.
        """

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
            reason="off",
        )
        # A's revoked report → denied (tombstoned).
        snap_a = self.fx.snapshot(self.fx.a_id)
        with self.assertRaises(H.AL.AccessDenied) as ctx_revoke:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080001"
            )
        # A on a totally unknown report → denied (no_grant).
        with self.assertRaises(H.AL.AccessDenied) as ctx_missing:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="9999998"
            )
        # A trying to reach B's report → denied (no_grant).
        with self.assertRaises(H.AL.AccessDenied) as ctx_peer:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap_a, source_namespace="cwork", report_id="3080002"
            )
        for ctx in (ctx_revoke, ctx_missing, ctx_peer):
            self.assertEqual(str(ctx.exception), "[denied] access denied")

    def test_no_enumeration_apis_on_public_surface(self) -> None:
        # A defensive reflection check: neither AL.AccessLedger nor
        # TV.TenantViewStore exposes a public enumeration entry point.
        forbidden = (
            "list_all_tenants",
            "list_all_grants",
            "iter_all_grants",
            "resolve_by_report_id",
            "resolve_by_object_id",
            "has_report_id",
            "has_object",
            "object_exists",
            "list_reports",
            "list_all_reports",
        )
        for cls in (H.AL.AccessLedger, H.TV.TenantViewStore):
            for name in forbidden:
                self.assertFalse(
                    hasattr(cls, name), f"{cls.__name__} unexpectedly exposes {name}"
                )
        for name in forbidden:
            self.assertNotIn(name, H.AL.__all__)
            self.assertNotIn(name, H.TV.__all__)


if __name__ == "__main__":
    unittest.main()
