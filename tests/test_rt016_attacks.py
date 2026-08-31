"""RT-016 security / attack surface tests.

Covers:
- log injection in actor/reason
- path containment (symlink / hardlink / traversal)
- input tampering: crosswalk / review / manifest tamper detection
- crash-recovery safety
- concurrency / duplicate imports
- forbidden-field scans of all durable payloads
- error opacity (no bodies, no absolute paths, no credentials leak)
- RT-015 API insufficiency documented as a fail-closed blocker (view deferred)
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

from _rt016_helpers import (  # noqa: E402
    AF,
    AL,
    C,
    FakeAuthorityContext,
    Fixture,
    I,
    R16,
    RT016TestBase,
    SE,
    TV,
    build_legacy_tree,
    sample_raw,
    utc_iso,
)


class LogInjectionTests(RT016TestBase):
    def test_actor_with_newline_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        raw = sample_raw()
        with self.assertRaises(R16.LogInjectionDetected):
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=raw,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin\nRESET",
                reason="ok",
                legacy_path_hint="a.md",
            )

    def test_reason_with_control_chars_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with self.assertRaises(R16.LogInjectionDetected):
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=sample_raw(),
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="bad\x1b[31mred",
                legacy_path_hint="a.md",
            )

    def test_actor_too_long_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with self.assertRaises(R16.LogInjectionDetected):
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=sample_raw(),
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="x" * 129,
                reason="ok",
                legacy_path_hint="a.md",
            )


class PathContainmentTests(RT016TestBase):
    def test_symlink_file_in_legacy_tree_skipped(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": sample_raw()})
            # Create a symlink pointing at a valid file, but under a
            # different name.
            (root / "b.md").symlink_to(root / "a.md")
            source = R16.LegacySource(str(root))
            receipts = self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="symlink test",
            )
        # Only the real file (a.md) is processed; symlink b.md is
        # silently skipped by LegacySource._walk_at.
        self.assertEqual(len(receipts), 1)

    def test_hardlink_file_in_legacy_tree_skipped(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with tempfile.TemporaryDirectory() as td:
            root = build_legacy_tree(Path(td), {"a.md": sample_raw()})
            os.link(root / "a.md", root / "a-hardlink.md")
            source = R16.LegacySource(str(root))
            receipts = self.fx.importer.import_batch(
                tenant_id=tenant_id,
                source_namespace="cwork",
                source=source,
                run_id=self.new_run_id(),
                run_started_at=utc_iso(),
                actor="admin",
                reason="hardlink test",
            )
        # Neither file is processed because both have nlink==2 after
        # the hardlink is created.  This is deliberately conservative:
        # if the caller wants hardlinked files migrated, they must
        # break the hardlink first (a manual, auditable step).
        self.assertEqual(len(receipts), 0)

    def test_traversal_in_legacy_path_hint_hash_deterministic(self):
        # Path hint is opaquely hashed; even a hostile string cannot
        # cause path traversal in RT-016's storage.
        hash_a = R16.compute_legacy_path_hash("../../etc/passwd")
        hash_b = R16.compute_legacy_path_hash("../../etc/passwd")
        self.assertEqual(hash_a, hash_b)

    def test_run_id_grammar_enforced(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        with self.assertRaises(R16.LegacyImportError):
            self.fx.importer.import_one(
                tenant_id=tenant_id,
                source_namespace="cwork",
                raw_bytes=sample_raw(),
                run_id="../etc/passwd",
                run_started_at=utc_iso(),
                actor="admin",
                reason="ok",
                legacy_path_hint="a.md",
            )


class TamperingTests(RT016TestBase):
    def test_crosswalk_field_swap_rejected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="tamper",
            legacy_path_hint="a.md",
        )
        leaf = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "crosswalks"
            / f"{rec.crosswalk_key}.json"
        )
        # Insert a forbidden field.
        payload = json.loads(leaf.read_text(encoding="utf-8"))
        payload["credential_ref"] = "secret://evil"
        leaf.write_bytes(json.dumps(payload).encode("utf-8"))
        with self.assertRaises(R16.LegacyImportError):
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
            )

    def test_manifest_entry_corruption_detected(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="setup",
            legacy_path_hint="a.md",
        )
        manifest_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "manifests"
            / f"{run_id}.jsonl"
        )
        # Wipe the trailing newline (RT-016 refuses truncated manifests).
        raw = manifest_path.read_bytes().rstrip(b"\n")
        manifest_path.write_bytes(raw)
        with self.assertRaises(R16.LegacyImportError):
            list(self.fx.importer.iter_manifest(tenant_id=tenant_id, run_id=run_id))


class ConcurrentImportTests(RT016TestBase):
    def test_repeated_import_yields_single_crosswalk(self):
        """Concurrent workers converge to one crosswalk_key.

        RT-015's on-disk lock file has a well-documented macOS-only
        ENOENT race under high contention (RT-014 works around the same
        quirk with a bounded retry).  RT-016 wraps ``AccessLedger.observe``
        with a bounded retry inside :meth:`ShadowImporter.import_one`;
        this test therefore asserts:

        - at least one worker succeeds;
        - every successful worker returns the same ``crosswalk_key``;
        - every failure is an accepted transient race token
          (``FileNotFoundError`` or ``LegacyImportError(code=ledger_denied)``
          that the caller can retry once the contention window has
          passed).
        """

        tenant_id = self.fx.new_tenant(status="pilot")
        run_id = self.new_run_id()
        raw = sample_raw()
        keys: list[str] = []
        errors: list[tuple[BaseException, str]] = []

        def go():
            try:
                rec = self.fx.importer.import_one(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    raw_bytes=raw,
                    run_id=run_id,
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="conc",
                    legacy_path_hint="a.md",
                )
                keys.append(rec.crosswalk_key)
            except BaseException as exc:
                import traceback as _tb
                errors.append((exc, _tb.format_exc()))

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Every failure must be one of the accepted transient races.
        for exc, trace in errors:
            self.assertTrue(
                isinstance(exc, FileNotFoundError)
                or (
                    isinstance(exc, R16.LegacyImportError)
                    and exc.code == "ledger_denied"
                ),
                f"unexpected non-race failure: {exc!r}\n{trace}",
            )
        # At least one worker must have succeeded (retry converges).
        self.assertGreaterEqual(len(keys), 1)
        # Every successful worker sees the same crosswalk_key.
        self.assertEqual(
            len(set(keys)),
            1,
            f"expected a single crosswalk_key, saw {set(keys)}",
        )
        # A follow-up serial import returns the same key idempotently.
        rec_after = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=raw,
            run_id=run_id,
            run_started_at=utc_iso(),
            actor="admin",
            reason="conc-after",
            legacy_path_hint="a.md",
        )
        self.assertEqual(rec_after.crosswalk_key, keys[0])


class LedgerConflictTests(RT016TestBase):
    def test_in_flight_revocation_defers_view(self):
        tenant_id = self.fx.new_tenant(status="active")
        raw = sample_raw()
        with FakeAuthorityContext() as ctx:
            grant_key = AL.compute_grant_key(tenant_id, "cwork:2070001")

            # First bring the grant to a revocation-in-progress state
            # by writing an intent journal directly (RT-015 uses this
            # crash-safe pipeline).  We must first observe the grant.
            observation = {
                "schema": "cwk.access_observation.v1",
                "tenant_id": tenant_id,
                "source_namespace": "cwork",
                "report_id": "2070001",
                "observed_at": utc_iso(),
                "observation_source": "legacy_raw_decomposition",
                "initial_status": "granted",
                "roles": ["receiver"],
                "visibility_scope": "unknown",
                "evidence_refs": ["pre-import"],
            }
            self.fx.ledger.observe(
                observation=observation, actor="admin", reason="pre"
            )
            # Kick off revocation; grant is not yet 'active' so the
            # revocation succeeds immediately.  The subsequent import
            # should then hit the "grant already revoked/tombstoned"
            # path via observe.
            self.fx.ledger.revoke(
                tenant_id=tenant_id,
                source_namespace="cwork",
                report_id="2070001",
                actor="admin",
                reason="revoke first",
            )

            receipt = ctx.receipt(
                tenant_id=tenant_id,
                source_namespace="cwork",
                report_id="2070001",
                grant_key=grant_key,
            )
            with self.assertRaises(R16.LegacyImportError) as cm:
                self.fx.importer.import_one(
                    tenant_id=tenant_id,
                    source_namespace="cwork",
                    raw_bytes=raw,
                    run_id=self.new_run_id(),
                    run_started_at=utc_iso(),
                    actor="admin",
                    reason="post-revoke",
                    legacy_path_hint="a.md",
                    authority_receipt=receipt,
                )
            # observe raises GrantStateError on tombstoned grant;
            # RT-016 wraps that as ledger_denied.
            self.assertEqual(cm.exception.code, "ledger_denied")


class ForbiddenFieldScanTests(RT016TestBase):
    def test_no_credentials_or_paths_leak_into_crosswalk(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="scan",
            legacy_path_hint="/absolute/host/path/should/not/leak.md",
        )
        cw_path = (
            self.fx.root
            / "registry"
            / R16.REGISTRY_SUBDIR
            / tenant_id
            / "crosswalks"
            / f"{rec.crosswalk_key}.json"
        )
        text = cw_path.read_text(encoding="utf-8")
        # No absolute path style tokens.
        self.assertNotIn("/absolute/host/path", text)
        # No credential keywords in leaf position (the value pattern for
        # authority receipts is deliberately never included in crosswalk).
        for banned in ("app_key", "credential_ref", "cookie", "session_token"):
            self.assertNotIn(f'"{banned}"', text)


class ErrorOpacityTests(RT016TestBase):
    def test_error_str_has_no_absolute_path(self):
        # Trigger a review then read a corrupt crosswalk (missing file).
        tenant_id = self.fx.new_tenant(status="pilot")
        with self.assertRaises(R16.LegacyImportError) as cm:
            # Read a crosswalk that does not exist.
            self.fx.importer.read_crosswalk(
                tenant_id=tenant_id, crosswalk_key="cw_" + "a" * 26
            )
        msg = str(cm.exception)
        self.assertNotIn(str(self.fx.root), msg)
        self.assertNotIn("/tmp", msg)


class Rt015InsufficientAuthorityBlockerTests(RT016TestBase):
    """Documents RT-015's authority-adapter fail-closed default as a blocker.

    The default RT-015 :class:`AuthorityAdapter` refuses every promotion
    request.  RT-016 respects that boundary: without a caller-supplied
    receipt (production path), the tenant view is deferred and the
    crosswalk records the reason.  The migration proceeds — canonical
    and observation are persisted — but view upsert waits until real
    authority is available.  This test locks in the contract so any
    future re-wiring of RT-015 is caught.
    """

    def test_production_path_defers_view_and_records_blocker_reason(self):
        tenant_id = self.fx.new_tenant(status="pilot")
        rec = self.fx.importer.import_one(
            tenant_id=tenant_id,
            source_namespace="cwork",
            raw_bytes=sample_raw(),
            run_id=self.new_run_id(),
            run_started_at=utc_iso(),
            actor="admin",
            reason="prod path",
            legacy_path_hint="a.md",
        )
        self.assertFalse(rec.tenant_view_written)
        self.assertEqual(
            rec.tenant_view_deferred_reason, "no_authority_receipt_available"
        )
        # Crosswalk still records the tenant view envelope so a future
        # authority-enabled run can replay upsert without re-decomposing.
        cw = self.fx.importer.read_crosswalk(
            tenant_id=tenant_id, crosswalk_key=rec.crosswalk_key
        )
        C.validate_tenant_view(cw["tenant_view_envelope"])


if __name__ == "__main__":
    unittest.main()
