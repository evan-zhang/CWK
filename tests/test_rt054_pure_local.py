"""Regression tests for the RT-054 fixed local-only acceptance harness."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import rt054_pure_local  # noqa: E402


class RT054PureLocalTests(unittest.TestCase):
    def test_child_environment_is_a_fixed_constant_not_a_parent_whitelist(self) -> None:
        root = Path("/private/tmp/cwk-rt054-test-root")
        parent = {
            "CWK_NAS_KB_HOST": "canary-host-not-used", "CWK_NAS_KB_USER": "canary-user-not-used",
            "CWK_NAS_KB_PASSWORD": "canary-password-not-used", "CWK_NAS_KB_SHARE": "canary-share-not-used",
            "CWK_NAS_KB_CERT_SHA256": "canary-cert-not-used", "CWORK_APP_KEY": "canary-appkey-not-used",
            "PROJECT_TOKEN": "canary-token-not-used", "SERVICE_SECRET": "canary-secret-not-used",
            "ANOTHER_PRIVATE_KEY": "canary-privatekey-not-used", "PATH": "canary-path-not-used",
            "TMP": "canary-tmp-not-used", "TEMP": "canary-temp-not-used",
            "PYTHONPATH": "canary-pythonpath-not-used", "PYTHONHOME": "canary-pythonhome-not-used",
            "PYTHONUSERBASE": "canary-usersite-not-used", "SITECUSTOMIZE": "canary-sitecustomize-not-used",
        }
        with mock.patch.dict(os.environ, parent, clear=True):
            child = rt054_pure_local.pure_local_env(root)
        self.assertEqual(child, {"CWK_RT054_PURE_LOCAL": "1", "HOME": str(root), "LANG": "C", "LC_ALL": "C", "TMPDIR": str(root)})
        self.assertFalse(set(child) & set(parent))

    def test_runner_rejects_all_user_test_selection(self) -> None:
        for argv in (("test_kb_storage",), ("--help",), ("",)):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(SystemExit, "accepts no test selection"):
                    rt054_pure_local.reject_arguments(argv)

    def test_pure_local_flag_skips_nas_smoke_before_backend_construction(self) -> None:
        import importlib
        import test_kb_storage

        with mock.patch.dict(os.environ, {"CWK_RT054_PURE_LOCAL": "1"}, clear=True):
            module = importlib.reload(test_kb_storage)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.NasSmokeTests)
            with mock.patch.object(module.storage.FileStationBackend, "from_env", side_effect=AssertionError("NAS backend must not be instantiated")) as trap:
                result = unittest.TestResult()
                suite.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.errors, [])
        trap.assert_not_called()
        importlib.reload(test_kb_storage)

    def test_temp_root_is_private_local_and_contains_only_a_guard_canary(self) -> None:
        root = rt054_pure_local.controlled_temp_root()
        try:
            self.assertIn(root.parent, (Path("/private/tmp"), Path("/tmp")))
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "CANARY_DOTENV_MUST_NOT_BE_READ=1\n")
        finally:
            import shutil
            shutil.rmtree(root)

    def test_main_passes_absolute_isolated_fixed_child_contract(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        root = Path("/tmp/cwk-rt054-fixed")
        with mock.patch.object(rt054_pure_local, "controlled_temp_root", return_value=root), mock.patch.object(rt054_pure_local, "trusted_interpreter", return_value="/trusted/python"), mock.patch.object(rt054_pure_local.shutil, "rmtree") as cleanup, mock.patch.object(rt054_pure_local.subprocess, "run", return_value=completed) as run:
            self.assertEqual(rt054_pure_local.main(()), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/trusted/python", "-I", "-S", "-c"])
        self.assertEqual(command[-4:], list(rt054_pure_local.TESTS))
        self.assertEqual(run.call_args.kwargs["cwd"], root)
        child_env = run.call_args.kwargs["env"]
        self.assertNotIn("PATH", child_env)
        self.assertNotIn("PYTHONPATH", child_env)
        self.assertEqual(child_env["CWK_RT054_PURE_LOCAL"], "1")
        cleanup.assert_called_once_with(root)

    def test_bootstrap_contract_has_no_dotenv_or_network_escape(self) -> None:
        bootstrap = rt054_pure_local.BOOTSTRAP
        self.assertIn("socket.socket.connect = blocked_socket", bootstrap)
        self.assertIn("socket.create_connection = blocked_socket", bootstrap)
        self.assertIn("FileStationBackend.from_env", bootstrap)
        self.assertIn("candidate.name == \".env\"", bootstrap)
        self.assertIn("RT054_PURE_LOCAL_BOOTSTRAP=1", bootstrap)


if __name__ == "__main__":
    unittest.main()
