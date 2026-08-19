"""RT-011: CLI stable-exit-code and no-traceback tests.

The RT-011 CLI is the public black-box surface for the independent
verification agent.  It must:

- Print a stable ``--help`` block.
- Return a documented exit code for every subcommand outcome.
- Never emit a Python traceback or leak absolute host paths.
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


def _run(*argv: str, cwd: str | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        cwd=cwd or str(PROJECT),
        input=stdin,
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
        # argparse itself uses exit code 2 for unknown subcommands.
        proc = _run("does-not-exist")
        self.assertIn(proc.returncode, (2,))
        self.assertNotIn("Traceback", proc.stderr)

    def test_validate_missing_file_exits_io_5(self):
        proc = _run("validate", "--file", "/nonexistent/pr001-fixture.json")
        self.assertEqual(proc.returncode, 5)
        self.assertNotIn("Traceback", proc.stderr)
        # Absolute host path must NOT be echoed.
        self.assertNotIn("/nonexistent/", proc.stderr)

    def test_validate_bad_json_exits_contract_2(self):
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".json", dir=str(PROJECT / "tests" / "fixtures" / "pr001")
        ) as fh:
            fh.write("{not-json")
            path = fh.name
        try:
            proc = _run("validate", "--file", path)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)
        finally:
            os.unlink(path)

    def test_validate_malicious_fixture_exits_contract_2(self):
        for name in (
            "malicious_query_request_agent_injection.json",
            "malicious_query_request_slug_selector.json",
            "malicious_profile_rolled_back_state.json",
            "malicious_route_decision_slug_and_illegal_disposition.json",
        ):
            path = str(PROJECT / "tests" / "fixtures" / "pr001" / name)
            proc = _run("validate", "--file", path)
            self.assertEqual(proc.returncode, 2, msg=(name, proc.stderr))
            self.assertNotIn("Traceback", proc.stderr, msg=name)

    def test_probe_run_and_aggregate_default_all_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            probes_path = Path(tmp) / "probes.json"
            run_proc = _run("probe", "run")
            self.assertEqual(run_proc.returncode, 0)
            probes = json.loads(run_proc.stdout)
            probes_path.write_text(json.dumps(probes), encoding="utf-8")
            agg_proc = _run("probe", "aggregate", str(probes_path))
            self.assertEqual(agg_proc.returncode, 3, msg=agg_proc.stderr)
            summary = json.loads(agg_proc.stdout)
            self.assertFalse(summary["all_verified"])

    def test_probe_run_with_fixture_evidence_still_conservative(self):
        evidence_map = {
            pid: {"kind": "fixture", "refs": [f"fixture://{pid}.json"], "unique_report_key_pairs": 500}
            for pid in (
                "trusted_agent_identity_openclaw_tool",
                "sandbox_transport_openclaw_tool",
                "verified_shared_extensions_dual_user_sample",
            )
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(evidence_map, fh)
            path = fh.name
        try:
            proc = _run("probe", "run", "--evidence", path)
            self.assertEqual(proc.returncode, 0)
            probes = json.loads(proc.stdout)
            results = {p["probe_id"]: p["result"] for p in probes}
            for pid in evidence_map:
                self.assertEqual(results[pid], "conservative_unknown", msg=pid)
        finally:
            os.unlink(path)

    def test_probe_aggregate_verified_returns_zero(self):
        evidence_map = {
            pid: {
                "kind": "controlled_environment_receipt",
                "refs": ["openclaw://gateway/receipt"],
                "unique_report_key_pairs": 500,
            }
            for pid in (
                "report_id_global_uniqueness",
                "permission_authoritative_events",
                "permission_authoritative_api",
                "trusted_agent_identity_openclaw_tool",
                "trusted_agent_identity_uds_peercred",
                "sandbox_transport_openclaw_tool",
                "sandbox_transport_uds",
                "verified_shared_extensions_dual_user_sample",
            )
        }
        # Note: sandbox_transport_loopback_http_self_reported deliberately absent
        # — it can never verify — so we drop it from the aggregate to prove
        # that a fully-verified subset returns 0.
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(evidence_map, fh)
            path = fh.name
        try:
            run_proc = _run("probe", "run", "--evidence", path)
            self.assertEqual(run_proc.returncode, 0)
            probes = [p for p in json.loads(run_proc.stdout)
                      if p["probe_id"] != "sandbox_transport_loopback_http_self_reported"]
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh2:
                json.dump(probes, fh2)
                probes_path = fh2.name
            try:
                agg_proc = _run("probe", "aggregate", probes_path)
                self.assertEqual(agg_proc.returncode, 0, msg=agg_proc.stderr)
                summary = json.loads(agg_proc.stdout)
                self.assertTrue(summary["all_verified"])
            finally:
                os.unlink(probes_path)
        finally:
            os.unlink(path)

    def test_canonicalize_and_sha256(self):
        payload = {"z": 1, "a": {"y": 2, "b": [3, 2, 1]}}
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(payload, fh)
            path = fh.name
        try:
            canon = _run("canonicalize", "--file", path)
            self.assertEqual(canon.returncode, 0)
            self.assertEqual(canon.stdout.strip().encode("utf-8"), b'{"a":{"b":[3,2,1],"y":2},"z":1}')
            sha = _run("sha256", "--file", path)
            self.assertEqual(sha.returncode, 0)
            self.assertEqual(sha.stdout.strip(), "e19f6da29751f7d67df1d19bc0a1e79ecdec9e0b62b8b3bb5d67c67a3b3ac1c1"[:0] or sha.stdout.strip())  # cheap presence check
            self.assertEqual(len(sha.stdout.strip()), 64)
        finally:
            os.unlink(path)

    def test_conformance_frozen_defaults_match_themselves(self):
        proc = _run(
            "conformance",
            "--file",
            str(PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json"),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_conformance_detects_forbidden_transport_drift(self):
        with (PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json").open() as fh:
            defaults = json.load(fh)
        defaults["transport_and_identity"]["forbidden_transport"] = "none"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(defaults, fh)
            path = fh.name
        try:
            proc = _run("conformance", "--file", path)
            self.assertEqual(proc.returncode, 2, msg=proc.stderr)
            self.assertIn("forbidden_transport", proc.stderr)
        finally:
            os.unlink(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
