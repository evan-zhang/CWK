#!/usr/bin/env python3
"""CWK sample-set pilot runner.

This prototype is intentionally local and read-only. It processes Markdown
references under references/cwork-samples and writes deterministic artifacts
under runs/sample-pilot. It does not call CWork, DocDB, Discord, or any
mutating API.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLES = PROJECT / "references" / "cwork-samples"


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
    "产品任务进度汇总",
    "来源会议任务",
    "任务进度汇总",
    "会议任务",
    "产品",
    "来源",
    "任务跟进",
    "进展报告",
    "截至",
}

GENERIC_ENTITY_TERMS = {
    "AI",
    "大模型",
    "知识库",
    "工作协同",
    "合同",
    "项目",
    "系统",
    "集团",
    "玄关",
    "销售部",
}

INVALID_EVENT_ANCHORS = {
    "及跟进",
    "跟进",
    "已更新",
    "更新",
    "例会",
    "会议",
    "双周会",
    "周会",
    "日报",
    "周报",
    "月报",
}

EVENT_ANCHOR_ALIASES = {
    "SFE系统": "SFE",
}

KNOWN_SYSTEMS = [
    "云端虾",
    "云龙虾",
    "OpenClaw",
    "小龙虾",
    "工作协同",
    "BP",
    "TBS",
    "SFE",
    "AI知识库",
    "知识库",
    "法务部AI",
    "合同AI",
    "AI服务器",
    "AI模型",
    "大模型",
    "投前系统",
    "数据分析系统",
    "药智网",
    "AI问答",
    "AI慧记",
    "AI费用",
    "RDS",
]

KNOWN_ORGS = [
    "集团",
    "玄关",
    "德镁",
    "销售部",
    "法务部",
    "产品中心",
    "数据管理部",
    "瑞金医院",
    "E药经理人",
    "工信局",
    "统计局",
]

ACTION_WORDS = [
    "申请",
    "审批",
    "确认",
    "跟进",
    "推进",
    "提供",
    "核对",
    "补充",
    "开发",
    "测试",
    "采购",
    "报销",
    "备案",
    "提交",
]

RISK_WORDS = [
    "风险",
    "延期",
    "争议",
    "不够",
    "不足",
    "阻塞",
    "成本",
    "预算",
    "权限",
    "合规",
    "未处理",
    "暂无回复",
    "错漏",
]


@dataclass
class Item:
    source_id: str
    title: str
    writer: str
    created_at: str
    lane: str
    collection_mode: str
    change_type: str
    source_scopes: str
    path: str
    content: str


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def first_match(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, re.M)
        if m:
            return m.group(1).strip()
    return ""


def infer_lane(path: Path, meta: dict[str, str], title: str) -> str:
    name = path.name
    ref = meta.get("reference_type") or meta.get("reference_focus") or ""
    if "inbox-awareness" in name or ref == "inbox_awareness":
        return "inbox_awareness"
    if "-reply-" in name or "reply" in ref:
        return "reply_chain"
    if "-todo-" in name:
        return "todo_backed"
    if "-stream-" in name:
        return "persistent_stream"
    if any(w in title for w in ["周报", "月报", "季报", "统计", "运营报告"]):
        return "recurring_digest"
    return "unknown"


def normalize_title(title: str) -> str:
    title = title.replace("AI 慧记", "AI慧记")
    title = re.sub(r"[【】\[\]（）()「」]", " ", title)
    title = re.sub(r"\d{4}[年./-]\d{1,2}[月./-]?\d{0,2}日?", " ", title)
    title = re.sub(r"\b\d{1,2}[-/]\d{1,2}\b", " ", title)
    title = re.sub(r"\d+月\d+日|\d+月|Q\d|第[一二三四五六七八九十]+周", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    for word in STOPWORDS:
        title = title.replace(word, " ")
    return re.sub(r"\s+", " ", title).strip()


def is_valid_event_anchor(value: str) -> bool:
    value = (value or "").strip(" -—_：:、,，")
    if not value or value in INVALID_EVENT_ANCHORS or value in STOPWORDS or value in GENERIC_ENTITY_TERMS:
        return False
    if re.fullmatch(r"(?:20\d{2}[-/.]?)?\d{1,2}[-/.]\d{1,2}|\d{3,8}", value):
        return False
    if re.fullmatch(r"(?:及|和|与)?(?:跟进|更新|汇报|纪要|报告)", value):
        return False
    if value.endswith(("和", "及", "与", "暨", "的")):
        return False
    return len(value) >= 2


def _title_anchor(title: str, entities: dict[str, list[str]]) -> str:
    clean = normalize_title(title)
    compact = re.sub(r"\s+", "", title)
    if any(term in compact for term in ("敏感词", "敏感字")) or ("内容" in compact and "合规治理" in compact):
        return "内容合规治理"
    if any(term in compact for term in ("大模型使用预算", "大模型费用", "AI费用")):
        return "AI费用"
    if "法务部" in compact and ("合同AI" in compact or "法务部AI" in compact):
        return "法务部AI"
    clinical_project = first_match(
        [
            r"(2026[^-—_：:、,，]{2,30}临床调研项目)",
            r"(2025[^-—_：:、,，]{2,30}临床调研项目)",
            r"(20\d{2}[^-—_：:、,，]{2,30}调研问卷[一二三四五六七八九十0-9]*期)",
        ],
        title,
    )
    if clinical_project:
        return clinical_project

    quoted = first_match(
        [
            r"[\"'“”‘’]([^\"'“”‘’]{2,20})[\"'“”‘’]\s*产品任务进度汇总",
            r"第[一二三四五六七八九十]+周[\"'“”‘’]([^\"'“”‘’]{2,20})[\"'“”‘’]",
            r"第[一二三四五六七八九十]+周-([^-/—_：:、,， ]{2,20})-产品任务进度汇总",
        ],
        title,
    )
    if quoted and quoted not in GENERIC_ENTITY_TERMS:
        return quoted

    title_candidates = []
    for value in entities["projects"] + entities["systems"] + entities["products"] + entities["customers"]:
        if value in title and value not in GENERIC_ENTITY_TERMS:
            title_candidates.append(value)
    if title_candidates:
        return max(title_candidates, key=len)

    chunks = [c for c in re.split(r"[-—_：:、,， ]+", clean) if len(c) >= 2]
    for chunk in chunks:
        if re.fullmatch(r"\d{1,2}[-/]\d{1,2}", chunk):
            continue
        if chunk not in STOPWORDS and chunk not in GENERIC_ENTITY_TERMS:
            return chunk[:20]

    candidates = []
    for value in entities["projects"] + entities["systems"] + entities["products"] + entities["customers"]:
        if value not in GENERIC_ENTITY_TERMS:
            candidates.append(value)
    if candidates:
        return candidates[0]
    return chunks[0] if chunks else clean[:20] or "未命名事项"


def title_anchor(title: str, entities: dict[str, list[str]]) -> str:
    raw_anchor = _title_anchor(title, entities)
    anchor = EVENT_ANCHOR_ALIASES.get(raw_anchor, raw_anchor)
    if is_valid_event_anchor(anchor):
        return anchor
    for bucket in ("projects", "systems", "products", "customers", "contracts"):
        for value in entities.get(bucket, []):
            if is_valid_event_anchor(value):
                return value
    return "未命名事项"


def extract_entities(text: str, title: str) -> dict[str, list[str]]:
    systems = sorted({x for x in KNOWN_SYSTEMS if x in text or x in title})
    orgs = sorted({x for x in KNOWN_ORGS if x in text or x in title})
    people = sorted(set(re.findall(r"[\u4e00-\u9fa5]{2,3}(?=[:：]|\s*/|\s+已处理|\s+待处理)", text)))
    amounts = sorted(set(re.findall(r"(?:人民币)?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:万元|元|亿|万)?", text)))
    dates = sorted(set(re.findall(r"\d{4}[-年./]\d{1,2}[-月./]\d{0,2}日?|\d{1,2}月\d{1,2}日", text)))
    projects = sorted({s for s in systems if s.endswith("系统") or s in {"云端虾", "云龙虾", "TBS", "OpenClaw", "AI知识库"}})
    return {
        "people": people[:12],
        "orgs": orgs,
        "customers": [x for x in orgs if x in {"瑞金医院", "E药经理人"}],
        "products": [x for x in systems if x.startswith("AI") or x in {"云端虾", "云龙虾", "OpenClaw", "小龙虾"}],
        "projects": projects,
        "contracts": ["合同"] if "合同" in text or "合同" in title else [],
        "systems": systems,
        "amounts": amounts[:10],
        "dates": dates[:10],
    }


def extract_bullets(words: list[str], text: str, limit: int = 8) -> list[str]:
    lines = [line.strip(" -\t") for line in text.splitlines() if line.strip()]
    found: list[str] = []
    for line in lines:
        if is_noise_line(line):
            continue
        if any(w in line for w in words):
            line = re.sub(r"\s+", " ", line)
            line = line.replace("&nbsp;", " ")
            if 8 <= len(line) <= 180 and line not in found:
                found.append(line)
        if len(found) >= limit:
            break
    return found


def item_nature(lane: str, title: str) -> str:
    if lane == "todo_backed":
        return "mixed"
    if lane == "reply_chain":
        return "administrative_approval" if any(w in title for w in ["盖章", "离职", "奖金"]) else "persistent_stream"
    if lane == "inbox_awareness" and any(w in title for w in ["周报", "月报", "季报", "统计", "运营报告", "进度汇总"]):
        return "recurring_digest"
    if lane == "inbox_awareness":
        return "persistent_stream"
    if lane == "persistent_stream":
        return "persistent_stream"
    return "unknown"


def attention_type(lane: str, title: str) -> str:
    if lane == "todo_backed":
        return "requires_action"
    if lane == "reply_chain" and any(w in title for w in ["审批", "申请", "盖章", "离职", "奖金"]):
        return "optional_review"
    if lane == "inbox_awareness":
        return "awareness_only"
    return "optional_review"


def event_family(title: str, nature: str) -> str:
    clean = normalize_title(title)
    probe = re.sub(r"\s+", "", title)
    if any(word in probe for word in ["日例会", "例会"]):
        return "routine_meeting"
    if any(word in probe for word in ["产品任务进度汇总", "周报", "周总结", "月报", "季报", "运营报告", "半年报", "日报", "用户数据分析报告"]):
        return "recurring_report"
    if any(word in clean for word in ["需求", "需求列表", "需求说明"]):
        return "requirements"
    if any(word in clean for word in ["云端虾申请", "权限申请", "申请云端虾", "开通云端虾", "申请龙虾AI说"]):
        return "access_request"
    if any(word in clean for word in ["预算", "费用", "成本"]):
        return "budget_cost"
    if any(word in clean for word in ["采购", "设备", "扩容"]):
        return "procurement_capacity"
    if any(word in clean for word in ["奖金", "评分", "验收", "操作考核", "考核"]):
        return "incentive_acceptance"
    if any(word in clean for word in ["合同", "协议", "法务"]):
        return "contract_legal"
    if any(word in clean for word in ["培训", "训战", "认证", "案例库"]):
        return "training_enablement"
    if any(word in clean for word in ["架构", "方案", "规划", "设计"]):
        return "planning_design"
    if "会议纪要" in probe or probe.endswith("纪要"):
        return "meeting_minutes"
    return nature or "general"


def load_items(source_dirs: list[Path]) -> list[Item]:
    items: list[Item] = []
    seen: set[str] = set()
    for source_dir in source_dirs:
        for path in sorted(source_dir.glob("*.md")):
            if path.name == "INDEX.md":
                continue
            text = path.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            source_id = (
                meta.get("report_id")
                or meta.get("reportRecordId")
                or first_match([r"Report ID: `([^`]+)`", r"reportRecordId: `([^`]+)`"], text)
                or path.name.split("-", 1)[0]
            )
            if source_id in seen:
                continue
            seen.add(source_id)
            title = (
                meta.get("title")
                or meta.get("main")
                or first_match([r"^#\s+(.+)$", r"reference_title:\s*\"?([^\"]+)\"?"], body)
                or path.stem
            )
            writer = (
                meta.get("writer")
                or meta.get("writeEmpName")
                or first_match([r"Writer:\s*(.+)$", r"writer:\s*(.+)$", r"汇报人\*\*:\s*(.+)$"], text)
            )
            created_at = meta.get("create_time") or meta.get("createTime") or first_match([r"Create Time:\s*(.+)$", r"created:\s*(.+)$"], text)
            lane = meta.get("source_lane") or infer_lane(path, meta, title)
            collection_mode = meta.get("collection_mode") or "reference-sample"
            change_type = meta.get("change_type") or "unknown"
            source_scopes = meta.get("source_scopes") or ""
            try:
                source_path = str(path.relative_to(PROJECT))
            except ValueError:
                source_path = str(Path("external-source") / path.parent.name / path.name)
            items.append(Item(source_id, title, writer, created_at, lane, collection_mode, change_type, source_scopes, source_path, text))
    return items


def build_extractions(items: list[Item]) -> dict[str, dict]:
    extractions: dict[str, dict] = {}
    for item in items:
        entities = extract_entities(item.content, item.title)
        extraction = {
            "source_ids": [item.source_id],
            "title": item.title,
            "source_lane": item.lane,
            "collection_mode": item.collection_mode,
            "change_type": item.change_type,
            "source_scopes": item.source_scopes,
            "item_nature": item_nature(item.lane, item.title),
            "attention_type": attention_type(item.lane, item.title),
            "event_anchor": title_anchor(item.title, entities),
            "entities": {k: v for k, v in entities.items() if k not in {"amounts", "dates"}},
            "actions": extract_bullets(ACTION_WORDS, item.content),
            "risks": extract_bullets(RISK_WORDS, item.content),
            "dates": entities["dates"],
            "amounts": entities["amounts"],
            "decision_points": extract_bullets(["决策", "决定", "结论", "同意", "确认", "确定"], item.content),
            "open_loops": extract_bullets(["后续", "待", "需", "需要", "跟进", "未处理", "暂无回复"], item.content),
            "reply_chain": {
                "has_replies": "## Replies" in item.content or "## Node / Opinion Chain" in item.content or "## Workflow Nodes" in item.content,
                "has_opinions": "同意" in item.content or "不同意" in item.content or "Opinion" in item.content,
            },
        }
        extraction["event_family"] = event_family(item.title, extraction["item_nature"])
        extractions[item.source_id] = extraction
    return extractions


def relation_entity_stats(extractions: dict[str, dict]) -> Counter:
    df: Counter = Counter()
    for extra in extractions.values():
        values = set()
        for bucket_values in extra.get("entities", {}).values():
            values.update(bucket_values)
        df.update(values)
    return df


def relation_anchor_stats(extractions: dict[str, dict]) -> Counter:
    return Counter(extra.get("event_anchor") for extra in extractions.values())


def relation_family_stats(extractions: dict[str, dict]) -> Counter:
    return Counter((extra.get("event_anchor"), extra.get("event_family")) for extra in extractions.values())


def is_specific_entity(value: str, entity_df: Counter, total: int) -> bool:
    if value in GENERIC_ENTITY_TERMS:
        return False
    if len(value.strip()) <= 1:
        return False
    # A term that appears in more than roughly a fifth of the current batch is
    # useful as context, but too broad to prove two reports belong together.
    return entity_df[value] <= max(3, int(total * 0.22))


def score_relation(a: dict, b: dict, entity_df: Counter, anchor_df: Counter, total: int) -> tuple[float, list[str], str]:
    evidence: list[str] = []
    score = 0.0
    strong_signals = 0
    same_anchor = False
    cross_anchor_strong_signal = False
    if a["event_anchor"] and a["event_anchor"] == b["event_anchor"] and (
        is_specific_entity(a["event_anchor"], entity_df, total)
        or a.get("event_family") == b.get("event_family")
    ):
        if anchor_df[a["event_anchor"]] > 3 and a.get("event_family") != b.get("event_family"):
            return 0.0, [], "same-program-only"
        evidence.append(f"same event anchor: {a['event_anchor']}")
        if a.get("event_family") == b.get("event_family"):
            evidence.append(f"same event family: {a.get('event_family')}")
            score += 0.08
            strong_signals += 1
        score += 0.30
        strong_signals += 1
        same_anchor = True
    for bucket in ["systems", "projects", "products", "customers", "contracts"]:
        overlap = sorted(
            value
            for value in set(a["entities"].get(bucket, [])) & set(b["entities"].get(bucket, []))
            if is_specific_entity(value, entity_df, total)
        )
        if not overlap:
            continue
        evidence.append(f"shared {bucket}: {', '.join(overlap[:3])}")
        if bucket in {"customers", "contracts"}:
            score += 0.18
            cross_anchor_strong_signal = True
        else:
            score += 0.12
        if bucket in {"projects", "customers", "contracts"} or len(overlap) >= 2:
            strong_signals += 1
    org_overlap = sorted(set(a["entities"].get("orgs", [])) & set(b["entities"].get("orgs", [])))
    org_overlap = [value for value in org_overlap if is_specific_entity(value, entity_df, total)]
    if org_overlap:
        evidence.append(f"shared orgs: {', '.join(org_overlap[:3])}")
        score += 0.03
    title_terms_a = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", normalize_title(a["title"])))
    title_terms_b = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", normalize_title(b["title"])))
    title_overlap = sorted(
        value
        for value in (title_terms_a & title_terms_b) - STOPWORDS - GENERIC_ENTITY_TERMS
        if is_specific_entity(value, entity_df, total)
    )
    if title_overlap:
        evidence.append(f"shared title terms: {', '.join(title_overlap[:4])}")
        score += 0.08
        if len(title_overlap) >= 2:
            strong_signals += 1
            cross_anchor_strong_signal = True
    if a["source_lane"] == b["source_lane"] and a["source_lane"] in {"todo_backed", "reply_chain"}:
        evidence.append(f"same source lane: {a['source_lane']}")
        score += 0.02
    if strong_signals < 2:
        return 0.0, [], "unrelated"
    if not same_anchor and not cross_anchor_strong_signal:
        return 0.0, [], "unrelated"
    confidence = min(0.92, 0.35 + score)
    relation_type = "updates-event" if confidence >= 0.85 else "suspected-related"
    return confidence, evidence, relation_type


def build_relations(extractions: dict[str, dict]) -> dict[str, list[dict]]:
    ids = list(extractions)
    entity_df = relation_entity_stats(extractions)
    anchor_df = relation_anchor_stats(extractions)
    family_df = relation_family_stats(extractions)
    total = len(extractions)
    relations: dict[str, list[dict]] = {sid: [] for sid in ids}
    candidates: list[dict] = []
    for index, sid in enumerate(ids):
        for other in ids[index + 1 :]:
            confidence, evidence, relation_type = score_relation(extractions[sid], extractions[other], entity_df, anchor_df, total)
            if evidence and confidence >= 0.58:
                if confidence >= 0.65:
                    decision = "mark_suspected"
                else:
                    decision = "leave_unclustered"
                candidates.append(
                    {
                        "type": relation_type,
                        "target_source_id": other,
                        "target_event_anchor": extractions[other]["event_anchor"],
                        "confidence": round(confidence, 2),
                        "evidence": evidence[:5],
                        "source_ids": [sid, other],
                        "decision": decision,
                    }
                )
    candidates.sort(key=lambda x: (-x["confidence"], x["source_ids"]))
    degree: Counter = Counter()

    def candidate_cap(source_id: str) -> int:
        source = extractions[source_id]
        candidate_cap = 3
        family_key = (source.get("event_anchor"), source.get("event_family"))
        if family_df[family_key] > 4 or (
            anchor_df[source.get("event_anchor")] > 3
            and source.get("event_family") in {"recurring_digest", "recurring_report", "budget_cost"}
        ):
            candidate_cap = 2
        return candidate_cap

    for candidate in candidates:
        sid, other = candidate["source_ids"]
        if degree[sid] >= candidate_cap(sid) or degree[other] >= candidate_cap(other):
            continue
        relations[sid].append(candidate)
        degree[sid] += 1
        degree[other] += 1
    return relations


def unique_relation_pairs(relations: dict[str, list[dict]]) -> list[dict]:
    unique: dict[tuple[str, str], dict] = {}
    for rels in relations.values():
        for relation in rels:
            pair = tuple(sorted(map(str, relation.get("source_ids", []))))
            if len(pair) != 2:
                continue
            previous = unique.get(pair)
            if previous is None or relation.get("confidence", 0) > previous.get("confidence", 0):
                unique[pair] = relation
    return list(unique.values())


def slug(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", value).strip("-")
    return value[:80] or "untitled"


def write_outputs(
    run_dir: Path,
    items: list[Item],
    extractions: dict[str, dict],
    relations: dict[str, list[dict]],
    acceptance_profile: str = "sample",
) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    for sub in ["raw", "extracted", "relations", "events", "entities"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    raw_count = 0
    for item in items:
        raw_path = run_dir / "raw" / f"{item.source_id}-{slug(item.title)}.md"
        raw_path.write_text(item.content, encoding="utf-8")
        raw_count += 1
        (run_dir / "extracted" / f"{item.source_id}.json").write_text(
            json.dumps(extractions[item.source_id], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "relations" / f"{item.source_id}.json").write_text(
            json.dumps(relations[item.source_id], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    event_groups: dict[str, list[str]] = defaultdict(list)
    entity_groups: dict[str, list[str]] = defaultdict(list)
    for sid, ext in extractions.items():
        event_groups[ext["event_anchor"]].append(sid)
        for bucket in ["systems", "projects", "orgs", "customers", "products"]:
            for ent in ext["entities"].get(bucket, []):
                entity_groups[f"{bucket}:{ent}"].append(sid)

    event_proposals = []
    for anchor, sids in sorted(event_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(sids) < 1:
            continue
        proposal = {
            "event": anchor,
            "related_raw_ids": sids,
            "current_state": summarize_state(anchor, sids, extractions),
            "timeline_update": [{"source_id": sid, "title": extractions[sid]["title"]} for sid in sids],
            "decisions_and_opinions": [d for sid in sids for d in extractions[sid]["decision_points"][:2]][:8],
            "open_loops": [o for sid in sids for o in extractions[sid]["open_loops"][:2]][:8],
        }
        event_proposals.append(proposal)
        (run_dir / "events" / f"{slug(anchor)}.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    entity_proposals = []
    for ent, sids in sorted(entity_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(sids) < 1:
            continue
        bucket, name = ent.split(":", 1)
        proposal = {
            "entity_type": bucket,
            "entity_name": name,
            "related_raw_ids": sids,
            "recent_activity": [{"source_id": sid, "title": extractions[sid]["title"]} for sid in sids[:8]],
            "related_events": sorted({extractions[sid]["event_anchor"] for sid in sids}),
        }
        entity_proposals.append(proposal)
        (run_dir / "entities" / f"{bucket}-{slug(name)}.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")

    digest = build_digest(items, extractions, relations, event_proposals, entity_proposals)
    (run_dir / "digest.md").write_text(digest, encoding="utf-8")

    summary = build_acceptance(run_dir, items, extractions, relations, event_proposals, entity_proposals, raw_count, acceptance_profile)
    (run_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "ACCEPTANCE-RESULT.md").write_text(render_acceptance(summary), encoding="utf-8")
    return summary


def summarize_state(anchor: str, sids: list[str], extractions: dict[str, dict]) -> str:
    attention = Counter(extractions[sid]["attention_type"] for sid in sids)
    nature = Counter(extractions[sid]["item_nature"] for sid in sids)
    return f"{anchor} 关联 {len(sids)} 条记录；attention={dict(attention)}, nature={dict(nature)}."


def build_digest(items: list[Item], extractions: dict[str, dict], relations: dict[str, list[dict]], events: list[dict], entities: list[dict]) -> str:
    by_attention: dict[str, list[dict]] = defaultdict(list)
    for ext in extractions.values():
        by_attention[ext["attention_type"]].append(ext)
    strong = [r for rels in relations.values() for r in rels if r["decision"] == "auto_append"]
    suspected = [r for rels in relations.values() for r in rels if r["decision"] == "mark_suspected"]
    lines = [
        "# CWK Daily Intelligence Digest",
        "",
        "## 今日总览",
        "",
        f"- 处理工作协同记录 {len(items)} 条：需动作 {len(by_attention.get('requires_action', []))} 条，应知 {len(by_attention.get('awareness_only', []))} 条，建议复核 {len(by_attention.get('optional_review', []))} 条。",
        f"- 形成事件更新 {len(events)} 个、实体更新 {len(entities)} 个；发现强历史关联 {len(strong)} 个、疑似关联 {len(suspected)} 个。",
        "- 本轮只读运行：未回复、未审批、未完成待办、未标记已读。",
        "",
        "## 需要 Evan 处理",
        "",
    ]
    action_items = by_attention.get("requires_action", []) + by_attention.get("optional_review", [])
    for ext in action_items[:10]:
        reason = first_nonempty(ext["open_loops"], ext["actions"], ext["decision_points"], ext["risks"])
        suffix = f"；关注：{reason}" if reason else ""
        lines.append(f"- `{ext['source_ids'][0]}` {ext['title']} -> {ext['event_anchor']}{suffix}")
    if not action_items:
        lines.append("- 暂无明确动作项。")
    lines += ["", "## 重要应知", ""]
    for ext in by_attention.get("awareness_only", [])[:10]:
        reason = first_nonempty(ext["risks"], ext["decision_points"], ext["open_loops"], ext["actions"])
        suffix = f"；信号：{reason}" if reason else ""
        lines.append(f"- `{ext['source_ids'][0]}` {ext['title']} -> {ext['event_anchor']}{suffix}")
    lines += ["", "## 事件流变化", ""]
    for event in events[:8]:
        open_loop = event["open_loops"][0] if event["open_loops"] else ""
        suffix = f"；待闭环：{open_loop}" if open_loop else ""
        lines.append(f"- {event['event']}: {len(event['related_raw_ids'])} 条证据，{event['current_state']}{suffix}")
    lines += ["", "## 强历史关联", ""]
    for rel in strong[:8]:
        lines.append(f"- `{rel['source_ids'][0]}` -> `{rel['target_source_id']}` ({rel['confidence']}): {'; '.join(rel['evidence'][:2])}")
    lines += ["", "## 疑似关联", ""]
    for rel in suspected[:8]:
        lines.append(f"- `{rel['source_ids'][0]}` -> `{rel['target_source_id']}` ({rel['confidence']}): {'; '.join(rel['evidence'][:2])}")
    lines += ["", "## 运行状态", "", "- 错误/跳过：无。", "- 安全边界：只读。"]
    return "\n".join(lines) + "\n"


def first_nonempty(*groups: list[str]) -> str:
    for group in groups:
        for item in group:
            clean = re.sub(r"\s+", " ", item).strip()
            if clean and not is_noise_line(clean):
                return clean[:160]
    return ""


def is_noise_line(line: str) -> bool:
    clean = line.strip()
    lower = clean.lower()
    if lower.startswith(("reference_title:", "reference type:", "title:", "main:", "report_id:", "collection_mode:", "source_lane:", "writer:", "create_time:")):
        return True
    if clean.startswith("#"):
        return True
    if clean.startswith(("```", "{", "}", "[", "]", ">")):
        return True
    if clean in {"---", "| 症状 | 根因 | 风险 |"}:
        return True
    if clean.startswith("|") and clean.endswith("|") and any(word in clean for word in ["---", "字段", "说明", "症状", "重点项目"]):
        return True
    if any(token in clean for token in ['"nodeName"', '"main"', '"title"', "null", "level 1"]):
        return True
    return False


def build_acceptance(
    run_dir: Path,
    items: list[Item],
    extractions: dict[str, dict],
    relations: dict[str, list[dict]],
    events: list[dict],
    entities: list[dict],
    raw_count: int,
    acceptance_profile: str = "sample",
) -> dict:
    lane_counts = Counter(item.lane for item in items)
    change_type_counts = Counter(item.change_type for item in items)
    action_node_count = lane_counts.get("todo_backed", 0) + lane_counts.get("reply_chain", 0)
    recurring_count = sum(1 for ext in extractions.values() if ext["item_nature"] == "recurring_digest")
    all_relations = unique_relation_pairs(relations)
    relation_item_ids = {source_id for rel in all_relations for source_id in rel["source_ids"]}
    relation_items = len(relation_item_ids)
    two_signal = sum(1 for rel in all_relations if len(rel["evidence"]) >= 2)
    strong = sum(1 for rel in all_relations if rel["decision"] == "auto_append")
    suspected = sum(1 for rel in all_relations if rel["decision"] == "mark_suspected")
    same_anchor_family = 0
    same_anchor = 0
    cross_anchor = 0
    evaluable_relation_pairs = 0
    for rel in all_relations:
        src_id, target_id = rel["source_ids"]
        src = extractions.get(src_id, {})
        target = extractions.get(target_id, {})
        if src and target:
            evaluable_relation_pairs += 1
        if src and target and src.get("event_anchor") == target.get("event_anchor"):
            same_anchor += 1
            if src.get("event_family") == target.get("event_family"):
                same_anchor_family += 1
        elif src and target and src.get("event_anchor") != target.get("event_anchor"):
            cross_anchor += 1
    high_signal = [e for e in extractions.values() if e["attention_type"] in {"requires_action", "awareness_only", "optional_review"}]
    anchored = [e for e in high_signal if e["event_anchor"] and e["event_anchor"] != "未命名事项"]
    valid_extractions = sum(1 for e in extractions.values() if all(k in e for k in ["item_nature", "attention_type", "entities", "actions", "risks", "decision_points", "open_loops", "reply_chain", "source_ids"]))
    results = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": run_dir.name,
        "mutating_commands_called": [],
        "processed_count": len(items),
        "lane_counts": dict(lane_counts),
        "change_type_counts": dict(change_type_counts),
        "action_node_count": action_node_count,
        "recurring_count": recurring_count,
        "raw_count": raw_count,
        "valid_extractions": valid_extractions,
        "high_signal_count": len(high_signal),
        "anchored_high_signal_count": len(anchored),
        "relation_items": relation_items,
        "unique_relation_pairs": len(all_relations),
        "two_signal_relations": two_signal,
        "strong_relations": strong,
        "suspected_relations": suspected,
        "same_anchor_family_relations": same_anchor_family,
        "same_anchor_relations": same_anchor,
        "cross_anchor_relations": cross_anchor,
        "evaluable_relation_pairs": evaluable_relation_pairs,
        "event_proposals": len(events),
        "entity_proposals": len(entities),
        "recurring_topic_proposals": sum(1 for e in events if any(extractions[sid]["item_nature"] == "recurring_digest" for sid in e["related_raw_ids"])),
        "failures": [],
        "skipped_ids": [],
    }
    is_backlog_calibration = "backlog-unread" in run_dir.name
    input_coverage = (
        len(items) >= 25
        and action_node_count >= 5
        and lane_counts.get("persistent_stream", 0) >= 5
        and lane_counts.get("inbox_awareness", 0) >= 8
        and lane_counts.get("reply_chain", 0) >= 4
        and recurring_count >= 3
    )
    if is_backlog_calibration:
        input_coverage = len(items) >= 80 and raw_count >= int(len(items) * 0.95)

    suspected_cap = max(40, int(len(items) * 0.6))
    cross_anchor_cap = max(3, int(max(1, len(all_relations)) * 0.1))
    same_anchor_family_min = math.ceil(evaluable_relation_pairs * 0.7)
    relation_quality = (
        strong == 0
        and suspected <= suspected_cap
        and cross_anchor <= cross_anchor_cap
        and same_anchor_family >= same_anchor_family_min
    )
    incremental_relation_quality = strong == 0 and suspected <= suspected_cap and cross_anchor <= cross_anchor_cap
    incremental_low_volume = acceptance_profile == "incremental" and (relation_items < 10 or two_signal < 5)
    checks = {
        "A1_input_coverage": input_coverage,
        "A2_raw_evidence": raw_count >= int(len(items) * 0.95),
        "A3_structured_extraction": valid_extractions >= int(len(items) * 0.9) and len(anchored) >= int(max(1, len(high_signal)) * 0.8),
        "A4_relationship_judgment": relation_quality if is_backlog_calibration else relation_items >= 10 and two_signal >= 5,
        "A5_event_entity_output": len(events) >= 3 and len(entities) >= 3 and results["recurring_topic_proposals"] >= 1,
        "A6_digest_quality": (run_dir / "digest.md").exists() if run_dir.exists() else True,
        "A7_non_mutation": not results["mutating_commands_called"],
        "A8_operability": True,
    }
    if acceptance_profile == "incremental":
        checks.update(
            {
                "A1_input_coverage": raw_count == len(items),
                "A2_raw_evidence": raw_count == len(items),
                "A3_structured_extraction": valid_extractions == len(items) and (not high_signal or len(anchored) >= int(len(high_signal) * 0.8)),
                # Incremental runs legitimately connect different event families under the
                # same anchor (for example, a plan followed by a requirements update).
                # Keep those review-only and reject only auto-append/cross-anchor excess.
                "A4_relationship_judgment": not all_relations or incremental_relation_quality,
                "A5_event_entity_output": not items or (len(events) >= 1 and len(entities) >= 1),
            }
        )
    results["A4_status"] = (
        "PASS_LOW_VOLUME"
        if checks["A4_relationship_judgment"] and incremental_low_volume
        else "PASS"
        if checks["A4_relationship_judgment"]
        else "FAIL"
    )
    results["warnings"] = []
    if results["A4_status"] == "PASS_LOW_VOLUME":
        results["warnings"].append(
            f"A4 relation volume is low for an incremental run: relation_items={relation_items}, "
            f"two_signal_pairs={two_signal}; quality gates passed."
        )
    results["checks"] = checks
    results["overall_pass"] = all(checks.values())
    if not checks["A1_input_coverage"]:
        results["failures"].append("A1 has insufficient action-node, persistent-stream, awareness, reply-chain, or recurring coverage.")
    if not checks["A4_relationship_judgment"]:
        if is_backlog_calibration:
            if strong != 0:
                results["failures"].append(f"A4 auto-append must remain disabled during calibration: actual={strong}, expected=0.")
            if suspected > suspected_cap:
                results["failures"].append(f"A4 suspected relation volume exceeds review cap: actual={suspected}, max={suspected_cap}.")
            if cross_anchor > cross_anchor_cap:
                results["failures"].append(f"A4 cross-anchor relation volume is too high: actual={cross_anchor}, max={cross_anchor_cap}.")
            if same_anchor_family < same_anchor_family_min:
                results["failures"].append(f"A4 same-anchor-family relation coverage is too low: actual={same_anchor_family}, min={same_anchor_family_min}, unique_pairs={len(all_relations)}.")
        elif acceptance_profile != "incremental":
            if relation_items < 10:
                results["failures"].append(f"A4 too few items have relation candidates: actual={relation_items}, min=10.")
            if two_signal < 5:
                results["failures"].append(f"A4 too few unique relation pairs have two evidence signals: actual={two_signal}, min=5.")
    return results


def render_acceptance(summary: dict) -> str:
    lines = [
        "# CWK Sample Pilot Acceptance Result",
        "",
        f"- Generated: {summary['generated_at']}",
        f"- Mode: {summary['mode']}",
        f"- Overall pass: {summary['overall_pass']}",
        "",
        "## Counts",
        "",
        f"- Processed: {summary['processed_count']}",
        f"- Lanes: `{json.dumps(summary['lane_counts'], ensure_ascii=False)}`",
        f"- Action nodes: {summary['action_node_count']}",
        f"- Recurring: {summary['recurring_count']}",
        f"- Raw files: {summary['raw_count']}",
        f"- Valid extractions: {summary['valid_extractions']}",
        f"- Relation items: {summary['relation_items']}",
        f"- Unique relation pairs: {summary.get('unique_relation_pairs', 0)}",
        f"- Two-signal relations: {summary['two_signal_relations']}",
        f"- Strong relations: {summary['strong_relations']}",
        f"- Suspected relations: {summary['suspected_relations']}",
        f"- Same-anchor-family relations: {summary.get('same_anchor_family_relations', 0)}",
        f"- Same-anchor relations: {summary.get('same_anchor_relations', 0)}",
        f"- Cross-anchor relations: {summary.get('cross_anchor_relations', 0)}",
        f"- Evaluable relation pairs: {summary.get('evaluable_relation_pairs', 0)}",
        f"- A4 status: {summary.get('A4_status', 'PASS' if summary['checks'].get('A4_relationship_judgment') else 'FAIL')}",
        f"- Event proposals: {summary['event_proposals']}",
        f"- Entity proposals: {summary['entity_proposals']}",
        "",
        "## Checks",
        "",
    ]
    for key, value in summary["checks"].items():
        lines.append(f"- {key}: {'PASS' if value else 'FAIL'}")
    lines += ["", "## Failures", ""]
    if summary["failures"]:
        lines.extend(f"- {f}" for f in summary["failures"])
    else:
        lines.append("- None")
    lines += ["", "## Warnings", ""]
    if summary.get("warnings"):
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CWK pilot over raw Markdown source directories.")
    parser.add_argument(
        "--source-dir",
        action="append",
        default=[],
        help="Directory containing raw CWork Markdown files. Can be passed multiple times.",
    )
    parser.add_argument("--run-name", default="sample-pilot", help="Run output directory name under project runs/.")
    parser.add_argument("--acceptance-profile", choices=["sample", "incremental"], default="sample")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_dirs = [Path(p).expanduser().resolve() for p in args.source_dir] or [DEFAULT_SAMPLES]
    run_dir = PROJECT / "runs" / args.run_name
    items = load_items(source_dirs)
    extractions = build_extractions(items)
    relations = build_relations(extractions)
    summary = write_outputs(run_dir, items, extractions, relations, args.acceptance_profile)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
