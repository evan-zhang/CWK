#!/usr/bin/env python3
"""Generate confidential OPS known-item cases without executing search.

This module is copied to and executed inside the run's mode-0700 OPS directory.
It reads only the private corpus snapshot, fixes expected documents with a
versioned seeded stratified order, and then derives candidate queries.  Its
output is private input for the independent verifier and must never be copied
back to the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SAMPLING_VERSION = "ops-known-item-stratified-v2"
SPLIT_VERSION = "ops-category-ordinal-calibration-holdout-v1"
CATEGORIES = (
    "title_filename",
    "exact_identifier_date",
    "body_only_rare_phrase",
    "table_row",
    "no_answer_mutation",
    "near_neighbour",
)
TARGETS = {
    "title_filename": 10,
    "exact_identifier_date": 8,
    "body_only_rare_phrase": 10,
    "table_row": 4,
    "no_answer_mutation": 5,
    "near_neighbour": 5,
}
IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![A-Za-z0-9])"
)
DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{4}年\d{1,2}月\d{1,2}日")
TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9][A-Za-z0-9_.-]*")
ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{3,}")


@dataclass(frozen=True)
class LockedExpected:
    kb_id: str
    doc_id: str
    ordinal: int
    stratum: str


def _stable_int(*parts: str) -> int:
    raw = "\x1f".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def split_for_category_ordinal(category: str, ordinal: int) -> str:
    """Assign the versioned split without consulting a query or document text."""
    if category not in CATEGORIES or ordinal < 0:
        raise ValueError("invalid_category_ordinal")
    return "calibration" if ordinal < round(TARGETS[category] / 3) else "holdout"


def doc_stratum(doc: dict[str, Any]) -> str:
    body = str(doc.get("body") or "")
    if any(line.count("|") >= 2 for line in body.splitlines()):
        return "table"
    size = len(TOKEN_RE.findall(body))
    if size >= 2000:
        return "long"
    if size >= 500:
        return "medium"
    return "short"


def locked_expected_documents(
    kb_id: str, docs: Sequence[dict[str, Any]], seed: str, version: str = SAMPLING_VERSION
) -> list[LockedExpected]:
    """Return the expected-document order before any query is derived."""
    strata: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        strata.setdefault(doc_stratum(doc), []).append(doc)
    for stratum, rows in strata.items():
        rows.sort(key=lambda row: (
            _stable_int(version, seed, kb_id, stratum, str(row["doc_id"])),
            str(row["doc_id"]),
        ))
    ordered: list[LockedExpected] = []
    active = sorted(strata)
    positions = {name: 0 for name in active}
    while active:
        next_active: list[str] = []
        for stratum in active:
            pos = positions[stratum]
            rows = strata[stratum]
            if pos < len(rows):
                row = rows[pos]
                ordered.append(LockedExpected(kb_id, str(row["doc_id"]), len(ordered), stratum))
                positions[stratum] += 1
            if positions[stratum] < len(rows):
                next_active.append(stratum)
        active = next_active
    return ordered


def phrase_candidates(text: str, *, min_tokens: int = 4, max_tokens: int = 10) -> Iterable[str]:
    for line in text.splitlines():
        clean = normalize(line.strip("#>*- \t"))
        tokens = TOKEN_RE.findall(clean)
        if len(tokens) < min_tokens:
            continue
        width = min(max_tokens, len(tokens))
        for start in range(0, max(1, len(tokens) - width + 1), max(1, width // 2)):
            phrase = "".join(tokens[start:start + width])
            if 6 <= len(phrase) <= 96:
                yield phrase


def title_filename_candidates(doc: dict[str, Any]) -> Iterable[str]:
    title = normalize(str(doc.get("title") or ""))
    filename = normalize(str(doc.get("filename") or ""))
    stem = filename.rsplit(".", 1)[0]
    for value in (title, filename, stem):
        if 3 <= len(value) <= 96 and any(ch.isalnum() or "\u3400" <= ch <= "\u9fff" for ch in value):
            yield value


def exact_candidates(doc: dict[str, Any]) -> Iterable[str]:
    # Match the indexed exact-field projection: body + title, never infer an
    # identifier from filename punctuation and route it to the wrong field.
    joined = "\n".join((str(doc.get("title") or ""), str(doc.get("body") or "")))
    yield from IDENTIFIER_RE.findall(joined)
    yield from DATE_RE.findall(joined)


def table_candidates(doc: dict[str, Any]) -> Iterable[str]:
    for line in str(doc.get("body") or "").splitlines():
        if line.count("|") < 2 or re.fullmatch(r"[\s|:=-]+", line):
            continue
        cells = [normalize(cell) for cell in line.split("|") if normalize(cell)]
        for cell in sorted(cells, key=len, reverse=True):
            if 4 <= len(cell) <= 96 and not re.fullmatch(r"[\d.,%+-]+", cell):
                yield cell


def relation_tokens(doc: dict[str, Any]) -> set[str]:
    text = normalize("\n".join((str(doc.get("title") or ""), str(doc.get("body") or ""))))
    cjk = re.findall(r"[\u3400-\u9fff]{2,6}", text)
    ascii_words = [x.casefold() for x in ASCII_WORD_RE.findall(text)]
    return {x for x in (*cjk, *ascii_words) if len(x) >= 2}


def mutate_to_absent(base: str, corpus_text: str, salt: str) -> str:
    alphabet = "qzxvkj"
    digest = hashlib.sha256((SAMPLING_VERSION + "\x1f" + salt + "\x1f" + base).encode()).hexdigest()
    for attempt in range(32):
        suffix = "".join(alphabet[int(ch, 16) % len(alphabet)] for ch in digest[attempt:attempt + 12])
        candidate = f"zz{suffix}{attempt:02d}"
        if candidate.casefold() not in corpus_text:
            return candidate
    raise ValueError("no_absent_mutation")


def derive_cases_for_library(kb_id: str, docs: Sequence[dict[str, Any]], seed: str) -> dict[str, Any]:
    by_id = {str(doc["doc_id"]): doc for doc in docs}
    locked = locked_expected_documents(kb_id, docs, seed)
    corpus_norm = {doc_id: normalize("\n".join((
        str(doc.get("title") or ""), str(doc.get("filename") or ""), str(doc.get("body") or "")
    ))) for doc_id, doc in by_id.items()}
    all_corpus = "\n".join(corpus_norm.values())
    relation = {doc_id: relation_tokens(doc) for doc_id, doc in by_id.items()}
    cases: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    def unique_owner(query: str) -> str | None:
        q = normalize(query)
        owners = [doc_id for doc_id, text in corpus_norm.items() if q and q in text]
        return owners[0] if len(owners) == 1 else None

    def add(lock: LockedExpected, category: str, category_ordinal: int,
            split: str, query: str, **extra: Any) -> bool:
        if counts[category] >= TARGETS[category]:
            return False
        row = {
            "ordinal": len(cases), "kb_id": kb_id, "category": category,
            "category_ordinal": category_ordinal, "split": split,
            "query": query, "expected_doc_id": lock.doc_id,
            "expected_lock_ordinal": lock.ordinal, "expected_stratum": lock.stratum,
            "sampling_version": SAMPLING_VERSION, "seed": seed,
        }
        row.update(extra)
        cases.append(row)
        counts[category] += 1
        return True

    # Every pass consumes the pre-locked document order. Query derivation starts only here.
    for lock in locked:
        if counts["title_filename"] >= TARGETS["title_filename"]:
            break
        category_ordinal = counts["title_filename"]
        split = split_for_category_ordinal("title_filename", category_ordinal)
        doc = by_id[lock.doc_id]
        query = next((q for q in title_filename_candidates(doc)
                      if normalize(q) in corpus_norm[lock.doc_id] and unique_owner(q) == lock.doc_id), None)
        if query:
            add(lock, "title_filename", category_ordinal, split, query)

    for lock in locked:
        if counts["exact_identifier_date"] >= TARGETS["exact_identifier_date"]:
            break
        category_ordinal = counts["exact_identifier_date"]
        split = split_for_category_ordinal("exact_identifier_date", category_ordinal)
        doc = by_id[lock.doc_id]
        query = next((q for q in exact_candidates(doc) if unique_owner(q) == lock.doc_id), None)
        if query:
            add(lock, "exact_identifier_date", category_ordinal, split, query)

    for lock in locked:
        if counts["body_only_rare_phrase"] >= TARGETS["body_only_rare_phrase"]:
            break
        category_ordinal = counts["body_only_rare_phrase"]
        split = split_for_category_ordinal("body_only_rare_phrase", category_ordinal)
        doc = by_id[lock.doc_id]
        title_file = normalize(str(doc.get("title") or "") + "\n" + str(doc.get("filename") or ""))
        phrase = next((q for q in phrase_candidates(str(doc.get("body") or ""))
                       if normalize(q) not in title_file and unique_owner(q) == lock.doc_id), None)
        if phrase:
            add(lock, "body_only_rare_phrase", category_ordinal, split, phrase)

    for lock in locked:
        if counts["table_row"] >= TARGETS["table_row"]:
            break
        category_ordinal = counts["table_row"]
        split = split_for_category_ordinal("table_row", category_ordinal)
        doc = by_id[lock.doc_id]
        query = next((q for q in table_candidates(doc) if unique_owner(q) == lock.doc_id), None)
        if query:
            add(lock, "table_row", category_ordinal, split, query)

    for lock in locked:
        if counts["no_answer_mutation"] >= TARGETS["no_answer_mutation"]:
            break
        category_ordinal = counts["no_answer_mutation"]
        split = split_for_category_ordinal("no_answer_mutation", category_ordinal)
        doc = by_id[lock.doc_id]
        base = next((q for q in phrase_candidates(str(doc.get("body") or ""))
                     if unique_owner(q) == lock.doc_id), None)
        if base:
            add(lock, "no_answer_mutation", category_ordinal, split,
                mutate_to_absent(base, all_corpus, lock.doc_id), mutation_source="rare_phrase")

    for lock in locked:
        if counts["near_neighbour"] >= TARGETS["near_neighbour"]:
            break
        category_ordinal = counts["near_neighbour"]
        split = split_for_category_ordinal("near_neighbour", category_ordinal)
        doc = by_id[lock.doc_id]
        query = next((q for q in phrase_candidates(str(doc.get("body") or ""))
                      if unique_owner(q) == lock.doc_id), None)
        if not query:
            continue
        candidates = []
        for other_id in by_id:
            if other_id == lock.doc_id:
                continue
            overlap = relation[lock.doc_id] & relation[other_id]
            if len(overlap) >= 2:
                candidates.append((len(overlap), _stable_int(seed, lock.doc_id, other_id), other_id))
        if candidates:
            _, _, neighbour = max(candidates)
            add(lock, "near_neighbour", category_ordinal, split, query,
                neighbour_doc_id=neighbour)

    unavailable = {
        category: ("AVAILABLE" if counts[category] else "UNKNOWN")
        for category in CATEGORIES
    }
    return {
        "kb_id": kb_id, "sampling_version": SAMPLING_VERSION,
        "split_version": SPLIT_VERSION, "seed": seed,
        "locked_count": len(locked), "cases": cases, "category_counts": dict(counts),
        "category_availability": unavailable,
    }


def generate(corpus: dict[str, Any], seed: str) -> dict[str, Any]:
    libraries = corpus.get("libraries")
    if not isinstance(libraries, dict):
        raise ValueError("libraries_required")
    return {
        "schema": "cwk.rt054.ops-known-item-candidates.v1",
        "sampling_version": SAMPLING_VERSION, "split_version": SPLIT_VERSION,
        "seed": seed,
        "libraries": {
            kb: derive_cases_for_library(kb, rows, seed)
            for kb, rows in sorted(libraries.items()) if isinstance(rows, list)
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    result = generate(corpus, args.seed)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
