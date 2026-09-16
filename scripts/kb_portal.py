#!/usr/bin/env python3
"""RT-058: the LAN-facing product portal for the KB service.

Why this is a separate process from :mod:`kb_admin` rather than one more
route on it.  The portal is the only face here meant to be reachable from
the office network; the console is not.  Sharing a process would mean
sharing a bind address, so making the portal visible would drag the
management API into the same exposure.  Keeping them apart lets the portal
bind wherever it is useful while the console keeps its loopback default.

The stronger property is what this module *cannot* do: it imports no
registry, no storage backend, no ``kb_ops``.  There is no code path from an
HTTP request to library contents, token metadata or an audit file, so an
exposed portal cannot leak management data — that is a structural fact
about the imports, not a promise about the routing table, and
``tests/test_rt058_kb_console.py`` asserts it stays that way.

Every page is static: no query is answered, no service is probed, nothing
is written.  Configuration arrives through the environment so a deployment
can point the console link at whatever the operator actually uses.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8792
ENV_HOST = "KB_PORTAL_HOST"
ENV_PORT = "KB_PORTAL_PORT"
ENV_CONSOLE_URL = "KB_PORTAL_CONSOLE_URL"
ENV_BANKS = "KB_PORTAL_BANKS"
ENV_RUNBOOK_URL = "KB_PORTAL_RUNBOOK_URL"
ENV_CONTACT = "KB_PORTAL_CONTACT"

# Display-only defaults.  These names are already public inside the company
# (they appear in the access runbook and in the query skill); the portal adds
# no information that an authorized reader does not already have.
DEFAULT_BANKS: tuple[tuple[str, str], ...] = (
    ("cwork-3m", "工作协同近三个月的汇报、待办与回复链"),
    ("docdb-touqian", "投前资料"),
    ("spbp-2027", "2027 集团 SP&BP"),
)

# A console URL that is not loopback is a deployment choice, not a default:
# the console's own default binding is 127.0.0.1, so the honest default link
# is the one that works from the machine the console runs on.
DEFAULT_CONSOLE_URL = "http://127.0.0.1:8791/console"


def parse_banks(raw: str | None) -> list[tuple[str, str]]:
    """``id:说明`` pairs, comma separated.  Malformed entries are dropped.

    A bank whose description is missing still shows up — an operator who
    lists a bank should see it on the page even if they skipped the prose,
    rather than silently losing it.
    """
    if not (raw or "").strip():
        return list(DEFAULT_BANKS)
    parsed: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        bank_id, _, description = item.partition(":")
        bank_id = bank_id.strip()
        if bank_id:
            parsed.append((bank_id, description.strip()))
    return parsed or list(DEFAULT_BANKS)


class PortalApp:
    """Request-independent application object, convenient for real HTTP tests."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.env = dict(environ if environ is not None else os.environ)
        self.banks = parse_banks(self.env.get(ENV_BANKS))
        self.console_url = (self.env.get(ENV_CONSOLE_URL) or DEFAULT_CONSOLE_URL).strip()
        self.runbook_url = (self.env.get(ENV_RUNBOOK_URL) or "").strip()
        self.contact = (self.env.get(ENV_CONTACT) or "知识库管理员").strip()

    def _safe_url(self, url: str) -> str:
        """Only http/https links reach the page.

        The console link is operator-supplied configuration; letting a
        ``javascript:`` or ``data:`` URL through would turn a config file
        into script execution in an admin's browser.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return ""
        return url if parsed.scheme in ("http", "https") and parsed.netloc else ""

    def page(self) -> str:
        console = self._safe_url(self.console_url)
        runbook = self._safe_url(self.runbook_url)
        banks = "".join(
            "<article class='bank'><h3>{}</h3><p>{}</p></article>".format(
                html.escape(bank_id), html.escape(description or "—")
            )
            for bank_id, description in self.banks
        )
        console_block = (
            "<a class='cta' href='{}'>进入管理控制台</a>".format(html.escape(console, quote=True))
            if console
            else "<span class='cta disabled'>管理控制台未配置</span>"
        )
        runbook_block = (
            "<p>完整接入步骤见 <a href='{}'>接入手册</a>。</p>".format(html.escape(runbook, quote=True))
            if runbook
            else "<p>完整接入步骤向管理员索取接入手册。</p>"
        )
        return _PAGE.format(
            banks=banks,
            console_block=console_block,
            runbook_block=runbook_block,
            contact=html.escape(self.contact),
        )

    def handle(self, method: str, path: str) -> tuple[int, str, bytes, dict[str, str]]:
        """Return ``(status, content_type, body, headers)``.

        Anything that is not the portal page or the health probe is a 404 —
        including ``/api/*``, which exists only on the console.  A request
        that wanders here looking for management data must not get a hint
        that some other shape of request would have worked.
        """
        if method not in ("GET", "HEAD"):
            return 405, "application/json", _json({"error": "method_not_allowed"}), {"Allow": "GET, HEAD"}
        route = urllib.parse.urlsplit(path).path
        if route == "/healthz":
            return 200, "application/json", _json({"schema": "cwk.kb.portal.health.v1", "status": "ok"}), {}
        if route in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", self.page().encode("utf-8"), {}
        return 404, "application/json", _json({"error": "not_found"}), {}


def _json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


_PAGE = """<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>CWK 知识库服务</title>
<style>
:root{{--bg:#f5f6fa;--card:#fff;--ink:#16202f;--muted:#5b6880;--line:#dfe4ee;--accent:#2b5bd7}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}}
header{{background:linear-gradient(160deg,#1d2b45,#2b3f66);color:#fff;padding:3.5rem 1.25rem 3rem}}
header div,main{{max-width:880px;margin:0 auto}}
header h1{{margin:0 0 .6rem;font-size:2rem;letter-spacing:.01em}}
header p{{margin:0;color:#c9d4ea;max-width:38em}}
main{{padding:2.25rem 1.25rem 4rem}}
section{{margin-bottom:2.5rem}}
h2{{font-size:1.15rem;margin:0 0 .9rem;padding-bottom:.5rem;border-bottom:1px solid var(--line)}}
.banks{{display:grid;gap:.85rem;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}}
.bank{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.1rem}}
.bank h3{{margin:0 0 .35rem;font-size:.95rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent)}}
.bank p{{margin:0;color:var(--muted);font-size:.9rem}}
ol{{padding-left:1.3rem}} ol li{{margin-bottom:.5rem}}
.cta{{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;padding:.7rem 1.4rem;border-radius:8px;font-weight:600}}
.cta.disabled{{background:#9aa5ba}}
.note{{color:var(--muted);font-size:.88rem;margin-top:.7rem}}
footer{{border-top:1px solid var(--line);padding:1.5rem 1.25rem;color:var(--muted);font-size:.85rem}}
footer div{{max-width:880px;margin:0 auto}}
a{{color:var(--accent)}}
@media (prefers-color-scheme:dark){{
  :root{{--bg:#11151d;--card:#1a202b;--ink:#e6ebf4;--muted:#94a1b8;--line:#2a3342;--accent:#7aa2ff}}
}}
</style></head>
<body>
<header><div>
<h1>CWK 知识库服务</h1>
<p>把授权范围内的工作资料做成可检索、可追溯的知识库。Agent 在局域网内提问，
每个结论都能回到原文，找不到就明确说找不到。</p>
</div></header>
<main>

<section>
<h2>当前可用的知识库</h2>
<div class='banks'>{banks}</div>
</section>

<section>
<h2>怎么接入</h2>
<ol>
<li>在你的 Agent 上安装知识库查询 Skill。</li>
<li>向{contact}申请访问令牌，说明需要哪几个库、用在哪台机器。</li>
<li>令牌按 Agent 实例发放、按库授权；拿到后先做一次健康检查再正式使用。</li>
</ol>
{runbook_block}
<p class='note'>访问令牌不要写进命令行、日志或聊天记录。令牌失效返回 401，
访问未授权的库返回 403——后者是库间隔离正常生效，不是故障。</p>
</section>

<section>
<h2>管理员入口</h2>
<p>{console_block}</p>
<p class='note'>管理控制台默认只监听本机，需要通过隧道或在服务器本机访问，并且需要管理密钥。
本页面不持有任何管理数据。</p>
</section>

</main>
<footer><div>CWK 知识库服务 · 内部使用 · 本页面为静态说明，不读取任何库内容或令牌信息</div></footer>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server: "PortalHTTPServer"
    server_version = "CWKPortal"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        self._respond(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        self._respond(include_body=False)

    def _respond(self, *, include_body: bool) -> None:
        status, content_type, body, headers = self.server.app.handle(self.command, self.path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The portal is static and public-by-design; it still should not be
        # framed by, or sniffed into, something else.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log.

        A request line can carry a query string; on a face that anyone on the
        network can reach, the cheapest way not to log something sensitive is
        not to log the request at all.
        """
        return


class PortalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: PortalApp) -> None:
        self.app = app
        super().__init__(address, _Handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CWK 知识库内网门户（静态页面，无数据访问）")
    parser.add_argument("--host", default=os.environ.get(ENV_HOST, DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, DEFAULT_PORT)))
    args = parser.parse_args(argv)
    app = PortalApp()
    server = PortalHTTPServer((args.host, args.port), app)
    print(f"kb_portal: http://{args.host}:{args.port}/ （静态门户；控制台在别处，本进程无管理数据）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
