#!/usr/bin/env python3
"""Review the CWK AI digest against the deterministic baseline."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from cwk_ai_common import QUALITY_SCHEMA, PROJECT, env_bool, invoke_openclaw_json, load_json, write_json


NOISE_TOKENS = ('"fileName"', '"nodeName"', '"content"', "暂无可读摘要", "null", "textContent")


def score_text(text: str, has_refs: bool) -> int:
    score = 70
    score -= min(20, sum(text.count(token) for token in NOISE_TOKENS) * 4)
    score += 10 if has_refs else -15
    score += 5 if "## 风险与阻塞" in text else 0
    score += 5 if "## 决策与行动" in text else 0
    return max(0, min(100, score))


def dry_run_review(rules: str, enhanced: str, events: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    found_ids = set(re.findall(r"`([^`]+)`", enhanced)) & valid_ids
    coverage = len(found_ids) / len(valid_ids) if valid_ids else 1.0
    rules_score = score_text(rules, False)
    ai_score = score_text(enhanced, bool(found_ids))
    improvements = []
    if sum(enhanced.count(token) for token in NOISE_TOKENS) < sum(rules.count(token) for token in NOISE_TOKENS):
        improvements.append("machine_noise_reduction")
    if found_ids:
        improvements.append("report_id_traceability")
    if len(events.get("events", [])) < len(valid_ids):
        improvements.append("duplicate_reduction")
    regressions = [] if coverage >= 0.8 else ["incomplete_evidence_coverage"]
    return {
        "schema_version": QUALITY_SCHEMA,
        "review_status": "dry_run",
        "quality_score": ai_score,
        "rules_score": rules_score,
        "evidence_coverage": round(coverage, 3),
        "improvements": improvements,
        "regressions": regressions,
        "issues": [],
        "recommendations": ["Continue side-by-side pilot; do not replace the rules baseline yet."],
        "release_recommendation": "pilot" if not regressions else "hold",
    }


def prompt_for(rules: str, enhanced: str, events: dict[str, Any], valid_ids: set[str]) -> str:
    return f"""# CWK AI quality review

Compare the deterministic rules digest and the AI-enhanced digest. Return exactly
one JSON object using this complete shape and these exact English key names:

{{
  "schema_version": "{QUALITY_SCHEMA}",
  "review_status": "completed",
  "quality_score": 0,
  "rules_score": 0,
  "evidence_coverage": 0.0,
  "improvements": [],
  "regressions": [],
  "issues": [{{"severity": "low|medium|high", "issue": "string", "evidence_report_ids": []}}],
  "recommendations": [],
  "release_recommendation": "pilot|hold|reject"
}}

Replace the example values with your assessment. Scores must be integers 0-100
and evidence_coverage must be a number from 0 to 1.

Judge duplicate reduction, readable summaries, event anchors, action clarity,
hallucination risk, over-merge, missed evidence, time consistency, and machine noise.
Only use report IDs from this allowlist: {sorted(valid_ids)}. Do not invent facts.
The AI version remains a pilot even when it scores higher. Return JSON only.

## Rules digest
{rules}

## AI-enhanced digest
{enhanced}

## AI events
{json.dumps(events, ensure_ascii=False)}
"""


def repair_prompt(rules: str, enhanced: str, events: dict[str, Any], valid_ids: set[str], errors: list[str]) -> str:
    return prompt_for(rules, enhanced, events, valid_ids) + f"""

## Contract correction

Your previous JSON failed validation: {json.dumps(errors, ensure_ascii=False)}
Return the complete JSON object again with the exact schema_version and exact
English keys shown above. Return JSON only.
"""


def validate_review(payload: dict[str, Any], valid_ids: set[str]) -> list[str]:
    errors = []
    if payload.get("schema_version") != QUALITY_SCHEMA:
        errors.append("invalid schema_version")
    for key in ("quality_score", "rules_score"):
        value = payload.get(key)
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            errors.append(f"invalid {key}")
    coverage = payload.get("evidence_coverage")
    if not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1:
        errors.append("invalid evidence_coverage")
    for issue in payload.get("issues", []):
        ids = {str(item) for item in issue.get("evidence_report_ids", [])}
        if not ids.issubset(valid_ids):
            errors.append("issue contains unknown report_id")
    if payload.get("release_recommendation") not in {"pilot", "hold", "reject"}:
        errors.append("invalid release_recommendation")
    return errors


def render_markdown(payload: dict[str, Any], run_name: str) -> str:
    lines = [
        "# CWK AI 质量复核",
        "",
        f"- run_name: `{run_name}`",
        f"- AI 增强版评分: **{payload.get('quality_score')} / 100**",
        f"- 规则版评分: **{payload.get('rules_score')} / 100**",
        f"- 证据覆盖率: **{payload.get('evidence_coverage')}**",
        f"- 发布建议: **{payload.get('release_recommendation')}**",
        "",
        "## 改进",
        "",
    ]
    lines.extend(f"- {item}" for item in payload.get("improvements", []))
    if not payload.get("improvements"):
        lines.append("- 未识别到明确改进。")
    lines += ["", "## 退化与问题", ""]
    lines.extend(f"- {item}" for item in payload.get("regressions", []))
    for issue in payload.get("issues", []):
        refs = "、".join(f"`{item}`" for item in issue.get("evidence_report_ids", [])) or "无"
        lines.append(f"- [{issue.get('severity', 'unknown')}] {issue.get('issue')}（证据：{refs}）")
    if not payload.get("regressions") and not payload.get("issues"):
        lines.append("- 未识别到明确退化。")
    lines += ["", "## 建议", ""]
    lines.extend(f"- {item}" for item in payload.get("recommendations", []))
    lines += ["", "本报告仅用于并行 pilot 评估，不构成替换规则 baseline 的自动授权。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Review CWK AI enhanced digest quality.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--model", default=os.environ.get("CWK_AI_QUALITY_MODEL", ""))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CWK_AI_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("CWK_AI_DRY_RUN"))
    args = parser.parse_args()

    run_dir = PROJECT / "runs" / args.run_name
    rules = (run_dir / "digest-human-v4.md").read_text(encoding="utf-8")
    enhanced = (run_dir / "digest-ai-enhanced.md").read_text(encoding="utf-8")
    events = load_json(run_dir / "ai-events.json")
    valid_ids = {str(item) for event in events.get("events", []) for item in event.get("record_ids", [])}
    started = time.monotonic()
    if args.dry_run:
        payload = dry_run_review(rules, enhanced, events, valid_ids)
    else:
        payload = invoke_openclaw_json(
            prompt_for(rules, enhanced, events, valid_ids),
            model=args.model,
            stage="quality-review",
            timeout_seconds=args.timeout_seconds,
            prompt_dir=run_dir / ".ai-prompts",
        )
        payload["review_status"] = "completed"
    errors = validate_review(payload, valid_ids)
    if errors and not args.dry_run:
        payload = invoke_openclaw_json(
            repair_prompt(rules, enhanced, events, valid_ids, errors),
            model=args.model,
            stage="quality-review-repair",
            timeout_seconds=args.timeout_seconds,
            prompt_dir=run_dir / ".ai-prompts",
        )
        payload["review_status"] = "completed"
        errors = validate_review(payload, valid_ids)
    if errors:
        raise SystemExit("invalid quality review: " + "; ".join(errors))
    payload["model"] = "dry-run" if args.dry_run else args.model
    payload["duration_seconds"] = round(time.monotonic() - started, 3)
    write_json(run_dir / "quality-review.json", payload)
    (run_dir / "quality-review.md").write_text(render_markdown(payload, args.run_name), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("quality_score", "rules_score", "evidence_coverage", "release_recommendation")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
