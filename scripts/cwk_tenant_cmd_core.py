#!/usr/bin/env python3
"""RT-012 core CLI provider: ``init``, ``show``, ``list``, ``doctor``, ``state-graph``.

Owned by RT-012.  Downstream RTs contribute *their own* provider modules
(``cwk_tenant_cmd_binding``, ``cwk_tenant_cmd_profile``,
``cwk_tenant_cmd_release``) and MUST NOT edit this file.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import stat as stat_module
from typing import Any, Sequence

import cwk_atomic_file as A
import cwk_instance as I
import cwk_tenant_cli_api as API
import cwk_tenant_registry as R

API_VERSION = API.COMMAND_PROVIDER_API_VERSION
PROVIDER_NAME = "cwk_tenant_cmd_core"
PROVIDER_VERSION = "v1"

_UTC = _dt.timezone.utc


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _open_layout() -> I.InstanceLayout:
    try:
        layout = I.InstanceLayout.open()
    except I.InstanceRootError as exc:
        raise API.CliError(str(exc), exit_code=API.EXIT_USAGE) from exc
    except I.InstanceError as exc:
        raise API.CliError(str(exc), exit_code=API.EXIT_IO) from exc
    return layout


def _write_json(ctx: API.CommandContext, payload: Any) -> None:
    ctx.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    ctx.stdout.write("\n")


def _redact_error(exc: Exception) -> str:
    """Return a short redacted representation of ``exc`` — no absolute paths."""

    msg = str(exc)
    # Nothing here mentions absolute paths: RT-012 error strings intentionally
    # only reference logical names.  We still cap the length to avoid noisy
    # backtraces sneaking through repr chains.
    if len(msg) > 256:
        msg = msg[:256] + "…"
    return msg


# ---------------------------------------------------------------------------
# tenant init
# ---------------------------------------------------------------------------


def _configure_init(parser: argparse.ArgumentParser) -> None:
    parser.description = "Provision a brand-new tenant in draft state."
    parser.add_argument(
        "--actor",
        required=True,
        help="Identifier of the operator initiating the provisioning (audit trail).",
    )
    parser.add_argument(
        "--reason",
        default="tenant_init",
        help="Short reason string recorded in the tenant's state_history.",
    )


def _cmd_init(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    layout = _open_layout()
    try:
        layout.initialize()
    except I.InstanceError as exc:
        raise API.CliError(f"layout init: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc

    reg = R.TenantRegistry(layout)
    try:
        # Best-effort recovery of any half-committed prior run first.
        reg.recover()
    except R.RegistryError as exc:
        raise API.CliError(f"recover: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc

    try:
        record, receipt = reg.init_tenant(actor=args.actor, reason=args.reason)
    except R.TenantExists as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONFLICT) from exc
    except R.SchemaError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    except R.RegistryConflict as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONFLICT) from exc
    except R.RegistryError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc

    _write_json(
        ctx,
        {
            "schema": "cwk.rt012.tenant_init_receipt.v1",
            "tenant_id": record.tenant_id,
            "status": record.status,
            "record_revision": record.record_revision,
            "auth_epoch": record.auth_epoch,
            "provision_txn_id": receipt.txn_id,
            "provision_receipt_sha256": receipt.payload["receipt_sha256"],
        },
    )
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# tenant show
# ---------------------------------------------------------------------------


def _configure_show(parser: argparse.ArgumentParser) -> None:
    parser.description = "Show the authoritative tenant record."
    parser.add_argument("--tenant-id", required=True)


def _cmd_show(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    try:
        I.validate_tenant_id(args.tenant_id)
    except I.TenantIdError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc

    layout = _open_layout()
    reg = R.TenantRegistry(layout)
    try:
        record = reg.get(args.tenant_id)
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except R.RecordCorruption as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    _write_json(ctx, record.payload)
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# tenant list
# ---------------------------------------------------------------------------


def _configure_list(parser: argparse.ArgumentParser) -> None:
    parser.description = "List every opaque tenant ID with a persisted record."


def _cmd_list(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    layout = _open_layout()
    reg = R.TenantRegistry(layout)
    try:
        ids = reg.list_tenant_ids()
    except I.LayoutError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    except R.RegistryError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    _write_json(
        ctx,
        {
            "schema": "cwk.rt012.tenant_list.v1",
            "count": len(ids),
            "tenant_ids": ids,
        },
    )
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# tenant state-graph
# ---------------------------------------------------------------------------


def _configure_state_graph(parser: argparse.ArgumentParser) -> None:
    parser.description = "Print the frozen tenant life-cycle FSM and operation matrix."


def _cmd_state_graph(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _write_json(ctx, R.state_graph())
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# tenant doctor
# ---------------------------------------------------------------------------


def _configure_doctor(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Inspect layout + registry structural integrity.  "
        "NEVER reads credentials or tenant view content."
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Optionally focus on a single tenant.",
    )


def _cmd_doctor(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    findings: list[dict[str, Any]] = []
    root_present = True
    tenant_id = args.tenant_id
    try:
        layout = I.InstanceLayout.open()
    except I.InstanceRootError as exc:
        root_present = False
        findings.append(_finding("instance_root", "error", "issue", _redact_error(exc)))
        report = {
            "schema": "cwk.rt012.layout_doctor_report.v1",
            "checked_at": _utcnow_iso(),
            "instance_root_present": root_present,
            "tenant_id": tenant_id,
            "issue_count": 1,
            "checks": findings,
        }
        _write_json(ctx, report)
        return API.EXIT_CONTRACT

    # Root permission check.
    try:
        st = os.lstat(layout.root)
        if st.st_mode & 0o077:
            findings.append(
                _finding(
                    "instance_root_permissions",
                    "warn",
                    "issue",
                    f"mode has group/other bits (mode={oct(st.st_mode & 0o777)})",
                )
            )
        else:
            findings.append(_finding("instance_root_permissions", "info", "ok", "0o700-compatible"))
    except OSError as exc:
        findings.append(_finding("instance_root_stat", "error", "issue", f"errno={exc.errno}"))

    # Top-level children.
    with layout.root_fd() as rfd:
        try:
            existing = {e.name for e in os.scandir(rfd)}
        except OSError as exc:
            findings.append(_finding("instance_root_scan", "error", "issue", f"errno={exc.errno}"))
            existing = set()
        for name in I.INSTANCE_ROOT_CHILDREN:
            if name not in existing:
                findings.append(_finding(f"missing_child:{name}", "error", "issue", "expected top-level child not found"))
            else:
                try:
                    st = os.stat(name, dir_fd=rfd, follow_symlinks=False)
                    if stat_module.S_ISLNK(st.st_mode):
                        findings.append(_finding(f"symlink_child:{name}", "error", "issue", "top-level child is a symlink"))
                    elif not stat_module.S_ISDIR(st.st_mode):
                        findings.append(_finding(f"non_dir_child:{name}", "error", "issue", "top-level child is not a dir"))
                    elif st.st_mode & 0o077:
                        findings.append(
                            _finding(
                                f"loose_perms:{name}",
                                "warn",
                                "issue",
                                f"mode={oct(st.st_mode & 0o777)} has group/other bits",
                            )
                        )
                except OSError as exc:
                    findings.append(_finding(f"stat_child:{name}", "error", "issue", f"errno={exc.errno}"))

    reg = R.TenantRegistry(layout)
    # Registry <-> tenant tree consistency (skipped if root not initialised).
    try:
        tenant_ids_registered = set(reg.list_tenant_ids())
    except (I.LayoutError, R.RegistryError):
        tenant_ids_registered = set()
    try:
        tenant_ids_on_disk = set(layout.tenants_root().list_tenant_ids())
    except (I.LayoutError, R.RegistryError):
        tenant_ids_on_disk = set()
    for missing in sorted(tenant_ids_registered - tenant_ids_on_disk):
        findings.append(
            _finding(
                f"tenant_tree_missing:{missing}",
                "error",
                "issue",
                "record exists but tenant directory tree is missing",
            )
        )
    for orphan in sorted(tenant_ids_on_disk - tenant_ids_registered):
        findings.append(
            _finding(
                f"tenant_tree_orphan:{orphan}",
                "error",
                "issue",
                "tenant directory tree exists without a registry record",
            )
        )

    # Per-tenant structural checks.
    ids_to_check = [tenant_id] if tenant_id else sorted(tenant_ids_registered)
    if tenant_id is not None:
        try:
            I.validate_tenant_id(tenant_id)
        except I.TenantIdError as exc:
            findings.append(_finding("tenant_id_grammar", "error", "issue", _redact_error(exc)))
            ids_to_check = []

    for tid in ids_to_check:
        try:
            record = reg.get(tid)
        except R.TenantNotFound:
            findings.append(_finding(f"tenant_record_missing:{tid}", "error", "issue", "no record for tenant"))
            continue
        except R.RecordCorruption as exc:
            findings.append(_finding(f"tenant_record_corrupt:{tid}", "error", "issue", _redact_error(exc)))
            continue

        # Tenant tree presence.
        tenant = layout.tenant(tid)
        if not tenant.exists():
            findings.append(_finding(f"tenant_tree_missing:{tid}", "error", "issue", "tenant tree missing"))
            continue

        # Sub-directory presence + mode + inode sanity + no symlinks.
        try:
            with tenant.tenant_fd() as tfd:
                with os.scandir(tfd) as entries:
                    existing_children = {e.name for e in entries}
                for name in I.TENANT_CHILDREN:
                    if name not in existing_children:
                        findings.append(_finding(f"tenant_child_missing:{tid}/{name}", "error", "issue", "expected tenant child missing"))
                        continue
                    try:
                        st = os.stat(name, dir_fd=tfd, follow_symlinks=False)
                    except OSError as exc:
                        findings.append(_finding(f"tenant_child_stat:{tid}/{name}", "error", "issue", f"errno={exc.errno}"))
                        continue
                    if stat_module.S_ISLNK(st.st_mode):
                        findings.append(_finding(f"tenant_child_symlink:{tid}/{name}", "error", "issue", "child is a symlink"))
                    elif not stat_module.S_ISDIR(st.st_mode):
                        findings.append(_finding(f"tenant_child_not_dir:{tid}/{name}", "error", "issue", "child is not a directory"))
                    elif st.st_mode & 0o077:
                        findings.append(
                            _finding(
                                f"tenant_child_perms:{tid}/{name}",
                                "warn",
                                "issue",
                                f"mode={oct(st.st_mode & 0o777)} has group/other bits",
                            )
                        )
        except I.LayoutError as exc:
            findings.append(_finding(f"tenant_tree_stat:{tid}", "error", "issue", _redact_error(exc)))
            continue

        # Projection sanity.
        try:
            with tenant.child_fd("config") as cfd:
                if not A.child_exists(cfd, "tenant.projection.json"):
                    findings.append(_finding(f"projection_missing:{tid}", "warn", "issue", "tenant.projection.json missing"))
                else:
                    body = A.read_file(cfd, "tenant.projection.json")
                    try:
                        proj = json.loads(body.decode("utf-8"))
                        proj_rev = proj.get("record_revision")
                        if proj_rev != record.record_revision:
                            findings.append(
                                _finding(
                                    f"projection_drift:{tid}",
                                    "warn",
                                    "issue",
                                    f"projection revision {proj_rev} != record {record.record_revision}",
                                )
                            )
                    except (ValueError, UnicodeDecodeError) as exc:
                        findings.append(_finding(f"projection_corrupt:{tid}", "warn", "issue", _redact_error(exc)))
        except I.LayoutError as exc:
            findings.append(_finding(f"projection_stat:{tid}", "warn", "issue", _redact_error(exc)))

        # Quota shape (unset structure only).
        quota = record.payload.get("quota", {})
        if quota.get("scheme") != "cwk.rt012.quota.unset.v1":
            findings.append(_finding(f"quota_scheme:{tid}", "warn", "issue", "quota.scheme unexpected"))

    # Journal residue (only if registry+provision-journal exist).
    try:
        with layout.registry_fd("provision-journal") as jfd:
            pending = [e.name for e in os.scandir(jfd) if not e.name.startswith(A.TEMP_PREFIX)]
        if pending:
            findings.append(
                _finding(
                    "provision_journal_residue",
                    "warn",
                    "issue",
                    f"{len(pending)} pending journal entr(y|ies); run recover",
                )
            )
    except I.LayoutError:
        # Missing sub-directory is already reported as a top-level "missing_child".
        pass

    issue_count = sum(1 for f in findings if f["status"] == "issue")
    report = {
        "schema": "cwk.rt012.layout_doctor_report.v1",
        "checked_at": _utcnow_iso(),
        "instance_root_present": True,
        "tenant_id": tenant_id,
        "issue_count": issue_count,
        "checks": findings,
    }
    _write_json(ctx, report)
    if issue_count > 0:
        return API.EXIT_CONTRACT
    return API.EXIT_OK


def _finding(name: str, severity: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "severity": severity, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Provider ABI
# ---------------------------------------------------------------------------


def list_commands() -> Sequence[API.CommandSpec]:
    return (
        API.CommandSpec(
            name="init",
            summary="Provision a fresh tenant (draft state).",
            configure_parser=_configure_init,
            handler=_cmd_init,
        ),
        API.CommandSpec(
            name="show",
            summary="Show the authoritative tenant record.",
            configure_parser=_configure_show,
            handler=_cmd_show,
        ),
        API.CommandSpec(
            name="list",
            summary="List opaque tenant IDs with a persisted record.",
            configure_parser=_configure_list,
            handler=_cmd_list,
        ),
        API.CommandSpec(
            name="doctor",
            summary="Structural / permission / integrity checks (no credentials).",
            configure_parser=_configure_doctor,
            handler=_cmd_doctor,
        ),
        API.CommandSpec(
            name="state-graph",
            summary="Emit the frozen tenant state machine and operation matrix.",
            configure_parser=_configure_state_graph,
            handler=_cmd_state_graph,
        ),
    )


def run_doctor(ctx: API.CommandContext) -> Sequence[API.DoctorFinding]:
    """Hook so future providers can aggregate their own findings via the
    dispatcher.  RT-012 currently emits no findings from this hook (the
    ``doctor`` command implements the full report itself); the callable
    exists so downstream RTs know the ABI is real."""

    return ()


__all__ = [
    "API_VERSION",
    "PROVIDER_NAME",
    "PROVIDER_VERSION",
    "list_commands",
    "run_doctor",
]
