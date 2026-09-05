#!/usr/bin/env python3
"""RT-044: the read-only HTTP query gateway for a KB (查询网关 v1).

Usage::

    export CWK_KB_ADMIN_KEY=...            # never on the command line
    python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY \\
        --backend local --root /path/to/kb --port 8787
    python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY \\
        --backend nas --prefix libraries/工作库 --check

Three verbs, all ``GET``, all answering ``application/json``:

``/health``     unauthenticated.  Gateway version plus whether the storage
                backend answers.  It deliberately says nothing else — an
                unauthenticated probe must not leak ``kb_code`` (128 random
                bits precisely so a library cannot be enumerated), the
                library name, or any path.
``/query``      substring search over the lineage index
                (``_system/raw-index.json``).  Same semantics as
                ``kb_wizard.py query``: one implementation, imported by the
                wizard, so the two faces cannot drift.
``/citation``   fetches the bytes **from the storage backend on every
                request** and hashes what it just read.  No cache, no
                memoisation, no trusting the index's recorded digest: the
                returned ``sha256`` is of the bytes in this response, which
                is the only way a citation can be evidence rather than a
                claim (RT-044 J3).

Two-process constitution (RT-044 红线).  The factory face (build wizard,
ingest) writes; this process reads.  That is enforced three ways rather than
asserted once:

1. This module imports no write verb.  ``kb_create`` / ``kb_wizard`` /
   ``kb_migrate`` are absent from the import graph, and
   ``tests/test_kb_gateway.py`` parses this file to keep them absent.
2. Every route is served through :meth:`GatewayApp.dispatch`, whose route
   table (:data:`ROUTES`) is the complete public surface; anything else is
   404 and any method other than ``GET`` is 405 before routing happens.
3. The tests drive every route against a backend that raises on ``write`` /
   ``mkdir`` / ``remove``, so "no writes" is observed, not promised.

Authentication (RT-044 J4).  The bearer value is ``sha256(admin_key)``
hex-encoded, supplied in the ``X-KB-Token`` header and compared with
:func:`hmac.compare_digest` so a wrong token costs the same time as a right
one.  The admin key itself is read from an environment *variable name* given
on the command line (CLI-SPEC §一.3: 传变量名不传值); it is never logged,
never echoed and never part of any response.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import socketserver
import sys
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

# Read-side imports only.  ``kb_ledger`` also holds the write primitives; the
# names bound here are the reading half of it and nothing else.
from kb_ledger import dumps, iso, read_json, utc_now  # noqa: E402
from kb_storage import (  # noqa: E402
    NotFound,
    StorageBackend,
    StorageError,
    UnsafePath,
    assert_no_plaintext_credential_flags,
    build_backend,
    close_backend,
    sha256_bytes,
)

GATEWAY_VERSION = "1.0.0"
GATEWAY_SCHEMA = "cwk.kb.gateway.v1"
HEALTH_SCHEMA = "cwk.kb.gateway.health.v1"
QUERY_SCHEMA = "cwk.kb.gateway.query.v1"
CITATION_SCHEMA = "cwk.kb.gateway.citation.v1"
STARTUP_SCHEMA = "cwk.kb.gateway.startup.v1"
ERROR_SCHEMA = "cwk.kb.gateway.error.v1"

RAW_INDEX_REL = "_system/raw-index.json"

TOKEN_HEADER = "X-KB-Token"
CONTENT_TYPE = "application/json; charset=utf-8"

#: The complete public surface.  Adding a verb means adding a row here, which
#: is what the "no management face behind the token" criterion inspects.
ROUTES: Tuple[str, ...] = ("/health", "/query", "/citation")

#: Only ``GET``.  There is no write method, so the refusal is a property of
#: the table rather than of each handler remembering to say no.
ALLOWED_METHODS: Tuple[str, ...] = ("GET",)

EXCERPT_CHARS = 500
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


class GatewayError(Exception):
    """A configuration or request error the gateway answers with, in JSON."""


class BackendUnavailable(GatewayError):
    """The storage backend could not be reached or read."""


# ── auth ────────────────────────────────────────────────────────────────────


def derive_token(admin_key: str) -> str:
    """``sha256(admin_key)`` hex.

    A derived token means the value a query client holds is not the
    administrator's key: it cannot be replayed against the OPS side, and
    rotating the key invalidates every token in one step.
    """
    if not isinstance(admin_key, str) or not admin_key:
        raise GatewayError("管理 Key 为空——无法派生 token")
    return hashlib.sha256(admin_key.encode("utf-8")).hexdigest()


def token_from_env(var_name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Read the admin key from ``env[var_name]`` and derive the token.

    The variable *name* travels on the command line; the value never does.
    An unset or empty variable is fatal — there is no anonymous mode to fall
    back to, because a gateway that starts without auth is worse than one
    that does not start.
    """
    source = os.environ if env is None else env
    if not var_name:
        raise GatewayError("--admin-key-env 必填：给环境变量名，不要给 Key 本身")
    admin_key = source.get(var_name, "")
    if not admin_key:
        raise GatewayError(
            f"环境变量 {var_name} 未设置或为空——网关拒绝在无鉴权状态下启动。"
        )
    return derive_token(admin_key)


def header_value(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup that works for dicts and email.Message."""
    getter = getattr(headers, "get", None)
    if getter is not None:
        found = getter(name)
        if found is not None:
            return str(found)
    lowered = name.lower()
    for key in headers:  # pragma: no cover - plain dict fallback
        if str(key).lower() == lowered:
            return str(headers[key])
    return ""


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time comparison, safe for arbitrary header bytes."""
    try:
        given = (provided or "").encode("utf-8")
        want = expected.encode("utf-8")
    except (AttributeError, UnicodeError):  # pragma: no cover - defensive
        return False
    return hmac.compare_digest(given, want)


# ── the lineage index ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """One ``_system/raw-index.json`` row, read defensively.

    RT-043 owns the writer and is being built in parallel, so every field
    except the id is optional here.  A gateway that raised on an unfamiliar
    row would turn a partially-populated index into a dead query face; it
    reports what it can read instead.
    """

    lineage_id: str
    path: str = ""
    title: str = ""
    version: int = 1
    sha256: str = ""
    status: str = "unknown"
    artifact_kind: str = "document"
    versions: Tuple[dict, ...] = ()

    def as_hit(self) -> dict:
        return {
            "lineage_id": self.lineage_id,
            "title": self.title,
            "version": self.version,
            "path": self.path,
            "sha256": self.sha256,
            "status": self.status,
            "artifact_kind": self.artifact_kind,
        }

    def haystack(self) -> str:
        return " ".join((self.lineage_id, self.title, self.path)).lower()


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse_entry(lineage_id: str, row: object) -> IndexEntry:
    """Build an :class:`IndexEntry` from whatever the index actually holds."""
    if not isinstance(row, dict):
        return IndexEntry(lineage_id=lineage_id, path=str(row or ""))
    versions = row.get("versions")
    chain: Tuple[dict, ...] = tuple(v for v in versions if isinstance(v, dict)) if isinstance(
        versions, list
    ) else ()
    return IndexEntry(
        lineage_id=lineage_id,
        path=str(row.get("path") or ""),
        title=str(row.get("title") or row.get("display_name") or ""),
        version=_as_int(row.get("version"), 1),
        sha256=str(row.get("sha256") or ""),
        status=str(row.get("status") or "unknown"),
        artifact_kind=str(row.get("artifact_kind") or "document"),
        versions=chain,
    )


def load_index(backend: StorageBackend) -> Dict[str, IndexEntry]:
    """Read the lineage index.  Accepts the dict and list shapes."""
    try:
        payload = read_json(backend, RAW_INDEX_REL)
    except NotFound as exc:
        raise BackendUnavailable(
            f"库内缺少 {RAW_INDEX_REL}——这不是一个建好的库，或摄取尚未落账"
        ) from exc
    except (StorageError, ValueError) as exc:
        raise BackendUnavailable(f"读取 {RAW_INDEX_REL} 失败：{exc}") from exc

    entries = payload.get("entries")
    out: Dict[str, IndexEntry] = {}
    if isinstance(entries, dict):
        for lineage_id, row in entries.items():
            out[str(lineage_id)] = parse_entry(str(lineage_id), row)
    elif isinstance(entries, list):
        for row in entries:
            if not isinstance(row, dict):
                continue
            lineage_id = str(row.get("lineage_id") or row.get("id") or "")
            if lineage_id:
                out[lineage_id] = parse_entry(lineage_id, row)
    return out


def clamp_limit(raw: object) -> int:
    limit = _as_int(raw, DEFAULT_LIMIT)
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def query_index(backend: StorageBackend, q: str, *, limit: int = DEFAULT_LIMIT) -> dict:
    """Substring search over lineage id, title and path.

    Shared by the gateway's ``/query`` and ``kb_wizard.py query`` — "同 wizard
    query 语义" is a property of there being one function, not of two
    implementations agreeing today.

    ``matched`` counts every hit; ``results`` is the truncated page, so a
    caller can tell "20 results" from "20 of 4000".
    """
    needle = (q or "").strip().lower()
    if not needle:
        raise GatewayError("查询词 q 不能为空")
    page = clamp_limit(limit)
    hits: List[dict] = []
    for entry in sorted(load_index(backend).values(), key=lambda item: item.lineage_id):
        if needle in entry.haystack():
            hits.append(entry.as_hit())
    return {
        "q": q,
        "matched": len(hits),
        "returned": min(len(hits), page),
        "limit": page,
        "results": hits[:page],
    }


def resolve_version(entry: IndexEntry, version: Optional[int]) -> Tuple[str, int, str]:
    """Return ``(path, version, recorded_sha256)`` for the requested version.

    Version chains are how a live document (a docdb file that keeps being
    edited) stays citable: a citation is pinned to ``(lineage_id, version)``
    per DOCDB-INGEST-DESIGN §二.  An unknown version is a 404 rather than a
    silent fall back to the current one — quietly citing the wrong revision
    is the failure this whole addressing scheme exists to prevent.
    """
    if version is None or version == entry.version:
        if not entry.path:
            raise GatewayError(f"索引条目 {entry.lineage_id} 没有 path，无法定位原件")
        return entry.path, entry.version, entry.sha256
    for row in entry.versions:
        if _as_int(row.get("version"), -1) == version:
            path = str(row.get("path") or entry.path)
            if not path:
                raise GatewayError(
                    f"索引条目 {entry.lineage_id} 的第 {version} 版没有 path"
                )
            return path, version, str(row.get("sha256") or "")
    raise KeyError(version)


def build_citation(
    backend: StorageBackend,
    lineage_id: str,
    version: Optional[int] = None,
    *,
    now: Optional[object] = None,
) -> dict:
    """Fetch the bytes now, hash what was fetched, quote the head of it.

    ``sha256`` is computed from the bytes this call just read, never copied
    from the index.  ``matches_index`` reports whether the two agree, which
    is how a hand-edited raw file surfaces as a finding
    (DOCDB-INGEST-DESIGN §四 「raw 被手工修改」) instead of being papered
    over by a stale digest.

    There is intentionally no ``path`` in the response: CLI-SPEC §三 pins
    citations to ``(lineage_id, version)`` because paths are a cache that
    reclassification invalidates.
    """
    entry = load_index(backend).get(lineage_id)
    if entry is None:
        raise KeyError(lineage_id)
    path, resolved_version, recorded = resolve_version(entry, version)
    try:
        data = backend.read(path)
    except NotFound as exc:
        raise BackendUnavailable(
            f"lineage {lineage_id} 第 {resolved_version} 版在存储后端上读不到（索引指向的原件缺失）"
        ) from exc
    except (StorageError, OSError) as exc:
        raise BackendUnavailable(f"读取 lineage {lineage_id} 失败：{exc}") from exc

    digest = sha256_bytes(data)
    text = data.decode("utf-8", errors="replace")
    return {
        "schema": CITATION_SCHEMA,
        "ok": True,
        "lineage": lineage_id,
        "version": resolved_version,
        "sha256": digest,
        "excerpt": text[:EXCERPT_CHARS],
        "excerpt_chars": len(text[:EXCERPT_CHARS]),
        "truncated": len(text) > EXCERPT_CHARS,
        "bytes": len(data),
        "index_sha256": recorded or None,
        "matches_index": (digest == recorded) if recorded else None,
        "fetch_mode": "live-backend-read",
        "fetched_at": iso(now or utc_now()),
    }


# ── responses ───────────────────────────────────────────────────────────────


@dataclass
class Response:
    status: int
    payload: dict
    headers: Dict[str, str] = field(default_factory=dict)

    def body(self) -> bytes:
        return dumps(self.payload)


def error_payload(kind: str, message: str, **extra: object) -> dict:
    payload = {
        "schema": ERROR_SCHEMA,
        "ok": False,
        "error": {"kind": kind, "message": message},
    }
    payload.update(extra)
    return payload


# ── the app ─────────────────────────────────────────────────────────────────


class GatewayApp:
    """Routing and auth, with no socket anywhere in sight.

    Kept separate from the HTTP handler so the criteria can be checked twice:
    once against this object directly, and once over a real socket.  A
    routing rule that only holds in one of those two is not a rule.
    """

    def __init__(
        self,
        backend: StorageBackend,
        token: str,
        *,
        backend_kind: str = "local",
        version: str = GATEWAY_VERSION,
        clock: Callable[[], object] = utc_now,
    ) -> None:
        self.backend = backend
        self.token = token
        self.backend_kind = backend_kind
        self.version = version
        self.clock = clock

    # -- auth ---------------------------------------------------------------

    def authorized(self, headers: Mapping[str, str]) -> bool:
        return tokens_match(header_value(headers, TOKEN_HEADER), self.token)

    # -- routes -------------------------------------------------------------

    def dispatch(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        """Method gate → health → auth gate → route table → 404.

        The order is the contract.  The method gate runs first so a write
        attempt is refused as a write (405) whether or not the caller has a
        token.  The auth gate runs before the route table so an unknown path
        answers 401 to an unauthenticated caller: probing for a management
        endpoint must not be cheaper than authenticating.
        """
        if (method or "").upper() not in ALLOWED_METHODS:
            return Response(
                405,
                error_payload(
                    "method_not_allowed",
                    f"只读网关不接受 {method} —— 网关进程不实现任何写动词（两进程宪法）",
                    allow=list(ALLOWED_METHODS),
                ),
                {"Allow": ", ".join(ALLOWED_METHODS)},
            )

        parsed = urllib.parse.urlsplit(target or "/")
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if path == "/health":
            return self.health()

        if not self.authorized(headers):
            return Response(
                401,
                error_payload(
                    "unauthorized",
                    f"{TOKEN_HEADER} 缺失或不匹配",
                    auth={"header": TOKEN_HEADER, "derivation": "sha256(admin_key) hex"},
                ),
            )

        if path == "/query":
            return self.query(params)
        if path == "/citation":
            return self.citation(params)
        return Response(
            404,
            error_payload(
                "not_found",
                f"未知路径 {path}",
                routes=list(ROUTES),
            ),
        )

    def health(self) -> Response:
        """Version + reachability.  Nothing identifying, no auth required."""
        reachable, detail = self.probe()
        return Response(
            200,
            {
                "schema": HEALTH_SCHEMA,
                "ok": reachable,
                "version": self.version,
                "read_only": True,
                "routes": list(ROUTES),
                "write_verbs": [],
                "backend": {"kind": self.backend_kind, "reachable": reachable, "detail": detail},
                "at": iso(self.clock()),
            },
        )

    def probe(self) -> Tuple[bool, str]:
        """Can this process read the library's index right now?

        Existence, not a full read: ``/health`` may be polled, and pulling
        the whole index off the NAS every few seconds would make the probe
        the most expensive thing the gateway does.

        The detail string is deliberately coarse (no path, no name, no
        ``kb_code``): ``/health`` is the one unauthenticated route.
        """
        try:
            present = self.backend.exists(RAW_INDEX_REL)
        except (StorageError, OSError):
            return False, "后端不可达"
        if not present:
            return False, "后端可达，但库内没有 lineage 索引"
        return True, "后端可达，lineage 索引在位"

    def query(self, params: Mapping[str, List[str]]) -> Response:
        q = (params.get("q") or [""])[0]
        limit = (params.get("limit") or [str(DEFAULT_LIMIT)])[0]
        try:
            result = query_index(self.backend, q, limit=clamp_limit(limit))
        except BackendUnavailable as exc:
            return Response(503, error_payload("backend_unavailable", str(exc)))
        except GatewayError as exc:
            return Response(400, error_payload("bad_request", str(exc)))
        payload = {"schema": QUERY_SCHEMA, "ok": True, "at": iso(self.clock())}
        payload.update(result)
        return Response(200, payload)

    def citation(self, params: Mapping[str, List[str]]) -> Response:
        lineage = (params.get("lineage") or [""])[0].strip()
        if not lineage:
            return Response(400, error_payload("bad_request", "缺少 lineage 参数"))
        raw_version = (params.get("version") or [""])[0].strip()
        version: Optional[int] = None
        if raw_version:
            try:
                version = int(raw_version)
            except ValueError:
                return Response(
                    400, error_payload("bad_request", f"version 必须是整数：{raw_version!r}")
                )
        try:
            payload = build_citation(self.backend, lineage, version, now=self.clock())
        except KeyError:
            return Response(
                404,
                error_payload(
                    "not_found",
                    f"lineage {lineage!r}"
                    + (f" 第 {version} 版" if version is not None else "")
                    + " 不在索引里",
                ),
            )
        except BackendUnavailable as exc:
            return Response(503, error_payload("backend_unavailable", str(exc)))
        except (GatewayError, UnsafePath) as exc:
            return Response(400, error_payload("bad_request", str(exc)))
        return Response(200, payload)


# ── HTTP ────────────────────────────────────────────────────────────────────


class GatewayHandler(BaseHTTPRequestHandler):
    """A thin socket shell over :class:`GatewayApp`.

    ``do_GET`` is the only verb with a body; every other method — including
    the ones a client might reach for to write — lands on
    :meth:`_refuse_method`, which answers 405 without ever consulting the
    route table.
    """

    protocol_version = "HTTP/1.1"
    server_version = f"cwk-kb-gateway/{GATEWAY_VERSION}"
    sys_version = ""

    app: GatewayApp  # bound by make_server

    # -- plumbing -----------------------------------------------------------

    def _send(self, response: Response, *, with_body: bool = True) -> None:
        body = response.body()
        self.send_response(response.status)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def _drain_body(self) -> None:
        """Read and discard a request body so keep-alive stays coherent."""
        length = _as_int(self.headers.get("Content-Length"), 0)
        if length > 0:
            self.rfile.read(length)

    def log_message(self, fmt: str, *args) -> None:
        """One line per request, on stderr, without the token header."""
        sys.stderr.write(f"[kb-gateway] {self.address_string()} {fmt % args}\n")

    # -- verbs --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._send(self.app.dispatch("GET", self.path, self.headers))

    def _refuse_method(self) -> None:
        self._drain_body()
        self.close_connection = True
        self._send(
            self.app.dispatch(self.command, self.path, self.headers),
            with_body=self.command != "HEAD",
        )

    do_POST = _refuse_method  # noqa: N815 - http.server naming
    do_PUT = _refuse_method  # noqa: N815
    do_PATCH = _refuse_method  # noqa: N815
    do_DELETE = _refuse_method  # noqa: N815
    do_HEAD = _refuse_method  # noqa: N815
    do_OPTIONS = _refuse_method  # noqa: N815


class KbGatewayServer(HTTPServer):
    """``HTTPServer`` without the reverse-DNS stall at bind time.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` to fill in
    ``server_name``, which only CGI ever reads.  On a network where the
    reverse lookup for ``127.0.0.1`` has to time out, that single call costs
    tens of seconds before the first request can be served — an unexplained
    startup hang caused entirely by a field this gateway never uses.
    """

    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def make_server(
    app: GatewayApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> HTTPServer:
    """A single-threaded server bound to ``host``.

    Single-threaded on purpose: one request at a time keeps a single
    FileStation session coherent, and the read face has no throughput
    requirement that would justify the concurrency.
    """
    bound = type("BoundGatewayHandler", (GatewayHandler,), {"app": app})
    return KbGatewayServer((host, port), bound)


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_root(root: Optional[str]) -> Optional[str]:
    """Accept a plain path or a ``file://`` URL for the local backend."""
    if root is None:
        return None
    if root.startswith("file://"):
        parsed = urllib.parse.urlsplit(root)
        if parsed.netloc not in ("", "localhost"):
            raise GatewayError(f"file:// 只支持本机路径，收到 host={parsed.netloc!r}")
        return urllib.parse.unquote(parsed.path)
    return root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KB 只读查询网关：/health /query /citation（GET，JSON）"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="绑定地址，默认只听本机")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--admin-key-env",
        required=True,
        metavar="VAR_NAME",
        help="管理 Key 所在的环境变量名（传变量名，不传 Key）",
    )
    parser.add_argument("--backend", default="local", choices=("local", "memory", "nas"))
    parser.add_argument("--root", help="local 后端的库根目录，支持 file:// 前缀")
    parser.add_argument("--prefix", default="", help="nas 后端在 share 下的子路径")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验配置并输出启动卡 JSON，不绑定端口",
    )
    return parser


def startup_card(args: argparse.Namespace, app: GatewayApp) -> dict:
    reachable, detail = app.probe()
    return {
        "schema": STARTUP_SCHEMA,
        "ok": True,
        "version": GATEWAY_VERSION,
        "host": args.host,
        "port": args.port,
        "routes": list(ROUTES),
        "methods": list(ALLOWED_METHODS),
        "write_verbs": [],
        "auth": {
            "header": TOKEN_HEADER,
            "key_env": args.admin_key_env,
            "derivation": "sha256(admin_key) hex",
            "comparison": "hmac.compare_digest",
        },
        "backend": {"kind": args.backend, "reachable": reachable, "detail": detail},
        "note": "只读网关：进程内不含任何写动词（RT-044 两进程宪法）",
        "at": iso(utc_now()),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    backend = None
    server = None
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        token = token_from_env(args.admin_key_env)
        backend = build_backend(args.backend, root=parse_root(args.root), prefix=args.prefix)
        app = GatewayApp(backend, token, backend_kind=args.backend)
        card = startup_card(args, app)
        sys.stdout.write(dumps(card).decode("utf-8"))
        sys.stdout.flush()
        if args.check:
            return 0
        server = make_server(app, args.host, args.port)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        # JSON on stdout so the caller can parse the failure the same way it
        # parses success (RT-044 J5); a human line on stderr as well.
        sys.stdout.write(
            dumps(error_payload(type(exc).__name__, str(exc))).decode("utf-8")
        )
        print(f"网关启动失败：{exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:  # pragma: no cover - only on shutdown
            server.server_close()
        close_backend(backend)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
