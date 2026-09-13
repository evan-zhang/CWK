#!/usr/bin/env python3
"""RT-054 Stage B real OpenSearch benchmark over the frozen 50-doc fixture.

The harness talks to one explicitly supplied loopback OpenSearch endpoint using
only Python's standard library.  It measures two deliberately separate lanes:

* shared-filter: all 50 docs, three analyzers, body in/excluded from ``_source``;
* isolated-scope: one physical index per fixture scope/analyzer, body excluded.

The shared lane's fixture_scope term filter limits candidates only: Lucene
BM25 term statistics remain global to the physical index.  The isolated lane
exists solely to measure the original review corpus with per-index statistics.
Every index is deleted in ``finally``.  The report stays NO-GO while the three
target-library equivalent corpus has not been measured.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import kb_stage_b_poc as poc  # noqa: E402

RT = PROJECT / "RT" / "RT-054"
EXPECTED_DOCS = 50
ANALYZERS = ("legacy_123gram", "analysis_icu", "analysis_smartcn")
SOURCE_MODELS = ("body_excluded_from_source", "body_in_source")
FIXTURE_SCOPES = ("rt051_a11", "rt054_extension")
INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,180}$")
IDENTIFIER_QUERY_RE = re.compile(r"^(?:\d+|[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+)$")
DATE_QUERY_RE = re.compile(r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)$")


class OpenSearchError(RuntimeError):
    """A bounded OpenSearch REST call failed."""


class OpenSearchClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("benchmark endpoint must be an unauthenticated loopback http URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("benchmark URL must not contain credentials, query, or fragment")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Any | None = None,
                *, content_type: str = "application/json") -> tuple[Any, int]:
        body: bytes | None
        if payload is None:
            body = None
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Accept": "application/json", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", "replace")
            finally:
                exc.close()
            raise OpenSearchError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise OpenSearchError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        if not raw:
            return {}, 0
        try:
            return json.loads(raw), len(raw)
        except json.JSONDecodeError as exc:
            raise OpenSearchError(f"{method} {path} returned non-JSON ({len(raw)} bytes)") from exc


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[rank], 3)


def mapping_for(analyzer: str, body_in_source: bool) -> dict[str, Any]:
    if analyzer not in ANALYZERS:
        raise ValueError(f"unsupported analyzer: {analyzer}")
    source: dict[str, Any] = {"enabled": True}
    if not body_in_source:
        source["excludes"] = ["body"]
    if analyzer == "legacy_123gram":
        analysis = {
            "char_filter": {
                "cwk_cjk_only": {
                    "type": "pattern_replace", "pattern": "[^\\p{IsHan}]", "replacement": " "
                },
                "cwk_ascii_only": {
                    "type": "pattern_replace", "pattern": "[^A-Za-z0-9_.-]", "replacement": " "
                },
            },
            "filter": {
                "cwk_legacy_123gram": {"type": "ngram", "min_gram": 1, "max_gram": 3}
            },
            "analyzer": {
                "cwk_legacy_cjk": {
                    "type": "custom", "char_filter": ["cwk_cjk_only"],
                    "tokenizer": "whitespace", "filter": ["cwk_legacy_123gram"],
                },
                "cwk_legacy_ascii": {
                    "type": "custom", "char_filter": ["cwk_ascii_only"],
                    "tokenizer": "whitespace", "filter": ["lowercase"],
                },
            },
        }
        text_field = {
            "type": "text", "analyzer": "cwk_legacy_cjk",
            "fields": {"ascii": {"type": "text", "analyzer": "cwk_legacy_ascii"}},
        }
    elif analyzer == "analysis_icu":
        analysis = {
            "analyzer": {
                "cwk_official_icu": {
                    "type": "custom", "tokenizer": "icu_tokenizer",
                    "filter": ["icu_normalizer", "lowercase"],
                }
            }
        }
        text_field = {"type": "text", "analyzer": "cwk_official_icu"}
    else:
        analysis = {"analyzer": {"cwk_official_smartcn": {"type": "smartcn"}}}
        text_field = {"type": "text", "analyzer": "cwk_official_smartcn"}
    properties: dict[str, Any] = {
        "tenant_id": {"type": "keyword"},
        "kb_id": {"type": "keyword"},
        "fixture_scope": {"type": "keyword"},
        "doc_id": {"type": "keyword"},
        "source_version": {"type": "long"},
        "parent_id": {"type": "keyword"},
        "chunk_id": {"type": "keyword"},
        "generation_schema": {"type": "keyword"},
        "title": text_field,
        "section_path": text_field,
        "body": text_field,
        "identifiers": {"type": "keyword"},
        "entity_names": {"type": "keyword"},
        "date_values": {"type": "keyword"},
        "filenames": {"type": "keyword"},
        "acronyms": {"type": "keyword"},
        "locator": {
            "type": "object", "dynamic": "strict", "properties": {
                "page_start": {"type": "integer"}, "page_end": {"type": "integer"},
                "sheet_name": {"type": "keyword"},
                "row_start": {"type": "integer"}, "row_end": {"type": "integer"},
                "paragraph_start": {"type": "integer"}, "paragraph_end": {"type": "integer"},
                "section_path": {"type": "keyword"},
            },
        },
    }
    return {
        "settings": {
            "number_of_shards": 1, "number_of_replicas": 0,
            "refresh_interval": "-1", "max_ngram_diff": 2, "analysis": analysis,
        },
        "mappings": {"dynamic": "strict", "_source": source, "properties": properties},
    }


def fixture_scope(doc_id: str) -> str:
    if doc_id.startswith("docdb:"):
        return "rt051_a11"
    if doc_id.startswith("synthetic:"):
        return "rt054_extension"
    raise ValueError(f"document is outside frozen fixture scopes: {doc_id}")


def project_fixture() -> tuple[list[poc.Parent], list[poc.Child], dict[str, Any]]:
    docs = poc.load_stage_a_corpus()
    if len(docs) != EXPECTED_DOCS:
        raise ValueError(f"frozen fixture must contain {EXPECTED_DOCS} docs, got {len(docs)}")
    parents, children, geometry = poc.project_documents(docs)
    if {child.doc_id for child in children} != {doc.doc_id for doc in docs}:
        raise ValueError("Parent/Child projection lost or added a frozen fixture document")
    return parents, children, geometry


def child_document(child: poc.Child) -> dict[str, Any]:
    return {
        "tenant_id": "synthetic-tenant", "kb_id": child.kb_id,
        "fixture_scope": fixture_scope(child.doc_id), "doc_id": child.doc_id,
        "source_version": 1, "parent_id": child.parent_id, "chunk_id": child.chunk_id,
        "generation_schema": poc.MAPPING_VERSION, "title": child.title,
        "section_path": list(child.section_path), "body": child.body,
        "identifiers": list(child.identifiers),
        "entity_names": list(child.company_names + child.person_names),
        "date_values": list(child.date_values), "filenames": list(child.filenames),
        "acronyms": list(child.acronyms), "locator": child.locator,
    }


def bulk_payload(index_name: str, children: Sequence[poc.Child]) -> bytes:
    lines: list[bytes] = []
    for child in children:
        action = {"index": {"_index": index_name, "_id": child.chunk_id}}
        lines.append(json.dumps(action, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        lines.append(json.dumps(child_document(child), ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
    return b"\n".join(lines) + b"\n"


def query_body(analyzer: str, case: dict[str, Any], scope: str) -> dict[str, Any]:
    query = case.get("query", "").strip()
    filters = [
        {"term": {"kb_id": case["kb_id"]}},
        {"term": {"fixture_scope": scope}},
    ]
    should: list[dict[str, Any]] = []
    if DATE_QUERY_RE.fullmatch(query):
        should.append({"term": {"date_values": {"value": query, "boost": 12}}})
    elif IDENTIFIER_QUERY_RE.fullmatch(query):
        should.append({"term": {"identifiers": {"value": query.upper(), "boost": 12}}})
    else:
        if analyzer == "legacy_123gram":
            fields = ["title^3", "title.ascii^3", "section_path^2", "section_path.ascii^2",
                      "body", "body.ascii"]
        else:
            fields = ["title^3", "section_path^2", "body"]
        should.append({"multi_match": {"query": query, "fields": fields,
                                        "type": "best_fields", "operator": "or"}})
        exact = poc.extract_exact_fields(query)
        for value in exact["company_names"] + exact["person_names"]:
            should.append({"term": {"entity_names": {"value": value, "boost": 12}}})
        for value in exact["acronyms"]:
            should.append({"term": {"acronyms": {"value": value, "boost": 12}}})
    return {
        "size": 10, "track_total_hits": True, "_source": ["doc_id"],
        "query": {"bool": {"filter": filters, "should": should, "minimum_should_match": 1}},
        "collapse": {"field": "doc_id"},
        "sort": [{"_score": "desc"}, {"doc_id": "asc"}],
    }


def search_case(client: OpenSearchClient, index_name: str, analyzer: str,
                case: dict[str, Any], scope: str) -> tuple[list[str], float, int]:
    started = time.perf_counter_ns()
    response, response_bytes = client.request(
        "POST", f"/{index_name}/_search", query_body(analyzer, case, scope))
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    hits = response.get("hits", {}).get("hits", [])
    return [hit.get("_source", {}).get("doc_id") for hit in hits], elapsed_ms, response_bytes


def evaluate_gold(client: OpenSearchClient, index_name: str, analyzer: str,
                  gold: dict[str, Any], repeats: int,
                  fixture_scope_only: str | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    quality_hits = quality_total = exact_hits = exact_total = 0
    no_answer_ok = no_answer_total = permission_leaks = 0
    authorized_probe_ok = authorized_probe_total = 0
    cases = [case for case in gold["cases"]
             if fixture_scope_only is None or case["fixture_scope"] == fixture_scope_only]
    for case in cases:
        outcome = case["expected_outcome"]
        base = {"id": case["id"], "category": case["category"],
                "expected_outcome": outcome, "fixture_scope": case["fixture_scope"]}
        if outcome in {"hits", "no_evidence"} or case["id"] in {"G32", "G33"}:
            effective_case = case
            if case["id"] in {"G32", "G33"}:
                effective_case = {**case, "kb_id": case["action"]["requested_kb"]}
            ranked, first_ms, response_bytes = search_case(
                client, index_name, analyzer, effective_case, case["fixture_scope"])
            timings = [first_ms]
            for _ in range(repeats - 1):
                again, elapsed, _ = search_case(
                    client, index_name, analyzer, effective_case, case["fixture_scope"])
                if again != ranked:
                    raise OpenSearchError(f"non-deterministic ranking for {case['id']}: {ranked} != {again}")
                timings.append(elapsed)
            latencies.extend(timings)
            if outcome == "hits":
                expected = case["expected_doc_ids"]
                rank = next((rank for rank, doc_id in enumerate(ranked, 1)
                             if doc_id in expected), None)
                quality_total += 1
                quality_hits += int(rank is not None)
                if case["category"] == "exact_identifier":
                    exact_total += 1
                    exact_hits += int(rank is not None)
                rows.append({**base, "status": "measured", "rank": rank,
                             "recall_at_10": rank is not None, "returned_doc_ids": ranked,
                             "latency_ms": [round(x, 3) for x in timings],
                             "response_bytes": response_bytes})
            elif outcome == "no_evidence":
                ok = not ranked
                no_answer_total += 1
                no_answer_ok += int(ok)
                rows.append({**base, "status": "measured", "rank": None,
                             "honest_no_evidence": ok, "returned_doc_ids": ranked,
                             "latency_ms": [round(x, 3) for x in timings],
                             "response_bytes": response_bytes})
            elif case["id"] == "G32":
                leak = "docdb:701" in ranked
                permission_leaks += int(leak)
                rows.append({**base, "status": "measured", "leak": leak,
                             "returned_doc_ids": ranked,
                             "latency_ms": [round(x, 3) for x in timings],
                             "note": "OpenSearch kb_id + fixture_scope filter; no token/Gateway behavior"})
            else:
                hit = "docdb:701" in ranked
                authorized_probe_total += 1
                authorized_probe_ok += int(hit)
                rows.append({**base, "status": "measured", "authorized_hit": hit,
                             "returned_doc_ids": ranked,
                             "latency_ms": [round(x, 3) for x in timings],
                             "note": "OpenSearch kb_id + fixture_scope filter; no token/Gateway behavior"})
        else:
            rows.append({**base, "status": "SKIP", "excluded_from_quality_denominator": True,
                         "reason": "requires legacy Gateway/token/read/index-fault behavior; real Stage B harness measures detached OpenSearch retrieval"})
    return {
        "quality_denominator": quality_total, "quality_hits": quality_hits,
        "macro_document_recall_at_10": quality_hits / quality_total if quality_total else None,
        "exact_identifier_denominator": exact_total, "exact_identifier_hits": exact_hits,
        "exact_identifier_recall_at_10": exact_hits / exact_total if exact_total else None,
        "no_answer_total": no_answer_total, "no_answer_honest": no_answer_ok,
        "permission_leaks": permission_leaks,
        "authorized_probe_total": authorized_probe_total,
        "authorized_probe_ok": authorized_probe_ok,
        "measured_cases": sum(row["status"] == "measured" for row in rows),
        "skipped_cases": sum(row["status"] == "SKIP" for row in rows),
        "latency_ms": {
            "samples": len(latencies), "p50": round(statistics.median(latencies), 3),
            "p95": percentile(latencies, 0.95), "p99": percentile(latencies, 0.99),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "cases": rows,
    }


def term_counts(client: OpenSearchClient, index_name: str, analyzer: str,
                children: Sequence[poc.Child]) -> dict[str, int]:
    """Count unique tokens through each index's real analyzer, independent of _source."""
    fields = ["body", "title", "section_path", "identifiers", "entity_names", "date_values"]
    if analyzer == "legacy_123gram":
        fields += ["body.ascii", "title.ascii", "section_path.ascii"]
    observed: dict[str, set[str]] = {field: set() for field in fields}
    for field in fields:
        texts: list[str]
        if field.startswith("body"):
            texts = [child.body for child in children]
        elif field.startswith("title"):
            texts = [child.title for child in children]
        elif field.startswith("section_path"):
            texts = [" ".join(child.section_path) for child in children]
        else:
            continue
        response, _ = client.request(
            "POST", f"/{index_name}/_analyze", {"field": field, "text": texts})
        observed[field].update(token["token"] for token in response.get("tokens", []))
    observed["identifiers"].update(value for child in children for value in child.identifiers)
    observed["entity_names"].update(value for child in children
                                    for value in child.company_names + child.person_names)
    observed["date_values"].update(value for child in children for value in child.date_values)
    return {field: len(values) for field, values in observed.items()}


def primary_stats(client: OpenSearchClient, index_name: str) -> tuple[int, int]:
    response, _ = client.request(
        "GET", f"/_cat/indices/{index_name}?format=json&bytes=b&h=index,docs.count,pri.store.size")
    if not isinstance(response, list) or len(response) != 1:
        raise OpenSearchError(f"unexpected _cat indices response for {index_name}: {response!r}")
    return int(response[0]["pri.store.size"]), int(response[0]["docs.count"])


def source_retrieval(client: OpenSearchClient, index_name: str,
                     expected_docs: int) -> dict[str, Any]:
    started = time.perf_counter_ns()
    response, response_bytes = client.request(
        "POST", f"/{index_name}/_search",
        {"size": expected_docs, "sort": [{"doc_id": "asc"}], "query": {"match_all": {}}})
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    hits = response.get("hits", {}).get("hits", [])
    return {
        "documents_returned": len(hits), "response_bytes": response_bytes,
        "latency_ms": round(elapsed_ms, 3),
        "body_fields_returned": sum("body" in hit.get("_source", {}) for hit in hits),
    }


def term_statistics_evidence(client: OpenSearchClient, index_name: str,
                             child: poc.Child) -> dict[str, Any]:
    """Capture Lucene field/term statistics for one indexed body term."""
    response, _ = client.request(
        "POST", f"/{index_name}/_termvectors",
        {"doc": child_document(child), "fields": ["body"],
         "field_statistics": True, "term_statistics": True,
         "positions": False, "offsets": False, "payloads": False})
    body = response.get("term_vectors", {}).get("body", {})
    terms = body.get("terms", {})
    if not terms:
        raise OpenSearchError(f"termvectors returned no body terms for {index_name}")
    term = sorted(terms)[0]
    row = terms[term]
    return {
        "field": "body", "probe_term": term,
        "field_statistics": body.get("field_statistics", {}),
        "term_statistics": {key: row.get(key) for key in ("doc_freq", "ttf")},
        "evidence_note": "statistics are reported by Lucene for this physical index",
    }


def run_index(client: OpenSearchClient, index_name: str, analyzer: str,
              body_in_source: bool, children: Sequence[poc.Child], gold: dict[str, Any],
              repeats: int, *, lane: str,
              fixture_scope_only: str | None = None) -> dict[str, Any]:
    mapping = mapping_for(analyzer, body_in_source)
    client.request("PUT", f"/{index_name}", mapping)
    build_started = time.perf_counter_ns()
    index_started = time.perf_counter_ns()
    bulk, _ = client.request("POST", "/_bulk?refresh=false", bulk_payload(index_name, children),
                             content_type="application/x-ndjson")
    indexing_time_ms = (time.perf_counter_ns() - index_started) / 1_000_000
    if bulk.get("errors"):
        failures = [item for item in bulk.get("items", []) if item.get("index", {}).get("error")]
        raise OpenSearchError(f"bulk indexing failed for {len(failures)} items")
    refresh_started = time.perf_counter_ns()
    client.request("POST", f"/{index_name}/_refresh")
    refresh_time_ms = (time.perf_counter_ns() - refresh_started) / 1_000_000
    build_time_ms = (time.perf_counter_ns() - build_started) / 1_000_000
    merge_started = time.perf_counter_ns()
    client.request("POST", f"/{index_name}/_forcemerge?max_num_segments=1&flush=true")
    merge_time_ms = (time.perf_counter_ns() - merge_started) / 1_000_000
    primary_store_bytes, docs_count = primary_stats(client, index_name)
    if docs_count != len(children):
        raise OpenSearchError(f"indexed docs mismatch for {index_name}: {docs_count} != {len(children)}")
    quality = evaluate_gold(client, index_name, analyzer, gold, repeats, fixture_scope_only)
    terms = term_counts(client, index_name, analyzer, children)
    retrieval = source_retrieval(client, index_name, len(children))
    term_stats = term_statistics_evidence(client, index_name, children[0])
    mapping_hash = hashlib.sha256(json.dumps(mapping, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
    return {
        "lane": lane, "fixture_scope": fixture_scope_only,
        "analyzer": analyzer,
        "evidence_level": "measured_opensearch_mapping",
        "source_model": "body_in_source" if body_in_source else "body_excluded_from_source",
        "mapping_sha256": mapping_hash,
        "metrics": {
            "primary_store_bytes": primary_store_bytes, "docs": docs_count,
            "chunks": len(children), "build_time_ms": round(build_time_ms, 3),
            "indexing_time_ms": round(indexing_time_ms, 3),
            "refresh_time_ms": round(refresh_time_ms, 3),
            "force_merge_time_ms": round(merge_time_ms, 3),
            "analyzed_unique_terms_by_field": terms,
            "analyzed_field_term_count_sum": sum(terms.values()),
        },
        "source_retrieval": retrieval, "quality": quality,
        "lucene_statistics_evidence": term_stats,
    }


def inspect_environment(client: OpenSearchClient) -> dict[str, Any]:
    root, _ = client.request("GET", "/")
    plugins, _ = client.request("GET", "/_cat/plugins?format=json")
    plugins = sorted(plugins, key=lambda row: (row.get("component", ""), row.get("version", "")))
    plugin_versions = {row["component"]: row["version"] for row in plugins}
    runtime_version = root.get("version", {}).get("number")
    return {
        "distribution": root.get("version", {}).get("distribution"),
        "runtime_version": runtime_version,
        "build_hash": root.get("version", {}).get("build_hash"),
        "lucene_version": root.get("version", {}).get("lucene_version"),
        "cluster_name": root.get("cluster_name"),
        "plugins": plugins,
        "analysis_plugins": {name: plugin_versions[name]
                             for name in ("analysis-icu", "analysis-smartcn")
                             if name in plugin_versions},
        "analysis_icu_available": "analysis-icu" in plugin_versions,
        "analysis_icu_matches_runtime": plugin_versions.get("analysis-icu") == runtime_version,
        "analysis_smartcn_available": "analysis-smartcn" in plugin_versions,
        "analysis_smartcn_matches_runtime": plugin_versions.get("analysis-smartcn") == runtime_version,
    }


def inspect_resources(client: OpenSearchClient) -> dict[str, Any]:
    response, _ = client.request(
        "GET", "/_nodes/stats/jvm,process,os?filter_path="
        "nodes.*.jvm.mem.heap_used_in_bytes,nodes.*.jvm.mem.heap_committed_in_bytes,"
        "nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.jvm.mem.non_heap_used_in_bytes,"
        "nodes.*.process.mem.total_virtual_in_bytes,nodes.*.os.mem.total_in_bytes,"
        "nodes.*.os.mem.used_in_bytes,nodes.*.os.mem.free_in_bytes")
    return {
        "node_stats_point_in_time": response.get("nodes", {}),
        "boundary_note": (
            "JVM heap is OpenSearch node telemetry; process total_virtual is not RSS. "
            "os.mem is the Docker/cgroup-visible boundary, not the macOS host. "
            "Container /proc RSS and host memory, when supplied, are separate observations."
        ),
    }


def validate_report(report: dict[str, Any]) -> None:
    if report.get("schema") != "cwk.rt054.stage-b-opensearch-benchmark.v2":
        raise ValueError("unexpected benchmark schema")
    shared = report.get("lanes", {}).get("shared_filter", {})
    isolated = report.get("lanes", {}).get("isolated_scope", {})
    runs = shared.get("runs")
    if not isinstance(runs, list) or len(runs) != 6:
        raise ValueError("shared-filter lane must contain six analyzer/source runs")
    expected = {(analyzer, source) for analyzer in ANALYZERS for source in SOURCE_MODELS}
    actual = {(run.get("analyzer"), run.get("source_model")) for run in runs}
    if actual != expected:
        raise ValueError(f"shared-filter run matrix mismatch: {actual}")
    for run in runs:
        if run.get("lane") != "shared_filter" or run.get("fixture_scope") is not None:
            raise ValueError("shared-filter run has invalid lane metadata")
        metrics = run.get("metrics", {})
        for key in ("primary_store_bytes", "docs", "chunks", "build_time_ms",
                    "indexing_time_ms", "refresh_time_ms", "force_merge_time_ms",
                    "analyzed_field_term_count_sum"):
            value = metrics.get(key)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"invalid metric {key} for {run.get('analyzer')}: {value!r}")
        if metrics["docs"] != EXPECTED_DOCS or metrics["chunks"] != EXPECTED_DOCS:
            raise ValueError("frozen fixture document/chunk count drifted")
        terms = metrics.get("analyzed_unique_terms_by_field")
        if not isinstance(terms, dict) or not terms or any(not isinstance(v, int) or v < 0 for v in terms.values()):
            raise ValueError("analyzed term-count schema is invalid")
        quality = run.get("quality", {})
        cases = quality.get("cases")
        if not isinstance(cases, list) or len(cases) != 72:
            raise ValueError("each run must contain all 72 gold rows")
        for row in cases:
            if row.get("status") == "SKIP" and not (
                    row.get("excluded_from_quality_denominator") is True and row.get("reason")):
                raise ValueError(f"unstructured SKIP row: {row!r}")
        retrieval = run.get("source_retrieval", {})
        expected_bodies = EXPECTED_DOCS if run["source_model"] == "body_in_source" else 0
        if retrieval.get("documents_returned") != EXPECTED_DOCS or retrieval.get("body_fields_returned") != expected_bodies:
            raise ValueError("_source body model did not produce the expected retrieval shape")
        field_stats = run.get("lucene_statistics_evidence", {}).get("field_statistics", {})
        field_doc_count = field_stats.get("doc_count")
        if not isinstance(field_doc_count, int) or not 0 < field_doc_count <= EXPECTED_DOCS:
            raise ValueError("shared-filter Lucene field statistics are invalid")
    isolated_runs = isolated.get("runs")
    if not isinstance(isolated_runs, list) or len(isolated_runs) != 6:
        raise ValueError("isolated-scope lane must contain six analyzer/scope runs")
    expected_isolated = {(analyzer, scope) for analyzer in ANALYZERS for scope in FIXTURE_SCOPES}
    actual_isolated = {(run.get("analyzer"), run.get("fixture_scope")) for run in isolated_runs}
    if actual_isolated != expected_isolated:
        raise ValueError(f"isolated-scope run matrix mismatch: {actual_isolated}")
    scope_counts = report.get("fixture", {}).get("scope_documents", {})
    scope_cases = {"rt051_a11": 48, "rt054_extension": 24}
    for run in isolated_runs:
        scope = run["fixture_scope"]
        expected_docs = scope_counts.get(scope)
        if run.get("lane") != "isolated_scope" or run.get("source_model") != "body_excluded_from_source":
            raise ValueError("isolated-scope run has invalid lane/source metadata")
        metrics = run.get("metrics", {})
        for key in ("primary_store_bytes", "docs", "chunks", "build_time_ms",
                    "indexing_time_ms", "refresh_time_ms", "force_merge_time_ms",
                    "analyzed_field_term_count_sum"):
            value = metrics.get(key)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"invalid isolated metric {key}: {value!r}")
        if metrics.get("docs") != expected_docs or metrics.get("chunks") != expected_docs:
            raise ValueError("isolated-scope document count drifted")
        if len(run.get("quality", {}).get("cases", [])) != scope_cases[scope]:
            raise ValueError("isolated-scope gold case count drifted")
        retrieval = run.get("source_retrieval", {})
        if retrieval.get("documents_returned") != expected_docs or retrieval.get("body_fields_returned") != 0:
            raise ValueError("isolated-scope _source retrieval shape drifted")
        field_stats = run.get("lucene_statistics_evidence", {}).get("field_statistics", {})
        field_doc_count = field_stats.get("doc_count")
        if not isinstance(field_doc_count, int) or not 0 < field_doc_count <= expected_docs:
            raise ValueError("isolated Lucene field statistics exceed their physical scope")
    semantics = report.get("statistics_semantics", {})
    if semantics.get("shared_filter_uses_global_lucene_statistics") is not True:
        raise ValueError("shared-filter Lucene statistics boundary is missing")
    if semantics.get("isolated_scope_uses_per_scope_lucene_statistics") is not True:
        raise ValueError("isolated-scope Lucene statistics boundary is missing")
    if report.get("stage_b_decision") != "NO-GO":
        raise ValueError("Stage B cannot be GO without the three-library equivalent-corpus gate")
    cleanup = report.get("cleanup", {})
    if cleanup.get("failures") or len(cleanup.get("deleted", [])) != 12:
        raise ValueError("temporary index cleanup evidence is incomplete")


def run_benchmark(base_url: str, index_prefix: str, repeats: int = 3,
                  timeout: float = 30.0, *, expected_runtime_version: str = "",
                  container_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if repeats < 1 or repeats > 20:
        raise ValueError("repeats must be between 1 and 20")
    if not INDEX_NAME_RE.fullmatch(index_prefix):
        raise ValueError("index prefix must be lowercase OpenSearch-safe text")
    client = OpenSearchClient(base_url, timeout)
    environment = inspect_environment(client)
    if environment["distribution"] != "opensearch":
        raise OpenSearchError("endpoint is not OpenSearch")
    if not environment["analysis_icu_available"] or not environment["analysis_icu_matches_runtime"]:
        raise OpenSearchError("official analysis-icu plugin is unavailable or version-mismatched")
    if not environment["analysis_smartcn_available"] or not environment["analysis_smartcn_matches_runtime"]:
        raise OpenSearchError("official analysis-smartcn plugin is unavailable or version-mismatched")
    parents, children, geometry = project_fixture()
    gold = json.loads((RT / "evidence" / "stage-a-gold-20260909.json").read_text(encoding="utf-8"))
    shared_names = [f"{index_prefix}-shared-{analyzer.replace('_', '-')}-{'source' if body else 'nosource'}"
                    for analyzer in ANALYZERS for body in (False, True)]
    isolated_names = [f"{index_prefix}-isolated-{scope.replace('_', '-')}-{analyzer.replace('_', '-')}"
                      for analyzer in ANALYZERS for scope in FIXTURE_SCOPES]
    names = shared_names + isolated_names
    created: list[str] = []
    cleanup: dict[str, Any] = {"attempted": [], "deleted": [], "failures": []}
    shared_runs: list[dict[str, Any]] = []
    isolated_runs: list[dict[str, Any]] = []
    try:
        for analyzer in ANALYZERS:
            for body in (False, True):
                index_name = f"{index_prefix}-shared-{analyzer.replace('_', '-')}-{'source' if body else 'nosource'}"
                created.append(index_name)
                shared_runs.append(run_index(
                    client, index_name, analyzer, body, children, gold, repeats,
                    lane="shared_filter"))
        for analyzer in ANALYZERS:
            for scope in FIXTURE_SCOPES:
                scoped_children = [child for child in children if fixture_scope(child.doc_id) == scope]
                index_name = f"{index_prefix}-isolated-{scope.replace('_', '-')}-{analyzer.replace('_', '-')}"
                created.append(index_name)
                isolated_runs.append(run_index(
                    client, index_name, analyzer, False, scoped_children, gold, repeats,
                    lane="isolated_scope", fixture_scope_only=scope))
    finally:
        for index_name in reversed(created):
            cleanup["attempted"].append(index_name)
            try:
                client.request("DELETE", f"/{index_name}")
                cleanup["deleted"].append(index_name)
            except Exception as exc:  # cleanup must be visible in the artifact
                cleanup["failures"].append({"index": index_name, "error": f"{type(exc).__name__}: {exc}"})
    if cleanup["failures"]:
        raise OpenSearchError(f"temporary index cleanup failed: {cleanup['failures']}")
    resources = inspect_resources(client)
    by_key = {(run["analyzer"], run["source_model"]): run for run in shared_runs}
    comparisons: dict[str, Any] = {}
    for source_model in SOURCE_MODELS:
        legacy = by_key[("legacy_123gram", source_model)]["metrics"]["primary_store_bytes"]
        comparisons[source_model] = {"legacy_primary_store_bytes": legacy, "candidates": {}}
        for analyzer in ("analysis_icu", "analysis_smartcn"):
            size = by_key[(analyzer, source_model)]["metrics"]["primary_store_bytes"]
            comparisons[source_model]["candidates"][analyzer] = {
                "primary_store_bytes": size,
                "to_legacy_ratio": round(size / legacy, 6),
                "reduction_percent": round((1 - size / legacy) * 100, 3),
            }
    legacy = by_key[("legacy_123gram", "body_excluded_from_source")]
    candidates = [by_key[(name, "body_excluded_from_source")]
                  for name in ("analysis_icu", "analysis_smartcn")]
    quality_pass = all(
        candidate["quality"]["macro_document_recall_at_10"] >= 0.90
        and candidate["quality"]["macro_document_recall_at_10"] >= legacy["quality"]["macro_document_recall_at_10"]
        and candidate["quality"]["exact_identifier_recall_at_10"] == 1.0
        and candidate["quality"]["permission_leaks"] == 0
        and candidate["quality"]["authorized_probe_ok"] == candidate["quality"]["authorized_probe_total"]
        for candidate in candidates)
    reduction_pass = all(
        row["reduction_percent"] >= 80
        for row in comparisons["body_excluded_from_source"]["candidates"].values())
    scope_counts = {scope: sum(fixture_scope(child.doc_id) == scope for child in children)
                    for scope in FIXTURE_SCOPES}
    report = {
        "schema": "cwk.rt054.stage-b-opensearch-benchmark.v2",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "repository-owned frozen 50-doc synthetic fixture; loopback OpenSearch only; no Gateway/NAS/OPS",
        "environment": {
            **environment,
            "expected_runtime_version": expected_runtime_version or None,
            "expected_runtime_version_matches": (
                environment["runtime_version"] == expected_runtime_version
                if expected_runtime_version else None
            ),
            "container": container_evidence or None,
            "resources": resources,
        },
        "fixture": {
            "documents": EXPECTED_DOCS, "parents": len(parents), "children": len(children),
            "scope_documents": scope_counts,
            "chunker_version": poc.CHUNKER_VERSION, "mapping_version": poc.MAPPING_VERSION,
            "geometry": geometry,
        },
        "statistics_semantics": {
            "shared_filter_uses_global_lucene_statistics": True,
            "isolated_scope_uses_per_scope_lucene_statistics": True,
            "shared_filter_note": "fixture_scope term filter limits candidates only; Lucene BM25 n/avgdl/df remain global to the shared physical index",
            "isolated_scope_note": "each fixture scope is indexed separately only for review-baseline comparison; these scores are not the shared-index production model",
        },
        "lanes": {
            "shared_filter": {"physical_corpus": "all_50_docs", "runs": shared_runs},
            "isolated_scope": {"physical_corpus": "one_fixture_scope_per_index", "runs": isolated_runs},
        },
        "comparisons": comparisons,
        "gates": {
            "same_50_doc_projection": {"pass": all(run["metrics"]["docs"] == len(children) for run in shared_runs)},
            "official_analysis_icu": {"pass": environment["analysis_icu_available"] and environment["analysis_icu_matches_runtime"]},
            "official_analysis_smartcn": {"pass": environment["analysis_smartcn_available"] and environment["analysis_smartcn_matches_runtime"]},
            "expected_runtime_version": {
                "pass": (environment["runtime_version"] == expected_runtime_version
                         if expected_runtime_version else None),
                "expected": expected_runtime_version or None,
                "observed": environment["runtime_version"],
                "reason": None if not expected_runtime_version or environment["runtime_version"] == expected_runtime_version
                else "image tag/digest resolved to a different OpenSearch runtime version",
            },
            "synthetic_quality": {"pass": quality_pass,
                                  "threshold": "macro Recall@10 >=0.90 and >= legacy; exact=1.00; leaks=0"},
            "same_fixture_primary_store_reduction": {"pass": reduction_pass,
                "threshold": "both ICU and SmartCN >=80% below legacy in shared-filter body-excluded lane"},
            "three_library_equivalent_primary_store": {
                "pass": False,
                "reason": "not measured: frozen repository fixture has two synthetic fixture scopes, not equivalent corpora for cwork-3m/docdb-touqian/spbp-2027",
            },
        },
        "stage_b_decision": "NO-GO",
        "decision_reason": "real 50-doc OpenSearch/ICU evidence cannot satisfy the mandatory three-target-library equivalent-corpus primary-store gate",
        "measurement_limits": [
            "primary_store_bytes is measured after refresh and one-segment force merge with one primary shard and zero replicas",
            "shared-filter fixture_scope filters candidates but does not create per-scope Lucene BM25 term statistics",
            "isolated-scope indices are a separate comparison lane and must not be presented as shared-index scoring",
            "fixture is repository-owned synthetic data, not a size-equivalent copy of any target library",
            "latency is loopback single-node micro-benchmark latency, not Search API or production concurrency evidence",
            "ten legacy Gateway/token/read/index-fault cases remain structured SKIP outside this detached retrieval harness",
        ],
        "cleanup": cleanup,
        "temporary_indices": names,
    }
    validate_report(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RT-054 real loopback OpenSearch analyzer benchmark")
    parser.add_argument("--url", default="http://127.0.0.1:19200")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--index-prefix", default=f"cwk-rt054-bench-{uuid.uuid4().hex[:10]}")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--expected-runtime-version", default="")
    parser.add_argument("--container-id", default="")
    parser.add_argument("--image-ref", default="")
    parser.add_argument("--image-id", default="")
    parser.add_argument("--image-repo-digest", default="")
    parser.add_argument("--base-image-ref", default="")
    parser.add_argument("--base-image-id", default="")
    parser.add_argument("--base-image-repo-digest", default="")
    parser.add_argument("--rejected-image-ref", default="")
    parser.add_argument("--rejected-image-id", default="")
    parser.add_argument("--rejected-image-runtime-version", default="")
    parser.add_argument("--container-rss-bytes", type=int)
    parser.add_argument("--container-rss-hwm-bytes", type=int)
    parser.add_argument("--host-memory-bytes", type=int)
    parser.add_argument("--host-boundary", default="")
    args = parser.parse_args(argv)
    container_evidence = {
        key: value for key, value in {
            "container_id": args.container_id or None,
            "image_ref": args.image_ref or None,
            "image_id": args.image_id or None,
            "image_repo_digest": args.image_repo_digest or None,
            "base_image_ref": args.base_image_ref or None,
            "base_image_id": args.base_image_id or None,
            "base_image_repo_digest": args.base_image_repo_digest or None,
            "rejected_preexisting_image": ({
                "image_ref": args.rejected_image_ref,
                "image_id": args.rejected_image_id,
                "observed_runtime_version": args.rejected_image_runtime_version,
                "reason": "label/tag did not match the OpenSearch binary version",
            } if args.rejected_image_ref else None),
            "proc_1_rss_bytes_point_in_time": args.container_rss_bytes,
            "proc_1_rss_high_water_bytes": args.container_rss_hwm_bytes,
            "macos_host_memory_bytes": args.host_memory_bytes,
            "host_boundary": args.host_boundary or None,
            "rss_note": "Linux container /proc/1 VmRSS/VmHWM; not JVM heap and not macOS process RSS",
        }.items() if value is not None
    }
    report = run_benchmark(
        args.url, args.index_prefix, args.repeats, args.timeout,
        expected_runtime_version=args.expected_runtime_version,
        container_evidence=container_evidence or None,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
