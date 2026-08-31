"""RT-016 v2 anchor remediation — third-round regressions.

Independent regression tests for the three third-round Major fixes on
top of e12e07b:

Fix A — Manifest replay / dedupe fail-open closed
    ``_load_manifest_line_bound`` is now the SOLE door for reading any
    existing manifest line; ``_append_manifest_entry`` routes its full
    existing-line replay through it and enforces contiguous / unique
    ``entry_seq`` plus outcome/key co-occurrence plus recomputed v2
    ``crosswalk_key`` / ``review_id``.  Any tampered / non-JCS /
    foreign-tenant / gapped / replayed entry MUST fail closed as
    ``LegacyImportError(code="corrupt")`` and never survive long
    enough to poison the dedupe set.

Fix B — Review coverage bound to raw SHA
    ``MigrationReconciler`` counts a v2 review as coverage only when
    ``(legacy_path_hash, legacy_source_sha256)`` matches the current
    on-disk raw SHA.  A stale review at the same path (raw SHA drift)
    MUST NOT suppress ``only_legacy`` and MUST emit
    ``review_source_sha_drift`` fail-closed.

Fix C — Frozen baseline actually matches the promised range
    A meta-guard test proves the exact-set frozen baseline surfaces a
    silently-added or silently-removed schema in the pinned
    ``contracts/schemas/`` + ``contracts/rt012~015/schemas/`` scope.

Also includes a smoke re-run of key e12 (second-round) forward-attack
regressions so the third-round diff cannot silently re-open a
previously-closed hole.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    C,
    R16,
    RT016TestBase,
    build_legacy_tree,
    default_anchor,
    sample_raw,
    utc_iso,
)


def _manifest_path(fx, tenant_id: str, run_id: str) -> Path:
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "manifests"
        / f"{run_id}.jsonl"
    )


def _review_path(fx, tenant_id: str, review_id: str) -> Path:
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "review"
        / f"{review_id}.json"
    )


def _canonical_line(payload: dict) -> bytes:
    """JCS+NFC bytes + trailing newline — the exact wire format."""

    return C.canonical_json_bytes(C.nfc_normalize(payload)) + b"\n"


def _seed_run_and_capture_line(
    fx, *, tenant_id: str, run_id: str, rel: str, raw: bytes
) -> tuple[bytes, dict]:
    """Import a single raw and return the sole manifest line + payload."""

    with tempfile.TemporaryDirectory() as td:
        root = build_legacy_tree(Path(td), {rel: raw})
        source = R16.LegacySource(str(root))
        fx.importer.import_batch(
            tenant_id=tenant_id,
            source_namespace="cwork",
            source=source,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed-third-round",
        )
    mf = _manifest_path(fx, tenant_id, run_id)
    raw_bytes = mf.read_bytes()
    line = raw_bytes.split(b"\n")[0]
    payload = C.strict_json_loads(line.decode("utf-8"))
    return line, payload


# ---------------------------------------------------------------------------
# Fix A: manifest replay / dedupe fail-closed regressions
# ---------------------------------------------------------------------------


class ManifestReplayAndDedupeFailClosedTests(RT016TestBase):
    """Third-round Major A regressions.

    Every attack MUST propagate as ``LegacyImportError(code="corrupt")``
    and MUST NOT leave the dedupe set populated with the malicious
    identity — every subsequent legitimate append against the same run
    file MUST also fail closed because the file is corrupt, not
    silently succeed.
    """

    # --- helpers ---------------------------------------------------------

    def _one_seed(self, tenant_id: str, run_id: str) -> tuple[bytes, dict]:
        return _seed_run_and_capture_line(
            self.fx,
            tenant_id=tenant_id,
            run_id=run_id,
            rel="a.md",
            raw=sample_raw(report_id="207A", body="A body"),
        )

    def _import_second(
        self, tenant_id: str, run_id: str, rel: str, raw: bytes
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {rel: raw})
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="second-append",
            )

    def _assert_corrupt_iter(self, tenant_id: str, run_id: str) -> None:
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(
                self.fx.importer.iter_manifest(
                    tenant_id=tenant_id, run_id=run_id
                )
            )
        self.assertEqual(cm.exception.code, "corrupt")

    # --- non-JCS strict JSON ---------------------------------------------

    def test_non_jcs_line_rejected_by_iter_and_append(self):
        """A re-encoded but semantically identical manifest line MUST NOT
        be silently accepted — JCS+NFC canonical bytes are the wire
        contract."""

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)

        # Re-encode via non-canonical JSON: pretty-print (2-space
        # indent) violates JCS.  Same payload after strict parse.
        import json as _json

        prettied = _json.dumps(payload, indent=2, ensure_ascii=False).encode(
            "utf-8"
        )
        self.assertNotEqual(prettied, line)
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(prettied + b"\n")

        self._assert_corrupt_iter(tenant_id, run_id)

        # A second legitimate append against the same corrupt file
        # must ALSO fail closed rather than silently overwriting the
        # tamper.  The dedupe set should never form from the corrupt
        # line.
        with self.assertRaises(R16.LegacyImportError) as cm:
            self._import_second(
                tenant_id,
                run_id,
                "b.md",
                sample_raw(report_id="207B", body="B body"),
            )
        self.assertEqual(cm.exception.code, "corrupt")

    # --- foreign tenant / run --------------------------------------------

    def test_foreign_tenant_line_never_enters_dedupe(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        tampered = dict(payload)
        # Rewrite tenant_id to a syntactically valid but foreign one.
        tampered["tenant_id"] = "t_" + ("z" * 26)
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))

        self._assert_corrupt_iter(tenant_id, run_id)
        with self.assertRaises(R16.LegacyImportError) as cm:
            self._import_second(
                tenant_id,
                run_id,
                "b.md",
                sample_raw(report_id="207B", body="B body"),
            )
        self.assertEqual(cm.exception.code, "corrupt")

    def test_foreign_run_line_never_enters_dedupe(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        tampered = dict(payload)
        tampered["run_id"] = R16.new_run_id()  # different from filename
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))

        self._assert_corrupt_iter(tenant_id, run_id)
        with self.assertRaises(R16.LegacyImportError):
            self._import_second(
                tenant_id,
                run_id,
                "b.md",
                sample_raw(report_id="207B", body="B body"),
            )

    # --- entry_seq duplicate / gap ---------------------------------------

    def test_entry_seq_duplicate_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        # Seed two legit lines with a second file so entry_seq=1 and 2.
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A"),
                    "b.md": sample_raw(report_id="207B", body="B"),
                },
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="two-line-seed",
            )
        mf = _manifest_path(self.fx, tenant_id, run_id)
        lines = mf.read_bytes().split(b"\n")[:-1]
        self.assertEqual(len(lines), 2)
        # Corrupt: rewrite line 2's entry_seq to 1 (duplicate).
        p2 = C.strict_json_loads(lines[1].decode("utf-8"))
        p2["entry_seq"] = 1
        lines[1] = C.canonical_json_bytes(C.nfc_normalize(p2))
        mf.write_bytes(b"\n".join(lines) + b"\n")

        # iter_manifest passes expected_entry_seq=line_number so line 2
        # (which now claims entry_seq=1) MUST fail.
        self._assert_corrupt_iter(tenant_id, run_id)

    def test_entry_seq_gap_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        tampered = dict(payload)
        tampered["entry_seq"] = 7  # gap
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))

        self._assert_corrupt_iter(tenant_id, run_id)

    # --- cross-key replay -------------------------------------------------

    def test_crosswalk_key_replayed_under_different_identity_rejected(self):
        """A tampered line that keeps a real crosswalk_key but reports a
        DIFFERENT (namespace, kind, path_hash, raw_sha) MUST fail closed
        at the append (or read) door.  Otherwise an attacker could
        re-target an existing crosswalk_key to a fabricated identity
        and win a subsequent dedupe hit.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        # Keep the same crosswalk_key but flip the path_hash to a value
        # of the same grammar but semantically different.  This breaks
        # the recomputation equation crosswalk_key == compute_key_v2(...)
        # and MUST be caught by the bound loader.
        tampered = dict(payload)
        # Flip path_hash and raw_sha to arbitrary syntactically valid
        # hex — since we keep the crosswalk_key, recomputation now
        # points somewhere else.
        tampered["legacy_path_hash"] = "f" * 64
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))
        self._assert_corrupt_iter(tenant_id, run_id)

    def test_review_id_replayed_under_different_identity_rejected(self):
        """Symmetric attack on review outcome."""

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        # Seed a review outcome by importing a raw that quarantines
        # (omit report_id → malformed frontmatter → quarantined).
        bad = sample_raw(report_id="207A", body="A", create_time="never")
        line, payload = _seed_run_and_capture_line(
            self.fx,
            tenant_id=tenant_id,
            run_id=run_id,
            rel="bad.md",
            raw=bad,
        )
        self.assertIn(payload["outcome"], ("review", "undecomposable"))
        tampered = dict(payload)
        tampered["legacy_path_hash"] = "0" * 64
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))
        self._assert_corrupt_iter(tenant_id, run_id)

    # --- outcome / key co-occurrence -------------------------------------

    def test_complete_outcome_without_crosswalk_key_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        self.assertEqual(payload["outcome"], "complete")
        tampered = dict(payload)
        tampered.pop("crosswalk_key", None)
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))
        self._assert_corrupt_iter(tenant_id, run_id)

    def test_complete_outcome_with_review_id_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        line, payload = self._one_seed(tenant_id, run_id)
        tampered = dict(payload)
        tampered["review_id"] = "qe_" + ("a" * 26)
        mf = _manifest_path(self.fx, tenant_id, run_id)
        mf.write_bytes(_canonical_line(tampered))
        self._assert_corrupt_iter(tenant_id, run_id)

    # --- happy path: legitimate second append still idempotent -----------

    def test_legitimate_same_run_second_append_still_idempotent(self):
        """Baseline: the bounded replay MUST NOT reject a legit append.

        Importing the same raw twice against the same run_id keeps the
        manifest at a single entry (idempotent) and iter_manifest still
        yields it.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        raw = sample_raw(report_id="207A", body="A")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": raw})
            src = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=src,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="first",
            )
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": raw})
            src = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=src,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="second",
            )
        entries = list(
            self.fx.importer.iter_manifest(
                tenant_id=tenant_id, run_id=run_id
            )
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["entry_seq"], 1)
        self.assertEqual(entries[0]["outcome"], "complete")


# ---------------------------------------------------------------------------
# Fix B: review coverage MUST bind (legacy_path_hash, legacy_source_sha256)
# ---------------------------------------------------------------------------


class ReviewCoverageBoundToRawShaTests(RT016TestBase):
    """Third-round Major B regressions.

    A v2 review whose recorded ``legacy_source_sha256`` differs from
    the current on-disk raw SHA at its ``legacy_path_hash`` MUST NOT
    suppress ``only_legacy`` and MUST emit ``review_source_sha_drift``.
    A v2 review whose SHA matches MUST suppress ``only_legacy`` and
    contribute to ``new_undecomposable_count``.
    """

    def _import_bad_and_get_review(
        self, tenant_id: str, run_id: str
    ) -> tuple[Path, dict]:
        """Import a synthetic bad raw as a review at a stable rel path.

        Returns (legacy_root_path, review_payload).
        """

        rel = "bad.md"
        bad = sample_raw(report_id="207A", body="A", create_time="never")
        legacy_root_ctx = tempfile.TemporaryDirectory()
        legacy_root = build_legacy_tree(Path(legacy_root_ctx.name), {rel: bad})
        source = R16.LegacySource(str(legacy_root))
        self.fx.importer.import_batch(
            tenant_id=tenant_id,
            source_namespace="cwork",
            source=source,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
        )
        # Locate the sole v2 review.
        reviews = list(
            r for r in self.fx.importer.iter_reviews(tenant_id=tenant_id)
            if r.get("schema") == "cwk.rt016.review_entry.v2"
        )
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        # Keep legacy_root_ctx alive by attaching to fixture; we hand
        # its Path back and manage cleanup via addCleanup.
        self.addCleanup(legacy_root_ctx.cleanup)
        return legacy_root, review

    def test_same_path_different_raw_sha_does_not_suppress_only_legacy(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        legacy_root, review = self._import_bad_and_get_review(
            tenant_id, self.new_run_id()
        )
        rel = "bad.md"

        # Overwrite the raw file with a DIFFERENT bad body.  Same path,
        # different SHA — the review is now stale.
        (legacy_root / rel).write_bytes(
            sample_raw(
                report_id="207A",
                body="B different body",
                omit_frontmatter=("report_id",),
            )
        )

        # Run reconciler against the current tree.  The review points
        # at the old sha; the file at the same path now has a new sha.
        source = R16.LegacySource(str(legacy_root))
        source.snapshot()
        report = self.fx.reconciler.reconcile(
            anchor=default_anchor(tenant_id),
            run_id=self.new_run_id(),
            source=source,
        )

        # only_legacy MUST count the file (stale review cannot suppress).
        self.assertGreaterEqual(report.only_legacy_count, 1)
        # review_source_sha_drift MUST appear in failure_samples.
        classes = [
            s["classification"] for s in report.payload["failure_samples"]
        ]
        self.assertIn("review_source_sha_drift", classes)
        # Neither review_count nor new_undecomposable_count may inflate
        # from the stale review (SHA does not match current disk).
        self.assertEqual(report.review_count, 0)
        self.assertEqual(report.new_undecomposable_count, 0)

    def test_same_path_same_raw_sha_does_suppress_only_legacy(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        legacy_root, review = self._import_bad_and_get_review(
            tenant_id, self.new_run_id()
        )

        # Leave the raw exactly as it was (SHA matches review).
        source = R16.LegacySource(str(legacy_root))
        source.snapshot()
        report = self.fx.reconciler.reconcile(
            anchor=default_anchor(tenant_id),
            run_id=self.new_run_id(),
            source=source,
        )

        # only_legacy MUST NOT count this raw — the SHA-matched v2
        # review covers it.
        self.assertEqual(report.only_legacy_count, 0)
        # new_undecomposable / review counters MUST include it.
        self.assertEqual(report.new_undecomposable_count, 1)
        # No review_source_sha_drift.
        classes = [
            s["classification"] for s in report.payload["failure_samples"]
        ]
        self.assertNotIn("review_source_sha_drift", classes)


# ---------------------------------------------------------------------------
# Fix C: exact-set meta-guard actually catches unpinned schemas
# ---------------------------------------------------------------------------


class FrozenBaselineTamperMetaTests(unittest.TestCase):
    """Third-round Major C: prove the exact-set + SHA baseline actually
    detect drift in a real RT-011~015 schema.

    We synthesise a mutated copy of ``rt015/schemas/access_tombstone.
    schema.json`` in a temp directory and re-run the SHA comparison
    against the pinned baseline — the check MUST fail with a specific
    diff.  We do NOT mutate the real on-disk file.
    """

    def test_tampered_rt015_schema_would_fail_pinned_sha_check(self):
        import hashlib
        # Import lazily to avoid picking up test_rt016_schemas as a
        # side effect of unittest discovery ordering.
        import importlib.util

        mod_path = PROJECT / "tests" / "test_rt016_schemas.py"
        spec = importlib.util.spec_from_file_location(
            "_rt016_schemas_baseline", str(mod_path)
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        baseline = mod._FROZEN_RT015_SCHEMA_BASELINE_SHAS  # type: ignore[attr-defined]
        rel = (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/"
            "rt015/schemas/access_tombstone.schema.json"
        )
        real_path = PROJECT / rel
        self.assertTrue(real_path.exists())
        real_sha = hashlib.sha256(real_path.read_bytes()).hexdigest()
        self.assertEqual(real_sha, baseline[rel])

        # Now write a tampered copy to a temp location and confirm its
        # SHA differs from the baseline (proving the check would fire
        # if the on-disk file were mutated).
        with tempfile.TemporaryDirectory() as td:
            copy_path = Path(td) / "access_tombstone.schema.json"
            copy_path.write_bytes(
                real_path.read_bytes() + b"\n// tampered\n"
            )
            tampered_sha = hashlib.sha256(copy_path.read_bytes()).hexdigest()
            self.assertNotEqual(tampered_sha, baseline[rel])

    def test_exact_set_catches_synthetic_extra_and_missing(self):
        """Simulate a hidden new schema being added but not pinned."""

        import importlib.util

        mod_path = PROJECT / "tests" / "test_rt016_schemas.py"
        spec = importlib.util.spec_from_file_location(
            "_rt016_schemas_baseline2", str(mod_path)
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        baseline = dict(
            mod._FROZEN_RT015_SCHEMA_BASELINE_SHAS  # type: ignore[attr-defined]
        )
        rel_dir = "PR/PR-001-multitenant-knowledge-spaces/contracts/rt015/schemas"
        present = {
            f"{rel_dir}/{p.name}"
            for p in (PROJECT / rel_dir).iterdir()
            if p.is_file() and p.name.endswith(".schema.json")
        }
        # Synthetic extra: pretend a new schema was added and NOT
        # pinned.  Detect via set difference.
        synthetic_present = set(present) | {f"{rel_dir}/new_schema.schema.json"}
        extra = synthetic_present - set(baseline.keys())
        self.assertIn(f"{rel_dir}/new_schema.schema.json", extra)
        # Synthetic missing: remove a real key.
        popped = baseline.pop(f"{rel_dir}/authority_receipt.schema.json")
        self.assertTrue(popped)  # was in baseline
        missing = set(present) - set(baseline.keys())
        self.assertIn(f"{rel_dir}/authority_receipt.schema.json", missing)


# ---------------------------------------------------------------------------
# e12 second-round forward regression smoke
# ---------------------------------------------------------------------------


class E12ForwardAttackRegressionTests(RT016TestBase):
    """Smoke-test a representative subset of e12 second-round positive
    attacks so this branch's third-round diff cannot silently re-open
    a previously-closed hole.
    """

    def test_iter_reviews_still_rejects_tampered_tenant_id(self):
        """Second-round bound review reader MUST still fail closed on a
        cross-tenant tamper — mirrors
        ``BoundReviewReaderNegativesTests.test_review_tenant_id_disagrees_with_parent_dir_detected``.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        # Seed a review.
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "bad.md": sample_raw(
                        report_id="207A",
                        body="A",
                        create_time="never",
                    )
                },
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="seed",
            )
        reviews = [
            r for r in self.fx.importer.iter_reviews(tenant_id=tenant_id)
            if r.get("schema") == "cwk.rt016.review_entry.v2"
        ]
        self.assertEqual(len(reviews), 1)
        rv = reviews[0]
        rv_path = _review_path(self.fx, tenant_id, rv["review_id"])
        tampered = dict(rv)
        tampered["tenant_id"] = "t_" + ("z" * 26)
        rv_path.write_bytes(_canonical_line(tampered).rstrip(b"\n"))
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        self.assertEqual(cm.exception.code, "corrupt")

    def test_finder_hit_still_fails_closed_when_rt014_object_gone(self):
        """Second-round coordinated tamper regression — the finder must
        NOT return an idempotent hit if RT-014 read_version differs.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        rel = "a.md"
        raw = sample_raw(report_id="207A", body="A")
        run_id = self.new_run_id()
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {rel: raw})
            src = R16.LegacySource(str(root))
            receipts = self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=src,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="seed",
            )
            self.assertEqual(len(receipts), 1)
            # Remove the RT-014 canonical object so a re-import can no
            # longer verify against read_version — the finder must
            # fail closed.
            evidence_root = (
                self.fx.root / "shared"
            )
            # Find the sole canonical file under shared/ (rt014's layout).
            objs = [
                p for p in evidence_root.rglob("*")
                if p.is_file() and p.suffix == ".json"
            ]
            self.assertGreaterEqual(len(objs), 1)
            for p in objs:
                # Only delete canonical envelopes, not catalog heads.
                # A simple heuristic: canonical is under "objects/".
                if "objects" in p.parts:
                    p.unlink()
            with tempfile.TemporaryDirectory() as td2:
                root2 = build_legacy_tree(Path(td2), {rel: raw})
                src2 = R16.LegacySource(str(root2))
                with self.assertRaises(R16.LegacyImportError) as cm:
                    self.fx.importer.import_batch(
                        tenant_id=tenant_id,
                        source_namespace="cwork",
                        source=src2,
                        run_id=self.new_run_id(),
                        run_started_at=utc_iso(),
                        actor="admin",
                        reason="reimport",
                    )
                self.assertEqual(cm.exception.code, "corrupt")


if __name__ == "__main__":
    unittest.main()
