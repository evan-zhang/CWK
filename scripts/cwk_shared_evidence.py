#!/usr/bin/env python3
"""RT-014: shared, immutable Canonical Evidence Store.

Owned by RT-014.  Every canonical evidence object and its per-report catalog
lives under ``$CWK_INSTANCE_ROOT/shared/`` and is opened via the RT-012
:class:`cwk_instance.InstanceLayout` dir-FD abstraction plus the RT-012
:mod:`cwk_atomic_file` primitives.

Guarantees (per PR-001 PRD §FR-06/AC-01, DESIGN §C-07 and §17, threat model
§T-04/T-11/T-12, and the RT-014 plan):

- ``report_id`` never appears as a filesystem segment; instead a
  domain-separated SHA-256 truncation is base32-encoded as a
  ``r_<26 chars>`` opaque ``catalog_key``.
- The public API is intentionally minimal: publish, read, recover.  No
  enumeration, no rollback, no existence probes, no delete.
- Publish is idempotent for the frozen ``(report_key, canonical_sha256)``
  identity; concurrent writers are serialised by an ``fcntl.flock`` on
  ``$SHARED_ROOT/locks/<catalog_key>.lock`` and by the RT-012 compare-and-swap
  primitives on ``catalog.jsonl`` + ``catalog.head``.
- Object files are written first, atomically, via
  :func:`cwk_atomic_file.write_atomic` with ``exclusive=True``; the catalog is
  updated only after the object is durable.  A crash between the two steps
  leaves an orphan object (never a stale catalog pointer).
- The reader independently verifies (1) the raw byte SHA-256 recorded in the
  catalog matches the file bytes, (2) the JCS round-trip is stable,
  (3) the body validates against the RT-011 canonical schema (deep-forbidden
  tenant / lane / reply / node / attachment / temporary-URL / credential
  scan included), (4) the internal ``canonical_sha256`` field matches, and
  (5) the ``source_namespace + report_id`` in the body composes to the
  requested ``report_key``.

Boundary rules (strict):

- Never reads ``CWORK_APP_KEY`` or any credential material.  Never opens
  tenant / view / access / registry directories.
- Never enumerates ``shared/objects/``; never lists all catalog keys via a
  public API.  ``recover()`` may scan directories internally but only emits
  opaque summaries.
- Never modifies RT-011 schemas, ``cwk_instance.py``, ``cwk_atomic_file.py``,
  the tenant CLI dispatcher, or any RT-013 binding/credential module.
- Never reads legacy ``cwk_raw_store``; the migration adaptor is RT-016's
  job to layer on top of this store's public API.
"""

from __future__ import annotations

import base64
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# RT-011: byte contracts (JCS/NFC), canonical envelope validator, opaque IDs,
# stable regexes.  RT-014 only *consumes* these public exports and never
# duplicates deep-forbidden scanning here.
import cwk_pr001_contracts as _C  # noqa: E402
# RT-012: dir-FD-anchored atomic file primitives + O_NOFOLLOW open helpers.
import cwk_atomic_file as _A  # noqa: E402
# RT-012: instance layout resolver.  Only used to obtain the shared/ dir FD.
import cwk_instance as _I  # noqa: E402


# ---------------------------------------------------------------------------
# Public error taxonomy
# ---------------------------------------------------------------------------


class SharedEvidenceError(Exception):
    """Raised by SharedEvidenceStore on any structural / integrity failure.

    ``code`` is drawn from a small closed vocabulary so downstream RTs can
    build defensive control flow without parsing free-form messages.  The
    ``__str__`` output never includes absolute host paths, canonical body
    bytes, ``report_id`` plaintext, or object-existence detail beyond the
    opaque ``catalog_key`` / ``object_id`` tokens that the caller already
    knows (or has authority to know).
    """

    _CODES: frozenset[str] = frozenset(
        {
            "contract",
            "not_initialized",
            "not_found",
            "sha_mismatch",
            "canonical_drift",
            "catalog_conflict",
            "corrupt_catalog",
            "orphan_object",
            "report_key_mismatch",
        }
    )

    def __init__(self, message: str, *, code: str, catalog_key: str | None = None,
                 object_id: str | None = None) -> None:
        if code not in self._CODES:  # pragma: no cover - defensive
            raise ValueError(f"invalid SharedEvidenceError code {code!r}")
        super().__init__(message)
        self.code = code
        self.catalog_key = catalog_key
        self.object_id = object_id

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"[{self.code}]", str(self.args[0])]
        if self.catalog_key is not None:
            parts.append(f"catalog_key={self.catalog_key}")
        if self.object_id is not None:
            parts.append(f"object_id={self.object_id}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Layout constants (frozen — do not add new siblings without a new schema)
# ---------------------------------------------------------------------------

# RT-014 sub-directories under $CWK_INSTANCE_ROOT/shared/.  RT-012 owns the
# ``shared`` leaf itself; RT-014 owns the four names below.  Each is created
# lazily by ``initialize()`` and never renamed / deleted at runtime.
SHARED_CHILDREN: tuple[str, ...] = (
    "objects",
    "report-versions",
    "staging",
    "locks",
)

# Object sharding: first two characters after the "o_" prefix.  RT-011's
# object_id regex is ``o_[a-z2-7]{26}`` so the shard alphabet is base32
# lowercase — always a valid leaf name under cwk_atomic_file's grammar.
_OBJECT_SHARD_LEN = 2

# Domain-separated hash used to derive the opaque catalog_key from a
# report_key.  Any change here MUST be a new schema version — existing
# catalogs would otherwise become unreachable.
_CATALOG_KEY_DOMAIN = b"cwk-rt014-report-key-v1\x00"
CATALOG_KEY_REGEX = re.compile(r"\Ar_[a-z2-7]{26}\Z")

# Frozen file names inside each report-versions/<catalog_key>/ directory.
_CATALOG_JSONL = "catalog.jsonl"
_CATALOG_HEAD = "catalog.head"

# Schema ids owned by RT-014 (in addition to RT-011's canonical envelope).
_REPORT_VERSION_SCHEMA_ID = "cwk.pr001.rt014.report_version.v1"
_CATALOG_HEAD_SCHEMA_ID = "cwk.pr001.rt014.catalog_head.v1"
_PUBLISH_RECEIPT_SCHEMA_ID = "cwk.pr001.rt014.publish_receipt.v1"
_RECOVERY_REPORT_SCHEMA_ID = "cwk.pr001.rt014.recovery_report.v1"

# Location of the RT-014 JSON schemas (loaded via strict_json_load_path).
_RT014_SCHEMAS_DIR = (
    _C.SCHEMA_ROOT / "rt014" / "schemas"
)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _catalog_key(report_key: str) -> str:
    """Derive the opaque catalog_key ``r_<base32>`` from a report_key.

    ``report_key`` MUST already have been validated by
    :func:`_C.compose_report_key` (i.e. matches
    ``SOURCE_NAMESPACE_REGEX + ":" + REPORT_ID_REGEX``).  The digest is
    domain-separated so a future migration cannot alias an old key.
    """

    if not isinstance(report_key, str) or not _C.REPORT_KEY_REGEX.match(report_key):
        raise SharedEvidenceError("invalid report_key grammar", code="contract")
    digest = hashlib.sha256(_CATALOG_KEY_DOMAIN + report_key.encode("utf-8")).digest()[:16]
    body = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    key = f"r_{body}"
    if not CATALOG_KEY_REGEX.match(key):  # pragma: no cover - defensive
        raise SharedEvidenceError("internal catalog_key derivation failed", code="contract")
    return key


def _object_shard(object_id: str) -> str:
    if not isinstance(object_id, str) or not _C.OBJECT_ID_REGEX.match(object_id):
        raise SharedEvidenceError("invalid object_id grammar", code="contract")
    return object_id[2 : 2 + _OBJECT_SHARD_LEN]


def _object_leaf(object_id: str) -> str:
    return f"{object_id}.json"


def _utc_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Schema validators (thin wrappers on RT-011's Draft-2020-12 subset engine)
# ---------------------------------------------------------------------------


_RT014_SCHEMA_CACHE: dict[str, Any] = {}


def _load_rt014_schema(schema_id: str) -> Any:
    if schema_id in _RT014_SCHEMA_CACHE:
        return _RT014_SCHEMA_CACHE[schema_id]
    filename_by_id = {
        _REPORT_VERSION_SCHEMA_ID: "report_version.schema.json",
        _CATALOG_HEAD_SCHEMA_ID: "catalog_head.schema.json",
        _PUBLISH_RECEIPT_SCHEMA_ID: "publish_receipt.schema.json",
        _RECOVERY_REPORT_SCHEMA_ID: "recovery_report.schema.json",
    }
    filename = filename_by_id.get(schema_id)
    if filename is None:  # pragma: no cover - defensive
        raise SharedEvidenceError(f"unknown RT-014 schema id {schema_id!r}", code="contract")
    schema = _C.strict_json_load_path(_RT014_SCHEMAS_DIR / filename)
    _RT014_SCHEMA_CACHE[schema_id] = schema
    return schema


def _validate_against(schema_id: str, payload: Any) -> None:
    """Validate ``payload`` against one of the RT-014 schemas.

    We reuse RT-011's private ``_validate_schema`` engine so RT-014 does not
    reimplement Draft-2020-12 subset semantics.  Deep forbidden properties
    listed in the schema's ``customKeywords.deepForbiddenProperties`` are
    also enforced.
    """

    schema = _load_rt014_schema(schema_id)
    try:
        _C._validate_schema(schema, payload, "$", root_schema=schema)
    except _C.ContractError as exc:
        raise SharedEvidenceError(f"schema {schema_id} failed: {exc}", code="contract") from exc
    forbidden = (
        schema.get("customKeywords", {}).get("deepForbiddenProperties")
    )
    if forbidden:
        try:
            _C._iter_deep_forbidden(payload, frozenset(forbidden), path="$")
        except _C.ContractError as exc:
            raise SharedEvidenceError(f"forbidden field present: {exc}", code="contract") from exc


def _validate_entry(entry: Any) -> None:
    _validate_against(_REPORT_VERSION_SCHEMA_ID, entry)


def _validate_head(head: Any) -> None:
    _validate_against(_CATALOG_HEAD_SCHEMA_ID, head)


def _validate_receipt(receipt: Any) -> None:
    _validate_against(_PUBLISH_RECEIPT_SCHEMA_ID, receipt)


def _validate_recovery_report(report: Any) -> None:
    _validate_against(_RECOVERY_REPORT_SCHEMA_ID, report)


# ---------------------------------------------------------------------------
# Dir-FD helpers (RT-014-local; do not export)
# ---------------------------------------------------------------------------


@contextmanager
def _flock_with_retry(parent_fd: int, name: str) -> Iterator[int]:
    """Acquire an exclusive advisory lock, working around a macOS quirk.

    On macOS, ``os.open(name, O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,
    mode=0o600, dir_fd=parent_fd)`` can occasionally raise ``ENOENT`` under
    high contention when the lock file does not yet exist and several
    threads race on creation.  We work around this without patching the
    frozen RT-012 primitive by retrying the ``exclusive_lock`` call a small
    number of times before giving up.  Each retry is safe because the lock
    file is content-less (used only as an ``fcntl.flock`` anchor) and the
    RT-012 primitive still owns the create/open/lock/close sequence.
    """

    last_exc: Exception | None = None
    for attempt in range(8):
        try:
            with _A.exclusive_lock(parent_fd, name, blocking=True) as fd:
                yield fd
                return
        except FileNotFoundError as exc:
            last_exc = exc
            # Extremely short backoff; give the racing creator a chance.
            import time as _time
            _time.sleep(0.001 * (attempt + 1))
            continue
    raise SharedEvidenceError(
        "could not acquire per-report lock after retries",
        code="catalog_conflict",
    ) from last_exc


def _openat_dir_nofollow(parent_fd: int, name: str) -> int:
    """Open a subdirectory beneath ``parent_fd`` with O_DIRECTORY|O_NOFOLLOW.

    Mirrors the private helper in :mod:`cwk_instance` so RT-014 can open its
    own opaque subdirectories (``objects/<shard>/``,
    ``report-versions/<catalog_key>/``) without depending on RT-012's
    fixed-leaf allow-list.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SharedEvidenceError(
                "child is a symlink; refusing to follow", code="contract"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise SharedEvidenceError(
                "child is not a directory", code="contract"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise SharedEvidenceError("child does not exist", code="not_found") from exc
        raise SharedEvidenceError(
            f"cannot open child ({exc.errno})", code="contract"
        ) from exc
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise SharedEvidenceError("child is not a directory", code="contract")
    return fd


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishReceipt:
    report_key: str
    canonical_sha256: str
    object_id: str
    catalog_key: str
    is_new_version: bool
    is_new_report: bool
    catalog_revision: int
    catalog_head_sha256: str

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": "cwk.rt014.publish_receipt.v1",
            "report_key": self.report_key,
            "canonical_sha256": self.canonical_sha256,
            "object_id": self.object_id,
            "catalog_key": self.catalog_key,
            "is_new_version": self.is_new_version,
            "is_new_report": self.is_new_report,
            "catalog_revision": self.catalog_revision,
            "catalog_head_sha256": self.catalog_head_sha256,
        }
        _validate_receipt(payload)
        return payload


@dataclass
class RecoveryReport:
    staging_orphans_removed: list[str] = field(default_factory=list)
    catalog_dirs_scanned: int = 0
    objects_verified: int = 0
    catalog_issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": "cwk.rt014.recovery_report.v1",
            "staging_orphans_removed": list(self.staging_orphans_removed),
            "catalog_dirs_scanned": self.catalog_dirs_scanned,
            "objects_verified": self.objects_verified,
            "catalog_issues": [dict(issue) for issue in self.catalog_issues],
        }
        _validate_recovery_report(payload)
        return payload


# ---------------------------------------------------------------------------
# SharedEvidenceStore
# ---------------------------------------------------------------------------


class SharedEvidenceStore:
    """Shared, immutable Canonical Evidence Store.

    Instances are cheap and stateless: they carry only a reference to the
    :class:`cwk_instance.InstanceLayout` handle.  Every operation opens its
    own directory file descriptors, executes under an anchored dir-FD, and
    closes them before returning.
    """

    __slots__ = ("_layout",)

    def __init__(self, layout: _I.InstanceLayout) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise SharedEvidenceError(
                "layout must be an InstanceLayout", code="contract"
            )
        self._layout = layout

    # -- Factory ---------------------------------------------------------

    @classmethod
    def open(cls, layout: _I.InstanceLayout) -> "SharedEvidenceStore":
        return cls(layout)

    # -- Initialisation --------------------------------------------------

    def initialize(self) -> None:
        """Idempotently create the four RT-014 subdirectories under ``shared/``.

        RT-012 pre-creates ``shared/`` itself (``INSTANCE_ROOT_CHILDREN``);
        this method never modifies that leaf, only creates and hardens
        children below it.
        """

        with self._layout.child_fd("shared") as sfd:
            for name in SHARED_CHILDREN:
                _A.mkdir_at(sfd, name, mode=_A.DIRECTORY_MODE, exist_ok=True)
            _A.fsync_dir(sfd)

    # -- Convenience dir-FDs (context managers) --------------------------

    @contextmanager
    def _shared_fd(self) -> Iterator[int]:
        with self._layout.child_fd("shared") as sfd:
            yield sfd

    @contextmanager
    def _sub_fd(self, sfd: int, name: str) -> Iterator[int]:
        fd = _openat_dir_nofollow(sfd, name)
        try:
            yield fd
        finally:
            os.close(fd)

    def _require_initialized(self, sfd: int) -> None:
        for name in SHARED_CHILDREN:
            if not _A.child_exists(sfd, name):
                raise SharedEvidenceError(
                    f"shared/{name} not initialized", code="not_initialized"
                )

    @contextmanager
    def _report_dir_fd(
        self, sfd: int, catalog_key: str, *, create: bool
    ) -> Iterator[int]:
        with self._sub_fd(sfd, "report-versions") as rvfd:
            if not _A.child_exists(rvfd, catalog_key):
                if not create:
                    raise SharedEvidenceError(
                        "no catalog for report_key",
                        code="not_found",
                        catalog_key=catalog_key,
                    )
                _A.mkdir_at(rvfd, catalog_key, mode=_A.DIRECTORY_MODE, exist_ok=True)
                _A.fsync_dir(rvfd)
            fd = _openat_dir_nofollow(rvfd, catalog_key)
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def _object_shard_fd(
        self, sfd: int, shard: str, *, create: bool
    ) -> Iterator[int]:
        with self._sub_fd(sfd, "objects") as ofd:
            if not _A.child_exists(ofd, shard):
                if not create:
                    raise SharedEvidenceError(
                        "object shard missing", code="orphan_object"
                    )
                _A.mkdir_at(ofd, shard, mode=_A.DIRECTORY_MODE, exist_ok=True)
                _A.fsync_dir(ofd)
            fd = _openat_dir_nofollow(ofd, shard)
            try:
                yield fd
            finally:
                os.close(fd)

    # -- Catalog IO helpers ---------------------------------------------

    def _read_head(self, rfd: int) -> tuple[Optional[dict[str, Any]], bytes]:
        """Return (head_dict, raw_bytes).  ``(None, b"")`` if the head file
        does not yet exist.  ``head_dict`` is validated against the RT-014
        schema; raises ``SharedEvidenceError(corrupt_catalog)`` on drift.
        """

        try:
            raw = _A.read_file(rfd, _CATALOG_HEAD)
        except FileNotFoundError:
            return None, b""
        except _A.ContainmentError as exc:
            raise SharedEvidenceError(
                "catalog head containment failure", code="corrupt_catalog"
            ) from exc
        try:
            head = _C.strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
            raise SharedEvidenceError(
                "catalog head is not strict JSON", code="corrupt_catalog"
            ) from exc
        try:
            _validate_head(head)
        except SharedEvidenceError as exc:
            raise SharedEvidenceError(
                "catalog head fails schema", code="corrupt_catalog"
            ) from exc
        # Belt-and-braces: the head file bytes must be canonical JCS.  Any
        # trailing whitespace / re-formatting is treated as tampering.
        if _C.canonical_json_bytes(head) != raw:
            raise SharedEvidenceError(
                "catalog head bytes are not canonical JCS", code="corrupt_catalog"
            )
        return head, raw

    def _read_entries(
        self, rfd: int, *, expected_count: int
    ) -> tuple[list[dict[str, Any]], bytes]:
        """Return (entries_list, raw_jsonl_bytes).  Raises corrupt_catalog on
        line-parse / schema / count drift.
        """

        try:
            raw = _A.read_file(rfd, _CATALOG_JSONL)
        except FileNotFoundError:
            if expected_count != 0:
                raise SharedEvidenceError(
                    "catalog.jsonl missing but head expects entries",
                    code="corrupt_catalog",
                )
            return [], b""
        except _A.ContainmentError as exc:
            raise SharedEvidenceError(
                "catalog.jsonl containment failure", code="corrupt_catalog"
            ) from exc

        entries: list[dict[str, Any]] = []
        # Trailing newline is expected because we always emit ``entry + \n``.
        # Reject an empty file if we expected any entries; reject an internal
        # blank line; reject the file if the trailing newline is missing.
        if not raw:
            if expected_count != 0:
                raise SharedEvidenceError(
                    "empty catalog.jsonl but head expects entries",
                    code="corrupt_catalog",
                )
            return [], raw
        if not raw.endswith(b"\n"):
            raise SharedEvidenceError(
                "catalog.jsonl missing trailing newline", code="corrupt_catalog"
            )
        for line in raw.split(b"\n")[:-1]:
            if not line:
                raise SharedEvidenceError(
                    "catalog.jsonl contains an empty line", code="corrupt_catalog"
                )
            try:
                entry = _C.strict_json_loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
                raise SharedEvidenceError(
                    "catalog.jsonl line is not strict JSON",
                    code="corrupt_catalog",
                ) from exc
            _validate_entry(entry)
            # Belt-and-braces: line bytes must be canonical JCS.
            if _C.canonical_json_bytes(entry) != line:
                raise SharedEvidenceError(
                    "catalog.jsonl line bytes are not canonical JCS",
                    code="corrupt_catalog",
                )
            entries.append(entry)
        if len(entries) != expected_count:
            raise SharedEvidenceError(
                f"catalog head/jsonl entry_count drift ({len(entries)} vs {expected_count})",
                code="corrupt_catalog",
            )
        return entries, raw

    def _reverify_object_on_disk(
        self,
        sfd: int,
        object_id: str,
        canonical_sha256: str,
        object_bytes_sha256: str,
    ) -> None:
        with self._object_shard_fd(sfd, _object_shard(object_id), create=False) as shard_fd:
            try:
                raw = _A.read_file(shard_fd, _object_leaf(object_id))
            except FileNotFoundError as exc:
                raise SharedEvidenceError(
                    "catalog references missing object",
                    code="orphan_object",
                    object_id=object_id,
                ) from exc
            except _A.ContainmentError as exc:
                raise SharedEvidenceError(
                    "object failed containment", code="contract", object_id=object_id
                ) from exc
        if _sha256_bytes(raw) != object_bytes_sha256:
            raise SharedEvidenceError(
                "object bytes sha256 mismatch (idempotent republish)",
                code="sha_mismatch",
                object_id=object_id,
            )
        try:
            payload = _C.strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
            raise SharedEvidenceError(
                "object body is not strict JSON", code="contract", object_id=object_id
            ) from exc
        try:
            _C.validate_canonical_envelope(payload)
        except _C.ContractError as exc:
            raise SharedEvidenceError(
                "object body fails RT-011 schema",
                code="contract",
                object_id=object_id,
            ) from exc
        if payload["canonical_sha256"] != canonical_sha256:
            raise SharedEvidenceError(
                "object body canonical_sha256 field mismatch",
                code="sha_mismatch",
                object_id=object_id,
            )

    # -- Publish ---------------------------------------------------------

    def publish(self, envelope: Any) -> PublishReceipt:
        """Idempotently publish a canonical envelope.

        Ordering (frozen, do not reorder):

        1. RT-011 ``validate_canonical_envelope`` (schema + deep forbidden +
           canonical_sha256 recompute).
        2. Compute ``report_key`` / ``catalog_key`` and canonical object bytes.
        3. Open shared/ and take the per-report ``flock``.
        4. Load ``catalog.head`` + ``catalog.jsonl`` under the lock; verify
           they agree.
        5. If ``canonical_sha256`` already present -> re-verify the on-disk
           object bytes, return the existing ``PublishReceipt`` with
           ``is_new_version=False``.
        6. Otherwise: allocate a fresh ``object_id``, mkdir shard, atomically
           write the object file (``exclusive=True``).
        7. Append to ``entries``, CAS-write ``catalog.jsonl`` (full rewrite)
           and ``catalog.head`` under the lock.
        """

        if not isinstance(envelope, dict):
            raise SharedEvidenceError(
                "envelope must be a JSON object", code="contract"
            )
        # 1. Strict RT-011 validation.
        try:
            _C.validate_canonical_envelope(envelope)
        except _C.ContractError as exc:
            raise SharedEvidenceError(
                f"envelope fails RT-011 canonical schema: {exc}",
                code="contract",
            ) from exc

        # 2. Compose keys, serialise deterministically.
        try:
            report_key = _C.compose_report_key(
                envelope["source_namespace"], envelope["report_id"]
            )
        except _C.ContractError as exc:
            raise SharedEvidenceError(
                f"invalid report_key components: {exc}", code="contract"
            ) from exc
        catalog_key = _catalog_key(report_key)
        body_bytes = _C.canonical_json_bytes(envelope)
        object_bytes_sha256 = _sha256_bytes(body_bytes)
        canonical_sha256 = envelope["canonical_sha256"]
        # Sanity: RT-011 validator already recomputes canonical_sha256; belt-
        # and-braces confirm object_bytes_sha256 is a distinct hex string.
        if not _C.SHA256_HEX_REGEX.match(object_bytes_sha256):  # pragma: no cover
            raise SharedEvidenceError(
                "internal sha256 derivation failed", code="contract"
            )

        with self._shared_fd() as sfd:
            self._require_initialized(sfd)
            with self._sub_fd(sfd, "locks") as lfd:
                with _flock_with_retry(lfd, f"{catalog_key}.lock"):
                    with self._report_dir_fd(sfd, catalog_key, create=True) as rfd:
                        head_dict, head_bytes_old = self._read_head(rfd)
                        expected_count = head_dict["entry_count"] if head_dict else 0
                        entries, jsonl_bytes_old = self._read_entries(
                            rfd, expected_count=expected_count
                        )
                        if head_dict is not None:
                            if head_dict["catalog_key"] != catalog_key:
                                raise SharedEvidenceError(
                                    "catalog head catalog_key mismatch",
                                    code="corrupt_catalog",
                                    catalog_key=catalog_key,
                                )
                            if head_dict["report_key"] != report_key:
                                raise SharedEvidenceError(
                                    "catalog head report_key mismatch",
                                    code="report_key_mismatch",
                                    catalog_key=catalog_key,
                                )
                            if head_dict["catalog_jsonl_sha256"] != _sha256_bytes(
                                jsonl_bytes_old
                            ):
                                raise SharedEvidenceError(
                                    "catalog.head vs catalog.jsonl SHA drift",
                                    code="corrupt_catalog",
                                    catalog_key=catalog_key,
                                )

                        # 3. Idempotency check by canonical_sha256.
                        for existing in entries:
                            if existing["canonical_sha256"] == canonical_sha256:
                                # Confirm object still healthy on disk.
                                self._reverify_object_on_disk(
                                    sfd,
                                    existing["object_id"],
                                    canonical_sha256,
                                    existing["object_bytes_sha256"],
                                )
                                head_sha = (
                                    _sha256_bytes(head_bytes_old)
                                    if head_bytes_old
                                    else "0" * 64
                                )
                                return PublishReceipt(
                                    report_key=report_key,
                                    canonical_sha256=canonical_sha256,
                                    object_id=existing["object_id"],
                                    catalog_key=catalog_key,
                                    is_new_version=False,
                                    is_new_report=False,
                                    catalog_revision=len(entries),
                                    catalog_head_sha256=head_sha,
                                )

                        # 4. Allocate opaque object id and write the object.
                        object_id = _C.new_object_id()
                        shard = _object_shard(object_id)
                        with self._object_shard_fd(sfd, shard, create=True) as shard_fd:
                            try:
                                _A.write_atomic(
                                    shard_fd,
                                    _object_leaf(object_id),
                                    body_bytes,
                                    exclusive=True,
                                )
                            except _A.AtomicFileError as exc:
                                if exc.code == "exists":
                                    # 128-bit random collision is
                                    # cryptographically infeasible; retry once
                                    # with a fresh id rather than trust the
                                    # existing content.
                                    raise SharedEvidenceError(
                                        "object_id unexpectedly collided",
                                        code="catalog_conflict",
                                    ) from exc
                                raise SharedEvidenceError(
                                    f"object write failed ({exc.code})",
                                    code="contract",
                                ) from exc

                        # 5. Compose and validate new entry.
                        first_seen_at = _utc_iso()
                        entry = {
                            "schema": "cwk.report_version.v1",
                            "report_key": report_key,
                            "canonical_sha256": canonical_sha256,
                            "object_bytes_sha256": object_bytes_sha256,
                            "object_id": object_id,
                            "first_seen_at": first_seen_at,
                            "source_updated_at": envelope["source_updated_at"],
                            "normalizer_version": envelope["normalizer_version"],
                        }
                        _validate_entry(entry)
                        entries.append(entry)

                        # 6. Serialise + CAS-write catalog.jsonl.
                        jsonl_bytes_new = b"".join(
                            _C.canonical_json_bytes(e) + b"\n" for e in entries
                        )
                        try:
                            if not entries[:-1]:
                                _A.cas_write(
                                    rfd,
                                    _CATALOG_JSONL,
                                    jsonl_bytes_new,
                                    expected_previous_sha256=None,
                                )
                            else:
                                _A.cas_write(
                                    rfd,
                                    _CATALOG_JSONL,
                                    jsonl_bytes_new,
                                    expected_previous_sha256=_sha256_bytes(
                                        jsonl_bytes_old
                                    ),
                                )
                        except _A.RevisionConflict as exc:
                            raise SharedEvidenceError(
                                "catalog.jsonl CAS conflict",
                                code="catalog_conflict",
                                catalog_key=catalog_key,
                            ) from exc

                        # 7. Compose head, CAS-write catalog.head.
                        created_at = (
                            head_dict["created_at"] if head_dict else first_seen_at
                        )
                        new_head = {
                            "schema": "cwk.rt014.catalog_head.v1",
                            "catalog_key": catalog_key,
                            "report_key": report_key,
                            "entry_count": len(entries),
                            "latest_object_id": object_id,
                            "latest_canonical_sha256": canonical_sha256,
                            "catalog_jsonl_sha256": _sha256_bytes(jsonl_bytes_new),
                            "head_revision": len(entries),
                            "created_at": created_at,
                            "updated_at": _utc_iso(),
                        }
                        _validate_head(new_head)
                        head_bytes_new = _C.canonical_json_bytes(new_head)
                        try:
                            _A.cas_write(
                                rfd,
                                _CATALOG_HEAD,
                                head_bytes_new,
                                expected_previous_sha256=(
                                    _sha256_bytes(head_bytes_old)
                                    if head_bytes_old
                                    else None
                                ),
                            )
                        except _A.RevisionConflict as exc:
                            raise SharedEvidenceError(
                                "catalog.head CAS conflict",
                                code="catalog_conflict",
                                catalog_key=catalog_key,
                            ) from exc

                        return PublishReceipt(
                            report_key=report_key,
                            canonical_sha256=canonical_sha256,
                            object_id=object_id,
                            catalog_key=catalog_key,
                            is_new_version=True,
                            is_new_report=(head_dict is None),
                            catalog_revision=len(entries),
                            catalog_head_sha256=_sha256_bytes(head_bytes_new),
                        )

    # -- Read ------------------------------------------------------------

    def read_version(self, report_key: str, canonical_sha256: str) -> dict[str, Any]:
        """Read and fully verify the canonical envelope for
        ``(report_key, canonical_sha256)``.

        Every check is deliberate: any single failure raises
        :class:`SharedEvidenceError` with a stable ``code`` and no path
        disclosure.  The returned dict is a fresh copy of the on-disk JSON
        payload — callers must not mutate it.
        """

        if not isinstance(report_key, str) or not _C.REPORT_KEY_REGEX.match(report_key):
            raise SharedEvidenceError("invalid report_key grammar", code="contract")
        if not isinstance(canonical_sha256, str) or not _C.SHA256_HEX_REGEX.match(
            canonical_sha256
        ):
            raise SharedEvidenceError("invalid canonical_sha256 grammar", code="contract")

        catalog_key = _catalog_key(report_key)
        with self._shared_fd() as sfd:
            self._require_initialized(sfd)
            with self._report_dir_fd(sfd, catalog_key, create=False) as rfd:
                head_dict, head_bytes = self._read_head(rfd)
                if head_dict is None:
                    raise SharedEvidenceError(
                        "no catalog head for report_key",
                        code="not_found",
                        catalog_key=catalog_key,
                    )
                if head_dict["catalog_key"] != catalog_key:
                    raise SharedEvidenceError(
                        "catalog head catalog_key mismatch",
                        code="corrupt_catalog",
                        catalog_key=catalog_key,
                    )
                if head_dict["report_key"] != report_key:
                    raise SharedEvidenceError(
                        "catalog head report_key mismatch",
                        code="report_key_mismatch",
                        catalog_key=catalog_key,
                    )
                entries, jsonl_bytes = self._read_entries(
                    rfd, expected_count=head_dict["entry_count"]
                )
                if head_dict["catalog_jsonl_sha256"] != _sha256_bytes(jsonl_bytes):
                    raise SharedEvidenceError(
                        "catalog.head vs catalog.jsonl SHA drift",
                        code="corrupt_catalog",
                        catalog_key=catalog_key,
                    )
                matches = [e for e in entries if e["canonical_sha256"] == canonical_sha256]
                if not matches:
                    raise SharedEvidenceError(
                        "no matching version in catalog",
                        code="not_found",
                        catalog_key=catalog_key,
                    )
                entry = matches[0]
                if entry["report_key"] != report_key:
                    raise SharedEvidenceError(
                        "catalog entry report_key mismatch",
                        code="report_key_mismatch",
                        catalog_key=catalog_key,
                    )

                # Open object.
                object_id = entry["object_id"]
                with self._object_shard_fd(
                    sfd, _object_shard(object_id), create=False
                ) as shard_fd:
                    try:
                        raw = _A.read_file(shard_fd, _object_leaf(object_id))
                    except FileNotFoundError as exc:
                        raise SharedEvidenceError(
                            "catalog references missing object",
                            code="orphan_object",
                            catalog_key=catalog_key,
                            object_id=object_id,
                        ) from exc
                    except _A.ContainmentError as exc:
                        raise SharedEvidenceError(
                            "object failed containment",
                            code="contract",
                            catalog_key=catalog_key,
                            object_id=object_id,
                        ) from exc

        # ------------------------------------------------------------------
        # Bytes-level verification (out of the lock; the bytes are immutable).
        # ------------------------------------------------------------------
        if _sha256_bytes(raw) != entry["object_bytes_sha256"]:
            raise SharedEvidenceError(
                "object bytes sha256 mismatch",
                code="sha_mismatch",
                catalog_key=catalog_key,
                object_id=object_id,
            )
        try:
            payload = _C.strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
            raise SharedEvidenceError(
                "object body is not strict JSON",
                code="contract",
                catalog_key=catalog_key,
                object_id=object_id,
            ) from exc
        try:
            _C.validate_canonical_envelope(payload)
        except _C.ContractError as exc:
            raise SharedEvidenceError(
                f"object body fails RT-011 canonical schema: {exc}",
                code="contract",
                catalog_key=catalog_key,
                object_id=object_id,
            ) from exc
        if _C.canonical_json_bytes(payload) != raw:
            raise SharedEvidenceError(
                "object body is not canonical JCS bytes",
                code="canonical_drift",
                catalog_key=catalog_key,
                object_id=object_id,
            )
        if payload["canonical_sha256"] != canonical_sha256:
            raise SharedEvidenceError(
                "object body canonical_sha256 field mismatch",
                code="sha_mismatch",
                catalog_key=catalog_key,
                object_id=object_id,
            )
        try:
            payload_report_key = _C.compose_report_key(
                payload["source_namespace"], payload["report_id"]
            )
        except _C.ContractError as exc:
            raise SharedEvidenceError(
                "object body report_key components invalid",
                code="report_key_mismatch",
                catalog_key=catalog_key,
                object_id=object_id,
            ) from exc
        if payload_report_key != report_key:
            raise SharedEvidenceError(
                "object body report_key mismatch",
                code="report_key_mismatch",
                catalog_key=catalog_key,
                object_id=object_id,
            )
        return payload

    # -- Recovery --------------------------------------------------------

    def recover(self) -> RecoveryReport:
        """Idempotently clean up orphan temp files and re-verify catalog state.

        This method NEVER deletes an object file or truncates a catalog.  It
        cleans only the ``.cwk-tmp-`` orphan files that ``cwk_atomic_file``
        may leave behind after a crash between temp-create and rename, and
        emits a machine-readable :class:`RecoveryReport`.
        """

        report = RecoveryReport()
        with self._shared_fd() as sfd:
            self._require_initialized(sfd)

            # Staging orphans.
            with self._sub_fd(sfd, "staging") as sf:
                report.staging_orphans_removed.extend(_A.recover_orphans(sf))
                _A.fsync_dir(sf)

            # Per-report scan (also cleans in-directory orphans).
            with self._sub_fd(sfd, "report-versions") as rvfd:
                with os.scandir(rvfd) as it:
                    catalog_keys = sorted(
                        e.name for e in it if CATALOG_KEY_REGEX.match(e.name)
                    )
            for catalog_key in catalog_keys:
                try:
                    with self._report_dir_fd(sfd, catalog_key, create=False) as rfd:
                        _A.recover_orphans(rfd)
                        _A.fsync_dir(rfd)
                        head_dict, _ = self._read_head(rfd)
                        if head_dict is None:
                            report.catalog_issues.append(
                                {
                                    "code": "corrupt_catalog",
                                    "catalog_key": catalog_key,
                                    "object_id": None,
                                }
                            )
                            continue
                        entries, jsonl_bytes = self._read_entries(
                            rfd, expected_count=head_dict["entry_count"]
                        )
                        if head_dict["catalog_jsonl_sha256"] != _sha256_bytes(
                            jsonl_bytes
                        ):
                            report.catalog_issues.append(
                                {
                                    "code": "corrupt_catalog",
                                    "catalog_key": catalog_key,
                                    "object_id": None,
                                }
                            )
                            continue
                except SharedEvidenceError as exc:
                    report.catalog_issues.append(
                        {
                            "code": "corrupt_catalog",
                            "catalog_key": catalog_key,
                            "object_id": None,
                        }
                    )
                    continue
                report.catalog_dirs_scanned += 1
                for entry in entries:
                    object_id = entry["object_id"]
                    try:
                        with self._object_shard_fd(
                            sfd, _object_shard(object_id), create=False
                        ) as shard_fd:
                            try:
                                raw = _A.read_file(shard_fd, _object_leaf(object_id))
                            except FileNotFoundError:
                                report.catalog_issues.append(
                                    {
                                        "code": "missing_object",
                                        "catalog_key": catalog_key,
                                        "object_id": object_id,
                                    }
                                )
                                continue
                    except SharedEvidenceError:
                        report.catalog_issues.append(
                            {
                                "code": "missing_object",
                                "catalog_key": catalog_key,
                                "object_id": object_id,
                            }
                        )
                        continue
                    if _sha256_bytes(raw) != entry["object_bytes_sha256"]:
                        report.catalog_issues.append(
                            {
                                "code": "sha_mismatch",
                                "catalog_key": catalog_key,
                                "object_id": object_id,
                            }
                        )
                        continue
                    report.objects_verified += 1

            # Report orphan objects (objects with no catalog reference).
            referenced = _collect_referenced_object_ids(self, sfd, catalog_keys)
            with self._sub_fd(sfd, "objects") as ofd:
                with os.scandir(ofd) as it:
                    shard_names = sorted(
                        e.name
                        for e in it
                        if len(e.name) == _OBJECT_SHARD_LEN
                        and all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in e.name)
                    )
                for shard in shard_names:
                    with self._sub_fd(ofd, shard) as shard_fd:
                        _A.recover_orphans(shard_fd)
                        with os.scandir(shard_fd) as sit:
                            for entry in sit:
                                name = entry.name
                                if not name.endswith(".json"):
                                    continue
                                obj_id = name[:-5]
                                if not _C.OBJECT_ID_REGEX.match(obj_id):
                                    continue
                                if obj_id not in referenced:
                                    report.catalog_issues.append(
                                        {
                                            "code": "orphan_object",
                                            "catalog_key": "r_" + "a" * 26,
                                            "object_id": obj_id,
                                        }
                                    )

        # Final schema validation (defense against bug drift).
        report.to_dict()
        return report


def _collect_referenced_object_ids(
    store: SharedEvidenceStore, sfd: int, catalog_keys: list[str]
) -> set[str]:
    """Enumerate every object_id referenced by any catalog under sfd.

    Private helper (module-private, not part of the store's public API): used
    solely by :meth:`SharedEvidenceStore.recover` to detect orphan objects.
    Never callable from outside this module by name because ``SharedEvidenceStore``
    does not expose it.
    """

    referenced: set[str] = set()
    for catalog_key in catalog_keys:
        try:
            with store._report_dir_fd(sfd, catalog_key, create=False) as rfd:
                head_dict, _ = store._read_head(rfd)
                if head_dict is None:
                    continue
                entries, _ = store._read_entries(
                    rfd, expected_count=head_dict["entry_count"]
                )
        except SharedEvidenceError:
            continue
        for e in entries:
            referenced.add(e["object_id"])
    return referenced


# ---------------------------------------------------------------------------
# Public exports (whitelist)
# ---------------------------------------------------------------------------

__all__ = [
    "CATALOG_KEY_REGEX",
    "PublishReceipt",
    "RecoveryReport",
    "SharedEvidenceError",
    "SharedEvidenceStore",
    "SHARED_CHILDREN",
]
