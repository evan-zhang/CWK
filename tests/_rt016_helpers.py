"""Shared test helpers for RT-016 (legacy raw shadow importer / reconciler).

This module is *not* a real package (leading underscore + not picked up
by unittest discovery); every RT-016 test imports from it by path.
Every helper here uses only synthetic legacy Markdown fixtures, a
temporary ``CWK_INSTANCE_ROOT``, and the fake RT-015 authority hooks —
no real CWork data, no real credentials.
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
from typing import Any, Callable, Iterable, Optional

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import cwk_access_ledger as AL  # noqa: E402
import cwk_agent_context as AC  # noqa: E402
import cwk_atomic_file as AF  # noqa: E402
import cwk_instance as I  # noqa: E402
import cwk_legacy_raw_import as R16  # noqa: E402
import cwk_migration_reconciler as MR  # noqa: E402
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


def promote_tenant(layout: I.InstanceLayout, tenant_id: str, new_status: str) -> None:
    """Test-only: mutate tenant.status via a raw CAS write.

    RT-012 does not expose a tenant status mutation surface (that's
    RT-018/RT-026's remit); the RT-015 helper uses the same pattern.
    """

    reg = TR.TenantRegistry(layout)
    record = reg.get(tenant_id)
    payload = dict(record.payload)
    payload["status"] = new_status
    with layout.registry_fd("tenants") as fd:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        AF.cas_write(
            fd, f"{tenant_id}.json", body, expected_previous_sha256=record.on_disk_sha256
        )


def sample_frontmatter(
    *,
    report_id: str = "2070001",
    title: str = "Sample Report",
    writer: str = "Zhang San",
    writer_id: str = "u_writer_1",
    create_time: str = "2024-06-15T10:30:00+08:00",
    update_time: str = "2024-06-16T09:00:00+08:00",
    source_lane: str = "inbox",
    extra_lines: Optional[list[str]] = None,
    omit: Iterable[str] = (),
) -> str:
    lines: list[str] = ["---"]
    for key, value in [
        ("report_id", report_id),
        ("title", title),
        ("writer", writer),
        ("writer_id", writer_id),
        ("create_time", create_time),
        ("update_time", update_time),
        ("source_lane", source_lane),
    ]:
        if key in omit:
            continue
        # Wrap in double quotes; the parser strips balanced quotes.
        lines.append(f'{key}: "{value}"')
    if extra_lines:
        lines.extend(extra_lines)
    lines.append("---")
    return "\n".join(lines) + "\n"


def sample_raw(
    *,
    report_id: str = "2070001",
    title: str = "Sample Report",
    body: str = "Body line 1\nBody line 2",
    writer: str = "Zhang San",
    writer_id: str = "u_writer_1",
    create_time: str = "2024-06-15T10:30:00+08:00",
    update_time: str = "2024-06-16T09:00:00+08:00",
    source_lane: str = "inbox",
    replies: Optional[list[dict[str, Any]]] = None,
    nodes: Optional[list[dict[str, Any]]] = None,
    row_extra: Optional[dict[str, Any]] = None,
    omit_frontmatter: Iterable[str] = (),
    replace_body_header: Optional[str] = None,
    extra_frontmatter: Optional[list[str]] = None,
) -> bytes:
    """Build a synthetic legacy Markdown raw file.

    Structure mirrors ``cwk_collect_live.py::write_markdown`` closely:
    frontmatter, an H1 title, then four ``## `` sections in the exact
    order expected by :class:`cwk_legacy_raw_import.LegacyRawDecomposer`.
    """

    row: dict[str, Any] = {
        "reportId": report_id,
        "writeEmpName": writer,
        "writeEmpId": writer_id,
        "createTime": create_time,
        "updateTime": update_time,
        "read": False,
    }
    if row_extra:
        row.update(row_extra)
    simple: dict[str, Any] = {"replyList": replies if replies is not None else []}
    node: dict[str, Any] = {"nodeList": nodes if nodes is not None else []}

    body_header = replace_body_header or "## Original Full Content For AI"

    parts: list[str] = [
        sample_frontmatter(
            report_id=report_id,
            title=title,
            writer=writer,
            writer_id=writer_id,
            create_time=create_time,
            update_time=update_time,
            source_lane=source_lane,
            omit=tuple(omit_frontmatter),
            extra_lines=extra_frontmatter,
        ),
        "",
        f"# {title}",
        "",
        body_header,
        "",
        body,
        "",
        "## List Row Metadata",
        "",
        "```json",
        json.dumps(row, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Record Simple Info",
        "",
        "```json",
        json.dumps(simple, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Node / Opinion Chain",
        "",
        "```json",
        json.dumps(node, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(parts).encode("utf-8")


class Fixture:
    """Full RT-011~015 + RT-016 stack over a temp instance root.

    Callers own the fixture; :meth:`close` restores ``CWK_INSTANCE_ROOT``.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_env = os.environ.get(I.ENV_VAR)
        os.environ[I.ENV_VAR] = str(Path(self._tmp.name).resolve())
        self.layout = I.InstanceLayout.open()
        self.layout.initialize()
        self.tenants = TR.TenantRegistry(self.layout)
        self.evidence = SE.SharedEvidenceStore.open(self.layout)
        self.evidence.initialize()
        self.ledger = AL.AccessLedger(self.layout, self.tenants, self.evidence)
        self.ledger.initialize()
        self.view_store = TV.TenantViewStore(self.layout, self.ledger, self.evidence)
        self.importer = R16.ShadowImporter(
            self.layout, self.tenants, self.evidence, self.ledger, self.view_store
        )
        self.importer.initialize()
        self.reconciler = MR.MigrationReconciler(self.layout, self.importer, self.evidence)

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

    def new_tenant(self, *, status: str = "pilot") -> str:
        tenant, _ = self.tenants.init_tenant(
            actor="admin", reason="rt016-test setup"
        )
        if status != tenant.status:
            promote_tenant(self.layout, tenant.tenant_id, status)
        return tenant.tenant_id


class FakeAuthorityContext:
    """Register RT-015 FakeSigningAuthority for the lifetime of a with-block."""

    def __init__(self, signer_id: str = "rt016_test_signer") -> None:
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

    def sign(self, payload_wo_sig: dict[str, Any]) -> dict[str, Any]:
        payload_bytes = C.canonical_json_bytes(C.nfc_normalize(payload_wo_sig))
        signature = hmac.new(self.secret, payload_bytes, hashlib.sha256).hexdigest()
        signed = dict(payload_wo_sig)
        signed["signature"] = signature
        return signed

    def receipt(
        self,
        *,
        tenant_id: str,
        source_namespace: str,
        report_id: str,
        grant_key: str,
        receipt_type: str = "grant_promote",
        roles: Optional[list[str]] = None,
        visibility_scope: str = "full",
        permission_source: str = "authoritative_permission_api",
        lease_ttl_seconds: int = 600,
    ) -> dict[str, Any]:
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
    raw = secrets.token_bytes(16)
    return base64.b32encode(raw).decode("ascii").lower().rstrip("=")


class RT016TestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def new_run_id(self) -> str:
        return R16.new_run_id()


def default_anchor(
    tenant_id: str,
    *,
    source_namespace: str = "cwork",
    source_kind: str = "current_raw",
    decomposer_version: Optional[str] = None,
    normalizer_version: Optional[str] = None,
) -> "MR.ReconciliationAnchor":
    """Build a ReconciliationAnchor keyed off the default test tuple.

    Every RT-016 test that used the pre-anchor ``reconcile(tenant_id=...)``
    signature previously implied ``source_namespace='cwork'`` +
    ``source_kind='current_raw'`` + the module's default
    ``decomposer_version`` / ``normalizer_version``.  This helper
    preserves that intent explicitly.
    """

    return MR.ReconciliationAnchor(
        tenant_id=tenant_id,
        source_namespace=source_namespace,
        source_kind=source_kind,
        decomposer_version=decomposer_version or R16.DECOMPOSER_VERSION,
        normalizer_version=normalizer_version or R16.NORMALIZER_VERSION,
    )


def build_legacy_tree(tmpdir: Path, files: dict[str, bytes]) -> Path:
    """Materialise a synthetic legacy tree; return the root.

    ``files`` maps relative POSIX paths (using ``/`` separators, no
    leading slash) to raw bytes.  Parents are created lazily; every
    file is created with mode ``0o600``.
    """

    root = tmpdir / "legacy-raw"
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o600)
    return root


__all__ = [
    "AC",
    "AF",
    "AL",
    "C",
    "FakeAuthorityContext",
    "Fixture",
    "I",
    "MR",
    "R16",
    "RT016TestBase",
    "SE",
    "TR",
    "TV",
    "build_legacy_tree",
    "default_anchor",
    "promote_tenant",
    "sample_frontmatter",
    "sample_raw",
    "utc_iso",
]
