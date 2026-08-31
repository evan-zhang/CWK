"""RT-014: main behaviour test suite for SharedEvidenceStore.

Every test targets a specific requirement from the RT-014 plan §RT-014, the
DESIGN §C-07 flow, and the PRD FR-06 / AC-01 acceptance clauses.  The test
matrix here is *black-box*: it only touches the public ``SharedEvidenceStore``
API, the on-disk ``$CWK_INSTANCE_ROOT/shared/`` layout, and the RT-011 helpers
required to construct valid envelopes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_atomic_file as A  # noqa: E402
import cwk_shared_evidence as S  # noqa: E402


def _iso(hour: int = 10) -> str:
    return f"2026-08-01T{hour:02d}:00:00Z"


def _canonical_envelope(
    *,
    source_namespace: str = "cwork",
    report_id: str = "2070001",
    body: str = "正文-α",
    title: str = "汇报-1",
) -> dict:
    envelope = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": source_namespace,
        "report_id": report_id,
        "title": title,
        "author": {"source_user_id": "u_writer_1", "display_name": "张三"},
        "created_at": _iso(10),
        "source_updated_at": _iso(12),
        "body": body,
        "normalizer_version": "v1",
    }
    envelope["canonical_sha256"] = C.canonical_sha256(
        {k: v for k, v in envelope.items() if k != "canonical_sha256"}
    )
    return envelope


class _StoreFixture:
    """Helper: TemporaryDirectory + InstanceLayout + initialised store."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_env = os.environ.get("CWK_INSTANCE_ROOT")
        os.environ["CWK_INSTANCE_ROOT"] = str(Path(self._tmp.name).resolve())
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.store = S.SharedEvidenceStore.open(self.layout)
        self.store.initialize()

    def close(self) -> None:
        self.layout.close()
        if self._prev_env is None:
            os.environ.pop("CWK_INSTANCE_ROOT", None)
        else:
            os.environ["CWK_INSTANCE_ROOT"] = self._prev_env
        self._tmp.cleanup()

    @property
    def root(self) -> Path:
        return Path(self._tmp.name)


class _StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _StoreFixture()

    def tearDown(self) -> None:
        self.fx.close()


# ---------------------------------------------------------------------------
# 1. Initialisation and required layout
# ---------------------------------------------------------------------------


class InitializationTests(_StoreTest):
    def test_initialize_creates_frozen_subtree(self):
        shared = self.fx.root / "shared"
        for name in S.SHARED_CHILDREN:
            path = shared / name
            self.assertTrue(path.is_dir(), f"missing shared/{name}")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_initialize_is_idempotent(self):
        for _ in range(3):
            self.fx.store.initialize()
        for name in S.SHARED_CHILDREN:
            self.assertTrue((self.fx.root / "shared" / name).is_dir())

    def test_publish_before_initialize_fails_closed(self):
        # Fresh store without initialize() — the shared/ children must not
        # exist for our sub-store, so publish must fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CWK_INSTANCE_ROOT"] = str(Path(tmp).resolve())
            with I.InstanceLayout.open() as layout:
                layout.initialize()  # RT-012 layout only creates shared/ leaf
                store = S.SharedEvidenceStore.open(layout)
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    store.publish(_canonical_envelope())
                self.assertEqual(cm.exception.code, "not_initialized")

    def test_read_before_initialize_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CWK_INSTANCE_ROOT"] = str(Path(tmp).resolve())
            with I.InstanceLayout.open() as layout:
                layout.initialize()
                store = S.SharedEvidenceStore.open(layout)
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    store.read_version("cwork:207", "0" * 64)
                self.assertEqual(cm.exception.code, "not_initialized")


# ---------------------------------------------------------------------------
# 2. Public surface locked down
# ---------------------------------------------------------------------------


class PublicSurfaceTests(_StoreTest):
    FORBIDDEN_METHODS = (
        "rollback",
        "delete",
        "delete_object",
        "delete_version",
        "unlink_object",
        "truncate_catalog",
        "list_versions",
        "list_report_keys",
        "list_objects",
        "iter_reports",
        "has_version",
        "object_exists",
        "sha_exists",
        "enumerate",
    )

    def test_no_enumeration_or_rollback_api(self):
        for name in self.FORBIDDEN_METHODS:
            self.assertFalse(
                hasattr(S.SharedEvidenceStore, name),
                f"SharedEvidenceStore must not expose .{name}()",
            )
            self.assertFalse(
                hasattr(self.fx.store, name),
                f"SharedEvidenceStore instance must not expose .{name}()",
            )

    def test_public_dunder_is_minimal(self):
        # Public methods (no leading underscore) allowed on the store.
        allowed = {"open", "initialize", "publish", "read_version", "recover"}
        actual_public = {
            name
            for name in dir(S.SharedEvidenceStore)
            if not name.startswith("_") and callable(getattr(S.SharedEvidenceStore, name))
        }
        self.assertTrue(
            actual_public <= allowed,
            f"unexpected public methods on store: {actual_public - allowed}",
        )

    def test_no_cli_module(self):
        # The RT-014 module must not register itself with the tenant CLI
        # dispatcher (frozen provider slots owned by RT-012).
        import cwk_tenant_cli as CLI  # noqa: WPS433
        self.assertNotIn("cwk_shared_evidence", CLI.FROZEN_PROVIDER_SLOTS)


# ---------------------------------------------------------------------------
# 3. Publish + Idempotency + Version append
# ---------------------------------------------------------------------------


class PublishTests(_StoreTest):
    def test_first_publish_creates_object_and_catalog(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        self.assertTrue(r.is_new_version)
        self.assertTrue(r.is_new_report)
        self.assertEqual(r.catalog_revision, 1)
        self.assertEqual(r.report_key, "cwork:2070001")
        self.assertRegex(r.object_id, r"^o_[a-z2-7]{26}$")
        self.assertRegex(r.catalog_key, r"^r_[a-z2-7]{26}$")
        self.assertEqual(r.canonical_sha256, env["canonical_sha256"])
        # Object file exists under objects/<shard>/<object_id>.json
        shard = r.object_id[2:4]
        obj_path = (
            self.fx.root
            / "shared"
            / "objects"
            / shard
            / f"{r.object_id}.json"
        )
        self.assertTrue(obj_path.is_file())
        # Object bytes == canonical JCS of envelope
        self.assertEqual(obj_path.read_bytes(), C.canonical_json_bytes(env))
        # Perms
        self.assertEqual(stat.S_IMODE(obj_path.stat().st_mode), A.FILE_MODE)
        # Catalog head + jsonl exist under report-versions/<catalog_key>/
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        self.assertTrue((cat_dir / "catalog.head").is_file())
        self.assertTrue((cat_dir / "catalog.jsonl").is_file())

    def test_same_envelope_publish_is_idempotent(self):
        env = _canonical_envelope()
        r1 = self.fx.store.publish(env)
        r2 = self.fx.store.publish(env)
        r3 = self.fx.store.publish(env)
        self.assertEqual(r1.object_id, r2.object_id)
        self.assertEqual(r2.object_id, r3.object_id)
        self.assertFalse(r2.is_new_version)
        self.assertFalse(r3.is_new_version)
        self.assertEqual(r1.catalog_revision, 1)
        self.assertEqual(r2.catalog_revision, 1)
        self.assertEqual(r3.catalog_revision, 1)
        # Catalog file contains a single line.
        cat_dir = (
            self.fx.root / "shared" / "report-versions" / r1.catalog_key
        )
        lines = (cat_dir / "catalog.jsonl").read_bytes().count(b"\n")
        self.assertEqual(lines, 1)

    def test_new_version_appends_and_preserves_old(self):
        env1 = _canonical_envelope(body="v1")
        env2 = _canonical_envelope(body="v2 with é and 汉字")
        r1 = self.fx.store.publish(env1)
        r2 = self.fx.store.publish(env2)
        self.assertNotEqual(r1.object_id, r2.object_id)
        self.assertTrue(r2.is_new_version)
        self.assertFalse(r2.is_new_report)
        self.assertEqual(r2.catalog_revision, 2)
        # Both objects exist
        for r in (r1, r2):
            shard = r.object_id[2:4]
            obj_path = (
                self.fx.root
                / "shared"
                / "objects"
                / shard
                / f"{r.object_id}.json"
            )
            self.assertTrue(obj_path.is_file())
        # Both are readable
        self.assertEqual(
            self.fx.store.read_version(r1.report_key, r1.canonical_sha256),
            env1,
        )
        self.assertEqual(
            self.fx.store.read_version(r2.report_key, r2.canonical_sha256),
            env2,
        )

    def test_receipt_to_dict_validates(self):
        r = self.fx.store.publish(_canonical_envelope())
        payload = r.to_dict()
        self.assertEqual(payload["schema"], "cwk.rt014.publish_receipt.v1")
        # Round-trip via strict json.
        raw = C.canonical_json_bytes(payload)
        again = C.strict_json_loads(raw.decode("utf-8"))
        self.assertEqual(again, payload)

    def test_publish_rejects_non_dict(self):
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish("not a dict")
        self.assertEqual(cm.exception.code, "contract")

    def test_publish_rejects_envelope_with_wrong_sha(self):
        env = _canonical_envelope()
        env["canonical_sha256"] = "f" * 64  # deliberately wrong
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")


# ---------------------------------------------------------------------------
# 4. Isolation between namespaces / report_ids
# ---------------------------------------------------------------------------


class IsolationTests(_StoreTest):
    def test_different_namespace_same_body_isolates(self):
        env_a = _canonical_envelope(source_namespace="cwork", body="same")
        env_b = _canonical_envelope(source_namespace="wework", body="same")
        r_a = self.fx.store.publish(env_a)
        r_b = self.fx.store.publish(env_b)
        self.assertNotEqual(r_a.catalog_key, r_b.catalog_key)
        self.assertNotEqual(r_a.object_id, r_b.object_id)
        self.assertNotEqual(r_a.report_key, r_b.report_key)
        # canonical_sha256 differs because source_namespace is part of the
        # canonical envelope.
        self.assertNotEqual(env_a["canonical_sha256"], env_b["canonical_sha256"])

    def test_different_report_id_same_body_isolates(self):
        env_a = _canonical_envelope(report_id="R-A", body="same")
        env_b = _canonical_envelope(report_id="R-B", body="same")
        r_a = self.fx.store.publish(env_a)
        r_b = self.fx.store.publish(env_b)
        self.assertNotEqual(r_a.catalog_key, r_b.catalog_key)
        self.assertNotEqual(r_a.object_id, r_b.object_id)

    def test_same_body_across_reports_produces_distinct_objects(self):
        # Even with same body, source_namespace/report_id difference forces
        # distinct canonical envelopes -> distinct object bytes -> distinct SHA.
        env_a = _canonical_envelope(report_id="ID-1")
        env_b = _canonical_envelope(report_id="ID-2")
        r_a = self.fx.store.publish(env_a)
        r_b = self.fx.store.publish(env_b)
        self.assertNotEqual(r_a.object_id, r_b.object_id)
        # Each catalog only has one entry
        cat_a = self.fx.root / "shared" / "report-versions" / r_a.catalog_key
        cat_b = self.fx.root / "shared" / "report-versions" / r_b.catalog_key
        self.assertEqual((cat_a / "catalog.jsonl").read_bytes().count(b"\n"), 1)
        self.assertEqual((cat_b / "catalog.jsonl").read_bytes().count(b"\n"), 1)

    def test_report_id_never_appears_in_filesystem(self):
        rid = "ABC-SECRET-REPORT-42"
        env = _canonical_envelope(report_id=rid)
        self.fx.store.publish(env)
        # Walk shared/ recursively and confirm no filename contains rid.
        shared = self.fx.root / "shared"
        for root, dirs, files in os.walk(shared):
            for name in dirs + files:
                self.assertNotIn(rid, name, f"report_id leaked in filesystem: {name}")


# ---------------------------------------------------------------------------
# 5. Read + verification
# ---------------------------------------------------------------------------


class ReadTests(_StoreTest):
    def test_read_round_trip(self):
        env = _canonical_envelope()
        self.fx.store.publish(env)
        got = self.fx.store.read_version("cwork:2070001", env["canonical_sha256"])
        self.assertEqual(got, env)

    def test_read_not_found_for_unknown_report(self):
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version("cwork:0000", "0" * 64)
        self.assertEqual(cm.exception.code, "not_found")

    def test_read_not_found_for_unknown_sha(self):
        env = _canonical_envelope()
        self.fx.store.publish(env)
        # Different SHA than published
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version("cwork:2070001", "f" * 64)
        self.assertEqual(cm.exception.code, "not_found")

    def test_read_invalid_report_key(self):
        for bad in ("", "no-colon", "UPPER:x", "cwork::x", "cwork:" + "a" * 200):
            with self.subTest(bad=bad):
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    self.fx.store.read_version(bad, "0" * 64)
                self.assertEqual(cm.exception.code, "contract")

    def test_read_invalid_sha(self):
        for bad in ("", "notahash", "0" * 63, "0" * 65, "G" * 64):
            with self.subTest(bad=bad):
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    self.fx.store.read_version("cwork:x", bad)
                self.assertEqual(cm.exception.code, "contract")


# ---------------------------------------------------------------------------
# 6. Recovery
# ---------------------------------------------------------------------------


class RecoveryTests(_StoreTest):
    def test_recover_reports_no_issues_on_healthy_store(self):
        for i in range(3):
            self.fx.store.publish(_canonical_envelope(report_id=f"R{i}"))
        report = self.fx.store.recover()
        self.assertEqual(report.catalog_dirs_scanned, 3)
        self.assertEqual(report.objects_verified, 3)
        self.assertEqual(report.catalog_issues, [])
        # Emitted report validates against the schema.
        report.to_dict()

    def test_recover_removes_staging_orphans(self):
        # Simulate a crash between temp create and rename in staging/: create
        # a bare .cwk-tmp-* file inside shared/staging/.
        staging = self.fx.root / "shared" / "staging"
        orphan = staging / f"{A.TEMP_PREFIX}fake.deadbeef"
        orphan.write_bytes(b"junk")
        report = self.fx.store.recover()
        self.assertIn(orphan.name, report.staging_orphans_removed)
        self.assertFalse(orphan.exists())

    def test_recover_removes_per_report_orphans(self):
        env = _canonical_envelope()
        self.fx.store.publish(env)
        catalog_key = S._catalog_key("cwork:2070001")
        report_dir = (
            self.fx.root / "shared" / "report-versions" / catalog_key
        )
        orphan = report_dir / f"{A.TEMP_PREFIX}fake.dead"
        orphan.write_bytes(b"junk")
        report = self.fx.store.recover()
        self.assertFalse(orphan.exists())
        # Object is still verified
        self.assertEqual(report.objects_verified, 1)

    def test_recover_reports_missing_object(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        # Delete the object file to simulate corruption / manual mistake.
        shard = r.object_id[2:4]
        obj_path = (
            self.fx.root
            / "shared"
            / "objects"
            / shard
            / f"{r.object_id}.json"
        )
        obj_path.unlink()
        report = self.fx.store.recover()
        codes = [i["code"] for i in report.catalog_issues]
        self.assertIn("missing_object", codes)

    def test_recover_reports_sha_mismatch(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        shard = r.object_id[2:4]
        obj_path = (
            self.fx.root / "shared" / "objects" / shard / f"{r.object_id}.json"
        )
        obj_path.write_bytes(obj_path.read_bytes()[:-1] + b"X")
        report = self.fx.store.recover()
        codes = [i["code"] for i in report.catalog_issues]
        self.assertIn("sha_mismatch", codes)

    def test_recover_never_deletes_object_files(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        shard = r.object_id[2:4]
        obj_path = (
            self.fx.root / "shared" / "objects" / shard / f"{r.object_id}.json"
        )
        pre = obj_path.read_bytes()
        # Even if catalog is corrupted, recover() should not touch the object.
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        (cat_dir / "catalog.head").write_bytes(b"corrupt")
        report = self.fx.store.recover()
        self.assertTrue(obj_path.exists())
        self.assertEqual(obj_path.read_bytes(), pre)
        codes = [i["code"] for i in report.catalog_issues]
        self.assertIn("corrupt_catalog", codes)

    def test_recover_flags_orphan_objects(self):
        # Simulate a mid-publish crash: object exists, no catalog entry.
        env = _canonical_envelope()
        body_bytes = C.canonical_json_bytes(env)
        object_id = C.new_object_id()
        shard = object_id[2:4]
        shard_dir = self.fx.root / "shared" / "objects" / shard
        shard_dir.mkdir(mode=0o700, exist_ok=True)
        (shard_dir / f"{object_id}.json").write_bytes(body_bytes)
        report = self.fx.store.recover()
        codes = [i["code"] for i in report.catalog_issues]
        self.assertIn("orphan_object", codes)


# ---------------------------------------------------------------------------
# 7. Serialisation contracts (JCS stability)
# ---------------------------------------------------------------------------


class SerialisationTests(_StoreTest):
    def test_object_bytes_are_exact_jcs(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        shard = r.object_id[2:4]
        obj_path = (
            self.fx.root / "shared" / "objects" / shard / f"{r.object_id}.json"
        )
        raw = obj_path.read_bytes()
        # Round-trip: parse and re-encode via JCS -> exactly equal.
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(C.canonical_json_bytes(payload), raw)

    def test_catalog_head_records_matching_hashes(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        jsonl = (cat_dir / "catalog.jsonl").read_bytes()
        head = json.loads((cat_dir / "catalog.head").read_bytes().decode("utf-8"))
        self.assertEqual(
            head["catalog_jsonl_sha256"], hashlib.sha256(jsonl).hexdigest()
        )
        self.assertEqual(head["entry_count"], 1)
        self.assertEqual(head["latest_object_id"], r.object_id)
        self.assertEqual(head["latest_canonical_sha256"], env["canonical_sha256"])


if __name__ == "__main__":
    unittest.main()
