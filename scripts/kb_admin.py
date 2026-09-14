#!/usr/bin/env python3
"""RT-056: small, disabled-by-default KB administration face.

This module is intentionally stdlib-only.  It has no KB write imports: job
routes only append a bounded audit event and never invoke an ingest/create
primitive.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

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
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
MAX_AUDIT_EVENTS = 100
MAX_BODY = 4096


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _safe_name(value: str) -> str:
    return value if ENV_NAME.fullmatch(value) else ""


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


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
        self._audit_lock = threading.Lock()

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
            return {
                "schema": "cwk.kb.admin.overview.v1",
                "ok": bool(payload.get("ok")),
                "complete": bool(payload.get("complete")),
                "registry_status": payload.get("registry_status", "unavailable"),
                "libraries": payload.get("libraries", []),
                "tokens": tokens,
            }
        except Exception:
            return {"schema": "cwk.kb.admin.overview.v1", "ok": False, "complete": False, "registry_status": "unavailable", "libraries": [], "tokens": []}
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

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes = b"") -> tuple[int, dict[str, Any], dict[str, str]]:
        if method not in ("GET", "POST"):
            return 405, {"error": "method_not_allowed"}, {"Allow": "GET, POST"}
        route = urllib.parse.urlsplit(path).path
        if route == "/healthz":
            return (200 if self.enabled else 503), {"schema": "cwk.kb.admin.health.v1", "enabled": self.enabled, "status": "ok" if self.enabled else "disabled"}, {}
        if route == "/":
            return 200, {"html": _HTML}, {"Content-Type": "text/html; charset=utf-8"}
        if not route.startswith("/api/"):
            return 404, {"error": "not_found"}, {}
        if not self._authorized(headers):
            return 401, {"error": "unauthorized"}, {}
        if route == "/api/overview" and method == "GET":
            self._audit("overview", "ok", 200)
            return 200, self._overview(), {}
        if route == "/api/services" and method == "GET":
            self._audit("services", "ok", 200)
            return 200, self._services(), {}
        if route == "/api/audit" and method == "GET":
            return 200, self._audit_read(), {}
        if route in ("/api/jobs/create", "/api/jobs/ingest") and method == "POST":
            action = route.rsplit("/", 1)[-1]
            if not self.write_enabled:
                self._audit(action, "rejected_write_disabled", 403)
                return 403, {"error": "write_disabled", "job": action}, {}
            self._audit(action, "placeholder_not_implemented", 501)
            return 501, {"error": "not_implemented", "job": action, "recorded": True}, {}
        return 404, {"error": "not_found"}, {}


_HTML = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>KB Admin</title><style>body{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;background:#f6f7fb;color:#172033}main{background:#fff;border:1px solid #dde2ec;border-radius:12px;padding:1.5rem;box-shadow:0 2px 12px #18233a12}button{margin:.3rem;padding:.5rem .8rem;border:1px solid #9aa8bf;border-radius:6px;background:#fff;cursor:pointer}pre{white-space:pre-wrap;overflow:auto;background:#f0f3f8;padding:1rem;border-radius:8px}</style></head><body><main><h1>知识库管理台</h1><p>只读概览与服务健康检查；写入操作仍由受控后台流程负责。</p><p><button onclick="get('/api/overview')">库概览</button><button onclick="get('/api/services')">服务健康</button><button onclick="get('/api/audit')">审计记录</button></p><pre id='out'>请选择一个视图。</pre></main><script>async function get(p){const k=prompt('管理密钥（不会保存）');if(k===null)return;const r=await fetch(p,{headers:{'X-KB-Admin-Key':k}});document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2)}</script></body></html>"""


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
            status, payload, headers = self.server.app.handle(self.command, self.path, self.headers, body)
        except Exception:
            status, payload, headers = 500, {"error": "internal_error"}, {}
        self._reply(status, payload, headers)

    do_GET = _request
    do_POST = _request

    def log_message(self, format: str, *args: Any) -> None:
        return


class AdminHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: AdminApp):
        self.app = app
        super().__init__(address, _Handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CWK KB admin MVP")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--key-env", default=None, help="环境变量名，不是密钥值")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.key_env is not None:
        if not _safe_name(args.key_env):
            parser.error("--key-env must be an environment variable name")
        os.environ[ENV_KEY_NAME] = args.key_env
    app = AdminApp()
    server = AdminHTTPServer((args.host, args.port), app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
