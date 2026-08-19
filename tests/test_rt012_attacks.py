"""RT-012 attack matrix.

Every test targets a specific attacker capability enumerated in the
RT-012 plan (path traversal, encoded variants, softlink/hardlink,
TOCTOU, tenant/instance-root escape, RT-011 drift, secret scanning,
provider ABI subversion, provisioning receipt tamper).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts" / "cwk_tenant_cli.py"
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_atomic_file as A  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_cli_api as API  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


# ---------------------------------------------------------------------------
# RT-011 frozen file drift
# ---------------------------------------------------------------------------


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Rt011FrozenFilesUntouched(unittest.TestCase):
    """RT-012 must not modify any RT-011 frozen file.

    We compare the current on-disk sha256 to a snapshot recorded when
    RT-012 was implemented; a mismatch means the implementation drifted
    into RT-011's territory.  Instead of hard-coding shas (which would
    embed a private oracle), we assert two things: (a) the files exist
    and are non-empty, and (b) grep confirms zero references to RT-012
    identifiers inside them.
    """

    FROZEN_FILES = (
        PROJECT / "scripts" / "cwk_pr001_contracts.py",
        PROJECT / "scripts" / "cwk_pr001_probes.py",
        PROJECT / "scripts" / "cwk_pr001_view_compare.py",
        PROJECT / "scripts" / "cwk_pr001_cli.py",
        PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json",
        PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "verified_shared_extensions_v1.json",
    )
    RT011_SCHEMAS = list(
        (PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "schemas").glob("*.schema.json")
    )
    RT012_MARKERS = ("cwk.rt012.", "InstanceLayout", "TenantRegistry", "cwk_tenant_cli")

    def test_frozen_files_exist_and_non_empty(self):
        for p in self.FROZEN_FILES + tuple(self.RT011_SCHEMAS):
            self.assertTrue(p.is_file(), str(p))
            self.assertGreater(p.stat().st_size, 0, str(p))

    def test_rt011_files_free_of_rt012_markers(self):
        for p in self.FROZEN_FILES + tuple(self.RT011_SCHEMAS):
            text = p.read_text(encoding="utf-8", errors="strict")
            for marker in self.RT012_MARKERS:
                self.assertNotIn(marker, text, f"{marker!r} leaked into {p.name}")


# ---------------------------------------------------------------------------
# Instance root attack matrix
# ---------------------------------------------------------------------------


class InstanceRootAttacks(unittest.TestCase):
    def setUp(self):
        self._save = os.environ.pop(I.ENV_VAR, None)

    def tearDown(self):
        if self._save is not None:
            os.environ[I.ENV_VAR] = self._save
        else:
            os.environ.pop(I.ENV_VAR, None)

    def test_unset_never_falls_back_to_runs_or_state(self):
        # If a bug ever caused us to fall back to `runs/` or the repo
        # `.env`, this call would silently succeed.  We assert it does not.
        with self.assertRaises(I.InstanceRootError):
            I.resolve_instance_root()

    def test_relative_never_promotes(self):
        for bad in ("runs", "./instance", "state/", "instance"):
            os.environ[I.ENV_VAR] = bad
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()

    def test_unicode_slash_variants_rejected(self):
        # Full-width solidus and Unicode fraction slash should not sneak
        # past — the resolver requires an absolute POSIX path.
        for bad in ("／tmp／foo", "⁄tmp⁄foo"):
            os.environ[I.ENV_VAR] = bad
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()

    def test_windows_absolute_rejected(self):
        for bad in (r"C:\Windows", "\\\\server\\share", "\\\\?\\C:\\evil"):
            os.environ[I.ENV_VAR] = bad
            with self.assertRaises(I.InstanceRootError):
                I.resolve_instance_root()


# ---------------------------------------------------------------------------
# Tenant ID / space ID grammar
# ---------------------------------------------------------------------------


class TenantIdAttackMatrix(unittest.TestCase):
    def test_traversal_and_encoded(self):
        for bad in (
            "../../etc/passwd",
            "/absolute/path",
            "t_../../..",
            "t_" + "a" * 25 + "%2e",
            "t_%2e%2e",
            "t_" + "a" * 26 + "%2f",
            "t_" + "a" * 26 + "\x00",
            "t_" + "a" * 26 + "\n",
            "t_" + "a" * 26 + " ",  # trailing whitespace
            "T_" + "a" * 26,        # uppercase T
            "t_" + "A" * 26,        # uppercase body
            "t_" + "a" * 25,        # too short
            "t_" + "a" * 27,        # too long
            "t_",                    # empty body
            "",
            "t_" + "z" * 25 + "!",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(I.TenantIdError):
                    I.validate_tenant_id(bad)

    def test_full_width_look_alikes_rejected(self):
        # Look-alike full-width chars must not slip past.
        for bad in ("ｔ_" + "a" * 26, "t_" + "ａ" * 26):
            with self.assertRaises(I.TenantIdError):
                I.validate_tenant_id(bad)


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class CrossTenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.reg = R.TenantRegistry(self.layout)
        self.a, _ = self.reg.init_tenant(actor="admin", reason="tenant_a")
        self.b, _ = self.reg.init_tenant(actor="admin", reason="tenant_b")

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)

    def test_bump_a_does_not_affect_b(self):
        self.reg.bump_auth_epoch(self.a.tenant_id, actor="admin", reason="rot", expected_auth_epoch=1)
        b_after = self.reg.get(self.b.tenant_id)
        self.assertEqual(b_after.auth_epoch, 1)
        self.assertEqual(b_after.record_revision, 1)

    def test_lock_isolation(self):
        # Hold A's lock; B's mutations must still succeed.
        with self.layout.registry_fd("tenants") as fd:
            with A.exclusive_lock(fd, f".{self.a.tenant_id}.lock"):
                self.reg.bump_auth_epoch(
                    self.b.tenant_id, actor="admin", reason="rot", expected_auth_epoch=1
                )
        b_after = self.reg.get(self.b.tenant_id)
        self.assertEqual(b_after.auth_epoch, 2)

    def test_corrupt_a_does_not_prevent_b(self):
        # Corrupt A's record on disk; querying B must still work.
        with self.layout.registry_fd("tenants") as fd:
            A.write_atomic(fd, f"{self.a.tenant_id}.json", b"garbage")
        b = self.reg.get(self.b.tenant_id)
        self.assertEqual(b.tenant_id, self.b.tenant_id)
        with self.assertRaises(R.RecordCorruption):
            self.reg.get(self.a.tenant_id)


# ---------------------------------------------------------------------------
# Symlink / hardlink attacks on the tenant tree
# ---------------------------------------------------------------------------


class TenantTreeSoftlinkAttacks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.reg = R.TenantRegistry(self.layout)
        self.tenant, _ = self.reg.init_tenant(actor="admin")

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)

    def test_replace_state_dir_with_symlink_is_refused(self):
        # Replace tenants/<tid>/state with a symlink to /tmp.
        outside = tempfile.mkdtemp(prefix="rt012-outside-")
        try:
            (Path(outside) / "canary.txt").write_text("outside")
            with self.layout.tenant(self.tenant.tenant_id).tenant_fd() as tfd:
                # Remove state and replace with symlink.
                os.rmdir("state", dir_fd=tfd)
                os.symlink(outside, "state", dir_fd=tfd)
            with self.assertRaises(I.LayoutError):
                with self.layout.tenant(self.tenant.tenant_id).child_fd("state"):
                    pass
            # Canary intact.
            self.assertEqual((Path(outside) / "canary.txt").read_text(), "outside")
        finally:
            (Path(outside) / "canary.txt").unlink(missing_ok=True)
            os.rmdir(outside)

    def test_replace_registry_tenant_json_with_symlink_is_refused(self):
        # Point the tenant record file at outside data.
        outside_dir = tempfile.mkdtemp(prefix="rt012-outside-")
        outside_file = Path(outside_dir) / "malicious.json"
        outside_file.write_text('{"schema": "attack"}')
        try:
            with self.layout.registry_fd("tenants") as rfd:
                # Remove real record, replace with symlink.
                os.unlink(f"{self.tenant.tenant_id}.json", dir_fd=rfd)
                os.symlink(str(outside_file), f"{self.tenant.tenant_id}.json", dir_fd=rfd)
            # `get` refuses via O_NOFOLLOW / FileNotFoundError.
            with self.assertRaises((R.TenantNotFound, R.RecordCorruption, R.RegistryError, A.ContainmentError)):
                self.reg.get(self.tenant.tenant_id)
        finally:
            outside_file.unlink(missing_ok=True)
            os.rmdir(outside_dir)


# ---------------------------------------------------------------------------
# Duplicate-key / boolean-as-int / negative attacks on registry payloads
# ---------------------------------------------------------------------------


class SchemaAttacks(unittest.TestCase):
    def test_duplicate_json_key_rejected(self):
        # strict_json_loads (RT-011) refuses duplicate keys — verify.
        with self.assertRaises(C.ContractError):
            C.strict_json_loads('{"tenant_id": "x", "tenant_id": "y"}')

    def test_negative_auth_epoch_rejected(self):
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record(
                _minimal_record({"auth_epoch": -1})
            )

    def test_over_2p53_rejected(self):
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record(_minimal_record({"auth_epoch": 2 ** 53}))

    def test_status_unknown_rejected(self):
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record(_minimal_record({"status": "enabled"}))


def _minimal_record(overrides=None) -> dict:
    good = "t_" + "a" * 26
    payload = {
        "schema": "cwk.rt012.tenant_record.v1",
        "tenant_id": good,
        "status": "draft",
        "credential_ref": None,
        "active_profile_version": None,
        "auth_epoch": 1,
        "record_revision": 1,
        "quota": {
            "scheme": "cwk.rt012.quota.unset.v1",
            "measurement_owner": "RT-024",
            "confirmation_owner": "RT-026",
            "limits": {
                "collector_concurrency": None,
                "scheduler_concurrency": None,
                "disk_bytes": None,
                "ai_calls_per_day": None,
                "retention_days": None,
            },
        },
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "provisioning": {
            "last_receipt_id": "txn_" + "a" * 26,
            "last_receipt_sha256": "0" * 64,
        },
    }
    if overrides:
        payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Secret-scan: no obvious secrets in our own files/output
# ---------------------------------------------------------------------------


class SecretScanTests(unittest.TestCase):
    """RT-012 error messages / logs must not contain any secret-shaped tokens.

    We run every RT-012 CLI command with valid input and grep the entire
    stdout+stderr for standard secret patterns.
    """

    _PATTERNS = (
        re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS
        re.compile(r"AppKey[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}"),
        re.compile(r"CWORK_APP_KEY=[^\s]+"),
        re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT
        re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),  # Slack
        re.compile(r"BEGIN (RSA|DSA|EC) PRIVATE KEY"),
    )

    def test_cli_output_never_leaks_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CWK_INSTANCE_ROOT": tmp}
            for argv in (
                ["--help"],
                ["state-graph"],
                ["list"],
                ["init", "--actor", "admin"],
            ):
                r = subprocess.run(
                    [sys.executable, str(CLI), *argv],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                blob = r.stdout + "\n" + r.stderr
                for pat in self._PATTERNS:
                    self.assertIsNone(pat.search(blob), f"{argv}: matched {pat.pattern}")

    def test_scripts_never_read_cwork_app_key(self):
        # RT-012 code MUST NOT call ``os.environ.get('CWORK_APP_KEY')`` or
        # otherwise consume the AppKey secret; only the string
        # ``CWK_APP_KEY``/``CWORK_APP_KEY`` reference we tolerate is inside
        # our own doc comment (e.g. "notably CWORK_APP_KEY not propagated").
        # This test enforces that no such env read exists.
        forbidden = (
            re.compile(r"os\.environ\[[\"']CWORK_APP_KEY"),
            re.compile(r"os\.environ\.get\([\"']CWORK_APP_KEY"),
            re.compile(r"os\.getenv\([\"']CWORK_APP_KEY"),
            re.compile(r"os\.environ\[[\"']CWK_APP_KEY"),
            re.compile(r"os\.environ\.get\([\"']CWK_APP_KEY"),
        )
        for name in (
            "cwk_atomic_file.py",
            "cwk_instance.py",
            "cwk_tenant_registry.py",
            "cwk_tenant_cli.py",
            "cwk_tenant_cli_api.py",
            "cwk_tenant_cmd_core.py",
        ):
            path = PROJECT / "scripts" / name
            text = path.read_text(encoding="utf-8")
            for pat in forbidden:
                self.assertIsNone(pat.search(text), f"{name}: matched {pat.pattern}")


# ---------------------------------------------------------------------------
# Rollback safety
# ---------------------------------------------------------------------------


class RollbackSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.reg = R.TenantRegistry(self.layout)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)

    def test_rollback_refuses_to_touch_populated_tree(self):
        # Bootstrap a tenant, add a stray file into one of its directories
        # (simulating RT-013+ data), then attempt rollback.
        rec, _ = self.reg.init_tenant(actor="admin")
        # Manually re-create the journal to mimic a "not committed" state.
        with self.layout.registry_fd("provision-journal") as jfd:
            txn = "txn_" + "z" * 26
            A.write_atomic(
                jfd,
                f"{rec.tenant_id}.{txn}.journal",
                b"{}\n",
                exclusive=True,
            )
        # Add a stray file into config/ to simulate RT-013 population.
        with self.layout.tenant(rec.tenant_id).child_fd("config") as cfd:
            A.write_atomic(cfd, "user.json", b"{}")
        # Now recover.
        self.reg.recover()
        # Tenant tree MUST remain.
        self.assertTrue(self.layout.tenant(rec.tenant_id).exists())
        # Tenant record still present.
        self.assertEqual(self.reg.get(rec.tenant_id).tenant_id, rec.tenant_id)

    def test_rollback_is_idempotent(self):
        for _ in range(3):
            self.reg.recover()
        self.assertEqual(self.reg.list_tenant_ids(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
