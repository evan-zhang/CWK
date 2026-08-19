"""RT-016 schema-only tests + zero-drift guard for RT-011~015 frozen files.

These tests exercise the RT-016-owned schemas without needing any
InstanceLayout or writes.  They also assert that RT-011~015 modules
and schema files have not been silently modified (zero drift check).

The frozen check uses an **explicit SHA-256 allowlist baseline**
recorded in this file — never a comparison against the current
branch HEAD, which would tautologically succeed on any subsequent
commit that already contains the drift.  The baseline for RT-011~015
files is the SHA-256 of the file bytes at commit ``7ba906f``
(``RT-015 record independent acceptance``), the last commit before
RT-016 v1 was introduced.  Any future accidental / illegal drift into
these frozen boundaries fails the test.
"""

from __future__ import annotations

import hashlib
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


# RT-011~015 baseline (SHA-256 at commit 7ba906f — RT-015 acceptance
# recorded, the last commit before RT-016 v1 introduction).  These
# files MUST NOT be modified by RT-016; the test compares the
# worktree's current bytes against these fixed baseline SHAs so any
# drift is caught even if a subsequent commit already contains it.
_FROZEN_RT011_015_BASELINE_SHAS: dict[str, str] = {
    "scripts/cwk_pr001_contracts.py": "39a71bea37a68fad3e9d8cc859d5e90f5180279ff124e9885cdbdf7e0ba86cc2",
    "scripts/cwk_pr001_probes.py": "4f43d8c0d10c6c704cf70b8158c7b70f85b52049f5299c40b9b3ed32f3fd4942",
    "scripts/cwk_pr001_view_compare.py": "0ac6af2b13725098bb2e9788a042ed32fb0f3d42e6a981f7978b9c1ef906dfaa",
    "scripts/cwk_pr001_cli.py": "2173d0f7eb198a0702d65f6c1d732b4cf350001ff37a46f95655b8bf09304ba3",
    "scripts/cwk_instance.py": "418bbdaabb8842b0a20443b42eca661c7be4c87c59e1e49c5d1a973a56bd5ae7",
    "scripts/cwk_atomic_file.py": "a8a75a2cc98dac1277d5634759f5468e5b9eda627f2094b4b56de5ce8204f978",
    "scripts/cwk_tenant_registry.py": "ad3a0d36bb226c05708784523c961ae2a93f54e00796bc8a1b81e36277abe774",
    "scripts/cwk_agent_binding.py": "9122d1ee6a637e811f6fa03c4eb7cf78960e458f2cd71bce5cdcadc8d426fdcd",
    "scripts/cwk_agent_context.py": "10d15cf23ab8f89c5479867bea39a320778ff104bf7c26320f70f8ed854915ac",
    "scripts/cwk_credential_broker.py": "2a0dcff324e6f63efd576dc0bf17ba0e3538c8f02412445967604c9fdc674150",
    "scripts/cwk_shared_evidence.py": "678386c91d4c402514cce12dca8b2b808bedaa621c66f8a8f05fa3a877906fd8",
    "scripts/cwk_access_ledger.py": "2d4979803d2cd0a2792278ded324ee13c6597c03ed09d713a5f42f6772967549",
    "scripts/cwk_tenant_view.py": "baa903306a2b82795ee0c4e58319ed1d6ddcff7fe1fc7cf86521d61c65beec3a",
    "scripts/cwk_raw_store.py": "78d466ae2565c7139ab0d30b8f64a6b4d7007bf8cb3847304a685656cae42473",
    "scripts/cwk_thread_timeline.py": "198c87293844085e424399b7145abdef94e21812db50758846ea5ea5e27b8e16",
    "scripts/cwk_collect_live.py": "3563da82f1d23954c95d60a29e339dad67662655180328b6f9156c4851c412f7",
    "scripts/cwk_nightly_pipeline.py": "dacd6f14a52e02271c86daa764ae4e090b0bef1d5140b4291b026d350c69a46b",
    "scripts/cwk_wiki_query.py": "e7d5ebb178076159c4facc9d3f0fa4e947a807b20ceb5b795de234010bb890a6",
    "scripts/cwk_entity_catalog.py": "5a5f346eb2369978c0ae2d570f4a120d6066e17fe47337d13b2f31dbf78f2810",
    "scripts/cwk_wiki_search_index.py": "1033f3b080154c90ad30a232ac4bc301d95336b87a89356547c670fac61958cd",
    "scripts/cwk_docdb_cloud.py": "20fe2dda32dbb03687183d9c06a8bce3abe592970c4c9dab2473be38c2ea8232",
    "scripts/cwk_sync_mirror_to_docdb.py": "a0df03e5dc1bb512e8ab2928496ce7dde7bb8a4258f60756823ebf0aa1f4af55",
    "scripts/cwk_tenant_cli.py": "2e34a56302f916d5e3f76eb2240f5426859dca367857ae526cc8a4708bce03fe",
    "scripts/cwk_tenant_cmd_binding.py": "73545b0c52677807967d3a2506569e5fb4cf83925fc8d2b7e2ffd41a7e55ed78",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_grant.schema.json": "40621d6145b268077f280b651ef23656e1486e3241eb78fc06520bcf2ca146c7",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_observation.schema.json": "f8c6db46f0b0a07cc701ad3b6a86945aa73bd1400e33e24bcc457d854d111f9f",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/canonical_report.schema.json": "d292730ffc592fcda1a6ad98af3336b91f60998fe471fb7c2f13ac4f4b5cdc03",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/tenant_view.schema.json": "b2a4ecc9c4bb0f2a34adad35b15d06449ebb68aab1201955af557c43c83eccf6",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/security_defaults.json": "7a84346981c51c6185301752e4673431e9396fb8a8a6998a1a2cf5c75ad8dabd",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/verified_shared_extensions_v1.json": "228f9eda0d565c47719ded823ff8a34369cd6dabe57cab83edd3202b30bf8dbe",
}


# RT-016 v1 schemas are RT-016-owned (evolvable within RT-016 scope) but
# also strictly frozen at their **current post-second-round-remediation
# baseline**.  Any future accidental modification to a v1 schema file
# fails the test — future RT-016 work that legitimately needs to
# evolve a v1 schema must update this allowlist together with the
# schema itself, forcing the drift into review.
_FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/decompose_report.schema.json": "8de259c9f9569e2fe26fd3356dae938eb6d9a77098c157b65c7b0c3c5360d2db",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_crosswalk.schema.json": "81e71cb60033cc4e725fab091d0be4f310f718a7b040800a0362a51c6b955a3e",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/review_entry.schema.json": "8d3862f44f2557872a72c9858ecc82688f95f7e69d1b4058dce99e0c45739ce5",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_manifest_entry.schema.json": "bfd7e2cc71cec1ba22d4ce334acfbafd1ab17ed2249f1bb8269214d5fb258806",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/reconciliation_report.schema.json": "148ec816d432dd11a1a14dd44233e8fd2d00c8fdb11534e6429b0b9cd396e9e4",
}


class FrozenFilesZeroDriftTests(unittest.TestCase):
    """Guard RT-011~015 (and RT-016 v1 schemas) against silent drift.

    Uses an explicit SHA-256 allowlist baseline recorded above — never
    a git-HEAD self-comparison, which would tautologically pass once
    the drift is committed.  If any file's current SHA-256 differs
    from the baseline the test fails naming the file, forcing the
    drift into review.
    """

    def test_rt011_015_files_match_baseline_sha(self):
        for rel, expected_sha in _FROZEN_RT011_015_BASELINE_SHAS.items():
            path = PROJECT / rel
            self.assertTrue(path.exists(), f"{rel} missing in worktree")
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(
                actual_sha,
                expected_sha,
                (
                    f"RT-011~015 frozen file drifted: {rel}\n"
                    f"  expected SHA (7ba906f baseline): {expected_sha}\n"
                    f"  actual   SHA (current worktree): {actual_sha}"
                ),
            )

    def test_rt016_v1_schemas_match_pinned_baseline_sha(self):
        for rel, expected_sha in _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS.items():
            path = PROJECT / rel
            self.assertTrue(path.exists(), f"{rel} missing in worktree")
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(
                actual_sha,
                expected_sha,
                (
                    f"RT-016 v1 schema drifted from pinned baseline: {rel}\n"
                    f"  expected SHA (post-second-round baseline): {expected_sha}\n"
                    f"  actual   SHA (current worktree):           {actual_sha}\n"
                    f"  If this drift is intentional, update the pinned "
                    f"SHA in _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS."
                ),
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
