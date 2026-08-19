#!/usr/bin/env python3
"""RT-013 CLI provider: ``bind-agent`` / ``revoke-agent`` / ``rotate-credential`` ...

Owned by RT-013.  Registered in the RT-012 dispatcher via
:data:`cwk_tenant_cli.FROZEN_PROVIDER_SLOTS`.  This module is the ONLY
place that turns admin CLI arguments into binding / credential /
rotation mutations.  It never reads material into stdout / stderr / audit
logs; the material key (``CWORK_APP_KEY``) never appears in argv, output,
or exception messages.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import stat as stat_module
from typing import Any, Sequence

import cwk_agent_binding as B
import cwk_agent_context as AC
import cwk_atomic_file as A
import cwk_credential_broker as CB
import cwk_instance as I
import cwk_tenant_cli_api as API
import cwk_tenant_registry as R


API_VERSION = API.COMMAND_PROVIDER_API_VERSION
PROVIDER_NAME = "cwk_tenant_cmd_binding"
PROVIDER_VERSION = "v1"

_UTC = _dt.timezone.utc


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_json(ctx: API.CommandContext, payload: Any) -> None:
    ctx.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    ctx.stdout.write("\n")


def _redact_error(exc: Exception) -> str:
    msg = str(exc)
    if len(msg) > 256:
        msg = msg[:256] + "…"
    return msg


def _open_layout() -> I.InstanceLayout:
    try:
        layout = I.InstanceLayout.open()
    except I.InstanceRootError as exc:
        raise API.CliError(str(exc), exit_code=API.EXIT_USAGE) from exc
    except I.InstanceError as exc:
        raise API.CliError(str(exc), exit_code=API.EXIT_IO) from exc
    return layout


def _open_binding_registry() -> tuple[I.InstanceLayout, B.BindingRegistry]:
    layout = _open_layout()
    try:
        layout.initialize()
    except I.InstanceError as exc:
        raise API.CliError(f"layout init: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc
    reg = B.BindingRegistry(layout)
    try:
        reg.initialize()
    except (B.BindingError, A.AtomicFileError, I.LayoutError) as exc:
        raise API.CliError(f"binding init: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc
    return layout, reg


def _open_credential_store() -> tuple[I.InstanceLayout, CB.CredentialRefStore]:
    layout = _open_layout()
    try:
        layout.initialize()
    except I.InstanceError as exc:
        raise API.CliError(f"layout init: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc
    store = CB.CredentialRefStore(layout)
    try:
        store.initialize()
    except (CB.CredentialError, A.AtomicFileError, I.LayoutError) as exc:
        raise API.CliError(f"credential init: {_redact_error(exc)}", exit_code=API.EXIT_IO) from exc
    return layout, store


def _raise_from_binding_error(exc: B.BindingError) -> None:
    """Translate a BindingError code into the stable CLI exit taxonomy."""

    code = exc.code
    if code in {"schema", "state", "actor", "reason", "agent_id"}:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    if code == "conflict":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONFLICT) from exc
    if code in {"not_found", "secret_missing"}:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    if code in {"revoked", "suspended"}:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    if code == "corruption":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    raise API.CliError(_redact_error(exc), exit_code=API.EXIT_INTERNAL) from exc


def _raise_from_credential_error(exc: CB.CredentialError) -> None:
    code = exc.code
    if code in {"schema", "state", "policy", "actor", "reason"}:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    if code == "conflict":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONFLICT) from exc
    if code == "not_found":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    if code == "backend":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    if code == "corruption":
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc
    raise API.CliError(_redact_error(exc), exit_code=API.EXIT_INTERNAL) from exc


# ---------------------------------------------------------------------------
# bind-agent
# ---------------------------------------------------------------------------


def _configure_bind(parser: argparse.ArgumentParser) -> None:
    parser.description = "Bind a raw agent id to a tenant.  HMAC-hashes the id; the raw id is never stored."
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Raw agent id.  Hashed via the binding secret before storage.",
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="bind_agent")


def _cmd_bind(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _validate_tenant_id_arg(args.tenant_id)
    layout, reg = _open_binding_registry()
    try:
        record, receipt = reg.bind(
            tenant_id=args.tenant_id,
            raw_agent_id=args.agent_id,
            actor=args.actor,
            reason=args.reason,
        )
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except B.BindingError as exc:
        _raise_from_binding_error(exc)
    _write_json(ctx, _redact_bind_output(record, receipt, action="bind"))
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# rebind-agent
# ---------------------------------------------------------------------------


def _configure_rebind(parser: argparse.ArgumentParser) -> None:
    parser.description = "Two-step rebind: revoke the current binding then bind to a new tenant."
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--new-tenant-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="rebind_agent")


def _cmd_rebind(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _validate_tenant_id_arg(args.new_tenant_id)
    layout, reg = _open_binding_registry()
    try:
        record, receipts = reg.rebind(
            raw_agent_id=args.agent_id,
            new_tenant_id=args.new_tenant_id,
            actor=args.actor,
            reason=args.reason,
        )
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except B.BindingError as exc:
        _raise_from_binding_error(exc)
    _write_json(
        ctx,
        {
            "schema": "cwk.rt013.rebind_output.v1",
            "agent_id_hash": record.agent_id_hash,
            "new_tenant_id": record.tenant_id,
            "binding_epoch": record.binding_epoch,
            "receipts": [_redact_receipt_pointer(rc) for rc in receipts],
        },
    )
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# revoke-agent / suspend-agent / reactivate-agent
# ---------------------------------------------------------------------------


def _configure_status_change(parser: argparse.ArgumentParser, action: str) -> None:
    verb = {
        "revoke": "Revoke an agent binding.  Immediate deny + auth_epoch bump.",
        "suspend": "Suspend an active agent binding.",
        "reactivate": "Reactivate a suspended agent binding.",
    }[action]
    parser.description = verb
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default=f"{action}_agent")


def _make_cmd_status_change(action: str):
    def _handler(ctx: API.CommandContext, args: argparse.Namespace) -> int:
        layout, reg = _open_binding_registry()
        try:
            if action == "revoke":
                record, receipt = reg.revoke(
                    raw_agent_id=args.agent_id, actor=args.actor, reason=args.reason
                )
            elif action == "suspend":
                record, receipt = reg.suspend(
                    raw_agent_id=args.agent_id, actor=args.actor, reason=args.reason
                )
            elif action == "reactivate":
                record, receipt = reg.reactivate(
                    raw_agent_id=args.agent_id, actor=args.actor, reason=args.reason
                )
            else:  # pragma: no cover - guarded upstream
                raise API.CliError(f"unknown action {action!r}", exit_code=API.EXIT_INTERNAL)
        except R.TenantNotFound as exc:
            raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
        except B.BindingError as exc:
            _raise_from_binding_error(exc)
        _write_json(ctx, _redact_bind_output(record, receipt, action=action))
        return API.EXIT_OK

    return _handler


# ---------------------------------------------------------------------------
# list-bindings / show-binding
# ---------------------------------------------------------------------------


def _configure_list(parser: argparse.ArgumentParser) -> None:
    parser.description = "List active/suspended agent bindings (opaque hashes only)."
    parser.add_argument("--tenant-id", default=None)


def _cmd_list(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    layout, reg = _open_binding_registry()
    if args.tenant_id is not None:
        _validate_tenant_id_arg(args.tenant_id)
    try:
        records = reg.list_active(tenant_id=args.tenant_id)
    except (B.BindingError, I.LayoutError) as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    _write_json(
        ctx,
        {
            "schema": "cwk.rt013.binding_list.v1",
            "count": len(records),
            "bindings": [
                {
                    "agent_id_hash": r.agent_id_hash,
                    "tenant_id": r.tenant_id,
                    "status": r.status,
                    "binding_epoch": r.binding_epoch,
                    "binding_secret_epoch": r.binding_secret_epoch,
                    "updated_at": r.payload["updated_at"],
                }
                for r in records
            ],
        },
    )
    return API.EXIT_OK


def _configure_show(parser: argparse.ArgumentParser) -> None:
    parser.description = "Show a binding record by raw agent id (HMAC hashed before lookup)."
    parser.add_argument("--agent-id", required=True)


def _cmd_show(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    layout, reg = _open_binding_registry()
    try:
        hash_hex = reg.hash_agent_id(args.agent_id)
        record = reg.get_by_hash(hash_hex)
    except B.BindingError as exc:
        _raise_from_binding_error(exc)
    payload = dict(record.payload)
    # Truncate the history if it grew large; the record itself is complete.
    _write_json(ctx, payload)
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# set-credential / disable-credential / rotate-credential
# ---------------------------------------------------------------------------


def _configure_set_credential(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Set the credential reference URI for a tenant.  Material is NEVER "
        "supplied on the CLI — only the reference URI + backend name."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--reference-uri", required=True, help="opaque secret:// URI")
    parser.add_argument("--backend", required=True, choices=list(CB.CREDENTIAL_BACKENDS))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="set_credential")


def _cmd_set_credential(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _validate_tenant_id_arg(args.tenant_id)
    layout, store = _open_credential_store()
    try:
        record, receipt = store.set_reference(
            tenant_id=args.tenant_id,
            reference_uri=args.reference_uri,
            backend=args.backend,
            actor=args.actor,
            reason=args.reason,
        )
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except CB.CredentialError as exc:
        _raise_from_credential_error(exc)
    _write_json(ctx, _redact_cred_output(record, receipt))
    return API.EXIT_OK


def _configure_disable_credential(parser: argparse.ArgumentParser) -> None:
    parser.description = "Disable a tenant's credential reference; broker refuses subsequent leases."
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="disable_credential")


def _cmd_disable_credential(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _validate_tenant_id_arg(args.tenant_id)
    layout, store = _open_credential_store()
    try:
        record, receipt = store.disable(
            tenant_id=args.tenant_id, actor=args.actor, reason=args.reason
        )
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except CB.CredentialError as exc:
        _raise_from_credential_error(exc)
    _write_json(ctx, _redact_cred_output(record, receipt))
    return API.EXIT_OK


def _configure_rotate_credential(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Rotate a tenant credential reference in two phases: --begin then --finalize.  "
        "Broker refuses to lease while a rotation is in flight."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--phase", required=True, choices=("begin", "finalize"))
    parser.add_argument("--new-reference-uri", default=None)
    parser.add_argument("--new-backend", default=None, choices=list(CB.CREDENTIAL_BACKENDS))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="rotate_credential")


def _cmd_rotate_credential(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    _validate_tenant_id_arg(args.tenant_id)
    layout, store = _open_credential_store()
    try:
        if args.phase == "begin":
            if not args.new_reference_uri or not args.new_backend:
                raise API.CliError(
                    "rotate --phase begin requires --new-reference-uri and --new-backend",
                    exit_code=API.EXIT_USAGE,
                )
            record, receipt = store.rotate_begin(
                tenant_id=args.tenant_id,
                new_reference_uri=args.new_reference_uri,
                new_backend=args.new_backend,
                actor=args.actor,
                reason=args.reason,
            )
        else:
            record, receipt = store.rotate_finalize(
                tenant_id=args.tenant_id, actor=args.actor, reason=args.reason
            )
    except R.TenantNotFound as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_IO) from exc
    except CB.CredentialError as exc:
        _raise_from_credential_error(exc)
    _write_json(ctx, _redact_cred_output(record, receipt))
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# rotate-binding-secret
# ---------------------------------------------------------------------------


def _configure_rotate_binding_secret(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Rotate the HMAC secret used to hash raw agent ids.  Two-phase: "
        "--phase begin --material-file <path> writes a new epoch material and "
        "flips the pointer to dual_write; --phase finalize swaps to the new "
        "epoch and tombstones every binding record that was tagged with the "
        "old epoch.  Operators must re-bind those agents; broker fails "
        "closed for tombstoned records."
    )
    parser.add_argument("--phase", required=True, choices=("begin", "finalize"))
    parser.add_argument(
        "--material-file",
        default=None,
        help="Absolute path to a 0o600 file containing new HMAC material (>=32 bytes).  Only used for --phase begin.",
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="rotate_binding_secret")


def _cmd_rotate_binding_secret(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    layout, reg = _open_binding_registry()
    try:
        if args.phase == "begin":
            if not args.material_file:
                raise API.CliError(
                    "rotate-binding-secret --phase begin requires --material-file",
                    exit_code=API.EXIT_USAGE,
                )
            material = _read_material_file(args.material_file)
            try:
                begin_pointer, _summary_placeholder = reg.rotate_secret(
                    new_material=material,
                    actor=args.actor,
                    reason=args.reason,
                )
            finally:
                _zero_bytearray(material)
            _write_json(
                ctx,
                {
                    "schema": "cwk.rt013.binding_secret_rotation_output.v1",
                    "phase": "begin_and_finalize",
                    "current_epoch": begin_pointer["current_epoch"],
                    "secondary_epoch": begin_pointer["secondary_epoch"],
                },
            )
        else:
            # rotate_secret runs begin+finalize atomically inside the module;
            # --phase finalize as a standalone step exists for recovery paths.
            # If the pointer is already stable, this command is a no-op with
            # a stable exit code so operators can retry safely.
            pointer = reg.secrets.read_pointer()
            if pointer["rotation_state"] == "stable":
                _write_json(
                    ctx,
                    {
                        "schema": "cwk.rt013.binding_secret_rotation_output.v1",
                        "phase": "finalize",
                        "current_epoch": pointer["current_epoch"],
                        "secondary_epoch": None,
                    },
                )
                return API.EXIT_OK
            reg.secrets.rotate_finalize(actor=args.actor, reason=args.reason)
            pointer = reg.secrets.read_pointer()
            _write_json(
                ctx,
                {
                    "schema": "cwk.rt013.binding_secret_rotation_output.v1",
                    "phase": "finalize",
                    "current_epoch": pointer["current_epoch"],
                    "secondary_epoch": None,
                },
            )
    except B.BindingError as exc:
        _raise_from_binding_error(exc)
    return API.EXIT_OK


# ---------------------------------------------------------------------------
# doctor:binding
# ---------------------------------------------------------------------------


def _configure_doctor(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Structural doctor for the binding registry + credential store; NEVER "
        "reads HMAC material or credential material."
    )


def _cmd_doctor(ctx: API.CommandContext, args: argparse.Namespace) -> int:
    findings: list[dict[str, Any]] = []
    try:
        layout = I.InstanceLayout.open()
    except I.InstanceRootError as exc:
        raise API.CliError(str(exc), exit_code=API.EXIT_USAGE) from exc
    try:
        reg = B.BindingRegistry(layout).initialize()
    except (B.BindingError, A.AtomicFileError, I.LayoutError) as exc:
        findings.append(_finding("binding_init", "error", "issue", _redact_error(exc)))
        _emit_doctor(ctx, findings, tenant_id=None)
        return API.EXIT_CONTRACT
    # Pointer sanity.
    try:
        pointer = reg.secrets.read_pointer()
    except B.BindingError as exc:
        findings.append(_finding("binding_secret_pointer", "error", "issue", _redact_error(exc)))
        _emit_doctor(ctx, findings, tenant_id=None)
        return API.EXIT_CONTRACT
    findings.append(
        _finding(
            "binding_secret_pointer",
            "info",
            "ok",
            f"rotation_state={pointer['rotation_state']!r} current_epoch={pointer['current_epoch']}",
        )
    )
    # Journal residue.
    with B._binding_sub(layout, "journal") as jfd:  # noqa: SLF001 - same-package helper
        pending = [e.name for e in os.scandir(jfd) if e.name.endswith(".journal")]
    if pending:
        findings.append(
            _finding("binding_journal_residue", "warn", "issue", f"{len(pending)} pending entries")
        )
    # Credential store findings.
    try:
        findings.extend(CB.doctor_credential_store(layout))
    except CB.CredentialError as exc:
        findings.append(_finding("credential_doctor", "error", "issue", _redact_error(exc)))
    _emit_doctor(ctx, findings, tenant_id=None)
    return API.EXIT_OK if all(f["status"] == "ok" for f in findings) else API.EXIT_CONTRACT


def _emit_doctor(
    ctx: API.CommandContext, findings: list[dict[str, Any]], *, tenant_id: str | None
) -> None:
    issue_count = sum(1 for f in findings if f["status"] == "issue")
    report = {
        "schema": "cwk.rt013.binding_doctor_report.v1",
        "checked_at": _utcnow_iso(),
        "tenant_id": tenant_id,
        "issue_count": issue_count,
        "checks": findings,
    }
    _write_json(ctx, report)


def _finding(name: str, severity: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "severity": severity, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# doctor hook (aggregate finding into `cwk-tenant doctor`)
# ---------------------------------------------------------------------------


def run_doctor(ctx: API.CommandContext) -> Sequence[API.DoctorFinding]:
    """Provider-level doctor hook consumed by the RT-012 dispatcher.

    Emits a compact summary — no raw agent id, no material, no absolute
    host paths.  Never raises: any failure inside is wrapped in a single
    ``issue`` finding.
    """

    try:
        layout = I.InstanceLayout.open()
    except I.InstanceError as exc:
        return (
            API.DoctorFinding(
                name="rt013_bootstrap",
                severity="warn",
                status="issue",
                detail=f"instance layout not open ({exc.__class__.__name__})",
            ),
        )
    findings: list[API.DoctorFinding] = []
    try:
        reg = B.BindingRegistry(layout).initialize()
        pointer = reg.secrets.read_pointer()
        findings.append(
            API.DoctorFinding(
                name="rt013_binding_pointer",
                severity="info",
                status="ok",
                detail=f"rotation_state={pointer['rotation_state']!r} current_epoch={pointer['current_epoch']}",
            )
        )
        count = len(reg.list_active())
        findings.append(
            API.DoctorFinding(
                name="rt013_active_bindings",
                severity="info",
                status="ok",
                detail=f"{count} active/suspended bindings",
            )
        )
    except (B.BindingError, A.AtomicFileError, I.LayoutError) as exc:
        findings.append(
            API.DoctorFinding(
                name="rt013_binding_pointer",
                severity="warn",
                status="issue",
                detail=exc.__class__.__name__,
            )
        )
    try:
        cred_findings = CB.doctor_credential_store(layout)
        for cf in cred_findings:
            findings.append(
                API.DoctorFinding(
                    name=cf["name"], severity=cf["severity"], status=cf["status"], detail=cf["detail"]
                )
            )
    except CB.CredentialError as exc:
        findings.append(
            API.DoctorFinding(
                name="rt013_credential_doctor",
                severity="warn",
                status="issue",
                detail=exc.__class__.__name__,
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Provider ABI
# ---------------------------------------------------------------------------


def list_commands() -> Sequence[API.CommandSpec]:
    return (
        API.CommandSpec(
            name="bind-agent",
            summary="Bind a raw agent id (hashed) to a tenant.",
            configure_parser=_configure_bind,
            handler=_cmd_bind,
        ),
        API.CommandSpec(
            name="rebind-agent",
            summary="Revoke current binding and bind to a new tenant (two receipts).",
            configure_parser=_configure_rebind,
            handler=_cmd_rebind,
        ),
        API.CommandSpec(
            name="revoke-agent",
            summary="Revoke an agent binding.",
            configure_parser=lambda p: _configure_status_change(p, "revoke"),
            handler=_make_cmd_status_change("revoke"),
        ),
        API.CommandSpec(
            name="suspend-agent",
            summary="Suspend an active agent binding.",
            configure_parser=lambda p: _configure_status_change(p, "suspend"),
            handler=_make_cmd_status_change("suspend"),
        ),
        API.CommandSpec(
            name="reactivate-agent",
            summary="Reactivate a suspended agent binding.",
            configure_parser=lambda p: _configure_status_change(p, "reactivate"),
            handler=_make_cmd_status_change("reactivate"),
        ),
        API.CommandSpec(
            name="list-bindings",
            summary="List agent-binding records (opaque hashes only).",
            configure_parser=_configure_list,
            handler=_cmd_list,
        ),
        API.CommandSpec(
            name="show-binding",
            summary="Show a binding record for the supplied raw agent id.",
            configure_parser=_configure_show,
            handler=_cmd_show,
        ),
        API.CommandSpec(
            name="set-credential",
            summary="Set the credential reference URI + backend for a tenant.",
            configure_parser=_configure_set_credential,
            handler=_cmd_set_credential,
        ),
        API.CommandSpec(
            name="disable-credential",
            summary="Disable a tenant credential reference.",
            configure_parser=_configure_disable_credential,
            handler=_cmd_disable_credential,
        ),
        API.CommandSpec(
            name="rotate-credential",
            summary="Rotate a tenant credential reference in two phases (begin/finalize).",
            configure_parser=_configure_rotate_credential,
            handler=_cmd_rotate_credential,
        ),
        API.CommandSpec(
            name="rotate-binding-secret",
            summary="Rotate the HMAC binding secret used to hash raw agent ids.",
            configure_parser=_configure_rotate_binding_secret,
            handler=_cmd_rotate_binding_secret,
        ),
        API.CommandSpec(
            name="doctor:binding",
            summary="Structural doctor for the binding registry + credential store.",
            configure_parser=_configure_doctor,
            handler=_cmd_doctor,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_tenant_id_arg(value: str) -> None:
    try:
        I.validate_tenant_id(value)
    except I.TenantIdError as exc:
        raise API.CliError(_redact_error(exc), exit_code=API.EXIT_CONTRACT) from exc


def _read_material_file(path: str) -> bytearray:
    """Read a 0o600 material file into a mutable bytearray we can zero later.

    Refuses symlinks and files with group/other perms; refuses files smaller
    than :data:`cwk_agent_binding.SECRET_MIN_BYTES`.
    """

    if not isinstance(path, str) or not path.strip():
        raise API.CliError("--material-file path is empty", exit_code=API.EXIT_USAGE)
    if not os.path.isabs(path):
        raise API.CliError("--material-file must be an absolute path", exit_code=API.EXIT_USAGE)
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise API.CliError(
            f"--material-file is not accessible ({exc.__class__.__name__})",
            exit_code=API.EXIT_IO,
        ) from exc
    if stat_module.S_ISLNK(st.st_mode):
        raise API.CliError("--material-file is a symlink; refusing", exit_code=API.EXIT_CONTRACT)
    if not stat_module.S_ISREG(st.st_mode):
        raise API.CliError("--material-file is not a regular file", exit_code=API.EXIT_CONTRACT)
    if st.st_mode & 0o077:
        raise API.CliError(
            "--material-file mode has group/other bits; refusing", exit_code=API.EXIT_CONTRACT
        )
    if st.st_size < B.SECRET_MIN_BYTES:
        raise API.CliError(
            f"--material-file must be >= {B.SECRET_MIN_BYTES} bytes", exit_code=API.EXIT_CONTRACT
        )
    with open(path, "rb") as fh:  # noqa: PTH123 - low-level read
        data = fh.read()
    return bytearray(data)


def _zero_bytearray(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


def _redact_bind_output(
    record: B.BindingRecord, receipt: B.BindingReceipt, *, action: str
) -> dict[str, Any]:
    return {
        "schema": "cwk.rt013.binding_output.v1",
        "action": action,
        "tenant_id": record.tenant_id,
        "agent_id_hash": record.agent_id_hash,
        "status": record.status,
        "binding_epoch": record.binding_epoch,
        "binding_secret_epoch": record.binding_secret_epoch,
        "receipt_id": receipt.payload["receipt_id"],
        "receipt_sha256": receipt.payload["receipt_sha256"],
        "tenant_auth_epoch_after": receipt.payload["tenant_auth_epoch_after"],
    }


def _redact_receipt_pointer(receipt: B.BindingReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.payload["receipt_id"],
        "receipt_sha256": receipt.payload["receipt_sha256"],
        "action": receipt.payload["action"],
        "binding_epoch_after": receipt.payload["binding_epoch_after"],
    }


def _redact_cred_output(
    record: CB.CredentialRecord, receipt: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": "cwk.rt013.credential_output.v1",
        "tenant_id": record.tenant_id,
        "status": record.status,
        "credential_epoch": record.credential_epoch,
        "reference_uri": record.reference_uri,
        "backend": record.backend,
        "rotation_state": record.rotation_state,
        "receipt_id": receipt["receipt_id"],
        "receipt_sha256": receipt["receipt_sha256"],
    }


__all__ = [
    "API_VERSION",
    "PROVIDER_NAME",
    "PROVIDER_VERSION",
    "list_commands",
    "run_doctor",
]
