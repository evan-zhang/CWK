#!/usr/bin/env python3
"""RT-013: Credential Broker and credential-reference store.

Owned by RT-013.  This module is the only writer of
``registry/credentials/`` beneath ``CWK_INSTANCE_ROOT``.  It:

- stores per-tenant *opaque* credential references (``secret://<opaque>``)
  and NEVER the material itself;
- resolves references to material only inside a short-lived
  :class:`CredentialLease` context manager whose ``env`` dict carries the
  material to a downstream subprocess and is zeroed on exit;
- enforces the RT-012 operation-permission matrix — tenants in
  ``draft/suspended/offboarded`` refuse to hand out material, and
  ``profile_pending`` only hands out material for bounded sampling /
  profile purposes;
- refuses every fallback path: no fallback to repository ``.env``, no
  fallback to another tenant's credential, no fallback to the host
  environment beyond a strict whitelist that only carries
  ``CWK_INSTANCE_ROOT`` in addition to the resolved material key;
- supports dual-write reference rotation with an atomic pointer swap on
  the credential record itself and a tombstone directory for the previous
  reference, so a query concurrent with rotation cannot see a mixed
  state.

Never touches ``.env`` / ``CWORK_APP_KEY`` from the host process's own
environment; never reads real gateway; only stdlib imports.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import secrets
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional, Protocol

import cwk_agent_binding as _AB  # for _open_subdir + _binding_root style helpers via re-import? No — provide our own copy to avoid coupling
import cwk_atomic_file as A
import cwk_instance as I
import cwk_pr001_contracts as C
import cwk_tenant_registry as R


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


CREDENTIAL_REF_SCHEMA = "cwk.rt013.credential_ref.v1"
BROKER_LEASE_SCHEMA = "cwk.rt013.credential_broker_lease.v1"

CREDENTIAL_STATES: tuple[str, ...] = ("active", "disabled", "revoked", "rotating")
CREDENTIAL_BACKENDS: tuple[str, ...] = ("env_ref", "file_ref")

# Which env vars a *downstream* Collector / Worker subprocess may see.  The
# broker builds this dict fresh on every lease; the host process's own env
# vars are NOT propagated, including ``CWORK_APP_KEY``.  The only material
# key we set is ``CWORK_APP_KEY`` (the historical CWK envelope for the
# app key); adding a new key is a breaking ABI change and requires a
# schema bump.
ENV_WHITELIST_INHERIT: frozenset[str] = frozenset({"CWK_INSTANCE_ROOT"})
ENV_MATERIAL_KEY = "CWORK_APP_KEY"

_REFERENCE_URI_REGEX = re.compile(r"\Asecret://[a-z0-9._-]{1,128}\Z")
_ENV_REF_PREFIX = "secret://env-"
_FILE_REF_PREFIX = "secret://file-"

_RECEIPT_ID_REGEX = re.compile(r"\Arcrd_[a-z0-9]{26}\Z")
_LEASE_ID_REGEX = re.compile(r"\Alease_[a-z0-9]{26}\Z")
_TXN_ID_REGEX = re.compile(r"\Actxn_[a-z0-9]{26}\Z")

# purpose → allowed tenant statuses.  Mirrors RT-012's TENANT_OPERATION_MATRIX
# but restated here so the broker can fail closed in isolation from the
# tenant registry even under a corrupted operation matrix (defense in depth).
_PURPOSE_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    "sampling_collect_bounded": frozenset({"profile_pending", "pilot", "active"}),
    "collector_run": frozenset({"pilot", "active"}),
    "scheduler_run": frozenset({"pilot", "active"}),
    "profile_ai": frozenset({"profile_pending", "pilot", "active"}),
    "profile_confirm": frozenset({"profile_pending", "pilot", "active"}),
    "query_broker": frozenset({"pilot", "active"}),
}

_CREDENTIAL_SUBDIRS: tuple[str, ...] = ("receipts", "journal", "tombstone")

_UTC = _dt.timezone.utc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CredentialError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CredentialNotFound(CredentialError):
    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"no credential reference for tenant {tenant_id!r}", code="not_found")


class CredentialSchemaError(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="schema")


class CredentialStateError(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="state")


class CredentialPolicyError(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="policy")


class CredentialConflictError(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="conflict")


class CredentialBackendError(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="backend")


class CredentialCorruption(CredentialError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="corruption")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(tz=_UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(C.canonical_json_bytes(payload)).hexdigest()


def _new_receipt_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"rcrd_{body}"


def _new_lease_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"lease_{body}"


def _new_txn_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"ctxn_{body}"


def _check_actor_reason(actor: str, reason: str) -> None:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
        raise CredentialError("actor must be a non-empty <=128 char str", code="actor")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise CredentialError("reason must be a non-empty <=256 char str", code="reason")


def _check_reference_uri(uri: str) -> str:
    if not isinstance(uri, str) or not _REFERENCE_URI_REGEX.match(uri):
        raise CredentialSchemaError(
            f"reference_uri must match {_REFERENCE_URI_REGEX.pattern!r}"
        )
    return uri


def _check_backend(backend: str) -> str:
    if backend not in CREDENTIAL_BACKENDS:
        raise CredentialSchemaError(f"backend {backend!r} not in {list(CREDENTIAL_BACKENDS)}")
    return backend


# ---------------------------------------------------------------------------
# dirfd helpers (re-use RT-013 binding subdir opener; own our own to avoid coupling)
# ---------------------------------------------------------------------------


def _open_subdir(parent_fd: int, name: str) -> int:
    A._validate_leaf(name)  # noqa: SLF001
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise A.ContainmentError(f"child {name!r} is a symlink; refusing to follow") from exc
        if exc.errno == errno.ENOTDIR:
            raise A.ContainmentError(f"child {name!r} is not a directory") from exc
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(name)
        raise A.AtomicFileError(f"cannot open child {name!r} (errno={exc.errno})", code="open") from exc
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise A.ContainmentError(f"child {name!r} is not a directory")
    return fd


@contextmanager
def _credentials_fd(layout: I.InstanceLayout) -> Iterator[int]:
    with layout.registry_fd("credentials") as fd:
        yield fd


@contextmanager
def _credentials_sub(layout: I.InstanceLayout, name: str) -> Iterator[int]:
    with _credentials_fd(layout) as fd:
        sub = _open_subdir(fd, name)
        try:
            yield sub
        finally:
            os.close(sub)


@contextmanager
def _tombstone_epoch(layout: I.InstanceLayout, epoch: int) -> Iterator[int]:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise CredentialSchemaError("tombstone epoch must be positive int")
    with _credentials_sub(layout, "tombstone") as t:
        A.mkdir_at(t, str(epoch), mode=A.DIRECTORY_MODE, exist_ok=True)
        fd = _open_subdir(t, str(epoch))
        try:
            yield fd
        finally:
            os.close(fd)


def _ensure_registry_dirs(layout: I.InstanceLayout) -> None:
    with _credentials_fd(layout) as fd:
        for name in _CREDENTIAL_SUBDIRS:
            A.mkdir_at(fd, name, mode=A.DIRECTORY_MODE, exist_ok=True)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA_DIR = C.SCHEMA_ROOT / "rt013" / "schemas"
_SCHEMA_CACHE: dict[str, Any] = {}


def _load_schema(name: str) -> Any:
    if name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[name] = C.strict_json_load_path(_SCHEMA_DIR / name)
    return _SCHEMA_CACHE[name]


def validate_credential_ref(payload: Any) -> None:
    schema = _load_schema("credential_ref.schema.json")
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise CredentialSchemaError(str(exc)) from exc
    for key in ("credential_epoch",):
        val = payload.get(key)
        if isinstance(val, bool):
            raise CredentialSchemaError(f"{key} must be int, not bool")
    rot = payload.get("rotation", {})
    for key in ("secondary_credential_epoch", "previous_credential_epoch"):
        val = rot.get(key)
        if isinstance(val, bool):
            raise CredentialSchemaError(f"rotation.{key} must be int|null, not bool")


def validate_broker_lease(payload: Any) -> None:
    schema = _load_schema("credential_broker_lease.schema.json")
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise CredentialSchemaError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


class CredentialBackend(Protocol):
    """Read-only material lookup keyed by the opaque reference URI.

    Backends MUST NOT log the material.  RT-013 ships two thin adapters —
    :class:`EnvRefBackend` and :class:`FileRefBackend` — both of which are
    intended for host-local, non-production use.  Production backends
    (system keychain, HSM, KMS, ...) are introduced by later RTs behind
    independent audits.
    """

    def read_material(self, reference_uri: str) -> bytes:  # pragma: no cover - protocol
        ...


class EnvRefBackend:
    """Reads material from an environment variable named ``CWK_CRED_<opaque>``.

    ``reference_uri`` grammar: ``secret://env-<opaque>``.  ``<opaque>`` is
    used verbatim to build the env var name so operators can rotate secrets
    by rotating both the env var and the reference.  The env var value is
    *never* echoed back into logs or receipts.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        # We snapshot the env at construction time so a downstream test can
        # inject a controlled dict and be sure the broker cannot fall back
        # to the actual process env.
        self._env: dict[str, str] = dict(env) if env is not None else dict(os.environ)

    def read_material(self, reference_uri: str) -> bytes:
        _check_reference_uri(reference_uri)
        if not reference_uri.startswith(_ENV_REF_PREFIX):
            raise CredentialBackendError(
                f"env_ref backend refuses reference_uri {reference_uri!r}"
            )
        opaque = reference_uri[len(_ENV_REF_PREFIX):]
        env_var = f"CWK_CRED_{opaque}"
        if env_var not in self._env:
            raise CredentialBackendError(
                f"env var {env_var!r} not present in isolated env snapshot"
            )
        value = self._env[env_var]
        if not value:
            raise CredentialBackendError(
                f"env var {env_var!r} is empty; refusing to lease"
            )
        return value.encode("utf-8")


class FileRefBackend:
    """Reads material from a path registered per opaque id.

    ``reference_uri`` grammar: ``secret://file-<opaque>``.  Absolute paths
    are supplied out-of-band via the constructor's ``paths`` dict — the
    reference URI never carries a filesystem path so the credential record
    is safe to back up / audit even under a stricter regime.
    """

    def __init__(self, paths: Mapping[str, str]) -> None:
        # Copy so the constructor argument's later mutation cannot smuggle
        # a new backend mapping through.
        self._paths: dict[str, str] = {k: str(v) for k, v in paths.items()}

    def read_material(self, reference_uri: str) -> bytes:
        _check_reference_uri(reference_uri)
        if not reference_uri.startswith(_FILE_REF_PREFIX):
            raise CredentialBackendError(
                f"file_ref backend refuses reference_uri {reference_uri!r}"
            )
        opaque = reference_uri[len(_FILE_REF_PREFIX):]
        path = self._paths.get(opaque)
        if path is None:
            raise CredentialBackendError(
                f"file_ref opaque {opaque!r} not registered in backend"
            )
        # The path is trusted (supplied by the host operator) but we still
        # refuse symlinks.
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise CredentialBackendError(
                f"file_ref material for opaque {opaque!r} is not accessible"
            ) from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise CredentialBackendError(
                f"file_ref material for opaque {opaque!r} is a symlink; refusing"
            )
        if not stat_module.S_ISREG(st.st_mode):
            raise CredentialBackendError(
                f"file_ref material for opaque {opaque!r} is not a regular file"
            )
        try:
            with open(path, "rb") as fh:  # noqa: PTH123 - intentional low-level read
                data = fh.read()
        except OSError as exc:
            raise CredentialBackendError(
                f"file_ref material for opaque {opaque!r} could not be read"
            ) from exc
        if not data:
            raise CredentialBackendError(
                f"file_ref material for opaque {opaque!r} is empty; refusing"
            )
        return data


@dataclass(frozen=True)
class BackendRegistry:
    """Bind backend names to backend instances at CredentialBroker construction."""

    backends: dict[str, CredentialBackend]

    def get(self, name: str) -> CredentialBackend:
        _check_backend(name)
        try:
            return self.backends[name]
        except KeyError as exc:
            raise CredentialBackendError(f"backend {name!r} not registered") from exc


# ---------------------------------------------------------------------------
# Credential reference record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialRecord:
    payload: dict[str, Any]
    on_disk_sha256: str = ""

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def status(self) -> str:
        return self.payload["status"]

    @property
    def credential_epoch(self) -> int:
        return int(self.payload["credential_epoch"])

    @property
    def reference_uri(self) -> str:
        return self.payload["reference_uri"]

    @property
    def backend(self) -> str:
        return self.payload["backend"]

    @property
    def rotation_state(self) -> str:
        return self.payload["rotation"]["state"]


class CredentialRefStore:
    """Single-writer, dirfd-anchored credential reference store."""

    def __init__(self, layout: I.InstanceLayout) -> None:
        self.layout = layout
        self._tenant_registry = R.TenantRegistry(layout)

    def initialize(self) -> "CredentialRefStore":
        _ensure_registry_dirs(self.layout)
        return self

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, tenant_id: str) -> CredentialRecord:
        I.validate_tenant_id(tenant_id)
        with _credentials_fd(self.layout) as fd:
            try:
                raw = A.read_file(fd, f"{tenant_id}.json")
            except FileNotFoundError as exc:
                raise CredentialNotFound(tenant_id) from exc
        try:
            payload = C.strict_json_loads(raw.decode("utf-8"))
        except C.ContractError as exc:
            raise CredentialCorruption(f"credential record for {tenant_id!r} is not strict JSON: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise CredentialCorruption(f"credential record for {tenant_id!r} is not UTF-8") from exc
        validate_credential_ref(payload)
        if payload["tenant_id"] != tenant_id:
            raise CredentialCorruption(
                f"credential record filename does not match tenant_id {tenant_id!r}"
            )
        return CredentialRecord(payload=payload, on_disk_sha256=hashlib.sha256(raw).hexdigest())

    def list_tenants(self) -> list[str]:
        results: list[str] = []
        try:
            with _credentials_fd(self.layout) as fd:
                for entry in os.scandir(fd):
                    name = entry.name
                    if not name.endswith(".json"):
                        continue
                    tid = name[:-5]
                    try:
                        I.validate_tenant_id(tid)
                    except I.TenantIdError:
                        continue
                    results.append(tid)
        except FileNotFoundError:
            return []
        results.sort()
        return results

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def set_reference(
        self,
        *,
        tenant_id: str,
        reference_uri: str,
        backend: str,
        actor: str,
        reason: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        _check_actor_reason(actor, reason)
        I.validate_tenant_id(tenant_id)
        _check_reference_uri(reference_uri)
        _check_backend(backend)

        tenant = self._tenant_registry.get(tenant_id)
        # `set-credential` is an admin_configure action; RT-012 matrix
        # forbids offboarded.  We permit draft/profile_pending/pilot/active/
        # suspended (admin can rotate/revoke on suspended).
        if tenant.status == "offboarded":
            raise CredentialStateError(
                f"tenant status {tenant.status!r} does not permit set-credential"
            )
        try:
            prior = self.get(tenant_id)
            prior_epoch = prior.credential_epoch
            prior_sha: Optional[str] = prior.on_disk_sha256
        except CredentialNotFound:
            prior = None
            prior_epoch = 0
            prior_sha = None

        new_epoch = prior_epoch + 1
        if new_epoch > C.IJSON_MAX_SAFE_INT:
            raise CredentialSchemaError("credential epoch overflow")

        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()
        payload = {
            "schema": CREDENTIAL_REF_SCHEMA,
            "tenant_id": tenant_id,
            "reference_uri": reference_uri,
            "backend": backend,
            "status": "active",
            "credential_epoch": new_epoch,
            "created_at": prior.payload["created_at"] if prior else now,
            "updated_at": now,
            "rotation": {
                "state": "stable",
                "secondary_reference_uri": None,
                "secondary_backend": None,
                "secondary_credential_epoch": None,
                "previous_credential_epoch": prior_epoch or None,
            },
            "provisioning": {
                "last_receipt_id": receipt_id,
                "last_receipt_sha256": "0" * 64,
            },
            "history": (list(prior.payload["history"]) if prior else []) + [
                {
                    "action": "set",
                    "at": now,
                    "actor": actor,
                    "reason": reason,
                    "credential_epoch_after": new_epoch,
                    "receipt_id": receipt_id,
                }
            ],
        }
        receipt = self._build_receipt(
            payload=payload,
            action="set",
            actor=actor,
            reason=reason,
            receipt_id=receipt_id,
            now=now,
        )
        payload["provisioning"]["last_receipt_sha256"] = receipt["receipt_sha256"]

        validate_credential_ref(payload)
        self._commit(
            tenant_id=tenant_id,
            payload=payload,
            receipt=receipt,
            action="set",
            actor=actor,
            reason=reason,
            txn_id=txn_id,
            expected_previous_sha256=prior_sha,
        )
        return CredentialRecord(payload=payload), receipt

    def disable(
        self,
        *,
        tenant_id: str,
        actor: str,
        reason: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        return self._simple_status_change(
            tenant_id=tenant_id,
            actor=actor,
            reason=reason,
            action="disable",
            new_status="disabled",
        )

    def revoke(
        self,
        *,
        tenant_id: str,
        actor: str,
        reason: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        return self._simple_status_change(
            tenant_id=tenant_id,
            actor=actor,
            reason=reason,
            action="revoke",
            new_status="revoked",
        )

    def rotate_begin(
        self,
        *,
        tenant_id: str,
        new_reference_uri: str,
        new_backend: str,
        actor: str,
        reason: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        _check_actor_reason(actor, reason)
        _check_reference_uri(new_reference_uri)
        _check_backend(new_backend)
        prior = self.get(tenant_id)
        if prior.rotation_state != "stable":
            raise CredentialConflictError(
                f"rotation already in-flight (state={prior.rotation_state!r})"
            )
        if prior.status != "active":
            raise CredentialStateError(
                f"cannot begin rotation while status={prior.status!r}"
            )
        if new_reference_uri == prior.reference_uri and new_backend == prior.backend:
            raise CredentialConflictError(
                "new_reference_uri equals current; rotation is a no-op"
            )
        new_epoch = prior.credential_epoch + 1
        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()
        payload = dict(prior.payload)
        payload["status"] = "rotating"
        payload["credential_epoch"] = new_epoch
        payload["updated_at"] = now
        payload["rotation"] = {
            "state": "dual_write",
            "secondary_reference_uri": new_reference_uri,
            "secondary_backend": new_backend,
            "secondary_credential_epoch": new_epoch,
            "previous_credential_epoch": prior.credential_epoch,
        }
        history = list(payload["history"])
        history.append(
            {
                "action": "rotation_begin",
                "at": now,
                "actor": actor,
                "reason": reason,
                "credential_epoch_after": new_epoch,
                "receipt_id": receipt_id,
            }
        )
        payload["history"] = history
        payload["provisioning"] = {
            "last_receipt_id": receipt_id,
            "last_receipt_sha256": "0" * 64,
        }
        receipt = self._build_receipt(
            payload=payload,
            action="rotation_begin",
            actor=actor,
            reason=reason,
            receipt_id=receipt_id,
            now=now,
        )
        payload["provisioning"]["last_receipt_sha256"] = receipt["receipt_sha256"]
        validate_credential_ref(payload)
        self._commit(
            tenant_id=tenant_id,
            payload=payload,
            receipt=receipt,
            action="rotation_begin",
            actor=actor,
            reason=reason,
            txn_id=txn_id,
            expected_previous_sha256=prior.on_disk_sha256,
        )
        return CredentialRecord(payload=payload), receipt

    def rotate_finalize(
        self,
        *,
        tenant_id: str,
        actor: str,
        reason: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        _check_actor_reason(actor, reason)
        prior = self.get(tenant_id)
        if prior.rotation_state != "dual_write":
            raise CredentialConflictError(
                f"rotation not in dual_write (state={prior.rotation_state!r})"
            )
        secondary_uri = prior.payload["rotation"]["secondary_reference_uri"]
        secondary_backend = prior.payload["rotation"]["secondary_backend"]
        if secondary_uri is None or secondary_backend is None:
            raise CredentialSchemaError("dual_write pointer missing secondary reference")

        # Tombstone the old reference first.
        old_ref_snapshot = {
            "schema": "cwk.rt013.credential_ref_tombstone.v1_internal",
            "tenant_id": tenant_id,
            "reference_uri": prior.reference_uri,
            "backend": prior.backend,
            "credential_epoch": prior.payload["rotation"]["previous_credential_epoch"],
            "tombstoned_at": _utcnow_iso(),
            "actor": actor,
            "reason": reason,
        }
        with _tombstone_epoch(self.layout, prior.payload["rotation"]["previous_credential_epoch"] or 1) as tfd:
            body = (json.dumps(old_ref_snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            A.write_atomic(tfd, f"{tenant_id}.json", body, exclusive=True)

        # Atomic pointer swap on the record.
        new_epoch = prior.credential_epoch + 1
        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()
        payload = dict(prior.payload)
        payload["reference_uri"] = secondary_uri
        payload["backend"] = secondary_backend
        payload["status"] = "active"
        payload["credential_epoch"] = new_epoch
        payload["updated_at"] = now
        payload["rotation"] = {
            "state": "stable",
            "secondary_reference_uri": None,
            "secondary_backend": None,
            "secondary_credential_epoch": None,
            "previous_credential_epoch": prior.credential_epoch,
        }
        history = list(payload["history"])
        history.append(
            {
                "action": "rotation_finalize",
                "at": now,
                "actor": actor,
                "reason": reason,
                "credential_epoch_after": new_epoch,
                "receipt_id": receipt_id,
            }
        )
        payload["history"] = history
        payload["provisioning"] = {
            "last_receipt_id": receipt_id,
            "last_receipt_sha256": "0" * 64,
        }
        receipt = self._build_receipt(
            payload=payload,
            action="rotation_finalize",
            actor=actor,
            reason=reason,
            receipt_id=receipt_id,
            now=now,
        )
        payload["provisioning"]["last_receipt_sha256"] = receipt["receipt_sha256"]
        validate_credential_ref(payload)
        self._commit(
            tenant_id=tenant_id,
            payload=payload,
            receipt=receipt,
            action="rotation_finalize",
            actor=actor,
            reason=reason,
            txn_id=txn_id,
            expected_previous_sha256=prior.on_disk_sha256,
        )
        return CredentialRecord(payload=payload), receipt

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        _ensure_registry_dirs(self.layout)
        summary = {"orphans_removed": 0, "journal_swept": 0}
        for sub in _CREDENTIAL_SUBDIRS:
            if sub == "tombstone":
                continue
            with _credentials_sub(self.layout, sub) as fd:
                summary["orphans_removed"] += len(A.recover_orphans(fd))
        with _credentials_sub(self.layout, "journal") as jfd:
            names = [e.name for e in os.scandir(jfd) if e.name.endswith(".journal")]
        for name in names:
            with _credentials_sub(self.layout, "journal") as jfd:
                A.unlink_at(jfd, name, missing_ok=True)
            summary["journal_swept"] += 1
        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _simple_status_change(
        self,
        *,
        tenant_id: str,
        actor: str,
        reason: str,
        action: str,
        new_status: str,
    ) -> tuple[CredentialRecord, dict[str, Any]]:
        _check_actor_reason(actor, reason)
        prior = self.get(tenant_id)
        if prior.status == new_status:
            raise CredentialConflictError(
                f"credential status already {new_status!r}"
            )
        if prior.rotation_state != "stable":
            raise CredentialConflictError(
                f"cannot {action!r} while rotation in-flight"
            )
        new_epoch = prior.credential_epoch + 1
        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()
        payload = dict(prior.payload)
        payload["status"] = new_status
        payload["credential_epoch"] = new_epoch
        payload["updated_at"] = now
        history = list(payload["history"])
        history.append(
            {
                "action": action,
                "at": now,
                "actor": actor,
                "reason": reason,
                "credential_epoch_after": new_epoch,
                "receipt_id": receipt_id,
            }
        )
        payload["history"] = history
        payload["provisioning"] = {
            "last_receipt_id": receipt_id,
            "last_receipt_sha256": "0" * 64,
        }
        receipt = self._build_receipt(
            payload=payload,
            action=action,
            actor=actor,
            reason=reason,
            receipt_id=receipt_id,
            now=now,
        )
        payload["provisioning"]["last_receipt_sha256"] = receipt["receipt_sha256"]
        validate_credential_ref(payload)
        self._commit(
            tenant_id=tenant_id,
            payload=payload,
            receipt=receipt,
            action=action,
            actor=actor,
            reason=reason,
            txn_id=txn_id,
            expected_previous_sha256=prior.on_disk_sha256,
        )
        return CredentialRecord(payload=payload), receipt

    def _build_receipt(
        self,
        *,
        payload: dict[str, Any],
        action: str,
        actor: str,
        reason: str,
        receipt_id: str,
        now: str,
    ) -> dict[str, Any]:
        base = {
            "schema": "cwk.rt013.credential_receipt.v1_internal",
            "receipt_id": receipt_id,
            "tenant_id": payload["tenant_id"],
            "action": action,
            "credential_epoch_after": payload["credential_epoch"],
            "committed_at": now,
            "actor": actor,
            "reason": reason,
            "reference_uri": payload["reference_uri"],
            "backend": payload["backend"],
            "rotation_state": payload["rotation"]["state"],
        }
        base["receipt_sha256"] = _canonical_sha256(base)
        return base

    def _commit(
        self,
        *,
        tenant_id: str,
        payload: dict[str, Any],
        receipt: dict[str, Any],
        action: str,
        actor: str,
        reason: str,
        txn_id: str,
        expected_previous_sha256: Optional[str],
    ) -> None:
        # 1. Journal.
        journal = {
            "schema": "cwk.rt013.credential_journal.v1_internal",
            "txn_id": txn_id,
            "action": action,
            "actor": actor,
            "reason": reason,
            "tenant_id": tenant_id,
            "started_at": _utcnow_iso(),
        }
        jbody = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _credentials_sub(self.layout, "journal") as jfd:
            A.write_atomic(jfd, f"{txn_id}.journal", jbody, exclusive=True)

        # 2. Record.
        rbody = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _credentials_fd(self.layout) as fd:
            with A.exclusive_lock(fd, f".{tenant_id}.lock"):
                A.cas_write(
                    fd,
                    f"{tenant_id}.json",
                    rbody,
                    expected_previous_sha256=expected_previous_sha256,
                )

        # 3. Receipt.
        rcbody = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _credentials_sub(self.layout, "receipts") as fd:
            A.write_atomic(
                fd,
                f"{receipt['receipt_id']}.json",
                rcbody,
                exclusive=True,
            )

        # 4. Bump tenant auth_epoch so downstream caches / broker sessions
        # observe the credential mutation.
        self._bump_tenant_auth_epoch(tenant_id, actor=actor, reason=f"credential_{action}: {reason}")

        # 5. Sweep journal.
        with _credentials_sub(self.layout, "journal") as jfd:
            A.unlink_at(jfd, f"{txn_id}.journal", missing_ok=True)

    def _bump_tenant_auth_epoch(
        self, tenant_id: str, *, actor: str, reason: str
    ) -> None:
        tenant = self._tenant_registry.get(tenant_id)
        try:
            self._tenant_registry.bump_auth_epoch(
                tenant_id,
                actor=actor[:128],
                reason=reason[:256],
                expected_auth_epoch=tenant.auth_epoch,
            )
        except R.RegistryConflict:
            tenant = self._tenant_registry.get(tenant_id)
            self._tenant_registry.bump_auth_epoch(
                tenant_id,
                actor=actor[:128],
                reason=reason[:256],
                expected_auth_epoch=tenant.auth_epoch,
            )


# ---------------------------------------------------------------------------
# Credential Lease
# ---------------------------------------------------------------------------


class CredentialLease:
    """Context manager wrapping a short-lived material acquisition.

    On ``__enter__`` the material is loaded from the backend into a
    :class:`bytearray` that we can zero on ``__exit__``.  The public
    :attr:`env` dict carries the material as a UTF-8 string under
    :data:`ENV_MATERIAL_KEY` alongside the frozen inherit-whitelist keys
    (only ``CWK_INSTANCE_ROOT`` today).  The material key value is best-
    effort scrubbed on exit; the :attr:`env` dict itself becomes empty.

    Nothing about the material is written to logs, audits, receipts, or
    stringified representations.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        agent_id_hash: str,
        reference_uri: str,
        backend: str,
        credential_epoch: int,
        purpose: str,
        tenant_status: str,
        inherit_env: Mapping[str, str],
    ) -> None:
        self.tenant_id = tenant_id
        self.agent_id_hash = agent_id_hash
        self.reference_uri = reference_uri
        self.backend = backend
        self.credential_epoch = credential_epoch
        self.purpose = purpose
        self.tenant_status = tenant_status
        self.lease_id = _new_lease_id()
        self.issued_at = _utcnow_iso()
        self._released_at: Optional[str] = None
        self._env: dict[str, str] = {
            k: v for k, v in inherit_env.items() if k in ENV_WHITELIST_INHERIT
        }
        self._material: Optional[bytearray] = None

    @property
    def env(self) -> Mapping[str, str]:
        return self._env

    @property
    def released_at(self) -> str:
        if self._released_at is None:
            raise CredentialError("lease has not been released", code="state")
        return self._released_at

    def _install_material(self, material: bytes) -> None:
        buf = bytearray(material)
        self._material = buf
        # Decode UTF-8 into a fresh string that the subprocess env can carry;
        # keeping the bytearray so we can also zero the *bytes* view.
        self._env[ENV_MATERIAL_KEY] = buf.decode("utf-8")

    def __enter__(self) -> "CredentialLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Best-effort scrub.
        if self._material is not None:
            for i in range(len(self._material)):
                self._material[i] = 0
            self._material = None
        # Replace the env string with a zero-length string first, then
        # remove the key entirely to reduce residency.
        if ENV_MATERIAL_KEY in self._env:
            self._env[ENV_MATERIAL_KEY] = ""
            del self._env[ENV_MATERIAL_KEY]
        self._env.clear()
        self._released_at = _utcnow_iso()

    def receipt(self) -> dict[str, Any]:
        """Return the audit receipt for this lease.

        MUST be called after :meth:`__exit__` — it references
        :attr:`released_at`.  The receipt intentionally does not carry the
        material.
        """

        base = {
            "schema": BROKER_LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "tenant_id": self.tenant_id,
            "agent_id_hash": self.agent_id_hash,
            "reference_uri": self.reference_uri,
            "backend": self.backend,
            "credential_epoch": self.credential_epoch,
            "purpose": self.purpose,
            "issued_at": self.issued_at,
            "released_at": self.released_at,
            "tenant_status": self.tenant_status,
            "receipt_sha256": "0" * 64,
        }
        base["receipt_sha256"] = _canonical_sha256(
            {k: v for k, v in base.items() if k != "receipt_sha256"}
        )
        validate_broker_lease(base)
        return base

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # NEVER print the material.  Only the lease id + hash prefix.
        return (
            f"CredentialLease(lease_id={self.lease_id!r}, tenant={self.tenant_id[:8]}..., "
            f"credential_epoch={self.credential_epoch}, purpose={self.purpose!r})"
        )


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


class CredentialBroker:
    """Trusted broker that mediates every material access.

    A single broker instance is scoped to one host process (Collector /
    Scheduler / Query Broker).  It holds the backend registry that binds
    reference URIs to concrete backends; callers reach material only via
    :meth:`lease`.  Concrete backends (env / file) are supplied at
    construction — the broker refuses to lease if a reference points at a
    backend name it does not know.
    """

    def __init__(
        self,
        *,
        layout: I.InstanceLayout,
        backends: BackendRegistry,
        inherit_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.layout = layout
        self.backends = backends
        # Snapshot the inherit env at construction time so a downstream
        # caller cannot mutate os.environ mid-lease to smuggle a new key.
        self._inherit_env: dict[str, str] = {
            k: v for k, v in (inherit_env if inherit_env is not None else os.environ).items()
            if k in ENV_WHITELIST_INHERIT
        }
        self.store = CredentialRefStore(layout).initialize()
        self._tenant_registry = R.TenantRegistry(layout)

    @contextmanager
    def lease(
        self,
        *,
        agent_id_hash: str,
        tenant_id: str,
        purpose: str,
    ) -> Iterator[CredentialLease]:
        """Yield a :class:`CredentialLease` for ``tenant_id`` / ``purpose``.

        The caller MUST use ``with``; on exit the material is scrubbed.
        Never leases:

        - if the tenant is not in a status the purpose permits;
        - if the credential record is not ``active`` (``disabled/revoked/
          rotating`` all fail closed);
        - if the reference points at an unknown or unregistered backend;
        - if the backend refuses to hand out material for any reason.

        Never falls back to the host's ``.env`` file, another tenant's
        credential, or any environment variable outside
        :data:`ENV_WHITELIST_INHERIT`.
        """

        I.validate_tenant_id(tenant_id)
        if not isinstance(agent_id_hash, str) or len(agent_id_hash) != 64:
            raise CredentialSchemaError("agent_id_hash must be 64-char hex")
        allowed_statuses = _PURPOSE_ALLOWED_STATUSES.get(purpose)
        if allowed_statuses is None:
            raise CredentialPolicyError(f"purpose {purpose!r} is not brokered")

        tenant = self._tenant_registry.get(tenant_id)
        if tenant.status not in allowed_statuses:
            raise CredentialPolicyError(
                f"tenant status {tenant.status!r} does not permit purpose {purpose!r}"
            )
        # Double check against RT-012 operation matrix.
        matrix = R.TENANT_OPERATION_MATRIX.get(tenant.status, frozenset())
        if purpose not in matrix:
            raise CredentialPolicyError(
                f"tenant operation matrix rejects purpose {purpose!r} in status {tenant.status!r}"
            )

        record = self.store.get(tenant_id)
        if record.status != "active":
            raise CredentialStateError(
                f"credential status {record.status!r} does not permit lease"
            )
        # During dual_write the ACTIVE reference is the current one; the
        # secondary is a preview only.  Broker refuses to lease during
        # rotation to keep the invariant "no mixed old/new state".
        if record.rotation_state != "stable":
            raise CredentialStateError(
                f"credential rotation in-flight (state={record.rotation_state!r})"
            )

        backend = self.backends.get(record.backend)
        try:
            material = backend.read_material(record.reference_uri)
        except CredentialBackendError:
            raise
        # Nothing between here and the yield may raise on the caller's
        # behalf without scrubbing; we build the lease which owns the
        # bytearray copy and zeros it on exit.
        lease = CredentialLease(
            tenant_id=tenant_id,
            agent_id_hash=agent_id_hash,
            reference_uri=record.reference_uri,
            backend=record.backend,
            credential_epoch=record.credential_epoch,
            purpose=purpose,
            tenant_status=tenant.status,
            inherit_env=self._inherit_env,
        )
        try:
            lease._install_material(material)  # noqa: SLF001 - intentional friend method
        finally:
            # Never let the raw material bytes linger in a local variable.
            if isinstance(material, bytearray):
                for i in range(len(material)):
                    material[i] = 0
            material = None  # noqa: F841

        with lease as active:
            yield active


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def doctor_credential_store(layout: I.InstanceLayout) -> list[dict[str, Any]]:
    """Return structural findings for the credential store.

    Never reads material.  Only checks records, receipts, journals and
    tombstone paths.
    """

    findings: list[dict[str, Any]] = []
    _ensure_registry_dirs(layout)
    store = CredentialRefStore(layout)
    for tid in store.list_tenants():
        try:
            rec = store.get(tid)
        except CredentialCorruption as exc:
            findings.append(
                {"name": f"credential_corrupt:{tid}", "severity": "error", "status": "issue", "detail": str(exc)}
            )
            continue
        if rec.status not in CREDENTIAL_STATES:
            findings.append(
                {"name": f"credential_state:{tid}", "severity": "error", "status": "issue", "detail": f"unknown status {rec.status!r}"}
            )
    # Journal residue.
    try:
        with _credentials_sub(layout, "journal") as jfd:
            pending = [e.name for e in os.scandir(jfd) if not e.name.startswith(A.TEMP_PREFIX)]
        if pending:
            findings.append(
                {
                    "name": "credential_journal_residue",
                    "severity": "warn",
                    "status": "issue",
                    "detail": f"{len(pending)} pending journal entr(y|ies)",
                }
            )
    except FileNotFoundError:
        pass
    return findings


__all__ = [
    "BROKER_LEASE_SCHEMA",
    "BackendRegistry",
    "CREDENTIAL_BACKENDS",
    "CREDENTIAL_REF_SCHEMA",
    "CREDENTIAL_STATES",
    "CredentialBackend",
    "CredentialBackendError",
    "CredentialBroker",
    "CredentialConflictError",
    "CredentialCorruption",
    "CredentialError",
    "CredentialLease",
    "CredentialNotFound",
    "CredentialPolicyError",
    "CredentialRecord",
    "CredentialRefStore",
    "CredentialSchemaError",
    "CredentialStateError",
    "ENV_MATERIAL_KEY",
    "ENV_WHITELIST_INHERIT",
    "EnvRefBackend",
    "FileRefBackend",
    "doctor_credential_store",
    "validate_broker_lease",
    "validate_credential_ref",
]
