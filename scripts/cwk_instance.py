#!/usr/bin/env python3
"""RT-012: Instance Layout Resolver and opaque tenant path resolver.

Owned by RT-012.  Every multitenant path resolution goes through this
module.  It never accepts arbitrary ``Path`` objects from the CLI, does
not fall back to repository-level ``runs/`` / ``state/`` / ``.env`` when
``CWK_INSTANCE_ROOT`` is missing, and refuses to follow symlinks anywhere
inside the instance root.

Key invariants (see PR-001 plan §RT-012 and DESIGN §C-01):

- ``CWK_INSTANCE_ROOT`` must be an explicit, absolute, non-empty env var.
  Missing or relative values fail closed — the resolver never falls back
  to ``runs/``, ``state/``, or the repository ``.env``.
- The instance root itself, every intermediate directory, and every
  child accessed inside the tenant sub-tree must not be a symlink;
  ``O_DIRECTORY | O_NOFOLLOW`` is used for every opendir, and
  hard-linked regular files (nlink > 1) are refused when read.
- Only opaque IDs (``t_[a-z0-9]{26}``, ``sp_[a-z0-9]{10,32}``, and
  ``o_[a-z2-7]{26}``) are permitted as path segments.  Fixed leaf names
  (``config``, ``state``, ``locks``, ...) are the only other allowed
  segments; anything else is a containment violation.
- Directory permissions are ``0o700``; file permissions ``0o600``;
  never dependent on ``umask``.
- No public helper accepts a raw ``str`` path from CLI input; instead
  callers work through :class:`TenantLayout` methods that return the
  parent ``dir_fd`` + validated leaf name pair, which then feeds
  :mod:`cwk_atomic_file`.
"""

from __future__ import annotations

import errno
import os
import re
import stat as stat_module
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Sequence

# Reuse the RT-011 frozen tenant/space/object regexes rather than redefining
# them.  RT-011 is the ground truth for identifier grammar (§V-02 of the
# RT-011 independent-verify report explicitly locks these regexes).
import cwk_pr001_contracts as _C


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstanceError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class InstanceRootError(InstanceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="instance_root")


class TenantIdError(InstanceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="tenant_id")


class LayoutError(InstanceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="layout")


# ---------------------------------------------------------------------------
# Frozen leaf-name grammar
# ---------------------------------------------------------------------------


ENV_VAR = "CWK_INSTANCE_ROOT"

# Instance root children.  RT-012 pre-creates these but never writes into
# the RT-013+ semantics (canonical store, audit sink, ...).
INSTANCE_ROOT_CHILDREN: tuple[str, ...] = (
    "shared",
    "registry",
    "tenants",
    "audit",
    "backups",
    "runtime",
    "staging",
)

# Second-level structure under registry/ (created empty; RT-013+ populates).
REGISTRY_CHILDREN: tuple[str, ...] = (
    "tenants",
    "agent-bindings",
    "access-ledger",
    "credentials",
    "provision-receipts",
    "provision-journal",
)

# Frozen tenant sub-directory names — DESIGN §4.
TENANT_CHILDREN: tuple[str, ...] = (
    "config",
    "access",
    "views",
    "routes",
    "knowledge-spaces",
    "indexes",
    "review",
    "archive",
    "state",
    "runs",
    "locks",
    "retries",
    "cache",
    "logs",
    "tmp",
)

# Knowledge-space child directories.
KNOWLEDGE_SPACE_CHILDREN: tuple[str, ...] = (
    "summaries",
    "topics",
    "entities",
    "_system",
)

# Sensitive directories that MUST be 0o700 (checked by doctor).
SENSITIVE_DIRS: tuple[str, ...] = (
    "shared",
    "registry",
    "tenants",
    "audit",
    "backups",
    "runtime",
    "staging",
)

# Sensitive top-level file classes (checked by doctor by prefix, not name).
SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    "*.json",
    "*.lock",
    "*.receipt",
    "*.journal",
)

_LEAF_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_LEAF_MIN_LEN = 1
_LEAF_MAX_LEN = 128


def _validate_fixed_leaf(name: str, allowed: Sequence[str]) -> None:
    if name not in allowed:
        raise LayoutError(f"leaf name {name!r} is not one of the frozen options")


def _validate_leaf_grammar(name: str) -> None:
    if not isinstance(name, str):
        raise LayoutError("leaf name must be a str")
    if len(name) < _LEAF_MIN_LEN or len(name) > _LEAF_MAX_LEN:
        raise LayoutError("leaf name length out of range")
    if name in (".", ".."):
        raise LayoutError("leaf name may not be '.' or '..'")
    if name.startswith("-"):
        raise LayoutError("leaf name may not start with '-'")
    for ch in name:
        if ch not in _LEAF_ALLOWED:
            raise LayoutError(f"leaf name contains disallowed character {ch!r}")


def validate_tenant_id(value: str) -> str:
    """Reject anything that isn't a well-formed opaque tenant ID.

    Delegates to the RT-011 frozen regex ``TENANT_ID_REGEX`` and then adds
    extra defenses against Unicode look-alikes, whitespace, embedded NULs,
    URL-encoded traversal, and Windows-style path separators.
    """

    if not isinstance(value, str):
        raise TenantIdError("tenant_id must be a str")
    # RT-011 regex is anchored \A...\Z and forbids CR/LF/slash/colon/control.
    if not _C.TENANT_ID_REGEX.match(value):
        raise TenantIdError(f"tenant_id does not match {_C.TENANT_ID_REGEX.pattern!r}")
    # Belt-and-braces: explicitly forbid any traversal / encoding variant.
    lower = value.lower()
    for bad in ("..", "/", "\\", "\x00", "%2e", "%2f", "%5c"):
        if bad in lower:
            raise TenantIdError(f"tenant_id contains forbidden sequence {bad!r}")
    return value


def validate_space_id(value: str) -> str:
    if not isinstance(value, str) or not _C.SPACE_ID_REGEX.match(value):
        raise LayoutError(
            f"space_id must match {_C.SPACE_ID_REGEX.pattern!r}"
        )
    return value


# ---------------------------------------------------------------------------
# CWK_INSTANCE_ROOT resolver
# ---------------------------------------------------------------------------


def _validate_instance_root_text(raw: object) -> str:
    """Validate the textual root without resolving or normalising it."""

    if not isinstance(raw, str):
        raise InstanceRootError(f"{ENV_VAR} is not a string")
    if raw == "":
        raise InstanceRootError(f"{ENV_VAR} must not be empty")
    if raw.strip() != raw:
        raise InstanceRootError(f"{ENV_VAR} has leading/trailing whitespace")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise InstanceRootError(f"{ENV_VAR} contains NUL/CR/LF")
    if len(raw) > 4096:
        raise InstanceRootError(f"{ENV_VAR} is too long")
    lower = raw.lower()
    for bad in ("%2e%2e", "%2f", "%5c"):
        if bad in lower:
            raise InstanceRootError(f"{ENV_VAR} contains encoded traversal")
    if not os.path.isabs(raw):
        raise InstanceRootError(f"{ENV_VAR} must be an absolute path")
    if raw == "/":
        raise InstanceRootError(f"{ENV_VAR} may not be the filesystem root")
    if "\\" in raw:
        raise InstanceRootError(f"{ENV_VAR} may not contain backslash separators")
    if raw.endswith("/") or "//" in raw:
        raise InstanceRootError(f"{ENV_VAR} must use a canonical absolute path")
    components = tuple(raw.split("/")[1:])
    if any(component in ("", ".", "..") for component in components):
        raise InstanceRootError(f"{ENV_VAR} must use a canonical absolute path")
    return raw


def _directory_open_flags() -> int:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise InstanceRootError("host lacks required nofollow dir-fd support")
    flags = os.O_RDONLY
    flags |= os.O_DIRECTORY
    flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _directory_identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        raise InstanceRootError("instance root component is not a directory")
    return (st.st_dev, st.st_ino)


def _open_absolute_dir_chain(raw: str) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Open every absolute-path component with ``openat`` and no symlinks.

    The returned FD owns the final component.  Intermediate descriptors are
    closed as the walk advances, while their device/inode identities are
    retained so a later walk can prove that the textual path still names the
    same component chain.
    """

    flags = _directory_open_flags()
    try:
        current_fd = os.open("/", flags)
    except OSError as exc:  # pragma: no cover - a usable POSIX host has '/'
        raise InstanceRootError(
            f"instance root chain cannot be opened safely (errno={exc.errno})"
        ) from exc
    identities: list[tuple[int, int]] = []
    try:
        identities.append(_directory_identity(current_fd))
        components = tuple(raw.split("/")[1:])
        for component in components:
            try:
                before = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if stat_module.S_ISLNK(before.st_mode):
                    raise InstanceRootError(
                        "instance root component is a symbolic link"
                    )
                if not stat_module.S_ISDIR(before.st_mode):
                    raise InstanceRootError(
                        "instance root component is not a directory"
                    )
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise InstanceRootError(
                    f"instance root component cannot be opened safely (errno={exc.errno})"
                ) from exc
            try:
                after_identity = _directory_identity(next_fd)
            except BaseException:
                os.close(next_fd)
                raise
            if after_identity != (before.st_dev, before.st_ino):
                os.close(next_fd)
                raise InstanceRootError(
                    "instance root component changed while being opened"
                )
            os.close(current_fd)
            current_fd = next_fd
            identities.append(after_identity)
        return current_fd, tuple(identities)
    except BaseException:
        os.close(current_fd)
        raise


def _close_fd_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as exc:  # finalizer must never surface an already-closed FD
        if exc.errno != errno.EBADF:
            raise


def resolve_instance_root() -> str:
    """Return the absolute, symlink-free instance root string.

    Fails closed if the env var is missing, empty, whitespace-only, contains
    NUL/CR/LF/percent-encoded traversal, is relative, or points to a symlink.

    Note: we return a ``str`` here (not a ``Path``) because everywhere down
    the stack we open a directory FD immediately and then use ``dir_fd``
    everywhere.  Passing an unwrapped ``Path`` around would encourage
    ``pathlib`` joins that skip our containment check.
    """

    raw = os.environ.get(ENV_VAR)
    if raw is None:
        raise InstanceRootError(f"{ENV_VAR} is not set")
    validated = _validate_instance_root_text(raw)
    fd, _identities = _open_absolute_dir_chain(validated)
    os.close(fd)
    return validated


# ---------------------------------------------------------------------------
# InstanceLayout / TenantLayout
# ---------------------------------------------------------------------------


# Deferred import: cwk_atomic_file also needs to be a stable dependency, but
# to avoid a hard import cycle we import lazily where needed.
def _atomic():
    import cwk_atomic_file as A  # noqa: WPS433

    return A


@dataclass(frozen=True, init=False)
class InstanceLayout:
    """Handle for the top-level ``CWK_INSTANCE_ROOT``.

    ``open()`` pins the root inode with a retained descriptor and records the
    identity of every absolute-path component.  Every later ``root_fd()``
    re-walks the textual path with ``openat(..., O_NOFOLLOW)`` and rejects any
    component drift before yielding a descriptor.  This makes one layout
    incapable of silently switching to a replacement instance tree.
    """

    root: str
    _anchor_fd: int = field(repr=False, compare=False)
    _component_identities: tuple[tuple[int, int], ...] = field(
        repr=False,
        compare=False,
    )
    _lock: threading.RLock = field(repr=False, compare=False)
    _closed: bool = field(repr=False, compare=False)
    _finalizer: weakref.finalize = field(repr=False, compare=False)

    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - misuse
        raise TypeError("InstanceLayout must be created with InstanceLayout.open()")

    @classmethod
    def open(cls, root: str | None = None) -> "InstanceLayout":
        """Open the instance root; ``root`` defaults to
        ``resolve_instance_root()``.  Callers MUST NOT bypass this factory."""

        actual = resolve_instance_root() if root is None else _validate_instance_root_text(root)
        anchor_fd, identities = _open_absolute_dir_chain(actual)
        layout = object.__new__(cls)
        object.__setattr__(layout, "root", actual)
        object.__setattr__(layout, "_anchor_fd", anchor_fd)
        object.__setattr__(layout, "_component_identities", identities)
        object.__setattr__(layout, "_lock", threading.RLock())
        object.__setattr__(layout, "_closed", False)
        object.__setattr__(
            layout,
            "_finalizer",
            weakref.finalize(layout, _close_fd_quietly, anchor_fd),
        )
        return layout

    def __copy__(self) -> "InstanceLayout":
        return self

    def __deepcopy__(self, memo: dict) -> "InstanceLayout":
        del memo
        return self

    def __reduce__(self):  # pragma: no cover - exercised by pickle machinery
        raise TypeError("InstanceLayout cannot be pickled; reopen it explicitly")

    def __enter__(self) -> "InstanceLayout":
        with self._lock:
            if self._closed:
                raise InstanceRootError("instance layout is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        """Release the root anchor; safe to call more than once."""

        with self._lock:
            if self._closed:
                return
            object.__setattr__(self, "_closed", True)
            self._finalizer.detach()
            _close_fd_quietly(self._anchor_fd)

    # -- root-level FD ---------------------------------------------------

    @contextmanager
    def root_fd(self) -> Iterator[int]:
        # Duplicate the anchor while holding the lifecycle lock, then release
        # it before the path walk and before yielding to callers.  The dup
        # keeps the original inode alive if another thread calls close(),
        # without serialising unrelated dirfd operations across the layout.
        with self._lock:
            if self._closed:
                raise InstanceRootError("instance layout is closed")
            try:
                anchor_fd = os.dup(self._anchor_fd)
            except OSError as exc:
                raise InstanceRootError(
                    f"instance root anchor cannot be duplicated (errno={exc.errno})"
                ) from exc
        try:
            fd, identities = _open_absolute_dir_chain(self.root)
            try:
                anchor_identity = _directory_identity(anchor_fd)
                if (
                    identities != self._component_identities
                    or identities[-1] != anchor_identity
                ):
                    raise InstanceRootError("instance root identity changed")
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(anchor_fd)

    @contextmanager
    def child_fd(self, name: str) -> Iterator[int]:
        _validate_fixed_leaf(name, INSTANCE_ROOT_CHILDREN)
        A = _atomic()
        with self.root_fd() as rfd:
            fd = _open_child_dir(rfd, name)
            try:
                yield fd
            finally:
                os.close(fd)

    # -- registry helpers ------------------------------------------------

    @contextmanager
    def registry_fd(self, sub: str) -> Iterator[int]:
        _validate_fixed_leaf(sub, REGISTRY_CHILDREN)
        with self.child_fd("registry") as reg:
            fd = _open_child_dir(reg, sub)
            try:
                yield fd
            finally:
                os.close(fd)

    # -- tenants helpers -------------------------------------------------

    def tenants_root(self) -> "TenantsRoot":
        return TenantsRoot(instance=self)

    def tenant(self, tenant_id: str) -> "TenantLayout":
        validate_tenant_id(tenant_id)
        return TenantLayout(instance=self, tenant_id=tenant_id)

    # -- provisioning helpers -------------------------------------------

    def initialize(self) -> None:
        """Create all fixed top-level directories with ``0o700``.

        Idempotent: safe to call on an already-populated root.  Does not
        create tenant sub-trees; those are provisioned via
        :class:`cwk_tenant_registry.TenantRegistry`.
        """

        A = _atomic()
        with self.root_fd() as rfd:
            for name in INSTANCE_ROOT_CHILDREN:
                A.mkdir_at(rfd, name, mode=A.DIRECTORY_MODE, exist_ok=True)
            # Registry sub-directories.
            with self.child_fd("registry") as reg:
                for name in REGISTRY_CHILDREN:
                    A.mkdir_at(reg, name, mode=A.DIRECTORY_MODE, exist_ok=True)


@dataclass(frozen=True)
class TenantsRoot:
    """Iterator over the tenants/ directory."""

    instance: InstanceLayout

    def list_tenant_ids(self) -> list[str]:
        results: list[str] = []
        try:
            with self.instance.child_fd("tenants") as tfd:
                with os.scandir(tfd) as entries:
                    for entry in entries:
                        if entry.name.startswith("."):
                            continue
                        try:
                            validate_tenant_id(entry.name)
                        except TenantIdError:
                            continue
                        # Must be a real directory and not a symlink.
                        try:
                            st = os.stat(
                                entry.name,
                                dir_fd=tfd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if not stat_module.S_ISDIR(st.st_mode):
                            continue
                        results.append(entry.name)
        except FileNotFoundError:
            return []
        results.sort()
        return results


@dataclass(frozen=True)
class TenantLayout:
    """Handle for a single tenant sub-tree beneath ``tenants/<tenant_id>/``.

    All accessors return a validated ``(dir_fd, leaf_name)`` pair that the
    caller passes into :mod:`cwk_atomic_file`.  No method returns an open
    path string; consumers must use dirfd operations.
    """

    instance: InstanceLayout
    tenant_id: str

    def exists(self) -> bool:
        try:
            with self.instance.child_fd("tenants") as tfd:
                A = _atomic()
                return A.child_exists(tfd, self.tenant_id)
        except InstanceError:
            return False

    @contextmanager
    def tenant_fd(self) -> Iterator[int]:
        with self.instance.child_fd("tenants") as tfd:
            fd = _open_child_dir(tfd, self.tenant_id)
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def child_fd(self, name: str) -> Iterator[int]:
        _validate_fixed_leaf(name, TENANT_CHILDREN)
        with self.tenant_fd() as t_fd:
            fd = _open_child_dir(t_fd, name)
            try:
                yield fd
            finally:
                os.close(fd)

    @contextmanager
    def space_fd(self, space_id: str) -> Iterator[int]:
        validate_space_id(space_id)
        with self.child_fd("knowledge-spaces") as ks:
            fd = _open_child_dir(ks, space_id)
            try:
                yield fd
            finally:
                os.close(fd)

    def initialize(self) -> None:
        """Create all fixed tenant sub-directories with ``0o700``.

        Idempotent; safe to call from ``recover``.  Does NOT create any
        knowledge space (those are provisioned by RT-021).
        """

        A = _atomic()
        with self.instance.child_fd("tenants") as tfd:
            A.mkdir_at(tfd, self.tenant_id, mode=A.DIRECTORY_MODE, exist_ok=True)
        with self.tenant_fd() as t_fd:
            for name in TENANT_CHILDREN:
                A.mkdir_at(t_fd, name, mode=A.DIRECTORY_MODE, exist_ok=True)

    # -- convenience path descriptions used only in structured errors ---

    def containment_prefix(self) -> str:
        """Return ``tenants/<tenant_id>`` — used only in redacted logs.

        This never returns an absolute path.
        """

        return f"tenants/{self.tenant_id}"


# ---------------------------------------------------------------------------
# Low-level helpers (private but re-used by tests)
# ---------------------------------------------------------------------------


def _open_child_dir(parent_fd: int, name: str) -> int:
    """Open a directory inside ``parent_fd``, refusing symlinks and non-dirs."""

    _validate_leaf_grammar(name)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LayoutError(f"child {name!r} is a symlink; refusing to follow") from exc
        if exc.errno == errno.ENOTDIR:
            raise LayoutError(f"child {name!r} is not a directory") from exc
        if exc.errno == errno.ENOENT:
            raise LayoutError(f"child {name!r} does not exist") from exc
        raise LayoutError(f"cannot open child {name!r} (errno={exc.errno})") from exc
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise LayoutError(f"child {name!r} is not a directory")
    return fd


def frozen_layout_descriptor() -> dict:
    """Return the machine-readable descriptor emitted by ``cwk tenant``.

    Matches ``cwk.rt012.instance_layout.v1``.
    """

    return {
        "schema": "cwk.rt012.instance_layout.v1",
        "instance_root_children": list(INSTANCE_ROOT_CHILDREN),
        "tenant_children": list(TENANT_CHILDREN),
        "knowledge_space_children": list(KNOWLEDGE_SPACE_CHILDREN),
        "sensitive_dirs": list(SENSITIVE_DIRS),
        "sensitive_files": list(SENSITIVE_FILE_PATTERNS),
    }


__all__ = [
    "ENV_VAR",
    "INSTANCE_ROOT_CHILDREN",
    "InstanceError",
    "InstanceLayout",
    "InstanceRootError",
    "KNOWLEDGE_SPACE_CHILDREN",
    "LayoutError",
    "REGISTRY_CHILDREN",
    "SENSITIVE_DIRS",
    "SENSITIVE_FILE_PATTERNS",
    "TENANT_CHILDREN",
    "TenantIdError",
    "TenantLayout",
    "TenantsRoot",
    "frozen_layout_descriptor",
    "resolve_instance_root",
    "validate_space_id",
    "validate_tenant_id",
]
