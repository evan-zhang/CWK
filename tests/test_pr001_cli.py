"""RT-011 (post-remediation): CLI black-box tests.

Covers Blocker #2 (CLI redaction), Blocker #6 (conformance), and the
stable-exit-code / no-traceback / no-absolute-path invariants.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts" / "cwk_pr001_cli.py"
FIX = PROJECT / "tests" / "fixtures" / "pr001"


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        cwd=str(PROJECT),
        env={**os.environ, "PYTHONPATH": str(PROJECT / "scripts")},
        check=False,
    )


class CliExitCodeTests(unittest.TestCase):
    def test_help_exit_zero(self):
        proc = _run("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("validate", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_no_command_exits_usage_4(self):
        proc = _run()
        self.assertEqual(proc.returncode, 4)
        self.assertNotIn("Traceback", proc.stderr)

    def test_unknown_subcommand_exit_2(self):
        proc = _run("does-not-exist")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_validate_missing_file_exits_io_5_no_abs_path(self):
        proc = _run("validate", "--file", "/nonexistent/pr001-fixture.json")
        self.assertEqual(proc.returncode, 5)
        self.assertNotIn("Traceback", proc.stderr)
        # Absolute path must NOT be echoed.
        self.assertNotIn("/nonexistent/", proc.stderr)

    def test_validate_duplicate_json_key_exits_contract_2(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", dir=str(FIX)) as fh:
            fh.write('{"a": 1, "a": 2}')
            path = fh.name
        try:
            proc = _run("validate", "--file", path)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("duplicate", proc.stderr.lower())
        finally:
            os.unlink(path)

    def test_validate_bad_json_exits_contract_2(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", dir=str(FIX)) as fh:
            fh.write("{not-json")
            path = fh.name
        try:
            proc = _run("validate", "--file", path)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)
        finally:
            os.unlink(path)

    def test_validate_malicious_fixtures(self):
        for name in (
            "malicious_query_request_nested_credential.json",
            "malicious_query_request_slug_selector.json",
            "malicious_profile_rolled_back_state.json",
            "malicious_profile_sha_recompute_drift.json",
            "malicious_route_decision_slug_and_illegal_disposition.json",
            "malicious_canonical_nested_tenant_id.json",
        ):
            proc = _run("validate", "--file", str(FIX / name))
            self.assertEqual(proc.returncode, 2, msg=(name, proc.stderr))
            self.assertNotIn("Traceback", proc.stderr)

    def test_probe_run_default_all_conservative_exit_zero(self):
        proc = _run("probe", "run")
        self.assertEqual(proc.returncode, 0)
        payloads = json.loads(proc.stdout)
        self.assertEqual(len(payloads), 9)
        for p in payloads:
            self.assertEqual(p["result"], "conservative_unknown")
            self.assertIsNone(p["receipt"])

    def test_probe_aggregate_default_exit_3(self):
        # Run then aggregate — with no receipts everything is conservative.
        run_proc = _run("probe", "run")
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            fh.write(run_proc.stdout)
            path = fh.name
        try:
            agg_proc = _run("probe", "aggregate", path)
            self.assertEqual(agg_proc.returncode, 3)
            summary = json.loads(agg_proc.stdout)
            self.assertFalse(summary["all_verified"])
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["missing_probe_ids"], [])
        finally:
            os.unlink(path)

    def test_probe_aggregate_incomplete_still_exit_3(self):
        # Aggregate over a strict subset of probe results.
        run_proc = _run("probe", "run")
        payloads = json.loads(run_proc.stdout)
        subset = payloads[:3]
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(subset, fh)
            path = fh.name
        try:
            agg_proc = _run("probe", "aggregate", path)
            self.assertEqual(agg_proc.returncode, 3)
            summary = json.loads(agg_proc.stdout)
            self.assertFalse(summary["all_verified"])
            self.assertFalse(summary["complete"])
            self.assertGreater(len(summary["missing_probe_ids"]), 0)
        finally:
            os.unlink(path)

    def test_probe_aggregate_rejects_malicious_forged_verified(self):
        proc = _run("probe", "aggregate", str(FIX / "malicious_probe_forged_verified.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_canonicalize_matches_library(self):
        payload = {"z": 1, "a": {"y": 2, "b": [3, 2, 1]}}
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(payload, fh)
            path = fh.name
        try:
            proc = _run("canonicalize", "--file", path)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip().encode("utf-8"), b'{"a":{"b":[3,2,1],"y":2},"z":1}')
        finally:
            os.unlink(path)

    def test_conformance_frozen_defaults_match_themselves(self):
        proc = _run(
            "conformance",
            "--file",
            str(PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json"),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_conformance_detects_loopback_allowed_addition(self):
        proc = _run("conformance", "--file", str(FIX / "malicious_security_defaults_loopback_allowed.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_conformance_detects_break_glass_alternate(self):
        proc = _run("conformance", "--file", str(FIX / "malicious_security_defaults_break_glass_alt.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_conformance_detects_extra_key(self):
        # Add an unexpected top-level key.
        with (PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json").open() as fh:
            defaults = json.load(fh)
        defaults["extra_unexpected_key"] = "surprise"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(defaults, fh)
            path = fh.name
        try:
            proc = _run("conformance", "--file", path)
            self.assertEqual(proc.returncode, 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
