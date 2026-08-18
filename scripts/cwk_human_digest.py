#!/usr/bin/env python3
"""Render a human-first CWK digest from a processed run."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from cwk_person_relation import classify_person_relation, load_relationship_manifest


PROJECT = Path(__file__).resolve().parents[1]


def date_part(value: str) -> str:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value or "")
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def embedded_json(text: str, heading: str) -> dict:
    pattern = rf"## {re.escape(heading)}\s+```json\s*(\{{.*?\}})\s*```"
    match = re.search(pattern, text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def load_raw_metadata(
    run_dir: Path,
    owner_emp_id: str = "",
    owner_name: str = "",
    relation_by_id: dict[str, dict] | None = None,
) -> dict[str, dict]:
    metadata = {}
    for path in sorted((run_dir / "raw").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("---"):
            continue
        match = re.match(r"---\n(.*?)\n---", text, re.S)
        if not match:
            continue
        fields = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
        writer = fields.get("writer", "")
        create_time = fields.get("create_time", "")
        if not writer:
            meta_writer = re.search(r"\*\*汇报人\*\*:\s*([^\n]+)", text)
            writer = meta_writer.group(1).strip() if meta_writer else ""
        if not create_time:
            meta_time = re.search(r"\*\*时间\*\*:\s*([^\n]+)", text)
            create_time = meta_time.group(1).strip() if meta_time else ""
        sid = fields.get("report_id") or path.name.split("-", 1)[0]
        scopes = {value for value in fields.get("source_scopes", "").split(",") if value}
        relationship = classify_person_relation(
            backend_relation=(relation_by_id or {}).get(sid),
        )
        metadata[sid] = {
            "writer": writer or "未知",
            "create_time": create_time or "未知时间",
            "title": fields.get("title", ""),
            "change_type": fields.get("change_type", "unknown"),
            "collection_mode": fields.get("collection_mode", "reference-sample"),
            "business_date": date_part(create_time),
            **relationship,
        }
    return metadata


def clean_text(value: str, limit: int = 150) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = re.sub(r"&nbsp;", " ", value)
    value = re.sub(r"[🟢🟡🔴✅⚠️❗️📌👉]", "", value)
    value = re.sub(r"\*\*|__|`", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" -*#|:：")
    if not value:
        return ""
    noise_prefixes = (
        "reference_title:",
        "reference type:",
        "title:",
        "main:",
        "report_id:",
        "nodeName",
        '"nodeName"',
        '"content"',
        '"textContent"',
        "content",
        "textContent",
        "任务名称",
        "任务描述",
        "主题",
        "汇报主题",
        "报时间",
        "汇报人",
    )
    if value.lower().startswith(noise_prefixes):
        return ""
    if value.startswith("|") or value.startswith("{") or value.startswith("["):
        return ""
    if "|" in value or value.count(":") >= 4:
        return ""
    if any(token in value for token in ['"status"', '"nodeName"', '"content"', '"textContent"', "主题**:", "会议主题：**", "参会人员：**", "level 1", "null", "huiJiId=", "linkType="]):
        return ""
    if re.fullmatch(r"[\d\s./:：年月日第多少期周()-]+", value):
        return ""
    return value[:limit]


def first_signal(ext: dict) -> str:
    for key in ["risks", "open_loops", "decision_points", "actions"]:
        for item in ext.get(key, []):
            clean = clean_text(item)
            if clean:
                return clean
    return ""


def item_signals(ext: dict, limit: int = 3) -> list[str]:
    signals = []
    for key in ["risks", "decision_points", "actions", "open_loops"]:
        for item in ext.get(key, []):
            clean = clean_text(item, limit=180)
            if clean and clean not in signals:
                signals.append(clean)
            if len(signals) >= limit:
                return signals
    return signals


def short_title(title: str) -> str:
    title = re.sub(r"[【】]", "", title or "").strip()
    return title[:70]


GENERIC_ANCHORS = {
    "",
    "未命名事项",
    "New",
    "⚠️",
    "2025年",
    "2026年",
    "下半年",
    "昨日",
    "今天",
    "工作说说",
}


def is_generic_anchor(name: str) -> bool:
    name = (name or "").strip()
    if name in GENERIC_ANCHORS:
        return True
    if re.fullmatch(r"\d{4}年?", name):
        return True
    if len(name) <= 1:
        return True
    return False


def item_group_key(ext: dict) -> str:
    title = ext.get("title", "")
    anchor = ext.get("event_anchor") or "未命名事项"
    family = ext.get("event_family") or ext.get("item_nature") or "unknown"
    title = re.sub(r"\d{2}/\d{2}[-_ ]*", "", title)
    title = re.sub(r"\d{4}[-./年]\d{1,2}[-./月]\d{1,2}日?", "", title)
    title = re.sub(r"\d+月第[一二三四五六七八九十]+周", "周度", title)
    title = re.sub(r"\d{4}[.-]\d{2}[.-]\d{2}", "", title)
    title = re.sub(r"\d{4,}", "", title)
    title = re.sub(r"[「」'\"（）()【】#]", "", title)
    title = re.sub(r"\s+", "", title)
    if "采集表复核申请" in title:
        return "采集表复核申请"
    if "云端虾申请" in title or "开通云端虾" in title or "申请云端虾" in title or ("云端虾" in title and "申请" in title):
        return "云端虾权限申请"
    if "AI费用日报" in title:
        return "AI费用日报"
    if "AI费用周报" in title:
        return "AI费用周报"
    return f"{anchor}:{family}:{title[:18]}"


def group_items(items: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for ext in items:
        key = item_group_key(ext)
        group = groups.setdefault(
            key,
            {
                "sample": ext,
                "count": 0,
                "anchors": Counter(),
                "signals": [],
                "items": [],
            },
        )
        group["count"] += 1
        group["items"].append(ext)
        group["anchors"][ext.get("event_anchor") or "未命名事项"] += 1
        signal = first_signal(ext)
        if signal and signal not in group["signals"]:
            group["signals"].append(signal)
    return sorted(groups.values(), key=group_sort_key)


def group_sort_key(group: dict) -> tuple[int, int, int, str]:
    sample = group["sample"]
    attention_priority = {"requires_action": 0, "optional_review": 1, "awareness_only": 2}.get(sample.get("attention_type"), 3)
    family = sample.get("event_family") or sample.get("item_nature") or ""
    title = sample.get("title", "")
    signal = 1 if group["signals"] else 0
    priority = 40
    if family in {"planning_design", "requirements", "issue_tracking", "technical_plan"}:
        priority = 0
    elif family in {"persistent_stream", "administrative_approval"}:
        priority = 10
    elif family in {"recurring_report", "recurring_digest"}:
        priority = 30
    elif family in {"access_request", "budget_cost"}:
        priority = 50
    if any(token in title for token in ["权限申请", "申请云端虾", "云端虾申请", "AI费用日报", "采集表复核"]):
        priority += 30
    if any(token in title for token in ["方案", "会议纪要", "架构", "风险", "问题", "需求", "优化", "评估"]):
        priority -= 10
    return attention_priority, priority, -signal, sample.get("title", "")


def sort_key(ext: dict) -> tuple[int, str]:
    attention = ext.get("attention_type")
    priority = {"requires_action": 0, "optional_review": 1, "awareness_only": 2}.get(attention, 3)
    return priority, ext.get("title", "")


def load_run_summary(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    return load_json(path) if path.exists() else {}


def event_family_counts(event: dict, extracted_by_id: dict[str, dict]) -> Counter:
    counts = Counter()
    for sid in event.get("related_raw_ids", []):
        ext = extracted_by_id.get(sid)
        if ext:
            counts[ext.get("event_family") or ext.get("item_nature") or "unknown"] += 1
    return counts


def build_extracted_index(extracted: list[dict]) -> dict[str, dict]:
    indexed = {}
    for ext in extracted:
        for sid in ext.get("source_ids") or []:
            indexed.setdefault(sid, ext)
    return indexed


def event_signal(event: dict) -> str:
    for key in ["open_loops", "decisions_and_opinions"]:
        for item in event.get(key, []):
            clean = clean_text(item, limit=180)
            if clean:
                return clean
    return ""


def event_score(event: dict, extracted_by_id: dict[str, dict]) -> tuple[int, int, int]:
    name = event.get("event", "")
    signal_bonus = 1 if event_signal(event) else 0
    generic_penalty = -100 if is_generic_anchor(name) else 0
    count = len(event.get("related_raw_ids", []))
    families = len(event_family_counts(event, extracted_by_id))
    return generic_penalty + signal_bonus + min(count, 20), families, count


def primary_source_id(ext: dict) -> str:
    ids = ext.get("source_ids") or []
    return ids[0] if ids else ""


def newest_item(items: list[dict], metadata: dict[str, dict]) -> dict:
    return max(
        items,
        key=lambda ext: metadata.get(primary_source_id(ext), {}).get("create_time", ""),
    )


def render_group_block(group: dict, metadata: dict[str, dict]) -> list[str]:
    sample = newest_item(group["items"], metadata)
    count = group["count"]
    anchors = "、".join(name for name, _ in group["anchors"].most_common(3))
    title = short_title(sample.get("title"))
    count_text = f"{count} 条同类" if count > 1 else "1 条"
    meta = metadata.get(primary_source_id(sample), {})
    writer = meta.get("writer", "未知")
    create_time = meta.get("create_time", "未知时间")
    signals = item_signals(sample, limit=3)
    source_ids = {
        sid
        for item in group["items"]
        for sid in (item.get("source_ids") or [])
    }
    visible_only_count = sum(
        1 for sid in source_ids if metadata.get(sid, {}).get("visible_only")
    )
    visibility_badge = " 【仅权限可见 · 与我无关】" if source_ids and visible_only_count == len(source_ids) else ""
    visibility_note = f" · 其中 {visible_only_count} 条仅权限可见" if 0 < visible_only_count < len(source_ids) else ""
    unknown_count = sum(
        1 for sid in source_ids if metadata.get(sid, {}).get("relationship_status") == "unknown"
    )
    relationship_note = f" · 其中 {unknown_count} 条关系待确认" if unknown_count else ""
    lines = [
        f"- {title}{visibility_badge}",
        f"  发送：{writer} · {create_time} · {count_text} · 归到 {anchors}{visibility_note}{relationship_note}",
    ]
    signals = [signal.strip("；; ") for signal in signals if signal.strip("；; ")]
    if signals:
        lines.append(f"  摘要：{'；'.join(signals)}")
    else:
        lines.append("  摘要：暂无可读摘要，需回看原文。")
    return lines


def render(
    run_dir: Path,
    report_date: str = "",
    owner_emp_id: str = "",
    owner_name: str = "",
    relationship_manifest: str | Path | None = None,
) -> str:
    extracted = [load_json(path) for path in sorted((run_dir / "extracted").glob("*.json"))]
    extracted_by_id = build_extracted_index(extracted)
    relation_by_id, relation_meta = load_relationship_manifest(relationship_manifest)
    metadata = load_raw_metadata(run_dir, owner_emp_id, owner_name, relation_by_id)
    events = [load_json(path) for path in sorted((run_dir / "events").glob("*.json"))]
    summary = load_run_summary(run_dir)

    def effective_change(ext: dict) -> str:
        meta = metadata.get(primary_source_id(ext), {})
        current_backfill = bool(
            report_date
            and meta.get("business_date") == report_date
            and (ext.get("change_type") == "historical_backfill" or ext.get("collection_mode") == "historical-backfill")
        )
        return "new" if current_backfill else str(ext.get("change_type") or "unknown")

    old_historical = [
        ext for ext in extracted
        if (ext.get("change_type") == "historical_backfill" or ext.get("collection_mode") == "historical-backfill")
        and effective_change(ext) != "new"
    ]
    visible = [ext for ext in extracted if ext not in old_historical]
    today = [ext for ext in visible if ext.get("attention_type") == "requires_action" or effective_change(ext) == "new"]
    recent_changes = [ext for ext in visible if effective_change(ext) == "updated"]
    ongoing = [ext for ext in visible if effective_change(ext) == "continuation" and ext.get("attention_type") != "requires_action"]

    today.sort(key=sort_key)
    recent_changes.sort(key=sort_key)
    ongoing.sort(key=sort_key)
    required_count = sum(1 for ext in today if ext.get("attention_type") == "requires_action")
    visible_source_ids = {
        sid for ext in visible for sid in (ext.get("source_ids") or [])
    }
    visible_only_count = sum(
        1 for sid in visible_source_ids if metadata.get(sid, {}).get("visible_only")
    )
    relationship_unknown_count = sum(
        1 for sid in visible_source_ids if metadata.get(sid, {}).get("relationship_status") == "unknown"
    )
    today_groups = group_items(today)
    recent_groups = group_items(recent_changes)
    ongoing_groups = group_items(ongoing)
    visible_ids = {sid for ext in visible for sid in ext.get("source_ids", [])}
    event_rank = sorted(
        [event for event in events if not is_generic_anchor(event.get("event", "")) and visible_ids.intersection(event.get("related_raw_ids", []))],
        key=lambda e: (-event_score(e, extracted_by_id)[0], -event_score(e, extracted_by_id)[1], -event_score(e, extracted_by_id)[2], e.get("event", "")),
    )

    change_counts = summary.get("change_type_counts") or Counter(ext.get("change_type", "unknown") for ext in extracted)
    lines = [
        "# 工作协同每日简报（人读版 v4）",
        "",
        "## 一句话结论",
        "",
        f"这轮只读处理 {summary.get('processed_count', len(extracted))} 条工作协同。按日报日期 `{report_date or '未指定'}` 重新分流后：今日处理 {len(today)} 条（其中明确待处理 {required_count} 条）、近期变更 {len(recent_changes)} 条、持续未闭环 {len(ongoing)} 条、仅权限可见 {visible_only_count} 条、关系待确认 {relationship_unknown_count} 条、普通历史补齐 {len(old_historical)} 条。没有执行任何变更操作。",
        "",
        "## 今日处理",
        "",
    ]

    for group in today_groups:
        lines.extend(render_group_block(group, metadata))
    if not today_groups:
        lines.append("- 今天没有新增汇报或需要你处理的待办。")

    lines += ["", "## 近期变更", ""]
    for group in recent_groups:
        lines.extend(render_group_block(group, metadata))
    if not recent_groups:
        lines.append("- 本轮没有检测到历史汇报的真实变化。")

    lines += ["", "## 持续未闭环", ""]
    for group in ongoing_groups:
        lines.extend(render_group_block(group, metadata))
    if not ongoing_groups:
        lines.append("- 当前没有持续未闭环事项。")

    lines += [
        "",
        "## 本版质量边界",
        "",
        f"- 人与汇报的业务关系只接受工作协同后台权威接口；当前接口状态 `{relation_meta.get('provider_status', 'unavailable')}`，未解析条目统一标为关系待确认。",
        "- 事件页会过滤明显泛化锚点，正式沉淀前只保留可审页面；低质量锚点仍留在 run 目录供追溯。",
        "- 采集表、权限申请、日报周报这类高重复内容已按组展示，避免挤占管理注意力。",
        f"- 另有 {len(old_historical)} 条普通历史补齐仅进入知识库，不进入今日处理或近期变更页签。",
        "",
        "## 证据入口",
        "",
        f"- 原始运行目录：`{run_dir.relative_to(PROJECT)}`",
        "- 需要追溯时看 `raw/`，需要看机器判断时看 `extracted/` 和 `relations/`。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a human-first CWK digest.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report-date", default="", help="Business date represented by this digest (YYYY-MM-DD).")
    parser.add_argument("--owner-emp-id", default="", help="Current CWork employee ID used for relationship classification.")
    parser.add_argument("--owner-name", default="", help="Current CWork employee name; fallback only when a candidate has no employee ID.")
    parser.add_argument("--relationship-manifest", default=None, help="Backend-owned relationship manifest for this run.")
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    output = Path(args.output) if args.output else run_dir / "digest-human.md"
    output.write_text(render(run_dir, args.report_date, args.owner_emp_id, args.owner_name, args.relationship_manifest), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
