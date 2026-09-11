#!/usr/bin/env python3
"""RT-055 builder role (ops-rt055-builder).

Separate process in its own 0700 directory. Reads the three libraries
read-only from NAS via the Gateway process environments, freezes the private
corpus snapshot, derives the single-use holdout with a fresh random seed, and
applies the candidate-B manual-ingestion pre-flight gate. Output stays private
on OPS (0600). Query/expected/content never leave this directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import rt055_exclusion as exclusion_gate
import rt055_opslib as ops  # noqa: E402  # copied into each role directory on OPS

SAMPLING_VERSION = "ops-rt055-known-item-stratified-v1"
SPLIT_VERSION = "ops-rt055-single-use-holdout-v1"
SPLIT = "holdout"
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


def validate_root() -> None:
    if (ROOT.is_symlink() or not ROOT.is_dir()
            or not ROOT.name.startswith("rt055-")
            or ROOT.stat().st_mode & 0o077):
        raise RuntimeError("private_root_invalid")
    if not (ROOT / ".rt055-owned").is_file():
        raise RuntimeError("private_root_marker_missing")


def stable_int(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()[:8], "big")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def doc_stratum(doc: dict) -> str:
    body = doc["body"]
    if any(line.count("|") >= 2 for line in body.splitlines()):
        return "table"
    size = len(TOKEN_RE.findall(body))
    if size >= 2000:
        return "long"
    if size >= 500:
        return "medium"
    return "short"


def locked_expected_documents(kb_id: str, docs: Sequence[dict], seed: str) -> list[dict]:
    """Pre-lock the expected-document order before any query is derived."""
    strata: dict[str, list[dict]] = {}
    for doc in docs:
        strata.setdefault(doc_stratum(doc), []).append(doc)
    for rows in strata.values():
        rows.sort(key=lambda row: (stable_int(SAMPLING_VERSION, seed, kb_id,
                                                 doc_stratum(row), row["doc_id"]),
                                   row["doc_id"]))
    ordered: list[dict] = []
    active = sorted(strata)
    positions = {name: 0 for name in active}
    while active:
        next_active: list[str] = []
        for stratum in active:
            pos = positions[stratum]
            if pos < len(strata[stratum]):
                row = strata[stratum][pos]
                ordered.append({"doc_id": row["doc_id"], "ordinal": len(ordered),
                                "stratum": stratum})
                positions[stratum] += 1
            if positions[stratum] < len(strata[stratum]):
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


def title_filename_candidates(doc: dict) -> Iterable[str]:
    title = normalize(doc["title"])
    filename = normalize(doc["filename"])
    stem = filename.rsplit(".", 1)[0]
    for value in (title, filename, stem):
        if 3 <= len(value) <= 96 and any(ch.isalnum() or "\u3400" <= ch <= "\u9fff" for ch in value):
            yield value


def exact_candidates(doc: dict) -> Iterable[str]:
    joined = "\n".join((doc["title"], doc["body"]))
    yield from IDENTIFIER_RE.findall(joined)
    yield from DATE_RE.findall(joined)


def table_candidates(doc: dict) -> Iterable[str]:
    for line in doc["body"].splitlines():
        if line.count("|") < 2 or re.fullmatch(r"[\s|:=-]+", line):
            continue
        cells = [normalize(cell) for cell in line.split("|") if normalize(cell)]
        for cell in sorted(cells, key=len, reverse=True):
            if 4 <= len(cell) <= 96 and not re.fullmatch(r"[\d.,%+-]+", cell):
                yield cell


def relation_tokens(doc: dict) -> set[str]:
    text = normalize("\n".join((doc["title"], doc["body"])))
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


def derive_cases_for_library(kb_id: str, docs: Sequence[dict], seed: str, *, exclusion=None, eligible_ids=None) -> dict[str, Any]:
    by_id = {doc["doc_id"]: doc for doc in docs}
    if exclusion is None:
        allowed = list(docs) if eligible_ids is None else [d for d in docs if d['doc_id'] in eligible_ids]
        excluded_sources = []
    else:
        allowed, excluded_sources = exclusion_gate.filter_sources(list(docs), exclusion, eligible_ids)
    allowed_ids = {doc['doc_id'] for doc in allowed}
    locked = locked_expected_documents(kb_id, allowed, seed)
    corpus_norm = {doc_id: normalize("\n".join((d["title"], d["filename"], d["body"])))
                   for doc_id, d in by_id.items()}
    all_corpus = "\n".join(corpus_norm.values())
    relation = {doc_id: relation_tokens(doc) for doc_id, doc in by_id.items()}
    cases: list[dict] = []
    counts: Counter[str] = Counter()

    def unique_owner(query: str) -> str | None:
        q = normalize(query)
        owners = [doc_id for doc_id, text in corpus_norm.items() if q and q in text]
        return owners[0] if len(owners) == 1 else None

    def add(lock: dict, category: str, category_ordinal: int, query: str, **extra: Any) -> bool:
        if counts[category] >= TARGETS[category]:
            return False
        if exclusion is not None and exclusion_gate.normalize(query) in exclusion['queries']:
            raise ValueError('r_query_inventory_incomplete')
        row = {"ordinal": len(cases), "kb_id": kb_id, "category": category,
               "category_ordinal": category_ordinal, "split": SPLIT,
               "query": query, "expected_doc_id": lock["doc_id"],
               "expected_lock_ordinal": lock["ordinal"], "expected_stratum": lock["stratum"],
               "sampling_version": SAMPLING_VERSION, "seed": seed}
        row.update(extra)
        cases.append(row)
        counts[category] += 1
        return True

    passes = (
        ("title_filename", title_filename_candidates, True),
        ("exact_identifier_date", exact_candidates, True),
        ("body_only_rare_phrase", lambda doc: (
            q for q in phrase_candidates(doc["body"])
            if normalize(q) not in normalize(doc["title"] + "\n" + doc["filename"])), True),
        ("table_row", table_candidates, True),
        ("no_answer_mutation", lambda doc: phrase_candidates(doc["body"]), False),
        ("near_neighbour", lambda doc: phrase_candidates(doc["body"]), True),
    )
    for category, candidate_fn, needs_owner in passes:
        for lock in locked:
            if counts[category] >= TARGETS[category]:
                break
            doc = by_id[lock["doc_id"]]
            if category == "no_answer_mutation":
                base = next((q for q in candidate_fn(doc) if unique_owner(q) == lock["doc_id"]), None)
                if base:
                    add(lock, category, counts[category],
                        mutate_to_absent(base, all_corpus, lock["doc_id"]),
                        mutation_source="rare_phrase")
                continue
            for query in candidate_fn(doc):
                if needs_owner and unique_owner(query) != lock["doc_id"]:
                    continue
                if category == "near_neighbour":
                    candidates = []
                    for other_id, other in by_id.items():
                        if other_id == lock["doc_id"] or other_id not in allowed_ids:
                            continue
                        overlap = relation[lock["doc_id"]] & relation[other_id]
                        if len(overlap) >= 2:
                            candidates.append((len(overlap), stable_int(seed, lock["doc_id"], other_id), other_id))
                    if not candidates:
                        continue
                    _, _, neighbour = max(candidates)
                    if add(lock, category, counts[category], query, neighbour_doc_id=neighbour):
                        break
                    continue
                if add(lock, category, counts[category], query):
                    break
    availability = {category: ("AVAILABLE" if counts[category] else "UNKNOWN")
                    for category in CATEGORIES}
    return {"kb_id": kb_id, "sampling_version": SAMPLING_VERSION,
            "split_version": SPLIT_VERSION, "seed": seed,
            "locked_count": len(locked), "cases": cases,
            "excluded_sources": excluded_sources,
            "category_counts": dict(counts), "category_availability": availability}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    validate_root()
    # O_EXCL claims the single construction before any source read. Failed
    # constructions keep the claim; no overwrite/reseed/retry-to-PASS path.
    import os
    with os.fdopen(os.open(HERE / 'single-build-claim.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        seed = secrets.token_hex(16)
        json.dump({'seed': seed, 'single_build_claimed': True}, handle)
    started = __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z")
    gateways = ops.find_gateway_processes()
    baseline = {
        "schema": "cwk.rt055.ops-baseline.v1",
        "captured_at": started,
        "gateways": [{"pid": row["pid"], "prefix": row["prefix"],
                      "port": row["port"], "command_sha256": row["command_sha256"]}
                     for row in gateways],
        "gateway_health": ops.gateway_health(),
        "nas_metadata": {},
    }
    envs = ops.gateway_source_envs()
    for kb in ops.LIBRARIES:
        env = dict(envs[kb])
        env["__prefix"] = kb
        baseline["nas_metadata"][kb] = ops.nas_metadata_summary(env)
    ops.write_private_json(HERE / "baseline.json", baseline)
    libraries, load_meta = ops.snapshot_libraries(envs, HERE)
    source_snapshot = {'schema': ops.CORPUS_SCHEMA, 'libraries': libraries}
    ops.write_private_json(HERE / 'private-source-snapshot.json', source_snapshot)
    authority = exclusion_gate.reconstruct(source_snapshot)
    ops.write_private_json(HERE / 'private-exclusion-r.json', authority)
    source_libraries = dict(libraries)
    # Corpus-level eligibility rule applied BEFORE sampling, equally to both
    # candidates: B's native manual ingestion hard-rejects canonical input
    # over 200k runes, so those documents are excluded from the frozen corpus
    # (never truncated, never split). Both candidates consume the identical
    # filtered corpus; per-library exclusion counts stay in the private
    # manifest and are reported in the acceptance evidence.
    for kb in ops.LIBRARIES:
        kept, over = [], 0
        for doc in libraries[kb]:
            if ops.canonical_runes(doc) > ops.B_MANUAL_MAX_RUNES:
                over += 1
            else:
                kept.append(doc)
        libraries[kb] = kept
        load_meta[kb]["excluded_over_manual_limit"] = over

    gates: dict[str, Any] = {"three_libraries_nonempty": all(len(v) > 0 for v in libraries.values())}
    max_runes = {kb: max(ops.canonical_runes(doc) for doc in docs) for kb, docs in libraries.items()}
    over_limit = {kb: sum(1 for doc in docs if ops.canonical_runes(doc) > ops.B_MANUAL_MAX_RUNES)
                  for kb, docs in libraries.items()}
    gates["b_manual_limit_ok"] = all(count == 0 for count in over_limit.values())
    gates["b_manual_limit_note"] = ("corpus-level eligibility: documents exceeding B's native manual "
                                    "limit were excluded before sampling, equally for both candidates")
    gates["b_manual_excluded_counts"] = {kb: load_meta[kb].get("excluded_over_manual_limit", 0)
                                         for kb in ops.LIBRARIES}
    gates["filtered_corpus_documents"] = {kb: len(libraries[kb]) for kb in ops.LIBRARIES}
    if not all(gates[k] for k in ("three_libraries_nonempty", "b_manual_limit_ok")):
        ops.write_private_json(HERE / "private-manifest.json", {
            "schema": "cwk.rt055.ops-builder-manifest.v1", "status": "GATE_FAILED",
            "gates": gates, "load_meta": load_meta, "max_canonical_runes": max_runes,
            "created_at": started})
        print("BUILDER GATE FAILED", json.dumps(gates, ensure_ascii=False))
        return 3

    per_library = {kb: derive_cases_for_library(kb, source_libraries[kb], seed,
                       exclusion=authority, eligible_ids={doc['doc_id'] for doc in docs})
                   for kb, docs in libraries.items()}
    corpus = {"schema": ops.CORPUS_SCHEMA, "created_at": started, "libraries": libraries}
    ops.write_private_json(HERE / "private-corpus.json", corpus)
    ops.write_private_json(HERE / "private-candidates.json", {
        "schema": "cwk.rt055.ops-known-item-candidates.v1",
        "sampling_version": SAMPLING_VERSION, "split_version": SPLIT_VERSION,
        "seed": seed, "libraries": per_library})
    ops.write_private_json(HERE / "private-manifest.json", {
        "schema": "cwk.rt055.ops-builder-manifest.v1", "status": "OK",
        "sampling_version": SAMPLING_VERSION, "split_version": SPLIT_VERSION,
        "seed_recorded": True, "created_at": started,
        "exclusion_authority_version": exclusion_gate.AUTHORITY_VERSION,
        "source_snapshot_sha256": ops.sha_file(HERE / 'private-source-snapshot.json'),
        "exclusion_r_sha256": ops.sha_file(HERE / 'private-exclusion-r.json'),
        "corpus_snapshot_sha256": ops.sha_file(HERE / "private-corpus.json"),
        "candidates_sha256": ops.sha_file(HERE / "private-candidates.json"),
        "libraries": {kb: {"documents": len(libraries[kb]),
                           "case_total": len(per_library[kb]["cases"]),
                           "category_counts": per_library[kb]["category_counts"],
                           "category_availability": per_library[kb]["category_availability"],
                           "max_canonical_runes": max_runes[kb]}
                      for kb in ops.LIBRARIES},
        "load_meta": load_meta, "gates": gates})
    print("BUILDER OK",
          json.dumps({kb: {"documents": len(libraries[kb]),
                           "cases": len(per_library[kb]["cases"])} for kb in ops.LIBRARIES}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
