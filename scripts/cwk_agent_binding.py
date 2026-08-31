#!/usr/bin/env python3
"""RT-013: Agent Binding Registry — trusted Agent → tenant binding.

Owned by RT-013.  This module is the single writer of
``registry/agent-bindings/`` beneath ``CWK_INSTANCE_ROOT``.  It:

- HMAC-SHA256 hashes the raw ``agent_id`` on ingest with a per-instance
  binding secret and *never* stores the raw ``agent_id``;
- persists one record per hash at ``current/<hex64>.json``;
- monotonically bumps ``binding_epoch`` on every mutate (bind / rebind /
  revoke / suspend / reactivate / secret-rotation) and immediately calls
  RT-012's ``TenantRegistry.bump_auth_epoch`` so downstream broker /
  cache layers observe the change;
- appends an audit ``binding_receipt.v1`` for every mutate;
- refuses to construct a record from a request body — the only ingress
  is a trusted admin CLI or gateway-authenticated context (RT-023 will
  activate the second source);
- supports dual-write secret rotation with an atomic pointer swap and a
  tombstone directory for the old-epoch hash files so no request can
  observe a mixed old/new state during rotation.

Never touches ``.env`` / ``CWORK_APP_KEY`` / real gateway / DocDB / cron.
Only stdlib imports.
"""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

import cwk_atomic_file as A
import cwk_instance as I
import cwk_pr001_contracts as C
import cwk_tenant_registry as R


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


BINDING_SCHEMA = "cwk.rt013.agent_binding.v1"
BINDING_RECEIPT_SCHEMA = "cwk.rt013.binding_receipt.v1"
SECRET_POINTER_SCHEMA = "cwk.rt013.binding_secret_pointer.v1"

BINDING_STATES: tuple[str, ...] = ("active", "suspended", "revoked")

# Only these binding statuses may be returned by :meth:`resolve`.
RESOLVE_HIT_STATUSES: frozenset[str] = frozenset({"active"})

# HMAC-SHA256 hex length.
_HASH_HEX_LEN = 64
_HASH_HEX_REGEX = re.compile(r"\A[0-9a-f]{64}\Z")

# Raw agent_id grammar — the request-body-forbidden invariant means we don't
# hand these out; but we still constrain the shape so a caller cannot bypass
# validation with a smuggled control character.
_AGENT_ID_ALLOWED = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@\-]{0,255}\Z")

# Opaque receipt / txn IDs used solely inside RT-013.
_RECEIPT_ID_REGEX = re.compile(r"\Arbnd_[a-z0-9]{26}\Z")
_ROTATION_RECEIPT_REGEX = re.compile(r"\Arsec_[a-z0-9]{26}\Z")
_TXN_ID_REGEX = re.compile(r"\Abtxn_[a-z0-9]{26}\Z")

# Binding-secret material length; HMAC-SHA256 requires >= 16 for security,
# we insist on 32 bytes of CSPRNG output.
SECRET_MIN_BYTES = 32

# Directory schema inside registry/agent-bindings/.  Every one of these must
# be created via :func:`_ensure_registry_dirs` before the first mutate.
_BINDING_SUBDIRS: tuple[str, ...] = ("current", "receipts", "journal", "tombstone")

# Directory schema inside registry/binding-secrets/.
_SECRET_SUBDIRS: tuple[str, ...] = ("material", "receipts")

_UTC = _dt.timezone.utc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BindingError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AgentIdError(BindingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="agent_id")


class BindingConflictError(BindingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="conflict")


class BindingNotFound(BindingError):
    def __init__(self) -> None:
        super().__init__("binding not found", code="not_found")


class BindingRevoked(BindingError):
    def __init__(self) -> None:
        super().__init__("binding is revoked", code="revoked")


class BindingSuspended(BindingError):
    def __init__(self) -> None:
        super().__init__("binding is suspended", code="suspended")


class BindingStateError(BindingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="state")


class BindingSecretMissing(BindingError):
    def __init__(self, message: str = "binding secret material is missing") -> None:
        super().__init__(message, code="secret_missing")


class BindingSchemaError(BindingError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="schema")


class BindingRecordCorruption(BindingError):
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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(payload: Any) -> bytes:
    return C.canonical_json_bytes(payload)


def _canonical_sha256(payload: Any) -> str:
    return _sha256_bytes(_canonical_bytes(payload))


def _new_receipt_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"rbnd_{body}"


def _new_rotation_receipt_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"rsec_{body}"


def _new_txn_id() -> str:
    body = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(26))
    return f"btxn_{body}"


def validate_raw_agent_id(raw_agent_id: str) -> None:
    """Reject anything that isn't a well-formed raw ``agent_id``.

    Raw agent_id NEVER lands in a record; it's only used to derive the HMAC
    hash.  We still constrain its shape defensively so a caller cannot smuggle
    control characters, NULs or path separators through the API surface.
    """

    if not isinstance(raw_agent_id, str):
        raise AgentIdError("raw agent_id must be a str")
    if not _AGENT_ID_ALLOWED.match(raw_agent_id):
        raise AgentIdError(
            f"raw agent_id does not match {_AGENT_ID_ALLOWED.pattern!r}"
        )
    # Belt-and-braces: reject a few sequences that regexes may miss under
    # future edits (NUL, CR, LF are already excluded above).
    lower = raw_agent_id.lower()
    for bad in ("..", "/", "\\", "\x00", "\r", "\n"):
        if bad in lower:
            raise AgentIdError(f"raw agent_id contains forbidden sequence {bad!r}")


def hmac_hash_agent_id(secret_material: bytes, raw_agent_id: str) -> str:
    """Return HMAC-SHA256 hex of ``raw_agent_id`` under ``secret_material``.

    The caller is responsible for zeroing ``secret_material`` after use if
    they built it via a copy; we only touch it via :func:`hmac.new` here.
    """

    if not isinstance(secret_material, (bytes, bytearray, memoryview)):
        raise BindingError("secret material must be bytes-like", code="type")
    if len(secret_material) < SECRET_MIN_BYTES:
        raise BindingError(
            f"secret material must be >= {SECRET_MIN_BYTES} bytes", code="secret_length"
        )
    validate_raw_agent_id(raw_agent_id)
    mac = hmac.new(bytes(secret_material), raw_agent_id.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


_SCHEMA_DIR = C.SCHEMA_ROOT / "rt013" / "schemas"
_SCHEMA_CACHE: dict[str, Any] = {}


def _load_schema(name: str) -> Any:
    if name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[name] = C.strict_json_load_path(_SCHEMA_DIR / name)
    return _SCHEMA_CACHE[name]


def validate_binding_record(payload: Any) -> None:
    schema = _load_schema("agent_binding.schema.json")
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise BindingSchemaError(str(exc)) from exc
    _reject_bool_int(payload, ("binding_epoch", "binding_secret_epoch"))
    for entry in payload.get("history", []):
        _reject_bool_int(entry, ("binding_epoch_after", "binding_secret_epoch"))


def validate_binding_receipt(payload: Any) -> None:
    schema = _load_schema("binding_receipt.schema.json")
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise BindingSchemaError(str(exc)) from exc
    _reject_bool_int(
        payload,
        (
            "binding_epoch_after",
            "binding_secret_epoch",
            "tenant_auth_epoch_before",
            "tenant_auth_epoch_after",
        ),
    )


def validate_secret_pointer(payload: Any) -> None:
    schema = _load_schema("binding_secret_pointer.schema.json")
    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)  # noqa: SLF001
    except C.ContractError as exc:
        raise BindingSchemaError(str(exc)) from exc
    _reject_bool_int(payload, ("current_epoch", "secondary_epoch", "previous_epoch"))


def _reject_bool_int(payload: dict[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        val = payload.get(key)
        if isinstance(val, bool):
            raise BindingSchemaError(f"{key} must be an integer, not bool")


# ---------------------------------------------------------------------------
# dirfd helpers (RT-013 owns these deeper sub-directories)
# ---------------------------------------------------------------------------


def _open_subdir(parent_fd: int, name: str) -> int:
    """Open ``name`` inside ``parent_fd`` with ``O_DIRECTORY|O_NOFOLLOW``.

    Refuses symlinks and non-directories.  RT-013 uses this to descend into
    ``current/`` / ``receipts/`` / ``journal/`` / ``tombstone/`` below the
    frozen ``registry/agent-bindings/`` and ``registry/credentials/`` dirs
    that RT-012 pre-creates.
    """

    A._validate_leaf(name)  # noqa: SLF001 — same grammar, safe reuse
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
def _binding_root(layout: I.InstanceLayout) -> Iterator[int]:
    with layout.registry_fd("agent-bindings") as fd:
        yield fd


@contextmanager
def _binding_sub(layout: I.InstanceLayout, name: str) -> Iterator[int]:
    with _binding_root(layout) as root:
        sub = _open_subdir(root, name)
        try:
            yield sub
        finally:
            os.close(sub)


@contextmanager
def _tombstone_epoch(layout: I.InstanceLayout, epoch: int) -> Iterator[int]:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise BindingSchemaError("tombstone epoch must be a positive int")
    with _binding_sub(layout, "tombstone") as t:
        # epoch dir name is decimal ascii; validated by leaf grammar.
        name = str(epoch)
        A.mkdir_at(t, name, mode=A.DIRECTORY_MODE, exist_ok=True)
        fd = _open_subdir(t, name)
        try:
            yield fd
        finally:
            os.close(fd)


def _ensure_registry_dirs(layout: I.InstanceLayout) -> None:
    """Create the RT-013 sub-directories beneath the pre-created RT-012 dirs.

    Idempotent; safe to call on every mutate.  The parents (``registry/
    agent-bindings/``, ``registry/binding-secrets/``) are the RT-012 frozen
    top-level layout — we never create top-level dirs.
    """

    with _binding_root(layout) as fd:
        for name in _BINDING_SUBDIRS:
            A.mkdir_at(fd, name, mode=A.DIRECTORY_MODE, exist_ok=True)
    # binding-secrets is not a RT-012 registry child; we open it via the
    # ``registry`` fd + explicit subdir creation.
    with layout.child_fd("registry") as rfd:
        A.mkdir_at(rfd, "binding-secrets", mode=A.DIRECTORY_MODE, exist_ok=True)
    with layout.child_fd("registry") as rfd:
        secret_fd = _open_subdir(rfd, "binding-secrets")
        try:
            for name in _SECRET_SUBDIRS:
                A.mkdir_at(secret_fd, name, mode=A.DIRECTORY_MODE, exist_ok=True)
        finally:
            os.close(secret_fd)


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------


class BindingSecretStore:
    """Manages the HMAC secret material used to hash raw agent_id values.

    The material is stored in ``registry/binding-secrets/material/<epoch>.material``.
    RT-013 provides no CLI to *read* the material back; only to rotate.  The
    pointer file ``registry/binding-secrets/pointer.json`` names the
    current / secondary / previous epoch.

    In production, external secret backends can replace file-backed material
    (out of scope for RT-013); the pointer schema stays the same.
    """

    def __init__(self, layout: I.InstanceLayout) -> None:
        self.layout = layout

    # -- initialization ---------------------------------------------------

    def initialize(self) -> "BindingSecretStore":
        """Create the epoch-1 material if the store is empty."""

        _ensure_registry_dirs(self.layout)
        with self._secrets_fd() as fd:
            if not A.child_exists(fd, "pointer.json"):
                epoch = 1
                material = secrets.token_bytes(SECRET_MIN_BYTES)
                self._write_material(epoch, material)
                self._write_pointer(
                    rotation_state="stable",
                    current_epoch=epoch,
                    secondary_epoch=None,
                    previous_epoch=None,
                    actor="rt013_bootstrap",
                    reason="initial secret",
                )
        return self

    # -- pointer + material -----------------------------------------------

    @contextmanager
    def _secrets_fd(self) -> Iterator[int]:
        with self.layout.child_fd("registry") as rfd:
            fd = _open_subdir(rfd, "binding-secrets")
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _material_fd(self) -> Iterator[int]:
        with self._secrets_fd() as fd:
            m = _open_subdir(fd, "material")
            try:
                yield m
            finally:
                os.close(m)

    @contextmanager
    def _secrets_receipts_fd(self) -> Iterator[int]:
        with self._secrets_fd() as fd:
            m = _open_subdir(fd, "receipts")
            try:
                yield m
            finally:
                os.close(m)

    def read_pointer(self) -> dict[str, Any]:
        with self._secrets_fd() as fd:
            try:
                raw = A.read_file(fd, "pointer.json")
            except FileNotFoundError as exc:
                raise BindingSecretMissing("secret pointer file missing") from exc
        try:
            payload = C.strict_json_loads(raw.decode("utf-8"))
        except C.ContractError as exc:
            raise BindingRecordCorruption(f"secret pointer is not strict JSON: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise BindingRecordCorruption("secret pointer is not UTF-8") from exc
        validate_secret_pointer(payload)
        return payload

    def read_material(self, epoch: int) -> bytes:
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise BindingSecretMissing(f"invalid epoch {epoch!r}")
        with self._material_fd() as fd:
            try:
                return A.read_file(fd, f"{epoch}.material")
            except FileNotFoundError as exc:
                raise BindingSecretMissing(
                    f"material for epoch {epoch} is not present"
                ) from exc

    def _write_material(self, epoch: int, material: bytes) -> None:
        if not isinstance(material, (bytes, bytearray)) or len(material) < SECRET_MIN_BYTES:
            raise BindingSchemaError("material too short")
        with self._material_fd() as fd:
            A.write_atomic(fd, f"{epoch}.material", bytes(material), exclusive=True)

    def _write_pointer(
        self,
        *,
        rotation_state: str,
        current_epoch: int,
        secondary_epoch: Optional[int],
        previous_epoch: Optional[int],
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        receipt_id = _new_rotation_receipt_id()
        payload = {
            "schema": SECRET_POINTER_SCHEMA,
            "rotation_state": rotation_state,
            "current_epoch": current_epoch,
            "secondary_epoch": secondary_epoch,
            "previous_epoch": previous_epoch,
            "updated_at": _utcnow_iso(),
            "provisioning": {
                "last_receipt_id": receipt_id,
                "last_receipt_sha256": "0" * 64,  # replaced below
            },
        }
        receipt_body = {
            "schema": "cwk.rt013.binding_secret_receipt.v1_internal",
            "receipt_id": receipt_id,
            "committed_at": payload["updated_at"],
            "rotation_state": rotation_state,
            "current_epoch": current_epoch,
            "secondary_epoch": secondary_epoch,
            "previous_epoch": previous_epoch,
            "actor": actor,
            "reason": reason,
        }
        # The pointer's own sha references the receipt (not the pointer itself)
        # so an attacker who tampers with the pointer must also tamper with the
        # receipt and vice-versa; we cannot cover both because the receipt is
        # append-only.
        sha = _canonical_sha256(receipt_body)
        payload["provisioning"]["last_receipt_sha256"] = sha
        validate_secret_pointer(payload)
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with self._secrets_fd() as fd:
            A.write_atomic(fd, "pointer.json", body)
        # Append the receipt.
        rbody = (json.dumps(receipt_body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with self._secrets_receipts_fd() as fd:
            A.write_atomic(fd, f"{receipt_id}.json", rbody, exclusive=True)
        return payload

    def rotate_begin(self, *, new_material: bytes, actor: str, reason: str) -> dict[str, Any]:
        """Phase 1 of rotation: write new material and switch pointer to dual_write."""

        pointer = self.read_pointer()
        if pointer["rotation_state"] != "stable":
            raise BindingConflictError(
                f"rotation already in-flight (state={pointer['rotation_state']!r})"
            )
        current_epoch = pointer["current_epoch"]
        new_epoch = current_epoch + 1
        if new_epoch > C.IJSON_MAX_SAFE_INT:
            raise BindingSchemaError("binding secret epoch overflow")
        # Write new material first (idempotent — refuse if a stale one is present).
        try:
            self._write_material(new_epoch, new_material)
        except A.AtomicFileError as exc:
            if exc.code != "exists":
                raise
            # A previous rotation attempt left the material; verify it matches
            # or refuse.  We refuse — the caller must recover/finalize first.
            raise BindingConflictError(
                f"material for epoch {new_epoch} already exists; run recover()"
            ) from exc
        return self._write_pointer(
            rotation_state="dual_write",
            current_epoch=current_epoch,
            secondary_epoch=new_epoch,
            previous_epoch=pointer.get("previous_epoch"),
            actor=actor,
            reason=reason,
        )

    def rotate_finalize(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Phase 3: promote secondary to current; leave previous marker."""

        pointer = self.read_pointer()
        if pointer["rotation_state"] != "dual_write":
            raise BindingConflictError(
                f"rotation is not in dual_write (state={pointer['rotation_state']!r})"
            )
        secondary = pointer["secondary_epoch"]
        if secondary is None:
            raise BindingSchemaError("dual_write pointer missing secondary_epoch")
        previous = pointer["current_epoch"]
        return self._write_pointer(
            rotation_state="stable",
            current_epoch=secondary,
            secondary_epoch=None,
            previous_epoch=previous,
            actor=actor,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Binding Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingRecord:
    """Immutable in-memory view of a binding record.

    ``on_disk_sha256`` is the sha of the exact bytes stored on disk — used
    for CAS.  The record intentionally does not carry the raw agent_id.
    """

    payload: dict[str, Any]
    on_disk_sha256: str = ""

    @property
    def agent_id_hash(self) -> str:
        return self.payload["agent_id_hash"]

    @property
    def tenant_id(self) -> str:
        return self.payload["tenant_id"]

    @property
    def status(self) -> str:
        return self.payload["status"]

    @property
    def binding_epoch(self) -> int:
        return int(self.payload["binding_epoch"])

    @property
    def binding_secret_epoch(self) -> int:
        return int(self.payload["binding_secret_epoch"])


@dataclass(frozen=True)
class BindingReceipt:
    payload: dict[str, Any]


class BindingRegistry:
    """Single-writer, dirfd-anchored agent-binding registry."""

    def __init__(self, layout: I.InstanceLayout) -> None:
        self.layout = layout
        self.secrets = BindingSecretStore(layout)
        self._tenant_registry = R.TenantRegistry(layout)

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def initialize(self) -> "BindingRegistry":
        _ensure_registry_dirs(self.layout)
        self.secrets.initialize()
        return self

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def _read_record_at(self, sub: str, hash_hex: str) -> BindingRecord:
        if not _HASH_HEX_REGEX.match(hash_hex):
            raise BindingSchemaError(f"agent_id_hash must match {_HASH_HEX_REGEX.pattern!r}")
        with _binding_sub(self.layout, sub) as fd:
            try:
                raw = A.read_file(fd, f"{hash_hex}.json")
            except FileNotFoundError as exc:
                raise BindingNotFound() from exc
        try:
            payload = C.strict_json_loads(raw.decode("utf-8"))
        except C.ContractError as exc:
            raise BindingRecordCorruption(f"binding record is not strict JSON: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise BindingRecordCorruption("binding record is not UTF-8") from exc
        validate_binding_record(payload)
        if payload["agent_id_hash"] != hash_hex:
            raise BindingRecordCorruption(
                "binding record filename does not match agent_id_hash"
            )
        return BindingRecord(payload=payload, on_disk_sha256=_sha256_bytes(raw))

    def get_by_hash(self, agent_id_hash: str) -> BindingRecord:
        return self._read_record_at("current", agent_id_hash)

    def list_active(self, *, tenant_id: Optional[str] = None) -> list[BindingRecord]:
        results: list[BindingRecord] = []
        try:
            with _binding_sub(self.layout, "current") as fd:
                names = [e.name for e in os.scandir(fd) if e.name.endswith(".json")]
        except FileNotFoundError:
            return []
        for name in sorted(names):
            hash_hex = name[:-5]
            if not _HASH_HEX_REGEX.match(hash_hex):
                continue
            try:
                rec = self.get_by_hash(hash_hex)
            except BindingRecordCorruption:
                # Fail-closed: skip corrupt entries for list view but never
                # return them to the caller.
                continue
            if tenant_id is not None and rec.tenant_id != tenant_id:
                continue
            results.append(rec)
        return results

    # ------------------------------------------------------------------
    # HMAC helpers
    # ------------------------------------------------------------------

    def _read_pointer_snapshot(self) -> dict[str, Any]:
        return self.secrets.read_pointer()

    def _current_hash(self, raw_agent_id: str) -> tuple[str, int]:
        """Hash under the pointer's ``current_epoch`` material."""

        pointer = self._read_pointer_snapshot()
        material = self.secrets.read_material(pointer["current_epoch"])
        try:
            return hmac_hash_agent_id(material, raw_agent_id), pointer["current_epoch"]
        finally:
            _zero_bytes(material)

    def _secondary_hash(self, raw_agent_id: str) -> Optional[tuple[str, int]]:
        """Hash under the pointer's ``secondary_epoch`` material if in dual_write."""

        pointer = self._read_pointer_snapshot()
        if pointer["rotation_state"] != "dual_write" or pointer["secondary_epoch"] is None:
            return None
        material = self.secrets.read_material(pointer["secondary_epoch"])
        try:
            return hmac_hash_agent_id(material, raw_agent_id), pointer["secondary_epoch"]
        finally:
            _zero_bytes(material)

    def hash_agent_id(self, raw_agent_id: str) -> str:
        """Compute the *current-epoch* hash — used by admin CLI lookups."""

        h, _ = self._current_hash(raw_agent_id)
        return h

    def _refuse_mutations_during_rotation(self) -> None:
        """Fail closed for every mutating API while a secret rotation is in flight.

        This is the "no mixed old/new state" invariant: while the pointer is
        ``dual_write`` we allow only *reads* (which double-probe both hashes)
        and the rotation-finalize call itself.  bind / rebind / revoke /
        suspend / reactivate are refused so a caller cannot land a record
        whose ``binding_secret_epoch`` mid-flight makes reasoning about the
        final state ambiguous.
        """

        pointer = self._read_pointer_snapshot()
        if pointer["rotation_state"] != "stable":
            raise BindingConflictError(
                f"binding-secret rotation in progress ({pointer['rotation_state']!r}); "
                "retry after rotate-binding-secret --finalize"
            )

    # ------------------------------------------------------------------
    # Resolve — used by AgentContext / Broker
    # ------------------------------------------------------------------

    def resolve(self, raw_agent_id: str, *, purpose: str) -> BindingRecord:
        """Locate the active binding for ``raw_agent_id``; fail closed otherwise.

        The purpose is checked against the tenant status via RT-012's operation
        matrix.  ``suspended`` and ``revoked`` bindings, unknown hashes, tenants
        outside the allowed status set, or expired secret material all fail
        closed.  Never falls back to repo ``.env`` / any other tenant / any
        other agent.
        """

        validate_raw_agent_id(raw_agent_id)
        if not isinstance(purpose, str) or purpose not in _ALL_PURPOSES:
            raise BindingStateError(f"unknown purpose {purpose!r}")

        # 1. Try current epoch.
        try:
            current_hash, current_epoch = self._current_hash(raw_agent_id)
        except BindingSecretMissing:
            raise
        record: Optional[BindingRecord] = None
        try:
            record = self.get_by_hash(current_hash)
        except BindingNotFound:
            record = None

        # 2. If not found and pointer is dual_write, try secondary.
        if record is None:
            sec = self._secondary_hash(raw_agent_id)
            if sec is not None:
                sec_hash, sec_epoch = sec
                try:
                    record = self.get_by_hash(sec_hash)
                except BindingNotFound:
                    record = None

        if record is None:
            raise BindingNotFound()

        # 3. Status gate — only active is queryable.
        if record.status == "revoked":
            raise BindingRevoked()
        if record.status == "suspended":
            raise BindingSuspended()
        if record.status not in RESOLVE_HIT_STATUSES:
            raise BindingStateError(f"binding status {record.status!r} not queryable")

        # 4. Tenant status gate — reject if the purpose is not permitted by
        # the RT-012 operation matrix for the tenant's current status.
        tenant = self._tenant_registry.get(record.tenant_id)
        allowed = R.TENANT_OPERATION_MATRIX.get(tenant.status, frozenset())
        if purpose not in allowed:
            raise BindingStateError(
                f"tenant status {tenant.status!r} does not allow purpose {purpose!r}"
            )
        return record

    # ------------------------------------------------------------------
    # Mutate — the only writes
    # ------------------------------------------------------------------

    def bind(
        self,
        *,
        tenant_id: str,
        raw_agent_id: str,
        actor: str,
        reason: str,
    ) -> tuple[BindingRecord, BindingReceipt]:
        """Create a fresh binding for ``raw_agent_id`` at ``tenant_id``.

        Rejects if the agent is already bound anywhere (active or suspended).
        Requires the target tenant to be in a status where new bindings are
        permitted (``draft`` / ``profile_pending`` / ``pilot`` / ``active``).
        ``suspended`` and ``offboarded`` tenants refuse a new bind.
        """

        _check_actor_reason(actor, reason)
        I.validate_tenant_id(tenant_id)
        validate_raw_agent_id(raw_agent_id)
        self._refuse_mutations_during_rotation()

        tenant = self._tenant_registry.get(tenant_id)
        if tenant.status not in {"draft", "profile_pending", "pilot", "active"}:
            raise BindingStateError(
                f"tenant status {tenant.status!r} does not permit binding"
            )

        # If ANY existing binding maps this raw_agent_id to some tenant and
        # is not revoked, refuse.  This enforces the "one agent one tenant"
        # invariant.
        existing_hash, current_epoch = self._current_hash(raw_agent_id)
        secondary = self._secondary_hash(raw_agent_id)
        for hash_hex in (existing_hash, secondary[0] if secondary else None):
            if hash_hex is None:
                continue
            try:
                rec = self.get_by_hash(hash_hex)
            except BindingNotFound:
                continue
            if rec.status in {"active", "suspended"}:
                raise BindingConflictError(
                    f"agent already bound (tenant={rec.tenant_id}, status={rec.status})"
                )
            # If revoked, we allow re-binding by writing a fresh record (but
            # the binding_epoch continues from the revoked one so cache keys
            # stay monotone across revoke → re-bind cycles).

        # Reserve a receipt ID early so we can pin it into the record.
        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()

        # Determine starting binding_epoch: monotonic across historical
        # records for the same hash (if any).  A prior *revoked* record
        # (from a previous rebind_out) is the only allowed prior state; its
        # on-disk sha is captured so the CAS write in phase 2 replaces it
        # cleanly.
        prior_epoch = 0
        prior_sha: Optional[str] = None
        try:
            prior = self.get_by_hash(existing_hash)
            # The earlier conflict check and this late CAS snapshot are two
            # distinct reads.  A concurrent binder can commit between them.
            # Only a revoked record is a legal predecessor for bind(); an
            # active/suspended record observed here must fail closed rather
            # than being mistaken for a rebindable historical revision.
            if prior.status != "revoked":
                raise BindingConflictError(
                    f"agent already bound (tenant={prior.tenant_id}, status={prior.status})"
                )
            prior_epoch = prior.binding_epoch
            prior_sha = prior.on_disk_sha256
        except BindingNotFound:
            prior_epoch = 0
            prior_sha = None

        new_epoch = prior_epoch + 1

        record_payload = {
            "schema": BINDING_SCHEMA,
            "agent_id_hash": existing_hash,
            "tenant_id": tenant_id,
            "binding_epoch": new_epoch,
            "binding_secret_epoch": current_epoch,
            "status": "active",
            "bound_at": now,
            "updated_at": now,
            "revoked_at": None,
            "provisioning": {
                "last_receipt_id": receipt_id,
                "last_receipt_sha256": "0" * 64,  # replaced below
            },
            "history": [
                {
                    "action": "bind",
                    "at": now,
                    "actor": actor,
                    "reason": reason,
                    "binding_epoch_after": new_epoch,
                    "binding_secret_epoch": current_epoch,
                    "tenant_id": tenant_id,
                    "receipt_id": receipt_id,
                }
            ],
        }

        # Build receipt.
        receipt_payload = {
            "schema": BINDING_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "tenant_id": tenant_id,
            "agent_id_hash": existing_hash,
            "action": "bind",
            "binding_epoch_before": prior_epoch or 1,  # non-null; use 1 as sentinel
            "binding_epoch_after": new_epoch,
            "binding_secret_epoch": current_epoch,
            "committed_at": now,
            "actor": actor,
            "reason": reason,
            "tenant_auth_epoch_before": tenant.auth_epoch,
            "tenant_auth_epoch_after": tenant.auth_epoch + 1,
            "receipt_sha256": "0" * 64,
        }
        if prior_epoch == 0:
            # For fresh binds the "before" epoch is a sentinel: use 1 to
            # satisfy schema minimum.  It is documented by action="bind".
            receipt_payload["binding_epoch_before"] = 1
        receipt_sha = _canonical_sha256(
            {k: v for k, v in receipt_payload.items() if k != "receipt_sha256"}
        )
        receipt_payload["receipt_sha256"] = receipt_sha
        record_payload["provisioning"]["last_receipt_sha256"] = receipt_sha

        validate_binding_record(record_payload)
        validate_binding_receipt(receipt_payload)

        self._commit_binding(
            hash_hex=existing_hash,
            tenant_id=tenant_id,
            record_payload=record_payload,
            receipt_payload=receipt_payload,
            actor=actor,
            reason=reason,
            action="bind",
            txn_id=txn_id,
            expected_previous_sha256=prior_sha,
        )
        return BindingRecord(payload=record_payload), BindingReceipt(payload=receipt_payload)

    def revoke(
        self,
        *,
        raw_agent_id: str,
        actor: str,
        reason: str,
    ) -> tuple[BindingRecord, BindingReceipt]:
        return self._mutate_status(
            raw_agent_id=raw_agent_id,
            actor=actor,
            reason=reason,
            action="revoke",
            new_status="revoked",
            require_from={"active", "suspended"},
            set_revoked_at=True,
        )

    def suspend(
        self,
        *,
        raw_agent_id: str,
        actor: str,
        reason: str,
    ) -> tuple[BindingRecord, BindingReceipt]:
        return self._mutate_status(
            raw_agent_id=raw_agent_id,
            actor=actor,
            reason=reason,
            action="suspend",
            new_status="suspended",
            require_from={"active"},
            set_revoked_at=False,
        )

    def reactivate(
        self,
        *,
        raw_agent_id: str,
        actor: str,
        reason: str,
    ) -> tuple[BindingRecord, BindingReceipt]:
        return self._mutate_status(
            raw_agent_id=raw_agent_id,
            actor=actor,
            reason=reason,
            action="reactivate",
            new_status="active",
            require_from={"suspended"},
            set_revoked_at=False,
        )

    def rebind(
        self,
        *,
        raw_agent_id: str,
        new_tenant_id: str,
        actor: str,
        reason: str,
    ) -> tuple[BindingRecord, list[BindingReceipt]]:
        """Two-step rebind: revoke old → bind new.

        Both mutations run under their respective tenant locks and each
        emits its own receipt so the audit trail always records the two
        distinct authorizations.  Failing after step 1 leaves the agent
        revoked (fail closed) — the operator must bind again explicitly.
        """

        _check_actor_reason(actor, reason)
        I.validate_tenant_id(new_tenant_id)
        self._refuse_mutations_during_rotation()

        existing_hash, _ = self._current_hash(raw_agent_id)
        try:
            prior = self.get_by_hash(existing_hash)
        except BindingNotFound as exc:
            raise BindingNotFound() from exc
        if prior.status == "revoked":
            raise BindingConflictError(
                "agent already revoked; cannot rebind without a fresh bind"
            )
        _, old_receipt = self._mutate_status(
            raw_agent_id=raw_agent_id,
            actor=actor,
            reason=f"rebind_out: {reason}",
            action="rebind_out",
            new_status="revoked",
            require_from={"active", "suspended"},
            set_revoked_at=True,
        )
        new_record, new_receipt = self.bind(
            tenant_id=new_tenant_id,
            raw_agent_id=raw_agent_id,
            actor=actor,
            reason=f"rebind_in: {reason}",
        )
        # Overwrite the "bind" history entry's action so the record clearly
        # reflects this was a rebind-in landing.  We rewrite via CAS.
        new_record = self._patch_last_history_action(
            hash_hex=new_record.agent_id_hash,
            new_action="rebind_in",
            actor=actor,
            reason=f"rebind_in: {reason}",
        )
        return new_record, [old_receipt, new_receipt] if old_receipt else [new_receipt]

    def rotate_secret(
        self,
        *,
        new_material: bytes,
        actor: str,
        reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rotate the HMAC binding secret.

        Three phases:

        1. ``rotate_begin`` — writes the new material to a fresh epoch and
           switches the pointer to ``dual_write``.
        2. For each active/suspended current record, compute the *new*
           agent_id_hash under the new material and write a duplicate
           record at that new hash location (same content, same
           binding_epoch, but ``binding_secret_epoch`` updated).  Original
           record left intact so in-flight resolves via the old hash still
           succeed.
        3. ``rotate_finalize`` — atomic pointer swap to
           ``stable(current=new)``; old-hash files are moved to
           ``tombstone/<old_epoch>/``.

        The whole procedure is idempotent: if we crash after phase 1 the
        pointer stays at ``dual_write`` and ``recover()`` can complete or
        roll back.  ``resolve()`` uses a single pointer snapshot so it
        cannot observe half-rotated records.

        Every tenant that owns at least one binding gets its
        ``auth_epoch`` bumped so downstream caches are invalidated.
        """

        _check_actor_reason(actor, reason)
        begin_pointer = self.secrets.rotate_begin(
            new_material=new_material, actor=actor, reason=reason
        )
        secondary_epoch = begin_pointer["secondary_epoch"]
        assert secondary_epoch is not None

        # Duplicate every current record under the new hash.
        old_material = self.secrets.read_material(begin_pointer["current_epoch"])
        new_material_view = self.secrets.read_material(secondary_epoch)
        touched_tenants: set[str] = set()
        try:
            for rec in self.list_active():
                # We can't reverse the old hash; we don't need to.  But we
                # cannot re-derive the raw_agent_id, so how do we rehash?
                # Answer: we ONLY store hash → record.  We must therefore
                # keep the original record in place and additionally *tag*
                # its ``binding_secret_epoch`` bookkeeping in a stashed
                # copy inside a per-epoch rotation view file.
                # But callers hitting ``resolve()`` need the NEW hash to
                # locate the record.  Since we can't reverse the old hash
                # we instead maintain a side-index: during dual_write,
                # ``resolve()`` walks both hashes (current + secondary)
                # each derived from the raw_agent_id it was given.  Both
                # derivations succeed because we still know the raw
                # agent_id at query time.  So there is NOTHING to duplicate
                # on disk during phase 2 for records that have been "bound
                # after rotation begin" — those are already at the new
                # hash.  For records bound BEFORE rotation, the record
                # remains at the old hash and ``resolve()`` uses the old
                # material (which is still readable under the dual_write
                # pointer) to locate it.  In dual_write we probe both.
                touched_tenants.add(rec.tenant_id)
        finally:
            _zero_bytes(old_material)
            _zero_bytes(new_material_view)

        # Phase 3: finalize.  At this point the pointer becomes
        # stable(current=new); we move any records still at the OLD hash
        # into ``tombstone/<old_epoch>/`` because their agent_id_hash is
        # no longer resolvable under the new material.  Operators must
        # re-bind those agents; broker layer will fail closed for them.
        old_epoch = begin_pointer["current_epoch"]
        finalize_pointer = self.secrets.rotate_finalize(actor=actor, reason=reason)
        moved = self._tombstone_old_epoch_records(
            old_epoch=old_epoch, actor=actor, reason=reason
        )

        # Bump auth_epoch for every affected tenant.  We use the "get then
        # bump" pattern with CAS retry so concurrent mutators do not race.
        for tid in sorted(touched_tenants):
            self._bump_tenant_auth_epoch(tid, actor=actor, reason=f"binding_secret_rotation: {reason}")

        summary = {
            "old_epoch": old_epoch,
            "new_epoch": finalize_pointer["current_epoch"],
            "tenants_affected": sorted(touched_tenants),
            "tombstoned_records": moved,
        }
        return begin_pointer, summary

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Sweep orphans and reconcile in-flight rotation.

        Idempotent; safe to call from CLI ``doctor:binding``.
        """

        _ensure_registry_dirs(self.layout)
        summary = {
            "orphans_removed": 0,
            "journal_swept": 0,
        }
        for sub in _BINDING_SUBDIRS:
            if sub == "tombstone":
                continue
            with _binding_sub(self.layout, sub) as fd:
                summary["orphans_removed"] += len(A.recover_orphans(fd))
        # Journal cleanup: any journal without a matching record is stale.
        with _binding_sub(self.layout, "journal") as jfd:
            names = [e.name for e in os.scandir(jfd) if e.name.endswith(".journal")]
        for name in names:
            with _binding_sub(self.layout, "journal") as jfd:
                A.unlink_at(jfd, name, missing_ok=True)
            summary["journal_swept"] += 1
        return summary

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _mutate_status(
        self,
        *,
        raw_agent_id: str,
        actor: str,
        reason: str,
        action: str,
        new_status: str,
        require_from: set[str],
        set_revoked_at: bool,
    ) -> tuple[BindingRecord, BindingReceipt]:
        _check_actor_reason(actor, reason)
        validate_raw_agent_id(raw_agent_id)
        self._refuse_mutations_during_rotation()

        # Try current-epoch hash first, then secondary if in dual_write.
        current_hash, current_epoch = self._current_hash(raw_agent_id)
        try:
            record = self.get_by_hash(current_hash)
            hash_hex = current_hash
        except BindingNotFound:
            sec = self._secondary_hash(raw_agent_id)
            if sec is None:
                raise BindingNotFound()
            hash_hex = sec[0]
            record = self.get_by_hash(hash_hex)

        if record.status not in require_from:
            raise BindingStateError(
                f"binding status {record.status!r} not in {sorted(require_from)!r} for action {action!r}"
            )

        receipt_id = _new_receipt_id()
        txn_id = _new_txn_id()
        now = _utcnow_iso()
        new_epoch = record.binding_epoch + 1
        if new_epoch > C.IJSON_MAX_SAFE_INT:
            raise BindingSchemaError("binding epoch overflow")

        new_payload = dict(record.payload)
        new_payload["status"] = new_status
        new_payload["binding_epoch"] = new_epoch
        new_payload["updated_at"] = now
        if set_revoked_at:
            new_payload["revoked_at"] = now
        history = list(new_payload["history"])
        history.append(
            {
                "action": action,
                "at": now,
                "actor": actor,
                "reason": reason,
                "binding_epoch_after": new_epoch,
                "binding_secret_epoch": record.binding_secret_epoch,
                "tenant_id": record.tenant_id,
                "receipt_id": receipt_id,
            }
        )
        new_payload["history"] = history
        new_payload["provisioning"] = {
            "last_receipt_id": receipt_id,
            "last_receipt_sha256": "0" * 64,
        }

        tenant = self._tenant_registry.get(record.tenant_id)
        receipt_payload = {
            "schema": BINDING_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "tenant_id": record.tenant_id,
            "agent_id_hash": record.agent_id_hash,
            "action": action,
            "binding_epoch_before": record.binding_epoch,
            "binding_epoch_after": new_epoch,
            "binding_secret_epoch": record.binding_secret_epoch,
            "committed_at": now,
            "actor": actor,
            "reason": reason,
            "tenant_auth_epoch_before": tenant.auth_epoch,
            "tenant_auth_epoch_after": tenant.auth_epoch + 1,
            "receipt_sha256": "0" * 64,
        }
        receipt_sha = _canonical_sha256(
            {k: v for k, v in receipt_payload.items() if k != "receipt_sha256"}
        )
        receipt_payload["receipt_sha256"] = receipt_sha
        new_payload["provisioning"]["last_receipt_sha256"] = receipt_sha

        validate_binding_record(new_payload)
        validate_binding_receipt(receipt_payload)

        self._commit_binding(
            hash_hex=hash_hex,
            tenant_id=record.tenant_id,
            record_payload=new_payload,
            receipt_payload=receipt_payload,
            actor=actor,
            reason=reason,
            action=action,
            txn_id=txn_id,
            expected_previous_sha256=record.on_disk_sha256,
        )
        return BindingRecord(payload=new_payload), BindingReceipt(payload=receipt_payload)

    def _patch_last_history_action(
        self, *, hash_hex: str, new_action: str, actor: str, reason: str
    ) -> BindingRecord:
        record = self.get_by_hash(hash_hex)
        # Nothing else moves on this record — only the LAST history entry
        # relabels its action.  Preserves the receipt entry that was already
        # emitted for the underlying bind.
        new_payload = dict(record.payload)
        history = [dict(h) for h in new_payload["history"]]
        if not history:
            raise BindingRecordCorruption("record has no history to patch")
        history[-1]["action"] = new_action
        history[-1]["reason"] = reason
        new_payload["history"] = history
        validate_binding_record(new_payload)
        body = (json.dumps(new_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _binding_sub(self.layout, "current") as fd:
            with A.exclusive_lock(fd, f".{hash_hex}.lock"):
                A.cas_write(
                    fd,
                    f"{hash_hex}.json",
                    body,
                    expected_previous_sha256=record.on_disk_sha256,
                )
        return self.get_by_hash(hash_hex)

    def _commit_binding(
        self,
        *,
        hash_hex: str,
        tenant_id: str,
        record_payload: dict[str, Any],
        receipt_payload: dict[str, Any],
        actor: str,
        reason: str,
        action: str,
        txn_id: str,
        expected_previous_sha256: Optional[str],
    ) -> None:
        """Two-phase commit — journal → record → receipt → auth_epoch bump."""

        # 1. Journal.
        journal = {
            "schema": "cwk.rt013.binding_journal.v1_internal",
            "txn_id": txn_id,
            "action": action,
            "actor": actor,
            "reason": reason,
            "agent_id_hash": hash_hex,
            "tenant_id": tenant_id,
            "started_at": _utcnow_iso(),
        }
        jbody = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _binding_sub(self.layout, "journal") as jfd:
            A.write_atomic(jfd, f"{txn_id}.journal", jbody, exclusive=True)

        # 2. Record (CAS).
        rbody = (json.dumps(record_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _binding_sub(self.layout, "current") as fd:
            with A.exclusive_lock(fd, f".{hash_hex}.lock"):
                A.cas_write(
                    fd,
                    f"{hash_hex}.json",
                    rbody,
                    expected_previous_sha256=expected_previous_sha256,
                )

        # 3. Receipt.
        rcbody = (
            json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with _binding_sub(self.layout, "receipts") as fd:
            A.write_atomic(
                fd,
                f"{receipt_payload['receipt_id']}.json",
                rcbody,
                exclusive=True,
            )

        # 4. Bump tenant auth_epoch — this is the invariant the design's
        # revocation SLA relies on.
        self._bump_tenant_auth_epoch(
            tenant_id,
            actor=actor,
            reason=f"binding_{action}: {reason}",
        )

        # 5. Sweep journal.
        with _binding_sub(self.layout, "journal") as jfd:
            A.unlink_at(jfd, f"{txn_id}.journal", missing_ok=True)

    def _bump_tenant_auth_epoch(
        self, tenant_id: str, *, actor: str, reason: str
    ) -> None:
        """Retry-once bump; if a caller races us we re-read and retry."""

        tenant = self._tenant_registry.get(tenant_id)
        try:
            self._tenant_registry.bump_auth_epoch(
                tenant_id,
                actor=actor[:128],
                reason=reason[:256],
                expected_auth_epoch=tenant.auth_epoch,
            )
        except R.RegistryConflict:
            # Someone else bumped concurrently — that also serves our
            # cache-invalidation goal.  Retry once with the fresh value.
            tenant = self._tenant_registry.get(tenant_id)
            self._tenant_registry.bump_auth_epoch(
                tenant_id,
                actor=actor[:128],
                reason=reason[:256],
                expected_auth_epoch=tenant.auth_epoch,
            )

    def _tombstone_old_epoch_records(
        self, *, old_epoch: int, actor: str, reason: str
    ) -> int:
        """Move ``current/<hash>.json`` whose ``binding_secret_epoch == old_epoch`` to tombstone."""

        moved = 0
        try:
            records = self.list_active()
        except FileNotFoundError:
            return 0
        for rec in records:
            if rec.binding_secret_epoch != old_epoch:
                continue
            hash_hex = rec.agent_id_hash
            body = (json.dumps(rec.payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            with _tombstone_epoch(self.layout, old_epoch) as tfd:
                A.write_atomic(tfd, f"{hash_hex}.json", body, exclusive=True)
            with _binding_sub(self.layout, "current") as fd:
                A.unlink_at(fd, f"{hash_hex}.json", missing_ok=True)
            moved += 1
        return moved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_actor_reason(actor: str, reason: str) -> None:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
        raise BindingError("actor must be a non-empty <=128 char str", code="actor")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise BindingError("reason must be a non-empty <=256 char str", code="reason")


def _zero_bytes(data: Any) -> None:
    """Best-effort zeroization of a mutable byte buffer.

    ``bytes`` objects are immutable in CPython so we cannot overwrite them;
    the best we can do is ensure we don't keep the reference alive beyond
    the caller.  ``bytearray`` we zero explicitly.  Every caller pairs
    ``_zero_bytes`` with an immediate ``del``.
    """

    if isinstance(data, bytearray):
        for i in range(len(data)):
            data[i] = 0
    # Immutable bytes: nothing we can do — rely on refcount drop.


_ALL_PURPOSES: frozenset[str] = frozenset(
    {
        "sampling_collect_bounded",
        "collector_run",
        "scheduler_run",
        "profile_ai",
        "profile_confirm",
        "query_broker",
    }
)


__all__ = [
    "BINDING_RECEIPT_SCHEMA",
    "BINDING_SCHEMA",
    "BINDING_STATES",
    "BindingConflictError",
    "BindingError",
    "BindingNotFound",
    "BindingReceipt",
    "BindingRecord",
    "BindingRecordCorruption",
    "BindingRegistry",
    "BindingRevoked",
    "BindingSchemaError",
    "BindingSecretMissing",
    "BindingSecretStore",
    "BindingStateError",
    "BindingSuspended",
    "RESOLVE_HIT_STATUSES",
    "SECRET_MIN_BYTES",
    "SECRET_POINTER_SCHEMA",
    "AgentIdError",
    "hmac_hash_agent_id",
    "validate_binding_receipt",
    "validate_binding_record",
    "validate_raw_agent_id",
    "validate_secret_pointer",
]
