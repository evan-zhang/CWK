"""Shared test helpers for the RT-015 access-ledger / tenant-view suites.

Intentionally not a real package (leading underscore + not in test discovery)
— test files simply import from this module by path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_atomic_file as AF  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_shared_evidence as SE  # noqa: E402
import cwk_tenant_registry as TR  # noqa: E402
import cwk_tenant_view as TV  # noqa: E402


def utc_iso(offset_seconds: int = 0) -> str:
    import datetime as _dt
    dt = _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0)
    if offset_seconds:
        dt += _dt.timedelta(seconds=offset_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def fixed_iso(hour: int = 10) -> str:
    return f"2026-08-01T{hour:02d}:00:00Z"


def promote_tenant(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    """Test-only: bypass RT-012 to promote tenant status.

    RT-012 exposes no status-mutation surface (that lives in RT-018/026);
    tests use a raw CAS write.  Aligned with tests/test_rt013_binding.py.
    """

    reg = TR.TenantRegistry(layout)
    record = reg.get(tenant_id)
    payload = dict(record.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        AF.cas_write(fd, f"{tenant_id}.json", body, expected_previous_sha256=record.on_disk_sha256)


def canonical_envelope(
    *,
    source_namespace: str = "cwork",
    report_id: str = "2070001",
    body: str = "汇报正文-α",
    title: str = "汇报-1",
) -> dict[str, Any]:
    envelope = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": source_namespace,
        "report_id": report_id,
        "title": title,
        "author": {"source_user_id": "u_writer_1", "display_name": "张三"},
        "created_at": fixed_iso(10),
        "source_updated_at": fixed_iso(12),
        "body": body,
        "normalizer_version": "v1",
    }
    envelope["canonical_sha256"] = C.canonical_sha256(
        {k: v for k, v in envelope.items() if k != "canonical_sha256"}
    )
    return envelope


def observation(
    *,
    tenant_id: str,
    source_namespace: str = "cwork",
    report_id: str = "2070001",
    initial_status: str = "granted",
    observation_source: str = "tenant_appkey_observation",
    roles: Optional[list[str]] = None,
    visibility_scope: str = "full",
    evidence_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "schema": "cwk.access_observation.v1",
        "tenant_id": tenant_id,
        "source_namespace": source_namespace,
        "report_id": report_id,
        "observed_at": utc_iso(),
        "observation_source": observation_source,
        "roles": roles if roles is not None else ["receiver"],
        "visibility_scope": visibility_scope,
        "initial_status": initial_status,
        "evidence_refs": evidence_refs or ["observation:sample"],
    }


class LedgerFixture:
    """TemporaryDirectory + fully-initialised Instance/Registry/Store/Ledger.

    Callers own the fixture; :meth:`close` restores CWK_INSTANCE_ROOT.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_env = os.environ.get(I.ENV_VAR)
        os.environ[I.ENV_VAR] = str(Path(self._tmp.name).resolve())
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenants = TR.TenantRegistry(self.layout)
        self.evidence = SE.SharedEvidenceStore.open(self.layout).initialize
        # `.initialize` is bound method above; call it to get None then keep the
        # store instance.
        self.evidence = SE.SharedEvidenceStore.open(self.layout)
        self.evidence.initialize()
        self.ledger = AL.AccessLedger(self.layout, self.tenants, self.evidence)
        self.ledger.initialize()
        self.view_store = TV.TenantViewStore(self.layout, self.ledger, self.evidence)

    def close(self) -> None:
        self.layout.close()
        if self._prev_env is None:
            os.environ.pop(I.ENV_VAR, None)
        else:
            os.environ[I.ENV_VAR] = self._prev_env
        self._tmp.cleanup()

    @property
    def root(self) -> Path:
        return Path(self._tmp.name)

    def new_tenant(self, *, status: str = "active") -> str:
        tenant, _ = self.tenants.init_tenant(actor="admin", reason="rt015-test setup")
        if status != tenant.status:
            promote_tenant(self.layout, tenant.tenant_id, status)
        return tenant.tenant_id

    def publish(self, envelope: dict[str, Any]) -> SE.PublishReceipt:
        return self.evidence.publish(envelope)

    def snapshot(
        self,
        tenant_id: str,
        *,
        agent_id_hash: Optional[str] = None,
        binding_epoch: int = 1,
        binding_secret_epoch: int = 1,
        source: str = "gateway_authenticated_context",
        override_tenant_auth_epoch: Optional[int] = None,
        override_tenant_status: Optional[str] = None,
    ) -> AC.AgentContextSnapshot:
        tenant = self.tenants.get(tenant_id)
        return AC.AgentContextSnapshot(
            agent_id_hash=(agent_id_hash or ("a" * 64)),
            tenant_id=tenant_id,
            tenant_auth_epoch=(
                override_tenant_auth_epoch
                if override_tenant_auth_epoch is not None
                else tenant.auth_epoch
            ),
            binding_epoch=binding_epoch,
            binding_secret_epoch=binding_secret_epoch,
            tenant_status=(
                override_tenant_status if override_tenant_status is not None else tenant.status
            ),
            resolved_at=utc_iso(),
        )


class FakeAuthorityContext:
    """Register the ``FakeSigningAuthority`` for the lifetime of a with-block."""

    def __init__(self, signer_id: str = "test_signer") -> None:
        self.signer_id = signer_id
        self.secret = secrets.token_bytes(32)

    def __enter__(self) -> "FakeAuthorityContext":
        AL._register_test_authority(
            AL.FakeSigningAuthority(), token=AL._TEST_AUTHORITY_TOKEN
        )
        AL._register_fake_signer(
            self.signer_id, self.secret, token=AL._TEST_AUTHORITY_TOKEN
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        AL._unregister_fake_signer(self.signer_id, token=AL._TEST_AUTHORITY_TOKEN)
        AL._unregister_test_authority(token=AL._TEST_AUTHORITY_TOKEN)

    def sign(self, receipt_wo_sig: dict[str, Any]) -> dict[str, Any]:
        payload_bytes = C.canonical_json_bytes(C.nfc_normalize(receipt_wo_sig))
        signature = hmac.new(self.secret, payload_bytes, hashlib.sha256).hexdigest()
        signed = dict(receipt_wo_sig)
        signed["signature"] = signature
        return signed

    def receipt(
        self,
        *,
        tenant_id: str,
        source_namespace: str = "cwork",
        report_id: str = "2070001",
        receipt_type: str = "grant_promote",
        roles: Optional[list[str]] = None,
        visibility_scope: str = "full",
        permission_source: str = "authoritative_permission_api",
        lease_ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        grant_key = AL.compute_grant_key(
            tenant_id, C.compose_report_key(source_namespace, report_id)
        )
        wo_sig = {
            "schema": "cwk.rt015.authority_receipt.v1",
            "receipt_id": "ar_" + _rand_id_tail(),
            "signer_id": self.signer_id,
            "receipt_type": receipt_type,
            "tenant_id": tenant_id,
            "source_namespace": source_namespace,
            "report_id": report_id,
            "grant_key": grant_key,
            "roles": roles if roles is not None else ["receiver"],
            "visibility_scope": visibility_scope,
            "permission_source": permission_source,
            "issued_at": utc_iso(),
            "lease_expires_at": utc_iso(lease_ttl_seconds),
        }
        return self.sign(wo_sig)


def _rand_id_tail() -> str:
    """26-char base32 tail matching the frozen ``o_/g_/rv_/ev_/ar_`` regex."""

    raw = secrets.token_bytes(16)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return encoded


class LedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = LedgerFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _grant_flow_to_active(
        self,
        *,
        tenant_id: str,
        signer: FakeAuthorityContext,
        source_namespace: str = "cwork",
        report_id: str = "2070001",
        publish_canonical: bool = True,
    ) -> AL.GrantRecord:
        if publish_canonical:
            self.fx.publish(
                canonical_envelope(
                    source_namespace=source_namespace, report_id=report_id
                )
            )
        obs = observation(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
        )
        self.fx.ledger.observe(observation=obs, actor="admin", reason="ingest")
        receipt = signer.receipt(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
        )
        rec = self.fx.ledger.promote_to_active(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            authority_receipt=receipt,
            actor="admin",
            reason="promote",
            lease_ttl_seconds=600,
        )
        return rec
