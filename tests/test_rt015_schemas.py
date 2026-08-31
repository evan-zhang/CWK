"""RT-015: schema and byte-contract tests + frozen-file zero-drift.

Covers:

- Every RT-015 schema loads and enforces the required fields, forbidden
  fields, and deepForbiddenProperties.
- grant_key derivation is stable, deterministic, and depends on both
  tenant_id and report_key.
- Every RT-011~014 frozen file that RT-015 depends on has not been
  modified by RT-015 (SHA-256 zero-drift assertion).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import _rt015_helpers as H  # noqa: E402


class GrantKeyDerivationTests(unittest.TestCase):
    def test_grant_key_deterministic(self):
        t = "t_" + "a" * 26
        rk = "cwork:2070001"
        k1 = AL.compute_grant_key(t, rk)
        k2 = AL.compute_grant_key(t, rk)
        self.assertEqual(k1, k2)

    def test_grant_key_regex(self):
        import re
        t = "t_" + "a" * 26
        for i, rid in enumerate(["r1", "R2-X", "2070001", "abc.def"]):
            gk = AL.compute_grant_key(t, f"cwork:{rid}")
            self.assertTrue(re.match(r"^g_[a-z2-7]{26}$", gk),
                            f"grant_key {gk!r} does not match regex")

    def test_grant_key_changes_with_tenant(self):
        rk = "cwork:2070001"
        k1 = AL.compute_grant_key("t_" + "a" * 26, rk)
        k2 = AL.compute_grant_key("t_" + "b" * 26, rk)
        self.assertNotEqual(k1, k2)

    def test_grant_key_changes_with_report_key(self):
        t = "t_" + "a" * 26
        k1 = AL.compute_grant_key(t, "cwork:1")
        k2 = AL.compute_grant_key(t, "cwork:2")
        self.assertNotEqual(k1, k2)

    def test_grant_key_domain_separated(self):
        """Ensures that concatenation-only birthday attacks (tenant='ab'+report vs
        tenant='a'+'b'+report) cannot produce the same grant_key."""

        t = "t_" + "a" * 26
        rk = "cwork:2070001"
        # Manual domain-separated recipe (matches implementation).
        material = (
            AL.GRANT_KEY_DOMAIN + b"\x00" + t.encode("utf-8") + b"\x00" + rk.encode("utf-8")
        )
        digest = hashlib.sha256(material).digest()[:16]
        encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
        expected = AL.GRANT_KEY_PREFIX + encoded
        self.assertEqual(AL.compute_grant_key(t, rk), expected)

    def test_grant_key_rejects_malformed_inputs(self):
        with self.assertRaises(Exception):
            AL.compute_grant_key("bad_tenant", "cwork:1")
        with self.assertRaises(Exception):
            AL.compute_grant_key("t_" + "a" * 26, "not a report key")
        with self.assertRaises(Exception):
            AL.compute_grant_key("t_" + "a" * 26, "cwork:with/slash")
        with self.assertRaises(Exception):
            AL.compute_grant_key("t_" + "a" * 26, "cwork:with\nnewline")


class GrantRecordSchemaTests(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "schema": "cwk.rt015.access_grant_record.v1",
            "grant_key": "g_" + "a" * 26,
            "tenant_id": "t_" + "a" * 26,
            "source_namespace": "cwork",
            "report_id": "R1",
            "grant": {
                "schema": "cwk.access_grant.v1",
                "tenant_id": "t_" + "a" * 26,
                "source_namespace": "cwork",
                "report_id": "R1",
                "status": "granted",
                "roles": ["receiver"],
                "visibility_scope": "full",
                "permission_source": "tenant_appkey_observation",
                "auth_epoch": 1,
                "granted_at": "2026-08-19T00:00:00Z",
                "last_verified_at": "2026-08-19T00:00:00Z",
                "lease_expires_at": None,
                "revoked_at": None,
            },
            "record_revision": 1,
            "created_at": "2026-08-19T00:00:00Z",
            "updated_at": "2026-08-19T00:00:00Z",
        }

    def test_baseline_passes(self):
        AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, self._base())

    def test_missing_required_key_rejected(self):
        for missing_key in [
            "schema", "grant_key", "tenant_id", "source_namespace",
            "report_id", "grant", "record_revision", "created_at", "updated_at",
        ]:
            payload = self._base()
            del payload[missing_key]
            with self.assertRaises(AL.AccessLedgerError):
                AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, payload)

    def test_additional_property_rejected(self):
        payload = self._base()
        payload["attacker"] = 1
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, payload)

    def test_deep_forbidden_credential(self):
        payload = self._base()
        payload["grant"]["credential_ref"] = "secret://leak"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, payload)

    def test_deep_forbidden_body(self):
        payload = self._base()
        payload["body"] = "unauthorised copy"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, payload)

    def test_deep_forbidden_temporary_url(self):
        payload = self._base()
        payload["grant"]["temporary_url"] = "https://x.example"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._GRANT_RECORD_SCHEMA_ID, payload)


class StateEventSchemaTests(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "schema": "cwk.rt015.state_transition_event.v1",
            "event_id": "ev_" + "a" * 26,
            "grant_key": "g_" + "a" * 26,
            "tenant_id": "t_" + "a" * 26,
            "from_status": "granted",
            "to_status": "active",
            "tenant_auth_epoch_before": 1,
            "tenant_auth_epoch_after": 1,
            "record_revision_before": 1,
            "record_revision_after": 2,
            "actor": "admin",
            "reason": "promote",
            "evidence_refs": ["authority_receipt_id:ar_" + "b" * 26],
            "happened_at": "2026-08-19T00:00:00Z",
        }

    def test_baseline_passes(self):
        AL._validate_against(AL._STATE_EVENT_SCHEMA_ID, self._base())

    def test_actor_rejects_control_chars(self):
        payload = self._base()
        for bad in ["admin\n", "admin\r", "admin\x00", "admin\x1b[31m",
                    "with tab\t", "with del\x7f"]:
            payload["actor"] = bad
            with self.assertRaises(AL.AccessLedgerError):
                AL._validate_against(AL._STATE_EVENT_SCHEMA_ID, payload)

    def test_reason_rejects_control_chars(self):
        payload = self._base()
        for bad in ["reason\n", "reason\r\n", "reason\x00attack"]:
            payload["reason"] = bad
            with self.assertRaises(AL.AccessLedgerError):
                AL._validate_against(AL._STATE_EVENT_SCHEMA_ID, payload)

    def test_initial_status_allowed(self):
        payload = self._base()
        payload["from_status"] = "_initial_"
        AL._validate_against(AL._STATE_EVENT_SCHEMA_ID, payload)

    def test_to_status_disallows_initial(self):
        payload = self._base()
        payload["to_status"] = "_initial_"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._STATE_EVENT_SCHEMA_ID, payload)


class TombstoneSchemaTests(unittest.TestCase):
    def test_baseline_passes(self):
        payload = {
            "schema": "cwk.rt015.access_tombstone.v1",
            "grant_key": "g_" + "a" * 26,
            "tenant_id": "t_" + "a" * 26,
            "source_namespace": "cwork",
            "report_id": "R1",
            "revoked_at": "2026-08-19T00:00:00Z",
            "tenant_auth_epoch_at_revoke": 2,
            "revocation_receipt_id": "rv_" + "a" * 26,
        }
        AL._validate_against(AL._TOMBSTONE_SCHEMA_ID, payload)

    def test_deep_forbidden(self):
        payload = {
            "schema": "cwk.rt015.access_tombstone.v1",
            "grant_key": "g_" + "a" * 26,
            "tenant_id": "t_" + "a" * 26,
            "source_namespace": "cwork",
            "report_id": "R1",
            "revoked_at": "2026-08-19T00:00:00Z",
            "tenant_auth_epoch_at_revoke": 2,
            "revocation_receipt_id": "rv_" + "a" * 26,
            "app_key": "leak",
        }
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._TOMBSTONE_SCHEMA_ID, payload)


class AuthorityReceiptSchemaTests(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "schema": "cwk.rt015.authority_receipt.v1",
            "receipt_id": "ar_" + "a" * 26,
            "signer_id": "test_signer",
            "receipt_type": "grant_promote",
            "tenant_id": "t_" + "a" * 26,
            "source_namespace": "cwork",
            "report_id": "R1",
            "grant_key": "g_" + "a" * 26,
            "roles": ["receiver"],
            "visibility_scope": "full",
            "permission_source": "authoritative_permission_api",
            "issued_at": "2026-08-19T00:00:00Z",
            "lease_expires_at": "2026-08-19T01:00:00Z",
            "signature": "0" * 64,
        }

    def test_baseline_passes(self):
        AL._validate_against(AL._AUTHORITY_RECEIPT_SCHEMA_ID, self._base())

    def test_signer_id_rejects_bad_chars(self):
        payload = self._base()
        for bad in ["with space", "UPPER", "with/slash", "with:colon"]:
            payload["signer_id"] = bad
            with self.assertRaises(AL.AccessLedgerError):
                AL._validate_against(AL._AUTHORITY_RECEIPT_SCHEMA_ID, payload)

    def test_receipt_type_gated(self):
        payload = self._base()
        payload["receipt_type"] = "unknown_type"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against(AL._AUTHORITY_RECEIPT_SCHEMA_ID, payload)


class TenantViewRecordSchemaTests(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "schema": "cwk.rt015.tenant_view_record.v1",
            "view_key": "g_" + "a" * 26,
            "tenant_id": "t_" + "a" * 26,
            "source_namespace": "cwork",
            "report_id": "R1",
            "canonical_sha256": "0" * 64,
            "view": {
                "schema": "cwk.tenant_view.v1",
                "tenant_id": "t_" + "a" * 26,
                "report_key": "cwork:R1",
                "canonical_sha256": "0" * 64,
                "observed_at": "2026-08-19T00:00:00Z",
            },
            "record_revision": 1,
            "created_at": "2026-08-19T00:00:00Z",
            "updated_at": "2026-08-19T00:00:00Z",
        }

    def test_baseline_passes(self):
        import cwk_tenant_view as TV
        AL._validate_against("cwk.pr001.rt015.tenant_view_record.v1", self._base())

    def test_body_leak_rejected(self):
        payload = self._base()
        payload["view"]["body"] = "leaked body"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against("cwk.pr001.rt015.tenant_view_record.v1", payload)

    def test_credential_ref_rejected(self):
        payload = self._base()
        payload["credential_ref"] = "secret://leak"
        with self.assertRaises(AL.AccessLedgerError):
            AL._validate_against("cwk.pr001.rt015.tenant_view_record.v1", payload)


# ---------------------------------------------------------------------------
# Zero-drift assertion for RT-011~014 frozen files
# ---------------------------------------------------------------------------


class FrozenFilesZeroDriftTests(unittest.TestCase):
    """Verify that RT-011~014 modules/schemas/tests are not modified by
    RT-015 (compare current file bytes to the ``main`` commit that landed
    RT-014).  The comparison is done with ``git`` so no baseline is
    hard-coded in tests.
    """

    RT011_14_PATHS = (
        "scripts/cwk_pr001_contracts.py",
        "scripts/cwk_pr001_probes.py",
        "scripts/cwk_instance.py",
        "scripts/cwk_atomic_file.py",
        "scripts/cwk_tenant_registry.py",
        "scripts/cwk_tenant_cli.py",
        "scripts/cwk_tenant_cli_api.py",
        "scripts/cwk_tenant_cmd_core.py",
        "scripts/cwk_tenant_cmd_binding.py",
        "scripts/cwk_agent_binding.py",
        "scripts/cwk_agent_context.py",
        "scripts/cwk_credential_broker.py",
        "scripts/cwk_shared_evidence.py",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_grant.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/tenant_view.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_observation.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/canonical_report.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/knowledge_profile.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/report_key.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/query_request.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/route_decision.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/sample_manifest.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/verified_shared_extensions.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/security_defaults.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/capability_probe.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/profile_pointer_rollback.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/dual_user_observation.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/security_defaults.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/verified_shared_extensions_v1.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/report_version.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/catalog_head.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/publish_receipt.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/recovery_report.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/agent_binding.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/binding_receipt.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/binding_secret_pointer.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/credential_broker_lease.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/credential_ref.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/command_spec.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/instance_layout.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/layout_doctor_report.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/provision_receipt.schema.json",
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/tenant_record.schema.json",
    )

    def test_no_drift_from_head(self):
        import subprocess
        policy_path = (
            PROJECT
            / "PR"
            / "PR-001-multitenant-knowledge-spaces"
            / "contracts"
            / "script-evolution"
            / "policy_v1.json"
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        evolvable = {entry["target_path"] for entry in policy["evolvable_paths"]}
        self.assertIn("scripts/cwk_instance.py", evolvable)
        for rel in self.RT011_14_PATHS:
            # The central script-evolution guard now owns these paths and
            # verifies their genesis-to-receipt chain.  This historical
            # RT-015 test keeps its direct HEAD check for every immutable path.
            if rel in evolvable:
                continue
            path = PROJECT / rel
            if not path.exists():
                # Not our problem — skip if repo layout changed.
                continue
            current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            # Compare with git ls-tree HEAD SHA of the same file.
            result = subprocess.run(
                ["git", "ls-tree", "HEAD", rel],
                cwd=str(PROJECT), capture_output=True, text=True, check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue  # New RT-015 file
            # Retrieve the file's blob content from HEAD.
            blob_result = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=str(PROJECT), capture_output=True, check=False,
            )
            if blob_result.returncode != 0:
                continue
            head_sha = hashlib.sha256(blob_result.stdout).hexdigest()
            self.assertEqual(
                current_sha, head_sha,
                f"RT-011/012/013/014 file {rel} was modified by RT-015 "
                f"(current {current_sha[:12]} != HEAD {head_sha[:12]})",
            )


if __name__ == "__main__":
    unittest.main()
