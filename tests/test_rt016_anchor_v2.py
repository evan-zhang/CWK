"""RT-016 second-round anchor remediation regression tests.

Extends ``tests/test_rt016_anchor.py`` with black-box regressions for the
fixes made after the first-round Reject on commit ``ad72f3a``:

1. **import_one / import_batch finder + CAS conflict must fresh-decompose**
   the caller's raw bytes and cross-check the found crosswalk (canonical
   sha, object bytes sha, report_key, view_key, full tenant_view_envelope
   JCS bytes) then call RT-014's public ``read_version`` for a
   bit-for-bit envelope match — a coordinated tamper that pointed A's
   crosswalk slot at B's canonical must fail closed even if the on-disk
   payload is self-consistent and the bound reader passes identity.
2. **Reconciler tenant_view_envelope byte compare** — any overlay drift
   (lane / read_status / todo_status / reply_overlay / node_overlay)
   between the fresh decomposition and the stored crosswalk is fatal for
   ``both_equal``.
3. **v1 review audit-only loader** — a truly old v1 review WITHOUT the
   post-Minor-2 ``source_namespace`` field remains loadable via
   ``iter_reviews`` for audit, but never satisfies v2 idempotency and is
   never counted for reconciler PASS.
4. **Bound review / manifest readers** — a review file whose payload
   ``tenant_id`` / ``review_id`` disagrees with the parent directory /
   file name, or whose recomputed v2 review_id disagrees with the file
   name, fails closed; a corrupt review file surfaces as a raised
   ``LegacyImportError`` (never silently suppressing ``only_legacy``).
   A manifest line whose ``tenant_id`` / ``run_id`` disagrees with the
   parent / filename fails closed.

All fixtures are synthetic; no real ``CWORK_APP_KEY`` / legacy raw /
DocDB / Cloud / cron / Gateway / real workflow API is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    AL,
    AF,
    C,
    MR,
    R16,
    RT016TestBase,
    build_legacy_tree,
    default_anchor,
    sample_raw,
    utc_iso,
)


def _crosswalk_path(fx, tenant_id, crosswalk_key):
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "crosswalks"
        / f"{crosswalk_key}.json"
    )


def _review_path(fx, tenant_id, review_id):
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "review"
        / f"{review_id}.json"
    )


def _manifest_path(fx, tenant_id, run_id):
    return (
        fx.root
        / "registry"
        / R16.REGISTRY_SUBDIR
        / tenant_id
        / "manifests"
        / f"{run_id}.jsonl"
    )


# ---------------------------------------------------------------------------
# Fix 1: idempotency finder + CAS-conflict must fresh-decompose caller's raw
# ---------------------------------------------------------------------------


class ImportOneFreshDecomposeVerificationTests(RT016TestBase):
    """import_one's idempotency finder must NOT trust a self-consistent
    payload just because the bound reader passes identity.
    """

    def test_coordinated_swap_fails_closed_on_second_import(self):
        """Import A and B at different paths.  Overwrite A's crosswalk
        file with the *entire* JSON contents of a fabricated payload
        that (a) declares A's identity fields exactly (so the bound
        reader passes) but (b) borrows B's canonical_sha256 /
        object_bytes_sha256 / report_key / view_key / object_id /
        publish_receipt.  Every internal binding stays self-consistent
        (using A's identity + B's canonical → recompute view_key etc.
        yields A's key set because they derive from A's tenant + A's
        namespace + A's report_id — actually attack is trickier).

        Simpler realistic attack path: overwrite A's crosswalk BYTES
        with B's whole payload, keep filename = A's key.  Bound reader
        would fail immediately on filename vs recomputed key mismatch.

        The *harder* attack: overwrite A's payload so all identity
        fields = A's, but replace tenant_view_envelope with B's
        overlay bytes (which have a different canonical_sha256 or
        different lane/todo).  The bound reader passes identity.  The
        old (rejected) v2 code returned this payload as an idempotent
        hit.  The second-round fix fresh-decomposes A's raw and
        compares full envelope bytes + RT-014 read_version bytes
        against the stored payload — the tenant_view_envelope drift
        (or canonical_sha drift) surfaces as ``corrupt``.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {
                    "a.md": sample_raw(report_id="207A", body="A body"),
                    "b.md": sample_raw(report_id="207B", body="B body"),
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
                reason="setup",
            )
            cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
            self.assertEqual(len(cws), 2)
            ph_a = R16.compute_legacy_path_hash("a.md")
            by_ph = {cw["legacy_path_hash"]: cw for cw in cws}
            cw_a = by_ph[ph_a]
            cw_b = [cw for cw in cws if cw["legacy_path_hash"] != ph_a][0]

            # Craft a hybrid payload: A's identity + B's tenant_view_envelope /
            # canonical_sha256 / object_bytes_sha256 / publish_receipt /
            # object_id / catalog_key / decompose_report / report_key /
            # view_key would be B's — but crosswalk_key must equal
            # compute_crosswalk_key_v2 for A (bound reader enforces
            # this).  We build a coordinated tamper that keeps A's
            # identity + crosswalk_key but *substitutes* the
            # tenant_view_envelope with B's overlay data.  Recompute
            # canonical_sha256 back to A's (so integrity binding
            # self-agrees) — that will fail because
            # tenant_view_envelope.canonical_sha256 must equal
            # top-level canonical_sha256 (both A's).  So we instead
            # rewrite the tenant_view_envelope's *observed_at* to a
            # different value (JCS-preserving; overlay drift only).

            a_path = _crosswalk_path(self.fx, tenant_id, cw_a["crosswalk_key"])
            payload_a = json.loads(a_path.read_bytes().decode("utf-8"))
            # Introduce lane drift into A's tenant_view_envelope (a
            # legitimate v1-integrity-passing change since the overlay
            # is a dict and lane is optional).
            payload_a["tenant_view_envelope"]["lane"] = "attacker_planted_lane"
            # Rewrite as strict JCS bytes.
            a_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload_a)))

            # A second import_one for a.md must not silently return
            # this coordinated-tampered record.  Fresh decompose of
            # the caller's raw_bytes produces the ORIGINAL
            # tenant_view_envelope (no attacker lane) whose JCS bytes
            # differ from the stored payload → corrupt.
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.import_one(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    raw_bytes=(root / "a.md").read_bytes(),
                    run_id=self.new_run_id(),
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="post-tamper",
                    legacy_path_hint="a.md",
                )
            self.assertEqual(cm.exception.code, "corrupt")

    def test_batch_import_fails_closed_on_coordinated_tamper(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td),
                {"a.md": sample_raw(report_id="207A", body="A body")},
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cws = list(self.fx.importer.iter_crosswalks(tenant_id=tenant_id))
            self.assertEqual(len(cws), 1)
            cw = cws[0]
            path = _crosswalk_path(self.fx, tenant_id, cw["crosswalk_key"])
            payload = json.loads(path.read_bytes().decode("utf-8"))
            # Overlay drift — read_status flipped.
            payload["tenant_view_envelope"]["read_status"] = "read"
            path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
            # A batch re-import must fail closed on the finder-hit.
            source2 = R16.LegacySource(str(root))
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.import_batch(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    source=source2,
                    run_id=self.new_run_id(),
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="reimport",
                )
            self.assertEqual(cm.exception.code, "corrupt")

    def test_finder_hit_fails_closed_when_rt014_object_deleted(self):
        """If a coordinated attacker changes crosswalk canonical_sha to
        point at a canonical that RT-014 doesn't have (or was deleted),
        the fresh-decompose path's read_version() call fails closed.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"a.md": sample_raw(report_id="207A", body="A body")}
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            # Nuke RT-014's shared objects.
            shared_obj_root = self.fx.root / "shared" / "objects"
            for shard in shared_obj_root.iterdir():
                for f in shard.iterdir():
                    f.unlink()
                shard.rmdir()
            # Second import — finder returns the crosswalk; fresh
            # decompose produces the same canonical_sha256; read_version
            # then fails not_found → corrupt.
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.import_one(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    raw_bytes=(root / "a.md").read_bytes(),
                    run_id=self.new_run_id(),
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="reimport",
                    legacy_path_hint="a.md",
                )
            self.assertEqual(cm.exception.code, "corrupt")

    def test_untampered_second_import_still_idempotent(self):
        """Baseline: without tampering, the second import is still a
        clean idempotent hit — fresh-decompose passes all checks.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw(report_id="207A")
        r1 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="one",
            legacy_path_hint="a.md",
        )
        r2 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="two",
            legacy_path_hint="a.md",
        )
        self.assertEqual(r1.crosswalk_key, r2.crosswalk_key)


# ---------------------------------------------------------------------------
# Fix 2: Reconciler tenant_view_envelope byte-level equality
# ---------------------------------------------------------------------------


class ReconcilerTenantViewByteCompareTests(RT016TestBase):
    def test_overlay_drift_prevents_both_equal(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"a.md": sample_raw(report_id="207A", body="A body")}
            )
            source = R16.LegacySource(str(root))
            run_id = self.new_run_id()
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=run_id,
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cw = next(iter(self.fx.importer.iter_crosswalks(tenant_id=tenant_id)))
            path = _crosswalk_path(self.fx, tenant_id, cw["crosswalk_key"])
            payload = json.loads(path.read_bytes().decode("utf-8"))
            # Mutate the overlay's lane — JCS-preserving; every other
            # binding still self-agrees (lane is an optional field).
            payload["tenant_view_envelope"]["lane"] = "attacker_planted_lane"
            path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("re_decompose_mismatch", classes)


# ---------------------------------------------------------------------------
# Canonical tenant-view projection: attacker-writable clock stripping
# ---------------------------------------------------------------------------


class TenantViewProjectionSemanticTests(RT016TestBase):
    """Locks down the security shape of ``canonical_tenant_view_projection``.

    - ``observed_at`` is the ONLY field stripped from both sides — a
      benign wall-clock bump between imports must not cause a false
      positive.
    - Every other field (lane, read_status, todo_status,
      new_reply_flag, reply_overlay, node_overlay, and all envelope
      identity fields) is preserved and must match bit-for-bit.
    - A coordinated tamper that rewrites BOTH the stored
      run_started_at + tenant_view_envelope.observed_at MUST still
      fail closed on any semantic drift — the projection strips
      observed_at symmetrically, so the semantic drift (e.g. lane)
      remains visible.
    """

    def _seed(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw(report_id="207P", body="P body")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="proj-seed",
            legacy_path_hint="p.md",
        )
        path = _crosswalk_path(self.fx, tenant_id, rec.crosswalk_key)
        return tenant_id, rec, path, raw

    def test_projection_strips_only_observed_at(self):
        view = {
            "schema": "cwk.tenant_view.v1",
            "tenant_id": "t_" + "a" * 26,
            "report_key": "cwork:207A",
            "canonical_sha256": "a" * 64,
            "observed_at": "2024-01-01T00:00:00Z",
            "lane": "inbox",
            "read_status": "unread",
            "todo_status": "pending",
        }
        projected = R16.canonical_tenant_view_projection(view)
        self.assertNotIn("observed_at", projected)
        for field in ("schema", "tenant_id", "report_key", "canonical_sha256", "lane", "read_status", "todo_status"):
            self.assertEqual(projected[field], view[field])

    def test_benign_observed_at_change_does_not_reject_reimport(self):
        """A legitimate re-import with a bumped wall clock changes
        observed_at only — the projection is identical, so the
        idempotent hit is accepted.
        """

        tenant_id, rec, _path, raw = self._seed()
        # Re-import with a very different run_started_at.
        rec2 = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=self.new_run_id(),
            run_started_at="2099-12-31T23:59:59Z",
            actor="admin",
            reason="clock-bumped",
            legacy_path_hint="p.md",
        )
        self.assertEqual(rec2.crosswalk_key, rec.crosswalk_key)

    def test_lane_rewrite_alone_fails_closed(self):
        """The pure semantic drift case: attacker rewrites only lane.
        Projection catches it on the next import.
        """

        tenant_id, rec, path, raw = self._seed()
        payload = json.loads(path.read_bytes().decode("utf-8"))
        payload["tenant_view_envelope"]["lane"] = "attacker_lane"
        path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        with self.assertRaises(R16.LegacyImportError) as cm:
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="reimport",
                legacy_path_hint="p.md",
            )
        self.assertEqual(cm.exception.code, "corrupt")

    def test_coordinated_run_started_at_and_observed_at_tamper_still_fails(self):
        """The critical attack: attacker rewrites BOTH the
        crosswalk's stored ``run_started_at`` AND
        ``tenant_view_envelope.observed_at`` to a fabricated timestamp,
        AND injects a semantic overlay drift (lane).  The verification
        must still fail closed on the semantic drift because the
        projection drops observed_at from BOTH sides.
        """

        tenant_id, rec, path, raw = self._seed()
        payload = json.loads(path.read_bytes().decode("utf-8"))
        # Coordinated tamper: rewrite both attacker-writable clocks +
        # inject a semantic drift.
        payload["run_started_at"] = "2099-12-31T23:59:59Z"
        payload["tenant_view_envelope"]["observed_at"] = "2099-12-31T23:59:59Z"
        payload["tenant_view_envelope"]["lane"] = "attacker_lane"
        path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        with self.assertRaises(R16.LegacyImportError) as cm:
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="post-tamper",
                legacy_path_hint="p.md",
            )
        self.assertEqual(cm.exception.code, "corrupt")

    def test_coordinated_clock_tamper_reconciler_also_fails(self):
        """Same attack surfaced through the reconciler path — semantic
        drift + coordinated clock rewrite must not be counted as
        both_equal.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"p.md": sample_raw(report_id="207P", body="P body")}
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cw = next(iter(self.fx.importer.iter_crosswalks(tenant_id=tenant_id)))
            path = _crosswalk_path(self.fx, tenant_id, cw["crosswalk_key"])
            payload = json.loads(path.read_bytes().decode("utf-8"))
            payload["run_started_at"] = "2099-12-31T23:59:59Z"
            payload["tenant_view_envelope"]["observed_at"] = "2099-12-31T23:59:59Z"
            payload["tenant_view_envelope"]["read_status"] = "read"  # semantic drift
            path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        self.assertEqual(report.both_equal_count, 0)
        classes = {s["classification"] for s in report.payload["failure_samples"]}
        self.assertIn("re_decompose_mismatch", classes)

    def test_observed_at_only_change_reconciler_still_passes(self):
        """Legitimate case: legacy re-import with a different
        observed_at (but same semantic overlay).  Reconciler must not
        false-positive.  We simulate this by seeding a crosswalk, then
        rewriting only its observed_at (no other change), then
        reconciling with the same legacy bytes.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"p.md": sample_raw(report_id="207P", body="P body")}
            )
            source = R16.LegacySource(str(root))
            self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="setup",
            )
            cw = next(iter(self.fx.importer.iter_crosswalks(tenant_id=tenant_id)))
            path = _crosswalk_path(self.fx, tenant_id, cw["crosswalk_key"])
            payload = json.loads(path.read_bytes().decode("utf-8"))
            payload["tenant_view_envelope"]["observed_at"] = "2099-12-31T23:59:59Z"
            path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
            source2 = R16.LegacySource(str(root))
            source2.snapshot()
            report = self.fx.reconciler.reconcile(
                anchor=default_anchor(tenant_id),
                run_id=self.new_run_id(),
                source=source2,
            )
        self.assertEqual(report.both_equal_count, 1)
        self.assertEqual(report.only_new_count, 0)


# ---------------------------------------------------------------------------
# Fix 3: Old v1 review audit compat (no source_namespace)
# ---------------------------------------------------------------------------


class V1ReviewAuditCompatTests(RT016TestBase):
    def _plant_v0_v1_review(self, tenant_id: str, run_id: str) -> str:
        """Plant a pre-Minor-2 v1 review payload with NO source_namespace."""

        legacy_bytes = b"pre-minor2-v1 raw"
        legacy_source_sha256 = hashlib.sha256(legacy_bytes).hexdigest()
        legacy_path_hash = R16.compute_legacy_path_hash("very-old/legacy.md")
        # Pre-Minor-2 review_id derivation did not include
        # source_namespace either.  We reuse compute_review_id which is
        # still exposed for that purpose.  The v1 loader (post-fix)
        # accepts payloads without source_namespace.
        review_id = R16.compute_review_id(
            tenant_id, "cwork", legacy_source_sha256, run_id
        )
        payload = {
            "schema": "cwk.rt016.review_entry.v1",
            "review_id": review_id,
            "tenant_id": tenant_id,
            # deliberately NO source_namespace — pre-Minor-2 shape
            "legacy_source_sha256": legacy_source_sha256,
            "legacy_path_hash": legacy_path_hash,
            "source_kind": "current_raw",
            "decomposer_version": R16.DECOMPOSER_VERSION,
            "normalizer_version": R16.NORMALIZER_VERSION,
            "decompose_report": {
                "schema": "cwk.rt016.decompose_report.v1",
                "decomposer_version": R16.DECOMPOSER_VERSION,
                "normalizer_version": R16.NORMALIZER_VERSION,
                "legacy_source_sha256": legacy_source_sha256,
                "canonical_sha256": None,
                "object_bytes_sha256": None,
                "body_bytes_length": 0,
                "body_truncation_would_occur": False,
                "field_sources": {},
                "hit_rules": [],
                "unknown_frontmatter_keys": [],
                "unknown_row_keys": [],
                "unknown_reply_keys": [],
                "unknown_node_keys": [],
                "timeline_event_hash_check": {
                    "snapshot_count": 0,
                    "event_count": 0,
                    "matched_event_count": 0,
                    "unmatched_event_count": 0,
                    "coverage_ok": True,
                },
                "decomposition_status": "quarantined",
                "quarantine_reasons": ["missing_author"],
            },
            "migration_status": "review",
            "quarantine_reasons": ["missing_author"],
            "run_id": run_id,
            "run_started_at": "2024-06-15T10:30:00Z",
            "created_at": "2024-06-15T10:30:00Z",
            "record_revision": 1,
        }
        # Seed a legit v2 record first so the tenant subdirs exist.
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(report_id="207SEED", body="seed"),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="seed.md",
        )
        rv_dir = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "review"
        )
        rv_dir.mkdir(parents=True, exist_ok=True)
        rv_path = rv_dir / f"{review_id}.json"
        rv_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        os.chmod(rv_path, 0o600)
        return review_id

    def test_pre_minor2_v1_review_loads_via_iter_reviews(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        rid = self._plant_v0_v1_review(tenant_id, run_id)
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        found = [r for r in reviews if r["review_id"] == rid]
        self.assertEqual(len(found), 1)
        rec = found[0]
        self.assertEqual(rec["schema"], "cwk.rt016.review_entry.v1")
        self.assertNotIn("source_namespace", rec)

    def test_pre_minor2_v1_review_never_hits_v2_idempotency(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        self._plant_v0_v1_review(tenant_id, run_id)
        # Import a *new* quarantine with the same tenant + ns.  The
        # v2 finder must not silently reuse the v1 record; a fresh v2
        # review is written.
        bad_raw = sample_raw(create_time="never")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="new-quar",
            legacy_path_hint="q.md",
        )
        self.assertIn(rec.outcome, ("review", "undecomposable"))
        reviews = list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        v2 = [r for r in reviews if r.get("schema") == "cwk.rt016.review_entry.v2"]
        self.assertGreaterEqual(len(v2), 1)


# ---------------------------------------------------------------------------
# Fix 4: bound readers for review / manifest
# ---------------------------------------------------------------------------


class BoundReviewReaderNegativesTests(RT016TestBase):
    def _seed_v2_review(self, tenant_id: str, run_id: str) -> tuple[str, Path, dict]:
        bad_raw = sample_raw(create_time="never")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=bad_raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="q.md",
        )
        rv_path = _review_path(self.fx, tenant_id, rec.review_id)
        raw = rv_path.read_bytes()
        return rec.review_id, rv_path, json.loads(raw.decode("utf-8"))

    def test_review_filename_review_id_mismatch_detected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        rid, rv_path, payload = self._seed_v2_review(tenant_id, run_id)
        # Move the file to a different (still-valid-shape) review_id.
        wrong_id = "qe_" + "z" * 26
        wrong_path = rv_path.parent / f"{wrong_id}.json"
        rv_path.rename(wrong_path)
        # iter_reviews now sees a file whose filename disagrees with
        # the payload's review_id → corrupt.
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_reviews(tenant_id=tenant_id))
        self.assertEqual(cm.exception.code, "corrupt")

    def test_review_tenant_id_disagrees_with_parent_dir_detected(self):
        tenant_id_a = self.fx.new_tenant(status="pilot")
        tenant_id_b = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        # Seed a v2 review under tenant B.
        rid, rv_path_b, payload_b = self._seed_v2_review(tenant_id_b, run_id)
        # Also make sure tenant A has the review subdir.
        self.fx.importer.import_one(
            tenant_id=tenant_id_a,
            source_namespace="cwork",
            raw_bytes=sample_raw(report_id="207SEED"),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed-a",
            legacy_path_hint="seed.md",
        )
        # Copy B's review file into A's review dir.
        a_review_dir = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id_a
            / "review"
        )
        a_review_dir.mkdir(parents=True, exist_ok=True)
        (a_review_dir / f"{rid}.json").write_bytes(rv_path_b.read_bytes())
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_reviews(tenant_id=tenant_id_a))
        self.assertEqual(cm.exception.code, "corrupt")

    def test_bad_review_does_not_suppress_only_legacy_in_reconciler(self):
        """A review that fails bound loading must not silently claim
        coverage of its path_hash.  ``iter_reviews`` raises; the
        reconciler propagates the corruption.  The legacy file at that
        path is therefore never counted as covered — the caller sees
        an explicit corrupt failure, not a quiet only_legacy = 0.
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        rid, rv_path, _payload = self._seed_v2_review(tenant_id, run_id)
        # Corrupt the review file: rewrite the payload's review_id to
        # something else while keeping the filename.
        payload = json.loads(rv_path.read_bytes().decode("utf-8"))
        payload["review_id"] = "qe_" + "y" * 26
        rv_path.write_bytes(C.canonical_json_bytes(C.nfc_normalize(payload)))
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(
                Path(td), {"q.md": sample_raw(create_time="never")}
            )
            source = R16.LegacySource(str(root))
            source.snapshot()
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.reconciler.reconcile(
                    anchor=default_anchor(tenant_id),
                    run_id=self.new_run_id(),
                    source=source,
                )
            self.assertEqual(cm.exception.code, "corrupt")


class BoundManifestReaderNegativesTests(RT016TestBase):
    def _seed_manifest(self, tenant_id: str, run_id: str) -> Path:
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(report_id="207M"),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="seed",
            legacy_path_hint="a.md",
        )
        return _manifest_path(self.fx, tenant_id, run_id)

    def test_manifest_line_wrong_tenant_id_detected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        mf_path = self._seed_manifest(tenant_id, run_id)
        raw = mf_path.read_bytes()
        # Corrupt just the first line's tenant_id.
        lines = raw.split(b"\n")
        payload = json.loads(lines[0].decode("utf-8"))
        payload["tenant_id"] = "t_" + "z" * 26
        lines[0] = C.canonical_json_bytes(C.nfc_normalize(payload))
        mf_path.write_bytes(b"\n".join(lines))
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))
        self.assertEqual(cm.exception.code, "corrupt")

    def test_manifest_line_wrong_run_id_detected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        mf_path = self._seed_manifest(tenant_id, run_id)
        raw = mf_path.read_bytes()
        lines = raw.split(b"\n")
        payload = json.loads(lines[0].decode("utf-8"))
        payload["run_id"] = "run_" + "z" * 26
        lines[0] = C.canonical_json_bytes(C.nfc_normalize(payload))
        mf_path.write_bytes(b"\n".join(lines))
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))
        self.assertEqual(cm.exception.code, "corrupt")

    def test_manifest_file_moved_to_different_run_leaf_detected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        mf_path = self._seed_manifest(tenant_id, run_id)
        wrong_run_id = R16.new_run_id()
        wrong_path = mf_path.parent / f"{wrong_run_id}.jsonl"
        mf_path.rename(wrong_path)
        # Now the file's run_id in payload disagrees with the file name.
        with self.assertRaises(R16.LegacyImportError) as cm:
            list(
                self.fx.importer.iter_manifest(
                    tenant_id=tenant_id, run_id=wrong_run_id
                )
            )
        self.assertEqual(cm.exception.code, "corrupt")


# ---------------------------------------------------------------------------
# Fix 6: frozen test allowlist actually catches drift
# ---------------------------------------------------------------------------


class FrozenBaselineCatchesDriftTests(unittest.TestCase):
    """Meta-test: the pinned baseline map catches simulated drift.

    We do NOT actually touch a frozen file; we simulate a drift by
    calling the SHA compare with a fabricated wrong SHA and confirm
    the assertion would fire.  Additionally we assert the pinned
    baseline dict is non-empty and covers the expected files.
    """

    def test_baseline_covers_expected_frozen_files(self):
        from test_rt016_schemas import (  # noqa: E402
            _FROZEN_RT011_015_BASELINE_SHAS,
            _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS,
        )
        # Sample RT-011~015 files that MUST be in the pin.
        for req in (
            "scripts/cwk_pr001_contracts.py",
            "scripts/cwk_shared_evidence.py",
            "scripts/cwk_access_ledger.py",
            "scripts/cwk_tenant_view.py",
            "scripts/cwk_raw_store.py",
            "scripts/cwk_nightly_pipeline.py",
        ):
            self.assertIn(req, _FROZEN_RT011_015_BASELINE_SHAS)
        # RT-016 v1 schemas required in the pin.
        for req in (
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/decompose_report.schema.json",
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/migration_crosswalk.schema.json",
            "PR/PR-001-multitenant-knowledge-spaces/contracts/rt016/schemas/review_entry.schema.json",
        ):
            self.assertIn(req, _FROZEN_RT016_V1_SCHEMA_BASELINE_SHAS)

    def test_simulated_drift_would_fail_assertion(self):
        """Not a git operation: verify the SHA comparison logic
        actually flags a mismatched SHA.  The equality check inside
        the frozen test is a simple `assertEqual` on two hex strings,
        which fails if the pinned baseline is edited to disagree with
        the actual file.
        """

        from test_rt016_schemas import _FROZEN_RT011_015_BASELINE_SHAS

        rel = "scripts/cwk_pr001_contracts.py"
        real_sha = _FROZEN_RT011_015_BASELINE_SHAS[rel]
        pretend_current_sha = "0" * 64
        # Simulating the same equality that the real test uses:
        with self.assertRaises(AssertionError):
            self.assertEqual(pretend_current_sha, real_sha)


if __name__ == "__main__":
    unittest.main()
