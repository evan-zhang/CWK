"""RT-011 (post-remediation): dual-user comparator tests.

Covers Blocker #3:

- No tenant overlay field is ever candidate for verified_shared upgrade.
- Threshold floor at 0.99: threshold=0 must fail.
- Coverage: 49/50 unique common report_keys must not upgrade; 50/50 with
  100% match_rate and 100% presence can.
- Duplicate report_key in a tenant set fails at load time.
- Conflicting canonical_sha256 between tenants blocks upgrade globally.
- Nested temporary_url in attachment_permissions triggers a global block
  (found via recursive scan).
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
import cwk_pr001_view_compare as V  # noqa: E402


def _make_obs(tenant_id: str, report_key: str, sha: str, title: str = "同一标题", body: str = "同一正文") -> dict:
    return {
        "schema": "cwk.dual_user_observation.v1",
        "tenant_id": tenant_id,
        "report_key": report_key,
        "canonical_sha256": sha,
        "canonical_fields": {
            "title": title,
            "body": body,
            "author": {"source_user_id": "u1", "display_name": None},
            "created_at": "2026-08-01T10:00:00Z",
            "source_updated_at": "2026-08-01T12:00:00Z",
        },
        "overlay_fields": {"lane": "received"},
        "observed_at": "2026-08-01T13:00:00Z",
    }


class ComparatorInputSecurityTests(unittest.TestCase):
    def test_refuses_top_level_appkey(self):
        with self.assertRaises(C.ContractError):
            V.load_observation_set(str(FIX / "malicious_comparator_appkey.json"))

    def test_refuses_nested_credential(self):
        with self.assertRaises(C.ContractError):
            V.load_observation_set(str(FIX / "malicious_comparator_nested_credential.json"))

    def test_duplicate_report_key_within_tenant_rejected(self):
        tenant = "t_" + "a" * 26
        obs = _make_obs(tenant, "cwork:2070001", "a" * 64)
        with self.assertRaises(C.ContractError):
            V.TenantObservationSet(tenant_id=tenant, observations=[obs, dict(obs)])


class ComparatorLogicTests(unittest.TestCase):
    def _pair(self, count: int):
        ta = "t_" + "a" * 26
        tb = "t_" + "b" * 26
        a_obs = []
        b_obs = []
        for i in range(count):
            rk = f"cwork:2070{i:03d}"
            sha = ("a" * 63) + hex(i % 16)[2:]
            a_obs.append(_make_obs(ta, rk, sha))
            b_obs.append(_make_obs(tb, rk, sha))
        return (
            V.TenantObservationSet(tenant_id=ta, observations=a_obs),
            V.TenantObservationSet(tenant_id=tb, observations=b_obs),
        )

    def test_49_common_never_upgrades(self):
        a, b = self._pair(49)
        r = V.compare(a, b)
        self.assertEqual(r["sample_sizes"]["common"], 49)
        self.assertEqual(r["suggested_verified_shared"], [])
        self.assertEqual(r["upgrade_block_reason"], "fewer than 50 unique common report_keys")

    def test_50_common_all_match_upgrades(self):
        a, b = self._pair(50)
        r = V.compare(a, b)
        self.assertGreaterEqual(len(r["suggested_verified_shared"]), 1)
        for entry in r["suggested_verified_shared"]:
            self.assertEqual(entry["match_rate"], 1.0)
            self.assertEqual(entry["common_samples"], 50)

    def test_threshold_zero_rejected(self):
        a, b = self._pair(50)
        with self.assertRaises(C.ContractError):
            V.compare(a, b, upgrade_threshold=0.0)

    def test_threshold_below_floor_rejected(self):
        a, b = self._pair(50)
        with self.assertRaises(C.ContractError):
            V.compare(a, b, upgrade_threshold=0.5)

    def test_canonical_sha_mismatch_blocks_upgrade(self):
        a, b = self._pair(50)
        # Corrupt one canonical_sha256 in B
        b.observations[0]["canonical_sha256"] = "f" * 64
        r = V.compare(a, b)
        self.assertEqual(r["upgrade_block_reason"], "canonical_sha256_mismatch_between_tenants")
        self.assertEqual(r["suggested_verified_shared"], [])

    def test_nested_temporary_url_blocks_upgrade(self):
        a, b = self._pair(50)
        # Put temporary_url deep inside attachment_permissions of tenant B.
        b.observations[0]["overlay_fields"]["attachment_permissions"] = [
            {"attachment_id": "att_1", "permission": "download", "temporary_url": "https://presign/x"}
        ]
        r = V.compare(a, b)
        self.assertTrue(r["overlay_differences"]["temporary_url_seen"])
        self.assertEqual(r["upgrade_block_reason"], "temporary_url_present_in_overlay")
        self.assertEqual(r["suggested_verified_shared"], [])

    def test_only_candidate_paths_are_suggested(self):
        """Overlay fields are never candidates, no matter how consistent."""

        a, b = self._pair(50)
        # Add identical overlay lane on both sides.
        for obs in list(a.observations) + list(b.observations):
            obs["overlay_fields"]["lane"] = "received"
        r = V.compare(a, b)
        candidate_paths = {e["path"] for e in r["suggested_verified_shared"]}
        # No overlay_fields.* path should appear anywhere.
        for p in candidate_paths:
            self.assertTrue(p.startswith("canonical_fields."), msg=p)

    def test_missing_field_in_some_reports_does_not_upgrade(self):
        a, b = self._pair(50)
        # Drop display_name on 1/50 of both sides so coverage falls to 49/50.
        a.observations[0]["canonical_fields"]["author"] = {"source_user_id": "u1"}
        b.observations[0]["canonical_fields"]["author"] = {"source_user_id": "u1"}
        r = V.compare(a, b)
        for entry in r["suggested_verified_shared"]:
            self.assertNotEqual(entry["path"], "canonical_fields.author.display_name")

    def test_mismatched_value_below_threshold_does_not_upgrade(self):
        a, b = self._pair(50)
        # Mismatch title on 1/50 samples → 49/50 match rate = 0.98, below 0.99.
        a.observations[0]["canonical_fields"]["title"] = "改动的标题"
        r = V.compare(a, b)
        for entry in r["suggested_verified_shared"]:
            self.assertNotEqual(entry["path"], "canonical_fields.title")

    def test_same_tenant_twice_rejected(self):
        a, _ = self._pair(50)
        with self.assertRaises(C.ContractError):
            V.compare(a, a)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
