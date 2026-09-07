#!/usr/bin/env python3
"""RT-044: the read-only HTTP query gateway for a KB (查询网关 v1).

Usage::

    export CWK_KB_ADMIN_KEY=...            # never on the command line
    python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY \\
        --backend local --root /path/to/kb --port 8787
    python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY \\
        --backend nas --prefix libraries/工作库 --check
    python3 scripts/kb_gateway.py --admin-key-env CWK_KB_ADMIN_KEY \\
        --backend nas --prefix cwork-3m --kb docdb-touqian --kb spbp-2027 \\
        --check

Multi-library mode (RT-049).  ``--kb <prefix>`` (repeatable, NAS backend)
mounts extra libraries behind one process; ``/query`` and ``/citation``
take ``?kb=<kb_id>`` and the binding-token scope check answers for the
*target* library.  ``--prefix`` stays the default library when ``kb`` is
omitted, and single-library mode behaves exactly as before.

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

Binding tokens (RT-047 P2).  ``--tokens-file`` additionally admits per-agent
bearer tokens issued by ``scripts/kb_token.py``.  The order is fixed: the
platform admin token first — byte-identical to the paragraph above, so the
operator face is unchanged — and only on a miss does the binding registry get
consulted.  A registry hit is read-only access to *this* library and nothing
else; a token whose ``kb_ids`` does not list this gateway's ``--prefix`` is a
403 rather than a 401, because "we know you, this is not yours" and "we do not
know you" are different facts.  Without ``--tokens-file`` the process behaves
exactly as it did before: admin-only.

The registry is re-read on every lookup (:class:`kb_token.TokenFile`), which
is what makes revocation take effect immediately rather than at the next
restart, and an unreadable registry refuses everyone instead of falling open.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import socketserver
import sys
import urllib.parse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

# Read-side imports only.  ``kb_ledger`` and ``kb_token`` also hold write
# primitives; the names bound here are the reading half of each and nothing
# else — ``tests/test_kb_gateway.py`` parses this file to keep it that way.
from kb_ledger import dumps, iso, read_json, utc_now  # noqa: E402
from kb_lexical import (  # noqa: E402  纯函数层，读面安全
    CANDIDATE_K,
    ELIGIBLE_STATUSES,
    LEXICAL_INDEX_REL,
    best_spans,
    bm25_rank,
    corpus_digest,
    eligible_rows,
    from_json_payload,
    rrf,
)
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
from kb_token import TokenDecision, TokenError, TokenFile  # noqa: E402

GATEWAY_VERSION = "1.3.0"
GATEWAY_SCHEMA = "cwk.kb.gateway.v1"
HEALTH_SCHEMA = "cwk.kb.gateway.health.v1"
QUERY_SCHEMA = "cwk.kb.gateway.query.v1"
CITATION_SCHEMA = "cwk.kb.gateway.citation.v1"
STARTUP_SCHEMA = "cwk.kb.gateway.startup.v1"
ERROR_SCHEMA = "cwk.kb.gateway.error.v1"

RAW_INDEX_REL = "_system/raw-index.json"
LEXICAL_READINESS_REL = "_system/lexical-readiness.json"

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

# ── RT-051 P1a: v2 controlled-read face ─────────────────────────────────────
#
# 设计来源 RT/RT-051/rt-lite.md（方案权威）。本层是「基础受控访问」的第一个
# 增量（P1a）：capabilities / list / search(metadata) / resolve / inspect /
# read / continue / renew，全部 GET、全部复用 v1 的 admin+binding 鉴权与
# RT-049 挂载面，不新增第二套权限。
#
# P1a 的诚实边界（写在注释里当合同）：
# - 每次读取像 /citation 一样全件读回并复核全 SHA：full_sha_verified 只有
#   「本次实读字节 == 索引记录」时为 true；字节与索引不符 → 409。传输级有界
#   读取与 OPS 快照 builder 是 P1b，本层不冒充。
# - document_ref / cursor 是进程内 HMAC 签名的不透明句柄（C06 红线：网关零
#   落盘，内存签发句柄明文允许）。进程重启即失效，客户端重新 resolve。
# - 词法融合（lexical_fusion_v1）是 P3：显式请求得 503 lexical_unavailable，
#   capabilities 如实报 lexical_modes=[]。

V2_PREFIX = "/v2/kb/"
V2_OPERATIONS: Tuple[str, ...] = (
    "libraries",
    "capabilities",
    "list",
    "search",
    "resolve",
    "inspect",
    "read",
    "continue",
    "renew",
)
CAPABILITIES_SCHEMA = "cwk.kb.capabilities.v2"
DOCUMENTS_SCHEMA = "cwk.kb.documents.v2"
DOCUMENT_SCHEMA = "cwk.kb.document.v2"
SEARCH_SCHEMA = "cwk.kb.search.v2"
READ_SCHEMA = "cwk.kb.read.v2"
ERROR_V2_SCHEMA = "cwk.kb.error.v2"

V2_VIEW = "raw_utf8"
V2_PAGE_DEFAULT = 50
V2_PAGE_MAX = 200
V2_MAX_BYTES_DEFAULT = 16384
V2_MAX_BYTES_MIN = 4
V2_MAX_BYTES_MAX = 65536
V2_Q_MAX_CODEPOINTS = 256
V2_REF_TTL_SECONDS = 900
V2_RENEW_GRACE_SECONDS = 600
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

#: 每个操作允许的查询参数白名单：v2 不静默忽略未知参数（C02）。
_V2_OP_PARAMS: Dict[str, Tuple[str, ...]] = {
    "libraries": (),
    "capabilities": ("kb",),
    "list": ("kb", "page_size", "cursor"),
    "search": (
        "kb", "q", "page_size", "cursor", "retrieval_mode", "allow_degraded",
    ),
    "resolve": ("kb", "lineage", "version"),
    "inspect": ("kb", "document_ref"),
    "read": (
        "kb", "document_ref", "max_bytes", "start_byte", "end_byte",
        "line_start", "line_end", "cursor",
    ),
    "continue": ("kb", "document_ref", "cursor"),
    "renew": ("kb", "document_ref"),
}


class V2Error(Exception):
    """A v2 request error carrying its HTTP status and machine code."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


def v2_error_payload(code: str, message: str, *, retryable: bool = False) -> dict:
    return {
        "schema": ERROR_V2_SCHEMA,
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
        "request_id": os.urandom(8).hex(),
    }


def _v2_int(
    clean: Mapping[str, str],
    name: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    default: Optional[int] = None,
) -> Optional[int]:
    raw = clean.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise V2Error(400, "bad_request", f"{name} 必须是整数：{raw!r}") from None
    if minimum is not None and value < minimum:
        raise V2Error(400, "bad_request", f"{name} 不能小于 {minimum}：{value}")
    if maximum is not None and value > maximum:
        raise V2Error(400, "bad_request", f"{name} 不能大于 {maximum}：{value}")
    return value


def _utf8_forward(data: bytes, end: int) -> int:
    """把页尾向前推到合法 UTF-8 码点边界（最多 3 字节）。

    C03「向前取合法边界，非空未到末尾每页必须推进」——max_bytes 是上限不是
    配额，宁可单页略超也不把码点截成替换字符。start 侧不回退：把起始点往回
    挪会重发字节、往前跳会丢字节，都破坏覆盖对账。
    """
    step = 0
    while end < len(data) and (data[end] & 0xC0) == 0x80 and step < 3:
        end += 1
        step += 1
    return end


def _v2_line_starts(data: bytes) -> List[int]:
    """LF 分行的行首字节偏移（1-based 行号由此而来）；空文 0 行（C03）。"""
    if not data:
        return []
    starts = [0]
    for i, b in enumerate(data):
        if b == 0x0A:
            starts.append(i + 1)
    return starts


def _v2_index_digest(index: Mapping[str, "IndexEntry"]) -> str:
    """list/search 游标的元数据快照摘要（C03：换快照必须 409，不能混新页）。"""
    h = hashlib.sha256()
    for lineage in sorted(index):
        e = index[lineage]
        row = "|".join(
            (lineage, e.path, e.title, str(e.version), e.sha256, e.status)
        )
        h.update(row.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


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
        tokens: Optional[TokenFile] = None,
        kb_id: str = "",
        kb_mounts: Optional[Mapping[str, StorageBackend]] = None,
    ) -> None:
        self.backend = backend
        self.token = token
        self.backend_kind = backend_kind
        self.version = version
        self.clock = clock
        #: The binding registry, or ``None`` for admin-only (the v1 behaviour).
        self.tokens = tokens
        #: This gateway's library identity, matched against ``token.kb_ids``.
        self.kb_id = kb_id
        #: RT-049 mount table: kb_id -> backend.  The primary library is
        #: always mounted under its own kb_id; ``kb_mounts`` carries the
        #: ``--kb`` extras.  Nothing outside this table is reachable via
        #: ``?kb=``.  Empty table = single-library mode: ``kb`` omitted
        #: falls back to the primary backend (the v1 behaviour).
        self.mounts: Dict[str, StorageBackend] = dict(kb_mounts or {})
        if kb_id and kb_id not in self.mounts:
            self.mounts[kb_id] = backend
        #: RT-051 P1a: v2 句柄的进程内签名密钥。重启即轮换——旧 ref/cursor
        #: 全部失效，客户端重新 resolve，这不是事故是设计（C06 零落盘）。
        self._v2_key = os.urandom(32)
        #: RT-051 P3b: 已解析词法代的进程内缓存（kb → (generation, payload)）。
        #: 零落盘红线允许内存缓存；换代/过时白 _v2_load_lexical 的投影复核拒掉。
        self._v2_lexical: Dict[str, Tuple[str, dict]] = {}

    # -- auth ---------------------------------------------------------------

    def authorized(self, headers: Mapping[str, str]) -> bool:
        """Does the caller hold the platform admin token?

        Unchanged from v1 and deliberately narrow: binding tokens are *not*
        admin, so anything that asks this question keeps meaning "operator".
        """
        return tokens_match(header_value(headers, TOKEN_HEADER), self.token)

    def authorize(self, headers: Mapping[str, str], kb_id: str = "") -> Optional[Response]:
        """``None`` when the caller may read; otherwise the refusal to send.

        Admin first, binding registry second.  The admin comparison runs
        unconditionally so enabling ``--tokens-file`` cannot change what the
        operator's own token does, and the registry is only touched on a miss
        — no file read on the hot path for the admin face.
        """
        presented = header_value(headers, TOKEN_HEADER)
        if tokens_match(presented, self.token):
            return None
        if self.tokens is None:
            return self.unauthorized("admin_token_mismatch")

        # RT-049: the scope check answers for the *target* library — the
        # resolved ``?kb=`` value, or the primary kb_id when omitted.
        decision: TokenDecision = self.tokens.decide(
            presented, kb_id=kb_id or self.kb_id, now=self.clock()
        )
        if decision.ok:
            return None
        if decision.forbidden:
            # A known holder asking for someone else's library.  The message
            # carries the fingerprint (which the caller could compute from the
            # token it already holds) and not this gateway's identity.
            return Response(
                403,
                error_payload(
                    "forbidden",
                    "该 token 有效，但授权面（kb_ids）不含本库——一支 token 只开一个库",
                    auth={"header": TOKEN_HEADER, "reason": decision.reason},
                    token_id=decision.token_id,
                ),
            )
        return self.unauthorized(decision.reason)

    def unauthorized(self, reason: str) -> Response:
        """The 401 body.  Same shape with or without ``--tokens-file``."""
        modes = ["admin"] + (["binding"] if self.tokens is not None else [])
        return Response(
            401,
            error_payload(
                "unauthorized",
                f"{TOKEN_HEADER} 缺失或不匹配",
                auth={
                    "header": TOKEN_HEADER,
                    "derivation": "sha256(admin_key) hex",
                    "modes": modes,
                    "reason": reason,
                },
            ),
        )

    # -- routes -------------------------------------------------------------

    def dispatch(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        """Method gate → health → auth gate → route table → 404.

        The order is the contract.  The method gate runs first so a write
        attempt is refused as a write (405) whether or not the caller has a
        token.  The auth gate runs before the route table so an unknown path
        answers 401 to an unauthenticated caller: probing for a management
        endpoint must not be cheaper than authenticating.  A binding token
        (RT-047 P2) enters at the same gate and reaches the same three routes
        — there is no second, wider surface behind it.
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

        # RT-052: this targetless operation has its own auth question.  It
        # must not be routed through the default kb (which would reject a
        # token that is valid for an attached library only).
        if path == V2_PREFIX + "libraries":
            refusal, scope = self.authorize_libraries(headers)
            if refusal is not None:
                return Response(refusal.status, v2_error_payload("unauthorized", "认证失败"))
            return self.v2_dispatch("libraries", params, libraries_scope=scope)

        # RT-049: ?kb= selects the target library for the two data routes.
        # Resolved before the auth gate so a binding token's scope is judged
        # against the library actually being asked about.
        target_kb = (params.get("kb") or [""])[0].strip() or self.kb_id

        refusal = self.authorize(headers, kb_id=target_kb)
        if refusal is not None:
            if path.startswith(V2_PREFIX):
                err = refusal.payload.get("error", {})
                return Response(
                    refusal.status,
                    v2_error_payload(
                        str(err.get("kind") or "unauthorized"),
                        str(err.get("message") or ""),
                    ),
                )
            return refusal

        if path.startswith(V2_PREFIX):
            return self.v2_dispatch(path[len(V2_PREFIX) :], params)

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

    def _backend_for_kb(self, kb: str) -> Optional[StorageBackend]:
        """RT-049: the backend serving ``kb``, or ``None`` when unknown.

        An omitted ``kb`` resolves to the primary backend — exactly the v1
        behaviour, in single- and multi-library mode alike.  An explicit
        ``kb`` must name a mounted library: anything else is a 404 rather
        than a silent fallthrough to the primary, because "asked for one
        library, answered from another" is precisely the cross-library leak
        this router exists to prevent.
        """
        if not kb:
            return self.backend
        return self.mounts.get(kb)

    def query(self, params: Mapping[str, List[str]]) -> Response:
        q = (params.get("q") or [""])[0]
        limit = (params.get("limit") or [str(DEFAULT_LIMIT)])[0]
        backend = self._backend_for_kb((params.get("kb") or [""])[0].strip())
        if backend is None:
            return Response(
                404,
                error_payload(
                    "unknown_kb",
                    "kb 参数不在本网关的挂载面内",
                ),
            )
        try:
            result = query_index(backend, q, limit=clamp_limit(limit))
        except BackendUnavailable as exc:
            return Response(503, error_payload("backend_unavailable", str(exc)))
        except GatewayError as exc:
            return Response(400, error_payload("bad_request", str(exc)))
        payload = {"schema": QUERY_SCHEMA, "ok": True, "at": iso(self.clock())}
        kb = (params.get("kb") or [""])[0].strip() or self.kb_id
        if kb:
            payload["kb"] = kb
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
        backend = self._backend_for_kb((params.get("kb") or [""])[0].strip())
        if backend is None:
            return Response(
                404,
                error_payload(
                    "unknown_kb",
                    "kb 参数不在本网关的挂载面内",
                ),
            )
        try:
            payload = build_citation(backend, lineage, version, now=self.clock())
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

    # ── RT-051 P1a: /v2/kb/* controlled-read face ───────────────────────────

    def authorize_libraries(
        self, headers: Mapping[str, str]
    ) -> Tuple[Optional[Response], set]:
        """Targetless discovery authorization: admin first, one registry scan.

        The authorized set is returned as request-local data.  It must never
        be stored on ``self``: one GatewayApp serves concurrent requests, so
        shared auth state could leak one holder's library set to another.
        """
        presented = header_value(headers, TOKEN_HEADER)
        if tokens_match(presented, self.token):
            return None, set(self.mounts)
        if self.tokens is None:
            return self.unauthorized("registry_unavailable"), set()
        decision = self.tokens.decide_scope(presented, now=self.clock())
        if not decision.ok:
            # For a targetless request a valid but empty scope is not forbidden.
            return self.unauthorized(decision.reason), set()
        return None, set(decision.scope).intersection(self.mounts)

    def v2_dispatch(
        self,
        op: str,
        params: Mapping[str, List[str]],
        *,
        libraries_scope: Optional[set] = None,
    ) -> Response:
        """Whitelist → mount → handler, with v2-shaped errors throughout."""
        if op not in V2_OPERATIONS:
            return Response(404, v2_error_payload("not_found", f"未知操作 {op!r}"))
        try:
            clean = self._v2_clean_params(op, params)
            if op == "libraries":
                return Response(
                    200,
                    self._v2_libraries(libraries_scope or set()),
                    {"Cache-Control": "no-store"},
                )
            backend = self._backend_for_kb(clean["kb"])
            if backend is None:
                raise V2Error(404, "unknown_kb", "kb 参数不在本网关的挂载面内")
            return Response(200, getattr(self, f"_v2_{op}")(clean, backend, clean["kb"]))
        except V2Error as exc:
            return Response(exc.status, v2_error_payload(exc.code, exc.message, retryable=exc.retryable))
        except BackendUnavailable as exc:
            return Response(
                503, v2_error_payload("source_unavailable", str(exc), retryable=True)
            )

    def _v2_libraries(self, allowed: set) -> dict:
        """Small, authorization-filtered discovery projection; never loads lexical index."""
        rows = []
        complete = True
        for kb in sorted(allowed):
            backend = self.mounts[kb]
            try:
                index = load_index(backend)
                readable = sum(1 for entry in index.values() if self._v2_readable(entry)[0])
                display_name, source = kb, "kb_id"
                try:
                    display_name = str(read_json(backend, "kb.json").get("display_name") or kb)
                    source = "kb.json"
                except (NotFound, StorageError, ValueError, KeyError):
                    pass
                lexical_status = self._lexical_readiness_status(backend, index)
                rows.append({"kb_id": kb, "display_name": display_name, "display_source": source,
                             "total": len(index), "readable_total": readable,
                             "lexical_status": lexical_status, "error": None})
            except (BackendUnavailable, StorageError, ValueError, KeyError, OSError):
                complete = False
                rows.append({"kb_id": kb, "display_name": kb, "display_source": "kb_id",
                             "total": None, "readable_total": None, "lexical_status": None,
                             "error": "source_unavailable"})
        return {"schema": "cwk.kb.libraries.v2", "ok": True, "gateway_version": self.version,
                "complete": complete, "libraries": rows, "at": iso(self.clock())}

    def _lexical_readiness_status(self, backend, index: Mapping[str, "IndexEntry"]) -> str:
        try:
            readiness = read_json(backend, LEXICAL_READINESS_REL)
        except NotFound:
            return "unknown"
        except (StorageError, ValueError, KeyError):
            return "corrupt"
        required = ("generation", "corpus_digest", "engine", "coverage_complete", "excluded_counts")
        if not all(key in readiness for key in required):
            return "corrupt"
        rows = eligible_rows((lineage, entry.version, entry.sha256, entry.status)
                             for lineage, entry in index.items())
        if str(readiness.get("corpus_digest")) != corpus_digest(rows):
            return "stale"
        return "ready"

    @staticmethod
    def _v2_clean_params(op: str, params: Mapping[str, List[str]]) -> Dict[str, str]:
        allowed = _V2_OP_PARAMS[op]
        clean: Dict[str, str] = {}
        for key, values in params.items():
            if key not in allowed:
                raise V2Error(400, "bad_request", f"操作 {op} 不接受参数 {key!r}")
            if len(values) > 1:
                raise V2Error(400, "bad_request", f"参数 {key!r} 重复出现")
            clean[key] = values[0]
        if op == "libraries":
            return clean
        kb = clean.get("kb", "").strip()
        if not kb:
            raise V2Error(400, "bad_request", "v2 调用必须显式携带 kb 参数")
        clean["kb"] = kb
        return clean

    def _v2_now_ts(self) -> float:
        now = self.clock()
        if isinstance(now, datetime):
            return now.timestamp()
        return float(now)  # pragma: no cover - clock injection convenience

    def _v2_sign(self, payload: dict) -> str:
        raw = dumps(payload)
        sig = hmac.new(self._v2_key, raw, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(raw + sig).decode("ascii").rstrip("=")

    def _v2_open(self, handle: str, *, kind: str, grace_seconds: int = 0) -> dict:
        """Verify + decode an opaque handle.  Restart rotates the key, so a
        handle from a previous process simply fails verification — the client
        re-resolves, which is the documented recovery, not an error state."""
        if not handle:
            raise V2Error(400, "bad_request", "缺少句柄")
        try:
            padded = handle + "=" * (-len(handle) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError):
            raise V2Error(400, "bad_request", "句柄不是合法 base64") from None
        if len(blob) < 17:
            raise V2Error(400, "bad_request", "句柄过短")
        raw, sig = blob[:-16], blob[-16:]
        expect = hmac.new(self._v2_key, raw, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(sig, expect):
            raise V2Error(400, "bad_request", "句柄签名不符")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise V2Error(400, "bad_request", "句柄载荷不是 JSON") from None
        if not isinstance(payload, dict) or payload.get("k") != kind:
            cursor_kinds = {"ref": ("read", "list", "search"),
                            "read": ("ref", "list", "search"),
                            "list": ("ref", "read", "search"),
                            "search": ("ref", "read", "list")}
            swapped = isinstance(payload, dict) and payload.get("k") in cursor_kinds.get(kind, ())
            raise V2Error(
                400, "invalid_cursor" if swapped else "bad_request", "句柄类型与操作不符"
            ) from None
        if self._v2_now_ts() > float(payload.get("e") or 0) + grace_seconds:
            code = "reference_expired" if kind == "ref" else "cursor_expired"
            raise V2Error(410, code, "句柄已过期——重新 resolve/list 后重试")
        return payload

    @staticmethod
    def _v2_check_handle_kb(payload: dict, kb: str, *, code: str = "bad_request") -> None:
        if str(payload.get("kb") or "") != kb:
            raise V2Error(400, code, "句柄不属于该库")

    @staticmethod
    def _v2_identity(kb: str, lineage: str, entry: IndexEntry) -> dict:
        return {
            "kb": kb,
            "lineage_id": lineage,
            "source_version": entry.version,
            "artifact_id": "primary",
            "raw_sha256": entry.sha256,
        }

    def _v2_issue_ref(self, kb: str, lineage: str, entry: IndexEntry) -> Tuple[str, str]:
        exp = self._v2_now_ts() + V2_REF_TTL_SECONDS
        # n: 每次签发唯一。renew 的语义是「重签一份新句柄」，不是返回同一串；
        # 没有随机数时固定时钟下两次签发会逐字相同，renew 就成了空操作。
        ref = self._v2_sign(
            {"k": "ref", "kb": kb, "l": lineage, "v": entry.version,
             "h": entry.sha256, "n": os.urandom(6).hex(), "e": exp}
        )
        return ref, datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()

    @staticmethod
    def _v2_readable(entry: IndexEntry) -> Tuple[bool, Optional[str]]:
        """Index-status view of readability.  The byte-level truth is checked
        at read time (409/422/503 there); this field keeps list/resolve cheap
        on NAS-sized indexes."""
        if entry.status in ("ok", "converted"):
            return True, None
        if entry.status == "placeholder":
            return True, "placeholder"
        if not entry.path:
            return False, "content_unavailable"
        return False, entry.status or "unknown"

    def _v2_document_row(self, kb: str, lineage: str, entry: IndexEntry) -> dict:
        readable, reason = self._v2_readable(entry)
        ref, _ = self._v2_issue_ref(kb, lineage, entry)
        title = entry.title or None
        return {
            "lineage_id": lineage,
            "title": title,
            "title_source": "index" if title else "missing",
            "display_label": title or lineage,
            "source_version": entry.version,
            "parse_status": entry.status,
            "readable": readable,
            "reason": reason,
            "document_ref": ref,
        }

    def _v2_current_bytes(
        self, backend: StorageBackend, lineage: str, ref_payload: dict
    ) -> Tuple[IndexEntry, bytes]:
        """Reload the index and fetch the bytes, refusing every drift.

        P1a honesty note: this reads the whole object per request (same as
        /citation) and verifies the full SHA — the transport-level bounded
        read over an OPS-built snapshot is P1b.  ``full_sha_verified`` in the
        read response is true precisely because of this check, not despite it.
        """
        index = load_index(backend)
        entry = index.get(lineage)
        if entry is None:
            raise V2Error(404, "document_unavailable", "lineage 已不在当前索引里")
        if entry.version != int(ref_payload.get("v") or -1) or entry.sha256 != str(
            ref_payload.get("h") or ""
        ):
            raise V2Error(409, "stale_reference", "索引已前进（版本或 SHA 变化）——重新 resolve")
        if not entry.path:
            raise V2Error(422, "content_unavailable", "该条目无 artifact（status=failed）")
        try:
            data = backend.read(entry.path)
        except NotFound:
            raise V2Error(503, "evidence_unavailable", "索引指向的原件缺失") from None
        except (StorageError, OSError) as exc:
            raise V2Error(503, "source_unavailable", f"读取原件失败：{exc}") from None
        if sha256_bytes(data) != entry.sha256:
            raise V2Error(409, "stale_reference", "当前字节与索引记录的 SHA 不符——重新 resolve 拿新身份")
        return entry, data

    # -- operations ----------------------------------------------------------

    def _v2_capabilities(self, clean, backend, kb) -> dict:
        return {
            "schema": CAPABILITIES_SCHEMA,
            "ok": True,
            "kb": kb,
            "version": self.version,
            "access_contract": {
                "token_header": TOKEN_HEADER,
                "modes": ["admin"] + (["binding"] if self.tokens is not None else []),
            },
            "supported_operations": list(V2_OPERATIONS),
            "limits": {
                "page_size_default": V2_PAGE_DEFAULT,
                "page_size_max": V2_PAGE_MAX,
                "max_bytes_default": V2_MAX_BYTES_DEFAULT,
                "max_bytes_min": V2_MAX_BYTES_MIN,
                "max_bytes_max": V2_MAX_BYTES_MAX,
                "q_max_codepoints": V2_Q_MAX_CODEPOINTS,
            },
            "offset_unit": "byte_0based_halfopen",
            "prepare_transport": {"supported": False},
            "lexical_modes": ["lexical_fusion_v1"],
            "at": iso(self.clock()),
        }

    def _v2_list(self, clean, backend, kb) -> dict:
        page_size = _v2_int(
            clean, "page_size", minimum=1, maximum=V2_PAGE_MAX, default=V2_PAGE_DEFAULT
        )
        index = load_index(backend)
        digest = _v2_index_digest(index)
        keys = sorted(index)
        offset = 0
        if clean.get("cursor"):
            cur = self._v2_open(clean["cursor"], kind="list")
            self._v2_check_handle_kb(cur, kb, code="invalid_cursor")
            if cur.get("d") != digest:
                raise V2Error(409, "metadata_changed", "索引已变化——丢弃本页并重新 list")
            if int(cur.get("n") or 0) != page_size:
                raise V2Error(400, "invalid_cursor", "page_size 与游标不一致")
            offset = int(cur.get("a") or 0)
        page = keys[offset : offset + page_size]
        payload: dict = {
            "schema": DOCUMENTS_SCHEMA,
            "ok": True,
            "kb": kb,
            "total": len(keys),
            "returned": len(page),
            "items": [self._v2_document_row(kb, k, index[k]) for k in page],
            "metadata_snapshot": digest,
            "eof": offset + page_size >= len(keys),
            "next_cursor": None,
            "at": iso(self.clock()),
        }
        if offset + page_size < len(keys):
            payload["next_cursor"] = self._v2_sign(
                {"k": "list", "kb": kb, "d": digest, "n": page_size,
                 "a": offset + page_size, "e": self._v2_now_ts() + V2_REF_TTL_SECONDS}
            )
        return payload

    def _v2_search(self, clean, backend, kb) -> dict:
        q = clean.get("q", "")
        if len(q) > V2_Q_MAX_CODEPOINTS:
            raise V2Error(400, "bad_request", f"q 超过 {V2_Q_MAX_CODEPOINTS} code points")
        needle = q.strip().lower()
        if not needle:
            raise V2Error(400, "bad_request", "查询词 q 不能为空")
        mode = clean.get("retrieval_mode") or "metadata"
        if mode not in ("metadata", "lexical_fusion_v1"):
            raise V2Error(400, "bad_request", f"未知 retrieval_mode {mode!r}")
        degraded_to = clean.get("allow_degraded") or ""
        if degraded_to not in ("", "metadata"):
            raise V2Error(400, "bad_request", f"allow_degraded 仅接受 metadata：{degraded_to!r}")
        page_size = _v2_int(
            clean, "page_size", minimum=1, maximum=V2_PAGE_MAX, default=V2_PAGE_DEFAULT
        )
        if mode == "lexical_fusion_v1" and clean.get("cursor"):
            raise V2Error(400, "bad_request", "lexical Top-K 不支持分页游标")
        index = load_index(backend)
        digest = _v2_index_digest(index)
        keys = [k for k in sorted(index) if needle in index[k].haystack()]
        offset = 0
        if clean.get("cursor"):
            cur = self._v2_open(clean["cursor"], kind="search")
            self._v2_check_handle_kb(cur, kb, code="invalid_cursor")
            if cur.get("d") != digest:
                raise V2Error(409, "metadata_changed", "索引已变化——丢弃本页并重新 search")
            if cur.get("q") != needle or int(cur.get("n") or 0) != page_size:
                raise V2Error(400, "invalid_cursor", "q 或 page_size 与游标不一致")
            offset = int(cur.get("a") or 0)
        page = keys[offset : offset + page_size]
        if mode == "lexical_fusion_v1":
            lex = self._v2_load_lexical(backend, kb)
            if lex is None and degraded_to != "metadata":
                raise V2Error(
                    503,
                    "lexical_unavailable",
                    "词法代未发布或已过时（missing/stale/corrupt）——先跑 kb_lexical_builder"
                    " 后重试；如需显式降级传 allow_degraded=metadata",
                    retryable=True,
                )
            if lex is not None:
                return self._v2_fusion_payload(kb, q, needle, page_size, index, digest, lex)
        degraded = mode == "lexical_fusion_v1"
        # C02：metadata 搜索回 documents.v2；cwk.kb.search.v2 只属于融合路
        payload: dict = {
            "schema": DOCUMENTS_SCHEMA,
            "ok": True,
            "kb": kb,
            "q": q,
            "query_kind": "metadata",
            "requested_mode": mode,
            "effective_mode": "metadata",
            "degraded": degraded,
            "reason": ("lexical generation 未发布或已过时，显式降级到 metadata" if degraded else None),
            "total": len(keys),
            "returned": len(page),
            "items": [self._v2_document_row(kb, k, index[k]) for k in page],
            "metadata_snapshot": digest,
            "eof": offset + page_size >= len(keys),
            "next_cursor": None,
            "at": iso(self.clock()),
        }
        if offset + page_size < len(keys):
            payload["next_cursor"] = self._v2_sign(
                {"k": "search", "kb": kb, "d": digest, "q": needle, "n": page_size,
                 "a": offset + page_size, "e": self._v2_now_ts() + V2_REF_TTL_SECONDS}
            )
        return payload

    def _v2_load_lexical(self, backend, kb) -> Optional[dict]:
        """读已发布词法代；missing/stale/corrupt 一律 None（C07）。

        过时代不得返回旧候选：这里用当前 raw-index 的资格域投影重算
        corpus_digest，与发布代不一致就拒绝——宁可 503 让人重建，不拿
        旧代候选冒充当前真相。
        """
        try:
            payload = read_json(backend, LEXICAL_INDEX_REL)
        except NotFound:
            return None
        except (StorageError, ValueError):
            return None
        gen = str(payload.get("generation") or "")
        if not gen:
            return None
        # 每次调用都重算资格域投影并复核 corpus_digest——陈旧代哪怕进程内
        # 缓存命中也不得返回旧候选；缓存只省 index 反序列化，不省一致性
        # 检查（否则源升版而 builder 未追上时，旧代会在同进程内继续冒充）。
        try:
            entries = load_index(backend)
            rows = eligible_rows(
                (lineage, e.version, e.sha256, e.status)
                for lineage, e in entries.items()
            )
        except BackendUnavailable:
            return None
        if corpus_digest(rows) != str(payload.get("corpus_digest") or ""):
            return None
        cached = self._v2_lexical.get(kb)
        if cached is not None and cached[0] == gen:
            return cached[1]
        try:
            payload["__index__"] = from_json_payload(payload.get("index") or {})
        except (TypeError, ValueError, KeyError):
            return None
        self._v2_lexical[kb] = (gen, payload)
        return payload

    def _v2_fusion_payload(self, kb, q, needle, page_size, index, digest, lex) -> dict:
        """cwk.kb.search.v2 融合响应（C07）：body BM25 + metadata 子串 RRF。

        两路同资格域（当前合格件）；每路 candidate_k 封顶；rank 从 1 起；
        RRF=1/(60+rank) 缺路 0；hit 带 document_ref 与候选 span 的**raw 绝对
        字节坐标**——客户端拿它走同一条 read 路出引文，不另立证据路径。
        """
        eligible = {
            lineage
            for lineage, entry in index.items()
            if entry.status in ELIGIBLE_STATUSES and entry.sha256
        }
        meta_all = [
            k for k in sorted(index) if k in eligible and needle in index[k].haystack()
        ]
        lex_index = lex["__index__"]
        body_all = bm25_rank(lex_index, q)
        relation = (
            "lower_bound"
            if len(meta_all) > CANDIDATE_K or len(body_all) > CANDIDATE_K
            else "exact"
        )
        meta_rank = {lin: i + 1 for i, lin in enumerate(meta_all[:CANDIDATE_K])}
        body_rank = {lin: i + 1 for i, (lin, _s) in enumerate(body_all[:CANDIDATE_K])}
        fused = sorted(
            (
                (lin, rrf(body_rank.get(lin, 0), meta_rank.get(lin)))
                for lin in set(meta_rank) | set(body_rank)
            ),
            key=lambda t: (-t[1], t[0]),
        )
        hits: List[dict] = []
        for lineage, score in fused[:page_size]:
            row = self._v2_document_row(kb, lineage, index[lineage])
            row.update(
                {
                    "body_rank": body_rank.get(lineage),
                    "metadata_rank": meta_rank.get(lineage),
                    "rrf_score": score,
                    "candidate_spans": [
                        {
                            "chunk_id": cid,
                            "start_byte": lex_index.chunk_bytes.get(cid, (0, 0))[0],
                            "end_byte": lex_index.chunk_bytes.get(cid, (0, 0))[1],
                            "score": sc,
                        }
                        for cid, _s, _e, sc in best_spans(lex_index, lineage, q)
                    ],
                }
            )
            hits.append(row)
        return {
            "schema": SEARCH_SCHEMA,
            "ok": True,
            "kb": kb,
            "q": q,
            "query_kind": "fusion",
            "requested_mode": "lexical_fusion_v1",
            "effective_mode": "lexical_fusion_v1",
            "degraded": False,
            "reason": None,
            "generation": lex.get("generation"),
            "engine": lex.get("engine"),
            "coverage_complete": bool(lex.get("coverage_complete")),
            "excluded_counts": lex.get("excluded_counts") or {},
            "matched_relation": relation,
            "candidate_truncated": len(fused) > page_size,
            "score_kind": "rrf_rank_v1",
            "total": len(fused),
            "returned": len(hits),
            "items": hits,
            "metadata_snapshot": digest,
            "eof": True,
            "next_cursor": None,
            "at": iso(self.clock()),
        }

    def _v2_resolve(self, clean, backend, kb) -> dict:
        lineage = clean.get("lineage", "").strip()
        if not lineage:
            raise V2Error(400, "bad_request", "缺少 lineage 参数")
        version = _v2_int(clean, "version", minimum=1)
        index = load_index(backend)
        entry = index.get(lineage)
        if entry is None:
            raise V2Error(404, "not_found", f"lineage {lineage!r} 不在索引里")
        if version is not None and version != entry.version:
            raise V2Error(
                409, "stale_reference",
                f"仅当前版可读（当前 v{entry.version}）；历史版读取不在本版合同内",
            )
        ref, expires_at = self._v2_issue_ref(kb, lineage, entry)
        readable, reason = self._v2_readable(entry)
        return {
            "schema": DOCUMENT_SCHEMA,
            "ok": True,
            "kb": kb,
            "identity": self._v2_identity(kb, lineage, entry),
            "document_ref": ref,
            "expires_at": expires_at,
            "readable": readable,
            "reason": reason,
            "parse_status": entry.status,
            "artifact_kind": entry.artifact_kind,
            "size_bytes": None,  # P1a: resolve 不读字节；inspect 才读
            "at": iso(self.clock()),
        }

    def _v2_inspect(self, clean, backend, kb) -> dict:
        ref_payload = self._v2_open(clean.get("document_ref", ""), kind="ref")
        self._v2_check_handle_kb(ref_payload, kb)
        lineage = str(ref_payload.get("l") or "")
        index = load_index(backend)
        entry = index.get(lineage)
        if entry is None:
            raise V2Error(404, "document_unavailable", "lineage 已不在当前索引里")
        if entry.version != int(ref_payload.get("v") or -1) or entry.sha256 != str(
            ref_payload.get("h") or ""
        ):
            raise V2Error(409, "stale_reference", "索引已前进（版本或 SHA 变化）——重新 resolve")
        data: Optional[bytes]
        try:
            data = backend.read(entry.path) if entry.path else None
        except NotFound:
            data = None
        full_sha = sha256_bytes(data) if data is not None else None
        ready = data is not None and full_sha == entry.sha256
        encoding: Optional[str] = None
        if data is not None:
            try:
                data.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoding = None
        return {
            "schema": DOCUMENT_SCHEMA,
            "ok": True,
            "kb": kb,
            "identity": self._v2_identity(kb, lineage, entry),
            "document_ref": clean.get("document_ref", ""),
            "readable": ready,
            "reason": None if ready else ("content_unavailable" if data is None else "stale"),
            "parse_status": entry.status,
            "artifact_kind": entry.artifact_kind,
            "title": entry.title or None,
            "title_source": "index" if entry.title else "missing",
            "display_label": entry.title or lineage,
            "size_bytes": len(data) if data is not None else None,
            "encoding": encoding,
            "view": V2_VIEW,
            "read_state": "ready" if ready else ("unavailable" if data is None else "stale"),
            "source_content_complete": None,
            "at": iso(self.clock()),
        }

    def _v2_read(self, clean, backend, kb) -> dict:
        ref_payload = self._v2_open(clean.get("document_ref", ""), kind="ref")
        self._v2_check_handle_kb(ref_payload, kb)
        lineage = str(ref_payload.get("l") or "")
        entry, data = self._v2_current_bytes(backend, lineage, ref_payload)
        total = len(data)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise V2Error(422, "unsupported_encoding", "raw 不是有效 UTF-8——view=raw_utf8 不做转码") from None
        budget = _v2_int(
            clean, "max_bytes", minimum=V2_MAX_BYTES_MIN, maximum=V2_MAX_BYTES_MAX,
            default=V2_MAX_BYTES_DEFAULT,
        )
        has_cursor = bool(clean.get("cursor"))
        has_range = bool(clean.get("start_byte") or clean.get("end_byte"))
        has_lines = bool(clean.get("line_start") or clean.get("line_end"))
        if sum(1 for flag in (has_cursor, has_range, has_lines) if flag) > 1:
            raise V2Error(400, "bad_request", "cursor / byte 范围 / 行范围三选一")
        range_limit: Optional[int] = None
        start: int
        if has_cursor:
            cur = self._v2_open(clean["cursor"], kind="read")
            self._v2_check_handle_kb(cur, kb, code="invalid_cursor")
            if (
                cur.get("l") != lineage
                or int(cur.get("v") or -1) != entry.version
                or cur.get("h") != entry.sha256
            ):
                raise V2Error(400, "invalid_cursor", "游标与 document_ref 身份不符")
            start = int(cur.get("s") or 0)
            r = cur.get("r")
            range_limit = int(r) if isinstance(r, int) else None
            budget = int(cur.get("b") or budget)  # cursor freezes the page budget
            # 重发同样的 max_bytes 是分页循环的自然形态，接受；改了预算才是冲突。
            resend = _v2_int(
                clean, "max_bytes", minimum=V2_MAX_BYTES_MIN, maximum=V2_MAX_BYTES_MAX
            )
            if resend is not None and resend != budget:
                raise V2Error(400, "invalid_cursor", "max_bytes 与游标冻结的页预算不一致")
        elif has_lines:
            line_start = _v2_int(clean, "line_start", minimum=1)
            line_end = _v2_int(clean, "line_end", minimum=1)
            if line_start is None:
                raise V2Error(400, "bad_request", "行范围模式需要 line_start")
            if line_end is not None and line_end < line_start:
                raise V2Error(416, "invalid_range", "line_end < line_start")
            starts = _v2_line_starts(data)
            le = line_end or line_start
            if line_start > len(starts) or le > len(starts):
                raise V2Error(416, "invalid_range", f"行号超出范围（共 {len(starts)} 行）")
            start = starts[line_start - 1]
            range_limit = starts[le] if le < len(starts) else total
        else:
            has_start = bool(clean.get("start_byte"))
            start = _v2_int(clean, "start_byte", minimum=0, default=0)
            end_raw = _v2_int(clean, "end_byte", minimum=0)
            if end_raw is not None and not has_start:
                raise V2Error(400, "bad_request", "end_byte 需要与 start_byte 同给")
            if start > total:
                raise V2Error(416, "invalid_range", f"start_byte 超出文件（total={total}）")
            if end_raw is not None:
                if end_raw < start:
                    raise V2Error(416, "invalid_range", "end_byte < start_byte")
                if end_raw > total:
                    raise V2Error(416, "invalid_range", f"end_byte 超出文件（total={total}）")
                range_limit = end_raw
                if start == end_raw and end_raw != total:
                    raise V2Error(416, "invalid_range", "空区间只有 start=end=total 合法")
        if start < total and (data[start] & 0xC0) == 0x80:
            raise V2Error(416, "invalid_utf8_boundary", "start_byte 落在 UTF-8 码点中间")
        if (
            range_limit is not None
            and range_limit < total
            and (data[range_limit] & 0xC0) == 0x80
        ):
            # 显式 end 落在码点中间是客户端错误：静默前推会改写请求的区间
            # 语义。forward-adjust 只服务于 max_bytes 预算上限。
            raise V2Error(416, "invalid_utf8_boundary", "end_byte 落在 UTF-8 码点中间")
        ceiling = range_limit if range_limit is not None else total
        span_end = _utf8_forward(data, min(ceiling, start + budget))
        page = data[start:span_end]
        eof = span_end >= total
        # 无显式范围时 ceiling==total，range_complete 与 eof 同义；next_cursor
        # 只在「未到整件末尾且请求覆盖还没读完」时签发。
        range_complete = span_end >= ceiling
        next_cursor = None
        if not eof and not range_complete:
            next_cursor = self._v2_sign(
                {"k": "read", "kb": kb, "l": lineage, "v": entry.version,
                 "h": entry.sha256, "s": span_end, "r": range_limit, "b": budget,
                 "e": self._v2_now_ts() + V2_REF_TTL_SECONDS}
            )
        return {
            "schema": READ_SCHEMA,
            "ok": True,
            "kb": kb,
            "identity": self._v2_identity(kb, lineage, entry),
            "document_ref": clean.get("document_ref", ""),
            "view": V2_VIEW,
            "encoding": "utf-8",
            "total_bytes": total,
            "span_start_byte": start,
            "span_end_byte": span_end,
            "returned_bytes": len(page),
            "text": page.decode("utf-8"),
            "page_sha256": sha256_bytes(page),
            "raw_sha256": entry.sha256,
            "full_sha_verified": True,
            "evidence_status": "verified",
            "eof": eof,
            "range_complete": range_complete,
            "next_cursor": next_cursor,
            "expires_at": datetime.fromtimestamp(
                float(ref_payload.get("e") or 0), tz=timezone.utc
            ).isoformat(),
            "coverage_scope": "returned_span",
            "source_content_complete": None,
            "at": iso(self.clock()),
        }

    def _v2_continue(self, clean, backend, kb) -> dict:
        if not clean.get("cursor"):
            raise V2Error(400, "bad_request", "continue 需要 cursor")
        return self._v2_read(clean, backend, kb)

    def _v2_renew(self, clean, backend, kb) -> dict:
        ref_payload = self._v2_open(
            clean.get("document_ref", ""), kind="ref", grace_seconds=V2_RENEW_GRACE_SECONDS
        )
        self._v2_check_handle_kb(ref_payload, kb)
        lineage = str(ref_payload.get("l") or "")
        entry, _ = self._v2_current_bytes(backend, lineage, ref_payload)
        ref, expires_at = self._v2_issue_ref(kb, lineage, entry)
        readable, reason = self._v2_readable(entry)
        return {
            "schema": DOCUMENT_SCHEMA,
            "ok": True,
            "kb": kb,
            "identity": self._v2_identity(kb, lineage, entry),
            "document_ref": ref,
            "expires_at": expires_at,
            "readable": readable,
            "reason": reason,
            "parse_status": entry.status,
            "artifact_kind": entry.artifact_kind,
            "renewed_from": clean.get("document_ref", ""),
            "at": iso(self.clock()),
        }


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
        """One line per request, on stderr, without the token header or query.

        RT-051 C04/C06: URLs now carry opaque refs/cursors, so the request
        line is logged path-only — a query string never reaches stderr.
        """
        safe = tuple(
            str(a).split("?", 1)[0] if "?" in str(a) else a for a in args
        )
        sys.stderr.write(f"[kb-gateway] {self.address_string()} {fmt % safe}\n")

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
    parser.add_argument(
        "--prefix",
        default="",
        help="nas 后端在 share 下的子路径；启用 --tokens-file 时它同时是本库的 kb_id",
    )
    parser.add_argument(
        "--tokens-file",
        default="",
        metavar="PATH",
        help=(
            "kb_token.py 登记表路径；给了才接受 Agent 绑定 token（不给 = 仅管理 token，"
            "与 v1 完全一致）"
        ),
    )
    parser.add_argument(
        "--kb",
        action="append",
        default=[],
        metavar="KB_ID",
        help="附加挂载的库（nas prefix，可重复）；RT-049 多库路由：/query?kb=<KB_ID>",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验配置并输出启动卡 JSON，不绑定端口",
    )
    return parser


def load_tokens(args: argparse.Namespace) -> Optional[TokenFile]:
    """Build the binding registry reader, or refuse to start.

    Three ways this says no, all before the port is bound:

    * ``--tokens-file`` without a non-empty ``--prefix``.  The prefix *is* the
      library identity compared against ``token.kb_ids``; an empty one would
      match nothing and every binding token would silently 403, which looks
      exactly like a revocation and is far worse than not starting.
    * a registry that cannot be read or parsed.  Starting anyway would mean
      running a gateway whose auth store is broken.
    * a registry with no records is *allowed* — a library with no agent bound
      yet is a legitimate state, and the admin face still works.
    """
    path = (getattr(args, "tokens_file", "") or "").strip()
    if not path:
        return None
    if not (args.prefix or "").strip():
        raise GatewayError(
            "--tokens-file 需要同时给 --prefix：prefix 就是本库的 kb_id，"
            "空 prefix 会让每一支绑定 token 都被判为不在授权面"
        )
    tokens = TokenFile(Path(path))
    try:
        tokens.summary()
    except TokenError as exc:
        raise GatewayError(f"绑定登记表不可用，网关拒绝启动：{exc}") from exc
    return tokens


def tokens_card(app: GatewayApp) -> dict:
    """The binding block of the startup card: counts, never a token.

    :meth:`kb_token.TokenFile.summary` returns records/active/epoch and the
    per-owner ceiling — enough for an operator to see the registry is the one
    they meant, with nothing that identifies a holder and nothing that could
    be replayed.
    """
    if app.tokens is None:
        return {"enabled": False, "note": "未启用绑定 token（仅管理 token），行为与 v1 一致"}
    card = dict(app.tokens.summary())
    card["kb_id"] = app.kb_id
    return card


def startup_card(args: argparse.Namespace, app: GatewayApp) -> dict:
    reachable, detail = app.probe()
    card_extra: Dict[str, object] = {}
    if len(app.mounts) > 1:
        # Top level, next to ``routes``/``methods``: the mount table is a
        # routing fact, not a storage fact — ``backend`` keeps describing the
        # primary only.
        card_extra["mounted_kbs"] = sorted(app.mounts)
    return {
        "schema": STARTUP_SCHEMA,
        "ok": True,
        "version": GATEWAY_VERSION,
        "host": args.host,
        "port": args.port,
        "routes": list(ROUTES),
        "v2_supported_operations": list(V2_OPERATIONS),
        "methods": list(ALLOWED_METHODS),
        "write_verbs": [],
        "auth": {
            "header": TOKEN_HEADER,
            "key_env": args.admin_key_env,
            "derivation": "sha256(admin_key) hex",
            "comparison": "hmac.compare_digest",
            "modes": ["admin"] + (["binding"] if app.tokens is not None else []),
        },
        "tokens": tokens_card(app),
        "backend": {
            "kind": args.backend,
            "reachable": reachable,
            "detail": detail,
        },
        **card_extra,
        "note": "只读网关：进程内不含任何写动词（RT-044 两进程宪法）",
        "at": iso(utc_now()),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    backend = None
    server = None
    extra_backends: List[StorageBackend] = []
    try:
        assert_no_plaintext_credential_flags(argv)
        args = build_parser().parse_args(argv)
        token = token_from_env(args.admin_key_env)
        tokens = load_tokens(args)
        primary_kb = (args.prefix or "").strip()
        extra_kbs: List[str] = []
        for kb in args.kb or []:
            kb = (kb or "").strip()
            if kb and kb != primary_kb and kb not in extra_kbs:
                extra_kbs.append(kb)
        if extra_kbs and args.backend != "nas":
            raise GatewayError("--kb 附加挂载仅支持 nas 后端（local 后端一进程一根目录）")
        backend = build_backend(args.backend, root=parse_root(args.root), prefix=args.prefix)
        kb_mounts: Dict[str, StorageBackend] = {}
        for kb in extra_kbs:
            mounted = build_backend("nas", prefix=kb)
            kb_mounts[kb] = mounted
            extra_backends.append(mounted)
        app = GatewayApp(
            backend,
            token,
            backend_kind=args.backend,
            tokens=tokens,
            kb_id=primary_kb,
            kb_mounts=kb_mounts,
        )
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
        for extra in extra_backends:
            close_backend(extra)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
