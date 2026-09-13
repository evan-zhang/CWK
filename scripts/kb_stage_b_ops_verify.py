#!/usr/bin/env python3
"""Independently verify confidential OPS known-item candidates.

The verifier is a separate module/process with no generator import. It reopens
the private corpus snapshot, independently recomputes the expected-document
order, and validates every category from source text. Only verified rows may be
consumed by the scorer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

SAMPLING_VERSION = "ops-known-item-stratified-v2"
SPLIT_VERSION = "ops-category-ordinal-calibration-holdout-v1"
CATEGORIES = (
    "title_filename", "exact_identifier_date", "body_only_rare_phrase",
    "table_row", "no_answer_mutation", "near_neighbour",
)
TARGETS = {
    "title_filename": 10, "exact_identifier_date": 8,
    "body_only_rare_phrase": 10, "table_row": 4,
    "no_answer_mutation": 5, "near_neighbour": 5,
}
IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![A-Za-z0-9])"
)
DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{4}年\d{1,2}月\d{1,2}日")
TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9][A-Za-z0-9_.-]*")
ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{3,}")
REJECTION_REASONS = (
    "schema_invalid", "sampling_version_mismatch", "unknown_library",
    "unknown_category", "expected_lock_mismatch", "expected_missing",
    "query_empty", "query_not_in_expected", "title_filename_constraint",
    "unique_constraint", "body_only_constraint", "table_structure_constraint",
    "no_answer_present", "near_neighbour_constraint", "duplicate_case",
    "category_ordinal_mismatch", "split_mismatch",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def independent_split(category: str, ordinal: int) -> str:
    if category not in CATEGORIES or ordinal < 0:
        raise ValueError("invalid_category_ordinal")
    return "calibration" if ordinal < round(TARGETS[category] / 3) else "holdout"


def _stable_int(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x1f".join(parts).encode()).digest()[:8], "big")


def _stratum(doc: dict[str, Any]) -> str:
    body = str(doc.get("body") or "")
    if any(line.count("|") >= 2 for line in body.splitlines()):
        return "table"
    size = len(TOKEN_RE.findall(body))
    return "long" if size >= 2000 else "medium" if size >= 500 else "short"


def independent_locked_order(kb_id: str, docs: Sequence[dict[str, Any]], seed: str) -> list[str]:
    strata: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        strata.setdefault(_stratum(doc), []).append(doc)
    for name, rows in strata.items():
        rows.sort(key=lambda row: (
            _stable_int(SAMPLING_VERSION, seed, kb_id, name, str(row["doc_id"])),
            str(row["doc_id"]),
        ))
    active = sorted(strata)
    positions = {name: 0 for name in active}
    ordered: list[str] = []
    while active:
        next_active: list[str] = []
        for name in active:
            position = positions[name]
            if position < len(strata[name]):
                ordered.append(str(strata[name][position]["doc_id"]))
                positions[name] += 1
            if positions[name] < len(strata[name]):
                next_active.append(name)
        active = next_active
    return ordered


def independent_relation_tokens(doc: dict[str, Any]) -> set[str]:
    text = normalize(str(doc.get("title") or "") + "\n" + str(doc.get("body") or ""))
    cjk = re.findall(r"[\u3400-\u9fff]{2,6}", text)
    ascii_words = [x.casefold() for x in ASCII_WORD_RE.findall(text)]
    return {x for x in (*cjk, *ascii_words) if len(x) >= 2}


def combined(doc: dict[str, Any]) -> str:
    return normalize("\n".join((
        str(doc.get("title") or ""), str(doc.get("filename") or ""), str(doc.get("body") or "")
    )))


def table_line_contains(doc: dict[str, Any], query: str) -> bool:
    q = normalize(query)
    for line in str(doc.get("body") or "").splitlines():
        if line.count("|") >= 2 and not re.fullmatch(r"[\s|:=-]+", line) and q in normalize(line):
            return True
    return False


def verify_library(
    kb_id: str, docs: Sequence[dict[str, Any]], generated: dict[str, Any], seed: str
) -> dict[str, Any]:
    by_id = {str(doc["doc_id"]): doc for doc in docs}
    locked = independent_locked_order(kb_id, docs, seed)
    corpus_text = {doc_id: combined(doc) for doc_id, doc in by_id.items()}
    relation = {doc_id: independent_relation_tokens(doc) for doc_id, doc in by_id.items()}
    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    category_ordinals: Counter[str] = Counter()

    def reject(category: str, reason: str) -> None:
        rejected.append({"category": category if category in CATEGORIES else "unknown", "reason": reason})

    for row in generated.get("cases", []) if isinstance(generated, dict) else []:
        if not isinstance(row, dict):
            reject("unknown", "schema_invalid"); continue
        category = str(row.get("category") or "")
        if category not in CATEGORIES:
            reject(category, "unknown_category"); continue
        if row.get("sampling_version") != SAMPLING_VERSION or row.get("seed") != seed:
            reject(category, "sampling_version_mismatch"); continue
        category_ordinal = category_ordinals[category]
        if row.get("category_ordinal") != category_ordinal:
            reject(category, "category_ordinal_mismatch"); continue
        split = independent_split(category, category_ordinal)
        if row.get("split") != split:
            reject(category, "split_mismatch"); continue
        category_ordinals[category] += 1
        query = str(row.get("query") or "").strip()
        expected = str(row.get("expected_doc_id") or "")
        try:
            ordinal = int(row.get("expected_lock_ordinal"))
        except (TypeError, ValueError):
            reject(category, "expected_lock_mismatch"); continue
        if ordinal < 0 or ordinal >= len(locked) or locked[ordinal] != expected:
            reject(category, "expected_lock_mismatch"); continue
        doc = by_id.get(expected)
        if doc is None:
            reject(category, "expected_missing"); continue
        if not query:
            reject(category, "query_empty"); continue
        key = (category, normalize(query), expected)
        if key in seen:
            reject(category, "duplicate_case"); continue
        seen.add(key)
        q = normalize(query)
        owners = [doc_id for doc_id, text in corpus_text.items() if q in text]
        expected_body = normalize(str(doc.get("body") or ""))
        expected_title_file = normalize(str(doc.get("title") or "") + "\n" + str(doc.get("filename") or ""))

        if category != "no_answer_mutation" and expected not in owners:
            reject(category, "query_not_in_expected"); continue
        if category == "title_filename":
            title = normalize(str(doc.get("title") or ""))
            filename = normalize(str(doc.get("filename") or ""))
            stem = filename.rsplit(".", 1)[0]
            if q not in {title, filename, stem} or owners != [expected]:
                reject(category, "title_filename_constraint"); continue
        elif category == "exact_identifier_date":
            if not (IDENTIFIER_RE.fullmatch(query) or DATE_RE.fullmatch(query)) or owners != [expected]:
                reject(category, "unique_constraint"); continue
        elif category == "body_only_rare_phrase":
            if q not in expected_body or q in expected_title_file or owners != [expected]:
                reject(category, "body_only_constraint"); continue
        elif category == "table_row":
            if not table_line_contains(doc, query):
                reject(category, "table_structure_constraint"); continue
        elif category == "no_answer_mutation":
            if owners:
                reject(category, "no_answer_present"); continue
        elif category == "near_neighbour":
            neighbour = str(row.get("neighbour_doc_id") or "")
            other = by_id.get(neighbour)
            overlap = relation.get(expected, set()) & relation.get(neighbour, set())
            if (other is None or neighbour == expected or len(overlap) < 2
                    or q in corpus_text.get(neighbour, "") or owners != [expected]):
                reject(category, "near_neighbour_constraint"); continue

        verified.append({
            "ordinal": len(verified), "kb_id": kb_id, "category": category,
            "category_ordinal": category_ordinal, "split": split,
            "query": query, "expected_doc_id": expected,
            "expected_outcome": "no_evidence" if category == "no_answer_mutation" else "hit",
        })

    counts = Counter(row["category"] for row in verified)
    rejected_counts = Counter(row["reason"] for row in rejected)
    generated_availability = generated.get("category_availability", {}) if isinstance(generated, dict) else {}
    availability: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        count = counts[category]
        if count >= TARGETS[category]:
            availability[category] = {"status": "AVAILABLE", "count": count}
        else:
            availability[category] = {
                "status": "UNKNOWN", "count": count,
                "reason": "source_category_unsupported" if generated_availability.get(category) == "UNKNOWN" else "source_category_insufficient",
            }
    return {
        "kb_id": kb_id, "split_version": SPLIT_VERSION,
        "cases": verified, "case_total": len(verified),
        "category_counts": dict(counts), "category_availability": availability,
        "rejections": {"total": len(rejected), "reason_counts": dict(rejected_counts), "rows": rejected},
    }


def verify(corpus: dict[str, Any], candidate_bundle: dict[str, Any]) -> dict[str, Any]:
    if candidate_bundle.get("schema") != "cwk.rt054.ops-known-item-candidates.v1":
        raise ValueError("schema_invalid")
    if candidate_bundle.get("sampling_version") != SAMPLING_VERSION:
        raise ValueError("sampling_version_mismatch")
    if candidate_bundle.get("split_version") != SPLIT_VERSION:
        raise ValueError("split_version_mismatch")
    seed = str(candidate_bundle.get("seed") or "")
    if not seed:
        raise ValueError("seed_missing")
    corpus_libraries = corpus.get("libraries")
    generated_libraries = candidate_bundle.get("libraries")
    if not isinstance(corpus_libraries, dict) or not isinstance(generated_libraries, dict):
        raise ValueError("libraries_required")
    libraries = {}
    for kb_id, docs in sorted(corpus_libraries.items()):
        if not isinstance(docs, list):
            raise ValueError("library_docs_invalid")
        libraries[kb_id] = verify_library(kb_id, docs, generated_libraries.get(kb_id, {}), seed)
    return {
        "schema": "cwk.rt054.ops-known-item-verified.v1",
        "sampling_version": SAMPLING_VERSION, "split_version": SPLIT_VERSION,
        "seed": seed, "libraries": libraries,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    candidate_bundle = json.loads(args.candidates.read_text(encoding="utf-8"))
    result = verify(corpus, candidate_bundle)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
