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

from cwk_ai_common import PROJECT, load_json, parse_frontmatter, write_json
from cwk_person_relation import classify_person_relation, load_relationship_manifest


SCHEMA = "cwk.action_cards.v2"
TYPE_ORDER = {
    "decision_todo": 0,
    "advice_todo": 1,
    "action_todo": 2,
    "reactivated_todo": 3,
    "update_notice": 4,
    "inbox_awareness": 5,
    "historical": 6,
}

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
    simple: dict[str, Any]
    node: dict[str, Any]


def date_part(value: str) -> str:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value or "")
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def strict_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true") or value == 1


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
        fallback_match = re.search(r"\*\*时间\*\*:\s*([^\n]+)", text) or re.search(r"Create Time:\s*([^\n]+)", text)
        fallback_time = fallback_match.group(1).strip() if fallback_match else ""
        row = embedded_json(text, "List Row Metadata")
        simple = embedded_json(text, "Record Simple Info")
        node = embedded_json(text, "Node / Opinion Chain")
        fallback_writer = str(simple.get("writeEmpName") or node.get("writeEmpName") or "")
        records.append(
            SourceRecord(
                report_id=report_id,
                title=str(meta.get("title") or path.stem),
                writer=str(meta.get("writer") or fallback_writer),
                create_time=str(meta.get("create_time") or fallback_time or ""),
                source_lane=str(meta.get("source_lane") or "unknown"),
                collection_mode=str(meta.get("collection_mode") or "unknown"),
                change_type=str(meta.get("change_type") or "unknown"),
                source_scopes=scopes,
                raw_path=path,
                row=row,
                simple=simple,
                node=node,
            )
        )
    return records


def classify(
    record: SourceRecord,
    report_date: str = "",
    owner_emp_id: str = "",
    owner_name: str = "",
    backend_relation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scopes = record.source_scopes
    is_todo = record.source_lane == "todo_backed" or bool(scopes & {"todo", "todo_pending"})
    business_date = date_part(record.create_time)
    current_date_backfill = bool(report_date and business_date == report_date and (
        record.change_type == "historical_backfill" or record.collection_mode == "historical-backfill"
    ))
    historical = (
        record.change_type == "historical_backfill" or record.collection_mode == "historical-backfill"
    ) and not current_date_backfill
    effective_change_type = "new" if current_date_backfill else record.change_type
    relationship = classify_person_relation(
        backend_relation=backend_relation,
    )
    role = relationship["relationship_role"]
    role_evidence = relationship["relationship_evidence"]
    pending_actions = set(relationship.get("relationship_pending_actions") or [])

    updated = record.change_type == "updated" or (
        record.change_type in {"", "unknown"} and strict_true(record.row.get("hasNewReply"))
    )
    if historical:
        primary_type = "historical"
    elif is_todo and updated:
        primary_type = "reactivated_todo"
    elif is_todo and (role in {"decision_maker", "approver"} or pending_actions & {"approve", "conditional_approve", "reject"}):
        primary_type = "decision_todo"
    elif is_todo and (role == "advisor" or "submit_advice" in pending_actions):
        primary_type = "advice_todo"
    elif is_todo:
        primary_type = "action_todo"
    elif updated:
        primary_type = "update_notice"
    else:
        primary_type = "inbox_awareness"

    backend_required = relationship.get("relationship_action_required")
    mandatory = (backend_required if isinstance(backend_required, bool) else is_todo) and not historical and not relationship["visible_only"]
    views = []
    if mandatory or effective_change_type == "new":
        views.append("today")
    if updated and not historical:
        views.append("recent_changes")
    if effective_change_type == "continuation" and not updated and not mandatory:
        views.append("ongoing")
    return {
        "primary_type": primary_type,
        "role": role,
        "underlying_role": role,
        "mandatory": mandatory,
        "requires_role_review": is_todo and relationship["relationship_status"] == "unknown",
        "role_evidence": role_evidence,
        "confidence": 0.95 if role in {"decision_maker", "advisor"} else 0.65 if not is_todo else 0.35,
        "business_date": business_date,
        "original_change_type": record.change_type,
        "effective_change_type": effective_change_type,
        "has_current_change": updated,
        "views": views,
        **relationship,
    }


def allowed_actions(classification: dict[str, Any]) -> list[dict[str, str]]:
    primary = classification["primary_type"]
    role = classification["role"]
    if classification.get("visible_only"):
        codes = ["acknowledge_local", "follow", "snooze", "open_source"]
    elif primary == "historical":
        codes = ["open_source", "follow"]
    elif classification.get("relationship_status") == "unknown":
        codes = ["defer", "open_source"] if classification.get("mandatory") else ["acknowledge_local", "follow", "snooze", "open_source"]
    elif classification.get("relationship_pending_actions"):
        codes = [code for code in classification["relationship_pending_actions"] if code in ACTION_LABELS]
        codes += [code for code in ("defer", "open_source") if code not in codes]
    elif role in {"decision_maker", "approver"} and not classification["requires_role_review"]:
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


def build_card(
    record: SourceRecord,
    extracted: dict[str, Any],
    ai: dict[str, Any],
    report_date: str = "",
    owner_emp_id: str = "",
    owner_name: str = "",
    backend_relation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    classification = classify(record, report_date, owner_emp_id, owner_name, backend_relation)
    summary = str(ai.get("summary") or first_text(extracted.get("open_loops", []), "text") or extracted.get("summary") or record.title)
    actions = ai.get("action_items") or []
    decisions = ai.get("decisions") or []
    risks = ai.get("risks") or extracted.get("risks") or []
    suggested = first_text(actions, "task") or first_text(decisions, "text")
    if classification["visible_only"]:
        recommendation = "该汇报仅因账号权限可见，默认无需处理；如有管理关注价值，可加入本地跟踪。"
    elif classification["relationship_status"] == "unknown":
        recommendation = "后台尚未返回本人和该汇报的权威关系；可先查看原文，不要据此执行审批或建议动作。"
    elif classification["role"] == "decision_maker":
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
        "new_since_last_seen": "本次存在新回复或内容变化。" if classification["has_current_change"] else "未检测到本轮独立更新。",
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
            card["views"] = sorted(set(current.get("views", [])) | set(card.get("views", [])))
            chosen[card["report_id"]] = card
        elif current:
            current["source_labels"] = sorted(set(current["source_labels"]) | set(card["source_labels"]))
            current["views"] = sorted(set(current.get("views", [])) | set(card.get("views", [])))
    return sorted(chosen.values(), key=lambda item: (TYPE_ORDER[item["primary_type"]], item["create_time"], item["report_id"]))


def build_cards(
    run_dir: Path,
    report_date: str = "",
    owner_emp_id: str = "",
    owner_name: str = "",
    relationship_manifest: str | Path | None = None,
) -> dict[str, Any]:
    extracted_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "extracted").glob("*.json")):
        payload = load_json(path)
        for report_id in payload.get("source_ids", []):
            extracted_by_id[str(report_id)] = payload
    ai_by_id = {path.stem: load_json(path) for path in sorted((run_dir / "ai-understanding").glob("*.json"))}
    relation_by_id, relation_meta = load_relationship_manifest(relationship_manifest)
    cards = choose_unique([
        build_card(
            record,
            extracted_by_id.get(record.report_id, {}),
            ai_by_id.get(record.report_id, {}),
            report_date,
            owner_emp_id,
            owner_name,
            relation_by_id.get(record.report_id),
        )
        for record in load_sources(run_dir)
    ])
    counts: dict[str, int] = {key: 0 for key in TYPE_ORDER}
    for card in cards:
        counts[card["primary_type"]] += 1
    view_counts = {
        view: sum(1 for card in cards if view in card.get("views", []))
        for view in ("today", "recent_changes", "ongoing")
    }
    return {
        "schema_version": SCHEMA,
        "run_name": run_dir.name,
        "report_date": report_date,
        "shadow_mode": True,
        "counts": counts,
        "view_counts": view_counts,
        "visible_only_count": sum(1 for card in cards if card.get("visible_only")),
        "relationship_unknown_count": sum(1 for card in cards if card.get("relationship_status") == "unknown"),
        "relationship_provider_status": relation_meta.get("provider_status", "unavailable"),
        "cards": cards,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    cards = payload["cards"]
    view_counts = payload.get("view_counts", {})
    lines = [
        "# 工作协同办理中心（Shadow Mode）",
        "",
        "> 当前页面只生成办理建议和操作预览，不会提交到 CWork。",
        "",
        f"日报日期：`{payload.get('report_date') or '未指定'}`。今日处理 **{view_counts.get('today', 0)}** 项，近期变更 **{view_counts.get('recent_changes', 0)}** 项，持续未闭环 **{view_counts.get('ongoing', 0)}** 项。",
        "",
    ]
    labels = {"decision_todo": "待我决策", "advice_todo": "待我给建议", "action_todo": "待我处理", "reactivated_todo": "更新后重新需要处理", "update_notice": "更新通知", "inbox_awareness": "普通知悉", "historical": "历史回填"}
    for view, heading in (("today", "今日处理"), ("recent_changes", "近期变更"), ("ongoing", "持续未闭环")):
        group = sorted(
            (card for card in cards if view in card.get("views", [])),
            key=lambda item: (not item["mandatory"], TYPE_ORDER[item["primary_type"]], item["create_time"], item["report_id"]),
        )
        lines += [f"## {heading}", ""]
        if not group:
            lines += ["- 当前没有符合条件的事项。", ""]
        for card in group:
            lines += [
                f"### {card['title']}",
                "",
                f"- 类型：{labels[card['primary_type']]}；角色：`{card['role']}`；{'仅权限可见' if card.get('visible_only') else '关系待确认' if card.get('relationship_status') == 'unknown' else '必须处理' if card['mandatory'] else '意见可选'}",
                f"- 原始日期：{card['create_time'] or '未知'}；本轮类型：`{card['effective_change_type']}`",
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
    mandatory = sum(1 for card in cards if card.get("mandatory"))
    view_counts = payload.get("view_counts", {})
    labels = {"decision_todo": "待我决策", "advice_todo": "待我给建议", "action_todo": "待我处理", "reactivated_todo": "重新激活", "update_notice": "更新通知", "inbox_awareness": "普通知悉", "historical": "历史回填"}

    def render_card(card: dict[str, Any]) -> str:
        actions = "".join(
            f'<button class="action" data-report="{html.escape(card["report_id"])}" data-action="{html.escape(action["code"])}">{html.escape(action["label"])}</button>'
            for action in card["allowed_actions"]
        )
        review = '<span class="review">角色待复核</span>' if card["requires_role_review"] and card.get("relationship_status") != "unknown" else ""
        visibility = '<span class="visibility">仅权限可见 · 与我无关</span>' if card.get("visible_only") else ""
        relationship_pending = '<span class="review">关系待确认</span>' if card.get("relationship_status") == "unknown" else ""
        change_label = "今日新增" if card.get("effective_change_type") == "new" else "本轮有变更" if card.get("has_current_change") else "持续事项"
        return f'''<article class="card{' visible-only' if card.get('visible_only') else ''}" data-type="{card['primary_type']}">
          <div class="meta"><span class="type">{labels[card['primary_type']]}</span><span class="{'must' if card['mandatory'] else 'optional'}">{'仅供参考' if card.get('visible_only') else '必须处理' if card['mandatory'] else '意见可选'}</span>{visibility}{relationship_pending}{review}</div>
          <h2>{html.escape(card['title'])}</h2>
          <p class="source-meta">{html.escape(change_label)} · 原始日期 {html.escape(card['create_time'] or '未知')} · {html.escape(card['writer'] or '未知')}</p>
          <p class="summary">{html.escape(card['summary'])}</p>
          <div class="recommend"><strong>AI 办理建议</strong><p>{html.escape(card['recommendation'])}</p></div>
          <p class="draft"><strong>建议草稿：</strong>{html.escape(card['draft_text'])}</p>
          <p class="evidence">证据 report_id：{html.escape(card['report_id'])}</p>
          <div class="actions">{actions}</div>
        </article>'''

    def view_cards(view: str) -> list[dict[str, Any]]:
        return sorted(
            (card for card in cards if view in card.get("views", [])),
            key=lambda item: (not item["mandatory"], TYPE_ORDER[item["primary_type"]], item["create_time"], item["report_id"]),
        )

    today_html = "".join(render_card(card) for card in view_cards("today")) or '<p class="empty">今天没有新增汇报或需要你处理的待办。</p>'
    changed_html = "".join(render_card(card) for card in view_cards("recent_changes")) or '<p class="empty">本轮没有检测到历史汇报的真实变化。</p>'
    ongoing_cards = view_cards("ongoing")
    ongoing_html = "".join(render_card(card) for card in ongoing_cards) or '<p class="empty">当前没有持续未闭环事项。</p>'
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>工作协同办理中心</title>
<style>:root{{--bg:#f5f7fb;--card:#fff;--ink:#172033;--muted:#697386;--brand:#3157d5;--danger:#b42318}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:980px;margin:auto;padding:24px}}header{{background:linear-gradient(135deg,#18254b,#3157d5);color:white;padding:28px;border-radius:20px}}.shadow{{display:inline-block;background:#fff3cd;color:#7a5200;padding:5px 10px;border-radius:999px;font-weight:700}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}}.stat{{background:#ffffff1c;padding:10px 14px;border-radius:12px}}nav{{display:flex;gap:8px;overflow:auto;padding:18px 0}}nav button,.action{{border:1px solid #d7ddea;background:white;padding:9px 12px;border-radius:10px;cursor:pointer}}nav button.active{{background:var(--brand);color:white}}.tab-panel[hidden]{{display:none}}.card{{background:var(--card);border-radius:18px;padding:20px;margin:0 0 14px;box-shadow:0 7px 24px #18254b12}}.card.visible-only{{border:1px dashed #d0b36a;background:#fffdf6}}.meta{{display:flex;gap:8px;flex-wrap:wrap}}.meta span{{font-size:12px;padding:3px 8px;border-radius:99px;background:#eef2ff}}.meta .must{{background:#fee4e2;color:var(--danger)}}.meta .optional{{background:#e8f7ef;color:#067647}}.meta .review{{background:#fff3cd;color:#7a5200}}.meta .visibility{{background:#fff0c2;color:#765400;font-weight:700}}h2{{font-size:19px;margin:12px 0 8px}}.summary,.draft{{color:#344054}}.source-meta,.empty{{color:var(--muted)}}.recommend{{background:#f0f4ff;padding:12px 14px;border-radius:12px}}.recommend p{{margin:3px 0}}.evidence{{font-size:12px;color:var(--muted)}}.actions{{display:flex;gap:8px;flex-wrap:wrap}}.action:hover{{border-color:var(--brand);color:var(--brand)}}details{{margin-top:18px;border:1px solid #d7ddea;border-radius:14px;background:#eef2ff;padding:12px}}details>summary{{cursor:pointer;font-weight:700}}details .ongoing{{margin-top:14px}}dialog{{width:min(680px,92vw);border:0;border-radius:18px;padding:0;box-shadow:0 20px 70px #0004}}dialog::backdrop{{background:#10182899}}.modal{{padding:22px}}textarea{{width:100%;min-height:130px;padding:12px;border:1px solid #ccd3df;border-radius:10px}}pre{{white-space:pre-wrap;background:#101828;color:#e4e7ec;padding:12px;border-radius:10px;max-height:240px;overflow:auto}}.warning{{color:#b54708;font-weight:700}}.modal-actions{{display:flex;gap:8px;justify-content:flex-end}}@media(max-width:600px){{main{{padding:12px}}header{{padding:20px}}.card{{padding:16px}}}}</style></head>
<body><main><header><span class="shadow">Shadow Mode · 不会提交到 CWork</span><h1>工作协同办理中心</h1><p>默认先看当天新增和当前待办；历史汇报的真实变化单独查看。</p><div class="stats"><div class="stat"><strong>{mandatory}</strong> 项必须处理</div><div class="stat"><strong>{view_counts.get('today', 0)}</strong> 项今日处理</div><div class="stat"><strong>{view_counts.get('recent_changes', 0)}</strong> 项近期变更</div><div class="stat"><strong>{payload.get('visible_only_count', 0)}</strong> 项仅权限可见</div><div class="stat"><strong>{payload.get('relationship_unknown_count', 0)}</strong> 项关系待确认</div></div></header>
<nav aria-label="工作视图"><button class="active" data-tab="today">今日处理 {view_counts.get('today', 0)}</button><button data-tab="recent_changes">近期变更 {view_counts.get('recent_changes', 0)}</button></nav>
<section class="tab-panel" data-panel="today">{today_html}<details><summary>持续未闭环 {len(ongoing_cards)}</summary><div class="ongoing">{ongoing_html}</div></details></section>
<section class="tab-panel" data-panel="recent_changes" hidden>{changed_html}</section></main>
<dialog id="preview"><div class="modal"><h2 id="modal-title">操作预览</h2><p class="warning">这是 Shadow Mode。确认只保存本页预览，不会向 CWork 提交、审批、回复、转办、完成或标已读。</p><label>意见或附加条件<textarea id="opinion"></textarea></label><p id="consequence"></p><button id="build-preview">生成操作预览</button><pre id="payload">尚未生成</pre><div class="modal-actions"><button id="close-preview">关闭</button><button id="confirm-preview">确认此预览（不提交）</button></div></div></dialog>
<script type="application/json" id="cards-data">{data}</script><script>
const payload=JSON.parse(document.getElementById('cards-data').textContent);const byId=Object.fromEntries(payload.cards.map(c=>[c.report_id,c]));let current=null;
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelectorAll('.tab-panel').forEach(p=>p.hidden=p.dataset.panel!==b.dataset.tab)}}));
document.querySelectorAll('.action').forEach(b=>b.addEventListener('click',()=>{{const c=byId[b.dataset.report],a=c.allowed_actions.find(x=>x.code===b.dataset.action);current={{card:c,action:a}};document.getElementById('modal-title').textContent=a.label+' · '+c.title;document.getElementById('opinion').value=c.draft_text;document.getElementById('consequence').textContent=a.consequence;document.getElementById('payload').textContent='尚未生成';document.getElementById('preview').showModal()}}));
document.getElementById('build-preview').addEventListener('click',()=>{{if(!current)return;document.getElementById('payload').textContent=JSON.stringify({{mode:'shadow',report_id:current.card.report_id,action:current.action.code,text:document.getElementById('opinion').value,consequence:current.action.consequence,will_submit:false}},null,2)}});
document.getElementById('confirm-preview').addEventListener('click',()=>{{document.getElementById('payload').textContent+='\\n\\n已确认预览：未提交到 CWork。'}});document.getElementById('close-preview').addEventListener('click',()=>document.getElementById('preview').close());
</script></body></html>'''


def assert_shadow_safe(text: str) -> None:
    forbidden = ("fetch(", "XMLHttpRequest", "cwork-todo.py", "cwork-reply", "approve(", "complete(")
    found = [value for value in forbidden if value in text]
    if found:
        raise RuntimeError("Shadow Mode output contains forbidden write capability: " + ", ".join(found))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the RT-002 Shadow Mode action center.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--report-date", default="", help="Business date represented by this action center (YYYY-MM-DD).")
    parser.add_argument("--owner-emp-id", default="", help="Current CWork employee ID used for relationship classification.")
    parser.add_argument("--owner-name", default="", help="Current CWork employee name; fallback only when a candidate has no employee ID.")
    parser.add_argument("--relationship-manifest", default=None, help="Backend-owned relationship manifest for this run.")
    args = parser.parse_args()
    run_dir = PROJECT / "runs" / args.run_name
    if not run_dir.exists():
        raise SystemExit(f"run not found: {run_dir}")
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else run_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_cards(run_dir, args.report_date, args.owner_emp_id, args.owner_name, args.relationship_manifest)
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
