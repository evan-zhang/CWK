#!/usr/bin/env python3
"""Render a human-readable CWK daily Markdown digest as standalone HTML."""

from __future__ import annotations

import argparse
import html
import re
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def inline_markdown(value: str) -> str:
    text = esc(value)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def render_blocks(markdown: str) -> str:
    blocks: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>\n" + "\n".join(list_items) + "\n</ul>")
            list_items = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code:
                blocks.append(f"<pre><code>{esc(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(len(heading.group(1)), 3)
            blocks.append(f"<h{level}>{inline_markdown(heading.group(2).strip())}</h{level}>")
            continue
        bullet = re.match(r"^-\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            list_items.append(f"<li>{inline_markdown(bullet.group(1).strip())}</li>")
            continue
        if line.startswith("  ") and list_items:
            addition = inline_markdown(line.strip())
            list_items[-1] = list_items[-1].replace("</li>", f"<div class=\"li-note\">{addition}</div></li>")
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    flush_list()
    if in_code and code_lines:
        blocks.append(f"<pre><code>{esc(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(blocks)


def title_from_markdown(markdown: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown, re.M)
    return match.group(1).strip() if match else "工作协同每日简报"


def daily_view_sections(markdown: str) -> dict[str, str] | None:
    lines = markdown.splitlines()
    positions: dict[str, int] = {}
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            headings.append((index, match.group(1)))
            if match.group(1) in {"今日处理", "近期变更", "持续未闭环"}:
                positions[match.group(1)] = index
    if not all(name in positions for name in ("今日处理", "近期变更", "持续未闭环")):
        return None

    def section(name: str) -> str:
        start = positions[name] + 1
        end = next((index for index, _ in headings if index >= start), len(lines))
        return "\n".join(lines[start:end]).strip()

    first = min(positions.values())
    ongoing_end = next((index for index, _ in headings if index > positions["持续未闭环"]), len(lines))
    return {
        "intro": "\n".join(lines[:first]).strip(),
        "today": section("今日处理"),
        "recent_changes": section("近期变更"),
        "ongoing": section("持续未闭环"),
        "rest": "\n".join(lines[ongoing_end:]).strip(),
    }


def render_html(markdown: str, source_name: str = "") -> str:
    title = title_from_markdown(markdown)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = daily_view_sections(markdown)
    if sections:
        def declared_count(label: str, content: str) -> int:
            match = re.search(rf"{re.escape(label)}\s+(\d+)\s+条", sections["intro"])
            return int(match.group(1)) if match else len(re.findall(r"^-\s+", content, re.M))

        today_count = declared_count("今日处理", sections["today"])
        changed_count = declared_count("近期变更", sections["recent_changes"])
        ongoing_count = declared_count("持续未闭环", sections["ongoing"])
        body = f'''
        {render_blocks(sections["intro"])}
        <nav class="daily-tabs" aria-label="日报视图">
          <button class="active" data-daily-tab="today">今日处理 {today_count}</button>
          <button data-daily-tab="recent_changes">近期变更 {changed_count}</button>
        </nav>
        <section class="daily-panel" data-daily-panel="today">
          <h2>今日处理</h2>
          {render_blocks(sections["today"])}
          <details><summary>持续未闭环 {ongoing_count}</summary>{render_blocks(sections["ongoing"])}</details>
        </section>
        <section class="daily-panel" data-daily-panel="recent_changes" hidden>
          <h2>近期变更</h2>
          {render_blocks(sections["recent_changes"])}
        </section>
        {render_blocks(sections["rest"])}
        '''
    else:
        body = render_blocks(markdown)
    css = """
    :root {
      --ink: #25313d;
      --muted: #607080;
      --line: #d9e1e8;
      --paper: #f7f9fb;
      --panel: #ffffff;
      --accent: #2f6f8f;
      --accent-soft: #e9f5f9;
      --warn: #9a5a13;
      --warn-soft: #fff6df;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
      line-height: 1.72;
    }
    .page {
      width: min(980px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 56px;
    }
    header {
      padding: 24px 0 20px;
      border-bottom: 1px solid var(--line);
    }
    .eyebrow {
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 13px;
    }
    h1 {
      margin: 0;
      font-size: 30px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    article {
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px 24px 24px;
    }
    article > h1:first-child { display: none; }
    .daily-tabs {
      display: flex;
      gap: 8px;
      margin: 20px 0 14px;
      overflow-x: auto;
    }
    .daily-tabs button {
      border: 1px solid var(--line);
      border-radius: 9px;
      background: #fff;
      color: var(--ink);
      padding: 9px 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .daily-tabs button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    .daily-panel[hidden] { display: none; }
    details {
      margin-top: 18px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--accent-soft);
    }
    details > summary { cursor: pointer; font-weight: 700; }
    h2 {
      margin: 24px 0 10px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
      font-size: 21px;
      line-height: 1.35;
      letter-spacing: 0;
    }
    h2:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
    h3 {
      margin: 18px 0 8px;
      font-size: 17px;
      line-height: 1.4;
      letter-spacing: 0;
    }
    p {
      margin: 8px 0 12px;
      color: #344250;
    }
    ul {
      margin: 8px 0 16px;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 8px;
    }
    li {
      position: relative;
      padding: 10px 12px 10px 18px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      background: #fbfdfe;
    }
    li::before {
      content: "";
      position: absolute;
      left: 8px;
      top: 20px;
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: var(--accent);
    }
    .li-note {
      margin-top: 5px;
      color: var(--muted);
      font-size: 14px;
    }
    code {
      padding: 1px 5px;
      border-radius: 5px;
      background: #edf1f4;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }
    pre {
      overflow: auto;
      padding: 14px;
      border-radius: 8px;
      background: #202b34;
      color: #edf5f8;
    }
    footer {
      margin-top: 18px;
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 640px) {
      .page { width: min(100% - 22px, 980px); padding-top: 12px; }
      h1 { font-size: 24px; }
      article { padding: 16px 14px 18px; }
      li { padding-right: 10px; }
    }
    @media print {
      body { background: #fff; }
      .page { width: auto; padding: 0; }
      article { border: 0; padding: 0; }
    }
    """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>{css}</style>
</head>
<body>
  <main class="page">
    <header>
      <p class="eyebrow">工作协同镜像 · 每日人读版</p>
      <h1>{esc(title)}</h1>
      <div class="meta">
        <span>生成：{esc(generated)}</span>
        <span>源文件：{esc(source_name or "daily Markdown")}</span>
        <span>只读分析，未修改工作协同状态</span>
      </div>
    </header>
    <article>
      {body}
    </article>
    <footer>Markdown 是长期知识源；HTML 是阅读发布件。两者内容同源生成。</footer>
  </main>
  <script>
    document.querySelectorAll('[data-daily-tab]').forEach(button => button.addEventListener('click', () => {{
      document.querySelectorAll('[data-daily-tab]').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
      document.querySelectorAll('[data-daily-panel]').forEach(panel => {{
        panel.hidden = panel.dataset.dailyPanel !== button.dataset.dailyTab;
      }});
    }}));
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render CWK daily Markdown digest as standalone HTML.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    markdown = source.read_text(encoding="utf-8")
    try:
        source_name = str(source.relative_to(PROJECT))
    except ValueError:
        source_name = str(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(markdown, source_name), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
