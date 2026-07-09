#!/usr/bin/env python3
"""Preview how incoming CWork records link to accumulated event knowledge."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]

GENERIC_ANCHORS = {
    "",
    "未命名事项",
    "New",
    "下半年",
    "今天",
    "昨日",
    "交流",
    "请示",
}

STOPWORDS = {
    "关于",
    "会议纪要",
    "工作汇报",
    "申请",
    "说明",
    "更新",
    "讨论",
    "项目",
    "系统",
    "产品",
    "需求",
    "方案",
    "工作协同",
    "决策人",
    "建议人",
    "意见",
    "汇报人",
    "时间",
    "图描述",
    "化如下",
    "为项目",
    "修改了",
    "例如",
    "合同",
    "业务",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_id(ext: dict) -> str:
    ids = ext.get("source_ids") or []
    return str(ids[0]) if ids else ""


def load_raw_metadata(run_dir: Path) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for path in sorted((run_dir / "raw").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        fields: dict[str, str] = {}
        match = re.match(r"---\n(.*?)\n---", text, re.S)
        if match:
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip().strip('"')
        sid = fields.get("report_id") or path.name.split("-", 1)[0]
        writer = fields.get("writer", "")
        create_time = fields.get("create_time", "")
        if not writer:
            meta_writer = re.search(r"\*\*汇报人\*\*:\s*([^\n<]+)", text)
            writer = meta_writer.group(1).strip() if meta_writer else ""
        if not create_time:
            meta_time = re.search(r"\*\*时间\*\*:\s*([^\n<]+)", text)
            create_time = meta_time.group(1).strip() if meta_time else ""
        metadata[sid] = {
            "writer": writer or "未知",
            "create_time": create_time or "未知时间",
            "title": fields.get("title", ""),
        }
    return metadata


def clean_text(value: str, limit: int = 180) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = re.sub(r"&nbsp;", " ", value)
    value = re.sub(r"\*\*|__|`", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" -*#|:：；;")
    if not value:
        return ""
    if value.startswith(("reference_title:", "title:", "main:", "report_id:", "{", "[", "|")):
        return ""
    if any(token in value for token in ['"nodeName"', '"content"', '"textContent"', "null", "huiJiId=", "linkType="]):
        return ""
    return value[:limit]


def first_signals(ext: dict, limit: int = 3) -> list[str]:
    result: list[str] = []
    for key in ["risks", "decision_points", "actions", "open_loops"]:
        for item in ext.get(key, []):
            clean = clean_text(item)
            if clean and clean not in result:
                result.append(clean)
            if len(result) >= limit:
                return result
    return result


def normalized_anchor(ext: dict) -> str:
    anchor = (ext.get("event_anchor") or "").strip()
    return "" if anchor in GENERIC_ANCHORS else anchor


def title_terms(title: str) -> set[str]:
    terms = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", title or ""))
    return {term for term in terms if term not in STOPWORDS and not re.fullmatch(r"\d+", term)}


def entity_values(ext: dict) -> set[str]:
    values: set[str] = set()
    for bucket, bucket_values in (ext.get("entities") or {}).items():
        if bucket in {"amounts", "dates"}:
            continue
        for value in bucket_values:
            if value and value not in GENERIC_ANCHORS and value not in STOPWORDS and len(value) > 1:
                values.add(value)
    anchor = normalized_anchor(ext)
    if anchor:
        values.add(anchor)
    return values


def build_history_events(history: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    fallback_groups: dict[str, dict] = {}
    for ext in history:
        anchor = normalized_anchor(ext)
        family = ext.get("event_family") or ext.get("item_nature") or "unknown"
        if not anchor:
            anchor = (ext.get("title") or "未命名事项")[:24]
        key = (anchor, family)
        group = groups.setdefault(
            key,
            {
                "event": anchor,
                "event_family": family,
                "source_ids": [],
                "titles": [],
                "entities": Counter(),
                "terms": Counter(),
                "signals": [],
            },
        )
        sid = source_id(ext)
        group["source_ids"].append(sid)
        group["titles"].append(ext.get("title", ""))
        group["entities"].update(entity_values(ext))
        group["terms"].update(title_terms(ext.get("title", "")))
        for signal in first_signals(ext, limit=2):
            if signal not in group["signals"]:
                group["signals"].append(signal)

    # Also keep an anchor-only view so an incoming item can find an event even
    # when family classification changes between versions.
    for group in groups.values():
        anchor_group = fallback_groups.setdefault(
            group["event"],
            {
                "event": group["event"],
                "event_family": "mixed",
                "source_ids": [],
                "titles": [],
                "entities": Counter(),
                "terms": Counter(),
                "signals": [],
            },
        )
        anchor_group["source_ids"].extend(group["source_ids"])
        anchor_group["titles"].extend(group["titles"])
        anchor_group["entities"].update(group["entities"])
        anchor_group["terms"].update(group["terms"])
        for signal in group["signals"]:
            if signal not in anchor_group["signals"]:
                anchor_group["signals"].append(signal)

    merged = list(groups.values())
    merged.extend(group for group in fallback_groups.values() if len(group["source_ids"]) >= 2)
    return merged


def score_candidate(ext: dict, event: dict) -> tuple[int, list[str]]:
    score = 0
    evidence: list[str] = []
    anchor = normalized_anchor(ext)
    family = ext.get("event_family") or ext.get("item_nature") or "unknown"
    if anchor and anchor == event["event"]:
        score += 38
        evidence.append(f"同一事件锚点：{anchor}")
    if family and family == event["event_family"]:
        score += 22
        evidence.append(f"同一事项类型：{family}")
    incoming_entities = entity_values(ext)
    shared_entities = sorted(incoming_entities & set(event["entities"]))
    if shared_entities:
        score += min(24, 8 * len(shared_entities))
        evidence.append(f"共享实体：{'、'.join(shared_entities[:4])}")
    shared_terms = sorted(title_terms(ext.get("title", "")) & set(event["terms"]))
    if shared_terms:
        score += min(18, 6 * len(shared_terms))
        evidence.append(f"标题关键词重合：{'、'.join(shared_terms[:4])}")
    if len(event["source_ids"]) >= 3:
        score += 5
        evidence.append(f"历史证据充足：{len(event['source_ids'])} 条")
    if len(event["source_ids"]) >= 50 and not (anchor and anchor == event["event"]):
        score -= 30
        evidence = [item for item in evidence if not item.startswith("历史证据充足")]
    if not evidence:
        return 0, []
    if score <= 0:
        return 0, []
    return min(score, 100), evidence[:5]


def decision_for(score: int, evidence: list[str]) -> str:
    if score >= 75 and len(evidence) >= 2:
        return "append_existing_event"
    if score >= 45:
        return "mark_suspected_relation"
    return "create_new_event"


def link_incoming(incoming: list[dict], history_events: list[dict]) -> list[dict]:
    results: list[dict] = []
    for ext in incoming:
        candidates = []
        for event in history_events:
            score, evidence = score_candidate(ext, event)
            if score:
                candidates.append({"event": event, "score": score, "evidence": evidence})
        candidates.sort(key=lambda item: (-item["score"], -len(item["event"]["source_ids"]), item["event"]["event"]))
        best = candidates[0] if candidates else None
        if best:
            decision = decision_for(best["score"], best["evidence"])
            target_event = best["event"]["event"] if decision != "create_new_event" else ""
            target_family = best["event"]["event_family"] if decision != "create_new_event" else ""
            target_count = len(best["event"]["source_ids"]) if decision != "create_new_event" else 0
            evidence = best["evidence"] if decision != "create_new_event" else []
            results.append(
                {
                    "source_id": source_id(ext),
                    "title": ext.get("title", ""),
                    "event_anchor": normalized_anchor(ext) or ext.get("event_anchor", ""),
                    "event_family": ext.get("event_family") or ext.get("item_nature") or "",
                    "decision": decision,
                    "target_event": target_event,
                    "target_family": target_family,
                    "score": best["score"],
                    "evidence": evidence,
                    "target_source_count": target_count,
                    "signals": first_signals(ext, limit=3),
                }
            )
        else:
            results.append(
                {
                    "source_id": source_id(ext),
                    "title": ext.get("title", ""),
                    "event_anchor": normalized_anchor(ext) or ext.get("event_anchor", ""),
                    "event_family": ext.get("event_family") or ext.get("item_nature") or "",
                    "decision": "create_new_event",
                    "target_event": "",
                    "target_family": "",
                    "score": 0,
                    "evidence": [],
                    "target_source_count": 0,
                    "signals": first_signals(ext, limit=3),
                }
            )
    return results


def render_report(
    run_dir: Path,
    history: list[dict],
    incoming: list[dict],
    results: list[dict],
    metadata: dict[str, dict],
    history_label: str | None = None,
) -> str:
    counts = Counter(result["decision"] for result in results)
    if history_label:
        scope = f"历史基线 `{history_label}` {len(history)} 条，新进消息 `{run_dir.relative_to(PROJECT)}` {len(incoming)} 条"
    else:
        scope = f"用 `{run_dir.relative_to(PROJECT)}` 做时间切片测试：历史知识 {len(history)} 条，新进消息 {len(incoming)} 条"
    lines = [
        "# CWK 增量链接预演",
        "",
        "## 结论",
        "",
        f"- 本次{scope}。",
        f"- 可直接追加到已有事件：{counts.get('append_existing_event', 0)} 条。",
        f"- 需要作为疑似关系待审：{counts.get('mark_suspected_relation', 0)} 条。",
        f"- 建议新建事件：{counts.get('create_new_event', 0)} 条。",
        "- 全程只读，没有改 CWork，也没有改知识库。",
        "",
        "## 它说明什么",
        "",
        "- 新消息进入后，先抽取事件锚点、事项类型、实体和标题关键词。",
        "- 再只检索候选历史事件，不把整个知识库丢进模型。",
        "- 分数高且证据不少于两类时，才建议追加到已有事件；否则进入疑似关系或新建事件。",
        "",
    ]
    for section, decision, limit in [
        ("可追加到已有事件", "append_existing_event", 20),
        ("疑似关系待审", "mark_suspected_relation", 20),
        ("建议新建事件", "create_new_event", 15),
    ]:
        rows = [result for result in results if result["decision"] == decision]
        rows.sort(key=lambda item: (-item["score"], item["title"]))
        lines.extend([f"## {section}", ""])
        for index, result in enumerate(rows[:limit], 1):
            meta = metadata.get(result["source_id"], {})
            lines.append(f"{index}. {result['title']}")
            lines.append(f"   发送：{meta.get('writer', '未知')} · {meta.get('create_time', '未知时间')} · 证据 `{result['source_id']}`")
            if result["target_event"]:
                lines.append(f"   目标事件：{result['target_event']} / {result['target_family']} · 历史证据 {result['target_source_count']} 条 · 分数 {result['score']}")
            else:
                lines.append(f"   新事件锚点：{result['event_anchor']} / {result['event_family']}")
            if result["evidence"]:
                lines.append(f"   链接依据：{'；'.join(result['evidence'])}")
            if result["signals"]:
                lines.append(f"   内容信号：{'；'.join(result['signals'][:2])}")
        if len(rows) > limit:
            lines.append(f"- 另有 {len(rows) - limit} 条未展开。")
        if not rows:
            lines.append("- 无。")
        lines.append("")
    lines.extend(
        [
            "## 下一步建议",
            "",
            "- 把这个预演规则接到 nightly dry-run：每天新来的记录先跑增量链接。",
            "- 对“疑似关系待审”抽样建立 gold set，校准误关联。",
            "- 在通过校准前，继续保持强合并关闭，只写建议和证据。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview incremental event linking from an existing processed CWK run.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--history-run-name", default=None, help="Use another processed run as accumulated history. Incoming records come from --run-name.")
    parser.add_argument("--incoming-count", type=int, default=80)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    metadata = load_raw_metadata(run_dir)
    extracted = [load_json(path) for path in sorted((run_dir / "extracted").glob("*.json"))]
    extracted.sort(key=lambda ext: metadata.get(source_id(ext), {}).get("create_time", ""))
    history_label = None
    if args.history_run_name:
        history_dir = PROJECT / "runs" / args.history_run_name
        history_metadata = load_raw_metadata(history_dir)
        metadata = {**history_metadata, **metadata}
        history = [load_json(path) for path in sorted((history_dir / "extracted").glob("*.json"))]
        history.sort(key=lambda ext: history_metadata.get(source_id(ext), {}).get("create_time", ""))
        incoming = extracted if args.incoming_count <= 0 else extracted[-args.incoming_count :]
        history_label = str(history_dir.relative_to(PROJECT))
    else:
        incoming = extracted[-args.incoming_count :]
        history = extracted[: -args.incoming_count]
    history_events = build_history_events(history)
    results = link_incoming(incoming, history_events)
    output = Path(args.output) if args.output else run_dir / "incremental-link-preview-v1.md"
    output.write_text(render_report(run_dir, history, incoming, results, metadata, history_label), encoding="utf-8")
    print(output)
    print(json.dumps(Counter(result["decision"] for result in results), ensure_ascii=False))


if __name__ == "__main__":
    main()
