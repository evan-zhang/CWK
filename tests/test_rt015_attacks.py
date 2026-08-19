"""RT-015: adversarial / attack-surface tests.

Covers:

- Log injection: NUL / CR / LF / ESC / other control chars in actor/reason
  rejected (both mutation-time and schema-time).
- Path traversal / Unicode / percent-encoded traversal / hidden separators
  rejected via existing RT-011 REPORT_ID_REGEX + tenant_id validation.
- Symlink attack on grant files → refused via O_NOFOLLOW.
- Hardlink attack on grant files → SHA reverify triggers fail-closed.
- Tampered tombstone / event log → detected as corruption.
- Preexisting temp files → recovered without impacting committed data.
- Error opacity: exceptions never contain absolute host paths or
  credential material.
- Enumeration prevention: no API returns other tenants' data even with
  raw tenant_id introspection attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_atomic_file as AF  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_view as TV  # noqa: E402
import _rt015_helpers as H  # noqa: E402


class LogInjectionTests(H.LedgerTestBase):
    BAD_STRINGS = (
        "admin\n",
        "admin\r",
        "admin\r\n",
        "admin\x00",
        "admin\x1b[31mRED\x1b[0m",
        "admin\t",
        "admin\x7f",
        "",
        "𝕒" * 200,  # too long (also non-ASCII would fail pattern too)
    )

    def test_observation_actor_bad_strings_rejected(self):
        t = self.fx.new_tenant()
        obs = H.observation(tenant_id=t)
        for bad in self.BAD_STRINGS:
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.observe(observation=obs, actor=bad, reason="ok")

    def test_observation_reason_bad_strings_rejected(self):
        t = self.fx.new_tenant()
        obs = H.observation(tenant_id=t)
        for bad in self.BAD_STRINGS:
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.observe(observation=obs, actor="admin", reason=bad)

    def test_revoke_actor_bad_strings_rejected(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        for bad in ("admin\n", "\x00"):
            with self.assertRaises(AL.LogInjectionDetected):
                self.fx.ledger.revoke(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    actor=bad,
                    reason="off",
                )


class PathTraversalTests(H.LedgerTestBase):
    def test_report_id_with_slash_rejected(self):
        t = self.fx.new_tenant()
        with self.assertRaises(Exception):
            AL.compute_grant_key(t, "cwork:../etc/passwd")

    def test_report_id_with_backslash_rejected(self):
        t = self.fx.new_tenant()
        with self.assertRaises(Exception):
            AL.compute_grant_key(t, "cwork:..\\etc")

    def test_report_id_with_nul_rejected(self):
        t = self.fx.new_tenant()
        with self.assertRaises(Exception):
            AL.compute_grant_key(t, "cwork:foo\x00bar")

    def test_report_id_with_newline_rejected(self):
        t = self.fx.new_tenant()
        with self.assertRaises(Exception):
            AL.compute_grant_key(t, "cwork:foo\nbar")

    def test_source_namespace_with_upper_rejected(self):
        t = self.fx.new_tenant()
        with self.assertRaises(Exception):
            AL.compute_grant_key(t, "CWORK:2070001")

    def test_tenant_id_with_traversal_rejected(self):
        with self.assertRaises(Exception):
            AL.compute_grant_key("t_../aaaaaaaaaaaaaaaaaaaaaaaaaa", "cwork:R1")

    def test_unicode_lookalike_rejected(self):
        # Various Unicode lookalikes for '/' or '.' — the tenant regex
        # only accepts [a-z0-9] so any non-ASCII fails immediately.
        with self.assertRaises(Exception):
            AL.compute_grant_key("t_" + "𝕒" * 26, "cwork:R1")


class SymlinkAttackTests(H.LedgerTestBase):
    def test_grant_file_symlinked_rejected(self):
        """Replace a grant file with a symlink → next read fails closed."""

        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        grants_dir = self.fx.root / "registry" / "access-ledger" / t / "grants"
        target = grants_dir / f"{grant_key}.json"
        # Overwrite with a symlink to /dev/null.
        target.unlink()
        os.symlink("/dev/null", target)
        # Any subsequent read must fail closed via O_NOFOLLOW.
        with self.assertRaises(Exception):
            with self.fx.ledger._tenant_fd(t) as tfd:
                self.fx.ledger._read_grant_file(tfd, grant_key)

    def test_events_symlink_rejected(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        events_dir = self.fx.root / "registry" / "access-ledger" / t / "events"
        target = events_dir / f"{grant_key}.jsonl"
        if target.exists():
            target.unlink()
        os.symlink("/dev/null", target)
        # iter_events must not follow the symlink.
        with self.assertRaises(Exception):
            self.fx.ledger.iter_events(
                tenant_id=t, source_namespace="cwork", report_id="2070001"
            )


class HardlinkAttackTests(H.LedgerTestBase):
    def test_grant_file_hardlinked_reads_still_verify_sha(self):
        """Hardlinking cannot change file content, but writing to the
        hardlinked file changes the SHA — the SHA-verify catches drift.
        """

        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        grants_dir = self.fx.root / "registry" / "access-ledger" / t / "grants"
        target = grants_dir / f"{grant_key}.json"
        hard = grants_dir / f".attacker-hardlink"
        os.link(target, hard)
        # Mutate content via the hardlink.
        hard.write_text('{"tampered": true}')
        # Now the CAS + JCS verify should reject the change.
        with self.assertRaises(AL.GrantCorruption):
            with self.fx.ledger._tenant_fd(t) as tfd:
                self.fx.ledger._read_grant_file(tfd, grant_key)


class TamperedFilesTests(H.LedgerTestBase):
    def test_tombstone_tamper_detected(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        self.fx.ledger.revoke(
            tenant_id=t,
            source_namespace="cwork",
            report_id="2070001",
            actor="admin",
            reason="off",
        )
        # Tamper: rewrite tombstone with junk.
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        tomb_path = (
            self.fx.root
            / "registry"
            / "access-ledger"
            / t
            / "tombstones"
            / f"{grant_key}.json"
        )
        tomb_path.write_bytes(b'{"schema":"cwk.rt015.access_tombstone.v1","bogus":true}')
        with self.assertRaises(Exception):
            self.fx.ledger.read_tombstone(
                tenant_id=t, source_namespace="cwork", report_id="2070001"
            )

    def test_event_log_tamper_detected(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        events_path = (
            self.fx.root
            / "registry"
            / "access-ledger"
            / t
            / "events"
            / f"{grant_key}.jsonl"
        )
        # Append a malformed line.
        with events_path.open("a") as f:
            f.write("not-json-at-all\n")
        with self.assertRaises(AL.GrantCorruption):
            self.fx.ledger.iter_events(
                tenant_id=t, source_namespace="cwork", report_id="2070001"
            )

    def test_grant_file_json_corruption_detected(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        grants_dir = self.fx.root / "registry" / "access-ledger" / t / "grants"
        (grants_dir / f"{grant_key}.json").write_bytes(b"not json")
        snap = self.fx.snapshot(t)
        # Query eligibility must fail closed with denied (grant_corrupt).
        with self.assertRaises(AL.AccessDenied) as cm:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="2070001"
            )
        # The code is "denied" and reason is opaque.
        self.assertEqual(cm.exception.code, "denied")


class PreexistingTempFileTests(H.LedgerTestBase):
    def test_atomic_write_survives_preexisting_temp_files(self):
        """An attacker pre-creates a file that guesses the temp prefix — the
        atomic-file write must still succeed (secrets.token_hex retry) and
        recovery must sweep the attacker's file.
        """

        t = self.fx.new_tenant()
        grants_dir = self.fx.root / "registry" / "access-ledger" / t / "grants"
        grants_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create some tmp files.
        (grants_dir / f"{AF.TEMP_PREFIX}attacker1.junk").write_bytes(b"junk")
        (grants_dir / f"{AF.TEMP_PREFIX}attacker2.junk").write_bytes(b"junk")
        # A real observation should still succeed.
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        # Recovery cleans up the attacker files.
        report = self.fx.ledger.recover(actor="admin", reason="sweep")
        self.assertGreaterEqual(report.orphans_removed, 2)


class ErrorOpacityTests(H.LedgerTestBase):
    def test_grant_not_found_message_opaque(self):
        t = self.fx.new_tenant()
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied) as cm:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="never"
            )
        msg = str(cm.exception)
        self.assertNotIn(str(self.fx.root), msg)
        self.assertNotIn("/tmp", msg)
        self.assertNotIn("/private", msg)

    def test_denied_message_does_not_leak_report_id(self):
        t = self.fx.new_tenant()
        snap = self.fx.snapshot(t)
        with self.assertRaises(AL.AccessDenied) as cm:
            self.fx.ledger.check_query_eligibility(
                snapshot=snap, source_namespace="cwork", report_id="secretR"
            )
        self.assertNotIn("secretR", str(cm.exception))

    def test_authority_rejection_does_not_leak_secret(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            secret = auth.secret
            # Attempt without observation.
            with self.assertRaises(AL.AccessLedgerError) as cm:
                r = auth.receipt(tenant_id=t)
                self.fx.ledger.promote_to_active(
                    tenant_id=t,
                    source_namespace="cwork",
                    report_id="2070001",
                    authority_receipt=r,
                    actor="admin",
                    reason="promote",
                )
            # Secret bytes never appear in the error message.
            self.assertNotIn(secret.hex(), str(cm.exception))

    def test_view_error_no_paths(self):
        t = self.fx.new_tenant()
        snap = self.fx.snapshot(t)
        with self.assertRaises(TV.TenantViewError) as cm:
            self.fx.view_store.read_view(
                snapshot=snap, source_namespace="cwork", report_id="never"
            )
        msg = str(cm.exception)
        self.assertNotIn(str(self.fx.root), msg)
        self.assertNotIn("/private", msg)


class EnumerationTests(H.LedgerTestBase):
    def test_list_query_eligible_empty_tenant(self):
        t = self.fx.new_tenant()
        snap = self.fx.snapshot(t)
        self.assertEqual(self.fx.ledger.list_query_eligible(snapshot=snap), [])

    def test_iter_cleanup_outbox_empty_tenant(self):
        t = self.fx.new_tenant()
        self.assertEqual(self.fx.ledger.iter_cleanup_outbox(tenant_id=t), [])

    def test_no_report_id_enumeration_api(self):
        # AccessLedger MUST NOT expose ``has_report_id`` /
        # ``resolve_by_report_id`` / any hash-existence probe.
        for name in (
            "has_report_id", "resolve_by_report_id", "list_all_reports",
            "list_reports", "list_grants", "list_tenants",
        ):
            self.assertFalse(hasattr(AL.AccessLedger, name),
                             f"AccessLedger accidentally exposes {name!r}")


class DirectoryPermissionsTests(H.LedgerTestBase):
    def test_tenant_subdir_created_0700(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        tenant_dir = self.fx.root / "registry" / "access-ledger" / t
        self.assertTrue(tenant_dir.is_dir())
        self.assertEqual(stat.S_IMODE(tenant_dir.stat().st_mode), 0o700)
        for sub in ("grants", "events", "revoke-intents", "revoke-receipts",
                    "tombstones", "cleanup-outbox", "locks"):
            self.assertEqual(
                stat.S_IMODE((tenant_dir / sub).stat().st_mode), 0o700
            )

    def test_grant_files_created_0600(self):
        t = self.fx.new_tenant()
        with H.FakeAuthorityContext() as auth:
            self._grant_flow_to_active(tenant_id=t, signer=auth)
        report_key = C.compose_report_key("cwork", "2070001")
        grant_key = AL.compute_grant_key(t, report_key)
        grant_file = (
            self.fx.root / "registry" / "access-ledger" / t / "grants"
            / f"{grant_key}.json"
        )
        self.assertEqual(stat.S_IMODE(grant_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
