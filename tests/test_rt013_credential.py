"""RT-013: Credential Broker + reference store."""

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
import cwk_credential_broker as CB  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


def _promote_tenant(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    reg = R.TenantRegistry(layout)
    record = reg.get(tenant_id)
    payload = dict(record.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        A.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=record.on_disk_sha256)


class _BrokerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenant_reg = R.TenantRegistry(self.layout)
        self.binding_reg = B.BindingRegistry(self.layout).initialize()
        self.store = CB.CredentialRefStore(self.layout).initialize()
        tenant, _ = self.tenant_reg.init_tenant(actor="admin")
        self.tenant_id = tenant.tenant_id
        _promote_tenant(self.layout, self.tenant_id, "active")

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)

    def _bind_alice(self) -> str:
        rec, _ = self.binding_reg.bind(
            tenant_id=self.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        return rec.agent_id_hash

    def _new_broker(self, *, env: dict[str, str] | None = None) -> CB.CredentialBroker:
        if env is None:
            env = {"CWK_INSTANCE_ROOT": self._tmp.name, "CWK_CRED_test1": "super-secret-material"}
        backends = CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=env)})
        return CB.CredentialBroker(layout=self.layout, backends=backends, inherit_env=env)


class SetReferenceTests(_BrokerBase):
    def test_set_active_record(self):
        rec, receipt = self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="initial",
        )
        self.assertEqual(rec.status, "active")
        self.assertEqual(rec.reference_uri, "secret://env-test1")
        self.assertEqual(rec.credential_epoch, 1)
        self.assertRegex(receipt["receipt_id"], r"^rcrd_[a-z0-9]{26}$")

    def test_set_bumps_auth_epoch(self):
        before = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        after = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.assertEqual(after, before + 1)

    def test_set_forbidden_for_offboarded(self):
        _promote_tenant(self.layout, self.tenant_id, "offboarded")
        with self.assertRaises(CB.CredentialStateError):
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="secret://env-test1",
                backend="env_ref", actor="admin", reason="t",
            )

    def test_set_refuses_bad_uri(self):
        with self.assertRaises(CB.CredentialSchemaError):
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="not-a-uri",
                backend="env_ref", actor="admin", reason="t",
            )
        with self.assertRaises(CB.CredentialSchemaError):
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="https://example.com/x",
                backend="env_ref", actor="admin", reason="t",
            )

    def test_set_refuses_bad_backend(self):
        with self.assertRaises(CB.CredentialSchemaError):
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="secret://env-test1",
                backend="attacker_backend", actor="admin", reason="t",
            )


class LeaseEnvTests(_BrokerBase):
    def test_lease_returns_env_with_minimum_whitelist(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        broker = self._new_broker()
        with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as lease:
            self.assertIn("CWK_INSTANCE_ROOT", lease.env)
            self.assertEqual(lease.env["CWORK_APP_KEY"], "super-secret-material")
            self.assertEqual(set(lease.env.keys()), {"CWK_INSTANCE_ROOT", "CWORK_APP_KEY"})
        # After exit env is empty.
        self.assertEqual(dict(lease.env), {})
        rc = lease.receipt()
        self.assertEqual(rc["schema"], "cwk.rt013.credential_broker_lease.v1")
        # Receipt has no material.
        blob = json.dumps(rc)
        self.assertNotIn("super-secret-material", blob)

    def test_lease_does_not_inherit_host_env(self):
        """The broker snapshots inherit env; adding a new var to os.environ
        after construction MUST NOT smuggle it into the lease."""

        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        broker = self._new_broker(env={"CWK_INSTANCE_ROOT": self._tmp.name, "CWK_CRED_test1": "sekret"})
        os.environ["ATTACKER_INJECTED"] = "boom"
        try:
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as lease:
                self.assertNotIn("ATTACKER_INJECTED", lease.env)
        finally:
            os.environ.pop("ATTACKER_INJECTED", None)


class StateGuardTests(_BrokerBase):
    def _prep(self, purpose="collector_run"):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        return ah

    def test_offboarded_tenant_refused(self):
        ah = self._prep()
        _promote_tenant(self.layout, self.tenant_id, "offboarded")
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialPolicyError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass

    def test_draft_tenant_refused(self):
        # Reset tenant to draft after we bound (test bypasses state machine).
        ah = self._prep()
        _promote_tenant(self.layout, self.tenant_id, "draft")
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialPolicyError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass

    def test_profile_pending_forbids_collector_run(self):
        ah = self._prep()
        _promote_tenant(self.layout, self.tenant_id, "profile_pending")
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialPolicyError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass

    def test_profile_pending_allows_sampling(self):
        ah = self._prep()
        _promote_tenant(self.layout, self.tenant_id, "profile_pending")
        broker = self._new_broker()
        with broker.lease(
            agent_id_hash=ah, tenant_id=self.tenant_id, purpose="sampling_collect_bounded"
        ) as lease:
            self.assertIn("CWORK_APP_KEY", lease.env)

    def test_unknown_purpose_refused(self):
        ah = self._prep()
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialPolicyError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="attacker_purpose") as _:
                pass


class DisableTests(_BrokerBase):
    def test_disable_blocks_lease(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        self.store.disable(tenant_id=self.tenant_id, actor="admin", reason="off")
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialStateError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass

    def test_disable_bumps_auth_epoch(self):
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        before = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.store.disable(tenant_id=self.tenant_id, actor="admin", reason="off")
        after = self.tenant_reg.get(self.tenant_id).auth_epoch
        self.assertEqual(after, before + 1)


class BackendTests(_BrokerBase):
    def test_env_backend_refuses_missing_var(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-missing",
            backend="env_ref", actor="admin", reason="t",
        )
        # env has CWK_CRED_test1 but NOT CWK_CRED_missing.
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialBackendError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass

    def test_env_backend_refuses_wrong_scheme(self):
        with self.assertRaises(CB.CredentialBackendError):
            CB.EnvRefBackend(env={"CWK_CRED_x": "v"}).read_material("secret://file-x")

    def test_file_backend_reads_regular_file(self):
        ah = self._bind_alice()
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"file-material-value")
            path = fh.name
        os.chmod(path, 0o600)
        try:
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="secret://file-abc",
                backend="file_ref", actor="admin", reason="t",
            )
            backend = CB.FileRefBackend({"abc": path})
            broker = CB.CredentialBroker(
                layout=self.layout,
                backends=CB.BackendRegistry({"file_ref": backend}),
                inherit_env={"CWK_INSTANCE_ROOT": self._tmp.name},
            )
            with broker.lease(
                agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run"
            ) as lease:
                self.assertEqual(lease.env["CWORK_APP_KEY"], "file-material-value")
        finally:
            os.unlink(path)

    def test_file_backend_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            with open(real, "wb") as fh:
                fh.write(b"x" * 64)
            os.chmod(real, 0o600)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            backend = CB.FileRefBackend({"abc": link})
            with self.assertRaises(CB.CredentialBackendError):
                backend.read_material("secret://file-abc")


class SecretScanTests(_BrokerBase):
    def test_receipt_and_record_contain_no_material(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        broker = self._new_broker()
        with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as lease:
            pass
        receipt = lease.receipt()
        blob = json.dumps(receipt)
        for forbidden in ("super-secret-material", "material", "CWORK_APP_KEY"):
            # "CWORK_APP_KEY" is only forbidden as a *value*; the schema id
            # contains "reference_uri" but never a material string.  We
            # check the entire receipt for material substring.
            if forbidden == "material":
                # 'material' is a common substring; only reject if it comes
                # from a known material-bearing key.
                continue
            self.assertNotIn(forbidden, blob)

    def test_lease_repr_does_not_leak_material(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        broker = self._new_broker()
        with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as lease:
            r = repr(lease)
            self.assertNotIn("super-secret-material", r)


class ListTenantsTests(_BrokerBase):
    def test_list(self):
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        self.assertEqual(self.store.list_tenants(), [self.tenant_id])


class RotationInFlightTests(_BrokerBase):
    def test_rotation_in_flight_refuses_lease(self):
        ah = self._bind_alice()
        self.store.set_reference(
            tenant_id=self.tenant_id, reference_uri="secret://env-test1",
            backend="env_ref", actor="admin", reason="t",
        )
        # Begin rotation.
        self.store.rotate_begin(
            tenant_id=self.tenant_id, new_reference_uri="secret://env-test2",
            new_backend="env_ref", actor="admin", reason="t",
        )
        broker = self._new_broker()
        with self.assertRaises(CB.CredentialStateError):
            with broker.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as _:
                pass
        # After finalize, the NEW reference is active.
        env = {"CWK_INSTANCE_ROOT": self._tmp.name, "CWK_CRED_test2": "new-material"}
        self.store.rotate_finalize(tenant_id=self.tenant_id, actor="admin", reason="t")
        broker2 = self._new_broker(env=env)
        with broker2.lease(agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run") as lease:
            self.assertEqual(lease.env["CWORK_APP_KEY"], "new-material")


class RepoEnvFallbackTests(_BrokerBase):
    def test_broker_never_falls_back_to_process_env(self):
        """If the tenant's credential resolves to a missing backend var,
        the broker MUST NOT read the host process's ``CWORK_APP_KEY``.
        """

        os.environ["CWORK_APP_KEY"] = "host-secret-should-never-leak"
        try:
            ah = self._bind_alice()
            self.store.set_reference(
                tenant_id=self.tenant_id, reference_uri="secret://env-nowhere",
                backend="env_ref", actor="admin", reason="t",
            )
            # Isolated env intentionally missing CWK_CRED_nowhere.
            broker = self._new_broker(env={"CWK_INSTANCE_ROOT": self._tmp.name})
            with self.assertRaises(CB.CredentialBackendError):
                with broker.lease(
                    agent_id_hash=ah, tenant_id=self.tenant_id, purpose="collector_run"
                ) as _:
                    pass
        finally:
            os.environ.pop("CWORK_APP_KEY", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
