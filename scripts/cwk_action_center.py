#!/usr/bin/env python3
"""Build the RT-002 Shadow Mode action center without CWork mutations."""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cwk_ai_common import PROJECT, contains_sensitive_text, load_json, parse_frontmatter, write_json


SCHEMA = "cwk.action_cards.v1"
TYPE_ORDER = {
    "decision_todo": 0,
    "advice_todo": 1,
    "reactivated_todo": 2,
    "update_notice": 3,
    "inbox_awareness": 4,
    "historical": 5,
}

DECISION_MARKERS = ("决策人", "最终决策", "审批人", "待审批", "决策节点", "审批节点")
ADVICE_MARKERS = ("建议人", "征询意见", "提供建议", "给出建议", "意见节点", "待建议")

ACTION_LABELS = {
    "approve": "同意（预览）",
    "conditional_approve": "有条件同意（预览）",
    "reject": "不同意（预览）",
    "ignore": "忽略/无需处理（预览）",
    "transfer": "转办（预览）",
    "defer": "暂缓处理",
    "submit_advice": "提交建议（预览）",
    "express_opinion": "我要发表意见",
    "acknowledge_local": "本地已知悉",
    "follow": "加入跟踪",
    "snooze": "稍后再看",
    "open_source": "查看原文信息",
}

ACTION_CONSEQUENCES = {
    "approve": "拟向原待办提交同意意见并结束当前决策节点。",
    "conditional_approve": "拟提交附带条件的同意意见；是否结束节点需在真实执行前核实。",
    "reject": "拟向原待办提交不同意意见并结束当前决策节点。",
    "ignore": "仅生成忽略/无需处理的操作预览；不会推断是否结束原待办。",
    "transfer": "拟转交给指定人员；Shadow Mode 不解析或提交目标人员。",
    "defer": "仅在工作台本地暂缓，不改变 CWork 状态。",
    "submit_advice": "拟把编辑后的建议写入原待办；Shadow Mode 不提交。",
    "express_opinion": "拟把编辑后的意见发布到原工作协同；Shadow Mode 不提交。",
    "acknowledge_local": "仅在本页面标记知悉，不标记 CWork 已读。",
    "follow": "仅在本地加入后续跟踪计划。",
    "snooze": "仅在本页面稍后提醒，不改变 CWork 状态。",
    "open_source": "显示 report_id 和已有来源信息，不执行外部跳转。",
}


@dataclass
class SourceRecord:
    report_id: str
    title: str
    writer: str
    create_time: str
    source_lane: str
    collection_mode: str
    change_type: str
    source_scopes: set[str]
    raw_path: Path
    row: dict[str, Any]
    node: dict[str, Any]


def embedded_json(text: str, heading: str) -> dict[str, Any]:
    pattern = rf"## {re.escape(heading)}\s+```json\s*(\{{.*?\}})\s*```"
    match = re.search(pattern, text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_sources(run_dir: Path) -> list[SourceRecord]:
    records = []
    for path in sorted((run_dir / "raw").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        meta, _ = parse_frontmatter(text)
        report_id = str(meta.get("report_id") or path.name.split("-", 1)[0]).strip()
        if not report_id:
            continue
        scopes = {value for value in str(meta.get("source_scopes") or "").split(",") if value}
        records.append(
            SourceRecord(
                report_id=report_id,
                title=str(meta.get("title") or path.stem),
                writer=str(meta.get("writer") or ""),
                create_time=str(meta.get("create_time") or ""),
                source_lane=str(meta.get("source_lane") or "unknown"),
                collection_mode=str(meta.get("collection_mode") or "unknown"),
                change_type=str(meta.get("change_type") or "unknown"),
                source_scopes=scopes,
                raw_path=path,
                row=embedded_json(text, "List Row Metadata"),
                node=embedded_json(text, "Node / Opinion Chain"),
            )
        )
    return records


def classify(record: SourceRecord) -> dict[str, Any]:
    scopes = record.source_scopes
    is_todo = record.source_lane == "todo_backed" or bool(scopes & {"todo", "todo_pending", "pending"})
    historical = record.change_type == "historical_backfill" or record.collection_mode == "historical-backfill"
    structured_role_text = json.dumps({"row": record.row, "node": record.node}, ensure_ascii=False)
    if any(marker in structured_role_text for marker in DECISION_MARKERS):
        role = "decision_maker"
        role_evidence = next(marker for marker in DECISION_MARKERS if marker in structured_role_text)
    elif any(marker in structured_role_text for marker in ADVICE_MARKERS):
        role = "advisor"
        role_evidence = next(marker for marker in ADVICE_MARKERS if marker in structured_role_text)
    else:
        role = "unknown" if is_todo else "recipient"
        role_evidence = "todo scope without explicit role" if is_todo else "non-todo recipient scope"

    updated = record.change_type == "updated" or record.source_lane == "reply_chain" or bool(record.row.get("hasNewReply"))
    if historical:
        primary_type = "historical"
    elif is_todo and updated:
        primary_type = "reactivated_todo"
    elif is_todo and role == "decision_maker":
        primary_type = "decision_todo"
    elif is_todo:
        primary_type = "advice_todo"
    elif updated:
        primary_type = "update_notice"
    else:
        primary_type = "inbox_awareness"

    return {
        "primary_type": primary_type,
        "role": role,
        "underlying_role": role,
        "mandatory": is_todo and not historical,
        "requires_role_review": is_todo and role == "unknown",
        "role_evidence": role_evidence,
        "confidence": 0.95 if role in {"decision_maker", "advisor"} else 0.65 if not is_todo else 0.35,
    }


def allowed_actions(classification: dict[str, Any]) -> list[dict[str, str]]:
    primary = classification["primary_type"]
    role = classification["role"]
    if primary == "historical":
        codes = ["open_source", "follow"]
    elif role == "decision_maker" and not classification["requires_role_review"]:
        codes = ["approve", "conditional_approve", "reject", "ignore", "transfer", "defer", "open_source"]
    elif primary in {"advice_todo", "reactivated_todo"}:
        codes = ["submit_advice", "transfer", "defer", "open_source"]
    else:
        codes = ["acknowledge_local", "express_opinion", "follow", "snooze", "open_source"]
    return [{"code": code, "label": ACTION_LABELS[code], "consequence": ACTION_CONSEQUENCES[code]} for code in codes]


def first_text(values: list[Any], *keys: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in keys:
                if value.get(key):
                    return str(value[key]).strip()
    return ""


def build_card(record: SourceRecord, extracted: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    classification = classify(record)
    summary = str(ai.get("summary") or first_text(extracted.get("open_loops", []), "text") or extracted.get("summary") or record.title)
    actions = ai.get("action_items") or []
    decisions = ai.get("decisions") or []
    risks = ai.get("risks") or extracted.get("risks") or []
    suggested = first_text(actions, "task") or first_text(decisions, "text")
    if classification["role"] == "decision_maker":
        recommendation = "请核对前序意见、风险和执行条件后作出最终决策。"
    elif classification["mandatory"]:
        recommendation = "请结合原文和历史背景提交明确、可执行的建议。"
    elif classification["primary_type"] == "update_notice":
        recommendation = "先阅读本次变化；只有在影响既有判断时才发表意见。"
    else:
        recommendation = "默认知悉即可；如需推动事项，可选择发表意见。"
    draft = suggested or ("建议补充关键依据、责任人和完成时间。" if classification["mandatory"] else "我已了解。如需推进，请补充下一步责任人和时间安排。")
    evidence = [{"report_id": record.report_id, "source": str(record.raw_path.relative_to(PROJECT))}]
    for ref in ai.get("evidence_refs", [])[:3]:
        if isinstance(ref, dict) and ref.get("quote"):
            evidence.append({"report_id": str(ref.get("report_id") or record.report_id), "quote": str(ref["quote"])})
    return {
        "schema_version": "cwk.action_card.v1",
        "report_id": record.report_id,
        "title": record.title,
        "writer": record.writer,
        "create_time": record.create_time,
        **classification,
        "source_labels": sorted(record.source_scopes | {record.source_lane, record.change_type}),
        "summary": summary[:800],
        "new_since_last_seen": "本次存在新回复或内容变化。" if record.change_type == "updated" or record.source_lane == "reply_chain" else "未检测到独立更新摘要。",
        "recommendation": recommendation,
        "draft_text": draft[:800],
        "risks": risks[:5],
        "allowed_actions": allowed_actions(classification),
        "evidence_refs": evidence,
        "shadow_mode": True,
    }


def choose_unique(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for card in cards:
        current = chosen.get(card["report_id"])
        if current is None:
            chosen[card["report_id"]] = card
        elif TYPE_ORDER[card["primary_type"]] < TYPE_ORDER[current["primary_type"]]:
            card["source_labels"] = sorted(set(current["source_labels"]) | set(card["source_labels"]))
            chosen[card["report_id"]] = card
        elif current:
            current["source_labels"] = sorted(set(current["source_labels"]) | set(card["source_labels"]))
    return sorted(chosen.values(), key=lambda item: (TYPE_ORDER[item["primary_type"]], item["create_time"], item["report_id"]))


def build_cards(run_dir: Path) -> dict[str, Any]:
    extracted_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "extracted").glob("*.json")):
        payload = load_json(path)
        for report_id in payload.get("source_ids", []):
            extracted_by_id[str(report_id)] = payload
    ai_by_id = {path.stem: load_json(path) for path in sorted((run_dir / "ai-understanding").glob("*.json"))}
    cards = choose_unique([build_card(record, extracted_by_id.get(record.report_id, {}), ai_by_id.get(record.report_id, {})) for record in load_sources(run_dir)])
    counts: dict[str, int] = {key: 0 for key in TYPE_ORDER}
    for card in cards:
        counts[card["primary_type"]] += 1
    return {"schema_version": SCHEMA, "run_name": run_dir.name, "shadow_mode": True, "counts": counts, "cards": cards}


def render_markdown(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    mandatory = counts["decision_todo"] + counts["advice_todo"] + counts["reactivated_todo"]
    lines = [
        "# 工作协同办理中心（Shadow Mode）",
        "",
        "> 当前页面只生成办理建议和操作预览，不会提交到 CWork。",
        "",
        f"本轮有 **{mandatory}** 项必须处理、**{counts['update_notice'] + counts['inbox_awareness']}** 项建议阅读、**{counts['historical']}** 项历史回填。",
        "",
    ]
    labels = {"decision_todo": "待我决策", "advice_todo": "待我给建议", "reactivated_todo": "更新后重新需要处理", "update_notice": "更新通知", "inbox_awareness": "普通知悉", "historical": "历史回填"}
    for primary in TYPE_ORDER:
        group = [card for card in payload["cards"] if card["primary_type"] == primary]
        if not group:
            continue
        lines += [f"## {labels[primary]}", ""]
        for card in group:
            lines += [
                f"### {card['title']}",
                "",
                f"- 角色：`{card['role']}`；{'必须处理' if card['mandatory'] else '意见可选'}",
                f"- 摘要：{card['summary']}",
                f"- 建议：{card['recommendation']}",
                f"- 草稿：{card['draft_text']}",
                f"- 操作预览：{' / '.join(action['label'] for action in card['allowed_actions'])}",
                f"- 证据：`{card['report_id']}`",
                "",
            ]
    return "\n".join(lines)


def render_html(payload: dict[str, Any]) -> str:
    cards = payload["cards"]
    counts = payload["counts"]
    mandatory = counts["decision_todo"] + counts["advice_todo"] + counts["reactivated_todo"]
    labels = {"decision_todo": "待我决策", "advice_todo": "待我给建议", "reactivated_todo": "重新激活", "update_notice": "更新通知", "inbox_awareness": "普通知悉", "historical": "历史回填"}
    card_html = []
    for card in cards:
        actions = "".join(
            f'<button class="action" data-report="{html.escape(card["report_id"])}" data-action="{html.escape(action["code"])}">{html.escape(action["label"])}</button>'
            for action in card["allowed_actions"]
        )
        review = '<span class="review">角色待复核</span>' if card["requires_role_review"] else ""
        card_html.append(f'''<article class="card" data-type="{card['primary_type']}">
          <div class="meta"><span class="type">{labels[card['primary_type']]}</span><span class="{'must' if card['mandatory'] else 'optional'}">{'必须处理' if card['mandatory'] else '意见可选'}</span>{review}</div>
          <h2>{html.escape(card['title'])}</h2>
          <p class="summary">{html.escape(card['summary'])}</p>
          <div class="recommend"><strong>AI 办理建议</strong><p>{html.escape(card['recommendation'])}</p></div>
          <p class="draft"><strong>建议草稿：</strong>{html.escape(card['draft_text'])}</p>
          <p class="evidence">证据 report_id：{html.escape(card['report_id'])}</p>
          <div class="actions">{actions}</div>
        </article>''')
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工作协同办理中心</title>
<style>:root{{--bg:#f5f7fb;--card:#fff;--ink:#172033;--muted:#697386;--brand:#3157d5;--danger:#b42318}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:980px;margin:auto;padding:24px}}header{{background:linear-gradient(135deg,#18254b,#3157d5);color:white;padding:28px;border-radius:20px}}.shadow{{display:inline-block;background:#fff3cd;color:#7a5200;padding:5px 10px;border-radius:999px;font-weight:700}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}}.stat{{background:#ffffff1c;padding:10px 14px;border-radius:12px}}nav{{display:flex;gap:8px;overflow:auto;padding:18px 0}}nav button,.action{{border:1px solid #d7ddea;background:white;padding:9px 12px;border-radius:10px;cursor:pointer}}nav button.active{{background:var(--brand);color:white}}.card{{background:var(--card);border-radius:18px;padding:20px;margin:0 0 14px;box-shadow:0 7px 24px #18254b12}}.meta{{display:flex;gap:8px;flex-wrap:wrap}}.meta span{{font-size:12px;padding:3px 8px;border-radius:99px;background:#eef2ff}}.meta .must{{background:#fee4e2;color:var(--danger)}}.meta .optional{{background:#e8f7ef;color:#067647}}.meta .review{{background:#fff3cd;color:#7a5200}}h2{{font-size:19px;margin:12px 0 8px}}.summary,.draft{{color:#344054}}.recommend{{background:#f0f4ff;padding:12px 14px;border-radius:12px}}.recommend p{{margin:3px 0}}.evidence{{font-size:12px;color:var(--muted)}}.actions{{display:flex;gap:8px;flex-wrap:wrap}}.action:hover{{border-color:var(--brand);color:var(--brand)}}dialog{{width:min(680px,92vw);border:0;border-radius:18px;padding:0;box-shadow:0 20px 70px #0004}}dialog::backdrop{{background:#10182899}}.modal{{padding:22px}}textarea{{width:100%;min-height:130px;padding:12px;border:1px solid #ccd3df;border-radius:10px}}pre{{white-space:pre-wrap;background:#101828;color:#e4e7ec;padding:12px;border-radius:10px;max-height:240px;overflow:auto}}.warning{{color:#b54708;font-weight:700}}.modal-actions{{display:flex;gap:8px;justify-content:flex-end}}@media(max-width:600px){{main{{padding:12px}}header{{padding:20px}}.card{{padding:16px}}}}</style></head>
<body><main><header><span class="shadow">Shadow Mode · 不会提交到 CWork</span><h1>今日工作协同办理中心</h1><p>先处理必须办理事项，再阅读更新和普通收件。</p><div class="stats"><div class="stat"><strong>{mandatory}</strong> 项必须处理</div><div class="stat"><strong>{counts['update_notice'] + counts['inbox_awareness']}</strong> 项建议阅读</div><div class="stat"><strong>{counts['historical']}</strong> 项历史回填</div></div></header>
<nav><button class="active" data-filter="all">全部</button><button data-filter="decision_todo">待决策</button><button data-filter="advice_todo">待建议</button><button data-filter="reactivated_todo">重新激活</button><button data-filter="update_notice">更新</button><button data-filter="inbox_awareness">收件</button></nav>
<section id="cards">{''.join(card_html)}</section></main>
<dialog id="preview"><div class="modal"><h2 id="modal-title">操作预览</h2><p class="warning">这是 Shadow Mode。确认只保存本页预览，不会向 CWork 提交、审批、回复、转办、完成或标已读。</p><label>意见或附加条件<textarea id="opinion"></textarea></label><p id="consequence"></p><button id="build-preview">生成操作预览</button><pre id="payload">尚未生成</pre><div class="modal-actions"><button id="close-preview">关闭</button><button id="confirm-preview">确认此预览（不提交）</button></div></div></dialog>
<script type="application/json" id="cards-data">{data}</script><script>
const payload=JSON.parse(document.getElementById('cards-data').textContent);const byId=Object.fromEntries(payload.cards.map(c=>[c.report_id,c]));let current=null;
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelectorAll('.card').forEach(c=>c.hidden=b.dataset.filter!=='all'&&c.dataset.type!==b.dataset.filter)}}));
document.querySelectorAll('.action').forEach(b=>b.addEventListener('click',()=>{{const c=byId[b.dataset.report],a=c.allowed_actions.find(x=>x.code===b.dataset.action);current={{card:c,action:a}};document.getElementById('modal-title').textContent=a.label+' · '+c.title;document.getElementById('opinion').value=c.draft_text;document.getElementById('consequence').textContent=a.consequence;document.getElementById('payload').textContent='尚未生成';document.getElementById('preview').showModal()}}));
document.getElementById('build-preview').addEventListener('click',()=>{{if(!current)return;document.getElementById('payload').textContent=JSON.stringify({{mode:'shadow',report_id:current.card.report_id,action:current.action.code,text:document.getElementById('opinion').value,consequence:current.action.consequence,will_submit:false}},null,2)}});
document.getElementById('confirm-preview').addEventListener('click',()=>{{document.getElementById('payload').textContent+='\n\n已确认预览：未提交到 CWork。'}});document.getElementById('close-preview').addEventListener('click',()=>document.getElementById('preview').close());
</script></body></html>'''


def assert_shadow_safe(text: str) -> None:
    forbidden = ("fetch(", "XMLHttpRequest", "cwork-todo.py", "cwork-reply", "approve(", "complete(")
    found = [value for value in forbidden if value in text]
    if found:
        raise RuntimeError("Shadow Mode output contains forbidden write capability: " + ", ".join(found))
    if contains_sensitive_text(text):
        raise RuntimeError("Shadow Mode output blocked by secret gate")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the RT-002 Shadow Mode action center.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    run_dir = PROJECT / "runs" / args.run_name
    if not run_dir.exists():
        raise SystemExit(f"run not found: {run_dir}")
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else run_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_cards(run_dir)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    md_text = render_markdown(payload)
    html_text = render_html(payload)
    for text in (json_text, md_text, html_text):
        assert_shadow_safe(text)
    write_json(output_dir / "action-cards.json", payload)
    (output_dir / "action-center.md").write_text(md_text, encoding="utf-8")
    (output_dir / "action-center.html").write_text(html_text, encoding="utf-8")
    print(json.dumps({"run_name": args.run_name, "card_count": len(payload["cards"]), "counts": payload["counts"], "shadow_mode": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
