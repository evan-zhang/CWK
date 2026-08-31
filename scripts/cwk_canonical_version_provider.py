#!/usr/bin/env python3
"""RT-017 point-lookup ABI for the current RT-014 canonical version.

The RT-014 store deliberately exposes no catalog-enumeration method.  This
module therefore derives exactly one opaque catalog key from a validated
``report_key``, reads only that catalog's ``catalog.head`` and
``catalog.jsonl`` through anchored directory file descriptors, and then asks
the public :meth:`cwk_shared_evidence.SharedEvidenceStore.read_version`
method to re-verify the selected object.

The catalog-key derivation is copied from the frozen RT-014 contract rather
than imported from RT-014's private ``_catalog_key`` helper::

    r_ + lower_base32(
        SHA256(b"cwk-rt014-report-key-v1\\0" + UTF8(report_key))[:16]
    ).rstrip("=")

No API in this module accepts a path, catalog key, object id, tenant id or
credential.  No API returns a path or object id.  Missing authority defaults
to :class:`NullCanonicalVersionProvider`, which always fails closed.
"""

from __future__ import annotations

import base64
import datetime as _dt
import errno
import fcntl
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Iterator, Protocol, runtime_checkable

import cwk_instance as _I
import cwk_pr001_contracts as _C
import cwk_shared_evidence as _S


CANONICAL_VERSION_PROVIDER_API_VERSION = "cwk.canonical_version_provider.v1"
CANONICAL_VERSION_SNAPSHOT_SCHEMA = "cwk.canonical_version_snapshot.v1"

# Frozen RT-014 catalog-key contract, copied verbatim by value.  Changing the
# domain or truncation would make every existing RT-014 catalog unreachable.
RT014_CATALOG_KEY_DOMAIN = b"cwk-rt014-report-key-v1\x00"
RT014_CATALOG_KEY_FIXED_VECTOR_REPORT_KEY = "cwork:2070001"
RT014_CATALOG_KEY_FIXED_VECTOR = "r_tx33a6ug3oomvn2klbxm3p5jhy"

CANONICAL_VERSION_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "schema",
    "report_key",
    "canonical_sha256",
    "catalog_revision",
    "catalog_head_sha256",
)

_CATALOG_HEAD = "catalog.head"
_CATALOG_JSONL = "catalog.jsonl"
_REPORT_VERSIONS = "report-versions"
_CATALOG_KEY_RE = re.compile(r"\Ar_[a-z2-7]{26}\Z")
_OBJECT_ID_RE = re.compile(r"\Ao_[a-z2-7]{26}\Z")
_NORMALIZER_RE = re.compile(r"\Av[0-9]{1,4}\Z")
_TIMEZONE_SUFFIX_RE = re.compile(r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_HEAD_BYTES = 64 * 1024
_MAX_CATALOG_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 64 * 1024

_HEAD_FIELDS = frozenset(
    {
        "schema",
        "catalog_key",
        "report_key",
        "entry_count",
        "latest_object_id",
        "latest_canonical_sha256",
        "catalog_jsonl_sha256",
        "head_revision",
        "created_at",
        "updated_at",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "schema",
        "report_key",
        "canonical_sha256",
        "object_bytes_sha256",
        "object_id",
        "first_seen_at",
        "source_updated_at",
        "normalizer_version",
    }
)


class CanonicalVersionProviderError(Exception):
    """Stable, path-opaque failure returned by the provider boundary."""

    _CODES = frozenset(
        {
            "contract",
            "unavailable",
            "not_found",
            "containment",
            "corrupt_catalog",
            "concurrent_update",
            "object_verification_failed",
        }
    )

    def __init__(self, message: str, *, code: str) -> None:
        if code not in self._CODES:  # pragma: no cover - defensive
            raise ValueError("unknown canonical-version error code")
        super().__init__(message)
        self.code = code


class CanonicalVersionUnavailable(CanonicalVersionProviderError):
    """No authoritative provider or initialized store is available."""

    def __init__(self) -> None:
        super().__init__("canonical version provider unavailable", code="unavailable")


@dataclass(frozen=True, slots=True)
class CanonicalVersionSnapshotV1:
    """Exact five-field snapshot of one RT-014 catalog head."""

    schema: str
    report_key: str
    canonical_sha256: str
    catalog_revision: int
    catalog_head_sha256: str

    def to_payload(self) -> dict[str, Any]:
        """Return the exact frozen JSON field set in contract order."""

        return {
            "schema": self.schema,
            "report_key": self.report_key,
            "canonical_sha256": self.canonical_sha256,
            "catalog_revision": self.catalog_revision,
            "catalog_head_sha256": self.catalog_head_sha256,
        }


@runtime_checkable
class CanonicalVersionProviderV1(Protocol):
    """Snapshot-only point-lookup protocol consumed by RT-017."""

    API_VERSION: ClassVar[str]

    def resolve_current(
        self, *, report_key: str
    ) -> CanonicalVersionSnapshotV1:  # pragma: no cover - protocol declaration
        ...


@dataclass(frozen=True, slots=True)
class _CatalogObservation:
    snapshot: CanonicalVersionSnapshotV1
    head_bytes: bytes
    jsonl_bytes: bytes


def _fail(message: str, *, code: str) -> None:
    raise CanonicalVersionProviderError(message, code=code)


def _require_report_key(report_key: Any) -> str:
    if type(report_key) is not str or _C.REPORT_KEY_REGEX.fullmatch(report_key) is None:
        _fail("report_key violates the frozen grammar", code="contract")
    return report_key


def _derive_catalog_lookup_key(report_key: str) -> str:
    """Implement the frozen RT-014 catalog-key formula locally."""

    digest = hashlib.sha256(
        RT014_CATALOG_KEY_DOMAIN + report_key.encode("utf-8")
    ).digest()[:16]
    body = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    key = "r_" + body
    if _CATALOG_KEY_RE.fullmatch(key) is None:  # pragma: no cover - defensive
        _fail("catalog key derivation failed", code="contract")
    return key


def _opened_path(fd: int) -> bytes:
    """Return the kernel-observed path for exact-case/Unicode verification.

    Darwin exposes ``F_GETPATH``.  Linux exposes an equivalent procfs link.
    If neither exists, the provider refuses to weaken its alias check.
    """

    if hasattr(fcntl, "F_GETPATH"):
        try:
            # Darwin's F_GETPATH ABI uses MAXPATHLEN (1024); Python also
            # rejects larger mutable-string arguments before the syscall.
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\x00" * 1024)
        except (OSError, ValueError) as exc:
            raise CanonicalVersionProviderError(
                "cannot verify opened member identity", code="containment"
            ) from exc
        path = bytes(raw).split(b"\x00", 1)[0]
        if not path:
            _fail("opened member has no verifiable path", code="containment")
        return path

    proc_link = f"/proc/self/fd/{fd}"
    try:
        value = os.readlink(proc_link)
    except OSError as exc:
        raise CanonicalVersionProviderError(
            "platform cannot verify exact member name", code="unavailable"
        ) from exc
    if value.endswith(" (deleted)"):
        _fail("opened member was unlinked", code="containment")
    return os.fsencode(value)


def _require_exact_opened_name(fd: int, expected: str) -> None:
    actual = os.path.basename(_opened_path(fd))
    if actual != os.fsencode(expected):
        _fail("case or Unicode path alias rejected", code="containment")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _entry_stat(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CanonicalVersionProviderError(
            "catalog member not found", code="not_found"
        ) from exc
    except OSError as exc:
        raise CanonicalVersionProviderError(
            "catalog member stat failed", code="containment"
        ) from exc


def _open_exact_directory(parent_fd: int, name: str) -> int:
    before = _entry_stat(parent_fd, name)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        _fail("catalog member is not a real directory", code="containment")
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    if any(not hasattr(os, flag) for flag in required):
        _fail("platform lacks fail-closed directory flags", code="unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise CanonicalVersionProviderError(
            "catalog member not found", code="not_found"
        ) from exc
    except OSError as exc:
        code = "containment" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "containment"
        raise CanonicalVersionProviderError(
            "catalog directory open rejected", code=code
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode) or _identity(before) != _identity(opened):
            _fail("catalog directory changed while opening", code="containment")
        _require_exact_opened_name(fd, name)
        return fd
    except Exception:
        os.close(fd)
        raise


def _require_reachable(parent_fd: int, name: str, child_fd: int) -> None:
    current = _entry_stat(parent_fd, name)
    opened = os.fstat(child_fd)
    if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(opened):
        _fail("catalog directory identity drift", code="containment")
    _require_exact_opened_name(child_fd, name)


@contextmanager
def _catalog_directory(
    layout: _I.InstanceLayout, catalog_key: str
) -> Iterator[int]:
    """Open exactly shared/report-versions/<catalog_key>, without listing."""

    try:
        with layout.root_fd() as root_fd:
            shared_fd = _open_exact_directory(root_fd, "shared")
            try:
                versions_fd = _open_exact_directory(shared_fd, _REPORT_VERSIONS)
                try:
                    catalog_fd = _open_exact_directory(versions_fd, catalog_key)
                    try:
                        yield catalog_fd
                        _require_reachable(versions_fd, catalog_key, catalog_fd)
                        _require_reachable(shared_fd, _REPORT_VERSIONS, versions_fd)
                        _require_reachable(root_fd, "shared", shared_fd)
                    finally:
                        os.close(catalog_fd)
                finally:
                    os.close(versions_fd)
            finally:
                os.close(shared_fd)
    except CanonicalVersionProviderError:
        raise
    except (_I.InstanceError, OSError) as exc:
        raise CanonicalVersionProviderError(
            "instance catalog is unavailable", code="unavailable"
        ) from exc


def _read_regular_file_at(
    parent_fd: int, name: str, *, max_bytes: int
) -> bytes:
    """Read one exact regular file and reject link/race/alias drift."""

    before = _entry_stat(parent_fd, name)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        _fail("catalog file is not a single-link regular file", code="containment")
    if before.st_size < 1 or before.st_size > max_bytes:
        _fail("catalog file size is outside the frozen bound", code="corrupt_catalog")
    if not hasattr(os, "O_NOFOLLOW"):
        _fail("platform lacks fail-closed file flags", code="unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise CanonicalVersionProviderError(
            "catalog file not found", code="not_found"
        ) from exc
    except OSError as exc:
        raise CanonicalVersionProviderError(
            "catalog file open rejected", code="containment"
        ) from exc
    try:
        opened_before = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or _identity(before) != _identity(opened_before)
        ):
            _fail("catalog file changed while opening", code="containment")
        _require_exact_opened_name(fd, name)

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, min(_READ_CHUNK, max_bytes + 1 - total))
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                _fail("catalog file exceeds the frozen bound", code="corrupt_catalog")
        raw = b"".join(chunks)

        opened_after = os.fstat(fd)
        linked_after = _entry_stat(parent_fd, name)
        if (
            _identity(opened_before) != _identity(opened_after)
            or _identity(opened_after) != _identity(linked_after)
            or len(raw) != opened_after.st_size
        ):
            _fail("catalog file changed while reading", code="containment")
        _require_exact_opened_name(fd, name)
        return raw
    finally:
        os.close(fd)


def _parse_canonical_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = _C.strict_json_loads(text)
    except (UnicodeDecodeError, ValueError, _C.ContractError) as exc:
        raise CanonicalVersionProviderError(
            f"{label} is not strict JSON", code="corrupt_catalog"
        ) from exc
    if type(value) is not dict:
        _fail(f"{label} is not a JSON object", code="corrupt_catalog")
    try:
        canonical = _C.canonical_json_bytes(value)
    except _C.ContractError as exc:
        raise CanonicalVersionProviderError(
            f"{label} is not JCS safe", code="corrupt_catalog"
        ) from exc
    if canonical != raw:
        _fail(f"{label} bytes are not canonical JCS", code="corrupt_catalog")
    return value


def _require_exact_fields(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if frozenset(value) != expected:
        _fail(f"{label} field set mismatch", code="corrupt_catalog")


def _require_datetime(value: Any, *, label: str) -> None:
    if type(value) is not str or value != value.strip():
        _fail(f"{label} is not an RFC3339 datetime", code="corrupt_catalog")
    if "T" not in value or _TIMEZONE_SUFFIX_RE.search(value) is None:
        _fail(f"{label} lacks an RFC3339 timezone", code="corrupt_catalog")
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalVersionProviderError(
            f"{label} is not a real datetime", code="corrupt_catalog"
        ) from exc


def _require_sha256(value: Any, *, label: str) -> None:
    if type(value) is not str or _C.SHA256_HEX_REGEX.fullmatch(value) is None:
        _fail(f"{label} is not lowercase SHA-256", code="corrupt_catalog")


def _require_positive_safe_int(value: Any, *, label: str) -> None:
    if type(value) is not int or value < 1 or value > _MAX_SAFE_INTEGER:
        _fail(f"{label} is not a positive I-JSON integer", code="corrupt_catalog")


def _validate_head(
    head: dict[str, Any], *, report_key: str, catalog_key: str
) -> None:
    _require_exact_fields(head, _HEAD_FIELDS, "catalog.head")
    if type(head["schema"]) is not str or head["schema"] != "cwk.rt014.catalog_head.v1":
        _fail("catalog.head schema mismatch", code="corrupt_catalog")
    if type(head["catalog_key"]) is not str or head["catalog_key"] != catalog_key:
        _fail("catalog.head catalog_key mismatch", code="corrupt_catalog")
    if type(head["report_key"]) is not str or head["report_key"] != report_key:
        _fail("catalog.head report_key mismatch", code="corrupt_catalog")
    _require_positive_safe_int(head["entry_count"], label="entry_count")
    _require_positive_safe_int(head["head_revision"], label="head_revision")
    if head["entry_count"] != head["head_revision"]:
        _fail("catalog revision/count mismatch", code="corrupt_catalog")
    if type(head["latest_object_id"]) is not str or _OBJECT_ID_RE.fullmatch(
        head["latest_object_id"]
    ) is None:
        _fail("latest_object_id grammar mismatch", code="corrupt_catalog")
    _require_sha256(head["latest_canonical_sha256"], label="latest_canonical_sha256")
    _require_sha256(head["catalog_jsonl_sha256"], label="catalog_jsonl_sha256")
    _require_datetime(head["created_at"], label="created_at")
    _require_datetime(head["updated_at"], label="updated_at")


def _validate_entry(entry: dict[str, Any], *, report_key: str) -> None:
    _require_exact_fields(entry, _ENTRY_FIELDS, "catalog entry")
    if type(entry["schema"]) is not str or entry["schema"] != "cwk.report_version.v1":
        _fail("catalog entry schema mismatch", code="corrupt_catalog")
    if type(entry["report_key"]) is not str or entry["report_key"] != report_key:
        _fail("catalog entry report_key mismatch", code="corrupt_catalog")
    _require_sha256(entry["canonical_sha256"], label="canonical_sha256")
    _require_sha256(entry["object_bytes_sha256"], label="object_bytes_sha256")
    if type(entry["object_id"]) is not str or _OBJECT_ID_RE.fullmatch(entry["object_id"]) is None:
        _fail("catalog entry object_id grammar mismatch", code="corrupt_catalog")
    _require_datetime(entry["first_seen_at"], label="first_seen_at")
    _require_datetime(entry["source_updated_at"], label="source_updated_at")
    if type(entry["normalizer_version"]) is not str or _NORMALIZER_RE.fullmatch(
        entry["normalizer_version"]
    ) is None:
        _fail("normalizer_version grammar mismatch", code="corrupt_catalog")


def _parse_entries(raw: bytes, *, report_key: str) -> list[dict[str, Any]]:
    if not raw.endswith(b"\n"):
        _fail("catalog.jsonl must end with one record newline", code="corrupt_catalog")
    lines = raw.split(b"\n")[:-1]
    if not lines or any(not line for line in lines):
        _fail("catalog.jsonl contains no records or an empty record", code="corrupt_catalog")
    entries: list[dict[str, Any]] = []
    canonical_seen: set[str] = set()
    object_seen: set[str] = set()
    for line in lines:
        entry = _parse_canonical_json(line, label="catalog entry")
        _validate_entry(entry, report_key=report_key)
        canonical_sha = entry["canonical_sha256"]
        object_id = entry["object_id"]
        if canonical_sha in canonical_seen or object_id in object_seen:
            _fail("catalog contains duplicate version identity", code="corrupt_catalog")
        canonical_seen.add(canonical_sha)
        object_seen.add(object_id)
        entries.append(entry)
    return entries


def _read_catalog_observation(
    layout: _I.InstanceLayout, *, report_key: str, catalog_key: str
) -> _CatalogObservation:
    with _catalog_directory(layout, catalog_key) as catalog_fd:
        head_bytes = _read_regular_file_at(
            catalog_fd, _CATALOG_HEAD, max_bytes=_MAX_HEAD_BYTES
        )
        jsonl_bytes = _read_regular_file_at(
            catalog_fd, _CATALOG_JSONL, max_bytes=_MAX_CATALOG_BYTES
        )
    head = _parse_canonical_json(head_bytes, label="catalog.head")
    _validate_head(head, report_key=report_key, catalog_key=catalog_key)
    entries = _parse_entries(jsonl_bytes, report_key=report_key)
    if len(entries) != head["entry_count"]:
        _fail("catalog head/jsonl count mismatch", code="corrupt_catalog")
    if hashlib.sha256(jsonl_bytes).hexdigest() != head["catalog_jsonl_sha256"]:
        _fail("catalog head/jsonl hash mismatch", code="corrupt_catalog")
    latest = entries[-1]
    if (
        latest["canonical_sha256"] != head["latest_canonical_sha256"]
        or latest["object_id"] != head["latest_object_id"]
    ):
        _fail("catalog latest pointer mismatch", code="corrupt_catalog")
    snapshot = CanonicalVersionSnapshotV1(
        schema=CANONICAL_VERSION_SNAPSHOT_SCHEMA,
        report_key=report_key,
        canonical_sha256=head["latest_canonical_sha256"],
        catalog_revision=head["head_revision"],
        catalog_head_sha256=hashlib.sha256(head_bytes).hexdigest(),
    )
    return _CatalogObservation(
        snapshot=snapshot,
        head_bytes=head_bytes,
        jsonl_bytes=jsonl_bytes,
    )


class CanonicalVersionProvider:
    """Concrete provider backed by one injected RT-012 instance layout."""

    API_VERSION = CANONICAL_VERSION_PROVIDER_API_VERSION
    __slots__ = ("_layout", "_shared_store")

    def __init__(self, *, layout: _I.InstanceLayout) -> None:
        if not isinstance(layout, _I.InstanceLayout):
            raise CanonicalVersionProviderError(
                "layout must be InstanceLayout", code="contract"
            )
        self._layout = layout
        # Public factory only.  Keeping construction here guarantees the
        # catalog point lookup and object verification use the same layout.
        self._shared_store = _S.SharedEvidenceStore.open(layout)

    def resolve_current(self, *, report_key: str) -> CanonicalVersionSnapshotV1:
        key = _require_report_key(report_key)
        catalog_key = _derive_catalog_lookup_key(key)
        first = _read_catalog_observation(
            self._layout, report_key=key, catalog_key=catalog_key
        )
        try:
            verified = self._shared_store.read_version(
                key, first.snapshot.canonical_sha256
            )
        except _S.SharedEvidenceError as exc:
            raise CanonicalVersionProviderError(
                "current canonical object failed public verification",
                code="object_verification_failed",
            ) from exc
        if (
            type(verified) is not dict
            or verified.get("canonical_sha256") != first.snapshot.canonical_sha256
        ):
            _fail("public reader returned a mismatched object", code="object_verification_failed")

        # The public object verification happens outside our catalog FDs.  A
        # second exact point-read prevents a concurrent append from turning
        # the returned value into an already-stale "current" snapshot.
        second = _read_catalog_observation(
            self._layout, report_key=key, catalog_key=catalog_key
        )
        if first.head_bytes != second.head_bytes or first.jsonl_bytes != second.jsonl_bytes:
            _fail("catalog changed during current-version resolution", code="concurrent_update")
        return first.snapshot


class NullCanonicalVersionProvider:
    """Default provider used until an authoritative instance is injected."""

    API_VERSION = CANONICAL_VERSION_PROVIDER_API_VERSION
    __slots__ = ()

    def resolve_current(self, *, report_key: str) -> CanonicalVersionSnapshotV1:
        _require_report_key(report_key)
        raise CanonicalVersionUnavailable()


__all__ = [
    "CANONICAL_VERSION_PROVIDER_API_VERSION",
    "CANONICAL_VERSION_SNAPSHOT_FIELDS",
    "CANONICAL_VERSION_SNAPSHOT_SCHEMA",
    "CanonicalVersionProvider",
    "CanonicalVersionProviderError",
    "CanonicalVersionProviderV1",
    "CanonicalVersionSnapshotV1",
    "CanonicalVersionUnavailable",
    "NullCanonicalVersionProvider",
    "RT014_CATALOG_KEY_DOMAIN",
    "RT014_CATALOG_KEY_FIXED_VECTOR",
    "RT014_CATALOG_KEY_FIXED_VECTOR_REPORT_KEY",
]
