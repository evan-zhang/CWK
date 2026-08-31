"""RT-013: rotation — credential + binding-secret dual-write, tombstone, recovery."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_agent_binding as B  # noqa: E402
import cwk_atomic_file as A  # noqa: E402
import cwk_credential_broker as CB  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


def _promote(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    reg = R.TenantRegistry(layout)
    rec = reg.get(tenant_id)
    payload = dict(rec.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        A.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=rec.on_disk_sha256)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.instance_root = str(Path(self._tmp.name).resolve())
        os.environ[I.ENV_VAR] = self.instance_root
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenant_reg = R.TenantRegistry(self.layout)
        self.binding_reg = B.BindingRegistry(self.layout).initialize()
        self.store = CB.CredentialRefStore(self.layout).initialize()
        tenant, _ = self.tenant_reg.init_tenant(actor="admin")
        self.tenant_id = tenant.tenant_id
        _promote(self.layout, self.tenant_id, "active")

    def tearDown(self):
        self.layout.close()
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)


class CredentialRotationTests(_Base):
    def test_dual_write_then_finalize(self):
        rec1, _ = self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-old",
            backend="env_ref", actor="admin", reason="t",
        )
        self.assertEqual(rec1.rotation_state, "stable")

        # Rotate begin.
        rec2, _ = self.store.rotate_begin(
            tenant_id=self.tenant_id, new_reference_uri="secret://env-new",
            new_backend="env_ref", actor="admin", reason="t",
        )
        self.assertEqual(rec2.rotation_state, "dual_write")
        self.assertEqual(rec2.status, "rotating")
        self.assertEqual(rec2.payload["rotation"]["secondary_reference_uri"], "secret://env-new")

        # Finalize.
        rec3, _ = self.store.rotate_finalize(
            tenant_id=self.tenant_id, actor="admin", reason="t",
        )
        self.assertEqual(rec3.rotation_state, "stable")
        self.assertEqual(rec3.status, "active")
        self.assertEqual(rec3.reference_uri, "secret://env-new")

        # Old reference lives in tombstone.
        with CB._credentials_sub(self.layout, "tombstone") as tfd:
            entries = list(os.scandir(tfd))
        self.assertGreaterEqual(len(entries), 1)

    def test_double_begin_conflict(self):
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-a",
            backend="env_ref", actor="admin", reason="t",
        )
        self.store.rotate_begin(
            tenant_id=self.tenant_id, new_reference_uri="secret://env-b",
            new_backend="env_ref", actor="admin", reason="t",
        )
        with self.assertRaises(CB.CredentialConflictError):
            self.store.rotate_begin(
                tenant_id=self.tenant_id, new_reference_uri="secret://env-c",
                new_backend="env_ref", actor="admin", reason="t",
            )

    def test_finalize_when_stable_rejected(self):
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-a",
            backend="env_ref", actor="admin", reason="t",
        )
        with self.assertRaises(CB.CredentialConflictError):
            self.store.rotate_finalize(
                tenant_id=self.tenant_id, actor="admin", reason="t",
            )

    def test_rotation_same_uri_rejected(self):
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-a",
            backend="env_ref", actor="admin", reason="t",
        )
        with self.assertRaises(CB.CredentialConflictError):
            self.store.rotate_begin(
                tenant_id=self.tenant_id, new_reference_uri="secret://env-a",
                new_backend="env_ref", actor="admin", reason="t",
            )


class BindingSecretRotationTests(_Base):
    def test_pointer_starts_at_epoch_1(self):
        pointer = self.binding_reg.secrets.read_pointer()
        self.assertEqual(pointer["rotation_state"], "stable")
        self.assertEqual(pointer["current_epoch"], 1)
        self.assertIsNone(pointer["secondary_epoch"])

    def test_rotate_begin_and_finalize_advances_epoch(self):
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        # Resolve works pre-rotation.
        r0 = self.binding_reg.resolve("alice", purpose="collector_run")
        self.assertEqual(r0.binding_secret_epoch, 1)

        new_material = secrets.token_bytes(B.SECRET_MIN_BYTES)
        begin_pointer, summary = self.binding_reg.rotate_secret(
            new_material=new_material, actor="admin", reason="rot",
        )
        self.assertEqual(summary["old_epoch"], 1)
        self.assertEqual(summary["new_epoch"], 2)
        self.assertIn(self.tenant_id, summary["tenants_affected"])
        # After rotation, the previous binding is tombstoned.
        with B._binding_sub(self.layout, "tombstone") as tfd:
            with os.scandir(tfd) as it:
                dirs = [e.name for e in it if e.is_dir()]
        self.assertIn("1", dirs)

        # The bound alice is unreachable via the new hash — she must re-bind.
        with self.assertRaises(B.BindingNotFound):
            self.binding_reg.resolve("alice", purpose="collector_run")

    def test_rotation_freezes_mutations(self):
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        # Manually enter dual_write phase (test the freeze semantics directly).
        new_material = secrets.token_bytes(B.SECRET_MIN_BYTES)
        self.binding_reg.secrets.rotate_begin(
            new_material=new_material, actor="admin", reason="rot",
        )
        # All mutating APIs must fail closed during rotation.
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=self.tenant_id, raw_agent_id="bob",
                actor="admin", reason="mid-rotation",
            )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="mid")

        # Cleanup rotation so tearDown doesn't blow up.
        self.binding_reg.secrets.rotate_finalize(actor="admin", reason="rot")

    def test_rotation_bumps_auth_epoch_for_touched_tenants(self):
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t2.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.binding_reg.bind(
            tenant_id=t2.tenant_id, raw_agent_id="bob",
            actor="admin", reason="t",
        )
        e_a = self.tenant_reg.get(self.tenant_id).auth_epoch
        e_b = self.tenant_reg.get(t2.tenant_id).auth_epoch

        new_material = secrets.token_bytes(B.SECRET_MIN_BYTES)
        self.binding_reg.rotate_secret(
            new_material=new_material, actor="admin", reason="rot",
        )

        e_a2 = self.tenant_reg.get(self.tenant_id).auth_epoch
        e_b2 = self.tenant_reg.get(t2.tenant_id).auth_epoch
        # Both tenants observe the auth_epoch bump.
        self.assertGreater(e_a2, e_a)
        self.assertGreater(e_b2, e_b)


class RecoveryTests(_Base):
    def test_recover_removes_binding_orphans(self):
        with B._binding_sub(self.layout, "receipts") as fd:
            os.open(f"{A.TEMP_PREFIX}foo.orphan", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
        s = self.binding_reg.recover()
        self.assertGreaterEqual(s["orphans_removed"], 1)

    def test_recover_removes_credential_orphans(self):
        _ = self.store  # ensure init
        with CB._credentials_sub(self.layout, "receipts") as fd:
            os.open(f"{A.TEMP_PREFIX}bar.orphan", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
        s = self.store.recover()
        self.assertGreaterEqual(s["orphans_removed"], 1)

    def test_recover_is_idempotent(self):
        s1 = self.binding_reg.recover()
        s2 = self.binding_reg.recover()
        self.assertEqual(s1, s2)


class DualWriteConsistencyTests(_Base):
    def test_no_lease_during_credential_rotation(self):
        """Concurrent lease during rotation must fail closed rather than
        return material from an unstable pointer."""

        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-a",
            backend="env_ref", actor="admin", reason="t",
        )
        self.store.rotate_begin(
            tenant_id=self.tenant_id, new_reference_uri="secret://env-b",
            new_backend="env_ref", actor="admin", reason="t",
        )
        env = {"CWK_INSTANCE_ROOT": self.instance_root, "CWK_CRED_a": "old", "CWK_CRED_b": "new"}
        broker = CB.CredentialBroker(
            layout=self.layout,
            backends=CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=env)}),
            inherit_env=env,
        )
        with self.assertRaises(CB.CredentialStateError):
            with broker.lease(
                agent_id_hash=rec.agent_id_hash, tenant_id=self.tenant_id,
                purpose="collector_run",
            ) as _:
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
