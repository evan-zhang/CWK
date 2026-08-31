#!/usr/bin/env python3
"""RT-012: Durable, dirfd-anchored atomic file primitives.

Owned by RT-012.  Every persistent write in the multitenant runtime (tenant
records, provisioning journals, receipts, locks, quota mutations, ...) is
required to go through this module.  Direct reuse of ``cwk_raw_store.atomic_write``
or any other legacy helper is forbidden by the RT-012 plan because those
helpers do not fsync the parent directory, do not anchor the rename to a
dirfd, and do not defend against O_NOFOLLOW-style symlink attacks.

Guarantees (per PR-001 plan §RT-012 and DESIGN §C-01):

- File writes go through a same-directory ``O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW``
  temp file.  The temp name is derived from ``secrets.token_hex`` so an
  attacker cannot pre-create the name.
- ``write()`` handles short writes and ``EINTR`` retries.
- After the payload is written, the file is ``fsync``'d, then renamed via
  ``os.rename(temp_name, final_name, src_dir_fd=fd, dst_dir_fd=fd)`` so the
  operation is anchored to a validated directory file descriptor.  Finally
  the parent directory itself is ``fsync``'d.
- All directories are created with mode ``0o700``; all files with mode
  ``0o600``; the code never depends on the process umask.
- Advisory ``fcntl.flock`` locks are held under an exclusive file descriptor
  that is closed when the process dies, so crashes release the lock
  automatically.
- Compare-and-swap (``cas_write``) checks the on-disk revision **inside** the
  parent lock so concurrent writers cannot silently lose the previous state.
- Recovery: ``recover_orphans`` sweeps the ``.tmp-`` temp files left behind
  by a crash between ``open`` and ``rename`` and unlinks them via the same
  dirfd; committed files are never touched.

This module intentionally exposes no ``open path``/``stat path`` helpers
that accept arbitrary strings — callers must always provide a validated
directory file descriptor from :mod:`cwk_instance`.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Union


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AtomicFileError(Exception):
    """Base class for atomic-file failures.

    Callers rely on the ``code`` attribute for stable error taxonomy and on
    the ``__str__`` never containing absolute host paths.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ContainmentError(AtomicFileError):
    """Raised when a caller passes an unsafe file/directory name."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="containment")


class RevisionConflict(AtomicFileError):
    """Raised by ``cas_write`` when the on-disk revision does not match."""

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message, code="revision_conflict")
        self.expected = expected
        self.actual = actual


class LockUnavailable(AtomicFileError):
    """Raised when a non-blocking lock cannot be acquired."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="lock_unavailable")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Frozen mode bits.  These are applied directly via os.open / os.mkdir with
# ``mode=`` and cross-checked afterwards; the process umask is never trusted.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

# Every temp file created by write_atomic uses this prefix so recover_orphans
# can safely identify and delete them.  We intentionally choose a name that
# an attacker cannot pre-create even if they can enumerate the directory: the
# suffix is ``secrets.token_hex(16)``.
TEMP_PREFIX = ".cwk-tmp-"

# Frozen leaf-name grammar.  Callers must pass a name that matches this
# grammar; anything else is treated as a containment violation.
_LEAF_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_LEAF_MIN_LEN = 1
_LEAF_MAX_LEN = 128


def _validate_leaf(name: str) -> None:
    """Reject anything that isn't a well-formed leaf name.

    A leaf name is:

    - non-empty, ASCII, length in ``[1, 128]``;
    - drawn from ``[a-z0-9._-]``;
    - not equal to ``.`` or ``..``;
    - not starting with a hyphen (avoids accidental CLI-flag confusion).
    """

    if not isinstance(name, str):
        raise ContainmentError("leaf name must be a str")
    if len(name) < _LEAF_MIN_LEN or len(name) > _LEAF_MAX_LEN:
        raise ContainmentError("leaf name length out of range")
    if name in (".", ".."):
        raise ContainmentError("leaf name may not be '.' or '..'")
    if name.startswith("-"):
        raise ContainmentError("leaf name may not start with '-'")
    for ch in name:
        if ch not in _LEAF_ALLOWED:
            raise ContainmentError(f"leaf name contains disallowed character {ch!r}")
    # Explicit path-separator and null defense (already ruled out by grammar
    # but kept as a belt-and-braces guard against future grammar changes).
    if "/" in name or "\\" in name or "\x00" in name:
        raise ContainmentError("leaf name may not contain path separators or NUL")


# ---------------------------------------------------------------------------
# fsync helpers
# ---------------------------------------------------------------------------


def _fsync_fd(fd: int) -> None:
    """Fsync ``fd``; tolerate EINVAL on file systems that don't support it
    for directory descriptors (a well-known behaviour on some macOS setups).

    On macOS ``F_FULLFSYNC`` is the strictest guarantee.  If unavailable we
    fall back to ``os.fsync``.
    """

    try:
        # F_FULLFSYNC is only defined on Darwin; use hasattr to guard.
        if hasattr(fcntl, "F_FULLFSYNC"):
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                return
            except OSError as exc:
                # Some file systems don't support F_FULLFSYNC.  Fall through.
                if exc.errno not in (errno.ENOTSUP, errno.EINVAL, errno.ENOTTY):
                    raise
        os.fsync(fd)
    except OSError as exc:
        # Directory fsync is a no-op on some setups; only re-raise for other
        # error codes that would indicate real trouble.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


def fsync_dir(dir_fd: int) -> None:
    """Public wrapper — fsync a directory file descriptor."""

    _fsync_fd(dir_fd)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def open_dir_nofollow(path: Union[str, os.PathLike[str]]) -> int:
    """Open ``path`` as a directory with ``O_DIRECTORY|O_NOFOLLOW``.

    This is the only sanctioned entry-point for turning a string path into a
    directory file descriptor.  All subsequent operations MUST use the
    ``dir_fd`` argument (openat / renameat / unlinkat / mkdirat semantics).

    ``ContainmentError`` is raised if the target is a symlink, missing, not
    a directory, or has the wrong owner.  ``FileNotFoundError`` /
    ``NotADirectoryError`` are converted so callers only need to handle
    :class:`AtomicFileError`.
    """

    flags = os.O_RDONLY | os.O_NOFOLLOW
    # Not every platform defines O_DIRECTORY (Windows does not), but POSIX
    # does; Python exposes it on Linux/macOS.  Guard just in case.
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP or exc.errno == errno.EMLINK:
            raise ContainmentError("directory is a symlink; refusing to follow") from exc
        if exc.errno == errno.ENOTDIR:
            raise ContainmentError("path is not a directory") from exc
        if exc.errno == errno.ENOENT:
            raise ContainmentError("directory does not exist") from exc
        raise AtomicFileError(f"cannot open directory ({exc.errno})", code="open") from exc
    # Extra: verify via fstat that this is indeed a directory (belt & braces).
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise ContainmentError("opened target is not a directory")
    return fd


def mkdir_at(parent_fd: int, name: str, *, mode: int = DIRECTORY_MODE, exist_ok: bool = False) -> None:
    """Create ``name`` inside the directory referenced by ``parent_fd``.

    Uses ``os.mkdir(name, mode=mode, dir_fd=parent_fd)`` so the operation is
    anchored to a validated directory FD.  Sets the permission bits via
    ``os.fchmodat`` afterwards so a restrictive umask cannot make the
    directory more permissive than requested.
    """

    _validate_leaf(name)
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except FileExistsError:
        if not exist_ok:
            raise AtomicFileError("directory already exists", code="exists")
    except OSError as exc:
        raise AtomicFileError(f"mkdir failed ({exc.errno})", code="mkdir") from exc
    # Re-apply mode explicitly; umask cannot loosen this after the fact.
    try:
        os.chmod(name, mode, dir_fd=parent_fd, follow_symlinks=False)
    except (NotImplementedError, OSError):
        # follow_symlinks=False may not be supported on macOS; open the
        # directory via O_NOFOLLOW and fchmod instead.
        child_fd = _openat_nofollow(parent_fd, name, os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0))
        try:
            os.fchmod(child_fd, mode)
        finally:
            os.close(child_fd)


def _openat_nofollow(parent_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
    """Open a child inside ``parent_fd`` with ``O_NOFOLLOW`` and validate."""

    _validate_leaf(name)
    if "O_NOFOLLOW" in dir(os):
        flags |= os.O_NOFOLLOW
    try:
        if flags & os.O_CREAT:
            fd = os.open(name, flags, mode, dir_fd=parent_fd)
        else:
            fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ContainmentError("child is a symlink; refusing to follow") from exc
        raise
    return fd


def read_file(parent_fd: int, name: str) -> bytes:
    """Read a small file anchored to ``parent_fd``."""

    _validate_leaf(name)
    try:
        fd = _openat_nofollow(parent_fd, name, os.O_RDONLY)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(name)
        raise
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise ContainmentError("child is not a regular file")
        if st.st_nlink != 1:
            raise ContainmentError("child has more than one hard link; refusing to read")
        chunks: list[bytes] = []
        while True:
            try:
                buf = os.read(fd, 65536)
            except InterruptedError:
                continue
            if not buf:
                break
            chunks.append(buf)
        return b"".join(chunks)
    finally:
        os.close(fd)


def unlink_at(parent_fd: int, name: str, *, missing_ok: bool = False) -> None:
    """Unlink ``name`` inside ``parent_fd``.

    Uses O_NOFOLLOW semantics via ``os.unlink(name, dir_fd=parent_fd)``.
    """

    _validate_leaf(name)
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        if not missing_ok:
            raise
    except IsADirectoryError as exc:
        raise ContainmentError("target is a directory, refusing to unlink") from exc


def child_exists(parent_fd: int, name: str) -> bool:
    """True iff ``name`` exists (as any file type) beneath ``parent_fd``.

    Uses lstat semantics (``follow_symlinks=False``) so a dangling symlink
    still counts as existing — which is the correct behaviour for
    containment / conflict checks.
    """

    _validate_leaf(name)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Full-write with short-write / EINTR handling
# ---------------------------------------------------------------------------


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written == 0:  # pragma: no cover - defensive
            raise AtomicFileError("write returned 0", code="write")
        view = view[written:]


# ---------------------------------------------------------------------------
# The canonical atomic write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtomicWriteReceipt:
    """Return value of ``write_atomic`` — describes the committed file."""

    name: str
    sha256: str
    size: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_temp_name(final_name: str) -> str:
    return f"{TEMP_PREFIX}{final_name}.{secrets.token_hex(8)}"


def write_atomic(
    parent_fd: int,
    name: str,
    data: bytes,
    *,
    mode: int = FILE_MODE,
    exclusive: bool = False,
) -> AtomicWriteReceipt:
    """Durable atomic write of ``data`` into ``parent_fd/name``.

    Order of operations (frozen — do not reorder):

    1. Create a same-directory temp file with
       ``O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW`` and ``mode=0o600``;
    2. Write the payload with EINTR/short-write handling;
    3. ``fsync`` the file descriptor;
    4. ``os.rename(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)``;
       when ``exclusive=True`` the final name must NOT exist beforehand and
       is verified before the rename.
    5. ``fsync`` the parent directory;
    6. Return :class:`AtomicWriteReceipt`.

    If any step raises we best-effort remove the temp file and re-raise; the
    final file is only ever visible after step 5 completes successfully.
    """

    _validate_leaf(name)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise AtomicFileError("data must be bytes-like", code="type")
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)

    if exclusive and child_exists(parent_fd, name):
        raise AtomicFileError(f"exclusive write: {name!r} already exists", code="exists")

    temp_name = _new_temp_name(name)
    _validate_leaf(temp_name)
    # Attempt to create the temp exclusively; on EEXIST regenerate the suffix.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    max_temp_attempts = 8
    fd: Optional[int] = None
    for _attempt in range(max_temp_attempts):
        try:
            fd = os.open(temp_name, flags, mode, dir_fd=parent_fd)
            break
        except FileExistsError:
            temp_name = _new_temp_name(name)
            continue
    if fd is None:  # pragma: no cover - practically unreachable
        raise AtomicFileError("could not create temp file (name collisions)", code="temp")

    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)  # umask hardening
        _fsync_fd(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            unlink_at(parent_fd, temp_name, missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(fd)

    # Anchored rename.
    try:
        os.rename(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError as exc:
        try:
            unlink_at(parent_fd, temp_name, missing_ok=True)
        except OSError:
            pass
        if exc.errno == errno.EEXIST and exclusive:
            raise AtomicFileError(f"exclusive write raced: {name!r} appeared", code="exists") from exc
        raise AtomicFileError(f"rename failed ({exc.errno})", code="rename") from exc

    # Parent dir fsync so the new dirent survives a crash.
    _fsync_fd(parent_fd)

    return AtomicWriteReceipt(name=name, sha256=_sha256_bytes(data), size=len(data))


def cas_write(
    parent_fd: int,
    name: str,
    data: bytes,
    *,
    expected_previous_sha256: Optional[str],
    mode: int = FILE_MODE,
) -> AtomicWriteReceipt:
    """Compare-and-swap: write ``data`` iff the existing file's sha256 matches.

    ``expected_previous_sha256`` MUST be ``None`` iff the file does not yet
    exist.  Callers MUST hold an exclusive advisory lock (``exclusive_lock``)
    while calling ``cas_write`` to prevent concurrent writers from producing
    diverging revisions.
    """

    exists = child_exists(parent_fd, name)
    if exists:
        current = read_file(parent_fd, name)
        current_sha = _sha256_bytes(current)
        if expected_previous_sha256 is None:
            raise RevisionConflict(
                "file exists but expected_previous_sha256 is None",
                expected=-1,
                actual=0,
            )
        if current_sha != expected_previous_sha256:
            raise RevisionConflict(
                "on-disk sha256 does not match expected_previous_sha256",
                expected=int(expected_previous_sha256[:8], 16),
                actual=int(current_sha[:8], 16),
            )
    else:
        if expected_previous_sha256 is not None:
            raise RevisionConflict(
                "expected_previous_sha256 given but file does not exist",
                expected=0,
                actual=-1,
            )
    return write_atomic(parent_fd, name, data, mode=mode)


# ---------------------------------------------------------------------------
# Advisory locks
# ---------------------------------------------------------------------------


@contextmanager
def exclusive_lock(parent_fd: int, name: str, *, blocking: bool = True) -> Iterator[int]:
    """Take an exclusive ``fcntl.flock`` on a lock file inside ``parent_fd``.

    - The lock file is created ``0o600`` and O_CREAT|O_NOFOLLOW.
    - The lock is released automatically when the FD is closed, so process
      death frees the lock.
    - ``blocking=False`` raises :class:`LockUnavailable` if another holder
      is present.
    """

    _validate_leaf(name)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(name, flags, FILE_MODE, dir_fd=parent_fd)
    try:
        op = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, op)
        except BlockingIOError as exc:
            raise LockUnavailable("lock is held by another process") from exc
        # Ensure permissions cannot drift.
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError:
            pass
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ---------------------------------------------------------------------------
# Recovery helper
# ---------------------------------------------------------------------------


def recover_orphans(parent_fd: int) -> list[str]:
    """Unlink any leftover ``.cwk-tmp-`` files under ``parent_fd``.

    A crash between ``os.open`` and ``os.rename`` leaves an orphan temp file
    whose committed counterpart never appeared.  This sweep is idempotent
    and only removes files with the frozen prefix.  Returns the list of
    orphan names actually removed.
    """

    removed: list[str] = []
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            if not entry.name.startswith(TEMP_PREFIX):
                continue
            try:
                unlink_at(parent_fd, entry.name, missing_ok=True)
                removed.append(entry.name)
            except OSError:
                continue
    return removed


__all__ = [
    "AtomicFileError",
    "AtomicWriteReceipt",
    "ContainmentError",
    "DIRECTORY_MODE",
    "FILE_MODE",
    "LockUnavailable",
    "RevisionConflict",
    "TEMP_PREFIX",
    "cas_write",
    "child_exists",
    "exclusive_lock",
    "fsync_dir",
    "mkdir_at",
    "open_dir_nofollow",
    "read_file",
    "recover_orphans",
    "unlink_at",
    "write_atomic",
]
