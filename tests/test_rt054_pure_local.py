"""Regression tests for the RT-054 fixed local-only acceptance harness."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
LOCAL_TMP_PARENT = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))
import rt054_pure_local  # noqa: E402


class RT054PureLocalTests(unittest.TestCase):
    def run_shell_path_validator(self, helper: Path, candidate: Path, records: dict[Path, str]) -> subprocess.CompletedProcess[str]:
        """Run the shell validator against an explicit, default-deny lstat map."""
        cases = "\n".join(
            f"    {shlex.quote(str(path))}) printf '%s\\n' {shlex.quote(record)} ;;"
            for path, record in sorted(records.items(), key=lambda item: str(item[0]))
        )
        return subprocess.run(
            ["/bin/sh", "-c", f'''\
. "$1"
rt054_stat_record() {{
    case "$1" in
{cases}
        *) return 1 ;;
    esac
}}
rt054_validate_trust_chain "$2"
''', "sh", str(helper), str(candidate)],
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def trusted_directories(path: Path) -> dict[Path, str]:
        records = {Path("/"): "0|755|directory"}
        current = Path("/")
        for part in path.parts[1:-1]:
            current /= part
            records[current] = "0|755|directory"
        return records

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
        owned = os.environ.get("CWK_RT054_PURE_LOCAL") != "1"
        root = rt054_pure_local.controlled_temp_root() if owned else Path(os.environ["TMPDIR"])
        try:
            self.assertIn(root.parent, (Path("/private/tmp"), Path("/tmp")))
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertTrue((root / ".env").is_file())
            if owned:
                self.assertEqual((root / ".env").read_text(encoding="utf-8"), "CANARY_DOTENV_MUST_NOT_BE_READ=1\n")
        finally:
            if owned:
                import shutil
                shutil.rmtree(root)

    def test_main_passes_absolute_isolated_fixed_child_contract(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        root = Path("/tmp/cwk-rt054-fixed")
        with mock.patch.object(rt054_pure_local, "controlled_temp_root", return_value=root), mock.patch.object(rt054_pure_local.sys, "executable", "/trusted/python"), mock.patch.object(rt054_pure_local.sys, "flags", type("Flags", (), {"isolated": 1, "no_site": 1})()), mock.patch.object(rt054_pure_local.shutil, "rmtree") as cleanup, mock.patch.object(rt054_pure_local.subprocess, "run", return_value=completed) as run:
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
        self.assertIn("sys.addaudithook(audit)", bootstrap)
        self.assertIn("builtins.open = guarded_builtin_open", bootstrap)
        self.assertIn("io.open = guarded_io_open", bootstrap)
        self.assertIn("Path.open = guarded_path_open", bootstrap)
        self.assertIn("os.open = guarded_os_open", bootstrap)
        self.assertIn("socket.socket.connect = blocked_socket", bootstrap)
        self.assertIn("socket.create_connection = blocked_socket", bootstrap)
        self.assertIn("socket.socket.connect_ex = blocked_socket_connect_ex", bootstrap)
        self.assertIn("FileStationBackend.from_env", bootstrap)
        self.assertIn("event == \"open\"", bootstrap)
        self.assertIn("RT054_GUARD interpreter", bootstrap)
        self.assertIn("RT054_PURE_LOCAL_BOOTSTRAP=1", bootstrap)

    def test_shell_path_validator_accepts_direct_and_linux_mode_777_symlink_fixtures(self) -> None:
        import tempfile

        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        with tempfile.TemporaryDirectory(prefix="rt054-path-check-", dir=LOCAL_TMP_PARENT) as tmp:
            root = Path(tmp)
            target = root / "python3"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            linked_dir = root / "trusted"
            linked_dir.mkdir()
            linked_target = linked_dir / "python3"
            linked_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            linked_target.chmod(0o755)
            link = root / "python-link"
            link.symlink_to("trusted/python3")
            direct_records = self.trusted_directories(target)
            direct_records[target] = "0|755|regular file"
            link_records = self.trusted_directories(link)
            link_records[linked_dir] = "0|755|directory"
            # Linux lstat mode is normally 0777 for a symlink.  It must not
            # substitute for checks of the link owner or target chain.
            link_records[link] = "0|777|symbolic link"
            link_records[linked_target] = "0|755|regular file"
            for candidate, records in ((target, direct_records), (link, link_records)):
                result = self.run_shell_path_validator(helper, candidate, records)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_path_validator_rejects_an_operator_owned_tree(self) -> None:
        import tempfile

        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        with tempfile.TemporaryDirectory(prefix="rt054-path-check-", dir=LOCAL_TMP_PARENT) as tmp:
            target = Path(tmp) / "python3"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            result = subprocess.run(
                ["/bin/sh", "-c", '. "$1"; rt054_validate_trust_chain "$2"', "sh", str(helper), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_shell_path_validator_rejects_nonroot_link_and_writable_parent_or_target(self) -> None:
        import tempfile

        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        with tempfile.TemporaryDirectory(prefix="rt054-path-check-", dir=LOCAL_TMP_PARENT) as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            target = trusted / "python3"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            link = root / "python-link"
            link.symlink_to("trusted/python3")
            base = self.trusted_directories(link)
            base[trusted] = "0|755|directory"
            base[link] = "0|777|symbolic link"
            base[target] = "0|755|regular file"
            for changed_path, record in (
                (link, "501|777|symbolic link"),
                (trusted, "0|775|directory"),
                (target, "0|775|regular file"),
            ):
                records = dict(base)
                records[changed_path] = record
                result = self.run_shell_path_validator(helper, link, records)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_shell_path_validator_rejects_symlink_cycle_and_depth(self) -> None:
        import tempfile

        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        with tempfile.TemporaryDirectory(prefix="rt054-path-check-", dir=LOCAL_TMP_PARENT) as tmp:
            root = Path(tmp)
            cycle_a = root / "cycle-a"
            cycle_b = root / "cycle-b"
            cycle_a.symlink_to("cycle-b")
            cycle_b.symlink_to("cycle-a")
            chain = [root / f"hop-{index}" for index in range(17)]
            for index, link in enumerate(chain):
                link.symlink_to(chain[index + 1].name if index + 1 < len(chain) else "target")
            (root / "target").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (root / "target").chmod(0o755)
            for candidate in (cycle_a, chain[0]):
                records = self.trusted_directories(candidate)
                for link in (cycle_a, cycle_b, *chain):
                    records[link] = "0|777|symbolic link"
                records[root / "target"] = "0|755|regular file"
                result = self.run_shell_path_validator(helper, candidate, records)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_shell_path_validator_accepts_only_the_fixed_candidate_spelling(self) -> None:
        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        result = subprocess.run(
            ["/bin/sh", "-c", '. "$1"; rt054_validate_candidate "$2"', "sh", str(helper), "/opt/homebrew/bin/python3"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_shell_path_validator_accepts_the_current_root_owned_system_python(self) -> None:
        helper = PROJECT / "scripts" / "rt054_pure_local_path_validation.sh"
        result = subprocess.run(
            ["/bin/sh", "-c", '. "$1"; rt054_validate_candidate /usr/bin/python3', "sh", str(helper)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_makefile_ignores_direct_and_makeflags_injection_in_dry_run(self) -> None:
        canary = "RT054_CANARY_MUST_NOT_APPEAR"
        env = dict(os.environ, MAKEFLAGS=f"SHELL={canary} CURDIR={canary} RT054_PYTHON={canary} PATH={canary}")
        result = subprocess.run(
            ["/usr/bin/make", "-n", "rt054-pure-local", f"SHELL={canary}", f"CURDIR={canary}", f"RT054_PYTHON={canary}", f"PATH={canary}"],
            cwd=PROJECT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)
        self.assertIn("/bin/sh -eu -c", result.stdout)
        self.assertIn("rt054_pure_local_launcher.sh", result.stdout)

    def test_dotenv_open_routes_are_exactly_blocked_inside_pure_local_child(self) -> None:
        if os.environ.get("CWK_RT054_PURE_LOCAL") != "1":
            self.skipTest("requires the fixed pure-local child")
        import builtins
        import io

        guard = builtins._rt054_pure_local_guard
        counter = guard["counts"]
        dotenv = Path(os.environ["TMPDIR"]) / ".env"
        routes = (
            lambda: builtins.open(dotenv),
            lambda: io.open(dotenv),
            lambda: dotenv.open(),
            lambda: os.open(dotenv, os.O_RDONLY),
        )
        for route in routes:
            before = counter["dotenv"]
            with self.assertRaisesRegex(AssertionError, r"^RT054_GUARD dotenv$"):
                route()
            self.assertEqual(counter["dotenv"], before + 1)
        counter["dotenv"] = 0

    def test_socket_connect_ex_is_blocked_inside_pure_local_child(self) -> None:
        if os.environ.get("CWK_RT054_PURE_LOCAL") != "1":
            self.skipTest("requires the fixed pure-local child")
        import builtins
        import socket

        counter = builtins._rt054_pure_local_guard["counts"]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            before = counter["socket"]
            with self.assertRaisesRegex(AssertionError, r"^RT054_GUARD external socket$"):
                sock.connect_ex(("127.0.0.1", 9))
            self.assertEqual(counter["socket"], before + 1)
        finally:
            sock.close()
        counter["socket"] = 0


if __name__ == "__main__":
    unittest.main()
