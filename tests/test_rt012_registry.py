"""RT-012: TenantRegistry — state machine, CAS, provisioning receipts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_atomic_file as A  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_tenant_registry as R  # noqa: E402


class StateMachineTests(unittest.TestCase):
    def test_frozen_six_states(self):
        self.assertEqual(
            R.TENANT_STATES,
            ("draft", "profile_pending", "pilot", "active", "suspended", "offboarded"),
        )

    def test_no_alias_states(self):
        for forbidden in ("enabled", "disabled", "provisioning", "retiring"):
            self.assertNotIn(forbidden, R.TENANT_STATES)

    def test_transitions_exact_prd(self):
        expected = {
            "draft": ("profile_pending",),
            "profile_pending": ("pilot", "suspended", "offboarded"),
            "pilot": ("active", "suspended", "offboarded"),
            "active": ("suspended", "offboarded"),
            "suspended": ("profile_pending", "pilot", "active", "offboarded"),
            "offboarded": (),
        }
        self.assertEqual(R.TENANT_ALLOWED_TRANSITIONS, expected)

    def test_offboarded_is_terminal(self):
        self.assertEqual(R.TERMINAL_STATE, "offboarded")
        self.assertEqual(R.TENANT_ALLOWED_TRANSITIONS["offboarded"], ())

    def test_assert_valid_transition_positive(self):
        R.assert_valid_transition("draft", "profile_pending")
        R.assert_valid_transition("profile_pending", "pilot")
        R.assert_valid_transition("suspended", "active")

    def test_assert_valid_transition_negative(self):
        with self.assertRaises(R.InvalidTransition):
            R.assert_valid_transition("draft", "active")
        with self.assertRaises(R.InvalidTransition):
            R.assert_valid_transition("offboarded", "active")
        with self.assertRaises(R.InvalidTransition):
            R.assert_valid_transition("active", "draft")

    def test_state_graph_shape(self):
        g = R.state_graph()
        self.assertEqual(g["schema"], "cwk.rt012.state_graph.v1")
        self.assertEqual(sorted(g["states"]), sorted(R.TENANT_STATES))
        self.assertIn("enabled", g["forbidden_aliases"])

    def test_operation_matrix_matches_prd(self):
        m = R.TENANT_OPERATION_MATRIX
        # PRD FR-02 / DESIGN §C-02 constraints.
        self.assertEqual(m["draft"], frozenset({"admin_configure"}))
        self.assertNotIn("query_broker", m["profile_pending"])
        self.assertNotIn("credential_resolve", m["draft"])
        self.assertNotIn("credential_resolve", m["suspended"])
        self.assertNotIn("query_broker", m["suspended"])
        self.assertNotIn("query_broker", m["draft"])
        self.assertNotIn("query_broker", m["offboarded"])
        self.assertIn("query_broker", m["pilot"])
        self.assertIn("query_broker", m["active"])
        self.assertEqual(m["offboarded"], frozenset())


class RegistryBaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[I.ENV_VAR] = str(Path(self._tmp.name).resolve())
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.reg = R.TenantRegistry(self.layout)

    def tearDown(self):
        self.layout.close()
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)


class InitTests(RegistryBaseTests):
    def test_init_creates_draft(self):
        rec, rcpt = self.reg.init_tenant(actor="admin", reason="initial")
        self.assertEqual(rec.status, "draft")
        self.assertEqual(rec.auth_epoch, 1)
        self.assertEqual(rec.record_revision, 1)
        self.assertEqual(rec.payload["credential_ref"], None)
        self.assertEqual(rec.payload["active_profile_version"], None)
        self.assertEqual(rec.payload["quota"]["scheme"], "cwk.rt012.quota.unset.v1")
        self.assertIsNone(rec.payload["quota"]["limits"]["disk_bytes"])
        self.assertTrue(rcpt.txn_id.startswith("txn_"))

    def test_init_id_is_opaque(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        self.assertRegex(rec.tenant_id, r"^t_[a-z0-9]{26}$")

    def test_init_refuses_user_derived_id(self):
        with self.assertRaises(I.TenantIdError):
            self.reg.init_tenant(tenant_id="alice", actor="admin")
        with self.assertRaises(I.TenantIdError):
            self.reg.init_tenant(tenant_id="../../etc/passwd", actor="admin")

    def test_init_writes_projection_with_revision(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        tenant = self.layout.tenant(rec.tenant_id)
        with tenant.child_fd("config") as cfd:
            body = A.read_file(cfd, "tenant.projection.json")
        proj = json.loads(body.decode("utf-8"))
        self.assertEqual(proj["schema"], "cwk.rt012.tenant_projection.v1")
        self.assertEqual(proj["record_revision"], rec.record_revision)
        self.assertEqual(proj["tenant_id"], rec.tenant_id)
        # Projection is a read-only projection — MUST NOT be a second SoR.
        self.assertNotIn("credential_ref", proj)

    def test_duplicate_init_rejected(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        with self.assertRaises(R.TenantExists):
            self.reg.init_tenant(tenant_id=rec.tenant_id, actor="admin")

    def test_init_generates_receipt_matching_record(self):
        rec, rcpt = self.reg.init_tenant(actor="admin")
        self.assertEqual(rcpt.tenant_id, rec.tenant_id)
        self.assertEqual(rcpt.payload["action"], "tenant_init")
        self.assertEqual(rcpt.payload["tenant_status_after"], "draft")

    def test_init_requires_actor(self):
        with self.assertRaises(R.RegistryError):
            self.reg.init_tenant(actor="")


class ValidationTests(RegistryBaseTests):
    def test_bad_record_rejected(self):
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record({"schema": "cwk.rt012.tenant_record.v1"})

    def test_extra_field_rejected(self):
        good_id = "t_" + "a" * 26
        payload = {
            "schema": "cwk.rt012.tenant_record.v1",
            "tenant_id": good_id,
            "status": "draft",
            "credential_ref": None,
            "active_profile_version": None,
            "auth_epoch": 1,
            "record_revision": 1,
            "quota": {
                "scheme": "cwk.rt012.quota.unset.v1",
                "measurement_owner": "RT-024",
                "confirmation_owner": "RT-026",
                "limits": {
                    "collector_concurrency": None,
                    "scheduler_concurrency": None,
                    "disk_bytes": None,
                    "ai_calls_per_day": None,
                    "retention_days": None,
                },
            },
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "provisioning": {
                "last_receipt_id": "txn_" + "a" * 26,
                "last_receipt_sha256": "0" * 64,
            },
            "malicious_field": "boom",
        }
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record(payload)

    def test_bool_as_int_rejected(self):
        good_id = "t_" + "a" * 26
        payload = {
            "schema": "cwk.rt012.tenant_record.v1",
            "tenant_id": good_id,
            "status": "draft",
            "credential_ref": None,
            "active_profile_version": None,
            "auth_epoch": True,   # bool masquerading as int
            "record_revision": 1,
            "quota": {
                "scheme": "cwk.rt012.quota.unset.v1",
                "measurement_owner": "RT-024",
                "confirmation_owner": "RT-026",
                "limits": {
                    "collector_concurrency": None,
                    "scheduler_concurrency": None,
                    "disk_bytes": None,
                    "ai_calls_per_day": None,
                    "retention_days": None,
                },
            },
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "provisioning": {
                "last_receipt_id": "txn_" + "a" * 26,
                "last_receipt_sha256": "0" * 64,
            },
        }
        with self.assertRaises(R.SchemaError):
            R.validate_tenant_record(payload)

    def test_credential_ref_setter_absent(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        # Neither the record nor the projection is allowed to be non-null.
        self.assertIsNone(rec.payload["credential_ref"])
        # No public API to set it either.
        for name in dir(self.reg):
            self.assertFalse(name.startswith("set_credential"), name)


class CasTests(RegistryBaseTests):
    def test_bump_auth_epoch_monotonic(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        r2 = self.reg.bump_auth_epoch(rec.tenant_id, actor="admin", reason="rot", expected_auth_epoch=1)
        self.assertEqual(r2.auth_epoch, 2)
        self.assertEqual(r2.record_revision, 2)

    def test_bump_auth_epoch_lost_update_rejected(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        self.reg.bump_auth_epoch(rec.tenant_id, actor="admin", reason="rot", expected_auth_epoch=1)
        with self.assertRaises(R.RegistryConflict):
            self.reg.bump_auth_epoch(rec.tenant_id, actor="admin", reason="rot", expected_auth_epoch=1)

    def test_no_direct_set_auth_epoch(self):
        # There must be no `set_auth_epoch` method — only monotonic bump.
        self.assertFalse(hasattr(self.reg, "set_auth_epoch"))
        self.assertFalse(hasattr(self.reg, "reset_auth_epoch"))

    def test_bump_reject_overflow(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        # Manually construct a record whose auth_epoch is one below the ceiling
        # and try to bump beyond 2**53-1.
        with self.assertRaises(R.RegistryError):
            R._monotonic(C.IJSON_MAX_SAFE_INT)

    def test_bump_reject_bool_input(self):
        with self.assertRaises(R.RegistryError):
            R._monotonic(True)


class RecoveryTests(RegistryBaseTests):
    def test_recover_is_no_op_when_clean(self):
        summary = self.reg.recover()
        self.assertEqual(summary["journal_swept"], 0)
        self.assertEqual(summary["uncommitted_rolled_back"], 0)

    def test_recover_cleans_orphans(self):
        # Simulate a mid-atomic-write crash: an orphan temp file.
        with self.layout.registry_fd("tenants") as fd:
            orphan = f"{A.TEMP_PREFIX}foo.deadbeef"
            os.open(orphan, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
        summary = self.reg.recover()
        self.assertGreaterEqual(summary["orphans_removed"], 1)

    def test_recover_rolls_back_uncommitted_tenant_tree(self):
        # Manually write a journal entry with no matching receipt and no
        # tenant record; recovery should remove the empty tenant tree and
        # clean the journal.
        tid = "t_" + "e" * 26
        txn = "txn_" + "f" * 26
        # Create the tenant tree.
        self.layout.tenant(tid).initialize()
        # Write a journal entry.
        journal = {"tenant_id": tid, "txn_id": txn, "action": "tenant_init"}
        with self.layout.registry_fd("provision-journal") as jfd:
            A.write_atomic(
                jfd,
                f"{tid}.{txn}.journal",
                (json.dumps(journal) + "\n").encode("utf-8"),
                exclusive=True,
            )
        summary = self.reg.recover()
        self.assertGreaterEqual(summary["uncommitted_rolled_back"], 1)
        # Tenant tree is now gone.
        self.assertFalse(self.layout.tenant(tid).exists())

    def test_recover_never_touches_active_tenant(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        summary = self.reg.recover()
        # Tenant record still present.
        self.assertEqual(self.reg.get(rec.tenant_id).tenant_id, rec.tenant_id)
        self.assertEqual(summary["uncommitted_rolled_back"], 0)

    def test_recover_refuses_to_touch_populated_tenant_tree(self):
        # Create a tenant, then write an unrelated journal entry pointing at
        # the same tenant.  recover_orphans should NOT delete the tenant
        # data because the record already exists (committed).
        rec, _ = self.reg.init_tenant(actor="admin")
        txn = "txn_" + "g" * 26
        with self.layout.registry_fd("provision-journal") as jfd:
            A.write_atomic(
                jfd,
                f"{rec.tenant_id}.{txn}.journal",
                b"{}\n",
                exclusive=True,
            )
        self.reg.recover()
        # Tenant record still present and unchanged.
        self.assertEqual(self.reg.get(rec.tenant_id).auth_epoch, 1)


class RecordCorruptionTests(RegistryBaseTests):
    def test_corrupt_record_not_treated_as_empty(self):
        rec, _ = self.reg.init_tenant(actor="admin")
        # Corrupt the on-disk file.
        with self.layout.registry_fd("tenants") as rfd:
            A.write_atomic(rfd, f"{rec.tenant_id}.json", b"not-json")
        with self.assertRaises(R.RecordCorruption):
            self.reg.get(rec.tenant_id)
        # list_tenant_ids still returns it (file *exists*), so downstream
        # code cannot silently re-init it.
        self.assertIn(rec.tenant_id, self.reg.list_tenant_ids())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
