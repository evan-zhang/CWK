"""RT-011: dual-user tenant-view comparator tests."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_pr001_view_compare as V  # noqa: E402


FIX = PROJECT / "tests" / "fixtures" / "pr001"


class ComparatorTests(unittest.TestCase):
    def test_comparator_refuses_appkey_input(self):
        with self.assertRaises(C.ContractError):
            V.load_envelope_set(str(FIX / "malicious_tenant_a_appkey.json"))

    def test_dual_tenant_common_visibility_and_overlay_diffs(self):
        a = V.load_envelope_set(str(FIX / "tenant_a_views.json"))
        b = V.load_envelope_set(str(FIX / "tenant_b_views.json"))
        report = V.compare(a, b)

        # 55 shared, 3 A-only, 2 B-only per fixture generator.
        self.assertEqual(report["sample_sizes"]["common"], 55)
        self.assertEqual(len(report["only_in_tenant_a"]), 3)
        self.assertEqual(len(report["only_in_tenant_b"]), 2)

        # A has extra admin replies for the first five common reports.
        self.assertGreaterEqual(report["overlay_differences"]["reply"], 5)
        # B has temporary URLs; comparator must flag their presence.
        self.assertTrue(report["overlay_differences"]["temporary_url_seen"])

        # Ensure no URL/identity path was suggested for promotion.
        for entry in report["suggested_verified_shared"]:
            self.assertNotIn("url", entry["path"].lower())
            self.assertNotIn("credential", entry["path"].lower())
            self.assertNotIn("tenant_id", entry["path"])
            self.assertNotIn("agent_id", entry["path"])

    def test_comparator_rejects_same_tenant_twice(self):
        a = V.load_envelope_set(str(FIX / "tenant_a_views.json"))
        with self.assertRaises(C.ContractError):
            V.compare(a, a)

    def test_below_50_common_samples_never_upgraded(self):
        """If fewer than 50 common samples are observed, every candidate stays in overlay."""

        a_full = V.load_envelope_set(str(FIX / "tenant_a_views.json"))
        b_full = V.load_envelope_set(str(FIX / "tenant_b_views.json"))
        # Truncate: keep first 10 envelopes of each.
        a_small = V.TenantEnvelopeSet(
            tenant_id=a_full.tenant_id, envelopes=a_full.envelopes[:10]
        )
        b_small = V.TenantEnvelopeSet(
            tenant_id=b_full.tenant_id, envelopes=b_full.envelopes[:10]
        )
        report = V.compare(a_small, b_small)
        self.assertLess(report["sample_sizes"]["common"], 50)
        self.assertEqual(report["suggested_verified_shared"], [])
        for entry in report["suggested_tenant_overlay"]:
            self.assertEqual(entry["recommendation"], "keep_in_tenant_overlay")

    def test_canonical_sha_mismatch_flagged(self):
        """If tenant A and B see different canonical_sha256 for the same report_key,
        that must appear in ``canonical_sha256_mismatches``."""

        a = V.load_envelope_set(str(FIX / "tenant_a_views.json"))
        b = V.load_envelope_set(str(FIX / "tenant_b_views.json"))
        # Tamper: change one canonical_sha256 in B.
        for env in b.envelopes:
            if env["report_key"] == a.envelopes[0]["report_key"]:
                env["canonical_sha256"] = "f" * 64
                break
        report = V.compare(a, b)
        self.assertTrue(report["canonical_sha256_mismatches"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
