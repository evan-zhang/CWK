"""VG-A helpers — synthesis-only fixtures for the VG-A integration gate.

Owned by VG-A synthesis (PR/PR-001/plans/开发计划.md §8).  These helpers
compose the RT-011~RT-015 public APIs into a two-tenant integration
fixture.  Nothing here modifies RT-011~RT-015 modules, schemas, tests,
docs, or the RT index.  Everything runs inside an ephemeral
``CWK_INSTANCE_ROOT`` created via :class:`tempfile.TemporaryDirectory`.

Rules explicitly followed (see task brief):

- No real ``CWORK_APP_KEY``, no real Work-collab, no Cloud/DocDB, no
  real Gateway, no cron.
- No HTTP / CLI / object-enumeration surface introduced.
- The only ``AuthorityAdapter`` in use is the module-local
  ``FakeSigningAuthority`` from RT-015; the fail-closed default is
  covered by the negative branches.
- ``AgentContextSnapshot`` is constructed via the VG-A test fixture
  helpers (mirroring ``tests/_rt015_helpers.py``), never via a
  request body path.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_atomic_file as AF  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_pr001_contracts as C  # noqa: E402
import cwk_shared_evidence as SE  # noqa: E402
import cwk_tenant_registry as TR  # noqa: E402
import cwk_tenant_view as TV  # noqa: E402


_UTC = _dt.timezone.utc


def utc_iso(offset_seconds: int = 0) -> str:
    dt = _dt.datetime.now(tz=_UTC).replace(microsecond=0)
    if offset_seconds:
        dt += _dt.timedelta(seconds=offset_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def fixed_iso(hour: int = 10) -> str:
    return f"2026-08-05T{hour:02d}:00:00Z"


def promote_tenant_status(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    """Test-only: CAS-rewrite the tenant record to promote ``status``.

    RT-012 exposes no public status-mutation surface (that lives in
    later RTs).  VG-A mirrors the same helper used by RT-013 / RT-015
    tests.
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
    report_id: str = "3080001",
    body: str = "VG-A shared canonical body α",
    title: str = "VG-A 汇报-1",
    author_id: str = "u_shared_writer",
    author_name: str = "共享作者",
    created_hour: int = 10,
    updated_hour: int = 12,
) -> dict[str, Any]:
    envelope = {
        "schema": "cwk.canonical_report.v1",
        "source_namespace": source_namespace,
        "report_id": report_id,
        "title": title,
        "author": {"source_user_id": author_id, "display_name": author_name},
        "created_at": fixed_iso(created_hour),
        "source_updated_at": fixed_iso(updated_hour),
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
    report_id: str = "3080001",
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
        "evidence_refs": evidence_refs or ["vga:observation:sample"],
    }


def view_envelope(
    *,
    tenant_id: str,
    source_namespace: str = "cwork",
    report_id: str = "3080001",
    canonical_sha256: str,
    lane: str = "received",
    include_reply: bool = True,
    include_node: bool = True,
    include_attachment_temp_url: bool = False,
) -> dict[str, Any]:
    env: dict[str, Any] = {
        "schema": "cwk.tenant_view.v1",
        "tenant_id": tenant_id,
        "report_key": C.compose_report_key(source_namespace, report_id),
        "canonical_sha256": canonical_sha256,
        "lane": lane,
        "read_status": "unread",
        "todo_status": "pending",
        "new_reply_flag": False,
        "roles": ["receiver"],
        "allowed_actions": ["read"],
        "visible_event_ids": [],
        "attachment_permissions": [],
        "reply_overlay": [],
        "node_overlay": [],
        "observed_at": utc_iso(),
    }
    if include_reply:
        env["reply_overlay"] = [
            {"reply_id": f"r-{tenant_id[:6]}-1", "content_sha256": "a" * 64, "visible": True}
        ]
    if include_node:
        env["node_overlay"] = [
            {
                "node_id": f"n-{tenant_id[:6]}-1",
                "type": "approval",
                "visible": True,
                "content_sha256": "b" * 64,
            }
        ]
    if include_attachment_temp_url:
        env["attachment_permissions"] = [
            {
                "attachment_id": f"att-{tenant_id[:6]}-1",
                "permission": "view",
                "temporary_url": "https://presign.vga.example/x?token=synthesised-fake-token-abcdef",
                "expires_at": "2026-08-19T05:00:00Z",
            }
        ]
    return env


@dataclass
class _RegisteredSigner:
    signer_id: str
    secret: bytes


class SyntheticAuthorityContext:
    """VG-A-scoped fake authority context.

    Register the ``FakeSigningAuthority`` and a fake HMAC signer for
    the lifetime of a ``with`` block.  Each context uses an independent
    secret so cross-tenant receipt-substitution tests can build two
    authority scopes if needed.
    """

    def __init__(self, signer_id: str = "vga_synthetic_signer") -> None:
        self.signer_id = signer_id
        self.secret = secrets.token_bytes(32)

    def __enter__(self) -> "SyntheticAuthorityContext":
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
        report_id: str = "3080001",
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
    """26-char base32 tail matching frozen ``o_/g_/rv_/ev_/ar_`` regex."""

    raw = secrets.token_bytes(16)
    return base64.b32encode(raw).decode("ascii").lower().rstrip("=")


class TwoTenantFixture:
    """Synthetic two-tenant integration harness for VG-A.

    Provides:

    - Ephemeral ``CWK_INSTANCE_ROOT`` beneath ``tempfile.mkdtemp``.
    - Initialised RT-012 layout / registry, RT-014 SharedEvidenceStore,
      RT-015 AccessLedger + TenantViewStore.
    - Two tenants ``a_id`` / ``b_id`` provisioned & promoted to
      ``active`` for the data-plane tests.
    - Helpers to produce fresh ``AgentContextSnapshot`` per tenant.

    The fixture DOES NOT swap in the synthetic authority — callers wrap
    ``with SyntheticAuthorityContext() as auth:`` around the section
    that needs a promote / refresh receipt.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="vga-instance-")
        self._prev_env = os.environ.get(I.ENV_VAR)
        os.environ[I.ENV_VAR] = self._tmp.name
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenants = TR.TenantRegistry(self.layout)
        self.evidence = SE.SharedEvidenceStore.open(self.layout)
        self.evidence.initialize()
        self.ledger = AL.AccessLedger(self.layout, self.tenants, self.evidence)
        self.ledger.initialize()
        self.view_store = TV.TenantViewStore(self.layout, self.ledger, self.evidence)
        self.a_id = self._new_tenant(status="active")
        self.b_id = self._new_tenant(status="active")

    def close(self) -> None:
        if self._prev_env is None:
            os.environ.pop(I.ENV_VAR, None)
        else:
            os.environ[I.ENV_VAR] = self._prev_env
        self._tmp.cleanup()

    @property
    def root(self) -> Path:
        return Path(self._tmp.name)

    def _new_tenant(self, *, status: str = "active") -> str:
        tenant, _ = self.tenants.init_tenant(actor="vga-admin", reason="vga-setup")
        if status != tenant.status:
            promote_tenant_status(self.layout, tenant.tenant_id, status)
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
            agent_id_hash=(agent_id_hash or ("v" * 64)),
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

    def promote_grant(
        self,
        *,
        tenant_id: str,
        signer: SyntheticAuthorityContext,
        source_namespace: str = "cwork",
        report_id: str = "3080001",
        lease_ttl_seconds: int = 600,
    ) -> AL.GrantRecord:
        obs = observation(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
        )
        self.ledger.observe(observation=obs, actor="vga-admin", reason="vga-observe")
        receipt = signer.receipt(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        return self.ledger.promote_to_active(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            authority_receipt=receipt,
            actor="vga-admin",
            reason="vga-promote",
            lease_ttl_seconds=lease_ttl_seconds,
        )

    def upsert_view(
        self,
        *,
        tenant_id: str,
        canonical_sha256: str,
        source_namespace: str = "cwork",
        report_id: str = "3080001",
        include_attachment_temp_url: bool = False,
    ) -> TV.ViewRecord:
        snap = self.snapshot(tenant_id)
        view = view_envelope(
            tenant_id=tenant_id,
            source_namespace=source_namespace,
            report_id=report_id,
            canonical_sha256=canonical_sha256,
            include_attachment_temp_url=include_attachment_temp_url,
        )
        return self.view_store.upsert_overlay(snapshot=snap, view_envelope=view)


class VgaTestBase(unittest.TestCase):
    """Base class that constructs / tears down the two-tenant fixture."""

    def setUp(self) -> None:
        self.fx = TwoTenantFixture()

    def tearDown(self) -> None:
        self.fx.close()


__all__ = [
    "AC",
    "AF",
    "AL",
    "C",
    "I",
    "SE",
    "TR",
    "TV",
    "SyntheticAuthorityContext",
    "TwoTenantFixture",
    "VgaTestBase",
    "canonical_envelope",
    "fixed_iso",
    "observation",
    "promote_tenant_status",
    "utc_iso",
    "view_envelope",
]
