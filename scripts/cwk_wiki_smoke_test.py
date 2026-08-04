#!/usr/bin/env python3
"""Smoke-test CWK cloud wiki compile outputs for daily usability checks."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR = PROJECT / "knowledge" / "工作协同镜像"


def count_md(path: Path, recursive: bool = False) -> int:
    if not path.exists():
        return 0
    files = path.rglob("*.md") if recursive else path.glob("*.md")
    return sum(1 for p in files if p.name != "index.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test CWK wiki layer.")
    parser.add_argument("--mirror-root", default=str(DEFAULT_MIRROR))
    parser.add_argument("--min-summaries", type=int, default=500)
    parser.add_argument("--min-topics", type=int, default=20)
    parser.add_argument("--min-entities", type=int, default=50)
    args = parser.parse_args()

    mirror = Path(args.mirror_root).expanduser().resolve()
    wiki = mirror / "wiki"
    checks: list[tuple[str, bool, str]] = []

    summaries = count_md(wiki / "summaries")
    topics = count_md(wiki / "topics")
    entities = count_md(wiki / "entities", recursive=True)
    checks.append(("summaries_count", summaries >= args.min_summaries, f"{summaries} >= {args.min_summaries}"))
    checks.append(("topics_count", topics >= args.min_topics, f"{topics} >= {args.min_topics}"))
    checks.append(("entities_count", entities >= args.min_entities, f"{entities} >= {args.min_entities}"))

    manifest_path = wiki / "_system" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        compiled = manifest.get("compiled_report_ids") or []
        refined = manifest.get("ai_refined_report_ids") or []
        fallback = manifest.get("fallback_report_ids") or []
        failures = manifest.get("failure_queue") or []
        failure_ids = {str(item.get("report_id")) for item in failures if item.get("report_id")}
        withheld = set(manifest.get("withheld_report_ids") or [])
        checks.append(("manifest_compiled", len(compiled) >= args.min_summaries, f"{len(compiled)} compiled"))
        checks.append(("manifest_quality_partition", len(set(refined) | set(fallback)) == len(set(compiled)), f"refined={len(refined)} fallback={len(fallback)} compiled={len(compiled)}"))
        checks.append(("manifest_quality_disjoint", not (set(refined) & set(fallback)), "refined/fallback sets are disjoint"))
        checks.append(("manifest_failures_preserve_fallback", failure_ids <= set(fallback), f"failures={len(failures)} all remain queryable fallback pages"))
        checks.append(("manifest_failure_attempts_bounded", all(1 <= int(item.get("attempts", 1)) <= 3 for item in failures), "failure attempts are bounded at 3"))
        checks.append(("manifest_withheld_is_fallback", withheld <= set(fallback), f"withheld={len(withheld)}"))
    else:
        checks.append(("manifest_exists", False, "missing wiki/_system/manifest.json"))

    # Sample pages must exist and cite report ids.
    samples = [
        wiki / "topics" / "云端虾申请.md",
        wiki / "entities" / "products" / "云端虾.md",
        wiki / "entities" / "people" / "李文俏.md",
    ]
    for sample in samples:
        if not sample.exists():
            checks.append((f"sample:{sample.name}", False, "missing"))
            continue
        text = sample.read_text(encoding="utf-8", errors="replace")
        has_id = bool(re.search(r"\d{15,}", text))
        has_sk = bool(re.search(r"sk-[A-Za-z0-9_-]{8,}", text))
        checks.append((f"sample:{sample.name}:report_id", has_id, "has report_id refs" if has_id else "no report_id"))
        checks.append((f"sample:{sample.name}:no_secret", not has_sk, "clean" if not has_sk else "secret-like token found"))

    # One summary frontmatter check.
    summary_files = sorted((wiki / "summaries").glob("*.md"))
    if summary_files:
        s = summary_files[0].read_text(encoding="utf-8", errors="replace")
        checks.append(("summary_frontmatter_type", "type: SourceSummary" in s, summary_files[0].name))
        checks.append(("summary_frontmatter_report_id", "report_id:" in s, summary_files[0].name))
    else:
        checks.append(("summary_exists", False, "no summaries"))

    query_contract = wiki / "_system" / "query-contract.md"
    checks.append(("query_contract_exists", query_contract.exists(), str(query_contract)))

    print(f"mirror={mirror}")
    print(f"counts summaries={summaries} topics={topics} entities={entities}")
    failed = 0
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{status} {name} :: {detail}")
    print(f"overall={'PASS' if failed == 0 else 'FAIL'} failed={failed}/{len(checks)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
