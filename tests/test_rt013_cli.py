"""RT-013: black-box CLI tests for the ``cwk_tenant_cmd_binding`` provider."""

from __future__ import annotations

import json
import os
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
import cwk_tenant_cli_api as API  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    real_env = dict(os.environ)
    if env is not None:
        real_env.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        env=real_env,
        cwd=str(PROJECT),
        check=False,
    )


def _promote(tmp: str, tenant_id: str, new_status: str) -> None:
    os.environ[I.ENV_VAR] = tmp
    layout = I.InstanceLayout.open()
    reg = R.TenantRegistry(layout)
    rec = reg.get(tenant_id)
    payload = dict(rec.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        A.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=rec.on_disk_sha256)


class SlotTests(unittest.TestCase):
    def test_binding_provider_registered(self):
        import cwk_tenant_cli as CLI_MOD  # noqa: PLC0415

        self.assertIn("cwk_tenant_cmd_binding", CLI_MOD.FROZEN_PROVIDER_SLOTS)
        # Core still present and first.
        self.assertEqual(CLI_MOD.FROZEN_PROVIDER_SLOTS[0], "cwk_tenant_cmd_core")

    def test_help_lists_binding_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("--help", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in (
            "bind-agent",
            "revoke-agent",
            "list-bindings",
            "set-credential",
            "rotate-credential",
            "rotate-binding-secret",
            "doctor:binding",
        ):
            self.assertIn(cmd, r.stdout)


class BindCmdTests(unittest.TestCase):
    def test_bind_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            tid = json.loads(r.stdout)["tenant_id"]

            r = _run("bind-agent", "--tenant-id", tid, "--agent-id", "alice", "--actor", "admin", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            body = json.loads(r.stdout)
            self.assertEqual(body["schema"], "cwk.rt013.binding_output.v1")
            self.assertEqual(body["status"], "active")
            self.assertRegex(body["agent_id_hash"], r"^[0-9a-f]{64}$")

            # Raw id must NOT appear anywhere in stdout / stderr.
            self.assertNotIn("alice", r.stdout)
            self.assertNotIn("alice", r.stderr)

    def test_bind_bad_tenant_id_exits_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("bind-agent", "--tenant-id", "not-a-tenant", "--agent-id", "alice",
                     "--actor", "admin", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, API.EXIT_CONTRACT, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bind_unknown_tenant_exits_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("bind-agent", "--tenant-id", "t_" + "z" * 26, "--agent-id", "alice",
                     "--actor", "admin", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, API.EXIT_IO, r.stderr)

    def test_duplicate_bind_exits_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            tid = json.loads(r.stdout)["tenant_id"]
            _run("bind-agent", "--tenant-id", tid, "--agent-id", "alice", "--actor", "admin", env=env)
            r = _run("bind-agent", "--tenant-id", tid, "--agent-id", "alice", "--actor", "admin", env=env)
        self.assertEqual(r.returncode, API.EXIT_CONFLICT, r.stderr)


class RevokeCmdTests(unittest.TestCase):
    def test_revoke_after_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            tid = json.loads(r.stdout)["tenant_id"]
            _run("bind-agent", "--tenant-id", tid, "--agent-id", "alice", "--actor", "admin", env=env)
            r = _run("revoke-agent", "--agent-id", "alice", "--actor", "admin", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["status"], "revoked")


class ListBindingsCmdTests(unittest.TestCase):
    def test_list_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("list-bindings", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["count"], 0)


class SetCredentialCmdTests(unittest.TestCase):
    def test_set_credential_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            tid = json.loads(r.stdout)["tenant_id"]
            r = _run(
                "set-credential", "--tenant-id", tid,
                "--reference-uri", "secret://env-test1",
                "--backend", "env_ref",
                "--actor", "admin",
                env=env,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["backend"], "env_ref")
        self.assertEqual(body["status"], "active")

    def test_set_credential_bad_uri_exits_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run("init", "--actor", "admin", env=env)
            tid = json.loads(r.stdout)["tenant_id"]
            r = _run(
                "set-credential", "--tenant-id", tid,
                "--reference-uri", "not-a-uri",
                "--backend", "env_ref",
                "--actor", "admin",
                env=env,
            )
        self.assertEqual(r.returncode, API.EXIT_CONTRACT, r.stderr)

    def test_credential_material_never_in_argv(self):
        """The CLI must never accept material as an argv value.  The only
        material entry-points are --reference-uri (opaque) and the file/env
        indirection through backend adapters."""

        with tempfile.TemporaryDirectory() as tmp:
            r = _run(
                "set-credential", "--material", "SECRET",
                env={"CWK_INSTANCE_ROOT": tmp},
            )
        # argparse rejects the unknown --material flag.
        self.assertEqual(r.returncode, API.EXIT_USAGE, r.stderr)


class RotateBindingSecretCmdTests(unittest.TestCase):
    def test_rotate_begin_requires_material_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run(
                "rotate-binding-secret", "--phase", "begin", "--actor", "admin",
                env={"CWK_INSTANCE_ROOT": tmp},
            )
        self.assertEqual(r.returncode, API.EXIT_USAGE, r.stderr)

    def test_rotate_begin_refuses_symlink_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.mat")
            with open(real, "wb") as fh:
                fh.write(b"m" * 64)
            os.chmod(real, 0o600)
            link = os.path.join(tmp, "link.mat")
            os.symlink(real, link)
            env = {"CWK_INSTANCE_ROOT": tmp}
            r = _run(
                "rotate-binding-secret", "--phase", "begin",
                "--material-file", link,
                "--actor", "admin",
                env=env,
            )
        self.assertEqual(r.returncode, API.EXIT_CONTRACT, r.stderr)

    def test_rotate_finalize_when_stable_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            _run("init", "--actor", "admin", env=env)
            r = _run("rotate-binding-secret", "--phase", "finalize", "--actor", "admin", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["current_epoch"], 1)


class DoctorCmdTests(unittest.TestCase):
    def test_doctor_binding_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp}
            _run("init", "--actor", "admin", env=env)
            r = _run("doctor:binding", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual(body["schema"], "cwk.rt013.binding_doctor_report.v1")
        self.assertEqual(body["issue_count"], 0)


class HelpAndErrorTests(unittest.TestCase):
    def test_bind_help_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("bind-agent", "--help", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unknown_flag_exits_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("bind-agent", "--not-a-flag", env={"CWK_INSTANCE_ROOT": tmp})
        self.assertEqual(r.returncode, API.EXIT_USAGE, r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class SecretEnvNotPropagatedTests(unittest.TestCase):
    """CWORK_APP_KEY set in the host env MUST NOT surface in any CLI output."""

    def test_no_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CWK_INSTANCE_ROOT": tmp, "CWORK_APP_KEY": "should-never-leak"}
            _run("init", "--actor", "admin", env=env)
            r = _run("list-bindings", env=env)
        self.assertNotIn("should-never-leak", r.stdout)
        self.assertNotIn("should-never-leak", r.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
