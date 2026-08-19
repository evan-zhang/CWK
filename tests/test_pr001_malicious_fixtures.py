"""RT-011 (post-remediation): fixture manifest + malicious rejection matrix.

Covers Blocker #6 (fixture manifest/canary consistency), plus
attributes-of-attack coverage across the schemas.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
FIX = PROJECT / "tests" / "fixtures" / "pr001"
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_pr001_probes as P  # noqa: E402


def _load(name: str):
    return C.strict_json_load_path(FIX / name)


class FixtureManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _load("manifest.json")

    def test_canary_file_exists_and_id_matches(self):
        self.assertEqual(self.manifest["canary_id"], "RT-011-CANARY-2026-08-19")
        canary_file = FIX / self.manifest["canary_file"]
        self.assertTrue(canary_file.exists(), f"canary file missing: {canary_file.name}")
        canary_payload = json.loads(canary_file.read_text(encoding="utf-8"))
        self.assertEqual(canary_payload["canary_id"], "RT-011-CANARY-2026-08-19")

    def test_manifest_sha256_matches_disk_via_independent_oracle(self):
        # Independent oracle: recompute sha256 ourselves and compare to the
        # value recorded in the manifest.  A tampered fixture on disk with
        # a stale manifest entry MUST fail this test.
        for entry in self.manifest["entries"]:
            data = (FIX / entry["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], entry["file"])
            self.assertEqual(len(data), entry["bytes"], entry["file"])

    def test_manifest_covers_every_json_file(self):
        listed = {entry["file"] for entry in self.manifest["entries"]}
        on_disk = {p.name for p in FIX.glob("*.json") if p.name != "manifest.json"}
        self.assertEqual(listed, on_disk)

    def test_entry_counts_reasonable(self):
        kinds = {"benign": 0, "malicious": 0, "canary": 0}
        for e in self.manifest["entries"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        self.assertEqual(kinds["canary"], 1)
        self.assertGreaterEqual(kinds["malicious"], 10)
        self.assertGreaterEqual(kinds["benign"], 1)


class MaliciousRejectionTests(unittest.TestCase):
    def test_canonical_nested_tenant_id_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(_load("malicious_canonical_nested_tenant_id.json"))

    def test_query_request_nested_credential_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_query_request(_load("malicious_query_request_nested_credential.json"))

    def test_query_request_slug_selector_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_query_request(_load("malicious_query_request_slug_selector.json"))

    def test_route_decision_slug_and_bad_disposition_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(_load("malicious_route_decision_slug_and_illegal_disposition.json"))

    def test_profile_rolled_back_state_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_knowledge_profile(_load("malicious_profile_rolled_back_state.json"))

    def test_profile_sha_recompute_drift_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_knowledge_profile(_load("malicious_profile_sha_recompute_drift.json"))

    def test_active_observation_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_access_observation(_load("malicious_grant_active_from_bad_source.json"))

    def test_bad_grant_transition_rejected(self):
        payload = _load("malicious_grant_bad_transition.json")
        with self.assertRaises(C.ContractError):
            C.validate_access_grant_transition(payload["from_status"], payload["to_status"])

    def test_sample_manifest_overlap_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(_load("malicious_sample_manifest_overlap.json"))

    def test_sample_manifest_undersized_holdout_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(_load("malicious_sample_manifest_undersized_holdout.json"))

    def test_sample_manifest_actual_size_drift_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(_load("malicious_sample_manifest_actual_size_drift.json"))

    def test_sample_manifest_chunk_gap_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(_load("malicious_sample_manifest_chunk_gap.json"))

    def test_sample_manifest_strata_mismatch_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(_load("malicious_sample_manifest_strata_mismatch.json"))

    def test_verified_extensions_temporary_url_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_verified_shared_extensions(_load("malicious_verified_extensions_temporary_url.json"))

    def test_verified_extensions_undersized_samples_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_verified_shared_extensions(_load("malicious_verified_extensions_undersized_samples.json"))

    def test_security_defaults_loopback_allowed_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(_load("malicious_security_defaults_loopback_allowed.json"))

    def test_security_defaults_break_glass_alt_rejected(self):
        with self.assertRaises(C.ContractError):
            C.validate_security_defaults(_load("malicious_security_defaults_break_glass_alt.json"))

    def test_forged_probes_rejected_in_aggregate(self):
        with self.assertRaises(C.ContractError):
            P.aggregate(_load("malicious_probe_forged_verified.json"))

    def test_report_key_namespace_isolation_fixture(self):
        payload = _load("malicious_report_key_namespace_collision.json")
        keys = {C.compose_report_key(p["source_namespace"], p["report_id"]) for p in payload["pairs"]}
        self.assertEqual(sorted(keys), sorted(payload["expected_report_keys"]))
        self.assertEqual(len(keys), len(payload["pairs"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
