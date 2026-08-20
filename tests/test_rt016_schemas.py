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
import stat
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_legacy_raw_import as R16  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import pr001_script_evolution_guard as EG  # noqa: E402


SCHEMA_ROOT = C.SCHEMA_ROOT / "rt016" / "schemas"

# PR-001 pre-checkpoint Wave-0 re-freeze.  Nine of the 26 RT-011~015 paths
# pinned below are allowed to evolve during RT-012/013/017~026, but ONLY by
# appending a receipt predeclared in
# PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution/policy_v1.json.
# The baseline table itself never changes: it stays the genesis of every chain,
# and the guard recomputes each path's expected SHA by replaying its receipts
# (with no receipt, the expected SHA is still the genesis SHA).  The guard
# helper's bytes are pinned here and, independently, in
# tests/test_pr001_script_evolution_guard.py.  A later RT must never refresh
# either pin, edit the policy or its schemas, or edit this table.
_SCRIPT_EVOLUTION_GUARD_SHA256 = "01abe94109d21ffbbfbf84aa8672058455237099d4625ebb5c5577986dabd32a"


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


# RT-011~015 script baseline (SHA-256 at commit 7ba906f — RT-015
# acceptance recorded, the last commit before RT-016 v1 introduction).
# These files MUST NOT be modified by RT-016; the test compares the
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
    "PR/PR-001-multitenant-knowledge-spaces/contracts/security_defaults.json": "7a84346981c51c6185301752e4673431e9396fb8a8a6998a1a2cf5c75ad8dabd",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/verified_shared_extensions_v1.json": "228f9eda0d565c47719ded823ff8a34369cd6dabe57cab83edd3202b30bf8dbe",
}


# RT-011 (PR-001 v1) shared contract-schema baseline — every JSON
# schema shipped under ``contracts/schemas/`` was frozen by RT-011 and
# subsequently referenced by RT-012~015; RT-016 is not allowed to
# modify them.  The exact-set meta-test below (see
# :class:`FrozenBaselineExactSetTests`) fails closed if a new schema
# appears in that directory without being added here, so the
# "RT-011~015 frozen-schema zero drift" promise cannot silently narrow
# by omission.
_FROZEN_PR001_SHARED_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_grant.schema.json": "40621d6145b268077f280b651ef23656e1486e3241eb78fc06520bcf2ca146c7",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/access_observation.schema.json": "f8c6db46f0b0a07cc701ad3b6a86945aa73bd1400e33e24bcc457d854d111f9f",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/canonical_report.schema.json": "d292730ffc592fcda1a6ad98af3336b91f60998fe471fb7c2f13ac4f4b5cdc03",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/capability_probe.schema.json": "4aaf6c2fb4f5f1fd64b4b1dde937c9425bfb3628c18b30e3b9768c463aa71d48",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/dual_user_observation.schema.json": "0db849e33e48e8bcfed2c29d61d08b595ba058747fb08de62400f60dfac50c25",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/knowledge_profile.schema.json": "97c341e9240329d4d2ebdd78bb40314a049b6d7f8e908100fc00ebf8be263436",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/profile_pointer_rollback.schema.json": "e99e5bd5fa9798e34e2eae425007815384132b474bd20d5a04f17d6289c5104c",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/query_request.schema.json": "d03fe1731e1eb0f7acdcf52f34c8590ba5d954bce4b0708ca59a9647593d7696",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/report_key.schema.json": "93937bbb5bc8c8dbecfdd44991d01501d68dbeaf673d426f96664f0e036bf9bf",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/route_decision.schema.json": "aed2e633ec45ce3ff84bc854d5ec4f4b2d9e63eb7eb0217b3c72606a44d2640d",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/sample_manifest.schema.json": "69a42247546226d088ef4c942c8123c3048f7efbf752f704e4c255bd41732ba4",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/security_defaults.schema.json": "4ec12c22a0081731aabba825ea26124b4a362597d698e7894977696b24d55f8f",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/tenant_view.schema.json": "b2a4ecc9c4bb0f2a34adad35b15d06449ebb68aab1201955af557c43c83eccf6",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas/verified_shared_extensions.schema.json": "34e52fa7eaf43dac86ea1a04263e67f9901a10c1408f6107b9258e20b3c243f9",
}


# RT-012 owned contract-schema baseline.
_FROZEN_RT012_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/command_spec.schema.json": "60eff5df943eb7b5d8cbef6ab30cfeb19c7d882593c4fdc339984ddafd2cb0c5",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/instance_layout.schema.json": "4444c7dbf180cf650b5c5db5d23882420210c811759e0b09be83f1d053540032",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/layout_doctor_report.schema.json": "7c36d5c5580b435fdc09ed3e182e281e0681b1ea7bf76799d1bd7391056404b0",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/provision_receipt.schema.json": "4c3c00d5a0543d1e8bf7ac56044342c280a6afc2051e5f57f726a05fe40dee0e",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas/tenant_record.schema.json": "dd63e84fa279355fc1e929a224ddba4389109a8bed4d24f9f0798b716e57f8a3",
}


# RT-013 owned contract-schema baseline.
_FROZEN_RT013_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/agent_binding.schema.json": "c38629f8e9eccad09a35bb0986ad2836b6d6daf893c797f6ae9496e3eaa47ed3",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/binding_receipt.schema.json": "64d263221aea4e28f49632fc617dfb0cbbebfa0efa7e9e303676de26c7126875",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/binding_secret_pointer.schema.json": "a267608cc5e5ebee04378991552e1ae0a834a12faea1b969d802b4014b30445d",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/credential_broker_lease.schema.json": "a5a00fda9ace4bd40327b813df683ac6b21ad63fb853a6046f1358da6572eeb3",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas/credential_ref.schema.json": "60b0a9032e1f44ce21fde1b493a95ef901a0291068512755a59c675906f94e34",
}


# RT-014 owned contract-schema baseline.
_FROZEN_RT014_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/catalog_head.schema.json": "d0773ec7d62bdb48e24fbc1bf123131fb6ce64a1162aef7786880fb7b48b3400",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/publish_receipt.schema.json": "df65ffcf0a087af1977c1eda2db1ae6e43cfc3a6ba3298dc247f5fda8bdb82dd",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/recovery_report.schema.json": "38ae2460fd4d13be0b93e43a7b263885c568a5b0493528f87ecaea9f97b8baa4",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas/report_version.schema.json": "6390b53668e1cfffcaa7941bf31439828f22c1dcd45f0767198de7856f821c47",
}


# RT-015 owned contract-schema baseline.
_FROZEN_RT015_SCHEMA_BASELINE_SHAS: dict[str, str] = {
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/access_grant_record.schema.json": "6b9d95d274ddc25a22f429d51ccd80c56d18638eeaac8a5cb33fa6996d07e031",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/access_tombstone.schema.json": "5787225851a4441587aa347cce2a77709f143da2c1f51ffe5f9db80434613621",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/authority_receipt.schema.json": "27cc01931f8586190aa96ef841f8ee4e5183a6183e77b76e1587db1289d8f7da",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/cleanup_outbox.schema.json": "54bc3710d4031481623f4b9892608695879c59191db085e7239c22aa063083ea",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/revoke_intent.schema.json": "1b5a2e74332949f5e69c3cca4da143db99e8be37681197fed6540313dd6203aa",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/revoke_receipt.schema.json": "3062505056cae7f4dacc91daf04c26d441de5fc8c0f9f5dbb8be735c304898a7",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/state_transition_event.schema.json": "0419a225bf29b4857fb71280e374a54d3810975394282cfebfe71e5ed991964e",
    "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas/tenant_view_record.schema.json": "c79468362222c9755c881491f74638d5fff9c42dd673975e007d89fd5cbfa91f",
}


# Directories whose entire contents are pinned to the corresponding
# per-directory baseline mapping.  The exact-set meta-test asserts
# every file present in each directory is a key of the mapping, and
# every mapping key exists as a file.
_EXACT_SET_DIRECTORY_MAP: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/schemas",
        _FROZEN_PR001_SHARED_SCHEMA_BASELINE_SHAS,
    ),
    (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt012/schemas",
        _FROZEN_RT012_SCHEMA_BASELINE_SHAS,
    ),
    (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt013/schemas",
        _FROZEN_RT013_SCHEMA_BASELINE_SHAS,
    ),
    (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt014/schemas",
        _FROZEN_RT014_SCHEMA_BASELINE_SHAS,
    ),
    (
        "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas",
        _FROZEN_RT015_SCHEMA_BASELINE_SHAS,
    ),
)


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

    def _read_frozen(self, rel: str) -> bytes:
        """Read a pinned file through the guard's dir-fd reader.

        PR-001 Wave-0: ``Path.exists()`` / ``Path.read_bytes()`` follow
        symlinks and ignore ``st_nlink``, so a symlink or hard link pointing
        at identical bytes passed every SHA check below while defeating the
        "this exact file is frozen" promise.  ``EG.read_checked_bytes``
        resolves each component through an ``openat`` directory fd, rejects
        symlinked components and leaves, rejects ``st_nlink != 1``, rejects
        case/Unicode name aliasing, and re-checks the file's identity across
        the read window.
        """

        data = EG.read_checked_bytes(PROJECT, rel, missing_ok=True)
        self.assertIsNotNone(data, f"{rel} missing in worktree")
        assert data is not None  # narrows the type for mypy-style readers
        return data

    def _assert_baseline_matches(self, label: str, mapping: dict[str, str]) -> None:
        for rel, expected_sha in mapping.items():
            actual_sha = hashlib.sha256(self._read_frozen(rel)).hexdigest()
            self.assertEqual(
                actual_sha,
                expected_sha,
                (
                    f"{label} frozen file drifted: {rel}\n"
                    f"  expected SHA (pinned baseline): {expected_sha}\n"
                    f"  actual   SHA (current worktree): {actual_sha}"
                ),
            )

    def test_rt011_015_files_match_baseline_sha(self):
        # PR-001 Wave-0: byte-identity is now the *floor*, not the rule.  Seven-
        # teen of these 26 paths must still be byte-identical forever; the other
        # nine may equal the tip of their policy-declared, append-only receipt
        # chain instead.  The guard below enforces both, and additionally
        # rejects an unrecorded edit to any of the nine.
        guard_sha = hashlib.sha256(
            self._read_frozen("tests/pr001_script_evolution_guard.py")
        ).hexdigest()
        self.assertEqual(
            guard_sha,
            _SCRIPT_EVOLUTION_GUARD_SHA256,
            (
                "tests/pr001_script_evolution_guard.py drifted: "
                f"expected {_SCRIPT_EVOLUTION_GUARD_SHA256}, got {guard_sha}.  "
                "The Wave-0 guard is frozen; a later RT must not refresh this pin."
            ),
        )
        EG.assert_frozen_baseline(PROJECT, _FROZEN_RT011_015_BASELINE_SHAS)

    def test_pr001_shared_schemas_match_baseline_sha(self):
        self._assert_baseline_matches(
            "PR-001 shared schemas (RT-011)",
            _FROZEN_PR001_SHARED_SCHEMA_BASELINE_SHAS,
        )

    def test_rt012_schemas_match_baseline_sha(self):
        self._assert_baseline_matches(
            "RT-012 schemas", _FROZEN_RT012_SCHEMA_BASELINE_SHAS
        )

    def test_rt013_schemas_match_baseline_sha(self):
        self._assert_baseline_matches(
            "RT-013 schemas", _FROZEN_RT013_SCHEMA_BASELINE_SHAS
        )

    def test_rt014_schemas_match_baseline_sha(self):
        self._assert_baseline_matches(
            "RT-014 schemas", _FROZEN_RT014_SCHEMA_BASELINE_SHAS
        )

    def test_rt015_schemas_match_baseline_sha(self):
        self._assert_baseline_matches(
            "RT-015 schemas", _FROZEN_RT015_SCHEMA_BASELINE_SHAS
        )

    def test_rt016_v1_schemas_match_pinned_baseline_sha(self):
        for rel, expected_sha in _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS.items():
            actual_sha = hashlib.sha256(self._read_frozen(rel)).hexdigest()
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


class FrozenBaselineExactSetTests(unittest.TestCase):
    """Detect **extra or missing** files in pinned frozen directories.

    The third-round fix requires that the "RT-011~015 (and their
    frozen schemas) zero-drift" promise cannot silently narrow by
    omission — for example, someone adding a new schema file that is
    not in the baseline would otherwise slip past the SHA test.

    For every directory in ``_EXACT_SET_DIRECTORY_MAP``:

    * Every ``*.schema.json`` present on disk must be a key of the
      per-directory baseline mapping (extra-detection).
    * Every mapping key must be a file present on disk
      (missing-detection).

    This closes the "silent narrowing" gap called out by the
    third-round review.
    """

    def test_pinned_directories_have_exact_set(self):
        for rel_dir, mapping in _EXACT_SET_DIRECTORY_MAP:
            # PR-001 Wave-0: listed through the guard's dir-fd walker so that
            # neither the directory nor its entries can be a symlink, and so
            # that ``is_file()`` cannot be satisfied by a symlink to a pinned
            # file elsewhere.  ``_list_dir`` returns lstat results, so
            # S_ISREG is a genuine regular-file test.
            entries = EG._list_dir(PROJECT, tuple(rel_dir.split("/")), label=rel_dir)
            self.assertTrue(entries, f"{rel_dir} is not a directory or is empty")
            for name, st in entries:
                self.assertFalse(
                    stat.S_ISLNK(st.st_mode), f"{rel_dir}/{name} is a symlink"
                )
            present = {
                f"{rel_dir}/{name}"
                for name, st in entries
                if stat.S_ISREG(st.st_mode) and name.endswith(".schema.json")
            }
            pinned = set(mapping.keys())
            extra = present - pinned
            missing = pinned - present
            self.assertFalse(
                extra,
                (
                    f"{rel_dir} contains schema file(s) NOT pinned in the "
                    f"third-round frozen baseline: {sorted(extra)}. "
                    "Add each to the corresponding _FROZEN_*_BASELINE_SHAS "
                    "map or explain why it is intentionally excluded."
                ),
            )
            self.assertFalse(
                missing,
                (
                    f"{rel_dir} is missing schema file(s) named in the "
                    f"pinned frozen baseline: {sorted(missing)}. If the "
                    "file was legitimately removed, delete its entry in "
                    "the baseline map (which will force review)."
                ),
            )

    def test_meta_missing_pin_would_be_detected(self):
        """Meta-test: prove the exact-set check catches unpinned files.

        Constructs a synthetic pinned map that intentionally OMITS
        ``access_tombstone.schema.json`` and reruns the extra-detection
        against the real ``rt015/schemas`` directory contents.  The
        omitted schema must show up in ``extra``.  If this assertion
        ever failed, the extra-detection logic itself would be silently
        broken and could hide RT-015 drift.
        """

        rel_dir = "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas"
        # Listed exactly the way test_pinned_directories_have_exact_set lists
        # it, so this meta-test validates the detector actually in use.
        present = {
            f"{rel_dir}/{name}"
            for name, st in EG._list_dir(PROJECT, tuple(rel_dir.split("/")), label=rel_dir)
            if stat.S_ISREG(st.st_mode) and name.endswith(".schema.json")
        }
        omitted_key = f"{rel_dir}/access_tombstone.schema.json"
        self.assertIn(
            omitted_key, present, f"{omitted_key} missing on disk"
        )
        synthetic = dict(_FROZEN_RT015_SCHEMA_BASELINE_SHAS)
        synthetic.pop(omitted_key, None)
        extra = present - set(synthetic.keys())
        self.assertIn(
            omitted_key,
            extra,
            (
                "Meta-guard broken: exact-set detector failed to surface a "
                "known-present schema that was intentionally omitted from "
                "the synthetic baseline."
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
