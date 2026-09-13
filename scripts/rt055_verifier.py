#!/usr/bin/env python3
"""RT-055 verifier role (ops-rt055-verifier).

Independent process; imports nothing from the builder. Re-opens the
frozen corpus snapshot, recomputes the expected-document order, validates every
case constraint, checks category coverage and RT-054 pool exclusion by
deterministic reconstruction. Freeze orchestration is a separate OPS executor;
this module never attests candidate artifacts. All output stays private (0600).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import rt055_exclusion as exclusion_gate
import rt055_tiers as tiers
import rt055_opslib as ops  # noqa: E402

SAMPLING_VERSION = "ops-rt055-known-item-stratified-v1"
SPLIT = "holdout"
CATEGORIES = (
    "title_filename", "exact_identifier_date", "body_only_rare_phrase",
    "table_row", "no_answer_mutation", "near_neighbour",
)
TARGETS = {
    "title_filename": 10, "exact_identifier_date": 8,
    "body_only_rare_phrase": 10, "table_row": 4,
    "no_answer_mutation": 5, "near_neighbour": 5,
}
RT054_SAMPLING_VERSION = "ops-known-item-stratified-v2"
RT054_SEED = "rt054-quality-v1-fixed-seed"
IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![A-Za-z0-9])"
)
DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{4}年\d{1,2}月\d{1,2}日")
TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9][A-Za-z0-9_.-]*")
ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{3,}")
REJECTION_REASONS = (
    "schema_invalid", "sampling_version_mismatch", "unknown_category",
    "expected_lock_mismatch", "expected_missing", "query_empty",
    "query_not_in_expected", "title_filename_constraint", "unique_constraint",
    "body_only_constraint", "table_structure_constraint", "no_answer_present",
    "near_neighbour_constraint", "duplicate_case", "category_ordinal_mismatch",
    "split_mismatch",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def stable_int(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x1f".join(parts).encode()).digest()[:8], "big")


def stratum(doc: dict) -> str:
    body = doc["body"]
    if any(line.count("|") >= 2 for line in body.splitlines()):
        return "table"
    size = len(TOKEN_RE.findall(body))
    return "long" if size >= 2000 else "medium" if size >= 500 else "short"


def independent_locked_order(kb_id: str, docs: Sequence[dict], seed: str) -> list[str]:
    strata: dict[str, list[dict]] = {}
    for doc in docs:
        strata.setdefault(stratum(doc), []).append(doc)
    for rows in strata.values():
        rows.sort(key=lambda row: (stable_int(SAMPLING_VERSION, seed, kb_id,
                                              stratum(row), row["doc_id"]), row["doc_id"]))
    active = sorted(strata)
    positions = {name: 0 for name in active}
    ordered: list[str] = []
    while active:
        next_active: list[str] = []
        for name in active:
            if positions[name] < len(strata[name]):
                ordered.append(strata[name][positions[name]]["doc_id"])
                positions[name] += 1
            if positions[name] < len(strata[name]):
                next_active.append(name)
        active = next_active
    return ordered


def relation_tokens(doc: dict) -> set[str]:
    text = normalize(doc["title"] + "\n" + doc["body"])
    cjk = re.findall(r"[\u3400-\u9fff]{2,6}", text)
    ascii_words = [x.casefold() for x in ASCII_WORD_RE.findall(text)]
    return {x for x in (*cjk, *ascii_words) if len(x) >= 2}


def table_line_contains(doc: dict, query: str) -> bool:
    q = normalize(query)
    for line in doc["body"].splitlines():
        if line.count("|") >= 2 and not re.fullmatch(r"[\s|:=-]+", line) and q in normalize(line):
            return True
    return False


def verify_library(kb_id: str, docs: Sequence[dict], generated: dict, seed: str, *, exclusion=None, eligible_ids=None, tier='T3') -> dict:
    tiers.fields(tier)
    by_id = {doc["doc_id"]: doc for doc in docs}
    # Independently recompute the admissible source order, not builder output.
    allowed = [doc for doc in docs if (eligible_ids is None or doc['doc_id'] in eligible_ids)
               and (exclusion is None or not any(exclusion_gate.intersections(doc, exclusion, tier).values()))]
    locked = independent_locked_order(kb_id, allowed, seed)
    exclusion_proof = (exclusion_gate.verify_exclusion(list(docs), exclusion, generated, tier)
                       if exclusion is not None else {'verified': False})
    corpus_text = {doc_id: normalize("\n".join((d["title"], d["filename"], d["body"])))
                   for doc_id, d in by_id.items()}
    relation = {doc_id: relation_tokens(doc) for doc_id, doc in by_id.items()}
    verified: list[dict] = []
    rejected: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    ordinals: Counter[str] = Counter()

    def reject(category: str, reason: str) -> None:
        rejected.append({"category": category if category in CATEGORIES else "unknown",
                         "reason": reason})

    for row in generated.get("cases", []) if isinstance(generated, dict) else []:
        if not isinstance(row, dict):
            reject("unknown", "schema_invalid"); continue
        category = str(row.get("category") or "")
        if category not in CATEGORIES:
            reject(category, "unknown_category"); continue
        if row.get("sampling_version") != SAMPLING_VERSION or row.get("seed") != seed:
            reject(category, "sampling_version_mismatch"); continue
        if row.get("split") != SPLIT:
            reject(category, "split_mismatch"); continue
        ordinal = ordinals[category]
        if row.get("category_ordinal") != ordinal:
            reject(category, "category_ordinal_mismatch"); continue
        ordinals[category] += 1
        query = str(row.get("query") or "").strip()
        expected = str(row.get("expected_doc_id") or "")
        try:
            lock_ordinal = int(row.get("expected_lock_ordinal"))
        except (TypeError, ValueError):
            reject(category, "expected_lock_mismatch"); continue
        if lock_ordinal < 0 or lock_ordinal >= len(locked) or locked[lock_ordinal] != expected:
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
        expected_body = normalize(doc["body"])
        expected_title_file = normalize(doc["title"] + "\n" + doc["filename"])

        if category != "no_answer_mutation" and expected not in owners:
            reject(category, "query_not_in_expected"); continue
        if category == "title_filename":
            stem = normalize(doc["filename"]).rsplit(".", 1)[0]
            if q not in {normalize(doc["title"]), normalize(doc["filename"]), stem} or owners != [expected]:
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
        verified.append({"ordinal": len(verified), "kb_id": kb_id, "category": category,
                         "category_ordinal": ordinal, "query": query,
                         "expected_doc_id": expected,
                         "exact": category == "exact_identifier_date",
                         "expected_outcome": "no_evidence" if category == "no_answer_mutation" else "hit"})

    counts = Counter(row["category"] for row in verified)
    coverage = {category: {"target": TARGETS[category], "verified": counts[category],
                           "floor": tiers.FLOORS[category], "ok": counts[category] >= tiers.FLOORS[category]}
                for category in CATEGORIES}
    return {"kb_id": kb_id, "cases": verified, "case_total": len(verified),
            "category_counts": dict(counts), "category_coverage": coverage,
            "exclusion_verified": exclusion_proof["verified"],
            "exclusion_proof": exclusion_proof,
            "rejections": {"total": len(rejected),
                           "reason_counts": dict(Counter(r["reason"] for r in rejected))}}


def independent_replay(kb_id, docs, seed, authority, eligible_ids, tier):
    """Independently enumerate the complete deterministic pool; never import builder."""
    historical = exclusion_gate.historical
    allowed, ledger = exclusion_gate.filter_sources(list(docs), authority, eligible_ids, tier)
    by_id = {d['doc_id']:d for d in docs}
    ordered = independent_locked_order(kb_id, allowed, seed)
    text = {k:normalize('\n'.join((d['title'],d['filename'],d['body']))) for k,d in by_id.items()}
    corpus = '\n'.join(text.values())
    relations = {k:relation_tokens(d) for k,d in by_id.items()}
    def owner(q):
        found=[k for k,v in text.items() if normalize(q) and normalize(q) in v]
        return found[0] if len(found)==1 else None
    def mutate(base, origin):
        digest=hashlib.sha256((SAMPLING_VERSION+'\x1f'+origin+'\x1f'+base).encode()).hexdigest()
        for attempt in range(32):
            suffix=''.join('qzxvkj'[int(c,16)%6] for c in digest[attempt:attempt+12])
            q=f'zz{suffix}{attempt:02d}'
            if q.casefold() not in corpus:return q
        raise ValueError('no_absent_mutation')
    rows=[]; counts=Counter()
    for cat in CATEGORIES:
        for lock, origin in enumerate(ordered):
            if counts[cat]>=TARGETS[cat]:break
            doc=by_id[origin]
            if cat=='title_filename': options=historical.title_filename_candidates(doc)
            elif cat=='exact_identifier_date': options=historical.exact_candidates(doc)
            elif cat=='table_row': options=historical.table_candidates(doc)
            else: options=historical.phrase_candidates(doc['body'])
            for q in options:
                if cat=='body_only_rare_phrase' and normalize(q) in normalize(doc['title']+'\n'+doc['filename']):continue
                if owner(q)!=origin:continue
                extra={}
                if cat=='no_answer_mutation':q=mutate(q,origin);extra={'mutation_source':'rare_phrase'}
                if cat=='near_neighbour':
                    neighbours=[(len(relations[origin]&relations[other]),stable_int(seed,origin,other),other)
                                for other in ordered if other!=origin and len(relations[origin]&relations[other])>=2]
                    if not neighbours:continue
                    extra={'neighbour_doc_id':max(neighbours)[2]}
                if 'queries' in tiers.fields(tier) and exclusion_gate.normalize(q) in authority['queries']:raise ValueError('r_query_inventory_incomplete')
                rows.append({'ordinal':len(rows),'kb_id':kb_id,'category':cat,'category_ordinal':counts[cat],
                             'split':SPLIT,'query':q,'expected_doc_id':origin,'expected_lock_ordinal':lock,
                             'expected_stratum':stratum(doc),'sampling_version':SAMPLING_VERSION,'seed':seed,**extra})
                counts[cat]+=1;break
    return {'cases':rows,'category_counts':dict(counts),'excluded_sources':ledger,'seed':seed}


def verify_adaptive_library(kb_id, docs, generated, seed, *, exclusion, eligible_ids=None):
    # Independent selector and floor arithmetic, not the builder's helper.
    trace = []
    selected = None
    for tier in ('T3', 'T2', 'T1'):
        pool = independent_replay(kb_id, docs, seed, exclusion, eligible_ids, tier)
        counts = Counter(row['category'] for row in pool['cases'])
        counts = {category: counts[category] for category in CATEGORIES}
        minima = dict(zip(CATEGORIES, (3, 2, 3, 3, 3, 3)))
        passed = len(pool['cases']) >= 16 and all(
            minima[c] <= counts[c] <= TARGETS[c] for c in CATEGORIES)
        trace.append({'tier': tier, 'category_counts': counts,
                      'total_count': len(pool['cases']), 'floor_pass': passed})
        if passed:
            selected = tier
            break
    replay = {**pool, 'status': 'PARTICIPATING' if selected else 'DEFERRED',
              'tier': selected, 'trace': trace, 'floors': minima,
              'targets': dict(TARGETS), 'total_floor': 16,
              'category_counts': counts if selected else dict.fromkeys(CATEGORIES, 0),
              'cases': pool['cases'] if selected else []}
    compare=('cases','seed','status','tier','trace','floors','targets','total_floor','category_counts','excluded_sources')
    selection_ok=all(generated.get(k)==replay[k] for k in compare)
    tier=replay['tier'] or 'T1'
    checked=verify_library(kb_id,docs,generated,seed,exclusion=exclusion,eligible_ids=eligible_ids,tier=tier)
    checked.update(status=replay['status'],tier=replay['tier'],selection_verified=selection_ok,
                   validity=tiers.public_selection(replay,verified=selection_ok))
    return checked


def mode_verify_cases() -> int:
    import os
    with os.fdopen(os.open(HERE / 'single-verify-claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as claim:
        claim.write('claimed')
    builder = ROOT / 'builder'
    manifest = ops.read_json(builder / 'private-manifest.json')
    if manifest.get('status') != 'OK':
        return 3
    corpus = ops.read_json(builder / 'private-corpus.json')
    original = ops.read_json(builder / 'private-source-snapshot.json')
    candidates = ops.read_json(builder / 'private-candidates.json')
    source_libraries, _ = ops.snapshot_libraries(ops.gateway_source_envs(), HERE)
    reread = {'schema': ops.CORPUS_SCHEMA, 'libraries': source_libraries}
    snapshot_equal = reread == original
    ops.write_private_json(HERE / 'private-source-reread.json', reread)
    authority = exclusion_gate.reconstruct(reread)
    ops.write_private_json(HERE / 'private-exclusion-r.json', authority)
    r_equal = authority == ops.read_json(builder / 'private-exclusion-r.json')
    hashes_equal = (
        manifest['source_snapshot_sha256'] == ops.sha_file(builder / 'private-source-snapshot.json')
        and manifest['exclusion_r_sha256'] == ops.sha_file(builder / 'private-exclusion-r.json')
        and manifest['corpus_snapshot_sha256'] == ops.sha_file(builder / 'private-corpus.json')
        and manifest['candidates_sha256'] == ops.sha_file(builder / 'private-candidates.json'))
    filtered = {kb: [doc for doc in rows if ops.canonical_runes(doc) <= ops.B_MANUAL_MAX_RUNES]
                for kb, rows in source_libraries.items()}
    corpus_equal = corpus['libraries'] == filtered
    seed = candidates.get('seed')
    claim = ops.read_json(builder/'single-build-claim.json')
    claim_equal = (isinstance(seed, str) and bool(re.fullmatch(r'[0-9a-f]{32}', seed))
                   and set(claim) == {'seed', 'single_build_claimed'}
                   and claim.get('single_build_claimed') is True and claim.get('seed') == seed)
    schema_ok = (bool(seed) and candidates.get('schema') == 'cwk.rt055.ops-known-item-candidates.v1'
                 and candidates.get('sampling_version') == SAMPLING_VERSION
                 and candidates.get('split_version') == 'ops-rt055-single-use-holdout-v1'
                 and set(source_libraries) == set(ops.LIBRARIES)
                 and set(candidates.get('libraries', {})) == set(ops.LIBRARIES))
    if not schema_ok:
        return 3
    libraries = {kb: verify_adaptive_library(kb, source_libraries[kb], candidates['libraries'][kb], seed,
                                    exclusion=authority,
                                    eligible_ids={doc['doc_id'] for doc in filtered[kb]})
                 for kb in ops.LIBRARIES}
    participating = {kb:lib for kb,lib in libraries.items() if lib['status']=='PARTICIPATING'}
    coverage_ok = bool(participating) and all(lib['case_total'] >= 16 and all(row['ok'] for row in lib['category_coverage'].values()) for lib in participating.values())
    denominators_ok = all(all(lib['category_counts'].get(category, 0) > 0 for category in CATEGORIES) for lib in participating.values())
    rejections = sum(lib['rejections']['total'] for lib in libraries.values())
    disjoint = all(lib['exclusion_verified'] for lib in libraries.values())
    gates = {
        'single_build_seed_verified': claim_equal,
        'selection_verified': all(lib['selection_verified'] for lib in libraries.values()),
        'at_least_one_participating': bool(participating),
        'category_coverage_verified': coverage_ok,
        'denominators_nonzero_all_categories': denominators_ok,
        'rt054_pool_reconstructed': True,
        'rt054_pool_excluded': disjoint and r_equal,
        'input_disjoint_verified': disjoint and r_equal,
        'source_snapshot_reread_equal': snapshot_equal,
        'corpus_eligibility_equal': corpus_equal,
        'private_artifact_hashes_verified': hashes_equal,
        'authority_reconstruction_equal': r_equal,
        'all_cases_accepted': rejections == 0,
    }
    ok = all(gates.values())
    ops.write_private_json(HERE / 'private-verified.json', {
        'schema': 'cwk.rt055.ops-known-item-verified.v1', 'sampling_version': SAMPLING_VERSION,
        'seed': seed, 'corpus_snapshot_sha256': ops.sha_file(builder / 'private-corpus.json'),
        'libraries': libraries})
    ops.write_private_json(HERE / 'case-verification.json', {
        **gates, 'verified': ok,
        'participating_libraries':list(participating),
        'deferred_libraries':[kb for kb in ops.LIBRARIES if kb not in participating],
        'library_validity':{kb:lib['validity'] for kb,lib in libraries.items()}, 'exclusion_authority_version': exclusion_gate.AUTHORITY_VERSION,
        'rt054_reconstruction_overlap_counts': {kb: lib['exclusion_proof']['overlap_counts'] for kb, lib in libraries.items()},
        'r_member_accounting': {kb: {key: lib['exclusion_proof'][key] for key in
                                   ('members_total', 'members_accounted', 'members_absent', 'members_unaccounted')}
                                for kb, lib in libraries.items()},
        'verified_sha256': ops.sha_file(HERE / 'private-verified.json'), 'rejections_total': rejections})
    print('VERIFIER_CASES', 'PASS' if ok else 'FAIL', json.dumps({kb: lib['case_total'] for kb, lib in libraries.items()}))
    return 0 if ok else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("verify-cases",), required=True)
    args = parser.parse_args()
    if (ROOT.is_symlink() or not ROOT.is_dir() or not ROOT.name.startswith("rt055-")
            or ROOT.stat().st_mode & 0o077 or not (ROOT / ".rt055-owned").is_file()):
        print("VERIFIER: private_root_invalid")
        return 3
    return mode_verify_cases()


if __name__ == "__main__":
    raise SystemExit(main())
