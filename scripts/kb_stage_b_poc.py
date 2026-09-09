#!/usr/bin/env python3
"""RT-054 Stage B: local-only Parent/Child projection and analyzer benchmark.

This module is deliberately detached from Gateway, NAS and search services.  It
builds a replaceable projection from repository-owned synthetic fixtures and
measures a compact in-memory inverted-index representation.  Its byte counter
is *not* an OpenSearch primary-store measurement.

Analyzer evidence levels:

* legacy_123gram: exact execution of the existing CJK 1/2/3-gram semantics;
* icu_equivalent_probe: deterministic Unicode/CJK unigram probe, not ICU plugin;
* smartcn_equivalent_probe: deterministic CJK bigram probe, not SmartCN plugin.

The probes make the quality harness executable when plugins are unavailable;
they never claim OpenSearch measurements or plugin parity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import resource
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

PROJECT = Path(__file__).resolve().parents[1]
RT = PROJECT / "RT" / "RT-054"

PARENT_MIN_TOKENS = 800
PARENT_TARGET_TOKENS = 1200
PARENT_MAX_TOKENS = 2000
CHILD_MIN_TOKENS = 200
CHILD_TARGET_TOKENS = 350
CHILD_SOFT_MAX_TOKENS = 500
CHILD_MAX_TOKENS = 700
FORCED_OVERLAP_TOKENS = 40
CHUNKER_VERSION = "cwk-parent-child-v1"
MAPPING_VERSION = "cwk-child-mapping-v1"

CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df"
TOKEN_RE = re.compile(rf"[{CJK_RANGE}]|[A-Za-z0-9][A-Za-z0-9_.-]*|[^\s]")
ASCII_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CJK_RUN_RE = re.compile(rf"[{CJK_RANGE}]+")
IDENTIFIER_RE = re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+\b")
DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{4}年\d{1,2}月\d{1,2}日")
COMPANY_LABELED_RE = re.compile(
    rf"(?:甲方|乙方|供应商|公司|单位)[：:\s]*([{CJK_RANGE}]{{2,24}}(?:有限公司|有限责任公司|集团|研究院|中心))"
)
COMPANY_RE = re.compile(rf"[{CJK_RANGE}]{{2,24}}(?:有限公司|有限责任公司|集团|研究院|中心)")
PERSON_RE = re.compile(
    rf"(?:负责人|复核人|审批人|联系人|姓名)[：:\s]*([{CJK_RANGE}]{{2,4}}?)(?=于|[，,；;。.!！\s]|$)"
)
FILENAME_RE = re.compile(r"[^/\\\s]+\.[A-Za-z0-9]{1,8}\b")
ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,10}(?![A-Za-z0-9])")
SENTENCE_END_RE = re.compile(r".*?(?:[。！？!?；;]\s*|\n+|$)", re.S)


@dataclass(frozen=True)
class SourceDocument:
    kb_id: str
    doc_id: str
    title: str
    filename: str
    text: str
    locator: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Parent:
    parent_id: str
    kb_id: str
    doc_id: str
    ordinal: int
    text: str
    token_count: int
    locator: dict
    underfilled: bool


@dataclass(frozen=True)
class Child:
    chunk_id: str
    parent_id: str
    kb_id: str
    doc_id: str
    ordinal: int
    title: str
    section_path: tuple[str, ...]
    body: str
    token_count: int
    overlap_tokens: int
    forced_split: bool
    locator: dict
    identifiers: tuple[str, ...]
    date_values: tuple[str, ...]
    company_names: tuple[str, ...]
    person_names: tuple[str, ...]
    filenames: tuple[str, ...]
    acronyms: tuple[str, ...]


@dataclass
class LocalIndex:
    analyzer: str
    body_in_source: bool
    postings: dict[str, dict[str, int]]
    chunk_terms: dict[str, dict[str, int]]
    chunk_lengths: dict[str, int]
    children: dict[str, Child]
    source: dict[str, dict]
    terms: int
    posting_count: int
    serialized_bytes: int
    postings_bytes: int
    source_bytes: int
    build_time_ms: float
    peak_rss_bytes: int


def structural_tokens(text: str) -> tuple[str, ...]:
    """Stable token-counting units used only for projection geometry."""
    return tuple(m.group(0) for m in TOKEN_RE.finditer(text))


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _slice_by_tokens(text: str, start: int, end: int) -> str:
    spans = _token_spans(text)
    if not spans or start >= len(spans):
        return ""
    end = min(end, len(spans))
    return text[spans[start][0]:spans[end - 1][1]]


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join((prefix, *(str(p) for p in parts)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_template_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def deduplicate_templates(docs: Sequence[SourceDocument]) -> tuple[list[SourceDocument], dict]:
    """Remove exact repeated boilerplate lines seen in at least three docs.

    Headings, table rows and short labels are retained.  The operation is
    corpus-local, deterministic and reports hashes/counts instead of content.
    """
    owners: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        for line in set(_normalize_template_line(x) for x in doc.text.splitlines()):
            if 12 <= len(line) <= 160 and not line.startswith("#") and "｜" not in line:
                owners[line].add(doc.doc_id)
    templates = {line for line, ids in owners.items() if len(ids) >= 3}
    removed = 0
    out: list[SourceDocument] = []
    for doc in docs:
        lines = []
        for raw in doc.text.splitlines(keepends=True):
            if _normalize_template_line(raw) in templates:
                removed += 1
            else:
                lines.append(raw)
        text = "".join(lines).strip()
        out.append(SourceDocument(doc.kb_id, doc.doc_id, doc.title, doc.filename,
                                  text or doc.text, doc.locator))
    return out, {
        "template_count": len(templates),
        "removed_line_instances": removed,
        "template_hashes": sorted(hashlib.sha256(x.encode()).hexdigest()[:16] for x in templates),
    }


def _structural_units(text: str) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        boundary = stripped.startswith("#") or "｜" in stripped or not stripped
        if boundary and current:
            units.append("".join(current))
            current = []
        if stripped:
            if boundary:
                units.append(line)
            else:
                current.append(line)
    if current:
        units.append("".join(current))
    return [u for u in units if u.strip()]


def _split_oversized(text: str, max_tokens: int, overlap: int = 0) -> list[tuple[str, int, bool]]:
    count = len(structural_tokens(text))
    if count <= max_tokens:
        return [(text, 0, False)]
    parts: list[tuple[str, int, bool]] = []
    start = 0
    while start < count:
        end = min(count, start + max_tokens)
        part = _slice_by_tokens(text, start, end)
        parts.append((part, overlap if start else 0, True))
        if end == count:
            break
        start = end - overlap
    return parts


def build_parents(doc: SourceDocument) -> list[Parent]:
    units: list[str] = []
    for unit in _structural_units(doc.text):
        units.extend(p[0] for p in _split_oversized(unit, PARENT_MAX_TOKENS))
    groups: list[list[str]] = []
    current: list[str] = []
    count = 0
    for unit in units:
        n = len(structural_tokens(unit))
        if current and count + n > PARENT_TARGET_TOKENS and count >= PARENT_MIN_TOKENS:
            groups.append(current)
            current, count = [], 0
        if current and count + n > PARENT_MAX_TOKENS:
            groups.append(current)
            current, count = [], 0
        current.append(unit)
        count += n
    if current:
        if groups:
            tail = sum(len(structural_tokens(x)) for x in current)
            prev = sum(len(structural_tokens(x)) for x in groups[-1])
            if tail < PARENT_MIN_TOKENS and prev + tail <= PARENT_MAX_TOKENS:
                groups[-1].extend(current)
            else:
                groups.append(current)
        else:
            groups.append(current)
    parents: list[Parent] = []
    for ordinal, group in enumerate(groups):
        text = "".join(group).strip()
        n = len(structural_tokens(text))
        parents.append(Parent(
            parent_id=_stable_id("cwk.parent.v1", doc.kb_id, doc.doc_id, ordinal, CHUNKER_VERSION),
            kb_id=doc.kb_id, doc_id=doc.doc_id, ordinal=ordinal, text=text,
            token_count=n, locator=dict(doc.locator), underfilled=n < PARENT_MIN_TOKENS,
        ))
    return parents


def _sentences(text: str) -> list[str]:
    return [m.group(0) for m in SENTENCE_END_RE.finditer(text) if m.group(0).strip()]


def extract_exact_fields(text: str, filename: str = "") -> dict[str, tuple[str, ...]]:
    names = set(PERSON_RE.findall(text))
    labeled_companies = set(COMPANY_LABELED_RE.findall(text))
    companies = labeled_companies or set(COMPANY_RE.findall(text))
    filenames = set(FILENAME_RE.findall(text))
    if filename:
        filenames.add(filename)
    return {
        "identifiers": tuple(sorted({x.upper() for x in IDENTIFIER_RE.findall(text)})),
        "date_values": tuple(sorted(set(DATE_RE.findall(text)))),
        "company_names": tuple(sorted(companies)),
        "person_names": tuple(sorted(names)),
        "filenames": tuple(sorted(filenames)),
        "acronyms": tuple(sorted(set(ACRONYM_RE.findall(text)))),
    }


def build_children(parent: Parent, title: str, filename: str) -> list[Child]:
    pieces: list[tuple[str, int, bool]] = []
    for sentence in _sentences(parent.text):
        pieces.extend(_split_oversized(sentence, CHILD_SOFT_MAX_TOKENS, FORCED_OVERLAP_TOKENS))
    groups: list[list[tuple[str, int, bool]]] = []
    current: list[tuple[str, int, bool]] = []
    count = 0
    for piece in pieces:
        n = len(structural_tokens(piece[0]))
        if current and count + n > CHILD_SOFT_MAX_TOKENS and count >= CHILD_MIN_TOKENS:
            groups.append(current)
            current, count = [], 0
        if current and count + n > CHILD_MAX_TOKENS:
            groups.append(current)
            current, count = [], 0
        current.append(piece)
        count += n
        if count >= CHILD_TARGET_TOKENS:
            groups.append(current)
            current, count = [], 0
    if current:
        tail = sum(len(structural_tokens(x[0])) for x in current)
        prev = sum(len(structural_tokens(x[0])) for x in groups[-1]) if groups else 0
        if groups and tail < CHILD_MIN_TOKENS and prev + tail <= CHILD_MAX_TOKENS:
            groups[-1].extend(current)
        else:
            groups.append(current)
    section = tuple(re.sub(r"^#+\s*", "", x.strip()) for x in parent.text.splitlines()
                    if x.strip().startswith("#"))
    children: list[Child] = []
    for ordinal, group in enumerate(groups):
        body = "".join(x[0] for x in group).strip()
        n = len(structural_tokens(body))
        overlap = max((x[1] for x in group), default=0)
        forced = any(x[2] for x in group)
        exact = extract_exact_fields(body + "\n" + title, filename)
        children.append(Child(
            chunk_id=_stable_id("cwk.child.v1", parent.parent_id, ordinal, CHUNKER_VERSION),
            parent_id=parent.parent_id, kb_id=parent.kb_id, doc_id=parent.doc_id,
            ordinal=ordinal, title=title, section_path=section, body=body,
            token_count=n, overlap_tokens=overlap, forced_split=forced,
            locator=dict(parent.locator), **exact,
        ))
    return children


def project_documents(docs: Sequence[SourceDocument]) -> tuple[list[Parent], list[Child], dict]:
    cleaned, dedup = deduplicate_templates(docs)
    parents: list[Parent] = []
    children: list[Child] = []
    for doc in cleaned:
        doc_parents = build_parents(doc)
        parents.extend(doc_parents)
        for parent in doc_parents:
            children.extend(build_children(parent, doc.title, doc.filename))
    geometry = {
        "parent": {"min_tokens": PARENT_MIN_TOKENS, "target_tokens": PARENT_TARGET_TOKENS,
                   "max_tokens": PARENT_MAX_TOKENS, "count": len(parents),
                   "underfilled_short_source": sum(p.underfilled for p in parents),
                   "observed_max_tokens": max((p.token_count for p in parents), default=0)},
        "child": {"min_tokens": CHILD_MIN_TOKENS, "target_tokens": CHILD_TARGET_TOKENS,
                  "soft_max_tokens": CHILD_SOFT_MAX_TOKENS, "max_tokens": CHILD_MAX_TOKENS,
                  "count": len(children),
                  "underfilled_short_parent": sum(c.token_count < CHILD_MIN_TOKENS for c in children),
                  "observed_max_tokens": max((c.token_count for c in children), default=0)},
        "overlap": {"default_tokens": 0, "forced_split_tokens": FORCED_OVERLAP_TOKENS,
                    "forced_split_range": [30, 50],
                    "forced_children": sum(c.forced_split for c in children)},
        "template_dedup": dedup,
    }
    return parents, children, geometry


def _scan_runs(text: str, cjk: Callable[[str], Iterable[str]]) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    i = 0
    while i < len(text):
        cm = CJK_RUN_RE.match(text, i)
        if cm:
            out.extend(cjk(cm.group(0)))
            i = cm.end()
            continue
        am = ASCII_RE.match(text, i)
        if am:
            out.append(am.group(0).lower())
            i = am.end()
            continue
        i += 1
    return tuple(out)


def analyze_legacy(text: str) -> tuple[str, ...]:
    def grams(run: str) -> Iterable[str]:
        for i in range(len(run)):
            yield run[i]
            if i + 2 <= len(run):
                yield run[i:i + 2]
            if i + 3 <= len(run):
                yield run[i:i + 3]
    return _scan_runs(text, grams)


def analyze_icu_probe(text: str) -> tuple[str, ...]:
    return _scan_runs(text, lambda run: tuple(run))


def analyze_smartcn_probe(text: str) -> tuple[str, ...]:
    def bigrams(run: str) -> Iterable[str]:
        if len(run) == 1:
            return (run,)
        # This remains an explicitly labelled local probe, not SmartCN parity.
        # Retaining characters as well as adjacent pairs makes short/no-answer
        # fixture-scope contamination observable instead of hiding it behind a
        # probe-specific segmentation miss.
        return (*tuple(run), *(run[i:i + 2] for i in range(len(run) - 1)))
    return _scan_runs(text, bigrams)


ANALYZERS: dict[str, tuple[Callable[[str], tuple[str, ...]], str, str]] = {
    "legacy_123gram": (analyze_legacy, "measured", "existing algorithm executed locally"),
    "icu_equivalent_probe": (analyze_icu_probe, "equivalent_simulation",
                             "stdlib NFKC + CJK unigram probe; no analysis-icu/OpenSearch"),
    "smartcn_equivalent_probe": (analyze_smartcn_probe, "equivalent_simulation",
                                 "stdlib NFKC + CJK unigram/bigram probe; no analysis-smartcn/OpenSearch"),
}


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def build_local_index(children: Sequence[Child], analyzer_name: str,
                      body_in_source: bool) -> LocalIndex:
    analyzer = ANALYZERS[analyzer_name][0]
    started = time.perf_counter_ns()
    postings: dict[str, dict[str, int]] = defaultdict(dict)
    chunk_terms: dict[str, dict[str, int]] = {}
    lengths: dict[str, int] = {}
    source: dict[str, dict] = {}
    for child in children:
        weighted: Counter[str] = Counter(analyzer(child.body))
        weighted.update({term: tf * 2 for term, tf in Counter(analyzer(" ".join(child.section_path))).items()})
        weighted.update({term: tf * 3 for term, tf in Counter(analyzer(child.title)).items()})
        for value in (*child.identifiers, *child.date_values, *child.company_names,
                      *child.person_names, *child.filenames, *child.acronyms):
            weighted["exact:" + unicodedata.normalize("NFKC", value).casefold()] += 12
        local = dict(sorted(weighted.items()))
        chunk_terms[child.chunk_id] = local
        lengths[child.chunk_id] = sum(local.values())
        for term, tf in local.items():
            postings[term][child.chunk_id] = tf
        row = {
            "tenant_id": "synthetic-tenant", "kb_id": child.kb_id,
            "doc_id": child.doc_id, "parent_id": child.parent_id,
            "chunk_id": child.chunk_id, "generation_schema": MAPPING_VERSION,
            "title": child.title, "section_path": list(child.section_path),
            "identifiers": list(child.identifiers), "entity_names": list(child.company_names + child.person_names),
            "date_values": list(child.date_values), "locator": child.locator,
        }
        if body_in_source:
            row["body"] = child.body
        source[child.chunk_id] = row
    postings_dict = {term: dict(sorted(rows.items())) for term, rows in sorted(postings.items())}
    postings_blob = json.dumps({"postings": postings_dict, "lengths": lengths}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")).encode()
    source_blob = json.dumps(source, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    return LocalIndex(analyzer_name, body_in_source, postings_dict, chunk_terms, lengths,
                      {c.chunk_id: c for c in children}, source, len(postings_dict),
                      sum(len(x) for x in postings_dict.values()),
                      len(postings_blob) + len(source_blob), len(postings_blob),
                      len(source_blob), elapsed, _rss_bytes())


def _query_terms(query: str, analyzer: Callable[[str], tuple[str, ...]]) -> tuple[str, ...]:
    terms = list(analyzer(query))
    exact = extract_exact_fields(query)
    for values in exact.values():
        terms.extend("exact:" + unicodedata.normalize("NFKC", x).casefold() for x in values)
    return tuple(dict.fromkeys(terms))


def search_scores(index: LocalIndex, query: str, kb_ids: Sequence[str], top_k: int = 10,
                  doc_id_prefixes: Sequence[str] = ()) -> list[tuple[str, float]]:
    """Rank documents with BM25 statistics computed only over the fixed scope.

    ``kb_ids`` and ``doc_id_prefixes`` define the scoring corpus, not merely a
    post-ranking candidate filter.  This deliberate behavior differs from a
    Lucene term filter on a shared index, whose term statistics stay global.
    """
    analyzer = ANALYZERS[index.analyzer][0]
    terms = _query_terms(query, analyzer)
    if not terms:
        return []
    allowed = set(kb_ids)
    prefixes = tuple(doc_id_prefixes)
    scoped_children = {
        cid for cid, child in index.children.items()
        if child.kb_id in allowed
        and (not prefixes or child.doc_id.startswith(prefixes))
    }
    if not scoped_children:
        return []
    candidates: set[str] = set()
    for term in terms:
        candidates.update(set(index.postings.get(term, ())) & scoped_children)
    n_docs = len(scoped_children)
    avgdl = sum(index.chunk_lengths[cid] for cid in scoped_children) / n_docs
    scores: dict[str, float] = defaultdict(float)
    for cid in candidates:
        dl = index.chunk_lengths[cid] or 1
        local = index.chunk_terms[cid]
        for term in terms:
            tf = local.get(term, 0)
            if not tf:
                continue
            df = len(set(index.postings.get(term, ())) & scoped_children)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            scores[cid] += idf * (tf * 2.5) / (tf + 1.5 * (0.25 + 0.75 * dl / avgdl))
    best_doc: dict[str, float] = {}
    for cid, score in scores.items():
        doc_id = index.children[cid].doc_id
        best_doc[doc_id] = max(score, best_doc.get(doc_id, 0.0))
    return sorted(best_doc.items(), key=lambda x: (-x[1], x[0]))[:top_k]


def search(index: LocalIndex, query: str, kb_ids: Sequence[str], top_k: int = 10,
           doc_id_prefixes: Sequence[str] = ()) -> list[str]:
    return [doc_id for doc_id, _ in search_scores(
        index, query, kb_ids, top_k, doc_id_prefixes)]


def load_stage_a_corpus() -> list[SourceDocument]:
    """Load all repository fixtures backing the frozen 72-case gold."""
    tests = PROJECT / "tests"
    sys.path.insert(0, str(tests))
    from test_rt051_a11_lexical_eval import LIB_A_DOCS, LIB_B_DOCS  # noqa: PLC0415
    docs: list[SourceDocument] = []
    for kb_id, rows in (("liba", LIB_A_DOCS), ("libb", LIB_B_DOCS)):
        for file_id, (filename, body) in sorted(rows.items()):
            docs.append(SourceDocument(kb_id, f"docdb:{file_id}", Path(filename).stem,
                                       filename, body.decode("utf-8"), {"paragraph_start": 1}))
    fixture = json.loads((RT / "evidence" / "stage-a-gold-fixture-20260909.json").read_text())
    for row in fixture["documents"]:
        evidence = row["evidence_units"][0]
        docs.append(SourceDocument(row["kb_id"], row["doc_id"], row["doc_id"],
                                   row["doc_id"] + ".md", row["search_text"],
                                   evidence["locator"]))
    return docs


def evaluate_gold(index: LocalIndex, gold: dict) -> dict:
    rows: list[dict] = []
    quality_hits = quality_total = exact_hits = exact_total = 0
    no_answer_ok = no_answer_total = 0
    permission_leaks = 0
    for case in gold["cases"]:
        base = {"id": case["id"], "category": case["category"],
                "expected_outcome": case["expected_outcome"]}
        outcome = case["expected_outcome"]
        scope = gold["fixture_scopes"][case["fixture_scope"]]
        prefixes = scope["doc_id_prefixes"]
        if outcome in {"hits", "no_evidence"}:
            ranked = search(index, case["query"], [case["kb_id"]], 10, prefixes)
            expected = case["expected_doc_ids"]
            rank = next((rank for rank, doc in enumerate(ranked, 1) if doc in expected), None)
            if outcome == "hits":
                quality_total += 1
                quality_hits += int(rank is not None)
                if case["category"] == "exact_identifier":
                    exact_total += 1
                    exact_hits += int(rank is not None)
                rows.append({**base, "status": "measured", "rank": rank,
                             "recall_at_10": rank is not None, "returned_doc_ids": ranked})
            else:
                ok = not ranked
                no_answer_total += 1
                no_answer_ok += int(ok)
                rows.append({**base, "status": "measured", "rank": None,
                             "honest_no_evidence": ok, "returned_doc_ids": ranked})
        elif case["id"] == "G32":
            ranked = search(index, case["query"], ["liba"], 10, prefixes)
            leak = "docdb:701" in ranked
            permission_leaks += int(leak)
            rows.append({**base, "status": "measured", "leak": leak,
                         "returned_doc_ids": ranked,
                         "note": "local kb filter probe; no token/Gateway behavior"})
        elif case["id"] == "G33":
            ranked = search(index, case["query"], ["libb"], 10, prefixes)
            hit = "docdb:701" in ranked
            rows.append({**base, "status": "measured", "authorized_hit": hit,
                         "returned_doc_ids": ranked,
                         "note": "local multi-kb scope probe; no token/Gateway behavior"})
        else:
            rows.append({**base, "status": "SKIP", "excluded_from_quality_denominator": True,
                         "reason": "requires legacy Gateway/token/read/index-fault behavior; Stage B is detached offline projection"})
    return {
        "quality_denominator": quality_total,
        "quality_hits": quality_hits,
        "macro_document_recall_at_10": quality_hits / quality_total if quality_total else None,
        "exact_identifier_denominator": exact_total,
        "exact_identifier_hits": exact_hits,
        "exact_identifier_recall_at_10": exact_hits / exact_total if exact_total else None,
        "no_answer_total": no_answer_total, "no_answer_honest": no_answer_ok,
        "permission_leaks": permission_leaks,
        "measured_cases": sum(r["status"] == "measured" for r in rows),
        "skipped_cases": sum(r["status"] == "SKIP" for r in rows),
        "cases": rows,
    }


def _worker(analyzer: str, body_in_source: bool) -> dict:
    docs = load_stage_a_corpus()
    parents, children, geometry = project_documents(docs)
    index = build_local_index(children, analyzer, body_in_source)
    gold = json.loads((RT / "evidence" / "stage-a-gold-20260909.json").read_text())
    quality = evaluate_gold(index, gold)
    evidence = ANALYZERS[analyzer]
    return {
        "analyzer": analyzer, "evidence_level": evidence[1], "evidence_note": evidence[2],
        "source_model": "body_in_source" if body_in_source else "body_excluded_from_source",
        "metrics": {"index_bytes": index.serialized_bytes, "terms": index.terms,
                    "postings": index.posting_count, "chunks": len(children), "docs": len(docs),
                    "parents": len(parents), "postings_bytes": index.postings_bytes,
                    "source_bytes": index.source_bytes, "build_time_ms": round(index.build_time_ms, 3),
                    "peak_rss_bytes": index.peak_rss_bytes},
        "quality": quality, "geometry": geometry,
    }


def _run_worker(analyzer: str, body: bool) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--analyzer", analyzer]
    if body:
        cmd.append("--body-in-source")
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, check=False,
                          env={**os.environ, "PYTHONHASHSEED": "0"})
    if proc.returncode:
        raise RuntimeError(f"worker {analyzer}/{body} failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout)


def run_benchmark() -> dict:
    runs = [_run_worker(name, body) for name in ANALYZERS for body in (False, True)]
    legacy = next(r for r in runs if r["analyzer"] == "legacy_123gram" and
                  r["source_model"] == "body_excluded_from_source")
    for run in runs:
        run["relative_to_legacy_same_corpus"] = {
            "index_bytes_ratio": round(run["metrics"]["index_bytes"] / legacy["metrics"]["index_bytes"], 6),
            "reduction_percent": round((1 - run["metrics"]["index_bytes"] /
                                        legacy["metrics"]["index_bytes"]) * 100, 3),
        }
    candidate_runs = [r for r in runs if r["analyzer"] != "legacy_123gram" and
                      r["source_model"] == "body_excluded_from_source"]
    candidate_quality = all(r["quality"]["macro_document_recall_at_10"] >= 0.90 and
                            r["quality"]["exact_identifier_recall_at_10"] == 1.0 and
                            r["quality"]["permission_leaks"] == 0 for r in candidate_runs)
    directional_reduction = all(r["relative_to_legacy_same_corpus"]["reduction_percent"] >= 80
                                for r in candidate_runs)
    return {
        "schema": "cwk.rt054.stage-b-benchmark.v1", "generated_at": "2026-09-09",
        "scope": "repository synthetic fixtures only; local offline; no Gateway/NAS/OPS/network",
        "gold_cases_consumed": 72, "chunker_version": CHUNKER_VERSION,
        "mapping_version": MAPPING_VERSION, "runs": runs,
        "permission_cross_kb": {"leaks": max(r["quality"]["permission_leaks"] for r in runs),
                                "scope": "local kb_id filter only"},
        "gates": {
            "synthetic_quality": {"pass": candidate_quality, "threshold": "macro >=0.90; exact=1.00; leaks=0"},
            "same_corpus_directional_size": {"pass": directional_reduction,
                                              "threshold": "candidate serialized PoC bytes >=80% below legacy PoC"},
            "three_library_opensearch_primary_store": {"pass": False,
                "reason": "not run: approved three-library equivalent corpus and OpenSearch plugins/service unavailable/in scope prohibited"},
            "real_icu_plugin": {"pass": False, "reason": "analysis-icu not installed or executed"},
            "real_smartcn_plugin": {"pass": False, "reason": "analysis-smartcn not installed or executed"},
        },
        "stage_b_decision": "NO-GO",
        "decision_reason": "directional synthetic PoC cannot prove the mandatory three-library OpenSearch primary-store reduction or real plugin quality",
        "measurement_limits": [
            "index_bytes is deterministic serialized PoC postings + _source projection, not OpenSearch primary store",
            "peak RSS is isolated worker ru_maxrss, not OpenSearch JVM heap/RSS",
            "ICU and SmartCN rows are equivalent simulations, not plugin measurements",
            "SKIP cases are excluded from quality denominators and keep their reasons per case",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RT-054 Stage B local analyzer PoC")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--analyzer", choices=tuple(ANALYZERS))
    parser.add_argument("--body-in-source", action="store_true")
    args = parser.parse_args(argv)
    if args.worker:
        if not args.analyzer:
            parser.error("--worker requires --analyzer")
        print(json.dumps(_worker(args.analyzer, args.body_in_source), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")))
        return 0
    report = run_benchmark()
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
