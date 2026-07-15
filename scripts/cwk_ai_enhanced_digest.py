#!/usr/bin/env python3
"""Render a traceable management digest from CWK AI event artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from cwk_ai_common import PROJECT, load_json


def refs(record_ids: list[str]) -> str:
    return "、".join(f"`{report_id}`" for report_id in record_ids)


def render(run_dir: Path) -> str:
    events_payload = load_json(run_dir / "ai-events.json")
    priorities_payload = load_json(run_dir / "ai-daily-priorities.json")
    event_by_id = {item["event_id"]: item for item in events_payload.get("events", [])}
    priorities = priorities_payload.get("priorities", [])
    record_summary_path = run_dir / "ai-record-summary.json"
    record_summary = load_json(record_summary_path) if record_summary_path.exists() else {}
    source_count = record_summary.get("processed_count", 0)
    completed_count = record_summary.get("completed_count", 0)
    skipped_sensitive = record_summary.get("skipped_sensitive_count", 0)
    evidence_ids = {str(report_id) for item in events_payload.get("events", []) for report_id in item.get("record_ids", [])}
    lines = [
        "# 工作协同每日简报（AI 增强版）",
        "",
        "## 管理结论",
        "",
        f"本轮共收到 {source_count or len(evidence_ids)} 条来源记录；其中 {completed_count or len(evidence_ids)} 条进入 AI 理解，{skipped_sensitive} 条因敏感信息在模型调用前安全隔离。基于 {len(evidence_ids)} 条可用证据归并出 {len(events_payload.get('events', []))} 个事项，筛出 {len(priorities)} 个优先关注项。所有判断均保留原始 report_id，可回到规则版和 raw 证据核验。",
        "",
        "## 今天优先看",
        "",
    ]
    for item in priorities:
        lines.extend(
            [
                f"- [{item.get('priority', 'P2')}] {item.get('title', '未命名事项')}（{item.get('status', 'unknown')}）",
                f"  摘要：{item.get('summary') or '未形成可靠摘要。'}",
                f"  价值：{item.get('why_it_matters') or '需结合原文判断。'}",
                f"  证据：{refs(item.get('record_ids', []))}",
            ]
        )

    lines += ["", "## 决策与行动", ""]
    action_count = 0
    for item in priorities:
        event = event_by_id.get(item.get("event_id"), {})
        for action in event.get("action_items", [])[:3]:
            task = action.get("task") if isinstance(action, dict) else str(action)
            evidence = action.get("evidence", "") if isinstance(action, dict) else ""
            lines.append(f"- {task}（事项：{event.get('event_title')}；证据：{refs(event.get('record_ids', []))}；原文：{evidence or '见对应 report_id'}）")
            action_count += 1
    if not action_count:
        lines.append("- 本轮没有提取到可验证的明确行动项。")

    lines += ["", "## 风险与阻塞", ""]
    risk_count = 0
    for item in priorities:
        event = event_by_id.get(item.get("event_id"), {})
        for risk in event.get("risks", [])[:2]:
            text = risk.get("risk") if isinstance(risk, dict) else str(risk)
            severity = risk.get("severity", "unknown") if isinstance(risk, dict) else "unknown"
            evidence = risk.get("evidence", "") if isinstance(risk, dict) else ""
            lines.append(f"- [{severity}] {text}（证据：{refs(event.get('record_ids', []))}；原文：{evidence or '见对应 report_id'}）")
            risk_count += 1
    if not risk_count:
        lines.append("- 本轮没有提取到可验证的明确风险。")

    continuing = [event for event in events_payload.get("events", []) if event.get("status") == "continuing"]
    lines += ["", "## 延续事项", ""]
    if continuing:
        for event in continuing[:10]:
            lines.append(f"- {event.get('event_title')}：{event.get('merged_summary')}（证据：{refs(event.get('record_ids', []))}）")
    else:
        lines.append("- 本轮未匹配到已有 AI 历史事件；不据此断言所有事项均为首次出现。")

    lines += [
        "",
        "## 质量与安全边界",
        "",
        "- 本版是 AI 增强阅读件，规则版仍是稳定 baseline。",
        "- AI 不操作 CWork；不标已读、不回复、不审批、不完成待办。",
        "- 任何无法由 report_id 或原文片段支持的结论都不应进入正式沉淀。",
        f"- 敏感源安全隔离：{skipped_sensitive} 条；隔离记录不进入模型或 AI 聚类，仍保留在规则 baseline 中。",
        "",
        "## 证据入口",
        "",
        f"- 运行目录：`{run_dir.relative_to(PROJECT)}`",
        "- 单篇理解：`ai-understanding/`；事件归并：`ai-events.json`；规则对照：`digest-human-v4.md`。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render CWK AI enhanced digest.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    run_dir = PROJECT / "runs" / args.run_name
    output = Path(args.output).resolve() if args.output else run_dir / "digest-ai-enhanced.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(run_dir), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
