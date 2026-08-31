#!/usr/bin/env python3
"""Compile CWK wiki topic/entity pages from traceable source summaries.

This stage is intentionally deterministic. It reads `wiki/summaries/*.md`,
extracts candidate topics/entities and evidence-bearing sections, and writes
aggregate pages under `wiki/topics/` and `wiki/entities/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cwk_ai_common import parse_frontmatter


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"
SCHEMA = "cwk.cloud_wiki_topics_entities.v1"
ENTITY_DIR_MAP = {
    "person": "people",
    "organization": "organizations",
    "project": "projects",
    "system": "systems",
    "product": "products",
    "other": "other",
}
QUOTE_LIMIT = 180
LIST_LIMIT = 12
EVIDENCE_LIMIT = 16
REPORT_LIMIT = 30
SOURCE_CATALOG_SHARD_SIZE = 100
QUERY_CONTRACT = """# CWK Trusted Query Contract

本 Wiki 是导航层，`../raw/` 是最终事实层。

1. 先使用 topics/entities/summaries 定位候选 `report_id`。
2. 回答前必须回读 raw，或使用已经在 raw 中验证的逐字引文。
3. 每个实质结论必须引用 `report_id` 与原文路径；多来源冲突应并列展示。
4. 证据不足、链接损坏或检索置信度为 none 时必须拒答。
5. Wiki 不授权回复、审批、转办、标已读或其他 CWork 写操作。

本地可信检索入口：`python3 scripts/cwk_wiki_query.py "<question>"`。
"""


@dataclass
class SummaryItem:
    text: str
    quote: str
    extra: str | None = None


@dataclass
class SummaryDoc:
    report_id: str
    title: str
    summary_path: Path
    raw_rel: str
    writer: str
    created_at: str
    source_lane: str
    summary_text: str
    topics: list[SummaryItem] = field(default_factory=list)
    entities: list[SummaryItem] = field(default_factory=list)
    decisions: list[SummaryItem] = field(default_factory=list)
    actions: list[SummaryItem] = field(default_factory=list)
    risks: list[SummaryItem] = field(default_factory=list)
    facts: list[SummaryItem] = field(default_factory=list)


@dataclass
class AggregateEntry:
    page_name: str
    page_slug: str
    page_rel: str
    page_type: str
    item_type: str
    report_ids: set[str] = field(default_factory=set)
    report_titles: dict[str, str] = field(default_factory=dict)
    report_times: dict[str, str] = field(default_factory=dict)
    source_items: list[dict[str, str]] = field(default_factory=list)
    decisions: list[dict[str, str]] = field(default_factory=list)
    actions: list[dict[str, str]] = field(default_factory=list)
    risks: list[dict[str, str]] = field(default_factory=list)
    facts: list[dict[str, str]] = field(default_factory=list)
    related_entities: Counter[str] = field(default_factory=Counter)
    related_topics: Counter[str] = field(default_factory=Counter)


def clean_text(value: str, limit: int = 500) -> str:
    value = re.sub(r"`+", "", value or "")
    value = re.sub(r"\s+", " ", value).strip(" -:：,，;；")
    return value[:limit]


def slug(value: str) -> str:
    value = clean_text(value, 200)
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"[<>:\"|?*]+", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip(".-")
    return value[:120] or "untitled"


def normalize_name(value: str) -> str:
    value = clean_text(value, 200)
    value = value.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"[\"'“”‘’]", "", value)
    value = re.sub(r"\s+", "", value)
    return value.lower()


def parse_sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
            continue
        if current:
            sections[current].append(line.rstrip())
    return sections


def parse_bullet_pairs(lines: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    current_label: str | None = None
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("- "):
            current_label = stripped[2:].strip()
            continue
        evidence = stripped.strip()
        if current_label and evidence.startswith("证据：>"):
            pairs.append((current_label, clean_text(evidence.removeprefix("证据：>").strip(), QUOTE_LIMIT)))
            current_label = None
    return pairs


def parse_section_items(lines: list[str], mode: str) -> list[SummaryItem]:
    items: list[SummaryItem] = []
    for label, quote in parse_bullet_pairs(lines):
        if mode == "entity":
            match = re.match(r"^(.*?)\s+`([^`]+)`$", label)
            if not match:
                continue
            items.append(SummaryItem(text=clean_text(match.group(1), 200), quote=quote, extra=clean_text(match.group(2), 40)))
            continue
        if mode == "risk":
            match = re.match(r"^(.*?)\s+\[([^\]]+)\]$", label)
            if match:
                items.append(SummaryItem(text=clean_text(match.group(1), 240), quote=quote, extra=clean_text(match.group(2), 20)))
            else:
                items.append(SummaryItem(text=clean_text(label, 240), quote=quote, extra="unknown"))
            continue
        items.append(SummaryItem(text=clean_text(label, 240), quote=quote))
    return items


def load_summary(path: Path) -> SummaryDoc:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    report_id = clean_text(meta.get("report_id", ""), 40)
    if not report_id:
        raise ValueError(f"summary missing report_id: {path}")
    sections = parse_sections(body)
    title_match = re.search(r"^#\s+(.+)$", body, flags=re.M)
    title = clean_text(title_match.group(1) if title_match else path.stem, 200)
    writer = ""
    created_at = ""
    source_lane = ""
    raw_rel = clean_text(meta.get("source", ""), 260)
    for line in body.splitlines():
        if line.startswith("- 发送人："):
            writer = clean_text(line.split("：", 1)[1], 80)
        elif line.startswith("- 时间："):
            created_at = clean_text(line.split("：", 1)[1], 80)
        elif line.startswith("- 来源类型："):
            source_lane = clean_text(line.split("：", 1)[1], 80)
        elif line.startswith("- 原文：[`") and "](../../" in line and not raw_rel:
            raw_rel = clean_text(line.split("](../../", 1)[1].rstrip(")"), 260)
    return SummaryDoc(
        report_id=report_id,
        title=title,
        summary_path=path,
        raw_rel=raw_rel,
        writer=writer,
        created_at=created_at,
        source_lane=source_lane,
        summary_text=clean_text("\n".join(sections.get("摘要", [])), 260),
        topics=parse_section_items(sections.get("候选主题", []), "plain"),
        entities=parse_section_items(sections.get("候选实体", []), "entity"),
        decisions=parse_section_items(sections.get("决策", []), "plain"),
        actions=parse_section_items(sections.get("行动项", []), "plain"),
        risks=parse_section_items(sections.get("风险", []), "risk"),
        facts=parse_section_items(sections.get("关键事实", []), "plain"),
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> bool:
    ensure_dir(path.parent)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ensure_dir(path.parent)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def to_sort_time(value: str) -> str:
    value = clean_text(value, 40)
    if not value or "未知" in value:
        return "9999-99-99 99:99:99"
    return value


def choose_display_name(names: list[str]) -> str:
    counter = Counter(name for name in names if name)
    if not counter:
        return "未命名"
    return sorted(counter.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0]


def add_collection(target: list[dict[str, str]], items: list[SummaryItem], report_id: str, report_title: str) -> None:
    for item in items:
        target.append(
            {
                "text": item.text,
                "quote": item.quote,
                "extra": item.extra or "",
                "report_id": report_id,
                "title": report_title,
            }
        )


def collect_aggregates(
    summaries: list[SummaryDoc],
    min_topic_reports: int,
    min_entity_reports: int,
    topic_limit: int | None,
    entity_limit: int | None,
) -> tuple[list[AggregateEntry], list[AggregateEntry]]:
    raw_topics: dict[str, AggregateEntry] = {}
    raw_entities: dict[str, AggregateEntry] = {}
    topic_names: dict[str, list[str]] = defaultdict(list)
    entity_names: dict[str, list[str]] = defaultdict(list)

    for doc in summaries:
        doc_topic_names = [item.text for item in doc.topics if item.text]
        doc_entity_names = [item.text for item in doc.entities if item.text]
        for item in doc.topics:
            key = normalize_name(item.text)
            if len(key) < 2:
                continue
            topic_names[key].append(item.text)
            if key not in raw_topics:
                raw_topics[key] = AggregateEntry(
                    page_name=item.text,
                    page_slug=slug(item.text),
                    page_rel=f"topics/{slug(item.text)}.md",
                    page_type="topic",
                    item_type="topic",
                )
            entry = raw_topics[key]
            entry.report_ids.add(doc.report_id)
            entry.report_titles[doc.report_id] = doc.title
            entry.report_times[doc.report_id] = doc.created_at
            entry.source_items.append({"quote": item.quote, "report_id": doc.report_id, "title": doc.title})
            add_collection(entry.decisions, doc.decisions, doc.report_id, doc.title)
            add_collection(entry.actions, doc.actions, doc.report_id, doc.title)
            add_collection(entry.risks, doc.risks, doc.report_id, doc.title)
            add_collection(entry.facts, doc.facts, doc.report_id, doc.title)
            for entity_name in doc_entity_names:
                if normalize_name(entity_name) != key:
                    entry.related_entities[entity_name] += 1
            for topic_name in doc_topic_names:
                if normalize_name(topic_name) != key:
                    entry.related_topics[topic_name] += 1

        for item in doc.entities:
            key = f"{item.extra or 'other'}::{normalize_name(item.text)}"
            if len(key) < 4:
                continue
            entity_names[key].append(item.text)
            entity_type = item.extra or "other"
            page_dir = ENTITY_DIR_MAP.get(entity_type, "other")
            if key not in raw_entities:
                raw_entities[key] = AggregateEntry(
                    page_name=item.text,
                    page_slug=slug(item.text),
                    page_rel=f"entities/{page_dir}/{slug(item.text)}.md",
                    page_type="entity",
                    item_type=entity_type,
                )
            entry = raw_entities[key]
            entry.report_ids.add(doc.report_id)
            entry.report_titles[doc.report_id] = doc.title
            entry.report_times[doc.report_id] = doc.created_at
            entry.source_items.append({"quote": item.quote, "report_id": doc.report_id, "title": doc.title})
            add_collection(entry.decisions, doc.decisions, doc.report_id, doc.title)
            add_collection(entry.actions, doc.actions, doc.report_id, doc.title)
            add_collection(entry.risks, doc.risks, doc.report_id, doc.title)
            add_collection(entry.facts, doc.facts, doc.report_id, doc.title)
            for topic_name in doc_topic_names:
                entry.related_topics[topic_name] += 1
            for entity_name in doc_entity_names:
                if normalize_name(entity_name) != normalize_name(item.text):
                    entry.related_entities[entity_name] += 1

    topic_entries = list(raw_topics.values())
    entity_entries = list(raw_entities.values())
    for key, entry in raw_topics.items():
        entry.page_name = choose_display_name(topic_names[key])
    for key, entry in raw_entities.items():
        entry.page_name = choose_display_name(entity_names[key])

    topic_entries = [entry for entry in topic_entries if len(entry.report_ids) >= min_topic_reports]
    entity_entries = [entry for entry in entity_entries if len(entry.report_ids) >= min_entity_reports]
    topic_entries.sort(key=lambda entry: (-len(entry.report_ids), entry.page_name))
    entity_entries.sort(key=lambda entry: (-len(entry.report_ids), entry.page_name))
    if topic_limit is not None:
        topic_entries = topic_entries[:topic_limit]
    if entity_limit is not None:
        entity_entries = entity_entries[:entity_limit]
    return topic_entries, entity_entries


def unique_support(items: list[dict[str, str]], text_key: str, limit: int = LIST_LIMIT) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in items:
        key = normalize_name(item.get(text_key, ""))
        if key:
            grouped[key].append(item)
    ranked = sorted(
        grouped.values(),
        key=lambda group: (-len({item["report_id"] for item in group}), group[0].get(text_key, "")),
    )
    result: list[dict[str, str]] = []
    for group in ranked[:limit]:
        first = group[0]
        report_ids = sorted({item["report_id"] for item in group})
        result.append(
            {
                "text": first.get(text_key, ""),
                "quote": first.get("quote", ""),
                "extra": first.get("extra", ""),
                "report_ids": "、".join(report_ids[:6]),
                "title": first.get("title", ""),
            }
        )
    return result


def report_link(report_id: str, entry: AggregateEntry) -> str:
    prefix = "../" if entry.page_type == "topic" else "../../"
    return f"[`{report_id}`]({prefix}summaries/{report_id}.md)"


def render_aggregate(entry: AggregateEntry) -> str:
    report_rows = sorted(
        entry.report_ids,
        key=lambda report_id: (to_sort_time(entry.report_times.get(report_id, "")), report_id),
    )
    created_times = [entry.report_times[report_id] for report_id in report_rows if entry.report_times.get(report_id)]
    overview_lines = [
        "---",
        f'type: "{ "TopicPage" if entry.page_type == "topic" else "EntityPage" }"',
        f'name: "{entry.page_name}"',
        f'page_kind: "{entry.page_type}"',
        f'item_type: "{entry.item_type}"',
        f"report_count: {len(entry.report_ids)}",
        f'generated_by: "{SCHEMA}"',
        "---",
        "",
        f"# {entry.page_name}",
        "",
        f"- 页面类型：`{entry.page_type}`",
        f"- {'实体类型' if entry.page_type == 'entity' else '聚合方式'}：`{entry.item_type if entry.page_type == 'entity' else '候选主题精确聚合'}`",
        f"- 覆盖报告：**{len(entry.report_ids)}**",
    ]
    if created_times:
        overview_lines.append(f"- 时间范围：{min(created_times)} → {max(created_times)}")
    overview_lines.extend(["", "## 相关报告", ""])
    for report_id in report_rows[:REPORT_LIMIT]:
        overview_lines.append(f"- {report_link(report_id, entry)} {entry.report_titles.get(report_id, '')}")

    overview_lines.extend(["", "## 证据摘录", ""])
    seen_evidence = set()
    for item in sorted(entry.source_items, key=lambda value: value["report_id"]):
        evidence_key = (item["report_id"], item["quote"])
        if evidence_key in seen_evidence:
            continue
        seen_evidence.add(evidence_key)
        overview_lines.append(f"- {report_link(item['report_id'], entry)} {item['quote']}")
        if len(seen_evidence) >= EVIDENCE_LIMIT:
            break

    facts = unique_support(entry.facts, "text")
    if facts:
        overview_lines.extend(["", "## 相关事实", ""])
        for item in facts:
            overview_lines.append(f"- {item['text']} 来源：{item['report_ids']}；证据：{item['quote']}")

    decisions = unique_support(entry.decisions, "text")
    if decisions:
        overview_lines.extend(["", "## 相关决策", ""])
        for item in decisions:
            overview_lines.append(f"- {item['text']} 来源：{item['report_ids']}；证据：{item['quote']}")

    actions = unique_support(entry.actions, "text")
    if actions:
        overview_lines.extend(["", "## 相关行动项", ""])
        for item in actions:
            overview_lines.append(f"- {item['text']} 来源：{item['report_ids']}；证据：{item['quote']}")

    risks = unique_support(entry.risks, "text")
    if risks:
        overview_lines.extend(["", "## 相关风险", ""])
        for item in risks:
            severity = f" [{item['extra']}]" if item.get("extra") else ""
            overview_lines.append(f"- {item['text']}{severity} 来源：{item['report_ids']}；证据：{item['quote']}")

    related = entry.related_entities if entry.page_type == "topic" else entry.related_topics
    if related:
        heading = "相关实体" if entry.page_type == "topic" else "相关主题"
        overview_lines.extend(["", f"## {heading}", ""])
        for name, count in related.most_common(10):
            overview_lines.append(f"- {name}（{count}）")

    overview_lines.extend(["", "## 证据边界", "", "本页由 source summaries 的候选主题/实体与证据条目确定性聚合而成；事实以原始 report_id 对应原文为准。"])
    return "\n".join(overview_lines) + "\n"


def render_dir_index(title: str, root_rel: str, entries: list[AggregateEntry]) -> str:
    lines = [f"# {title}", ""]
    if not entries:
        lines.append("- 暂无页面。")
        return "\n".join(lines) + "\n"
    for entry in entries:
        rel = entry.page_rel[len(root_rel) + 1 :] if entry.page_rel.startswith(root_rel + "/") else Path(entry.page_rel).name
        lines.append(f"- [{entry.page_name}]({rel}) · {len(entry.report_ids)} reports")
    return "\n".join(lines) + "\n"


def render_source_catalog(rows: list[SummaryDoc], shard_number: int) -> str:
    lines = [
        f"# 原文目录 {shard_number:03d}",
        "",
        "每项链接到工作协同原文；本目录用于定位，不替代原文证据。",
        "",
    ]
    for row in rows:
        raw_link = f"../../{row.raw_rel.lstrip('../')}" if row.raw_rel else ""
        title = row.title or row.report_id
        if raw_link:
            title = f"[{title}]({raw_link})"
        lines.append(
            f"- `{row.report_id}` · {title} · {row.writer or '未知'} · "
            f"{row.created_at or '未知时间'} · `{row.source_lane or 'unknown'}`"
        )
    return "\n".join(lines) + "\n"


def rebuild_sources_index(wiki: Path, summaries: list[SummaryDoc]) -> tuple[list[str], int]:
    """Build the raw-source navigation layer from the current summary corpus.

    Source catalogs are generated here rather than by the old one-off bootstrap,
    so they stay aligned with incrementally added raw/summary pairs.
    """
    source_dir = wiki / "sources"
    ensure_dir(source_dir)
    changed: list[str] = []
    rows = sorted(summaries, key=lambda item: (item.created_at or "9999", item.report_id))
    expected_names: set[str] = set()
    for start in range(0, len(rows), SOURCE_CATALOG_SHARD_SIZE):
        shard_number = start // SOURCE_CATALOG_SHARD_SIZE + 1
        name = f"catalog-{shard_number:03d}.md"
        expected_names.add(name)
        path = source_dir / name
        if atomic_write(path, render_source_catalog(rows[start : start + SOURCE_CATALOG_SHARD_SIZE], shard_number)):
            changed.append(path.relative_to(wiki.parent).as_posix())
    for old_path in source_dir.glob("catalog-*.md"):
        if old_path.name not in expected_names:
            old_path.unlink()
            changed.append(old_path.relative_to(wiki.parent).as_posix())

    months = Counter(item.created_at[:7] for item in rows if re.match(r"^\d{4}-\d{2}", item.created_at or ""))
    lanes = Counter(item.source_lane or "unknown" for item in rows)
    dated_rows = [item.created_at for item in rows if re.match(r"^\d{4}-\d{2}-\d{2}", item.created_at or "")]
    catalog_count = (len(rows) + SOURCE_CATALOG_SHARD_SIZE - 1) // SOURCE_CATALOG_SHARD_SIZE
    lines = [
        "# 工作协同原文索引",
        "",
        "本页由当前 raw/summary 对自动生成，按汇报业务日期排序。原文是最终事实依据。",
        "",
        "## 覆盖范围",
        "",
        f"- 原文记录：**{len(rows)}**",
        f"- 日期范围：{min(dated_rows)[:10] if dated_rows else '未知'} 至 {max(dated_rows)[:10] if dated_rows else '未知'}",
        f"- 原文目录分片：**{catalog_count}**（每页最多 {SOURCE_CATALOG_SHARD_SIZE} 篇）",
        "",
        "## 按业务月份",
        "",
    ]
    lines.extend(f"- {month}：{count} 篇" for month, count in sorted(months.items()))
    lines.extend(["", "## 按来源通道", ""])
    lines.extend(f"- `{lane}`：{count} 篇" for lane, count in sorted(lanes.items()))
    lines.extend(["", "## 原文目录", ""])
    lines.extend(f"- [原文目录 {number:03d}](catalog-{number:03d}.md)" for number in range(1, catalog_count + 1))
    index_path = source_dir / "index.md"
    if atomic_write(index_path, "\n".join(lines) + "\n"):
        changed.append(index_path.relative_to(wiki.parent).as_posix())
    return changed, catalog_count


def rewrite_main_index(index_path: Path, topic_count: int, entity_count: int, source_count: int, source_catalog_count: int) -> bool:
    content = "\n".join(
        [
            "# 工作协同 LLM Wiki",
            "",
            "这是工作协同原文的受约束导航与综合层。原文事实以 [`../raw/`](../raw/) 为准。",
            "",
            "## Sources",
            "",
            f"- [sources/index.md](sources/index.md) · {source_count} raw records · {source_catalog_count} catalogs",
            "",
            "## Topics",
            "",
            f"- [topics/index.md](topics/index.md) · {topic_count} pages",
            "",
            "## Entities",
            "",
            f"- [entities/index.md](entities/index.md) · {entity_count} pages",
            "",
            "## System",
            "",
            "- [_system/manifest.json](_system/manifest.json)",
            "- [_system/status.md](_system/status.md)",
            "- [_system/query-contract.md](_system/query-contract.md)",
            "",
        ]
    )
    return atomic_write(index_path, content)


def rewrite_status(path: Path, manifest: dict[str, Any], topic_count: int, entity_count: int, rebuild: dict[str, Any]) -> bool:
    compiled = len(manifest.get("compiled_report_ids", []) or [])
    refined = len(manifest.get("ai_refined_report_ids", []) or [])
    fallback_ids = set(manifest.get("fallback_report_ids", []) or [])
    terminal_ids = {
        str(item.get("report_id"))
        for item in manifest.get("failure_queue", []) or []
        if item.get("report_id") and int(item.get("attempts", 1)) >= 3
    } & fallback_ids
    pending = len(fallback_ids - terminal_ids)
    mode = "query_ready" if (path.parent / "query-contract.md").exists() else ("topics_entities_ready" if topic_count or entity_count else "foundation_ready")
    content = "\n".join(
        [
            "# Cloud Wiki Status",
            "",
            f"- Schema: `{manifest.get('schema_version', 'cwk.cloud_wiki.v1')}`",
            f"- Raw truth sources: **{manifest.get('source_count', 0)}**",
            f"- Navigable source summaries: **{compiled}**",
            f"- AI-refined summaries: **{refined}**",
            f"- Fallback summaries pending refinement: **{pending}**",
            f"- Fallback summaries stopped after bounded failures: **{len(terminal_ids)}**",
            f"- Topic pages: **{topic_count}**",
            f"- Entity pages: **{entity_count}**",
            f"- Last aggregate rebuild attempted: `{rebuild['attempted_at']}`",
            f"- Last aggregate rebuild content changes: **{rebuild['content_change_count']}** pages"
            + (" (no aggregate content changed)" if not rebuild["content_change_count"] else ""),
            f"- Last aggregate rebuild summary: topics {rebuild['topic_page_changes']} · entities {rebuild['entity_page_changes']} · sources {rebuild['source_page_changes']} · indexes {rebuild['index_page_changes']}",
            f"- Mode: `{mode}`",
            "- Persistence: all generated files must be synchronised to the personal DocDB mirror.",
            "",
        ]
    )
    return atomic_write(path, content)


def append_log(log_path: Path, summary: dict[str, Any]) -> bool:
    stamp = summary["generated_at"]
    block = "\n".join(
        [
            "",
            f"## [{stamp}] compile | topics entities",
            "",
            f"- summaries_scanned: {summary['summaries_scanned']} · topics_written: {summary['topics_written']} · entities_written: {summary['entities_written']}",
            f"- topic_candidates: {summary['topic_candidates']} · entity_candidates: {summary['entity_candidates']}",
            "",
        ]
    )
    old = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    if old.endswith(block):
        return False
    ensure_dir(log_path.parent)
    log_path.write_text(old + block, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile CWK wiki topic/entity pages from source summaries.")
    parser.add_argument("--mirror-root", default=str(DEFAULT_MIRROR))
    parser.add_argument("--topic-limit", type=int, default=None)
    parser.add_argument("--entity-limit", type=int, default=None)
    parser.add_argument("--min-topic-reports", type=int, default=2)
    parser.add_argument("--min-entity-reports", type=int, default=2)
    parser.add_argument("--manifest-out", default="")
    args = parser.parse_args()

    mirror = Path(args.mirror_root).expanduser().resolve()
    wiki = mirror / "wiki"
    summaries_dir = wiki / "summaries"
    manifest_path = wiki / "_system" / "manifest.json"
    if not summaries_dir.exists():
        raise SystemExit(f"summaries directory not found: {summaries_dir}")
    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}")

    summaries = [load_summary(path) for path in sorted(summaries_dir.glob("*.md"))]
    topic_entries, entity_entries = collect_aggregates(
        summaries=summaries,
        min_topic_reports=max(1, args.min_topic_reports),
        min_entity_reports=max(1, args.min_entity_reports),
        topic_limit=args.topic_limit,
        entity_limit=args.entity_limit,
    )

    changed: list[str] = []
    for directory in [wiki / "topics", wiki / "entities"]:
        ensure_dir(directory)

    query_contract = wiki / "_system" / "query-contract.md"
    if atomic_write(query_contract, QUERY_CONTRACT):
        changed.append(query_contract.relative_to(mirror).as_posix())

    for entry in topic_entries + entity_entries:
        output = wiki / entry.page_rel
        if atomic_write(output, render_aggregate(entry)):
            changed.append(output.relative_to(mirror).as_posix())

    source_changed, source_catalog_count = rebuild_sources_index(wiki, summaries)
    changed.extend(source_changed)

    topic_index = wiki / "topics" / "index.md"
    if atomic_write(topic_index, render_dir_index("工作协同主题索引", "topics", topic_entries)):
        changed.append(topic_index.relative_to(mirror).as_posix())

    entity_index = wiki / "entities" / "index.md"
    if atomic_write(entity_index, render_dir_index("工作协同实体索引", "entities", entity_entries)):
        changed.append(entity_index.relative_to(mirror).as_posix())

    main_index = wiki / "index.md"
    actual_topic_count = sum(1 for path in (wiki / "topics").glob("*.md") if path.name != "index.md")
    actual_entity_count = sum(1 for path in (wiki / "entities").rglob("*.md") if path.name != "index.md")

    if rewrite_main_index(main_index, actual_topic_count, actual_entity_count, len(summaries), source_catalog_count):
        changed.append(main_index.relative_to(mirror).as_posix())

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    summary = {
        "schema_version": SCHEMA,
        "generated_at": generated_at,
        "mirror_root": str(mirror),
        "summaries_scanned": len(summaries),
        "topic_candidates": len(topic_entries),
        "entity_candidates": len(entity_entries),
        "topics_written": len(topic_entries),
        "entities_written": len(entity_entries),
        "topic_limit": args.topic_limit,
        "entity_limit": args.entity_limit,
        "min_topic_reports": args.min_topic_reports,
        "min_entity_reports": args.min_entity_reports,
        "topic_pages": [entry.page_rel for entry in topic_entries],
        "entity_pages": [entry.page_rel for entry in entity_entries],
        "changed_relative_paths": sorted(set(changed)),
    }
    aggregate_content_changes = [
        path for path in summary["changed_relative_paths"]
        if path.startswith(("wiki/topics/", "wiki/entities/", "wiki/sources/", "wiki/index.md"))
    ]
    rebuild = {
        "attempted_at": generated_at,
        "content_change_count": len(aggregate_content_changes),
        "topic_page_changes": sum(path.startswith("wiki/topics/") for path in aggregate_content_changes),
        "entity_page_changes": sum(path.startswith("wiki/entities/") for path in aggregate_content_changes),
        "source_page_changes": sum(path.startswith("wiki/sources/") for path in aggregate_content_changes),
        "index_page_changes": sum(path == "wiki/index.md" for path in aggregate_content_changes),
    }
    summary["rebuild"] = rebuild
    manifest["last_topic_entity_compile_at"] = generated_at
    manifest["topic_entity_compile"] = summary
    manifest["last_aggregate_rebuild"] = rebuild
    manifest["topic_page_count"] = actual_topic_count
    manifest["entity_page_count"] = actual_entity_count
    manifest["changed_relative_paths"] = summary["changed_relative_paths"]
    if atomic_write_json(manifest_path, manifest):
        changed.append(manifest_path.relative_to(mirror).as_posix())
        summary["changed_relative_paths"] = sorted(set(changed))
        manifest["changed_relative_paths"] = summary["changed_relative_paths"]
        atomic_write_json(manifest_path, manifest)

    status_path = wiki / "_system" / "status.md"
    if rewrite_status(status_path, manifest, actual_topic_count, actual_entity_count, rebuild):
        changed.append(status_path.relative_to(mirror).as_posix())

    log_path = wiki / "log.md"
    if append_log(log_path, summary):
        changed.append(log_path.relative_to(mirror).as_posix())

    summary["changed_relative_paths"] = sorted(set(changed))
    manifest["changed_relative_paths"] = summary["changed_relative_paths"]
    manifest["topic_entity_compile"] = summary
    atomic_write_json(manifest_path, manifest)

    if args.manifest_out:
        atomic_write_json(Path(args.manifest_out).expanduser().resolve(), summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
