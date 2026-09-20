#!/usr/bin/env python3
"""RT-056: small, disabled-by-default KB administration face.

This module is intentionally stdlib-only.  It has no KB write imports: job
routes only append a bounded audit event and never invoke an ingest/create
primitive.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import hmac
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import sys
PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT / "scripts"))
import kb_ops  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
ENV_ENABLED = "KB_ADMIN_ENABLED"
ENV_KEY_NAME = "KB_ADMIN_KEY_ENV"
ENV_WRITE_ENABLED = "KB_ADMIN_WRITE_ENABLED"
ENV_LIBRARY_ROOT = "KB_LOCAL_LIBRARY_ROOT"
ENV_REGISTRY = "KB_REGISTRY_PATH"
ENV_AUDIT = "KB_ADMIN_AUDIT_PATH"
ENV_GATEWAY_URL = "KB_GATEWAY_URL"
ENV_OPS_URL = "KB_OPS_URL"
ENV_SERVICE_TIMEOUT = "KB_ADMIN_SERVICE_TIMEOUT"
ENV_SNAPSHOT_INDEX = "KB_SNAPSHOT_INDEX"
ENV_SNAPSHOT_ROOT = "KB_SNAPSHOT_ROOT"
# RT-065: self-serve registration (paste business Key → person directory + thin session).
ENV_REGISTER_ENABLED = "KB_REGISTER_ENABLED"
ENV_AUTHZ_STORE = "KB_AUTHZ_STORE"
ENV_SESSION_SECRET = "KB_REGISTER_SESSION_SECRET"
ENV_ALLOW_HTTP = "KB_REGISTER_ALLOW_HTTP"
#: 只有显式信任反向代理时，才采信 X-Forwarded-Proto —— 那个头是客户端发的，
#: 没有反代覆盖它时谁都能写 "https"，于是「必须加密」会变成一句空话。
ENV_TRUST_PROXY = "KB_REGISTER_TRUST_PROXY"
ENV_RATE_LIMIT = "KB_REGISTER_RATE_LIMIT"
#: 两个面：管理面默认回环、要管理密钥；注册面可对局域网、不含任何管理接口。
#: 进程只开一个面，边界由启动参数保证，而不是由「记得别点错」保证。
ENV_FACE = "KB_ADMIN_FACE"
FACE_ADMIN = "admin"
FACE_REGISTER = "register"
FACES = (FACE_ADMIN, FACE_REGISTER)
ENV_TLS_CERT = "KB_TLS_CERT"
ENV_TLS_KEY = "KB_TLS_KEY"
# RT-070: 内容同步——页面手工触发一次，和每晚自动跑的是同一条流水线。
ENV_SYNC_ENABLED = "KB_SYNC_ENABLED"
ENV_SYNC_META = "KB_SNAPSHOT_META"
ENV_SYNC_SCRIPT = "KB_SYNC_SCRIPT"
ENV_SYNC_NAS = "KB_NAS_MOUNT"
ENV_SYNC_INDEX_MAP = "KB_SNAPSHOT_INDEX"
#: 同步要读整个库再建索引，实测约一两分钟；给足余量，超时就当失败。
SYNC_TIMEOUT_SECONDS = 15 * 60
SESSION_COOKIE = "cwk_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
#: RT-068: a header a cross-site form cannot add without triggering a preflight
#: that we never answer.  Cheap second lock next to SameSite on the cookie.
MEMBER_WRITE_HEADER = "X-CWK-Members"
DEFAULT_TOKEN_TTL_DAYS = 90
DEFAULT_RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 60
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
MAX_AUDIT_EVENTS = 100
MAX_BODY = 4096
MAX_APP_KEY_CHARS = 2048
# The snapshot index is a plain doc_id -> "<bank>/<file>" map.  These bounds keep
# one HTTP request from turning into an unbounded read or an unbounded number of
# stat calls when a future bank is far larger than today's few hundred documents.
MAX_SNAPSHOT_INDEX_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_STATS = 20000


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _safe_name(value: str) -> str:
    return value if ENV_NAME.fullmatch(value) else ""


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _action_for(route: str) -> str:
    """The audit name of an ``/api/...`` route: ``/api/jobs/create`` → ``jobs.create``.

    Kept to a bounded shape so a crafted path cannot write an arbitrarily
    long or newline-bearing token into the audit file.
    """
    tail = route[len("/api/"):].strip("/")
    name = re.sub(r"[^a-z0-9_.]", "", tail.replace("/", ".").lower())[:64]
    return name or "api"


def _address(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "configured"
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        return f"{host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    except (ValueError, TypeError):
        return "configured"


def _request_is_https(headers: Mapping[str, str], *, direct_tls: bool, trust_proxy: bool) -> bool:
    """True when the client really reached us over TLS.

    ``direct_tls`` is what the socket says and cannot be faked.  The forwarded
    header is only consulted when the operator has declared that a reverse
    proxy sits in front and rewrites it; otherwise any caller could set it and
    walk their key through plaintext while the page tells them they are safe.
    """
    if direct_tls:
        return True
    if not trust_proxy:
        return False
    forwarded = str(headers.get("X-Forwarded-Proto") or headers.get("Forwarded") or "").lower()
    return "proto=https" in forwarded or forwarded.split(",")[0].strip() == "https"


class _RateLimiter:
    """Per-client cap on a public endpoint.

    Registration hands whatever it is given to 玄关, so without a cap it is a
    free "is this key valid" oracle for anyone on the office network, and a way
    to hammer 玄关 through us.
    """

    def __init__(self, limit: int, window: int = RATE_WINDOW_SECONDS) -> None:
        self.limit = max(1, int(limit))
        self.window = window
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(client or "unknown", collections.deque())
            while hits and hits[0] <= moment - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            if len(self._hits) > 1024:  # bound the table; stale buckets are empty anyway
                for key in [k for k, v in self._hits.items() if not v]:
                    del self._hits[key]
            return True


def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _seal_session(secret: str, *, principal: str, name: str, exp: int) -> str:
    body = {"principal": principal, "name": name, "exp": int(exp)}
    raw = base64.urlsafe_b64encode(_json_bytes(body)).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _open_session(secret: str, token: str) -> Optional[dict[str, Any]]:
    if not secret or not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expect = hmac.new(secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    pad = "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + pad).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    principal = payload.get("principal")
    name = payload.get("name")
    if not isinstance(principal, str) or not principal.startswith("person:"):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return {"principal": principal, "name": name.strip()[:64], "exp": exp}


def _session_cookie(value: str, *, secure: bool, max_age: int) -> str:
    parts = [
        f"{SESSION_COOKIE}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max(0, int(max_age))}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _clear_session_cookie(*, secure: bool) -> str:
    return _session_cookie("", secure=secure, max_age=0)


class AdminApp:
    """Request-independent application object, convenient for real HTTP tests."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.env = dict(environ or os.environ)
        self.enabled = _enabled(self.env.get(ENV_ENABLED))
        # The indirection must be explicit: do not silently select a common
        # ambient variable when the operator forgot the binding.
        key_name = self.env.get(ENV_KEY_NAME, "")
        self.key_name = _safe_name(key_name)
        self.library_root = Path(self.env.get(ENV_LIBRARY_ROOT, str(Path.home() / "CWK" / "libraries")).strip()).expanduser()
        self.registry = Path(self.env.get(ENV_REGISTRY, str(Path.home() / "CWK" / "ops" / "tokens.json")).strip()).expanduser()
        self.audit_path = Path(self.env.get(ENV_AUDIT, str(Path.home() / "CWK" / "ops" / "admin-audit.jsonl")).strip()).expanduser()
        self.write_enabled = _enabled(self.env.get(ENV_WRITE_ENABLED))
        # RT-058: production does not carry the RT-042 library tree — it stores a
        # snapshot (a doc_id index plus one directory per bank).  Configuring these
        # two lets the overview describe the libraries that actually exist there
        # instead of reporting an empty list forever.
        snapshot_index = (self.env.get(ENV_SNAPSHOT_INDEX) or "").strip()
        snapshot_root = (self.env.get(ENV_SNAPSHOT_ROOT) or "").strip()
        self.snapshot_index = Path(snapshot_index).expanduser() if snapshot_index else None
        self.snapshot_root = Path(snapshot_root).expanduser() if snapshot_root else None
        self._audit_lock = threading.Lock()
        # RT-065 registration: disabled until store + session secret are both set.
        authz = (self.env.get(ENV_AUTHZ_STORE) or "").strip()
        secret = (self.env.get(ENV_SESSION_SECRET) or "").strip()
        self.register_enabled = (
            _enabled(self.env.get(ENV_REGISTER_ENABLED)) and bool(authz) and len(secret) >= 16
        )
        self.authz_store = Path(authz).expanduser() if authz else None
        self.session_secret = secret if self.register_enabled else ""
        self.allow_http_register = _enabled(self.env.get(ENV_ALLOW_HTTP))
        self.trust_proxy = _enabled(self.env.get(ENV_TRUST_PROXY))
        face = (self.env.get(ENV_FACE) or FACE_ADMIN).strip().lower()
        self.face = face if face in FACES else FACE_ADMIN
        try:
            limit = int(self.env.get(ENV_RATE_LIMIT) or DEFAULT_RATE_LIMIT)
        except ValueError:
            limit = DEFAULT_RATE_LIMIT
        self.rate_limiter = _RateLimiter(limit)
        self.sync_enabled = _enabled(self.env.get(ENV_SYNC_ENABLED))
        self.sync_script = (self.env.get(ENV_SYNC_SCRIPT) or "").strip()
        self.sync_meta = Path((self.env.get(ENV_SYNC_META) or "").strip()).expanduser() \
            if self.env.get(ENV_SYNC_META) else None
        self.sync_nas = (self.env.get(ENV_SYNC_NAS) or "").strip()
        self.sync_index_map = (self.env.get(ENV_SYNC_INDEX_MAP) or "").strip()
        self._sync_lock = threading.Lock()
        self._sync_running = False
        # Tests inject a fake resolver; production resolves through 玄关.
        self.resolve_person: Callable[..., Any] | None = None

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        if not self.enabled or not self.key_name:
            return False
        expected = self.env.get(self.key_name, "")
        supplied = headers.get("X-KB-Admin-Key", headers.get("X-KB-Token", ""))
        return bool(expected) and hmac.compare_digest(str(supplied), str(expected))

    def _audit(self, action: str, outcome: str, status: int) -> None:
        event = {"timestamp": _now(), "action": action, "outcome": outcome, "status": status}
        try:
            line = _json_bytes(event) + b"\n"
            with self._audit_lock:
                self.audit_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                fd = os.open(self.audit_path, flags, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "ab", closefd=True) as handle:
                        handle.write(line)
                    fd = -1
                finally:
                    if fd >= 0:
                        os.close(fd)
        except (OSError, ValueError):
            pass

    def _session_from_headers(self, headers: Mapping[str, str]) -> Optional[dict[str, Any]]:
        if not self.register_enabled:
            return None
        cookies = _parse_cookies(str(headers.get("Cookie") or ""))
        return _open_session(self.session_secret, cookies.get(SESSION_COOKIE, ""))

    def _register(
        self, headers: Mapping[str, str], body: bytes, *, direct_tls: bool = False, client: str = ""
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Self-serve enroll: business Key in body → person directory + session cookie."""
        if not self.register_enabled or self.authz_store is None:
            self._audit("register", "disabled", 404)
            return 404, {"error": "not_found"}, {}
        secure = self._is_secure(headers, direct_tls)
        if not secure and not self.allow_http_register:
            self._audit("register", "https_required", 403)
            return 403, {"error": "https_required"}, {}
        if not self.rate_limiter.allow(client):
            self._audit("register", "rate_limited", 429)
            return 429, {"error": "rate_limited"}, {"Retry-After": str(RATE_WINDOW_SECONDS)}
        try:
            payload = json.loads(body.decode("utf-8") if body else b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._audit("register", "invalid_json", 400)
            return 400, {"error": "invalid_request"}, {}
        if not isinstance(payload, dict):
            self._audit("register", "invalid_json", 400)
            return 400, {"error": "invalid_request"}, {}
        # Accept a few synonyms so a future Chinese label does not fork the contract.
        app_key = payload.get("app_key") or payload.get("key") or payload.get("business_key") or ""
        if not isinstance(app_key, str) or not app_key.strip():
            self._audit("register", "missing_key", 400)
            return 400, {"error": "missing_key"}, {}
        app_key = app_key.strip()
        if len(app_key) > MAX_APP_KEY_CHARS:
            self._audit("register", "key_too_long", 400)
            return 400, {"error": "invalid_request"}, {}
        import kb_authz  # noqa: E402  — only loaded when registration runs
        import kb_identity  # noqa: E402
        import kb_token  # noqa: E402

        resolver = self.resolve_person or kb_identity.resolve_person
        try:
            person = resolver(app_key)
        except kb_identity.IdentityResolutionError as exc:
            # Refuse without echoing the key.  Same status for "bad key" and
            # "upstream down" so the page cannot be used to probe 玄关.
            detail = kb_token.redact(str(exc), app_key)
            self._audit("register", "identity_refused", 401)
            return 401, {"error": "identity_refused", "message": detail[:120]}, {}
        try:
            kb_authz.mutate(
                self.authz_store,
                lambda data: kb_authz.upsert_person(data, person, operator="self_register"),
            )
        except kb_authz.AuthzError as exc:
            self._audit("register", "store_failed", 500)
            return 500, {"error": "store_failed", "message": str(exc)[:120]}, {}
        except (OSError, ValueError):
            self._audit("register", "store_failed", 500)
            return 500, {"error": "store_failed"}, {}
        exp = int(time.time()) + SESSION_TTL_SECONDS
        token = _seal_session(
            self.session_secret, principal=person.principal, name=person.name, exp=exp
        )
        self._audit("register", "ok", 200)
        return (
            200,
            {
                "schema": "cwk.kb.register.v1",
                "principal": person.principal,
                "name": person.name,
                "expires_at": datetime.fromtimestamp(exp, timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            {"Set-Cookie": _session_cookie(token, secure=secure, max_age=SESSION_TTL_SECONDS)},
        )

    def _members_view(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
        """What the signed-in person may manage: their banks, members, and the directory.

        No admin key anywhere near this: the actor is whoever the session says
        it is, and what they may see follows the same rule that decides what
        they may change — owner of that bank, or an administrator.
        """
        row = self._session_from_headers(headers)
        if not row:
            return 401, {"error": "unauthorized"}, {}
        import kb_authz  # noqa: PLC0415

        try:
            data = kb_authz.load_store(self.authz_store)
        except kb_authz.AuthzError as exc:
            return 500, {"error": "store_unreadable", "message": str(exc)[:120]}, {}
        actor = str(row["principal"])
        admin = kb_authz.is_admin(data, actor)
        banks = []
        for bank_id, entry in sorted(data["banks"].items()):
            if not isinstance(entry, dict) or entry.get("status") != kb_authz.BANK_ACTIVE:
                continue
            my_role = kb_authz.role_of(data, bank_id, actor)
            if not (admin or my_role == kb_authz.ROLE_OWNER):
                continue
            banks.append({
                "bank_id": bank_id,
                "name": entry.get("name", bank_id),
                "my_role": "admin" if admin and my_role is None else (my_role or ""),
                "members": kb_authz.members_of(data, bank_id),
            })
        people = [
            {"principal": principal, "name": person.get("name", "")}
            for principal, person in sorted(data["persons"].items())
            if isinstance(person, dict)
        ] if banks else []
        return 200, {
            "schema": "cwk.kb.members.v1",
            "version": data["version"],
            "me": {"principal": actor, "name": row["name"], "admin": admin},
            "banks": banks,
            "persons": people,
            "roles": list(kb_authz.ROLES),
        }, {}

    def _members_write(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Add, change or remove one member.  The rules live in kb_authz, not here."""
        row = self._session_from_headers(headers)
        if not row:
            self._audit("members", "unauthorized", 401)
            return 401, {"error": "unauthorized"}, {}
        if not str(headers.get(MEMBER_WRITE_HEADER) or "").strip():
            # A page of ours always sends it; a form on someone else's site cannot.
            self._audit("members", "missing_marker", 400)
            return 400, {"error": "invalid_request"}, {}
        try:
            payload = json.loads(body.decode("utf-8") if body else b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._audit("members", "invalid_json", 400)
            return 400, {"error": "invalid_request"}, {}
        if not isinstance(payload, dict):
            self._audit("members", "invalid_json", 400)
            return 400, {"error": "invalid_request"}, {}
        bank_id = payload.get("bank_id")
        principal = payload.get("principal")
        role = payload.get("role")  # None / "" means remove
        expect = payload.get("expect_version")
        if not isinstance(bank_id, str) or not isinstance(principal, str):
            self._audit("members", "invalid_request", 400)
            return 400, {"error": "invalid_request"}, {}
        if expect is not None and (isinstance(expect, bool) or not isinstance(expect, int)):
            self._audit("members", "invalid_request", 400)
            return 400, {"error": "invalid_request"}, {}

        import kb_authz  # noqa: PLC0415

        actor = str(row["principal"])
        audit = {"operator": f"members-page:{actor}", "reason": "成员管理页面"}

        def change(data: dict):
            if role:
                return kb_authz.set_member(data, actor=actor, bank_id=bank_id, principal=principal,
                                           role=role, **audit)
            kb_authz.remove_member(data, actor=actor, bank_id=bank_id, principal=principal, **audit)
            return True

        try:
            data, _ = kb_authz.mutate(self.authz_store, change, expect_version=expect)
        except kb_authz.Forbidden as exc:
            self._audit("members", "forbidden", 403)
            return 403, {"error": "forbidden", "message": str(exc)}, {}
        except kb_authz.NotFound as exc:
            self._audit("members", "not_found", 404)
            return 404, {"error": "not_found", "message": str(exc)}, {}
        except kb_authz.VersionConflict as exc:
            # 别人刚改过：刷新重试就行。
            self._audit("members", "version_conflict", 409)
            return 409, {"error": "version_conflict", "message": str(exc)}, {}
        except kb_authz.ConflictError as exc:
            # 规则不允许（例如库至少要保留一位所有者）：重试多少次都一样，
            # 必须把真正的原因原样交给用户。
            self._audit("members", "conflict", 409)
            return 409, {"error": "conflict", "message": str(exc)}, {}
        except (kb_authz.UsageError, kb_authz.StoreError) as exc:
            self._audit("members", "rejected", 400)
            return 400, {"error": "rejected", "message": str(exc)}, {}
        self._audit("members", "ok", 200)
        return 200, {"schema": "cwk.kb.members-write.v1", "version": data["version"],
                     "effective": "next_request"}, {}

    def _token_identity(self, row: Mapping[str, Any], app_key: object):
        """Re-verify the key at issue time and insist it is the signed-in person.

        The session says who is here; RT-047's rule says a token's owner may
        only be derived from a key that just proved itself.  Both must agree,
        so a stolen session cannot mint a token and a valid key cannot mint one
        for somebody else's session.
        """
        import kb_identity  # noqa: PLC0415
        import kb_token  # noqa: PLC0415

        if not isinstance(app_key, str) or not app_key.strip():
            return None, (400, {"error": "missing_key"}, {})
        app_key = app_key.strip()
        if len(app_key) > MAX_APP_KEY_CHARS:
            return None, (400, {"error": "invalid_request"}, {})
        try:
            data = kb_token.load_registry(self.registry)
        except kb_token.TokenError as exc:
            return None, (500, {"error": "registry_unreadable", "message": str(exc)[:120]}, {})
        resolver = self.resolve_person or kb_identity.resolve_person
        try:
            identity = kb_token.verify_business_key(
                "SESSION_KEY", salt_hex=str(data.get("owner_ref_salt") or ""),
                env={"SESSION_KEY": app_key}, probe=lambda key: resolver(key),
            )
        except kb_token.IdentityError as exc:
            return None, (401, {"error": "identity_refused",
                                "message": kb_token.redact(str(exc), app_key)[:120]}, {})
        if identity.principal != row["principal"]:
            # 用别人的 Key 给自己签，或反过来——两边必须是同一个人。
            self._audit("token_issue", "identity_mismatch", 403)
            return None, (403, {"error": "identity_mismatch",
                                "message": "这把 Key 属于另一个人，请用你本人登录时用的那把"}, {})
        return (identity, data), None

    def _token_rows(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
        """My tokens (metadata only).  Administrators additionally see everyone's."""
        row = self._session_from_headers(headers)
        if not row:
            return 401, {"error": "unauthorized"}, {}
        import kb_authz  # noqa: PLC0415
        import kb_token  # noqa: PLC0415

        try:
            data = kb_token.load_registry(self.registry)
            store = kb_authz.load_store(self.authz_store)
        except (kb_token.TokenError, kb_authz.AuthzError) as exc:
            return 500, {"error": "unreadable", "message": str(exc)[:120]}, {}
        now = kb_token.utc_now()
        admin = kb_authz.is_admin(store, row["principal"])
        names = {p: entry.get("name", "") for p, entry in store["persons"].items() if isinstance(entry, dict)}
        mine, others = [], []
        for record in kb_token.records(data):
            view = kb_token.public_view(record, now)
            # 明文和摘要都不在 public_view 里；这里再把派生标识也去掉，
            # 页面不需要它们，少一个字段就少一条泄露路径。
            for field in ("owner_ref", "owner_ref_basis", "agent_binding_id", "membership_epoch"):
                view.pop(field, None)
            view["holder"] = names.get(view.get("principal", ""), "")
            (mine if view.get("principal") == row["principal"] else others).append(view)
        mine.sort(key=lambda v: (v["status"] != "active", v["created_at"]), reverse=False)
        banks = [b["bank_id"] for b in kb_authz.list_banks(store, row["principal"])]
        return 200, {
            "schema": "cwk.kb.tokens.v1",
            "me": {"principal": row["principal"], "name": row["name"], "admin": admin},
            "tokens": mine,
            "all_tokens": sorted(others, key=lambda v: v["created_at"]) if admin else [],
            "my_banks": banks,
            "max_active_per_owner": int(data.get("max_active_per_owner", 5)),
            "default_ttl_days": DEFAULT_TOKEN_TTL_DAYS,
        }, {}

    def _token_issue(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any], dict[str, str]]:
        row = self._session_from_headers(headers)
        if not row:
            self._audit("token_issue", "unauthorized", 401)
            return 401, {"error": "unauthorized"}, {}
        if not str(headers.get(MEMBER_WRITE_HEADER) or "").strip():
            return 400, {"error": "invalid_request"}, {}
        try:
            payload = json.loads(body.decode("utf-8") if body else b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid_request"}, {}
        if not isinstance(payload, dict):
            return 400, {"error": "invalid_request"}, {}

        import kb_authz  # noqa: PLC0415
        import kb_token  # noqa: PLC0415

        verified, refusal = self._token_identity(row, payload.get("app_key"))
        if refusal:
            return refusal
        identity, data = verified
        try:
            store = kb_authz.load_store(self.authz_store)
        except kb_authz.AuthzError as exc:
            return 500, {"error": "unreadable", "message": str(exc)[:120]}, {}
        banks = [b["bank_id"] for b in kb_authz.list_banks(store, row["principal"])]
        if not banks:
            self._audit("token_issue", "no_banks", 409)
            return 409, {"error": "no_banks",
                         "message": "你还不是任何库的成员，先请管理员把你加进一个库再来签"}, {}
        try:
            record, plaintext = kb_token.issue_token(
                data, identity=identity, raw_agent_id=str(payload.get("agent_id") or ""),
                kb_ids=banks, ttl_days=DEFAULT_TOKEN_TTL_DAYS,
                authz=kb_token.AUTHZ_GRANTS, label=str(payload.get("label") or ""),
                actor=f"token-page:{row['principal']}", reason="自助签发",
            )
        except kb_token.ConflictError as exc:
            self._audit("token_issue", "conflict", 409)
            return 409, {"error": "conflict", "message": str(exc)}, {}
        except kb_token.TokenError as exc:
            self._audit("token_issue", "rejected", 400)
            return 400, {"error": "rejected", "message": str(exc)}, {}
        kb_token.save_registry(self.registry, data)
        self._audit("token_issue", "ok", 200)
        view = kb_token.public_view(record, kb_token.utc_now())
        return 200, {
            "schema": "cwk.kb.token-issued.v1",
            "token": plaintext,
            "token_id": view["token_id"],
            "expires_at": view["expires_at"],
            "banks": banks,
        }, {}

    def _token_revoke(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any], dict[str, str]]:
        row = self._session_from_headers(headers)
        if not row:
            self._audit("token_revoke", "unauthorized", 401)
            return 401, {"error": "unauthorized"}, {}
        if not str(headers.get(MEMBER_WRITE_HEADER) or "").strip():
            return 400, {"error": "invalid_request"}, {}
        try:
            payload = json.loads(body.decode("utf-8") if body else b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid_request"}, {}
        token_id = payload.get("token_id") if isinstance(payload, dict) else None
        if not isinstance(token_id, str) or not token_id:
            return 400, {"error": "invalid_request"}, {}

        import kb_authz  # noqa: PLC0415
        import kb_token  # noqa: PLC0415

        try:
            data = kb_token.load_registry(self.registry)
            store = kb_authz.load_store(self.authz_store)
            record = kb_token.find_record(data, token_id)
        except kb_token.NotFound:
            return 404, {"error": "not_found"}, {}
        except (kb_token.TokenError, kb_authz.AuthzError) as exc:
            return 500, {"error": "unreadable", "message": str(exc)[:120]}, {}
        # 吊销是安全动作：自己的随时可以撤；别人的只有管理员能撤（例如人离职、令牌外泄）。
        if record.get("principal") != row["principal"] and not kb_authz.is_admin(store, row["principal"]):
            self._audit("token_revoke", "forbidden", 403)
            return 403, {"error": "forbidden", "message": "只能吊销自己的令牌"}, {}
        try:
            kb_token.revoke_token(data, token_id=token_id,
                                  actor=f"token-page:{row['principal']}", reason="页面吊销")
        except kb_token.ConflictError as exc:
            return 409, {"error": "conflict", "message": str(exc)}, {}
        kb_token.save_registry(self.registry, data)
        self._audit("token_revoke", "ok", 200)
        return 200, {"schema": "cwk.kb.token-revoked.v1", "token_id": token_id, "effective": "immediate"}, {}

    def _require_admin(self, headers: Mapping[str, str]):
        """同步动的是所有人查到的内容，所以只让管理员点。"""
        row = self._session_from_headers(headers)
        if not row:
            return None, (401, {"error": "unauthorized"}, {})
        import kb_authz  # noqa: PLC0415

        try:
            store = kb_authz.load_store(self.authz_store)
        except kb_authz.AuthzError as exc:
            return None, (500, {"error": "store_unreadable", "message": str(exc)[:120]}, {})
        if not kb_authz.is_admin(store, row["principal"]):
            return None, (403, {"error": "forbidden", "message": "只有管理员能触发同步"}, {})
        return row, None

    def _sync_status(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
        """线上这份内容是什么时候的、每个库多少条、有没有更新失败。"""
        row, refusal = self._require_admin(headers)
        if refusal:
            return refusal
        meta: dict[str, Any] = {}
        if self.sync_meta:
            try:
                meta = json.loads(self.sync_meta.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                meta = {}
        return 200, {
            "schema": "cwk.kb.sync-status.v1",
            "enabled": self.sync_enabled,
            "running": self._sync_running,
            "me": {"principal": row["principal"], "name": row["name"]},
            "meta": meta or None,
        }, {}

    def _sync_run(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
        row, refusal = self._require_admin(headers)
        if refusal:
            self._audit("sync", "refused", refusal[0])
            return refusal
        if not str(headers.get(MEMBER_WRITE_HEADER) or "").strip():
            return 400, {"error": "invalid_request"}, {}
        if not (self.sync_enabled and self.sync_script and self.sync_nas and self.sync_index_map and self.sync_meta):
            self._audit("sync", "not_configured", 404)
            return 404, {"error": "not_found"}, {}
        with self._sync_lock:
            if self._sync_running:
                # 两个人同时点，第二次直接拒绝：同一条流水线并发跑会互相覆盖索引。
                self._audit("sync", "already_running", 409)
                return 409, {"error": "already_running", "message": "已经有一次同步在跑，等它结束"}, {}
            self._sync_running = True
        started = time.time()
        try:
            import subprocess  # noqa: PLC0415

            completed = subprocess.run(
                [sys.executable, self.sync_script, "build",
                 "--nas", self.sync_nas, "--index-map", self.sync_index_map,
                 "--meta", str(self.sync_meta)],
                capture_output=True, text=True, timeout=SYNC_TIMEOUT_SECONDS,
                cwd=str(Path(self.sync_script).resolve().parents[1]),
            )
        except subprocess.TimeoutExpired:
            self._audit("sync", "timeout", 504)
            return 504, {"error": "timeout", "message": "同步超时，线上保持原样"}, {}
        except OSError as exc:
            self._audit("sync", "spawn_failed", 500)
            return 500, {"error": "spawn_failed", "message": f"{type(exc).__name__}"}, {}
        finally:
            with self._sync_lock:
                self._sync_running = False
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        elapsed = int(time.time() - started)
        if completed.returncode != 0:
            self._audit("sync", "failed", 500)
            message = ((payload.get("error") or {}).get("message")
                       or (completed.stderr or "").strip()[:160] or "同步失败")
            return 500, {"error": "sync_failed", "message": message, "elapsed_seconds": elapsed}, {}
        self._audit("sync", "ok", 200)
        return 200, {"schema": "cwk.kb.sync-run.v1", "elapsed_seconds": elapsed,
                     "result": payload}, {}

    def _session_status(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any], dict[str, str]]:
        if not self.register_enabled:
            return 404, {"error": "not_found"}, {}
        row = self._session_from_headers(headers)
        if not row:
            return 401, {"error": "unauthorized"}, {}
        return (
            200,
            {
                "schema": "cwk.kb.session.v1",
                "principal": row["principal"],
                "name": row["name"],
                "expires_at": datetime.fromtimestamp(int(row["exp"]), timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            {},
        )

    def _is_secure(self, headers: Mapping[str, str], direct_tls: bool) -> bool:
        return _request_is_https(headers, direct_tls=direct_tls, trust_proxy=self.trust_proxy)

    def _logout(self, headers: Mapping[str, str], *, direct_tls: bool = False) -> tuple[int, dict[str, Any], dict[str, str]]:
        if not self.register_enabled:
            return 404, {"error": "not_found"}, {}
        secure = self._is_secure(headers, direct_tls)
        self._audit("logout", "ok", 200)
        return 200, {"schema": "cwk.kb.logout.v1", "ok": True}, {
            "Set-Cookie": _clear_session_cookie(secure=secure)
        }

    def _snapshot_libraries(self) -> list[dict[str, Any]]:
        """Describe the banks of an RT-055 snapshot: an index plus one dir per bank.

        ``total`` counts the documents the index claims; ``readable_total`` counts
        the ones whose file is actually on disk.  Keeping them apart is the point —
        a snapshot that lost files still reports a healthy-looking index, and the
        gap between the two numbers is the only place that shows up.
        """
        if not self.snapshot_index:
            return []
        entry: dict[str, dict[str, Any]] = {}
        try:
            if self.snapshot_index.stat().st_size > MAX_SNAPSHOT_INDEX_BYTES:
                raise ValueError("snapshot index too large")
            index = json.loads(self.snapshot_index.read_text("utf-8"))
            if not isinstance(index, dict):
                raise ValueError("snapshot index is not a mapping")
        except (OSError, ValueError, UnicodeError):
            # Name the failure rather than rendering an empty library list: an
            # unreadable index and an empty corpus must not look the same.
            return [{"kb_id": "(快照索引)", "total": None, "readable_total": None,
                     "lexical_status": None, "up_to_date": None, "source": "snapshot",
                     "error": "索引不可读"}]
        stats_left = MAX_SNAPSHOT_STATS
        for relative in index.values():
            text = str(relative or "").strip()
            bank, _, remainder = text.partition("/")
            if not bank or not remainder or bank.startswith("."):
                continue
            row = entry.setdefault(bank, {"total": 0, "readable": 0, "checked": 0})
            row["total"] += 1
            if self.snapshot_root is not None and stats_left > 0:
                stats_left -= 1
                row["checked"] += 1
                # Re-anchor under the configured root so an index entry cannot
                # point the existence check outside the snapshot directory.
                candidate = (self.snapshot_root / text).resolve()
                try:
                    inside = candidate.is_relative_to(self.snapshot_root.resolve())
                except (OSError, ValueError):
                    inside = False
                if inside and candidate.is_file():
                    row["readable"] += 1
        libraries = []
        for bank in sorted(entry):
            row = entry[bank]
            partial = row["checked"] < row["total"]
            missing = row["checked"] - row["readable"]
            libraries.append({
                "kb_id": bank,
                "total": row["total"],
                # Unverified is not the same as zero: say nothing rather than
                # report a count the bound stopped us from establishing.
                "readable_total": None if row["checked"] == 0 else row["readable"],
                "lexical_status": None,
                "up_to_date": None,
                "source": "snapshot",
                "error": (f"{missing} 个文件缺失" if missing > 0 else
                          ("未逐一核对" if partial and row["checked"] else None)),
            })
        return libraries

    def _overview(self) -> dict[str, Any]:
        backends: dict[str, Any] = {}
        try:
            backends, mounted = kb_ops._build_local_backends(self.library_root)
            payload, _ = kb_ops.status(backends, self.registry, mounted=mounted)
            tokens = []
            for row in payload.get("tokens", []):
                tokens.append({
                    "token_id": row.get("token_id_suffix", ""),
                    "scope": row.get("kb_ids", []),
                    "status": row.get("status", "unknown"),
                    "created_at": row.get("created_at", ""),
                    "expires_at": row.get("expires_at", ""),
                    "remaining_days": row.get("remaining_days"),
                })
            tree = [dict(row, source="library-tree") for row in payload.get("libraries", [])]
            return {
                "schema": "cwk.kb.admin.overview.v1",
                "ok": bool(payload.get("ok")),
                "complete": bool(payload.get("complete")),
                "registry_status": payload.get("registry_status", "unavailable"),
                "libraries": tree + self._snapshot_libraries(),
                "tokens": tokens,
            }
        except Exception:
            # The registry read failed, but a snapshot listing needs neither the
            # registry nor a backend — do not throw it away with the rest.
            return {"schema": "cwk.kb.admin.overview.v1", "ok": False, "complete": False,
                    "registry_status": "unavailable", "libraries": self._snapshot_libraries(), "tokens": []}
        finally:
            for backend in backends.values():
                try:
                    kb_ops.close_backend(backend)
                except Exception:
                    pass

    def _services(self) -> dict[str, Any]:
        try:
            timeout = min(3.0, max(0.1, float(self.env.get(ENV_SERVICE_TIMEOUT, "1.0"))))
        except ValueError:
            timeout = 1.0
        results = []
        for name, env_name, default in (("gateway", ENV_GATEWAY_URL, "http://127.0.0.1:8787/health"), ("rag_answer", ENV_OPS_URL, "http://127.0.0.1:8790/healthz")):
            url = self.env.get(env_name, default)
            try:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    results.append({"name": name, "address": _address(url), "status": "healthy" if 200 <= response.status < 400 else "unhealthy", "http_status": response.status})
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                results.append({"name": name, "address": _address(url), "status": "unhealthy", "http_status": None})
        return {"schema": "cwk.kb.admin.services.v1", "services": results, "timeout_seconds": timeout}

    def _audit_read(self) -> dict[str, Any]:
        events = []
        try:
            lines = self.audit_path.read_text("utf-8").splitlines()[-MAX_AUDIT_EVENTS:]
            for line in lines:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        event = {key: row[key] for key in ("timestamp", "action", "outcome", "status") if key in row}
                        if set(event) >= {"timestamp", "action", "outcome", "status"}:
                            events.append(event)
                except (ValueError, TypeError, UnicodeDecodeError):
                    continue
        except (OSError, UnicodeError):
            pass
        return {"schema": "cwk.kb.admin.audit.v1", "events": events}

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes = b"",
        *,
        direct_tls: bool = False,
        client: str = "",
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if method not in ("GET", "POST"):
            return 405, {"error": "method_not_allowed"}, {"Allow": "GET, POST"}
        route = urllib.parse.urlsplit(path).path
        if route == "/healthz":
            live = self.enabled or self.face == FACE_REGISTER
            return (200 if live else 503), {"schema": "cwk.kb.admin.health.v1", "enabled": self.enabled, "face": self.face, "status": "ok" if live else "disabled"}, {}
        # RT-065: registration is a public face (no admin key).  It still requires
        # an explicit enable switch plus store + session secret, and it lives only
        # on the register face — the admin face keeps its loopback default.
        if self.face == FACE_REGISTER:
            if route == "/register" and method == "GET":
                if not self.register_enabled:
                    return 404, {"error": "not_found"}, {}
                if not self._is_secure(headers, direct_tls) and not self.allow_http_register:
                    # Refuse *before* the key is typed.  A form served over
                    # plaintext that rejects the key afterwards has already let
                    # it cross the network once.
                    self._audit("register_page", "https_required", 403)
                    return 403, {"html": _REGISTER_INSECURE_HTML}, {"Content-Type": "text/html; charset=utf-8"}
                return 200, {"html": _REGISTER_HTML}, {"Content-Type": "text/html; charset=utf-8"}
            if route == "/api/register" and method == "POST":
                return self._register(headers, body, direct_tls=direct_tls, client=client)
            if route == "/api/session" and method == "GET":
                return self._session_status(headers)
            if route == "/api/logout" and method == "POST":
                return self._logout(headers, direct_tls=direct_tls)
            # RT-068 member management: same face, because it needs exactly what
            # this face already has — TLS and a person behind the session.
            if route == "/members" and method == "GET":
                if not self._is_secure(headers, direct_tls) and not self.allow_http_register:
                    return 403, {"html": _REGISTER_INSECURE_HTML}, {"Content-Type": "text/html; charset=utf-8"}
                return 200, {"html": _MEMBERS_HTML}, {"Content-Type": "text/html; charset=utf-8"}
            if route == "/api/members" and method == "GET":
                return self._members_view(headers)
            if route == "/api/members" and method == "POST":
                return self._members_write(headers, body)
            # RT-069 自助令牌：同样只在这个面上，同样要本人身份。
            if route == "/tokens" and method == "GET":
                if not self._is_secure(headers, direct_tls) and not self.allow_http_register:
                    return 403, {"html": _REGISTER_INSECURE_HTML}, {"Content-Type": "text/html; charset=utf-8"}
                return 200, {"html": _TOKENS_HTML}, {"Content-Type": "text/html; charset=utf-8"}
            if route == "/api/tokens" and method == "GET":
                return self._token_rows(headers)
            if route == "/api/tokens" and method == "POST":
                return self._token_issue(headers, body)
            if route == "/api/tokens/revoke" and method == "POST":
                return self._token_revoke(headers, body)
            # RT-070 内容同步：页面上手工跑一次，和每晚自动跑的是同一条流水线。
            if route == "/sync" and method == "GET":
                if not self._is_secure(headers, direct_tls) and not self.allow_http_register:
                    return 403, {"html": _REGISTER_INSECURE_HTML}, {"Content-Type": "text/html; charset=utf-8"}
                return 200, {"html": _SYNC_HTML}, {"Content-Type": "text/html; charset=utf-8"}
            if route == "/api/sync" and method == "GET":
                return self._sync_status(headers)
            if route == "/api/sync" and method == "POST":
                return self._sync_run(headers)
            # Nothing else exists on this face: no overview, no audit, no services.
            return 404, {"error": "not_found"}, {}
        # …and on the admin face the registration routes do not exist either,
        # answered before the key check so the boundary reads the same way from
        # both sides: registration lives on one face only.
        if route in ("/register", "/api/register", "/api/session", "/api/logout",
                     "/members", "/api/members", "/tokens", "/api/tokens", "/api/tokens/revoke",
                     "/sync", "/api/sync"):
            return 404, {"error": "not_found"}, {}
        if route in ("/", "/console"):
            return 200, {"html": _HTML}, {"Content-Type": "text/html; charset=utf-8"}
        if not route.startswith("/api/"):
            return 404, {"error": "not_found"}, {}
        if not self._authorized(headers):
            # RT-058: a rejected attempt is the event an administrator most
            # needs to see, and until now it was the one event not recorded —
            # somebody guessing at the key left no trace at all.  Only the
            # route is written; the supplied key never reaches the log.
            self._audit(_action_for(route), "unauthorized", 401)
            return 401, {"error": "unauthorized"}, {}
        if route == "/api/overview" and method == "GET":
            self._audit("overview", "ok", 200)
            return 200, self._overview(), {}
        if route == "/api/services" and method == "GET":
            self._audit("services", "ok", 200)
            return 200, self._services(), {}
        if route == "/api/audit" and method == "GET":
            # Reading the log is itself an administrative action; leaving it
            # unrecorded made the log an unaudited read of an audit file.
            self._audit("audit", "ok", 200)
            return 200, self._audit_read(), {}
        if route in ("/api/jobs/create", "/api/jobs/ingest") and method == "POST":
            action = route.rsplit("/", 1)[-1]
            if not self.write_enabled:
                self._audit(action, "rejected_write_disabled", 403)
                return 403, {"error": "write_disabled", "job": action}, {}
            self._audit(action, "placeholder_not_implemented", 501)
            return 501, {"error": "not_implemented", "job": action, "recorded": True}, {}
        return 404, {"error": "not_found"}, {}


_SYNC_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>内容同步 · CWK 知识库</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7;
  --ok:#1a7f4b;--bad:#b3261e;--warn:#a86a00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:780px;margin:0 auto;padding:2rem 1.25rem 3rem}
h1{font-size:1.4rem;margin:0 0 .3rem}
.lede{color:var(--muted);margin:0 0 1.5rem;font-size:.92rem}
.who{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:.8rem;margin-bottom:1.5rem}
.who a{color:var(--accent);text-decoration:none;margin-left:1rem;font-size:.9rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.2rem;margin-bottom:1rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.5rem .45rem;border-bottom:1px solid var(--line)}
th{font-size:.78rem;color:var(--muted);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
button{padding:.55rem 1.2rem;border:1px solid var(--accent);border-radius:7px;background:var(--accent);
  color:#fff;cursor:pointer;font:inherit;font-weight:600}
button:disabled{opacity:.45;cursor:not-allowed}
.msg{padding:.75rem 1rem;border-radius:8px;font-size:.9rem;margin-bottom:1rem}
.msg.bad{background:#fbe9e7;color:var(--bad)} .msg.ok{background:#e6f4ec;color:var(--ok)}
.msg.warn{background:#fdf1dd;color:var(--warn)}
.hint{color:var(--muted);font-size:.85rem;margin:.6rem 0 0}
.stale{color:var(--warn);font-weight:600}
@media (prefers-color-scheme:dark){
  :root{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff}
  .msg.bad{background:#3a1f1d} .msg.ok{background:#16301f} .msg.warn{background:#332715}
}
</style></head>
<body><div class='wrap'>
<h1>内容同步</h1>
<p class='lede'>把 NAS 上各个库的当前内容同步成可检索的索引。每晚自动跑一次；需要立刻生效时在这里手工跑。</p>
<div class='who'><span id='me'>正在确认身份…</span>
  <span><a href='/members'>成员管理</a><a href='/tokens'>我的令牌</a></span></div>
<div id='msg'></div>
<div id='state'></div>
<div class='panel'>
  <button type='button' id='go'>立即同步</button>
  <p class='hint'>大约一到两分钟。同步期间线上照常服务，新内容全部就绪并自检通过后才会切换；
     中途任何一步失败都保持原样，不会出现只更新一半的情况。</p>
</div>
<script>
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

function message(kind, text){
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}

function daysAgo(iso){
  if (!iso) return '';
  const days = Math.floor((Date.now() - Date.parse(iso)) / 86400000);
  if (isNaN(days)) return '';
  if (days <= 0) return '今天';
  return days + ' 天前';
}

function render(meta){
  if (!meta){
    $('state').innerHTML = "<div class='panel'>还没有同步记录。点下面的按钮跑第一次。</div>";
    return;
  }
  const banks = Object.values(meta.banks || {});
  const rows = banks.map((b) => {
    const ago = daysAgo(b.newest_update);
    const stale = ago && ago !== '今天' && parseInt(ago, 10) >= 3;
    return '<tr><td>' + esc(b.bank) + "</td><td class='num'>" + b.usable + '</td>'
      + "<td class='num'>" + (b.skipped_unconverted || 0) + '</td>'
      + '<td' + (stale ? " class='stale'" : '') + '>' + esc(b.newest_update ? b.newest_update.slice(0,10) : '—')
      + (ago ? '（' + esc(ago) + '）' : '') + '</td></tr>';
  }).join('');
  $('state').innerHTML = "<div class='panel'>"
    + '<table><thead><tr><th>库</th><th>已入库</th><th>待转换</th><th>内容截止</th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table>'
    + "<p class='hint'>上次同步：" + esc((meta.generated_at || '').replace('T',' ').replace('Z',' UTC'))
    + '｜合计 ' + (meta.total || 0) + " 条。「待转换」是还没转成文字的原件（图片、表格、PDF），不参与检索。</p>"
    + '</div>';
}

async function load(){
  let response;
  try { response = await fetch('/api/sync', {cache: 'no-store'}); }
  catch (err) { message('bad', '连不上服务。'); return; }
  if (response.status === 401){
    $('me').textContent = '还没登录';
    message('warn', '请先到<a href="/register">注册页</a>用本人工作协同 Key 登录。');
    $('go').disabled = true;
    return;
  }
  if (response.status === 403){
    $('me').textContent = '已登录';
    message('warn', '只有管理员能触发同步。');
    $('go').disabled = true;
    return;
  }
  if (!response.ok){ message('bad', '读取状态失败（' + response.status + '）。'); return; }
  const data = await response.json();
  $('me').innerHTML = '当前身份：<b>' + esc(data.me.name) + '</b>（管理员）';
  $('go').disabled = !data.enabled || data.running;
  if (!data.enabled) message('warn', '同步功能没有启用（服务端未配置）。');
  else if (data.running) message('warn', '已经有一次同步正在跑。');
  render(data.meta);
}

$('go').addEventListener('click', async () => {
  $('go').disabled = true;
  message('warn', '正在同步，大约一到两分钟，请不要关闭页面…');
  let response;
  try {
    response = await fetch('/api/sync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CWK-Members': '1'},
      cache: 'no-store'
    });
  } catch (err) {
    $('go').disabled = false;
    message('bad', '连接中断。线上内容保持原样；稍后看状态确认这次有没有生效。');
    return;
  }
  let data = {};
  try { data = await response.json(); } catch (err) {}
  $('go').disabled = false;
  if (!response.ok){
    message('bad', esc(data.message || '同步失败（' + response.status + '）。') + ' 线上内容保持原样。');
    await load();
    return;
  }
  const result = data.result || {};
  message('ok', '同步完成，用时 ' + (data.elapsed_seconds || 0) + ' 秒，共 '
    + (result.total || 0) + ' 条已生效。');
  await load();
});

load();
</script></body></html>"""


_TOKENS_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>我的令牌 · CWK 知识库</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7;
  --ok:#1a7f4b;--bad:#b3261e;--warn:#a86a00;--code:#161d2e;--code-ink:#dde5f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:2rem 1.25rem 3rem}
h1{font-size:1.4rem;margin:0 0 .3rem}
h2{font-size:1.05rem;margin:2rem 0 .6rem}
.lede{color:var(--muted);margin:0 0 1.5rem;font-size:.92rem}
.who{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:.8rem;margin-bottom:1.5rem}
.who a{color:var(--accent);text-decoration:none;font-size:.9rem;margin-left:1rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.2rem;margin-bottom:1rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.5rem .45rem;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:.78rem;color:var(--muted);font-weight:600}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
.tag{display:inline-block;border-radius:99px;padding:.1rem .55rem;font-size:.76rem;font-weight:600}
.tag.active{background:#e6f4ec;color:var(--ok)} .tag.revoked{background:#fbe9e7;color:var(--bad)}
.tag.expired{background:#eaedf2;color:var(--muted)}
label{display:block;font-size:.88rem;color:var(--muted);margin:.7rem 0 .25rem}
input{width:100%;padding:.55rem .7rem;border:1px solid var(--line);border-radius:7px;font:inherit;background:var(--bg);color:var(--ink)}
button{padding:.5rem 1rem;border:1px solid var(--accent);border-radius:7px;background:var(--accent);
  color:#fff;cursor:pointer;font:inherit;font-weight:600;margin-top:.9rem}
button.ghost{background:var(--card);color:var(--muted);border-color:var(--line);font-weight:400;
  margin:0;padding:.25rem .6rem;font-size:.85rem}
button.ghost:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed}
.msg{padding:.75rem 1rem;border-radius:8px;font-size:.9rem;margin-bottom:1rem}
.msg.bad{background:#fbe9e7;color:var(--bad)} .msg.ok{background:#e6f4ec;color:var(--ok)}
.msg.warn{background:#fdf1dd;color:var(--warn)}
.secret{background:var(--code);color:var(--code-ink);border-radius:9px;padding:1rem;margin:.8rem 0 0;
  word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.hint{color:var(--muted);font-size:.85rem;margin:.6rem 0 0}
@media (prefers-color-scheme:dark){
  :root{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff}
  .tag.active{background:#16301f} .tag.revoked{background:#3a1f1d} .tag.expired{background:#232c3d}
  .msg.bad{background:#3a1f1d} .msg.ok{background:#16301f} .msg.warn{background:#332715}
}
</style></head>
<body><div class='wrap'>
<h1>我的令牌</h1>
<p class='lede'>令牌代表"你是谁"。能查哪些库由成员表决定，加减权限不用重签。</p>
<div class='who'><span id='me'>正在确认身份…</span>
  <span><a href='/members'>成员管理</a><a href='/register'>注册页</a></span></div>
<div id='msg'></div>
<div id='mine'></div>
<div class='panel' id='issue' hidden>
  <h2 style='margin-top:0'>签一支新的</h2>
  <p class='hint'>为了确认是你本人，这里要再粘一次你的工作协同 Key。它只用于当场核实，不会被保存。</p>
  <label for='key'>工作协同 Key</label>
  <input type='password' id='key' autocomplete='off' placeholder='粘贴后点签发'>
  <label for='agent'>用途标识（英文，例如 my-mac、chat-agent）</label>
  <input type='text' id='agent' autocomplete='off' placeholder='同一个用途同时只能有一支有效令牌'>
  <label for='label'>备注名（可选）</label>
  <input type='text' id='label' autocomplete='off' placeholder='方便你自己认，别写成主机名'>
  <button type='button' id='go'>签发</button>
</div>
<div id='all'></div>
<script>
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const STATUS_CN = {active: '有效', revoked: '已吊销', expired: '已过期'};
let state = {me: null};

function message(kind, text){
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}

function rows(list, mine){
  if (!list.length) return "<p class='hint'>（没有）</p>";
  return '<table><thead><tr><th>用途 / 备注</th>' + (mine ? '' : '<th>持有人</th>')
    + '<th>状态</th><th>到期</th><th>编号</th><th></th></tr></thead><tbody>'
    + list.map((t) => {
      const act = t.status === 'active';
      return '<tr><td>' + esc(t.label || '—') + '</td>'
        + (mine ? '' : '<td>' + esc(t.holder || t.principal || '—') + '</td>')
        + "<td><span class='tag " + esc(t.status) + "'>" + (STATUS_CN[t.status] || esc(t.status)) + '</span></td>'
        + '<td>' + esc((t.expires_at || '').slice(0, 10)) + '</td>'
        + "<td class='mono'>" + esc(t.token_id) + '</td>'
        + "<td>" + (act ? "<button class='ghost' data-revoke='" + esc(t.token_id) + "'>吊销</button>" : '') + '</td></tr>';
    }).join('') + '</tbody></table>';
}

async function load(){
  let response;
  try { response = await fetch('/api/tokens', {cache: 'no-store'}); }
  catch (err) { message('bad', '连不上服务，请稍后重试。'); return; }
  if (response.status === 401){
    $('me').textContent = '还没登录';
    message('warn', '请先到<a href="/register">注册页</a>用本人工作协同 Key 登录，再回到这一页。');
    return;
  }
  if (!response.ok){ message('bad', '读取失败（' + response.status + '）。'); return; }
  const data = await response.json();
  state.me = data.me;
  $('me').innerHTML = '当前身份：<b>' + esc(data.me.name) + '</b>' + (data.me.admin ? '（管理员）' : '');
  const active = data.tokens.filter((t) => t.status === 'active').length;
  $('mine').innerHTML = "<div class='panel'><h2 style='margin-top:0'>我的令牌 "
    + "<span class='hint'>（有效 " + active + ' / 上限 ' + data.max_active_per_owner + '）</span></h2>'
    + rows(data.tokens, true)
    + (data.my_banks.length
        ? "<p class='hint'>签出的令牌跟随成员表：你现在是 " + esc(data.my_banks.join('、')) + ' 的成员。</p>'
        : "<p class='hint'>你还不是任何库的成员，现在签出来的令牌查不到东西——先请管理员把你加进一个库。</p>");
  $('issue').hidden = false;
  $('all').innerHTML = data.me.admin
    ? "<div class='panel'><h2 style='margin-top:0'>其他人的令牌</h2>"
      + "<p class='hint'>只看得到元数据；不能替别人签，必要时可以吊销。</p>" + rows(data.all_tokens, false) + '</div>'
    : '';
  for (const button of document.querySelectorAll('button[data-revoke]')){
    button.addEventListener('click', () => revoke(button.dataset.revoke));
  }
}

$('go').addEventListener('click', async () => {
  const key = $('key').value, agent = $('agent').value.trim();
  if (!key){ message('warn', '请先粘贴你的工作协同 Key。'); return; }
  if (!agent){ message('warn', '请填一个用途标识。'); return; }
  $('go').disabled = true;
  message('warn', '正在核实身份并签发…');
  let response;
  try {
    response = await fetch('/api/tokens', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CWK-Members': '1'},
      body: JSON.stringify({app_key: key, agent_id: agent, label: $('label').value.trim()}),
      cache: 'no-store'
    });
  } catch (err) { $('go').disabled = false; message('bad', '连不上服务。'); return; }
  $('key').value = '';
  $('go').disabled = false;
  let data = {};
  try { data = await response.json(); } catch (err) {}
  if (!response.ok){ message('bad', esc(data.message || '签发失败（' + response.status + '）。')); return; }
  $('agent').value = ''; $('label').value = '';
  message('ok', '签发成功，到期 ' + esc((data.expires_at || '').slice(0, 10))
    + '。<b>下面这串只出现这一次</b>，请立刻复制保存；关掉页面就再也拿不到了，只能重签。'
    + "<div class='secret'>" + esc(data.token) + '</div>');
  await load();
});

async function revoke(tokenId){
  if (!window.confirm('吊销后这支令牌立刻失效，确定吗？')) return;
  let response;
  try {
    response = await fetch('/api/tokens/revoke', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CWK-Members': '1'},
      body: JSON.stringify({token_id: tokenId}), cache: 'no-store'
    });
  } catch (err) { message('bad', '连不上服务。'); return; }
  let data = {};
  try { data = await response.json(); } catch (err) {}
  if (!response.ok){ message('bad', esc(data.message || '吊销失败（' + response.status + '）。')); return; }
  await load();
  message('ok', '已吊销，立即生效。');
}

load();
</script></body></html>"""


_MEMBERS_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>成员管理 · CWK 知识库</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7;
  --ok:#1a7f4b;--bad:#b3261e;--warn:#a86a00;--owner:#2b5bd7;--writer:#0d7a68;--reader:#5d6a7e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:2rem 1.25rem 3rem}
h1{font-size:1.4rem;margin:0 0 .3rem}
.lede{color:var(--muted);margin:0 0 1.5rem;font-size:.92rem}
.who{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:.8rem;margin-bottom:1.5rem}
.who b{font-weight:600}
.who a{color:var(--accent);text-decoration:none;font-size:.9rem}
.banks{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.2rem}
.banks button{border:1px solid var(--line);background:var(--card);border-radius:8px;padding:.45rem .9rem;
  cursor:pointer;font:inherit;color:var(--muted)}
.banks button[aria-pressed=true]{border-color:var(--accent);color:var(--accent);font-weight:600}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.1rem 1.2rem;margin-bottom:1rem}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:.55rem .5rem;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:.8rem;color:var(--muted);font-weight:600}
.role{display:inline-block;border-radius:99px;padding:.1rem .6rem;font-size:.78rem;font-weight:600}
.role.owner{background:#e7eefc;color:var(--owner)} .role.writer{background:#dff3ee;color:var(--writer)}
.role.reader{background:#eaedf2;color:var(--reader)}
select,button.act{font:inherit;padding:.3rem .5rem;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button.act{cursor:pointer;background:var(--card)}
button.act:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button.act:disabled,select:disabled{opacity:.45;cursor:not-allowed}
button.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
.add{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:1rem;padding-top:1rem;border-top:1px dashed var(--line)}
.msg{padding:.75rem 1rem;border-radius:8px;font-size:.9rem;margin-bottom:1rem}
.msg.bad{background:#fbe9e7;color:var(--bad)} .msg.ok{background:#e6f4ec;color:var(--ok)}
.msg.warn{background:#fdf1dd;color:var(--warn)}
.hint{color:var(--muted);font-size:.85rem;margin:.6rem 0 0}
.svc{color:var(--muted);font-size:.82rem}
@media (prefers-color-scheme:dark){
  :root{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff}
  .role.owner{background:#1c2a4a} .role.writer{background:#133a33} .role.reader{background:#232c3d}
  .msg.bad{background:#3a1f1d} .msg.ok{background:#16301f} .msg.warn{background:#332715}
}
</style></head>
<body><div class='wrap'>
<h1>成员管理</h1>
<p class='lede'>谁能查哪个库，在这里改。改完约十秒生效，不用重发令牌。</p>
<div class='who'><span id='me'>正在确认身份…</span><a href='/register'>注册页</a></div>
<div id='msg'></div>
<div class='banks' id='banks'></div>
<div id='panel'></div>
<script>
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const ROLE_CN = {owner: '所有者', writer: '可写', reader: '只读'};
let state = {version: 0, banks: [], persons: [], me: null, current: null};

function message(kind, text){
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}

async function load(keepMessage){
  let response;
  try { response = await fetch('/api/members', {cache: 'no-store'}); }
  catch (err) { message('bad', '连不上服务，请稍后重试。'); return; }
  if (response.status === 401){
    $('me').textContent = '还没登录';
    message('warn', '请先到<a href="/register">注册页</a>用本人工作协同 Key 登录，再回到这一页。');
    return;
  }
  if (!response.ok){ message('bad', '读取失败（' + response.status + '）。'); return; }
  const data = await response.json();
  state.version = data.version;
  state.banks = data.banks;
  state.persons = data.persons;
  state.me = data.me;
  $('me').innerHTML = '当前身份：<b>' + esc(data.me.name) + '</b>' + (data.me.admin ? '（管理员）' : '');
  if (!data.banks.length){
    $('banks').innerHTML = '';
    $('panel').innerHTML = "<div class='panel'>你目前不是任何库的所有者，没有可管理的库。</div>";
    return;
  }
  if (!state.banks.some((b) => b.bank_id === state.current)) state.current = data.banks[0].bank_id;
  $('banks').innerHTML = data.banks.map((b) =>
    "<button type='button' data-bank='" + esc(b.bank_id) + "' aria-pressed='" + (b.bank_id === state.current) + "'>"
    + esc(b.name) + '</button>').join('');
  for (const button of $('banks').querySelectorAll('button')){
    button.addEventListener('click', () => { state.current = button.dataset.bank; render(); });
  }
  if (!keepMessage) message('', '');
  render();
}

function render(){
  const bank = state.banks.find((b) => b.bank_id === state.current);
  if (!bank) return;
  for (const button of $('banks').querySelectorAll('button')){
    button.setAttribute('aria-pressed', String(button.dataset.bank === state.current));
  }
  const admin = state.me.admin;
  const rows = bank.members.map((m) => {
    const service = String(m.principal).startsWith('service:');
    const isOwner = m.role === 'owner';
    const isMe = m.principal === state.me.principal;
    // 所有者不能动别的所有者，只能降级自己；服务只有管理员能动。这两条由服务端强制，
    // 这里只是不给按钮，省得点了再被拒。
    const locked = (service && !admin) || (isOwner && !admin && !isMe);
    const name = service ? "<span class='svc'>服务 · " + esc(m.principal.slice(8)) + '</span>' : esc(m.name || m.principal);
    const options = ['owner','writer','reader'].map((r) =>
      "<option value='" + r + "'" + (r === m.role ? ' selected' : '') + ">" + ROLE_CN[r] + '</option>').join('');
    return '<tr><td>' + name + (isMe ? " <span class='svc'>（你）</span>" : '') + "</td>"
      + "<td><span class='role " + esc(m.role) + "'>" + (ROLE_CN[m.role] || esc(m.role)) + '</span></td>'
      + "<td><select data-p='" + esc(m.principal) + "'" + (locked ? ' disabled' : '') + '>' + options + '</select></td>'
      + "<td><button class='act danger' data-remove='" + esc(m.principal) + "'" + (locked ? ' disabled' : '') + '>移除</button></td></tr>';
  }).join('');
  const existing = new Set(bank.members.map((m) => m.principal));
  const choices = state.persons.filter((p) => !existing.has(p.principal))
    .map((p) => "<option value='" + esc(p.principal) + "'>" + esc(p.name || p.principal) + '</option>').join('');
  $('panel').innerHTML = "<div class='panel'>"
    + '<table><thead><tr><th>成员</th><th>当前角色</th><th>改为</th><th></th></tr></thead><tbody>'
    + rows + '</tbody></table>'
    + (choices
        ? "<div class='add'><select id='who'>" + choices + "</select>"
          + "<select id='role'><option value='reader'>只读</option><option value='writer'>可写</option><option value='owner'>所有者</option></select>"
          + "<button class='act' id='add'>加入这个库</button></div>"
        : "<p class='hint'>人员目录里没有其他可加的人。让同事先到注册页用本人 Key 报到。</p>")
    + "<p class='hint'>改动约十秒生效。所有者不能降级其他所有者；库至少要保留一位所有者。</p>"
    + '</div>';
  for (const select of $('panel').querySelectorAll('select[data-p]')){
    select.addEventListener('change', () => write(select.dataset.p, select.value));
  }
  for (const button of $('panel').querySelectorAll('button[data-remove]')){
    button.addEventListener('click', () => {
      const principal = button.dataset.remove;
      if (window.confirm('确定把这个人从「' + bank.name + '」移除吗？')) write(principal, null);
    });
  }
  const add = $('add');
  if (add) add.addEventListener('click', () => write($('who').value, $('role').value));
}

async function write(principal, role){
  message('warn', '正在保存…');
  let response;
  try {
    response = await fetch('/api/members', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CWK-Members': '1'},
      body: JSON.stringify({bank_id: state.current, principal: principal, role: role,
                            expect_version: state.version}),
      cache: 'no-store'
    });
  } catch (err) { message('bad', '连不上服务，改动没有保存。'); return; }
  let data = {};
  try { data = await response.json(); } catch (err) {}
  if (response.status === 409 && data.error === 'version_conflict'){
    message('warn', '这份名单刚被别人改过，已为你刷新，请确认后重试。');
    await load(true);
    return;
  }
  if (response.status === 409){
    // 规则不允许：重试无用，把服务端给的原因原样说清楚。
    message('bad', esc(data.message || '这个改动不被允许。'));
    await load(true);
    return;
  }
  if (!response.ok){
    message('bad', esc(data.message || '保存失败（' + response.status + '）。'));
    await load(true);
    return;
  }
  await load(true);
  message('ok', role ? '已保存，约十秒后生效。' : '已移除，约十秒后生效。');
}

load();
</script></body></html>"""


_REGISTER_INSECURE_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>请用加密地址打开 · CWK 知识库</title>
<style>
body{margin:0;background:#f5f6fa;color:#16202f;font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:520px;margin:0 auto;padding:3rem 1.25rem}
h1{font-size:1.35rem;margin:0 0 .6rem}
p{color:#5b6880;margin:0 0 .8rem}
.box{background:#fdf1dd;border:1px solid #e7cd9b;color:#8a5600;border-radius:9px;padding:1rem 1.15rem}
@media (prefers-color-scheme:dark){body{background:#11151d;color:#e6ebf4}p{color:#94a1b8}
  .box{background:#332715;border-color:#5b4620;color:#e9bf6a}}
</style></head>
<body><div class='wrap'>
<h1>请用加密地址打开本页</h1>
<div class='box'>这一页要你填本人的工作协同 Key。现在这条连接没有加密，
  Key 会以明文经过公司网络，所以这里<strong>不显示输入框</strong>。</div>
<p>请把地址栏里的 <code>http://</code> 换成 <code>https://</code> 重新打开；
   打不开或提示证书问题，找知识库管理员要正确的地址。</p>
<p>管理员本机调试可以设置 <code>KB_REGISTER_ALLOW_HTTP=true</code> 临时放行，
   生产环境不要开。</p>
</div></body></html>"""


_REGISTER_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>注册 · CWK 知识库</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7;--ok:#1a7f4b;--bad:#b3261e;--warn:#a86a00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:520px;margin:0 auto;padding:2.5rem 1.25rem 3rem}
h1{font-size:1.45rem;margin:0 0 .4rem}
.lede{color:var(--muted);margin:0 0 1.5rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1.2rem 1.25rem}
label{font-size:.9rem;color:var(--muted);display:block;margin-bottom:.35rem}
input[type=password]{width:100%;padding:.65rem .75rem;border:1px solid var(--line);border-radius:7px;font:inherit;background:var(--bg);color:var(--ink)}
button{margin-top:.85rem;padding:.55rem 1.1rem;border:1px solid var(--accent);border-radius:7px;background:var(--accent);color:#fff;cursor:pointer;font:inherit;font-weight:600}
button:hover{opacity:.92}
button:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--muted);font-size:.85rem;margin-top:.7rem}
.msg{padding:.8rem 1rem;border-radius:8px;font-size:.9rem;margin:1rem 0 0}
.msg.bad{background:#fbe9e7;color:var(--bad)} .msg.ok{background:#e6f4ec;color:var(--ok)} .msg.warn{background:#fdf1dd;color:var(--warn)}
.nav{margin-bottom:1.5rem;font-size:.9rem}
.nav a{color:var(--accent);text-decoration:none;margin-right:1rem}
@media (prefers-color-scheme:dark){
  :root{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff}
  .msg.bad{background:#3a1f1d} .msg.ok{background:#16301f} .msg.warn{background:#332715}
}
</style></head>
<body><div class='wrap'>
<div class='nav'><a href='/console'>管理控制台</a></div>
<h1>注册</h1>
<p class='lede'>填入你本人的工作协同 Key。系统只用来确认你是谁，不会保存这串 Key。</p>
<div class='panel' id='box'>
  <label for='key'>工作协同 Key</label>
  <input type='password' id='key' autocomplete='off' placeholder='粘贴后点注册'>
  <button type='button' id='go'>注册</button>
  <p class='hint'>请使用加密网址（https）打开本页。Key 只在本次请求里使用，刷新后输入框是空的。</p>
</div>
<div id='msg'></div>
<script>
'use strict';
// Key 只活在这次提交里：不进 cookie、不进本地存储、不进 URL。
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

function message(kind, text){
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}

async function showSession(){
  try {
    const response = await fetch('/api/session', {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    message('ok', '你已注册并登录为 <strong>' + esc(data.name) + '</strong>。');
    $('key').disabled = true;
    $('go').disabled = true;
  } catch (err) {}
}

$('go').addEventListener('click', async () => {
  const value = $('key').value;
  if (!value) { message('warn', '请先粘贴你的工作协同 Key。'); return; }
  $('go').disabled = true;
  message('warn', '正在核实…');
  let response;
  try {
    response = await fetch('/api/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({app_key: value}),
      cache: 'no-store'
    });
  } catch (err) {
    $('go').disabled = false;
    message('bad', '连不上服务，请稍后重试。');
    return;
  }
  $('key').value = '';
  let data = {};
  try { data = await response.json(); } catch (err) {}
  if (response.status === 403 && data.error === 'https_required') {
    $('go').disabled = false;
    message('bad', '请使用加密网址（https）打开本页后再注册。');
    return;
  }
  if (response.status === 401) {
    $('go').disabled = false;
    message('bad', '无法确认这把 Key 对应的人，请检查后重试。');
    return;
  }
  if (!response.ok) {
    $('go').disabled = false;
    message('bad', '注册失败（' + response.status + '）。');
    return;
  }
  message('ok', '注册成功：<strong>' + esc(data.name) + '</strong>。你已登录。');
  $('key').disabled = true;
});

$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('go').click(); });
showSession();
</script></body></html>"""


_HTML = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>知识库管理控制台</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7;--ok:#1a7f4b;--warn:#a86a00;--bad:#b3261e;--head:#eef1f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:1.5rem 1.25rem 3rem}
h1{font-size:1.35rem;margin:0 0 .25rem}
.env{color:var(--muted);font-size:.85rem;margin-bottom:1.25rem}
.env code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.env .remote{color:var(--warn);font-weight:600}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.15rem;margin-bottom:1.1rem}
label{font-size:.9rem;color:var(--muted);display:block;margin-bottom:.35rem}
input[type=password]{width:min(340px,100%);padding:.5rem .65rem;border:1px solid var(--line);border-radius:7px;font:inherit;background:var(--bg);color:var(--ink)}
button{padding:.5rem .9rem;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink);cursor:pointer;font:inherit}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{opacity:.9;color:#fff}
button[aria-selected=true]{border-color:var(--accent);color:var(--accent);font-weight:600}
button:disabled{opacity:.45;cursor:not-allowed}
.tabs{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--head);font-weight:600;color:var(--muted);font-size:.82rem;text-transform:none}
td.mono,th.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.tag{display:inline-block;padding:.1rem .5rem;border-radius:99px;font-size:.78rem;font-weight:600}
.tag.ok{background:#e6f4ec;color:var(--ok)} .tag.warn{background:#fdf1dd;color:var(--warn)} .tag.bad{background:#fbe9e7;color:var(--bad)} .tag.mute{background:var(--head);color:var(--muted)}
h2{font-size:1rem;margin:1.25rem 0 .6rem}
h2:first-child{margin-top:0}
.msg{padding:.8rem 1rem;border-radius:8px;font-size:.9rem;margin-bottom:1rem}
.msg.bad{background:#fbe9e7;color:var(--bad)} .msg.warn{background:#fdf1dd;color:var(--warn)} .msg.mute{background:var(--head);color:var(--muted)}
.hint{color:var(--muted);font-size:.85rem;margin-top:.6rem}
.scroll{overflow-x:auto}
@media (prefers-color-scheme:dark){
  :root{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff;--head:#222b39;--ok:#5fd39b;--warn:#e0b060;--bad:#ff8a80}
  .tag.ok{background:#16301f} .tag.warn{background:#332715} .tag.bad{background:#3a1f1d} .msg.bad{background:#3a1f1d} .msg.warn{background:#332715}
}
</style></head>
<body><div class='wrap'>

<h1>知识库管理控制台</h1>
<div class='env' id='env'></div>

<div class='panel' id='lock'>
  <label for='key'>管理密钥（只保存在当前页面内存里，刷新即失效）</label>
  <div style='display:flex;gap:.5rem;flex-wrap:wrap'>
    <input type='password' id='key' autocomplete='off' placeholder='输入后解锁三个视图'>
    <button class='primary' id='unlock'>解锁</button>
    <button id='relock' hidden>锁定</button>
  </div>
  <div class='hint'>解锁后三个视图共用这把密钥，不再逐次询问。关闭或刷新页面即清除。
    同事请走 <a href='/register'>注册页</a>（用本人工作协同 Key）。</div>
</div>

<div class='tabs' id='tabs' hidden>
  <button data-view='overview' aria-selected='true'>库概览</button>
  <button data-view='services'>服务健康</button>
  <button data-view='audit'>审计记录</button>
  <button id='refresh'>刷新</button>
</div>

<div id='msg'></div>
<div id='body'></div>

</div>
<script>
'use strict';
// 密钥只活在这个变量里：不进任何浏览器存储、不进 cookie、不进 URL。
// 页面源码里连这些 API 的名字都不该出现，测试按字面断言。
let key = null;
let view = 'overview';

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v === null || v === undefined || v === '' ? '—' : v);

function renderEnv(){
  const host = location.host || '(file)';
  const local = /^(127\\.0\\.0\\.1|localhost|\\[::1\\])(:\\d+)?$/.test(host);
  $('env').innerHTML = '当前访问的是 <code>' + esc(host) + '</code>'
    + (local ? '（本机回环）' : ' <span class="remote">（非本机地址，请确认你面对的是哪台服务器）</span>');
}

function message(kind, text){
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + esc(text) + '</div>' : '';
}

function tag(value, kind){ return '<span class="tag ' + kind + '">' + esc(value) + '</span>'; }

function statusTag(value){
  const v = String(value || '').toLowerCase();
  if (['ok','healthy','active','available','ready'].includes(v)) return tag(value, 'ok');
  if (['revoked','unhealthy','unavailable','expired','error'].includes(v)) return tag(value, 'bad');
  if (['unknown','stale','pending'].includes(v)) return tag(value, 'warn');
  return tag(value, 'mute');
}

function table(columns, rows, render){
  if (!rows || !rows.length) return '<p class="hint">没有数据。</p>';
  const head = columns.map((c) => '<th' + (c.mono ? " class='mono'" : '') + '>' + c.label + '</th>').join('');
  const body = rows.map((row) => '<tr>' + render(row).map((cell, i) =>
    '<td' + (columns[i].mono ? " class='mono'" : '') + '>' + cell + '</td>').join('') + '</tr>').join('');
  return '<div class="scroll"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
}

const views = {
  overview(data){
    const sourceLabel = {snapshot: '快照', 'library-tree': '目录树'};
    const libs = table(
      [{label:'知识库', mono:true},{label:'来源'},{label:'文档数'},{label:'可读'},{label:'词法索引'},{label:'是否最新'},{label:'异常'}],
      data.libraries,
      (r) => [esc(r.kb_id), tag(sourceLabel[r.source] || r.source || '—', 'mute'),
              esc(r.total), esc(r.readable_total), statusTag(r.lexical_status),
              r.up_to_date === null || r.up_to_date === undefined ? '—' : (r.up_to_date ? tag('是','ok') : tag('否','warn')),
              r.error ? tag(r.error,'bad') : '—']);
    const tokens = table(
      [{label:'令牌', mono:true},{label:'授权库', mono:true},{label:'状态'},{label:'签发'},{label:'到期'},{label:'剩余天数'}],
      data.tokens,
      (r) => [esc(r.token_id), (r.scope || []).map((s) => esc(s)).join('<br>') || '—', statusTag(r.status),
              esc(r.created_at), esc(r.expires_at), esc(r.remaining_days)]);
    const active = (data.tokens || []).filter((t) => t.status === 'active').length;
    return '<div class="panel"><h2>知识库（' + (data.libraries || []).length + '）</h2>' + libs
      + '<h2>访问令牌（有效 ' + active + ' / 共 ' + (data.tokens || []).length + '）</h2>' + tokens
      + '<p class="hint">登记表状态：' + esc(data.registry_status)
      + '；投影完整性：' + (data.complete ? '完整' : '不完整') + '。令牌只显示末位标识，不含明文或摘要。</p></div>';
  },
  services(data){
    return '<div class="panel"><h2>服务健康</h2>' + table(
      [{label:'服务'},{label:'地址', mono:true},{label:'状态'},{label:'HTTP'}],
      data.services,
      (r) => [esc(r.name), esc(r.address), statusTag(r.status), esc(r.http_status)])
      + '<p class="hint">探测超时 ' + esc(data.timeout_seconds) + ' 秒；本页只做健康探测，不经过它们查询任何内容。</p></div>';
  },
  audit(data){
    return '<div class="panel"><h2>管理审计（最近 ' + (data.events || []).length + ' 条）</h2>' + table(
      [{label:'时间', mono:true},{label:'动作'},{label:'结果'},{label:'HTTP'}],
      (data.events || []).slice().reverse(),
      (r) => [esc(r.timestamp), esc(r.action), statusTag(r.outcome === 'ok' ? 'ok' : r.outcome), esc(r.status)])
      + '<p class="hint">只记录时间、动作、结果和状态码，不记录密钥、参数或返回内容。</p></div>';
  }
};

async function load(){
  if (!key) return;
  message('mute', '加载中…');
  let response;
  try {
    response = await fetch('/api/' + view, {headers: {'X-KB-Admin-Key': key}, cache: 'no-store'});
  } catch (err) {
    message('bad', '连不上管理服务——进程可能没在运行，或者隧道断了。');
    $('body').innerHTML = '';
    return;
  }
  if (response.status === 401) {
    message('bad', '密钥不对，或者管理台没有启用——服务对这两种情况返回同样的 401，请两个都查一下。');
    $('body').innerHTML = '';
    return;
  }
  if (response.status === 403) { message('warn', '这个操作被写开关拦住了（403）。'); $('body').innerHTML = ''; return; }
  if (!response.ok) { message('bad', '服务返回 ' + response.status + '，不是鉴权问题。'); $('body').innerHTML = ''; return; }
  let data;
  try { data = await response.json(); } catch (err) { message('bad', '服务返回的不是合法 JSON。'); return; }
  message('', '');
  $('body').innerHTML = views[view](data);
}

$('unlock').addEventListener('click', () => {
  const value = $('key').value;
  if (!value) { message('warn', '先输入管理密钥。'); return; }
  key = value;
  $('key').value = '';
  $('key').disabled = true;
  $('unlock').disabled = true;
  $('relock').hidden = false;
  $('tabs').hidden = false;
  load();
});

$('relock').addEventListener('click', () => {
  key = null;
  $('key').disabled = false;
  $('unlock').disabled = false;
  $('relock').hidden = true;
  $('tabs').hidden = true;
  $('body').innerHTML = '';
  message('mute', '已锁定，密钥已从页面清除。');
});

$('key').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('unlock').click(); });

$('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-view]');
  if (!button) return;
  view = button.dataset.view;
  for (const other of $('tabs').querySelectorAll('button[data-view]')) {
    other.setAttribute('aria-selected', String(other === button));
  }
  load();
});

$('refresh').addEventListener('click', load);
renderEnv();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server: "AdminHTTPServer"

    def _reply(self, status: int, payload: dict[str, Any], headers: Mapping[str, str] | None = None) -> None:
        data = payload.get("html", "") if "html" in payload else _json_bytes(payload)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", (headers or {}).get("Content-Type", "application/json; charset=utf-8"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            if key.lower() != "content-type":
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _request(self) -> None:
        try:
            length = min(MAX_BODY, max(0, int(self.headers.get("Content-Length", "0") or 0)))
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        try:
            status, payload, headers = self.server.app.handle(
                self.command,
                self.path,
                self.headers,
                body,
                direct_tls=isinstance(self.connection, ssl.SSLSocket),
                client=str(self.client_address[0]) if self.client_address else "",
            )
        except Exception:
            status, payload, headers = 500, {"error": "internal_error"}, {}
        self._reply(status, payload, headers)

    do_GET = _request
    do_POST = _request

    def log_message(self, format: str, *args: Any) -> None:
        return


class AdminHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: AdminApp, *, tls: ssl.SSLContext | None = None):
        self.app = app
        super().__init__(address, _Handler)
        if tls is not None:
            # Wrap after binding so a certificate problem fails at startup,
            # not on the first person who tries to register.
            self.socket = tls.wrap_socket(self.socket, server_side=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CWK KB admin MVP")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--key-env", default=None, help="环境变量名，不是密钥值")
    parser.add_argument("--face", choices=FACES, default=None,
                        help="admin=管理面（默认，回环）；register=注册面（可对局域网，不含任何管理接口）")
    parser.add_argument("--tls-cert", default=None, help="证书文件路径（给注册面直接跑 https）")
    parser.add_argument("--tls-key", default=None, help="私钥文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.key_env is not None:
        if not _safe_name(args.key_env):
            parser.error("--key-env must be an environment variable name")
        os.environ[ENV_KEY_NAME] = args.key_env
    if args.face is not None:
        os.environ[ENV_FACE] = args.face
    cert = args.tls_cert or os.environ.get(ENV_TLS_CERT) or ""
    key = args.tls_key or os.environ.get(ENV_TLS_KEY) or ""
    if bool(cert) != bool(key):
        parser.error("--tls-cert 与 --tls-key 必须成对给出")
    tls = None
    if cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(certfile=cert, keyfile=key)
    app = AdminApp()
    if app.face == FACE_REGISTER and tls is None and not app.allow_http_register:
        parser.error("注册面必须给 --tls-cert/--tls-key；本机联调可设 KB_REGISTER_ALLOW_HTTP=true")
    server = AdminHTTPServer((args.host, args.port), app, tls=tls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
