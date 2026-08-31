"""Fail-closed filesystem reads for PR-001 receipt evaluation.

Every receipt, archive entry and referenced artifact consumed by the PR-001
gate / capability-activation / security-gate evaluators is read through this
module.  The original implementation used a `pathlib` check-then-read pattern
(`is_symlink()` / `exists()` / `resolve()` ... then `read_bytes()`), which is a
textbook TOCTOU window: every check ran against a *path string* that could be
re-pointed before the final open, and the receipt files themselves were never
opened safely at all -- an independent review demonstrated that a current
receipt could simply be a symlink.

The chain below is deliberately the same shape as the already-accepted script
evolution guard (`tests/pr001_script_evolution_guard.py::read_checked_bytes`):
walk to the leaf's parent one `openat` hop at a time so each component names a
fixed inode, open the leaf with `O_NOFOLLOW`, then re-verify identity after the
read.  It is a separate module rather than an import of the guard because the
guard's path grammar is deliberately ASCII-only, while PR-001 receipts bind
evidence inside RT packages whose filenames are Chinese.  The guard's own
trust-root and 6-path surface are untouched by this file.

Design corrections applied after an independent read-only audit
---------------------------------------------------------------
The audit reproduced five concrete defects against the first version of this
module.  Each is now closed, and the reason is recorded here so a later reader
does not "simplify" the fix back out:

1.  **The root itself was opened by path without `O_NOFOLLOW`.**  A symlinked
    repository root therefore defeated the entire walk before it started.  The
    root is now `lstat`-ed, opened `O_DIRECTORY|O_NOFOLLOW`, and the open fd is
    `fstat`-ed back against the `lstat` identity.

2.  **Parent fds were closed as the walk advanced.**  Only the immediate parent
    survived to the read, so an attacker could `rename()` a *higher* directory
    after the leaf was open: the read still returned the OLD bytes while the
    path now named a NEW file, and nothing detected the divergence.  The whole
    chain of directory fds is now held open for the duration of the read and
    re-verified afterwards, component by component, plus a fresh re-open of the
    root that must match the original identity.

3.  **The leaf was opened without `O_NONBLOCK`.**  `O_NOFOLLOW` does not reject
    a FIFO, so swapping the leaf for a FIFO made the evaluator *block forever*
    -- a denial of verification, which is strictly worse than a rejection.
    The leaf is opened `O_NONBLOCK` and the flag is dropped after `S_ISREG` is
    confirmed on the open descriptor.

4.  **`safe_listdir` returned raw names.**  Every caller then suffix-filtered
    (`*.json`), which is exactly how `archive/junk.txt` stayed invisible.  That
    function is gone.  `directory_snapshot()` is now the only directory API: it
    enumerates, reads and re-enumerates through one directory fd and rejects
    *every* undeclared or non-regular entry, so a caller cannot choose to
    ignore junk.

5.  **Unicode aliasing was only partly handled.**  Names are now rejected for
    surrogates and control characters, required to be NFC, and any NFC+casefold
    collision inside a directory is refused rather than silently resolved by a
    case-insensitive filesystem.

What is rejected, and why each one matters:

* absolute paths, `..`, `.`, empty components, backslashes, control
  characters, surrogates, non-NFC names, over-long paths  -- path traversal;
* a symlink at the ROOT, at ANY intermediate component, or at the leaf;
* a parent directory swapped mid-walk or mid-read (`rename`) -- caught by
  comparing `(st_dev, st_ino)` across `lstat`/`open` on every hop and again
  after the read;
* a leaf swapped between `lstat` and `open`, or re-pointed while being read;
* hard links (`st_nlink != 1`), before AND after the read;
* non-regular files -- directories, FIFOs, sockets, device nodes;
* mount-point crossings;
* oversize files, bounded before and during the read;
* any in-place rewrite during the read, caught by re-checking
  `st_dev/st_ino/st_nlink/st_size/st_mode/st_mtime_ns/st_ctime_ns` afterwards
  and by requiring the byte count to equal the final `st_size`;
* any undeclared, hidden, non-regular or aliased entry in a snapshotted
  directory.

Everything fails closed: callers get `None` (when `missing_ok`) or a
`SafeReadError`, never a partially-trusted value.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

# A receipt or its bound evidence is a small text artifact.  4 MiB is already
# far past anything legitimate and keeps a hostile file from exhausting memory.
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PATH_CHARS = 512
MAX_COMPONENT_CHARS = 128
# A receipt archive is an append-only chain of a handful of entries.  A bound
# directory with thousands of entries is an attack, not a chain.
MAX_DIR_ENTRIES = 256

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)

# Identity + content fields re-checked across the read window.
_STAT_INVARIANTS = (
    "st_dev",
    "st_ino",
    "st_nlink",
    "st_size",
    "st_mode",
    "st_mtime_ns",
    "st_ctime_ns",
)

# Directory identity + mutation fields re-checked across a snapshot window.
_DIR_INVARIANTS = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")


class SafeReadError(Exception):
    """Raised for every unsafe path shape. Never means 'maybe fine'."""


# ---------------------------------------------------------------------------
# name grammar
# ---------------------------------------------------------------------------


def _fold(name: str) -> str:
    """The equivalence class a case/normalisation-insensitive FS would collapse."""

    return unicodedata.normalize("NFC", name).casefold()


def _name_problem(name: object) -> str | None:
    """Why `name` is unusable as a single path component, or `None` if it is fine."""

    if not isinstance(name, str):
        return f"component must be a string, got {type(name).__name__}"
    if not name:
        return "component is empty"
    if len(name) > MAX_COMPONENT_CHARS:
        return f"component {name!r} is longer than {MAX_COMPONENT_CHARS} characters"
    for ch in name:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return f"component {name!r} contains a control character"
        if 0xD800 <= code <= 0xDFFF:
            # os.listdir() surrogate-escapes undecodable bytes; such a name can
            # never be a legitimate declared artifact.
            return f"component {name!r} contains a surrogate (undecodable byte)"
    if "/" in name or "\\" in name:
        return f"component {name!r} contains a path separator"
    if name.startswith("."):
        # Covers ".", "..", and hidden entries in one rule.
        return f"component {name!r} starts with a dot"
    if name != name.strip():
        return f"component {name!r} has stray whitespace"
    if unicodedata.normalize("NFC", name) != name:
        return f"component {name!r} is not NFC-normalised"
    return None


def _reject_alias_collisions(names: Iterable[str], *, label: str) -> None:
    """Refuse a directory where two entries would alias on a case-folding FS.

    On APFS/HFS+ `Receipt.json` and `receipt.json` are the same file, and NFD
    and NFC spellings of a Chinese filename resolve to each other.  If a bound
    directory contains both spellings we cannot say which one a hash covers, so
    we refuse the directory outright rather than pick one.
    """

    seen: dict[str, str] = {}
    for name in sorted(names):
        key = _fold(name)
        if key in seen:
            raise SafeReadError(
                f"{label}: entries {seen[key]!r} and {name!r} collide under "
                "NFC+casefold; a case/Unicode-insensitive filesystem cannot "
                "distinguish them"
            )
        seen[key] = name


def safe_relpath(rel: object, *, label: str = "path") -> tuple[str, ...]:
    """Split a repo-relative POSIX path, failing closed on every trick.

    Unlike the script-evolution guard's grammar this permits non-ASCII (NFC)
    component names, because PR-001 evidence legitimately lives in files such
    as ``RT/RT-017/tasks/开发任务.md``.  Everything structural is still
    rejected: any component beginning with ``.`` is refused outright, which
    covers ``.``, ``..`` and hidden directories in one rule.
    """

    if not isinstance(rel, str):
        raise SafeReadError(f"{label}: path must be a string, got {type(rel).__name__}")
    if not rel:
        raise SafeReadError(f"{label}: path must not be empty")
    if len(rel) > MAX_PATH_CHARS:
        raise SafeReadError(f"{label}: path longer than {MAX_PATH_CHARS} characters")
    if unicodedata.normalize("NFC", rel) != rel:
        raise SafeReadError(f"{label}: path is not NFC-normalised")
    if rel != rel.strip():
        raise SafeReadError(f"{label}: path has leading/trailing whitespace")
    if rel.startswith("/") or rel.startswith("~") or os.path.isabs(rel):
        raise SafeReadError(f"{label}: path must be repo-relative, not absolute")
    if len(rel) > 1 and rel[1] == ":":
        raise SafeReadError(f"{label}: path must be repo-relative, not a drive path")
    parts = rel.split("/")
    for part in parts:
        problem = _name_problem(part)
        if problem is not None:
            raise SafeReadError(f"{label}: {problem}")
    return tuple(parts)


# ---------------------------------------------------------------------------
# the directory-fd chain
# ---------------------------------------------------------------------------


def _require_dir_fd(label: str) -> None:
    if not _DIR_FD_SUPPORTED:
        raise SafeReadError(
            f"{label}: this platform does not support openat()/fstatat() directory "
            "file descriptors, so receipts cannot be read without a TOCTOU window. "
            "Refusing to verify rather than verifying weakly."
        )


def _ident(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _lstat_at(name: str, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _listdir_fd(fd: int, *, label: str) -> list[str]:
    try:
        return os.listdir(fd)
    except OSError as exc:
        raise SafeReadError(
            f"{label}: cannot list a path component ({exc.__class__.__name__})"
        ) from None


def _check_exact_name(fd: int, name: str, *, label: str, missing_ok: bool) -> bool:
    """Exact-name membership test against the real directory listing.

    macOS/APFS is case- and normalisation-insensitive, so `open` will happily
    resolve `RECEIPT.JSON` to `receipt.json`.  Listing the parent and demanding
    a byte-exact match is the only reliable mitigation -- and if some *other*
    entry folds to the same key, the directory is ambiguous and we refuse.
    """

    names = _listdir_fd(fd, label=label)
    aliases = sorted(n for n in names if n != name and _fold(n) == _fold(name))
    if aliases:
        raise SafeReadError(
            f"{label}: entry {name!r} is ambiguous; {aliases!r} alias it under "
            "NFC+casefold on this filesystem"
        )
    if name in names:
        return True
    if missing_ok:
        return False
    raise SafeReadError(f"{label}: component {name!r} is missing")


class _Chain:
    """A held-open chain of directory fds from the repo root to a leaf's parent.

    Holding *every* hop open (not just the last one) is what closes the
    higher-directory rename race: after the read we can re-`fstatat` each
    component name inside its own parent fd and prove the path still resolves
    to the very inodes we walked through.
    """

    __slots__ = ("root", "label", "root_lstat_ident", "fds", "names", "idents")

    def __init__(self, root: Path, label: str) -> None:
        self.root = Path(root)
        self.label = label
        self.fds: list[int] = []
        self.names: list[str] = []
        self.idents: list[tuple[int, int]] = []
        self.root_lstat_ident: tuple[int, int] | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()

    def __enter__(self) -> "_Chain":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- accessors ---------------------------------------------------------

    @property
    def leaf_parent_fd(self) -> int:
        return self.fds[-1]

    @property
    def root_fd(self) -> int:
        return self.fds[0]

    # -- construction ------------------------------------------------------

    def open_root(self) -> None:
        _require_dir_fd(self.label)
        root_str = str(self.root)
        try:
            pre = os.lstat(root_str)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: unusable root ({exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(pre.st_mode):
            # A symlinked root defeats every subsequent hop, so it is refused
            # before the walk begins.
            raise SafeReadError(f"{self.label}: repository root is a symlink")
        if not stat.S_ISDIR(pre.st_mode):
            raise SafeReadError(f"{self.label}: repository root is not a directory")
        try:
            fd = os.open(root_str, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: cannot open repository root ({exc.__class__.__name__})"
            ) from None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise SafeReadError(f"{self.label}: repository root is not a directory")
            if _ident(opened) != _ident(pre):
                raise SafeReadError(
                    f"{self.label}: repository root was swapped between stat and open (TOCTOU)"
                )
        except BaseException:
            os.close(fd)
            raise
        self.root_lstat_ident = _ident(pre)
        self.fds.append(fd)
        self.idents.append(_ident(opened))

    def descend(self, name: str, *, missing_ok: bool) -> bool:
        """One `openat` hop. Returns False only for a benign missing component."""

        fd = self.leaf_parent_fd
        if not _check_exact_name(fd, name, label=self.label, missing_ok=missing_ok):
            return False
        try:
            pre = _lstat_at(name, fd)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: cannot stat component {name!r} ({exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(pre.st_mode):
            raise SafeReadError(f"{self.label}: component {name!r} is a symlink")
        if not stat.S_ISDIR(pre.st_mode):
            raise SafeReadError(f"{self.label}: component {name!r} is not a directory")
        if pre.st_dev != self.idents[0][0]:
            raise SafeReadError(f"{self.label}: component {name!r} crosses a mount point")
        try:
            child = os.open(
                name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd
            )
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: cannot open component {name!r} ({exc.__class__.__name__})"
            ) from None
        try:
            post = os.fstat(child)
            if _ident(post) != _ident(pre):
                raise SafeReadError(
                    f"{self.label}: component {name!r} was swapped between stat and open (TOCTOU)"
                )
            if post.st_dev != self.idents[0][0]:
                raise SafeReadError(f"{self.label}: component {name!r} crosses a mount point")
        except BaseException:
            os.close(child)
            raise
        self.fds.append(child)
        self.names.append(name)
        self.idents.append(_ident(post))
        return True

    # -- post-read re-verification ----------------------------------------

    def verify_unchanged(self) -> None:
        """Prove the path still resolves to exactly the inodes we walked.

        Called after the leaf has been read.  A `rename()` of any directory in
        the chain -- including one *above* the leaf's parent, which the earlier
        implementation could not see -- changes what the path now means while
        our fds still point at the old inodes.  Re-resolving each name inside
        its own parent fd, plus a fresh open of the root, makes that divergence
        an error instead of a silently stale answer.
        """

        root_str = str(self.root)
        try:
            now = os.lstat(root_str)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: repository root vanished during the read "
                f"({exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(now.st_mode) or _ident(now) != self.root_lstat_ident:
            raise SafeReadError(
                f"{self.label}: repository root was replaced during the read"
            )
        try:
            reopened = os.open(root_str, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: cannot re-open repository root ({exc.__class__.__name__})"
            ) from None
        try:
            if _ident(os.fstat(reopened)) != self.idents[0]:
                raise SafeReadError(
                    f"{self.label}: repository root identity changed during the read"
                )
        finally:
            os.close(reopened)
        for index, name in enumerate(self.names):
            parent_fd = self.fds[index]
            try:
                st = _lstat_at(name, parent_fd)
            except OSError as exc:
                raise SafeReadError(
                    f"{self.label}: component {name!r} vanished during the read "
                    f"({exc.__class__.__name__})"
                ) from None
            if stat.S_ISLNK(st.st_mode):
                raise SafeReadError(
                    f"{self.label}: component {name!r} became a symlink during the read"
                )
            if _ident(st) != self.idents[index + 1]:
                raise SafeReadError(
                    f"{self.label}: component {name!r} was replaced during the read; the path "
                    "now names a different directory than the one that was read"
                )

    def verify_leaf_entry(self, leaf: str, expected: tuple[int, int]) -> None:
        """The leaf NAME must still resolve to the inode we actually read."""

        try:
            st = _lstat_at(leaf, self.leaf_parent_fd)
        except OSError as exc:
            raise SafeReadError(
                f"{self.label}: {leaf!r} vanished during the read ({exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(st.st_mode):
            raise SafeReadError(f"{self.label}: {leaf!r} became a symlink during the read")
        if _ident(st) != expected:
            raise SafeReadError(
                f"{self.label}: {leaf!r} was replaced during the read; the name now points "
                "at a different file than the bytes that were returned"
            )


def _walk(
    root: Path, parts: Sequence[str], *, label: str, missing_ok: bool
) -> tuple[_Chain, str] | None:
    """Build a held-open chain down to the leaf's parent."""

    chain = _Chain(root, label)
    try:
        chain.open_root()
        for name in parts[:-1]:
            if not chain.descend(name, missing_ok=missing_ok):
                chain.close()
                return None
    except BaseException:
        chain.close()
        raise
    return chain, parts[-1]


# ---------------------------------------------------------------------------
# leaf reads
# ---------------------------------------------------------------------------


def _assert_stat_unchanged(
    pre: os.stat_result, post: os.stat_result, fields: Sequence[str], *, label: str
) -> None:
    for field in fields:
        before, after = getattr(pre, field), getattr(post, field)
        if before != after:
            raise SafeReadError(
                f"{label}: changed while it was being read "
                f"({field}: {before} -> {after}); a bound file must be stable for the whole read"
            )


def _read_regular_at(
    parent_fd: int, leaf: str, *, label: str, max_bytes: int
) -> tuple[bytes, tuple[int, int]]:
    """Open + read one regular file by name inside an already-open directory fd.

    Returns the bytes and the `(dev, ino)` of the inode they came from, so the
    caller can prove afterwards that the NAME still points at that inode.
    """

    try:
        pre = _lstat_at(leaf, parent_fd)
    except OSError as exc:
        raise SafeReadError(f"{label}: cannot stat ({exc.__class__.__name__})") from None
    if stat.S_ISLNK(pre.st_mode):
        raise SafeReadError(f"{label}: is a symlink")
    if not stat.S_ISREG(pre.st_mode):
        raise SafeReadError(f"{label}: not a regular file")
    try:
        # O_NONBLOCK matters: O_NOFOLLOW does NOT reject a FIFO, and opening a
        # FIFO with no writer blocks forever.  A hostile swap must make the
        # evaluator FAIL, never hang.
        fd = os.open(
            leaf, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=parent_fd
        )
    except OSError as exc:
        raise SafeReadError(f"{label}: cannot open ({exc.__class__.__name__})") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SafeReadError(f"{label}: not a regular file after open")
        if opened.st_nlink != 1:
            raise SafeReadError(
                f"{label}: file has {opened.st_nlink} hard links; "
                "a bound file must have exactly one"
            )
        if _ident(opened) != _ident(pre):
            raise SafeReadError(f"{label}: file was swapped between stat and open (TOCTOU)")
        if opened.st_size > max_bytes:
            raise SafeReadError(f"{label}: file larger than {max_bytes} bytes")
        # Now that the descriptor is proven to be a regular file, non-blocking
        # mode has served its purpose; clear it so a short read is impossible.
        if _O_NONBLOCK:
            try:
                import fcntl

                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~_O_NONBLOCK)
            except (ImportError, OSError):
                pass
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SafeReadError(f"{label}: file larger than {max_bytes} bytes")
            chunks.append(chunk)
        after = os.fstat(fd)
        _assert_stat_unchanged(opened, after, _STAT_INVARIANTS, label=label)
        if after.st_nlink != 1:
            raise SafeReadError(
                f"{label}: file gained hard links while being read; "
                "a bound file must have exactly one"
            )
        if total != after.st_size:
            raise SafeReadError(
                f"{label}: read {total} bytes but the file reports {after.st_size}"
            )
        return b"".join(chunks), _ident(after)
    finally:
        os.close(fd)


def read_checked_bytes(
    root: Path,
    rel: str,
    *,
    label: str | None = None,
    missing_ok: bool = False,
    max_bytes: int = MAX_FILE_BYTES,
) -> bytes | None:
    """Read a repo-relative regular file, failing closed on every path trick."""

    label = label or rel
    parts = safe_relpath(rel, label=label)
    walked = _walk(root, parts, label=label, missing_ok=missing_ok)
    if walked is None:
        return None
    chain, leaf = walked
    try:
        if not _check_exact_name(chain.leaf_parent_fd, leaf, label=label, missing_ok=missing_ok):
            return None
        data, ident = _read_regular_at(
            chain.leaf_parent_fd, leaf, label=label, max_bytes=max_bytes
        )
        # The bytes are stable; now prove the PATH that named them is too.
        chain.verify_leaf_entry(leaf, ident)
        chain.verify_unchanged()
        return data
    finally:
        chain.close()


def try_read_bytes(root: Path, rel: str, *, max_bytes: int = MAX_FILE_BYTES) -> bytes | None:
    """Non-raising variant: `None` for missing OR unsafe. Fails closed."""

    try:
        return read_checked_bytes(root, rel, missing_ok=True, max_bytes=max_bytes)
    except SafeReadError:
        return None


def read_checked_json(root: Path, rel: str, *, label: str | None = None):
    """Safe-read then parse. Raises `SafeReadError` on unsafe path or bad JSON."""

    data = read_checked_bytes(root, rel, label=label)
    assert data is not None  # missing_ok=False never returns None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeReadError(f"{label or rel}: not valid UTF-8 JSON ({exc.__class__.__name__})")


def try_read_json(root: Path, rel: str):
    """`None` for missing, unsafe or malformed. Fails closed."""

    try:
        data = read_checked_bytes(root, rel, missing_ok=True)
    except SafeReadError:
        return None
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def file_sha256(root: Path, rel: str, *, label: str | None = None) -> str | None:
    """sha256 of a safely-read file, or `None` if it is missing/unsafe."""

    data = try_read_bytes(root, rel)
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def hash_matches(root: Path, rel: str, expected: object) -> bool:
    """True only when `rel` is safe, present and hashes to `expected`."""

    if not isinstance(expected, str):
        return False
    actual = file_sha256(root, rel)
    return actual is not None and actual == expected


# ---------------------------------------------------------------------------
# exact directory snapshot
# ---------------------------------------------------------------------------


class DirectorySnapshot:
    """The complete, verified contents of one bound directory.

    There is no name-filtering entry point on purpose.  The previous
    `safe_listdir()` handed callers a raw name list, every caller filtered it
    to `*.json`, and `archive/junk.txt` was therefore invisible to the archive
    validators -- an independent review reproduced VG-A reporting VALID with a
    non-empty archive.  Building a snapshot instead requires the caller to
    declare, up front, the exact filename grammar the directory is allowed to
    contain; anything outside it is a hard refusal *inside* this module.  A
    caller cannot opt out of seeing junk, because the junk never reaches it.
    """

    __slots__ = ("rel", "files", "dirs", "identity")

    def __init__(
        self,
        rel: str,
        files: dict[str, bytes],
        dirs: tuple[str, ...],
        identity: tuple[int, int],
    ) -> None:
        self.rel = rel
        self.files = files
        self.dirs = dirs
        self.identity = identity

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted((*self.files, *self.dirs)))

    def __len__(self) -> int:
        return len(self.files) + len(self.dirs)

    def __contains__(self, name: object) -> bool:
        return name in self.files or name in self.dirs

    def sha256(self, name: str) -> str:
        return hashlib.sha256(self.files[name]).hexdigest()

    def json(self, name: str):
        """Parse one entry, or `None` when it is not UTF-8 JSON."""

        try:
            return json.loads(self.files[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def directory_snapshot(
    root: Path,
    rel: str,
    *,
    name_pattern,
    allow_dirs: bool = False,
    label: str | None = None,
    missing_ok: bool = True,
    max_entries: int = MAX_DIR_ENTRIES,
    max_bytes: int = MAX_FILE_BYTES,
) -> DirectorySnapshot | None:
    """Enumerate, read and re-enumerate a bound directory through one dir fd.

    This is the ONLY directory API.  It exists because archive validation needs
    *exact membership*: an append-only archive whose declared content is a set
    of `<sha256>.json` files must contain those files and nothing else.

    `name_pattern` is mandatory and is the directory's frozen filename grammar
    (a compiled regex, matched with `fullmatch`).  Requiring it at the call
    site is the whole point: an archive validator must state what the archive
    is allowed to contain before it is allowed to look, so a stray `junk.txt`,
    a `.hidden` dotfile, a misnamed entry, a nested directory, a symlink, a
    hardlinked entry or a name that aliases another under NFC+casefold all make
    the directory unverifiable and raise, instead of being quietly filtered out
    by a `*.json` glob further up the stack.

    `allow_dirs` admits plain sub-directories as entries (used for the security
    receipt root, which is a directory of `RT-0NN/` packages); their names are
    still subject to `name_pattern`, and they are returned as names only.

    The enumerate/read/re-enumerate sequence runs against a single held-open
    directory fd, and the directory's own identity and timestamps are compared
    before and after, so an entry added or removed mid-snapshot is caught.

    Returns `None` when the directory is absent and `missing_ok` (an absent
    archive is legitimate -- VG-A pins exactly that).
    """

    if not hasattr(name_pattern, "fullmatch"):
        raise SafeReadError(
            f"{label or rel}: a compiled name_pattern is required; a bound directory "
            "may not be enumerated without declaring its filename grammar"
        )
    label = label or rel
    parts = safe_relpath(rel, label=label)
    walked = _walk(root, parts, label=label, missing_ok=missing_ok)
    if walked is None:
        return None
    chain, leaf = walked
    try:
        if not _check_exact_name(chain.leaf_parent_fd, leaf, label=label, missing_ok=missing_ok):
            return None
        parent_fd = chain.leaf_parent_fd
        try:
            pre = _lstat_at(leaf, parent_fd)
        except OSError as exc:
            raise SafeReadError(f"{label}: cannot stat ({exc.__class__.__name__})") from None
        if stat.S_ISLNK(pre.st_mode):
            raise SafeReadError(f"{label}: directory is a symlink")
        if not stat.S_ISDIR(pre.st_mode):
            raise SafeReadError(f"{label}: is not a directory")
        try:
            dir_fd = os.open(
                leaf, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent_fd
            )
        except OSError as exc:
            raise SafeReadError(
                f"{label}: cannot open directory ({exc.__class__.__name__})"
            ) from None
        try:
            before = os.fstat(dir_fd)
            if _ident(before) != _ident(pre):
                raise SafeReadError(f"{label}: directory swapped between stat and open (TOCTOU)")
            if not stat.S_ISDIR(before.st_mode):
                raise SafeReadError(f"{label}: is not a directory after open")

            first = _listdir_fd(dir_fd, label=label)
            if len(first) > max_entries:
                raise SafeReadError(
                    f"{label}: directory holds {len(first)} entries, more than the "
                    f"{max_entries} a bound archive may contain"
                )
            for name in sorted(first):
                problem = _name_problem(name)
                if problem is not None:
                    raise SafeReadError(f"{label}: undeclared or illegal entry -- {problem}")
                if not name_pattern.fullmatch(name):
                    raise SafeReadError(
                        f"{label}: undeclared entry {name!r} does not match the frozen "
                        f"filename grammar {name_pattern.pattern!r}"
                    )
            _reject_alias_collisions(first, label=label)

            files: dict[str, bytes] = {}
            dirs: list[str] = []
            for name in sorted(first):
                entry_label = f"{rel}/{name}"
                st = _lstat_at(name, dir_fd)
                if stat.S_ISLNK(st.st_mode):
                    raise SafeReadError(f"{entry_label}: entry is a symlink")
                if stat.S_ISDIR(st.st_mode):
                    if not allow_dirs:
                        raise SafeReadError(f"{entry_label}: entry is a nested directory")
                    dirs.append(name)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise SafeReadError(
                        f"{entry_label}: entry is not a regular file (FIFO, socket or device)"
                    )
                data, ident = _read_regular_at(
                    dir_fd, name, label=entry_label, max_bytes=max_bytes
                )
                # The name must still resolve to the inode we just read.
                now = _lstat_at(name, dir_fd)
                if stat.S_ISLNK(now.st_mode) or _ident(now) != ident:
                    raise SafeReadError(
                        f"{entry_label}: entry was replaced while the directory was snapshotted"
                    )
                files[name] = data

            second = _listdir_fd(dir_fd, label=label)
            if sorted(second) != sorted(first):
                added = sorted(set(second) - set(first))
                removed = sorted(set(first) - set(second))
                raise SafeReadError(
                    f"{label}: directory membership changed during the snapshot "
                    f"(added={added!r}, removed={removed!r})"
                )
            after = os.fstat(dir_fd)
            _assert_stat_unchanged(before, after, _DIR_INVARIANTS, label=label)
            snapshot = DirectorySnapshot(rel, files, tuple(sorted(dirs)), _ident(after))
        finally:
            os.close(dir_fd)
        chain.verify_leaf_entry(leaf, snapshot.identity)
        chain.verify_unchanged()
        return snapshot
    finally:
        chain.close()


def try_directory_snapshot(root: Path, rel: str, *, name_pattern, allow_dirs: bool = False):
    """Non-raising snapshot. `None` for absent, unsafe, or contaminated.

    Callers that must distinguish "absent" from "contaminated" use
    `directory_snapshot()` and catch `SafeReadError`; callers that treat both
    as "no usable archive" use this.
    """

    try:
        return directory_snapshot(
            root, rel, name_pattern=name_pattern, allow_dirs=allow_dirs, missing_ok=True
        )
    except SafeReadError:
        return None
