#!/usr/bin/env python3
"""Protocol R: deterministic CURRENT-snapshot authority, not a historical record.

Pure functions; no I/O or candidate imports. Real inputs/outputs, including the
member ledger, are OPS-private. Local tests use synthetic documents only.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict

import kb_stage_b_ops_cases as historical

AUTHORITY_VERSION = 'rt055-current-snapshot-exclusion-r-v1'
RT054_SEED = 'rt054-quality-v1-fixed-seed'
RT054_SAMPLING_VERSION = 'ops-known-item-stratified-v2'
RT054_SPLIT_VERSION = 'ops-category-ordinal-calibration-holdout-v1'
FIELDS = ('doc_ids', 'queries', 'tokens')
DASHES = str.maketrans({c: '-' for c in '\u2010\u2011\u2012\u2013\u2014\u2212\ufe63'})
IDENTIFIER = re.compile(r'(?<![a-z0-9])(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)+(?![a-z0-9])')


def normalize(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).translate(DASHES)).strip().casefold()


def source_tokens(doc: dict) -> set[str]:
    """Whole title, whole filename, stem, and complete alphanumeric identifiers.

    No extension-only or individual-character tokens; cross-field matches are
    allowed. Identifiers include body identifiers to catch renamed sources.
    """
    title, filename = normalize(doc['title']), normalize(doc['filename'])
    values = {title, filename, filename.rsplit('.', 1)[0]}
    values.update(IDENTIFIER.findall(normalize('\n'.join((doc['title'], doc['filename'], doc['body'])))))
    return values - {''}


def potential_queries(doc: dict) -> set[str]:
    """Superset of all queries the locked RT055 builder can derive from a source.

    Include every phrase, not just the first unique phrase. Enumerate all 32
    no-answer mutation attempts before selection, so exclusion never drops or
    replaces an already-built case. No expected labels or candidate behavior.
    """
    phrases = set(historical.phrase_candidates(doc['body']))
    values = set(historical.title_filename_candidates(doc))
    values.update(historical.exact_candidates(doc))
    values.update(historical.table_candidates(doc))
    values.update(phrases)
    alphabet = 'qzxvkj'
    for base in phrases:
        digest = hashlib.sha256(('ops-rt055-known-item-stratified-v1\x1f' + doc['doc_id'] + '\x1f' + base).encode()).hexdigest()
        for attempt in range(32):
            suffix = ''.join(alphabet[int(ch, 16) % len(alphabet)] for ch in digest[attempt:attempt + 12])
            values.add(f'zz{suffix}{attempt:02d}')
    return {normalize(value) for value in values if normalize(value)}


def reconstruct(corpus: dict) -> dict:
    if (historical.SAMPLING_VERSION != RT054_SAMPLING_VERSION
            or historical.SPLIT_VERSION != RT054_SPLIT_VERSION):
        raise ValueError('r_generator_version_mismatch')
    bundle = historical.generate(corpus, RT054_SEED)
    members = {field: defaultdict(set) for field in FIELDS}
    docs_by_id = defaultdict(list)
    for docs in corpus['libraries'].values():
        for doc in docs:
            docs_by_id[doc['doc_id']].append(doc)
    sources = set()
    for library in bundle['libraries'].values():
        for row in library['cases']:
            origin = row['expected_doc_id']
            sources.add(origin)
            if row.get('neighbour_doc_id'):
                sources.add(row['neighbour_doc_id'])
            members['queries'][normalize(row['query'])].add(origin)
    for source_id in sources:
        members['doc_ids'][source_id].add(source_id)
        for doc in docs_by_id[source_id]:
            for token in source_tokens(doc):
                members['tokens'][token].add(source_id)
    return {
        'authority_version': AUTHORITY_VERSION,
        'sampling_version': RT054_SAMPLING_VERSION,
        'split_version': RT054_SPLIT_VERSION,
        'seed': RT054_SEED,
        **{field: sorted(members[field]) for field in FIELDS},
        'members': [{'kind': field, 'value': value, 'source_ids': sorted(origins)}
                    for field in FIELDS for value, origins in sorted(members[field].items())],
    }


def validate_r(authority: dict) -> None:
    if authority.get('authority_version') != AUTHORITY_VERSION:
        raise ValueError('r_authority_version_mismatch')
    seen = {field: set() for field in FIELDS}
    for member in authority['members']:
        field, value, origins = member['kind'], member['value'], member['source_ids']
        if field not in FIELDS or not value or not origins or not all(isinstance(x, str) and x for x in origins):
            raise ValueError('r_member_invalid')
        if value in seen[field]:
            raise ValueError('r_member_duplicate')
        seen[field].add(value)
    if any(seen[field] != set(authority[field]) for field in FIELDS):
        raise ValueError('r_member_coverage_mismatch')


def intersections(doc: dict, authority: dict) -> dict:
    return {
        'doc_ids': sorted({doc['doc_id']} & set(authority['doc_ids'])),
        'queries': sorted(potential_queries(doc) & set(authority['queries'])),
        'tokens': sorted(source_tokens(doc) & set(authority['tokens'])),
    }


def filter_sources(docs: list[dict], authority: dict, eligible_ids=None) -> tuple[list[dict], list[dict]]:
    validate_r(authority)
    eligible = {doc['doc_id'] for doc in docs} if eligible_ids is None else set(eligible_ids)
    allowed, ledger = [], []
    for doc in docs:
        matches = intersections(doc, authority)
        if any(matches.values()):
            ledger.append({'source_id': doc['doc_id'], 'matches': matches})
        elif doc['doc_id'] in eligible:
            allowed.append(doc)
    return allowed, ledger


def verify_exclusion(docs: list[dict], authority: dict, generated: dict) -> dict:
    """Independent reconciliation: recompute, never trust builder booleans.

    Check all emitted source references (expected AND distractor), every query,
    all source tokens, each matching snapshot item, and every authority member.
    An origin outside the current snapshot is explicitly accounted as absent.
    """
    validate_r(authority)
    by_id = {doc['doc_id']: doc for doc in docs}
    ledger = {}
    ledger_valid = True
    for row in generated.get('excluded_sources', []):
        source_id = row.get('source_id')
        if source_id in ledger or source_id not in by_id:
            ledger_valid = False
            continue
        ledger[source_id] = row.get('matches')
    computed = {source_id: intersections(doc, authority) for source_id, doc in by_id.items()}
    for source_id, matches in computed.items():
        if any(matches.values()) and ledger.get(source_id) != matches:
            ledger_valid = False
        if source_id in ledger and (not any(matches.values()) or ledger[source_id] != matches):
            ledger_valid = False
    emitted_ids, queries = set(), set()
    for row in generated.get('cases', []):
        emitted_ids.add(row.get('expected_doc_id', ''))
        if row.get('neighbour_doc_id'):
            emitted_ids.add(row['neighbour_doc_id'])
        queries.add(normalize(row.get('query', '')))
    tokens = set()
    unknown_sources = bool(emitted_ids - by_id.keys())
    for source_id in emitted_ids & by_id.keys():
        tokens.update(source_tokens(by_id[source_id]))
    overlaps = {
        'doc_ids': len(emitted_ids & set(authority['doc_ids'])),
        'queries': len(queries & set(authority['queries'])),
        'tokens': len(tokens & set(authority['tokens'])),
    }
    # Even if the chosen query itself is clean, a matching *source* cannot emit
    # a different query. This prevents query-level deletion masquerading as R.
    source_level_ok = all(not any(computed[source_id].values()) for source_id in emitted_ids & by_id.keys())
    accounted = absent = 0
    for member in authority['members']:
        origins = set(member['source_ids'])
        present = origins & by_id.keys()
        if not present:
            absent += 1
            accounted += 1
        elif present <= ledger.keys() and all(ledger[source_id] == computed[source_id] and any(computed[source_id].values()) for source_id in present):
            accounted += 1
    unaccounted = len(authority['members']) - accounted
    ok = ledger_valid and not unknown_sources and source_level_ok and not any(overlaps.values()) and unaccounted == 0
    return {'verified': ok, 'overlap_counts': overlaps, 'ledger_verified': ledger_valid,
            'source_level_verified': source_level_ok, 'members_total': len(authority['members']),
            'members_accounted': accounted, 'members_absent': absent,
            'members_unaccounted': unaccounted}
