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
  <div class='hint'>解锁后三个视图共用这把密钥，不再逐次询问。关闭或刷新页面即清除。</div>
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
    const libs = table(
      [{label:'知识库', mono:true},{label:'文档数'},{label:'可读'},{label:'词法索引'},{label:'是否最新'},{label:'异常'}],
      data.libraries,
      (r) => [esc(r.kb_id), esc(r.total), esc(r.readable_total), statusTag(r.lexical_status),
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
