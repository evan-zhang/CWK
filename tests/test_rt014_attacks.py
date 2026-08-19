"""RT-014: adversarial / negative test matrix for SharedEvidenceStore.

Coverage:

- Deep tenant/lane/reply/node/attachment/temporary_url/credential injection
  into a canonical envelope (RT-011 deep-forbidden scan).
- Fake ``verified_shared_extensions_ref`` fields.
- Symlink / hardlink attacks on object files.
- Pre-populated staging temp-file names.
- Bit-flips and canonical-drift tampering.
- Catalog head vs jsonl SHA drift; catalog entry with wrong report_key or
  wrong object_id.
- Error-message opacity: no host paths, no report_id plaintext, no body bytes.
- CanonicalEnvelope with slot names that only RT-011 forbids (e.g. reply
  overlay inside author.display_name) must be rejected before publish.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
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


def _canonical_envelope(**overrides) -> dict:
    envelope = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": "cwork",
        "report_id": "2070001",
        "title": "汇报",
        "author": {"source_user_id": "u_writer", "display_name": "张三"},
        "created_at": _iso(10),
        "source_updated_at": _iso(12),
        "body": "α",
        "normalizer_version": "v1",
    }
    envelope.update(overrides)
    envelope["canonical_sha256"] = C.canonical_sha256(
        {k: v for k, v in envelope.items() if k != "canonical_sha256"}
    )
    return envelope


class _Fx:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("CWK_INSTANCE_ROOT")
        os.environ["CWK_INSTANCE_ROOT"] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.store = S.SharedEvidenceStore.open(self.layout)
        self.store.initialize()

    def close(self) -> None:
        if self._prev is None:
            os.environ.pop("CWK_INSTANCE_ROOT", None)
        else:
            os.environ["CWK_INSTANCE_ROOT"] = self._prev
        self._tmp.cleanup()

    @property
    def root(self) -> Path:
        return Path(self._tmp.name)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fx()

    def tearDown(self) -> None:
        self.fx.close()


# ---------------------------------------------------------------------------
# 1. Deep overlay injection (RT-011 forbidden field scan on canonical)
# ---------------------------------------------------------------------------


class DeepOverlayInjectionTests(_Base):
    FORBIDDEN_TOP_LEVEL = (
        {"tenant_id": "t_abc"},
        {"agent_id": "gw_1"},
        {"agent_id_hash": "a" * 64},
        {"credential_ref": "secret://x"},
        {"credentials": {"kind": "env"}},
        {"app_key": "SECRET"},
        {"lane": "received"},
        {"read_status": "unread"},
        {"todo_status": "pending"},
        {"allowed_actions": ["reply"]},
        {"role": "receiver"},
        {"roles": ["receiver"]},
        {"reply": [{"text": "hi"}]},
        {"replies": [{"text": "hi"}]},
        {"reply_overlay": {}},
        {"node": {"id": 1}},
        {"nodes": []},
        {"node_overlay": {}},
        {"attachment": {"name": "x"}},
        {"attachments": []},
        {"attachment_permissions": []},
        {"attachment_url": "https://cwork/x"},
        {"preview_url": "https://cwork/y"},
        {"short_url": "https://cwork/z"},
        {"presign_url": "https://cwork/p"},
        {"download_url": "https://cwork/d"},
        {"temporary_url": "https://cwork/t"},
        {"collected_at": _iso(9)},
        {"observed_at": _iso(9)},
        {"path": "/tmp/x"},
        {"absolute_path": "/etc/passwd"},
        {"mirror_root": "/mirror"},
        {"auth_epoch": 1},
        {"binding_epoch": 1},
        {"cookie": "sid=x"},
        {"session_token": "tok"},
        {"authorization": "Bearer y"},
    )

    def test_top_level_forbidden_fields_rejected(self):
        for extra in self.FORBIDDEN_TOP_LEVEL:
            key = next(iter(extra))
            with self.subTest(field=key):
                env = _canonical_envelope(**extra)
                # Re-compute sha because extra was folded in.
                env["canonical_sha256"] = C.canonical_sha256(
                    {k: v for k, v in env.items() if k != "canonical_sha256"}
                )
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    self.fx.store.publish(env)
                self.assertEqual(cm.exception.code, "contract")

    def test_nested_forbidden_field_in_author_rejected(self):
        env = _canonical_envelope()
        env["author"] = {"source_user_id": "u1", "display_name": "x", "temporary_url": "http://x"}
        env["canonical_sha256"] = C.canonical_sha256(
            {k: v for k, v in env.items() if k != "canonical_sha256"}
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")

    def test_completely_unrelated_top_level_field_rejected(self):
        env = _canonical_envelope()
        env["evil_extension"] = {"reply": "hi"}
        env["canonical_sha256"] = C.canonical_sha256(
            {k: v for k, v in env.items() if k != "canonical_sha256"}
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")

    def test_report_id_with_path_traversal_rejected(self):
        # RT-011 REPORT_ID_REGEX only allows [A-Za-z0-9][A-Za-z0-9_-.]*, so
        # anything containing / or .. must be rejected before publish.
        for bad in ("../../etc/passwd", "id/../boom", "id with space", "id\n"):
            with self.subTest(bad=bad):
                # We must construct envelope manually because _canonical_envelope
                # would recompute the sha successfully; RT-011 schema regex
                # will still refuse.
                env = {
                    "schema": "cwk.canonical_report.v1",
                    "source_namespace": "cwork",
                    "report_id": bad,
                    "title": "x",
                    "author": {"source_user_id": "u1"},
                    "created_at": _iso(10),
                    "source_updated_at": _iso(12),
                    "body": "α",
                    "normalizer_version": "v1",
                }
                env["canonical_sha256"] = C.canonical_sha256(env)
                with self.assertRaises(S.SharedEvidenceError) as cm:
                    self.fx.store.publish(env)
                self.assertEqual(cm.exception.code, "contract")


# ---------------------------------------------------------------------------
# 2. Fake verified_shared_extensions_ref
# ---------------------------------------------------------------------------


class ExtensionsRefTests(_Base):
    def test_extensions_ref_with_bad_version_rejected(self):
        env = _canonical_envelope()
        env["verified_shared_extensions_ref"] = {"version": "not-a-version", "sha256": "a" * 64}
        env["canonical_sha256"] = C.canonical_sha256(
            {k: v for k, v in env.items() if k != "canonical_sha256"}
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")

    def test_extensions_ref_with_bad_sha_rejected(self):
        env = _canonical_envelope()
        env["verified_shared_extensions_ref"] = {"version": "v1", "sha256": "not-a-hash"}
        env["canonical_sha256"] = C.canonical_sha256(
            {k: v for k, v in env.items() if k != "canonical_sha256"}
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")

    def test_extensions_ref_with_extra_key_rejected(self):
        env = _canonical_envelope()
        env["verified_shared_extensions_ref"] = {
            "version": "v1",
            "sha256": "a" * 64,
            "temporary_url": "http://x",
        }
        env["canonical_sha256"] = C.canonical_sha256(
            {k: v for k, v in env.items() if k != "canonical_sha256"}
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.publish(env)
        self.assertEqual(cm.exception.code, "contract")


# ---------------------------------------------------------------------------
# 3. Corruption / bit flips / drift
# ---------------------------------------------------------------------------


class CorruptionTests(_Base):
    def _publish_one(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        return env, r

    def _object_path(self, r):
        return (
            self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
        )

    def test_bit_flip_in_object_body_detected(self):
        env, r = self._publish_one()
        obj = self._object_path(r)
        b = bytearray(obj.read_bytes())
        b[10] ^= 0x01
        obj.write_bytes(bytes(b))
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        self.assertEqual(cm.exception.code, "sha_mismatch")

    def test_canonical_drift_semantically_equivalent_bytes_rejected(self):
        env, r = self._publish_one()
        # Rewrite the object as prettified JSON (semantically equal but
        # different bytes and different SHA).  This should fail sha_mismatch
        # because the object_bytes_sha256 stored in the catalog matches only
        # the JCS bytes.
        obj = self._object_path(r)
        payload = json.loads(obj.read_bytes().decode("utf-8"))
        pretty = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        obj.write_bytes(pretty)
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        # sha_mismatch fires first (bytes SHA vs catalog SHA)
        self.assertIn(cm.exception.code, {"sha_mismatch", "canonical_drift"})

    def test_object_replaced_with_different_valid_envelope(self):
        # Publish A and B, then swap A's file bytes with B's -> A's catalog
        # still points to A's object_id, so we get sha_mismatch on read A.
        env_a, r_a = self._publish_one()
        env_b = _canonical_envelope(report_id="ID-B", body="β")
        r_b = self.fx.store.publish(env_b)
        pa = self._object_path(r_a)
        pb = self._object_path(r_b)
        pa.write_bytes(pb.read_bytes())
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r_a.report_key, r_a.canonical_sha256)
        self.assertEqual(cm.exception.code, "sha_mismatch")

    def test_catalog_jsonl_tampered_head_sha_mismatch(self):
        env, r = self._publish_one()
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        jsonl = cat_dir / "catalog.jsonl"
        jsonl.write_bytes(jsonl.read_bytes() + b"\n")
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        self.assertEqual(cm.exception.code, "corrupt_catalog")

    def test_catalog_head_wrong_catalog_key_rejected(self):
        env, r = self._publish_one()
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        head = json.loads((cat_dir / "catalog.head").read_bytes().decode("utf-8"))
        head["catalog_key"] = "r_" + "b" * 26
        (cat_dir / "catalog.head").write_bytes(
            C.canonical_json_bytes(head)
        )
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        # Either corrupt_catalog (head_key mismatch) or SHA drift
        self.assertIn(cm.exception.code, {"corrupt_catalog", "report_key_mismatch"})

    def test_catalog_entry_report_key_mutated(self):
        env, r = self._publish_one()
        cat_dir = self.fx.root / "shared" / "report-versions" / r.catalog_key
        # Rewrite jsonl with a different report_key -> read raises
        # report_key_mismatch (schema still passes because grammar is preserved).
        entry = json.loads(
            (cat_dir / "catalog.jsonl").read_bytes().decode("utf-8").strip()
        )
        entry["report_key"] = "cwork:OTHER-ID"
        new_line = C.canonical_json_bytes(entry) + b"\n"
        (cat_dir / "catalog.jsonl").write_bytes(new_line)
        # Head sha still matches the old jsonl, so we get corrupt_catalog first.
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        self.assertIn(cm.exception.code, {"corrupt_catalog", "report_key_mismatch"})

    def test_catalog_entry_object_id_swapped_to_another(self):
        # Two reports published, then swap the object_id of entry A to point
        # to B's object.
        env_a, r_a = self._publish_one()
        env_b = _canonical_envelope(report_id="B-ID")
        r_b = self.fx.store.publish(env_b)
        cat_dir = self.fx.root / "shared" / "report-versions" / r_a.catalog_key
        entry = json.loads(
            (cat_dir / "catalog.jsonl").read_bytes().decode("utf-8").strip()
        )
        entry["object_id"] = r_b.object_id
        new_line = C.canonical_json_bytes(entry) + b"\n"
        (cat_dir / "catalog.jsonl").write_bytes(new_line)
        # Head sha mismatch first
        with self.assertRaises(S.SharedEvidenceError):
            self.fx.store.read_version(r_a.report_key, r_a.canonical_sha256)


# ---------------------------------------------------------------------------
# 4. Symlink / hardlink attacks
# ---------------------------------------------------------------------------


class SymlinkAttackTests(_Base):
    def test_object_replaced_with_symlink_refused(self):
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        obj = (
            self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
        )
        # Move original aside, replace with symlink to another random file.
        elsewhere = self.fx.root / "decoy.json"
        obj.rename(elsewhere)
        os.symlink(elsewhere, obj)
        with self.assertRaises(S.SharedEvidenceError):
            self.fx.store.read_version(r.report_key, r.canonical_sha256)

    def test_report_dir_symlink_refused(self):
        # Attacker plants a symlink under report-versions/ pretending to be a
        # catalog dir; opening it via _openat_dir_nofollow must fail closed.
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        rv = self.fx.root / "shared" / "report-versions"
        evil = rv / ("r_" + "z" * 26)
        # Symlink to a real dir elsewhere with a valid-looking catalog.head
        # would still get O_NOFOLLOW-blocked at open time.
        real_target = self.fx.root / "elsewhere"
        real_target.mkdir()
        (real_target / "catalog.head").write_bytes(b"whatever")
        os.symlink(real_target, evil)
        # Even calling into read for a totally unrelated report_key should
        # not open the symlinked dir.  Recover should skip it too.
        report = self.fx.store.recover()
        # No object verified for the symlink; no crash.
        # The evil symlink is still present but not followed.
        self.assertTrue(evil.is_symlink())

    def test_object_replaced_with_hardlink_refused(self):
        # Hardlink another file over the object -> nlink>1 -> read_file rejects
        env = _canonical_envelope()
        r = self.fx.store.publish(env)
        obj = (
            self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
        )
        # Create a hardlink; the resulting file has nlink=2
        second = self.fx.root / "second-hardlink.json"
        os.link(obj, second)
        with self.assertRaises(S.SharedEvidenceError):
            self.fx.store.read_version(r.report_key, r.canonical_sha256)


# ---------------------------------------------------------------------------
# 5. Staging pre-fill (temp-file collision)
# ---------------------------------------------------------------------------


class StagingPrefillTests(_Base):
    def test_prefilled_staging_names_do_not_affect_publish(self):
        staging = self.fx.root / "shared" / "staging"
        # Attempt to squat on temp names that MAY be produced by write_atomic.
        for i in range(50):
            (staging / f"{A.TEMP_PREFIX}object.deadbeef{i:04x}").write_bytes(b"junk")
        env = _canonical_envelope()
        # publish should still succeed: temp-files are created in the target
        # directory (shards / report dirs), not in staging/.
        r = self.fx.store.publish(env)
        # And recover cleans the prefilled staging orphans.
        report = self.fx.store.recover()
        self.assertGreaterEqual(len(report.staging_orphans_removed), 50)


# ---------------------------------------------------------------------------
# 6. Error opacity: no host path / body / report_id leaks
# ---------------------------------------------------------------------------


class ErrorOpacityTests(_Base):
    def test_not_found_error_does_not_leak_report_id_or_path(self):
        # Publish nothing; the request contains a secret-shaped report_id
        # that we should not see echoed back.
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version("cwork:MY-SECRET-42", "0" * 64)
        msg = str(cm.exception)
        self.assertNotIn("MY-SECRET-42", msg)
        # Message never contains the on-disk absolute path prefix
        self.assertNotIn(str(self.fx.root), msg)

    def test_error_does_not_leak_body_bytes(self):
        env = _canonical_envelope(body="THE-SECRET-BODY-XYZZY")
        r = self.fx.store.publish(env)
        # Corrupt object
        obj = (
            self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
        )
        obj.write_bytes(b"\x00" * len(obj.read_bytes()))
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version(r.report_key, r.canonical_sha256)
        msg = str(cm.exception)
        self.assertNotIn("THE-SECRET-BODY-XYZZY", msg)
        self.assertNotIn(str(self.fx.root), msg)

    def test_error_contains_stable_code_only(self):
        with self.assertRaises(S.SharedEvidenceError) as cm:
            self.fx.store.read_version("cwork:x", "0" * 64)
        # Code is always present and part of the closed vocabulary
        self.assertIn(cm.exception.code, S.SharedEvidenceError._CODES)


# ---------------------------------------------------------------------------
# 7. Immutability: no delete / rollback path
# ---------------------------------------------------------------------------


class ImmutabilityTests(_Base):
    def test_publish_never_unlinks_prior_versions(self):
        # 3 versions of the same report_key; all three object files must survive.
        rs = []
        for i in range(3):
            env = _canonical_envelope(body=f"v{i}")
            rs.append(self.fx.store.publish(env))
        for r in rs:
            obj = (
                self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
            )
            self.assertTrue(obj.is_file(), f"object {r.object_id} should still exist")

    def test_recover_never_touches_committed_objects(self):
        # After recover(), all previously published objects must still exist
        # with identical bytes.
        publishes = []
        for i in range(5):
            env = _canonical_envelope(report_id=f"R{i}")
            r = self.fx.store.publish(env)
            obj = (
                self.fx.root / "shared" / "objects" / r.object_id[2:4] / f"{r.object_id}.json"
            )
            publishes.append((r.object_id, obj, obj.read_bytes()))
        # Add some staging orphans
        st = self.fx.root / "shared" / "staging"
        for i in range(3):
            (st / f"{A.TEMP_PREFIX}orphan.abc{i}").write_bytes(b"junk")
        self.fx.store.recover()
        for oid, path, expected in publishes:
            self.assertTrue(path.exists(), f"{oid} disappeared during recover")
            self.assertEqual(path.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
