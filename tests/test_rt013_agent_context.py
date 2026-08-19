"""RT-013: AgentContext — trusted-source-only construction, snapshot, redact."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_agent_binding as B  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_atomic_file as A  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


def _promote_tenant(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    reg = R.TenantRegistry(layout)
    record = reg.get(tenant_id)
    payload = dict(record.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        A.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=record.on_disk_sha256)


class _ACBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenant_reg = R.TenantRegistry(self.layout)
        self.binding_reg = B.BindingRegistry(self.layout).initialize()
        tenant, _ = self.tenant_reg.init_tenant(actor="admin")
        self.tenant_id = tenant.tenant_id
        _promote_tenant(self.layout, self.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)


class TrustedSourceTests(_ACBase):
    def test_frozen_source_set(self):
        self.assertEqual(
            AC.TRUSTED_AGENT_SOURCES,
            frozenset({"admin_cli", "gateway_authenticated_context"}),
        )

    def test_admin_cli_ok(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        self.assertEqual(ctx.tenant_id, self.tenant_id)
        self.assertEqual(ctx.source, "admin_cli")

    def test_untrusted_source_rejected(self):
        for bad in ("request_body", "cli_query", "user_supplied", "env", "argv"):
            with self.assertRaises(AC.AgentContextError, msg=bad):
                AC.AgentContext.from_trusted(
                    raw_agent_id="alice", source=bad,
                    purpose="collector_run", layout=self.layout,
                )


class NoUntrustedConstructorTests(_ACBase):
    def test_no_untrusted_class_method(self):
        # There must be NO from_untrusted / from_request_body / from_env method.
        for name in ("from_untrusted", "from_request_body", "from_env", "from_argv", "from_body"):
            self.assertFalse(
                hasattr(AC.AgentContext, name),
                f"AgentContext.{name} must NOT exist; found instead",
            )

    def test_direct_init_refused(self):
        # Attempting to construct without the private token → AgentContextError.
        with self.assertRaises(AC.AgentContextError):
            AC.AgentContext(
                _snapshot=AC.AgentContextSnapshot(
                    agent_id_hash="a" * 64,
                    tenant_id="t_" + "a" * 26,
                    tenant_auth_epoch=1,
                    binding_epoch=1,
                    binding_secret_epoch=1,
                    tenant_status="active",
                    resolved_at="2026-08-19T00:00:00Z",
                ),
                _source="admin_cli",
                _construction_token=object(),  # not the real token
            )


class SnapshotTests(_ACBase):
    def test_snapshot_fields(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        s = ctx.snapshot()
        self.assertEqual(s.tenant_id, self.tenant_id)
        self.assertEqual(s.binding_epoch, 1)
        self.assertEqual(s.binding_secret_epoch, 1)
        self.assertEqual(s.tenant_status, "active")
        self.assertEqual(s.tenant_auth_epoch, ctx.tenant_auth_epoch)
        self.assertRegex(s.agent_id_hash, r"^[0-9a-f]{64}$")

    def test_snapshot_immutable(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        s = ctx.snapshot()
        with self.assertRaises(Exception):
            s.tenant_id = "t_other"  # dataclass frozen

    def test_snapshot_hashable(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        self.assertEqual(hash(ctx), hash(ctx))

    def test_from_snapshot_roundtrip(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        s = ctx.snapshot()
        ctx2 = AC.AgentContext.from_snapshot(snapshot=s, source="admin_cli")
        self.assertEqual(ctx, ctx2)

    def test_from_snapshot_rejects_untrusted_source(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        s = ctx.snapshot()
        with self.assertRaises(AC.AgentContextError):
            AC.AgentContext.from_snapshot(snapshot=s, source="attacker")


class RedactTests(_ACBase):
    def test_redact_never_leaks_raw_id(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice",
            source="admin_cli",
            purpose="collector_run",
            layout=self.layout,
        )
        r = ctx.redact()
        blob = json.dumps(r)
        # The literal raw agent id string never appears.
        self.assertNotIn("alice", blob)
        # Only 8-char prefix of hash surfaces.
        self.assertEqual(len(r["agent_id_hash_prefix"]), 8)
        self.assertEqual(len(r["tenant_id_prefix"]), 8)

    def test_repr_shows_truncated_hash(self):
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice",
            source="admin_cli",
            purpose="collector_run",
            layout=self.layout,
        )
        r = repr(ctx)
        self.assertNotIn("alice", r)
        self.assertIn("hash=", r)
        # Full 64-char hash MUST NOT appear.
        self.assertNotIn(ctx.agent_id_hash, r)


class RaiseOnUnknownAgentTests(_ACBase):
    def test_unknown_agent_fails_closed(self):
        with self.assertRaises(B.BindingNotFound):
            AC.AgentContext.from_trusted(
                raw_agent_id="unknown", source="admin_cli",
                purpose="collector_run", layout=self.layout,
            )

    def test_revoked_agent_fails_closed(self):
        self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="off")
        with self.assertRaises(B.BindingRevoked):
            AC.AgentContext.from_trusted(
                raw_agent_id="alice", source="admin_cli",
                purpose="collector_run", layout=self.layout,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
