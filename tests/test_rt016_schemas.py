"""RT-016 schema-only tests + zero-drift guard for RT-011~015 frozen files.

These tests exercise the RT-016-owned schemas without needing any
InstanceLayout or writes.  They also assert that RT-011~015 modules
and schema files have not been silently modified (zero drift check).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_legacy_raw_import as R16  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402


SCHEMA_ROOT = C.SCHEMA_ROOT / "rt016" / "schemas"


# ---------------------------------------------------------------------------
# Schema loading + validation
# ---------------------------------------------------------------------------


class RT016SchemaFilesTests(unittest.TestCase):
    def test_schemas_exist(self):
        expected = {
            # v1 schemas — retained for backward-compat read of pre-v2
            # crosswalks/reviews (audit only).
            "decompose_report.schema.json",
            "migration_crosswalk.schema.json",
            "review_entry.schema.json",
            "migration_manifest_entry.schema.json",
            "reconciliation_report.schema.json",
            # v2 schemas — RT-016 v2 anchor-bound records emitted by
            # ShadowImporter and verified by MigrationReconciler.
            "migration_crosswalk_v2.schema.json",
            "review_entry_v2.schema.json",
            "migration_manifest_entry_v2.schema.json",
            "reconciliation_report_v2.schema.json",
        }
        present = {p.name for p in SCHEMA_ROOT.iterdir() if p.is_file()}
        self.assertEqual(expected, present)

    def test_schemas_are_strict_json(self):
        for path in SCHEMA_ROOT.iterdir():
            schema = C.strict_json_load_path(path)
            self.assertEqual(
                schema["additionalProperties"],
                False,
                f"{path.name} must have additionalProperties:false at root",
            )
            self.assertEqual(
                schema["unevaluatedProperties"],
                False,
                f"{path.name} must have unevaluatedProperties:false at root",
            )
            self.assertIn("$id", schema, path.name)
            self.assertIn("customKeywords", schema, path.name)

    def test_deep_forbidden_lists_credentials_and_paths(self):
        for path in SCHEMA_ROOT.iterdir():
            schema = C.strict_json_load_path(path)
            forbidden = schema["customKeywords"].get("deepForbiddenProperties", [])
            names = set(forbidden)
            self.assertIn("app_key", names, path.name)
            self.assertIn("credential_ref", names, path.name)
            self.assertIn("agent_id", names, path.name)
            self.assertIn("absolute_path", names, path.name)
            self.assertIn("mirror_root", names, path.name)


# ---------------------------------------------------------------------------
# Zero-drift over frozen RT-011~015 payload files
# ---------------------------------------------------------------------------


_FROZEN_FILES: tuple[str, ...] = (
    "scripts/cwk_pr001_contracts.py",
    "scripts/cwk_pr001_probes.py",
    "scripts/cwk_pr001_view_compare.py",
    "scripts/cwk_pr001_cli.py",
    "scripts/cwk_instance.py",
    "scripts/cwk_atomic_file.py",
    "scripts/cwk_tenant_registry.py",
    "scripts/cwk_agent_binding.py",
    "scripts/cwk_agent_context.py",
    "scripts/cwk_credential_broker.py",
    "scripts/cwk_shared_evidence.py",
    "scripts/cwk_access_ledger.py",
    "scripts/cwk_tenant_view.py",
    "scripts/cwk_raw_store.py",
    "scripts/cwk_thread_timeline.py",
    "scripts/cwk_collect_live.py",
    "scripts/cwk_nightly_pipeline.py",
    "scripts/cwk_wiki_query.py",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_grant.schema.json",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_observation.schema.json",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/canonical_report.schema.json",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/tenant_view.schema.json",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/security_defaults.json",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/verified_shared_extensions_v1.json",
)


class FrozenFilesZeroDriftTests(unittest.TestCase):
    """RT-016 must not silently modify frozen RT-011~015 layer files.

    Compare each file's current SHA-256 against the HEAD version.  If
    any mismatch is detected the test fails with the file path, so a
    reviewer can catch the drift immediately.
    """

    def test_no_drift_from_head(self):
        for rel in _FROZEN_FILES:
            path = PROJECT / rel
            self.assertTrue(path.exists(), f"{rel} missing in worktree")
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            head = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=str(PROJECT),
                capture_output=True,
                check=True,
            ).stdout
            head_sha = hashlib.sha256(head).hexdigest()
            self.assertEqual(
                current, head_sha, f"RT-011~015 frozen file drifted: {rel}"
            )


# ---------------------------------------------------------------------------
# RT-016 opaque ID helpers
# ---------------------------------------------------------------------------


class OpaqueIdTests(unittest.TestCase):
    def test_new_run_id_shape(self):
        run_id = R16.new_run_id()
        self.assertRegex(run_id, r"^run_[a-z2-7]{26}$")
        # Not equal on subsequent calls.
        self.assertNotEqual(run_id, R16.new_run_id())

    def test_compute_crosswalk_key_deterministic(self):
        tenant_id = "t_" + "a" * 26
        view_key = "g_" + "a" * 26
        legacy_sha = "0" * 64
        a = R16.compute_crosswalk_key(tenant_id, view_key, legacy_sha)
        b = R16.compute_crosswalk_key(tenant_id, view_key, legacy_sha)
        self.assertEqual(a, b)
        self.assertRegex(a, r"^cw_[a-z2-7]{26}$")

    def test_compute_crosswalk_key_rejects_bad_grammar(self):
        # Bad tenant_id delegates to RT-012 validator which raises
        # TenantIdError; RT-016 does not re-wrap that (matches other
        # public APIs that consume RT-012 IDs).
        import cwk_instance as _I
        with self.assertRaises(_I.TenantIdError):
            R16.compute_crosswalk_key("bad-tenant", "g_" + "a" * 26, "0" * 64)
        with self.assertRaises(R16.LegacyImportError):
            R16.compute_crosswalk_key("t_" + "a" * 26, "bad-key", "0" * 64)
        with self.assertRaises(R16.LegacyImportError):
            R16.compute_crosswalk_key("t_" + "a" * 26, "g_" + "a" * 26, "not-hex")

    def test_compute_legacy_path_hash_deterministic(self):
        a = R16.compute_legacy_path_hash("legacy-raw/2024-06/2070001-sample.md")
        b = R16.compute_legacy_path_hash("legacy-raw/2024-06/2070001-sample.md")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        c = R16.compute_legacy_path_hash("legacy-raw/other/foo.md")
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
