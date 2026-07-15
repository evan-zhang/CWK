#!/usr/bin/env python3
"""Evaluate deterministic suspected-relation candidates on an adjudicated pair set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def pair_key(source_ids: list[str]) -> tuple[str, str]:
    values = sorted(map(str, source_ids))
    if len(values) != 2 or values[0] == values[1]:
        raise ValueError(f"invalid relation pair: {source_ids}")
    return values[0], values[1]


def predicted_pairs(run_dir: Path) -> set[tuple[str, str]]:
    predicted = set()
    for path in sorted((run_dir / "relations").glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            if item.get("decision") == "mark_suspected":
                predicted.add(pair_key(item.get("source_ids", [])))
    return predicted


def evaluate(run_dir: Path, gold_path: Path) -> dict:
    gold_payload = json.loads(gold_path.read_text(encoding="utf-8"))
    gold = {pair_key(item["source_ids"]): bool(item["related"]) for item in gold_payload["pairs"]}
    if len(gold) != 30:
        raise ValueError(f"gold set must contain 30 unique pairs, found {len(gold)}")
    predicted = predicted_pairs(run_dir)
    tp = sum(gold[pair] and pair in predicted for pair in gold)
    fp = sum(not gold[pair] and pair in predicted for pair in gold)
    fn = sum(gold[pair] and pair not in predicted for pair in gold)
    tn = sum(not gold[pair] and pair not in predicted for pair in gold)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / len(gold)
    return {
        "schema_version": "cwk.relation_eval.v1",
        "run_name": run_dir.name,
        "gold_pair_count": len(gold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(accuracy, 4),
        "missed_positive_pairs": [list(pair) for pair, related in gold.items() if related and pair not in predicted],
        "false_positive_pairs": [list(pair) for pair, related in gold.items() if not related and pair in predicted],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--gold", default=str(PROJECT / "references" / "relation-gold-v1.json"))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = evaluate(PROJECT / "runs" / args.run_name, Path(args.gold))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
