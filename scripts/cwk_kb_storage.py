#!/usr/bin/env python3
"""RT-042: storage abstraction for the KB platform (local FS + NAS FileStation).

One protocol, three backends, no third-party dependencies:

- :class:`StorageBackend` — the protocol every KB writer talks to.  It is
  deliberately tiny (mkdir / write / read / exists / list_dir / sha256 /
  remove / remove_dir) so that a second implementation is auditable by
  reading it, not by trusting it.
- :class:`LocalFSBackend` — the **contract backend**.  Writes are atomic
  (temp file + ``os.replace``) and every path is re-anchored under the
  configured root, so a symlink planted inside the tree cannot be used to
  escape it.
- :class:`MemoryBackend` — an in-process backend with the same semantics.
  Used by the dual-backend equivalence criterion (J1) and by dry runs.
- :class:`FileStationBackend` — Synology FileStation over HTTPS
  (``urllib.request``).  Credentials come from the environment only.

Credential rule (KB-PARAMETERS §F.5).  ``FileStationBackend.from_env()`` is
the only constructor the CLIs use, and it reads:

===========================  ==========================================
``CWK_NAS_KB_HOST``          ``nas.example.lan`` or ``nas.example.lan:5001``
``CWK_NAS_KB_USER``          service account name
``CWK_NAS_KB_PASSWORD``      service account password
``CWK_NAS_KB_SHARE``         share root, e.g. ``/kb``
===========================  ==========================================

There is no ``--password`` flag anywhere in this RT and there is no default
credential baked into the source: :func:`credentials_from_env` raises
:class:`MissingCredentials` when a variable is absent, and
:func:`assert_no_plaintext_credential_flags` rejects an ``argv`` that carries
a credential on the command line (process tables are world-readable, so a
password passed there has already leaked by the time it is parsed).

Path rule (J3).  Every public method funnels through :func:`normalize_path`,
which rejects absolute paths, ``..`` segments, drive letters, backslash
separators, NUL bytes and empty components *before* any I/O happens.  The
local backend then re-checks the resolved path against the resolved root so
a symlink that appeared between two calls still cannot redirect a write.

Reliability rule.  FileStation calls go through :func:`retry_call`, which
retries transient failures (5xx, connection resets, timeouts and the
FileStation "device busy" family) with exponential backoff.  Every write is
idempotent: ``mkdir`` on an existing folder succeeds, ``write`` overwrites,
``remove`` on a missing file succeeds.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, TypeVar

SCHEMA = "cwk.kb.storage.v1"

# Environment variables that carry NAS credentials.  Nothing else in this
# repository may hold them; see KB-PARAMETERS §F.5.
ENV_HOST = "CWK_NAS_KB_HOST"
ENV_USER = "CWK_NAS_KB_USER"
ENV_PASSWORD = "CWK_NAS_KB_PASSWORD"
ENV_SHARE = "CWK_NAS_KB_SHARE"

# Flags that would put a secret into the process table.  Rejected on sight.
FORBIDDEN_CREDENTIAL_FLAGS = (
    "--password",
    "--passwd",
    "--pass",
    "--nas-password",
    "--secret",
    "--token",
    "--api-key",
)

T = TypeVar("T")


# ── errors ──────────────────────────────────────────────────────────────────


class StorageError(Exception):
    """Base class for every failure raised by this module."""


class UnsafePath(StorageError):
    """A caller supplied a path that escapes, or could escape, the KB root."""


class NotFound(StorageError):
    """The requested object does not exist."""


class MissingCredentials(StorageError):
    """A required ``CWK_NAS_KB_*`` variable is not set."""


class PlaintextCredential(StorageError):
    """A credential was passed on the command line."""


class TransientStorageError(StorageError):
    """A failure that is worth retrying (5xx, reset connection, busy device)."""


class RemoteStorageError(StorageError):
    """A failure the remote reported as permanent."""


# ── path safety (J3) ────────────────────────────────────────────────────────


def normalize_path(path: str) -> str:
    """Return ``path`` as a safe repository-relative POSIX path.

    Rejects anything that is not unambiguously *inside* the KB root.  This
    runs before any I/O in every backend, so the check cannot be bypassed by
    picking a different method.
    """
    if not isinstance(path, str):
        raise UnsafePath(f"路径必须是 str，收到 {type(path).__name__}")
    if path == "":
        raise UnsafePath("路径不能为空")
    if "\x00" in path:
        raise UnsafePath("路径含 NUL 字节")
    if "\\" in path:
        raise UnsafePath(f"路径含反斜杠分隔符，拒绝：{path!r}")
    if path.startswith("/") or path.startswith("~"):
        raise UnsafePath(f"拒绝绝对路径 / home 展开：{path!r}")
    if len(path) >= 2 and path[1] == ":":
        raise UnsafePath(f"拒绝盘符路径：{path!r}")

    parts: List[str] = []
    for part in PurePosixPath(path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePath(f"拒绝上跳路径段 '..'：{path!r}")
        if part == "/":
            raise UnsafePath(f"拒绝绝对路径：{path!r}")
        parts.append(part)
    if not parts:
        raise UnsafePath(f"路径规范化后为空：{path!r}")
    return "/".join(parts)


def join_under(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and prove the result stays inside.

    ``normalize_path`` already removed the textual escapes; this second check
    catches the symlink case, where every path component is innocent but a
    component resolves outside the tree.
    """
    safe = normalize_path(rel)
    root_resolved = root.resolve()
    candidate = root_resolved / safe
    # ``strict=False``: the leaf usually does not exist yet on a write.
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise UnsafePath(f"路径解析后逃逸出库根：{rel!r} → {resolved}")
    # Also refuse when any *existing* parent is a symlink pointing out of the
    # tree — ``resolve`` above covers it, but an explicit lstat keeps the
    # failure message honest about what happened.
    probe = root_resolved
    for part in PurePosixPath(safe).parts:
        probe = probe / part
        if probe.is_symlink():
            target = probe.resolve()
            if target != root_resolved and root_resolved not in target.parents:
                raise UnsafePath(f"路径经由符号链接逃逸出库根：{rel!r} → {target}")
    return candidate


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── protocol ────────────────────────────────────────────────────────────────


class StorageBackend(Protocol):
    """The contract every KB writer codes against.

    Implementations must be idempotent: ``mkdir`` on an existing directory,
    ``write`` over an existing file and ``remove`` of a missing file all
    succeed instead of raising.
    """

    name: str

    def mkdir(self, path: str) -> None:
        """Create ``path`` and every missing parent.  Idempotent."""

    def write(self, path: str, data: bytes) -> str:
        """Write ``data`` at ``path`` (creating parents).  Returns the sha256."""

    def read(self, path: str) -> bytes:
        """Return the bytes at ``path``; raise :class:`NotFound` if absent."""

    def exists(self, path: str) -> bool:
        """True when a file or directory lives at ``path``."""

    def list_dir(self, path: str) -> List[str]:
        """Return the sorted child names of directory ``path``."""

    def walk_files(self, path: str = ".") -> List[str]:
        """Return every file path under ``path``, sorted, root-relative."""

    def sha256(self, path: str) -> str:
        """Return the sha256 of the object at ``path``."""

    def remove(self, path: str) -> None:
        """Delete the file at ``path``.  Idempotent."""

    def remove_dir(self, path: str) -> None:
        """Delete the directory at ``path`` recursively.  Idempotent."""


# ── local filesystem (contract backend) ─────────────────────────────────────


class LocalFSBackend:
    """Filesystem backend rooted at ``root``.  This is the contract backend."""

    name = "localfs"

    def __init__(self, root: Path | str) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        # Resolved once: every other method compares against ``self.root``,
        # and a mix of resolved and unresolved forms (``/tmp`` vs
        # ``/private/tmp`` on macOS) would make those comparisons lie.
        self.root = Path(root).resolve()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LocalFSBackend(root={self.root!s})"

    def _path(self, path: str) -> Path:
        return join_under(self.root, path)

    def mkdir(self, path: str) -> None:
        self._path(path).mkdir(parents=True, exist_ok=True)

    def write(self, path: str, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise StorageError("write 只接受 bytes——文本请显式 encode('utf-8')")
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise UnsafePath(f"拒绝覆盖符号链接：{path!r}")
        with tempfile.NamedTemporaryFile(
            "wb", dir=target.parent, prefix=".cwk-kb-tmp-", delete=False
        ) as handle:
            handle.write(bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
            tmp = Path(handle.name)
        os.replace(tmp, target)
        return sha256_bytes(bytes(data))

    def read(self, path: str) -> bytes:
        target = self._path(path)
        if not target.is_file():
            raise NotFound(f"文件不存在：{path}")
        return target.read_bytes()

    def exists(self, path: str) -> bool:
        try:
            return self._path(path).exists()
        except UnsafePath:
            raise

    def list_dir(self, path: str) -> List[str]:
        target = self._path(path) if path not in (".", "") else self.root
        if not target.is_dir():
            raise NotFound(f"目录不存在：{path}")
        return sorted(child.name for child in target.iterdir())

    def walk_files(self, path: str = ".") -> List[str]:
        base = self.root if path in (".", "") else self._path(path)
        if not base.exists():
            return []
        out: List[str] = []
        for current, _dirs, files in os.walk(base):
            for name in files:
                if name.startswith(".cwk-kb-tmp-"):
                    continue
                rel = Path(current, name).relative_to(self.root)
                out.append(rel.as_posix())
        return sorted(out)

    def sha256(self, path: str) -> str:
        return sha256_bytes(self.read(path))

    def remove(self, path: str) -> None:
        target = self._path(path)
        try:
            target.unlink()
        except FileNotFoundError:
            return

    def remove_dir(self, path: str) -> None:
        import shutil

        target = self._path(path)
        if target.is_dir():
            shutil.rmtree(target)


# ── in-memory backend ───────────────────────────────────────────────────────


class MemoryBackend:
    """In-process backend with LocalFS semantics.

    Used by the J1 equivalence criterion (same operation sequence, two
    backends, byte-identical terminal state) and by any caller that wants a
    dry run.  Directories are tracked explicitly so ``list_dir`` on an empty
    directory behaves like the real thing.
    """

    name = "memory"

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.dirs: set[str] = set()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MemoryBackend(files={len(self.files)}, dirs={len(self.dirs)})"

    @staticmethod
    def _parents(path: str) -> Iterable[str]:
        parts = path.split("/")
        for i in range(1, len(parts)):
            yield "/".join(parts[:i])

    def mkdir(self, path: str) -> None:
        safe = normalize_path(path)
        self.dirs.add(safe)
        self.dirs.update(self._parents(safe))

    def write(self, path: str, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise StorageError("write 只接受 bytes——文本请显式 encode('utf-8')")
        safe = normalize_path(path)
        self.dirs.update(self._parents(safe))
        self.files[safe] = bytes(data)
        return sha256_bytes(bytes(data))

    def read(self, path: str) -> bytes:
        safe = normalize_path(path)
        if safe not in self.files:
            raise NotFound(f"文件不存在：{path}")
        return self.files[safe]

    def exists(self, path: str) -> bool:
        safe = normalize_path(path)
        return safe in self.files or safe in self.dirs

    def list_dir(self, path: str) -> List[str]:
        if path in (".", ""):
            prefix = ""
        else:
            safe = normalize_path(path)
            if safe not in self.dirs:
                raise NotFound(f"目录不存在：{path}")
            prefix = safe + "/"
        names = set()
        for known in list(self.files) + list(self.dirs):
            if prefix and not known.startswith(prefix):
                continue
            rest = known[len(prefix) :]
            if not rest:
                continue
            names.add(rest.split("/")[0])
        return sorted(names)

    def walk_files(self, path: str = ".") -> List[str]:
        if path in (".", ""):
            return sorted(self.files)
        prefix = normalize_path(path) + "/"
        return sorted(p for p in self.files if p.startswith(prefix))

    def sha256(self, path: str) -> str:
        return sha256_bytes(self.read(path))

    def remove(self, path: str) -> None:
        self.files.pop(normalize_path(path), None)

    def remove_dir(self, path: str) -> None:
        prefix = normalize_path(path) + "/"
        for known in [p for p in self.files if p.startswith(prefix)]:
            del self.files[known]
        self.dirs = {d for d in self.dirs if d != prefix.rstrip("/") and not d.startswith(prefix)}


# ── credentials ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NasCredentials:
    """NAS connection settings.  Never serialised, never logged."""

    host: str
    user: str
    password: str = field(repr=False)
    share: str

    def __repr__(self) -> str:
        # Defensive: a stray repr in a traceback must not print the password.
        return f"NasCredentials(host={self.host!r}, user={self.user!r}, share={self.share!r})"

    def redacted(self) -> Dict[str, str]:
        return {"host": self.host, "user": self.user, "share": self.share}


def credentials_from_env(env: Optional[Dict[str, str]] = None) -> NasCredentials:
    """Read the four ``CWK_NAS_KB_*`` variables or fail loudly."""
    source = os.environ if env is None else env
    missing = [
        name
        for name in (ENV_HOST, ENV_USER, ENV_PASSWORD, ENV_SHARE)
        if not source.get(name)
    ]
    if missing:
        raise MissingCredentials(
            "缺少 NAS 凭据环境变量：" + ", ".join(missing) + "。凭据只从环境变量读，"
            "不接受命令行明文，也不写进任何配置文件。"
        )
    return NasCredentials(
        host=source[ENV_HOST].strip(),
        user=source[ENV_USER].strip(),
        password=source[ENV_PASSWORD],
        share=normalize_share(source[ENV_SHARE]),
    )


def normalize_share(share: str) -> str:
    """Return the share root as ``/name`` — FileStation wants a leading slash."""
    cleaned = "/" + share.strip().strip("/")
    if cleaned == "/":
        raise MissingCredentials(f"{ENV_SHARE} 不能是根目录")
    if ".." in PurePosixPath(cleaned).parts:
        raise UnsafePath(f"{ENV_SHARE} 含上跳路径段：{share!r}")
    return cleaned


def assert_no_plaintext_credential_flags(argv: Sequence[str]) -> None:
    """Refuse an ``argv`` that carries a secret on the command line."""
    for token in argv:
        head = token.split("=", 1)[0]
        if head in FORBIDDEN_CREDENTIAL_FLAGS:
            raise PlaintextCredential(
                f"拒绝命令行明文凭据 {head}：进程表全局可读，凭据只从 "
                f"{ENV_PASSWORD} 等环境变量读取。"
            )


# ── retry ───────────────────────────────────────────────────────────────────


@dataclass
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.2
    max_delay: float = 3.0
    sleep: Callable[[float], None] = time.sleep

    def delay_for(self, attempt: int) -> float:
        return min(self.base_delay * (2 ** attempt), self.max_delay)


def retry_call(policy: RetryPolicy, operation: Callable[[], T]) -> T:
    """Run ``operation``, retrying :class:`TransientStorageError` with backoff.

    Permanent failures (auth, not-found, malformed response) are re-raised on
    the first occurrence — retrying them only turns a clear error into a slow
    one.
    """
    last: Optional[BaseException] = None
    for attempt in range(policy.attempts):
        try:
            return operation()
        except TransientStorageError as exc:
            last = exc
            if attempt == policy.attempts - 1:
                break
            policy.sleep(policy.delay_for(attempt))
    assert last is not None
    raise last


# ── Synology FileStation ────────────────────────────────────────────────────

# FileStation error codes that describe a temporary condition.  Everything
# else is treated as permanent so a wrong password fails immediately instead
# of being retried four times.
FILESTATION_TRANSIENT_CODES = frozenset({105, 118, 119, 407, 800, 1002, 1003})
FILESTATION_EXISTS_CODES = frozenset({408, 414, 1100, 1805})


class FileStationBackend:
    """Synology FileStation backend over HTTPS.

    Only ``urllib`` is used, so the backend runs on a clean Python 3.11 with
    no wheels installed.  Construct it with :meth:`from_env`; the explicit
    constructor exists so tests can inject a fake transport.
    """

    name = "filestation"

    def __init__(
        self,
        credentials: NasCredentials,
        *,
        prefix: str = "",
        transport: Optional[Callable[[urllib.request.Request], bytes]] = None,
        retry: Optional[RetryPolicy] = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.credentials = credentials
        self.prefix = normalize_path(prefix) if prefix else ""
        self.retry = retry or RetryPolicy()
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._transport = transport or self._https_transport
        self._sid: Optional[str] = None

    @classmethod
    def from_env(
        cls, env: Optional[Dict[str, str]] = None, **kwargs
    ) -> "FileStationBackend":
        return cls(credentials_from_env(env), **kwargs)

    # -- path helpers -------------------------------------------------------

    def _remote(self, path: str) -> str:
        safe = normalize_path(path)
        parts = [self.credentials.share.strip("/")]
        if self.prefix:
            parts.append(self.prefix)
        parts.append(safe)
        return "/" + "/".join(parts)

    def _base_url(self) -> str:
        host = self.credentials.host
        if "://" in host:
            raise MissingCredentials(
                f"{ENV_HOST} 只填主机名或 host:port，协议固定 https"
            )
        if ":" not in host:
            host = f"{host}:5001"
        return f"https://{host}/webapi"

    # -- transport ----------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        if self.verify_tls:
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _https_transport(self, request: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context()
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code == 429:
                raise TransientStorageError(f"FileStation HTTP {exc.code}") from exc
            raise RemoteStorageError(f"FileStation HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TransientStorageError(f"FileStation 连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise TransientStorageError("FileStation 请求超时") from exc

    def _call(self, request: urllib.request.Request) -> dict:
        raw = retry_call(self.retry, lambda: self._transport(request))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteStorageError("FileStation 返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteStorageError("FileStation 返回的 JSON 不是对象")
        return payload

    @staticmethod
    def _error_code(payload: dict) -> Optional[int]:
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), int):
            return error["code"]
        return None

    def _check(self, payload: dict, *, tolerate: Iterable[int] = ()) -> dict:
        if payload.get("success"):
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        code = self._error_code(payload)
        if code in set(tolerate):
            return {}
        if code in FILESTATION_TRANSIENT_CODES:
            raise TransientStorageError(f"FileStation 暂时性错误 code={code}")
        raise RemoteStorageError(f"FileStation 错误 code={code}")

    # -- session ------------------------------------------------------------

    def login(self) -> str:
        if self._sid:
            return self._sid
        query = urllib.parse.urlencode(
            {
                "api": "SYNO.API.Auth",
                "version": "6",
                "method": "login",
                "account": self.credentials.user,
                "passwd": self.credentials.password,
                "session": "FileStation",
                "format": "sid",
            }
        )
        # POST body, not query string: a query string ends up in the NAS
        # access log, and that log is not a place for a password.
        request = urllib.request.Request(
            f"{self._base_url()}/auth.cgi",
            data=query.encode("utf-8"),
            method="POST",
        )
        data = self._check(self._call(request))
        sid = data.get("sid")
        if not isinstance(sid, str) or not sid:
            raise RemoteStorageError("FileStation 登录成功但没有返回 sid")
        self._sid = sid
        return sid

    def logout(self) -> None:
        if not self._sid:
            return
        query = urllib.parse.urlencode(
            {
                "api": "SYNO.API.Auth",
                "version": "1",
                "method": "logout",
                "session": "FileStation",
                "_sid": self._sid,
            }
        )
        request = urllib.request.Request(
            f"{self._base_url()}/auth.cgi", data=query.encode("utf-8"), method="POST"
        )
        try:
            self._call(request)
        except StorageError:
            pass
        finally:
            self._sid = None

    def _get(self, cgi: str, params: Dict[str, str], *, tolerate: Iterable[int] = ()) -> dict:
        merged = dict(params)
        merged["_sid"] = self.login()
        request = urllib.request.Request(
            f"{self._base_url()}/{cgi}",
            data=urllib.parse.urlencode(merged).encode("utf-8"),
            method="POST",
        )
        return self._check(self._call(request), tolerate=tolerate)

    def _download(self, cgi: str, params: Dict[str, str]) -> bytes:
        merged = dict(params)
        merged["_sid"] = self.login()
        request = urllib.request.Request(
            f"{self._base_url()}/{cgi}?{urllib.parse.urlencode(merged)}", method="GET"
        )
        raw = retry_call(self.retry, lambda: self._transport(request))
        # A failed download returns a JSON error envelope instead of bytes.
        if raw[:1] == b"{":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return raw
            if isinstance(payload, dict) and "success" in payload:
                code = self._error_code(payload)
                if code in (408, 1100):
                    raise NotFound("FileStation 下载目标不存在")
                self._check(payload)
        return raw

    # -- StorageBackend -----------------------------------------------------

    def mkdir(self, path: str) -> None:
        safe = normalize_path(path)
        parts = safe.split("/")
        for depth in range(len(parts)):
            parent_rel = "/".join(parts[:depth])
            parent = self._remote(parent_rel) if parent_rel else self._remote_root()
            self._get(
                "entry.cgi",
                {
                    "api": "SYNO.FileStation.CreateFolder",
                    "version": "2",
                    "method": "create",
                    "folder_path": parent,
                    "name": parts[depth],
                    "force_parent": "true",
                },
                # 408/414/1100: the folder already exists.  mkdir is idempotent.
                tolerate=FILESTATION_EXISTS_CODES,
            )

    def _remote_root(self) -> str:
        parts = [self.credentials.share.strip("/")]
        if self.prefix:
            parts.append(self.prefix)
        return "/" + "/".join(parts)

    def write(self, path: str, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise StorageError("write 只接受 bytes——文本请显式 encode('utf-8')")
        payload = bytes(data)
        safe = normalize_path(path)
        parent = PurePosixPath(safe).parent.as_posix()
        if parent not in (".", ""):
            self.mkdir(parent)
        boundary = "----cwk-kb-" + secrets.token_hex(16)
        body = self._multipart(
            boundary,
            fields={
                "api": "SYNO.FileStation.Upload",
                "version": "2",
                "method": "upload",
                "path": self._remote(parent) if parent not in (".", "") else self._remote_root(),
                # overwrite makes the write idempotent: replaying a batch after
                # a mid-run crash converges instead of erroring on conflict.
                "overwrite": "true",
                "_sid": self.login(),
            },
            filename=PurePosixPath(safe).name,
            payload=payload,
        )
        request = urllib.request.Request(
            f"{self._base_url()}/entry.cgi",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self._check(self._call(request))
        return sha256_bytes(payload)

    @staticmethod
    def _multipart(
        boundary: str, *, fields: Dict[str, str], filename: str, payload: bytes
    ) -> bytes:
        buffer = io.BytesIO()
        for key, value in fields.items():
            buffer.write(f"--{boundary}\r\n".encode("utf-8"))
            buffer.write(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
            )
            buffer.write(f"{value}\r\n".encode("utf-8"))
        buffer.write(f"--{boundary}\r\n".encode("utf-8"))
        buffer.write(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(
                "utf-8"
            )
        )
        buffer.write(b"Content-Type: application/octet-stream\r\n\r\n")
        buffer.write(payload)
        buffer.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return buffer.getvalue()

    def read(self, path: str) -> bytes:
        return self._download(
            "entry.cgi",
            {
                "api": "SYNO.FileStation.Download",
                "version": "2",
                "method": "download",
                "path": self._remote(path),
                "mode": "open",
            },
        )

    def exists(self, path: str) -> bool:
        try:
            data = self._get(
                "entry.cgi",
                {
                    "api": "SYNO.FileStation.List",
                    "version": "2",
                    "method": "getinfo",
                    "path": json.dumps([self._remote(path)]),
                },
                tolerate=(408, 1100),
            )
        except NotFound:
            return False
        files = data.get("files")
        if not isinstance(files, list) or not files:
            return False
        entry = files[0]
        return isinstance(entry, dict) and "code" not in entry

    def list_dir(self, path: str) -> List[str]:
        target = self._remote(path) if path not in (".", "") else self._remote_root()
        data = self._get(
            "entry.cgi",
            {
                "api": "SYNO.FileStation.List",
                "version": "2",
                "method": "list",
                "folder_path": target,
            },
        )
        files = data.get("files")
        if not isinstance(files, list):
            raise NotFound(f"目录不存在：{path}")
        return sorted(str(item.get("name")) for item in files if isinstance(item, dict))

    def walk_files(self, path: str = ".") -> List[str]:
        out: List[str] = []
        stack = [path if path not in (".", "") else ""]
        while stack:
            current = stack.pop()
            listing = self._get(
                "entry.cgi",
                {
                    "api": "SYNO.FileStation.List",
                    "version": "2",
                    "method": "list",
                    "folder_path": self._remote(current) if current else self._remote_root(),
                    "additional": json.dumps(["type"]),
                },
                tolerate=(408, 1100),
            )
            for item in listing.get("files") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name"))
                child = f"{current}/{name}" if current else name
                if item.get("isdir"):
                    stack.append(child)
                else:
                    out.append(child)
        return sorted(out)

    def sha256(self, path: str) -> str:
        return sha256_bytes(self.read(path))

    def remove(self, path: str) -> None:
        self._get(
            "entry.cgi",
            {
                "api": "SYNO.FileStation.Delete",
                "version": "2",
                "method": "delete",
                "path": json.dumps([self._remote(path)]),
            },
            # Deleting something that is already gone is a success, not an error.
            tolerate=(408, 1100),
        )

    def remove_dir(self, path: str) -> None:
        self.remove(path)


# ── factory ─────────────────────────────────────────────────────────────────


def build_backend(
    kind: str,
    *,
    root: Optional[Path | str] = None,
    prefix: str = "",
    env: Optional[Dict[str, str]] = None,
) -> StorageBackend:
    """Return a backend by name.  ``local`` needs ``root``; ``nas`` needs env."""
    if kind == "local":
        if root is None:
            raise StorageError("local 后端必须给 --root")
        return LocalFSBackend(root)
    if kind == "memory":
        return MemoryBackend()
    if kind in ("nas", "filestation"):
        return FileStationBackend.from_env(env, prefix=prefix)
    raise StorageError(f"未知后端：{kind}（可选 local / memory / nas）")


__all__ = [
    "SCHEMA",
    "ENV_HOST",
    "ENV_USER",
    "ENV_PASSWORD",
    "ENV_SHARE",
    "StorageBackend",
    "LocalFSBackend",
    "MemoryBackend",
    "FileStationBackend",
    "NasCredentials",
    "RetryPolicy",
    "StorageError",
    "UnsafePath",
    "NotFound",
    "MissingCredentials",
    "PlaintextCredential",
    "TransientStorageError",
    "RemoteStorageError",
    "assert_no_plaintext_credential_flags",
    "build_backend",
    "credentials_from_env",
    "join_under",
    "normalize_path",
    "retry_call",
    "sha256_bytes",
]
