#!/usr/bin/env python3
"""Select high-value fallback summaries for bounded AI refinement.

The selector is deliberately deterministic and auditable: it favours project,
decision and discussion-bearing reports, while excluding routine personal
administrative records.  It never mutates the mirror.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


POSITIVE = {
    "TBS": 14, "云端虾": 14, "小龙虾": 12, "OpenClaw": 12,
    "AI平台": 12, "Agent": 10, "智能体": 10, "知识库": 10,
    "BP": 9, "SFE": 9, "项目": 7, "会议纪要": 8, "专项": 6,
    "规划": 6, "方案": 6, "评审": 6, "上线": 6, "复盘": 5,
    "风险": 6, "预算": 5, "战略": 6, "医学": 5,
}
NEGATIVE = (
    "考勤", "请假", "补卡", "报销", "工资", "社保", "花名册",
    "异常确认", "入职", "离职", "转正", "付款申请", "用印申请",
)
DECISION_RE = re.compile(r"决策人[:：]|意见[:：]|待办|下一步|风险|审批|回复")
REPLY_RE = re.compile(r'"replyCount"\s*:\s*(\d+)')
DATE_RE = re.compile(r"^(?:business_date|create_time):\s*[\"']?([^\n\"']+)", re.M)
TITLE_RE = re.compile(r"^title:\s*[\"']?(.*?)[\"']?\s*$", re.M)
RID_RE = re.compile(r"^report_id:\s*[\"']?(\d+)[\"']?\s*$", re.M)


def metadata(path: Path) -> tuple[str, str, str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rid = (RID_RE.search(text).group(1) if RID_RE.search(text) else path.stem.split("-", 1)[0])
    title = (TITLE_RE.search(text).group(1) if TITLE_RE.search(text) else path.stem)
    date = (DATE_RE.search(text).group(1) if DATE_RE.search(text) else "")
    return rid, title, date, text


def score_row(title: str, text: str) -> tuple[int, list[str]]:
    score, reasons = 0, []
    searchable = f"{title}\n{text[:24000]}"
    for phrase, points in POSITIVE.items():
        if phrase.lower() in searchable.lower():
            score += points
            reasons.append(phrase)
    if DECISION_RE.search(searchable):
        score += 5
        reasons.append("decision_or_followup")
    reply_match = REPLY_RE.search(text)
    replies = int(reply_match.group(1)) if reply_match else 0
    if replies:
        score += min(replies, 10) * 2
        reasons.append(f"replies={replies}")
    if len(text) >= 6000:
        score += 3
        reasons.append("substantive")
    if any(term in title for term in NEGATIVE):
        score -= 30
        reasons.append("routine_admin")
    return score, reasons


def title_family(title: str) -> str:
    """Collapse draft/final variants of one report into one initial candidate."""
    clean = re.sub(r"[【\[].*?[】\]]", "", title)
    clean = re.sub(r"(?:初稿|终稿|定稿|v\d+)", "", clean, flags=re.I)
    return re.sub(r"\s+", "", clean).lower()


def variant_preference(title: str) -> int:
    if "终稿" in title or "定稿" in title:
        return 3
    if "初稿" in title or "草稿" in title:
        return -3
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror-root", required=True)
    parser.add_argument("--limit", type=int, default=512)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    mirror = Path(args.mirror_root).expanduser().resolve()
    manifest = json.loads((mirror / "wiki/_system/manifest.json").read_text(encoding="utf-8"))
    fallback_ids = set(manifest.get("fallback_report_ids", []))
    rows = []
    for raw in (mirror / "raw").rglob("*.md"):
        if "_system" in raw.relative_to(mirror).parts:
            continue
        rid, title, date, text = metadata(raw)
        if rid not in fallback_ids:
            continue
        score, reasons = score_row(title, text)
        if score > 0:
            rows.append({"report_id": rid, "title": title, "business_date": date,
                         "score": score, "reasons": reasons,
                         "raw_path": raw.relative_to(mirror).as_posix()})
    # A sequence of draft/final variants is useful as raw evidence but does
    # not deserve several slots in the first-pass AI refinement queue.
    best_by_family = {}
    for row in rows:
        family = title_family(row["title"])
        key = (row["score"] + variant_preference(row["title"]), row["business_date"], row["report_id"])
        if family not in best_by_family or key > best_by_family[family][0]:
            best_by_family[family] = (key, row)
    rows = [entry[1] for entry in best_by_family.values()]
    rows.sort(key=lambda item: (-item["score"], item["business_date"], item["report_id"]))
    selected = rows[:args.limit]
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "selector": "cwk_select_priority_batch.py",
        "selection_rule": "project/decision/reply priority; routine personal admin excluded",
        "fallback_available": len(fallback_ids), "eligible": len(rows),
        "selected": len(selected), "items": selected,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected": len(selected), "eligible": len(rows), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
