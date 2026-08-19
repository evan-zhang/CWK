"""RT-011: malicious fixture rejection matrix.

Every file under ``tests/fixtures/pr001/malicious_*.json`` must be rejected
by the corresponding validator.  ``manifest.json`` must contain a canary
(otherwise the fixture bundle has been silently swapped or the test suite
is loading a different manifest).
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


class FixtureManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        with (FIX / "manifest.json").open("r", encoding="utf-8") as fh:
            self.manifest = json.load(fh)

    def test_canary_present(self):
        canary = self.manifest.get("canary", {})
        self.assertEqual(canary.get("value"), "RT-011-CANARY-2026-08-19")
        self.assertEqual(canary.get("file"), "__canary__")

    def test_entry_hashes_match_disk(self):
        for entry in self.manifest["entries"]:
            path = FIX / entry["file"]
            data = path.read_bytes()
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                entry["sha256"],
                msg=f"SHA drift for {entry['file']}",
            )
            self.assertEqual(len(data), entry["bytes"])

    def test_manifest_covers_every_json_file(self):
        listed = {entry["file"] for entry in self.manifest["entries"]}
        on_disk = {p.name for p in FIX.glob("*.json") if p.name != "manifest.json"}
        self.assertEqual(listed, on_disk)


class MaliciousRejectionTests(unittest.TestCase):
    def test_canonical_with_forbidden_fields_rejected(self):
        payload = _load("malicious_canonical_with_tenant_fields.json")
        with self.assertRaises(C.ContractError):
            C.validate_canonical_envelope(payload)

    def test_query_request_agent_injection_rejected(self):
        payload = _load("malicious_query_request_agent_injection.json")
        with self.assertRaises(C.ContractError):
            C.validate_query_request(payload)

    def test_query_request_slug_selector_rejected(self):
        payload = _load("malicious_query_request_slug_selector.json")
        with self.assertRaises(C.ContractError):
            C.validate_query_request(payload)

    def test_route_decision_slug_and_bad_disposition_rejected(self):
        payload = _load("malicious_route_decision_slug_and_illegal_disposition.json")
        with self.assertRaises(C.ContractError):
            C.validate_route_decision(payload)

    def test_profile_rolled_back_state_rejected(self):
        payload = _load("malicious_profile_rolled_back_state.json")
        with self.assertRaises(C.ContractError):
            C.validate_knowledge_profile(payload)

    def test_active_observation_rejected(self):
        payload = _load("malicious_grant_active_from_bad_source.json")
        with self.assertRaises(C.ContractError):
            C.validate_access_observation(payload)

    def test_bad_grant_transition_rejected(self):
        payload = _load("malicious_grant_bad_transition.json")
        with self.assertRaises(C.ContractError):
            C.validate_access_grant_transition(payload["from_status"], payload["to_status"])

    def test_sample_manifest_overlap_rejected(self):
        payload = _load("malicious_sample_manifest_overlap.json")
        with self.assertRaises(C.ContractError):
            C.validate_sample_manifest(payload)

    def test_verified_extensions_temporary_url_rejected(self):
        payload = _load("malicious_verified_extensions_temporary_url.json")
        with self.assertRaises(C.ContractError):
            C.validate_verified_shared_extensions(payload)

    def test_forged_probes_rejected_in_aggregate(self):
        payloads = _load("malicious_probe_forged_verified.json")
        with self.assertRaises(C.ContractError):
            P.aggregate(payloads)

    def test_tenant_view_path_injection_rejected(self):
        payload = _load("malicious_tenant_view_path_injection.json")
        with self.assertRaises(C.ContractError):
            C.validate_tenant_view(payload)

    def test_report_key_namespace_isolation_fixture(self):
        payload = _load("malicious_report_key_namespace_collision.json")
        keys = {C.compose_report_key(p["source_namespace"], p["report_id"]) for p in payload["pairs"]}
        self.assertEqual(sorted(keys), sorted(payload["expected_report_keys"]))
        self.assertEqual(len(keys), len(payload["pairs"]))  # No collapse.


def _load(name: str):
    with (FIX / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
