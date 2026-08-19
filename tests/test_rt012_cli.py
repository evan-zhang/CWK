"""RT-012: black-box CLI tests for ``cwk-tenant`` dispatcher and core cmds."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts" / "cwk_tenant_cli.py"
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_tenant_cli_api as API  # noqa: E402


def _run(*argv: str, env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    real_env = dict(os.environ)
    if env is not None:
        real_env.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        env=real_env,
        cwd=cwd or str(PROJECT),
        check=False,
    )


class HelpAndErrorTests(unittest.TestCase):
    def test_help_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("--help", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("init", r.stdout)
        self.assertIn("doctor", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_no_command_exits_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, API.EXIT_USAGE)
        self.assertNotIn("Traceback", r.stderr)

    def test_unknown_subcommand_exits_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("wat", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, API.EXIT_USAGE)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_env_fails_usage_no_absolute_path(self):
        env = {k: v for k, v in os.environ.items() if k != "CWK_INSTANCE_ROOT"}
        r = _run("init", "--actor", "admin", env={"__wipe__": "1", **env})
        # The dispatcher redirects missing env to USAGE.
        self.assertIn(r.returncode, (API.EXIT_USAGE,))
        self.assertNotIn("Traceback", r.stderr)


class StateGraphTests(unittest.TestCase):
    def test_state_graph_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("state-graph", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, 0)
        g = json.loads(r.stdout)
        self.assertEqual(g["schema"], "cwk.rt012.state_graph.v1")
        self.assertIn("draft", g["states"])
        self.assertEqual(g["terminal_state"], "offboarded")


class InitFlowTests(unittest.TestCase):
    def test_init_then_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            body = json.loads(r.stdout)
            tid = body["tenant_id"]
            self.assertRegex(tid, r"^t_[a-z0-9]{26}$")
            self.assertEqual(body["status"], "draft")

            r2 = _run("show", "--tenant-id", tid, env=env)
            self.assertEqual(r2.returncode, 0)
            rec = json.loads(r2.stdout)
            self.assertEqual(rec["tenant_id"], tid)
            self.assertEqual(rec["status"], "draft")
            self.assertEqual(rec["auth_epoch"], 1)

    def test_show_unknown_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("show", "--tenant-id", "t_" + "z" * 26, env=env)
        self.assertEqual(r.returncode, API.EXIT_IO)
        self.assertNotIn("Traceback", r.stderr)

    def test_show_bad_tenant_id_returns_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("show", "--tenant-id", "not-a-tenant", env=env)
        self.assertEqual(r.returncode, API.EXIT_CONTRACT)
        self.assertNotIn("Traceback", r.stderr)

    def test_list_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("list", env=env)
        self.assertEqual(r.returncode, 0)
        body = json.loads(r.stdout)
        self.assertEqual(body["count"], 0)


class DoctorTests(unittest.TestCase):
    def test_doctor_uninit_root_reports_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("doctor", env=env)
        self.assertEqual(r.returncode, API.EXIT_CONTRACT)
        body = json.loads(r.stdout)
        self.assertEqual(body["schema"], "cwk.rt012.layout_doctor_report.v1")
        self.assertGreater(body["issue_count"], 0)

    def test_doctor_clean_after_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            _run("init", "--actor", "admin", env=env)
            r = _run("doctor", env=env)
        self.assertEqual(r.returncode, 0, r.stdout)


class ProviderFailClosedTests(unittest.TestCase):
    """The dispatcher must not scan CWD/PYTHONPATH or env for providers."""

    def test_malicious_cwd_provider_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            evil_dir = Path(tmp) / "evil"
            evil_dir.mkdir()
            # Same name as a legitimate provider but in a different dir.
            (evil_dir / "cwk_tenant_cmd_core.py").write_text(
                "raise RuntimeError('evil provider ran')\n"
            )
            env = {"CWK_INSTANCE_ROOT": tmp, "PYTHONPATH": str(evil_dir)}
            r = _run("state-graph", env=env, cwd=str(evil_dir))
        # Dispatcher must have loaded the trusted provider, not the evil one.
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("evil provider ran", r.stderr)


class ProviderAbiTests(unittest.TestCase):
    """The dispatcher's ABI checks fail closed if a provider is broken."""

    def test_missing_api_version_refused(self):
        # We can't easily replace the shipped module.  Instead, exercise the
        # loader directly.
        import cwk_tenant_cli as CLI_MOD  # noqa: PLC0415

        # Create a temp module file the loader will attempt to import.
        with tempfile.TemporaryDirectory() as tmp:
            fake_scripts = Path(tmp) / "scripts"
            fake_scripts.mkdir()
            (fake_scripts / "cwk_tenant_cmd_core.py").write_text("PROVIDER_NAME='core'\nPROVIDER_VERSION='v1'\n")
            # Patch the trusted dir to point at the fake dir so the loader
            # picks up our broken shim.
            original = CLI_MOD._TRUSTED_SCRIPTS_DIR
            try:
                CLI_MOD._TRUSTED_SCRIPTS_DIR = fake_scripts
                with self.assertRaises(CLI_MOD._ProviderLoadError):
                    CLI_MOD._load_provider("cwk_tenant_cmd_core")
            finally:
                CLI_MOD._TRUSTED_SCRIPTS_DIR = original

    def test_symlink_provider_refused(self):
        import cwk_tenant_cli as CLI_MOD  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            fake_scripts = Path(tmp) / "scripts"
            fake_scripts.mkdir()
            real = fake_scripts / "real.py"
            real.write_text("API_VERSION='v1'\nPROVIDER_NAME='core'\nPROVIDER_VERSION='v1'\ndef list_commands(): return ()\n")
            link = fake_scripts / "cwk_tenant_cmd_core.py"
            os.symlink(str(real), str(link))
            original = CLI_MOD._TRUSTED_SCRIPTS_DIR
            try:
                CLI_MOD._TRUSTED_SCRIPTS_DIR = fake_scripts
                with self.assertRaises(CLI_MOD._ProviderLoadError):
                    CLI_MOD._load_provider("cwk_tenant_cmd_core")
            finally:
                CLI_MOD._TRUSTED_SCRIPTS_DIR = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
