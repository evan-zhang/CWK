"""RT-016 v1.1 remediation tests.

Covers the three fixes applied in response to the RT-016 independent
acceptance report (commit c6a740a):

- Minor-1: ``read_crosswalk`` (and every path that goes through
  ``_load_crosswalk_payload``) MUST detect JCS-preserving semantic
  tamper on the top-level ``canonical_sha256`` / ``object_id`` /
  ``observe_grant_key`` / ``view_key`` / ``crosswalk_key`` / etc.
  fields.  Any inconsistency raises
  ``LegacyImportError(code='corrupt')``.
- Minor-2: The idempotency key for both crosswalks and reviews MUST
  include ``source_namespace``.  Same raw bytes imported under two
  different namespaces produce two independent crosswalks / reviews.
  Same namespace + same raw bytes stays idempotent.
- Info-1: ``read_crosswalk`` (and equivalent read paths) MUST wrap
  ``json.JSONDecodeError`` / ``UnicodeDecodeError`` /
  ``ContractError`` as ``LegacyImportError(code='corrupt')`` so
  callers can rely on a single exception family.

Each test only uses synthetic fixtures, a temp ``CWK_INSTANCE_ROOT``
and the fake RT-015 authority; no real CWork / DocDB / Cloud / cron /
real legacy raw data is touched.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    AL,
    AF,
    C,
    R16,
    RT016TestBase,
    sample_raw,
    utc_iso,
)


def _rewrite_crosswalk(cw_path: Path, mutator) -> None:
    """Read the on-disk crosswalk JSON, apply ``mutator(payload)`` in
    place, re-emit as strict NFC + JCS bytes and overwrite the file.

    The result is byte-for-byte a legitimate JCS payload, so the byte
    round-trip check inside ``read_crosswalk`` would happily accept it
    without the cross-field integrity binding.  Any semantic drift
    against the embedded ``publish_receipt`` / ``tenant_view_envelope``
    / ``decompose_report`` or against the deterministic key
    derivations MUST now be detected and raised as
    ``LegacyImportError(code='corrupt')``.
    """

    payload = json.loads(cw_path.read_bytes().decode("utf-8"))
    mutator(payload)
    cw_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))


# ---------------------------------------------------------------------------
# Minor-1: cross-field integrity binding
# ---------------------------------------------------------------------------


class CrosswalkIntegrityBindingTests(RT016TestBase):
    def _seed_one(self) -> tuple[str, str, Path]:
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="a.md",
        )
        cw_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "crosswalks"
            / f"{rec.crosswalk_key}.json"
        )
        self.assertTrue(cw_path.is_file())
        return tenant_id, rec.crosswalk_key, cw_path

    def test_jcs_preserving_canonical_sha_tamper_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("canonical_sha256", "0" * 64),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_object_id_tamper_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("object_id", "o_" + "a" * 26),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_observe_grant_key_tamper_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("observe_grant_key", "g_" + "b" * 26),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_view_key_tamper_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("view_key", "g_" + "c" * 26),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_catalog_key_mismatch_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("catalog_key", "r_" + "a" * 26),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_nested_publish_receipt_drift_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        def mutator(p):
            p["publish_receipt"]["canonical_sha256"] = "0" * 64
        _rewrite_crosswalk(cw_path, mutator)
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_jcs_preserving_tenant_view_envelope_drift_fails_closed(self):
        tenant_id, ck, cw_path = self._seed_one()
        def mutator(p):
            p["tenant_view_envelope"]["canonical_sha256"] = "0" * 64
        _rewrite_crosswalk(cw_path, mutator)
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_iter_crosswalks_surfaces_integrity_failure(self):
        tenant_id, ck, cw_path = self._seed_one()
        _rewrite_crosswalk(
            cw_path,
            lambda p: p.__setitem__("canonical_sha256", "0" * 64),
        )
        with self.assertRaises(R16.LegacyImportError) as ctx:
            list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_untampered_record_still_reads_ok(self):
        """Baseline: the exact record RT-016 writes must still pass the
        upgraded integrity check.  This protects against a future
        regression where the derivation helpers drift out of sync with
        the writer's derivation formulas."""

        tenant_id, ck, cw_path = self._seed_one()
        payload = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=ck
        )
        # Full integrity signals present.
        self.assertEqual(
            payload["canonical_sha256"],
            payload["publish_receipt"]["canonical_sha256"],
        )
        self.assertEqual(
            payload["canonical_sha256"],
            payload["tenant_view_envelope"]["canonical_sha256"],
        )
        self.assertEqual(
            payload["canonical_sha256"],
            payload["decompose_report"]["canonical_sha256"],
        )
        self.assertEqual(payload["view_key"], payload["observe_grant_key"])


# ---------------------------------------------------------------------------
# Minor-2: cross-namespace idempotency isolation
# ---------------------------------------------------------------------------


class CrossNamespaceIdempotencyTests(RT016TestBase):
    def test_same_raw_different_namespace_yields_two_crosswalks(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        rec_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns_a",
            legacy_path_hint="a.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork_alt",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns_b",
            legacy_path_hint="a.md",
        )
        # Different crosswalk_keys (crosswalk_key derivation includes
        # view_key which includes report_key which includes namespace).
        self.assertNotEqual(rec_a.crosswalk_key, rec_b.crosswalk_key)
        # Different view_keys (grant_key derivation includes report_key).
        self.assertNotEqual(rec_a.view_key, rec_b.view_key)
        # Both crosswalks are discoverable via iter_crosswalks.
        cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        keys = {cw["crosswalk_key"] for cw in cws}
        self.assertEqual(len(cws), 2)
        self.assertIn(rec_a.crosswalk_key, keys)
        self.assertIn(rec_b.crosswalk_key, keys)
        # Each crosswalk records its own source_namespace faithfully.
        by_key = {cw["crosswalk_key"]: cw for cw in cws}
        self.assertEqual(by_key[rec_a.crosswalk_key]["source_namespace"], "cwork")
        self.assertEqual(by_key[rec_b.crosswalk_key]["source_namespace"], "cwork_alt")
        # canonical_sha256 differs across namespaces because
        # source_namespace is part of the canonical envelope (RT-011
        # cwk.canonical_report.v1 required field).  This is *stronger*
        # than the pre-remediation assumption that identical raw
        # collapsed to one canonical — namespace boundaries carry all
        # the way down.
        self.assertNotEqual(rec_a.canonical_sha256, rec_b.canonical_sha256)

    def test_same_namespace_same_raw_stays_idempotent(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        run_id = self.new_run_id()
        rec_1 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="first",
            legacy_path_hint="a.md",
        )
        rec_2 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="repeat",
            legacy_path_hint="a.md",
        )
        self.assertEqual(rec_1.crosswalk_key, rec_2.crosswalk_key)
        cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        self.assertEqual(len(cws), 1)

    def test_review_is_namespace_scoped(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        bad_raw = sample_raw(create_time="never")
        rec_a = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns_a bad",
            legacy_path_hint="q.md",
        )
        rec_b = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork_alt",
            raw_bytes=bad_raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="ns_b bad",
            legacy_path_hint="q.md",
        )
        self.assertIn(rec_a.outcome, ("review", "undecomposable"))
        self.assertIn(rec_b.outcome, ("review", "undecomposable"))
        self.assertNotEqual(rec_a.review_id, rec_b.review_id)
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        review_ids = {r["review_id"] for r in reviews}
        self.assertEqual(len(reviews), 2)
        self.assertIn(rec_a.review_id, review_ids)
        self.assertIn(rec_b.review_id, review_ids)
        by_id = {r["review_id"]: r for r in reviews}
        self.assertEqual(by_id[rec_a.review_id]["source_namespace"], "cwork")
        self.assertEqual(by_id[rec_b.review_id]["source_namespace"], "cwork_alt")

    def test_compute_review_id_namespace_binding(self):
        tenant_id = "t_" + "a" * 26
        legacy_sha = "0" * 64
        run_id = "run_" + "a" * 26
        a = R16.compute_review_id(tenant_id, "cwork", legacy_sha, run_id)
        b = R16.compute_review_id(tenant_id, "cwork_alt", legacy_sha, run_id)
        self.assertNotEqual(a, b)
        # Same inputs → same id (deterministic).
        c = R16.compute_review_id(tenant_id, "cwork", legacy_sha, run_id)
        self.assertEqual(a, c)


# ---------------------------------------------------------------------------
# Info-1: JSONDecodeError wrapping
# ---------------------------------------------------------------------------


class JsonDecodeErrorWrappingTests(RT016TestBase):
    def _seed_one(self) -> tuple[str, str, Path]:
        tenant_id = self.fx.new_tenant(status="pilot")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="a.md",
        )
        cw_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "crosswalks"
            / f"{rec.crosswalk_key}.json"
        )
        return tenant_id, rec.crosswalk_key, cw_path

    def test_stray_byte_appended_reports_corrupt_not_raw_jsondecode(self):
        tenant_id, ck, cw_path = self._seed_one()
        cw_path.write_bytes(cw_path.read_bytes() + b" ")
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        # Wrapped, not raw JSONDecodeError.
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_completely_malformed_json_reports_corrupt(self):
        tenant_id, ck, cw_path = self._seed_one()
        cw_path.write_bytes(b"not-json-at-all")
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_invalid_utf8_reports_corrupt(self):
        tenant_id, ck, cw_path = self._seed_one()
        cw_path.write_bytes(b"\xff\xfe\x00\x00")
        with self.assertRaises(R16.LegacyImportError) as ctx:
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=ck
            )
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_iter_crosswalks_wraps_json_error(self):
        tenant_id, ck, cw_path = self._seed_one()
        cw_path.write_bytes(b"[not, json}")
        with self.assertRaises(R16.LegacyImportError) as ctx:
            list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
        self.assertEqual(ctx.exception.code, "corrupt")

    def test_iter_manifest_wraps_json_error(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="manifest seed",
            legacy_path_hint="a.md",
        )
        mf_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "manifests"
            / f"{run_id}.jsonl"
        )
        mf_path.write_bytes(b"{bad}\n")
        with self.assertRaises(R16.LegacyImportError) as ctx:
            list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))
        self.assertEqual(ctx.exception.code, "corrupt")


if __name__ == "__main__":
    unittest.main()
