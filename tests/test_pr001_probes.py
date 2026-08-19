"""RT-011: capability probe skeleton tests.

Confirms:

- All nine probe IDs are exercised.
- Default result is ``conservative_unknown``.
- Only ``controlled_environment_receipt`` evidence promotes to ``verified``.
- Fixture / mock evidence NEVER promotes; ``fixture://`` refs never promote.
- ``sandbox_transport_loopback_http_self_reported`` cannot be promoted at all.
- ``verified_shared_extensions_dual_user_sample`` requires
  ``unique_report_key_pairs >= 50`` before promotion.
- Aggregation returns ``all_verified=false`` whenever any probe is
  conservative; the malicious fixture is rejected outright.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_pr001_probes as P  # noqa: E402


class ProbeSkeletonTests(unittest.TestCase):
    def test_default_matrix_all_conservative(self):
        payloads = P.run_default_matrix()
        self.assertEqual(len(payloads), 9)
        for payload in payloads:
            self.assertEqual(payload["result"], "conservative_unknown")
            C.validate_capability_probe(payload)
        agg = P.aggregate(payloads)
        self.assertFalse(agg["all_verified"])
        self.assertEqual(agg["verified"], [])
        self.assertEqual(agg["conservative_unknown"], sorted(P.ALL_PROBE_IDS))
        # Policy-forbidden probes remain conservative even in aggregate.
        self.assertIn("sandbox_transport_loopback_http_self_reported", agg["policy_forbidden_probe_ids"])

    def test_authoritative_receipt_upgrades_non_forbidden(self):
        payload = P.run_probe(
            "sandbox_transport_openclaw_tool",
            evidence=P.EvidenceBundle(
                kind="controlled_environment_receipt",
                refs=("openclaw://gateway/receipt/2026-08-19",),
                notes="RT-023 real-gateway receipt",
            ),
        )
        self.assertEqual(payload["result"], "verified")
        self.assertGreater(len(payload["evidence_refs"]), 0)

    def test_forbidden_probe_cannot_upgrade(self):
        payload = P.run_probe(
            "sandbox_transport_loopback_http_self_reported",
            evidence=P.EvidenceBundle(
                kind="controlled_environment_receipt",
                refs=("openclaw://gateway/receipt",),
            ),
        )
        self.assertEqual(payload["result"], "conservative_unknown")

    def test_fixture_evidence_cannot_upgrade(self):
        for kind in ("fixture", "mock", "assertion", "documentation"):
            payload = P.run_probe(
                "trusted_agent_identity_openclaw_tool",
                evidence=P.EvidenceBundle(kind=kind, refs=(f"{kind}://a.json",)),
            )
            self.assertEqual(payload["result"], "conservative_unknown", msg=kind)

    def test_fixture_ref_prefix_blocks_upgrade_even_if_kind_is_receipt(self):
        payload = P.run_probe(
            "sandbox_transport_openclaw_tool",
            evidence=P.EvidenceBundle(
                kind="controlled_environment_receipt",
                refs=("fixture://spoof.json",),
            ),
        )
        self.assertEqual(payload["result"], "conservative_unknown")

    def test_verified_shared_extensions_requires_50_common_pairs(self):
        under = P.EvidenceBundle(
            kind="controlled_environment_receipt",
            refs=("openclaw://gateway/receipt",),
            unique_report_key_pairs=49,
        )
        self.assertEqual(
            P.run_probe("verified_shared_extensions_dual_user_sample", evidence=under)["result"],
            "conservative_unknown",
        )
        exactly = P.EvidenceBundle(
            kind="controlled_environment_receipt",
            refs=("openclaw://gateway/receipt",),
            unique_report_key_pairs=50,
        )
        self.assertEqual(
            P.run_probe("verified_shared_extensions_dual_user_sample", evidence=exactly)["result"],
            "verified",
        )

    def test_aggregate_rejects_malicious_forged_probes(self):
        fixture = PROJECT / "tests" / "fixtures" / "pr001" / "malicious_probe_forged_verified.json"
        with fixture.open("r", encoding="utf-8") as fh:
            payloads = json.load(fh)
        with self.assertRaises(C.ContractError):
            P.aggregate(payloads)

    def test_aggregate_rejects_duplicate_probe_ids(self):
        pid = "sandbox_transport_openclaw_tool"
        p1 = P.run_probe(pid)
        p2 = P.run_probe(pid)
        with self.assertRaises(C.ContractError):
            P.aggregate([p1, p2])

    def test_all_probe_ids_are_covered_by_conservative_defaults(self):
        self.assertEqual(set(P.CONSERVATIVE_DEFAULTS.keys()), set(P.ALL_PROBE_IDS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
