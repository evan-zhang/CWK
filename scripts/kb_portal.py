#!/usr/bin/env python3
"""RT-058/RT-059: the LAN-facing product site for the KB service.

Why this is a separate process from :mod:`kb_admin` rather than one more
route on it.  The site is the only face here meant to be reachable from
the office network; the console is not.  Sharing a process would mean
sharing a bind address, so making the site visible would drag the
management API into the same exposure.  Keeping them apart lets the site
bind wherever it is useful while the console keeps its loopback default.

The stronger property is what this module *cannot* do: it imports no
registry, no storage backend, no ``kb_ops``.  There is no code path from an
HTTP request to library contents, token metadata or an audit file, so an
exposed site cannot leak management data — that is a structural fact
about the imports, not a promise about the routing table, and
``tests/test_rt058_kb_console.py`` asserts it stays that way.

RT-059 turned the one-screen notice into a product site: what the service
is, what it can do, and the complete install-and-configure runbook, so a
colleague who has never used it can read it and finish their own setup.
It deliberately shows **no** library contents — not a document, not a hit,
not a count.  Every number on the page would be a number this process had
to read from somewhere, and reading is exactly what it must not do.

Every page is static: no query is answered, no service is probed, nothing
is written.  Configuration arrives through the environment so a deployment
can point the addresses at whatever the operator actually uses.
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
ENV_RETRIEVAL_BASE = "KB_PORTAL_RETRIEVAL_BASE"
ENV_ANSWER_BASE = "KB_PORTAL_ANSWER_BASE"
ENV_REPO_URL = "KB_PORTAL_REPO_URL"

# Display-only defaults.  These names are already public inside the company
# (they appear in the access runbook and in the query skill); the site adds
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
# The service addresses, in contrast, are what a reader must type to reach a
# running deployment, and the repository already documents these two in the
# access runbook and the query skill.  Keeping them as defaults means the
# commands on the page are correct without extra configuration.
DEFAULT_RETRIEVAL_BASE = "http://192.168.91.72:8787"
DEFAULT_ANSWER_BASE = "http://192.168.91.72:8790"
DEFAULT_REPO_URL = "https://github.com/evan-zhang/CWK.git"


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
        self.retrieval_base = (self.env.get(ENV_RETRIEVAL_BASE) or DEFAULT_RETRIEVAL_BASE).strip().rstrip("/")
        self.answer_base = (self.env.get(ENV_ANSWER_BASE) or DEFAULT_ANSWER_BASE).strip().rstrip("/")
        self.repo_url = (self.env.get(ENV_REPO_URL) or DEFAULT_REPO_URL).strip()

    def _safe_url(self, url: str) -> str:
        """Only http/https links reach the page.

        The console link is operator-supplied configuration; letting a
        ``javascript:`` or ``data:`` URL through would turn a config file
        into script execution in a reader's browser.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return ""
        return url if parsed.scheme in ("http", "https") and parsed.netloc else ""

    # ── page fragments ──────────────────────────────────────────────────────

    def _bank_cards(self) -> str:
        return "".join(
            "<article class='bank'><h3>{}</h3><p>{}</p></article>".format(
                html.escape(bank_id), html.escape(description or "—")
            )
            for bank_id, description in self.banks
        )

    def _bank_checklist(self) -> str:
        return "\n".join(
            "  [ ] {}{}".format(bank_id, f"（{description}）" if description else "")
            for bank_id, description in self.banks
        )

    def _first_bank(self) -> str:
        return self.banks[0][0] if self.banks else "cwork-3m"

    def _console_block(self) -> str:
        console = self._safe_url(self.console_url)
        if not console:
            return "<span class='cta ghost disabled'>管理控制台未配置</span>"
        return "<a class='cta ghost' href='{}'>打开管理控制台</a>".format(html.escape(console, quote=True))

    def _runbook_line(self) -> str:
        runbook = self._safe_url(self.runbook_url)
        if not runbook:
            return "<p class='note'>本页即完整接入手册。卡在任何一步，把该步的原始报错发给{}。</p>".format(
                html.escape(self.contact))
        return "<p class='note'>本页即完整接入手册，另有一份可发给 AI 助手代劳的<a href='{}'>文字版</a>。</p>".format(
            html.escape(runbook, quote=True))

    def _fill(self, template: str) -> str:
        """把配置填进模板。所有取值都在这里转义一次，页面片段里不再重复。"""
        replacements = {
            "{{BANK_CARDS}}": self._bank_cards(),
            "{{BANK_CHECKLIST}}": html.escape(self._bank_checklist()),
            "{{FIRST_BANK}}": html.escape(self._first_bank()),
            "{{CONSOLE_BLOCK}}": self._console_block(),
            "{{CONTACT}}": html.escape(self.contact),
            "{{RETRIEVAL}}": html.escape(self.retrieval_base),
            "{{ANSWER}}": html.escape(self.answer_base),
            "{{REPO}}": html.escape(self.repo_url),
            "{{DEPLOY_SVG}}": _DEPLOY_SVG,
            "{{FLOW_SVG}}": _FLOW_SVG,
        }
        for marker, value in replacements.items():
            template = template.replace(marker, value)
        return template

    def page(self) -> str:
        """首页：产品介绍，给没用过的人看。技术细节都在文档中心。"""
        return _shell("CWK 知识库服务", self._fill(_HOME_BODY), active="home")

    def docs_index(self) -> str:
        cards = "".join(
            "<a href='/docs/{s}'><h3>{t} →</h3><p>{d}</p></a>".format(
                s=slug, t=html.escape(title), d=html.escape(desc))
            for slug, title, desc in _DOC_NAV
        )
        body = _DOCS_INDEX_BODY.replace("{{DOC_CARDS}}", cards)
        return _shell("文档中心 · CWK 知识库服务", self._fill(body), active="docs")

    def doc(self, slug: str) -> str | None:
        """单个文档页；未知 slug 返回 None，由路由转成 404。"""
        body = _DOC_BODIES.get(slug)
        if body is None:
            return None
        title, sub = next((t, d) for s, t, d in _DOC_NAV if s == slug)
        rendered = _doc_page(slug, html.escape(title), html.escape(sub), self._fill(body))
        return _shell(f"{title} · CWK 知识库服务", rendered, active="docs")

    def handle(self, method: str, path: str) -> tuple[int, str, bytes, dict[str, str]]:
        """Return ``(status, content_type, body, headers)``.

        Anything that is not a known page or the health probe is a 404 —
        including ``/api/*``, which exists only on the console.  A request
        that wanders here looking for management data must not get a hint
        that some other shape of request would have worked.
        """
        if method not in ("GET", "HEAD"):
            return 405, "application/json", _json({"error": "method_not_allowed"}), {"Allow": "GET, HEAD"}
        route = urllib.parse.urlsplit(path).path.rstrip("/") or "/"
        if route == "/healthz":
            return 200, "application/json", _json({"schema": "cwk.kb.portal.health.v1", "status": "ok"}), {}
        if route in ("/", "/index.html"):
            return 200, _HTML_TYPE, self.page().encode("utf-8"), {}
        if route == "/docs":
            return 200, _HTML_TYPE, self.docs_index().encode("utf-8"), {}
        if route.startswith("/docs/"):
            # 只认注册过的 slug：路径里的任何东西都不会被当成文件名去找。
            rendered = self.doc(route[len("/docs/"):])
            if rendered is not None:
                return 200, _HTML_TYPE, rendered.encode("utf-8"), {}
        return 404, "application/json", _json({"error": "not_found"}), {}


_HTML_TYPE = "text/html; charset=utf-8"


def _json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


_ARROW_DEFS = """<defs>
<marker id='dgar' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7'
  orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='currentColor'/></marker>
</defs>"""

# 部署拓扑：谁跑在哪台机器上、哪些端口对局域网开放、哪些只在服务器本机可达。
# 坐标是手排的固定网格——三列的列心在 327/556/785，两列的在 385/727。
_DEPLOY_SVG = """<svg viewBox='0 0 920 570' role='img'
  aria-label='部署拓扑图：数据来源经摄取管道进入 OPS 服务器的存储层、服务层与门面层，局域网内的同事通过 Agent 访问'>
""" + _ARROW_DEFS + """
<g class='dg-zt'><text x='8' y='0'></text></g>

<!-- 数据来源 -->
<rect class='dg-box' x='8' y='8' width='284' height='62' rx='10'/>
<text class='dg-t' x='150' y='34' text-anchor='middle'>工作协同系统</text>
<text class='dg-s' x='150' y='54' text-anchor='middle'>汇报、待办、回复链</text>

<rect class='dg-box' x='318' y='8' width='284' height='62' rx='10'/>
<text class='dg-t' x='460' y='34' text-anchor='middle'>云端文件库</text>
<text class='dg-s' x='460' y='54' text-anchor='middle'>投前资料这类已有文档库</text>

<rect class='dg-box dash' x='628' y='8' width='284' height='62' rx='10'/>
<text class='dg-t' x='770' y='34' text-anchor='middle'>你自己的文件</text>
<text class='dg-s' x='770' y='54' text-anchor='middle'>尚未开通，见下方说明</text>

<g class='dg-line' color='currentColor'>
<path d='M150,70 L150,98' marker-end='url(#dgar)'/>
<path d='M460,70 L460,98' marker-end='url(#dgar)'/>
<path d='M770,70 L770,98' marker-end='url(#dgar)' stroke-dasharray='5 4'/>
</g>

<!-- 摄取 -->
<rect class='dg-bar' x='8' y='100' width='904' height='48' rx='10'/>
<text class='dg-t' x='460' y='122' text-anchor='middle'>摄取管道</text>
<text class='dg-s' x='460' y='140' text-anchor='middle'>格式转换 → 原文快照 + 检索索引（离线执行，定期更新，原件只读不改）</text>
<g class='dg-line strong' color='currentColor'><path d='M460,148 L460,172' marker-end='url(#dgar)'/></g>

<!-- OPS 服务器 -->
<rect class='dg-zone' x='200' y='176' width='712' height='340' rx='14'/>
<text class='dg-zt' x='218' y='198'>OPS 服务器 · 192.168.91.72</text>

<text class='dg-zt' x='220' y='230'>存储层</text>
<rect class='dg-box' x='220' y='238' width='200' height='62' rx='9'/>
<text class='dg-t' x='320' y='262' text-anchor='middle'>检索索引</text>
<text class='dg-s' x='320' y='282' text-anchor='middle'>OpenSearch · 仅本机</text>
<rect class='dg-box' x='448' y='238' width='200' height='62' rx='9'/>
<text class='dg-t' x='548' y='262' text-anchor='middle'>原文快照</text>
<text class='dg-s' x='548' y='282' text-anchor='middle'>只读，按库分目录</text>
<rect class='dg-box' x='676' y='238' width='216' height='62' rx='9'/>
<text class='dg-t' x='784' y='262' text-anchor='middle'>令牌登记表</text>
<text class='dg-s' x='784' y='282' text-anchor='middle'>只存指纹，两个服务每次都查</text>

<!-- 每列的箭头必须对得上真实依赖：索引喂检索、快照喂问答。
     登记表被两个服务查、不喂任何单一组件，所以它没有竖箭头，改用文字说明——
     画一条竖线到本机模型会变成「登记表喂模型」这种假话。 -->
<g class='dg-line' color='currentColor'>
<path d='M320,300 L320,330' marker-end='url(#dgar)'/>
<path d='M548,300 L548,330' marker-end='url(#dgar)'/>
</g>

<text class='dg-zt' x='220' y='328'>服务层</text>
<rect class='dg-box accent' x='220' y='336' width='200' height='62' rx='9'/>
<text class='dg-t' x='320' y='360' text-anchor='middle'>检索服务</text>
<text class='dg-p' x='320' y='380' text-anchor='middle'>:8787</text>
<rect class='dg-box accent' x='448' y='336' width='200' height='62' rx='9'/>
<text class='dg-t' x='548' y='360' text-anchor='middle'>问答 · 读原文</text>
<text class='dg-p' x='548' y='380' text-anchor='middle'>:8790</text>
<rect class='dg-box' x='676' y='336' width='216' height='62' rx='9'/>
<text class='dg-t' x='784' y='360' text-anchor='middle'>本机模型</text>
<text class='dg-s' x='784' y='380' text-anchor='middle'>仅本机，不出内网</text>
<g class='dg-line' color='currentColor'><path d='M652,367 L672,367' marker-end='url(#dgar)'/></g>

<text class='dg-zt' x='220' y='426'>门面层</text>
<rect class='dg-box accent' x='220' y='434' width='328' height='62' rx='9'/>
<text class='dg-t' x='384' y='458' text-anchor='middle'>官网</text>
<text class='dg-s' x='384' y='478' text-anchor='middle'><tspan class='dg-p'>:8792</tspan> 免密码，就是你正在看的这页</text>
<rect class='dg-box accent' x='560' y='434' width='332' height='62' rx='9'/>
<text class='dg-t' x='726' y='458' text-anchor='middle'>管理控制台</text>
<text class='dg-s' x='726' y='478' text-anchor='middle'><tspan class='dg-p'>:8791</tspan> 需要管理密码</text>

<!-- 使用者 -->
<rect class='dg-box accent' x='8' y='388' width='170' height='96' rx='10'/>
<text class='dg-t' x='93' y='420' text-anchor='middle'>局域网里的你</text>
<text class='dg-s' x='93' y='440' text-anchor='middle'>OpenClaw Agent</text>
<text class='dg-s' x='93' y='458' text-anchor='middle'>装了查询 Skill</text>

<g class='dg-line strong' color='currentColor'>
<path d='M178,412 L214,376' marker-end='url(#dgar)'/>
<path d='M178,452 L214,462' marker-end='url(#dgar)'/>
</g>
<text class='dg-s' x='196' y='356' text-anchor='middle'>带令牌</text>
<text class='dg-s' x='150' y='502' text-anchor='middle'>浏览</text>

<text class='dg-s' x='8' y='540'>实线＝局域网内可达；标「仅本机」的两项只在服务器自己身上可达，任何人从局域网都连不到。</text>
</svg>"""


# 一次提问的完整链路，画成时序图：四条泳道自左向右，时间自上向下。
# 选时序图而不是流程框图，是因为要看清「谁在跟谁说话、令牌在哪一跳被校验」。
_FLOW_SVG = """<svg viewBox='0 0 920 540' role='img'
  aria-label='数据流转时序图：Agent 发起检索与问答请求，经令牌校验、双通道检索、读取原文快照与本机模型，返回结论与引用'>
""" + _ARROW_DEFS + """
<rect class='dg-box accent' x='20' y='8' width='180' height='46' rx='9'/>
<text class='dg-t' x='110' y='30' text-anchor='middle'>你的 Agent</text>
<text class='dg-s' x='110' y='46' text-anchor='middle'>在你自己的机器上</text>

<rect class='dg-box accent' x='280' y='8' width='180' height='46' rx='9'/>
<text class='dg-t' x='370' y='30' text-anchor='middle'>检索服务</text>
<text class='dg-p' x='370' y='46' text-anchor='middle'>:8787</text>

<rect class='dg-box accent' x='530' y='8' width='180' height='46' rx='9'/>
<text class='dg-t' x='620' y='30' text-anchor='middle'>问答 · 读原文</text>
<text class='dg-p' x='620' y='46' text-anchor='middle'>:8790</text>

<rect class='dg-box' x='740' y='8' width='170' height='46' rx='9'/>
<text class='dg-t' x='825' y='30' text-anchor='middle'>快照与本机模型</text>
<text class='dg-s' x='825' y='46' text-anchor='middle'>都在服务器本机</text>

<g class='dg-life'>
<path d='M110,54 L110,506'/><path d='M370,54 L370,506'/>
<path d='M620,54 L620,506'/><path d='M825,54 L825,506'/>
</g>

<g class='dg-line strong' color='currentColor'>
<path d='M110,98 L366,98' marker-end='url(#dgar)'/>
<path d='M370,140 L418,140 L418,164 L374,164' marker-end='url(#dgar)'/>
<path d='M370,204 L418,204 L418,228 L374,228' marker-end='url(#dgar)'/>
<path d='M366,268 L114,268' marker-end='url(#dgar)'/>
<path d='M110,312 L616,312' marker-end='url(#dgar)'/>
<path d='M620,354 L821,354' marker-end='url(#dgar)'/>
<path d='M620,398 L821,398' marker-end='url(#dgar)'/>
<path d='M616,442 L114,442' marker-end='url(#dgar)'/>
<path d='M110,486 L616,486' marker-end='url(#dgar)' stroke-dasharray='5 4'/>
</g>

<text class='dg-n' x='118' y='90'>①</text>
<text class='dg-s' x='138' y='90'>POST /query —— 请求头带上你的令牌</text>

<text class='dg-n' x='430' y='146'>②</text>
<text class='dg-s' x='450' y='146'>校验令牌：缺失或过期 401，库不在授权内 403</text>

<text class='dg-n' x='430' y='210'>③</text>
<text class='dg-s' x='450' y='210'>精确通道与语义通道并行，结果合并排序</text>

<text class='dg-n' x='128' y='260'>④</text>
<text class='dg-s' x='148' y='260'>返回候选文档编号，毫秒级</text>

<text class='dg-n' x='118' y='304'>⑤</text>
<text class='dg-s' x='138' y='304'>POST /answer —— 要一段带出处的结论</text>

<text class='dg-n' x='628' y='346'>⑥</text>
<text class='dg-s' x='648' y='346'>按编号取出原文，只在内存里用</text>

<text class='dg-n' x='628' y='390'>⑦</text>
<text class='dg-s' x='648' y='390'>交给本机模型归纳，原文不出这台机器</text>

<text class='dg-n' x='128' y='434'>⑧</text>
<text class='dg-s' x='148' y='434'>返回结论 + 引用编号，约 25–45 秒</text>

<text class='dg-n' x='118' y='478'>⑨</text>
<text class='dg-s' x='138' y='478'>可选：POST /read 逐页翻原文，亲自核对那句话</text>
</svg>"""


_CSS = """
:root{
  --bg:#f6f7fb; --card:#fff; --ink:#131b2b; --muted:#5a6780; --line:#e2e7f0;
  --accent:#2b5bd7; --accent-soft:#eaf0ff; --deep:#131c30; --ok:#16794c; --warn:#9a6400;
  --code-bg:#161d2e; --code-ink:#dde5f5;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:0 auto;padding:0 1.25rem}
a{color:var(--accent)}

nav{position:sticky;top:0;z-index:20;background:#fffffff2;backdrop-filter:saturate(180%) blur(12px);
  border-bottom:1px solid var(--line)}
nav .wrap{display:flex;align-items:center;gap:1.1rem;height:56px;overflow-x:auto}
nav strong{font-size:.95rem;white-space:nowrap}
nav a{color:var(--muted);text-decoration:none;font-size:.9rem;white-space:nowrap}
nav a:hover{color:var(--accent)}
nav .spacer{flex:1}

header{background:radial-gradient(1200px 400px at 15% -10%,#2b3f6b 0%,var(--deep) 60%);color:#fff;
  padding:4.5rem 0 4rem}
header .eyebrow{display:inline-block;font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;
  color:#9fb4dd;border:1px solid #3d4f78;border-radius:99px;padding:.25rem .8rem;margin-bottom:1.2rem}
header h1{margin:0 0 1rem;font-size:2.4rem;line-height:1.25;letter-spacing:-.01em;max-width:18em}
header p{margin:0 0 2rem;color:#c5d2e8;max-width:36em;font-size:1.05rem}
.ctas{display:flex;gap:.75rem;flex-wrap:wrap}
.cta{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;
  padding:.75rem 1.5rem;border-radius:9px;font-weight:600;font-size:.95rem}
.cta:hover{opacity:.92}
.cta.ghost{background:transparent;border:1px solid #4a5b83;color:#dce5f5}
.cta.ghost:hover{border-color:#7f93c0}
.cta.disabled{opacity:.5}

section{padding:3.5rem 0;border-bottom:1px solid var(--line)}
section:last-of-type{border-bottom:none}
h2{font-size:1.5rem;margin:0 0 .5rem;letter-spacing:-.01em}
.lede{color:var(--muted);margin:0 0 2rem;max-width:44em}
h3{font-size:1.02rem;margin:0 0 .4rem}

.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.3rem}
.card p{margin:0;color:var(--muted);font-size:.93rem}
.card .k{display:block;font-size:.75rem;letter-spacing:.1em;color:var(--accent);
  text-transform:uppercase;margin-bottom:.6rem}
.bank{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem}
.bank h3{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.95rem;color:var(--accent);margin-bottom:.3rem}
.bank p{margin:0;color:var(--muted);font-size:.9rem}

.pain{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.pain .card{border-left:3px solid var(--accent)}
.pain q{display:block;font-weight:600;margin-bottom:.5rem;font-style:normal}
.pain q::before{content:"「"} .pain q::after{content:"」"}

ol.steps{list-style:none;counter-reset:s;padding:0;margin:0}
ol.steps > li{counter-increment:s;position:relative;padding:0 0 1.9rem 3rem;border-left:2px solid var(--line);
  margin-left:1rem}
ol.steps > li:last-child{border-left-color:transparent;padding-bottom:0}
ol.steps > li::before{content:counter(s);position:absolute;left:-1rem;top:-.15rem;width:2rem;height:2rem;
  border-radius:50%;background:var(--accent);color:#fff;display:grid;place-items:center;
  font-size:.85rem;font-weight:700}
ol.steps h3{margin-bottom:.35rem}
ol.steps .why{color:var(--muted);font-size:.92rem;margin:0 0 .7rem}

.code{position:relative;background:var(--code-bg);border-radius:10px;margin:.6rem 0 .8rem}
/* 顶部留出按钮的高度：代码块可以横向滚动，靠 padding-right 挡不住首行——
   滚动之后它照样会钻到按钮底下。留一行上边距才是与滚动无关的解法。 */
.code pre{margin:0;padding:2.5rem 1.1rem 1rem;overflow-x:auto}
.code code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84rem;
  line-height:1.65;color:var(--code-ink);white-space:pre}
.code button{position:absolute;top:.5rem;right:.5rem;background:#ffffff1a;color:#cfd9ee;
  border:1px solid #ffffff2e;border-radius:6px;padding:.25rem .6rem;font-size:.75rem;cursor:pointer}
.code button:hover{background:#ffffff2e}
.expect{font-size:.88rem;color:var(--ok);margin:0 0 .3rem}
.expect::before{content:"期望 ";color:var(--muted)}

.rules{list-style:none;padding:0;margin:0;display:grid;gap:.7rem}
.rules li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1.1rem;
  font-size:.94rem}
.rules b{display:block;margin-bottom:.15rem}
.rules span{color:var(--muted)}

details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:.6rem}
details summary{padding:.9rem 1.1rem;cursor:pointer;font-weight:600;font-size:.95rem;list-style:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"＋";color:var(--accent);margin-right:.6rem;font-weight:400}
details[open] summary::before{content:"－"}
details .body{padding:0 1.1rem 1rem;color:var(--muted);font-size:.93rem}

.note{color:var(--muted);font-size:.88rem}

/* 图：用 CSS 变量着色，因此深浅色主题共用同一份 SVG。
   外层 .figure 负责窄屏横向滚动——图本身不缩到看不清。 */
.figure{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:1.25rem;margin:0 0 .8rem;overflow-x:auto}
.figure svg{display:block;width:100%;min-width:720px;height:auto}
.figcap{color:var(--muted);font-size:.86rem;margin:0 0 2rem}
.dg-zone{fill:var(--accent-soft);stroke:var(--line)}
.dg-box{fill:var(--card);stroke:var(--line);stroke-width:1.5}
.dg-box.accent{stroke:var(--accent)}
.dg-box.dash{stroke-dasharray:5 4}
.dg-bar{fill:var(--accent);opacity:.12;stroke:var(--accent)}
.dg-t{fill:var(--ink);font-size:14px;font-weight:600}
.dg-s{fill:var(--muted);font-size:12px}
.dg-p{fill:var(--accent);font-size:12px;font-weight:700;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.dg-zt{fill:var(--muted);font-size:12px;font-weight:700;letter-spacing:.08em}
.dg-line{stroke:var(--muted);stroke-width:1.5;fill:none}
.dg-line.strong{stroke:var(--accent);stroke-width:2}
.dg-life{stroke:var(--line);stroke-width:1.5;stroke-dasharray:4 5}
/* 圈码在 12px 下糊成一个点，认不出是几 */
.dg-n{fill:var(--accent);font-size:16px;font-weight:700}
.admin{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.5rem;
  display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;justify-content:space-between}
.admin .cta.ghost{border-color:var(--line);color:var(--accent)}
.admin .cta.ghost:hover{border-color:var(--accent)}

footer{padding:2rem 0 3rem;color:var(--muted);font-size:.85rem}

@media (prefers-color-scheme:dark){
  :root{--bg:#0e1320;--card:#161d2d;--ink:#e7ecf7;--muted:#95a3bd;--line:#26314604;
    --accent:#7ba2ff;--accent-soft:#1b2540;--deep:#0a0f1c;--ok:#5fd39b;--warn:#e0b060;
    --code-bg:#0a0f1c;--code-ink:#cfdaf0}
  :root{--line:#263146}
  nav{background:#0e1320f2}
  .admin .cta.ghost{color:var(--accent)}
}
@media (max-width:640px){
  header h1{font-size:1.85rem}
  section{padding:2.5rem 0}
}

/* 文档中心：侧栏 + 正文两栏，窄屏折成一栏 */
.doclayout{display:grid;grid-template-columns:220px 1fr;gap:2.5rem;align-items:start;
  max-width:1120px;margin:0 auto;padding:2rem 1.25rem 4rem}
.sidebar{position:sticky;top:72px}
.sidebar .sgroup{font-size:.75rem;letter-spacing:.1em;color:var(--muted);
  text-transform:uppercase;margin:0 0 .6rem;font-weight:700}
.sidebar a{display:block;padding:.45rem .7rem;border-radius:7px;text-decoration:none;
  color:var(--muted);font-size:.92rem;border-left:2px solid transparent}
.sidebar a:hover{background:var(--accent-soft);color:var(--accent)}
.sidebar a[aria-current=page]{color:var(--accent);font-weight:600;background:var(--accent-soft);
  border-left-color:var(--accent)}
.doc h1{font-size:1.75rem;margin:0 0 .5rem;letter-spacing:-.01em}
.doc .sub{color:var(--muted);margin:0 0 2.5rem;font-size:1rem}
.doc h2{font-size:1.25rem;margin:2.5rem 0 .8rem;padding-bottom:.5rem;border-bottom:1px solid var(--line)}
.doc h3{font-size:1rem;margin:1.8rem 0 .5rem}
.doc p{margin:0 0 1rem}
.doc ul,.doc ol{padding-left:1.3rem;margin:0 0 1rem}
.doc li{margin-bottom:.45rem}
.doc table{width:100%;border-collapse:collapse;font-size:.9rem;margin:0 0 1.2rem}
.doc th,.doc td{text-align:left;padding:.55rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
.doc th{background:var(--head,var(--accent-soft));font-weight:600;color:var(--muted);font-size:.82rem}
.doc td code,.doc p code,.doc li code{background:var(--accent-soft);color:var(--accent);
  padding:.1rem .35rem;border-radius:4px;font-size:.86em;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.doc .scroll{overflow-x:auto}
.method{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.78rem;font-weight:700;padding:.15rem .5rem;border-radius:5px;
  background:var(--accent);color:#fff;margin-right:.5rem}
.endpoint{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.95rem;font-weight:600}
.callout{background:var(--accent-soft);border-left:3px solid var(--accent);
  border-radius:0 8px 8px 0;padding:.9rem 1.1rem;margin:0 0 1.2rem;font-size:.92rem}
.callout b{display:block;margin-bottom:.2rem}
.doccards{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
.doccards a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:1.2rem;text-decoration:none;color:inherit}
.doccards a:hover{border-color:var(--accent)}
.doccards h3{margin:0 0 .35rem;font-size:1rem;color:var(--accent)}
.doccards p{margin:0;color:var(--muted);font-size:.9rem}
@media (max-width:820px){
  .doclayout{grid-template-columns:1fr;gap:1.5rem}
  .sidebar{position:static;display:flex;gap:.4rem;flex-wrap:wrap;
    border-bottom:1px solid var(--line);padding-bottom:1rem}
  .sidebar .sgroup{width:100%}
  .sidebar a{border-left:none;border:1px solid var(--line)}
}
"""

_SCRIPT = """<script>
// 这个站点通过普通 HTTP 提供，不是安全上下文，浏览器新的异步剪贴板 API 在这里
// 根本不存在。因此用一个临时 textarea 加 execCommand——它在非安全上下文仍然有效。
// 判据按字面断言页面里不出现那个新 API 的名字，所以这里连提都不提它。
function copyBlock(button){
  var code = button.parentElement.querySelector('code');
  if (!code) return;
  var area = document.createElement('textarea');
  area.value = code.innerText;
  area.setAttribute('readonly', '');
  area.style.position = 'absolute';
  area.style.left = '-9999px';
  document.body.appendChild(area);
  area.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
  document.body.removeChild(area);
  var original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.textContent = ok ? '已复制' : '请手动选择复制';
  setTimeout(function(){ button.textContent = button.dataset.label; }, 1800);
}
</script>"""

# 站点导航：首页 + 文档中心。文档中心的子页在侧栏里，不挤进顶部导航。
_DOC_NAV: tuple[tuple[str, str, str], ...] = (
    ("quickstart", "快速开始", "十分钟把 Skill 装上、拿到令牌、跑通第一次检索"),
    ("architecture", "架构与部署", "东西跑在哪台机器上，一次请求经过了什么"),
    ("api", "API 参考", "三个端点的请求、响应与错误码"),
    ("extend", "扩展开发", "接一个新数据源，或把知识库接进你自己的 Agent"),
    ("faq", "常见问题", "查不到、401、403、耗时、令牌纪律"),
)


_HOME_BODY = """
<header><div class='wrap'>
  <span class='eyebrow'>内部知识库服务</span>
  <h1>让 AI 替你翻遍公司资料，<br>每句结论都能追回原文</h1>
  <p>把授权范围内的工作汇报、投前资料和年度规划变成可检索的知识库。
     问一句话，拿到结论和出处；库里没有的，它会直接说没有，而不是编一个。</p>
  <div class='ctas'>
    <a class='cta' href='/docs/quickstart'>十分钟接入</a>
    <a class='cta ghost' href='/docs'>查看文档</a>
  </div>
</div></header>

<section id='value'><div class='wrap'>
  <h2>它解决的是这三件事</h2>
  <p class='lede'>如果下面任何一条你每周都要经历一次，这套东西就值得接。</p>
  <div class='pain'>
    <div class='card'>
      <q>那份材料到底在哪</q>
      <p>三个月的工作汇报、几百份投前文档。你记得有这么回事，记不得标题，更记不得编号。</p>
    </div>
    <div class='card'>
      <q>翻一遍要一下午</q>
      <p>逐个文件打开、翻页、确认不是这份、再关掉。找到之前，一下午过去了。</p>
    </div>
    <div class='card'>
      <q>AI 说的我不敢信</q>
      <p>通用 AI 会把不确定的事说得很肯定。这里每条结论都绑定原文位置，你可以当场翻开核对。</p>
    </div>
  </div>
</div></section>

<section id='features'><div class='wrap'>
  <h2>四项能力</h2>
  <p class='lede'>每一项都写清楚它能做什么、以及它不做什么——后半句往往更重要。</p>
  <div class='grid'>
    <div class='card'>
      <span class='k'>检索</span>
      <h3>精确与语义双通道</h3>
      <p>编号、日期、文件名走精确通道，<code>ABC-2026-007</code> 这种串不会被中文分词拆碎；
         概念性的问法走语义通道。两路结果合并后给你候选。</p>
    </div>
    <div class='card'>
      <span class='k'>问答</span>
      <h3>给结论，并且给出处</h3>
      <p>提一个自然语言问题，拿到一段结论加若干条引用。通常 25–45 秒。
         检索零命中时它会体面地说找不到，不会硬凑一个答案。</p>
    </div>
    <div class='card'>
      <span class='k'>核验</span>
      <h3>原文可以逐页翻</h3>
      <p>拿到结论后可以按页读原文，确认那句话确实是这么写的。
         这是"可追溯"的兑现方式——不是承诺，是一个你能亲自执行的动作。</p>
    </div>
    <div class='card'>
      <span class='k'>授权</span>
      <h3>按 Agent 和库分别授权</h3>
      <p>令牌按 Agent 实例签发、按库授权。访问不在授权范围的库会被拒绝——
         这是隔离在生效，不是故障。</p>
    </div>
  </div>
</div></section>

<section id='banks'><div class='wrap'>
  <h2>当前可接入的知识库</h2>
  <p class='lede'>按需申请，只给你实际要用的那几个。</p>
  <div class='grid'>{{BANK_CARDS}}</div>
</div></section>

<section id='trust'><div class='wrap'>
  <h2>几件先说清楚的事</h2>
  <p class='lede'>先知道它不做什么，用起来才不会误判。展开的说明在
     <a href='/docs/faq'>常见问题</a>。</p>
  <ul class='rules'>
    <li><b>只读</b><span>不会替你标已读、回复、处理待办或删除任何东西。</span></li>
    <li><b>找不到就说找不到</b><span>零命中时体面拒答，不编造。</span></li>
    <li><b>语料是快照</b><span>定期更新，不是实时镜像。</span></li>
    <li><b>全程在内网</b><span>检索、读原文和模型生成都在公司服务器上完成。</span></li>
  </ul>
</div></section>

<section id='next'><div class='wrap'>
  <h2>下一步</h2>
  <div class='doccards'>
    <a href='/docs/quickstart'><h3>十分钟接入 →</h3>
      <p>装 Skill、申请令牌、跑通第一次检索。命令可以直接复制。</p></a>
    <a href='/docs'><h3>文档中心 →</h3>
      <p>架构与部署、API 参考、扩展开发，想深入就从这里进。</p></a>
  </div>
</div></section>

<section id='admin'><div class='wrap'>
  <h2>管理员入口</h2>
  <div class='admin'>
    <div>
      <b>管理控制台</b>
      <p class='note' style='margin:.2rem 0 0'>查看知识库状态、访问令牌和管理审计。需要管理密码。</p>
    </div>
    {{CONSOLE_BLOCK}}
  </div>
  <p class='note' style='margin-top:1rem'>本页面是静态说明，不读取任何库内容、令牌或审计信息。</p>
</div></section>
"""

_DOCS_INDEX_BODY = """
<div class='wrap' style='padding:2.5rem 0 1rem'>
  <h1 style='font-size:1.9rem;margin:0 0 .5rem'>文档中心</h1>
  <p class='lede' style='margin-bottom:2rem'>产品介绍在<a href='/'>首页</a>；
     这里是把它接进你的工作流、或者在它上面做开发所需要的全部内容。</p>
  <div class='doccards'>{{DOC_CARDS}}</div>
  <p class='note' style='margin-top:2rem'>看完仍有问题，找{{CONTACT}}。</p>
</div>
"""


_DOC_QUICKSTART = """
<p>前置条件：你的机器和知识库服务在同一局域网，并且以 OpenClaw 运行 Agent。
   五步做完就能用，全程大约十分钟。</p>

<ol class='steps'>
  <li>
    <h3>安装查询 Skill</h3>
    <p class='why'>可重复执行，再跑一次就是升级。</p>
    <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>git clone --depth 1 {{REPO}} /tmp/CWK 2>/dev/null || git -C /tmp/CWK pull --ff-only

# 找到本网关的 skills 目录（多个网关拿不准就全列出来，确认后再选）
ls -d ~/.openclaw/gateways/*/state/workspace*/skills

# 把 &lt;skills目录&gt; 换成上一条确认的路径
rm -rf &lt;skills目录&gt;/cwk-kb-query
cp -R /tmp/CWK/skills/cwk-kb-query &lt;skills目录&gt;/cwk-kb-query</code></pre></div>
    <p class='expect'><code>SKILL.md</code> 存在，内容里能搜到 <code>8787</code>、<code>8790</code>、<code>/read</code>。</p>
  </li>

  <li>
    <h3>加进 Agent 白名单</h3>
    <p class='why'>文件放好不等于 Agent 看得见——能不能用取决于该 Agent 的 skills 白名单。</p>
    <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code># 1. 备份网关配置（通常在 ~/.openclaw/gateways/&lt;网关名&gt;/openclaw.json）
# 2. 在 agents.entries.&lt;你的agent-id&gt;.skills 数组末尾追加 "cwk-kb-query"
#    只加不删，保持既有条目不动
# 3. 验证：
openclaw skills info cwk-kb-query</code></pre></div>
    <p class='expect'>输出里出现 <code>Visible to model: yes</code>。</p>
    <p class='note'>若显示 excluded / not visible：让该 Agent 新起一轮对话重载 skills；
       仍不行就带着现象找{{CONTACT}}，不要改其他配置。</p>
  </li>

  <li>
    <h3>预检并收集登记信息</h3>
    <p class='why'>纯只读，不改任何东西。</p>
    <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>curl -s -m 6 {{RETRIEVAL}}/healthz        # 服务是否可达
scutil --get ComputerName 2>/dev/null || hostname   # 机器名
openclaw agents list 2>/dev/null | head             # 本网关 agent-id</code></pre></div>
    <p class='expect'>第一条返回 <code>{"status":"ok"}</code>。</p>
  </li>

  <li>
    <h3>申请令牌</h3>
    <p class='why'>按模板回报，管理员据此签发。不要附带任何令牌、密码或配置原文。</p>
    <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>--- CWK-KB 接入登记 ---
机器名: &lt;...&gt;
agent-id: &lt;...&gt;
服务可达: &lt;{"status":"ok"} / 不可达 + 原始报错&gt;
申请库（勾选）:
{{BANK_CHECKLIST}}
Skill 安装: &lt;成功/已升级 路径 / 失败原因&gt;
白名单可见: &lt;Visible to model: yes / 未通过 + 现象&gt;
---</code></pre></div>
    <p class='note'>把它发给{{CONTACT}}。令牌会单独私发给你，只出现一次。</p>
  </li>

  <li>
    <h3>落地令牌并验证</h3>
    <p class='why'>令牌写进 0600 的私有文件，不要放进命令行参数（会留在 history 里）。</p>
    <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>mkdir -p ~/.openclaw/cwk
cat > ~/.openclaw/cwk/kb.env &lt;&lt;'EOF'
CWK_KB_TOKEN=&lt;私发给你的令牌&gt;
EOF
chmod 600 ~/.openclaw/cwk/kb.env
set -a; source ~/.openclaw/cwk/kb.env; set +a

# 验证：检索一次（bank 换成你申请到的库）
curl -s -m 15 {{RETRIEVAL}}/query -X POST \\
  -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \\
  -d '{"bank":"{{FIRST_BANK}}","query":"立项","top_k":3}'</code></pre></div>
    <p class='expect'>HTTP 200，返回 <code>hits</code> 数组，<code>took_ms</code> 是毫秒级。</p>
    <p class='note'>签发或换发后等 10 秒再验证，登记表同步有秒级延迟。</p>
  </li>
</ol>

<h2>装好之后怎么问</h2>
<p>日常直接对你的 Agent 说人话就行：</p>
<div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>用 cwk-kb-query 查 {{FIRST_BANK}} 库：上个月关于 XX 项目的进展汇报
用 cwk-kb-query 查 {{FIRST_BANK}} 库：编号 ABC-2026-007 对应的是什么事
用 cwk-kb-query 问 {{FIRST_BANK}} 库：这个项目目前卡在哪一步，给出处</code></pre></div>
<p class='note'>想直接调接口而不经过 Skill，看 <a href='/docs/api'>API 参考</a>。</p>
"""


_DOC_ARCHITECTURE = """
<h2>知识库能承接哪些数据</h2>
<p>知识库本身不产生内容，它只是把别处已有的资料整理成可检索的样子。
   目前接了两类来源，第三类还没开通。</p>
<div class='grid' style='margin-bottom:1.5rem'>
  <div class='card'>
    <span class='k'>已接入</span>
    <h3>工作协同系统</h3>
    <p>日常的工作汇报、待办和回复链，按业务日期整理入库。这是目前量最大的一类，
       适合回答"某件事当时是怎么推进的"。</p>
  </div>
  <div class='card'>
    <span class='k'>已接入</span>
    <h3>云端文件库</h3>
    <p>已经存在公司云端文档库里的资料，按项目分类入库。Word、PDF、表格、纯文本
       都会先经过格式转换再进索引。</p>
  </div>
  <div class='card'>
    <span class='k' style='color:var(--muted)'>尚未开通</span>
    <h3>你自己上传的文件</h3>
    <p>目前还没有对应的接入通道——摄取管道只认上面两类来源。
       确实需要把自己手里的一批资料建成库，先跟{{CONTACT}}说，
       这需要新写一个数据源适配器，做法见<a href='/docs/extend'>扩展开发</a>。</p>
  </div>
</div>
<p class='note'>两类来源进库之后是一样的：原件只读、不被改写，入库的是它的一份快照和索引。
   新建一个库由管理员执行摄取，不是自助操作。</p>

<h2>它是怎么部署的</h2>
<p>全部跑在公司内网的一台服务器上。知道东西在哪、哪些端口开着、哪些根本连不到，
   用起来心里有数。</p>
<div class='figure'>{{DEPLOY_SVG}}</div>
<p class='figcap'>你的机器只会碰到四个端口：官网、管理控制台、检索、问答。
   检索索引和本机模型都只监听服务器自己，局域网里连不上——这不是配置疏漏，是有意如此。</p>

<h2>一次提问经过了什么</h2>
<p>从你说出问题，到拿回一段带出处的结论，中间是这样一条链路。</p>
<div class='figure'>{{FLOW_SVG}}</div>
<p class='figcap'>三件值得记住的事：令牌在第一跳就被校验，越权在这里就被挡住；
   原文只在服务器内存里经手，不出这台机器；服务本身不保存你的问题，也不保存生成的答案。</p>

<div class='callout'><b>快照，不是实时镜像</b>
语料是定期生成的快照。这意味着刚发生的事可能还没进库；也意味着你看到的内容
与源系统当下的状态可能有时间差。要确认某份材料是否已入库，问{{CONTACT}}。</div>
"""


_DOC_API = """
<p>三个端点构成全部对外能力：检索、问答、读原文。下面的字段与错误码都与服务端实现一致，
   多传一个字段就会被拒绝——契约是收紧的，不是尽力而为的。</p>

<h2>鉴权</h2>
<p>所有业务端点都必须带 <code>X-KB-Token</code> 请求头。健康检查端点免鉴权。</p>
<table>
  <tr><th>状态码</th><th>含义</th><th>怎么办</th></tr>
  <tr><td><code>401</code></td><td>令牌缺失、过期或已被吊销</td><td>找{{CONTACT}}重签</td></tr>
  <tr><td><code>403</code></td><td>令牌有效，但目标库不在它的授权范围内</td>
      <td>这是库间隔离生效；确需该库请调整授权</td></tr>
</table>

<h2><span class='method'>POST</span><span class='endpoint'>{{RETRIEVAL}}/query</span></h2>
<p>检索候选文档。请求体<strong>只接受</strong>下面三个字段，多一个就是 <code>400</code>。</p>
<div class='scroll'><table>
  <tr><th>字段</th><th>类型</th><th>必填</th><th>说明</th></tr>
  <tr><td><code>bank</code></td><td>string</td><td>是</td>
      <td>库标识；未注册的库返回 <code>404</code></td></tr>
  <tr><td><code>query</code></td><td>string</td><td>是</td><td>查询串，最长 4096 字符</td></tr>
  <tr><td><code>top_k</code></td><td>integer</td><td>否</td><td>1–100，缺省由服务端配置决定</td></tr>
</table></div>
<div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>curl -s {{RETRIEVAL}}/query -X POST \\
  -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \\
  -d '{"bank":"{{FIRST_BANK}}","query":"立项","top_k":3}'</code></pre></div>
<p>响应 <code>200</code>：</p>
<div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>{
  "hits": [
    {"doc_id": "cwork:2061147125840072705", "score": 12.34, "channel": "exact"}
  ],
  "no_answer": false,
  "took_ms": 5.799
}</code></pre></div>
<ul>
  <li><code>channel</code>：<code>exact</code> 是精确通道（编号、日期、文件名），
      <code>lexical</code> 是语义通道。</li>
  <li><code>no_answer</code>：<code>hits</code> 为空时为 <code>true</code>。
      它只代表这一次没命中，不代表全库没有。</li>
</ul>
<p>错误响应形如 <code>{"error":{"code":"...","message":"..."}}</code>：</p>
<table>
  <tr><th>状态码</th><th>code</th><th>含义</th></tr>
  <tr><td><code>400</code></td><td><code>invalid_request</code></td><td>字段缺失、类型不对或含未知字段</td></tr>
  <tr><td><code>404</code></td><td><code>bank_not_found</code></td><td>库未注册</td></tr>
  <tr><td><code>503</code></td><td><code>backend_unavailable</code></td><td>检索后端不可用</td></tr>
</table>
<p class='note'>健康检查：<code>GET /healthz</code> 返回 <code>{"status":"ok"}</code>；
   <code>GET /readyz</code> 就绪时返回 <code>{"status":"ready"}</code>，
   否则 <code>503 not_ready</code>。</p>

<h2><span class='method'>POST</span><span class='endpoint'>{{ANSWER}}/answer</span></h2>
<p>要一段带出处的结论。接受 <code>query</code>、<code>top_k</code>、<code>bank</code> 三个字段。</p>
<div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>curl -s -m 120 {{ANSWER}}/answer -X POST \\
  -H 'Content-Type: application/json' -H "X-KB-Token: $CWK_KB_TOKEN" \\
  -d '{"bank":"{{FIRST_BANK}}","query":"这个项目卡在哪一步","top_k":3}'</code></pre></div>
<p>响应 <code>200</code>：</p>
<div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>{
  "answer": "……结论正文……",
  "citations": [{"doc_id": "cwork:2061147125840072705", "score": 12.34}],
  "model": "……",
  "took_ms": 31245
}</code></pre></div>
<div class='callout'><b>两件必须知道的事</b>
检索零命中时，<code>answer</code> 会是「知识库中未找到相关内容。」且 <code>citations</code> 为空数组——
它不会为了给个交代而编。另外这一步通常要 25–45 秒，客户端超时请设到 120 秒以上，
否则会在它正常工作时被你自己掐断。</div>

<h2><span class='method'>POST</span><span class='endpoint'>{{ANSWER}}/read</span></h2>
<p>按文档编号逐页翻原文，用来核验结论。分页单位是 Unicode 字符，不是字节。</p>
<div class='scroll'><table>
  <tr><th>字段</th><th>类型</th><th>必填</th><th>说明</th></tr>
  <tr><td><code>doc_id</code></td><td>string</td><td>是</td><td>来自检索或问答返回的编号</td></tr>
  <tr><td><code>offset</code></td><td>integer</td><td>否</td><td>起始字符位置，默认 0</td></tr>
  <tr><td><code>length</code></td><td>integer</td><td>否</td><td>本页字符数，默认 65536</td></tr>
</table></div>
<p>响应 <code>200</code> 含 <code>doc_id</code>、<code>text</code>、<code>offset</code>、
   <code>eof</code>、<code>total_chars</code>。翻下一页：
   <code>下一个 offset = 当前 offset + 本页 text 的长度</code>，直到 <code>eof</code> 为 <code>true</code>。</p>
<table>
  <tr><th>状态码</th><th>含义</th></tr>
  <tr><td><code>400</code></td><td>缺 <code>doc_id</code>，或 <code>offset</code>/<code>length</code> 不是合法整数</td></tr>
  <tr><td><code>404</code></td><td>该编号不存在</td></tr>
  <tr><td><code>416</code></td><td><code>offset</code> 越界——按响应里的 <code>total_chars</code> 重算</td></tr>
  <tr><td><code>503</code></td><td>源文件暂时不可读</td></tr>
</table>

<h2>能不能开放给外部定制开发</h2>
<p>这套接口只在公司内网可达，没有公网入口，也没有面向外部的令牌签发流程。
   内部团队要在它上面做开发，申请一个自己的令牌即可，契约就是本页——它是稳定的，
   字段增减会先通知。</p>
<p>需要注意的是<strong>写操作不在 API 面上</strong>：建库、摄取、签发和吊销令牌都集中在管理侧，
   由管理员执行。这是有意的——读可以分散，写必须集中，否则审计和吊销都会失去意义。</p>
"""


_DOC_EXTEND = """
<p>有两种把这套东西接进你自己工作流的方式：接一个新的数据源进来，
   或者把知识库接到你自己的 Agent 上去。</p>

<h2>一、接一个新数据源</h2>
<p>新数据源写成一个<strong>适配器</strong>，放在 <code>adapters/</code> 目录，
   核心处理链一行都不用改。每个适配器实现四个操作：</p>
<div class='scroll'><table>
  <tr><th>操作</th><th>职责</th></tr>
  <tr><td><code>discover</code></td><td>增量枚举源里有什么，游标由适配器自己维护</td></tr>
  <tr><td><code>fetch</code></td><td>拉取单篇并规范化</td></tr>
  <tr><td><code>dedupe_key</code></td><td>产出全局唯一键 <code>&lt;源前缀&gt;-&lt;原ID&gt;</code>，跨源永不相撞</td></tr>
  <tr><td><code>watch</code></td><td>变更检测，指纹策略由各源自己定</td></tr>
</table></div>
<p>四个操作的出口统一为 <code>NormalizedDoc</code>，也就是现有的原文契约。
   正因为出口统一，下游的入库、编译、检索完全不需要知道多了一个源。
   完整契约见仓库里的 <code>docs/ADAPTER-CONTRACT.md</code>。</p>
<div class='callout'><b>三条不可破的约定</b>
源一律只读，不回写；原件不可变，内容变化写新版本而不是改旧的；
每个新适配器要带自己的测试，并在治理清单里登记归属——否则代码门会直接拒绝。</div>
<p>目前注册了两个适配器：工作协同镜像与云端文件库。
   「用户自己上传的文件」要成为第三个，走的就是这条路。</p>

<h2>二、接进你自己的 Agent</h2>
<p>两种做法，按你的场景选：</p>
<ul>
  <li><strong>装 Skill（推荐）</strong>：按<a href='/docs/quickstart'>快速开始</a>装上
      <code>cwk-kb-query</code>，之后直接对 Agent 说人话即可，检索、问答、读原文的
      调用细节由 Skill 负责。</li>
  <li><strong>直接调接口</strong>：你的程序自己发 HTTP 请求，契约见
      <a href='/docs/api'>API 参考</a>。适合要把结果嵌进自己产品的场景。</li>
</ul>

<h2>边界</h2>
<ul>
  <li>没有公网入口，所有接口只在公司内网可达。</li>
  <li>写操作（建库、摄取、签发令牌）不在 API 面上，集中在管理侧。</li>
  <li>令牌按 Agent 实例签发。你的程序如果跑在多台机器上，每台单独申请，
      不要共用一个令牌——共用会让吊销和审计同时失效。</li>
</ul>
"""


_DOC_FAQ = """
<h2>常见问题</h2>
<details><summary>查不到想要的东西，是不是库里就没有？</summary>
  <div class='body'>不一定。先换 2–3 种说法再下结论：换同义词、换更短的关键词、
  把编号或日期单独拿出来查。编号、日期、文件名这类走精确通道，
  用完整串比用描述更容易命中。</div></details>
<details><summary>返回 403 是什么意思？</summary>
  <div class='body'>令牌有效，但你访问的库不在授权范围内。这是库间隔离正常生效。
  确实需要那个库，联系{{CONTACT}}调整授权范围。</div></details>
<details><summary>返回 401 呢？</summary>
  <div class='body'>令牌缺失、过期或已被吊销。检查 <code>~/.openclaw/cwk/kb.env</code>
  是否已加载，仍不行就联系{{CONTACT}}重签。</div></details>
<details><summary>AI 问答要等多久？</summary>
  <div class='body'>通常 25–45 秒。客户端超时请设到 120 秒以上，
  否则会在它正常工作时被你自己掐断。</div></details>
<details><summary>能不能把令牌给同事一起用？</summary>
  <div class='body'>不能。令牌按 Agent 实例签发，共用会让吊销和审计都失去意义。
  同事按<a href='/docs/quickstart'>快速开始</a>自己申请一个，几分钟的事。</div></details>
<details><summary>我的提问和资料内容会被发到外部吗？</summary>
  <div class='body'>检索、读原文和模型生成都在公司内网的服务器上完成，
  原文不出那台机器。服务本身也不保存你的问题和生成的答案。</div></details>
<details><summary>语料多久更新一次？</summary>
  <div class='body'>语料是定期生成的快照，不是实时镜像。刚发生的事可能还没进库。
  要确认某份材料是否已入库，问{{CONTACT}}。</div></details>

<h2>边界与承诺</h2>
<p>这些不是免责声明，是这套东西的设计取舍。先知道它不做什么，用起来才不会误判。</p>
<ul class='rules'>
  <li><b>只读</b><span>不会替你标已读、回复、处理待办或删除任何东西。</span></li>
  <li><b>找不到就说找不到</b><span>零命中时体面拒答，不编造。拒答不等于全库没有——
      先换两三种说法再下结论。</span></li>
  <li><b>可追溯</b><span>每条事实性回答都带引用或原文页；你可以逐页翻开核对。</span></li>
  <li><b>语料是快照</b><span>不是实时镜像。要确认最新内容是否已入库，问{{CONTACT}}。</span></li>
  <li><b>库间隔离</b><span>访问未授权的库会被拒绝。这是隔离生效，不是服务故障。</span></li>
  <li><b>令牌按实例发放</b><span>不要复制给其他机器或 Agent 共用；
      怀疑泄露立刻联系{{CONTACT}}吊销重签。</span></li>
</ul>
"""


_DOC_BODIES = {
    "quickstart": _DOC_QUICKSTART,
    "architecture": _DOC_ARCHITECTURE,
    "api": _DOC_API,
    "extend": _DOC_EXTEND,
    "faq": _DOC_FAQ,
}


def _shell(title: str, body: str, *, active: str = "") -> str:
    """站点外壳：所有页面共用同一套头尾，导航高亮由 active 决定。"""
    def mark(key: str) -> str:
        return " aria-current='page'" if active == key else ""
    return (
        "<!doctype html>\n<html lang='zh-CN'><head><meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        f"<title>{title}</title>\n<style>{_CSS}</style></head>\n<body>\n"
        "<nav><div class='wrap'>\n"
        "  <strong><a href='/' style='text-decoration:none;color:inherit'>CWK 知识库</a></strong>\n"
        f"  <a href='/'{mark('home')}>首页</a>\n"
        f"  <a href='/docs'{mark('docs')}>文档中心</a>\n"
        "  <span class='spacer'></span>\n"
        "  <a href='/docs/quickstart'>快速开始</a>\n"
        "</div></nav>\n"
        f"{body}\n"
        "<footer><div class='wrap'>CWK 知识库服务 · 内部使用 · "
        "<a href='/docs'>文档中心</a></div></footer>\n"
        f"{_SCRIPT}\n</body></html>\n"
    )


def _doc_page(slug: str, title: str, sub: str, body: str) -> str:
    """文档页布局：左侧栏固定，右侧正文。"""
    links = "".join(
        "<a href='/docs/{s}'{cur}>{t}</a>".format(
            s=s, t=t, cur=" aria-current='page'" if s == slug else "")
        for s, t, _ in _DOC_NAV
    )
    return (
        "<div class='doclayout'>\n"
        f"<aside class='sidebar'><p class='sgroup'>文档中心</p>{links}</aside>\n"
        f"<article class='doc'><h1>{title}</h1><p class='sub'>{sub}</p>\n{body}\n</article>\n"
        "</div>"
    )


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
        # The site is static and public-by-design; it still should not be
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
    parser = argparse.ArgumentParser(description="CWK 知识库服务官网（静态页面，无数据访问）")
    parser.add_argument("--host", default=os.environ.get(ENV_HOST, DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, DEFAULT_PORT)))
    args = parser.parse_args(argv)
    app = PortalApp()
    server = PortalHTTPServer((args.host, args.port), app)
    print(f"kb_portal: http://{args.host}:{args.port}/ （静态官网；控制台在别处，本进程无管理数据）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
