"""RT-017 contract and attack tests for CanonicalVersionProvider v1."""

from __future__ import annotations

import ast
import base64
import copy
import dataclasses
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"
SCHEMA_PATH = (
    PROJECT
    / "PR"
    / "PR-001-multitenant-knowledge-spaces"
    / "contracts"
    / "rt017"
    / "schemas"
    / "canonical_version_snapshot_v1.schema.json"
)
RUNTIME_PATH = SCRIPTS / "cwk_canonical_version_provider.py"
sys.path.insert(0, str(SCRIPTS))

import cwk_canonical_version_provider as P  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_shared_evidence as S  # noqa: E402


def _iso(hour: int) -> str:
    return f"2026-08-20T{hour:02d}:00:00Z"


def _envelope(*, body: str = "canonical body", hour: int = 10) -> dict:
    payload = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": "cwork",
        "report_id": "2070001",
        "title": "report title",
        "author": {"source_user_id": "u_writer_1", "display_name": "Writer"},
        "created_at": _iso(9),
        "source_updated_at": _iso(hour),
        "body": body,
        "normalizer_version": "v1",
    }
    payload["canonical_sha256"] = C.canonical_sha256(payload)
    return payload


class _Fixture:
    def __init__(self, *, publish: bool = True) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # macOS exposes its default temporary root through /var, which is a
        # symlink to /private/var.  Production InstanceLayout correctly rejects
        # user-supplied ancestor symlinks, so the test fixture must pass the
        # canonical path rather than weakening the runtime check.
        self.root = Path(self.temp.name).resolve()
        self.layout = I.InstanceLayout.open(root=str(self.root))
        self.layout.initialize()
        self.store = S.SharedEvidenceStore.open(self.layout)
        self.store.initialize()
        self.receipts: list[S.PublishReceipt] = []
        if publish:
            self.receipts.append(self.store.publish(_envelope()))
        self.provider = P.CanonicalVersionProvider(layout=self.layout)

    def close(self) -> None:
        self.temp.cleanup()

    @property
    def receipt(self) -> S.PublishReceipt:
        return self.receipts[-1]

    @property
    def catalog_dir(self) -> Path:
        return (
            self.root
            / "shared"
            / "report-versions"
            / self.receipt.catalog_key
        )

    @property
    def head_path(self) -> Path:
        return self.catalog_dir / "catalog.head"

    @property
    def jsonl_path(self) -> Path:
        return self.catalog_dir / "catalog.jsonl"

    @property
    def object_path(self) -> Path:
        oid = self.receipt.object_id
        return self.root / "shared" / "objects" / oid[2:4] / f"{oid}.json"

    def head(self) -> dict:
        return C.strict_json_loads(self.head_path.read_text(encoding="utf-8"))

    def entries(self) -> list[dict]:
        return [
            C.strict_json_loads(line.decode("utf-8"))
            for line in self.jsonl_path.read_bytes().splitlines()
        ]

    def write_head(self, head: dict) -> None:
        self.head_path.write_bytes(C.canonical_json_bytes(head))

    def write_entries(self, entries: list[dict], *, align_head: bool = True) -> None:
        raw = b"".join(C.canonical_json_bytes(entry) + b"\n" for entry in entries)
        self.jsonl_path.write_bytes(raw)
        if align_head:
            head = self.head()
            head["entry_count"] = len(entries)
            head["head_revision"] = len(entries)
            head["catalog_jsonl_sha256"] = hashlib.sha256(raw).hexdigest()
            head["latest_object_id"] = entries[-1]["object_id"]
            head["latest_canonical_sha256"] = entries[-1]["canonical_sha256"]
            self.write_head(head)


class _ProviderTest(unittest.TestCase):
    publish = True

    def setUp(self) -> None:
        self.fx = _Fixture(publish=self.publish)

    def tearDown(self) -> None:
        self.fx.close()

    def resolve(self) -> P.CanonicalVersionSnapshotV1:
        return self.fx.provider.resolve_current(report_key="cwork:2070001")


class ContractSurfaceTests(_ProviderTest):
    def test_constant_protocol_and_exact_parameter_signature(self):
        self.assertEqual(
            P.CANONICAL_VERSION_PROVIDER_API_VERSION,
            "cwk.canonical_version_provider.v1",
        )
        self.assertEqual(
            P.CanonicalVersionProvider.API_VERSION,
            P.CANONICAL_VERSION_PROVIDER_API_VERSION,
        )
        self.assertEqual(
            P.NullCanonicalVersionProvider.API_VERSION,
            P.CANONICAL_VERSION_PROVIDER_API_VERSION,
        )
        for owner in (P.CanonicalVersionProviderV1, P.CanonicalVersionProvider):
            parameters = inspect.signature(owner.resolve_current).parameters
            self.assertEqual(tuple(parameters), ("self", "report_key"))
            self.assertEqual(
                parameters["report_key"].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
            self.assertIs(parameters["report_key"].default, inspect.Parameter.empty)

    def test_snapshot_dataclass_and_payload_are_exactly_five_fields(self):
        expected = (
            "schema",
            "report_key",
            "canonical_sha256",
            "catalog_revision",
            "catalog_head_sha256",
        )
        self.assertEqual(P.CANONICAL_VERSION_SNAPSHOT_FIELDS, expected)
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(P.CanonicalVersionSnapshotV1)),
            expected,
        )
        snapshot = self.resolve()
        self.assertEqual(tuple(snapshot.to_payload()), expected)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.catalog_revision = 99  # type: ignore[misc]
        self.assertFalse(hasattr(snapshot, "__dict__"))

    def test_schema_is_closed_and_matches_runtime(self):
        schema = C.strict_json_loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "cwk.canonical_version_snapshot.v1")
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["unevaluatedProperties"])
        self.assertEqual(tuple(schema["required"]), P.CANONICAL_VERSION_SNAPSHOT_FIELDS)
        self.assertEqual(tuple(schema["properties"]), P.CANONICAL_VERSION_SNAPSHOT_FIELDS)
        forbidden = {
            "path",
            "object_id",
            "catalog_key",
            "body",
            "tenant_id",
            "credential_ref",
        }
        self.assertTrue(forbidden.isdisjoint(schema["properties"]))

    def test_catalog_key_fixed_vector_is_independently_reproducible(self):
        report_key = "cwork:2070001"
        digest = hashlib.sha256(
            b"cwk-rt014-report-key-v1\0" + report_key.encode("utf-8")
        ).digest()[:16]
        independent = "r_" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        self.assertEqual(independent, "r_tx33a6ug3oomvn2klbxm3p5jhy")
        self.assertEqual(P.RT014_CATALOG_KEY_FIXED_VECTOR_REPORT_KEY, report_key)
        self.assertEqual(P.RT014_CATALOG_KEY_FIXED_VECTOR, independent)
        self.assertEqual(self.fx.receipt.catalog_key, independent)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["contractVector"]["catalogKey"], independent)

    def test_null_provider_fails_closed_without_any_filesystem_open(self):
        provider = P.NullCanonicalVersionProvider()
        with mock.patch.object(P.os, "open", side_effect=AssertionError("filesystem used")):
            with self.assertRaises(P.CanonicalVersionUnavailable) as caught:
                provider.resolve_current(report_key="cwork:2070001")
        self.assertEqual(caught.exception.code, "unavailable")

    def test_invalid_report_key_rejected_before_filesystem(self):
        bad = (
            None,
            b"cwork:2070001",
            "",
            "CWork:2070001",
            "cwork/2070001",
            "cwork:../2070001",
            "cwork：2070001",
            "cwork:e\u0301",
            " cwork:2070001",
        )
        for value in bad:
            with self.subTest(value=value):
                with mock.patch.object(P.os, "open", side_effect=AssertionError("filesystem used")):
                    with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                        self.fx.provider.resolve_current(report_key=value)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.code, "contract")

    def test_maximum_length_frozen_report_key_passes_grammar(self):
        maximum = "a" * 64 + ":" + "B" * 128
        self.assertEqual(len(maximum), 193)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.fx.provider.resolve_current(report_key=maximum)
        self.assertEqual(caught.exception.code, "not_found")

    def test_runtime_never_calls_rt014_private_helpers_or_enumerators(self):
        tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
        forbidden_attributes = {
            "_read_head",
            "_read_entries",
            "_catalog_key",
            "_read_catalog",
            "recover",
            "publish",
            "scandir",
            "listdir",
            "walk",
            "glob",
            "rglob",
        }
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes
        }
        self.assertEqual(used, set())

    def test_only_public_read_version_is_called_with_exact_identity(self):
        calls: list[tuple[str, str]] = []
        original = S.SharedEvidenceStore.read_version

        def tracking(store, report_key, canonical_sha256):
            calls.append((report_key, canonical_sha256))
            return original(store, report_key, canonical_sha256)

        with mock.patch.object(S.SharedEvidenceStore, "read_version", new=tracking):
            snapshot = self.resolve()
        self.assertEqual(calls, [(snapshot.report_key, snapshot.canonical_sha256)])


class NormalResolutionTests(_ProviderTest):
    def test_one_version_resolves_and_binds_raw_head_hash(self):
        snapshot = self.resolve()
        self.assertEqual(snapshot.schema, "cwk.canonical_version_snapshot.v1")
        self.assertEqual(snapshot.report_key, "cwork:2070001")
        self.assertEqual(snapshot.canonical_sha256, self.fx.receipt.canonical_sha256)
        self.assertEqual(snapshot.catalog_revision, 1)
        self.assertEqual(
            snapshot.catalog_head_sha256,
            hashlib.sha256(self.fx.head_path.read_bytes()).hexdigest(),
        )

    def test_two_versions_resolve_latest_only(self):
        second = self.fx.store.publish(_envelope(body="new body", hour=11))
        self.fx.receipts.append(second)
        snapshot = self.resolve()
        self.assertEqual(snapshot.canonical_sha256, second.canonical_sha256)
        self.assertEqual(snapshot.catalog_revision, 2)

    def test_resolution_is_read_only(self):
        before = {
            path.relative_to(self.fx.root): (path.stat().st_ino, path.read_bytes())
            for path in self.fx.root.rglob("*")
            if path.is_file()
        }
        self.resolve()
        after = {
            path.relative_to(self.fx.root): (path.stat().st_ino, path.read_bytes())
            for path in self.fx.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_provider_rejects_non_layout_constructor_input(self):
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            P.CanonicalVersionProvider(layout=object())  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "contract")


class MissingCatalogTests(_ProviderTest):
    publish = False

    def test_missing_report_catalog_fails_closed(self):
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "not_found")

    def test_missing_report_versions_root_fails_closed(self):
        shutil.rmtree(self.fx.root / "shared" / "report-versions")
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertIn(caught.exception.code, {"not_found", "unavailable"})


class CatalogHeadRejectionTests(_ProviderTest):
    def _reject_head_bytes(self, raw: bytes, expected_code: str = "corrupt_catalog") -> None:
        self.fx.head_path.write_bytes(raw)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, expected_code)

    def _reject_head_mutation(self, mutate) -> None:
        head = self.fx.head()
        mutate(head)
        self._reject_head_bytes(C.canonical_json_bytes(head))

    def test_malformed_duplicate_key_and_non_jcs_head_rejected(self):
        valid = self.fx.head()
        cases = (
            b"{",
            b'{"schema":"a","schema":"b"}',
            json.dumps(valid, ensure_ascii=False, indent=2).encode("utf-8"),
            C.canonical_json_bytes([valid]),
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                original = self.fx.head_path.read_bytes()
                try:
                    self._reject_head_bytes(raw)
                finally:
                    self.fx.head_path.write_bytes(original)

    def test_missing_extra_and_wrong_typed_head_fields_rejected(self):
        mutations = (
            lambda h: h.pop("updated_at"),
            lambda h: h.update({"path": "/tmp/escape"}),
            lambda h: h.update({"entry_count": True}),
            lambda h: h.update({"head_revision": 0}),
            lambda h: h.update({"latest_canonical_sha256": "A" * 64}),
            lambda h: h.update({"latest_object_id": "../object"}),
            lambda h: h.update({"created_at": "2026-08-20"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original = self.fx.head_path.read_bytes()
                try:
                    self._reject_head_mutation(mutate)
                finally:
                    self.fx.head_path.write_bytes(original)

    def test_head_identity_count_and_latest_drift_rejected(self):
        mutations = (
            lambda h: h.update({"schema": "cwk.rt014.catalog_head.v2"}),
            lambda h: h.update({"catalog_key": "r_" + "a" * 26}),
            lambda h: h.update({"report_key": "cwork:other"}),
            lambda h: h.update({"head_revision": h["entry_count"] + 1}),
            lambda h: h.update({"latest_canonical_sha256": "0" * 64}),
            lambda h: h.update({"latest_object_id": "o_" + "a" * 26}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original = self.fx.head_path.read_bytes()
                try:
                    self._reject_head_mutation(mutate)
                finally:
                    self.fx.head_path.write_bytes(original)

    def test_head_to_jsonl_hash_drift_rejected(self):
        self._reject_head_mutation(
            lambda h: h.update({"catalog_jsonl_sha256": "0" * 64})
        )

    def test_missing_or_empty_head_and_jsonl_fail_closed(self):
        cases = (
            ("catalog.head", None, "not_found"),
            ("catalog.head", b"", "corrupt_catalog"),
            ("catalog.jsonl", None, "not_found"),
            ("catalog.jsonl", b"", "corrupt_catalog"),
        )
        for leaf, replacement, expected_code in cases:
            with self.subTest(leaf=leaf, replacement=replacement):
                path = self.fx.catalog_dir / leaf
                original = path.read_bytes()
                try:
                    if replacement is None:
                        path.unlink()
                    else:
                        path.write_bytes(replacement)
                    with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                        self.resolve()
                    self.assertEqual(caught.exception.code, expected_code)
                finally:
                    path.write_bytes(original)


class CatalogJsonlRejectionTests(_ProviderTest):
    def _reject_jsonl(self, raw: bytes, *, align_hash: bool) -> None:
        self.fx.jsonl_path.write_bytes(raw)
        if align_hash:
            head = self.fx.head()
            head["catalog_jsonl_sha256"] = hashlib.sha256(raw).hexdigest()
            self.fx.write_head(head)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "corrupt_catalog")

    def test_missing_newline_blank_record_malformed_and_non_jcs_rejected(self):
        entry = self.fx.entries()[0]
        cases = (
            C.canonical_json_bytes(entry),
            C.canonical_json_bytes(entry) + b"\n\n",
            b"{\n",
            json.dumps(entry, indent=2).encode("utf-8") + b"\n",
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                original_head = self.fx.head_path.read_bytes()
                original_jsonl = self.fx.jsonl_path.read_bytes()
                try:
                    self._reject_jsonl(raw, align_hash=True)
                finally:
                    self.fx.head_path.write_bytes(original_head)
                    self.fx.jsonl_path.write_bytes(original_jsonl)

    def test_entry_missing_extra_wrong_identity_and_type_rejected(self):
        mutations = (
            lambda e: e.pop("normalizer_version"),
            lambda e: e.update({"path": "/tmp/object"}),
            lambda e: e.update({"schema": "cwk.report_version.v2"}),
            lambda e: e.update({"report_key": "cwork:other"}),
            lambda e: e.update({"canonical_sha256": "A" * 64}),
            lambda e: e.update({"object_id": "o_bad"}),
            lambda e: e.update({"source_updated_at": 17}),
            lambda e: e.update({"normalizer_version": "latest"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                entry = self.fx.entries()[0]
                mutate(entry)
                raw = C.canonical_json_bytes(entry) + b"\n"
                original_head = self.fx.head_path.read_bytes()
                original_jsonl = self.fx.jsonl_path.read_bytes()
                try:
                    self._reject_jsonl(raw, align_hash=True)
                finally:
                    self.fx.head_path.write_bytes(original_head)
                    self.fx.jsonl_path.write_bytes(original_jsonl)

    def test_raw_hash_drift_rejected_before_public_object_read(self):
        raw = self.fx.jsonl_path.read_bytes() + b" "
        with mock.patch.object(
            S.SharedEvidenceStore,
            "read_version",
            side_effect=AssertionError("public reader should not run"),
        ):
            self._reject_jsonl(raw, align_hash=False)

    def test_duplicate_canonical_or_object_identity_rejected(self):
        second = self.fx.store.publish(_envelope(body="second", hour=11))
        self.fx.receipts.append(second)
        base = self.fx.entries()
        variants = []
        duplicate_sha = copy.deepcopy(base)
        duplicate_sha[1]["canonical_sha256"] = duplicate_sha[0]["canonical_sha256"]
        variants.append(duplicate_sha)
        duplicate_object = copy.deepcopy(base)
        duplicate_object[1]["object_id"] = duplicate_object[0]["object_id"]
        variants.append(duplicate_object)
        for entries in variants:
            with self.subTest(entries=entries):
                original_head = self.fx.head_path.read_bytes()
                original_jsonl = self.fx.jsonl_path.read_bytes()
                try:
                    self.fx.write_entries(entries)
                    with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                        self.resolve()
                    self.assertEqual(caught.exception.code, "corrupt_catalog")
                finally:
                    self.fx.head_path.write_bytes(original_head)
                    self.fx.jsonl_path.write_bytes(original_jsonl)


class ObjectVerificationTests(_ProviderTest):
    def test_missing_object_rejected_by_public_reader(self):
        self.fx.object_path.unlink()
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "object_verification_failed")

    def test_object_byte_drift_rejected_by_public_reader(self):
        self.fx.object_path.write_bytes(b"{}")
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "object_verification_failed")

    def test_object_symlink_and_hardlink_rejected_by_public_reader(self):
        original = self.fx.object_path
        saved = original.with_suffix(".saved")
        original.rename(saved)
        os.symlink(saved.name, original)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "object_verification_failed")
        original.unlink()
        os.link(saved, original)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "object_verification_failed")

    def test_mismatched_public_reader_result_rejected(self):
        with mock.patch.object(
            S.SharedEvidenceStore,
            "read_version",
            return_value={"canonical_sha256": "0" * 64},
        ):
            with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                self.resolve()
        self.assertEqual(caught.exception.code, "object_verification_failed")


class ContainmentAttackTests(_ProviderTest):
    def test_catalog_head_symlink_rejected(self):
        head = self.fx.head_path
        saved = head.with_name("saved.head")
        head.rename(saved)
        os.symlink(saved.name, head)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_catalog_head_hardlink_rejected(self):
        head = self.fx.head_path
        saved = head.with_name("saved.head")
        head.rename(saved)
        os.link(saved, head)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_catalog_head_fifo_rejected_without_opening_it(self):
        head = self.fx.head_path
        head.unlink()
        os.mkfifo(head)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_catalog_directory_symlink_rejected(self):
        catalog = self.fx.catalog_dir
        saved = catalog.with_name(catalog.name + ".saved")
        catalog.rename(saved)
        os.symlink(saved.name, catalog)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_case_alias_catalog_directory_rejected(self):
        catalog = self.fx.catalog_dir
        alias = catalog.with_name(catalog.name.upper())
        catalog.rename(alias)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertIn(caught.exception.code, {"containment", "not_found"})

    def test_case_alias_fixed_parent_rejected(self):
        versions = self.fx.root / "shared" / "report-versions"
        alias = versions.with_name("REPORT-VERSIONS")
        versions.rename(alias)
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        self.assertIn(caught.exception.code, {"containment", "not_found"})

    def test_same_inode_rewrite_during_read_rejected(self):
        real_read = P.os.read
        fired = False

        def rewrite_then_read(fd: int, count: int) -> bytes:
            nonlocal fired
            if not fired:
                fired = True
                raw = self.fx.head_path.read_bytes()
                self.fx.head_path.write_bytes(raw)
            return real_read(fd, count)

        with mock.patch.object(P.os, "read", side_effect=rewrite_then_read):
            with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_parent_swap_during_read_rejected(self):
        real_read = P.os.read
        fired = False
        catalog = self.fx.catalog_dir
        displaced = catalog.with_name(catalog.name + ".displaced")

        def swap_parent_then_read(fd: int, count: int) -> bytes:
            nonlocal fired
            if not fired:
                fired = True
                catalog.rename(displaced)
                catalog.mkdir(mode=0o700)
            return real_read(fd, count)

        with mock.patch.object(P.os, "read", side_effect=swap_parent_then_read):
            with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                self.resolve()
        self.assertEqual(caught.exception.code, "containment")

    def test_concurrent_append_between_catalog_reads_rejected(self):
        original = S.SharedEvidenceStore.read_version
        fired = False

        def verify_then_append(store, report_key, canonical_sha256):
            nonlocal fired
            result = original(store, report_key, canonical_sha256)
            if not fired:
                fired = True
                self.fx.store.publish(_envelope(body="racing version", hour=11))
            return result

        with mock.patch.object(S.SharedEvidenceStore, "read_version", new=verify_then_append):
            with self.assertRaises(P.CanonicalVersionProviderError) as caught:
                self.resolve()
        self.assertEqual(caught.exception.code, "concurrent_update")


class ErrorOpacityTests(_ProviderTest):
    def test_errors_do_not_disclose_absolute_root_or_object_identity(self):
        self.fx.object_path.unlink()
        with self.assertRaises(P.CanonicalVersionProviderError) as caught:
            self.resolve()
        rendered = str(caught.exception)
        self.assertNotIn(str(self.fx.root), rendered)
        self.assertNotIn(self.fx.receipt.object_id, rendered)
        self.assertNotIn("/", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
