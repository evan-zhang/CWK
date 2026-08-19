"""RT-013: Binding Registry — HMAC, epoch, one-agent-one-tenant, resolve gating."""

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
import cwk_atomic_file as A  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


def _promote_tenant(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    """Test-only tenant status promotion via raw CAS.  Bypasses RT-012 API
    since RT-012 exposes no state-mutation surface beyond ``init_tenant``.
    """

    reg = R.TenantRegistry(layout)
    record = reg.get(tenant_id)
    payload = dict(record.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        A.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=record.on_disk_sha256)


class _RegBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenant_reg = R.TenantRegistry(self.layout)
        self.binding_reg = B.BindingRegistry(self.layout).initialize()
        tenant, _ = self.tenant_reg.init_tenant(actor="admin", reason="setup")
        self.tenant_id = tenant.tenant_id

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)


class SchemaTests(unittest.TestCase):
    def test_frozen_states(self):
        self.assertEqual(B.BINDING_STATES, ("active", "suspended", "revoked"))

    def test_only_active_is_queryable(self):
        self.assertEqual(B.RESOLVE_HIT_STATUSES, frozenset({"active"}))

    def test_secret_min_bytes(self):
        self.assertEqual(B.SECRET_MIN_BYTES, 32)

    def test_binding_record_schema_rejects_extra_field(self):
        good = {
            "schema": "cwk.rt013.agent_binding.v1",
            "agent_id_hash": "a" * 64,
            "tenant_id": "t_" + "a" * 26,
            "binding_epoch": 1,
            "binding_secret_epoch": 1,
            "status": "active",
            "bound_at": "2026-08-19T00:00:00Z",
            "updated_at": "2026-08-19T00:00:00Z",
            "revoked_at": None,
            "provisioning": {
                "last_receipt_id": "rbnd_" + "a" * 26,
                "last_receipt_sha256": "0" * 64,
            },
            "history": [
                {
                    "action": "bind",
                    "at": "2026-08-19T00:00:00Z",
                    "actor": "admin",
                    "reason": "test",
                    "binding_epoch_after": 1,
                    "binding_secret_epoch": 1,
                    "tenant_id": "t_" + "a" * 26,
                    "receipt_id": "rbnd_" + "a" * 26,
                }
            ],
        }
        # Baseline validates.
        B.validate_binding_record(good)
        # Extra top-level field → reject.
        bad = dict(good)
        bad["attacker"] = 1
        with self.assertRaises(B.BindingSchemaError):
            B.validate_binding_record(bad)

    def test_binding_record_forbidden_status(self):
        good = {
            "schema": "cwk.rt013.agent_binding.v1",
            "agent_id_hash": "a" * 64,
            "tenant_id": "t_" + "a" * 26,
            "binding_epoch": 1,
            "binding_secret_epoch": 1,
            "status": "enabled",  # not in enum
            "bound_at": "2026-08-19T00:00:00Z",
            "updated_at": "2026-08-19T00:00:00Z",
            "revoked_at": None,
            "provisioning": {
                "last_receipt_id": "rbnd_" + "a" * 26,
                "last_receipt_sha256": "0" * 64,
            },
            "history": [
                {
                    "action": "bind",
                    "at": "2026-08-19T00:00:00Z",
                    "actor": "admin",
                    "reason": "test",
                    "binding_epoch_after": 1,
                    "binding_secret_epoch": 1,
                    "tenant_id": "t_" + "a" * 26,
                    "receipt_id": "rbnd_" + "a" * 26,
                }
            ],
        }
        with self.assertRaises(B.BindingSchemaError):
            B.validate_binding_record(good)

    def test_bool_int_rejected(self):
        good = {
            "schema": "cwk.rt013.agent_binding.v1",
            "agent_id_hash": "a" * 64,
            "tenant_id": "t_" + "a" * 26,
            "binding_epoch": True,  # bool, not int
            "binding_secret_epoch": 1,
            "status": "active",
            "bound_at": "2026-08-19T00:00:00Z",
            "updated_at": "2026-08-19T00:00:00Z",
            "revoked_at": None,
            "provisioning": {
                "last_receipt_id": "rbnd_" + "a" * 26,
                "last_receipt_sha256": "0" * 64,
            },
            "history": [
                {
                    "action": "bind",
                    "at": "2026-08-19T00:00:00Z",
                    "actor": "admin",
                    "reason": "test",
                    "binding_epoch_after": 1,
                    "binding_secret_epoch": 1,
                    "tenant_id": "t_" + "a" * 26,
                    "receipt_id": "rbnd_" + "a" * 26,
                }
            ],
        }
        with self.assertRaises(B.BindingSchemaError):
            B.validate_binding_record(good)


class RawAgentIdValidationTests(unittest.TestCase):
    def test_bad_shapes_rejected(self):
        for bad in ("", "with space", "../../etc", "a/b", "a\\b", "a\nb", "a\x00b", "-startdash"):
            with self.assertRaises(B.AgentIdError, msg=bad):
                B.validate_raw_agent_id(bad)

    def test_good_shapes_accepted(self):
        for good in ("alice", "alice@example.com", "svc:agent-01", "a.b_c-1"):
            B.validate_raw_agent_id(good)  # does not raise


class HmacTests(unittest.TestCase):
    def test_deterministic(self):
        secret = b"a" * 32
        h1 = B.hmac_hash_agent_id(secret, "alice")
        h2 = B.hmac_hash_agent_id(secret, "alice")
        self.assertEqual(h1, h2)
        self.assertRegex(h1, r"^[0-9a-f]{64}$")

    def test_different_secrets_different_hashes(self):
        h1 = B.hmac_hash_agent_id(b"a" * 32, "alice")
        h2 = B.hmac_hash_agent_id(b"b" * 32, "alice")
        self.assertNotEqual(h1, h2)

    def test_short_secret_rejected(self):
        with self.assertRaises(B.BindingError):
            B.hmac_hash_agent_id(b"tiny", "alice")

    def test_short_agent_id_rejected(self):
        with self.assertRaises(B.AgentIdError):
            B.hmac_hash_agent_id(b"a" * 32, "")


class BindTests(_RegBase):
    def test_bind_creates_active_record(self):
        rec, receipt = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="initial",
        )
        self.assertEqual(rec.status, "active")
        self.assertEqual(rec.binding_epoch, 1)
        self.assertEqual(rec.tenant_id, self.tenant_id)
        self.assertRegex(rec.agent_id_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(receipt.payload["receipt_id"], r"^rbnd_[a-z0-9]{26}$")
        self.assertEqual(receipt.payload["action"], "bind")

    def test_bind_raw_id_not_stored(self):
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice-secret-id",
            actor="admin", reason="test",
        )
        # Read the on-disk record and assert no raw id anywhere.
        with B._binding_sub(self.layout, "current") as fd:
            body = A.read_file(fd, f"{rec.agent_id_hash}.json").decode("utf-8")
        self.assertNotIn("alice-secret-id", body)

    def test_bind_conflict_when_agent_already_bound(self):
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=self.tenant_id, raw_agent_id="alice",
                actor="admin", reason="dup",
            )

    def test_bind_conflict_across_tenants(self):
        # Second tenant.
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="first",
        )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=t2.tenant_id, raw_agent_id="alice",
                actor="admin", reason="conflict",
            )

    def test_bind_refused_for_offboarded_tenant(self):
        _promote_tenant(self.layout, self.tenant_id, "offboarded")
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.bind(
                tenant_id=self.tenant_id, raw_agent_id="alice",
                actor="admin", reason="t",
            )

    def test_bind_refused_for_suspended_tenant(self):
        _promote_tenant(self.layout, self.tenant_id, "suspended")
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.bind(
                tenant_id=self.tenant_id, raw_agent_id="alice",
                actor="admin", reason="t",
            )

    def test_bind_bumps_auth_epoch(self):
        before = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        after = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.assertEqual(after, before + 1)


class ResolveTests(_RegBase):
    def _bind_alice_in_active_tenant(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        return rec

    def test_resolve_hit(self):
        self._bind_alice_in_active_tenant()
        resolved = self.binding_reg.resolve("alice", purpose="collector_run")
        self.assertEqual(resolved.tenant_id, self.tenant_id)
        self.assertEqual(resolved.status, "active")

    def test_resolve_miss_for_unknown_agent(self):
        with self.assertRaises(B.BindingNotFound):
            self.binding_reg.resolve("nobody", purpose="collector_run")

    def test_resolve_refuses_wrong_purpose(self):
        self._bind_alice_in_active_tenant()
        with self.assertRaises(B.BindingStateError):
            # active tenants don't allow admin_configure via resolve — but
            # neither does `admin_configure` count as a broker purpose, so
            # the unknown-purpose check fires first.
            self.binding_reg.resolve("alice", purpose="admin_configure")

    def test_resolve_refuses_when_tenant_in_draft(self):
        # Bind while tenant is in draft (allowed) — resolve fails because
        # draft does not permit any broker purpose.
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.resolve("alice", purpose="collector_run")

    def test_resolve_refuses_when_tenant_offboarded(self):
        self._bind_alice_in_active_tenant()
        _promote_tenant(self.layout, self.tenant_id, "offboarded")
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.resolve("alice", purpose="collector_run")


class RevokeTests(_RegBase):
    def test_revoke_bumps_epoch_and_blocks_resolve(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        # Resolve works.
        self.binding_reg.resolve("alice", purpose="collector_run")
        auth_before = self.tenant_reg.get(self.tenant_id).auth_epoch
        _, receipt = self.binding_reg.revoke(
            raw_agent_id="alice", actor="admin", reason="off",
        )
        auth_after = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.assertEqual(auth_after, auth_before + 1)
        self.assertEqual(receipt.payload["action"], "revoke")
        self.assertEqual(receipt.payload["binding_epoch_after"], rec.binding_epoch + 1)
        with self.assertRaises(B.BindingRevoked):
            self.binding_reg.resolve("alice", purpose="collector_run")

    def test_double_revoke_conflict(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="off")
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="off2")

    def test_revoke_unknown_agent(self):
        with self.assertRaises(B.BindingNotFound):
            self.binding_reg.revoke(raw_agent_id="nobody", actor="admin", reason="x")


class SuspendReactivateTests(_RegBase):
    def test_suspend_then_reactivate_cycle(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        _, receipt1 = self.binding_reg.suspend(
            raw_agent_id="alice", actor="admin", reason="hold",
        )
        with self.assertRaises(B.BindingSuspended):
            self.binding_reg.resolve("alice", purpose="collector_run")
        _, receipt2 = self.binding_reg.reactivate(
            raw_agent_id="alice", actor="admin", reason="resume",
        )
        # Resolve works again.
        r = self.binding_reg.resolve("alice", purpose="collector_run")
        self.assertEqual(r.binding_epoch, rec.binding_epoch + 2)
        self.assertEqual(receipt1.payload["action"], "suspend")
        self.assertEqual(receipt2.payload["action"], "reactivate")

    def test_reactivate_of_active_rejected(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.reactivate(raw_agent_id="alice", actor="admin", reason="x")


class RebindTests(_RegBase):
    def test_rebind_forces_two_step(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        rec1, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote_tenant(self.layout, t2.tenant_id, "active")
        rec2, receipts = self.binding_reg.rebind(
            raw_agent_id="alice", new_tenant_id=t2.tenant_id,
            actor="admin", reason="move",
        )
        self.assertEqual(rec2.tenant_id, t2.tenant_id)
        self.assertEqual(rec2.status, "active")
        self.assertGreaterEqual(rec2.binding_epoch, rec1.binding_epoch + 2)
        # Both receipts present (rebind_out + rebind_in).
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0].payload["action"], "rebind_out")
        # The bind path emits "bind" action inside the receipt; the record's
        # history is rewritten to "rebind_in".
        self.assertIn(receipts[1].payload["action"], {"bind", "rebind_in"})
        # Verify history contains rebind_in as last entry.
        self.assertEqual(rec2.payload["history"][-1]["action"], "rebind_in")


class EpochPropagationTests(_RegBase):
    def test_every_mutation_bumps_tenant_auth_epoch(self):
        _promote_tenant(self.layout, self.tenant_id, "active")
        e0 = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        e1 = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.binding_reg.suspend(raw_agent_id="alice", actor="admin", reason="s")
        e2 = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.binding_reg.reactivate(raw_agent_id="alice", actor="admin", reason="r")
        e3 = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="rev")
        e4 = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.assertEqual([e0, e1, e2, e3, e4], [1, 2, 3, 4, 5])


class ListTests(_RegBase):
    def test_list_active_returns_records(self):
        for name in ("alice", "bob", "carol"):
            self.binding_reg.bind(
                tenant_id=self.tenant_id, raw_agent_id=name,
                actor="admin", reason="t",
            )
        records = self.binding_reg.list_active()
        self.assertEqual(len(records), 3)

    def test_list_filtered_by_tenant(self):
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.binding_reg.bind(
            tenant_id=t2.tenant_id, raw_agent_id="bob",
            actor="admin", reason="t",
        )
        self.assertEqual(len(self.binding_reg.list_active(tenant_id=self.tenant_id)), 1)
        self.assertEqual(len(self.binding_reg.list_active(tenant_id=t2.tenant_id)), 1)


class RecoveryTests(_RegBase):
    def test_recover_is_no_op_when_clean(self):
        s = self.binding_reg.recover()
        self.assertEqual(s["orphans_removed"], 0)

    def test_recover_cleans_orphans(self):
        with B._binding_sub(self.layout, "current") as fd:
            os.open(f"{A.TEMP_PREFIX}test.orphan", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
        s = self.binding_reg.recover()
        self.assertGreaterEqual(s["orphans_removed"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
