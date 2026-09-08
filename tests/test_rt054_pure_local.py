"""Regression tests for the RT-054 local-only acceptance harness."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import rt054_pure_local  # noqa: E402


class RT054PureLocalTests(unittest.TestCase):
    def test_whitelist_rebuild_drops_nas_and_all_secret_like_parent_variables(self) -> None:
        parent = {
            "PATH": "/safe/bin",
            "CWK_NAS_KB_HOST": "canary-host-not-used",
            "CWK_NAS_KB_PASSWORD": "canary-password-not-used",
            "CWORK_APP_KEY": "canary-app-key-not-used",
            "PROJECT_TOKEN": "canary-token-not-used",
            "SERVICE_SECRET": "canary-secret-not-used",
            "ANOTHER_PRIVATE_KEY": "canary-key-not-used",
        }
        child = rt054_pure_local.pure_local_env(parent)
        self.assertEqual(child["CWK_RT054_PURE_LOCAL"], "1")
        self.assertEqual(child["PATH"], "/safe/bin")
        self.assertFalse(set(child) & (set(parent) - {"PATH"}))

    def test_pure_local_gate_skips_nas_smoke_before_backend_can_be_instantiated(self) -> None:
        import test_kb_storage

        old = os.environ.get("CWK_RT054_PURE_LOCAL")
        os.environ["CWK_RT054_PURE_LOCAL"] = "1"
        try:
            # Reload evaluates the decorator under the forced pure-local gate.
            import importlib
            module = importlib.reload(test_kb_storage)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.NasSmokeTests)
            with mock.patch.object(
                module.storage.FileStationBackend,
                "from_env",
                side_effect=AssertionError("NAS backend must not be instantiated"),
            ) as trap:
                result = unittest.TestResult()
                suite.run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(len(result.skipped), 1)
            self.assertEqual(result.failures, [])
            self.assertEqual(result.errors, [])
            trap.assert_not_called()
        finally:
            if old is None:
                os.environ.pop("CWK_RT054_PURE_LOCAL", None)
            else:
                os.environ["CWK_RT054_PURE_LOCAL"] = old
            importlib.reload(test_kb_storage)

    def test_canary_parent_still_produces_a_skipped_nas_smoke_in_child(self) -> None:
        parent = dict(os.environ)
        parent.update(
            {
                "CWK_NAS_KB_HOST": "canary-host-not-used",
                "CWK_NAS_KB_PASSWORD": "canary-password-not-used",
                "CWORK_APP_KEY": "canary-app-key-not-used",
                "PROJECT_TOKEN": "canary-token-not-used",
            }
        )
        completed = subprocess.run(
            [sys.executable, "scripts/rt054_pure_local.py", "test_kb_storage"],
            cwd=PROJECT,
            env=parent,
            text=True,
            capture_output=True,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("OK (skipped=1)", output)
        self.assertIn("RT-054 pure-local gate", output)
        self.assertNotIn("canary-host-not-used", output)
        self.assertNotIn("canary-password-not-used", output)
