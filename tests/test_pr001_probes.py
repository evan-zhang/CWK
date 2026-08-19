"""RT-011 (post-remediation): capability probe skeleton tests.

Covers Blocker #2:

- Default result is always ``conservative_unknown``.
- ``verified`` requires a well-formed :class:`ReceiptEnvelope` signed by a
  signer on the frozen :data:`TRUSTED_PROBE_SIGNERS` allowlist and
  targeting this specific probe.
- Trusted signer allowlist is empty in RT-011; tests inject a signer via
  ``_register_test_probe_signer`` and always unregister on tear-down.
- The permanently-forbidden probe (loopback + self-reported agent_id)
  never emits ``verified`` even with a valid receipt.
- ``aggregate`` requires every frozen probe_id to be present exactly once
  before ``all_verified`` can even be considered; a subset input always
  reports ``all_verified=false`` with ``missing_probe_ids`` populated.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
FIX = PROJECT / "tests" / "fixtures" / "pr001"
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_pr001_probes as P  # noqa: E402


class _WithTestSigner:
    """Context manager that registers a test signer for the duration."""

    def __init__(self, signer_id: str = "test_signer") -> None:
        self.signer_id = signer_id
        self.secret = b"test-secret-for-rt011-tests"

    def __enter__(self):
        C._register_test_probe_signer(self.signer_id, self.secret)
        return self

    def __exit__(self, exc_type, exc, tb):
        C._unregister_test_probe_signer(self.signer_id)


class ProbeSkeletonTests(unittest.TestCase):
    def test_default_matrix_all_conservative(self):
        payloads = P.run_default_matrix()
        self.assertEqual(len(payloads), 9)
        for payload in payloads:
            self.assertEqual(payload["result"], "conservative_unknown")
            self.assertIsNone(payload["receipt"])

    def test_default_aggregate_all_verified_false(self):
        agg = P.aggregate(P.run_default_matrix())
        self.assertFalse(agg["all_verified"])
        self.assertTrue(agg["complete"])
        self.assertIn("sandbox_transport_loopback_http_self_reported", agg["policy_forbidden_probe_ids"])

    def test_verified_requires_registered_signer(self):
        pid = "sandbox_transport_openclaw_tool"
        with _WithTestSigner() as w:
            receipt = P.build_receipt(
                probe_id=pid,
                signer=w.signer_id,
                environment="gateway_production",
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-12-31T00:00:00Z",
                payload_body={"kind": "test-evidence"},
            )
            payload = P.run_probe(pid, receipt=receipt)
            self.assertEqual(payload["result"], "verified")

    def test_forbidden_probe_never_upgrades_even_with_receipt(self):
        pid = "sandbox_transport_loopback_http_self_reported"
        with _WithTestSigner() as w:
            receipt = P.build_receipt(
                probe_id=pid,
                signer=w.signer_id,
                environment="gateway_production",
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-12-31T00:00:00Z",
                payload_body={"kind": "evidence"},
            )
            payload = P.run_probe(pid, receipt=receipt)
            self.assertEqual(payload["result"], "conservative_unknown")

    def test_receipt_from_untrusted_signer_rejected(self):
        pid = "sandbox_transport_openclaw_tool"
        # Craft receipt directly without registering the signer.
        receipt = P.ReceiptEnvelope(
            probe_id=pid,
            signer="attacker",
            envelope_sha256="a" * 64,
            signature="b" * 64,
            target=pid,
            environment="gateway_production",
            not_before="2026-01-01T00:00:00Z",
            not_after="2027-12-31T00:00:00Z",
        )
        with self.assertRaises(C.ContractError):
            P.run_probe(pid, receipt=receipt)

    def test_receipt_wrong_target_rejected(self):
        pid = "sandbox_transport_openclaw_tool"
        with _WithTestSigner() as w:
            other = P.build_receipt(
                probe_id="sandbox_transport_uds",  # wrong target
                signer=w.signer_id,
                environment="gateway_production",
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-12-31T00:00:00Z",
                payload_body={"kind": "evidence"},
            )
            with self.assertRaises(C.ContractError):
                P.run_probe(pid, receipt=other)

    def test_receipt_signature_mismatch_rejected(self):
        pid = "sandbox_transport_openclaw_tool"
        with _WithTestSigner() as w:
            good = P.build_receipt(
                probe_id=pid,
                signer=w.signer_id,
                environment="gateway_production",
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-12-31T00:00:00Z",
                payload_body={"kind": "evidence"},
            )
            tampered = P.ReceiptEnvelope(
                probe_id=pid,
                signer=w.signer_id,
                envelope_sha256=good.envelope_sha256,
                signature="0" * 64,  # tampered
                target=pid,
                environment="gateway_production",
                not_before=good.not_before,
                not_after=good.not_after,
            )
            with self.assertRaises(C.ContractError):
                P.run_probe(pid, receipt=tampered)

    def test_receipt_outside_window_becomes_conservative(self):
        pid = "sandbox_transport_openclaw_tool"
        with _WithTestSigner() as w:
            expired = P.build_receipt(
                probe_id=pid,
                signer=w.signer_id,
                environment="gateway_production",
                not_before="2020-01-01T00:00:00Z",
                not_after="2021-01-01T00:00:00Z",
                payload_body={"kind": "old"},
            )
            payload = P.run_probe(pid, receipt=expired)
            self.assertEqual(payload["result"], "conservative_unknown")

    def test_aggregate_incomplete_forces_all_verified_false(self):
        # Include only 3 probes.  Even if they were all "verified",
        # aggregate must remain all_verified=false because the set is
        # incomplete.
        pids = P.ALL_PROBE_IDS[:3]
        with _WithTestSigner() as w:
            probes = []
            for pid in pids:
                if pid in P.POLICY_FORBIDDEN_UPGRADE:
                    probes.append(P.run_probe(pid))
                else:
                    r = P.build_receipt(
                        probe_id=pid,
                        signer=w.signer_id,
                        environment="gateway_production",
                        not_before="2026-01-01T00:00:00Z",
                        not_after="2027-12-31T00:00:00Z",
                        payload_body={"kind": "evidence"},
                    )
                    probes.append(P.run_probe(pid, receipt=r))
            agg = P.aggregate(probes)
            self.assertFalse(agg["all_verified"])
            self.assertFalse(agg["complete"])
            self.assertGreater(len(agg["missing_probe_ids"]), 0)

    def test_aggregate_complete_verifies_only_when_forbidden_is_conservative(self):
        with _WithTestSigner() as w:
            probes = []
            for pid in P.ALL_PROBE_IDS:
                if pid in P.POLICY_FORBIDDEN_UPGRADE:
                    probes.append(P.run_probe(pid))
                else:
                    r = P.build_receipt(
                        probe_id=pid,
                        signer=w.signer_id,
                        environment="gateway_production",
                        not_before="2026-01-01T00:00:00Z",
                        not_after="2027-12-31T00:00:00Z",
                        payload_body={"kind": "evidence"},
                    )
                    probes.append(P.run_probe(pid, receipt=r))
            agg = P.aggregate(probes)
            self.assertTrue(agg["all_verified"])
            self.assertTrue(agg["complete"])

    def test_aggregate_rejects_duplicate_probe_ids(self):
        p1 = P.run_probe("sandbox_transport_openclaw_tool")
        p2 = P.run_probe("sandbox_transport_openclaw_tool")
        with self.assertRaises(C.ContractError):
            P.aggregate([p1, p2])

    def test_aggregate_rejects_malicious_forged_probes(self):
        fixture = FIX / "malicious_probe_forged_verified.json"
        payloads = C.strict_json_load_path(fixture)
        with self.assertRaises(C.ContractError):
            P.aggregate(payloads)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
