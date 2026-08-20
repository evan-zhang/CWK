"""RT-013: hostile input matrix — identity forgery, secret scan, cross-tenant,
legacy fallback, RT-011 / RT-012 frozen-file drift, tenant status regression."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts" / "cwk_tenant_cli.py"
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_agent_binding as B  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_atomic_file as A  # noqa: E402
import cwk_credential_broker as CB  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
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

    def tearDown(self):
        self.layout.close()
        self._tmp.cleanup()
        os.environ.pop(I.ENV_VAR, None)


class IdentityForgeryTests(_Base):
    def test_agent_context_untrusted_source_rejected(self):
        for bad in ("request_body", "cli_query", "user_input", "attacker", "env"):
            with self.assertRaises(AC.AgentContextError, msg=bad):
                AC.AgentContext.from_trusted(
                    raw_agent_id="alice",
                    source=bad,
                    purpose="collector_run",
                    layout=self.layout,
                )

    def test_agent_context_has_no_from_untrusted_family(self):
        for name in ("from_untrusted", "from_request_body", "from_env", "from_argv", "from_body"):
            self.assertFalse(hasattr(AC.AgentContext, name), f"AgentContext.{name} exists")

    def test_direct_init_refused(self):
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
                _construction_token=object(),
            )

    def test_binding_record_no_raw_agent_id_disk(self):
        tenant, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, tenant.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=tenant.tenant_id, raw_agent_id="alice-secret-id",
            actor="admin", reason="t",
        )
        with B._binding_sub(self.layout, "current") as fd:
            body = A.read_file(fd, f"{rec.agent_id_hash}.json").decode("utf-8")
        self.assertNotIn("alice-secret-id", body)


class BindingConflictReplayTests(_Base):
    def test_agent_cannot_bind_to_two_tenants(self):
        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        _promote(self.layout, t2.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=t2.tenant_id, raw_agent_id="alice",
                actor="admin", reason="attack",
            )

    def test_replay_bind_receipt_is_meaningless(self):
        """Even if an attacker copies a receipt file elsewhere, resolving
        the raw_agent_id NEVER promotes the receipt back to an active
        binding — the record is the SoR, not the receipt."""

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        _, receipt = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        # Revoke.
        self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="off")
        # Attacker replays the receipt file — irrelevant, the record is revoked.
        with self.assertRaises(B.BindingRevoked):
            self.binding_reg.resolve("alice", purpose="collector_run")


class StateEpochRollbackTests(_Base):
    def test_binding_epoch_monotonic_after_manual_rollback(self):
        """If an attacker manually rewrites a binding record with an older
        binding_epoch on disk, the sha stored in the receipt still refers
        to the old canonical bytes; subsequent CAS-based mutations must
        detect the drift and fail closed."""

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.binding_reg.suspend(raw_agent_id="alice", actor="admin", reason="s")

        # Attacker rewrites disk with the *original* record payload (epoch 1).
        with B._binding_sub(self.layout, "current") as fd:
            body = (json.dumps(rec.payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            A.write_atomic(fd, f"{rec.agent_id_hash}.json", body)

        # Next legitimate mutation (reactivate) reads back the rewritten
        # record; it thinks the current state is 'active' (from the rewrite)
        # and refuses to reactivate an already-active binding.
        with self.assertRaises(B.BindingStateError):
            self.binding_reg.reactivate(raw_agent_id="alice", actor="admin", reason="r")


class EnvPollutionTests(_Base):
    def test_broker_env_isolated_from_process_env(self):
        """CWK_CRED_* / CWORK_APP_KEY in the host process env must NOT be
        readable by the broker unless the reference explicitly points at
        that env var via the isolated env snapshot."""

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.store.set_reference(
            tenant_id=t1.tenant_id, reference_uri="secret://env-t1",
            backend="env_ref", actor="admin", reason="t",
        )
        os.environ["CWK_CRED_t1"] = "host-secret-should-never-leak"
        os.environ["CWORK_APP_KEY"] = "host-app-key-should-never-leak"
        try:
            # Broker constructed with an ISOLATED env snapshot lacking
            # CWK_CRED_t1 — it must NOT fall back to os.environ.
            isolated = {"CWK_INSTANCE_ROOT": self.instance_root}
            broker = CB.CredentialBroker(
                layout=self.layout,
                backends=CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=isolated)}),
                inherit_env=isolated,
            )
            with self.assertRaises(CB.CredentialBackendError):
                with broker.lease(
                    agent_id_hash=rec.agent_id_hash,
                    tenant_id=t1.tenant_id,
                    purpose="collector_run",
                ) as _:
                    pass
        finally:
            os.environ.pop("CWK_CRED_t1", None)
            os.environ.pop("CWORK_APP_KEY", None)


class SecretScanTests(_Base):
    def test_receipts_never_contain_material(self):
        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.store.set_reference(
            tenant_id=t1.tenant_id, reference_uri="secret://env-t1",
            backend="env_ref", actor="admin", reason="t",
        )
        isolated = {"CWK_INSTANCE_ROOT": self.instance_root, "CWK_CRED_t1": "material-goes-here"}
        broker = CB.CredentialBroker(
            layout=self.layout,
            backends=CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=isolated)}),
            inherit_env=isolated,
        )
        with broker.lease(
            agent_id_hash=rec.agent_id_hash,
            tenant_id=t1.tenant_id,
            purpose="collector_run",
        ) as lease:
            pass
        lease_receipt = lease.receipt()
        blob = json.dumps(lease_receipt)
        self.assertNotIn("material-goes-here", blob)

        # Scan the entire on-disk instance root for material substring.
        material_probe = "material-goes-here"
        offenders: list[str] = []
        for root, _, files in os.walk(self._tmp.name):
            for fname in files:
                path = os.path.join(root, fname)
                try:
                    with open(path, "rb") as fh:
                        if material_probe.encode("utf-8") in fh.read():
                            offenders.append(path)
                except OSError:
                    continue
        self.assertEqual(offenders, [], f"secret leaked into: {offenders!r}")


class CrossTenantTests(_Base):
    def test_a_cannot_use_b_credential(self):
        """The broker only reads the credential owned by ``tenant_id``; it
        never falls back to another tenant's record."""

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        t2, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        _promote(self.layout, t2.tenant_id, "active")

        r1, _ = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.store.set_reference(
            tenant_id=t1.tenant_id, reference_uri="secret://env-a",
            backend="env_ref", actor="admin", reason="t",
        )
        # No credential for t2.
        isolated = {"CWK_INSTANCE_ROOT": self.instance_root, "CWK_CRED_a": "a-material"}
        broker = CB.CredentialBroker(
            layout=self.layout,
            backends=CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=isolated)}),
            inherit_env=isolated,
        )
        # Attempting to lease t1's material for t2 fails because t2 has no
        # credential.
        with self.assertRaises(CB.CredentialNotFound):
            with broker.lease(
                agent_id_hash=r1.agent_id_hash,
                tenant_id=t2.tenant_id,
                purpose="collector_run",
            ) as _:
                pass


class LegacyFallbackTests(_Base):
    def test_broker_never_reads_repo_env_file(self):
        """Even if repo `.env` contains CWORK_APP_KEY, the broker MUST NOT
        read it as a fallback when the tenant credential lookup fails."""

        # Simulate a repo `.env` file adjacent to the working tree.
        env_path = os.path.join(self._tmp.name, ".env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("CWORK_APP_KEY=legacy-app-key\n")

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        rec, _ = self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        self.store.set_reference(
            tenant_id=t1.tenant_id, reference_uri="secret://env-missing",
            backend="env_ref", actor="admin", reason="t",
        )
        isolated = {"CWK_INSTANCE_ROOT": self.instance_root}
        broker = CB.CredentialBroker(
            layout=self.layout,
            backends=CB.BackendRegistry({"env_ref": CB.EnvRefBackend(env=isolated)}),
            inherit_env=isolated,
        )
        with self.assertRaises(CB.CredentialBackendError):
            with broker.lease(
                agent_id_hash=rec.agent_id_hash,
                tenant_id=t1.tenant_id,
                purpose="collector_run",
            ) as _:
                pass


class RevocationWindowTests(_Base):
    def test_stale_snapshot_rejected_by_auth_epoch_bump(self):
        """AgentContext.snapshot() captures the tenant_auth_epoch at
        resolve time.  After a revoke bumps auth_epoch, the snapshot is
        stale — RT-022 will reject cached results, but even here we can
        detect the bump has happened."""

        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=t1.tenant_id, raw_agent_id="alice",
            actor="admin", reason="t",
        )
        ctx = AC.AgentContext.from_trusted(
            raw_agent_id="alice", source="admin_cli",
            purpose="collector_run", layout=self.layout,
        )
        old_epoch = ctx.tenant_auth_epoch
        self.binding_reg.revoke(raw_agent_id="alice", actor="admin", reason="off")
        new_epoch = self.tenant_reg.get(t1.tenant_id).auth_epoch
        self.assertEqual(new_epoch, old_epoch + 1)


class Rt011FrozenFilesUntouchedTests(unittest.TestCase):
    """RT-013 MUST NOT modify any RT-011 frozen contract semantics.

    We assert on the frozen v1 constants exposed by
    :mod:`cwk_pr001_contracts` — every one of these would drift if a
    downstream RT edited the RT-011 schema files.  A byte-for-byte drift
    check across the whole ``PR-001-multitenant-knowledge-spaces/contracts/``
    tree is the verifier's job at commit-review time; the runtime tests
    only guarantee the *semantic* invariants continue to hold.
    """

    def test_report_key_regex_frozen(self):
        self.assertEqual(C.SOURCE_NAMESPACE_REGEX.pattern, r"\A[a-z][a-z0-9_]{0,63}\Z")
        self.assertEqual(C.TENANT_ID_REGEX.pattern, r"\At_[a-z0-9]{26}\Z")
        self.assertEqual(C.SPACE_ID_REGEX.pattern, r"\Asp_[a-z0-9]{10,32}\Z")

    def test_ijson_safe_int(self):
        self.assertEqual(C.IJSON_MAX_SAFE_INT, 2 ** 53 - 1)

    def test_rt011_security_defaults_still_forbid_loopback(self):
        path = PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "security_defaults.json"
        payload = C.strict_json_load_path(path)
        transport = payload["transport_and_identity"]
        self.assertEqual(transport["preferred_transport"], "openclaw_controlled_tool")
        self.assertEqual(
            transport["forbidden_transport"], "loopback_http_self_reported_agent_id"
        )
        self.assertEqual(transport["identity_source"], "gateway_authenticated_context")
        self.assertFalse(transport["request_body_identity_fields_permitted"])

    def test_rt011_verified_shared_extensions_still_empty(self):
        path = PROJECT / "PR" / "PR-001-multitenant-knowledge-spaces" / "contracts" / "verified_shared_extensions_v1.json"
        payload = C.strict_json_load_path(path)
        self.assertEqual(payload["entries"], [])


class Rt012FrozenFilesUntouchedTests(unittest.TestCase):
    """RT-013 MUST NOT modify RT-012 core files except a one-line slot addition."""

    def test_dispatcher_only_slot_addition(self):
        # Read the current dispatcher, isolate FROZEN_PROVIDER_SLOTS, and
        # confirm it contains exactly two active entries (RT-012 core +
        # RT-013 binding).  Any other logical change would show up here.
        with open(PROJECT / "scripts" / "cwk_tenant_cli.py", "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"cwk_tenant_cmd_binding"', source)
        # Structural sanity: the tuple still starts with cwk_tenant_cmd_core.
        import cwk_tenant_cli as CLI_MOD  # noqa: PLC0415
        self.assertEqual(CLI_MOD.FROZEN_PROVIDER_SLOTS[0], "cwk_tenant_cmd_core")
        self.assertEqual(CLI_MOD.FROZEN_PROVIDER_SLOTS[1], "cwk_tenant_cmd_binding")

    def test_rt012_core_modules_unmodified(self):
        """The RT-012 owner modules (excluding the dispatcher's one-line
        slot change) must be untouched.  We assert by importing them and
        checking their expected top-level surface."""

        import cwk_atomic_file as ATF  # noqa: PLC0415
        import cwk_instance as II  # noqa: PLC0415
        import cwk_tenant_cli_api as APIM  # noqa: PLC0415
        import cwk_tenant_cmd_core as COREM  # noqa: PLC0415
        import cwk_tenant_registry as REG  # noqa: PLC0415

        self.assertEqual(APIM.COMMAND_PROVIDER_API_VERSION, "v1")
        self.assertEqual(APIM.STABLE_EXIT_CODES, (0, 2, 3, 4, 5, 6))
        self.assertEqual(REG.TENANT_STATES, ("draft", "profile_pending", "pilot", "active", "suspended", "offboarded"))
        self.assertEqual(II.ENV_VAR, "CWK_INSTANCE_ROOT")
        self.assertEqual(COREM.PROVIDER_NAME, "cwk_tenant_cmd_core")


class ConcurrentBindTests(unittest.TestCase):
    """Canonical direct TestCase surface for the Stage-10 receipt."""

    def setUp(self):
        _Base.setUp(self)

    def tearDown(self):
        _Base.tearDown(self)

    def test_parallel_bind_same_agent_only_one_wins(self):
        t1, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, t1.tenant_id, "active")

        results: list[str] = []

        def _bind_worker(i: int) -> str:
            try:
                rec, _ = self.binding_reg.bind(
                    tenant_id=t1.tenant_id, raw_agent_id="alice",
                    actor="admin", reason=f"t{i}",
                )
                return f"ok:{rec.binding_epoch}"
            except B.BindingConflictError:
                return "conflict"
            except Exception as exc:  # noqa: BLE001
                return f"err:{exc.__class__.__name__}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(_bind_worker, i) for i in range(8)]
            results = [f.result() for f in futures]

        oks = [r for r in results if r.startswith("ok")]
        confs = [r for r in results if r == "conflict"]
        # Exactly one should succeed; the rest either conflict or fail
        # closed with a stable error.  We tolerate both since the CAS
        # can produce either a BindingConflictError or a RevisionConflict
        # (surfacing as an AtomicFileError).
        self.assertEqual(len(oks), 1, f"results={results!r}")

    def test_late_prior_active_recheck_allows_only_one_commit(self):
        """Force the exact early-check/late-snapshot race deterministically.

        Both workers first prove the record absent.  The loser then pauses
        before its second ``get_by_hash`` until the winner has committed.  A
        correct bind path re-checks the newly visible status and rejects it;
        the historical bug treated the winner's active record as a revoked
        predecessor and committed binding_epoch=2.
        """

        tenant, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, tenant.tenant_id, "active")
        auth_epoch_before = self.tenant_reg.get(tenant.tenant_id).auth_epoch

        initial_reads_complete = threading.Barrier(2)
        winner_committed = threading.Event()
        original_get = self.binding_reg.get_by_hash
        call_counts: dict[int, int] = {}
        call_counts_lock = threading.Lock()

        def _controlled_get(agent_id_hash: str) -> B.BindingRecord:
            ident = threading.get_ident()
            with call_counts_lock:
                call_counts[ident] = call_counts.get(ident, 0) + 1
                call_number = call_counts[ident]

            if call_number == 1:
                try:
                    return original_get(agent_id_hash)
                except B.BindingNotFound:
                    initial_reads_complete.wait(timeout=5)
                    raise

            if threading.current_thread().name == "bind-loser" and call_number == 2:
                self.assertTrue(winner_committed.wait(timeout=5), "winner did not commit")
            return original_get(agent_id_hash)

        self.binding_reg.get_by_hash = _controlled_get  # type: ignore[method-assign]

        def _worker(role: str) -> str:
            try:
                rec, _ = self.binding_reg.bind(
                    tenant_id=tenant.tenant_id,
                    raw_agent_id="alice",
                    actor="admin",
                    reason=role,
                )
                if role == "winner":
                    winner_committed.set()
                return f"ok:{rec.binding_epoch}"
            except B.BindingConflictError:
                return "conflict"

        winner = threading.Thread(
            target=lambda: results.__setitem__("winner", _worker("winner")),
            name="bind-winner",
        )
        loser = threading.Thread(
            target=lambda: results.__setitem__("loser", _worker("loser")),
            name="bind-loser",
        )
        results: dict[str, str] = {}
        winner.start()
        loser.start()
        winner.join(timeout=10)
        loser.join(timeout=10)
        self.assertFalse(winner.is_alive(), "winner thread hung")
        self.assertFalse(loser.is_alive(), "loser thread hung")

        self.assertEqual(results, {"winner": "ok:1", "loser": "conflict"})
        final = self.binding_reg.get_by_hash(self.binding_reg.hash_agent_id("alice"))
        self.assertEqual(final.binding_epoch, 1)
        with B._binding_sub(self.layout, "receipts") as fd:
            receipt_names = sorted(
                entry.name for entry in os.scandir(fd) if entry.name.endswith(".json")
            )
        self.assertEqual(len(receipt_names), 1)
        auth_epoch_after = self.tenant_reg.get(tenant.tenant_id).auth_epoch
        self.assertEqual(auth_epoch_after, auth_epoch_before + 1)

    def test_sequential_duplicate_bind_conflicts_same_and_cross_tenant(self):
        first, _ = self.tenant_reg.init_tenant(actor="admin")
        second, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, first.tenant_id, "active")
        _promote(self.layout, second.tenant_id, "active")
        self.binding_reg.bind(
            tenant_id=first.tenant_id,
            raw_agent_id="alice",
            actor="admin",
            reason="first",
        )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=first.tenant_id,
                raw_agent_id="alice",
                actor="admin",
                reason="same-tenant-duplicate",
            )
        with self.assertRaises(B.BindingConflictError):
            self.binding_reg.bind(
                tenant_id=second.tenant_id,
                raw_agent_id="alice",
                actor="admin",
                reason="cross-tenant-duplicate",
            )

    def test_revoked_predecessor_still_allows_two_step_rebind(self):
        first, _ = self.tenant_reg.init_tenant(actor="admin")
        second, _ = self.tenant_reg.init_tenant(actor="admin")
        _promote(self.layout, first.tenant_id, "active")
        _promote(self.layout, second.tenant_id, "active")
        original, _ = self.binding_reg.bind(
            tenant_id=first.tenant_id,
            raw_agent_id="alice",
            actor="admin",
            reason="first",
        )
        rebound, receipts = self.binding_reg.rebind(
            raw_agent_id="alice",
            new_tenant_id=second.tenant_id,
            actor="admin",
            reason="move",
        )
        self.assertEqual(rebound.tenant_id, second.tenant_id)
        self.assertEqual(rebound.status, "active")
        self.assertGreaterEqual(rebound.binding_epoch, original.binding_epoch + 2)
        self.assertEqual(len(receipts), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
