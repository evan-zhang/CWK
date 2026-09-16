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

    def page(self) -> str:
        retrieval = html.escape(self.retrieval_base)
        answer = html.escape(self.answer_base)
        replacements = {
            "{{BANK_CARDS}}": self._bank_cards(),
            "{{BANK_CHECKLIST}}": html.escape(self._bank_checklist()),
            "{{FIRST_BANK}}": html.escape(self._first_bank()),
            "{{CONSOLE_BLOCK}}": self._console_block(),
            "{{RUNBOOK_LINE}}": self._runbook_line(),
            "{{CONTACT}}": html.escape(self.contact),
            "{{RETRIEVAL}}": retrieval,
            "{{ANSWER}}": answer,
            "{{REPO}}": html.escape(self.repo_url),
        }
        page = _PAGE
        for marker, value in replacements.items():
            page = page.replace(marker, value)
        return page

    def handle(self, method: str, path: str) -> tuple[int, str, bytes, dict[str, str]]:
        """Return ``(status, content_type, body, headers)``.

        Anything that is not the site or the health probe is a 404 —
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
</style></head>
<body>

<nav><div class='wrap'>
  <strong>CWK 知识库</strong>
  <a href='#value'>能解决什么</a>
  <a href='#features'>功能</a>
  <a href='#setup'>接入</a>
  <a href='#limits'>边界</a>
  <a href='#faq'>常见问题</a>
  <span class='spacer'></span>
  <a href='#admin'>管理员</a>
</div></nav>

<header><div class='wrap'>
  <span class='eyebrow'>内部知识库服务</span>
  <h1>让 AI 替你翻遍公司资料，<br>每句结论都能追回原文</h1>
  <p>把授权范围内的工作汇报、投前资料和年度规划变成可检索的知识库。
     问一句话，拿到结论和出处；库里没有的，它会直接说没有，而不是编一个。</p>
  <div class='ctas'>
    <a class='cta' href='#setup'>开始接入</a>
    {{CONSOLE_BLOCK}}
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

<section id='setup'><div class='wrap'>
  <h2>怎么接入</h2>
  <p class='lede'>前置条件：你的机器和知识库服务在同一局域网，并且以 OpenClaw 运行 Agent。
     下面五步做完就能用，全程大约十分钟。</p>

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
      <p class='note'>若显示 excluded / not visible：让该 Agent 新起一轮对话重载 skills；仍不行就带着现象找{{CONTACT}}，不要改其他配置。</p>
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

  {{RUNBOOK_LINE}}

  <h3 style='margin-top:2.5rem'>装好之后怎么问</h3>
  <p class='note' style='margin-bottom:.6rem'>日常直接对你的 Agent 说人话就行：</p>
  <div class='code'><button onclick='copyBlock(this)'>复制</button><pre><code>用 cwk-kb-query 查 {{FIRST_BANK}} 库：上个月关于 XX 项目的进展汇报
用 cwk-kb-query 查 {{FIRST_BANK}} 库：编号 ABC-2026-007 对应的是什么事
用 cwk-kb-query 问 {{FIRST_BANK}} 库：这个项目目前卡在哪一步，给出处</code></pre></div>
</div></section>

<section id='limits'><div class='wrap'>
  <h2>边界与承诺</h2>
  <p class='lede'>这些不是免责声明，是这套东西的设计取舍。先知道它不做什么，用起来才不会误判。</p>
  <ul class='rules'>
    <li><b>只读</b><span>不会替你标已读、回复、处理待办或删除任何东西。</span></li>
    <li><b>找不到就说找不到</b><span>零命中时体面拒答，不编造。拒答不等于全库没有——先换两三种说法再下结论。</span></li>
    <li><b>可追溯</b><span>每条事实性回答都带引用或原文页；你可以逐页翻开核对。</span></li>
    <li><b>语料是快照</b><span>不是实时镜像。要确认最新内容是否已入库，问{{CONTACT}}。</span></li>
    <li><b>库间隔离</b><span>访问未授权的库会被拒绝。这是隔离生效，不是服务故障。</span></li>
    <li><b>令牌按实例发放</b><span>不要复制给其他机器或 Agent 共用；怀疑泄露立刻联系{{CONTACT}}吊销重签。</span></li>
  </ul>
</div></section>

<section id='faq'><div class='wrap'>
  <h2>常见问题</h2>
  <details><summary>查不到想要的东西，是不是库里就没有？</summary>
    <div class='body'>不一定。先换 2–3 种说法再下结论：换同义词、换更短的关键词、把编号或日期单独拿出来查。
    编号、日期、文件名这类走精确通道，用完整串比用描述更容易命中。</div></details>
  <details><summary>返回 403 是什么意思？</summary>
    <div class='body'>令牌有效，但你访问的库不在授权范围内。这是库间隔离正常生效。
    确实需要那个库，联系{{CONTACT}}调整授权范围。</div></details>
  <details><summary>返回 401 呢？</summary>
    <div class='body'>令牌缺失、过期或已被吊销。检查 <code>~/.openclaw/cwk/kb.env</code> 是否已加载，
    仍不行就联系{{CONTACT}}重签。</div></details>
  <details><summary>AI 问答要等多久？</summary>
    <div class='body'>通常 25–45 秒。客户端超时请设到 120 秒以上，否则会在它正常工作时被你自己掐断。</div></details>
  <details><summary>能不能把令牌给同事一起用？</summary>
    <div class='body'>不能。令牌按 Agent 实例签发，共用会让吊销和审计都失去意义。
    同事按本页流程自己申请一个，几分钟的事。</div></details>
  <details><summary>我的提问和资料内容会被发到外部吗？</summary>
    <div class='body'>检索与原文读取全程在公司内网完成。具体到 AI 问答环节使用的模型部署位置，
    以{{CONTACT}}的说明为准。</div></details>
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

<footer><div class='wrap'>CWK 知识库服务 · 内部使用 · 有问题找{{CONTACT}}</div></footer>

<script>
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
</script>
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
